#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PID=""
VITE_PID=""

cleanup() {
  if [[ -n "$VITE_PID" ]]; then
    kill "$VITE_PID" >/dev/null 2>&1 || true
  fi
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
  echo "smoke-user-web: timed out waiting for $url" >&2
  return 1
}

BACKEND_PORT="${BACKEND_PORT:-18000}"
FRONTEND_PORT="${FRONTEND_PORT:-15175}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! curl -fsS "http://127.0.0.1:${BACKEND_PORT}/api/v1/health" >/dev/null 2>&1; then
  (
    cd "$ROOT/backend"
    RUNTIME_CHAT_MODE="${RUNTIME_CHAT_MODE:-contract}" HERMES_RUNTIME_WARMUP_MODE="${HERMES_RUNTIME_WARMUP_MODE:-contract}" \
      .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT"
  ) >/tmp/hermes-user-web-backend.log 2>&1 &
  BACKEND_PID="$!"
fi
wait_for_url "http://127.0.0.1:${BACKEND_PORT}/api/v1/health"

if ! curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/user/chat" >/dev/null 2>&1; then
  (
    cd "$ROOT/frontend/user-web"
    VITE_AUTH_MODE=dev VITE_BACKEND_PROXY_TARGET="http://127.0.0.1:${BACKEND_PORT}" npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT"
  ) >/tmp/hermes-user-web-vite.log 2>&1 &
  VITE_PID="$!"
fi
wait_for_url "http://127.0.0.1:${FRONTEND_PORT}/user/chat"

curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/user/chat" | rg '<div id="root">' >/dev/null
curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/api/v1/user/runtime-workspace" | rg '"runtime_workspace_id"|"runtimeWorkspaceId"' >/dev/null
curl -fsS -X POST "http://127.0.0.1:${FRONTEND_PORT}/api/v1/user/runtime-workspace/keepalive" >/dev/null
curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/api/v1/user/runtime-workspace/models" | rg '"model_entry_id"|"modelEntryId"' >/dev/null
curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/api/v1/user/runtime-workspace/1/capabilities" | rg '"experts"' >/dev/null
curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/api/v1/user/expert-plaza" | rg '"template_id"|"templateId"' >/dev/null
curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/api/v1/user/chat-files?runtime_workspace_id=1" | rg '^\[' >/dev/null

SESSION_JSON="$(curl -fsS \
  -X POST \
  -H 'Content-Type: application/json' \
  -d '{"title":"User web smoke","model_entry_id":1,"source_type":"smoke"}' \
  "http://127.0.0.1:${FRONTEND_PORT}/api/v1/user/runtime-workspace/1/sessions")"
RUNTIME_SESSION_ID="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["runtime_session_id"])' <<<"$SESSION_JSON")"

EDGE_BOOTSTRAP="$(curl -fsS \
  -X POST \
  -H 'Content-Type: application/json' \
  -H "Origin: http://127.0.0.1:${FRONTEND_PORT}" \
  -d "{\"runtime_workspace_id\":1,\"runtime_session_id\":\"${RUNTIME_SESSION_ID}\",\"model_entry_id\":1,\"attachment_file_ids\":[]}" \
  "http://127.0.0.1:${FRONTEND_PORT}/api/v1/user/runtime-edge/prepare")"
"$PYTHON_BIN" -c '
import json
import sys

payload = json.load(sys.stdin)
assert payload["schema_version"] == 1
assert payload["runtime_session_id"] == sys.argv[1]
assert payload["websocket_protocol"] == "hermes-runtime-v1"
assert payload["websocket_path"].startswith("/runtime-edge/")
assert payload["websocket_path"].endswith("/ws")
assert len(payload["runtime_access"]) >= 32
assert len(payload["session_grant"]) >= 32
' "$RUNTIME_SESSION_ID" <<<"$EDGE_BOOTSTRAP"

FILE_JSON="$(curl -fsS \
  -X POST \
  -H 'Content-Type: application/json' \
  -d "{\"runtime_workspace_id\":1,\"runtime_session_id\":\"${RUNTIME_SESSION_ID}\",\"relative_path\":\"workspace/exports/user-web-smoke.txt\",\"mime_type\":\"text/plain\",\"size_bytes\":18,\"source_type\":\"export\"}" \
  "http://127.0.0.1:${FRONTEND_PORT}/api/v1/user/chat-files")"
FILE_ID="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["file_id"])' <<<"$FILE_JSON")"
curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/api/v1/user/chat-files?runtime_workspace_id=1&runtime_session_id=${RUNTIME_SESSION_ID}" | rg "\"file_id\":${FILE_ID}|\"fileId\":${FILE_ID}" >/dev/null

echo "user web smoke: ok"
