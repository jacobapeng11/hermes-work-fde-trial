#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
USER_WEB_DIR="$ROOT_DIR/frontend/user-web"
ADMIN_WEB_DIR="$ROOT_DIR/frontend/admin-web"

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-dev-lifecycle.sh"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-mode-handoff.sh"
public_mode_acquire_lock
public_dev_acquire_lock

stop_status=0
runtime_config_available=1
if ! public_dev_load_backend_env; then
  echo "stop-public-dev: 无法读取 backend/.env，无法确定 Runtime 命名空间。" >&2
  runtime_config_available=0
  stop_status=1
fi
public_dev_resolve_ports
public_mode_switch_nginx prod || exit 1

if ! public_mode_stop_registered_full_dev; then
  stop_status=1
fi

exit "$stop_status"
