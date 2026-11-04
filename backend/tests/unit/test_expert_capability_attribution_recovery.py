"""回归测试：专家能力归属恢复 + 虚拟部门显式拒绝。

覆盖 2026-09-07 修复：
  * legacy `skill_ids` 引用创建者本人 personal Skill 时，_validated_stable_capabilities
    应将 source 恢复为 "personal"，而不是错误地标记为 "public"；
  * _prepare_release_artifacts（authoring / review 两侧）不再把这类版本拒绝为
    "selected expert Skill is not public"；
  * _validate_share_scope 与 personal_knowledge 的部门校验对 "virtual:" 前缀显式拒绝；
  * 前端 ExpertVisibilityEditor 对 virtual=true 节点不渲染 checkbox。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.mysql import LONGTEXT

from app.models import (
    Base,
    CloudHermesProfile,
    ExpertPackage,
    ExpertPackageVersion,
    KnowledgeBase,
    ModelEntry,
    ModelProvider,
    Organization,
    RuntimeAgentProfile,
    Skill,
    SkillVersion,
    SysDept,
    SysUser,
    User,
)
from app.schemas.experts import (
    CreateExpertRequest,
    ExpertCapabilitySelection,
    ExpertCapabilityReference,
    ExpertShareScope,
)
from app.services.expert_authoring_commands import ExpertAuthoringCommandService
from app.services.expert_errors import ExpertProfileError
from app.services.expert_review_commands import ExpertReviewCommandService
from app.services.personal_knowledge import PersonalKnowledgeError, PersonalKnowledgeService
from app.services.runtime_workspace import Principal
from tests.unit.expert_service_fixture import ExpertServiceFixture as ExpertProfileOrchestrator


@compiles(BigInteger, "sqlite")
def _big_int_sqlite(_type: BigInteger, _compiler: Any, **_kwargs: Any) -> str:
    return "INTEGER"


@compiles(LONGTEXT, "sqlite")
def _longtext_sqlite(_type: LONGTEXT, _compiler: Any, **_kwargs: Any) -> str:
    return "TEXT"


async def _database() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, name="Org", code="org-1", status="active"),
                User(id=1, org_id=1, name="Creator", user_code="U001", status="active"),
                CloudHermesProfile(
                    id=1,
                    org_id=1,
                    user_id=1,
                    profile_type="default_chat_agent",
                    name="默认数字人",
                    status="active",
                ),
                ModelProvider(
                    id=1,
                    org_id=1,
                    provider_key="platform",
                    display_name="Platform",
                    provider_type="openai_compatible",
                    api_endpoint="http://provider.invalid/v1",
                    api_key_encrypted="encrypted",
                    status="active",
                ),
                ModelEntry(
                    id=1,
                    org_id=1,
                    provider_id=1,
                    model_key="model-default",
                    display_name="Default Model",
                    context_length=128_000,
                    is_default=True,
                    is_restricted=False,
                    status="active",
                ),
            ]
        )
        await db.commit()
    return engine, sessions


async def _seed_personal_skill(
    db: AsyncSession,
    *,
    skill_id: int,
    version_id: int,
    owner_user_id: int,
    slug: str,
) -> None:
    skill = Skill(
        id=skill_id,
        org_id=1,
        owner_user_id=owner_user_id,
        slug=slug,
        name=slug,
        description="personal",
        category_key="personal",
        asset_scope="personal",
        visibility="private",
        status="published",
        current_version_id=version_id,
    )
    version = SkillVersion(
        id=version_id,
        org_id=1,
        skill_id=skill_id,
        version=1,
        package_uri="/tmp/personal-skill",
        package_sha256="a" * 64,
        manifest={},
        status="published",
    )
    db.add_all([skill, version])
    await db.commit()


@pytest.mark.asyncio
async def test_legacy_skill_ids_referencing_creator_personal_skill_recover_source() -> None:
    """legacy skill_ids 引用创建者 personal Skill 时 source 应恢复为 personal。"""
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed_personal_skill(db, skill_id=7, version_id=70, owner_user_id=1, slug="p-skill-7")
            await _seed_personal_skill(db, skill_id=15, version_id=150, owner_user_id=1, slug="p-skill-15")

            principal = Principal(org_id=1, user_id=1)
            orchestrator = ExpertProfileOrchestrator(db)

            created = await orchestrator.create(
                principal=principal,
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="带个人 Skill 的自建专家",
                    soul_content="人格",
                    capabilities=ExpertCapabilitySelection(
                        skill_ids=[7, 15],
                    ),
                ),
            )
            # skill_ids 只包含 public Skill；personal 的应出现在 skills 列表中
            assert created.capabilities.skill_ids == []
            assert {ref.asset_id for ref in created.capabilities.skills} == {7, 15}
            assert all(ref.source == "personal" for ref in created.capabilities.skills)

            profile = await db.get(RuntimeAgentProfile, created.runtime_agent_profile_id)
            assert profile is not None
            config = profile.capability_config or {}
            skills = config.get("skills", [])
            assert {item["asset_id"] for item in skills} == {7, 15}
            assert all(item["source"] == "personal" for item in skills), (
                "legacy skill_ids 指向 personal Skill 时应恢复 source=personal"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_personal_skill_reference_with_explicit_source_is_preserved() -> None:
    """客户端显式传 source=personal 的个人 Skill 引用应保持 personal。"""
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed_personal_skill(db, skill_id=7, version_id=70, owner_user_id=1, slug="p-skill-7")

            principal = Principal(org_id=1, user_id=1)
            orchestrator = ExpertProfileOrchestrator(db)

            created = await orchestrator.create(
                principal=principal,
                command=CreateExpertRequest(
                    runtime_workspace_id=1,
                    name="显式个人 Skill 专家",
                    soul_content="人格",
                    capabilities=ExpertCapabilitySelection(
                        skills=[ExpertCapabilityReference(source="personal", asset_id=7)],
                    ),
                ),
            )
            config = (
                await db.get(RuntimeAgentProfile, created.runtime_agent_profile_id)
            ).capability_config or {}
            assert config["skills"][0]["source"] == "personal"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_personal_skill_not_owned_by_principal_is_rejected() -> None:
    """别人的 personal Skill 不能冒用为 public 引用。"""
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add(User(id=2, org_id=1, name="Other", user_code="U002", status="active"))
            await db.commit()
            await _seed_personal_skill(db, skill_id=7, version_id=70, owner_user_id=2, slug="p-other")

            principal = Principal(org_id=1, user_id=1)
            orchestrator = ExpertProfileOrchestrator(db)

            with pytest.raises(ExpertProfileError) as excinfo:
                await orchestrator.create(
                    principal=principal,
                    command=CreateExpertRequest(
                        runtime_workspace_id=1,
                        name="越权专家",
                        soul_content="人格",
                        capabilities=ExpertCapabilitySelection(
                            skill_ids=[7],
                        ),
                    ),
                )
            assert excinfo.value.code == "expert_capability_not_visible"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_prepare_release_artifacts_accepts_legacy_public_for_personal_skill() -> None:
    """存储版本中 source=public 但实际指向个人 Skill 的 legacy 数据，应通过预检。"""
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed_personal_skill(db, skill_id=7, version_id=70, owner_user_id=1, slug="p-skill-7")

            package = ExpertPackage(
                id=1,
                org_id=1,
                owner_user_id=1,
                origin_runtime_workspace_id=1,
                slug="legacy-expert",
                name="Legacy",
                visibility="private",
                status="pending_review",
            )
            version = ExpertPackageVersion(
                id=1,
                expert_package_id=1,
                org_id=1,
                version=1,
                skill_refs=[{"source": "public", "asset_id": 7, "version_id": 70}],
                mcp_refs=[],
                knowledge_refs=[],
                plugin_refs=[],
                manifest={
                    "capabilities": {
                        "skill_ids": [7],
                        "skills": [{"source": "public", "asset_id": 7, "version_id": 70}],
                        "mcp_ids": [],
                        "mcps": [],
                        "knowledge_base_ids": [],
                        "knowledge_bases": [],
                        "plugin_ids": [],
                    }
                },
                status="pending_review",
            )
            db.add_all([package, version])
            await db.commit()

            # authoring 侧与 review 侧的预检都应通过（不再抛 "selected expert Skill is not public"）
            await ExpertAuthoringCommandService(db)._prepare_release_artifacts(version=version)
            await ExpertReviewCommandService(db)._prepare_release_artifacts(version=version)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_validate_share_scope_rejects_virtual_department_prefix() -> None:
    """_validate_share_scope 显式拒绝 virtual: 前缀部门编号。"""
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            service = ExpertAuthoringCommandService(db)
            principal = Principal(org_id=1, user_id=1)

            with pytest.raises(ExpertProfileError) as excinfo:
                await service._validate_share_scope(
                    principal=principal,
                    scope=ExpertShareScope(
                        organization=False,
                        user_ids=[],
                        department_ids=["virtual:unassigned"],
                    ),
                )
            assert excinfo.value.code == "invalid_expert_share_department"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_personal_knowledge_rejects_virtual_department_prefix() -> None:
    """personal_knowledge 部门校验同样显式拒绝 virtual: 前缀。"""
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            # 补齐 sys_dept 一行真实部门，确保查询路径可执行
            db.add(SysDept(dept_id="D1", parent_id=None, code="d1", name="真实部门", status=1))
            await db.commit()
            service = PersonalKnowledgeService(db)
            principal = Principal(org_id=1, user_id=1)

            with pytest.raises(PersonalKnowledgeError):
                await service._validate_permission_targets(
                    principal=principal,
                    user_ids=set(),
                    department_ids={"virtual:unassigned"},
                    existing_user_ids=set(),
                )
    finally:
        await engine.dispose()
