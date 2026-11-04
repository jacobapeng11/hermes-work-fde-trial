"""Test: self-created expert deletion properly clears asset bindings.

This test verifies the fix for the issue where ExpertAuthoringService.delete()
would leave orphaned rows in profile_skill_binding, profile_mcp_binding, and
profile_knowledge_binding tables because the ON DELETE CASCADE on the FK
never fires (profile is soft-deleted, not hard-deleted).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import BigInteger, func, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import (
    Base,
    CloudHermesProfile,
    KnowledgeBase,
    Mcp,
    McpVersion,
    Organization,
    ProfileKnowledgeBinding,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    Skill,
    SkillVersion,
    User,
)
from app.services.expert_authoring import ExpertAuthoringService
from app.services.runtime_workspace import Principal


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(
    _type: BigInteger, _compiler: Any, **_kwargs: Any
) -> str:
    return "INTEGER"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_sqlite_text(
    _type: LONGTEXT, _compiler: Any, **_kwargs: Any
) -> str:
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
                User(id=1, org_id=1, name="Owner", status="active"),
                CloudHermesProfile(
                    id=1,
                    org_id=1,
                    user_id=1,
                    profile_type="default_chat_agent",
                    name="默认数字人",
                    status="active",
                ),
            ]
        )
        await db.commit()
    return engine, sessions


@pytest.mark.asyncio
async def test_self_created_expert_delete_clears_asset_bindings() -> None:
    """Deleting a self-created expert must clear all three binding tables.

    Root cause: profile rows are soft-deleted (status='deleted'), so the
    ON DELETE CASCADE on binding FKs never fires. The delete() method must
    explicitly remove binding rows.
    """
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            # Create a self-created expert profile
            profile = RuntimeAgentProfile(
                org_id=1,
                user_id=1,
                runtime_workspace_id=1,
                hermes_profile_name="expert-to-delete",
                profile_kind="expert",
                display_name="待删除专家",
                ownership_type="self_created",
                capability_config={},
                status="active",
            )
            db.add(profile)
            await db.commit()
            profile_id = int(profile.id)

            # Seed asset catalog rows that bindings reference
            skill = Skill(
                id=101,
                org_id=1,
                owner_user_id=1,
                slug="test-skill",
                name="Test Skill",
                asset_scope="private",
                visibility="private",
                status="published",
                current_version_id=201,
            )
            skill_version = SkillVersion(
                id=201,
                org_id=1,
                skill_id=101,
                version=1,
                package_uri="mem://test",
                status="published",
            )
            mcp = Mcp(
                id=301,
                org_id=1,
                owner_user_id=1,
                slug="test-mcp",
                name="Test MCP",
                visibility="private",
                status="published",
                transport="streamable_http",
                config={"url": "https://mcp.example.com"},
            )
            mcp_version = McpVersion(
                id=401,
                org_id=1,
                mcp_id=301,
                version=1,
                transport="streamable_http",
                connection_config={"url": "https://mcp.example.com"},
                config_digest="sha256:" + "4" * 64,
                status="published",
            )
            kb = KnowledgeBase(
                id=501,
                org_id=1,
                owner_user_id=1,
                slug="test-kb",
                name="Test KB",
                kind="personal",
                provider="filebase",
                visibility="private",
                status="active",
            )
            db.add_all([skill, skill_version, mcp, mcp_version, kb])
            await db.commit()

            # Create bindings for the profile
            db.add_all(
                [
                    ProfileSkillBinding(
                        org_id=1,
                        runtime_workspace_id=1,
                        runtime_agent_profile_id=profile_id,
                        skill_id=101,
                        skill_version_id=201,
                        version_policy="follow_latest",
                        status="enabled",
                    ),
                    ProfileMcpBinding(
                        org_id=1,
                        runtime_workspace_id=1,
                        runtime_agent_profile_id=profile_id,
                        mcp_id=301,
                        mcp_version_id=401,
                        toolset_key="mcp-asset-301",
                        status="enabled",
                    ),
                    ProfileKnowledgeBinding(
                        org_id=1,
                        runtime_workspace_id=1,
                        runtime_agent_profile_id=profile_id,
                        knowledge_base_id=501,
                        status="enabled",
                    ),
                ]
            )
            await db.commit()

            # Verify bindings exist before deletion
            assert (
                await db.scalar(
                    select(func.count(ProfileSkillBinding.id)).where(
                        ProfileSkillBinding.runtime_agent_profile_id == profile_id
                    )
                )
            ) == 1
            assert (
                await db.scalar(
                    select(func.count(ProfileMcpBinding.id)).where(
                        ProfileMcpBinding.runtime_agent_profile_id == profile_id
                    )
                )
            ) == 1
            assert (
                await db.scalar(
                    select(func.count(ProfileKnowledgeBinding.id)).where(
                        ProfileKnowledgeBinding.runtime_agent_profile_id == profile_id
                    )
                )
            ) == 1

            # Delete the self-created expert
            principal = Principal(org_id=1, user_id=1)
            result = await ExpertAuthoringService(db).delete(
                principal=principal,
                runtime_workspace_id=1,
                runtime_agent_profile_id=profile_id,
            )
            await db.commit()

            # Profile should be soft-deleted
            assert result.status == "deleted"
            assert result.active_display_name_key is None

            # All three binding tables must be cleared for this profile
            assert (
                await db.scalar(
                    select(func.count(ProfileSkillBinding.id)).where(
                        ProfileSkillBinding.runtime_agent_profile_id == profile_id
                    )
                )
            ) == 0
            assert (
                await db.scalar(
                    select(func.count(ProfileMcpBinding.id)).where(
                        ProfileMcpBinding.runtime_agent_profile_id == profile_id
                    )
                )
            ) == 0
            assert (
                await db.scalar(
                    select(func.count(ProfileKnowledgeBinding.id)).where(
                        ProfileKnowledgeBinding.runtime_agent_profile_id == profile_id
                    )
                )
            ) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_self_created_expert_delete_with_no_bindings_succeeds() -> None:
    """Deleting a self-created expert with no bindings should succeed (idempotent)."""
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            # Create a self-created expert profile with no bindings
            profile = RuntimeAgentProfile(
                org_id=1,
                user_id=1,
                runtime_workspace_id=1,
                hermes_profile_name="expert-no-bindings",
                profile_kind="expert",
                display_name="无绑定专家",
                ownership_type="self_created",
                capability_config={},
                status="active",
            )
            db.add(profile)
            await db.commit()
            profile_id = int(profile.id)

            # Delete should succeed without errors
            principal = Principal(org_id=1, user_id=1)
            result = await ExpertAuthoringService(db).delete(
                principal=principal,
                runtime_workspace_id=1,
                runtime_agent_profile_id=profile_id,
            )
            await db.commit()

            assert result.status == "deleted"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_self_created_expert_delete_does_not_affect_other_profiles() -> None:
    """Deleting one expert must not clear bindings belonging to other profiles."""
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            # Create two self-created expert profiles
            profile_a = RuntimeAgentProfile(
                org_id=1,
                user_id=1,
                runtime_workspace_id=1,
                hermes_profile_name="expert-a",
                profile_kind="expert",
                display_name="专家A",
                ownership_type="self_created",
                capability_config={},
                status="active",
            )
            profile_b = RuntimeAgentProfile(
                org_id=1,
                user_id=1,
                runtime_workspace_id=1,
                hermes_profile_name="expert-b",
                profile_kind="expert",
                display_name="专家B",
                ownership_type="self_created",
                capability_config={},
                status="active",
            )
            db.add_all([profile_a, profile_b])
            await db.commit()
            profile_a_id = int(profile_a.id)
            profile_b_id = int(profile_b.id)

            # Seed a skill for bindings
            skill = Skill(
                id=101,
                org_id=1,
                owner_user_id=1,
                slug="shared-skill",
                name="Shared Skill",
                asset_scope="private",
                visibility="private",
                status="published",
                current_version_id=201,
            )
            skill_version = SkillVersion(
                id=201,
                org_id=1,
                skill_id=101,
                version=1,
                package_uri="mem://test",
                status="published",
            )
            db.add_all([skill, skill_version])
            await db.commit()

            # Both profiles bind the same skill
            db.add_all(
                [
                    ProfileSkillBinding(
                        org_id=1,
                        runtime_workspace_id=1,
                        runtime_agent_profile_id=profile_a_id,
                        skill_id=101,
                        skill_version_id=201,
                        version_policy="follow_latest",
                        status="enabled",
                    ),
                    ProfileSkillBinding(
                        org_id=1,
                        runtime_workspace_id=1,
                        runtime_agent_profile_id=profile_b_id,
                        skill_id=101,
                        skill_version_id=201,
                        version_policy="follow_latest",
                        status="enabled",
                    ),
                ]
            )
            await db.commit()

            # Delete profile A
            principal = Principal(org_id=1, user_id=1)
            await ExpertAuthoringService(db).delete(
                principal=principal,
                runtime_workspace_id=1,
                runtime_agent_profile_id=profile_a_id,
            )
            await db.commit()

            # Profile A's bindings should be cleared
            assert (
                await db.scalar(
                    select(func.count(ProfileSkillBinding.id)).where(
                        ProfileSkillBinding.runtime_agent_profile_id == profile_a_id
                    )
                )
            ) == 0

            # Profile B's bindings should be untouched
            assert (
                await db.scalar(
                    select(func.count(ProfileSkillBinding.id)).where(
                        ProfileSkillBinding.runtime_agent_profile_id == profile_b_id
                    )
                )
            ) == 1
    finally:
        await engine.dispose()
