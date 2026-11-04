from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

from platform_supervisor.execution import PrepareIdentity
from platform_supervisor.hermes.execution_adapter import (
    HermesExecutionAdapter,
    HermesSessionPrepareError,
)
from platform_supervisor.hermes.protocol import HermesProtocolError
from platform_supervisor.hermes.events import PlatformStreamEvent


class FakeModelRouter:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.calls = calls

    async def prepare(self, identity: PrepareIdentity) -> None:
        self.calls.append(("route", identity.session_id))

    def hermes_provider(self, identity: PrepareIdentity) -> str:
        del identity
        return "custom:runtime-route"

    async def close_session(self, identity: PrepareIdentity) -> None:
        self.calls.append(("route_close", identity.session_id))


class FakeProtocol:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.calls = calls

    async def create_session(self, _endpoint, **kwargs) -> dict[str, object]:
        self.calls.append(("create", dict(kwargs)))
        return {"session": {"id": kwargs["runtime_session_id"]}}

    async def refresh_session_profile(self, _endpoint, **kwargs) -> None:
        self.calls.append(("refresh_session_profile", dict(kwargs)))

    async def release_live_session(self, _endpoint, **kwargs) -> object:
        self.calls.append(("live_release", dict(kwargs)))
        return object()

    def issue_route_prepare_proof(self, release, **kwargs) -> object:
        self.calls.append(("route_proof", dict(kwargs)))
        return release

    async def prewarm_session(self, _endpoint, **kwargs) -> dict[str, object]:
        self.calls.append(("prewarm", dict(kwargs)))
        return {"session_id": kwargs["runtime_session_id"]}

    async def stream_session_chat(self, _endpoint, **kwargs):
        self.calls.append(("stream", dict(kwargs)))
        if False:
            yield None

    async def stop_run(self, _endpoint, **kwargs) -> None:
        self.calls.append(("stop", dict(kwargs)))

    async def list_profile_mcp_servers(self, _endpoint, **kwargs):
        self.calls.append(("list_profile_mcp_servers", dict(kwargs)))
        return [{"name": "scoped-docs"}]

    async def health(self, _endpoint) -> dict[str, object]:
        self.calls.append(("health", None))
        return {"status": "ok"}

    async def initialize_default_session_store(
        self, _endpoint
    ) -> dict[str, object]:
        self.calls.append(("default_session_store", None))
        return {"sessions": []}

    async def prewarm(self, _endpoint) -> dict[str, object]:
        self.calls.append(("prewarm_tools", None))
        return {"sections": [], "total": 0}

    async def list_sessions(self, _endpoint, **kwargs) -> dict[str, object]:
        self.calls.append(("list_sessions", dict(kwargs)))
        return {
            "sessions": [{"id": "session-1"}],
            "total": 1,
            "limit": kwargs["limit"],
            "offset": kwargs.get("offset", 0),
        }

    async def reload_mcp(self, _endpoint) -> None:
        self.calls.append(("reload_mcp", None))

    async def reload_mcp_unconditional_and_list(self, _endpoint):
        self.calls.append(("reload_mcp_registry", None))
        return [{"slug": "crm", "tools": [{"name": "search"}]}]

    async def list_mcp_registry_servers(self, _endpoint):
        self.calls.append(("list_mcp_registry", None))
        return [{"slug": "crm", "tools": [{"name": "search"}]}]


class ProfileConfigProtocol(FakeProtocol):
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        super().__init__(calls)
        self.document: dict[str, object] = {
            "model": {"default": "17"},
            "providers": {},
        }

    async def dashboard_json_request(
        self, _endpoint, method: str, path: str, *, json_body=None
    ) -> dict[str, object]:
        self.calls.append(("dashboard_json", (method, path, json_body)))
        if method == "GET":
            return {"yaml": yaml.safe_dump(self.document), "path": "redacted"}
        assert json_body is not None
        self.document = yaml.safe_load(str(json_body["yaml_text"]))
        return {"ok": True}


