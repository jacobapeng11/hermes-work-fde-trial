from __future__ import annotations

import asyncio
import json
import re
import types
from contextlib import asynccontextmanager
from dataclasses import replace
from io import BytesIO
from unittest.mock import Mock

import pytest

import platform_supervisor.hermes.dashboard_protocol as dashboard_protocol
from platform_supervisor.hermes.dashboard_protocol import (
    DashboardHttpSessionPool,
    HermesDashboardProtocolAdapter,
    _hot_channel_expected_model,
)
from platform_supervisor.hermes.dashboard_hot_channel import (
    ActiveRunControlPeerRegistry,
    DashboardChannelBusy,
    DashboardHotChannelIdentity,
    DashboardHotChannelManager,
    DashboardPromptSubmitTimeout,
    DashboardRedirectDisposition,
    DashboardRpcPeer,
)
from platform_supervisor.hermes.protocol import HermesProtocolError
from platform_supervisor.hermes.schemas import RuntimeEndpoint


def _endpoint() -> RuntimeEndpoint:
    return RuntimeEndpoint(
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        endpoint_base_url="http://127.0.0.1:31080",
        endpoint_host_header="hermes-runtime-101.hermes-runtimes.hermes.local",
        api_key="runtime-api-key",
    )


class FakeJsonResponse:
    def __init__(self, payload: dict, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class SequencedDashboardPool:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.calls: list[tuple[str, str]] = []

    async def request(self, _endpoint, method, path, **_kwargs):
        self.calls.append((method, path))
        if not self.payloads:
            raise AssertionError("unexpected Dashboard HTTP request")
        return FakeJsonResponse(self.payloads.pop(0))


class FakeWebSocket:
    def __init__(self, frames: list[dict]) -> None:
        self.frames = [json.dumps(frame) for frame in frames]
        self.sent: list[dict] = []
        self._response_count = 0

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        if not self.frames:
            raise AssertionError("dashboard adapter read past terminal frame")
        frame = json.loads(self.frames.pop(0))
        if frame.get("$wait"):
            await asyncio.sleep(float(frame["$wait"]))
            return await self.recv()
        if frame.get("$disconnect"):
            raise ConnectionError("simulated dashboard disconnect")
        if frame.get("$response"):
            while len(self.sent) <= self._response_count:
                await asyncio.sleep(0)
            request = self.sent[self._response_count]
            self._response_count += 1
            result = dict(frame["$response"])
            if (
                request.get("method") == "session.resume"
                and request.get("params", {}).get("omit_messages") is True
            ):
                result.setdefault("messages_omitted", True)
                result.setdefault("messages", [])
                result.setdefault("message_count", 0)
            frame = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": result,
            }
        elif frame.get("$error"):
            while len(self.sent) <= self._response_count:
                await asyncio.sleep(0)
            request = self.sent[self._response_count]
            self._response_count += 1
            error = frame["$error"]
            if not isinstance(error, dict):
                error = {"code": 5032, "message": error}
            frame = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": error,
            }
        return json.dumps(frame)


class RecordingHotChannels:
    def __init__(self, *, busy: bool = False) -> None:
        self.lock = asyncio.Lock()
        self.busy = busy
        self.idle_checks: list[int] = []

    def workspace_operation(self, runtime_workspace_id: int) -> asyncio.Lock:
        assert runtime_workspace_id == 101
        return self.lock

    async def require_workspace_idle(self, runtime_workspace_id: int) -> None:
        self.idle_checks.append(runtime_workspace_id)
        if self.busy:
            raise DashboardChannelBusy(
                "runtime dashboard workspace has an active run"
            )


class AutoReplyWebSocket:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        request = json.loads(payload)
        self.sent.append(request)
        method = request["method"]
        if method == "session.resume":
            result = {
                "session_id": "rs-101-a",
                "info": {"model": "6"},
            }
            if request["params"].get("omit_messages") is True:
                result.update(
                    messages_omitted=True,
                    messages=[],
                    message_count=0,
                )
        elif method == "config.set":
            result = {
                "key": "model",
                "value": str(request["params"]["value"]).split(" ", 1)[0],
                "confirm_required": False,
                "scope": "session",
            }
        elif method == "shell.exec":
            result = {"code": 0, "stdout": ""}
        else:
            result = {}
        if method == "prompt.submit":
            result = {"status": "streaming"}
        await self.frames.put(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
        )
        if method == "prompt.submit":
            await self.frames.put(
                json.dumps(_event("message.start", "rs-101-a", {}))
            )
            await self.frames.put(
                json.dumps(_event("message.delta", "rs-101-a", {"text": "ok"}))
            )
            await self.frames.put(
                json.dumps(
                    _event(
                        "message.complete",
                        "rs-101-a",
                        {"text": "ok", "status": "complete", "usage": {}},
                    )
                )
            )

    async def recv(self) -> str:
        value = await self.frames.get()
        if isinstance(value, BaseException):
            raise value
        return value


class RouteAwareAutoReplyWebSocket(AutoReplyWebSocket):
    def __init__(self, provider: str) -> None:
        super().__init__()
        self.provider = provider

    async def send(self, payload: str) -> None:
        request = json.loads(payload)
        if request["method"] != "session.resume":
            await super().send(payload)
            return
        self.sent.append(request)
        await self.frames.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "session_id": "rs-101-a",
                        "info": {"model": "6", "provider": self.provider},
                        **(
                            {
                                "messages_omitted": True,
                                "messages": [],
                                "message_count": 0,
                            }
                            if request["params"].get("omit_messages") is True
                            else {}
                        ),
                    },
                }
            )
        )


class HotPrepareWebSocket:
    def __init__(self, resume_result: dict) -> None:
        self.resume_result = resume_result
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        request = json.loads(payload)
        self.sent.append(request)
        method = request["method"]
        if method == "session.resume":
            result = dict(self.resume_result)
            if request["params"].get("omit_messages") is True:
                result.setdefault("messages_omitted", True)
                result.setdefault("messages", [])
                result.setdefault("message_count", 0)
        elif method == "config.set":
            result = {
                "key": "model",
                "value": str(request["params"]["value"]).split(" ", 1)[0],
                "confirm_required": False,
                "scope": "session",
            }
        elif method == "shell.exec":
            result = {"code": 0, "stdout": ""}
        else:
            result = {}
        await self.frames.put(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
        )

    async def recv(self) -> str:
        return await self.frames.get()


class StallOnMethodWebSocket:
    def __init__(self, stalled_method: str) -> None:
        self.stalled_method = stalled_method
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        request = json.loads(payload)
        self.sent.append(request)
        method = request["method"]
        if method == self.stalled_method:
            return
        if method == "shell.exec":
            result = {"code": 0, "stdout": ""}
        elif method == "session.resume":
            result = {
                "session_id": "rs-101-a",
                "info": {
                    "cwd": "/data/workspace/sessions/rs-101-a",
                    "model": "6",
                },
            }
            if request["params"].get("omit_messages") is True:
                result.update(
                    messages_omitted=True,
                    messages=[],
                    message_count=0,
                )
        else:
            result = {}
        await self.frames.put(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
        )

    async def recv(self) -> str:
        return await self.frames.get()


class NonMatchingEventWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        await asyncio.sleep(0.01)
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "session.info",
                    "session_id": "another-session",
                    "payload": {},
                },
            }
        )


class DisconnectOnPromptWebSocket(AutoReplyWebSocket):
    async def send(self, payload: str) -> None:
        request = json.loads(payload)
        if request["method"] != "prompt.submit":
            await super().send(payload)
            return
        self.sent.append(request)
        await self.frames.put(ConnectionError("simulated dashboard disconnect"))


class ControlStreamWebSocket:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []
        self.recv_active = 0
        self.recv_concurrency_max = 0

    async def send(self, payload: str) -> None:
        request = json.loads(payload)
        self.sent.append(request)
        method = request["method"]
        if method == "session.resume":
            result = {
                "session_id": "rs-101-a",
                "info": {"model": "6"},
            }
            if request["params"].get("omit_messages") is True:
                result.update(
                    messages_omitted=True,
                    messages=[],
                    message_count=0,
                )
        elif method == "prompt.submit":
            result = {"status": "streaming"}
        elif method == "session.redirect":
            result = {"status": "rejected"}
        else:
            result = {}
        await self.frames.put(
            json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": result}
            )
        )
    async def recv(self) -> str:
        self.recv_active += 1
        self.recv_concurrency_max = max(
            self.recv_concurrency_max,
            self.recv_active,
        )
        try:
            return await self.frames.get()
        finally:
            self.recv_active -= 1


def _event(event_type: str, session_id: str, payload: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": event_type, "session_id": session_id, "payload": payload},
    }


def _hot_identity() -> DashboardHotChannelIdentity:
    return DashboardHotChannelIdentity(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_session_db_id=5,
        runtime_session_id="rs-101-a",
        runtime_id="org-1-rt-101",
        gateway_model_id="6",
        config_generation=4,
        endpoint_digest="endpoint",
        credential_digest="credential",
        workspace_path_digest="workspace",
        effective_model_entry_id=6,
        model_policy_digest="model-policy",
    )


def test_hot_channel_uses_explicit_gateway_model_not_internal_model_id() -> None:
    assert _hot_channel_expected_model(
        replace(_hot_identity(), effective_model_entry_id=0)
    ) == "6"
    assert _hot_channel_expected_model(_hot_identity()) == "6"


