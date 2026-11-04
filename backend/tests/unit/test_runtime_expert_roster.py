from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.main import create_app
from app.models import (
    Base,
    CloudHermesProfile,
    HermesRuntimeBinding,
    Organization,
    RuntimeAgentProfile,
    User,
)
from app.services.runtime_expert_roster import (
    RuntimeExpertRosterError,
    RuntimeExpertRosterService,
)
from app.services.runtime_workspace import RuntimeApiKeyCodec


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


async def _database() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, name="Org", code="org-1", status="active"),
                User(id=1, org_id=1, name="User", status="active"),
                CloudHermesProfile(
                    id=10,
                    org_id=1,
                    user_id=1,
                    profile_type="default_chat_agent",
                    name="默认数字人",
                    status="active",
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                HermesRuntimeBinding(
                    id=20,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=10,
                    runtime_id="rt-10",
                    image="runtime:test",
                    k8s_namespace="runtimes",
                    knative_service="runtime-user",
                    endpoint_base_url="http://runtime.invalid",
                    endpoint_host_header="runtime.invalid",
                    hermes_home_sub_path="home",
                    workspace_sub_path="workspace",
                    hermes_home_mount_path="/opt/data",
                    workspace_mount_path="/data/workspace",
                    api_key_encrypted=RuntimeApiKeyCodec().encrypt("secret"),
                    status="ready",
                ),
                RuntimeAgentProfile(
                    id=30,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=10,
                    hermes_profile_name="default",
                    profile_kind="default",
                    display_name="自己",
                    default_workspace_key=10,
                    config_generation=1,
                    applied_config_generation=1,
                    status="active",
                ),
                RuntimeAgentProfile(
                    id=31,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=10,
                    hermes_profile_name="expert-31",
                    profile_kind="expert",
                    display_name="市场洞察专家",
                    description="擅长市场洞察",
                    capability_config={
                        "skill_ids": [],
                        "mcp_ids": [],
                        "knowledge_base_ids": [],
                        "plugin_ids": ["enterprise-knowledge"],
                    },
                    config_generation=1,
                    applied_config_generation=1,
                    status="active",
                ),
            ]
        )
        await db.commit()
    return engine, sessions


def test_runtime_expert_roster_route_requires_runtime_auth() -> None:
    operation = create_app().openapi()["paths"][
        "/api/v1/runtime-experts/{runtime_workspace_id}/roster"
    ]["get"]

    assert "runtime-experts" in operation["tags"]
    assert any(
        parameter["name"] == "authorization"
        and parameter["in"] == "header"
        for parameter in operation["parameters"]
    )


@pytest.mark.asyncio
async def test_default_profile_lists_only_ready_owned_experts() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            roster = await RuntimeExpertRosterService(db).roster(
                runtime_workspace_id=10,
                runtime_agent_profile_id=30,
                runtime_token="secret",
            )

            assert len(roster.experts) == 1
            assert roster.experts[0].display_name == "市场洞察专家"
            assert roster.experts[0].assignee == "expert-31"
            assert roster.experts[0].capabilities.plugins == [
                "enterprise-knowledge"
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ready_expert_profile_can_list_roster_but_wrong_token_fails_closed() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            service = RuntimeExpertRosterService(db)
            roster = await service.roster(
                runtime_workspace_id=10,
                runtime_agent_profile_id=31,
                runtime_token="secret",
            )
            assert [item.display_name for item in roster.experts] == ["市场洞察专家"]
            with pytest.raises(RuntimeExpertRosterError, match="unauthorized"):
                await service.roster(
                    runtime_workspace_id=10,
                    runtime_agent_profile_id=30,
                    runtime_token="wrong",
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retired_generation_drift_does_not_hide_applied_expert() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            expert = await db.get(RuntimeAgentProfile, 31)
            assert expert is not None
            expert.config_generation = 2
            await db.commit()

            service = RuntimeExpertRosterService(db)
            roster = await service.roster(
                runtime_workspace_id=10,
                runtime_agent_profile_id=30,
                runtime_token="secret",
            )
            assert [item.display_name for item in roster.experts] == ["市场洞察专家"]
            authenticated = await service.roster(
                runtime_workspace_id=10,
                runtime_agent_profile_id=31,
                runtime_token="secret",
            )
            assert [item.display_name for item in authenticated.experts] == [
                "市场洞察专家"
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_desired_revision_drift_expert_keeps_old_applied_roster_identity() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            expert = await db.get(RuntimeAgentProfile, 31)
            assert expert is not None
            expert.desired_revision = 2
            expert.applied_revision = 1
            await db.commit()

            service = RuntimeExpertRosterService(db)
            roster = await service.roster(
                runtime_workspace_id=10,
                runtime_agent_profile_id=30,
                runtime_token="secret",
            )
            assert [item.assignee for item in roster.experts] == ["expert-31"]
            authenticated = await service.roster(
                runtime_workspace_id=10,
                runtime_agent_profile_id=31,
                runtime_token="secret",
            )
            assert [item.assignee for item in authenticated.experts] == [
                "expert-31"
            ]
    finally:
        await engine.dispose()
