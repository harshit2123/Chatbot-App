#!/usr/bin/env bash
# PostToolUse(Write|Edit): type-check the frontend after a frontend source edit.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

f="$(jq -r '.tool_input.file_path // .tool_response.filePath // empty')"
case "$f" in
  *"/frontend/"*.ts|*"/frontend/"*.tsx) ;;
  *) exit 0 ;;
esac

cd "$ROOT/frontend" || exit 0
[ -x node_modules/.bin/tsc ] || exit 0

out="$(node_modules/.bin/tsc -b 2>&1)"
[ $? -eq 0 ] && exit 0

python3 - "$out" <<'PY'
import json, sys
errs = "\n".join(sys.argv[1].splitlines()[:20])
print(json.dumps({
    "systemMessage": "frontend tsc reported type errors",
    "hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "tsc -b failed:\n" + errs,
    },
}))
PY
