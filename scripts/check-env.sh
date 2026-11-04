#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

# This is a read-only preflight; backend/.env remains the sole runtime source.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
ENV_FILE="$BACKEND_DIR/.env"

mode="${1:-${HERMES_PUBLIC_MODE:-dev}}"
case "$mode" in
  dev|prod) ;;
  *) echo "Usage: $0 [dev|prod]" >&2; exit 2 ;;
esac
if [[ -f "$ROOT_DIR/docs/architecture/current/configuration/registry.yaml" ]]; then
  python3 "$ROOT_DIR/scripts/check-env-registry.py" --env-file "$ENV_FILE" --root "$ROOT_DIR"
fi
exec python3 "$ROOT_DIR/scripts/check-env-alignment.py" --env-file "$ENV_FILE" --mode "$mode" --root "$ROOT_DIR"