@pytest.mark.asyncio
async def test_execution_adapter_close_propagates_to_dashboard_stream(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []

    class ClosingProtocol(FakeProtocol):
        closed = False

        async def stream_session_chat(self, _endpoint, **kwargs):
            self.calls.append(("stream", dict(kwargs)))
            try:
                yield PlatformStreamEvent(
                    "chat.done",
                    {"run_id": "dashboard-session-1"},
                )
            finally:
                self.closed = True

    protocol = ClosingProtocol(calls)
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="application",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=protocol,
    )
    selected = PrepareIdentity(
        runtime_id="runtime-1",
        session_id="session-1",
        profile_id="default",
        model_id="7",
        config_revision="revision-1",
        admission_generation=1,
    )

    stream = adapter.stream(
        identity=selected,
        run_id="run-1",
        prompt="hello",
    )
    assert (await anext(stream)).event == "chat.done"
    assert protocol.closed is False

    await stream.aclose()

    assert protocol.closed is True


@pytest.mark.asyncio
async def test_execution_adapter_refreshes_stored_prompt_through_hermes_boundary(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=FakeProtocol(calls),
    )
    identity = PrepareIdentity(
        runtime_id="runtime-1",
        session_id="session-1",
        profile_id="expert-7",
        model_id="7",
        config_revision="revision-1",
        admission_generation=1,
    )

    await adapter.refresh_session_profile(identity)

    assert calls == [
        (
            "refresh_session_profile",
            {
                "runtime_session_id": "session-1",
                "model": "7",
                "provider": "custom:runtime-route",
                "hermes_profile_name": "expert-7",
            },
        )
    ]


@pytest.mark.asyncio
async def test_execution_adapter_delegates_profile_scoped_mcp_list(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=FakeProtocol(calls),
    )

    assert await adapter.list_profile_mcp_servers(
        profile_name="expert-7"
    ) == [{"name": "scoped-docs"}]
    assert calls == [
        ("list_profile_mcp_servers", {"profile_name": "expert-7"})
    ]


@pytest.mark.asyncio
async def test_execution_adapter_profile_config_uses_raw_dashboard_api_with_readback(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []
    protocol = ProfileConfigProtocol(calls)
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=protocol,
    )
    desired = {
        "model": {"default": "17"},
        "providers": {"runtime-test": {"name": "runtime-test"}},
    }

    assert await adapter.read_profile_config("expert-125") == {
        "model": {"default": "17"},
        "providers": {},
    }
    await adapter.write_profile_config("expert-125", desired)

    assert protocol.document == desired
    assert [call[1][0:2] for call in calls if call[0] == "dashboard_json"] == [
        ("GET", "/api/config/raw?profile=expert-125"),
        ("PUT", "/api/config/raw?profile=expert-125"),
        ("GET", "/api/config/raw?profile=expert-125"),
    ]


@pytest.mark.asyncio
async def test_execution_adapter_initializes_empty_named_profile_config(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []
    protocol = ProfileConfigProtocol(calls)
    protocol.document = None  # type: ignore[assignment]
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=protocol,
    )

    assert await adapter.read_profile_config("expert-107") == {}
    with pytest.raises(HermesProtocolError, match="root is invalid"):
        await adapter.read_profile_config("default")


@pytest.mark.asyncio
async def test_execution_adapter_mcp_reload_invalidates_supervisor_sessions(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=FakeProtocol(calls),
    )

    async def invalidate(config_revision: str) -> int:
        calls.append(("invalidate_sessions", config_revision))
        return 2

    adapter.bind_agent_generation_advancer(invalidate)

    await adapter.reload_mcp()

    assert calls == [
        ("reload_mcp", None),
        ("invalidate_sessions", "mcp-reload"),
    ]


@pytest.mark.asyncio
async def test_execution_adapter_registry_reload_advances_all_agent_generations(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=FakeProtocol(calls),
    )

    async def invalidate(config_revision: str) -> int:
        calls.append(("invalidate_sessions", config_revision))
        return 2

    adapter.bind_agent_generation_advancer(invalidate)

    result = await adapter.reload_mcp_registry_and_list()

    assert result == [{"slug": "crm", "tools": [{"name": "search"}]}]
    assert calls == [
        ("reload_mcp_registry", None),
        ("invalidate_sessions", "mcp-reload"),
    ]

    assert await adapter.list_mcp_registry_servers() == [
        {"slug": "crm", "tools": [{"name": "search"}]}
    ]
    assert calls[-1] == ("list_mcp_registry", None)


