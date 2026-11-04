#!/usr/bin/env bash

# Shared lifecycle helpers for the public dev stack. The caller must set ROOT_DIR.

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/no-proxy-env.sh"
# shellcheck source=scripts/lib/private-env-file.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/private-env-file.sh"

public_dev_require_tools() {
  command -v flock >/dev/null 2>&1 || {
    echo "public-dev lifecycle: flock is required" >&2
    return 1
  }
  command -v ss >/dev/null 2>&1 || {
    echo "public-dev lifecycle: ss is required" >&2
    return 1
  }
}

public_dev_acquire_lock() {
  if [[ "${PUBLIC_DEV_LOCK_HELD:-0}" == "1" ]]; then
    return 0
  fi
  public_dev_require_tools
  mkdir -p "$ROOT_DIR/.runtime/public-dev"
  exec 9>"$ROOT_DIR/.runtime/public-dev/lifecycle.lock"
  if ! flock -w "${PUBLIC_DEV_LOCK_TIMEOUT_SECONDS:-30}" 9; then
    echo "public-dev lifecycle: timed out waiting for lifecycle lock" >&2
    return 1
  fi
  export PUBLIC_DEV_LOCK_HELD=1
}

# The public development lifecycle has one Backend configuration source.  All
# callers (start, stop and restart) must load it before resolving ports or
# touching Runtime resources.
public_dev_load_backend_env() {
  local env_file="${1:-$ROOT_DIR/backend/.env}"
  if [[ ! -f "$env_file" ]]; then
    echo "public-dev lifecycle: 缺少 Backend 配置文件：$env_file" >&2
    return 1
  fi
  hermes_require_private_env_file "$env_file" || return 1
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
  hermes_clear_proxy_env
}

public_dev_require_real_backend_modes() {
  if [[ "${RUNTIME_CHAT_MODE:-}" != "real" ]]; then
    echo "public-dev lifecycle: 真实 Runtime 模式要求 RUNTIME_CHAT_MODE=real（请检查 backend/.env）。" >&2
    return 1
  fi
}

public_dev_require_public_web_bases() {
  local user_base="${PUBLIC_WEB_BASE:-}"
  local admin_base="${PUBLIC_ADMIN_BASE:-}"
  if [[ -z "$user_base" || -z "$admin_base" ]]; then
    echo "public-dev lifecycle: 缺少 PUBLIC_WEB_BASE 或 PUBLIC_ADMIN_BASE（请检查 backend/.env）。" >&2
    return 1
  fi
  python3 - "$user_base" "$admin_base" <<'PY'
from urllib.parse import urlparse
import sys

user_base, admin_base = sys.argv[1:]
for name, value in (("PUBLIC_WEB_BASE", user_base), ("PUBLIC_ADMIN_BASE", admin_base)):
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(f"public-dev lifecycle: {name} 必须是完整 HTTP(S) 地址（请检查 backend/.env）。")
if urlparse(admin_base).path.rstrip("/") not in {"", "/admin"}:
    raise SystemExit("public-dev lifecycle: PUBLIC_ADMIN_BASE 只能是管理端根路径或以 /admin 结尾。")
PY
}

public_dev_resolve_ports() {
  # PUBLIC_API_BASE is an external HTTPS origin behind Nginx.  It must never
  # be used to infer the backend's loopback listener port.
  BACKEND_PORT="${BACKEND_PORT:-${PORT_BACKEND:-}}"
  PORT_BACKEND="${PORT_BACKEND:-$BACKEND_PORT}"
  for name in BACKEND_HOST BACKEND_LOCAL_HOST USER_WEB_HOST ADMIN_WEB_HOST BACKEND_PORT USER_WEB_PORT ADMIN_WEB_PORT; do
    if [[ -z "${!name:-}" ]]; then
      echo "public-dev lifecycle: $name 必须来自 backend/.env。" >&2
      return 1
    fi
  done
  export BACKEND_HOST BACKEND_LOCAL_HOST USER_WEB_HOST ADMIN_WEB_HOST
  export BACKEND_PORT PORT_BACKEND USER_WEB_PORT ADMIN_WEB_PORT
}

