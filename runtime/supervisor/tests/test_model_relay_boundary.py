from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from platform_supervisor.model_relay.app import create_model_relay_app
from platform_supervisor.model_relay.boundary import (
    MAIN_BOUNDARY_HEADER,
    MainBoundaryAction,
    MainBoundaryBoot,
    MainBoundaryCoordinator,
    MainBoundaryDispatchResult,
    MainBoundaryRouteBinding,
)
from platform_supervisor.model_relay.relay import (
    ActiveModelRoute,
    ModelRelay,
    ModelRelayBoundaryRedirected,
    ModelRelayBoundaryReplay,
    ModelRelayBoundaryUncertain,
    ModelRouteRegistry,
)
from platform_supervisor.settings import SupervisorSettings


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = (
    REPOSITORY_ROOT / "runtime" / "hermes" / "compatibility" / "v0.20.1.yaml"
)
ROUTE_TOKEN = "a" * 64
BOOT_ID = "dashboard-boot-1"
CONNECTION_ID = "dashboard-connection-1"
STORED_SESSION_ID = "session-golden"
LIVE_SESSION_ID = "live-session-golden"
TASK_ID = "task-golden"
FIXED_NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def settings() -> SupervisorSettings:
    return SupervisorSettings(
        _env_file=None,
        RUNTIME_ID="runtime-application-3",
        RUNTIME_BINDING_ID="binding-3",
        RUNTIME_MODE="application",
            HERMES_STARTUP_MODE="steady_state",
        API_SERVER_KEY="runtime-only-secret-0123456789abcdef",
        NEWAPI_API_KEY="model-gateway-only-secret-0123456789",
        NEWAPI_BASE_URL="https://newapi.example/v1",
    )


def active_route(
    *,
    api_mode: str = "chat_completions",
) -> ActiveModelRoute:
    return ActiveModelRoute(
        route_token=ROUTE_TOKEN,
        run_id="run-golden",
        session_id=STORED_SESSION_ID,
        model_id="model-a",
        api_mode=api_mode,
        max_output_tokens=4096,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


class RecordingGate:
    def __init__(
        self,
        result: MainBoundaryDispatchResult = MainBoundaryDispatchResult.RELEASE,
    ) -> None:
        self.result = result
        self.calls = []
        self.request_observations = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def observe_model_request(self, *, binding, request_body):
        self.request_observations.append((binding, request_body))
        return False

    async def dispatch(self, handle):
        self.calls.append(handle)
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.result is not MainBoundaryDispatchResult.RELEASE:
            assert handle.claim_dashboard_redirect(
                runtime_run_id=handle.runtime_run_id,
                stored_session_id=handle.stored_session_id,
                live_session_id=handle.live_session_id,
                dashboard_connection_id=handle.dashboard_connection_id,
            )
        return self.result


class RaisingPreRpcGate:
    async def dispatch(self, _handle):
        raise RuntimeError("pre-RPC durable gate failure")


class JoiningRouteResolver:
    def __init__(self, *, resolve: bool = True) -> None:
        self.resolve = resolve
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def resolve_route(self, *, route, marker):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if not self.resolve:
            return None
        return MainBoundaryRouteBinding(
            route_token=route.route_token,
            run_id=route.run_id,
            dashboard_boot_id=BOOT_ID,
            dashboard_connection_id=CONNECTION_ID,
            stored_session_id=route.session_id,
            live_session_id=LIVE_SESSION_ID,
            task_id=marker.task_id,
            profile_key="default",
            boundary_epoch_nonce=marker.boundary_epoch_nonce,
        )


async def coordinator(
    gate: RecordingGate,
    *,
    join_timeout_seconds: float = 0.5,
    gate_timeout_seconds: float = 0.5,
    clock=None,
    route_resolver=None,
    register_route: bool = True,
) -> tuple[MainBoundaryCoordinator, str]:
    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    golden = manifest["runtime_overlay"]["platform_main_conversation_boundary"][
        "golden_vector"
    ]
    key = bytes(range(32))
    boundary = MainBoundaryCoordinator(
        gate=gate,
        route_resolver=route_resolver,
        clock=clock or (lambda: FIXED_NOW),
        join_timeout_seconds=join_timeout_seconds,
        gate_timeout_seconds=gate_timeout_seconds,
    )
    await boundary.register_boot(
        MainBoundaryBoot(
            dashboard_boot_id=BOOT_ID,
            marker_key=key,
            boundary_epoch_nonce=golden["payload"]["boundary_epoch_nonce"],
        )
    )
    if register_route:
        await boundary.register_route(
            MainBoundaryRouteBinding(
                route_token=ROUTE_TOKEN,
                run_id="run-golden",
                dashboard_boot_id=BOOT_ID,
                dashboard_connection_id=CONNECTION_ID,
                stored_session_id=STORED_SESSION_ID,
                live_session_id=LIVE_SESSION_ID,
                task_id=TASK_ID,
                profile_key="default",
                boundary_epoch_nonce=golden["payload"]["boundary_epoch_nonce"],
            )
        )
    return boundary, golden["header"]


async def relay_with_handler(
    boundary: MainBoundaryCoordinator,
    handler,
    *,
    api_mode: str = "chat_completions",
) -> tuple[ModelRelay, ModelRouteRegistry]:
    registry = ModelRouteRegistry()
    await registry.register(active_route(api_mode=api_mode))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        ModelRelay(
            settings(),
            registry,
            client=client,
            main_boundary=boundary,
        ),
        registry,
    )


