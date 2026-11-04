from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import BigInteger, func, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import (
    Base,
    ExpertPackage,
    ExpertPackageVersion,
    HermesRuntimeBinding,
    Mcp,
    McpVersion,
    ProfileConvergenceJob,
    ProfileKnowledgeBinding,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    RuntimeMcpRegistryEntry,
    RuntimeMcpRegistryJob,
    RuntimeMcpRegistryRevision,
    RuntimeMcpRegistryState,
    Skill,
    SkillVersion,
)
from app.schemas.profile_desired import (
    ProfileDesiredCommand,
    ProfileDesiredDefinition,
    ProfileDesiredMcpRef,
    ProfileDesiredSkillRef,
)
from app.services.profile_desired import ProfileDesiredError, ProfileDesiredService
from app.services.profile_desired_mutations import ProfileDesiredMutationService


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


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _profile(
    *,
    profile_id: int = 10,
    user_id: int = 2,
    expert_package_id: int | None = None,
    ownership_type: str | None = None,
) -> RuntimeAgentProfile:
    return RuntimeAgentProfile(
        id=profile_id,
        org_id=1,
        user_id=user_id,
        runtime_workspace_id=3,
        hermes_profile_name=("default" if profile_id == 10 else f"expert-{profile_id}"),
        profile_kind=("default" if profile_id == 10 else "expert"),
        display_name="Default",
        ownership_type=(
            ownership_type
            if ownership_type is not None
            else ("default" if profile_id == 10 else "self_created")
        ),
        expert_package_id=expert_package_id,
        default_workspace_key=3 if profile_id == 10 else None,
        config_generation=1,
        applied_config_generation=1,
        desired_revision=0,
        applied_revision=0,
        capability_config={},
        status="active",
    )


def _skill(
    *,
    skill_id: int = 101,
    version_id: int = 201,
    owner_user_id: int = 2,
    asset_scope: str = "public",
) -> tuple[Skill, SkillVersion]:
    skill = Skill(
        id=skill_id,
        org_id=1,
        owner_user_id=owner_user_id,
        slug=f"skill-{skill_id}",
        name=f"Skill {skill_id}",
        asset_scope=asset_scope,
        visibility="public" if asset_scope == "public" else "private",
        status="published" if asset_scope == "public" else "private",
        current_version_id=version_id,
    )
    version = SkillVersion(
        id=version_id,
        org_id=1,
        skill_id=skill_id,
        version=1,
        package_uri=f"/data/assets/skills/{skill_id}/{version_id}",
        package_sha256=f"{skill_id:064x}"[-64:],
        manifest={"name": skill.slug},
        status="published" if asset_scope == "public" else "private",
        published_at=datetime.utcnow(),
    )
    return skill, version


def _mcp(
    *,
    mcp_id: int = 301,
    version_id: int = 401,
    owner_user_id: int = 2,
    visibility: str = "public",
) -> tuple[Mcp, McpVersion]:
    mcp = Mcp(
        id=mcp_id,
        org_id=1,
        owner_user_id=owner_user_id,
        slug=f"mcp-{mcp_id}",
        name=f"MCP {mcp_id}",
        visibility=visibility,
        current_version_id=version_id,
        transport="streamable_http",
        config={},
        status="published",
    )
    version = McpVersion(
        id=version_id,
        org_id=1,
        mcp_id=mcp_id,
        version=1,
        transport="streamable_http",
        connection_config={"url": "https://mcp.example.com"},
        secret_refs={"Authorization": "Bearer test-token"},
        config_digest="sha256:" + "4" * 64,
        tools_schema=[{"name": "search"}],
        tools_schema_digest="sha256:" + "5" * 64,
        status="published",
        published_at=datetime.utcnow(),
    )
    return mcp, version


