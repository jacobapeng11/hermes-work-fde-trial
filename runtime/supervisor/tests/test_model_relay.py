from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest

import platform_supervisor.model_relay.relay as relay_module
from platform_supervisor.model_relay import (
    ActiveModelRoute,
    ModelRelay,
    ModelRelayCapacityExhausted,
    ModelRelayRequestRejected,
    ModelRouteRegistry,
    RelayOutboxObservationSink,
)
from platform_supervisor.model_relay.app import create_model_relay_app
from platform_supervisor.model_relay.server import ModelRelayServer
from platform_supervisor.settings import SupervisorSettings
from platform_supervisor.storage import SQLiteStore


ROUTE_TOKEN = "route-token-0123456789abcdef0123456789abcdef"


def settings(**overrides: object) -> SupervisorSettings:
    values = {
        "RUNTIME_ID": "runtime-application-3",
        "RUNTIME_BINDING_ID": "binding-3",
        "RUNTIME_MODE": "application",
        "HERMES_STARTUP_MODE": "steady_state",
        "API_SERVER_KEY": "runtime-only-secret-0123456789abcdef",
        "NEWAPI_API_KEY": "model-gateway-only-secret-0123456789",
        "NEWAPI_BASE_URL": "https://newapi.example/v1",
    }
    values.update(overrides)
    return SupervisorSettings(_env_file=None, **values)


