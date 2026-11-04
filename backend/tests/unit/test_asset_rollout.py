from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import (
    AssetRolloutBatch,
    AssetRolloutTarget,
    CloudHermesProfile,
    ExpertPackage,
    ExpertPackageVersion,
    HermesRuntimeBinding,
    InstalledExpertBinding,
    Mcp,
    McpVersion,
    Organization,
    Permission,
    ProfileExecutionControlJob,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    RuntimeSession,
    ScheduledTask,
    Skill,
    SkillVersion,
    User,
)
from app.schemas.asset_rollout import AssetRolloutCommand
from app.schemas.profile_desired import (
    ProfileDesiredCommand,
    ProfileDesiredDefinition,
    ProfileDesiredMcpRef,
    ProfileDesiredSkillRef,
)
from app.services.asset_rollout import (
    AssetRolloutError,
    AssetRolloutService,
    AssetRolloutWorker,
    refresh_rollout_targets_for_profile,
)
from app.services.asset_publication import PublicAssetPublicationService
from app.services.expert_installations import (
    ExpertInstallationError,
    ExpertInstallationService,
)
from app.services.expert_rollout import ExpertRolloutService
from tests.unit.expert_service_fixture import ExpertServiceFixture as ExpertProfileOrchestrator
from app.services.profile_desired import ProfileDesiredService
from app.services.runtime_workspace import Principal
from tests.unit.test_profile_desired import (
    _database,
    _mcp,
    _profile,
    _skill,
)


def _binding(*, binding_id: int, user_id: int, workspace_id: int) -> HermesRuntimeBinding:
    return HermesRuntimeBinding(
        id=binding_id,
        org_id=1,
        user_id=user_id,
        runtime_workspace_id=workspace_id,
        runtime_id=f"rt-{workspace_id}",
        image="hermes:test",
        k8s_namespace="test",
        knative_service=f"rt-{workspace_id}",
        endpoint_base_url="http://runtime.invalid",
        endpoint_host_header="runtime.invalid",
        hermes_home_sub_path=f"users/{user_id}",
        workspace_sub_path=f"workspaces/{workspace_id}",
        hermes_home_mount_path="/home/hermes",
        workspace_mount_path="/workspace",
        api_key_encrypted="secret",
        status="ready",
    )


def _command(*, profile: RuntimeAgentProfile) -> ProfileDesiredCommand:
    return ProfileDesiredCommand(
        org_id=1,
        user_id=int(profile.user_id),
        runtime_workspace_id=int(profile.runtime_workspace_id),
        runtime_agent_profile_id=int(profile.id),
        definition=ProfileDesiredDefinition(
            display_name=str(profile.display_name),
            soul_content="Use the selected assets.",
        ),
        skills=[ProfileDesiredSkillRef(skill_id=101)],
        mcps=[ProfileDesiredMcpRef(mcp_id=301)],
        source_type=(
            "default_user" if profile.profile_kind == "default" else "private_expert"
        ),
        source_id=f"seed-profile-{int(profile.id)}",
        created_by_user_id=int(profile.user_id),
    )


async def _seed_two_profiles(session) -> None:
    skill, skill_version = _skill()
    mcp, mcp_version = _mcp()
    default = _profile()
    expert = _profile(profile_id=11, user_id=3)
    expert.runtime_workspace_id = 4
    expert.display_name = "Beta"
    session.add_all(
        [
            skill,
            skill_version,
            mcp,
            mcp_version,
            default,
            expert,
            _binding(binding_id=119, user_id=2, workspace_id=3),
            _binding(binding_id=120, user_id=3, workspace_id=4),
        ]
    )
    await session.commit()
    for profile in (default, expert):
        await ProfileDesiredService(session).replace(_command(profile=profile))
    await session.commit()


