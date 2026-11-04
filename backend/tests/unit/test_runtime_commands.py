from __future__ import annotations

from datetime import datetime, timezone
import json

import httpx
import pytest
from types import SimpleNamespace

from app.schemas.runtime_internal import PrepareCommandPayload, RuntimeTurnIdentity
from app.services.runtime_commands import (
    RuntimeCommandClient,
    RuntimeCommandTarget,
    build_runtime_command,
    deterministic_command_id,
    load_runtime_command_admission_generation,
)
import app.services.runtime_commands as runtime_commands_module


def target() -> RuntimeCommandTarget:
    return RuntimeCommandTarget(
        runtime_id="runtime-1",
        binding_id="17",
        endpoint_host_header="runtime-1.runtimes.example",
        api_server_key="runtime-secret",
        admission_generation=11,
    )


@pytest.mark.asyncio
async def test_revocation_can_read_generation_while_new_work_is_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Control:
        def __init__(self, _db: object) -> None:
            pass

        async def snapshot(self):
            calls.append("snapshot")
            return SimpleNamespace(admission_generation=11)

        async def require_admission_open(self):
            raise AssertionError("revocation must not require new-work admission")

    monkeypatch.setattr(runtime_commands_module, "RuntimeMaintenanceService", Control)

    generation = await load_runtime_command_admission_generation(
        object(),  # type: ignore[arg-type]
        require_open=False,
    )

    assert generation == 11
    assert calls == ["snapshot"]


def envelope():
    destination = target()
    payload = PrepareCommandPayload(
        identity=RuntimeTurnIdentity(
            user_id=7,
            org_id=3,
            session_id="session-00000001",
            profile_id="default",
            runtime_agent_profile_id=51,
            profile_desired_revision=1,
            profile_applied_revision=1,
            profile_digest="sha256:" + "1" * 64,
            skill_digest="sha256:" + "2" * 64,
            mcp_digest="sha256:" + "3" * 64,
            knowledge_digest="sha256:" + "4" * 64,
            model_id="9",
            config_revision="revision-18",
            admission_generation=11,
            max_prompt_bytes=1048576,
            max_output_tokens=32768,
        ),
        priority="interactive",
    )
    return build_runtime_command(
        target=destination,
        command_id=deterministic_command_id(
            destination.runtime_id,
            "prepare",
            payload.identity.session_id,
            payload.identity.config_revision,
        ),
        payload=payload,
        issued_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_command_retries_same_envelope_and_trusted_host() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "command_id": body["command_id"],
                "state": "completed",
            },
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = RuntimeCommandClient(
        ingress_base_url="http://kourier.internal:30364",
        client=http_client,
    )

    ack = await client.send(target=target(), envelope=envelope())

    assert ack.state == "completed"
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert requests[0].headers["host"] == "runtime-1.runtimes.example"
    assert requests[0].headers["authorization"] == "Bearer runtime-secret"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_repeated_config_apply_uses_distinct_command_ids() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        bundle = body["payload"]["bundle"]
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "command_id": body["command_id"],
                "state": "completed",
                "result": {
                    "config_revision": bundle["config_revision"],
                    "config_digest": bundle["config_digest"],
                },
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RuntimeCommandClient(
        ingress_base_url="http://kourier.internal:30364",
        client=http_client,
    )
    destination = target()

    for _ in range(2):
        await client.apply_config(
            target=destination,
            config_revision="generation-6",
            config_digest="a" * 64,
            download_url="/internal/v1/config-bundles/application/43/6",
        )

    assert len(requests) == 2
    assert requests[0]["command_id"] != requests[1]["command_id"]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_repeated_session_invalidation_uses_distinct_command_ids() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "command_id": body["command_id"],
                "state": "completed",
                "result": {"invalidated": True},
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RuntimeCommandClient(
        ingress_base_url="http://kourier.internal:30364",
        client=http_client,
    )

    for _ in range(2):
        await client.mutate_session(
            target=target(),
            operation="invalidate",
            session_id="session-00000001",
            profile_id="expert-7",
        )

    assert requests[0]["payload"] == requests[1]["payload"]
    assert requests[0]["command_id"] != requests[1]["command_id"]
    await http_client.aclose()


@pytest.mark.asyncio
async def test_cancel_scheduled_candidate_requires_confirmed_linearized_status() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "command_id": body["command_id"],
                "state": "completed",
                "result": {"status": "already_running"},
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RuntimeCommandClient(
        ingress_base_url="http://kourier.internal:30364",
        client=http_client,
    )

    result = await client.cancel_scheduled_candidate(
        target=target(),
        session_id="session-00000001",
        scheduled_task_id=7,
    )

    assert result == "already_running"
    assert captured["payload"] == {
        "type": "cancel_scheduled_candidate",
        "session_id": "session-00000001",
        "scheduled_task_id": 7,
    }
    await http_client.aclose()


@pytest.mark.asyncio
async def test_wait_run_consumes_authenticated_realtime_event_stream() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/events"):
            events = [
                {
                    "schema_version": 1,
                    "type": "run.event",
                    "run_id": "run-realtime-0001",
                    "seq": 1,
                    "event_type": "chat.run.started",
                    "data": {},
                },
                {
                    "schema_version": 1,
                    "type": "run.event",
                    "run_id": "run-realtime-0001",
                    "seq": 2,
                    "event_type": "chat.reasoning.delta",
                    "data": {"delta": "真实思考"},
                },
                {
                    "schema_version": 1,
                    "type": "run.event",
                    "run_id": "run-realtime-0001",
                    "seq": 3,
                    "event_type": "chat.message.completed",
                    "data": {
                        "text": "最终回答",
                        "reasoning": "真实思考",
                    },
                },
                {
                    "schema_version": 1,
                    "type": "run.event",
                    "run_id": "run-realtime-0001",
                    "seq": 4,
                    "event_type": "chat.done",
                    "data": {"status": "completed"},
                },
            ]
            return httpx.Response(
                200,
                content="".join(
                    json.dumps(event, ensure_ascii=False) + "\n"
                    for event in events
                ).encode(),
                headers={"content-type": "application/x-ndjson"},
            )
        return httpx.Response(
            200,
            json={
                "run_id": "run-realtime-0001",
                "through_seq": 4,
                "status": "COMPLETED",
                "answer": "最终回答",
                "events": [],
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RuntimeCommandClient(
        ingress_base_url="http://kourier.internal:30364",
        client=http_client,
    )
    projected: list[tuple[str, dict[str, object]]] = []

    snapshot = await client.wait_run(
        target=target(),
        run_id="run-realtime-0001",
        event_sink=lambda name, data: _capture_event(
            projected,
            name,
            data,
        ),
        poll_interval_seconds=0,
    )

    assert snapshot["status"] == "COMPLETED"
    assert [name for name, _data in projected] == [
        "chat.run.started",
        "chat.reasoning.delta",
        "chat.message.completed",
        "chat.done",
    ]
    assert projected[1][1]["delta"] == "真实思考"
    stream_request = next(
        request for request in requests if request.url.path.endswith("/events")
    )
    assert stream_request.headers["host"] == "runtime-1.runtimes.example"
    assert stream_request.headers["authorization"] == "Bearer runtime-secret"
    await http_client.aclose()


async def _capture_event(
    target_events: list[tuple[str, dict[str, object]]],
    name: str,
    data: dict[str, object],
) -> None:
    target_events.append((name, data))
