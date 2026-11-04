#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=scripts/lib/no-proxy-env.sh
source "${REPOSITORY_ROOT}/scripts/lib/no-proxy-env.sh"
cd "$REPOSITORY_ROOT/backend"
exec "$REPOSITORY_ROOT/backend/.venv/bin/python" \
  "$REPOSITORY_ROOT/backend/scripts/runtime_legacy_config_cleanup.py" "$@"
