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
  echo "smoke-admin-web: timed out waiting for $url" >&2
  return 1
}

BACKEND_PORT="${BACKEND_PORT:-18001}"
FRONTEND_PORT="${FRONTEND_PORT:-15174}"

if ! curl -fsS "http://127.0.0.1:${BACKEND_PORT}/api/v1/health" >/dev/null 2>&1; then
  (
    cd "$ROOT/backend"
    RUNTIME_CHAT_MODE="${RUNTIME_CHAT_MODE:-contract}" HERMES_RUNTIME_WARMUP_MODE="${HERMES_RUNTIME_WARMUP_MODE:-contract}" \
      .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT"
  ) >/tmp/hermes-admin-web-backend.log 2>&1 &
  BACKEND_PID="$!"
fi
wait_for_url "http://127.0.0.1:${BACKEND_PORT}/api/v1/health"

if ! curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/admin" >/dev/null 2>&1; then
  (
    cd "$ROOT/frontend/admin-web"
    VITE_AUTH_MODE=dev VITE_BACKEND_PROXY_TARGET="http://127.0.0.1:${BACKEND_PORT}" npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT"
  ) >/tmp/hermes-admin-web-vite.log 2>&1 &
  VITE_PID="$!"
fi
wait_for_url "http://127.0.0.1:${FRONTEND_PORT}/admin"

curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/admin" | rg '<div id="root">' >/dev/null
curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/api/v1/admin/me" | rg '"roles"' >/dev/null
curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/api/v1/admin/models" | rg '"model_entry_id"|"modelEntryId"' >/dev/null
runtime_resources="$(
  curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/api/v1/admin/resources/runtime-workspaces"
)"
printf '%s' "$runtime_resources" | rg '"runtime_workspace_id"|"runtimeWorkspaceId"' >/dev/null
runtime_target="$(
  printf '%s' "$runtime_resources" | python3 -c '
import json
import sys

resources = json.load(sys.stdin)
if not resources:
    raise SystemExit("smoke-admin-web: no Runtime resource is available")
resource = next(
    (item for item in resources if item.get("has_runtime_binding", True)),
    resources[0],
)
runtime_kind = resource.get("runtime_kind", "personal")
runtime_workspace_id = resource["runtime_workspace_id"]
print(f"{runtime_kind}:{runtime_workspace_id}")
'
)"
runtime_kind="${runtime_target%%:*}"
runtime_workspace_id="${runtime_target#*:}"
curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/api/v1/admin/resources/cluster-summary" | rg '"node_count"|"nodeCount"' >/dev/null
curl -fsS \
  "http://127.0.0.1:${FRONTEND_PORT}/api/v1/admin/resources/runtime-workspaces/${runtime_workspace_id}/kubernetes?runtime_kind=${runtime_kind}" \
  | rg '"aggregate_status"|"aggregateStatus"' >/dev/null
curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/api/v1/admin/usage/summary" | rg '"total_tokens"|"totalTokens"' >/dev/null
curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/api/v1/admin/experts" | rg '^\[' >/dev/null

echo "admin web smoke: ok"
