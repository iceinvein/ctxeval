#!/usr/bin/env bash
# Retrieval-delta eval: does code-intel still earn its place on current models?
#
# One run = (task, arm, model). The arms differ only in whether code intelligence
# exists on the machine; prompt, tools, effort, repo and model are identical.
#
# The grep arm runs with a PATH where code-intel is absent. That does two things
# at once: the agent genuinely has no index to call, and the global
# prefer-code-intel PreToolUse hook stands down (it exits 0 when the binary is
# missing) instead of denying every grep. Isolating CLAUDE_CONFIG_DIR would have
# been the tidier way to drop the hook, but it also drops the keychain
# credentials and every run comes back "Not logged in".
set -uo pipefail

EVAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:?set REPO to the repository under test}"
RUNS="${RUNS:-$EVAL_DIR/runs}"
SHIM="$EVAL_DIR/nobin"
BUDGET="${BUDGET:-0.60}"
EFFORT="${EFFORT:-medium}"
TASKS="${TASKS:-$EVAL_DIR/tasks.json}"
MODELS="${MODELS:-haiku sonnet opus}"
ARMS="${ARMS:-grep codeintel free}"
REP="${REP:-1}"

mkdir -p "$RUNS"

# PATH with code-intel removed, every other homebrew tool kept.
if [ ! -d "$SHIM" ]; then
  mkdir -p "$SHIM"
  for f in "${CODE_INTEL_BIN_DIR:-/opt/homebrew/bin}"/*; do
    [ "$(basename "$f")" = "code-intel" ] || ln -sf "$f" "$SHIM/$(basename "$f")"
  done
fi
# ci-search is the same CLI under a name the hook does not look for.
CISHIM="$EVAL_DIR/cishim"
if [ ! -x "$CISHIM/ci-search" ]; then
  mkdir -p "$CISHIM"
  printf '#!/bin/sh\nexec %s "$@"\n' "$(command -v code-intel)" > "$CISHIM/ci-search"
  chmod +x "$CISHIM/ci-search"
fi

NOCI_PATH=$(echo "$PATH" | tr ':' '\n' | sed "s#^${CODE_INTEL_BIN_DIR:-/opt/homebrew/bin}\$#$SHIM#" | paste -sd: -)

SCHEMA='{"type":"object","properties":{"files":{"type":"array","items":{"type":"string"}}},"required":["files"],"additionalProperties":false}'

read -r -d '' BASE_SYS <<'EOF' || true
You are locating code in a repository. Answer only with the repo-relative paths
of the source files that implement the described behaviour.

Rules:
- Paths are relative to the repository root, e.g. packages/backend/src/app.ts
- Name only files that genuinely implement the behaviour. Do not pad the list.
- Exclude test files.
- Stop searching once you can answer. Do not read files you do not need.
EOF

# EXHAUSTIVE=1 swaps the stopping instruction for its opposite. Everything else
# about the run is unchanged, so the difference isolates stopping discipline from
# retrieval capability.
if [ "${EXHAUSTIVE:-0}" = "1" ]; then
read -r -d '' BASE_SYS <<'EOF' || true
You are locating code in a repository. Answer only with the repo-relative paths
of the source files that implement the described behaviour.

Rules:
- Paths are relative to the repository root, e.g. packages/backend/src/app.ts
- Name only files that genuinely implement the behaviour. Do not pad the list.
- Exclude test files.
- Be exhaustive. Every file that participates must appear. Before answering,
  check that you have found all of them: a partial list is a wrong answer.
EOF
fi

read -r -d '' GREP_SYS <<'EOF' || true

No code-intelligence index is available on this machine. Use text search and
file reading.
EOF

read -r -d '' FREE_SYS <<'EOF' || true

Two ways of searching this repository are available and you may use either,
both, or neither as you see fit.

  Text search:  ripgrep, and the Grep and Glob tools.
  Code index:   the ci-search CLI, which carries definitions, references
                and call structure. Pass --json for machine-readable output.

    ci-search search --repo . --context snippets --json "query"
    ci-search definition|references|call-hierarchy --repo . --json SYMBOL
    ci-search repo-map --repo . --json

Neither is preferred. Choose whichever you judge will answer the question.
EOF

read -r -d '' CI_SYS <<'EOF' || true

A local code-intelligence index of this repository is available via the
`code-intel` CLI. Prefer it over text search for symbols, definitions,
references and structure. `--json` is the output contract.

  code-intel search --repo . --context snippets --json "query"
  code-intel hydrate --repo . --ids sym_1,sym_2 --json
  code-intel ask --repo . --json "question"
  code-intel definition|references|call-hierarchy --repo . --json SYMBOL
  code-intel repo-map --repo . --json

Exit code 5 means no results: that is an answer, not a reason to fall back.
EOF

# ONLY="id1 id2" restricts the run; unset runs every task.
ONLY="${ONLY:-}"

jq -c '.[]' "$TASKS" | while read -r task; do
  id=$(jq -r '.id' <<<"$task")
  prompt=$(jq -r '.prompt' <<<"$task")

  if [ -n "$ONLY" ] && ! grep -qw "$id" <<<"$ONLY"; then
    continue
  fi

  for model in $MODELS; do
    for arm in $ARMS; do
      out="$RUNS/${id}__${arm}__${model}__r${REP}.json"
      [ -s "$out" ] && { echo "skip  $id $arm $model"; continue; }

      case "$arm" in
        codeintel) sys="$BASE_SYS$CI_SYS";   runpath="$PATH" ;;
        free)      sys="$BASE_SYS$FREE_SYS"; runpath="$NOCI_PATH:$CISHIM" ;;
        *)         sys="$BASE_SYS$GREP_SYS"; runpath="$NOCI_PATH" ;;
      esac

      printf 'run   %-24s %-10s %-7s r%s ' "$id" "$arm" "$model" "$REP"
      start=$(date +%s)
      ( cd "$REPO" && PATH="$runpath" claude -p "$prompt" \
          --model "$model" \
          --effort "$EFFORT" \
          --output-format json \
          --json-schema "$SCHEMA" \
          --append-system-prompt "$sys" \
          --allowedTools Read Grep Glob Bash \
          --permission-mode bypassPermissions \
          --max-budget-usd "$BUDGET" \
      ) >"$out" 2>"$out.err" </dev/null
      # </dev/null matters: claude reads stdin, and without this it swallows the
      # rest of the jq task stream feeding the enclosing while-read loop, so only
      # the first task ever runs.
      rc=$?
      elapsed=$(( $(date +%s) - start ))

      if [ -s "$out" ] && jq -e '.is_error == false' "$out" >/dev/null 2>&1; then
        echo "ok ${elapsed}s"
      else
        echo "FAILED rc=$rc $(jq -r '.result // "no output"' "$out" 2>/dev/null | head -c 120)"
      fi
      [ -s "$out" ] && jq --argjson e "$elapsed" --arg a "$arm" --argjson r "$REP" \
        '. + {wall_s: $e, arm: $a, rep: $r}' "$out" >"$out.tmp" && mv "$out.tmp" "$out"
    done
  done
done

echo
echo "score with: python3 $EVAL_DIR/score.py"
