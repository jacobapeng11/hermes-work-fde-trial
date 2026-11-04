from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import (
    ApplicationAgent,
    ApplicationAgentPersonalKnowledgeBinding,
    ApplicationAgentSkill,
    ApplicationAgentConfigRevision,
    Base,
    Mcp,
    McpVersion,
    KnowledgeBase,
    Organization,
    PersonalSkillCategory,
    Skill,
    SkillCategory,
    SkillVersion,
    User,
)
from app.services.application_agent_asset_snapshot import (
    ApplicationAgentAssetSnapshotBuilder,
    ApplicationAgentAssetSnapshotError,
)
from app.services.application_agent_asset_rollout import (
    ApplicationAgentAssetRolloutService,
)
from app.services.application_agent_skills import (
    ApplicationAgentSkillError,
    SqlAlchemyApplicationAgentSkillService,
)
from app.services.runtime_workspace import Principal
from app.config import settings


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_integer(
    _type: BigInteger,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "INTEGER"


async def _database() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                Organization.__table__,
                User.__table__,
                ApplicationAgent.__table__,
                ApplicationAgentConfigRevision.__table__,
                ApplicationAgentPersonalKnowledgeBinding.__table__,
                ApplicationAgentSkill.__table__,
                Skill.__table__,
                SkillVersion.__table__,
                SkillCategory.__table__,
                PersonalSkillCategory.__table__,
                Mcp.__table__,
                McpVersion.__table__,
                KnowledgeBase.__table__,
            ],
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _application_skill_zip(*, marker: str, name: str = "app-ticket") -> bytes:
    payload = BytesIO()
    with ZipFile(payload, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: 应用工单\n---\n\n"
            f"# {marker}\n",
        )
    return payload.getvalue()


@pytest.mark.asyncio
async def test_application_private_skill_uses_stable_skill_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    monkeypatch.setattr(
        settings,
        "HERMES_APPLICATION_AGENT_RUNTIME_ROOT",
        str(tmp_path),
    )
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, code="org-1", name="测试企业"),
                User(id=7, org_id=1, user_code="ADMIN", name="管理员", status="active"),
                ApplicationAgent(
                    id=31,
                    org_id=1,
                    created_by_user_id=7,
                    updated_by_user_id=7,
                    slug="sales-agent",
                    name="销售 Agent",
                    config={},
                ),
            ]
        )
        await db.commit()
        service = SqlAlchemyApplicationAgentSkillService(db)
        first = await service.upload_skill(
            principal=Principal(org_id=1, user_id=7),
            agent_id=31,
            content=_application_skill_zip(marker="v1"),
            category_key="personal",
        )
        second = await service.upload_skill(
            principal=Principal(org_id=1, user_id=7),
            agent_id=31,
            content=_application_skill_zip(marker="v2"),
            category_key="personal",
        )

        assert second.application_agent_skill_id == first.application_agent_skill_id
        assert first.version == 1
        assert second.version == 2
        assert second.skill_version_id != first.skill_version_id
        skill = await db.get(Skill, second.application_agent_skill_id)
        assert skill is not None
        assert skill.asset_scope == "application"
        assert skill.application_agent_id == 31
        snapshot = await ApplicationAgentAssetSnapshotBuilder(db).build(
            org_id=1,
            application_agent_id=31,
            config={
                "skill_asset_ids": [],
                "application_skill_ids": [second.application_agent_skill_id],
                "mcp_asset_ids": [],
                "knowledge_bindings": [],
            },
        )
        assert snapshot["asset_snapshot"]["application_skills"] == [
            {
                "asset_id": second.application_agent_skill_id,
                "version_id": second.skill_version_id,
                "sha256": second.package_sha256,
            }
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_application_private_skill_description_update_is_metadata_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    monkeypatch.setattr(
        settings,
        "HERMES_APPLICATION_AGENT_RUNTIME_ROOT",
        str(tmp_path),
    )
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, code="org-1", name="测试企业"),
                User(id=7, org_id=1, user_code="ADMIN", name="管理员", status="active"),
                ApplicationAgent(
                    id=31,
                    org_id=1,
                    created_by_user_id=7,
                    updated_by_user_id=7,
                    slug="description-agent",
                    name="描述 Agent",
                    config={},
                ),
            ]
        )
        await db.commit()
        service = SqlAlchemyApplicationAgentSkillService(db)
        principal = Principal(org_id=1, user_id=7)
        uploaded = await service.upload_skill(
            principal=principal,
            agent_id=31,
            content=_application_skill_zip(marker="description"),
            category_key="personal",
        )
        version_id = uploaded.skill_version_id

        updated = await service.update_description(
            principal=principal,
            agent_id=31,
            skill_id=uploaded.application_agent_skill_id,
            description="应用专用说明",
        )

        assert updated.description == "应用专用说明"
        assert updated.skill_version_id == version_id
        row = await db.get(Skill, uploaded.application_agent_skill_id)
        assert row is not None and row.description == "应用专用说明"
    await engine.dispose()


