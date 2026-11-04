from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.v1.user import default_profile_assets
from app.models import (
    PersonalSkillCategory,
    ProfileConvergenceJob,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    Skill,
    SkillVersion,
)
from app.services.profile_desired import ProfileDesiredError
from app.services.profile_desired_mutations import ProfileDesiredMutationService
from app.services.plaza.user_asset_scope import DefaultUserAssetScope
from app.services.runtime_agent_profiles import (
    RuntimeAgentProfileNotFoundError,
    resolve_default_runtime_agent_profile_id,
)
from app.services.runtime_workspace import Principal
from tests.unit.test_profile_desired import _database, _seed, _skill


def _scope() -> DefaultUserAssetScope:
    return DefaultUserAssetScope(
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        runtime_agent_profile_id=10,
        binding=SimpleNamespace(),
    )


def _skill_zip(name: str = "Atomic Personal Skill") -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: atomic upload\n---\n# Body\n",
        )
    return buffer.getvalue()


class _Request:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def body(self) -> bytes:
        return self._body


@pytest.mark.asyncio
async def test_default_profile_install_uses_stable_asset_id_and_desired_only(
    monkeypatch,
) -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)

        async def resolved_scope(**_kwargs):
            return _scope()

        monkeypatch.setattr(default_profile_assets, "_scope", resolved_scope)
        result = await default_profile_assets.install_default_profile_skill(
            3,
            101,
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )

        profile = await session.get(RuntimeAgentProfile, 10)
        binding = await session.scalar(
            select(ProfileSkillBinding).where(
                ProfileSkillBinding.runtime_agent_profile_id == 10
            )
        )
        assert result.asset_id == 101
        assert result.desired_revision == 1
        assert binding is not None and binding.skill_id == 101
        assert profile is not None and profile.applied_revision == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_personal_skill_upload_and_default_install_are_one_atomic_command(
    monkeypatch,
    tmp_path,
) -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)

        async def resolved_scope(**_kwargs):
            return _scope()

        monkeypatch.setattr(default_profile_assets, "_scope", resolved_scope)
        monkeypatch.setattr(
            default_profile_assets.settings,
            "HERMES_PUBLIC_ASSET_ROOT",
            str(tmp_path),
        )
        first = await default_profile_assets.upload_and_install_personal_skill(
            3,
            _Request(_skill_zip()),
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )
        session.add(
            PersonalSkillCategory(
                org_id=1,
                owner_user_id=2,
                category_key="personal-reporting",
                display_name="汇报",
                normalized_name="汇报",
            )
        )
        await session.commit()
        repeated = await default_profile_assets.upload_and_install_personal_skill(
            3,
            _Request(_skill_zip()),
            category_key="personal-reporting",
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )

        binding = await session.scalar(
            select(ProfileSkillBinding).where(
                ProfileSkillBinding.runtime_agent_profile_id == 10,
                ProfileSkillBinding.skill_id == first.asset.skill_id,
            )
        )
        assert first.asset.skill_id == repeated.asset.skill_id
        assert first.asset.skill_version_id == repeated.asset.skill_version_id
        assert first.asset.category_key == "uncategorized"
        assert repeated.asset.category_key == "personal-reporting"
        assert first.operation.desired_revision == 1
        assert repeated.operation.desired_revision == 1
        assert binding is not None
        assert await session.scalar(
            select(func.count(SkillVersion.id)).where(
                SkillVersion.skill_id == first.asset.skill_id
            )
        ) == 1
        stable = await session.get(Skill, first.asset.skill_id)
        assert stable is not None and stable.category_key == "personal-reporting"
        skill_files = list((tmp_path / "personal-skills").rglob("SKILL.md"))
        assert len(skill_files) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_personal_skill_upload_compensates_file_and_database_on_desired_failure(
    monkeypatch,
    tmp_path,
) -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)

        async def resolved_scope(**_kwargs):
            return _scope()

        async def fail_desired(_service, _mutation):
            raise ProfileDesiredError(
                "PROFILE_DESIRED_HEAD_INVALID",
                "forced desired failure",
            )

        monkeypatch.setattr(default_profile_assets, "_scope", resolved_scope)
        monkeypatch.setattr(
            default_profile_assets.settings,
            "HERMES_PUBLIC_ASSET_ROOT",
            str(tmp_path),
        )
        monkeypatch.setattr(
            ProfileDesiredMutationService,
            "mutate_default_asset",
            fail_desired,
        )

        with pytest.raises(HTTPException) as error:
            await default_profile_assets.upload_and_install_personal_skill(
                3,
                _Request(_skill_zip("Rollback Personal Skill")),
                principal=Principal(org_id=1, user_id=2),
                db=session,
            )

        assert error.value.detail == "PROFILE_DESIRED_HEAD_INVALID"
        assert await session.scalar(
            select(func.count(Skill.id)).where(Skill.asset_scope == "personal")
        ) == 0
        assert not list((tmp_path / "personal-skills").rglob("SKILL.md"))
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_profile_view_exposes_configured_and_applied_separately(
    monkeypatch,
) -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)

        async def resolved_scope(**_kwargs):
            return _scope()

        monkeypatch.setattr(default_profile_assets, "_scope", resolved_scope)
        await default_profile_assets.install_default_profile_mcp(
            3,
            301,
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )

        view = await default_profile_assets.get_default_profile_assets(
            3,
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )

        mcp = next(item for item in view.mcps if item.asset_id == 301)
        assert mcp.desired is True
        assert mcp.applied is False
        assert await session.scalar(
            select(ProfileMcpBinding.id).where(
                ProfileMcpBinding.runtime_agent_profile_id == 10,
                ProfileMcpBinding.mcp_id == 301,
            )
        ) is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_profile_view_uses_authoritative_skill_scope_and_semantic_version(
    monkeypatch,
) -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        archived, archived_version = _skill(
            skill_id=102,
            version_id=1701,
            asset_scope="personal",
        )
        archived.status = "archived"
        archived_version.status = "archived"
        session.add_all([archived, archived_version])
        await session.commit()

        async def resolved_scope(**_kwargs):
            return _scope()

        monkeypatch.setattr(default_profile_assets, "_scope", resolved_scope)
        view = await default_profile_assets.get_default_profile_assets(
            3,
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )

        assert [item.asset_id for item in view.skills] == [101]
        assert view.skills[0].asset_scope == "public"
        assert view.skills[0].version == 1
        assert view.skills[0].current_version_id == 201
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_profile_catalog_and_view_hide_public_system_skill_duplicates(
    monkeypatch,
) -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        duplicate, duplicate_version = _skill(skill_id=150, version_id=250)
        duplicate.slug = "hermes-agent"
        duplicate.name = "Hermes Agent"
        session.add_all([duplicate, duplicate_version])
        await session.commit()

        async def resolved_scope(**_kwargs):
            return _scope()

        monkeypatch.setattr(default_profile_assets, "_scope", resolved_scope)
        catalog = await default_profile_assets.get_user_asset_catalog(
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )
        view = await default_profile_assets.get_default_profile_assets(
            3,
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )

        assert all(item.name != "Hermes Agent" for item in catalog.skills)
        assert all(item.name != "Hermes Agent" for item in view.skills)
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_profile_view_exposes_terminal_convergence_failure(
    monkeypatch,
) -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)

        async def resolved_scope(**_kwargs):
            return _scope()

        monkeypatch.setattr(default_profile_assets, "_scope", resolved_scope)
        await default_profile_assets.install_default_profile_skill(
            3,
            101,
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )
        job = await session.scalar(select(ProfileConvergenceJob))
        assert job is not None
        job.status = "failed"
        job.last_error_code = "PROFILE_PROJECTION_STATE_DIVERGED"
        job.last_error_message = "PROFILE_PROJECTION_STATE_DIVERGED"
        await session.commit()

        view = await default_profile_assets.get_default_profile_assets(
            3,
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )

        assert view.convergence_status == "failed"
        assert view.last_error == "PROFILE_PROJECTION_STATE_DIVERGED"
    await engine.dispose()


