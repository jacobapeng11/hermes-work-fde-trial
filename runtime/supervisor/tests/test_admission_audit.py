from __future__ import annotations

import json

import httpx
import pytest

from platform_supervisor.admission_audit import AdmissionAuditReporter


@pytest.mark.asyncio
async def test_admission_audit_is_best_effort_and_omits_prompt() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "runtime_id": "runtime-1",
                "accepted_event_ids": [body["items"][0]["event_id"]],
                "duplicate_event_ids": [],
                "rejected_event_ids": [],
            },
        )

    reporter = AdmissionAuditReporter(
        backend_base_url="http://backend.test",
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="runtime-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    event_id = reporter.record(
        operation="run.submit",
        status="rejected",
        outcome="rejected",
        stage="edge.run_submit.persist_dispatch_intent",
        session_id="rs-12345678",
        client_message_id="message-12345678",
        error_code="SESSION_BUSY",
        prompt_bytes=123,
        prompt="不应写入审计",
    )
    await reporter.flush()

    assert event_id
    assert len(requests) == 1
    body = requests[0].content.decode()
    assert '"prompt_bytes":123' in body
    assert "prompt\"" not in body
    assert requests[0].headers["authorization"] == "Bearer runtime-secret"
    await reporter.close()


@pytest.mark.asyncio
async def test_admission_audit_retries_transient_failure_with_same_event_id() -> None:
    event_ids: list[str] = []
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        body = json.loads(request.content)
        event_ids.append(body["items"][0]["event_id"])
        if attempts == 1:
            return httpx.Response(503, json={"detail": "temporarily unavailable"})
        return httpx.Response(
            200,
            json={
                "runtime_id": "runtime-1",
                "accepted_event_ids": [event_ids[-1]],
                "duplicate_event_ids": [],
                "rejected_event_ids": [],
            },
        )

    reporter = AdmissionAuditReporter(
        backend_base_url="http://backend.test",
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="runtime-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_backoff_seconds=0,
    )
    event_id = reporter.record(
        operation="session.prepare",
        status="rejected",
        outcome="rejected",
        stage="edge.session_prepare.dashboard_prewarm",
    )

    await reporter.flush()

    assert attempts == 2
    assert event_ids == [event_id, event_id]
    await reporter.close()


@pytest.mark.asyncio
async def test_admission_audit_does_not_retry_permanent_4xx() -> None:
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(409, json={"detail": "stale binding"})

    reporter = AdmissionAuditReporter(
        backend_base_url="http://backend.test",
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="runtime-secret",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_backoff_seconds=0,
    )
    reporter.record(
        operation="session.prepare",
        status="rejected",
        outcome="rejected",
        stage="edge.session_prepare.dashboard_prewarm",
    )

    await reporter.flush()

    assert attempts == 1
    await reporter.close()
