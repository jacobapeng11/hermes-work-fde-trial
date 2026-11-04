from pathlib import Path
from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.models import (
    AssetRolloutBatch,
    AssetRolloutTarget,
    ExpertPackage,
    ExpertPackageVersion,
    ProfileConvergenceJob,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    Skill,
    SkillVersion,
)
from app.schemas.profile_desired import (
    DefaultProfileAssetMutation,
    ProfileDesiredSkillRef,
)
from app.services.personal_skill_lifecycle import PersonalSkillLifecycleService
from app.services.asset_rollout import AssetRolloutWorker
from app.services.profile_desired import ProfileDesiredService
from app.services.profile_desired_mutations import ProfileDesiredMutationService
from tests.unit.test_profile_desired import (
    _command,
    _database,
    _profile,
    _seed,
    _skill,
)


@pytest.mark.asyncio
async def test_default_single_asset_commands_merge_into_complete_desired() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        service = ProfileDesiredMutationService(session)
        operations = [
            ("skill", "install", 101, "operation-skill-install"),
            ("mcp", "install", 301, "operation-mcp-install"),
            ("skill", "uninstall", 101, "operation-skill-remove"),
            ("skill", "uninstall", 101, "operation-skill-remove-again"),
            ("knowledge", "install", 999, "operation-knowledge-install"),
        ]
        results = []
        for asset_type, action, asset_id, operation_id in operations:
            results.append(
                await service.mutate_default_asset(
                    DefaultProfileAssetMutation(
                        org_id=1,
                        user_id=2,
                        runtime_workspace_id=3,
                        runtime_agent_profile_id=10,
                        asset_type=asset_type,
                        action=action,
                        asset_id=asset_id,
                        operation_id=operation_id,
                    )
                )
            )
        await session.commit()

        assert [item.desired_revision for item in results] == [1, 2, 3, 3, 4]
        assert results[3].created_revision is False
        latest = await session.scalar(
            select(RuntimeAgentProfileRevision)
            .where(RuntimeAgentProfileRevision.runtime_agent_profile_id == 10)
            .order_by(RuntimeAgentProfileRevision.revision.desc())
        )
        assert latest is not None
        assert latest.snapshot["skills"] == []
        assert [item["mcp_id"] for item in latest.snapshot["mcps"]] == [301]
        assert latest.snapshot["knowledge"]["personal_knowledge_base_ids"] == [999]
    await engine.dispose()


