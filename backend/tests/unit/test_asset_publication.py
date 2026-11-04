from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AssetRolloutBatch
from app.schemas.asset_catalog import (
    McpVersionMaterial,
    PublishMcpVersionCommand,
    PublishSkillVersionCommand,
    SkillVersionMaterial,
)
from app.services.asset_publication import PublicAssetPublicationService
from tests.unit.test_asset_rollout import _seed_two_profiles
from tests.unit.test_profile_desired import _database


@pytest.mark.asyncio
async def test_public_skill_publication_automatically_freezes_rollout() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        result = await PublicAssetPublicationService(session).publish_skill(
            command=PublishSkillVersionCommand(
                org_id=1,
                skill_id=101,
                material=SkillVersionMaterial(
                    package_uri="/data/assets/skills/101/v2",
                    package_sha256="b" * 64,
                    manifest={"name": "skill-101-v2"},
                ),
            ),
            rollout_id="publish-skill-101-v2",
            created_by_user_id=9,
        )
        await session.commit()

        batch = await session.scalar(select(AssetRolloutBatch))
        assert result.catalog is not None and result.catalog.created_version is True
        assert result.rollout is not None
        assert result.rollout.affected_user_count == 2
        assert result.rollout.affected_profile_count == 2
        assert batch is not None
        assert batch.source_version_id == 201
        assert batch.target_version_id == result.catalog.version_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_public_mcp_publication_automatically_freezes_rollout() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        result = await PublicAssetPublicationService(session).publish_mcp(
            command=PublishMcpVersionCommand(
                org_id=1,
                mcp_id=301,
                material=McpVersionMaterial(
                    transport="streamable_http",
                    connection_config={"url": "https://mcp-v2.example.com"},
                    secret_refs={"Authorization": "Bearer v2-test-token"},
                    tools_schema=[{"name": "search_v2"}],
                ),
            ),
            rollout_id="publish-mcp-301-v2",
            created_by_user_id=9,
        )
        await session.commit()

        batch = await session.scalar(select(AssetRolloutBatch))
        assert result.catalog is not None and result.catalog.created_version is True
        assert result.rollout is not None
        assert result.rollout.affected_user_count == 2
        assert result.rollout.affected_profile_count == 2
        assert batch is not None
        assert batch.source_version_id == 401
        assert batch.target_version_id == result.catalog.version_id
    await engine.dispose()