def _expert_version(*, version_id: int, version: int, enterprise: bool) -> ExpertPackageVersion:
    return ExpertPackageVersion(
        id=version_id,
        org_id=1,
        expert_package_id=701,
        version=version,
        manifest={
            "schema_version": "expert.profile/v2",
            "capabilities": {"enterprise_knowledge_enabled": enterprise},
        },
        basic_info_snapshot={
            "name": f"Market Expert v{version}",
            "description": f"Version {version}",
        },
        soul_snapshot=f"Soul v{version}",
        prompt_snapshot=f"Prompt v{version}",
        skill_refs=[{"source": "public", "asset_id": 101}],
        mcp_refs=[{"source": "public", "asset_id": 301}],
        knowledge_refs=[{"source": "personal", "asset_id": 501}],
        plugin_refs=[{"plugin_id": "enterprise-knowledge"}],
        status="published",
    )


async def _seed_market_experts(session) -> None:
    skill, skill_version = _skill()
    mcp, mcp_version = _mcp()
    package = ExpertPackage(
        id=701,
        org_id=1,
        owner_user_id=9,
        slug="market-expert",
        name="Market Expert",
        status="published",
        visibility="public",
        current_version_id=711,
    )
    version_one = _expert_version(version_id=711, version=1, enterprise=False)
    version_two = _expert_version(version_id=712, version=2, enterprise=True)
    profiles = [
        _profile(profile_id=21, user_id=2, expert_package_id=701),
        _profile(profile_id=22, user_id=3, expert_package_id=701),
    ]
    profiles[0].ownership_type = "market_installed"
    profiles[1].ownership_type = "market_installed"
    profiles[1].runtime_workspace_id = 4
    session.add_all(
        [
            Organization(id=1, name="Org", code="org-1", status="active"),
            User(id=2, org_id=1, name="User 2", status="active"),
            User(id=3, org_id=1, name="User 3", status="active"),
            User(id=9, org_id=1, name="Owner", status="active"),
            CloudHermesProfile(
                id=3,
                org_id=1,
                user_id=2,
                profile_type="default_chat_agent",
                name="Workspace 3",
                status="active",
            ),
            CloudHermesProfile(
                id=4,
                org_id=1,
                user_id=3,
                profile_type="default_chat_agent",
                name="Workspace 4",
                status="active",
            ),
            skill,
            skill_version,
            mcp,
            mcp_version,
            package,
            version_one,
            version_two,
            Permission(
                org_id=1,
                asset_type="expert_package",
                asset_id=701,
                grantee_type="all",
                grantee_id="*",
            ),
            *profiles,
            _binding(binding_id=119, user_id=2, workspace_id=3),
            _binding(binding_id=120, user_id=3, workspace_id=4),
        ]
    )
    await session.commit()
    for profile in profiles:
        await ProfileDesiredService(session).replace(
            ProfileDesiredCommand(
                **{
                    **_command(profile=profile).model_dump(),
                    "definition": ProfileDesiredDefinition(
                        display_name="Market Expert v1",
                        soul_content="Soul v1",
                    ),
                    "expert_package_version_id": 711,
                    "source_type": "market_expert",
                }
            )
        )
        session.add(
            InstalledExpertBinding(
                org_id=1,
                user_id=int(profile.user_id),
                runtime_workspace_id=int(profile.runtime_workspace_id),
                expert_package_id=701,
                expert_package_version_id=711,
                runtime_agent_profile_id=int(profile.id),
                source_type="market",
                status="enabled",
            )
        )
    await session.commit()


