#!/usr/bin/env bash
# Isolated regression tests for the host-process scheduled-task release gate.
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$ROOT_DIR/scripts/check-scheduled-task-deployment.sh"
TMP_DIR="$(mktemp -d)"
SERVER_PID=""
BACKEND_PID=""

cleanup() {
  [[ -z "$BACKEND_PID" ]] || kill "$BACKEND_PID" >/dev/null 2>&1 || true
  [[ -z "$SERVER_PID" ]] || kill "$SERVER_PID" >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

fail() {
  echo "test-check-scheduled-task-deployment: $*" >&2
  exit 1
}

expect_failure() {
  local expected="$1"
  shift
  local output
  if output="$("$@" 2>&1)"; then
    fail "expected command to fail: $*"
  fi
  [[ "$output" == *"$expected"* ]] || fail "failure output did not contain: $expected"
}

port="$((20000 + RANDOM % 10000))"
openapi_file="$TMP_DIR/openapi.json"
printf '%s\n' '{"openapi":"3.1.0","paths":{}}' > "$openapi_file"
python3 - "$port" "$openapi_file" <<'PY' >"$TMP_DIR/health.log" 2>&1 &
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys

openapi_file = sys.argv[2]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/v1/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        if self.path == "/openapi.json":
            with open(openapi_file, "rb") as source:
                payload = source.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args):
        pass

HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PY
SERVER_PID="$!"

for _ in $(seq 1 50); do
  curl -fsS "http://127.0.0.1:$port/api/v1/health" >/dev/null 2>&1 && break
  sleep 0.05
done
curl -fsS "http://127.0.0.1:$port/api/v1/health" >/dev/null || fail "health stub did not start"

pid_file="$TMP_DIR/backend.pid"
health_url="http://127.0.0.1:$port/api/v1/health"

# exec -a gives the sleeping test process the same observable command shape as
# the host Uvicorn process without launching a real application server.
bash -c 'exec -a "uvicorn app.main:app --host 0.0.0.0 --port 8001" sleep 60' &
BACKEND_PID="$!"
printf '%s\n' "$BACKEND_PID" > "$pid_file"
expect_failure "OpenAPI document is missing scheduled-task routes" \
  "$CHECK_SCRIPT" --pid-file "$pid_file" --health-url "$health_url"

printf '%s\n' '{"openapi":"3.1.0","paths":{"/api/v1/user/runtime-workspace/{runtime_workspace_id}/scheduled-tasks":{"get":{},"post":{}}}}' > "$openapi_file"
"$CHECK_SCRIPT" --pid-file "$pid_file" --health-url "$health_url"
kill "$BACKEND_PID" >/dev/null 2>&1 || true
wait "$BACKEND_PID" 2>/dev/null || true
BACKEND_PID=""

printf '999999\n' > "$pid_file"
expect_failure "scheduled scanner backend instance check failed" \
  "$CHECK_SCRIPT" --pid-file "$pid_file" --health-url "$health_url"

bash -c 'exec -a "python app.main:app --port 8001" sleep 60' &
BACKEND_PID="$!"
printf '%s\n' "$BACKEND_PID" > "$pid_file"
expect_failure "must invoke uvicorn app.main:app" \
  "$CHECK_SCRIPT" --pid-file "$pid_file" --health-url "$health_url"
kill "$BACKEND_PID" >/dev/null 2>&1 || true
wait "$BACKEND_PID" 2>/dev/null || true

bash -c 'exec -a "uvicorn app.main:app --workers 2 --port 8001" sleep 60' &
BACKEND_PID="$!"
printf '%s\n' "$BACKEND_PID" > "$pid_file"
"$CHECK_SCRIPT" --pid-file "$pid_file" --health-url "$health_url"
kill "$BACKEND_PID" >/dev/null 2>&1 || true
wait "$BACKEND_PID" 2>/dev/null || true

bash -c 'exec -a "uvicorn app.main:app --workers=1 --port 8001" sleep 60' &
BACKEND_PID="$!"
printf '%s\n' "$BACKEND_PID" > "$pid_file"
kill "$SERVER_PID" >/dev/null 2>&1 || true
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""
expect_failure "health check failed" \
  "$CHECK_SCRIPT" --pid-file "$pid_file" --health-url "$health_url"

echo "test-check-scheduled-task-deployment: passed"
