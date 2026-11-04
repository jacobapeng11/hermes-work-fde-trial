#!/usr/bin/env bash
set -euo pipefail

MCP_CHECK_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$MCP_CHECK_REPO_ROOT"
# Keep all network checks deterministic and independent of shell proxy state.
source scripts/lib/no-proxy-env.sh

BACKEND_TESTS=(
  backend/tests/unit/test_admin_mcp_assets_api.py
  backend/tests/unit/test_asset_catalog.py
  backend/tests/unit/test_asset_rollout.py
  backend/tests/unit/test_asset_rollout_coalescing.py
  backend/tests/unit/test_asset_unification_models.py
  backend/tests/unit/test_current_schema_sql.py
  backend/tests/unit/test_mcp_identity.py
  backend/tests/unit/test_mcp_identity_migration.py
  backend/tests/unit/test_profile_convergence.py
  backend/tests/unit/test_profile_desired.py
  backend/tests/unit/test_runtime_mcp_registry_convergence.py
  backend/tests/unit/test_settings.py
)

PYTHONPATH=.:backend backend/.venv/bin/python -m pytest -q "${BACKEND_TESTS[@]}"
PYTHONPATH=runtime/supervisor backend/.venv/bin/python -m pytest -q \
  runtime/supervisor/tests/test_mcp_registry.py
backend/.venv/bin/ruff check \
  backend/app/api/v1/admin/assets.py \
  backend/app/config.py \
  backend/app/models/assets.py \
  backend/app/schemas/asset_catalog.py \
  backend/app/schemas/asset_projection.py \
  backend/app/services/asset_catalog.py \
  backend/app/services/mcp_identity.py \
  backend/app/services/mcp_identity_migration.py \
  backend/app/services/profile_desired.py \
  backend/app/services/profile_desired_mutations.py \
  backend/app/services/runtime_mcp_registry.py \
  backend/scripts/migrate_mcp_runtime_identity.py \
  runtime/supervisor/platform_supervisor/asset_contracts.py \
  runtime/supervisor/tests/test_mcp_registry.py
pnpm --dir frontend/admin-web test
pnpm --dir frontend/admin-web run test:contracts
pnpm --dir frontend/admin-web run build

echo "MCP asset governance check: ok"
