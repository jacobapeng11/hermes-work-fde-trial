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
  echo "smoke-auth: timed out waiting for $url" >&2
  return 1
}

BACKEND_PORT="${BACKEND_PORT:-18003}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! curl -fsS "http://127.0.0.1:${BACKEND_PORT}/api/v1/health" >/dev/null 2>&1; then
  (
    cd "$ROOT/backend"
    HERMES_AUTH_MODE=jwt RUNTIME_CHAT_MODE=contract HERMES_RUNTIME_WARMUP_MODE=contract \
      .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT"
  ) >/tmp/hermes-auth-backend.log 2>&1 &
  BACKEND_PID="$!"
fi

wait_for_url "http://127.0.0.1:${BACKEND_PORT}/api/v1/health"

STATUS="$(curl -sS -o /tmp/hermes-auth-missing.json -w "%{http_code}" "http://127.0.0.1:${BACKEND_PORT}/api/v1/user/runtime-workspace")"
if [[ "$STATUS" != "401" ]]; then
  echo "smoke-auth: expected missing bearer request to return 401, got $STATUS" >&2
  cat /tmp/hermes-auth-missing.json >&2
  exit 1
fi

LOGIN_JSON="$(curl -fsS \
  -H "Content-Type: application/json" \
  -d '{"identifier":"dev-user","password":"dev-user"}' \
  "http://127.0.0.1:${BACKEND_PORT}/api/v1/auth/login")"

ACCESS_TOKEN="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["access_token"])' <<<"$LOGIN_JSON")"

curl -fsS -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  "http://127.0.0.1:${BACKEND_PORT}/api/v1/auth/me" | rg '"user_id":-1|"userId":-1' >/dev/null
curl -fsS -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  "http://127.0.0.1:${BACKEND_PORT}/api/v1/user/runtime-workspace" | rg '"runtime_workspace_id"|"runtimeWorkspaceId"' >/dev/null
curl -fsS -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  "http://127.0.0.1:${BACKEND_PORT}/api/v1/admin/me" | rg '"roles"' >/dev/null

echo "auth smoke: ok"