def _command(
    *,
    profile_id: int = 10,
    user_id: int = 2,
    skills: list[ProfileDesiredSkillRef] | None = None,
    mcps: list[ProfileDesiredMcpRef] | None = None,
    knowledge_base_ids: list[int] | None = None,
) -> ProfileDesiredCommand:
    return ProfileDesiredCommand(
        org_id=1,
        user_id=user_id,
        runtime_workspace_id=3,
        runtime_agent_profile_id=profile_id,
        definition=ProfileDesiredDefinition(
            display_name="Customer Expert",
            description="Handles customer questions",
            soul_content="Be precise.",
            system_prompt="Use approved tools only.",
            enterprise_knowledge_enabled=True,
            plugin_ids=["enterprise-knowledge"],
        ),
        skills=(
            [ProfileDesiredSkillRef(skill_id=101, version_policy="follow_latest")]
            if skills is None
            else skills
        ),
        mcps=([ProfileDesiredMcpRef(mcp_id=301)] if mcps is None else mcps),
        knowledge_base_ids=[501] if knowledge_base_ids is None else knowledge_base_ids,
        source_type="default_user" if profile_id == 10 else "private_expert",
        source_id="operation-0001",
        created_by_user_id=user_id,
    )


async def _seed(session, *, include_second_profile: bool = False) -> None:
    skill, version = _skill()
    mcp, mcp_version = _mcp()
    session.add_all(
        [
            _profile(),
            skill,
            version,
            mcp,
            mcp_version,
            HermesRuntimeBinding(
                id=119,
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                runtime_id="rt-119",
                image="hermes:test",
                k8s_namespace="test",
                knative_service="rt-119",
                endpoint_base_url="http://runtime.invalid",
                endpoint_host_header="runtime.invalid",
                hermes_home_sub_path="users/2",
                workspace_sub_path="workspaces/3",
                hermes_home_mount_path="/home/hermes",
                workspace_mount_path="/workspace",
                api_key_encrypted="secret",
                status="ready",
            ),
        ]
    )
    if include_second_profile:
        session.add(_profile(profile_id=11))
    await session.commit()


