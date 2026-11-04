#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/no-proxy-env.sh
source "$ROOT_DIR/scripts/lib/no-proxy-env.sh"
# shellcheck source=scripts/lib/public-prod-lifecycle.sh
source "$ROOT_DIR/scripts/lib/public-prod-lifecycle.sh"
public_prod_load_backend_env

prod_config="$PUBLIC_NGINX_CONFIG"
dev_config="${PUBLIC_NGINX_DEV_CONFIG:-$(dirname "$prod_config")/hermes-enterprise_idp-public-dev.conf}"

if [[ -f "$prod_config" && ! -f "$dev_config" ]]; then
  echo prod
elif [[ -f "$dev_config" && ! -f "$prod_config" ]]; then
  echo dev
else
  echo "invalid (prod=$prod_config dev=$dev_config)" >&2
  exit 1
fi
