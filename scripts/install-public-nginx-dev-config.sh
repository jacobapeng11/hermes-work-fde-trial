#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/no-proxy-env.sh
source "$ROOT_DIR/scripts/lib/no-proxy-env.sh"
# shellcheck source=scripts/lib/public-prod-lifecycle.sh
source "$ROOT_DIR/scripts/lib/public-prod-lifecycle.sh"
public_prod_load_backend_env
public_prod_require_topology

prod_config="$PUBLIC_NGINX_CONFIG"
dev_config="${PUBLIC_NGINX_DEV_CONFIG:-}"
[[ -n "$dev_config" ]] || {
  echo "install-public-nginx-dev-config: PUBLIC_NGINX_DEV_CONFIG must come from backend/.env" >&2
  exit 1
}
dev_disabled="${dev_config}.disabled"
template="${PUBLIC_NGINX_DEV_TEMPLATE:-}"
[[ -n "$template" ]] || {
  echo "install-public-nginx-dev-config: PUBLIC_NGINX_DEV_TEMPLATE must come from backend/.env" >&2
  exit 1
}

[[ -f "$prod_config" ]] || {
  echo "install-public-nginx-dev-config: 正式 Nginx 配置不存在：$prod_config" >&2
  exit 1
}
[[ ! -e "$dev_config" && ! -e "$dev_disabled" ]] || {
  echo "install-public-nginx-dev-config: 开发配置已存在，拒绝覆盖：$dev_config" >&2
  exit 1
}

install -m 0644 "$template" "$dev_disabled"
echo "install-public-nginx-dev-config: 开发配置已安装但未启用：$dev_disabled"
