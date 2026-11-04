from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.main import app
from app.services.runtime_personal_knowledge import (
    RuntimePersonalKnowledgeError,
    RuntimePersonalKnowledgeIdentity,
    SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore,
)


class FakeCodec:
    def decrypt(self, _value: str) -> str:
        return "runtime-token"


class FakeAcl:
    async def effective_role(self, **_kwargs) -> str:
        return "viewer"

    async def effective_roles(self, **_kwargs) -> dict[int, str]:
        return {9: "retrieve"}


class FakeResult:
    def __init__(
        self,
        *,
        row: tuple | None = None,
        scalars: list[object] | None = None,
    ) -> None:
        self.row = row
        self.scalar_values = scalars or []

    def first(self):
        return self.row

    def scalars(self):
        return iter(self.scalar_values)


class FakeSession:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.commits = 0

    async def execute(self, _statement):
        return self.results.pop(0)

    async def commit(self) -> None:
        self.commits += 1


def _auth_row(
    *,
    subject_type: str = "user",
    subject_user_id: int | None = 8,
    turn_id: str | None = None,
    channel: str = "studio",
    dynamic: bool = False,
) -> tuple:
    binding = SimpleNamespace(api_key_encrypted="encrypted")
    agent = SimpleNamespace(
        id=19,
        org_id=1,
        created_by_user_id=8,
        config={
            "knowledge_bindings": (
                []
                if dynamic
                else [
                    {
                        "knowledge_base_id": 9,
                        "access_mode": "delegated_user",
                    }
                ]
            ),
            "delegated_personal_knowledge_enabled": dynamic,
        },
    )
    run = SimpleNamespace(
        turn_id=turn_id,
        subject_type=subject_type,
        subject_user_id=subject_user_id,
        channel=channel,
    )
    user = (
        SimpleNamespace(id=8, org_id=1, status="active")
        if subject_user_id is not None
        else None
    )
    return binding, agent, run, user


def test_application_agent_personal_knowledge_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    base = (
        "/api/v1/application-agent-runtime-personal-knowledge/"
        "{application_agent_id}"
    )
    assert f"{base}/state" in paths
    assert f"{base}/search" in paths
    assert f"{base}/wiki-search" in paths
    assert f"{base}/hybrid-search" in paths
    assert f"{base}/wiki-read-page" in paths


@pytest.mark.asyncio
async def test_application_agent_runtime_auth_uses_active_run_user_identity() -> None:
    session = FakeSession([
        FakeResult(row=_auth_row()),
        FakeResult(scalars=[9]),
    ])
    store = SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="studio-19-8-session",
        codec=FakeCodec(),  # type: ignore[arg-type]
        acl=FakeAcl(),  # type: ignore[arg-type]
    )

    identity = await store.authenticate(19, None, "runtime-token")

    assert identity == RuntimePersonalKnowledgeIdentity(
        org_id=1,
        user_id=8,
        runtime_workspace_id=19,
        runtime_agent_profile_id=0,
        plugin_active=True,
        allowed_knowledge_base_ids=frozenset({9}),
        selection_owner_user_id=8,
    )


@pytest.mark.asyncio
async def test_application_agent_runtime_auth_rejects_missing_active_run() -> None:
    session = FakeSession([FakeResult(row=None)])
    store = SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="wrong-or-finished-session",
        codec=FakeCodec(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await store.authenticate(19, None, "runtime-token")

    assert exc.value.code == "application_agent_active_user_run_required"
    assert exc.value.status_code == 403
    assert session.commits == 0


@pytest.mark.asyncio
async def test_application_agent_runtime_auth_rejects_wrong_runtime_token(
) -> None:
    session = FakeSession([FakeResult(row=_auth_row())])
    store = SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="studio-19-8-session",
        codec=FakeCodec(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimePersonalKnowledgeError) as exc:
        await store.authenticate(19, None, "wrong-token")

    assert exc.value.code == "runtime_personal_knowledge_unauthorized"
    assert exc.value.status_code == 401
    assert session.commits == 0


@pytest.mark.asyncio
async def test_application_agent_runtime_denies_service_personal_knowledge() -> None:
    session = FakeSession(
        [
            FakeResult(
                row=_auth_row(
                    subject_type="service",
                    subject_user_id=None,
                    channel="openapi",
                )
            ),
            FakeResult(scalars=[9]),
        ]
    )
    store = SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="app-19-session",
        codec=FakeCodec(),  # type: ignore[arg-type]
    )

    identity = await store.authenticate(19, None, "runtime-token")

    assert identity.user_id == 0
    assert identity.plugin_active is False
    assert identity.allowed_knowledge_base_ids == frozenset()
    assert identity.search_denial_code == "knowledge_user_delegation_required"