@pytest.mark.asyncio
async def test_personal_skill_delete_unbinds_default_and_private_expert_profiles() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        personal, personal_version = _skill(
            skill_id=102,
            version_id=202,
            owner_user_id=2,
            asset_scope="personal",
        )
        other_user_market_profile = _profile(
            profile_id=12,
            user_id=3,
            expert_package_id=701,
            ownership_type="market_installed",
        )
        session.add_all([
            personal,
            personal_version,
            _profile(profile_id=10),
            _profile(profile_id=11),
            other_user_market_profile,
            ExpertPackage(
                id=701,
                org_id=1,
                owner_user_id=2,
                slug="personal-skill-expert",
                name="Personal Skill Expert",
                status="published",
                visibility="public",
                current_version_id=711,
            ),
            ExpertPackageVersion(
                id=711,
                org_id=1,
                expert_package_id=701,
                version=1,
                manifest={},
                basic_info_snapshot={"name": "Personal Skill Expert"},
                skill_refs=[{"source": "personal", "asset_id": 102}],
                mcp_refs=[],
                knowledge_refs=[],
                plugin_refs=[],
                status="published",
            ),
        ])
        await session.commit()
        desired = ProfileDesiredService(session)
        for profile_id, user_id in ((10, 2), (11, 2), (12, 3)):
            command = _command(
                    profile_id=profile_id,
                    user_id=user_id,
                    skills=[ProfileDesiredSkillRef(skill_id=102)],
                    mcps=[],
                    knowledge_base_ids=[],
                )
            if profile_id == 12:
                command = command.model_copy(update={
                    "source_type": "market_expert",
                    "expert_package_version_id": 711,
                })
            await desired.replace(command)
        await session.flush()

        removal = await PersonalSkillLifecycleService(
            session
        ).delete(
            org_id=1,
            user_id=2,
            skill_id=102,
            operation_id="personal-skill-delete-0001",
        )
        await session.commit()

        skill = await session.get(Skill, 102)
        version = await session.get(SkillVersion, 202)
        assert removal.affected_profile_ids == (10, 11, 12)
        assert removal.desired_results == ()
        assert skill is not None and skill.status == "deleted"
        assert version is not None and version.status == "private"
        batch = await session.scalar(
            select(AssetRolloutBatch).where(
                AssetRolloutBatch.asset_type == "skill",
                AssetRolloutBatch.asset_id == 102,
                AssetRolloutBatch.action == "delete",
            )
        )
        assert batch is not None
        assert batch.affected_user_count == 2
        assert batch.affected_profile_count == 3
        assert await session.scalar(select(func.count(ProfileSkillBinding.id))) == 3
        assert await session.scalar(
            select(func.count(RuntimeAgentProfileRevision.id))
        ) == 3

    assert await AssetRolloutWorker(session_factory=sessions).reconcile_once() == 3

    async with sessions() as session:
        assert await session.scalar(select(func.count(ProfileSkillBinding.id))) == 0
        targets = list(await session.scalars(select(AssetRolloutTarget)))
        assert len(targets) == 3
        for profile_id in (10, 11, 12):
            latest = await session.scalar(
                select(RuntimeAgentProfileRevision)
                .where(RuntimeAgentProfileRevision.runtime_agent_profile_id == profile_id)
                .order_by(RuntimeAgentProfileRevision.revision.desc())
            )
            assert latest is not None and latest.revision == 2
            assert latest.snapshot["skills"] == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_rapid_personal_skill_deletes_coalesce_deleted_refs() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        first, first_version = _skill(
            skill_id=102,
            version_id=202,
            owner_user_id=2,
            asset_scope="personal",
        )
        second, second_version = _skill(
            skill_id=103,
            version_id=203,
            owner_user_id=2,
            asset_scope="personal",
        )
        session.add_all([
            first,
            first_version,
            second,
            second_version,
            _profile(),
        ])
        await session.commit()
        await ProfileDesiredService(session).replace(
            _command(
                skills=[
                    ProfileDesiredSkillRef(skill_id=102),
                    ProfileDesiredSkillRef(skill_id=103),
                ],
                mcps=[],
                knowledge_base_ids=[],
            )
        )
        for skill_id in (102, 103):
            await PersonalSkillLifecycleService(session).delete(
                org_id=1,
                user_id=2,
                skill_id=skill_id,
                operation_id=f"rapid-personal-skill-delete-{skill_id}",
            )
        await session.commit()

    worker = AssetRolloutWorker(session_factory=sessions, concurrency=1)
    assert await worker.reconcile_once() == 2

    async with sessions() as session:
        assert await session.scalar(select(func.count(ProfileSkillBinding.id))) == 0
        targets = list(await session.scalars(select(AssetRolloutTarget)))
        assert len(targets) == 2
        assert all(target.status != "failed" for target in targets)
        latest = await session.scalar(
            select(RuntimeAgentProfileRevision)
            .where(RuntimeAgentProfileRevision.runtime_agent_profile_id == 10)
            .order_by(RuntimeAgentProfileRevision.revision.desc())
        )
        assert latest is not None
        assert latest.snapshot["skills"] == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_delete_rollout_preserves_another_emergency_disabled_binding() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        revoked, revoked_version = _skill(skill_id=101, version_id=201)
        deleted, deleted_version = _skill(
            skill_id=102,
            version_id=202,
            owner_user_id=2,
            asset_scope="personal",
        )
        profile = _profile()
        session.add_all([
            revoked,
            revoked_version,
            deleted,
            deleted_version,
            profile,
        ])
        await session.commit()
        await ProfileDesiredService(session).replace(
            _command(
                skills=[
                    ProfileDesiredSkillRef(skill_id=101),
                    ProfileDesiredSkillRef(skill_id=102),
                ],
                mcps=[],
                knowledge_base_ids=[],
            )
        )
        revoked.revoked_at = datetime.utcnow()
        deleted.status = "deleted"
        await session.flush()

        result = await ProfileDesiredMutationService(
            session
        ).refresh_asset_for_rollout(
            profile=profile,
            asset_type="skill",
            asset_id=102,
            action="delete",
            rollout_id="delete-while-other-revoked",
            created_by_user_id=2,
        )
        await session.commit()

        assert result.created_revision is True
        bindings = list(await session.scalars(select(ProfileSkillBinding)))
        assert {binding.skill_id for binding in bindings} == {101}
        latest = await session.scalar(
            select(RuntimeAgentProfileRevision)
            .where(RuntimeAgentProfileRevision.runtime_agent_profile_id == 10)
            .order_by(RuntimeAgentProfileRevision.revision.desc())
        )
        assert latest is not None
        assert latest.snapshot["skills"] == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_profile_desired_does_not_commit_its_mysql_transaction() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        await ProfileDesiredService(session).replace(_command())
        await session.rollback()

    async with sessions() as verification:
        profile = await verification.get(RuntimeAgentProfile, 10)
        assert profile is not None and profile.desired_revision == 0
        assert await verification.scalar(
            select(func.count(RuntimeAgentProfileRevision.id))
        ) == 0
        assert await verification.scalar(
            select(func.count(ProfileConvergenceJob.id))
        ) == 0
    await engine.dispose()


def test_profile_desired_transaction_boundary_has_no_external_side_effects() -> None:
    service_root = Path(__file__).resolve().parents[2] / "app" / "services"
    sources = "\n".join(
        (service_root / name).read_text(encoding="utf-8")
        for name in (
            "asset_catalog.py",
            "profile_desired.py",
            "profile_desired_mutations.py",
            "personal_skill_lifecycle.py",
        )
    )
    for forbidden in (
        ".commit(",
        "Dashboard",
        "session.resume",
        "reload.mcp",
        "httpx",
        "requests",
        "shutil",
        "subprocess",
        "kubectl",
    ):
        assert forbidden not in sources
