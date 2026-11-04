#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${1:-"$ROOT_DIR/enterprise-knowledge"}"
DESTINATION="${2:-/data/hermes/enterprise-knowledge}"

if ! command -v rsync >/dev/null 2>&1; then
  echo "publish-enterprise-knowledge-bundle: rsync is required" >&2
  exit 2
fi

source_path="$(realpath -m "$SOURCE")"
destination_path="$(realpath -m "$DESTINATION")"
if [[ "$destination_path" == "/" || "$destination_path" == "$source_path" || "$destination_path" == "$source_path/"* ]]; then
  echo "publish-enterprise-knowledge-bundle: unsafe destination $destination_path" >&2
  exit 2
fi

destination_parent="$(dirname "$destination_path")"
mkdir -p "$destination_parent" "$destination_path"
stage="$(mktemp -d "$destination_parent/.enterprise-knowledge-stage.XXXXXX")"
cleanup() {
  rm -rf "$stage"
}
trap cleanup EXIT

bash "$ROOT_DIR/scripts/build-enterprise-knowledge-bundle.sh" "$source_path" "$stage"
rsync --archive --delete --chmod=D755,F644 "$stage/" "$destination_path/"

test -r "$destination_path/PRIMER.md"
test -r "$destination_path/workflows/query.md"
test ! -e "$destination_path/data_service"
echo "enterprise knowledge bundle published at $destination_path"
