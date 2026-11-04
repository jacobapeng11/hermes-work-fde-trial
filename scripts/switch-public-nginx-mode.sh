#!/usr/bin/env bash
set -euo pipefail

mode="${1:-}"
case "$mode" in
  dev|prod) ;;
  *)
    echo "Usage: $0 dev|prod" >&2
    exit 2
    ;;
esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/no-proxy-env.sh
source "$ROOT_DIR/scripts/lib/no-proxy-env.sh"
# shellcheck source=scripts/lib/public-prod-lifecycle.sh
source "$ROOT_DIR/scripts/lib/public-prod-lifecycle.sh"
public_prod_acquire_lock
public_prod_load_backend_env
public_prod_require_topology

prod_config="$PUBLIC_NGINX_CONFIG"
dev_config="${PUBLIC_NGINX_DEV_CONFIG:-}"
[[ -n "$dev_config" ]] || {
  echo "switch-public-nginx-mode: PUBLIC_NGINX_DEV_CONFIG must come from backend/.env" >&2
  exit 1
}
prod_disabled="${prod_config}.disabled"
dev_disabled="${dev_config}.disabled"
prod_template="${PUBLIC_NGINX_PROD_TEMPLATE:-}"
dev_template="${PUBLIC_NGINX_DEV_TEMPLATE:-}"
[[ -n "$prod_template" && -n "$dev_template" ]] || {
  echo "switch-public-nginx-mode: PUBLIC_NGINX_PROD_TEMPLATE and PUBLIC_NGINX_DEV_TEMPLATE must come from backend/.env" >&2
  exit 1
}
template="$prod_template"
[[ "$mode" == dev ]] && template="$dev_template"
[[ -f "$template" ]] || {
  echo "switch-public-nginx-mode: repository template missing: $template" >&2
  exit 1
}

current="$("$ROOT_DIR/scripts/public-nginx-mode.sh" 2>/dev/null || true)"
[[ "$current" == "$mode" ]] && {
  target_config="$prod_config"
  [[ "$mode" == dev ]] && target_config="$dev_config"
  install -m 0644 "$template" "$target_config"
  nginx -t
  nginx -s reload
  echo "switch-public-nginx-mode: already in $mode mode"
  exit 0
}
[[ "$current" == dev || "$current" == prod ]] || {
  echo "switch-public-nginx-mode: 当前 Nginx 模式不受管理；prod=$prod_config dev=$dev_config" >&2
  exit 1
}

if [[ "$mode" == dev ]]; then
  curl -fsS "http://${USER_WEB_HOST}:${USER_WEB_PORT}/user/chat" >/dev/null || {
    echo "switch-public-nginx-mode: user-web Vite 未就绪" >&2
    exit 1
  }
  curl -fsS "http://${ADMIN_WEB_HOST}:${ADMIN_WEB_PORT}/admin/" >/dev/null || {
    echo "switch-public-nginx-mode: admin-web Vite 未就绪" >&2
    exit 1
  }
  install -m 0644 "$dev_template" "$dev_disabled"
  [[ -f "$prod_disabled" ]] || install -m 0644 "$prod_template" "$prod_disabled"
  mv "$prod_config" "$prod_disabled"
  mv "$dev_disabled" "$dev_config"
else
  [[ -f "$PUBLIC_RELEASE_ROOT/frontend/user-web/dist/index.html" ]] \
    && [[ -f "$PUBLIC_RELEASE_ROOT/frontend/admin-web/dist/index.html" ]] || {
    echo "switch-public-nginx-mode: 正式静态产物不完整" >&2
    exit 1
  }
  install -m 0644 "$prod_template" "$prod_disabled"
  [[ -f "$dev_disabled" ]] || install -m 0644 "$dev_template" "$dev_disabled"
  mv "$dev_config" "$dev_disabled"
  mv "$prod_disabled" "$prod_config"
fi

restore_previous_mode() {
  if [[ "$mode" == dev ]]; then
    mv "$dev_config" "$dev_disabled" 2>/dev/null || true
    mv "$prod_disabled" "$prod_config" 2>/dev/null || true
  else
    mv "$prod_config" "$prod_disabled" 2>/dev/null || true
    mv "$dev_disabled" "$dev_config" 2>/dev/null || true
  fi
}

if ! nginx -t; then
  restore_previous_mode
  echo "switch-public-nginx-mode: Nginx 校验失败，已恢复 $current 模式" >&2
  exit 1
fi
if ! nginx -s reload; then
  restore_previous_mode
  echo "switch-public-nginx-mode: reload 失败，已恢复配置文件；请手动检查 Nginx 运行状态" >&2
  exit 1
fi
echo "switch-public-nginx-mode: switched to $mode mode"
