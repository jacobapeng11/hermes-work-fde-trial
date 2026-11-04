#!/usr/bin/env bash

# Shared helpers for the frontend-only public development overlay.  This
# overlay deliberately owns only Vite processes; the formal Backend keeps
# serving the loopback listener declared in backend/.env throughout its lifetime.

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/no-proxy-env.sh"
# shellcheck source=scripts/lib/public-prod-lifecycle.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/public-prod-lifecycle.sh"
# shellcheck source=scripts/lib/public-frontend-auth-config.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/public-frontend-auth-config.sh"

public_frontend_dev_load_environment() {
  public_prod_load_backend_env
  public_prod_require_topology
  public_frontend_require_legacy_sso_config

  for name in USER_WEB_HOST USER_WEB_PORT ADMIN_WEB_HOST ADMIN_WEB_PORT; do
    if [[ -z "${!name:-}" ]]; then
      echo "public frontend dev: $name 必须来自 backend/.env" >&2
      return 1
    fi
  done
  export USER_WEB_HOST USER_WEB_PORT ADMIN_WEB_HOST ADMIN_WEB_PORT
}

public_frontend_dev_hmr_environment() {
  python3 - "$PUBLIC_WEB_BASE" <<'PY'
from urllib.parse import urlparse
import sys

parsed = urlparse(sys.argv[1])
if parsed.scheme != "https" or not parsed.hostname:
    raise SystemExit("public frontend dev: PUBLIC_WEB_BASE 必须是 HTTPS 公开地址。")
print(parsed.hostname)
print(parsed.port or 443)
PY
}

public_frontend_dev_process_belongs() {
  local pid="$1"
  local package_dir="$2"
  local port="$3"
  local cwd cmdline

  [[ "$pid" =~ ^[1-9][0-9]*$ && -d "/proc/$pid" ]] || return 1
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" || return 1
  [[ "$cwd" == "$package_dir" || "$cwd" == "$package_dir"/* ]] || return 1
  cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || return 1
  [[ "$cmdline" == *"vite"* || "$cmdline" == *"npm run dev"* ]] \
    && [[ "$cmdline" == *"$port"* ]]
}

public_frontend_dev_listener_pids() {
  local host="$1"
  local port="$2"
  [[ -n "$host" && -n "$port" ]] || return 0
  ss -H -ltnp "sport = :$port" 2>/dev/null \
    | python3 -c '
import re
import sys

host, port = sys.argv[1:]
targets = {f"{host}:{port}", f"0.0.0.0:{port}", f"[::]:{port}"}
seen: set[str] = set()
for line in sys.stdin:
    fields = line.split()
    if len(fields) < 4 or fields[3] not in targets:
        continue
    for pid in re.findall(r"pid=(\d+)", line):
        if pid not in seen:
            seen.add(pid)
            print(pid)
' "$host" "$port"
}

public_frontend_dev_port_is_free() {
  local host="$1"
  local port="$2"
  python3 - "$host" "$port" <<'PY'
import socket
import sys

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind((sys.argv[1], int(sys.argv[2])))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}

public_frontend_dev_wait_url() {
  local url="$1"
  local name="$2"
  local _
  for _ in $(seq 1 60); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  echo "public frontend dev: timed out waiting for $name at $url" >&2
  return 1
}

public_frontend_dev_launch() {
  local pid_file="$1"
  local package_dir="$2"
  local log_file="$3"
  local host="$4"
  local port="$5"
  local hmr_host="$6"
  local hmr_port="$7"
  local frontend_auth_mode="${VITE_AUTH_MODE:-$HERMES_AUTH_MODE}"
  local admin_frontend_auth_mode="${VITE_ADMIN_AUTH_MODE:-$HERMES_ADMIN_AUTH_MODE}"
  local hmr_protocol="${VITE_HMR_PROTOCOL:-wss}"

  rm -f "$pid_file"
  (
    cd "$package_dir"
    exec setsid bash -c '
      pid_file="$1"
      shift
      echo "$$" > "$pid_file"
      exec "$@"
    ' _ "$pid_file" \
      env VITE_AUTH_MODE="$frontend_auth_mode" \
        VITE_ADMIN_AUTH_MODE="$admin_frontend_auth_mode" \
        VITE_LEGACY_SSO_LOGIN_BASE_URL="$VITE_LEGACY_SSO_LOGIN_BASE_URL" \
        VITE_LEGACY_SSO_APP_NAME="$VITE_LEGACY_SSO_APP_NAME" \
        VITE_BACKEND_PROXY_TARGET="http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}" \
        VITE_HMR_PROTOCOL="$hmr_protocol" \
        VITE_HMR_HOST="$hmr_host" \
        VITE_HMR_CLIENT_PORT="$hmr_port" \
        npm run dev -- --host "$host" --port "$port" --strictPort
  ) >"$log_file" 2>&1 &

  local _
  for _ in $(seq 1 50); do
    if [[ -s "$pid_file" ]] && kill -0 "$(tr -d '[:space:]' < "$pid_file")" 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  echo "public frontend dev: Vite failed to publish a live PID for $package_dir" >&2
  return 1
}

public_frontend_dev_stop() {
  local label="$1"
  local pid_file="$2"
  local package_dir="$3"
  local port="$4"
  local host="$5"
  local pid pgid listener_pid

  if [[ -f "$pid_file" ]]; then
    pid="$(tr -d '[:space:]' < "$pid_file")"
    if [[ -d "/proc/$pid" ]]; then
      if ! public_frontend_dev_process_belongs "$pid" "$package_dir" "$port"; then
        echo "$label: refused to signal unowned pid=${pid:-empty}" >&2
        return 1
      fi
      pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
      [[ "$pgid" =~ ^[1-9][0-9]*$ ]] || return 1
      kill -TERM -- "-$pgid"
      rm -f "$pid_file"
      echo "$label: stopped pid=$pid"
      return 0
    fi
    rm -f "$pid_file"
  fi

  # An interrupted launcher can leave Vite alive after its PID file is lost.
  # Discover only listeners on the configured port, then retain the same
  # cwd/cmdline ownership check before signalling their process group.
  while IFS= read -r listener_pid; do
    [[ -n "$listener_pid" ]] || continue
    if ! public_frontend_dev_process_belongs "$listener_pid" "$package_dir" "$port"; then
      echo "$label: refused to signal unowned listener pid=$listener_pid" >&2
      return 1
    fi
    pgid="$(ps -o pgid= -p "$listener_pid" 2>/dev/null | tr -d '[:space:]')"
    [[ "$pgid" =~ ^[1-9][0-9]*$ ]] || return 1
    kill -TERM -- "-$pgid"
    echo "$label: stopped orphan listener pid=$listener_pid"
  done < <(public_frontend_dev_listener_pids "$host" "$port")
  rm -f "$pid_file"
  echo "$label: not running"
}
