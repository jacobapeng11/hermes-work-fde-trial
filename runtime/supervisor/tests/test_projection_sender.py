from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path

import httpx
import pytest

from platform_supervisor.projection import ProjectionSender
import platform_supervisor.projection.sender as sender_module
from platform_supervisor.settings import SupervisorSettings
from platform_supervisor.storage import SQLiteStore


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
        BACKEND_INTERNAL_BASE_URL="http://backend.internal:8011",
    )


async def enqueue_terminal(store: SQLiteStore) -> None:
    await store.enqueue_projection(
        projection_id="run-00000001:terminal",
        projection_type="run.terminal",
        payload={
            "run_id": "run-00000001",
            "terminal_seq": 7,
            "config_revision": "revision-18",
            "state": "COMPLETED",
        },
        created_at=datetime.now(timezone.utc),
    )


def accepted_response() -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtime_id": "runtime-application-3",
        "accepted_projection_ids": ["run-00000001:terminal"],
        "rejected": [],
    }


async def wait_for_outbox_empty(store: SQLiteStore, *, timeout: float = 2) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not await store.load_projection_batch():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("projection outbox did not drain")


async def wait_for_log(caplog: pytest.LogCaptureFixture, pattern: str) -> str:
    deadline = asyncio.get_running_loop().time() + 1
    while asyncio.get_running_loop().time() < deadline:
        for record in caplog.records:
            message = record.getMessage()
            if pattern in message:
                return message
        await asyncio.sleep(0.01)
    raise AssertionError(f"log message not emitted: {pattern}")


@pytest.mark.asyncio
async def test_sender_acknowledges_only_backend_accepted_items(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    await enqueue_terminal(store)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=accepted_response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = ProjectionSender(settings(), store, client=client)

    assert await sender.deliver_once() == 1
    assert await store.load_projection_batch() == []
    request = requests[0]
    assert request.url == (
        "http://backend.internal:8011/internal/v1/runtime-projections"
    )
    assert request.headers["authorization"].startswith("Bearer runtime-only-")
    item = json.loads(request.content)["items"][0]
    assert item["run_id"] == "run-00000001"
    assert item["seq"] is None
    assert item["payload"] == {
        "run_id": "run-00000001",
        "terminal_seq": 7,
        "config_revision": "revision-18",
        "state": "COMPLETED",
    }
    await client.aclose()
    await store.close()


@pytest.mark.asyncio
async def test_sender_replaces_owned_client_after_transport_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    await enqueue_terminal(store)
    failed_requests: list[httpx.Request] = []
    successful_requests: list[httpx.Request] = []

    async def failed_handler(request: httpx.Request) -> httpx.Response:
        failed_requests.append(request)
        raise httpx.ConnectError("projection connection failed", request=request)

    async def successful_handler(request: httpx.Request) -> httpx.Response:
        successful_requests.append(request)
        return httpx.Response(200, json=accepted_response())

    first = httpx.AsyncClient(transport=httpx.MockTransport(failed_handler))
    second = httpx.AsyncClient(transport=httpx.MockTransport(successful_handler))
    clients = iter((first, second))

    def client_factory(**_kwargs: object) -> httpx.AsyncClient:
        return next(clients)

    monkeypatch.setattr(sender_module.httpx, "AsyncClient", client_factory)
    sender = ProjectionSender(settings(), store)
    caplog.set_level(logging.WARNING, logger=sender_module.__name__)
    sender.start()
    try:
        await wait_for_outbox_empty(store)
    finally:
        await sender.close()
        await store.close()

    assert len(failed_requests) == 1
    assert len(successful_requests) == 1
    assert first.is_closed
    assert second.is_closed
    assert any(
        "client_reset=true" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_sender_does_not_reuse_closed_owned_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    first = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    second = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    clients = iter((first, second))

    def client_factory(**_kwargs: object) -> httpx.AsyncClient:
        return next(clients)

    monkeypatch.setattr(sender_module.httpx, "AsyncClient", client_factory)
    sender = ProjectionSender(settings(), store)
    try:
        assert sender._shared_client() is first
        await first.aclose()
        assert sender._shared_client() is second
    finally:
        await sender.close()
        await second.aclose()
        await store.close()


@pytest.mark.asyncio
async def test_sender_never_replaces_or_closes_injected_client(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    await enqueue_terminal(store)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("projection connection failed", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = ProjectionSender(settings(), store, client=client)
    with pytest.raises(httpx.ConnectError):
        await sender.deliver_once()
    assert sender._shared_client() is client
    await sender.close()
    assert not client.is_closed
    await client.aclose()
    await store.close()


@pytest.mark.asyncio
async def test_sender_keeps_owned_client_after_http_status_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    await enqueue_terminal(store)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "temporarily unavailable"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda **_: client)
    sender = ProjectionSender(settings(), store)
    with pytest.raises(httpx.HTTPStatusError):
        await sender.deliver_once()
    assert sender._shared_client() is client
    assert not client.is_closed
    assert len(await store.load_projection_batch()) == 1
    await sender.close()
    await store.close()


@pytest.mark.asyncio
async def test_sender_failure_log_is_visible_and_redacted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    await enqueue_terminal(store)
    config = settings()
    secret = config.API_SERVER_KEY.get_secret_value()

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"transport rejected credential {secret} " + ("detail " * 80),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = ProjectionSender(config, store, client=client)
    caplog.set_level(logging.WARNING, logger=sender_module.__name__)
    sender.start()
    try:
        message = await wait_for_log(caplog, "Runtime projection delivery failed")
    finally:
        await sender.close()
        await client.aclose()
        await store.close()

    assert "error_type=ConnectError" in message
    assert "consecutive_failures=1" in message
    assert "retry_in_seconds=" in message
    assert "client_reset=false" in message
    assert secret not in message
    assert "[redacted]" in message
    assert len(message.split(" error=", 1)[1]) <= 240