def route(**overrides: object) -> ActiveModelRoute:
    values = {
        "route_token": ROUTE_TOKEN,
        "run_id": "run-01234567",
        "session_id": "session-01234567",
        "model_id": "model-a",
        "api_mode": "chat_completions",
        "max_output_tokens": 4096,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    values.update(overrides)
    return ActiveModelRoute(**values)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class RecordingObservationSink:
    def __init__(self, *, fail_first_byte: bool = False) -> None:
        self.fail_first_byte = fail_first_byte
        self.first_bytes: list[str] = []
        self.semantic_tokens: list[str] = []
        self.usages: list[dict] = []
        self.post_turn_runs: list[str] = []
        self.stream_summaries: list[dict] = []

    def first_upstream_byte(self, *, run_id: str, elapsed_ms: float) -> None:
        self.first_bytes.append(run_id)
        if self.fail_first_byte:
            raise RuntimeError("observer failure")

    def first_semantic_token(self, *, run_id: str, elapsed_ms: float) -> None:
        self.semantic_tokens.append(run_id)

    def usage(
        self,
        *,
        run_id: str,
        request_id: str,
        usage: dict,
        authorization_scope: str,
    ) -> None:
        del run_id, request_id, authorization_scope
        self.usages.append(usage)

    def post_turn_activity(
        self,
        *,
        run_id: str,
        request_id: str,
    ) -> None:
        del request_id
        self.post_turn_runs.append(run_id)

    def stream_summary(self, **summary: object) -> None:
        self.stream_summaries.append(dict(summary))


@pytest.mark.asyncio
async def test_relay_observations_are_durable_outbox_items(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    sink = RelayOutboxObservationSink(store)

    sink.first_upstream_byte(run_id="run-1", elapsed_ms=12.3456)
    sink.first_semantic_token(run_id="run-1", elapsed_ms=23.4567)
    sink.stream_summary(
        run_id="run-1",
        request_id="request-stream-1",
        authorization_scope="active_run",
        status_code=200,
        content_type="text/event-stream",
        content_encoding="identity",
        raw_chunk_count=3,
        sse_event_count=2,
        first_chunk_bytes=13,
        max_chunk_bytes=72,
        max_chunk_gap_ms=8.25,
        body_duration_ms=31.5,
        semantic_span_ms=12.75,
        first_reasoning_delta_ms=14.0,
        first_text_delta_ms=18.75,
    )
    sink.usage(
        run_id="run-1",
        request_id="request-1",
        usage={"prompt_tokens": 7, "completion_tokens": 11},
        authorization_scope="active_run",
    )
    sink.post_turn_activity(
        run_id="run-1",
        request_id="request-2",
    )
    await sink.aclose()

    items = await store.load_projection_batch()
    assert [item.projection_type for item in items[:2]] == [
        "run.metric",
        "run.metric",
    ]
    stream_metrics = {
        item.payload["metric"]: item.payload["value"]
        for item in items
        if item.payload.get("request_id") == "request-stream-1"
    }
    assert stream_metrics["model_relay_upstream_raw_chunk_count"] == 3
    assert stream_metrics["model_relay_upstream_first_text_delta_ms"] == 18.75
    assert len(stream_metrics) == 9
    assert items[-2].payload["usage"]["completion_tokens"] == 11
    assert items[-1].payload["request_id"] == "request-2"
    await store.close()


@pytest.mark.asyncio
async def test_relay_observation_projection_ids_are_bounded_and_collision_safe(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    sink = RelayOutboxObservationSink(store)
    run_id = "run-" + ("r" * 180)
    common_request_prefix = "request-" + ("q" * 80)

    for suffix in ("-one", "-two"):
        sink.stream_summary(
            run_id=run_id,
            request_id=common_request_prefix + suffix,
            authorization_scope="active_run",
            status_code=200,
            content_type="text/event-stream",
            content_encoding="identity",
            raw_chunk_count=1,
            sse_event_count=1,
            first_chunk_bytes=1,
            max_chunk_bytes=1,
            max_chunk_gap_ms=1,
            body_duration_ms=1,
            semantic_span_ms=1,
            first_reasoning_delta_ms=1,
            first_text_delta_ms=1,
        )
    sink.usage(
        run_id=run_id,
        request_id=common_request_prefix + "-one",
        usage={"total_tokens": 1},
        authorization_scope="active_run",
    )
    sink.post_turn_activity(
        run_id=run_id,
        request_id=common_request_prefix + "-one",
    )
    await sink.aclose()

    items = await store.load_projection_batch(max_items=100)
    projection_ids = [item.projection_id for item in items]
    assert len(projection_ids) == 20
    assert len(projection_ids) == len(set(projection_ids))
    assert all(len(projection_id) <= 128 for projection_id in projection_ids)
    await store.close()


@pytest.mark.asyncio
async def test_relay_preserves_request_and_response_bytes() -> None:
    chunks = [
        b": keepalive\n\n",
        (
            b'data: {"choices":[{"delta":{"content":"answer"}}],'
            b'"usage":{"total_tokens":9}}\n\n'
        ),
        b"data: [DONE]\n\n",
    ]
    stream = ChunkStream(chunks)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": "req-1"},
            stream=stream,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ModelRouteRegistry()
    await registry.register(route())
    observations = RecordingObservationSink(fail_first_byte=True)
    relay = ModelRelay(
        settings(),
        registry,
        client=client,
        observation_sink=observations,
    )
    body = (
        b'{"model":"model-a","stream":true,"max_tokens":4096,'
        b'"messages":[{"role":"user","content":"hello"}]}'
    )

    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=body,
    )
    received = [chunk async for chunk in response.body_iterator]

    assert received == chunks
    assert requests[0].content == body
    assert requests[0].headers["authorization"] == (
        "Bearer model-gateway-only-secret-0123456789"
    )
    assert requests[0].headers["accept-encoding"] == "identity"
    assert requests[0].url == "https://newapi.example/v1/chat/completions"
    assert response.headers == {
        "content-type": "text/event-stream",
        "x-request-id": "req-1",
    }
    assert observations.first_bytes == ["run-01234567"]
    assert observations.semantic_tokens == ["run-01234567"]
    assert observations.usages == [{"total_tokens": 9}]
    assert len(observations.stream_summaries) == 1
    summary = observations.stream_summaries[0]
    assert summary["raw_chunk_count"] == 3
    assert summary["sse_event_count"] == 2
    assert summary["first_chunk_bytes"] == len(chunks[0])
    assert summary["max_chunk_bytes"] == len(chunks[1])
    assert summary["first_text_delta_ms"] is not None
    assert summary["first_reasoning_delta_ms"] is None
    assert summary["content_type"] == "text/event-stream"
    assert summary["content_encoding"] == "identity"
    assert stream.closed is True
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_rewrites_only_authorized_runtime_model_to_gateway_model() -> None:
    forwarded: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        return httpx.Response(
            200,
            stream=ChunkStream([b'{"choices":[]}']),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ModelRouteRegistry()
    await registry.register(
        route(model_id="17", upstream_model_id="qwen3-235b-a22b")
    )
    relay = ModelRelay(settings(), registry, client=client)

    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"qwen3-235b-a22b","max_tokens":64,"messages":[]}',
    )
    _ = [chunk async for chunk in response.body_iterator]

    assert forwarded == [
        {
            "model": "qwen3-235b-a22b",
            "max_tokens": 64,
            "messages": [],
        }
    ]
    await relay.aclose()


@pytest.mark.asyncio
async def test_required_reasoning_replay_preserves_real_trace_across_tool_continuation() -> None:
    forwarded: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, stream=ChunkStream([b'{"choices":[]}']))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ModelRouteRegistry()
    await registry.register(
        route(
            model_id="19",
            upstream_model_id="deepseek-v4-flash",
            runtime_model_id="deepseek-v4-flash",
            runtime_capabilities={
                "reasoning_replay": "required",
                "thinking_mode": "enabled",
            },
        )
    )
    relay = ModelRelay(settings(), registry, client=client)
    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=(
            b'{"model":"deepseek-v4-flash","max_tokens":64,"messages":['
            b'{"role":"assistant","reasoning_content":"real trace",'
            b'"tool_calls":[{"id":"call-1","type":"function"}]},'
            b'{"role":"tool","tool_call_id":"call-1","content":"ok"}]}'
        ),
    )
    _ = [chunk async for chunk in response.body_iterator]

    assert forwarded[0]["model"] == "deepseek-v4-flash"
    assert forwarded[0]["thinking"] == {"type": "enabled"}
    assert forwarded[0]["messages"][0]["reasoning_content"] == "real trace"
    assert relay.metrics_snapshot()["reasoning_replay.real_replayed"] == 1
    assert relay.metrics_snapshot()["reasoning_replay.fallback_space"] == 0
    assert relay.metrics_snapshot()["thinking_mode.enabled_requests"] == 1
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_model_listing_exposes_hermes_internal_model_name() -> None:
    registry = ModelRouteRegistry()
    # Hermes asks /models while the session is being prepared, before a Run
    # route can be activated.  The listing is deliberately narrower than a
    # forwarding route: it cannot authorize any model request.
    await registry.register_model_listing(
        ROUTE_TOKEN,
        "deepseek-v4-flash",
    )
    relay = ModelRelay(settings(), registry)
    app = create_model_relay_app(relay, registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://relay") as client:
        response = await client.get(f"/model-relay/{ROUTE_TOKEN}/v1/models")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "deepseek-v4-flash"
    await relay.aclose()


@pytest.mark.asyncio
async def test_reasoning_policy_promotes_internal_and_strips_unsupported_fields() -> None:
    required = route(
        runtime_capabilities={"reasoning_replay": "required"},
    )
    strict = route(
        runtime_capabilities={"reasoning_replay": "not_supported"},
    )
    relay = ModelRelay(settings(), ModelRouteRegistry())
    required_payload = {"messages": [{"role": "assistant", "reasoning": "internal"}]}
    relay._apply_reasoning_replay_policy(required, required_payload)
    assert required_payload["messages"][0]["reasoning_content"] == "internal"
    strict_payload = {"messages": [{"role": "assistant", "reasoning_content": "stale"}]}
    relay._apply_reasoning_replay_policy(strict, strict_payload)
    assert "reasoning_content" not in strict_payload["messages"][0]


@pytest.mark.parametrize(
    ("thinking_mode", "request_thinking", "expected_thinking"),
    [
        ("provider_default", None, None),
        ("provider_default", {"type": "provider"}, {"type": "provider"}),
        ("disabled", {"type": "enabled"}, None),
    ],
)
def test_thinking_mode_policy_preserves_default_and_removes_disabled(
    thinking_mode: str,
    request_thinking: dict[str, str] | None,
    expected_thinking: dict[str, str] | None,
) -> None:
    relay = ModelRelay(settings(), ModelRouteRegistry())
    active_route = route(
        runtime_capabilities={"thinking_mode": thinking_mode},
    )
    payload: dict[str, object] = {}
    if request_thinking is not None:
        payload["thinking"] = request_thinking

    relay._apply_thinking_mode_policy(active_route, payload)

    assert payload.get("thinking") == expected_thinking


@pytest.mark.parametrize(
    ("api_mode", "operation", "body", "limit_key"),
    [
        (
            "chat_completions",
            "chat/completions",
            b'{"model":"model-a","messages":[]}',
            "max_tokens",
        ),
        (
            "responses",
            "responses",
            b'{"model":"model-a","input":"hello"}',
            "max_output_tokens",
        ),
    ],
)
@pytest.mark.asyncio
async def test_relay_injects_granted_limit_when_client_omits_it(
    api_mode: str,
    operation: str,
    body: bytes,
    limit_key: str,
) -> None:
    forwarded: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, stream=ChunkStream([b'{"ok":true}']))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ModelRouteRegistry()
    await registry.register(route(api_mode=api_mode, max_output_tokens=4096))
    relay = ModelRelay(settings(), registry, client=client)

    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation=operation,
        body=body,
    )
    _ = [chunk async for chunk in response.body_iterator]

    assert forwarded[0][limit_key] == 4096
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_limits_output_by_model_context() -> None:
    forwarded: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, stream=ChunkStream([b'{"ok":true}']))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ModelRouteRegistry()
    await registry.register(
        route(max_output_tokens=32768, context_length=128000)
    )
    relay = ModelRelay(settings(), registry, client=client)

    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"model-a","max_tokens":32768}',
    )
    _ = [part async for part in response.body_iterator]

    assert forwarded[0]["max_tokens"] == 16000
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_retries_unambiguous_output_cap_400() -> None:
    forwarded: list[dict] = []
    chunk = b'{"ok":true}'

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        forwarded.append(payload)
        if payload["max_tokens"] == 32768:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "This model's maximum context length is 128000 "
                            "tokens. However, you requested 32768 output "
                            "tokens and your prompt contains at least 95233 "
                            "input tokens, for a total of at least 128001 "
                            "tokens."
                        )
                    }
                },
            )
        return httpx.Response(200, stream=ChunkStream([chunk]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ModelRouteRegistry()
    await registry.register(
        route(max_output_tokens=32768, context_length=1_000_000)
    )
    relay = ModelRelay(settings(), registry, client=client)

    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"model-a","max_tokens":32768}',
    )

    assert [part async for part in response.body_iterator] == [chunk]
    assert [item["max_tokens"] for item in forwarded] == [32768, 32703]
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_does_not_retry_other_400_responses() -> None:
    forwarded = 0
    error_body = b'{"error":{"message":"json decode error"}}'

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal forwarded
        del request
        forwarded += 1
        return httpx.Response(400, content=error_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ModelRouteRegistry()
    await registry.register(route())
    relay = ModelRelay(settings(), registry, client=client)

    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"model-a","max_tokens":4096}',
    )

    assert response.status_code == 400
    assert [part async for part in response.body_iterator] == [error_body]
    assert forwarded == 1
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_rejects_model_path_and_normalizes_output_limit_to_grant() -> None:
    registry = ModelRouteRegistry()
    await registry.register(route())
    relay = ModelRelay(settings(), registry)

    with pytest.raises(ModelRelayRequestRejected, match="operation is not allowed"):
        await relay.forward(
            route_token=ROUTE_TOKEN,
            operation="responses",
            body=b'{"model":"model-a","max_tokens":1}',
        )
    with pytest.raises(ModelRelayRequestRejected, match="model is not allowed"):
        await relay.forward(
            route_token=ROUTE_TOKEN,
            operation="chat/completions",
            body=b'{"model":"model-b","max_tokens":1}',
        )
    forwarded = relay._validate_request_body(
        route(max_output_tokens=4096),
        b'{"model":"model-a","max_tokens":4097}',
    )
    assert json.loads(forwarded)["max_tokens"] == 4096
    with pytest.raises(ModelRelayRequestRejected, match="positive output token limit"):
        await relay.forward(
            route_token=ROUTE_TOKEN,
            operation="chat/completions",
            body=b'{"model":"model-a","max_tokens":0}',
        )
    await relay.aclose()