@pytest.mark.asyncio
async def test_golden_marker_joins_start_then_redirects_at_active_provider_boundary() -> None:
    gate = RecordingGate(MainBoundaryDispatchResult.REDIRECTED)
    boundary, marker = await coordinator(gate)
    await boundary.arm_redirect("run-golden")
    provider_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, stream=ChunkStream([b"unexpected"]))

    relay, _ = await relay_with_handler(boundary, handler)
    forwarding = asyncio.create_task(
        relay.forward(
            route_token=ROUTE_TOKEN,
            operation="chat/completions",
            body=b'{"model":"model-a","max_tokens":64,"messages":[]}',
            request_headers={MAIN_BOUNDARY_HEADER: marker},
        )
    )
    await asyncio.sleep(0)
    assert forwarding.done() is False
    assert provider_calls == 0
    assert gate.calls == []

    await boundary.observe_message_start(
        run_id="run-golden",
        stored_session_id=STORED_SESSION_ID,
        live_session_id=LIVE_SESSION_ID,
        dashboard_connection_id=CONNECTION_ID,
    )
    await boundary.observe_message_start(
        run_id="run-golden",
        stored_session_id=STORED_SESSION_ID,
        live_session_id=LIVE_SESSION_ID,
        dashboard_connection_id=CONNECTION_ID,
    )

    with pytest.raises(ModelRelayBoundaryRedirected):
        await forwarding
    assert len(gate.calls) == 1
    assert gate.calls[0].dashboard_connection_id == CONNECTION_ID
    assert provider_calls == 1
    await relay.aclose()


@pytest.mark.asyncio
async def test_redirect_dispatch_races_provider_headers() -> None:
    gate = RecordingGate(MainBoundaryDispatchResult.REDIRECTED)
    boundary, marker = await coordinator(gate)
    await boundary.arm_redirect("run-golden")
    provider_started = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        provider_started.set()
        await asyncio.Future()
        raise AssertionError("provider response should be canceled")

    relay, _ = await relay_with_handler(boundary, handler)
    await boundary.observe_message_start(
        run_id="run-golden",
        stored_session_id=STORED_SESSION_ID,
        live_session_id=LIVE_SESSION_ID,
        dashboard_connection_id=CONNECTION_ID,
    )
    forwarding = asyncio.create_task(
        relay.forward(
            route_token=ROUTE_TOKEN,
            operation="chat/completions",
            body=b'{"model":"model-a","max_tokens":64,"messages":[]}',
            request_headers={MAIN_BOUNDARY_HEADER: marker},
        )
    )
    await provider_started.wait()
    for _ in range(100):
        if gate.calls:
            break
        await asyncio.sleep(0.001)
    assert gate.calls
    with pytest.raises(ModelRelayBoundaryRedirected):
        await forwarding
    await relay.aclose()


