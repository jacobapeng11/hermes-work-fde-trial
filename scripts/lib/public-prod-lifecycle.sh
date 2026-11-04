#!/usr/bin/env bash

# Shared helpers for the formal static-release lifecycle. The caller must set
# ROOT_DIR. This lifecycle deliberately manages only the Backend: Nginx serves
# static frontends and no Vite process is part of a formal release.

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/no-proxy-env.sh"
# shellcheck source=scripts/lib/private-env-file.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/private-env-file.sh"

public_prod_acquire_lock() {
  if [[ "${PUBLIC_PROD_LOCK_HELD:-0}" == "1" ]]; then
    return 0
  fi
  command -v flock >/dev/null 2>&1 || {
    echo "public-prod lifecycle: flock is required" >&2
    return 1
  }
  mkdir -p "$ROOT_DIR/.runtime/public-prod"
  exec 8>"$ROOT_DIR/.runtime/public-prod/lifecycle.lock"
  if ! flock -w "${PUBLIC_PROD_LOCK_TIMEOUT_SECONDS:-30}" 8; then
    echo "public-prod lifecycle: timed out waiting for lifecycle lock" >&2
    return 1
  fi
  export PUBLIC_PROD_LOCK_HELD=1
}

public_prod_load_backend_env() {
  # Every public lifecycle (dev and prod) uses the same private checkout-local
  # environment. An explicit argument is retained for isolated contract tests;
  # production entrypoints always use backend/.env.
  local env_file="${1:-$ROOT_DIR/backend/.env}"
  if [[ ! -f "$env_file" ]]; then
    echo "public lifecycle: 缺少 Backend 配置文件：$env_file" >&2
    return 1
  fi
  hermes_require_private_env_file "$env_file" || return 1
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
  hermes_clear_proxy_env

  BACKEND_HOST="${BACKEND_HOST:-}"
  BACKEND_LOCAL_HOST="${BACKEND_LOCAL_HOST:-}"
  BACKEND_PORT="${BACKEND_PORT:-${PORT_BACKEND:-}}"
  PORT_BACKEND="${PORT_BACKEND:-$BACKEND_PORT}"
  PUBLIC_RELEASE_ROOT="${PUBLIC_RELEASE_ROOT:-}"
  PUBLIC_NGINX_CONFIG="${PUBLIC_NGINX_CONFIG:-}"
  PUBLIC_RELEASE_REVISION="${PUBLIC_RELEASE_REVISION:-$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)}"
  if [[ ! "$PUBLIC_RELEASE_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
    echo "public-prod lifecycle: unable to determine the 40-character release revision." >&2
    return 1
  fi
  HERMES_ACTIVE_ENV_FILE="$env_file"
  export BACKEND_HOST BACKEND_LOCAL_HOST BACKEND_PORT PORT_BACKEND
  export PUBLIC_RELEASE_ROOT PUBLIC_NGINX_CONFIG PUBLIC_RELEASE_REVISION HERMES_ACTIVE_ENV_FILE
  # Database structure is never an implicit lifecycle side effect.
  export INIT_DB=0
}

public_prod_require_topology() {
  if [[ -z "${BACKEND_HOST:-}" || -z "${BACKEND_LOCAL_HOST:-}" || -z "${BACKEND_PORT:-}" ]]; then
    echo "public-prod lifecycle: BACKEND_HOST、BACKEND_LOCAL_HOST、BACKEND_PORT 必须来自 backend/.env。" >&2
    return 1
  fi
  local parsed
  parsed="$(python3 - "$BACKEND_HOST" "$BACKEND_LOCAL_HOST" "$BACKEND_PORT" "${PORT_BACKEND:-}" "${PUBLIC_API_BASE:-}" "${PUBLIC_WEB_BASE:-}" "${PUBLIC_ADMIN_BASE:-}" <<'PY'
from urllib.parse import urlparse
import ipaddress
import sys
import re

backend_host, backend_local_host, backend_port, port_backend, api, web, admin = sys.argv[1:]

for name, value in (("BACKEND_HOST", backend_host), ("BACKEND_LOCAL_HOST", backend_local_host)):
    try:
        if not ipaddress.ip_address(value).is_loopback:
            raise ValueError
    except ValueError:
        raise SystemExit(f"public-prod lifecycle: {name} must be a loopback IP from backend/.env")
if not re.fullmatch(r"[1-9][0-9]{0,4}", backend_port) or not (1 <= int(backend_port) <= 65535):
    raise SystemExit("public-prod lifecycle: BACKEND_PORT must be between 1 and 65535")
if port_backend and port_backend != backend_port:
    raise SystemExit("public-prod lifecycle: PORT_BACKEND must match BACKEND_PORT")

def origin(value: str, name: str, *, allow_path: str = "/") -> tuple[str, int, str]:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit(f"public-prod lifecycle: {name} must be an absolute HTTP(S) URL")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise SystemExit(f"public-prod lifecycle: {name} must not contain credentials/query/fragment")
    expected_path = allow_path.rstrip("/") or "/"
    actual_path = (parsed.path or "/").rstrip("/") or "/"
    if actual_path != expected_path:
        raise SystemExit(f"public-prod lifecycle: {name} has unexpected path")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", parsed.hostname):
        raise SystemExit(f"public-prod lifecycle: {name} has an unsafe hostname")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname, port, parsed.scheme

api_host, api_port, _ = origin(api, "PUBLIC_API_BASE")
web_host, web_port, _ = origin(web, "PUBLIC_WEB_BASE")
admin_host, admin_port, _ = origin(admin, "PUBLIC_ADMIN_BASE", allow_path="/admin")
if (admin_host, admin_port) != (web_host, web_port):
    raise SystemExit("public-prod lifecycle: PUBLIC_ADMIN_BASE must share the web origin")
try:
    api_is_loopback = ipaddress.ip_address(api_host).is_loopback
except ValueError:
    api_is_loopback = api_host.lower() == "localhost"
if api_is_loopback:
    raise SystemExit("public-prod lifecycle: PUBLIC_API_BASE must be reachable from Runtime Pods")
print(f"{api_host}\t{api_port}\t{web_host}\t{web_port}\t{admin_host}\t{admin_port}")
PY
)" || return 1
  IFS=$'\t' read -r PUBLIC_API_HOST PUBLIC_API_PORT PUBLIC_WEB_HOST PUBLIC_WEB_PORT PUBLIC_ADMIN_HOST PUBLIC_ADMIN_PORT <<<"$parsed"
  # A public DNS name often terminates on a distinct local IP/port (for
  # example, TLS/Sangine may forward to 15173).  Deployments can declare the
  # exact Nginx bind host/ports; URL-derived values remain the safe default.
  PUBLIC_NGINX_LISTEN_HOST="${PUBLIC_NGINX_LISTEN_HOST:-$PUBLIC_API_HOST}"
  PUBLIC_NGINX_API_LISTEN_HOST="${PUBLIC_NGINX_API_LISTEN_HOST:-$PUBLIC_NGINX_LISTEN_HOST}"
  PUBLIC_NGINX_WEB_LISTEN_PORT="${PUBLIC_NGINX_WEB_LISTEN_PORT:-$PUBLIC_WEB_PORT}"
  PUBLIC_NGINX_API_LISTEN_PORT="${PUBLIC_NGINX_API_LISTEN_PORT:-$PUBLIC_API_PORT}"
  if [[ ! "$PUBLIC_NGINX_LISTEN_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.:-]*$ \
    || ! "$PUBLIC_NGINX_API_LISTEN_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.:-]*$ \
    || ! "$PUBLIC_NGINX_WEB_LISTEN_PORT" =~ ^[1-9][0-9]{1,4}$ \
    || ! "$PUBLIC_NGINX_API_LISTEN_PORT" =~ ^[1-9][0-9]{1,4}$ ]]; then
    echo "public-prod lifecycle: Nginx listener host/ports are invalid." >&2
    return 1
  fi
  export PUBLIC_API_HOST PUBLIC_API_PORT PUBLIC_WEB_HOST PUBLIC_WEB_PORT PUBLIC_ADMIN_HOST PUBLIC_ADMIN_PORT
  export PUBLIC_NGINX_LISTEN_HOST PUBLIC_NGINX_API_LISTEN_HOST PUBLIC_NGINX_WEB_LISTEN_PORT PUBLIC_NGINX_API_LISTEN_PORT
}

