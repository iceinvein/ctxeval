# ctxeval

An admission protocol for code-retrieval evaluation tasks, and a small harness
that runs them.

Most published comparisons of code-retrieval strategies measure tasks that
cannot tell the strategies apart. This repository is the screening step that
catches those tasks before you pay to run them, plus the runner and scorer used
to produce the results in
[Is Your Code Context Good? The Benchmarks Can't Tell You.](https://dikrana.dev/blog/code-context-benchmarks-cant-tell/)

Every rule below exists because a task violated it and the violation cost real
money to discover. Across 32 hand-written candidate tasks on two repositories,
214 agent runs and $85, exactly zero survived all eight rules on a current
frontier model.

## The problem

You want to know whether a code-intelligence index beats plain text search for
your agent. So you write localisation tasks: describe a behaviour, ask which
files implement it, score recall against an answer key.

Then one command finds the answer:

```
$ rg -l dedupeProviderSkillsByName
packages/client-runtime/src/providerSkills.ts
apps/web/src/providerSkillSearch.ts
apps/mobile/src/features/threads/use-composer-command-menu.ts
```

That is the expected set, exactly. Both arms score full marks, the comparison
measures nothing, and the number you publish is noise. Four of five tasks in one
of my own sets failed this way, including the one I had hand-picked as the most
vocabulary-disjoint.

## The eight rules

Four run for free. Four need a cheap pilot.

| Rule | Rejects a task when | Cost of learning it |
|------|--------------------|---------------------|
| **R1** | An expected file no longer exists at HEAD | A rename silently breaks the task |
| **R2** | An expected file's only change is inside a test block | 6 runs marked wrong for obeying "exclude test files" |
| **R3** | One literal search reproduces the expected set | An entire five-task set, ~$20 |
| **R4** | One word of the asker's own vocabulary locates the answer | A prompt killed by the word "assembles": 14 hits, 100% of the answer |
| **R5** | Repeated runs disagree with each other | 19 of 24 runs named a different, defensible file |
| **R6** | Both arms saturate at 1.00 | 8 tasks across three rounds |
| **R6b** | Both arms score identically at any level | A task both arms answered 0.67 on, every run |
| **R7** | Runs agree on an answer that is not yours | 75% agreement at 0.00 recall against the key |

Two are worth expanding.

**R4 is the positive form of R3.** It does not reject a task because prompt
words appear in the files, since words like `active` or `command` appear in
hundreds of files and therefore help nobody find anything. It rejects a task
when a *discriminating* word, one whose search returns a short list already
containing the answer, appears. Hand-written prompts leak vocabulary in ways
that are invisible to the person writing them.

**R7 is the one most worth stealing.** R5 checks that repeated runs agree with
each other, which is necessary and not sufficient: runs can agree perfectly on
an answer that is not the recorded one. Inter-run agreement and correctness are
different axes, and only both together distinguish a hard task from a wrong
answer key.

| | High recall | Low recall |
|---|---|---|
| **High agreement** | good task | your ground truth is wrong (R7) |
| **Low agreement** | unstable | ambiguous prompt (R5) |

### The rule that has to be symmetric

R6 rejects a task only when **both** arms saturate. The tempting version,
"reject tasks the baseline already solves", leaves only tasks the baseline
fails, and the thing you are testing wins by construction. That is selecting on
the dependent variable. Keep the cull neutral between arms or the result is
decided before you run it.

## Quickstart

```bash
git clone https://github.com/iceinvein/code_intelligence_mcp_server /tmp/demo-repo
python3 admit.py demo-tasks.json /tmp/demo-repo admitted.json
```

That runs R1 to R4, costs nothing, and needs no model. Output on the bundled
demo set:

```
=== daemon-unreachable-reason
  FAIL  R3 no single grep    `rg -l daemon_recovery_hint` returns exactly the expected set
  FAIL  R4 no vocab shortcut advice (3 hits, 100% of answer); reinstall (4 hits, 50% of answer)
  -> REJECTED

=== grace-period-setting
  FAIL  R3 no single grep    `rg -l default_exclude_patterns` returns exactly the expected set
  -> REJECTED

=== orphaned-rows-after-replacement
  PASS  R1 files exist       all present
  PASS  R2 not test-only     all have real changes
  PASS  R3 no single grep    no single identifier of 43 shared reproduces it
  PASS  R4 no vocab shortcut 20 prompt words, none discriminating
  -> ADMITTED

1/5 admitted
```

Five plausible tasks written by hand against a public repository, four of them
rejected. That ratio is normal. It is the finding, not a setback.

### Running the pilot (R5 to R7)

R5, R6 and R7 need repeated runs, which cost money. The harness drives Claude
Code headless:

```bash
REPO=/tmp/demo-repo TASKS=admitted.json RUNS=./runs MODELS=sonnet \
  bash run.sh                      # rep 1
REPO=/tmp/demo-repo TASKS=admitted.json RUNS=./runs MODELS=sonnet REP=2 \
  bash run.sh                      # rep 2

python3 -c "
import json, pathlib, sys; sys.path.insert(0, '.')
from admit import pilot_verdict
for t in json.loads(pathlib.Path('admitted.json').read_text()):
    print(pilot_verdict(t, 'runs'), t['id'])
"
```

Then score:

```bash
TASKS=admitted.json RUNS=./runs python3 score.py
```

Run at least three repeats per cell. At one run per cell my headline effect and
my run-to-run noise were both 0.33, one file out of three: a finding at n=1 and
a coin flip at n=3.

## The two arms

`run.sh` compares an agent with and without a code-intelligence index by
manipulating `PATH`. The index arm runs normally; the baseline arm runs with a
`PATH` where the `code-intel` binary is absent, built from a symlink farm so
every other tool survives.

That does two things at once: the agent genuinely has no index to call, and any
hook that would redirect text search to the index stands down, because the
binary it checks for is missing. Isolating `CLAUDE_CONFIG_DIR` would be tidier
but also drops the credentials, and every run comes back "Not logged in".

Set `CODE_INTEL_BIN_DIR` if your binary is not in `/opt/homebrew/bin`.

**This is not a neutral A/B.** If you run a hook that pushes the agent toward
the index, the index arm is pushed rather than choosing. That measures your
configuration, which is a legitimate thing to measure, but say so.

## Files

| | |
|---|---|
| `admit.py` | The eight rules. R1-R4 are free; `pilot_verdict()` applies R5-R7 to pilot runs |
| `run.sh` | Runs one task, one arm, one model, one repeat. Writes raw CLI JSON |
| `score.py` | Recall, precision, turns, token split with cache reads separated, per-model deltas |
| `modgraph.py` | Import graph for a pnpm TypeScript monorepo, for finding two-hop task candidates |
| `demo-tasks.json` | Five hand-written tasks against a public repository, four of which fail |

## Known limitations

**R3 over-rejects.** It tests every identifier shared by the expected files, so
an incidental shared string will kill an otherwise good task. One of mine died
on a test-fixture timestamp no agent would ever search for. Conservative in the
right direction, but check what it rejected on before you delete the task.

**The task sets that survive are adversarial by construction.** R6 removes what
both arms find easy, so what remains is harder than real work. Any delta
measured on survivors is an upper bound on what you would see day to day, not
an estimate of it.

**`modgraph.py` is approximate.** Static `import ... from` only. No dynamic
imports, no `require`. It proposes candidates; check them by hand.

**Scoring is file-level localisation, not task completion.** A task can be
localised perfectly and still fail to be fixed correctly.

## Prior art

[ContextBench](https://arxiv.org/abs/2602.05892) publishes admission criteria
that overlap these, and is a far larger effort: 1,136 tasks, 66 repositories,
four months of expert annotation. It removes semantically trivial tasks and
high-solvability ones, which is R6. It does not report variance across repeated
runs, and neither does [CORE-Bench](https://arxiv.org/abs/2606.11864). The
single-grep test (R3) and the vocabulary-shortcut test (R4) are the parts I have
not found published elsewhere.

## Licence

MIT.