@pytest.mark.asyncio
async def test_authenticated_marker_joins_unbound_route_and_message_start_once() -> None:
    gate = RecordingGate(MainBoundaryDispatchResult.REDIRECTED)
    resolver = JoiningRouteResolver()
    boundary, marker = await coordinator(
        gate,
        join_timeout_seconds=0.2,
        route_resolver=resolver,
        register_route=False,
    )
    await boundary.arm_redirect("run-golden")

    decision_task = asyncio.create_task(
        boundary.before_forward(
            route=active_route(),
            marker_header=marker,
        )
    )
    await resolver.started.wait()
    assert decision_task.done() is False
    assert gate.calls == []

    await boundary.observe_message_start(
        run_id="run-golden",
        stored_session_id=STORED_SESSION_ID,
        live_session_id=LIVE_SESSION_ID,
        dashboard_connection_id=CONNECTION_ID,
    )
    resolver.release.set()
    decision = await decision_task
    assert decision.action.value == "forward"
    assert decision.disposition == "awaiting_upstream_active"
    decision = await boundary.mark_upstream_active(decision)

    assert decision.action.value == "abort"
    assert decision.disposition == "redirected"
    assert len(gate.calls) == 1
    assert gate.calls[0].task_id == TASK_ID


@pytest.mark.asyncio
async def test_unbound_route_join_timeout_forwards_without_uncertainty() -> None:
    gate = RecordingGate(MainBoundaryDispatchResult.REDIRECTED)
    resolver = JoiningRouteResolver()
    boundary, marker = await coordinator(
        gate,
        join_timeout_seconds=0.01,
        route_resolver=resolver,
        register_route=False,
    )
    await boundary.arm_redirect("run-golden")

    decision = await boundary.before_forward(
        route=active_route(),
        marker_header=marker,
    )

    assert decision.action.value == "forward"
    assert decision.disposition == "route_join_timeout"
    assert gate.calls == []
    assert resolver.cancelled is True


@pytest.mark.asyncio
async def test_start_first_rejected_releases_original_request_to_provider() -> None:
    gate = RecordingGate(MainBoundaryDispatchResult.REJECTED)
    boundary, marker = await coordinator(gate)
    await boundary.observe_message_start(
        run_id="run-golden",
        stored_session_id=STORED_SESSION_ID,
        live_session_id=LIVE_SESSION_ID,
        dashboard_connection_id=CONNECTION_ID,
    )
    await boundary.arm_redirect("run-golden")
    provider_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        assert MAIN_BOUNDARY_HEADER.lower() not in request.headers
        return httpx.Response(200, stream=ChunkStream([b"ok"]))

    relay, _ = await relay_with_handler(boundary, handler)
    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"model-a","max_tokens":64,"messages":[]}',
        request_headers={MAIN_BOUNDARY_HEADER: marker},
    )
    assert [chunk async for chunk in response.body_iterator] == [b"ok"]
    assert provider_calls == 1
    assert len(gate.calls) == 1
    await relay.aclose()


@pytest.mark.asyncio
async def test_authenticated_forward_observes_exact_model_request_body() -> None:
    gate = RecordingGate()
    boundary, marker = await coordinator(gate)
    await boundary.observe_message_start(
        run_id="run-golden",
        stored_session_id=STORED_SESSION_ID,
        live_session_id=LIVE_SESSION_ID,
        dashboard_connection_id=CONNECTION_ID,
    )
    provider_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, stream=ChunkStream([b"ok"]))

    body = (
        b'{"model":"model-a","max_tokens":64,"messages":'
        b'[{"role":"tool","content":"base\\n\\nmarker"}]}'
    )
    relay, _ = await relay_with_handler(boundary, handler)
    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=body,
        request_headers={MAIN_BOUNDARY_HEADER: marker},
    )

    assert [chunk async for chunk in response.body_iterator] == [b"ok"]
    assert provider_calls == 1
    assert len(gate.request_observations) == 1
    binding, observed_body = gate.request_observations[0]
    assert binding.stored_session_id == STORED_SESSION_ID
    assert observed_body == body
    await relay.aclose()