public_prod_require_static_artifacts() {
  local path marker
  [[ -n "${PUBLIC_RELEASE_ROOT:-}" ]] || {
    echo "public-prod lifecycle: PUBLIC_RELEASE_ROOT must come from backend/.env" >&2
    return 1
  }
  marker="$PUBLIC_RELEASE_ROOT/.hermes-release-revision"
  if [[ ! -f "$marker" ]] || [[ "$(tr -d '[:space:]' < "$marker")" != "$PUBLIC_RELEASE_REVISION" ]]; then
    echo "public-prod lifecycle: static release marker does not match current release revision: $marker" >&2
    return 1
  fi
  for path in \
    "$PUBLIC_RELEASE_ROOT/frontend/user-web/dist/index.html" \
    "$PUBLIC_RELEASE_ROOT/frontend/admin-web/dist/index.html"; do
    if [[ ! -f "$path" ]]; then
      echo "public-prod lifecycle: 缺少正式静态产物：$path" >&2
      return 1
    fi
    if [[ "$(tr -d '[:space:]' < "$(dirname "$path")/.hermes-release-revision" 2>/dev/null || true)" != "$PUBLIC_RELEASE_REVISION" ]]; then
      echo "public-prod lifecycle: frontend artifact marker does not match current release revision: $path" >&2
      return 1
    fi
  done
}

