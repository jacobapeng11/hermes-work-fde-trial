from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import (
    AssetRolloutTarget,
    Mcp,
    McpVersion,
    ProfileConvergenceJob,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    Skill,
    SkillVersion,
)
from app.schemas.asset_rollout import AssetRolloutCommand
from app.services.asset_rollout import (
    AssetRolloutService,
    AssetRolloutWorker,
    refresh_rollout_targets_for_profile,
)
from tests.unit.test_asset_rollout import _seed_two_profiles
from tests.unit.test_profile_desired import _database


@pytest.mark.asyncio
async def test_different_asset_targets_coalesce_into_latest_profile_revision() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        skill = await session.get(Skill, 101)
        mcp = await session.get(Mcp, 301)
        assert skill is not None and mcp is not None
        session.add_all(
            [
                SkillVersion(
                    id=202,
                    org_id=1,
                    skill_id=101,
                    version=2,
                    package_uri="/data/assets/skills/101/202",
                    package_sha256="a" * 64,
                    manifest={"name": "skill-101-v2"},
                    status="published",
                    published_at=datetime.utcnow(),
                ),
                McpVersion(
                    id=402,
                    org_id=1,
                    mcp_id=301,
                    version=2,
                    transport="streamable_http",
                    connection_config={"url": "https://mcp-v2.example.com"},
                    config_digest="sha256:" + "6" * 64,
                    tools_schema=[{"name": "search_v2"}],
                    tools_schema_digest="sha256:" + "7" * 64,
                    status="published",
                    published_at=datetime.utcnow(),
                ),
            ]
        )
        skill.current_version_id = 202
        mcp.current_version_id = 402
        rollouts = AssetRolloutService(session)
        await rollouts.create(
            AssetRolloutCommand(
                rollout_id="coalesce-skill-v2",
                org_id=1,
                asset_type="skill",
                asset_id=101,
                action="publish",
                source_version_id=201,
                target_version_id=202,
                created_by_user_id=9,
            )
        )
        await rollouts.create(
            AssetRolloutCommand(
                rollout_id="coalesce-mcp-v2",
                org_id=1,
                asset_type="mcp",
                asset_id=301,
                action="publish",
                source_version_id=401,
                target_version_id=402,
                created_by_user_id=9,
            )
        )
        await session.commit()

    worker = AssetRolloutWorker(session_factory=sessions, concurrency=1)
    assert await worker.reconcile_once() == 4

    async with sessions() as session:
        profiles = list(
            await session.scalars(
                select(RuntimeAgentProfile).order_by(RuntimeAgentProfile.id)
            )
        )
        assert [item.desired_revision for item in profiles] == [2, 2]
        jobs = list(
            await session.scalars(
                select(ProfileConvergenceJob).order_by(
                    ProfileConvergenceJob.runtime_agent_profile_id
                )
            )
        )
        assert [item.target_revision for item in jobs] == [2, 2]
        latest = list(
            await session.scalars(
                select(RuntimeAgentProfileRevision)
                .where(RuntimeAgentProfileRevision.revision == 2)
                .order_by(RuntimeAgentProfileRevision.runtime_agent_profile_id)
            )
        )
        assert len(latest) == 2
        assert all(item.snapshot["skills"][0]["skill_version_id"] == 202 for item in latest)
        assert all(item.snapshot["mcps"][0]["mcp_version_id"] == 402 for item in latest)

        targets = list(await session.scalars(select(AssetRolloutTarget)))
        assert {item.target_revision for item in targets} == {2}
        for profile in profiles:
            await refresh_rollout_targets_for_profile(
                session,
                runtime_agent_profile_id=int(profile.id),
                applied_revision=2,
                status="applied",
            )
        await session.commit()
        assert {item.status for item in targets} == {"applied"}
    await engine.dispose()