@pytest.mark.asyncio
async def test_expired_or_rebound_route_is_rejected() -> None:
    registry = ModelRouteRegistry()
    expired = route(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    await registry.register(expired)

    with pytest.raises(ModelRelayRequestRejected, match="expired"):
        await registry.get(ROUTE_TOKEN)

    active = route()
    await registry.register(active)
    with pytest.raises(ModelRelayRequestRejected, match="already bound"):
        await registry.register(route(run_id="run-99999999"))


@pytest.mark.asyncio
async def test_pool_timeout_has_distinct_capacity_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("pool busy", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ModelRouteRegistry()
    await registry.register(route())
    relay = ModelRelay(settings(), registry, client=client)

    with pytest.raises(
        ModelRelayCapacityExhausted,
        match="MODEL_RELAY_CAPACITY_EXHAUSTED",
    ):
        await relay.forward(
            route_token=ROUTE_TOKEN,
            operation="chat/completions",
            body=b'{"model":"model-a","max_tokens":4096}',
        )
    await relay.aclose()


@pytest.mark.asyncio
async def test_transport_warmup_only_uses_models_get() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = ModelRelay(settings(), ModelRouteRegistry(), client=client)

    relay.start_transport_warmup()
    await relay.wait_for_transport_warmup()

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v1/models")
    ]
    await relay.aclose()


@pytest.mark.asyncio
async def test_application_relay_pool_uses_confirmed_derived_limits(
    monkeypatch,
) -> None:
    captured: dict = {}

    class FakeClient:
        is_closed = False

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def aclose(self) -> None:
            self.is_closed = True

    monkeypatch.setattr(relay_module.httpx, "AsyncClient", FakeClient)
    relay = ModelRelay(settings(), ModelRouteRegistry())

    await relay.shared_client()

    assert captured["trust_env"] is False
    assert captured["http2"] is False
    assert captured["limits"].max_connections == 600
    assert captured["limits"].max_keepalive_connections == 128
    assert captured["limits"].keepalive_expiry == 600
    assert captured["timeout"].connect == 5
    assert captured["timeout"].pool == 1
    assert captured["timeout"].write == 30
    assert captured["timeout"].read == 600
    await relay.aclose()


@pytest.mark.asyncio
async def test_responses_mode_observes_semantic_delta_without_changing_bytes() -> None:
    chunk = (
        b'data: {"type":"response.output_text.delta","delta":"answer",'
        b'"usage":{"output_tokens":1}}\n\n'
    )
    stream = ChunkStream([chunk])

    async def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["max_output_tokens"] == 64
        return httpx.Response(200, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ModelRouteRegistry()
    await registry.register(
        route(api_mode="responses", max_output_tokens=64)
    )
    observations = RecordingObservationSink()
    relay = ModelRelay(
        settings(),
        registry,
        client=client,
        observation_sink=observations,
    )

    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="responses",
        body=b'{"model":"model-a","max_output_tokens":64}',
    )

    assert [part async for part in response.body_iterator] == [chunk]
    assert observations.semantic_tokens == ["run-01234567"]
    assert observations.usages == [{"output_tokens": 1}]
    await relay.aclose()


@pytest.mark.asyncio
async def test_post_turn_route_reports_activity_without_polluting_ttft() -> None:
    chunk = (
        b'data: {"choices":[{"delta":{"content":"review"}}],'
        b'"usage":{"completion_tokens":2}}\n\n'
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkStream([chunk]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ModelRouteRegistry()
    await registry.register(
        route(
            authorization_scope="post_turn_review",
            activation_deadline_at=(
                datetime.now(timezone.utc) + timedelta(seconds=30)
            ),
        )
    )
    observations = RecordingObservationSink()
    relay = ModelRelay(
        settings(),
        registry,
        client=client,
        observation_sink=observations,
    )

    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=b'{"model":"model-a","max_tokens":64}',
    )
    assert [part async for part in response.body_iterator] == [chunk]
    assert observations.post_turn_runs == ["run-01234567"]
    assert observations.first_bytes == []
    assert observations.semantic_tokens == []
    assert observations.usages == [{"completion_tokens": 2}]
    await relay.aclose()


def test_model_relay_server_is_localhost_only_and_has_no_access_log() -> None:
    configured_settings = settings()
    registry = ModelRouteRegistry()
    relay = ModelRelay(configured_settings, registry)
    server = ModelRelayServer(configured_settings, relay, registry)

    assert server.config.host == "127.0.0.1"
    assert server.config.port == 9121
    assert server.config.access_log is False


@pytest.mark.asyncio
async def test_relay_uses_provider_endpoint_and_key_from_exact_route() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream([b"data: [DONE]\n\n"]),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ModelRouteRegistry()
    await registry.register(
        route(
            provider_base_url="https://coding.dashscope.aliyuncs.com/v1",
            provider_api_key="dashscope-route-secret",
        )
    )
    relay = ModelRelay(settings(), registry, client=client)

    response = await relay.forward(
        route_token=ROUTE_TOKEN,
        operation="chat/completions",
        body=(
            b'{"model":"model-a","stream":true,'
            b'"messages":[{"role":"user","content":"hello"}]}'
        ),
    )
    assert [chunk async for chunk in response.body_iterator] == [
        b"data: [DONE]\n\n"
    ]
    await asyncio.gather(*tuple(relay._provider_warmup_tasks))
    post_requests = [request for request in requests if request.method == "POST"]
    warmup_requests = [request for request in requests if request.method == "GET"]
    assert post_requests[0].url == (
        "https://coding.dashscope.aliyuncs.com/v1/chat/completions"
    )
    assert post_requests[0].headers["authorization"] == (
        "Bearer dashscope-route-secret"
    )
    assert [(request.url.path, request.headers["authorization"])
            for request in warmup_requests] == [
        ("/v1/models", "Bearer dashscope-route-secret")
    ]
    await relay.aclose()


@pytest.mark.asyncio
async def test_model_relay_http_app_returns_unmodified_stream_without_padding() -> None:
    chunk = b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream([chunk]),
        )

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = ModelRouteRegistry()
    await registry.register(route())
    relay = ModelRelay(settings(), registry, client=upstream_client)
    app = create_model_relay_app(relay, registry)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://relay.local",
    ) as client:
        models = await client.get(f"/model-relay/{ROUTE_TOKEN}/v1/models")
        response = await client.post(
            f"/model-relay/{ROUTE_TOKEN}/v1/chat/completions",
            content=b'{"model":"model-a","max_tokens":64}',
            headers={"content-type": "application/json"},
        )

    assert models.json()["data"] == [
        {
            "id": "model-a",
            "object": "model",
            "created": 0,
            "owned_by": "hermes-platform",
        }
    ]
    assert response.status_code == 200
    assert response.content == chunk
    assert b"padding" not in response.content
    await relay.aclose()