@pytest.mark.asyncio
async def test_active_handle_identity_miss_emits_content_free_reason_matrix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING")
    boundary, marker = await coordinator(RecordingGate())
    await boundary.arm_redirect("run-golden")
    route = active_route()
    decision_task = asyncio.create_task(
        boundary.before_forward(route=route, marker_header=marker)
    )
    await asyncio.sleep(0)
    await boundary.observe_message_start(
        run_id="run-golden",
        stored_session_id="different-stored-session",
        live_session_id=LIVE_SESSION_ID,
        dashboard_connection_id=CONNECTION_ID,
    )
    decision = await decision_task
    assert decision.action is MainBoundaryAction.FORWARD
    await boundary.mark_upstream_active(decision)

    assert await boundary.take_active_upstream_handle("run-golden") is None
    receipts = [
        record
        for record in caplog.records
        if getattr(record, "event", None)
        == "followup_boundary_handle_receipt"
    ]
    assert receipts
    receipt = receipts[-1]
    assert receipt.followup_stage == "active_handle_identity_mismatch"
    assert receipt.followup_identity_checks["start_epoch_matches"] is False
    assert LIVE_SESSION_ID in receipt.getMessage()
    assert "messages" not in receipt.getMessage()


@pytest.mark.asyncio
async def test_queued_redirect_isolated_and_never_projected_as_success() -> None:
    gate = RecordingGate(MainBoundaryDispatchResult.QUEUED)
    boundary, marker = await coordinator(gate)
    await boundary.observe_message_start(
        run_id="run-golden",
        stored_session_id=STORED_SESSION_ID,
        live_session_id=LIVE_SESSION_ID,
        dashboard_connection_id=CONNECTION_ID,
    )
    await boundary.arm_redirect("run-golden")
    provider_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200)

    relay, _ = await relay_with_handler(boundary, handler)
    with pytest.raises(ModelRelayBoundaryUncertain, match="queued"):
        await relay.forward(
            route_token=ROUTE_TOKEN,
            operation="chat/completions",
            body=b'{"model":"model-a","max_tokens":64,"messages":[]}',
            request_headers={MAIN_BOUNDARY_HEADER: marker},
        )
    assert provider_calls == 1
    await relay.aclose()


@pytest.mark.asyncio
async def test_marker_join_and_pre_rpc_gate_timeouts_forward_without_redirect_rpc() -> None:
    gate = RecordingGate()
    boundary, marker = await coordinator(
        gate,
        join_timeout_seconds=0.01,
        gate_timeout_seconds=0.01,
    )
    await boundary.arm_redirect("run-golden")
    provider_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, stream=ChunkStream([b"ok"]))

    relay, _ = await relay_with_handler(boundary, handler)
    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"model-a","max_tokens":64,"messages":[]}',
        request_headers={MAIN_BOUNDARY_HEADER: marker},
    )
    assert [chunk async for chunk in response.body_iterator] == [b"ok"]
    assert gate.calls == []
    assert provider_calls == 1

    await boundary.finish_turn("run-golden")
    await boundary.observe_message_start(
        run_id="run-golden",
        stored_session_id=STORED_SESSION_ID,
        live_session_id=LIVE_SESSION_ID,
        dashboard_connection_id=CONNECTION_ID,
    )
    gate.block = True
    # A new signed request is needed: an exact retry replays its first
    # forwarding disposition and never becomes a second gate attempt.
    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    golden = manifest["runtime_overlay"]["platform_main_conversation_boundary"][
        "golden_vector"
    ]
    payload = dict(golden["payload"])
    payload["api_request_id"] = "request-gate-timeout"
    timeout_marker = MainBoundaryCoordinator.sign_test_marker(bytes(range(32)), payload)
    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"model-a","max_tokens":64,"messages":[]}',
        request_headers={MAIN_BOUNDARY_HEADER: timeout_marker},
    )
    assert [chunk async for chunk in response.body_iterator] == [b"ok"]
    assert len(gate.calls) == 1
    assert provider_calls == 2
    await relay.aclose()