public_prod_require_enterprise_knowledge_data_service() {
  if [[ -z "${KNOWLEDGE_DATA_SERVICE_BASE_URL:-}" ]]; then
    return 0
  fi
  "$ROOT_DIR/scripts/check-enterprise-knowledge-data-service.sh" \
    --backend-env "${HERMES_ACTIVE_ENV_FILE:-$ROOT_DIR/backend/.env}"
}

public_prod_require_nginx_config() {
  [[ -n "${PUBLIC_NGINX_CONFIG:-}" ]] || {
    echo "public-prod lifecycle: PUBLIC_NGINX_CONFIG must come from backend/.env" >&2
    return 1
  }
  command -v nginx >/dev/null 2>&1 || {
    echo "public-prod lifecycle: nginx is required" >&2
    return 1
  }
  if [[ ! -f "$PUBLIC_NGINX_CONFIG" ]]; then
    echo "public-prod lifecycle: 正式 Nginx 配置未启用：$PUBLIC_NGINX_CONFIG" >&2
    return 1
  fi
  if ! grep -Eq "^[[:space:]]*listen[[:space:]]+${PUBLIC_NGINX_LISTEN_HOST//./\\.}:${PUBLIC_NGINX_WEB_LISTEN_PORT}([[:space:]]|;)" "$PUBLIC_NGINX_CONFIG" \
    || ! grep -Eq "^[[:space:]]*listen[[:space:]]+${PUBLIC_NGINX_API_LISTEN_HOST//./\\.}:${PUBLIC_NGINX_API_LISTEN_PORT}([[:space:]]|;)" "$PUBLIC_NGINX_CONFIG"; then
    echo "public-prod lifecycle: Nginx 配置未声明 PUBLIC_WEB_BASE/PUBLIC_API_BASE 对应的监听入口。" >&2
    return 1
  fi
  if ! grep -Eq "^[[:space:]]*server_name[[:space:]]+${PUBLIC_WEB_HOST//./\\.}([[:space:]]|;)" "$PUBLIC_NGINX_CONFIG"; then
    echo "public-prod lifecycle: Nginx server_name 与 PUBLIC_WEB_BASE 不一致。" >&2
    return 1
  fi
  nginx -t
}