@pytest.mark.asyncio
async def test_dashboard_prewarm_loads_tool_metadata() -> None:
    ws = FakeWebSocket([{"$response": {"sections": [], "total": 72}}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    result = await HermesDashboardProtocolAdapter(connector=connector).prewarm(_endpoint())

    assert result == {"sections": [], "total": 72}
    assert ws.sent == [
        {
            "jsonrpc": "2.0",
            "id": ws.sent[0]["id"],
            "method": "tools.show",
            "params": {},
        }
    ]


@pytest.mark.asyncio
async def test_dashboard_initializes_default_session_store_without_profile() -> None:
    ws = FakeWebSocket([{"$response": {"sessions": []}}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    result = await HermesDashboardProtocolAdapter(
        connector=connector
    ).initialize_default_session_store(_endpoint())

    assert result == {"sessions": []}
    assert ws.sent == [
        {
            "jsonrpc": "2.0",
            "id": ws.sent[0]["id"],
            "method": "session.list",
            "params": {"limit": 1},
        }
    ]


@pytest.mark.asyncio
async def test_dashboard_default_session_store_rejects_invalid_sessions() -> None:
    ws = FakeWebSocket([{"$response": {"sessions": "invalid"}}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    with pytest.raises(HermesProtocolError, match="default session store"):
        await HermesDashboardProtocolAdapter(
            connector=connector
        ).initialize_default_session_store(_endpoint())


@pytest.mark.asyncio
async def test_dashboard_prewarm_rejects_invalid_tool_metadata() -> None:
    ws = FakeWebSocket([{"$response": {"sections": [], "total": "72"}}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    with pytest.raises(HermesProtocolError, match="invalid tool metadata"):
        await HermesDashboardProtocolAdapter(connector=connector).prewarm(_endpoint())


@pytest.mark.asyncio
async def test_dashboard_rpc_timeout_keeps_one_deadline_while_events_arrive() -> None:
    websocket = NonMatchingEventWebSocket()
    buffered: list[dict] = []
    started = asyncio.get_running_loop().time()

    with pytest.raises(HermesProtocolError, match="RPC session.resume timed out"):
        await asyncio.wait_for(
            HermesDashboardProtocolAdapter._request(
                websocket,
                "session.resume",
                {"session_id": "rs-101-a"},
                buffered,
                timeout_seconds=0.1,
            ),
            timeout=0.5,
        )

    assert asyncio.get_running_loop().time() - started < 0.4
    assert len(buffered) >= 2


@pytest.mark.asyncio
async def test_dashboard_rpc_error_preserves_structured_fields() -> None:
    ws = FakeWebSocket(
        [
            {
                "$error": {
                    "code": 4130,
                    "message": "resume history exceeds configured limit",
                    "data": {"message_count": 20001},
                }
            }
        ]
    )

    with pytest.raises(HermesProtocolError) as captured:
        await HermesDashboardProtocolAdapter._request(
            ws,
            "session.resume",
            {"session_id": "rs-101-a"},
            [],
        )

    assert captured.value.rpc_code == 4130
    assert captured.value.rpc_method == "session.resume"
    assert captured.value.rpc_data == {"message_count": 20001}


@pytest.mark.asyncio
async def test_dashboard_session_prewarm_eagerly_builds_selected_agent() -> None:
    ws = FakeWebSocket([{"$response": {"session_id": "live-123", "resumed": "rs-101-a"}}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    result = await HermesDashboardProtocolAdapter(connector=connector).prewarm_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
    )

    assert result["session_id"] == "live-123"
    assert ws.sent[0]["method"] == "session.resume"
    assert ws.sent[0]["params"] == {
        "session_id": "rs-101-a",
        "cols": 96,
        "eager_build": True,
        "omit_messages": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resume_result",
    [
        {"session_id": "live-123", "messages_omitted": False, "messages": []},
        {"session_id": "live-123", "messages_omitted": True, "messages": [{}]},
        {
            "session_id": "live-123",
            "messages_omitted": True,
            "messages": [],
            "message_count": True,
        },
    ],
)
async def test_dashboard_session_prewarm_validates_omit_messages_response(
    resume_result,
) -> None:
    ws = FakeWebSocket([{"$response": resume_result}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    with pytest.raises(HermesProtocolError, match="omit_messages"):
        await HermesDashboardProtocolAdapter(connector=connector).prewarm_session(
            _endpoint(),
            runtime_session_id="rs-101-a",
        )


@pytest.mark.asyncio
async def test_dashboard_session_prewarm_rejects_missing_live_session_id() -> None:
    ws = FakeWebSocket([{"$response": {"session_id": "", "resumed": "rs-101-a"}}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    with pytest.raises(HermesProtocolError, match="invalid live session"):
        await HermesDashboardProtocolAdapter(connector=connector).prewarm_session(
            _endpoint(),
            runtime_session_id="rs-101-a",
        )


@pytest.mark.asyncio
async def test_dashboard_expert_prewarm_requires_hot_channel_identity() -> None:
    connector_entered = False

    @asynccontextmanager
    async def connector(_endpoint):
        nonlocal connector_entered
        connector_entered = True
        raise AssertionError("expert prewarm must fail before opening a connection")
        yield

    with pytest.raises(HermesProtocolError, match="requires a HotChannel identity"):
        await HermesDashboardProtocolAdapter(connector=connector).prewarm_session(
            _endpoint(),
            runtime_session_id="rs-101-a",
            hermes_profile_name="expert-7",
            runtime_agent_profile_id=7,
        )

    assert connector_entered is False


@pytest.mark.asyncio
async def test_dashboard_live_release_closes_only_exact_idle_session_key() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "sessions": [
                        {
                            "id": "live-other-working",
                            "session_key": "rs-other",
                            "status": "working",
                        },
                        {
                            "id": "live-target-id",
                            "session_key": "rs-101-a",
                            "status": "idle",
                        },
                    ]
                }
            },
            {"$response": {"closed": True}},
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    await HermesDashboardProtocolAdapter(
        connector=connector
    ).release_live_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
    )

    assert [request["method"] for request in ws.sent] == [
        "session.active_list",
        "session.close",
    ]
    assert ws.sent[1]["params"] == {"session_id": "live-target-id"}


@pytest.mark.asyncio
async def test_dashboard_live_release_allows_missing_or_already_closed_session() -> None:
    ws = FakeWebSocket([{"$response": {"sessions": []}}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    await HermesDashboardProtocolAdapter(
        connector=connector
    ).release_live_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
    )

    assert [request["method"] for request in ws.sent] == ["session.active_list"]


@pytest.mark.asyncio
async def test_dashboard_live_release_treats_closed_false_as_idempotent() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "sessions": [
                        {
                            "id": "live-target-id",
                            "session_key": "rs-101-a",
                            "status": "idle",
                        }
                    ]
                }
            },
            {"$response": {"closed": False}},
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    await HermesDashboardProtocolAdapter(
        connector=connector
    ).release_live_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
    )

    assert [request["method"] for request in ws.sent] == [
        "session.active_list",
        "session.close",
    ]


@pytest.mark.asyncio
async def test_dashboard_live_release_rejects_working_exact_session() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "sessions": [
                        {
                            "id": "live-target-id",
                            "session_key": "rs-101-a",
                            "status": "working",
                        }
                    ]
                }
            }
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    with pytest.raises(HermesProtocolError, match="still working"):
        await HermesDashboardProtocolAdapter(
            connector=connector
        ).release_live_session(
            _endpoint(),
            runtime_session_id="rs-101-a",
        )

    assert [request["method"] for request in ws.sent] == ["session.active_list"]


async def _prepare_model_hot_peer(
    websocket: HotPrepareWebSocket,
    *,
    adapter: HermesDashboardProtocolAdapter | None = None,
    hermes_profile_name: str = "default",
    runtime_agent_profile_id: int | None = None,
    expected_model: str | None,
    expected_provider: str | None = None,
    route_prepare_proof: object | None = None,
) -> None:
    peer = DashboardRpcPeer(websocket)
    try:
        await (adapter or HermesDashboardProtocolAdapter())._prepare_hot_peer(
            peer,
            runtime_session_id="rs-101-a",
            hermes_profile_name=hermes_profile_name,
            runtime_agent_profile_id=runtime_agent_profile_id,
            expected_model=expected_model,
            timeout_seconds=1,
            eager_build=False,
            expected_provider=expected_provider,
            route_prepare_proof=route_prepare_proof,
        )
    finally:
        await peer.close()


async def _fresh_route_prepare_proof(
    *,
    runtime_session_id: str = "rs-101-a",
    hermes_profile_name: str = "default",
    expected_model: str = "deepseek-v4-flash",
    expected_provider: str = "custom:runtime-10001-route",
) -> tuple[HermesDashboardProtocolAdapter, object]:
    release_ws = FakeWebSocket([{"$response": {"sessions": []}}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield release_ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    release = await adapter.release_live_session(
        _endpoint(),
        runtime_session_id=runtime_session_id,
    )
    proof = adapter.issue_route_prepare_proof(
        release,
        runtime_session_id=runtime_session_id,
        hermes_profile_name=hermes_profile_name,
        expected_model=expected_model,
        expected_provider=expected_provider,
    )
    return adapter, proof


@pytest.mark.asyncio
async def test_hot_prepare_accepts_generic_custom_only_with_fresh_route_proof() -> None:
    adapter, proof = await _fresh_route_prepare_proof()
    websocket = HotPrepareWebSocket(
        {
            "session_id": "live-runtime-route-custom",
            "info": {
                "model": "deepseek-v4-flash",
                "provider": "custom",
            },
        }
    )

    await _prepare_model_hot_peer(
        websocket,
        adapter=adapter,
        expected_model="deepseek-v4-flash",
        expected_provider="custom:runtime-10001-route",
        route_prepare_proof=proof,
    )

    assert [request["method"] for request in websocket.sent] == ["session.resume"]
    assert websocket.sent[0]["params"]["omit_messages"] is True


@pytest.mark.asyncio
async def test_hot_prepare_rejects_generic_custom_without_route_proof() -> None:
    websocket = HotPrepareWebSocket(
        {
            "session_id": "live-runtime-route-custom",
            "info": {
                "model": "deepseek-v4-flash",
                "provider": "custom",
            },
        }
    )

    with pytest.raises(HermesProtocolError, match="session route mismatch"):
        await _prepare_model_hot_peer(
            websocket,
            expected_model="deepseek-v4-flash",
            expected_provider="custom:runtime-10001-route",
        )

    assert [request["method"] for request in websocket.sent] == ["session.resume"]


@pytest.mark.asyncio
async def test_hot_prepare_rejects_stale_custom_route_without_model_switch() -> None:
    adapter, proof = await _fresh_route_prepare_proof()
    websocket = HotPrepareWebSocket(
        {
            "session_id": "live-runtime-route-stale",
            "info": {
                "model": "deepseek-v4-flash",
                "provider": "custom:stale-route",
            },
        }
    )

    with pytest.raises(HermesProtocolError, match="session route mismatch"):
        await _prepare_model_hot_peer(
            websocket,
            adapter=adapter,
            expected_model="deepseek-v4-flash",
            expected_provider="custom:runtime-10001-route",
            route_prepare_proof=proof,
        )

    assert [request["method"] for request in websocket.sent] == ["session.resume"]


@pytest.mark.asyncio
async def test_personal_hot_prepare_rejects_mismatched_model_without_model_switch() -> None:
    websocket = HotPrepareWebSocket(
        {
            "session_id": "live-personal-mismatch",
            "info": {"model": "21"},
        }
    )

    with pytest.raises(HermesProtocolError, match="session route mismatch"):
        await _prepare_model_hot_peer(websocket, expected_model="6")

    assert [request["method"] for request in websocket.sent] == ["session.resume"]


@pytest.mark.asyncio
async def test_personal_hot_prepare_skips_calibration_when_model_matches() -> None:
    websocket = HotPrepareWebSocket(
        {
            "session_id": "live-personal-match",
            "info": {"model": "6"},
        }
    )

    await _prepare_model_hot_peer(websocket, expected_model="6")

    assert [request["method"] for request in websocket.sent] == ["session.resume"]


@pytest.mark.asyncio
async def test_expert_hot_prepare_accepts_matching_profile_route_without_model_switch() -> None:
    websocket = HotPrepareWebSocket(
        {
            "session_id": "live-expert-match",
            "info": {"model": "6", "provider": "custom:platform"},
        }
    )

    await _prepare_model_hot_peer(
        websocket,
        hermes_profile_name="expert-7",
        runtime_agent_profile_id=7,
        expected_model="6",
    )

    assert [request["method"] for request in websocket.sent] == ["session.resume"]
    assert websocket.sent[0]["params"]["profile"] == "expert-7"


@pytest.mark.asyncio
async def test_hot_prepare_skips_calibration_when_model_and_route_provider_match() -> None:
    websocket = HotPrepareWebSocket(
        {
            "session_id": "live-runtime-route-match",
            "info": {
                "model": "deepseek-v4-flash",
                "provider": "custom:runtime-10001-route",
            },
        }
    )

    await _prepare_model_hot_peer(
        websocket,
        expected_model="deepseek-v4-flash",
        expected_provider="custom:runtime-10001-route",
    )

    assert [request["method"] for request in websocket.sent] == ["session.resume"]


@pytest.mark.asyncio
async def test_hot_prepare_rejects_mismatched_route_provider_without_model_switch() -> None:
    websocket = HotPrepareWebSocket(
        {
            "session_id": "live-runtime-route-provider-mismatch",
            "info": {
                "model": "deepseek-v4-flash",
                "provider": "custom:stale-route",
            },
        }
    )

    with pytest.raises(HermesProtocolError, match="session route mismatch"):
        await _prepare_model_hot_peer(
            websocket,
            expected_model="deepseek-v4-flash",
            expected_provider="custom:runtime-10001-route",
        )

    assert [request["method"] for request in websocket.sent] == ["session.resume"]


@pytest.mark.asyncio
async def test_hot_prepare_rejects_platform_model_entry_id_instead_of_gateway_model() -> None:
    websocket = HotPrepareWebSocket(
        {
            "session_id": "live-model-entry-mismatch",
            "info": {
                "model": "19",
                "provider": "custom:runtime-10001-route",
            },
        }
    )

    identity = replace(
        _hot_identity(),
        effective_model_entry_id=19,
        gateway_model_id="deepseek-v4-flash",
    )
    with pytest.raises(HermesProtocolError, match="session route mismatch"):
        await _prepare_model_hot_peer(
            websocket,
            expected_model=_hot_channel_expected_model(identity),
            expected_provider="custom:runtime-10001-route",
        )

    assert [request["method"] for request in websocket.sent] == ["session.resume"]


@pytest.mark.asyncio
async def test_expert_hot_prepare_rejects_missing_expected_model() -> None:
    websocket = HotPrepareWebSocket(
        {"session_id": "live-expert-missing-model"}
    )

    with pytest.raises(
        HermesProtocolError,
        match="runtime dashboard expert model is unavailable",
    ):
        await _prepare_model_hot_peer(
            websocket,
            hermes_profile_name="expert-7",
            runtime_agent_profile_id=7,
            expected_model=None,
        )

    assert [request["method"] for request in websocket.sent] == ["session.resume"]


@pytest.mark.asyncio
async def test_dashboard_hot_session_prewarm_retries_transient_agent_build_failure() -> None:
    class FlakyHotChannels:
        def __init__(self) -> None:
            self.attempts = 0

        async def prewarm(self, *_args, **_kwargs) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise HermesProtocolError("agent build is not ready")

    hot_channels = FlakyHotChannels()
    adapter = HermesDashboardProtocolAdapter(
        hot_channels=hot_channels,
        prewarm_attempts=3,
        prewarm_backoff_seconds=0,
    )

    result = await adapter.prewarm_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
        hot_channel_identity=_hot_identity(),
    )

    assert result == {"session_id": "rs-101-a"}
    assert hot_channels.attempts == 2


@pytest.mark.asyncio
async def test_dashboard_selected_only_superseded_prewarm_is_not_retried() -> None:
    class SupersededHotChannels:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        async def prewarm(self, *_args, **kwargs) -> bool:
            self.calls.append(kwargs["selected_only"])
            return False

    hot_channels = SupersededHotChannels()
    adapter = HermesDashboardProtocolAdapter(
        hot_channels=hot_channels,
        prewarm_attempts=3,
        prewarm_backoff_seconds=0,
    )

    result = await adapter.prewarm_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
        hot_channel_identity=_hot_identity(),
        selected_only=True,
    )

    assert result == {"session_id": "rs-101-a", "superseded": True}
    assert hot_channels.calls == [True]


@pytest.mark.asyncio
async def test_dashboard_selected_only_real_failure_keeps_retry_budget() -> None:
    class FailingHotChannels:
        def __init__(self) -> None:
            self.attempts = 0

        async def prewarm(self, *_args, **kwargs) -> bool:
            assert kwargs["selected_only"] is True
            assert kwargs["selection_epoch"] == 4
            self.attempts += 1
            raise HermesProtocolError("agent build is not ready")

    hot_channels = FailingHotChannels()
    adapter = HermesDashboardProtocolAdapter(
        hot_channels=hot_channels,
        prewarm_attempts=2,
        prewarm_backoff_seconds=0,
    )

    with pytest.raises(HermesProtocolError, match="agent build is not ready"):
        await adapter.prewarm_session(
            _endpoint(),
            runtime_session_id="rs-101-a",
            hot_channel_identity=_hot_identity(),
            selected_only=True,
            selection_epoch=4,
        )

    assert hot_channels.attempts == 2


@pytest.mark.asyncio
async def test_dashboard_hot_session_prewarm_stops_after_retry_budget() -> None:
    class FailingHotChannels:
        def __init__(self) -> None:
            self.attempts = 0

        async def prewarm(self, *_args, **_kwargs) -> None:
            self.attempts += 1
            raise HermesProtocolError("agent build is not ready")

    hot_channels = FailingHotChannels()
    adapter = HermesDashboardProtocolAdapter(
        hot_channels=hot_channels,
        prewarm_attempts=2,
        prewarm_backoff_seconds=0,
    )

    with pytest.raises(HermesProtocolError, match="agent build is not ready"):
        await adapter.prewarm_session(
            _endpoint(),
            runtime_session_id="rs-101-a",
            hot_channel_identity=_hot_identity(),
        )

    assert hot_channels.attempts == 2


@pytest.mark.asyncio
async def test_default_hot_prepare_defers_agent_when_workspace_already_matches() -> None:
    ws = HotPrepareWebSocket(
        {
            "session_id": "rs-101-a",
            "running": True,
            "info": {
                "cwd": "/data/workspace/sessions/rs-101-a",
                "model": "6",
            },
        }
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
    )
    await adapter.prewarm_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
        hot_channel_identity=_hot_identity(),
    )

    assert [request["method"] for request in ws.sent] == [
        "shell.exec",
        "session.resume",
        "session.cwd.set",
    ]
    assert "eager_build" not in ws.sent[1]["params"]
    assert manager.ready_count == 1
    await manager.close()


@pytest.mark.asyncio
async def test_default_hot_prewarm_waits_for_deferred_agent_ready() -> None:
    ws = HotPrepareWebSocket(
        {
            "session_id": "live-deferred",
            "info": {
                "cwd": "/data/workspace/sessions/rs-101-a",
                "lazy": True,
                "model": "6",
            },
        }
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
    )
    prewarm = asyncio.create_task(
        adapter.prewarm_session(
            _endpoint(),
            runtime_session_id="rs-101-a",
            hot_channel_identity=_hot_identity(),
        )
    )
    for _ in range(100):
        if manager.warming_count == 1:
            break
        await asyncio.sleep(0)

    assert manager.warming_count == 1
    assert manager.ready_count == 0
    assert prewarm.done() is False
    resume_request = next(
        request for request in ws.sent if request["method"] == "session.resume"
    )
    assert "eager_build" not in resume_request["params"]

    await ws.frames.put(
        json.dumps(
            _event(
                "session.info",
                "live-deferred",
                {
                    "cwd": "/data/workspace/sessions/rs-101-a",
                    "lazy": True,
                },
            )
        )
    )
    await asyncio.sleep(0)
    assert prewarm.done() is False

    await ws.frames.put(
        json.dumps(
            _event(
                "session.info",
                "live-deferred",
                {"cwd": "/data/workspace/sessions/rs-101-a", "model": "6"},
            )
        )
    )

    assert await prewarm == {"session_id": "rs-101-a"}
    assert manager.warming_count == 0
    assert manager.ready_count == 1
    await manager.close()


@pytest.mark.asyncio
async def test_hot_prewarm_deferred_build_omits_eager_and_waits_until_ready() -> None:
    ws = HotPrepareWebSocket(
        {
            "session_id": "live-deferred",
            "info": {
                "cwd": "/data/workspace/sessions/rs-101-a",
                "lazy": True,
                "model": "6",
            },
        }
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
    )
    prewarm = asyncio.create_task(
        adapter.prewarm_session(
            _endpoint(),
            runtime_session_id="rs-101-a",
            hot_channel_identity=_hot_identity(),
            eager_build=False,
        )
    )
    for _ in range(100):
        if manager.warming_count == 1:
            break
        await asyncio.sleep(0)

    assert manager.warming_count == 1
    assert prewarm.done() is False
    resume_request = next(
        request for request in ws.sent if request["method"] == "session.resume"
    )
    assert "eager_build" not in resume_request["params"]

    await ws.frames.put(
        json.dumps(
            _event(
                "session.info",
                "live-deferred",
                {"cwd": "/data/workspace/sessions/rs-101-a", "model": "6"},
            )
        )
    )

    assert await prewarm == {"session_id": "rs-101-a"}
    assert manager.ready_count == 1
    await manager.close()


@pytest.mark.asyncio
async def test_hot_prepare_keeps_workspace_prepare_when_resume_cwd_mismatches() -> None:
    ws = HotPrepareWebSocket(
        {
            "session_id": "rs-101-a",
            "info": {"cwd": "/wrong", "model": "6"},
        }
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
    )
    await adapter.prewarm_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
        hot_channel_identity=_hot_identity(),
    )

    assert [request["method"] for request in ws.sent] == [
        "shell.exec",
        "session.resume",
        "session.cwd.set",
    ]
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stalled_method", "expected_methods"),
    [
        ("shell.exec", ["shell.exec"]),
        ("session.resume", ["shell.exec", "session.resume"]),
        (
            "session.cwd.set",
            ["shell.exec", "session.resume", "session.cwd.set"],
        ),
    ],
)
async def test_hot_prepare_rpc_timeout_cleans_inflight_channel(
    stalled_method: str,
    expected_methods: list[str],
) -> None:
    websocket = StallOnMethodWebSocket(stalled_method)
    connector_closed = False

    @asynccontextmanager
    async def connector(_endpoint):
        nonlocal connector_closed
        try:
            yield websocket
        finally:
            connector_closed = True

    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
        prewarm_attempts=1,
    )

    with pytest.raises(
        HermesProtocolError,
        match=rf"RPC {re.escape(stalled_method)} timed out",
    ):
        await asyncio.wait_for(
            adapter.prewarm_session(
                replace(_endpoint(), timeout_seconds=0.1),
                runtime_session_id="rs-101-a",
                hot_channel_identity=_hot_identity(),
            ),
            timeout=1,
        )

    assert [request["method"] for request in websocket.sent] == expected_methods
    assert connector_closed is True
    assert manager.inflight_count == 0
    assert manager.slot_count == 0
    assert manager.warming_count == 0
    assert manager.ready_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_cold_prompt_ack_timeout_is_bounded_and_never_resubmitted() -> None:
    websocket = StallOnMethodWebSocket("prompt.submit")
    connector_close_count = 0

    @asynccontextmanager
    async def connector(_endpoint):
        nonlocal connector_close_count
        try:
            yield websocket
        finally:
            connector_close_count += 1

    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        reconnect_attempts=8,
        reconnect_backoff_seconds=0,
    )

    with pytest.raises(DashboardPromptSubmitTimeout):
        async for _event_item in adapter.stream_session_chat(
            replace(_endpoint(), timeout_seconds=0.1),
            runtime_session_id="rs-101-a",
            runtime_run_id="rr-cold-timeout",
            message="submit once",
        ):
            pass

    assert [request["method"] for request in websocket.sent] == [
        "session.resume",
        "prompt.submit",
    ]
    assert connector_close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("use_hot", [False, True])
async def test_active_run_control_peer_reuses_cold_and_hot_stream_socket(
    use_hot: bool,
) -> None:
    websocket = ControlStreamWebSocket()

    @asynccontextmanager
    async def connector(_endpoint):
        yield websocket

    control_peers = ActiveRunControlPeerRegistry()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        hot_channels=manager,
        control_peers=control_peers,
        reconnect_backoff_seconds=0,
    )
    selected = _hot_identity() if use_hot else None
    if selected is not None:
        await adapter.prewarm_session(
            _endpoint(),
            runtime_session_id="rs-101-a",
            hot_channel_identity=selected,
        )

    events = []

    async def consume() -> None:
        async for event in adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-101-a",
            runtime_run_id="rr-control",
            hot_channel_identity=selected,
            message="start",
        ):
            events.append(event)

    stream = asyncio.create_task(consume())
    connection_id: str | None = None
    for _ in range(200):
        connection_id = await control_peers.current_connection_id("rr-control")
        if (
            connection_id is not None
            and await control_peers.is_agent_ready("rr-control")
        ):
            break
        await asyncio.sleep(0)
    assert connection_id is not None
    assert await control_peers.is_agent_ready("rr-control") is True
    route_connection_id, live_session_id = (
        await control_peers.wait_for_route_identity("rr-control")
    )
    assert route_connection_id == connection_id

    class Boundary:
        def __init__(self, live_session_id: str) -> None:
            self.live_session_id = live_session_id
            self.claimed = False

        def claim_dashboard_redirect(
            self,
            *,
            runtime_run_id: str,
            stored_session_id: str,
            live_session_id: str,
            dashboard_connection_id: str,
        ) -> bool:
            if self.claimed:
                return False
            assert runtime_run_id == "rr-control"
            assert stored_session_id == "rs-101-a"
            assert live_session_id == self.live_session_id
            assert dashboard_connection_id == connection_id
            self.claimed = True
            return True

    redirected = await control_peers.redirect(
        runtime_run_id="rr-control",
        wire_text="use the corrected requirement",
        boundary_handle=Boundary(live_session_id),
        timeout_seconds=1,
    )

    assert redirected.disposition is DashboardRedirectDisposition.REJECTED
    assert sum(
        request["method"] == "session.redirect"
        for request in websocket.sent
    ) == 1
    assert websocket.recv_concurrency_max == 1
    assert events[0].event == "chat.run.started"

    stream.cancel()
    await asyncio.gather(stream, return_exceptions=True)
    assert await control_peers.current_connection_id("rr-control") is None
    await manager.close()


