#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

# Read-only release gate for one Backend instance. Database execution leases
# make multiple host processes/Uvicorn workers safe; this checker verifies the
# explicitly selected instance and its API surface.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${SCHEDULED_TASK_BACKEND_PID_FILE:-$ROOT_DIR/.runtime/public-dev/backend.pid}"
HEALTH_URL="${SCHEDULED_TASK_BACKEND_HEALTH_URL:-}"

usage() {
  cat >&2 <<EOF
usage: $0 [--pid-file <backend-pid-file>] [--health-url <http://host:port/api/v1/health>]

Defaults:
  --pid-file   \$SCHEDULED_TASK_BACKEND_PID_FILE or .runtime/public-dev/backend.pid
  --health-url \$SCHEDULED_TASK_BACKEND_HEALTH_URL or the configured Uvicorn port on 127.0.0.1
EOF
}

fail() {
  echo "scheduled scanner backend instance check failed" >&2
  if [[ $# -gt 0 ]]; then
    echo "$1" >&2
  fi
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pid-file)
      [[ $# -ge 2 && -n "${2:-}" ]] || { usage; fail "missing PID file"; }
      PID_FILE="$2"
      shift 2
      ;;
    --health-url)
      [[ $# -ge 2 && -n "${2:-}" ]] || { usage; fail "missing health URL"; }
      HEALTH_URL="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage
      fail "unknown argument: $1"
      ;;
  esac
done

[[ -f "$PID_FILE" ]] || fail "backend PID file is missing: $PID_FILE"
pid="$(<"$PID_FILE")"
[[ "$pid" =~ ^[1-9][0-9]*$ ]] || fail "backend PID file must contain one positive PID: $PID_FILE"
kill -0 "$pid" >/dev/null 2>&1 || fail "backend PID is not running: $pid"

# ps reads only the requested PID and never mutates its state.  It is used
# instead of broad process matching, so an unrelated Uvicorn process cannot
# satisfy this gate.
command_args="$(ps -p "$pid" -o args= 2>/dev/null)" || fail "could not read backend process command: $pid"
[[ "$command_args" =~ (^|[[:space:]/])uvicorn([[:space:]]|$) ]] \
  && [[ "$command_args" =~ app\.main:app([[:space:]]|$) ]] \
  || fail "configured backend PID must invoke uvicorn app.main:app"

# Accept any positive worker count. Each worker owns a scanner, while the
# database claim/heartbeat/fencing protocol elects exactly one execution owner.
read -r -a command_tokens <<< "$command_args"
for ((index = 0; index < ${#command_tokens[@]}; index += 1)); do
  case "${command_tokens[$index]}" in
    --workers)
      ((index + 1 < ${#command_tokens[@]})) || fail "uvicorn --workers requires a value"
      [[ "${command_tokens[$((index + 1))]}" =~ ^[1-9][0-9]*$ ]] || fail "uvicorn --workers must be a positive integer"
      index=$((index + 1))
      ;;
    --workers=*)
      [[ "${command_tokens[$index]#--workers=}" =~ ^[1-9][0-9]*$ ]] || fail "uvicorn --workers must be a positive integer"
      ;;
  esac
done

if [[ -z "$HEALTH_URL" ]]; then
  backend_port=""
  for ((index = 0; index < ${#command_tokens[@]}; index += 1)); do
    case "${command_tokens[$index]}" in
      --port)
        ((index + 1 < ${#command_tokens[@]})) || fail "Uvicorn --port requires a value"
        backend_port="${command_tokens[$((index + 1))]}"
        break
        ;;
      --port=*)
        backend_port="${command_tokens[$index]#--port=}"
        break
        ;;
    esac
  done
  backend_port="${backend_port:-${PORT_BACKEND:-8011}}"
  [[ "$backend_port" =~ ^[1-9][0-9]*$ ]] || fail "could not determine a valid Uvicorn port"
  HEALTH_URL="http://127.0.0.1:$backend_port/api/v1/health"
fi

[[ "$HEALTH_URL" =~ ^https?://[^[:space:]]+/api/v1/health([?#].*)?$ ]] \
  || fail "health URL must target /api/v1/health"
curl --fail --silent --show-error --max-time 5 "$HEALTH_URL" >/dev/null \
  || fail "health check failed: $HEALTH_URL"

# The health endpoint only proves that *a* backend is reachable.  Scheduled
# task creation must also be exposed by that same backend process.  Strip the
# allowed query/fragment from the validated health URL, then replace only its
# fixed path so the OpenAPI request stays on the same scheme, host and port.
health_url_without_query_or_fragment="${HEALTH_URL%%[?#]*}"
OPENAPI_URL="${health_url_without_query_or_fragment%/api/v1/health}/openapi.json"
SCHEDULED_TASK_COLLECTION_ROUTE="/api/v1/user/runtime-workspace/{runtime_workspace_id}/scheduled-tasks"
if ! curl --fail --silent --show-error --max-time 5 "$OPENAPI_URL" \
  | python3 -c '
import json
import sys

route = sys.argv[1]
try:
    document = json.load(sys.stdin)
except (json.JSONDecodeError, TypeError):
    raise SystemExit(1)

paths = document.get("paths")
methods = paths.get(route) if isinstance(paths, dict) else None
if not isinstance(methods, dict) or not {"get", "post"}.issubset(methods):
    raise SystemExit(1)
' "$SCHEDULED_TASK_COLLECTION_ROUTE"; then
  fail "OpenAPI document is missing scheduled-task routes (GET and POST $SCHEDULED_TASK_COLLECTION_ROUTE): $OPENAPI_URL"
fi

echo "scheduled task host-process check: ok (pid=$pid pid_file=$PID_FILE health_url=$HEALTH_URL openapi_url=$OPENAPI_URL)"
