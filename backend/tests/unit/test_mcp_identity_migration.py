from __future__ import annotations

import pytest

from app.services.mcp_identity_migration import McpIdentityMigrationService
from tests.unit.test_asset_rollout import _seed_two_profiles
from tests.unit.test_profile_desired import _database


@pytest.mark.asyncio
async def test_mcp_identity_migration_plans_one_stable_user_cohort_without_writes() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        result = await McpIdentityMigrationService(session).run(
            operation_id="mcp-identity-canary-test",
            created_by_user_id=9,
            user_id=2,
            apply=False,
        )

        assert result.selected_user_count == 1
        assert result.selected_workspace_count == 1
        assert result.selected_profile_count == 1
        assert result.refreshed_profile_count == 0
        assert result.profile_ids == (10,)
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_identity_migration_apply_is_idempotent_for_asset_id_snapshots() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        service = McpIdentityMigrationService(session)
        first = await service.run(
            operation_id="mcp-identity-all-test-1",
            created_by_user_id=9,
            user_id=None,
            apply=True,
        )
        second = await service.run(
            operation_id="mcp-identity-all-test-2",
            created_by_user_id=9,
            user_id=None,
            apply=True,
        )

        assert first.selected_user_count == 2
        assert first.selected_profile_count == 2
        assert first.refreshed_profile_count == 0
        assert second.refreshed_profile_count == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_identity_migration_rejects_apply_outside_enabled_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        monkeypatch.setattr(
            "app.services.mcp_identity_migration.settings.MCP_RUNTIME_IDENTITY_MODE",
            "canary",
        )
        monkeypatch.setattr(
            "app.services.mcp_identity_migration.settings.MCP_RUNTIME_IDENTITY_CANARY_USER_IDS",
            "3",
        )

        with pytest.raises(RuntimeError, match="does not enable selected users: 2"):
            await McpIdentityMigrationService(session).run(
                operation_id="mcp-identity-wrong-canary",
                created_by_user_id=9,
                user_id=2,
                apply=True,
            )
    await engine.dispose()