@pytest.mark.asyncio
async def test_hot_prompt_ack_timeout_releases_lease_without_resubmitting() -> None:
    websocket = StallOnMethodWebSocket("prompt.submit")
    connector_close_count = 0

    @asynccontextmanager
    async def connector(_endpoint):
        nonlocal connector_close_count
        try:
            yield websocket
        finally:
            connector_close_count += 1

    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
        reconnect_attempts=8,
        reconnect_backoff_seconds=0,
    )
    selected = _hot_identity()
    endpoint = replace(_endpoint(), timeout_seconds=0.1)
    await adapter.prewarm_session(
        endpoint,
        runtime_session_id="rs-101-a",
        hot_channel_identity=selected,
    )

    with pytest.raises(DashboardPromptSubmitTimeout):
        async for _event_item in adapter.stream_session_chat(
            endpoint,
            runtime_session_id="rs-101-a",
            runtime_run_id="rr-hot-timeout",
            hot_channel_identity=selected,
            message="submit once",
        ):
            pass

    assert sum(
        request["method"] == "prompt.submit"
        for request in websocket.sent
    ) == 1
    assert manager.slot_count == 0
    assert manager.inflight_count == 0
    assert connector_close_count == 1
    await manager.close()


@pytest.mark.asyncio
async def test_hot_prepare_timeout_recovers_on_cold_connection_without_duplicate_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hot_websocket = StallOnMethodWebSocket("shell.exec")
    cold_websocket = FakeWebSocket(
        [
            {"$response": {"stdout": "", "stderr": "", "code": 0}},
            {
                "$response": {
                    "session_id": "rs-101-a",
                    "resumed": "rs-101-a",
                }
            },
            {
                "$response": {
                    "cwd": "/data/workspace/sessions/rs-101-a",
                }
            },
            {"$response": {"status": "submitted"}},
            _event(
                "message.complete",
                "rs-101-a",
                {"text": "recovered", "status": "complete", "usage": {}},
            ),
        ]
    )
    websockets = iter([hot_websocket, cold_websocket])
    connector_close_count = 0

    @asynccontextmanager
    async def connector(_endpoint):
        nonlocal connector_close_count
        try:
            yield next(websockets)
        finally:
            connector_close_count += 1

    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
        reconnect_attempts=1,
        reconnect_backoff_seconds=0,
    )
    request_timeouts: list[tuple[str, float | None]] = []
    original_request = adapter._request

    async def recording_request(
        websocket,
        method,
        params,
        buffered,
        *,
        timeout_seconds=None,
    ):
        request_timeouts.append((method, timeout_seconds))
        return await original_request(
            websocket,
            method,
            params,
            buffered,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(adapter, "_request", recording_request)

    events = [
        event
        async for event in adapter.stream_session_chat(
            replace(_endpoint(), timeout_seconds=0.1),
            runtime_session_id="rs-101-a",
            runtime_run_id="rr-timeout",
            hot_channel_identity=_hot_identity(),
            message="do it once",
        )
    ]

    assert [request["method"] for request in hot_websocket.sent] == ["shell.exec"]
    assert [request["method"] for request in cold_websocket.sent] == [
        "shell.exec",
        "session.resume",
        "session.cwd.set",
        "prompt.submit",
    ]
    assert sum(
        request["method"] == "prompt.submit"
        for request in hot_websocket.sent + cold_websocket.sent
    ) == 1
    assert request_timeouts == [
        ("shell.exec", 0.1),
        ("shell.exec", 0.1),
        ("session.resume", 0.1),
        ("session.cwd.set", 0.1),
        ("prompt.submit", 0.1),
    ]
    reconnecting = next(
        event for event in events if event.event == "chat.transport.reconnecting"
    )
    assert reconnecting.data["prompt_submitted"] is False
    assert events[-1].event == "chat.done"
    assert connector_close_count == 2
    assert manager.inflight_count == 0
    assert manager.slot_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_hot_and_cold_prepare_timeouts_stop_before_prompt_submit() -> None:
    hot_websocket = StallOnMethodWebSocket("shell.exec")
    cold_websocket = StallOnMethodWebSocket("shell.exec")
    websockets = iter([hot_websocket, cold_websocket])
    connector_close_count = 0

    @asynccontextmanager
    async def connector(_endpoint):
        nonlocal connector_close_count
        try:
            yield next(websockets)
        finally:
            connector_close_count += 1

    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
        reconnect_attempts=8,
        reconnect_backoff_seconds=0,
    )
    events = []

    with pytest.raises(HermesProtocolError, match="RPC shell.exec timed out"):
        async for event in adapter.stream_session_chat(
            replace(_endpoint(), timeout_seconds=0.1),
            runtime_session_id="rs-101-a",
            runtime_run_id="rr-double-timeout",
            hot_channel_identity=_hot_identity(),
            message="do it once",
        ):
            events.append(event)

    assert [request["method"] for request in hot_websocket.sent] == ["shell.exec"]
    assert [request["method"] for request in cold_websocket.sent] == ["shell.exec"]
    assert sum(
        request["method"] == "prompt.submit"
        for request in hot_websocket.sent + cold_websocket.sent
    ) == 0
    assert [
        event.event for event in events
        if event.event == "chat.transport.reconnecting"
    ] == ["chat.transport.reconnecting"]
    assert events[0].data["prompt_submitted"] is False
    assert connector_close_count == 2
    assert manager.inflight_count == 0
    assert manager.slot_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_prepared_hot_session_reuses_resume_and_workspace_for_exactly_one_turn() -> None:
    websocket = AutoReplyWebSocket()
    connect_count = 0
    timing_marks: list[str] = []
    timing_metadata: dict[str, str] = {}
    prompt_submit_started: list[bool] = []

    class Timing:
        def mark(self, name: str) -> bool:
            timing_marks.append(name)
            return True

        def set_metadata(self, name: str, value: str) -> None:
            timing_metadata[name] = value

    @asynccontextmanager
    async def connector(_endpoint):
        nonlocal connect_count
        connect_count += 1
        # A terminal lease automatically rearms the selected hot Session.
        # Keep that replacement on a separate fake transport so assertions
        # below describe only the connection leased by this turn.
        yield websocket if connect_count == 1 else AutoReplyWebSocket()

    identity = DashboardHotChannelIdentity(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_session_db_id=5,
        runtime_session_id="rs-101-a",
        runtime_id="org-1-rt-101",
        gateway_model_id="6",
        config_generation=4,
        endpoint_digest="endpoint",
        credential_digest="credential",
        workspace_path_digest="workspace",
        effective_model_entry_id=6,
        model_policy_digest="model-policy",
    )
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
    )

    await adapter.prewarm_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
        hot_channel_identity=identity,
    )
    first = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-101-a",
            runtime_run_id="rr-1",
            hot_channel_identity=identity,
            message="first",
            expected_model="6",
            timing=Timing(),
            prompt_submit_started_sink=lambda: prompt_submit_started.append(
                True
            ),
        )
    ]
    methods = [request["method"] for request in websocket.sent]
    assert connect_count >= 1
    assert manager.result_snapshot().get("hit") == 1
    assert methods.count("session.resume") == 1
    assert methods.count("config.set") == 0
    assert methods.count("shell.exec") == 1
    assert methods.count("session.cwd.set") == 1
    assert methods.count("prompt.submit") == 1
    assert prompt_submit_started == [True]
    assert timing_metadata == {
        "hot_channel": "hit",
        "prompt_submit_status": "streaming",
    }
    assert timing_marks.index("hot_channel_acquired") < timing_marks.index(
        "dashboard_connection_acquired"
    )
    assert timing_marks.index("dashboard_connection_acquired") < timing_marks.index(
        "prompt_submit_begin"
    )
    assert timing_marks.index("prompt_submit_begin") < timing_marks.index("prompt_ack")
    assert [event.event for event in first] == [
        "chat.run.started",
        "chat.message.delta",
        "chat.message.completed",
        "chat.usage",
        "chat.run.completed",
        "chat.done",
    ]
    assert manager.slot_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_personal_hot_lease_reuses_verified_provider_without_second_model_switch() -> None:
    provider = "custom:runtime-10001-route"
    websocket = RouteAwareAutoReplyWebSocket(provider)
    connect_count = 0

    @asynccontextmanager
    async def connector(_endpoint):
        nonlocal connect_count
        connect_count += 1
        yield websocket if connect_count == 1 else AutoReplyWebSocket()

    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        hot_channels=manager,
    )
    await adapter.prewarm_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
        hot_channel_identity=_hot_identity(),
        expected_provider=provider,
    )

    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-101-a",
            runtime_run_id="rr-provider-reuse",
            hot_channel_identity=_hot_identity(),
            expected_model="6",
            expected_provider=provider,
            message="hello",
        )
    ]

    assert events[-1].event == "chat.done"
    assert [request["method"] for request in websocket.sent].count("config.set") == 0
    await manager.close()


@pytest.mark.asyncio
async def test_expert_hot_prewarm_reuses_verified_provider_without_model_switch() -> None:
    websocket = RouteAwareAutoReplyWebSocket("custom:platform")
    connect_count = 0
    timing_metadata: dict[str, str] = {}

    class Timing:
        def mark(self, _name: str) -> bool:
            return True

        def set_metadata(self, name: str, value: str) -> None:
            timing_metadata[name] = value

    @asynccontextmanager
    async def connector(_endpoint):
        nonlocal connect_count
        connect_count += 1
        yield websocket if connect_count == 1 else AutoReplyWebSocket()

    identity = _hot_identity()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
    )

    await adapter.prewarm_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
        hermes_profile_name="expert-7",
        runtime_agent_profile_id=7,
        hot_channel_identity=identity,
    )
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-101-a",
            runtime_run_id="rr-expert-1",
            hot_channel_identity=identity,
            message="hello",
            expected_model="6",
            hermes_profile_name="expert-7",
            runtime_agent_profile_id=7,
            timing=Timing(),
        )
    ]

    methods = [request["method"] for request in websocket.sent]
    assert methods.count("session.resume") == 1
    assert methods.count("config.set") == 0
    assert methods.count("prompt.submit") == 1
    assert timing_metadata["provider_calibration"] == "hot_hit"
    assert events[-1].event == "chat.done"
    await manager.close()


@pytest.mark.asyncio
async def test_expert_hot_prewarm_route_mismatch_does_not_publish_ready_channel() -> None:
    websocket = RouteAwareAutoReplyWebSocket("custom:stale-route")

    @asynccontextmanager
    async def connector(_endpoint):
        yield websocket

    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
        prewarm_attempts=1,
    )

    with pytest.raises(HermesProtocolError, match="session route mismatch"):
        await adapter.prewarm_session(
            _endpoint(),
            runtime_session_id="rs-101-a",
            hermes_profile_name="expert-7",
            runtime_agent_profile_id=7,
            hot_channel_identity=_hot_identity(),
        )

    assert [request["method"] for request in websocket.sent] == [
        "shell.exec",
        "session.resume",
    ]
    assert manager.slot_count == 0
    assert manager.ready_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_two_hot_turns_need_only_initial_explicit_prewarm() -> None:
    websockets: list[AutoReplyWebSocket] = []

    @asynccontextmanager
    async def connector(_endpoint):
        websocket = AutoReplyWebSocket()
        websockets.append(websocket)
        yield websocket

    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
    )
    identity = _hot_identity()
    await adapter.prewarm_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
        hot_channel_identity=identity,
    )

    for number in (1, 2):
        events = [
            event
            async for event in adapter.stream_session_chat(
                _endpoint(),
                runtime_session_id="rs-101-a",
                runtime_run_id=f"rr-{number}",
                hot_channel_identity=identity,
                message=f"turn {number}",
            )
        ]
        assert events[-1].event == "chat.done"

    assert sum(
        request["method"] == "prompt.submit"
        for websocket in websockets
        for request in websocket.sent
    ) == 2
    assert all(
        "eager_build" not in request["params"]
        for websocket in websockets
        for request in websocket.sent
        if request["method"] == "session.resume"
    )
    await manager.close()


@pytest.mark.asyncio
async def test_clean_terminal_aclose_reuses_personal_default_hot_channel() -> None:
    websockets: list[AutoReplyWebSocket] = []

    @asynccontextmanager
    async def connector(_endpoint):
        websocket = AutoReplyWebSocket()
        websockets.append(websocket)
        yield websocket

    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
    )
    identity = replace(_hot_identity(), reuse_across_turns=True)
    await adapter.prewarm_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
        hot_channel_identity=identity,
    )

    for number in (1, 2):
        stream = adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-101-a",
            runtime_run_id=f"rr-{number}",
            hot_channel_identity=identity,
            message=f"turn {number}",
        )
        async for event in stream:
            if event.event == "chat.done":
                await stream.aclose()

    assert len(websockets) == 1
    assert sum(
        request["method"] == "prompt.submit"
        for request in websockets[0].sent
    ) == 2
    assert manager.result_snapshot()["run_reused"] == 2
    await manager.close()


@pytest.mark.asyncio
async def test_hot_disconnect_after_submit_resumes_without_submitting_twice() -> None:
    hot_websocket = DisconnectOnPromptWebSocket()
    recovery_websocket = FakeWebSocket(
        [
            {"$response": {"stdout": "", "stderr": "", "code": 0}},
            {
                "$response": {
                    "session_id": "rs-101-a",
                    "running": True,
                    "inflight": {"user": "do it once", "assistant": "", "streaming": True},
                }
            },
            _event(
                "message.complete",
                "rs-101-a",
                {"text": "recovered", "status": "complete", "usage": {}},
            ),
        ]
    )
    websockets = iter([hot_websocket, recovery_websocket])

    @asynccontextmanager
    async def connector(_endpoint):
        yield next(websockets)

    identity = DashboardHotChannelIdentity(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_session_db_id=5,
        runtime_session_id="rs-101-a",
        runtime_id="org-1-rt-101",
        gateway_model_id="6",
        config_generation=4,
        endpoint_digest="endpoint",
        credential_digest="credential",
        workspace_path_digest="workspace",
        effective_model_entry_id=6,
        model_policy_digest="model-policy",
    )
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
        hot_channels=manager,
        reconnect_backoff_seconds=0,
    )
    await adapter.prewarm_session(
        _endpoint(),
        runtime_session_id="rs-101-a",
        hot_channel_identity=identity,
    )

    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-101-a",
            runtime_run_id="rr-1",
            hot_channel_identity=identity,
            message="do it once",
        )
    ]

    sent = hot_websocket.sent + recovery_websocket.sent
    assert sum(request["method"] == "prompt.submit" for request in sent) == 1
    assert "chat.transport.reconnecting" in [event.event for event in events]
    assert "chat.transport.resumed" in [event.event for event in events]
    assert events[-1].event == "chat.done"
    await manager.close()


@pytest.mark.asyncio
async def test_hot_acquire_failure_propagates_without_entering_second_connector() -> None:
    connector_entered = False

    @asynccontextmanager
    async def connector(_endpoint):
        nonlocal connector_entered
        connector_entered = True
        raise AssertionError("ordinary connector must not be entered")
        yield

    class FailingHotChannels:
        async def acquire_run(self, *_args, **_kwargs):
            raise HermesProtocolError("connect failed")

    identity = DashboardHotChannelIdentity(
        org_id=1,
        user_id=2,
        runtime_workspace_id=101,
        runtime_session_db_id=5,
        runtime_session_id="rs-101-a",
        runtime_id="org-1-rt-101",
        gateway_model_id="6",
        config_generation=4,
        endpoint_digest="endpoint",
        credential_digest="credential",
        workspace_path_digest="workspace",
        effective_model_entry_id=6,
        model_policy_digest="model-policy",
    )
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        hot_channels=FailingHotChannels(),
        reconnect_backoff_seconds=0,
    )

    with pytest.raises(HermesProtocolError, match="connect failed"):
        _ = [
            event
            async for event in adapter.stream_session_chat(
                _endpoint(),
                runtime_session_id="rs-101-a",
                runtime_run_id="rr-fail",
                hot_channel_identity=identity,
                message="hello",
            )
        ]

    assert connector_entered is False


