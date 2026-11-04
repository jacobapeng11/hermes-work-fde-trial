#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
USER_WEB_DIR="$ROOT_DIR/frontend/user-web"
ADMIN_WEB_DIR="$ROOT_DIR/frontend/admin-web"
IM_BRIDGE_DIR="$BACKEND_DIR/app/im_gateway/node_bridge"
ENTERPRISE_KNOWLEDGE_DIR="$ROOT_DIR/enterprise-knowledge"
RUNTIME_DIR="$ROOT_DIR/.runtime/public-dev"
LOG_DIR="$ROOT_DIR/logs/public-dev"

REQUIRE_GATES="${REQUIRE_GATES:-1}"
INIT_DB="${INIT_DB:-0}"
WARMUP_RUNTIME="${WARMUP_RUNTIME:-1}"

case "$INIT_DB" in
  0|1) ;;
  *)
    echo "start-public-dev: INIT_DB must be 0 or 1." >&2
    exit 2
    ;;
esac

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-dev-lifecycle.sh"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-mode-handoff.sh"
public_mode_acquire_lock
public_dev_acquire_lock

fail() {
  echo "start-public-dev: $*" >&2
  exit 1
}

startup_transaction_active=0
startup_complete=0

rollback_partial_start() {
  local status=$?
  trap - EXIT INT TERM
  if [[ "$startup_transaction_active" == "1" && "$startup_complete" != "1" ]]; then
    echo "start-public-dev: startup failed; rolling back managed services and Runtimes" >&2
    if ! PUBLIC_DEV_LOCK_HELD=1 bash "$ROOT_DIR/scripts/stop-public-dev.sh"; then
      echo "start-public-dev: rollback was incomplete; inspect lifecycle logs" >&2
    fi
  fi
  exit "$status"
}

trap rollback_partial_start EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if ! command -v setsid >/dev/null 2>&1; then
  fail "setsid is required to keep the dev stack running after this script exits."
fi

wait_for_url() {
  local url="$1"
  local name="$2"
  for _ in $(seq 1 120); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "start-public-dev: timed out waiting for $name at $url" >&2
  return 1
}

launch_detached() {
  local pid_file="$1"
  local work_dir="$2"
  local log_file="$3"
  shift 3
  rm -f "$pid_file"
  (
    exec 7>&-
    exec 8>&-
    exec 9>&-
    cd "$work_dir"
    exec setsid bash -c '
      pid_file="$1"
      shift
      echo "$$" > "$pid_file"
      exec "$@"
    ' _ "$pid_file" "$@"
  ) >"$log_file" 2>&1 &
  for _ in $(seq 1 50); do
    if [[ -s "$pid_file" ]]; then
      local service_pid
      service_pid="$(cat "$pid_file")"
      if [[ "$service_pid" =~ ^[0-9]+$ ]] && kill -0 "$service_pid" >/dev/null 2>&1; then
        return 0
      fi
    fi
    sleep 0.1
  done
  fail "detached service did not publish a live pid for $work_dir"
}

ensure_not_running() {
  local pid_file="$1"
  local name="$2"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file")"
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      fail "$name is already running with pid $pid. Run bash scripts/stop-public-dev.sh first."
    fi
    rm -f "$pid_file"
  fi
}

require_port_free() {
  local host="$1"
  local port="$2"
  local name="$3"
  python3 - "$host" "$port" "$name" <<'PY'
import socket
import sys

host, port, name = sys.argv[1], int(sys.argv[2]), sys.argv[3]
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind((host, port))
except OSError as exc:
    raise SystemExit(f"start-public-dev: {name} port {host}:{port} is not available: {exc}")
finally:
    sock.close()
PY
}

cd "$ROOT_DIR"

public_mode_prepare_dev || exit 1
public_dev_load_backend_env || exit 1
public_dev_resolve_ports
public_dev_require_enterprise_knowledge_bundle
public_dev_require_public_web_bases || exit 1
public_dev_require_real_backend_modes || exit 1
if [[ -z "${HERMES_PLATFORM_REVISION:-}" || "${HERMES_PLATFORM_REVISION}" == "workspace" ]]; then
  HERMES_PLATFORM_REVISION="$(git -C "$ROOT_DIR" rev-parse HEAD)"
  export HERMES_PLATFORM_REVISION
fi

if [[ "$REQUIRE_GATES" == "1" ]]; then
  bash scripts/check-gates.sh
fi

python3 scripts/lib/runtime_infra_probe.py --format text

# Keep the browser gate aligned with the backend mode unless the caller
# explicitly overrides it, e.g. VITE_AUTH_MODE=dev for local UI work.
FRONTEND_AUTH_MODE="${VITE_AUTH_MODE:-${HERMES_AUTH_MODE:-dev}}"
ADMIN_FRONTEND_AUTH_MODE="${VITE_ADMIN_AUTH_MODE:-${HERMES_ADMIN_AUTH_MODE:-dev}}"

ensure_not_running "$RUNTIME_DIR/backend.pid" "backend"
ensure_not_running "$RUNTIME_DIR/user-web.pid" "user-web"
ensure_not_running "$RUNTIME_DIR/admin-web.pid" "admin-web"

