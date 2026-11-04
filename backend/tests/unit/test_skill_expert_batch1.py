from __future__ import annotations

from datetime import datetime
from io import BytesIO
from zipfile import ZipFile

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import (
    AssetRolloutBatch,
    AssetRolloutTarget,
    ExpertPackage,
    ExpertPackageVersion,
    Permission,
    ProfileConvergenceJob,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    Skill,
    SkillVersion,
    SysDept,
    SysUser,
    TemplateReview,
    User,
)
from app.schemas.experts import (
    ApproveExpertReviewRequest,
    CreateExpertRequest,
    ExpertShareScope,
    UpdateExpertConfigurationRequest,
)
from app.schemas.profile_desired import ProfileDesiredSkillRef
from app.services.expert_catalog import ExpertCatalogQueryService
from app.services.expert_actions import ExpertActionContext, decide_expert_actions
from app.services.expert_errors import ExpertProfileError
from tests.unit.expert_service_fixture import ExpertServiceFixture as ExpertProfileOrchestrator
from app.services.expert_visibility import ExpertVisibilityPolicy
from app.services.personal_skill_assets import (
    PersonalSkillAssetError,
    PersonalSkillAssetService,
    SqlAlchemyPersonalSkillAssetStore,
)
from app.services.profile_desired import ProfileDesiredService
from app.services.profile_desired_mutations import ProfileDesiredMutationService
from app.services.runtime_workspace import Principal
from tests.unit.test_expert_profiles import _database as _expert_database
from tests.unit.test_profile_desired import (
    _command,
    _database as _profile_database,
    _profile,
    _seed,
    _skill,
)


def _skill_zip(*, name: str, body: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: personal\n---\n{body}\n",
        )
    return buffer.getvalue()


def test_running_operation_blocks_mutations_but_keeps_old_applied_runnable() -> None:
    actions = decide_expert_actions(
        ExpertActionContext(
            ownership_type="self_created",
            lifecycle_status="active",
            applied_revision=2,
            operation_running=True,
        )
    )

    assert actions.edit is False
    assert actions.copy_expert is False
    assert actions.delete is False
    assert actions.summon is True