@pytest.mark.asyncio
async def test_public_skill_rollout_freezes_users_and_advances_each_profile() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        legacy_pinned = await session.scalar(
            select(ProfileSkillBinding).where(
                ProfileSkillBinding.runtime_agent_profile_id == 11,
                ProfileSkillBinding.skill_id == 101,
            )
        )
        assert legacy_pinned is not None
        legacy_pinned.version_policy = "pinned"
        legacy_pinned.skill_version_id = 201
        skill = await session.get(Skill, 101)
        assert skill is not None
        session.add(
            SkillVersion(
                id=202,
                org_id=1,
                skill_id=101,
                version=2,
                package_uri="/data/assets/skills/101/202",
                package_sha256="a" * 64,
                manifest={"name": "skill-101"},
                status="published",
                published_at=datetime.utcnow(),
            )
        )
        skill.current_version_id = 202
        rollout = await AssetRolloutService(session).create(
            AssetRolloutCommand(
                rollout_id="rollout-skill-202",
                org_id=1,
                asset_type="skill",
                asset_id=101,
                action="publish",
                source_version_id=201,
                target_version_id=202,
                created_by_user_id=9,
            )
        )
        await session.commit()

        assert rollout.affected_user_count == 2
        assert rollout.affected_profile_count == 2

    assert await AssetRolloutWorker(
        session_factory=sessions,
        concurrency=2,
    ).reconcile_once() == 2

    async with sessions() as session:
        targets = list(
            await session.scalars(
                select(AssetRolloutTarget).order_by(AssetRolloutTarget.id)
            )
        )
        assert [item.target_revision for item in targets] == [2, 2]
        assert [item.status for item in targets] == ["updating", "updating"]
        revisions = list(
            await session.scalars(
                select(RuntimeAgentProfileRevision)
                .where(RuntimeAgentProfileRevision.revision == 2)
                .order_by(RuntimeAgentProfileRevision.runtime_agent_profile_id)
            )
        )
        assert [item.snapshot["skills"][0]["skill_version_id"] for item in revisions] == [
            202,
            202,
        ]
        normalized_binding = await session.scalar(
            select(ProfileSkillBinding).where(
                ProfileSkillBinding.runtime_agent_profile_id == 11,
                ProfileSkillBinding.skill_id == 101,
            )
        )
        assert normalized_binding is not None
        assert normalized_binding.version_policy == "follow_latest"
        assert normalized_binding.skill_version_id == 202
        for target in targets:
            await refresh_rollout_targets_for_profile(
                session,
                runtime_agent_profile_id=int(target.runtime_agent_profile_id),
                applied_revision=2,
                status="applied",
            )
        await session.commit()
        batch = await session.scalar(select(AssetRolloutBatch))
        assert batch is not None
        assert batch.status == "completed"
        assert batch.applied_count == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_market_expert_resolves_deleted_personal_skill_out_of_effective_desired() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_market_experts(session)
        skill = await session.get(Skill, 101)
        version = await session.get(ExpertPackageVersion, 711)
        profile = await session.get(RuntimeAgentProfile, 21)
        binding = await session.scalar(
            select(InstalledExpertBinding).where(
                InstalledExpertBinding.runtime_agent_profile_id == 21,
            )
        )
        assert skill is not None and version is not None
        assert profile is not None and binding is not None
        skill.asset_scope = "personal"
        skill.visibility = "private"
        skill.owner_user_id = 9
        skill.status = "deleted"
        skill.revoked_at = datetime.utcnow()
        version.skill_refs = [{"source": "personal", "asset_id": 101}]
        await session.commit()

        result = await ExpertRolloutService(session).apply_version(
            profile=profile,
            binding=binding,
            version=version,
            source_id="deleted-personal-skill-effective-resolver",
            created_by_user_id=9,
        )
        await session.commit()

        revision = await session.scalar(
            select(RuntimeAgentProfileRevision).where(
                RuntimeAgentProfileRevision.runtime_agent_profile_id == 21,
                RuntimeAgentProfileRevision.revision == result.desired_revision,
            )
        )
        assert revision is not None
        assert revision.snapshot["skills"] == []
        assert version.skill_refs == [{"source": "personal", "asset_id": 101}]
    await engine.dispose()