@pytest.mark.asyncio
async def test_application_agent_runtime_uses_active_run_not_transient_turn() -> None:
    session = FakeSession(
        [
            FakeResult(row=_auth_row(turn_id="turn-expected")),
            FakeResult(scalars=[9]),
        ]
    )
    store = SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="studio-19-8-session",
        turn_id="turn-other",
        codec=FakeCodec(),  # type: ignore[arg-type]
    )

    identity = await store.authenticate(19, None, "runtime-token")

    assert identity.user_id == 8
    assert session.commits == 0


@pytest.mark.asyncio
async def test_application_agent_ignores_legacy_personal_delegation() -> None:
    session = FakeSession(
        [
            FakeResult(row=_auth_row(dynamic=True, channel="wecom")),
            FakeResult(scalars=[]),
        ]
    )
    store = SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="wecom-19-user-8",
        codec=FakeCodec(),  # type: ignore[arg-type]
        acl=FakeAcl(),  # type: ignore[arg-type]
    )

    identity = await store.authenticate(19, None, "runtime-token")

    assert identity.plugin_active is False
    assert identity.allowed_knowledge_base_ids == frozenset()


@pytest.mark.asyncio
async def test_application_agent_never_falls_back_to_legacy_binding() -> None:
    session = FakeSession(
        [
            FakeResult(row=_auth_row(channel="wecom")),
            FakeResult(scalars=[]),
        ]
    )
    store = SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="wecom-19-user-8",
        codec=FakeCodec(),  # type: ignore[arg-type]
        acl=FakeAcl(),  # type: ignore[arg-type]
    )

    identity = await store.authenticate(19, None, "runtime-token")

    assert identity.plugin_active is False
    assert identity.allowed_knowledge_base_ids == frozenset()


@pytest.mark.asyncio
async def test_application_agent_effective_knowledge_stays_within_selection() -> None:
    knowledge_base = SimpleNamespace(
        id=9,
        name="我的知识库",
        description="个人资料",
        dataset_id="dataset-9",
        updated_at=datetime(2026, 7, 29),
    )
    file = SimpleNamespace(
        id=91,
        filename="handbook.pdf",
        oss_url="https://files.example.test/handbook.pdf",
        oss_key="personal/handbook.pdf",
    )
    session = FakeSession(
        [
            FakeResult(scalars=[knowledge_base]),
            FakeResult(scalars=[file]),
        ]
    )
    store = SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="studio-19-8-session",
        codec=FakeCodec(),  # type: ignore[arg-type]
        acl=FakeAcl(),  # type: ignore[arg-type]
    )

    effective = await store.effective(
        RuntimePersonalKnowledgeIdentity(
            org_id=1,
            user_id=8,
            runtime_workspace_id=19,
            runtime_agent_profile_id=0,
            plugin_active=True,
            allowed_knowledge_base_ids=frozenset({9}),
            selection_owner_user_id=8,
        )
    )

    assert [item.knowledge_base_id for item in effective] == [9]
    assert effective[0].files[0].filename == "handbook.pdf"


@pytest.mark.asyncio
async def test_only_explicitly_selected_shared_library_is_loaded() -> None:
    knowledge_bases = [
        SimpleNamespace(
            id=9,
            name="我的知识库",
            description="个人资料",
            dataset_id="dataset-9",
            updated_at=datetime(2026, 7, 29),
        ),
        SimpleNamespace(
            id=10,
            name="共享知识库",
            description="同事授权",
            dataset_id="dataset-10",
            updated_at=datetime(2026, 7, 30),
        ),
    ]
    files = [
        SimpleNamespace(
            id=91,
            knowledge_base_id=9,
            filename="handbook.pdf",
            oss_url="https://files.example.test/handbook.pdf",
            oss_key="personal/handbook.pdf",
        ),
        SimpleNamespace(
            id=101,
            knowledge_base_id=10,
            filename="shared.pdf",
            oss_url="https://files.example.test/shared.pdf",
            oss_key="personal/shared.pdf",
        ),
    ]
    session = FakeSession([
        FakeResult(scalars=knowledge_bases),
        FakeResult(scalars=files),
    ])
    store = SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="wecom-19-user-8",
        codec=FakeCodec(),  # type: ignore[arg-type]
        acl=FakeAcl(),  # type: ignore[arg-type]
    )

    effective = await store.effective(
        RuntimePersonalKnowledgeIdentity(
            org_id=1,
            user_id=8,
            runtime_workspace_id=19,
            runtime_agent_profile_id=0,
            plugin_active=True,
            allowed_knowledge_base_ids=frozenset({9}),
            selection_owner_user_id=8,
        )
    )

    assert [item.knowledge_base_id for item in effective] == [9]
