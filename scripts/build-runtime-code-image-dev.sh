#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/no-proxy-env.sh
source "${SCRIPT_DIR}/lib/no-proxy-env.sh"
exec "${SCRIPT_DIR}/build-publish-runtime-code-image-dev.py" "$@"
