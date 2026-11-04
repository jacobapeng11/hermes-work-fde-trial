#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

# Reports only the Backend recorded by the formal public-prod lifecycle.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-prod-lifecycle.sh"
public_prod_acquire_lock
public_prod_load_backend_env

if public_prod_backend_status; then
  exit 0
fi
exit 1