@pytest.mark.asyncio
async def test_pre_rpc_gate_exception_forwards_without_uncertain_isolation() -> None:
    boundary, marker = await coordinator(RaisingPreRpcGate())  # type: ignore[arg-type]
    await boundary.observe_message_start(
        run_id="run-golden",
        stored_session_id=STORED_SESSION_ID,
        live_session_id=LIVE_SESSION_ID,
        dashboard_connection_id=CONNECTION_ID,
    )
    await boundary.arm_redirect("run-golden")

    decision = await boundary.before_forward(
        route=active_route(),
        marker_header=marker,
    )
    decision = await boundary.mark_upstream_active(decision)

    assert decision.action.value == "forward"
    assert decision.disposition == "gate_error"


@pytest.mark.asyncio
async def test_invalid_and_subagent_markers_forward_without_claiming_waiting_boundary() -> None:
    gate = RecordingGate(MainBoundaryDispatchResult.REDIRECTED)
    boundary, marker = await coordinator(gate)
    await boundary.observe_message_start(
        run_id="run-golden",
        stored_session_id=STORED_SESSION_ID,
        live_session_id=LIVE_SESSION_ID,
        dashboard_connection_id=CONNECTION_ID,
    )
    await boundary.arm_redirect("run-golden")
    provider_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, stream=ChunkStream([b"ok"]))

    relay, _ = await relay_with_handler(boundary, handler)
    invalid = marker[:-1] + ("0" if marker[-1] != "0" else "1")
    first = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"model-a","max_tokens":64,"messages":[]}',
        request_headers={MAIN_BOUNDARY_HEADER: invalid},
    )
    _ = [chunk async for chunk in first.body_iterator]

    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    golden = manifest["runtime_overlay"]["platform_main_conversation_boundary"][
        "golden_vector"
    ]
    payload = dict(golden["payload"])
    payload.update(
        session_id="subagent-session",
        task_id="subagent-task",
        api_request_id="subagent-request",
    )
    subagent = MainBoundaryCoordinator.sign_test_marker(bytes(range(32)), payload)
    second = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"model-a","max_tokens":64,"messages":[]}',
        request_headers={MAIN_BOUNDARY_HEADER: subagent},
    )
    _ = [chunk async for chunk in second.body_iterator]

    assert provider_calls == 2
    assert gate.calls == []
    assert gate.request_observations == []
    assert await boundary.has_waiting_redirect("run-golden") is True
    await relay.aclose()


@pytest.mark.asyncio
async def test_exact_marker_retry_never_calls_provider_twice() -> None:
    gate = RecordingGate()
    boundary, marker = await coordinator(gate)
    provider_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, stream=ChunkStream([b"ok"]))

    relay, _ = await relay_with_handler(boundary, handler)
    request = {
        "route_token": ROUTE_TOKEN,
        "operation": "chat/completions",
        "body": b'{"model":"model-a","max_tokens":64,"messages":[]}',
        "request_headers": {MAIN_BOUNDARY_HEADER: marker},
    }
    response = await relay.forward(**request)
    assert [chunk async for chunk in response.body_iterator] == [b"ok"]

    with pytest.raises(ModelRelayBoundaryReplay, match="completed"):
        await relay.forward(**request)
    assert provider_calls == 1
    assert gate.request_observations == []
    await relay.aclose()