@pytest.mark.asyncio
async def test_dashboard_health_uses_authenticated_status_and_state_store(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []
    responses = {
        "/api/status": (200, {"version": "0.19.0", "auth_required": False}),
        "/api/sessions?limit=1": (200, {"sessions": [], "total": 0}),
    }

    class FakeResponse:
        def __init__(self, status_code: int, payload: object) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    async def request(_endpoint, method, path, **_kwargs):
        calls.append((method, path))
        status_code, payload = responses[path]
        return FakeResponse(status_code, payload)

    monkeypatch.setattr(dashboard_protocol._dashboard_http_sessions, "request", request)

    payload = await HermesDashboardProtocolAdapter().health(_endpoint())

    assert payload == {
        "status": "ok",
        "version": "0.19.0",
        "platform": "hermes-agent",
        "transport": "dashboard-ws",
        "http": "ready",
        "auth": "ready",
    }
    assert calls == [
        ("GET", "/api/status"),
        ("GET", "/api/sessions?limit=1"),
    ]


@pytest.mark.asyncio
async def test_dashboard_lists_profile_scoped_mcp_without_reload(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"servers": [{"name": "scoped-docs"}]}

    async def request(_endpoint, method, path, **_kwargs):
        calls.append((method, path))
        return FakeResponse()

    monkeypatch.setattr(
        dashboard_protocol._dashboard_http_sessions,
        "request",
        request,
    )

    servers = await HermesDashboardProtocolAdapter().list_profile_mcp_servers(
        _endpoint(),
        profile_name="expert-7",
    )

    assert servers == [{"name": "scoped-docs"}]
    assert calls == [("GET", "/api/mcp/servers?profile=expert-7")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_payload", "sessions_payload", "message"),
    [
        ([], {"sessions": []}, "non-object"),
        ({"version": "", "auth_required": False}, {"sessions": []}, "health payload"),
        ({"version": "0.19.0", "auth_required": True}, {"sessions": []}, "health payload"),
        ({"version": "0.19.0", "auth_required": False}, {"sessions": {}}, "state store"),
    ],
)
async def test_dashboard_health_rejects_invalid_payloads(
    monkeypatch,
    status_payload,
    sessions_payload,
    message,
) -> None:
    class FakeResponse:
        status_code = 200

        def __init__(self, payload: object) -> None:
            self._payload = payload

        def json(self):
            return self._payload

    async def request(_endpoint, _method, path, **_kwargs):
        return FakeResponse(
            status_payload if path == "/api/status" else sessions_payload
        )

    monkeypatch.setattr(dashboard_protocol._dashboard_http_sessions, "request", request)

    with pytest.raises(HermesProtocolError, match=message):
        await HermesDashboardProtocolAdapter().health(_endpoint())


@pytest.mark.asyncio
async def test_dashboard_capabilities_aggregates_dashboard_http_and_ws(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []
    responses = {
        "/api/model/info": {"provider": "platform", "model": "model-7"},
        "/api/skills": [
            {
                "name": "sales-analysis",
                "description": "Analyze sales",
                "category": "business",
                "enabled": True,
                "provenance": "agent",
            }
        ],
        "/api/tools/toolsets": [
            {
                "name": "terminal",
                "platform": "cli",
                "enabled": True,
                "available": True,
                "tools": ["terminal"],
            }
        ],
        "/api/mcp/servers": {
            "servers": [
                {
                    "name": "enterprise-knowledge",
                    "transport": "stdio",
                    "enabled": True,
                }
            ]
        },
    }

    class FakeResponse:
        status_code = 200

        def __init__(self, payload: object) -> None:
            self._payload = payload

        def json(self):
            return self._payload

    async def request(_endpoint, method, path, **_kwargs):
        calls.append((method, path))
        return FakeResponse(responses[path])

    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "sections": [
                        {
                            "name": "Core",
                            "tools": [
                                {
                                    "name": "terminal",
                                    "description": "Run a command",
                                }
                            ],
                        }
                    ],
                    "total": 1,
                }
            }
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    monkeypatch.setattr(dashboard_protocol._dashboard_http_sessions, "request", request)

    payload = await HermesDashboardProtocolAdapter(connector=connector).capabilities(
        _endpoint()
    )

    assert payload == {
        "object": "hermes.dashboard.capabilities",
        "platform": "hermes-agent",
        "runtime": {
            "mode": "dashboard_live_session",
            "tool_execution": "server",
            "split_runtime": False,
        },
        "model": {"provider": "platform", "model": "model-7"},
        "skills": responses["/api/skills"],
        "toolsets": responses["/api/tools/toolsets"],
        "mcp_servers": responses["/api/mcp/servers"]["servers"],
        "resolved_tools": [
            {"name": "terminal", "description": "Run a command"}
        ],
        "features": {
            "dashboard_http": True,
            "dashboard_ws": True,
            "session_resources": True,
            "session_chat_streaming": True,
            "session_model_switch": True,
            "run_stop": True,
            "approval_response": True,
            "artifact_streaming": True,
        },
    }
    assert set(calls) == {
        ("GET", "/api/model/info"),
        ("GET", "/api/skills"),
        ("GET", "/api/tools/toolsets"),
        ("GET", "/api/mcp/servers"),
    }
    assert ws.sent[0]["method"] == "tools.show"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload", "message"),
    [
        ("/api/skills", {"skills": []}, "skills payload"),
        ("/api/tools/toolsets", {"toolsets": []}, "toolsets payload"),
        ("/api/mcp/servers", {"servers": {}}, "MCP payload"),
    ],
)
async def test_dashboard_capabilities_rejects_invalid_inventories(
    monkeypatch,
    path,
    payload,
    message,
) -> None:
    responses = {
        "/api/model/info": {},
        "/api/skills": [],
        "/api/tools/toolsets": [],
        "/api/mcp/servers": {"servers": []},
    }
    responses[path] = payload

    class FakeResponse:
        status_code = 200

        def __init__(self, response_payload: object) -> None:
            self._payload = response_payload

        def json(self):
            return self._payload

    async def request(_endpoint, _method, request_path, **_kwargs):
        return FakeResponse(responses[request_path])

    ws = FakeWebSocket([{"$response": {"sections": [], "total": 0}}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    monkeypatch.setattr(dashboard_protocol._dashboard_http_sessions, "request", request)

    with pytest.raises(HermesProtocolError, match=message):
        await HermesDashboardProtocolAdapter(connector=connector).capabilities(
            _endpoint()
        )


@pytest.mark.asyncio
async def test_dashboard_capabilities_rejects_invalid_tools_show(monkeypatch) -> None:
    responses = {
        "/api/model/info": {},
        "/api/skills": [],
        "/api/tools/toolsets": [],
        "/api/mcp/servers": {"servers": []},
    }

    class FakeResponse:
        status_code = 200

        def __init__(self, payload: object) -> None:
            self._payload = payload

        def json(self):
            return self._payload

    async def request(_endpoint, _method, path, **_kwargs):
        return FakeResponse(responses[path])

    ws = FakeWebSocket([{"$response": {"sections": {}, "total": 0}}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    monkeypatch.setattr(dashboard_protocol._dashboard_http_sessions, "request", request)

    with pytest.raises(HermesProtocolError, match=r"tools\.show payload"):
        await HermesDashboardProtocolAdapter(connector=connector).capabilities(
            _endpoint()
        )


@pytest.mark.asyncio
async def test_dashboard_capabilities_rejects_resolved_cronjob_tool(
    monkeypatch,
) -> None:
    responses = {
        "/api/model/info": {},
        "/api/skills": [],
        "/api/tools/toolsets": [
            {
                "name": "cronjob",
                "enabled": True,
                "available": True,
                "tools": ["cronjob"],
            }
        ],
        "/api/mcp/servers": {"servers": []},
    }

    class FakeResponse:
        status_code = 200

        def __init__(self, payload: object) -> None:
            self._payload = payload

        def json(self):
            return self._payload

    async def request(_endpoint, _method, path, **_kwargs):
        return FakeResponse(responses[path])

    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "sections": [
                        {
                            "name": "Scheduling",
                            "tools": [{"name": "cronjob"}],
                        }
                    ],
                    "total": 1,
                }
            }
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    monkeypatch.setattr(
        dashboard_protocol._dashboard_http_sessions,
        "request",
        request,
    )

    with pytest.raises(HermesProtocolError, match="disabled cronjob"):
        await HermesDashboardProtocolAdapter(connector=connector).capabilities(
            _endpoint()
        )


@pytest.mark.asyncio
async def test_dashboard_list_session_artifacts_excludes_work_files(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "entries": [
                    {
                        "path": "/data/workspace/sessions/rs-1/lesson.pptx",
                        "name": "lesson.pptx",
                        "is_directory": False,
                        "size": 100,
                        "mime_type": (
                            "application/vnd.openxmlformats-officedocument."
                            "presentationml.presentation"
                        ),
                    },
                    {
                        "path": "/data/workspace/sessions/rs-1/final.js",
                        "name": "final.js",
                        "is_directory": False,
                        "size": 20,
                        "mime_type": "text/javascript",
                    },
                    {
                        "path": "/data/workspace/sessions/rs-1/.work",
                        "name": ".work",
                        "is_directory": True,
                        "size": 0,
                    },
                    {
                        "path": "/data/workspace/sessions/rs-1/.work/create_ppt.js",
                        "name": "create_ppt.js",
                        "is_directory": False,
                        "size": 200,
                        "mime_type": "text/javascript",
                    },
                    {
                        "path": "/data/workspace/sessions/rs-1/.venv/lib/python3.13/site-packages/openpyxl/__init__.pyc",
                        "name": "__init__.pyc",
                        "is_directory": False,
                        "size": 200,
                        "mime_type": "application/octet-stream",
                    },
                ]
            }

    class FakeClient:
        async def get(self, _url, **_kwargs):
            return FakeResponse()

    @asynccontextmanager
    async def fake_client(_endpoint):
        yield FakeClient(), "http://dashboard", {"Host": "runtime.example"}

    monkeypatch.setattr(
        dashboard_protocol,
        "authenticated_dashboard_client",
        fake_client,
    )
    artifacts = await HermesDashboardProtocolAdapter(
        session_workspace_root="/data/workspace"
    ).list_session_artifacts(_endpoint(), runtime_session_id="rs-1")

    assert [artifact.relative_path for artifact in artifacts] == [
        "workspace/sessions/rs-1/lesson.pptx",
        "workspace/sessions/rs-1/final.js",
    ]


@pytest.mark.asyncio
async def test_dashboard_list_session_artifacts_uses_expert_session_root(monkeypatch) -> None:
    requests: list[dict] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "entries": [{
                    "path": "/data/workspace/experts/69/sessions/rs-1/标准电子发票_已打码.pdf",
                    "is_directory": False,
                    "size": 4096,
                    "mime_type": "application/pdf",
                }]
            }

    class FakeClient:
        async def get(self, _url, **kwargs):
            requests.append(kwargs)
            return FakeResponse()

    @asynccontextmanager
    async def fake_client(_endpoint):
        yield FakeClient(), "http://dashboard", {"Host": "runtime.example"}

    monkeypatch.setattr(dashboard_protocol, "authenticated_dashboard_client", fake_client)
    artifacts = await HermesDashboardProtocolAdapter(
        session_workspace_root="/data/workspace"
    ).list_session_artifacts(
        _endpoint(),
        runtime_session_id="rs-1",
        runtime_agent_profile_id=69,
        hermes_profile_name="expert-69",
    )

    assert requests[0]["params"]["path"] == "/data/workspace/experts/69/sessions/rs-1"
    assert [artifact.relative_path for artifact in artifacts] == [
        "workspace/experts/69/sessions/rs-1/标准电子发票_已打码.pdf"
    ]


@pytest.mark.asyncio
async def test_dashboard_managed_file_upload_and_delete_use_guarded_file_routes(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"entry": {"size": 5, "mime_type": "text/plain"}}

    class FakeClient:
        async def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            return FakeResponse()

        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return FakeResponse()

    @asynccontextmanager
    async def fake_client(_endpoint):
        yield FakeClient(), "http://dashboard", {"Host": "runtime.example"}

    monkeypatch.setattr(dashboard_protocol, "authenticated_dashboard_client", fake_client)
    adapter = HermesDashboardProtocolAdapter(session_workspace_root="/data/workspace")
    uploaded = await adapter.upload_managed_artifact(
        _endpoint(),
        relative_path="workspace/sessions/rs-1/uploads/readme.txt",
        content=BytesIO(b"hello"),
        filename="readme.txt",
        mime_type="text/plain",
    )
    await adapter.delete_managed_artifact(
        _endpoint(), relative_path="workspace/sessions/rs-1/uploads/readme.txt"
    )

    assert uploaded.size_bytes == 5
    assert calls[0][0:2] == ("POST", "http://dashboard/api/files/upload-stream")
    assert calls[0][2]["data"]["path"] == "/data/workspace/sessions/rs-1/uploads/readme.txt"
    assert calls[0][2]["data"]["overwrite"] == "false"
    assert calls[1][0:2] == ("DELETE", "http://dashboard/api/files")
    assert calls[1][2]["json"]["path"] == "/data/workspace/sessions/rs-1/uploads/readme.txt"


@pytest.mark.asyncio
async def test_dashboard_managed_file_rename_stays_inside_workspace() -> None:
    ws = FakeWebSocket([{"$response": {"stdout": "", "stderr": "", "code": 0}}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
    )
    await adapter.rename_managed_artifact(
        _endpoint(),
        source_relative_path="workspace/sessions/rs-1/uploads/old.txt",
        target_relative_path="workspace/sessions/rs-1/uploads/new.txt",
    )

    command = ws.sent[0]["params"]["command"]
    assert ws.sent[0]["method"] == "shell.exec"
    assert "/data/workspace/sessions/rs-1/uploads/old.txt" in command
    assert "/data/workspace/sessions/rs-1/uploads/new.txt" in command


@pytest.mark.asyncio
async def test_dashboard_http_session_pool_reuses_token_and_client(monkeypatch) -> None:
    clients = []

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, **_kwargs):
            self.is_closed = False
            self.posts = []
            clients.append(self)

        async def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return FakeResponse()

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(dashboard_protocol.httpx, "AsyncClient", FakeClient)
    pool = DashboardHttpSessionPool()

    first = await pool.acquire(_endpoint())
    second = await pool.acquire(_endpoint())

    assert first is second
    assert len(clients) == 1
    assert len(clients[0].posts) == 0
    assert first.headers == {
        "Host": "hermes-runtime-101.hermes-runtimes.hermes.local",
        "X-Hermes-Session-Token": "runtime-api-key",
    }
    await pool.aclose()
    assert clients[0].is_closed is True


@pytest.mark.asyncio
async def test_dashboard_http_session_pool_does_not_call_password_login(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, **_kwargs):
            self.is_closed = False
            self.posts = []

        async def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            raise AssertionError("acquire must not perform a password login")

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(dashboard_protocol.httpx, "AsyncClient", FakeClient)
    pool = DashboardHttpSessionPool()

    session = await pool.acquire(_endpoint())

    assert session.client.posts == []
    assert session.headers["X-Hermes-Session-Token"] == "runtime-api-key"
    await pool.aclose()


@pytest.mark.asyncio
async def test_connect_dashboard_uses_loopback_session_token(monkeypatch) -> None:
    captured: dict[str, object] = {}
    websocket = FakeWebSocket([])

    class FakeConnection:
        async def __aenter__(self):
            return websocket

        async def __aexit__(self, *_args):
            return None

    def connect(uri: str, **kwargs):
        captured["uri"] = uri
        captured["kwargs"] = kwargs
        return FakeConnection()

    monkeypatch.setattr(dashboard_protocol.websockets, "connect", connect)

    async with dashboard_protocol.connect_dashboard(_endpoint()) as connected:
        assert connected is websocket

    assert captured["uri"] == (
        "ws://hermes-runtime-101.hermes-runtimes.hermes.local/"
        "api/ws?token=runtime-api-key"
    )
    assert captured["kwargs"] == {
        "host": "127.0.0.1",
        "port": 31080,
        "proxy": None,
        "open_timeout": 30.0,
        "ping_interval": 20,
        "ping_timeout": 60,
        "max_size": 4 * 1024 * 1024,
    }


@pytest.mark.asyncio
async def test_dashboard_http_session_pool_reauthenticates_once_after_401(
    monkeypatch,
) -> None:
    clients = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **_kwargs):
            self.is_closed = False
            self.index = len(clients)
            self.posts = []
            self.requests = []
            clients.append(self)

        async def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return FakeResponse(200)

        async def request(self, method, url, **kwargs):
            self.requests.append((method, url, kwargs))
            if self.index == 0:
                return FakeResponse(401)
            return FakeResponse(200, {"sessions": []})

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(dashboard_protocol.httpx, "AsyncClient", FakeClient)
    pool = DashboardHttpSessionPool()

    response = await pool.request(
        _endpoint(),
        "GET",
        "/api/sessions?limit=1",
    )

    assert response.status_code == 200
    assert len(clients) == 2
    assert clients[0].is_closed is True
    assert len(clients[0].posts) == 0
    assert len(clients[1].posts) == 0
    assert len(clients[0].requests) == 1
    assert len(clients[1].requests) == 1
    await pool.aclose()


@pytest.mark.asyncio
async def test_dashboard_health_fails_closed_after_second_403(monkeypatch) -> None:
    clients = []

    class FakeResponse:
        status_code = 403

    class FakeClient:
        def __init__(self, **_kwargs):
            self.is_closed = False
            self.posts = []
            self.requests = []
            clients.append(self)

        async def post(self, url, **kwargs):
            self.posts.append((url, kwargs))

            class LoginResponse:
                status_code = 200

            return LoginResponse()

        async def request(self, method, url, **kwargs):
            self.requests.append((method, url, kwargs))
            return FakeResponse()

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(dashboard_protocol.httpx, "AsyncClient", FakeClient)
    pool = DashboardHttpSessionPool()
    monkeypatch.setattr(dashboard_protocol, "_dashboard_http_sessions", pool)

    with pytest.raises(HermesProtocolError) as captured:
        await HermesDashboardProtocolAdapter().health(_endpoint())

    assert captured.value.status_code == 403
    assert captured.value.path == "/api/status"
    assert len(clients) == 2
    assert sum(len(client.posts) for client in clients) == 0
    assert sum(len(client.requests) for client in clients) == 2
    await pool.aclose()


