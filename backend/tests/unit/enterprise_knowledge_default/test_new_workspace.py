from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.models import CloudHermesProfile
from app.repositories.runtime import RuntimeWorkspaceRepository
from app.services.enterprise_knowledge_settings import enterprise_knowledge_enabled
from app.services.runtime_workspace import SqlAlchemyRuntimeWorkspaceStore


@pytest.mark.asyncio
async def test_new_personal_workspace_enables_enterprise_knowledge_by_default() -> None:
    profile = CloudHermesProfile(
        id=17,
        org_id=1,
        user_id=2,
        profile_type="default_chat_agent",
        name="默认数字人",
        status="active",
    )

    class ExecuteResult:
        def scalars(self):
            return self

        def first(self):
            return profile

    class Session:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            if len(self.statements) == 1:
                return None
            return ExecuteResult()

        async def commit(self):
            return None

    session = Session()
    store = SqlAlchemyRuntimeWorkspaceStore(
        session,
        namespace="test",
        hermes_home_mount_path="/opt/data",
        workspace_mount_path="/data/workspace",
        ingress_base_url="http://runtime.invalid",
    )

    async def record_from_profile(_profile):
        return SimpleNamespace(runtime_workspace_id=int(_profile.id))

    store._record_from_profile = record_from_profile  # type: ignore[method-assign]
    record = await store.create_default_profile(
        org_id=1,
        user_id=2,
        name="默认数字人",
    )
    insert_parameters = session.statements[0].compile().params

    assert record.runtime_workspace_id == 17
    assert enterprise_knowledge_enabled(insert_parameters["profile_config"]) is True
    assert insert_parameters["profile_config"]["knowledge"]["enterprise"] == {
        "enabled": True,
        "mode": "full",
    }


def test_repository_default_profile_statement_preserves_enterprise_default() -> None:
    statement = RuntimeWorkspaceRepository.create_default_profile_statement(
        org_id=1,
        user_id=2,
    )
    profile_config = statement.compile().params["profile_config"]

    assert profile_config["default_mcp_bootstrap"] == {"status": "pending"}
    assert profile_config["knowledge"]["enterprise"] == {
        "enabled": True,
        "mode": "full",
    }
