#!/usr/bin/env python3
"""Admission tests for retrieval-eval tasks.

Every rule here was learned by shipping a task that violated it and paying to
find out. A task that fails any of them cannot measure retrieval, so it is
rejected before it costs anything to run.

  R1  every expected file exists at HEAD
      (a rename since the source commit silently breaks the task)

  R2  no expected file's change is test-only
      Cost: six runs marked wrong for obeying "exclude test files", because
      an expected file changed only inside an inline `mod tests` block and a filename filter
      does not catch an inline test module.

  R3  no single literal search reproduces the expected set
      Cost: an entire five-task set that scored ~0.95 in both arms. When
      `rg -l <symbol>` returns exactly the answer, the task
      measures nothing about retrieval architecture: both arms trivially win.
      Checked over identifiers shared by the expected files AND over module
      specifiers, since importers are found by either.

  R4  the prompt's distinctive words do not appear in the expected files
      This is the positive form of R3. If the question can be answered by
      matching the asker's vocabulary, text search is already optimal and there
      is no headroom for an index to win. The regime worth measuring is where
      the user says "cooldown after repeated failures" and the code says
      "breaker", "latch", "probe".

  R5  (run separately, costs money) independent pilot runs agree
      Cost: the one task that ever appeared to separate the arms. Nineteen of
      twenty-four runs named a different but entirely defensible file, because
      the prompt admitted both readings. They were reading it correctly and the
      ground truth was answering a different question. Disagreement between
      runs means an ambiguous prompt, not a model failure.
"""

import json
import pathlib
import re
import subprocess
import sys

IDENT_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]{4,}\b")
STOPWORDS = {
    "the", "that", "this", "which", "where", "when", "with", "from", "into",
    "identify", "source", "files", "file", "behaviour", "behavior", "implement",
    "implements", "implemented", "repository", "relative", "paths", "before",
    "after", "their", "there", "would", "could", "should", "every", "other",
    "about", "under", "while", "being", "these", "those", "than", "then",
    "instead", "rather", "again", "still", "does", "each", "both", "same",
}


