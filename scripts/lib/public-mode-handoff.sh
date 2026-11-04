#!/usr/bin/env bash

# Safe ownership-aware handoff between the complete public dev and prod modes.
# Callers must set ROOT_DIR before sourcing this library.

public_mode_acquire_lock() {
  if [[ "${PUBLIC_MODE_LOCK_HELD:-0}" == "1" ]]; then
    return 0
  fi
  command -v flock >/dev/null 2>&1 || {
    echo "public mode handoff: flock is required" >&2
    return 1
  }
  mkdir -p "$ROOT_DIR/.runtime/public-mode"
  exec 7>"$ROOT_DIR/.runtime/public-mode/lifecycle.lock"
  flock -w "${PUBLIC_MODE_LOCK_TIMEOUT_SECONDS:-30}" 7 || {
    echo "public mode handoff: timed out waiting for lifecycle lock" >&2
    return 1
  }
  export PUBLIC_MODE_LOCK_HELD=1
}

public_mode_switch_nginx() {
  local mode="$1"
  PUBLIC_PROD_LOCK_HELD="${PUBLIC_PROD_LOCK_HELD:-0}" \
    bash "$ROOT_DIR/scripts/switch-public-nginx-mode.sh" "$mode"
}

public_mode_stop_frontend_overlay() {
  # shellcheck disable=SC1090
  source "$ROOT_DIR/scripts/lib/public-frontend-dev-lifecycle.sh"
  public_frontend_dev_load_environment
  public_frontend_dev_stop user-web \
    "$ROOT_DIR/.runtime/public-frontends-dev/user-web.pid" \
    "$ROOT_DIR/frontend/user-web" "$USER_WEB_PORT" "$USER_WEB_HOST"
  public_frontend_dev_stop admin-web \
    "$ROOT_DIR/.runtime/public-frontends-dev/admin-web.pid" \
    "$ROOT_DIR/frontend/admin-web" "$ADMIN_WEB_PORT" "$ADMIN_WEB_HOST"
}

public_mode_stop_registered_dev_service() {
  local service="$1"
  local service_dir="$2"
  local port="$3"
  local pid_file="$ROOT_DIR/.runtime/public-dev/$service.pid"
  local pid host listener

  case "$service" in
    backend) host="$BACKEND_HOST" ;;
    user-web) host="$USER_WEB_HOST" ;;
    admin-web) host="$ADMIN_WEB_HOST" ;;
    *)
      echo "public-dev $service: unknown service listener host" >&2
      return 1
      ;;
  esac

  [[ -f "$pid_file" ]] || {
    echo "public-dev $service: not running"
    return 0
  }
  pid="$(tr -d '[:space:]' < "$pid_file")"
  if [[ ! "$pid" =~ ^[1-9][0-9]*$ || ! -d "/proc/$pid" ]]; then
    rm -f "$pid_file"
    echo "public-dev $service: removed stale pid record"
    return 0
  fi
  if ! public_dev_process_belongs "$pid" "$service" "$service_dir" "$port"; then
    echo "public-dev $service: refused to signal unowned pid=$pid" >&2
    return 1
  fi
  public_dev_signal_group TERM "$pid"
  rm -f "$pid_file"
  echo "public-dev $service: stopped pid=$pid"

  # A verified process group can need a moment to close its listening socket.
  # Wait before the aggregate port check below so a normal TERM shutdown is not
  # misreported as a failed dev-to-prod handoff.
  if ! public_dev_wait_port_free "$host" "$port" 25; then
    for listener in $(public_dev_listener_pids "$port" "$host"); do
      if public_dev_process_belongs "$listener" "$service" "$service_dir" "$port"; then
        public_dev_signal_group KILL "$listener"
        echo "public-dev $service: forced process-group kill pid=$listener port=$port"
      else
        echo "public-dev $service: foreign listener refused pid=$listener port=$port" >&2
        return 1
      fi
    done
    public_dev_wait_port_free "$host" "$port" 10 || {
      echo "public-dev $service: port $host:$port was not released" >&2
      return 1
    }
  fi
}

public_mode_stop_registered_full_dev() {
  # shellcheck disable=SC1090
  source "$ROOT_DIR/scripts/lib/public-dev-lifecycle.sh"
  public_dev_acquire_lock
  public_dev_load_backend_env
  public_dev_resolve_ports
  public_mode_stop_registered_dev_service admin-web "$ROOT_DIR/frontend/admin-web" "$ADMIN_WEB_PORT"
  public_mode_stop_registered_dev_service user-web "$ROOT_DIR/frontend/user-web" "$USER_WEB_PORT"
  public_mode_stop_registered_dev_service backend "$ROOT_DIR/backend" "$BACKEND_PORT"
}

public_mode_stop_full_dev() {
  public_mode_stop_registered_full_dev
  # nginx -s reload can leave an old worker holding the former loopback
  # listener for a brief graceful-exit window.  Registered dev services have
  # already been stopped above; wait a bounded interval before the strict
  # aggregate assertion, without signaling any unknown listener.
  public_dev_wait_port_free \
    "$BACKEND_HOST" "$BACKEND_PORT" 25 || true
  public_dev_assert_ports_free
}

public_mode_stop_prod_backend() {
  # shellcheck disable=SC1090
  source "$ROOT_DIR/scripts/lib/public-prod-lifecycle.sh"
  public_prod_acquire_lock
  public_prod_load_backend_env
  public_prod_stop_backend
}

public_mode_prepare_dev() {
  public_mode_stop_prod_backend
  public_mode_stop_frontend_overlay
}

public_mode_prepare_prod() {
  public_mode_switch_nginx prod
  public_mode_stop_frontend_overlay
  public_mode_stop_full_dev
}