public_prod_backend_process_belongs() {
  local pid="$1"
  local cmdline cwd
  [[ "$pid" =~ ^[1-9][0-9]*$ && -d "/proc/$pid" ]] || return 1
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" || return 1
  [[ "$cwd" == "$ROOT_DIR/backend" || "$cwd" == "$ROOT_DIR/backend"/* ]] || return 1
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || return 1
  [[ "$cmdline" == *"app.main:app"* ]] \
    && [[ "$cmdline" == *"uvicorn"* ]] \
    && [[ "$cmdline" == *"$BACKEND_PORT"* ]]
}

public_prod_stop_backend() {
  local pid_file="$ROOT_DIR/.runtime/public-prod/backend.pid"
  local pid pgid
  [[ -f "$pid_file" ]] || return 0
  pid="$(tr -d '[:space:]' < "$pid_file")"
  if ! public_prod_backend_process_belongs "$pid"; then
    echo "public-prod lifecycle: 移除过期或不受管的 Backend PID 文件：${pid:-empty}" >&2
    rm -f "$pid_file"
    return 0
  fi
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
  [[ "$pgid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -TERM -- "-$pgid"
  for _ in $(seq 1 30); do
    kill -0 "$pid" >/dev/null 2>&1 || break
    sleep 0.2
  done
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -KILL -- "-$pgid"
  fi
  rm -f "$pid_file"
}

public_prod_backend_status() {
  local pid_file="$ROOT_DIR/.runtime/public-prod/backend.pid"
  local pid
  if [[ ! -f "$pid_file" ]]; then
    echo "public-prod backend: not running (no pid file)"
    return 1
  fi
  pid="$(tr -d '[:space:]' < "$pid_file")"
  if ! public_prod_backend_process_belongs "$pid"; then
    echo "public-prod backend: stale or unmanaged pid file${pid:+ pid=$pid}" >&2
    return 1
  fi
  echo "public-prod backend: running pid=$pid host=$BACKEND_HOST port=$BACKEND_PORT"
}

public_prod_dev_backend_is_running() {
  local pid_file="$ROOT_DIR/.runtime/public-dev/backend.pid"
  local pid cwd cmdline
  [[ -f "$pid_file" ]] || return 1
  pid="$(tr -d '[:space:]' < "$pid_file")"
  [[ "$pid" =~ ^[1-9][0-9]*$ && -d "/proc/$pid" ]] || return 1
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" || return 1
  [[ "$cwd" == "$ROOT_DIR/backend" || "$cwd" == "$ROOT_DIR/backend"/* ]] || return 1
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || return 1
  [[ "$cmdline" == *"uvicorn"* && "$cmdline" == *"app.main:app"* ]]
}

public_prod_remove_stale_dev_backend_pid() {
  local pid_file="$ROOT_DIR/.runtime/public-dev/backend.pid"
  local pid
  [[ -f "$pid_file" ]] || return 0
  pid="$(tr -d '[:space:]' < "$pid_file")"
  if public_prod_dev_backend_is_running; then
    return 1
  fi
  rm -f "$pid_file"
  echo "public-prod lifecycle: removed stale public-dev Backend PID file${pid:+ pid=$pid}" >&2
}

public_prod_require_backend_port_free() {
  python3 - "$BACKEND_HOST" "$BACKEND_PORT" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind((host, port))
except OSError as exc:
    raise SystemExit(f"public-prod lifecycle: Backend port {host}:{port} is unavailable: {exc}")
finally:
    sock.close()
PY
}