@pytest.mark.asyncio
async def test_newer_rollout_supersedes_waiting_target_for_same_profile() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        service = AssetRolloutService(session)
        await service.create(
            AssetRolloutCommand(
                rollout_id="rollout-skill-old",
                org_id=1,
                asset_type="skill",
                asset_id=101,
                action="publish",
                target_version_id=201,
                created_by_user_id=9,
            )
        )
        await service.create(
            AssetRolloutCommand(
                rollout_id="rollout-skill-new",
                org_id=1,
                asset_type="skill",
                asset_id=101,
                action="rollback",
                source_version_id=202,
                target_version_id=201,
                created_by_user_id=9,
            )
        )
        await session.commit()

        batches = list(
            await session.scalars(
                select(AssetRolloutBatch).order_by(AssetRolloutBatch.id)
            )
        )
        old_targets = list(
            await session.scalars(
                select(AssetRolloutTarget).where(
                    AssetRolloutTarget.asset_rollout_batch_id == batches[0].id
                )
            )
        )
        assert {item.status for item in old_targets} == {"superseded"}
        assert batches[0].status == "completed"
        assert batches[0].superseded_count == 2
        assert batches[1].waiting_count == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_revoke_and_enable_rollout_preserve_binding_and_toggle_effective_asset() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        await AssetRolloutService(session).create(
            AssetRolloutCommand(
                rollout_id="rollout-skill-revoke",
                org_id=1,
                asset_type="skill",
                asset_id=101,
                action="revoke",
                source_version_id=201,
                created_by_user_id=9,
            )
        )
        await session.commit()

    await AssetRolloutWorker(session_factory=sessions).reconcile_once()

    async with sessions() as session:
        bindings = list(await session.scalars(select(ProfileSkillBinding)))
        assert len(bindings) == 2
        assert {item.status for item in bindings} == {"enabled"}
        revisions = list(
            await session.scalars(
                select(RuntimeAgentProfileRevision).where(
                    RuntimeAgentProfileRevision.revision == 2
                )
            )
        )
        assert len(revisions) == 2
        assert all(item.snapshot["skills"] == [] for item in revisions)
        assert all(len(item.snapshot["mcps"]) == 1 for item in revisions)

        await AssetRolloutService(session).create(
            AssetRolloutCommand(
                rollout_id="rollout-skill-enable",
                org_id=1,
                asset_type="skill",
                asset_id=101,
                action="enable",
                source_version_id=201,
                created_by_user_id=9,
            )
        )
        await session.commit()

    await AssetRolloutWorker(session_factory=sessions).reconcile_once()

    async with sessions() as session:
        bindings = list(await session.scalars(select(ProfileSkillBinding)))
        assert len(bindings) == 2
        revisions = list(
            await session.scalars(
                select(RuntimeAgentProfileRevision).where(
                    RuntimeAgentProfileRevision.revision == 3
                )
            )
        )
        assert len(revisions) == 2
        assert all(len(item.snapshot["skills"]) == 1 for item in revisions)
    await engine.dispose()


@pytest.mark.asyncio
async def test_skill_emergency_disable_and_restore_are_one_reversible_axis() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        publication = PublicAssetPublicationService(session)

        skill = await session.get(Skill, 101)
        assert skill is not None
        assert skill.status == "published"
        assert skill.revoked_at is None
        assert list(await session.scalars(select(AssetRolloutBatch))) == []
        assert len(list(await session.scalars(select(ProfileSkillBinding)))) == 2

        disabled = await publication.set_skill_execution_enabled(
            org_id=1,
            skill_id=101,
            enabled=False,
            rollout_id="skill-revoke-security-101",
            created_by_user_id=9,
            reason="security incident",
        )
        assert disabled.rollout is not None
        assert disabled.rollout.affected_profile_count == 2
        assert skill.status == "published"
        assert skill.archived_at is None
        assert skill.revoked_at is not None
        await session.commit()

    await AssetRolloutWorker(session_factory=sessions).reconcile_once()

    async with sessions() as session:
        publication = PublicAssetPublicationService(session)
        enabled = await publication.set_skill_execution_enabled(
            org_id=1,
            skill_id=101,
            enabled=True,
            rollout_id="skill-enable-security-101",
            created_by_user_id=9,
            reason="incident resolved",
        )
        assert enabled.rollout is not None
        skill = await session.get(Skill, 101)
        assert skill is not None
        assert skill.status == "published"
        assert skill.archived_at is None
        assert skill.revoked_at is None
        await session.commit()

    await AssetRolloutWorker(session_factory=sessions).reconcile_once()

    async with sessions() as session:
        assert len(list(await session.scalars(select(ProfileSkillBinding)))) == 2
        latest = list(
            await session.scalars(
                select(RuntimeAgentProfileRevision).where(
                    RuntimeAgentProfileRevision.revision == 3
                )
            )
        )
        assert len(latest) == 2
        assert all(len(item.snapshot["skills"]) == 1 for item in latest)
    await engine.dispose()


