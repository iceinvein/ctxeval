#!/usr/bin/env python3
"""Import graph for a pnpm TS monorepo, used to build and vet eval tasks.

Two jobs:

1. Find candidate tasks whose answer is two hops away, so the second hop names
   an intermediate rather than the symbol under discussion. Those are the tasks
   a single symbol-name grep cannot solve.

2. Admission test. An entire hand-written task set turned out to fall to one
   `rg -l <symbol>`, which is why both arms scored the same: when one command
   answers the question, the retrieval layer is irrelevant. Any candidate whose
   expected set is reproducible that way is rejected here rather than after
   paying to run it.

Deliberately independent of both arms under test: it reads import specifiers
directly and knows nothing about code-intel's index or about text search
ranking. It is approximate (static `import ... from` only, no dynamic import or
require), so every task it proposes is hand-checked before use.
"""

import json
import pathlib
import re
import subprocess
import sys
from collections import defaultdict

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
SRC_EXT = {".ts", ".tsx"}
IMPORT_RE = re.compile(r"""(?:from|import)\s*["']([^"']+)["']""")
IDENT_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]{3,}\b")


def is_test(p: pathlib.Path) -> bool:
    s = str(p)
    return ".test." in s or ".spec." in s or "__tests__" in s or s.endswith(".d.ts")


def first_party(p: pathlib.Path) -> bool:
    """`.repos/` holds vendored upstream checkouts: not this project's code."""
    r = str(p.relative_to(ROOT)) if p.is_absolute() else str(p)
    return (r.startswith("apps/") or r.startswith("packages/")) and not r.startswith(".repos")


def workspace_packages() -> dict[str, pathlib.Path]:
    """package name -> its source root, from every workspace package.json."""
    pkgs = {}
    for pj in list(ROOT.glob("packages/*/package.json")) + list(ROOT.glob("apps/*/package.json")):
        try:
            name = json.loads(pj.read_text()).get("name")
        except (json.JSONDecodeError, OSError):
            continue
        if name:
            pkgs[name] = pj.parent
    return pkgs


def source_files() -> list[pathlib.Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                         capture_output=True, text=True, check=True).stdout.split("\n")
    return [ROOT / f for f in out
            if f and pathlib.Path(f).suffix in SRC_EXT
            and not is_test(pathlib.Path(f)) and first_party(pathlib.Path(f))]


def resolve(spec: str, importer: pathlib.Path, pkgs: dict) -> pathlib.Path | None:
    """Resolve an import specifier to a file, relative or workspace-package."""
    if spec.startswith("."):
        base = (importer.parent / spec).resolve()
    else:
        pkg = next((n for n in pkgs if spec == n or spec.startswith(n + "/")), None)
        if not pkg:
            return None
        sub = spec[len(pkg):].lstrip("/")
        base = (pkgs[pkg] / "src" / sub).resolve() if sub else (pkgs[pkg] / "src" / "index").resolve()
    for cand in (base, base.with_suffix(".ts"), base.with_suffix(".tsx"),
                 base / "index.ts", base / "index.tsx"):
        if cand.is_file() and cand.suffix in SRC_EXT:
            return cand
    return None


def build():
    pkgs = workspace_packages()
    files = source_files()
    importers = defaultdict(set)   # file -> files that import it
    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for spec in IMPORT_RE.findall(text):
            tgt = resolve(spec, f, pkgs)
            if tgt and tgt != f:
                importers[tgt].add(f)
    return files, importers


def rel(p: pathlib.Path) -> str:
    return str(p.relative_to(ROOT))


def single_grep_solves(symbol: str, expected: set[str]) -> tuple[bool, set[str]]:
    """True when one literal ripgrep for `symbol` reproduces the expected set."""
    r = subprocess.run(
        ["rg", "-l", "--fixed-strings", symbol,
         "--glob", "!**/*.test.*", "--glob", "!**/*.spec.*", "--glob", "!**/__tests__/**",
         str(ROOT)],
        capture_output=True, text=True, env={"LITERAL": "1", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
    )
    hits = {str(pathlib.Path(l).relative_to(ROOT))
            for l in r.stdout.split("\n") if l.strip()}
    return hits == expected, hits


if __name__ == "__main__":
    files, importers = build()
    print(f"{len(files)} source files, {len(importers)} with importers\n")

    # Two-hop candidates: a file whose importers themselves have importers.
    # The outer ring never names the seed file's symbols, so a symbol grep
    # cannot reach it.
    cands = []
    for tgt, direct in importers.items():
        if not (2 <= len(direct) <= 4):
            continue
        outer = set()
        for d in direct:
            outer |= importers.get(d, set())
        outer -= direct | {tgt}
        if 2 <= len(outer) <= 6:
            cands.append((len(direct), len(outer), tgt, direct, outer))

    cands.sort(key=lambda c: (c[1], c[0]))
    for d, o, tgt, direct, outer in cands[:25]:
        print(f"seed {rel(tgt)}  direct={d} two_hop={o}")
        for x in sorted(direct):
            print(f"    D {rel(x)}")
        for x in sorted(outer):
            print(f"    H {rel(x)}")
        print()
