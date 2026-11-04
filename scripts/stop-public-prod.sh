#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

# Stops only the Backend recorded by the formal public-prod lifecycle.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/lib/public-prod-lifecycle.sh"
public_prod_acquire_lock
public_prod_load_backend_env

public_prod_stop_backend
echo "stop-public-prod: Backend stopped; Nginx, static files, database, and dev services were not changed."
