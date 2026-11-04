from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.mysql import LONGTEXT

from app.models import Base, Skill
from app.api.v1.user import default_profile_assets
from app.schemas.asset_catalog import CreateSkillAssetCommand, SkillVersionMaterial
from app.services.asset_catalog import AssetCatalogService
from app.services.profile_desired import ProfileDesiredError, ProfileDesiredService
from app.services.plaza.user_asset_scope import DefaultUserAssetScope
from app.services.runtime_workspace import Principal
from tests.unit.test_profile_desired import (
    _command,
    _database as _profile_database,
    _seed,
    _skill,
)


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(_type: BigInteger, _compiler: Any, **_kwargs: Any) -> str:
    return "INTEGER"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_sqlite_text(_type: LONGTEXT, _compiler: Any, **_kwargs: Any) -> str:
    return "TEXT"


@pytest.mark.asyncio
async def test_unpublish_hides_market_without_revoking_installed_skill() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        result = await AssetCatalogService(session).create_skill(
            CreateSkillAssetCommand(
                org_id=1,
                owner_user_id=2,
                slug="market-skill",
                name="Market Skill",
                asset_scope="public",
                visibility="organization",
                material=SkillVersionMaterial(
                    package_uri="/assets/current",
                    package_sha256="a" * 64,
                    manifest={"name": "Market Skill"},
                ),
            )
        )
        await AssetCatalogService(session).set_skill_market_visible(
            org_id=1,
            skill_id=result.asset_id,
            visible=False,
        )
        await session.commit()

        skill = await session.scalar(select(Skill).where(Skill.id == result.asset_id))
        assert skill is not None
        assert skill.market_visible is False
        assert skill.revoked_at is None
        assert skill.status == "published"
    await engine.dispose()


@pytest.mark.asyncio
async def test_unpublished_skill_blocks_new_install_but_keeps_existing_binding() -> None:
    engine, sessions = await _profile_database()
    async with sessions() as session:
        await _seed(session)
        skill = await session.get(Skill, 101)
        assert skill is not None
        skill.market_visible = False
        await session.flush()

        with pytest.raises(ProfileDesiredError) as blocked:
            await ProfileDesiredService(session).replace(_command())
        assert blocked.value.code == "SKILL_NOT_MARKET_VISIBLE"

        skill.market_visible = True
        await ProfileDesiredService(session).replace(_command())
        await session.commit()

        skill.market_visible = False
        await session.flush()
        retained = await ProfileDesiredService(session).replace(_command())
        assert retained.created_revision is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_profile_view_hides_uninstalled_unpublished_market_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _profile_database()
    async with sessions() as session:
        await _seed(session)
        hidden, hidden_version = _skill(skill_id=102, version_id=202)
        hidden.market_visible = False
        session.add_all([hidden, hidden_version])
        await session.flush()

        async def resolved_scope(**_kwargs: object) -> DefaultUserAssetScope:
            return DefaultUserAssetScope(
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                runtime_agent_profile_id=10,
                binding=SimpleNamespace(),
            )

        monkeypatch.setattr(default_profile_assets, "_scope", resolved_scope)
        view = await default_profile_assets.get_default_profile_assets(
            3,
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )

        asset_ids = [item.asset_id for item in view.skills]
        assert 101 in asset_ids
        assert 102 not in asset_ids
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_profile_view_keeps_installed_skill_after_market_unpublish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _profile_database()
    async with sessions() as session:
        await _seed(session)
        await ProfileDesiredService(session).replace(_command())
        await session.flush()
        skill = await session.get(Skill, 101)
        assert skill is not None
        skill.market_visible = False
        await session.flush()

        async def resolved_scope(**_kwargs: object) -> DefaultUserAssetScope:
            return DefaultUserAssetScope(
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                runtime_agent_profile_id=10,
                binding=SimpleNamespace(),
            )

        monkeypatch.setattr(default_profile_assets, "_scope", resolved_scope)
        view = await default_profile_assets.get_default_profile_assets(
            3,
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )

        item = next(item for item in view.skills if item.asset_id == 101)
        assert item.configured is True
        assert item.desired is True
    await engine.dispose()
