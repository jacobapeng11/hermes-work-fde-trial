#!/usr/bin/env bash
set -euo pipefail

# Restart only the local development Backend.  This deliberately does not
# build frontends, touch Kubernetes resources, or run database migrations.
# Its configuration source is backend/.env, matching start-public-dev.sh.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/no-proxy-env.sh
source "$ROOT_DIR/scripts/lib/no-proxy-env.sh"
BACKEND_DIR="$ROOT_DIR/backend"
RUNTIME_DIR="$ROOT_DIR/.runtime/public-dev"
LOG_DIR="$ROOT_DIR/logs/public-dev"
PID_FILE="$RUNTIME_DIR/backend.pid"

allow_prod_handoff=0
if [[ "${1:-}" == "--from-prod" ]]; then
  allow_prod_handoff=1
  shift
fi
if (($#)); then
  echo "Usage: $0 [--from-prod]" >&2
  exit 2
fi

# shellcheck source=scripts/lib/public-dev-lifecycle.sh
source "$ROOT_DIR/scripts/lib/public-dev-lifecycle.sh"

fail() {
  echo "restart-public-backend-dev: $*" >&2
  exit 1
}

public_dev_acquire_lock
public_dev_load_backend_env
public_dev_require_real_backend_modes
public_dev_resolve_ports

if "$ROOT_DIR/scripts/status-public-prod.sh" >/dev/null 2>&1; then
  if ((allow_prod_handoff == 0)); then
    fail "formal public-prod Backend is running; use restart-public-prod.sh instead"
  fi
  # Fleet reload invokes this after the Runtime Pod-zero gate.  At that point
  # the control plane is frozen and the loopback listener can change modes.
  # shellcheck disable=SC1091
  source "$ROOT_DIR/scripts/lib/public-prod-lifecycle.sh"
  public_prod_acquire_lock
  public_prod_load_backend_env
  public_prod_stop_backend
fi

[[ -x "$BACKEND_DIR/.venv/bin/uvicorn" ]] || fail "Backend venv is missing: $BACKEND_DIR/.venv/bin/uvicorn"
command -v setsid >/dev/null 2>&1 || fail "setsid is required"

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

stop_owned_backend() {
  local pid="$1"
  local pgid

  public_dev_process_belongs "$pid" backend "$BACKEND_DIR" "$BACKEND_PORT" \
    || fail "refusing to stop unowned listener pid=$pid on ${BACKEND_HOST}:${BACKEND_PORT}"
  pgid="$(public_dev_process_group "$pid")"
  [[ "$pgid" =~ ^[1-9][0-9]*$ ]] || fail "cannot resolve process group for pid=$pid"
  public_dev_signal_group TERM "$pid"
  for _ in $(seq 1 50); do
    [[ ! -d "/proc/$pid" ]] && return 0
    sleep 0.2
  done
  public_dev_signal_group KILL "$pid"
  for _ in $(seq 1 10); do
    [[ ! -d "/proc/$pid" ]] && return 0
    sleep 0.2
  done
  fail "Backend process group $pgid did not stop"
}

# A PID file is preferred, but an interrupted shell can leave it stale.  A
# configured loopback listener is adopted only after the same cwd/uvicorn
# ownership checks, so an unrelated process is never killed merely because it
# owns the configured port.
if [[ -f "$PID_FILE" ]]; then
  managed_pid="$(tr -d '[:space:]' < "$PID_FILE")"
  if [[ "$managed_pid" =~ ^[1-9][0-9]*$ && -d "/proc/$managed_pid" ]]; then
    stop_owned_backend "$managed_pid"
  fi
fi

while IFS= read -r listener_pid; do
  [[ -n "$listener_pid" ]] || continue
  [[ -d "/proc/$listener_pid" ]] || continue
  stop_owned_backend "$listener_pid"
done < <(public_dev_listener_pids "$BACKEND_PORT" "$BACKEND_HOST")

public_dev_wait_port_free "$BACKEND_HOST" "$BACKEND_PORT" 150 \
  || fail "Backend port is still occupied: ${BACKEND_HOST}:${BACKEND_PORT}"

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
  ' _ "$PID_FILE" \
    "$BACKEND_DIR/.venv/bin/uvicorn" app.main:app \
    --host "$BACKEND_HOST" --port "$BACKEND_PORT" \
    --timeout-graceful-shutdown 10 --reload
) >"$LOG_DIR/backend.log" 2>&1 &

for _ in $(seq 1 120); do
  if curl -fsS "http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}/api/v1/readyz" >/dev/null 2>&1; then
    backend_pid="$(tr -d '[:space:]' < "$PID_FILE")"
    public_dev_process_belongs "$backend_pid" backend "$BACKEND_DIR" "$BACKEND_PORT" \
      || fail "new Backend pid is not owned by this workspace"
    echo "restart-public-backend-dev: ready pid=$backend_pid url=http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}/api/v1/readyz"
    exit 0
  fi
  sleep 0.5
done

echo "restart-public-backend-dev: readiness timed out; recent log follows:" >&2
tail -n 120 "$LOG_DIR/backend.log" >&2 || true
exit 1