@pytest.mark.asyncio
async def test_replace_creates_complete_revision_bindings_and_one_job() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)

        result = await ProfileDesiredService(session).replace(_command())
        await session.commit()

        profile = await session.get(RuntimeAgentProfile, 10)
        revision = await session.scalar(select(RuntimeAgentProfileRevision))
        job = await session.scalar(select(ProfileConvergenceJob))
        assert result.created_revision is True
        assert result.desired_revision == 1
        assert profile is not None and profile.status == "active"
        assert profile.desired_digest == result.profile_digest
        assert revision is not None
        assert revision.snapshot["knowledge"] == {
            "enterprise_knowledge_enabled": True,
            "personal_knowledge_base_ids": [501],
        }
        assert revision.snapshot["mcps"] == [
            {
                "mcp_id": 301,
                "mcp_version_id": 401,
                "server_slug": "asset-301",
                "toolset_key": "mcp-asset-301",
                "transport": "streamable_http",
                "connection_config": {"url": "https://mcp.example.com"},
                "credential_refs": {"Authorization": "Bearer test-token"},
                "config_digest": "sha256:" + "4" * 64,
            }
        ]
        assert job is not None and job.target_revision == 1
        assert await session.scalar(select(func.count(ProfileSkillBinding.id))) == 1
        assert await session.scalar(select(func.count(ProfileMcpBinding.id))) == 1
        assert await session.scalar(select(func.count(ProfileKnowledgeBinding.id))) == 1
        registry = await session.scalar(select(RuntimeMcpRegistryState))
        assert registry is not None
        assert registry.desired_revision == 1
        assert registry.applied_revision == 0
        assert registry.status == "installing"
        assert await session.scalar(
            select(func.count(RuntimeMcpRegistryRevision.id))
        ) == 1
        assert await session.scalar(
            select(func.count(RuntimeMcpRegistryEntry.id))
        ) == 1
        registry_job = await session.scalar(select(RuntimeMcpRegistryJob))
        assert registry_job is not None and registry_job.target_revision == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_replace_rejects_historical_mcp_with_empty_http_config() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        version = await session.get(McpVersion, 401)
        assert version is not None
        version.connection_config = {}

        with pytest.raises(ProfileDesiredError) as failure:
            await ProfileDesiredService(session).replace(_command())

        assert failure.value.code == "MCP_VERSION_UNAVAILABLE"
        assert await session.scalar(select(func.count(RuntimeAgentProfileRevision.id))) == 0
        assert await session.scalar(select(func.count(ProfileMcpBinding.id))) == 0
        assert await session.scalar(select(func.count(RuntimeMcpRegistryRevision.id))) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_replace_rejects_historical_mcp_with_non_object_config() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        version = await session.get(McpVersion, 401)
        assert version is not None
        version.connection_config = []

        with pytest.raises(ProfileDesiredError) as failure:
            await ProfileDesiredService(session).replace(_command())

        assert failure.value.code == "MCP_VERSION_UNAVAILABLE"
        assert await session.scalar(select(func.count(ProfileMcpBinding.id))) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_unload_resets_failed_registry_and_profile_jobs() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        service = ProfileDesiredService(session)
        await service.replace(_command())
        await session.flush()
        registry_state = await session.scalar(select(RuntimeMcpRegistryState))
        registry_job = await session.scalar(select(RuntimeMcpRegistryJob))
        profile_job = await session.scalar(select(ProfileConvergenceJob))
        assert registry_state is not None
        assert registry_job is not None
        assert profile_job is not None
        registry_state.status = "failed"
        registry_state.last_error_code = "MCP_REGISTRY_CONFIG_INVALID"
        registry_job.status = "failed"
        profile_job.status = "failed"

        result = await service.replace(
            _command(skills=[], mcps=[], knowledge_base_ids=[])
        )
        await session.commit()

        assert result.desired_revision == 2
        assert registry_state.desired_revision == 2
        assert registry_state.status == "installing"
        assert registry_state.last_error_code is None
        assert registry_job.target_revision == 2
        assert registry_job.status == "waiting"
        assert profile_job.target_revision == 2
        assert profile_job.status == "waiting"
        assert await session.scalar(
            select(func.count(RuntimeMcpRegistryRevision.id))
        ) == 2
        assert await session.scalar(
            select(func.count(RuntimeMcpRegistryEntry.id))
        ) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_identical_complete_desired_is_idempotent_and_repairs_job() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        service = ProfileDesiredService(session)
        first = await service.replace(_command())
        await session.flush()
        job = await session.scalar(select(ProfileConvergenceJob))
        assert job is not None
        await session.delete(job)
        await session.flush()

        second = await service.replace(_command())
        await session.commit()

        assert first.desired_revision == second.desired_revision == 1
        assert second.created_revision is False
        assert await session.scalar(
            select(func.count(RuntimeAgentProfileRevision.id))
        ) == 1
        assert await session.scalar(select(func.count(ProfileConvergenceJob.id))) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_new_complete_desired_replaces_bindings_and_advances_once() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        service = ProfileDesiredService(session)
        await service.replace(_command())
        await session.flush()

        result = await service.replace(
            _command(skills=[], mcps=[], knowledge_base_ids=[])
        )
        await session.commit()

        job = await session.scalar(select(ProfileConvergenceJob))
        assert result.desired_revision == 2
        assert job is not None and job.target_revision == 2
        assert await session.scalar(select(func.count(ProfileSkillBinding.id))) == 0
        assert await session.scalar(select(func.count(ProfileMcpBinding.id))) == 0
        assert await session.scalar(select(func.count(ProfileKnowledgeBinding.id))) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_profile_binding_reuses_ready_registry_until_last_reference_removed() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session, include_second_profile=True)
        service = ProfileDesiredService(session)
        await service.replace(_command())
        await session.flush()
        registry = await session.scalar(select(RuntimeMcpRegistryState))
        registry_job = await session.scalar(select(RuntimeMcpRegistryJob))
        assert registry is not None and registry_job is not None
        registry.applied_revision = 1
        registry.applied_digest = registry.desired_digest
        registry.status = "ready"
        registry_job.status = "completed"
        await session.flush()

        await service.replace(
            _command(
                profile_id=11,
                skills=[],
                knowledge_base_ids=[],
            )
        )
        await session.flush()
        assert registry.desired_revision == 1
        assert await session.scalar(
            select(func.count(RuntimeMcpRegistryRevision.id))
        ) == 1

        await service.replace(_command(mcps=[]))
        await session.flush()
        assert registry.desired_revision == 1

        await service.replace(
            _command(
                profile_id=11,
                skills=[],
                mcps=[],
                knowledge_base_ids=[],
            )
        )
        await session.commit()
        assert registry.desired_revision == 2
        assert registry.status == "installing"
        assert await session.scalar(
            select(func.count(RuntimeMcpRegistryRevision.id))
        ) == 2
        assert await session.scalar(
            select(func.count(RuntimeMcpRegistryEntry.id))
        ) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_same_registry_is_reapplied_when_runtime_identity_changes() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        service = ProfileDesiredService(session)
        await service.replace(_command())
        await session.flush()
        registry = await session.scalar(select(RuntimeMcpRegistryState))
        binding = await session.get(HermesRuntimeBinding, 119)
        assert registry is not None and binding is not None
        registry.applied_revision = 1
        registry.applied_digest = registry.desired_digest
        registry.status = "ready"
        binding.runtime_id = "rt-120"
        await session.flush()

        result = await service.replace(_command())
        await session.commit()

        assert result.created_revision is False
        assert registry.desired_revision == 2
        assert registry.runtime_id == "rt-120"
        assert registry.status == "installing"
        assert await session.scalar(
            select(func.count(RuntimeMcpRegistryRevision.id))
        ) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_archived_skill_can_remain_but_cannot_enter_another_profile() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session, include_second_profile=True)
        service = ProfileDesiredService(session)
        await service.replace(_command())
        await session.flush()
        skill = await session.get(Skill, 101)
        assert skill is not None
        skill.archived_at = datetime.utcnow()
        await session.flush()

        preserved = await service.replace(_command())
        assert preserved.desired_revision == 1
        with pytest.raises(ProfileDesiredError) as error:
            await service.replace(_command(profile_id=11))
        assert error.value.code == "SKILL_ARCHIVED"
        migrated = await service.replace(
            _command(profile_id=11).model_copy(update={"source_type": "migration"})
        )
        assert migrated.created_revision is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_personal_skill_is_owner_scoped() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        personal, version = _skill(
            skill_id=102,
            version_id=202,
            owner_user_id=9,
            asset_scope="personal",
        )
        session.add_all([_profile(), personal, version])
        await session.commit()

        with pytest.raises(ProfileDesiredError) as error:
            await ProfileDesiredService(session).replace(
                _command(
                    skills=[
                        ProfileDesiredSkillRef(
                            skill_id=102,
                            version_policy="follow_latest",
                        )
                    ],
                    mcps=[],
                    knowledge_base_ids=[],
                )
            )
        assert error.value.code == "PERSONAL_SKILL_FORBIDDEN"
        profile = await session.get(RuntimeAgentProfile, 10)
        assert profile is not None and profile.desired_revision == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_archived_mcp_can_remain_but_cannot_enter_another_profile() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session, include_second_profile=True)
        service = ProfileDesiredService(session)
        await service.replace(_command())
        await session.flush()
        mcp = await session.get(Mcp, 301)
        assert mcp is not None
        mcp.archived_at = datetime.utcnow()
        await session.flush()

        preserved = await service.replace(_command())
        assert preserved.desired_revision == 1
        with pytest.raises(ProfileDesiredError) as error:
            await service.replace(_command(profile_id=11, skills=[]))
        assert error.value.code == "MCP_ARCHIVED"
        migrated = await service.replace(
            _command(profile_id=11, skills=[]).model_copy(update={"source_type": "migration"})
        )
        assert migrated.created_revision is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_organization_mcp_is_visible_to_another_user_in_the_same_org() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        mcp = await session.get(Mcp, 301)
        assert mcp is not None
        mcp.owner_user_id = 9
        mcp.visibility = "organization"
        await session.flush()

        result = await ProfileDesiredService(session).replace(
            _command(skills=[], knowledge_base_ids=[])
        )

        assert result.created_revision is True
        assert await session.scalar(select(func.count(ProfileMcpBinding.id))) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_market_profile_personal_skill_requires_approved_version_declaration() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        package = ExpertPackage(
            id=601,
            org_id=1,
            owner_user_id=9,
            slug="market-expert",
            name="Market Expert",
            status="published",
            visibility="organization",
        )
        versions = [
            ExpertPackageVersion(
                id=701,
                org_id=1,
                expert_package_id=601,
                version=1,
                manifest={},
                skill_refs=[{"source": "personal", "asset_id": 103}],
                status="approved",
            ),
            ExpertPackageVersion(
                id=702,
                org_id=1,
                expert_package_id=601,
                version=2,
                manifest={},
                skill_refs=[],
                status="approved",
            ),
        ]
        personal, personal_version = _skill(
            skill_id=103,
            version_id=203,
            owner_user_id=9,
            asset_scope="personal",
        )
        first_profile = _profile(profile_id=11, expert_package_id=601)
        first_profile.ownership_type = "market_installed"
        second_profile = _profile(profile_id=12, expert_package_id=601)
        second_profile.ownership_type = "market_installed"
        session.add_all(
            [
                package,
                *versions,
                personal,
                personal_version,
                first_profile,
                second_profile,
            ]
        )
        await session.commit()

        valid = _command(
            profile_id=11,
            skills=[ProfileDesiredSkillRef(skill_id=103)],
            mcps=[],
            knowledge_base_ids=[],
        ).model_copy(
            update={
                "expert_package_version_id": 701,
                "source_type": "market_expert",
            }
        )
        result = await ProfileDesiredMutationService(session).save_expert_complete(valid)
        assert result.desired_revision == 1

        mismatch = valid.model_copy(
            update={
                "runtime_agent_profile_id": 12,
                "expert_package_version_id": 702,
            }
        )
        with pytest.raises(ProfileDesiredError) as error:
            await ProfileDesiredService(session).replace(mismatch)
        assert error.value.code == "PERSONAL_SKILL_FORBIDDEN"
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_and_expert_command_scopes_cannot_cross() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session, include_second_profile=True)

        expert_command = _command(profile_id=11, skills=[], mcps=[], knowledge_base_ids=[])
        invalid = expert_command.model_copy(update={"source_type": "default_user"})
        with pytest.raises(ProfileDesiredError) as error:
            await ProfileDesiredService(session).replace(invalid)
        assert error.value.code == "PROFILE_COMMAND_SCOPE_INVALID"
    await engine.dispose()


@pytest.mark.asyncio
async def test_private_expert_save_cannot_target_another_users_profile() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session, include_second_profile=True)
        other_users_command = _command(
            profile_id=11,
            skills=[],
            mcps=[],
            knowledge_base_ids=[],
        ).model_copy(update={"user_id": 3})

        with pytest.raises(ProfileDesiredError) as error:
            await ProfileDesiredMutationService(session).save_expert_complete(
                other_users_command
            )
        assert error.value.code == "PROFILE_NOT_FOUND"
        profile = await session.get(RuntimeAgentProfile, 11)
        assert profile is not None and profile.desired_revision == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_broken_desired_head_fails_closed_instead_of_guessing() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        service = ProfileDesiredService(session)
        await service.replace(_command())
        await session.flush()
        revision = await session.scalar(select(RuntimeAgentProfileRevision))
        assert revision is not None
        await session.delete(revision)
        await session.flush()

        with pytest.raises(ProfileDesiredError) as error:
            await service.replace(_command())
        assert error.value.code == "PROFILE_DESIRED_HEAD_INVALID"
    await engine.dispose()
