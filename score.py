#!/usr/bin/env python3
"""Score the retrieval-delta eval.

Recall is the headline: did the agent find the files that a real change to this
behaviour touched? Precision is reported but not weighted, because a file the
ground truth omits may still be legitimately relevant.
"""

import json
import pathlib
import re
import sys
from collections import defaultdict

import os
EVAL = pathlib.Path(__file__).parent
TASKS_FILE = pathlib.Path(os.environ.get("TASKS", EVAL / "tasks.json"))
RUNS_DIR = pathlib.Path(os.environ.get("RUNS", EVAL / "runs"))
TASKS = {t["id"]: t for t in json.loads(TASKS_FILE.read_text())}
RUN_RE = re.compile(r"^(?P<task>.+?)__(?P<arm>grep|codeintel|free)__(?P<model>[a-z]+)(?:__r(?P<rep>\d+))?\.json$")


def norm(p: str) -> str:
    p = p.strip().lstrip("./")
    prefix = os.environ.get("STRIP_PREFIX", "")
    if prefix and p.startswith(prefix):
        p = p[len(prefix):]
    return p


def answer_files(blob: dict) -> list[str]:
    """The CLI puts the schema-validated object in `result`, as dict or string."""
    r = blob.get("result")
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", r, re.S)
            r = json.loads(m.group(0)) if m else {}
    if not isinstance(r, dict):
        return []
    return [norm(f) for f in r.get("files", []) if isinstance(f, str)]


rows = []
errored = []
for path in sorted(RUNS_DIR.glob("*.json")):
    m = RUN_RE.match(path.name)
    if not m:
        continue
    try:
        blob = json.loads(path.read_text())
    except json.JSONDecodeError:
        print(f"unparseable: {path.name}", file=sys.stderr)
        continue

    # A run the budget cap killed has no answer. Scoring it as zero recall
    # blames the model for my spend limit, so it is excluded and listed instead.
    if blob.get("is_error"):
        errored.append((m["task"], m["model"], m["arm"],
                        blob.get("total_cost_usd", 0.0) or 0.0))
        continue

    task = TASKS[m["task"]]
    expected = {norm(f) for f in task["expected"]}
    found = set(answer_files(blob))
    hit = expected & found

    u = blob.get("usage", {}) or {}
    rows.append({
        "task": m["task"],
        "difficulty": task["difficulty"],
        "arm": m["arm"],
        "model": m["model"],
        "recall": len(hit) / len(expected) if expected else 0.0,
        "precision": len(hit) / len(found) if found else 0.0,
        "missed": sorted(expected - found),
        "extra": sorted(found - expected),
        "turns": blob.get("num_turns", 0),
        "cost": blob.get("total_cost_usd", 0.0) or 0.0,
        "wall_s": blob.get("wall_s", 0),
        "in_tok": u.get("input_tokens", 0),
        "out_tok": u.get("output_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0),
        "cache_write": u.get("cache_creation_input_tokens", 0),
    })

if not rows:
    sys.exit("no runs scored")

MODEL_ORDER = ["haiku", "sonnet", "opus"]


def agg(rs, key):
    return sum(r[key] for r in rs) / len(rs)


print(f"\n{'model':8} {'arm':10} {'n':>2}  {'recall':>6} {'prec':>5} "
      f"{'turns':>5} {'in_tok':>8} {'cache_rd':>9} {'out':>6} {'$':>7} {'wall':>5}")
print("-" * 82)

by = defaultdict(list)
for r in rows:
    by[(r["model"], r["arm"])].append(r)

deltas = {}
for model in MODEL_ORDER:
    for arm in ("grep", "codeintel", "free"):
        rs = by.get((model, arm))
        if not rs:
            continue
        print(f"{model:8} {arm:10} {len(rs):>2}  {agg(rs,'recall'):>6.2f} "
              f"{agg(rs,'precision'):>5.2f} {agg(rs,'turns'):>5.1f} "
              f"{agg(rs,'in_tok'):>8.0f} {agg(rs,'cache_read'):>9.0f} "
              f"{agg(rs,'out_tok'):>6.0f} {agg(rs,'cost'):>7.4f} {agg(rs,'wall_s'):>5.0f}")
    g, c = by.get((model, "grep")), by.get((model, "codeintel"))
    if g and c:
        deltas[model] = {
            "recall": agg(c, "recall") - agg(g, "recall"),
            "turns": agg(c, "turns") - agg(g, "turns"),
            "cost": agg(c, "cost") - agg(g, "cost"),
        }
    print()

if deltas:
    print("retrieval delta (code-intel minus grep)")
    print("-" * 82)
    print(f"{'model':8} {'d_recall':>9} {'d_turns':>8} {'d_cost':>9}")
    for model in MODEL_ORDER:
        d = deltas.get(model)
        if d:
            print(f"{model:8} {d['recall']:>+9.2f} {d['turns']:>+8.1f} {d['cost']:>+9.4f}")

print("\nper-task recall")
print("-" * 82)
print(f"{'task':22} {'diff':7} " + " ".join(f"{m[:3]}:{a[:4]:>5}"
      for m in MODEL_ORDER for a in ("grep", "codeintel")))
for tid, t in TASKS.items():
    cells = []
    for m in MODEL_ORDER:
        for a in ("grep", "codeintel"):
            rs = [r for r in rows if r["task"] == tid and r["model"] == m and r["arm"] == a]
            if rs:
                mean = sum(x["recall"] for x in rs) / len(rs)
                spread = max(x["recall"] for x in rs) - min(x["recall"] for x in rs)
                cells.append(f"{mean:>6.2f}{'*' if spread > 0.01 else ' '}  ")
            else:
                cells.append(f"{'-':>9}")
    print(f"{tid:22} {t['difficulty']:7} " + " ".join(cells))

print("\nmisses")
print("-" * 82)
for r in sorted(rows, key=lambda r: (r["task"], r["model"], r["arm"])):
    if r["missed"]:
        print(f"{r['task']:22} {r['model']:7} {r['arm']:10} missed: {', '.join(r['missed'])}")

if errored:
    print("\nexcluded (no answer produced)")
    print("-" * 82)
    for t, mo, a, c in errored:
        print(f"{t:24} {mo:7} {a:10} no answer (${c:.2f}, budget cap)")

total = sum(r["cost"] for r in rows) + sum(c for *_, c in errored)
print(f"\n{len(rows)} runs, total ${total:.4f}")
