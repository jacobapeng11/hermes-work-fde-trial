#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PID=""
BACKEND_PORT="${BACKEND_PORT:-18004}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cleanup() {
  if [[ -n "$BACKEND_PID" ]]; then
    kill "$BACKEND_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ -z "${ADMIN_INITIAL_PASSWORD:-}" ]]; then
  echo "smoke-admin-password-auth: ADMIN_INITIAL_PASSWORD is required" >&2
  exit 2
fi

wait_for_url() {
  local url="$1"
  for _ in $(seq 1 60); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "smoke-admin-password-auth: timed out waiting for $url" >&2
  return 1
}

if ! curl -fsS "http://127.0.0.1:${BACKEND_PORT}/api/v1/health" >/dev/null 2>&1; then
  (
    cd "$ROOT/backend"
    APP_ENV=dev HERMES_AUTH_MODE=enterprise_idpAES HERMES_ADMIN_AUTH_MODE=password \
      ADMIN_INITIAL_PASSWORD="$ADMIN_INITIAL_PASSWORD" .venv/bin/python -m scripts.seed
    APP_ENV=dev HERMES_AUTH_MODE=enterprise_idpAES HERMES_ADMIN_AUTH_MODE=password \
      ADMIN_INITIAL_PASSWORD="$ADMIN_INITIAL_PASSWORD" \
      .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT"
  ) >/tmp/hermes-admin-password-auth-backend.log 2>&1 &
  BACKEND_PID="$!"
fi

wait_for_url "http://127.0.0.1:${BACKEND_PORT}/api/v1/health"

anonymous_status="$(curl -sS -o /dev/null -w "%{http_code}" "http://127.0.0.1:${BACKEND_PORT}/api/v1/admin/me")"
if [[ "$anonymous_status" != "401" ]]; then
  echo "smoke-admin-password-auth: expected anonymous admin access to return 401, got $anonymous_status" >&2
  exit 1
fi

wrong_status="$(curl -sS -o /dev/null -w "%{http_code}" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"incorrect-password"}' \
  "http://127.0.0.1:${BACKEND_PORT}/api/v1/auth/admin/login")"
if [[ "$wrong_status" != "401" ]]; then
  echo "smoke-admin-password-auth: expected rejected password to return 401, got $wrong_status" >&2
  exit 1
fi

login_json="$(curl -fsS \
  -H "Content-Type: application/json" \
  -d "$("$PYTHON_BIN" -c 'import json, os; print(json.dumps({"username": "admin", "password": os.environ["ADMIN_INITIAL_PASSWORD"]}))')" \
  "http://127.0.0.1:${BACKEND_PORT}/api/v1/auth/admin/login")"
access_token="$("$PYTHON_BIN" -c 'import json, sys; print(json.load(sys.stdin)["access_token"])' <<<"$login_json")"

admin_status="$(curl -sS -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer ${access_token}" \
  "http://127.0.0.1:${BACKEND_PORT}/api/v1/admin/me")"
if [[ "$admin_status" != "200" ]]; then
  echo "smoke-admin-password-auth: expected authenticated admin access to return 200, got $admin_status" >&2
  exit 1
fi

echo "admin password auth smoke: ok"