@pytest.mark.asyncio
async def test_asset_management_remains_available_while_default_profile_converges(
    monkeypatch,
) -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)

        async def resolved_scope(**_kwargs):
            return _scope()

        with monkeypatch.context() as patch:
            patch.setattr(default_profile_assets, "_scope", resolved_scope)
            accepted = await default_profile_assets.install_default_profile_mcp(
                3,
                301,
                principal=Principal(org_id=1, user_id=2),
                db=session,
            )

        profile = await session.get(RuntimeAgentProfile, 10)
        assert profile is not None
        assert profile.status == "active"
        assert profile.desired_revision == accepted.desired_revision == 1
        assert profile.applied_revision == 0

        with pytest.raises(RuntimeAgentProfileNotFoundError):
            await resolve_default_runtime_agent_profile_id(
                session,
                runtime_workspace_id=3,
                org_id=1,
                user_id=2,
            )

        view = await default_profile_assets.get_default_profile_assets(
            3,
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )
        repeated = await default_profile_assets.install_default_profile_mcp(
            3,
            301,
            principal=Principal(org_id=1, user_id=2),
            db=session,
        )

        mcp = next(item for item in view.mcps if item.asset_id == 301)
        assert mcp.desired is True
        assert mcp.applied is False
        assert repeated.desired_revision == 1
    await engine.dispose()