public_dev_require_enterprise_knowledge_bundle() {
  local source_dir="$ROOT_DIR/enterprise-knowledge"

  for required_file in \
    "$source_dir/PRIMER.md" \
    "$source_dir/workflows/query.md"; do
    if [[ ! -f "$required_file" ]]; then
      echo "public-dev lifecycle: 企业知识文件缺失：$required_file" >&2
      return 1
    fi
  done
  if [[ -z "${ENTERPRISE_KNOWLEDGE_HOST_PATH:-}" || "${ENTERPRISE_KNOWLEDGE_HOST_PATH}" != /* ]]; then
    echo "public-dev lifecycle: ENTERPRISE_KNOWLEDGE_HOST_PATH 必须是绝对路径。" >&2
    return 1
  fi
}

public_dev_process_belongs() {
  local pid="$1"
  local service="$2"
  local service_dir="$3"
  local port="$4"
  local cwd cmdline

  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ -d "/proc/$pid" ]] || return 1
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" || return 1
  case "$cwd" in
    "$service_dir"|"$service_dir"/*) ;;
    *) return 1 ;;
  esac
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || return 1

  case "$service" in
    backend)
      # A standard launch uses an absolute venv path, while an operator may
      # start the same Backend from its own directory with `.venv/bin/uvicorn`.
      # The cwd check above still scopes the relative form to this project.
      [[ "$cmdline" == *"$ROOT_DIR/backend/.venv/bin/uvicorn"* || "$cmdline" == *".venv/bin/uvicorn"* ]]
      [[ "$cmdline" == *"app.main:app"* ]]
      ;;
    user-web|admin-web)
      [[ "$cmdline" == *"vite"* || "$cmdline" == *"npm run dev"* ]]
      [[ "$cmdline" == *"$port"* ]]
      ;;
    *) return 1 ;;
  esac
}

public_dev_require_prod_backend_stopped() {
  local status_script="$ROOT_DIR/scripts/status-public-prod.sh"
  local pid_file="$ROOT_DIR/.runtime/public-prod/backend.pid"

  if [[ ! -f "$status_script" ]]; then
    echo "public-dev lifecycle: formal Backend status script is missing: $status_script" >&2
    return 1
  fi
  if bash "$status_script" >/dev/null 2>&1; then
    echo "public-dev lifecycle: formal public-prod Backend is running; use the public-prod lifecycle or stop it explicitly before full public-dev." >&2
    return 1
  fi
  if [[ -f "$pid_file" ]]; then
    echo "public-dev lifecycle: formal public-prod Backend PID record exists but cannot be verified; run stop-public-prod.sh to resolve it before full public-dev." >&2
    return 1
  fi
  return 0
}

public_dev_frontend_overlay_process_belongs() {
  local pid="$1"
  local service="$2"
  local service_dir="$3"
  local port="$4"
  local cmdline

  public_dev_process_belongs "$pid" "$service" "$service_dir" "$port" \
    || return 1
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || return 1
  if [[ "$cmdline" == *"--strictPort"* ]]; then
    return 0
  fi
  tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
    | grep -q '^VITE_HMR_HOST='
}

public_dev_require_frontend_overlay_stopped() {
  local runtime_dir="$ROOT_DIR/.runtime/public-frontends-dev"
  local service service_dir host port pid_file pid listener

  while read -r service service_dir host port; do
    pid_file="$runtime_dir/$service.pid"
    [[ -f "$pid_file" ]] || continue
    pid="$(tr -d '[:space:]' < "$pid_file" 2>/dev/null || true)"
    if [[ ! "$pid" =~ ^[1-9][0-9]*$ || ! -d "/proc/$pid" ]]; then
      continue
    fi
    if public_dev_frontend_overlay_process_belongs \
      "$pid" "$service" "$service_dir" "$port"; then
      echo "public-dev lifecycle: public frontend dev overlay is running ($service pid=$pid); switch Nginx to prod and stop the overlay before full public-dev." >&2
    else
      echo "public-dev lifecycle: public frontend dev overlay has a live unverified PID record ($service pid=$pid); refusing cross-lifecycle cleanup." >&2
    fi
    return 1
  done <<EOF
user-web $ROOT_DIR/frontend/user-web $USER_WEB_HOST $USER_WEB_PORT
admin-web $ROOT_DIR/frontend/admin-web $ADMIN_WEB_HOST $ADMIN_WEB_PORT
EOF

  while read -r service service_dir host port; do
    for listener in $(public_dev_listener_pids "$port" "$host"); do
      if public_dev_frontend_overlay_process_belongs \
        "$listener" "$service" "$service_dir" "$port"; then
        echo "public-dev lifecycle: public frontend dev overlay orphan listener is running ($service pid=$listener); switch Nginx to prod and stop the overlay before full public-dev." >&2
        return 1
      fi
    done
  done <<EOF
user-web $ROOT_DIR/frontend/user-web $USER_WEB_HOST $USER_WEB_PORT
admin-web $ROOT_DIR/frontend/admin-web $ADMIN_WEB_HOST $ADMIN_WEB_PORT
EOF
  return 0
}

public_dev_stop_background_service() {
  local service="$1"
  local service_dir="$2"
  local pid_file="$ROOT_DIR/.runtime/public-dev/$service.pid"
  local pid=""
  local orphan_pid

  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if public_dev_process_belongs "$pid" "$service" "$service_dir" "0"; then
      public_dev_signal_group TERM "$pid"
      echo "$service: stopped pid-file process pid=$pid"
    else
      echo "$service: removed stale pid file${pid:+ pid=$pid}"
    fi
    rm -f "$pid_file"
  fi

  # Background workers have no TCP listener.  The lifecycle PID file is the
  # ownership boundary; scanning every process under /proc here can block a
  # shutdown indefinitely on an unrelated unresponsive process.
  if [[ -z "$pid" ]]; then
    echo "$service: not running"
  fi

  return 0
}

public_dev_background_pids() {
  local service="$1"
  local service_dir="$2"
  local process_dir candidate_pid

  for process_dir in /proc/[0-9]*; do
    candidate_pid="${process_dir##*/}"
    [[ "$candidate_pid" != "$$" ]] || continue
    if public_dev_process_belongs \
      "$candidate_pid" "$service" "$service_dir" "0"; then
      echo "$candidate_pid"
    fi
  done

  # An empty scan is normal.  Do not leak the final failed predicate as this
  # helper's return status: callers run under `set -e` during shutdown.
  return 0
}

public_dev_listener_pids() {
  local port="$1"
  local host="${2:-}"
  [[ -n "$host" ]] || return 0
  # `lsof` can block for a long time on this host while inspecting a busy
  # Nginx process. `ss` already exposes only LISTEN sockets and their PIDs;
  # Filter its local address so an external Nginx listener cannot be mistaken
  # for the managed Backend listener declared in backend/.env.
  ss -H -ltnp "sport = :$port" 2>/dev/null \
    | python3 -c '
import re
import sys

host, port = sys.argv[1:]
target = f"{host}:{port}"
seen: set[str] = set()
for line in sys.stdin:
    fields = line.split()
    if len(fields) < 4 or fields[3] != target:
        continue
    for pid in re.findall(r"pid=(\d+)", line):
        if pid not in seen:
            seen.add(pid)
            print(pid)
' "$host" "$port"
}

public_dev_process_group() {
  local pid="$1"
  ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' '
}

public_dev_signal_group() {
  local signal="$1"
  local pid="$2"
  local pgid
  pgid="$(public_dev_process_group "$pid")"
  [[ "$pgid" =~ ^[0-9]+$ ]] || return 1
  if [[ "$signal" == "TERM" ]]; then
    kill -TERM -- "-$pgid" >/dev/null 2>&1 || true
  else
    kill -KILL -- "-$pgid" >/dev/null 2>&1 || true
  fi
}

public_dev_host_port_is_free() {
  local host="$1"
  local port="$2"
  local listeners
  listeners="$(ss -H -ltn "sport = :$port" 2>/dev/null)" || return 1
  printf '%s\n' "$listeners" | python3 -c '
import sys

host, port = sys.argv[1:]
conflicts = {
    f"{host}:{port}",
    f"0.0.0.0:{port}",
    f"*:{port}",
    f"[::]:{port}",
    f":::{port}",
}
for line in sys.stdin:
    fields = line.split()
    if len(fields) >= 4 and fields[3] in conflicts:
        raise SystemExit(1)
' "$host" "$port"
}

public_dev_wait_port_free() {
  local host="$1"
  local port="$2"
  local attempts="$3"
  local _
  for _ in $(seq 1 "$attempts"); do
    if public_dev_host_port_is_free "$host" "$port"; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

public_dev_command_summary() {
  local pid="$1"
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-180
}

public_dev_stop_service() {
  local service="$1"
  local service_dir="$2"
  local port="$3"
  local pid_file="$ROOT_DIR/.runtime/public-dev/$service.pid"
  local pid listener foreign=0 host

  case "$service" in
    backend) host="$BACKEND_HOST" ;;
    user-web) host="$USER_WEB_HOST" ;;
    admin-web) host="$ADMIN_WEB_HOST" ;;
    *)
      echo "$service: unknown service listener host" >&2
      return 1
      ;;
  esac

  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if public_dev_process_belongs "$pid" "$service" "$service_dir" "$port"; then
      public_dev_signal_group TERM "$pid"
      echo "$service: stopped pid-file process pid=$pid"
    else
      echo "$service: removed stale pid file${pid:+ pid=$pid}"
    fi
    rm -f "$pid_file"
  fi

  for listener in $(public_dev_listener_pids "$port" "$host"); do
    if public_dev_process_belongs "$listener" "$service" "$service_dir" "$port"; then
      public_dev_signal_group TERM "$listener"
      echo "$service: stopped orphan listener pid=$listener port=$port"
    else
      echo "$service: foreign listener refused pid=$listener port=$port command=$(public_dev_command_summary "$listener")" >&2
      foreign=1
    fi
  done
  [[ "$foreign" == "0" ]] || return 1

  if ! public_dev_wait_port_free "$host" "$port" 25; then
    for listener in $(public_dev_listener_pids "$port" "$host"); do
      if public_dev_process_belongs "$listener" "$service" "$service_dir" "$port"; then
        local pgid
        pgid="$(public_dev_process_group "$listener")"
        public_dev_signal_group KILL "$listener"
        echo "$service: forced process-group kill pgid=$pgid port=$port"
      else
        echo "$service: foreign listener refused pid=$listener port=$port command=$(public_dev_command_summary "$listener")" >&2
        return 1
      fi
    done
    public_dev_wait_port_free "$host" "$port" 10 || {
      echo "$service: port $host:$port was not released" >&2
      return 1
    }
  fi
  echo "$service: port released port=$host:$port"
}