@pytest.mark.asyncio
async def test_deleted_application_skill_can_be_reuploaded_with_same_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delete hides the asset without reserving its name forever."""
    engine, sessions = await _database()
    monkeypatch.setattr(
        settings,
        "HERMES_APPLICATION_AGENT_RUNTIME_ROOT",
        str(tmp_path),
    )
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, code="org-1", name="测试企业"),
                User(id=7, org_id=1, user_code="ADMIN", name="管理员", status="active"),
                ApplicationAgent(
                    id=31,
                    org_id=1,
                    created_by_user_id=7,
                    updated_by_user_id=7,
                    slug="sales-agent",
                    name="销售 Agent",
                    config={},
                ),
            ]
        )
        await db.commit()
        service = SqlAlchemyApplicationAgentSkillService(db)
        principal = Principal(org_id=1, user_id=7)
        content = _application_skill_zip(marker="deleted-then-restored")

        first = await service.upload_skill(
            principal=principal,
            agent_id=31,
            content=content,
            category_key="personal",
        )
        await service.delete_skill(
            principal=principal,
            agent_id=31,
            skill_id=first.application_agent_skill_id,
        )
        assert await service.list_skills(principal=principal, agent_id=31) == []

        restored = await service.upload_skill(
            principal=principal,
            agent_id=31,
            content=content,
            category_key="personal",
        )

        assert restored.application_agent_skill_id == first.application_agent_skill_id
        assert restored.version == 2
        assert [
            item.application_agent_skill_id
            for item in await service.list_skills(principal=principal, agent_id=31)
        ] == [first.application_agent_skill_id]
        skill = await db.get(Skill, first.application_agent_skill_id)
        assert skill is not None
        assert skill.status == "private"
        assert skill.current_version_id == restored.skill_version_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_application_private_skill_uses_personal_skill_category_policy(
    tmp_path: Path,
) -> None:
    engine, sessions = await _database()
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, code="org-1", name="测试企业"),
                User(id=7, org_id=1, user_code="ADMIN", name="管理员", status="active"),
                ApplicationAgent(
                    id=31,
                    org_id=1,
                    created_by_user_id=7,
                    updated_by_user_id=7,
                    slug="category-agent",
                    name="分类 Agent",
                    config={},
                ),
                SkillCategory(
                    org_id=1,
                    category_key="productivity",
                    display_name="效率工具",
                    is_builtin=True,
                    is_market_visible=True,
                    is_personal_visible=True,
                ),
                PersonalSkillCategory(
                    org_id=1,
                    owner_user_id=7,
                    category_key="personal-writing",
                    display_name="写作",
                    normalized_name="写作",
                ),
            ]
        )
        await db.commit()
        service = SqlAlchemyApplicationAgentSkillService(db)
        principal = Principal(org_id=1, user_id=7)

        uncategorized = await service.upload_skill(
            principal=principal,
            agent_id=31,
            content=_application_skill_zip(marker="uncategorized", name="uncategorized"),
            category_key="uncategorized",
        )
        organization = await service.upload_skill(
            principal=principal,
            agent_id=31,
            content=_application_skill_zip(marker="organization", name="organization"),
            category_key="productivity",
        )
        personal = await service.upload_skill(
            principal=principal,
            agent_id=31,
            content=_application_skill_zip(marker="personal", name="personal"),
            category_key="personal-writing",
        )

        assert uncategorized.category_key == "uncategorized"
        assert organization.category_key == "productivity"
        assert personal.category_key == "personal-writing"

        with pytest.raises(
            ApplicationAgentSkillError,
            match="application_agent_skill_category_invalid",
        ):
            await service.upload_skill(
                principal=principal,
                agent_id=31,
                content=_application_skill_zip(marker="invalid", name="invalid"),
                category_key="missing-category",
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_application_asset_snapshot_pins_public_and_private_versions() -> None:
    engine, sessions = await _database()
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, code="org-1", name="测试企业"),
                User(id=7, org_id=1, user_code="ADMIN", name="管理员", status="active"),
            ]
        )
        await db.flush()
        agent = ApplicationAgent(
            id=31,
            org_id=1,
            created_by_user_id=7,
            updated_by_user_id=7,
            slug="sales-agent",
            name="销售 Agent",
            config={},
        )
        db.add(agent)
        await db.flush()
        skill = Skill(
            id=10,
            org_id=1,
            owner_user_id=7,
            slug="public-skill",
            name="公共 Skill",
            asset_scope="public",
            visibility="organization",
            status="published",
        )
        db.add(skill)
        await db.flush()
        skill_version = SkillVersion(
            id=11,
            org_id=1,
            skill_id=10,
            version=1,
            package_uri="/assets/public-skill",
            package_sha256="a" * 64,
            status="published",
        )
        db.add(skill_version)
        mcp = Mcp(
            id=20,
            org_id=1,
            owner_user_id=7,
            slug="crm",
            name="CRM",
            visibility="organization",
            status="published",
            transport="streamable_http",
            config={},
        )
        db.add(mcp)
        await db.flush()
        mcp_version = McpVersion(
            id=21,
            org_id=1,
            mcp_id=20,
            version=1,
            transport="streamable_http",
            connection_config={"url": "https://crm.example/mcp"},
            config_digest="sha256:" + "b" * 64,
            status="published",
        )
        db.add(mcp_version)
        private_skill = ApplicationAgentSkill(
            id=30,
            org_id=1,
            application_agent_id=31,
            created_by_user_id=7,
            slug="app-only",
            name="应用私有 Skill",
            package_uri="/app/skill",
            package_sha256="c" * 64,
            manifest={},
        )
        db.add(private_skill)
        await db.flush()
        skill.current_version_id = skill_version.id
        mcp.current_version_id = mcp_version.id
        await db.commit()

        snapshot = await ApplicationAgentAssetSnapshotBuilder(db).build(
            org_id=1,
            application_agent_id=31,
            config={
                "skill_asset_ids": [10],
                "application_skill_ids": [30],
                "mcp_asset_ids": [20],
                "knowledge_bindings": [],
            },
        )

        assert snapshot["asset_snapshot"] == {
            "schema_version": 1,
            "skills": [{"asset_id": 10, "version_id": 11, "sha256": "a" * 64}],
            "application_skills": [{"asset_id": 30, "version_id": 30, "sha256": "c" * 64}],
            "mcps": [{"asset_id": 20, "version_id": 21, "config_digest": "sha256:" + "b" * 64}],
            "knowledge_base_ids": [],
        }
    await engine.dispose()


@pytest.mark.asyncio
async def test_personal_skill_cannot_be_bound_as_application_skill() -> None:
    engine, sessions = await _database()
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, code="org-1", name="测试企业"),
                User(id=7, org_id=1, user_code="ADMIN", name="管理员", status="active"),
                Skill(
                    id=10,
                    org_id=1,
                    owner_user_id=7,
                    slug="personal",
                    name="个人 Skill",
                    asset_scope="personal",
                    visibility="private",
                    status="published",
                ),
            ]
        )
        await db.commit()
        with pytest.raises(ApplicationAgentAssetSnapshotError) as exc:
            await ApplicationAgentAssetSnapshotBuilder(db).build(
                org_id=1,
                application_agent_id=31,
                config={"skill_asset_ids": [10]},
            )
        assert exc.value.code == "application_agent_skill_not_active"
    await engine.dispose()


@pytest.mark.asyncio
async def test_public_asset_rollout_advances_only_bound_application_desired() -> None:
    engine, sessions = await _database()
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, code="org-1", name="测试企业"),
                User(id=7, org_id=1, user_code="ADMIN", name="管理员", status="active"),
            ]
        )
        await db.flush()
        skill = Skill(
            id=10,
            org_id=1,
            owner_user_id=7,
            slug="public-skill",
            name="公共 Skill",
            asset_scope="public",
            visibility="organization",
            status="published",
        )
        db.add(skill)
        await db.flush()
        version = SkillVersion(
            id=11,
            org_id=1,
            skill_id=10,
            version=1,
            package_uri="/assets/public-skill",
            package_sha256="a" * 64,
            status="published",
        )
        db.add(version)
        await db.flush()
        skill.current_version_id = version.id
        bound = ApplicationAgent(
            id=31,
            org_id=1,
            created_by_user_id=7,
            updated_by_user_id=7,
            slug="bound",
            name="已绑定",
            config={"skill_asset_ids": [10]},
            config_generation=1,
            apply_status="applied",
            status="published",
        )
        unbound = ApplicationAgent(
            id=32,
            org_id=1,
            created_by_user_id=7,
            updated_by_user_id=7,
            slug="unbound",
            name="未绑定",
            config={"skill_asset_ids": []},
            config_generation=1,
            apply_status="applied",
            status="published",
        )
        db.add_all([bound, unbound])
        await db.commit()

        changed = await ApplicationAgentAssetRolloutService(db).refresh_public_asset(
            org_id=1,
            asset_type="skill",
            asset_id=10,
            created_by_user_id=7,
        )
        await db.commit()

        assert changed == 1
        await db.refresh(bound)
        await db.refresh(unbound)
        assert bound.config_generation == 2
        assert bound.apply_status == "applying"
        assert bound.config["asset_snapshot"]["skills"] == [
            {"asset_id": 10, "version_id": 11, "sha256": "a" * 64}
        ]
        assert unbound.config_generation == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_public_asset_delete_removes_binding_from_application_desired() -> None:
    engine, sessions = await _database()
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, code="org-1", name="测试企业"),
                User(id=7, org_id=1, user_code="ADMIN", name="管理员", status="active"),
                ApplicationAgent(
                    id=31,
                    org_id=1,
                    created_by_user_id=7,
                    updated_by_user_id=7,
                    slug="bound",
                    name="已绑定",
                    config={
                        "skill_asset_ids": [10, 12],
                        "mcp_asset_ids": [20],
                        "asset_snapshot": {
                            "schema_version": 1,
                            "skills": [
                                {"asset_id": 10, "version_id": 11, "sha256": "a" * 64},
                                {"asset_id": 12, "version_id": 13, "sha256": "b" * 64},
                            ],
                            "application_skills": [],
                            "mcps": [
                                {"asset_id": 20, "version_id": 21, "config_digest": "sha256:" + "c" * 64}
                            ],
                            "knowledge_base_ids": [],
                        },
                    },
                    config_generation=1,
                    apply_status="applied",
                    status="published",
                ),
            ]
        )
        await db.commit()

        changed = await ApplicationAgentAssetRolloutService(db).remove_public_asset(
            org_id=1,
            asset_type="skill",
            asset_id=10,
            created_by_user_id=7,
        )
        await db.commit()

        agent = await db.get(ApplicationAgent, 31)
        assert changed == 1
        assert agent is not None
        assert agent.config_generation == 2
        assert agent.apply_status == "applying"
        assert agent.config["skill_asset_ids"] == [12]
        assert agent.config["asset_snapshot"]["skills"] == [
            {"asset_id": 12, "version_id": 13, "sha256": "b" * 64}
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_personal_knowledge_binding_is_selected_per_application() -> None:
    engine, sessions = await _database()
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, code="org-1", name="测试企业"),
                User(id=7, org_id=1, user_code="ADMIN", name="管理员", status="active"),
                ApplicationAgent(
                    id=31,
                    org_id=1,
                    created_by_user_id=7,
                    updated_by_user_id=7,
                    slug="app",
                    name="应用",
                    config={},
                ),
                ApplicationAgent(
                    id=32,
                    org_id=1,
                    created_by_user_id=7,
                    updated_by_user_id=7,
                    slug="other-app",
                    name="其他应用",
                    config={},
                ),
                KnowledgeBase(
                    id=41,
                    org_id=1,
                    owner_user_id=7,
                    slug="shared",
                    name="共享知识库",
                    kind="personal",
                    status="active",
                    dataset_id="dataset-41",
                ),
                ApplicationAgentPersonalKnowledgeBinding(
                    org_id=1,
                    application_agent_id=31,
                    knowledge_base_id=41,
                    selected_by_user_id=7,
                    status="enabled",
                    selected_at=datetime(2026, 9, 1),
                ),
            ]
        )
        await db.commit()
        selected = await ApplicationAgentAssetSnapshotBuilder(db).build(
            org_id=1,
            application_agent_id=31,
            config={},
        )
        unselected = await ApplicationAgentAssetSnapshotBuilder(db).build(
            org_id=1,
            application_agent_id=32,
            config={},
        )
        assert selected["asset_snapshot"]["knowledge_base_ids"] == [41]
        assert unselected["asset_snapshot"]["knowledge_base_ids"] == []
    await engine.dispose()