@pytest.mark.asyncio
async def test_accepted_exact_retry_replays_after_ttl_but_old_boot_never_does() -> None:
    gate = RecordingGate()
    current_time = [FIXED_NOW]
    boundary, marker = await coordinator(gate, clock=lambda: current_time[0])
    provider_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, stream=ChunkStream([b"ok"]))

    relay, _ = await relay_with_handler(boundary, handler)
    request = {
        "route_token": ROUTE_TOKEN,
        "operation": "chat/completions",
        "body": b'{"model":"model-a","max_tokens":64,"messages":[]}',
        "request_headers": {MAIN_BOUNDARY_HEADER: marker},
    }
    response = await relay.forward(**request)
    _ = [chunk async for chunk in response.body_iterator]
    current_time[0] = FIXED_NOW + timedelta(minutes=5)

    with pytest.raises(ModelRelayBoundaryReplay):
        await relay.forward(**request)
    assert provider_calls == 1

    await boundary.revoke_boot(BOOT_ID)
    response = await relay.forward(**request)
    _ = [chunk async for chunk in response.body_iterator]
    assert provider_calls == 2
    await relay.aclose()


@pytest.mark.asyncio
async def test_unseen_expired_and_same_identity_different_wire_never_claim() -> None:
    gate = RecordingGate(MainBoundaryDispatchResult.REDIRECTED)
    boundary, marker = await coordinator(gate)
    provider_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_calls
        provider_calls += 1
        return httpx.Response(200, stream=ChunkStream([b"ok"]))

    relay, _ = await relay_with_handler(boundary, handler)
    first = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"model-a","max_tokens":64,"messages":[]}',
        request_headers={MAIN_BOUNDARY_HEADER: marker},
    )
    _ = [chunk async for chunk in first.body_iterator]

    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    golden = manifest["runtime_overlay"]["platform_main_conversation_boundary"][
        "golden_vector"
    ]
    conflicting_payload = dict(golden["payload"])
    conflicting_payload["request_nonce"] = "eHh4eHh4eHh4eHh4eHh4eA"
    conflicting = MainBoundaryCoordinator.sign_test_marker(
        bytes(range(32)),
        conflicting_payload,
    )
    second = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"model-a","max_tokens":64,"messages":[]}',
        request_headers={MAIN_BOUNDARY_HEADER: conflicting},
    )
    _ = [chunk async for chunk in second.body_iterator]

    expired_payload = dict(golden["payload"])
    expired_payload.update(
        api_request_id="expired-unseen",
        issued_at="2026-08-13T00:00:00.000Z",
        expires_at="2026-08-13T00:00:20.000Z",
    )
    expired = MainBoundaryCoordinator.sign_test_marker(
        bytes(range(32)),
        expired_payload,
    )
    third = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"model-a","max_tokens":64,"messages":[]}',
        request_headers={MAIN_BOUNDARY_HEADER: expired},
    )
    _ = [chunk async for chunk in third.body_iterator]

    assert provider_calls == 3
    assert gate.calls == []
    assert await boundary.request_disposition(expired) is None
    await relay.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_mode", "operation", "body"),
    [
        (
            "chat_completions",
            "chat/completions",
            b'{"model":"model-a","max_tokens":64,"messages":[]}',
        ),
        (
            "responses",
            "responses",
            b'{"model":"model-a","max_output_tokens":64,"input":"hi"}',
        ),
        (
            "anthropic",
            "messages",
            b'{"model":"model-a","max_tokens":64,"messages":[]}',
        ),
    ],
)
async def test_http_transports_preserve_marker_to_relay_and_strip_it_upstream(
    api_mode: str,
    operation: str,
    body: bytes,
) -> None:
    gate = RecordingGate()
    boundary, marker = await coordinator(gate)
    upstream_headers: list[httpx.Headers] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        upstream_headers.append(request.headers)
        return httpx.Response(200, stream=ChunkStream([b"ok"]))

    relay, registry = await relay_with_handler(
        boundary,
        handler,
        api_mode=api_mode,
    )
    app = create_model_relay_app(relay, registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://relay.local",
    ) as client:
        response = await client.post(
            f"/model-relay/{ROUTE_TOKEN}/v1/{operation}",
            content=body,
            headers={
                "content-type": "application/json",
                MAIN_BOUNDARY_HEADER: marker,
            },
        )

    assert response.status_code == 200
    assert response.content == b"ok"
    assert len(upstream_headers) == 1
    assert MAIN_BOUNDARY_HEADER.lower() not in upstream_headers[0]
    assert await boundary.request_disposition(marker) == "completed"
    await relay.aclose()
