#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

mode="${1:-}"
case "$mode" in
  dev) exec "$(dirname "$0")/restart-public-dev.sh" "${@:2}" ;;
  prod) exec "$(dirname "$0")/restart-public-prod.sh" "${@:2}" ;;
  *)
    echo "Usage: $0 dev|prod" >&2
    exit 2
    ;;
esac