@pytest.mark.asyncio
async def test_workspace_name_lease_is_shared_by_self_and_market_profiles() -> None:
    engine, sessions = await _expert_database()
    try:
        async with sessions() as db:
            db.add(
                RuntimeAgentProfile(
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    hermes_profile_name="expert-self",
                    profile_kind="expert",
                    display_name="同名专家",
                    active_display_name_key="同名专家",
                    ownership_type="self_created",
                    status="active",
                )
            )
            await db.commit()
            db.add(
                RuntimeAgentProfile(
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    hermes_profile_name="expert-market",
                    profile_kind="expert",
                    display_name="同名专家",
                    active_display_name_key="同名专家",
                    ownership_type="market_installed",
                    status="active",
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unified_editor_rejects_a_market_installed_profile_name_without_mutation() -> None:
    engine, sessions = await _expert_database()
    try:
        async with sessions() as db:
            service = ExpertProfileOrchestrator(db)
            created = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(runtime_workspace_id=1, name="自建专家"),
            )
            db.add(
                RuntimeAgentProfile(
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    hermes_profile_name="expert-market-name-lease",
                    profile_kind="expert",
                    display_name="市场已安装专家",
                    active_display_name_key="市场已安装专家",
                    ownership_type="market_installed",
                    status="active",
                )
            )
            await db.commit()

            with pytest.raises(ExpertProfileError) as conflict:
                await service.save_configuration(
                    principal=Principal(org_id=1, user_id=1),
                    runtime_workspace_id=1,
                    runtime_agent_profile_id=created.runtime_agent_profile_id,
                    command=UpdateExpertConfigurationRequest(
                        name="市场已安装专家",
                        description=created.description,
                        avatar_url=created.avatar_url,
                        soul_content=created.soul_content,
                        capabilities=created.capabilities,
                        share_scope=created.share_scope,
                    ),
                )

            assert conflict.value.code == "expert_name_conflict"
            profile = await db.get(RuntimeAgentProfile, created.runtime_agent_profile_id)
            assert profile is not None
            assert profile.display_name == "自建专家"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_profile_lifecycle_is_independent_from_convergence_health() -> None:
    engine, sessions = await _expert_database()
    try:
        async with sessions() as db:
            service = ExpertProfileOrchestrator(db)
            created = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(runtime_workspace_id=1, name="健康分离专家"),
            )
            assert created.status == "active"
            assert created.convergence_status == "waiting"
            job = await db.scalar(
                select(ProfileConvergenceJob).where(
                    ProfileConvergenceJob.runtime_agent_profile_id
                    == created.runtime_agent_profile_id
                )
            )
            assert job is not None
            job.status = "failed"
            job.last_error_code = "PROJECTION_FAILED"
            await db.commit()
            observed = await service.get_profile(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=created.runtime_agent_profile_id,
            )
            assert observed.status == "active"
            assert observed.convergence_status == "failed"
            assert observed.last_error == "PROJECTION_FAILED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mine_summary_exposes_backend_owned_actions_for_real_profile_states() -> None:
    engine, sessions = await _expert_database()
    try:
        async with sessions() as db:
            service = ExpertProfileOrchestrator(db)
            created = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(runtime_workspace_id=1, name="动作合同专家"),
            )
            assert created.status == "active"
            assert created.allowed_actions.edit is True
            assert created.allowed_actions.copy_expert is True
            assert created.allowed_actions.delete is True
            assert created.allowed_actions.summon is False

            profile = await db.get(
                RuntimeAgentProfile,
                created.runtime_agent_profile_id,
            )
            assert profile is not None
            profile.applied_revision = profile.desired_revision
            await db.commit()
            applied = await service.get_profile(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=created.runtime_agent_profile_id,
            )
            assert applied.allowed_actions.summon is True

            profile.status = "disabled"
            await db.commit()
            disabled = await service.get_profile(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=created.runtime_agent_profile_id,
            )
            assert disabled.allowed_actions.edit is False
            assert disabled.allowed_actions.copy_expert is False
            assert disabled.allowed_actions.summon is False
            assert disabled.allowed_actions.delete is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_expert_configuration_returns_precise_lifecycle_error() -> None:
    engine, sessions = await _expert_database()
    try:
        async with sessions() as db:
            service = ExpertProfileOrchestrator(db)
            created = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(runtime_workspace_id=1, name="已停用专家"),
            )
            profile = await db.get(
                RuntimeAgentProfile,
                created.runtime_agent_profile_id,
            )
            assert profile is not None
            profile.status = "disabled"
            await db.commit()

            with pytest.raises(ExpertProfileError) as error:
                await service.replace_share_scope(
                    principal=Principal(org_id=1, user_id=1),
                    runtime_workspace_id=1,
                    runtime_agent_profile_id=created.runtime_agent_profile_id,
                    share_scope=ExpertShareScope(),
                )
            assert error.value.code == "expert_disabled"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_self_created_expert_can_be_deleted() -> None:
    engine, sessions = await _expert_database()
    try:
        async with sessions() as db:
            service = ExpertProfileOrchestrator(db)
            created = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(runtime_workspace_id=1, name="待清理专家"),
            )
            profile = await db.get(RuntimeAgentProfile, created.runtime_agent_profile_id)
            package = await db.get(ExpertPackage, created.expert_package_id)
            assert profile is not None and package is not None
            profile.status = "disabled"
            package.status = "withdrawn"
            await db.commit()

            await service.uninstall(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=created.runtime_agent_profile_id,
            )

            assert profile.status == "deleted"
            assert package.status == "deleted"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deleting_self_created_expert_supersedes_pending_review() -> None:
    engine, sessions = await _expert_database()
    try:
        async with sessions() as db:
            service = ExpertProfileOrchestrator(db)
            created = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="待审删除专家",
                    share_scope=ExpertShareScope(user_ids=[2]),
                ),
            )
            assert created.review_id is not None
            review = await db.get(TemplateReview, created.review_id)
            assert review is not None and review.expert_package_version_id is not None
            pending_version = await db.get(
                ExpertPackageVersion,
                review.expert_package_version_id,
            )

            await service.uninstall(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=created.runtime_agent_profile_id,
            )

            await db.refresh(review)
            assert review.status == "superseded"
            assert review.review_snapshot["superseded_reason"] == "owner_deleted"
            package = await db.get(ExpertPackage, created.expert_package_id)
            assert package is not None and package.status == "deleted"
            assert pending_version is not None and pending_version.status == "superseded"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_visibility_uses_live_department_ancestors_after_org_move() -> None:
    engine, sessions = await _expert_database()
    try:
        async with sessions() as db:
            user = await db.get(User, 2)
            assert user is not None
            user.user_code = "installer"
            package = ExpertPackage(
                org_id=1,
                owner_user_id=1,
                slug="department-visible",
                name="部门专家",
                status="published",
                visibility="restricted",
            )
            db.add_all(
                [
                    SysDept(dept_id="root", name="总部", status=1),
                    SysDept(
                        dept_id="child", parent_id="root", name="子部门", status=1
                    ),
                    SysDept(dept_id="other", name="其他部门", status=1),
                    SysUser(
                        user_id="directory-installer",
                        code="installer",
                        username="installer",
                        dept_id="child",
                        status=1,
                    ),
                    package,
                ]
            )
            await db.flush()
            db.add(
                Permission(
                    org_id=1,
                    asset_type="expert_package",
                    asset_id=int(package.id),
                    grantee_type="department",
                    grantee_id="root",
                )
            )
            await db.commit()
            policy = ExpertVisibilityPolicy(db)
            assert await policy.can_view_package(
                principal=Principal(org_id=1, user_id=2),
                expert_package_id=int(package.id),
            )
            directory_user = await db.get(SysUser, "directory-installer")
            assert directory_user is not None
            directory_user.dept_id = "other"
            await db.commit()
            assert not await policy.can_view_package(
                principal=Principal(org_id=1, user_id=2),
                expert_package_id=int(package.id),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_market_keeps_enabled_install_after_permission_removal_and_pins_first() -> None:
    engine, sessions = await _expert_database()
    try:
        async with sessions() as db:
            service = ExpertProfileOrchestrator(db)
            first = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="已安装专家",
                    share_scope=ExpertShareScope(user_ids=[2]),
                ),
            )
            second = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="可见未安装专家",
                    share_scope=ExpertShareScope(user_ids=[2]),
                ),
            )
            for item in (first, second):
                assert item.review_id is not None
                await service.approve_review(
                    org_id=1,
                    reviewer_user_id=1,
                    review_id=item.review_id,
                    command=ApproveExpertReviewRequest(),
                )
            await service.install_market(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=2,
                expert_package_id=int(first.expert_package_id),
            )
            await db.execute(
                Permission.__table__.delete().where(
                    Permission.asset_type == "expert_package",
                    Permission.asset_id == first.expert_package_id,
                )
            )
            await db.commit()
            query = ExpertCatalogQueryService(db)
            page = await query.market_page(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=2,
                limit=1,
            )
            assert [item.expert_package_id for item in page.items] == [
                first.expert_package_id
            ]
            assert page.items[0].installed is True
            assert page.next_cursor == "1"
            next_page = await query.market_page(
                principal=Principal(org_id=1, user_id=2),
                runtime_workspace_id=2,
                cursor=page.next_cursor,
                limit=1,
            )
            assert [item.expert_package_id for item in next_page.items] == [
                second.expert_package_id
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_copy_uses_incrementing_names_and_independent_profile_identity() -> None:
    engine, sessions = await _expert_database()
    try:
        async with sessions() as db:
            service = ExpertProfileOrchestrator(db)
            original = await service.create(
                principal=Principal(org_id=1, user_id=1),
                command=CreateExpertRequest(runtime_workspace_id=1, name="写作专家"),
            )
            first = await service.copy_owned(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=original.runtime_agent_profile_id,
            )
            second = await service.copy_owned(
                principal=Principal(org_id=1, user_id=1),
                runtime_workspace_id=1,
                runtime_agent_profile_id=original.runtime_agent_profile_id,
            )
            assert first.display_name == "写作专家-副本"
            assert second.display_name == "写作专家-副本2"
            assert len(
                {
                    original.runtime_agent_profile_id,
                    first.runtime_agent_profile_id,
                    second.runtime_agent_profile_id,
                }
            ) == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_personal_skill_upload_updates_stable_asset_in_sql_store(tmp_path) -> None:
    engine, sessions = await _profile_database()
    try:
        async with sessions() as db:
            await _seed(db)
            service = PersonalSkillAssetService(
                store=SqlAlchemyPersonalSkillAssetStore(db),
                root=str(tmp_path),
            )
            principal = Principal(org_id=1, user_id=2)
            first = await service.upload_skill_zip(
                principal=principal,
                content=_skill_zip(name="稳定个人技能", body="v1"),
            )
            await db.commit()
            await ProfileDesiredService(db).replace(
                _command(
                    profile_id=10,
                    skills=[ProfileDesiredSkillRef(skill_id=first.skill_id)],
                    mcps=[],
                    knowledge_base_ids=[],
                )
            )
            await db.commit()
            second = await service.upload_skill_zip(
                principal=principal,
                content=_skill_zip(name="稳定个人技能", body="v2"),
            )
            await db.commit()
            assert second.skill_id == first.skill_id
            assert second.skill_version_id != first.skill_version_id
            assert second.version == 2
            assert await db.scalar(
                select(func.count(SkillVersion.id)).where(
                    SkillVersion.skill_id == first.skill_id
                )
            ) == 2
            rollout = await db.scalar(
                select(AssetRolloutBatch).where(
                    AssetRolloutBatch.asset_type == "skill",
                    AssetRolloutBatch.asset_id == first.skill_id,
                    AssetRolloutBatch.target_version_id == second.skill_version_id,
                )
            )
            assert rollout is not None
            assert await db.scalar(
                select(func.count(AssetRolloutTarget.id)).where(
                    AssetRolloutTarget.asset_rollout_batch_id == rollout.id
                )
            ) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_archived_personal_skill_remains_visible_and_can_be_reuploaded(tmp_path) -> None:
    engine, sessions = await _profile_database()
    try:
        async with sessions() as db:
            await _seed(db)
            service = PersonalSkillAssetService(
                store=SqlAlchemyPersonalSkillAssetStore(db),
                root=str(tmp_path),
            )
            principal = Principal(org_id=1, user_id=2)
            first = await service.upload_skill_zip(
                principal=principal,
                content=_skill_zip(name="可恢复技能", body="v1"),
            )
            await db.commit()

            skill = await db.get(Skill, first.skill_id)
            assert skill is not None
            skill.status = "archived"
            skill.archived_at = datetime.utcnow()
            await db.commit()

            archived = await service.list_skills(principal=principal)
            assert [item.skill_id for item in archived] == [first.skill_id]

            restored = await service.upload_skill_zip(
                principal=principal,
                content=_skill_zip(name="可恢复技能", body="v2"),
            )
            await db.commit()

            restored_skill = await db.get(Skill, first.skill_id)
            assert restored.skill_id == first.skill_id
            assert restored.version == 2
            assert restored_skill is not None
            assert restored_skill.status == "private"
            assert restored_skill.archived_at is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_personal_upload_rejects_runtime_system_skill_names(tmp_path) -> None:
    engine, sessions = await _profile_database()
    try:
        async with sessions() as db:
            await _seed(db)
            service = PersonalSkillAssetService(
                store=SqlAlchemyPersonalSkillAssetStore(db),
                root=str(tmp_path),
            )
            with pytest.raises(PersonalSkillAssetError) as error:
                await service.upload_skill_zip(
                    principal=Principal(org_id=1, user_id=2),
                    content=_skill_zip(name="Hermes Agent", body="reserved"),
                )
            assert str(error.value) == "SKILL_SYSTEM_RESERVED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_personal_skill_list_excludes_legacy_frozen_assets() -> None:
    engine, sessions = await _profile_database()
    try:
        async with sessions() as db:
            personal, personal_version = _skill(
                skill_id=102,
                version_id=202,
                owner_user_id=2,
                asset_scope="personal",
            )
            frozen, frozen_version = _skill(
                skill_id=103,
                version_id=203,
                owner_user_id=2,
                asset_scope="expert_frozen",
            )
            db.add_all([personal, personal_version, frozen, frozen_version])
            await db.commit()

            records = await SqlAlchemyPersonalSkillAssetStore(db).list_for_user(
                principal=Principal(org_id=1, user_id=2),
            )

            assert [item.skill_id for item in records] == [102]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_removing_skill_from_one_expert_preserves_asset_and_other_bindings() -> None:
    engine, sessions = await _profile_database()
    try:
        async with sessions() as db:
            await _seed(db, include_second_profile=True)
            third = _profile(profile_id=12)
            db.add(third)
            await db.commit()
            desired = ProfileDesiredService(db)
            for profile_id in (10, 11, 12):
                await desired.replace(
                    _command(
                        profile_id=profile_id,
                        skills=[ProfileDesiredSkillRef(skill_id=101)],
                        mcps=[],
                        knowledge_base_ids=[],
                    )
                )
            await ProfileDesiredMutationService(db).remove_expert_skill(
                org_id=1,
                user_id=2,
                runtime_agent_profile_id=11,
                skill_id=101,
                operation_id="remove-current-expert-skill",
            )
            await db.commit()
            assert await db.get(Skill, 101) is not None
            remaining = set(
                await db.scalars(select(ProfileSkillBinding.runtime_agent_profile_id))
            )
            assert remaining == {10, 12}
    finally:
        await engine.dispose()