@pytest.mark.asyncio
async def test_dashboard_apply_config_uses_reload_rpc_and_returns_requested_generation_hash() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"sessions": []}},
            {"$response": {"added": [], "removed": [], "total": 1}},
            {"$response": {"status": "reloaded"}},
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    result = await adapter.apply_config(
        _endpoint(),
        generation=12,
        expected_projection_hash="sha256:abc",
        reload_skills=True,
        reload_mcp=True,
        invalidate_agents=True,
    )

    assert [request["method"] for request in ws.sent] == [
        "session.active_list",
        "skills.reload",
        "reload.mcp",
    ]
    assert ws.sent[2]["params"] == {"confirm": True}
    assert result["loaded_generation"] == 12
    assert result["projection_hash"] == "sha256:abc"
    assert result["reload"]["mcp"] == {"status": "reloaded"}
    assert result["reload"]["mcp_sessions"] == []


@pytest.mark.asyncio
async def test_dashboard_apply_config_closes_idle_agents_before_global_mcp_reload() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "sessions": [
                        {"id": "live-1", "status": "idle"},
                        {"id": "live-2", "status": "idle"},
                    ]
                }
            },
            {"$response": {"closed": True}},
            {"$response": {"closed": True}},
            {"$response": {"status": "reloaded"}},
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    hot_channels = RecordingHotChannels()
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        hot_channels=hot_channels,
    )
    result = await adapter.apply_config(
        _endpoint(),
        generation=12,
        expected_projection_hash="sha256:abc",
        reload_skills=False,
        reload_mcp=True,
        invalidate_agents=True,
    )

    assert [request["method"] for request in ws.sent] == [
        "session.active_list",
        "session.close",
        "session.close",
        "reload.mcp",
    ]
    assert [request["params"] for request in ws.sent[1:3]] == [
        {"session_id": "live-1"},
        {"session_id": "live-2"},
    ]
    assert ws.sent[3]["params"] == {"confirm": True}
    assert result["reload"]["mcp_sessions"] == ["live-1", "live-2"]
    assert hot_channels.idle_checks == [101]


@pytest.mark.asyncio
async def test_dashboard_reload_mcp_invalidates_all_idle_agents_before_reload() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "sessions": [
                        {"id": "live-alpha", "status": "idle"},
                        {"id": "live-beta", "status": "idle"},
                    ]
                }
            },
            {"$response": {"closed": True}},
            {"$response": {"closed": True}},
            {"$response": {"status": "reloaded"}},
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)

    await adapter.reload_mcp(_endpoint())

    assert [request["method"] for request in ws.sent] == [
        "session.active_list",
        "session.close",
        "session.close",
        "reload.mcp",
    ]
    assert ws.sent[-1]["params"] == {"confirm": True}


@pytest.mark.asyncio
async def test_dashboard_reload_mcp_keeps_active_agent_open_in_test_image_mode() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "sessions": [
                        {"id": "live-working", "status": "working"},
                        {"id": "live-idle", "status": "idle"},
                    ]
                }
            },
            {"$response": {"closed": True}},
            {"$response": {"status": "reloaded"}},
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    hot_channels = RecordingHotChannels(busy=True)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        hot_channels=hot_channels,
        allow_active_mcp_reload=True,
    )

    await adapter.reload_mcp(_endpoint())

    assert [request["method"] for request in ws.sent] == [
        "session.active_list",
        "session.close",
        "reload.mcp",
    ]
    assert ws.sent[1]["params"] == {"session_id": "live-idle"}
    assert hot_channels.idle_checks == []


@pytest.mark.asyncio
async def test_dashboard_registry_reload_is_unconditional_with_active_run(
    monkeypatch,
) -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "sessions": [
                        {"id": "live-working", "status": "working"},
                        {"id": "live-idle", "status": "idle"},
                    ]
                }
            },
            {"$response": {"closed": True}},
            {"$response": {"status": "reloaded"}},
            {
                "$response": {
                    "toolsets": [
                        {
                            "name": "crm",
                            "enabled": False,
                            "tools": ["mcp__crm__search"],
                        }
                    ],
                }
            },
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    async def request(_endpoint, method, path, **_kwargs):
        assert method == "GET"
        responses = {
            "/api/mcp/servers": {"servers": [{"slug": "crm"}]},
        }
        return FakeResponse(responses[path])

    monkeypatch.setattr(
        dashboard_protocol._dashboard_http_sessions,
        "request",
        request,
    )
    hot_channels = RecordingHotChannels(busy=True)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        hot_channels=hot_channels,
    )

    result = await adapter.reload_mcp_unconditional_and_list(_endpoint())

    assert result == [
        {
            "slug": "crm",
            "tools": ["mcp__crm__search"],
            "toolset_available": True,
        }
    ]
    assert [request["method"] for request in ws.sent] == [
        "session.active_list",
        "session.close",
        "reload.mcp",
        "tools.list",
    ]
    assert ws.sent[1]["params"] == {"session_id": "live-idle"}
    assert hot_channels.idle_checks == []


@pytest.mark.asyncio
async def test_dashboard_reload_mcp_still_rejects_active_run_by_default() -> None:
    connector_calls = 0

    @asynccontextmanager
    async def connector(_endpoint):
        nonlocal connector_calls
        connector_calls += 1
        yield FakeWebSocket([])

    hot_channels = RecordingHotChannels(busy=True)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        hot_channels=hot_channels,
    )

    with pytest.raises(DashboardChannelBusy, match="active run"):
        await adapter.reload_mcp(_endpoint())

    assert connector_calls == 0
    assert hot_channels.idle_checks == [101]


@pytest.mark.asyncio
async def test_dashboard_apply_config_reloads_skills_without_closing_idle_agents() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"added": [], "removed": [], "total": 1}},
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    hot_channels = RecordingHotChannels()
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        hot_channels=hot_channels,
    )
    result = await adapter.apply_config(
        _endpoint(),
        generation=12,
        expected_projection_hash="sha256:abc",
        reload_skills=True,
        reload_mcp=False,
        invalidate_agents=True,
    )

    assert [request["method"] for request in ws.sent] == ["skills.reload"]
    assert ws.sent[0]["params"] == {}
    assert result["reload"]["skills"]["total"] == 1
    assert hot_channels.idle_checks == []


@pytest.mark.asyncio
async def test_dashboard_reload_skills_preserves_empty_default_params() -> None:
    ws = FakeWebSocket(
        [{"$response": {"added": [], "removed": [], "total": 1}}]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    await adapter.reload_skills(_endpoint())

    assert ws.sent[0]["method"] == "skills.reload"
    assert ws.sent[0]["params"] == {}


@pytest.mark.asyncio
async def test_dashboard_reload_skills_returns_native_response() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "output": "1 skill(s) available",
                    "result": {"added": [], "removed": [], "total": 1},
                }
            },
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    result = await adapter.reload_skills(_endpoint())

    assert ws.sent[0]["method"] == "skills.reload"
    assert ws.sent[0]["params"] == {}
    assert result == {
        "output": "1 skill(s) available",
        "result": {"added": [], "removed": [], "total": 1},
    }


@pytest.mark.asyncio
async def test_dashboard_apply_config_rejects_busy_hot_channel_before_mcp_reload() -> None:
    connector_calls = 0

    @asynccontextmanager
    async def connector(_endpoint):
        nonlocal connector_calls
        connector_calls += 1
        yield FakeWebSocket([])

    hot_channels = RecordingHotChannels(busy=True)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        hot_channels=hot_channels,
    )
    with pytest.raises(DashboardChannelBusy, match="active run"):
        await adapter.apply_config(
            _endpoint(),
            generation=12,
            expected_projection_hash="sha256:abc",
            reload_skills=False,
            reload_mcp=True,
            invalidate_agents=True,
        )

    assert connector_calls == 0
    assert hot_channels.idle_checks == [101]


@pytest.mark.asyncio
async def test_dashboard_apply_config_allows_skill_reload_during_existing_run() -> None:
    ws = FakeWebSocket(
        [{"$response": {"added": ["new-skill"], "removed": [], "total": 1}}]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    hot_channels = RecordingHotChannels(busy=True)
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        hot_channels=hot_channels,
    )
    result = await adapter.apply_config(
        _endpoint(),
        generation=12,
        expected_projection_hash="sha256:abc",
        reload_skills=True,
        reload_mcp=False,
        invalidate_agents=True,
    )

    assert [request["method"] for request in ws.sent] == ["skills.reload"]
    assert result["reload"]["skills"]["total"] == 1
    assert hot_channels.idle_checks == []


@pytest.mark.asyncio
async def test_dashboard_apply_config_rejects_mcp_reload_while_session_is_active() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"sessions": [{"id": "live-running", "status": "streaming"}]}},
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    with pytest.raises(
        HermesProtocolError,
        match="cannot reload Runtime config.*streaming",
    ):
        await adapter.apply_config(
            _endpoint(),
            generation=12,
            expected_projection_hash="sha256:abc",
            reload_skills=False,
            reload_mcp=True,
            invalidate_agents=True,
        )

    assert [request["method"] for request in ws.sent] == ["session.active_list"]


@pytest.mark.asyncio
async def test_dashboard_apply_config_times_out_when_reload_rpc_does_not_reply() -> None:
    ws = FakeWebSocket([{"$wait": 5}])
    endpoint = RuntimeEndpoint(
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        endpoint_base_url="http://127.0.0.1:31080",
        endpoint_host_header="hermes-runtime-101.hermes-runtimes.hermes.local",
        api_key="runtime-api-key",
        timeout_seconds=0.1,
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    with pytest.raises(HermesProtocolError, match="skills.reload timed out"):
        await adapter.apply_config(
            endpoint,
            generation=12,
            expected_projection_hash="sha256:abc",
            reload_skills=True,
            reload_mcp=False,
            invalidate_agents=False,
        )


@pytest.mark.asyncio
async def test_dashboard_stream_maps_live_reasoning_before_answer_and_terminal_events() -> None:
    ws = FakeWebSocket(
        [
            _event("gateway.ready", "", {}),
            {"$response": {"session_id": "live-1", "resumed": "rs-101"}},
            {"$response": {"status": "submitted"}},
            _event("reasoning.delta", "live-1", {"text": "first thought"}),
            _event("reasoning.available", "live-1", {"text": "fallback snapshot"}),
            _event("message.delta", "live-1", {"text": "answer"}),
            _event(
                "message.complete",
                "live-1",
                {"text": "answer", "usage": {"input": 3, "output": 2, "total": 5}},
            ),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
    )
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-101",
            message="hello",
        )
    ]

    assert [event.event for event in events] == [
        "chat.run.started",
        "chat.reasoning.delta",
        "chat.message.delta",
        "chat.message.completed",
        "chat.usage",
        "chat.run.completed",
        "chat.done",
    ]
    assert events[0].data["run_id"] == "live-1"
    assert events[1].data["delta"] == "first thought"
    assert events[2].data["delta"] == "answer"
    assert events[3].data["reasoning"] == "first thought"
    assert events[4].data["usage"]["total"] == 5
    assert [request["method"] for request in ws.sent] == ["session.resume", "prompt.submit"]
    assert ws.sent[0]["params"]["session_id"] == "rs-101"


@pytest.mark.asyncio
@pytest.mark.parametrize("busy_status", ["steered", "redirected"])
async def test_dashboard_stream_rejects_busy_prompt_submit_disposition(
    busy_status,
) -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-queued", "resumed": "rs-queued"}},
            {"$response": {"status": busy_status}},
        ]
    )
    prompt_acks = 0

    async def prompt_ack() -> None:
        nonlocal prompt_acks
        prompt_acks += 1

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    with pytest.raises(HermesProtocolError, match="busy prompt.submit disposition"):
        _ = [
            event
            async for event in adapter.stream_session_chat(
                _endpoint(),
                runtime_session_id="rs-queued",
                message="follow up",
                prompt_ack_sink=prompt_ack,
            )
        ]

    assert prompt_acks == 0


@pytest.mark.asyncio
async def test_dashboard_stream_treats_queued_prompt_as_accepted_turn() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-queued", "resumed": "rs-queued"}},
            {"$response": {"status": "queued"}},
            _event("message.start", "live-queued", {}),
            _event("message.delta", "live-queued", {"text": "answer"}),
            _event(
                "message.complete",
                "live-queued",
                {"text": "answer", "status": "complete", "usage": {}},
            ),
        ]
    )
    prompt_acks = 0
    timing_marks: list[str] = []
    timing_metadata: dict[str, str] = {}

    class Timing:
        def mark(self, name: str) -> bool:
            timing_marks.append(name)
            return True

        def set_metadata(self, name: str, value: str) -> None:
            timing_metadata[name] = value

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)

    async def prompt_ack() -> None:
        nonlocal prompt_acks
        prompt_acks += 1

    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-queued",
            message="follow up",
            prompt_ack_sink=prompt_ack,
            timing=Timing(),
        )
    ]

    assert prompt_acks == 1
    assert timing_metadata == {
        "hot_channel": "cold",
        "prompt_submit_status": "queued",
    }
    assert timing_marks.index("prompt_submit_begin") < timing_marks.index(
        "prompt_ack"
    )
    prompt_submit = next(
        request for request in ws.sent if request["method"] == "prompt.submit"
    )
    assert prompt_submit["params"]["queued"] is True
    assert [event.event for event in events] == [
        "chat.run.started",
        "chat.message.delta",
        "chat.message.completed",
        "chat.usage",
        "chat.run.completed",
        "chat.done",
    ]
    assert not any(event.event == "chat.error" for event in events)


@pytest.mark.asyncio
async def test_queued_prompt_waits_for_message_start_to_mark_agent_ready() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-queued-control", "resumed": "rs-queued"}},
            {"$response": {"status": "queued"}},
            _event("message.start", "live-queued-control", {}),
        ]
    )
    prompt_ack = asyncio.Event()
    message_start_seen = asyncio.Event()

    class BoundaryObserver:
        async def observe_control_peer_registered(
            self,
            *,
            run_id: str,
            stored_session_id: str,
            dashboard_connection_id: str,
        ) -> None:
            del run_id, stored_session_id, dashboard_connection_id

        async def observe_message_start(
            self,
            *,
            run_id: str,
            stored_session_id: str,
            live_session_id: str,
            dashboard_connection_id: str,
        ) -> None:
            assert run_id == "rr-queued-control"
            assert stored_session_id == "rs-queued"
            assert live_session_id == "live-queued-control"
            assert dashboard_connection_id
            message_start_seen.set()

        async def observe_tool_activity(
            self,
            *,
            run_id: str,
            active: bool,
        ) -> None:
            del run_id, active

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    control_peers = ActiveRunControlPeerRegistry()
    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        control_peers=control_peers,
        reconnect_attempts=0,
        reconnect_backoff_seconds=0,
    )

    control_peers.set_boundary_observer(BoundaryObserver())

    async def acknowledge_prompt() -> None:
        assert not message_start_seen.is_set()
        prompt_ack.set()

    async def consume() -> list[str]:
        return [
            event.event
            async for event in adapter.stream_session_chat(
                _endpoint(),
                runtime_session_id="rs-queued",
                runtime_run_id="rr-queued-control",
                message="follow up",
                prompt_ack_sink=acknowledge_prompt,
            )
        ]

    stream = asyncio.create_task(consume())
    await asyncio.wait_for(prompt_ack.wait(), timeout=1)
    assert not message_start_seen.is_set()
    await asyncio.wait_for(message_start_seen.wait(), timeout=1)

    stream.cancel()
    await asyncio.gather(stream, return_exceptions=True)
    prompt_submit = next(
        request for request in ws.sent if request["method"] == "prompt.submit"
    )
    assert prompt_submit["params"]["queued"] is True


@pytest.mark.asyncio
async def test_dashboard_stream_streaming_turn_keeps_reasoning_before_ack_and_message_start() -> None:
    ws = FakeWebSocket(
        [
            _event("reasoning.delta", "live-streaming", {"text": "stale thought"}),
            {"$response": {"session_id": "live-streaming", "resumed": "rs-streaming"}},
            _event("reasoning.delta", "live-streaming", {"text": "current thought"}),
            {"$response": {"status": "streaming"}},
            _event("message.start", "live-streaming", {}),
            _event(
                "message.complete",
                "live-streaming",
                {"text": "current answer", "status": "complete"},
            ),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-streaming", message="hello"
        )
    ]

    reasoning = [
        event.data.get("delta")
        for event in events
        if event.event == "chat.reasoning.delta"
    ]
    assert reasoning == ["current thought"]
    assert all("stale thought" not in str(event.data) for event in events)
    assert any(event.data.get("text") == "current answer" for event in events)


@pytest.mark.asyncio
async def test_dashboard_stream_rejects_unknown_prompt_submit_status() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-unknown", "resumed": "rs-unknown"}},
            {"$response": {"status": "mystery"}},
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    with pytest.raises(HermesProtocolError, match="unsupported prompt.submit status"):
        _ = [
            event
            async for event in adapter.stream_session_chat(
                _endpoint(), runtime_session_id="rs-unknown", message="hello"
            )
        ]


