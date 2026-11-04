#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/no-proxy-env.sh
source "$ROOT_DIR/scripts/lib/no-proxy-env.sh"
USER_WEB_DIR="$ROOT_DIR/frontend/user-web"
ADMIN_WEB_DIR="$ROOT_DIR/frontend/admin-web"
RUNTIME_DIR="$ROOT_DIR/.runtime/public-frontends-dev"
LOG_DIR="$ROOT_DIR/logs/public-frontends-dev"

# shellcheck source=scripts/lib/public-frontend-dev-lifecycle.sh
source "$ROOT_DIR/scripts/lib/public-frontend-dev-lifecycle.sh"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-dev-lifecycle.sh"

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"
public_frontend_dev_load_environment

[[ -d "$USER_WEB_DIR/node_modules" && -d "$ADMIN_WEB_DIR/node_modules" ]] || {
  echo "start-public-frontends-dev: 前端依赖缺失，请先安装 node_modules。" >&2
  exit 1
}
frontend_backend_mode="${PUBLIC_FRONTENDS_BACKEND_MODE:-prod}"
case "$frontend_backend_mode" in
  prod)
    bash "$ROOT_DIR/scripts/status-public-prod.sh" >/dev/null 2>&1 || {
      echo "start-public-frontends-dev: 未找到登记且运行中的正式 Backend，未启动 Vite。" >&2
      exit 1
    }
    ;;
  dev)
    # The complete dev project intentionally pairs Vite with the managed
    # loopback dev Backend.  Verify ownership before reusing that listener;
    # never infer ownership from a port alone.
    public_dev_load_backend_env
    public_dev_resolve_ports
    dev_backend_pid_file="$ROOT_DIR/.runtime/public-dev/backend.pid"
    dev_backend_pid=""
    if [[ -f "$dev_backend_pid_file" ]]; then
      dev_backend_pid="$(tr -d '[:space:]' < "$dev_backend_pid_file")"
    fi
    public_dev_process_belongs "$dev_backend_pid" backend "$ROOT_DIR/backend" "$BACKEND_PORT" || {
      echo "start-public-frontends-dev: 未找到登记且归属当前工作区的 dev Backend，未启动 Vite。" >&2
      exit 1
    }
    ;;
  *)
    echo "start-public-frontends-dev: PUBLIC_FRONTENDS_BACKEND_MODE must be prod or dev." >&2
    exit 2
    ;;
esac
curl -fsS "http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}/api/v1/readyz" >/dev/null || {
  echo "start-public-frontends-dev: Backend 未就绪，未启动 Vite。" >&2
  exit 1
}

for service in user-web admin-web; do
  pid_file="$RUNTIME_DIR/$service.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(tr -d '[:space:]' < "$pid_file")" 2>/dev/null; then
    echo "start-public-frontends-dev: $service 已在运行；请先停止或复用现有服务。" >&2
    exit 1
  fi
  rm -f "$pid_file"
done
public_frontend_dev_port_is_free "$USER_WEB_HOST" "$USER_WEB_PORT" || {
  echo "start-public-frontends-dev: user-web 端口被占用：$USER_WEB_HOST:$USER_WEB_PORT" >&2
  exit 1
}
public_frontend_dev_port_is_free "$ADMIN_WEB_HOST" "$ADMIN_WEB_PORT" || {
  echo "start-public-frontends-dev: admin-web 端口被占用：$ADMIN_WEB_HOST:$ADMIN_WEB_PORT" >&2
  exit 1
}

mapfile -t hmr_values < <(public_frontend_dev_hmr_environment)
hmr_host="${hmr_values[0]}"
hmr_port="${hmr_values[1]}"

cleanup() {
  "$ROOT_DIR/scripts/stop-public-frontends-dev.sh" >/dev/null 2>&1 || true
}
trap cleanup ERR INT TERM

public_frontend_dev_launch "$RUNTIME_DIR/user-web.pid" "$USER_WEB_DIR" "$LOG_DIR/user-web.log" \
  "$USER_WEB_HOST" "$USER_WEB_PORT" "$hmr_host" "$hmr_port"
public_frontend_dev_wait_url "http://$USER_WEB_HOST:$USER_WEB_PORT/user/chat" user-web
public_frontend_dev_launch "$RUNTIME_DIR/admin-web.pid" "$ADMIN_WEB_DIR" "$LOG_DIR/admin-web.log" \
  "$ADMIN_WEB_HOST" "$ADMIN_WEB_PORT" "$hmr_host" "$hmr_port"
public_frontend_dev_wait_url "http://$ADMIN_WEB_HOST:$ADMIN_WEB_PORT/admin/" admin-web
trap - ERR INT TERM

cat <<EOF
Public frontend development overlay is running.

- User Vite:  http://$USER_WEB_HOST:$USER_WEB_PORT/user/chat
- Admin Vite: http://$ADMIN_WEB_HOST:$ADMIN_WEB_PORT/admin/
- Backend:    reused at http://${BACKEND_LOCAL_HOST}:${BACKEND_PORT}

To expose both Vite applications through the public HTTPS domain:
  bash scripts/switch-public-nginx-mode.sh dev

To return to the published static frontend:
  bash scripts/switch-public-nginx-mode.sh prod
  bash scripts/stop-public-frontends-dev.sh
EOF
