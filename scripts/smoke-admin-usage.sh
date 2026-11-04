#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PID=""

cleanup() {
  if [[ -n "$BACKEND_PID" ]]; then
    kill "$BACKEND_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

wait_for_url() {
  local url="$1"
  for _ in $(seq 1 60); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "smoke-admin-usage: timed out waiting for $url" >&2
  return 1
}

BACKEND_PORT="${BACKEND_PORT:-18002}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! curl -fsS "http://127.0.0.1:${BACKEND_PORT}/api/v1/health" >/dev/null 2>&1; then
  (
    cd "$ROOT/backend"
    RUNTIME_CHAT_MODE="${RUNTIME_CHAT_MODE:-contract}" HERMES_RUNTIME_WARMUP_MODE="${HERMES_RUNTIME_WARMUP_MODE:-contract}" \
      .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT"
  ) >/tmp/hermes-admin-usage-backend.log 2>&1 &
  BACKEND_PID="$!"
fi

wait_for_url "http://127.0.0.1:${BACKEND_PORT}/api/v1/health"

SUMMARY_JSON="$(curl -fsS "http://127.0.0.1:${BACKEND_PORT}/api/v1/admin/usage/summary")"
"$PYTHON_BIN" -c '
import json
import sys

payload = json.load(sys.stdin)
assert payload["overview"]["run_count"] >= 1
assert payload["overview"]["model_call_count"] >= 1
assert payload["overview"]["total_tokens"] == payload["overview"]["input_tokens"] + payload["overview"]["output_tokens"]
assert payload["runtime_workspaces"][0]["runtime_workspace_id"] == 1
assert payload["models"][0]["model_call_count"] >= 1
assert payload["recent_errors"][0]["source"] in {"model_relay", "runtime_run"}

serialized = json.dumps(payload).lower()
for forbidden in (
    "api" + "_key",
    "provider" + "_key",
    "runtime" + "_token",
    "kube" + "config",
    "host" + "_path",
):
    assert forbidden not in serialized
' <<<"$SUMMARY_JSON"

echo "admin usage smoke: ok"