@pytest.mark.asyncio
async def test_dashboard_stream_suppresses_waiting_for_model_interrupt_metadata() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-stop", "resumed": "rs-stop"}},
            {"$response": {"status": "submitted"}},
            _event(
                "message.complete",
                "live-stop",
                {
                    "text": "Operation interrupted: waiting for model response (5.7s elapsed).",
                    "status": "interrupted",
                    "usage": {"input": 2, "output": 0, "total": 2},
                },
            ),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-stop", message="hello"
        )
    ]

    assert [event.event for event in events] == [
        "chat.run.started",
        "chat.usage",
        "chat.done",
    ]
    assert events[-1].data["status"] == "canceled"


@pytest.mark.asyncio
async def test_dashboard_stream_preserves_real_partial_answer_on_interrupt() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-partial", "resumed": "rs-partial"}},
            {"$response": {"status": "submitted"}},
            _event(
                "message.complete",
                "live-partial",
                {"text": "已经完成第一部分。", "status": "interrupted"},
            ),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-partial", message="hello"
        )
    ]

    assert [event.event for event in events] == [
        "chat.run.started",
        "chat.message.completed",
        "chat.done",
    ]
    assert events[1].data["text"] == "已经完成第一部分。"
    assert events[-1].data["status"] == "canceled"
    assert all(event.event != "chat.error" for event in events)


@pytest.mark.asyncio
async def test_dashboard_stream_emits_only_unstreamed_interim_text_without_repeating_complete() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-interim", "resumed": "rs-interim"}},
            {"$response": {"status": "submitted"}},
            _event("message.start", "live-interim", {}),
            _event("message.delta", "live-interim", {"text": "already visible"}),
            _event(
                "message.interim",
                "live-interim",
                {"text": "already visible", "already_streamed": True},
            ),
            _event(
                "message.interim",
                "live-interim",
                {"text": "verification fallback", "already_streamed": False},
            ),
            _event(
                "message.complete",
                "live-interim",
                {
                    "text": "verification fallback",
                    "status": "complete",
                    "response_previewed": True,
                },
            ),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-interim", message="verify"
        )
    ]

    streamed = [event.data["delta"] for event in events if event.event == "chat.message.delta"]
    activity = [event.data["delta"] for event in events if event.event == "chat.activity.delta"]
    assert streamed == ["already visible"]
    assert activity == ["verification fallback"]
    assert next(event for event in events if event.event == "chat.message.completed").data == {
        "text": "verification fallback",
        "status": "complete",
        "response_previewed": True,
    }
@pytest.mark.asyncio
async def test_dashboard_treats_failure_like_subagent_complete_as_failed_run() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-failed-child", "resumed": "rs-failed-child"}},
            {"$response": {"status": "submitted"}},
            _event(
                "subagent.complete",
                "live-failed-child",
                {
                    "subagent_id": "sub-failed",
                    "status": "timeout",
                    "preview": "Timed out after 30s",
                    "summary": "",
                },
            ),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-failed-child", message="delegate"
        )
    ]

    subagent = next(event for event in events if event.event == "chat.subagent.event")
    assert subagent.data["type"] == "subagent.complete"
    assert subagent.data["status"] == "failed"
    assert any(event.event == "chat.error" for event in events)
    assert events[-1].event == "chat.done"
    assert events[-1].data["status"] == "failed"


@pytest.mark.asyncio
async def test_dashboard_submits_prompt_before_exposing_run_started() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-fast", "resumed": "rs-fast"}},
            {"$response": {"status": "submitted"}},
            _event("message.complete", "live-fast", {"text": "done"}),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    boundaries: list[str] = []

    async def provider_ready(live_session_id: str) -> None:
        assert live_session_id == "live-fast"
        boundaries.append("provider_ready")

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    stream = adapter.stream_session_chat(
        _endpoint(),
        runtime_session_id="rs-fast",
        message="hello",
        runtime_provider_run_id_sink=provider_ready,
        prompt_submit_started_sink=lambda: boundaries.append(
            "prompt_submit_started"
        ),
    )

    first = await anext(stream)

    assert first.event == "chat.run.started"
    assert boundaries == ["provider_ready", "prompt_submit_started"]
    assert [request["method"] for request in ws.sent] == ["session.resume", "prompt.submit"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_dashboard_skips_model_switch_when_resumed_session_already_matches() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "session_id": "live-model-same",
                    "resumed": "rs-model-same",
                    "info": {"model": "17"},
                }
            },
            {"$response": {"status": "submitted"}},
            _event("message.complete", "live-model-same", {"text": "done"}),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-model-same",
            message="hello",
            expected_model="17",
        )
    ]

    assert events[-1].event == "chat.done"
    assert [request["method"] for request in ws.sent] == ["session.resume", "prompt.submit"]


@pytest.mark.asyncio
async def test_dashboard_uses_matching_expert_provider_without_model_switch() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "session_id": "live-expert-model-same",
                    "resumed": "rs-expert-model-same",
                    "info": {"model": "17", "provider": "custom:platform"},
                }
            },
            {"$response": {"status": "submitted"}},
            _event(
                "message.complete",
                "live-expert-model-same",
                {"text": "done"},
            ),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-expert-model-same",
            message="hello",
            expected_model="17",
            hermes_profile_name="expert-7",
            runtime_agent_profile_id=7,
        )
    ]

    assert events[-1].event == "chat.done"
    assert [request["method"] for request in ws.sent] == [
        "session.resume",
        "prompt.submit",
    ]
    assert ws.sent[0]["params"]["profile"] == "expert-7"


@pytest.mark.asyncio
async def test_dashboard_rejects_resumed_model_mismatch_before_prompt() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "session_id": "live-model-switch",
                    "resumed": "rs-model-switch",
                    "info": {"model": "10"},
                }
            },
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    with pytest.raises(HermesProtocolError, match="session route mismatch"):
        _ = [
            event
            async for event in adapter.stream_session_chat(
                _endpoint(),
                runtime_session_id="rs-model-switch",
                message="hello",
                expected_model="17",
            )
        ]

    assert [request["method"] for request in ws.sent] == ["session.resume"]


@pytest.mark.asyncio
async def test_dashboard_does_not_submit_prompt_when_route_validation_fails() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "session_id": "live-route-confirm",
                    "resumed": "rs-model-confirm",
                    "info": {"model": "17", "provider": "custom:stale-route"},
                }
            },
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    with pytest.raises(HermesProtocolError, match="session route mismatch"):
        _ = [
            event
            async for event in adapter.stream_session_chat(
                _endpoint(),
                runtime_session_id="rs-model-confirm",
                message="hello",
                expected_model="17",
                expected_provider="custom:runtime-expected-route",
            )
        ]

    assert [request["method"] for request in ws.sent] == ["session.resume"]


@pytest.mark.asyncio
async def test_dashboard_complete_only_emits_unstreamed_answer_suffix_once() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-2", "resumed": "rs-102"}},
            {"$response": {"status": "submitted"}},
            _event("message.delta", "live-2", {"text": "partial"}),
            _event("message.complete", "live-2", {"text": "partial suffix"}),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-102", message="hello"
        )
    ]

    deltas = [event.data["delta"] for event in events if event.event == "chat.message.delta"]
    assert deltas == ["partial", " suffix"]


@pytest.mark.asyncio
async def test_dashboard_prepares_session_scoped_workspace_before_prompt() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"stdout": "", "stderr": "", "code": 0}},
            {"$response": {"session_id": "live-workspace", "resumed": "rs-workspace"}},
            {"$response": {"cwd": "/data/workspace/sessions/rs-workspace"}},
            {"$response": {"status": "submitted"}},
            _event("message.complete", "live-workspace", {"text": "done"}),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
    )
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-workspace", message="create file"
        )
    ]

    assert [request["method"] for request in ws.sent] == [
        "shell.exec",
        "session.resume",
        "session.cwd.set",
        "prompt.submit",
    ]
    workspace_command = ws.sent[0]["params"]["command"]
    assert workspace_command.startswith(
        "mkdir -p -m 2770 -- /data/workspace/sessions/rs-workspace "
        "/data/workspace/sessions/rs-workspace/.work"
    )
    assert "platform_supervisor.workspace_probe" in workspace_command
    assert workspace_command.count("--directory") == 2
    assert ws.sent[2]["params"] == {
        "session_id": "live-workspace",
        "cwd": "/data/workspace/sessions/rs-workspace",
    }
    submitted = ws.sent[3]["params"]["text"]
    assert submitted.startswith("create file\n\n")
    assert "<platform_workspace>" in submitted
    assert "本次任务文件目录：/data/workspace/sessions/rs-workspace" in submitted
    assert "最终交付给用户的文件直接写入本次任务文件目录。" in submitted
    assert (
        "生成脚本、临时图片、缓存、解压内容和检查文件必须写入本目录下的 .work/ 子目录。"
        in submitted
    )
    assert "最终答复不得将 .work/ 内的文件作为用户交付物。" in submitted
    assert "/data/workspace 是共享挂载根，不是本次任务的交付目录。" in submitted
    assert submitted.index("<platform_workspace>") < submitted.index("<platform_progress_contract>")
    assert events[-1].event == "chat.done"


@pytest.mark.asyncio
async def test_dashboard_rejects_unwritable_session_workspace_before_resume() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "stdout": "",
                    "stderr": "managed Session workspace is not writable",
                    "code": 1,
                }
            },
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        session_workspace_root="/data/workspace",
    )

    with pytest.raises(HermesProtocolError, match="workspace creation failed"):
        async for _event_item in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-unwritable", message="create file"
        ):
            pass

    assert [request["method"] for request in ws.sent] == ["shell.exec"]


def test_dashboard_platform_contract_keeps_progress_only_shape_without_workspace() -> None:
    adapter = HermesDashboardProtocolAdapter()

    assert adapter._message_with_platform_contract("hello") == adapter._message_with_progress_contract("hello")


def test_dashboard_appends_server_trusted_user_context_after_user_input() -> None:
    adapter = HermesDashboardProtocolAdapter()

    message = adapter._message_with_platform_contract(
        "查询我的工单",
        trusted_context={
            "org_id": 1,
            "platform_user_id": 8,
            "user_name": "张三",
            "user_code": "E0008",
            "chat_type": "group",
        },
    )

    assert message.startswith("查询我的工单\n\n")
    assert "<platform_trusted_user_context>" in message
    assert '"platform_user_id":8' in message
    assert '"user_code":"E0008"' in message
    assert message.index("<platform_trusted_user_context>") < (
        message.index("<platform_progress_contract>")
    )


def test_dashboard_v2_session_contract_is_sent_only_when_requested() -> None:
    adapter = HermesDashboardProtocolAdapter()
    workspace = "/data/workspace/sessions/app-agent-25-session"
    digest = adapter.platform_session_contract_digest(workspace)
    trusted_context = {
        "org_id": 1,
        "platform_user_id": 8,
        "user_name": "张三",
    }

    first = adapter._message_with_platform_contract(
        "首次消息",
        workspace,
        trusted_context=trusted_context,
        platform_contract_version=2,
        platform_contract_digest=digest,
        include_platform_contract=True,
    )
    followup = adapter._message_with_platform_contract(
        "追问",
        workspace,
        trusted_context=trusted_context,
        platform_contract_version=2,
        platform_contract_digest=digest,
        include_platform_contract=False,
    )

    assert (
        f'<platform_session_contract version="2" digest="{digest}">'
        in first
    )
    assert "<platform_workspace>" in first
    assert "<platform_progress_contract>" in first
    assert "<platform_trusted_user_context>" in first
    assert first.index("<platform_trusted_user_context>") < first.index(
        "<platform_session_contract"
    )
    assert followup.startswith("追问\n\n<platform_trusted_user_context>")
    assert "<platform_session_contract" not in followup
    assert "<platform_workspace>" not in followup
    assert "<platform_progress_contract>" not in followup


def test_dashboard_v2_session_contract_digest_changes_with_workspace() -> None:
    adapter = HermesDashboardProtocolAdapter()

    assert adapter.platform_session_contract_digest(
        "/data/workspace/sessions/one"
    ) == adapter.platform_session_contract_digest(
        "/data/workspace/sessions/one"
    )
    assert adapter.platform_session_contract_digest(
        "/data/workspace/sessions/one"
    ) != adapter.platform_session_contract_digest(
        "/data/workspace/sessions/two"
    )


@pytest.mark.asyncio
async def test_dashboard_reconnects_without_resubmitting_prompt_and_reconciles_inflight_text() -> None:
    first = FakeWebSocket(
        [
            {"$response": {"session_id": "live-reconnect", "resumed": "rs-reconnect"}},
            {"$response": {"status": "submitted"}},
            _event("message.delta", "live-reconnect", {"text": "partial"}),
            {"$disconnect": True},
        ]
    )
    second = FakeWebSocket(
        [
            {
                "$response": {
                    "session_id": "live-reconnect",
                    "resumed": "rs-reconnect",
                    "running": True,
                    "status": "streaming",
                    "inflight": {"assistant": "partial recovered", "streaming": True},
                }
            },
            _event("message.delta", "live-reconnect", {"text": " final"}),
            _event("message.complete", "live-reconnect", {"text": "partial recovered final"}),
        ]
    )
    sockets = iter([first, second])

    @asynccontextmanager
    async def connector(_endpoint):
        yield next(sockets)

    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        reconnect_attempts=2,
        reconnect_backoff_seconds=0,
    )
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-reconnect", message="hello"
        )
    ]

    assert [request["method"] for request in first.sent] == ["session.resume", "prompt.submit"]
    assert [request["method"] for request in second.sent] == ["session.resume"]
    assert first.sent[0]["params"]["omit_messages"] is True
    assert "omit_messages" not in second.sent[0]["params"]
    assert [event.event for event in events].count("chat.run.started") == 1
    assert "chat.transport.reconnecting" in [event.event for event in events]
    assert "chat.transport.resumed" in [event.event for event in events]
    assert [event.data["delta"] for event in events if event.event == "chat.activity.delta"] == [
        " recovered",
    ]
    assert [event.data["delta"] for event in events if event.event == "chat.message.delta"] == [
        "partial",
        " final",
    ]
    recovered = next(event for event in events if event.event == "chat.activity.delta")
    assert recovered.data["interim"] is True
    assert recovered.data["source"] == "recovered"
    assert events[-1].event == "chat.done"


@pytest.mark.asyncio
async def test_dashboard_reconnect_recovers_terminal_answer_from_resume_history() -> None:
    first = FakeWebSocket(
        [
            {"$response": {"stdout": "", "stderr": "", "code": 0}},
            {"$response": {"session_id": "live-history", "resumed": "rs-history"}},
            {"$response": {"cwd": "/data/workspace/sessions/rs-history"}},
            {"$response": {"status": "submitted"}},
            _event("message.delta", "live-history", {"text": "partial"}),
            {"$disconnect": True},
        ]
    )
    second = FakeWebSocket(
        [
            {"$response": {"stdout": "", "stderr": "", "code": 0}},
            {
                "$response": {
                    "session_id": "live-history",
                    "resumed": "rs-history",
                    "running": False,
                    "status": "idle",
                    "messages": [
                        {
                            "role": "user",
                            "text": HermesDashboardProtocolAdapter._message_with_platform_contract(
                                "hello", "/data/workspace/sessions/rs-history"
                            ),
                        },
                        {"role": "assistant", "text": "partial complete"},
                    ],
                }
            }
        ]
    )
    sockets = iter([first, second])

    @asynccontextmanager
    async def connector(_endpoint):
        yield next(sockets)

    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        reconnect_backoff_seconds=0,
        session_workspace_root="/data/workspace",
    )
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-history", message="hello"
        )
    ]

    assert [event.data["delta"] for event in events if event.event == "chat.message.delta"] == [
        "partial",
        " complete",
    ]
    assert events[-2].event == "chat.run.completed"
    assert events[-1].event == "chat.done"


@pytest.mark.asyncio
async def test_dashboard_idle_poll_recovers_completion_after_revision_handoff() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-handoff", "resumed": "rs-handoff"}},
            {"$response": {"status": "submitted"}},
            {"$wait": 1},
            {
                "$response": {
                    "session_id": "live-handoff",
                    "running": False,
                    "status": "idle",
                    "messages": [
                        {"role": "user", "text": "create ppt"},
                        {"role": "assistant", "text": "PPT created"},
                    ],
                }
            },
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        resume_poll_seconds=0.01,
    )
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-handoff", message="create ppt"
        )
    ]

    assert [request["method"] for request in ws.sent] == [
        "session.resume",
        "prompt.submit",
        "session.resume",
    ]
    assert [event.event for event in events][-3:] == [
        "chat.message.completed",
        "chat.run.completed",
        "chat.done",
    ]


@pytest.mark.asyncio
async def test_dashboard_idle_poll_keeps_expert_profile_on_checkpoint_resume() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-expert", "resumed": "rs-expert"}},
            {"$response": {"status": "submitted"}},
            {"$wait": 1},
            {
                "$response": {
                    "session_id": "live-expert",
                    "running": False,
                    "status": "idle",
                    "messages": [
                        {"role": "user", "text": "invoice redaction"},
                        {"role": "assistant", "text": "done"},
                    ],
                }
            },
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        resume_poll_seconds=0.01,
    )
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-expert",
            message="invoice redaction",
            hermes_profile_name="expert-69",
            runtime_agent_profile_id=69,
        )
    ]

    resume_requests = [
        request for request in ws.sent if request["method"] == "session.resume"
    ]
    assert len(resume_requests) == 2
    assert all(request["params"]["profile"] == "expert-69" for request in resume_requests)
    assert [event.event for event in events][-3:] == [
        "chat.message.completed",
        "chat.run.completed",
        "chat.done",
    ]


