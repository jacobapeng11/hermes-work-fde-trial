#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE=${1:-"$ROOT_DIR/enterprise-knowledge"}
DESTINATION=${2:-}

if [[ -z "$DESTINATION" ]]; then
  echo "usage: $0 <source> <empty-destination>" >&2
  exit 2
fi
if [[ ! -f "$SOURCE/PRIMER.md" || ! -f "$SOURCE/workflows/query.md" ]]; then
  echo "source is not a complete enterprise knowledge tree" >&2
  exit 2
fi
mkdir -p "$DESTINATION"
if find "$DESTINATION" -mindepth 1 -print -quit | grep -q .; then
  echo "destination must be empty" >&2
  exit 2
fi

tar -C "$SOURCE" \
  --exclude='./.git' \
  --exclude='./.venv' \
  --exclude='./.memory' \
  --exclude='./var' \
  --exclude='./generated' \
  --exclude='./ui' \
  --exclude='./tests' \
  --exclude='./schemas' \
  --exclude='./contracts' \
  --exclude='./data_service' \
  --exclude='./ontology_compiler.py' \
  --exclude='./serve.py' \
  --exclude='.mcp.json' \
  --exclude='*/.mcp.json' \
  --exclude='.playwright-mcp' \
  --exclude='*/.playwright-mcp' \
  --exclude='.env' \
  --exclude='*/.env' \
  --exclude='.env.*' \
  --exclude='*/.env.*' \
  --exclude='.dc_platform_authorization' \
  --exclude='*/.dc_platform_authorization' \
  --exclude='*.key' \
  --exclude='*.pem' \
  --exclude='*.secret' \
  --exclude='*.token' \
  --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  -cf - . | tar -C "$DESTINATION" -xf -

test -f "$DESTINATION/PRIMER.md"
test -f "$DESTINATION/workflows/query.md"
test ! -e "$DESTINATION/data_service"
test ! -e "$DESTINATION/ontology_compiler.py"
test ! -e "$DESTINATION/generated"
test ! -e "$DESTINATION/ui"
test ! -e "$DESTINATION/serve.py"
test ! -e "$DESTINATION/tests"
test ! -e "$DESTINATION/schemas"
test ! -e "$DESTINATION/contracts"
test ! -e "$DESTINATION/.venv"
test ! -e "$DESTINATION/.memory"
test ! -e "$DESTINATION/var"
test ! -e "$DESTINATION/.mcp.json"
test ! -e "$DESTINATION/.playwright-mcp"
if find "$DESTINATION" -type f \( \
  -name '.env' -o -name '.env.*' -o \
  -name '.dc_platform_authorization' -o \
  -name '*.key' -o -name '*.pem' -o -name '*.secret' -o -name '*.token' \
\) -print -quit | grep -q .; then
  echo "bundle contains an excluded credential file" >&2
  exit 1
fi
echo "enterprise knowledge bundle created at $DESTINATION"