@pytest.mark.asyncio
async def test_execution_adapter_registry_reload_invalidates_agents_when_result_is_uncertain(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )

    class UncertainReloadProtocol(FakeProtocol):
        async def reload_mcp_unconditional_and_list(self, _endpoint):
            self.calls.append(("reload_mcp_registry", None))
            raise RuntimeError("post-reload evidence unavailable")

    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=UncertainReloadProtocol(calls),
    )

    async def invalidate(config_revision: str) -> int:
        calls.append(("invalidate_sessions", config_revision))
        return 1

    adapter.bind_agent_generation_advancer(invalidate)

    with pytest.raises(RuntimeError, match="evidence unavailable"):
        await adapter.reload_mcp_registry_and_list()

    assert calls == [
        ("reload_mcp_registry", None),
        ("invalidate_sessions", "mcp-reload"),
    ]


@pytest.mark.asyncio
async def test_execution_adapter_delegates_paginated_session_list(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=FakeProtocol(calls),
    )

    result = await adapter.list_sessions(limit=205)

    assert result["sessions"] == [{"id": "session-1"}]
    assert calls == [("list_sessions", {"limit": 205, "offset": 0})]


@pytest.mark.asyncio
async def test_health_initializes_default_store_before_health_and_prewarm(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=FakeProtocol(calls),
    )

    await adapter.health()

    assert calls == [
        ("default_session_store", None),
        ("health", None),
        ("prewarm_tools", None),
    ]


@pytest.mark.asyncio
async def test_health_fails_closed_when_default_session_store_is_unavailable(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    protocol = FakeProtocol(calls)

    async def unavailable_default_session_store(_endpoint) -> dict[str, object]:
        protocol.calls.append(("default_session_store", None))
        raise HermesProtocolError("default session store is unavailable")

    protocol.initialize_default_session_store = unavailable_default_session_store
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=protocol,
    )

    with pytest.raises(HermesProtocolError, match="default session store"):
        await adapter.health()

    assert calls == [("default_session_store", None)]


@pytest.mark.asyncio
async def test_prepare_idempotently_materializes_session_before_prewarm(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=14,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="dashboard-session-token",
        model_router=FakeModelRouter(calls),
        protocol=FakeProtocol(calls),
    )
    identity = PrepareIdentity(
        runtime_id="runtime-1",
        session_id="session-new",
        profile_id="default",
        model_id="7",
        config_revision="revision-18",
        admission_generation=11,
    )

    await adapter.prepare(identity)

    assert [name for name, _payload in calls] == [
        "live_release",
        "route",
        "create",
        "route_proof",
        "prewarm",
    ]
    release = calls[0][1]
    assert isinstance(release, dict)
    assert release == {"runtime_session_id": "session-new"}
    create = calls[2][1]
    assert isinstance(create, dict)
    assert create == {
        "runtime_session_id": "session-new",
        "model": "7",
        "provider": "custom:runtime-route",
        "hermes_profile_name": "default",
    }
    prewarm = calls[4][1]
    assert isinstance(prewarm, dict)
    assert prewarm["runtime_session_id"] == "session-new"
    assert prewarm["hermes_profile_name"] == "default"
    assert prewarm["expected_provider"] == "custom:runtime-route"
    hot_identity = prewarm["hot_channel_identity"]
    assert hot_identity.independent_session is True
    assert hot_identity.reuse_across_turns is True
    assert hot_identity.max_coordination_slots == 14

    expert_identity = PrepareIdentity(
        runtime_id="runtime-1",
        session_id="session-expert",
        profile_id="expert-7",
        model_id="7",
        config_revision="revision-18",
        admission_generation=11,
    )
    assert adapter.hot_identity(expert_identity).reuse_across_turns is False

    settings.RUNTIME_MODE = "application"
    assert adapter.hot_identity(identity).reuse_across_turns is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_id", "expected_eager_build"),
    [
        ("default", False),
        ("expert-7", True),
        ("application-43", False),
    ],
)
async def test_prepare_selects_safe_resume_build_policy_for_session_db_ownership(
    tmp_path,
    profile_id: str,
    expected_eager_build: bool,
) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="application" if profile_id.startswith("application-") else "personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="dashboard-session-token",
        model_router=FakeModelRouter(calls),
        protocol=FakeProtocol(calls),
    )

    await adapter.prepare(
        PrepareIdentity(
            runtime_id="runtime-1",
            session_id=f"session-{profile_id}",
            profile_id=profile_id,
            model_id="7",
            config_revision="revision-18",
            admission_generation=11,
        )
    )

    prewarm = next(payload for name, payload in calls if name == "prewarm")
    assert isinstance(prewarm, dict)
    assert prewarm["eager_build"] is expected_eager_build