require_port_free "$BACKEND_HOST" "$BACKEND_PORT" "backend"
require_port_free "$USER_WEB_HOST" "$USER_WEB_PORT" "user-web"
require_port_free "$ADMIN_WEB_HOST" "$ADMIN_WEB_PORT" "admin-web"

if [[ "$INIT_DB" == "1" ]]; then
  bash scripts/init-db.sh
fi

bash "$ROOT_DIR/scripts/publish-enterprise-knowledge-bundle.sh" \
  "$ENTERPRISE_KNOWLEDGE_DIR" \
  "$ENTERPRISE_KNOWLEDGE_HOST_PATH"

if [[ ! -x "$BACKEND_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$BACKEND_DIR/.venv"
fi
"$BACKEND_DIR/.venv/bin/pip" install -q -r "$BACKEND_DIR/requirements.txt"

if [[ ! -f "$IM_BRIDGE_DIR/node_modules/@wecom/aibot-node-sdk/package.json" ]]; then
  (cd "$IM_BRIDGE_DIR" && npm ci --omit=dev --ignore-scripts)
fi
if [[ ! -d "$USER_WEB_DIR/node_modules" ]]; then
  (cd "$USER_WEB_DIR" && npm install)
fi
if [[ ! -d "$ADMIN_WEB_DIR/node_modules" ]]; then
  (cd "$ADMIN_WEB_DIR" && npm install)
fi

"$ROOT_DIR/scripts/check-enterprise-knowledge-data-service.sh" \
  --backend-env "$ROOT_DIR/backend/.env"

startup_transaction_active=1

launch_detached "$RUNTIME_DIR/backend.pid" "$BACKEND_DIR" "$LOG_DIR/backend.log" \
  "$BACKEND_DIR/.venv/bin/uvicorn" app.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" \
  --timeout-graceful-shutdown 10 --reload
wait_for_url "http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}/api/v1/readyz" "backend readiness"

if [[ "$WARMUP_RUNTIME" == "1" && "$FRONTEND_AUTH_MODE" != "enterprise_idpAES" && "$FRONTEND_AUTH_MODE" != "oauth2" ]]; then
  echo "Warming up real Hermes runtime..."
  curl -fsS -X POST "http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}/api/v1/user/runtime-workspace/warmup" >/dev/null
fi

if [[ "$WARMUP_RUNTIME" == "1" && ( "$FRONTEND_AUTH_MODE" == "enterprise_idpAES" || "$FRONTEND_AUTH_MODE" == "oauth2" ) ]]; then
  echo "Skipping unauthenticated runtime warmup in enterprise authentication mode."
fi

launch_detached "$RUNTIME_DIR/user-web.pid" "$USER_WEB_DIR" "$LOG_DIR/user-web.log" \
  env VITE_AUTH_MODE="$FRONTEND_AUTH_MODE" VITE_BACKEND_PROXY_TARGET="http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}" \
  npm run dev -- --host "$USER_WEB_HOST" --port "$USER_WEB_PORT"
wait_for_url "http://${USER_WEB_HOST}:${USER_WEB_PORT}/user/chat" "user-web"

launch_detached "$RUNTIME_DIR/admin-web.pid" "$ADMIN_WEB_DIR" "$LOG_DIR/admin-web.log" \
  env VITE_AUTH_MODE="$ADMIN_FRONTEND_AUTH_MODE" VITE_BACKEND_PROXY_TARGET="http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}" \
  npm run dev -- --host "$ADMIN_WEB_HOST" --port "$ADMIN_WEB_PORT"
wait_for_url "http://${ADMIN_WEB_HOST}:${ADMIN_WEB_PORT}/admin/" "admin-web"

public_mode_switch_nginx dev

startup_complete=1
trap - EXIT INT TERM

cat <<EOF
Hermes Cloud public dev stack is running.

Local:
- User web:  http://${USER_WEB_HOST}:${USER_WEB_PORT}/user/chat
- Admin web: http://${ADMIN_WEB_HOST}:${ADMIN_WEB_PORT}/admin
- Backend:   http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}/api/v1/health
- Readiness: http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}/api/v1/readyz
- Enterprise data: systemd (${ENTERPRISE_KNOWLEDGE_DATA_SERVICE_UNIT:-hermes-enterprise-knowledge-data.service})

Public entrypoints are provided by Nginx HTTPS only:
- User web:  ${PUBLIC_WEB_BASE%/}/user/chat
- Admin web: ${PUBLIC_ADMIN_BASE%/}
- Backend public API for K8s pods: ${PUBLIC_API_BASE:-check backend/.env}/api/v1

The loopback ports ${BACKEND_PORT}, ${USER_WEB_PORT}, and ${ADMIN_WEB_PORT}
must not be opened in the firewall or security group.

Logs:
- $LOG_DIR/backend.log
- journalctl -u ${ENTERPRISE_KNOWLEDGE_DATA_SERVICE_UNIT:-hermes-enterprise-knowledge-data.service}
- $LOG_DIR/user-web.log
- $LOG_DIR/admin-web.log

Stop:
- bash scripts/stop-public-dev.sh
EOF