def sh(args: list[str], root: pathlib.Path) -> str:
    """ripgrep, with the LITERAL escape the prefer-code-intel hook honours."""
    r = subprocess.run(args, capture_output=True, text=True, cwd=root,
                       env={"LITERAL": "1", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
    return r.stdout


def rg_files(pattern: str, root: pathlib.Path) -> set[str]:
    out = sh(["rg", "-l", "--fixed-strings", pattern,
              "--glob", "!**/*.test.*", "--glob", "!**/*.spec.*",
              "--glob", "!**/__tests__/**", "--glob", "!.repos/**", "."], root)
    return {l.strip().lstrip("./") for l in out.split("\n") if l.strip()}


def r1_files_exist(task, root):
    missing = [f for f in task["expected"] if not (root / f).is_file()]
    return (not missing), f"missing at HEAD: {missing}" if missing else "all present"


def r2_not_test_only(task, root):
    """Each expected file must have a non-test change in the source commit."""
    sha = task.get("source_commit")
    if not sha:
        return True, "no source commit to check"
    bad = []
    for f in task["expected"]:
        diff = subprocess.run(
            ["git", "-C", str(root), "show", "--format=", "--unified=0", sha, "--", f],
            capture_output=True, text=True).stdout
        added = [l for l in diff.split("\n") if l.startswith(("+", "-"))
                 and not l.startswith(("+++", "---"))]
        if added and all(_inside_test_block(diff, l) for l in added):
            bad.append(f)
    return (not bad), f"test-only changes: {bad}" if bad else "all have real changes"


def _inside_test_block(diff: str, line: str) -> bool:
    """Crude: an inline Rust/TS test module hunk header names the test block."""
    for hunk in diff.split("@@"):
        if line in hunk:
            return bool(re.search(r"mod\s+\w*test|describe\(|it\(", hunk))
    return False


def r3_no_single_grep(task, root):
    """Reject when one literal search reproduces the expected set exactly."""
    expected = set(task["expected"])
    shared = None
    for f in task["expected"]:
        idents = set(IDENT_RE.findall((root / f).read_text(errors="ignore")))
        shared = idents if shared is None else (shared & idents)
    for tok in sorted(shared or (), key=len, reverse=True)[:60]:
        if rg_files(tok, root) == expected:
            return False, f"`rg -l {tok}` returns exactly the expected set"
    return True, f"no single identifier of {len(shared or ())} shared reproduces it"


def r4_no_vocabulary_shortcut(task, root):
    """Reject when one word of the asker's own vocabulary locates the answer.

    Not "no prompt word appears in the files": words like `active` or `command`
    are unavoidable and appear in hundreds of files, which is precisely why they
    do not help anyone find anything. What kills a task is a *discriminating*
    word - one whose search returns a short list that already contains the
    answer. Then the asker has named the code's own term and text search is
    optimal by construction, leaving no headroom for an index.
    """
    words = {w.lower() for w in re.findall(r"[a-zA-Z]{5,}", task["prompt"])} - STOPWORDS
    expected = set(task["expected"])
    shortcuts = []
    for w in sorted(words):
        hits = rg_files(w, root)
        if not hits or len(hits) > 25:
            continue                      # too broad to be a shortcut
        covered = len(hits & expected) / len(expected)
        if covered >= 0.5:
            shortcuts.append(f"{w} ({len(hits)} hits, {covered:.0%} of answer)")
    return (not shortcuts), ("; ".join(shortcuts[:4]) if shortcuts
                             else f"{len(words)} prompt words, none discriminating")


RULES = [("R1 files exist", r1_files_exist),
         ("R2 not test-only", r2_not_test_only),
         ("R3 no single grep", r3_no_single_grep),
         ("R4 no vocab shortcut", r4_no_vocabulary_shortcut)]


def main():
    tasks = json.loads(pathlib.Path(sys.argv[1]).read_text())
    root = pathlib.Path(sys.argv[2]).resolve()
    admitted = []
    for t in tasks:
        print(f"\n=== {t['id']}")
        ok_all = True
        for name, fn in RULES:
            try:
                ok, detail = fn(t, root)
            except Exception as e:                      # a rule that cannot run
                ok, detail = False, f"rule errored: {e}"  # is not a pass
            ok_all &= ok
            print(f"  {'PASS' if ok else 'FAIL'}  {name:20} {detail}")
        print(f"  -> {'ADMITTED' if ok_all else 'REJECTED'}")
        if ok_all:
            admitted.append(t)
    print(f"\n{len(admitted)}/{len(tasks)} admitted")
    if len(sys.argv) > 3:
        pathlib.Path(sys.argv[3]).write_text(json.dumps(admitted, indent=2) + "\n")
        print(f"wrote {sys.argv[3]}")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Pilot-based rules. These cost money, so they run after R1-R4 have culled the
# free rejections. Usage: pilot_verdict(task, runs_dir)
# ---------------------------------------------------------------------------

def _answer(blob):
    r = blob.get("result")
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", r, re.S)
            try:
                r = json.loads(m.group(0)) if m else {}
            except json.JSONDecodeError:
                return None
    if not isinstance(r, dict):
        return None
    return frozenset(f.strip().lstrip("./") for f in r.get("files", [])
                     if isinstance(f, str))


def pilot_verdict(task, runs_dir):
    """R5 (agreement) and R6 (headroom) from pilot runs.

    R5  The modal answer must appear in at least half the runs. Runs that keep
        substituting *different plausible* files are reporting that the prompt
        is ambiguous; the models are not failing, the question is.

    R6  Both arms must not saturate. A task every arm answers perfectly cannot
        discriminate between them.

        The rule is symmetric on purpose. Rejecting tasks the grep arm already
        solves would be selecting on the dependent variable: keep only what grep
        fails and the index wins by construction. Requiring *both* arms to
        saturate before rejecting keeps the cull neutral between them.
    """
    runs_dir = pathlib.Path(runs_dir)
    expected = set(task["expected"])
    per_arm, answers = {}, []
    for p in sorted(runs_dir.glob(f"{task['id']}__*.json")):
        blob = json.loads(p.read_text())
        if blob.get("is_error"):
            continue
        got = _answer(blob)
        if got is None:
            continue
        arm = blob.get("arm", "?")
        answers.append(got)
        per_arm.setdefault(arm, []).append(len(expected & got) / len(expected))

    if len(answers) < 2:
        return False, "too few usable pilot runs"

    modal = max(set(answers), key=answers.count)
    agree = answers.count(modal) / len(answers)
    if agree < 0.5:
        return False, (f"R5 ambiguous: modal answer in {answers.count(modal)}/"
                       f"{len(answers)} runs ({len(set(answers))} distinct)")

    means = {a: sum(v) / len(v) for a, v in per_arm.items()}
    if means and all(m >= 0.999 for m in means.values()):
        return False, f"R6 saturated: every arm at 1.00 ({means})"

    # R6b. Saturation is only the obvious way to carry no signal. A task where
    # the arms score *identically* discriminates just as poorly, whatever the
    # level. One task returned 0.67 from both arms on all four runs:
    # headroom by R6's original wording, and zero information about the
    # difference between them.
    if len(means) > 1 and (max(means.values()) - min(means.values())) < 0.02:
        return False, (f"R6b arms indistinguishable: spread "
                       f"{max(means.values()) - min(means.values()):.2f} ({means})")

    # R7. High agreement plus zero recall does not mean a hard task: it means
    # the runs concur on a coherent answer that is not the one recorded, i.e.
    # the ground truth points at the wrong thing. R5 alone scores this as a
    # pass, because the runs do agree - with each other, just not with me.
    # Cost: a task where all four runs converged on one subsystem
    # while the ground truth named another. Both existed; the prompt never
    # said which.
    # Generalised: the tell is not a zero score, it is consensus on an answer
    # that is not the recorded one. One task returned exactly 0.33 on all nine
    # runs - flawless agreement at one-of-three - and the original 0.00-only
    # form of this rule waved it through.
    modal_recall = len(expected & modal) / len(expected)
    if agree >= 0.6 and modal_recall < 0.5:
        return False, (f"R7 ground truth suspect: {agree:.0%} of runs agree on an "
                       f"answer scoring only {modal_recall:.2f} against it")

    return True, f"R5 agreement {agree:.0%}, R6/R7 headroom (means {means})"
