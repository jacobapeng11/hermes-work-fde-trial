#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/no-proxy-env.sh
source "$ROOT_DIR/scripts/lib/no-proxy-env.sh"
USER_WEB_DIR="$ROOT_DIR/frontend/user-web"
ADMIN_WEB_DIR="$ROOT_DIR/frontend/admin-web"
RUNTIME_DIR="$ROOT_DIR/.runtime/public-frontends-dev"

# shellcheck source=scripts/lib/public-frontend-dev-lifecycle.sh
source "$ROOT_DIR/scripts/lib/public-frontend-dev-lifecycle.sh"
public_frontend_dev_load_environment

if [[ "$("$ROOT_DIR/scripts/public-nginx-mode.sh" 2>/dev/null || true)" == dev ]]; then
  echo "stop-public-frontends-dev: Nginx 仍在 dev 模式；请先执行 scripts/switch-public-nginx-mode.sh prod。" >&2
  exit 1
fi

public_frontend_dev_stop user-web "$RUNTIME_DIR/user-web.pid" "$USER_WEB_DIR" "$USER_WEB_PORT" "$USER_WEB_HOST"
public_frontend_dev_stop admin-web "$RUNTIME_DIR/admin-web.pid" "$ADMIN_WEB_DIR" "$ADMIN_WEB_PORT" "$ADMIN_WEB_HOST"
