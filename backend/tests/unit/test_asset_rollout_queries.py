from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AssetRolloutBatch, AssetRolloutTarget
from app.schemas.asset_rollout import AssetRolloutCommand
from app.services.asset_rollout import AssetRolloutService, refresh_rollout_batch
from app.services.asset_rollout_queries import AssetRolloutQueryService
from app.services.profile_desired import ProfileDesiredService
from tests.unit.test_asset_rollout import _command, _seed_two_profiles
from tests.unit.test_profile_desired import _database, _profile


@pytest.mark.asyncio
async def test_rollout_detail_groups_user_and_profile_progress() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        second_profile = _profile(profile_id=12, user_id=2)
        second_profile.display_name = "Alpha"
        session.add(second_profile)
        await session.commit()
        await ProfileDesiredService(session).replace(_command(profile=second_profile))
        await session.commit()

        await AssetRolloutService(session).create(
            AssetRolloutCommand(
                rollout_id="rollout-monitoring-skill",
                org_id=1,
                asset_type="skill",
                asset_id=101,
                action="publish",
                target_version_id=201,
                created_by_user_id=9,
            )
        )
        batch = await session.scalar(select(AssetRolloutBatch))
        assert batch is not None
        targets = list(
            await session.scalars(
                select(AssetRolloutTarget).order_by(
                    AssetRolloutTarget.user_id,
                    AssetRolloutTarget.runtime_agent_profile_id,
                )
            )
        )
        assert [(item.user_id, item.runtime_agent_profile_id) for item in targets] == [
            (2, 10),
            (2, 12),
            (3, 11),
        ]
        targets[0].status = "applied"
        targets[1].status = "failed"
        targets[1].last_error_code = "SKILL_PACKAGE_INVALID"
        targets[2].status = "failed"
        targets[2].last_error_code = "PROFILE_PROJECTION_FAILED"
        await refresh_rollout_batch(session, batch_id=int(batch.id))
        await session.commit()

        detail = await AssetRolloutQueryService(session).detail(
            org_id=1,
            rollout_id="rollout-monitoring-skill",
        )
        assert detail.total_users == 2
        assert [(item.user_id, item.status) for item in detail.users] == [
            (2, "partial_failed"),
            (3, "failed"),
        ]
        assert [len(item.profiles) for item in detail.users] == [2, 1]
        assert detail.batch.status == "partial_failed"

        filtered = await AssetRolloutQueryService(session).detail(
            org_id=1,
            rollout_id="rollout-monitoring-skill",
            error_code="SKILL_PACKAGE_INVALID",
        )
        assert filtered.total_users == 1
        assert [item.runtime_agent_profile_id for item in filtered.users[0].profiles] == [
            12
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_rollout_targets_can_be_requeued_together_or_individually() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        await AssetRolloutService(session).create(
            AssetRolloutCommand(
                rollout_id="rollout-retry-skill",
                org_id=1,
                asset_type="skill",
                asset_id=101,
                action="publish",
                target_version_id=201,
                created_by_user_id=9,
            )
        )
        batch = await session.scalar(select(AssetRolloutBatch))
        assert batch is not None
        targets = list(await session.scalars(select(AssetRolloutTarget)))
        for target in targets:
            target.status = "failed"
            target.last_error_code = "PROFILE_PROJECTION_FAILED"
        await refresh_rollout_batch(session, batch_id=int(batch.id))
        await session.commit()

        queued = await AssetRolloutService(session).retry_failed(
            org_id=1,
            rollout_id="rollout-retry-skill",
            target_id=int(targets[0].id),
        )
        assert queued == 1
        await session.commit()
        await session.refresh(targets[0])
        assert targets[0].status == "waiting"
        assert targets[0].last_error_code is None

        queued = await AssetRolloutService(session).retry_failed(
            org_id=1,
            rollout_id="rollout-retry-skill",
        )
        assert queued == len(targets) - 1
        await session.commit()
        assert all(
            status == "waiting"
            for status in await session.scalars(select(AssetRolloutTarget.status))
        )
        await session.refresh(batch)
        assert batch.status == "waiting"
        assert batch.completed_at is None
    await engine.dispose()
