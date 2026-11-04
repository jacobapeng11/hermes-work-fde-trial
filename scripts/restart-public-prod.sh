#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

# Formal static-release restart. This script owns only the loopback Backend;
# Nginx owns the external ports and static frontends. No Vite, build, or
# database initialization is permitted here.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
RUNTIME_DIR="$ROOT_DIR/.runtime/public-prod"
LOG_DIR="$ROOT_DIR/logs/public-prod"

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-prod-lifecycle.sh"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-mode-handoff.sh"
public_mode_acquire_lock
public_prod_acquire_lock
public_prod_load_backend_env
public_prod_require_topology
public_prod_require_static_artifacts
public_prod_require_nginx_config
public_prod_require_enterprise_knowledge_data_service
storage_guard="$ROOT_DIR/scripts/check-runtime-vdb-storage-mounts.sh"
if [[ ! -x "$storage_guard" ]]; then
  echo "restart-public-prod: required runtime storage guard is missing or not executable: $storage_guard" >&2
  exit 1
fi
"$storage_guard" hermes-data

public_prod_stop_backend
public_mode_prepare_prod
public_prod_load_backend_env

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"
public_prod_require_backend_port_free

if [[ ! -x "$BACKEND_DIR/.venv/bin/uvicorn" ]]; then
  echo "restart-public-prod: Backend virtual environment is missing: $BACKEND_DIR/.venv/bin/uvicorn" >&2
  exit 1
fi
command -v setsid >/dev/null 2>&1 || {
  echo "restart-public-prod: setsid is required" >&2
  exit 1
}

pid_file="$RUNTIME_DIR/backend.pid"
(
  exec 7>&-
  exec 8>&-
  exec 9>&-
  cd "$BACKEND_DIR"
  exec setsid bash -c '
    pid_file="$1"
    shift
    echo "$$" > "$pid_file"
    exec "$@"
  ' _ "$pid_file" \
    "$BACKEND_DIR/.venv/bin/uvicorn" app.main:app \
    --host "$BACKEND_HOST" --port "$BACKEND_PORT" \
    --timeout-graceful-shutdown 10
) >"$LOG_DIR/backend.log" 2>&1 &

for _ in $(seq 1 120); do
  if curl -fsS "http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}/api/v1/readyz" >/dev/null 2>&1; then
    nginx -s reload
    echo "restart-public-prod: complete (Backend ${BACKEND_HOST}:${BACKEND_PORT}; Vite was not started; INIT_DB=0)"
    exit 0
  fi
  sleep 0.5
done

echo "restart-public-prod: Backend readiness timed out; see $LOG_DIR/backend.log" >&2
exit 1
