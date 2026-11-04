#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"

if [[ ! -x "$BACKEND_DIR/.venv/bin/pytest" ]]; then
  echo "Gate 类型: environment" >&2
  echo "阻塞项: backend/.venv/bin/pytest 不存在。" >&2
  exit 2
fi

cd "$ROOT_DIR"
"$BACKEND_DIR/.venv/bin/pytest" -q \
	backend/tests/unit/test_asset_catalog.py \
	backend/tests/unit/test_profile_desired.py \
	backend/tests/unit/test_default_profile_assets_api.py \
	backend/tests/unit/test_expert_profile_desired_cutover.py \
	backend/tests/unit/test_profile_convergence.py \
	backend/tests/unit/test_runtime_mcp_registry_convergence.py \
	backend/tests/unit/test_skillhub_integration.py \
	backend/tests/unit/test_runtime_storage_mapping.py \
	backend/tests/unit/test_asset_unification_migration.py

echo "plaza assets local contract smoke: ok"