@pytest.mark.asyncio
async def test_dashboard_idle_poll_recovers_completion_from_http_history_when_resume_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-http-history", "resumed": "rs-http-history"}},
            {"$response": {"status": "submitted"}},
            {"$wait": 1},
            {
                "$response": {
                    "session_id": "live-http-history",
                    "running": False,
                    "status": "idle",
                    "inflight": None,
                    "messages": [
                        {"role": "user", "text": "switch model"},
                        {"role": "assistant", "text": "model switched"},
                    ],
                }
            },
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        resume_poll_seconds=0.01,
    )
    history_requests: list[dict[str, object]] = []

    async def get_latest_session_messages(_endpoint, **kwargs):
        history_requests.append(kwargs)
        return {
            "messages": [
                {
                    "role": "user",
                    "content": adapter._message_with_platform_contract("你好"),
                },
                {
                    "role": "assistant",
                    "content": "你好！有什么可以帮你？",
                },
            ]
        }

    monkeypatch.setattr(
        adapter,
        "_get_latest_session_messages",
        get_latest_session_messages,
    )

    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-http-history",
            message="你好",
        )
    ]

    assert history_requests == [
        {
            "runtime_session_id": "rs-http-history",
            "hermes_profile_name": "default",
            "initiating_user_message": "你好",
            "workspace": None,
        }
    ]
    assert [event.data["delta"] for event in events if event.event == "chat.message.delta"] == [
        "你好！有什么可以帮你？"
    ]
    assert [event.event for event in events][-3:] == [
        "chat.message.completed",
        "chat.run.completed",
        "chat.done",
    ]


@pytest.mark.asyncio
async def test_dashboard_poll_recovers_http_history_despite_non_terminal_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-heartbeat", "resumed": "rs-heartbeat"}},
            {"$response": {"status": "submitted"}},
            {"$wait": 0.04},
            _event("session.info", "live-heartbeat", {"status": "streaming"}),
            {"$wait": 0.04},
            _event("session.info", "live-heartbeat", {"status": "streaming"}),
            {"$wait": 0.04},
            _event("session.info", "live-heartbeat", {"status": "idle"}),
            {
                "$response": {
                    "session_id": "live-heartbeat",
                    "running": False,
                    "status": "idle",
                    "inflight": None,
                    # The process-local resume snapshot can lag the canonical
                    # HTTP history after a gateway history-version conflict.
                    "messages": [{"role": "user", "text": "你好"}],
                }
            },
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(
        connector=connector,
        resume_poll_seconds=0.05,
    )

    async def get_latest_session_messages(_endpoint, **_kwargs):
        return {
            "messages": [
                {
                    "role": "user",
                    "content": adapter._message_with_platform_contract("你好"),
                },
                {"role": "assistant", "content": "你好！有什么可以帮你？"},
            ]
        }

    monkeypatch.setattr(
        adapter,
        "_get_latest_session_messages",
        get_latest_session_messages,
    )

    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(),
            runtime_session_id="rs-heartbeat",
            message="你好",
        )
    ]

    assert [request["method"] for request in ws.sent] == [
        "session.resume",
        "prompt.submit",
        "session.resume",
    ]
    assert [event.data["delta"] for event in events if event.event == "chat.message.delta"] == [
        "你好！有什么可以帮你？"
    ]
    assert [event.event for event in events][-3:] == [
        "chat.message.completed",
        "chat.run.completed",
        "chat.done",
    ]


def test_dashboard_resume_running_reads_nested_desktop_info() -> None:
    assert HermesDashboardProtocolAdapter._resume_is_running(
        {"info": {"running": True, "status": "streaming"}}
    ) is True


@pytest.mark.parametrize(
    "checkpoint",
    [
        {"running": True, "status": "idle", "inflight": None},
        {"running": False, "status": "streaming", "inflight": None},
        {
            "running": False,
            "status": "idle",
            "inflight": {"user": "你好", "streaming": False},
        },
        {
            "running": False,
            "status": "idle",
            "inflight": None,
            "info": {"usage": {"active_subagents": 1}},
        },
        {"running": False, "inflight": None},
    ],
)
def test_dashboard_http_history_recovery_requires_fully_idle_checkpoint(
    checkpoint: dict[str, object],
) -> None:
    assert (
        HermesDashboardProtocolAdapter._resume_allows_history_recovery(checkpoint)
        is False
    )


@pytest.mark.asyncio
async def test_dashboard_parks_for_background_delegation_then_resumes_before_terminal() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-bg", "resumed": "rs-bg"}},
            {"$response": {"status": "submitted"}},
            _event(
                "tool.complete",
                "live-bg",
                {
                    "name": "delegate_task",
                    "tool_id": "call-bg",
                    "result": {
                        "mode": "background",
                        "status": "dispatched",
                        "delegation_id": "deleg-bg",
                    },
                },
            ),
            _event(
                "message.complete",
                "live-bg",
                {"text": "", "status": "complete", "usage": {"active_subagents": 1}},
            ),
            _event(
                "subagent.start",
                "live-bg",
                {"subagent_id": "sub-bg", "goal": "search news", "status": "running"},
            ),
            _event(
                "subagent.progress",
                "live-bg",
                {"subagent_id": "sub-bg", "goal": "search news", "text": "Searching", "status": "running"},
            ),
            _event(
                "subagent.complete",
                "live-bg",
                {"subagent_id": "sub-bg", "goal": "search news", "summary": "found", "status": "completed"},
            ),
            _event("message.start", "live-bg", {}),
            _event("message.delta", "live-bg", {"text": "final news"}),
            _event(
                "message.complete",
                "live-bg",
                {"text": "final news", "status": "complete", "usage": {"active_subagents": 0}},
            ),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-bg", message="search"
        )
    ]

    names = [event.event for event in events]
    assert names.count("chat.done") == 1
    assert names.count("chat.run.completed") == 1
    assert names.index("chat.turn.parked") < names.index("chat.subagent.event")
    assert names.index("chat.subagent.event") < names.index("chat.turn.resumed")
    assert names.index("chat.turn.resumed") < names.index("chat.message.delta")
    assert [event.data["type"] for event in events if event.event == "chat.subagent.event"] == [
        "subagent.start",
        "subagent.progress",
        "subagent.complete",
    ]
    assert next(event for event in events if event.event == "chat.turn.parked").data["active_subagents"] == 1


@pytest.mark.asyncio
async def test_dashboard_buffers_interim_commentary_into_progress_until_subagents_finish() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-progress", "resumed": "rs-progress"}},
            {"$response": {"status": "submitted"}},
            _event("message.start", "live-progress", {}),
            _event(
                "message.delta",
                "live-progress",
                {"text": "<platform_progress>正在检查演示文稿的版式与可读性</platform_progress>"},
            ),
            _event(
                "message.complete",
                "live-progress",
                {"text": "<platform_progress>正在检查演示文稿的版式与可读性</platform_progress>", "status": "complete", "usage": {"active_subagents": 1}},
            ),
            _event("message.start", "live-progress", {}),
            _event("message.delta", "live-progress", {"text": "已完成《水蒸发原理》PPT。"}),
            _event(
                "message.complete",
                "live-progress",
                {"text": "已完成《水蒸发原理》PPT。", "status": "complete", "usage": {"active_subagents": 0}},
            ),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-progress", message="make a PPT"
        )
    ]

    assert [event.event for event in events if event.event in {"chat.progress.delta", "chat.message.delta"}] == [
        "chat.progress.delta",
        "chat.message.delta",
    ]
    assert next(event for event in events if event.event == "chat.progress.delta").data["delta"] == "正在检查演示文稿的版式与可读性"
    assert next(event for event in events if event.event == "chat.message.delta").data["delta"] == "已完成《水蒸发原理》PPT。"
    assert next(event for event in events if event.event == "chat.message.completed").data["interim"] is True


@pytest.mark.asyncio
async def test_dashboard_terminal_error_emits_chat_error_and_not_completed() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-error", "resumed": "rs-error"}},
            {"$response": {"status": "submitted"}},
            _event(
                "message.complete",
                "live-error",
                {"text": "Error: upstream failed", "status": "error", "usage": {"active_subagents": 0}},
            ),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-error", message="hello"
        )
    ]

    assert [event.event for event in events] == [
        "chat.run.started",
        "chat.message.completed",
        "chat.usage",
        "chat.error",
        "chat.done",
    ]
    assert events[3].data["message"] == "Error: upstream failed"


@pytest.mark.asyncio
async def test_dashboard_marks_completed_tool_payload_with_result_error_as_failed() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-tool-error", "resumed": "rs-tool-error"}},
            {"$response": {"status": "submitted"}},
            _event(
                "tool.complete",
                "live-tool-error",
                {
                    "name": "write_file",
                    "tool_id": "call-write",
                    "args": {"path": "/root/output.txt", "token": "must-not-leak"},
                    "result": {"error": "Write denied", "path": "/root/output.txt"},
                    "token": "must-not-leak",
                },
            ),
            _event("message.complete", "live-tool-error", {"text": "unable"}),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    events = [
        event
        async for event in adapter.stream_session_chat(
            _endpoint(), runtime_session_id="rs-tool-error", message="write"
        )
    ]

    tool = next(event for event in events if event.event == "chat.tool.event")
    assert tool.data == {
        "name": "write_file",
        "status": "failed",
        "callId": "call-write",
        "hasStableCallId": True,
        "input": '{\n  "path": "<path>/output.txt",\n  "token": "••••"\n}',
        "result": '{\n  "error": "Write denied",\n  "path": "<path>/output.txt"\n}',
    }


@pytest.mark.asyncio
async def test_dashboard_observes_successful_skill_create_without_persisting_content() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-skill", "resumed": "rs-skill"}},
            {"$response": {"status": "submitted"}},
            _event(
                "tool.complete",
                "live-skill",
                {
                    "name": "skill_manage",
                    "tool_id": "call-skill",
                    "args": {
                        "action": "create",
                        "name": "test-skill",
                        "content": "top-secret skill body",
                    },
                    "result": {"success": True, "content": "top-secret result"},
                    "duration_s": 1.5,
                },
            ),
            _event("message.complete", "live-skill", {"text": "done"}),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    events = [
        event
        async for event in HermesDashboardProtocolAdapter(connector=connector).stream_session_chat(
            _endpoint(), runtime_session_id="rs-skill", message="create"
        )
    ]

    tool = next(event for event in events if event.event == "chat.tool.event")
    observed = tool.internal
    assert observed is not None
    assert observed.tool_call_id == "call-skill"
    assert observed.action == "create"
    assert observed.skill_name == "test-skill"
    assert observed.duration_ms == 1500
    assert "top-secret" not in str(tool.data)


@pytest.mark.asyncio
async def test_dashboard_skill_observation_requires_explicit_success() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-skill-failed", "resumed": "rs-skill-failed"}},
            {"$response": {"status": "submitted"}},
            _event(
                "tool.complete",
                "live-skill-failed",
                {
                    "name": "skill_manage",
                    "tool_id": "call-skill-failed",
                    "args": {"action": "delete", "name": "test-skill"},
                    "result": {"success": False},
                },
            ),
            _event("message.complete", "live-skill-failed", {"text": "done"}),
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    events = [
        event
        async for event in HermesDashboardProtocolAdapter(connector=connector).stream_session_chat(
            _endpoint(), runtime_session_id="rs-skill-failed", message="delete"
        )
    ]
    tool = next(event for event in events if event.event == "chat.tool.event")
    assert tool.internal is None


@pytest.mark.asyncio
async def test_dashboard_does_not_retry_prompt_from_rpc_error_message() -> None:
    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-cold", "resumed": "rs-cold"}},
            {"$error": "agent initialization timed out"},
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    with pytest.raises(HermesProtocolError) as captured:
        _ = [
            event
            async for event in adapter.stream_session_chat(
                _endpoint(), runtime_session_id="rs-cold", message="hello"
            )
        ]

    assert [request["method"] for request in ws.sent] == [
        "session.resume",
        "prompt.submit",
    ]
    assert captured.value.rpc_code == 5032
    assert captured.value.rpc_method == "prompt.submit"


@pytest.mark.asyncio
async def test_dashboard_stop_uses_session_interrupt_rpc() -> None:
    ws = FakeWebSocket([{"$response": {"status": "interrupted"}}])

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)

    result = await adapter.stop_run(_endpoint(), runtime_provider_run_id="live-stop")

    assert result == {"status": "interrupted"}
    assert ws.sent[0]["method"] == "session.interrupt"
    assert ws.sent[0]["params"] == {"session_id": "live-stop"}


@pytest.mark.asyncio
async def test_redirect_recovery_peer_resumes_interrupts_and_proves_settle() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "session_id": "live-recovery",
                    "running": True,
                    "status": "streaming",
                    "inflight": {"streaming": True},
                }
            },
            {"$response": {"status": "interrupted"}},
            {
                "$response": {
                    "session_id": "live-recovery",
                    "running": False,
                    "status": "idle",
                    "inflight": None,
                    "queued": [],
                }
            },
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    result = await adapter.interrupt_and_settle_redirect(
        _endpoint(),
        runtime_session_id="stored-recovery",
        hermes_profile_name="expert-9",
        timeout_seconds=1,
    )

    assert result.interrupt_sent is True
    assert [request["method"] for request in ws.sent] == [
        "session.resume",
        "session.interrupt",
        "session.resume",
    ]
    assert ws.sent[0]["params"]["profile"] == "expert-9"
    assert ws.sent[1]["params"] == {"session_id": "live-recovery"}


@pytest.mark.asyncio
async def test_redirect_boot_recovery_skips_interrupt_when_resume_is_settled() -> None:
    ws = FakeWebSocket(
        [
            {
                "$response": {
                    "session_id": "live-recovery",
                    "running": False,
                    "status": "idle",
                    "inflight": None,
                    "queued": [],
                }
            }
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)
    result = await adapter.interrupt_and_settle_redirect(
        _endpoint(),
        runtime_session_id="stored-recovery",
        timeout_seconds=1,
    )

    assert result.interrupt_sent is False
    assert [request["method"] for request in ws.sent] == ["session.resume"]


@pytest.mark.asyncio
async def test_dashboard_stop_interrupt_rpc_is_bounded() -> None:
    ws = StallOnMethodWebSocket("session.interrupt")

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    adapter = HermesDashboardProtocolAdapter(connector=connector)

    with pytest.raises(
        HermesProtocolError,
        match="RPC session.interrupt timed out",
    ):
        await asyncio.wait_for(
            adapter.stop_run(
                replace(_endpoint(), timeout_seconds=0.1),
                runtime_provider_run_id="live-stop",
            ),
            timeout=1,
        )

    assert [request["method"] for request in ws.sent] == [
        "session.interrupt"
    ]


@pytest.mark.asyncio
async def test_dashboard_protocol_keeps_session_management_off_gateway_cold_path(
    monkeypatch,
) -> None:
    class FakeResponse:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class FakeDashboardPool:
        def __init__(self) -> None:
            self.calls = []
            self.responses = [
                FakeResponse(
                    200,
                    {
                        "ok": True,
                        "imported": 1,
                        "skipped": 0,
                        "imported_ids": ["rs-103"],
                        "skipped_ids": [],
                    },
                ),
                FakeResponse(
                    200,
                    {
                        "session_id": "rs-103",
                        "messages": [],
                        "pagination": {
                            "limit": 500,
                            "offset": 0,
                            "order": "oldest",
                            "returned": 0,
                        },
                    },
                ),
                FakeResponse(200, {"ok": True, "title": "Release plan"}),
                FakeResponse(200, {"ok": True}),
            ]

        async def request(self, endpoint, method, path, **kwargs):
            self.calls.append((endpoint, method, path, kwargs))
            return self.responses.pop(0)

    dashboard_pool = FakeDashboardPool()
    monkeypatch.setattr(dashboard_protocol, "_dashboard_http_sessions", dashboard_pool)
    adapter = HermesDashboardProtocolAdapter(
        connector=None,
        session_workspace_root="/data/workspace",
    )

    created = await adapter.create_session(
        _endpoint(),
        runtime_session_id="rs-103",
        model="7",
        title="Release plan",
    )
    history = await adapter.get_session_messages(
        _endpoint(), runtime_session_id="rs-103"
    )
    renamed = await adapter.rename_session(
        _endpoint(), runtime_session_id="rs-103", title="Release plan"
    )
    deleted = await adapter.delete_session(
        _endpoint(), runtime_session_id="rs-103"
    )

    assert created["session"]["id"] == "rs-103"
    assert history == {"session_id": "rs-103", "messages": []}
    assert renamed["session"]["title"] == "Release plan"
    assert deleted == {"ok": True}
    assert [
        (method, path)
        for _endpoint_value, method, path, _kwargs in dashboard_pool.calls
    ] == [
        ("POST", "/api/sessions/import"),
        ("GET", "/api/sessions/rs-103/messages?limit=500&offset=0&order=oldest"),
        ("PATCH", "/api/sessions/rs-103"),
        ("DELETE", "/api/sessions/rs-103"),
    ]
    import_payload = dashboard_pool.calls[0][3]["json"]["sessions"][0]
    assert import_payload == {
        "id": "rs-103",
        "source": "relay",
        "model": "7",
        "title": "Release plan [rs-103]",
        "messages": [],
    }
    assert dashboard_pool.calls[2][3]["json"] == {
        "title": "Release plan [rs-103]"
    }


@pytest.mark.asyncio
async def test_dashboard_session_create_makes_repeated_visible_titles_unique(
    monkeypatch,
) -> None:
    class FakeResponse:
        status_code = 200

        def __init__(self, runtime_session_id: str) -> None:
            self.runtime_session_id = runtime_session_id

        def json(self):
            return {
                "ok": True,
                "imported": 1,
                "skipped": 0,
                "imported_ids": [self.runtime_session_id],
                "skipped_ids": [],
            }

    class FakeDashboardPool:
        def __init__(self) -> None:
            self.calls = []

        async def request(self, endpoint, method, path, **kwargs):
            self.calls.append((endpoint, method, path, kwargs))
            runtime_session_id = kwargs["json"]["sessions"][0]["id"]
            return FakeResponse(runtime_session_id)

    dashboard_pool = FakeDashboardPool()
    monkeypatch.setattr(dashboard_protocol, "_dashboard_http_sessions", dashboard_pool)
    adapter = HermesDashboardProtocolAdapter(connector=None)

    await adapter.create_session(
        _endpoint(),
        runtime_session_id="rs-first",
        title="重复标题",
    )
    await adapter.create_session(
        _endpoint(),
        runtime_session_id="rs-second",
        title="重复标题",
    )

    titles = [
        call[3]["json"]["sessions"][0]["title"] for call in dashboard_pool.calls
    ]
    assert titles == ["重复标题 [rs-first]", "重复标题 [rs-second]"]


@pytest.mark.asyncio
async def test_dashboard_session_create_persists_runtime_provider_for_first_resume(
    monkeypatch,
) -> None:
    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "ok": True,
                "imported": 1,
                "skipped": 0,
                "imported_ids": ["rs-runtime-route"],
                "skipped_ids": [],
            }

    class FakeDashboardPool:
        def __init__(self) -> None:
            self.calls = []

        async def request(self, endpoint, method, path, **kwargs):
            self.calls.append((endpoint, method, path, kwargs))
            return FakeResponse()

    dashboard_pool = FakeDashboardPool()
    monkeypatch.setattr(dashboard_protocol, "_dashboard_http_sessions", dashboard_pool)
    adapter = HermesDashboardProtocolAdapter(connector=None)

    created = await adapter.create_session(
        _endpoint(),
        runtime_session_id="rs-runtime-route",
        model="deepseek-v4-flash",
        provider="custom:runtime-10001-route",
    )

    assert created["session"] == {
        "id": "rs-runtime-route",
        "model": "deepseek-v4-flash",
        "provider": "custom:runtime-10001-route",
        "title": None,
    }
    assert dashboard_pool.calls[0][3]["json"]["sessions"][0] == {
        "id": "rs-runtime-route",
        "source": "relay",
        "messages": [],
        "title": "新会话 [rs-runtime-route]",
        "model": "deepseek-v4-flash",
        "model_config": {
            "model": "deepseek-v4-flash",
            "provider": "custom:runtime-10001-route",
        },
    }