public_dev_assert_ports_free() {
  local failed=0
  local host port
  while read -r host port; do
    [[ -n "$port" ]] || continue
    if ! public_dev_host_port_is_free "$host" "$port"; then
      echo "public-dev lifecycle: port remains occupied: $host:$port" >&2
      failed=1
    fi
  done <<EOF
$BACKEND_HOST $BACKEND_PORT
$USER_WEB_HOST $USER_WEB_PORT
$ADMIN_WEB_HOST $ADMIN_WEB_PORT
EOF
  [[ "$failed" == "0" ]]
}

public_dev_force_runtime_zero() {
  local namespace="${HERMES_RUNTIME_NAMESPACE:-hermes-runtimes}"
  local kubectl_bin="${KUBECTL_BIN:-kubectl}"
  local timeout_seconds="${PUBLIC_DEV_RUNTIME_ZERO_TIMEOUT_SECONDS:-120}"
  local helper="$ROOT_DIR/scripts/lib/public_dev_runtime_control.py"

  if ! command -v python3 >/dev/null 2>&1; then
    echo "public-dev lifecycle: python3 is required for Runtime scale-to-zero" >&2
    return 1
  fi
  if ! command -v "$kubectl_bin" >/dev/null 2>&1; then
    echo "public-dev lifecycle: kubectl is required for Runtime scale-to-zero" >&2
    return 1
  fi
  if [[ ! -f "$helper" ]]; then
    echo "public-dev lifecycle: Runtime control helper is missing: $helper" >&2
    return 1
  fi

  python3 "$helper" force-zero \
    --namespace "$namespace" \
    --kubectl "$kubectl_bin" \
    --timeout-seconds "$timeout_seconds"
}
