#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/no-proxy-env.sh
source "${REPOSITORY_ROOT}/scripts/lib/no-proxy-env.sh"
exec "${REPOSITORY_ROOT}/backend/.venv/bin/python" \
  "${REPOSITORY_ROOT}/scripts/reload-one-runtime-from-env-dev.py" "$@"
