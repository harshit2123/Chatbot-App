#!/usr/bin/env bash
# Stop hook: run the backend suite, but only when backend files actually changed.
# Stop fires on every turn including read-only ones; the git guard keeps a 98-test
# run from firing when nothing backend-side was touched.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 0

git status --porcelain -- backend worker | grep -q . || exit 0

out="$(cd backend && .venv/bin/python -m pytest -q 2>&1)"
status=$?
summary="$(printf '%s\n' "$out" | tail -n 1)"

if [ "$status" -eq 0 ]; then
  # A skip here is a false green: suites create their own databases rather than
  # skipping, so "skipped" means Postgres is down or conftest changed.
  case "$summary" in
    *skipped*)
      printf '{"systemMessage":"backend tests SKIPPED tests — %s (is Postgres up?)"}\n' "$summary"
      ;;
    *)
      printf '{"systemMessage":"backend tests pass — %s","suppressOutput":true}\n' "$summary"
      ;;
  esac
else
  fails="$(printf '%s\n' "$out" | grep -E '^(FAILED|ERROR)' | head -n 10)"
  printf '%s' "$out" | tail -n 40 >&2
  python3 - "$summary" "$fails" <<'PY'
import json, sys
print(json.dumps({
    "systemMessage": "backend tests FAILED — " + sys.argv[1],
    "hookSpecificOutput": {
        "hookEventName": "Stop",
        "additionalContext": "Backend pytest failed:\n" + sys.argv[2],
    },
}))
PY
fi