@pytest.mark.asyncio
async def test_public_mcp_rollout_advances_profile_and_registry_versions() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        mcp = await session.get(Mcp, 301)
        assert mcp is not None
        session.add(
            McpVersion(
                id=402,
                org_id=1,
                mcp_id=301,
                version=2,
                transport="streamable_http",
                connection_config={"url": "https://mcp-v2.example.com"},
                secret_refs={"Authorization": "Bearer v2-test-token"},
                config_digest="sha256:" + "6" * 64,
                tools_schema=[{"name": "search_v2"}],
                tools_schema_digest="sha256:" + "7" * 64,
                status="published",
                published_at=datetime.utcnow(),
            )
        )
        mcp.current_version_id = 402
        await AssetRolloutService(session).create(
            AssetRolloutCommand(
                rollout_id="rollout-mcp-402",
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

    assert await AssetRolloutWorker(
        session_factory=sessions,
        concurrency=2,
    ).reconcile_once() == 2

    async with sessions() as session:
        revisions = list(
            await session.scalars(
                select(RuntimeAgentProfileRevision).where(
                    RuntimeAgentProfileRevision.revision == 2
                )
            )
        )
        assert len(revisions) == 2
        assert all(
            item.snapshot["mcps"][0]["mcp_version_id"] == 402
            for item in revisions
        )
        assert all(
            item.snapshot["mcps"][0]["credential_refs"]
            == {"Authorization": "Bearer v2-test-token"}
            for item in revisions
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_rollout_id_is_idempotent_and_rejects_changed_identity() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_two_profiles(session)
        service = AssetRolloutService(session)
        command = AssetRolloutCommand(
            rollout_id="rollout-idempotent",
            org_id=1,
            asset_type="mcp",
            asset_id=301,
            action="publish",
            target_version_id=401,
            created_by_user_id=9,
        )
        first = await service.create(command)
        second = await service.create(command)
        assert first.created is True
        assert second.created is False
        with pytest.raises(AssetRolloutError, match="ROLLOUT_IDEMPOTENCY_CONFLICT"):
            await service.create(
                command.model_copy(update={"asset_id": 999})
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_approved_expert_rollout_preserves_enterprise_knowledge_switch() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_market_experts(session)
        package = await session.get(ExpertPackage, 701)
        assert package is not None
        package.current_version_id = 712
        await AssetRolloutService(session).create(
            AssetRolloutCommand(
                rollout_id="rollout-expert-712",
                org_id=1,
                asset_type="expert",
                asset_id=701,
                action="publish",
                source_version_id=711,
                target_version_id=712,
                created_by_user_id=9,
            )
        )
        await session.commit()

    assert await AssetRolloutWorker(
        session_factory=sessions,
        concurrency=2,
    ).reconcile_once() == 2

    async with sessions() as session:
        revisions = list(
            await session.scalars(
                select(RuntimeAgentProfileRevision).where(
                    RuntimeAgentProfileRevision.revision == 2
                )
            )
        )
        assert len(revisions) == 2
        assert all(
            item.snapshot["expert_package_version_id"] == 712
            for item in revisions
        )
        assert all(
            item.snapshot["knowledge"]["enterprise_knowledge_enabled"] is True
            for item in revisions
        )
        assert all(
            item.snapshot["profile"]["plugin_ids"] == ["enterprise-knowledge"]
            for item in revisions
        )
        bindings = list(await session.scalars(select(InstalledExpertBinding)))
        assert {item.expert_package_version_id for item in bindings} == {712}
    await engine.dispose()


@pytest.mark.asyncio
async def test_expert_revoke_keeps_bindings_and_queues_independent_execution_denial() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_market_experts(session)
        await AssetRolloutService(session).create(
            AssetRolloutCommand(
                rollout_id="rollout-expert-revoke",
                org_id=1,
                asset_type="expert",
                asset_id=701,
                action="revoke",
                source_version_id=711,
                created_by_user_id=9,
            )
        )
        await session.commit()

    assert await AssetRolloutWorker(
        session_factory=sessions,
        concurrency=2,
    ).reconcile_once() == 2

    async with sessions() as session:
        bindings = list(await session.scalars(select(InstalledExpertBinding)))
        assert {item.status for item in bindings} == {"enabled"}
        profiles = list(
            await session.scalars(
                select(RuntimeAgentProfile).where(RuntimeAgentProfile.id.in_((21, 22)))
            )
        )
        assert {item.id for item in profiles} == {21, 22}
        revisions = list(
            await session.scalars(
                select(RuntimeAgentProfileRevision).where(
                    RuntimeAgentProfileRevision.revision == 2
                )
            )
        )
        assert revisions == []
        jobs = list(await session.scalars(select(ProfileExecutionControlJob)))
        assert {item.runtime_agent_profile_id for item in jobs} == {21, 22}
        assert {item.target_action for item in jobs} == {"revoke"}
        assert {item.status for item in jobs} == {"waiting"}
        targets = list(await session.scalars(select(AssetRolloutTarget)))
        for target in targets:
            await refresh_rollout_targets_for_profile(
                session,
                runtime_agent_profile_id=int(target.runtime_agent_profile_id),
                applied_revision=1,
                status="applied",
            )
        await session.commit()
        assert {item.status for item in targets} == {"updating"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_expert_uninstall_and_reinstall_creates_new_profile_epoch() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_market_experts(session)
        service = ExpertInstallationService(session)

        uninstall = await service.uninstall(
            org_id=1,
            user_id=2,
            runtime_workspace_id=3,
            runtime_agent_profile_id=21,
            operation_id="uninstall-expert-701",
        )
        await session.commit()

        profile = await session.get(RuntimeAgentProfile, 21)
        binding = await session.scalar(
            select(InstalledExpertBinding).where(
                InstalledExpertBinding.runtime_agent_profile_id == 21
            )
        )
        revision = await session.scalar(
            select(RuntimeAgentProfileRevision).where(
                RuntimeAgentProfileRevision.runtime_agent_profile_id == 21,
                RuntimeAgentProfileRevision.revision == 2,
            )
        )
        assert uninstall.desired_revision == 2
        assert profile is not None and profile.id == 21
        assert binding is not None and binding.status == "disabled"
        assert revision is not None
        assert revision.snapshot["skills"] == []
        assert revision.snapshot["mcps"] == []

        package = await session.get(ExpertPackage, 701)
        assert package is not None
        package.current_version_id = 712
        restored_profile = await service.install(
            principal=Principal(org_id=1, user_id=2),
            runtime_workspace_id=3,
            expert_package_id=701,
            operation_id="restore-expert-701",
        )
        await session.commit()

        restored_binding = await session.scalar(
            select(InstalledExpertBinding).where(
                InstalledExpertBinding.expert_package_id == 701,
                InstalledExpertBinding.user_id == 2,
            )
        )
        restored_revision = await session.scalar(
            select(RuntimeAgentProfileRevision).where(
                RuntimeAgentProfileRevision.runtime_agent_profile_id
                == restored_profile.id,
                RuntimeAgentProfileRevision.revision == 1,
            )
        )
        assert restored_profile.id != 21
        assert restored_binding is not None
        assert restored_binding.status == "enabled"
        assert restored_binding.expert_package_version_id == 712
        assert restored_binding.runtime_agent_profile_id == restored_profile.id
        assert restored_binding.installation_epoch == 2
        assert restored_revision is not None
        assert restored_revision.snapshot["expert_package_version_id"] == 712
        assert (
            restored_revision.snapshot["knowledge"][
                "enterprise_knowledge_enabled"
            ]
            is True
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_permission_removal_blocks_reinstall_but_not_existing_history() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_market_experts(session)
        service = ExpertInstallationService(session)
        await service.uninstall(
            org_id=1,
            user_id=2,
            runtime_workspace_id=3,
            runtime_agent_profile_id=21,
            operation_id="uninstall-before-permission-removal",
        )
        await session.execute(
            Permission.__table__.delete().where(
                Permission.asset_type == "expert_package",
                Permission.asset_id == 701,
            )
        )
        await session.commit()

        with pytest.raises(
            ExpertInstallationError,
            match="EXPERT_MARKET_ITEM_NOT_FOUND",
        ):
            await service.install(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=3,
                expert_package_id=701,
                operation_id="reinstall-after-permission-removal",
            )
        old_profile = await session.get(RuntimeAgentProfile, 21)
        assert old_profile is not None and old_profile.status == "disabled"
    await engine.dispose()


@pytest.mark.asyncio
async def test_market_expert_uninstall_is_owner_safe_and_keeps_old_sessions_read_only() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_market_experts(session)
        session.add(
            RuntimeSession(
                id=801,
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                runtime_agent_profile_id=21,
                runtime_session_id="market-expert-history",
                status="active",
            )
        )
        session.add(
            ScheduledTask(
                id=802,
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                target_session_id=801,
                task_name="market expert task",
                prompt="summarize",
                schedule_type="once",
                schedule_config={"type": "once"},
                status="active",
            )
        )
        await session.commit()
        orchestrator = ExpertProfileOrchestrator(session)
        principal = Principal(org_id=1, user_id=2)

        await orchestrator.uninstall(
            principal=principal,
            runtime_workspace_id=3,
            runtime_agent_profile_id=21,
        )

        package = await session.get(ExpertPackage, 701)
        assert package is not None
        package.current_version_id = 712
        restored = await orchestrator.install_market(
            principal=principal,
            runtime_workspace_id=3,
            expert_package_id=701,
        )
        assert restored.runtime_agent_profile_id != 21
        binding = await session.scalar(
            select(InstalledExpertBinding).where(
                InstalledExpertBinding.expert_package_id == 701,
                InstalledExpertBinding.user_id == 2,
            )
        )
        history = await session.get(RuntimeSession, 801)
        scheduled_task = await session.get(ScheduledTask, 802)
        assert binding is not None
        assert binding.status == "enabled"
        assert binding.expert_package_version_id == 712
        assert binding.runtime_agent_profile_id == restored.runtime_agent_profile_id
        assert binding.installation_epoch == 2
        assert history is not None and history.status == "expert_deleted"
        assert scheduled_task is not None and scheduled_task.status == "paused"
        assert scheduled_task.next_run_at is None

        await orchestrator.uninstall(
            principal=principal,
            runtime_workspace_id=3,
            runtime_agent_profile_id=restored.runtime_agent_profile_id,
        )
        await orchestrator.uninstall(
            principal=principal,
            runtime_workspace_id=3,
            runtime_agent_profile_id=restored.runtime_agent_profile_id,
        )

        other_binding = await session.scalar(
            select(InstalledExpertBinding).where(
                InstalledExpertBinding.runtime_agent_profile_id == 22
            )
        )
        package = await session.get(ExpertPackage, 701)
        assert package is not None and package.status == "published"
        assert other_binding is not None and other_binding.status == "enabled"
    await engine.dispose()