@pytest.mark.asyncio
async def test_prepare_hot_identity_keeps_platform_id_and_gateway_model_separate(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-10001",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="dashboard-session-token",
        model_router=FakeModelRouter(calls),
        protocol=FakeProtocol(calls),
    )
    identity = PrepareIdentity(
        runtime_id="runtime-10001",
        session_id="session-model-identity",
        profile_id="default",
        model_id="19",
        upstream_model_id="deepseek-v4-flash",
        config_revision="revision-19",
        admission_generation=11,
    )

    await adapter.prepare(identity)

    prewarm = calls[4][1]
    assert isinstance(prewarm, dict)
    hot_identity = prewarm["hot_channel_identity"]
    assert hot_identity.effective_model_entry_id == 19
    assert hot_identity.gateway_model_id == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_prepare_materializes_hermes_session_with_upstream_model_name(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1", HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119, WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900, max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(settings, dashboard_session_token="token", model_router=FakeModelRouter(calls), protocol=FakeProtocol(calls))
    await adapter.prepare(PrepareIdentity(
        runtime_id="runtime-1", session_id="session-model-name", profile_id="default",
        model_id="19", upstream_model_id="deepseek-v4-flash", config_revision="revision-19",
        admission_generation=11,
    ))
    assert calls[2][1]["model"] == "deepseek-v4-flash"
    assert calls[2][1]["provider"] == "custom:runtime-route"


@pytest.mark.asyncio
async def test_prepare_rebuilds_same_session_when_model_route_changes(
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []

    class RouteChangingModelRouter(FakeModelRouter):
        async def prepare(self, identity: PrepareIdentity) -> None:
            self.calls.append(("route", identity.gateway_model_id))

        def hermes_provider(self, identity: PrepareIdentity) -> str:
            return f"custom:runtime-{identity.gateway_model_id}-route"

    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="dashboard-session-token",
        model_router=RouteChangingModelRouter(calls),
        protocol=FakeProtocol(calls),
    )

    for model_id, revision in (("model-a", "revision-a"), ("model-b", "revision-b")):
        await adapter.prepare(
            PrepareIdentity(
                runtime_id="runtime-1",
                session_id="session-existing",
                profile_id="default",
                model_id=model_id,
                config_revision=revision,
                admission_generation=11,
            )
        )

    assert [name for name, _payload in calls] == [
        "live_release",
        "route",
        "create",
        "route_proof",
        "prewarm",
    ] * 2
    second_create = calls[7][1]
    assert isinstance(second_create, dict)
    assert second_create["runtime_session_id"] == "session-existing"
    assert second_create["model"] == "model-b"
    assert second_create["provider"] == "custom:runtime-model-b-route"
    second_proof = calls[8][1]
    assert isinstance(second_proof, dict)
    assert second_proof["expected_model"] == "model-b"
    assert second_proof["expected_provider"] == "custom:runtime-model-b-route"
    second_prewarm = calls[9][1]
    assert isinstance(second_prewarm, dict)
    assert second_prewarm["expected_provider"] == "custom:runtime-model-b-route"


@pytest.mark.asyncio
async def test_prepare_stops_before_route_when_live_execution_release_fails(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    model_router = FakeModelRouter(calls)
    protocol = FakeProtocol(calls)

    async def fail_release(*_args, **_kwargs) -> object:
        calls.append(("live_release", "failed"))
        raise HermesProtocolError("live session close failed")

    protocol.release_live_session = fail_release
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="dashboard-session-token",
        model_router=model_router,
        protocol=protocol,
    )

    with pytest.raises(HermesSessionPrepareError) as captured:
        await adapter.prepare(
            PrepareIdentity(
                runtime_id="runtime-1",
                session_id="session-release-failed",
                profile_id="default",
                model_id="7",
                config_revision="revision-18",
                admission_generation=11,
            )
        )

    assert captured.value.phase == "live_execution_release"
    assert calls == [("live_release", "failed")]


@pytest.mark.asyncio
async def test_close_session_releases_live_execution_before_route_cleanup(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="dashboard-session-token",
        model_router=FakeModelRouter(calls),
        protocol=FakeProtocol(calls),
    )

    async def invalidate(*_args, **_kwargs) -> None:
        calls.append(("hot_invalidate", None))

    adapter.hot_channels.invalidate_session = invalidate
    identity = PrepareIdentity(
        runtime_id="runtime-1",
        session_id="session-close",
        profile_id="default",
        model_id="7",
        config_revision="revision-18",
        admission_generation=11,
    )

    await adapter.close_session(identity)

    assert [name for name, _payload in calls] == [
        "hot_invalidate",
        "live_release",
        "route_close",
    ]


@pytest.mark.asyncio
async def test_cancel_targets_live_dashboard_session_id_when_available(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=FakeProtocol(calls),
    )

    await adapter.cancel(
        PrepareIdentity(
            runtime_id="runtime-1",
            session_id="persistent-session-id",
            profile_id="default",
            model_id="7",
            config_revision="revision-18",
            admission_generation=11,
        ),
        runtime_provider_run_id="live-dashboard-session-id",
    )

    assert calls == [
        ("stop", {"runtime_provider_run_id": "live-dashboard-session-id"})
    ]


@pytest.mark.asyncio
async def test_cancel_never_falls_back_to_platform_session_id(tmp_path) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=14,
        RUNTIME_MODE="personal",
    )
    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="token",
        model_router=FakeModelRouter(calls),
        protocol=FakeProtocol(calls),
    )

    with pytest.raises(HermesProtocolError, match="live session id"):
        await adapter.cancel(
            PrepareIdentity(
                runtime_id="runtime-1",
                session_id="persistent-session-id",
                profile_id="default",
                model_id="7",
                config_revision="revision-18",
                admission_generation=11,
            )
        )

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "failed_call"),
    [
        ("model_route", "route"),
        ("session_import", "create"),
        ("dashboard_prewarm", "prewarm"),
    ],
)
async def test_prepare_wraps_dashboard_errors_with_the_failing_phase(
    tmp_path,
    phase: str,
    failed_call: str,
) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(
        RUNTIME_ID="runtime-1",
        HERMES_DASHBOARD_HOST="127.0.0.1",
        HERMES_DASHBOARD_PORT=9119,
        WORKSPACE_DIR=tmp_path,
        APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS=900,
        max_resident_sessions=1,
        RUNTIME_MODE="personal",
    )
    model_router = FakeModelRouter(calls)
    protocol = FakeProtocol(calls)

    async def fail(*_args, **_kwargs) -> None:
        raise HermesProtocolError(
            "sensitive upstream response body",
            status_code=502,
            path="/api/sessions?session_grant=secret",
            rpc_code=5071,
            rpc_method="session.resume",
            rpc_data={"retry_after_ms": 250},
        )

    if failed_call == "route":
        model_router.prepare = fail
    elif failed_call == "create":
        protocol.create_session = fail
    else:
        protocol.prewarm_session = fail

    adapter = HermesExecutionAdapter(
        settings,
        dashboard_session_token="dashboard-session-token",
        model_router=model_router,
        protocol=protocol,
    )
    with pytest.raises(HermesSessionPrepareError) as captured:
        await adapter.prepare(
            PrepareIdentity(
                runtime_id="runtime-1",
                session_id="session-failed-prepare",
                profile_id="default",
                model_id="7",
                config_revision="revision-18",
                admission_generation=11,
            )
        )

    assert captured.value.phase == phase
    assert captured.value.status_code == 502
    assert captured.value.path == "/api/sessions?session_grant=secret"
    assert captured.value.rpc_code == 5071
    assert captured.value.rpc_method == "session.resume"
    assert captured.value.rpc_data == {"retry_after_ms": 250}
    assert "sensitive upstream response body" not in str(captured.value)
