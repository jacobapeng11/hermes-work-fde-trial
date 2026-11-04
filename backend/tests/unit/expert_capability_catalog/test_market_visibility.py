from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import Base, ProfileSkillBinding, Skill, SkillVersion
from app.services import expert_capability_catalog
from app.services.expert_capability_catalog import (
    ExpertCapabilityCatalogQueryService,
)
from app.services.runtime_workspace import Principal


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(
    _type: BigInteger,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "INTEGER"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_sqlite_text(
    _type: LONGTEXT,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "TEXT"


@pytest.mark.asyncio
async def test_market_catalog_hides_unpublished_skill_but_keeps_it_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    try:
        async with sessions() as db:
            visible = Skill(
                id=10,
                org_id=1,
                owner_user_id=1,
                slug="visible-skill",
                name="市场可见技能",
                category_key="other",
                asset_scope="public",
                visibility="public",
                status="published",
                market_visible=True,
                current_version_id=11,
            )
            hidden = Skill(
                id=12,
                org_id=1,
                owner_user_id=1,
                slug="hidden-skill",
                name="已下架技能",
                category_key="other",
                asset_scope="public",
                visibility="public",
                status="published",
                market_visible=False,
                current_version_id=13,
            )
            application = Skill(
                id=14,
                org_id=1,
                owner_user_id=1,
                slug="application-skill",
                name="应用 Agent 技能",
                category_key="other",
                asset_scope="application",
                visibility="private",
                status="private",
                market_visible=True,
                current_version_id=15,
            )
            personal = Skill(
                id=16,
                org_id=1,
                owner_user_id=1,
                slug="personal-skill",
                name="个人技能",
                category_key="other",
                asset_scope="personal",
                visibility="private",
                status="private",
                market_visible=True,
                current_version_id=17,
            )
            db.add_all(
                [
                    visible,
                    hidden,
                    application,
                    personal,
                    SkillVersion(
                        id=11,
                        org_id=1,
                        skill_id=10,
                        version=1,
                        package_uri="/tmp/visible",
                        package_sha256="a" * 64,
                        manifest={},
                        status="published",
                    ),
                    SkillVersion(
                        id=15,
                        org_id=1,
                        skill_id=14,
                        version=1,
                        package_uri="/tmp/application",
                        package_sha256="c" * 64,
                        manifest={},
                        status="private",
                    ),
                    SkillVersion(
                        id=17,
                        org_id=1,
                        skill_id=16,
                        version=1,
                        package_uri="/tmp/personal",
                        package_sha256="d" * 64,
                        manifest={},
                        status="private",
                    ),
                    SkillVersion(
                        id=13,
                        org_id=1,
                        skill_id=12,
                        version=1,
                        package_uri="/tmp/hidden",
                        package_sha256="b" * 64,
                        manifest={},
                        status="published",
                    ),
                    ProfileSkillBinding(
                        org_id=1,
                        runtime_workspace_id=1,
                        runtime_agent_profile_id=1,
                        skill_id=12,
                        skill_version_id=13,
                        version_policy="pinned",
                        status="enabled",
                    ),
                ]
            )
            await db.commit()

            service = ExpertCapabilityCatalogQueryService(db)
            service._visible_knowledge_base_ids = _no_knowledge_bases  # type: ignore[method-assign]
            monkeypatch.setattr(
                expert_capability_catalog,
                "resolve_default_user_asset_scope",
                _default_scope,
            )
            catalog = await service.list_for(principal=Principal(org_id=1, user_id=1))

            assert [item.asset_id for item in catalog.market_skills] == [10]
            assert [item.asset_id for item in catalog.installed_skills] == [12]
            assert [item.asset_id for item in catalog.personal_skills] == [16]
    finally:
        await engine.dispose()


async def _no_knowledge_bases(
    *_args: Any,
    **_kwargs: Any,
) -> set[int]:
    return set()


async def _default_scope(
    *_args: Any,
    **_kwargs: Any,
) -> SimpleNamespace:
    return SimpleNamespace(runtime_workspace_id=1, runtime_agent_profile_id=1)