@pytest.mark.asyncio
async def test_dashboard_session_create_repairs_skipped_legacy_runtime_metadata(
    monkeypatch,
) -> None:
    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "ok": True,
                "imported": 0,
                "skipped": 1,
                "imported_ids": [],
                "skipped_ids": ["rs-legacy-runtime-route"],
            }

    class FakeDashboardPool:
        async def request(self, _endpoint, _method, _path, **_kwargs):
            return FakeResponse()

    repaired: list[dict[str, str]] = []

    async def repair(**kwargs: str) -> None:
        repaired.append(kwargs)

    monkeypatch.setattr(dashboard_protocol, "_dashboard_http_sessions", FakeDashboardPool())
    adapter = HermesDashboardProtocolAdapter(connector=None)
    monkeypatch.setattr(adapter, "_repair_existing_session_model", repair)

    await adapter.create_session(
        _endpoint(),
        runtime_session_id="rs-legacy-runtime-route",
        model="deepseek-v4-flash",
        provider="custom:runtime-10001-route",
        hermes_profile_name="expert-73",
    )

    assert repaired == [{
        "runtime_session_id": "rs-legacy-runtime-route",
        "model": "deepseek-v4-flash",
        "provider": "custom:runtime-10001-route",
        "hermes_profile_name": "expert-73",
    }]


def test_legacy_session_metadata_repair_uses_the_fixed_hermes_child_helper(
    monkeypatch,
    tmp_path,
) -> None:
    completed = types.SimpleNamespace(returncode=0, stdout=b'{"ok":true}')
    runner = Mock(return_value=completed)
    monkeypatch.setattr(dashboard_protocol.subprocess, "run", runner)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    HermesDashboardProtocolAdapter._repair_existing_session_model_blocking(
        "rs-legacy-runtime-route",
        "deepseek-v4-flash",
        "custom:runtime-current",
        "expert-73",
    )

    call_args, options = runner.call_args
    command = call_args[0]
    assert command[:9] == [
        "/opt/hermes/.venv/bin/platform-hermes-child-exec",
        "--uid",
        "10001",
        "--gid",
        "10001",
        "--supplemental-gid",
        "1000",
        "--",
        "/opt/hermes/.venv/bin/python",
    ]
    assert command[-1] == "/opt/hermes-platform/bin/hermes-session-metadata-helper.py"
    assert json.loads(options["input"]) == {
        "contract_version": "hermes-session-metadata-repair-v1",
        "runtime_session_id": "rs-legacy-runtime-route",
        "model": "deepseek-v4-flash",
        "provider": "custom:runtime-current",
        "hermes_profile_name": "expert-73",
    }
    assert options["env"] == {
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path),
        "HERMES_SKIP_CONFIG_MIGRATION": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def test_profile_refresh_clears_prompt_through_the_same_hermes_child_helper(
    monkeypatch,
    tmp_path,
) -> None:
    completed = types.SimpleNamespace(returncode=0, stdout=b'{"ok":true}')
    runner = Mock(return_value=completed)
    monkeypatch.setattr(dashboard_protocol.subprocess, "run", runner)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    HermesDashboardProtocolAdapter._repair_existing_session_model_blocking(
        "rs-profile-refresh",
        "deepseek-v4-flash",
        "custom:runtime-current",
        "expert-73",
        True,
    )

    _call_args, options = runner.call_args
    assert json.loads(options["input"]) == {
        "contract_version": "hermes-session-metadata-repair-v2",
        "runtime_session_id": "rs-profile-refresh",
        "model": "deepseek-v4-flash",
        "provider": "custom:runtime-current",
        "hermes_profile_name": "expert-73",
        "refresh_system_prompt": True,
    }


@pytest.mark.asyncio
async def test_dashboard_session_create_does_not_fallback_when_import_route_is_missing(
    monkeypatch,
) -> None:
    class MissingImportPool:
        async def request(self, _endpoint, _method, _path, **_kwargs):
            class Response:
                status_code = 404

                @staticmethod
                def json():
                    return {"detail": "Not Found"}

            return Response()

    monkeypatch.setattr(
        dashboard_protocol,
        "_dashboard_http_sessions",
        MissingImportPool(),
    )
    adapter = HermesDashboardProtocolAdapter(connector=None)

    with pytest.raises(HermesProtocolError) as captured:
        await adapter.create_session(
            _endpoint(),
            runtime_session_id="rs-legacy",
            model="7",
        )

    assert captured.value.status_code == 404
    assert captured.value.path == "/api/sessions/import"


def test_dashboard_recognizes_durable_clarify_park_marker() -> None:
    assert HermesDashboardProtocolAdapter._clarify_park_request_id({
        "name": "clarify",
        "result": {"error": "__HERMES_CLARIFY_PARK__:clr_123"},
    }) == "clr_123"
    assert HermesDashboardProtocolAdapter._clarify_park_request_id({
        "name": "terminal",
        "result": {"error": "__HERMES_CLARIFY_PARK__:clr_123"},
    }) is None


@pytest.mark.asyncio
async def test_dashboard_parks_on_clarify_hook_tool_error_before_model_can_consume_marker() -> None:
    """A blocked plugin hook is reported by Hermes as ``tool.error``.

    The marker is top-level in this event shape, rather than nested in the
    ordinary ``tool.complete.result`` mapping.
    """

    ws = FakeWebSocket(
        [
            {"$response": {"session_id": "live-clarify", "resumed": "rs-clarify"}},
            {"$response": {"status": "submitted"}},
            _event(
                "tool.error",
                "live-clarify",
                {
                    "name": "clarify",
                    "tool_id": "call-clarify",
                    "error": "__HERMES_CLARIFY_PARK__:clr_hook_error",
                },
            ),
            {"$response": {"interrupted": True}},
        ]
    )

    @asynccontextmanager
    async def connector(_endpoint):
        yield ws

    events = [
        event
        async for event in HermesDashboardProtocolAdapter(
            connector=connector
        ).stream_session_chat(
            _endpoint(), runtime_session_id="rs-clarify", message="test"
        )
    ]

    assert [event.event for event in events] == [
        "chat.run.started",
        "chat.clarify.requested",
        "chat.done",
    ]
    assert events[-1].data["status"] == "parked_for_clarify"


@pytest.mark.asyncio
async def test_dashboard_get_session_messages_reads_complete_history_with_encoded_query(
    monkeypatch,
) -> None:
    messages = [
        {"id": index, "role": "assistant", "content": f"message-{index}"}
        for index in range(1203)
    ]
    payloads = []
    for offset, page in (
        (0, messages[:500]),
        (500, messages[500:1000]),
        (1000, messages[1000:]),
    ):
        payloads.append(
            {
                "session_id": "tip-1203",
                "messages": page,
                "pagination": {
                    "limit": 500,
                    "offset": offset,
                    "order": "oldest",
                    "returned": len(page),
                    "total": 1203,
                },
            }
        )
    pool = SequencedDashboardPool(payloads)
    monkeypatch.setattr(dashboard_protocol, "_dashboard_http_sessions", pool)

    history = await HermesDashboardProtocolAdapter(
        connector=None
    ).get_session_messages(
        _endpoint(),
        runtime_session_id="root/id",
        hermes_profile_name="expert /7?",
    )

    assert history == {"session_id": "tip-1203", "messages": messages}
    assert pool.calls == [
        (
            "GET",
            "/api/sessions/root%2Fid/messages?"
            "profile=expert+%2F7%3F&limit=500&offset=0&order=oldest",
        ),
        (
            "GET",
            "/api/sessions/root%2Fid/messages?"
            "profile=expert+%2F7%3F&limit=500&offset=500&order=oldest",
        ),
        (
            "GET",
            "/api/sessions/root%2Fid/messages?"
            "profile=expert+%2F7%3F&limit=500&offset=1000&order=oldest",
        ),
    ]


@pytest.mark.asyncio
async def test_dashboard_get_session_messages_rejects_inconsistent_pagination(
    monkeypatch,
) -> None:
    pool = SequencedDashboardPool(
        [
            {
                "session_id": "tip",
                "messages": [{"id": 1}],
                "pagination": {
                    "limit": 500,
                    "offset": 0,
                    "order": "oldest",
                    "returned": 2,
                },
            }
        ]
    )
    monkeypatch.setattr(dashboard_protocol, "_dashboard_http_sessions", pool)

    with pytest.raises(HermesProtocolError, match="pagination"):
        await HermesDashboardProtocolAdapter(
            connector=None
        ).get_session_messages(_endpoint(), runtime_session_id="rs-1")


@pytest.mark.asyncio
async def test_dashboard_get_session_messages_rejects_repeated_page(
    monkeypatch,
) -> None:
    repeated = [{"id": index} for index in range(500)]
    pool = SequencedDashboardPool(
        [
            {
                "session_id": "tip",
                "messages": repeated,
                "pagination": {
                    "limit": 500,
                    "offset": 0,
                    "order": "oldest",
                    "returned": 500,
                },
            },
            {
                "session_id": "tip",
                "messages": repeated,
                "pagination": {
                    "limit": 500,
                    "offset": 500,
                    "order": "oldest",
                    "returned": 500,
                },
            },
        ]
    )
    monkeypatch.setattr(dashboard_protocol, "_dashboard_http_sessions", pool)

    with pytest.raises(HermesProtocolError, match="repeated"):
        await HermesDashboardProtocolAdapter(
            connector=None
        ).get_session_messages(_endpoint(), runtime_session_id="rs-1")


@pytest.mark.asyncio
async def test_dashboard_get_session_messages_rejects_zero_progress_before_total(
    monkeypatch,
) -> None:
    pool = SequencedDashboardPool(
        [
            {
                "session_id": "tip",
                "messages": [],
                "pagination": {
                    "limit": 500,
                    "offset": 0,
                    "order": "oldest",
                    "returned": 0,
                    "total": 1,
                },
            }
        ]
    )
    monkeypatch.setattr(dashboard_protocol, "_dashboard_http_sessions", pool)

    with pytest.raises(HermesProtocolError, match="no progress"):
        await HermesDashboardProtocolAdapter(
            connector=None
        ).get_session_messages(_endpoint(), runtime_session_id="rs-1")


@pytest.mark.asyncio
async def test_dashboard_get_session_messages_restarts_once_on_lineage_change(
    monkeypatch,
) -> None:
    first_page = [{"id": index} for index in range(500)]
    stable_page = [{"id": "stable"}]
    pool = SequencedDashboardPool(
        [
            {
                "session_id": "tip-a",
                "messages": first_page,
                "pagination": {
                    "limit": 500,
                    "offset": 0,
                    "order": "oldest",
                    "returned": 500,
                },
            },
            {
                "session_id": "tip-b",
                "messages": [{"id": "mixed"}],
                "pagination": {
                    "limit": 500,
                    "offset": 500,
                    "order": "oldest",
                    "returned": 1,
                },
            },
            {
                "session_id": "tip-b",
                "messages": stable_page,
                "pagination": {
                    "limit": 500,
                    "offset": 0,
                    "order": "oldest",
                    "returned": 1,
                },
            },
        ]
    )
    monkeypatch.setattr(dashboard_protocol, "_dashboard_http_sessions", pool)

    history = await HermesDashboardProtocolAdapter(
        connector=None
    ).get_session_messages(_endpoint(), runtime_session_id="root")

    assert history == {"session_id": "tip-b", "messages": stable_page}
    assert [path for _method, path in pool.calls] == [
        "/api/sessions/root/messages?limit=500&offset=0&order=oldest",
        "/api/sessions/root/messages?limit=500&offset=500&order=oldest",
        "/api/sessions/root/messages?limit=500&offset=0&order=oldest",
    ]


@pytest.mark.asyncio
async def test_dashboard_latest_history_helper_pages_back_to_initiating_user(
    monkeypatch,
) -> None:
    newest_page = [
        {"role": "assistant", "content": f"later-{index}"}
        for index in range(499)
    ] + [{"role": "assistant", "content": "terminal answer"}]
    pool = SequencedDashboardPool(
        [
            {
                "session_id": "tip",
                "messages": newest_page,
                "pagination": {
                    "limit": 500,
                    "offset": 0,
                    "order": "latest",
                    "returned": 500,
                },
            },
            {
                "session_id": "tip",
                "messages": [{"role": "user", "content": "begin turn"}],
                "pagination": {
                    "limit": 500,
                    "offset": 500,
                    "order": "latest",
                    "returned": 1,
                },
            },
        ]
    )
    monkeypatch.setattr(dashboard_protocol, "_dashboard_http_sessions", pool)
    adapter = HermesDashboardProtocolAdapter(connector=None)

    history = await adapter._get_latest_session_messages(
        _endpoint(),
        runtime_session_id="root",
        hermes_profile_name="default",
        initiating_user_message="begin turn",
        workspace=None,
    )

    assert history["messages"][0] == {"role": "user", "content": "begin turn"}
    assert adapter._completed_turn_text(
        history["messages"], "begin turn"
    ) == "terminal answer"
    assert [path for _method, path in pool.calls] == [
        "/api/sessions/root/messages?limit=500&offset=0&order=latest",
        "/api/sessions/root/messages?limit=500&offset=500&order=latest",
    ]


@pytest.mark.asyncio
async def test_dashboard_list_sessions_pages_at_100_and_appends_only_outside_pins(
    monkeypatch,
) -> None:
    def row(index: int, *, pinned: bool = False) -> dict:
        return {"id": f"session-{index}", "pinned": pinned}

    pinned_in_later_base = row(150, pinned=True)
    pinned_outside = {"id": "pinned-outside", "pinned": True}
    pool = SequencedDashboardPool(
        [
            {
                "sessions": [
                    *[row(index) for index in range(100)],
                    pinned_in_later_base,
                    pinned_outside,
                ],
                "total": 300,
                "limit": 100,
                "offset": 0,
            },
            {
                "sessions": [
                    *[
                        pinned_in_later_base if index == 150 else row(index)
                        for index in range(100, 200)
                    ],
                    pinned_outside,
                ],
                "total": 300,
                "limit": 100,
                "offset": 100,
            },
            {
                "sessions": [
                    *[row(index) for index in range(200, 205)],
                    pinned_in_later_base,
                    pinned_outside,
                ],
                "total": 300,
                "limit": 5,
                "offset": 200,
            },
        ]
    )
    monkeypatch.setattr(dashboard_protocol, "_dashboard_http_sessions", pool)

    result = await HermesDashboardProtocolAdapter(connector=None).list_sessions(
        _endpoint(), limit=205, offset=0
    )

    assert [session["id"] for session in result["sessions"]] == [
        *[f"session-{index}" for index in range(205)],
        "pinned-outside",
    ]
    assert result == {
        "sessions": result["sessions"],
        "total": 300,
        "limit": 205,
        "offset": 0,
    }
    assert [path for _method, path in pool.calls] == [
        "/api/sessions?limit=100&offset=0",
        "/api/sessions?limit=100&offset=100",
        "/api/sessions?limit=5&offset=200",
    ]
