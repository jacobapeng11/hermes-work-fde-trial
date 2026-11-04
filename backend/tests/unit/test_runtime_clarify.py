from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.runtime_clarify as runtime_clarify_module

from app.schemas.runtime_internal import CancelCommandPayload, RuntimeCommandAck
from app.schemas.runtime_clarify import RuntimeClarifyCreateRequest
from app.services.runtime_clarify import RuntimeClarifyService
from app.services.runtime_workspace import RuntimeApiKeyCodec


@pytest.fixture(autouse=True)
def _runtime_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_clarify_module,
        "load_runtime_command_admission_generation",
        AsyncMock(return_value=11),
    )


@pytest.mark.asyncio
async def test_clarify_parking_cancels_parent_runtime_run_before_returning_card() -> None:
    """The plugin must not resume Hermes with the parking marker in context."""

    class RecordingClient:
        def __init__(self) -> None:
            self.sent = []
            self.closed = False

        async def send(self, *, target, envelope):
            self.sent.append((target, envelope))
            return RuntimeCommandAck(
                command_id=envelope.command_id,
                state="completed",
                run_id="run-parent-0001",
            )

        async def close(self) -> None:
            self.closed = True

    client = RecordingClient()
    binding = SimpleNamespace(
        id=17,
        runtime_id="runtime-115",
        endpoint_host_header="runtime-115.hermes-runtimes.hermes.local",
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-secret"),
    )
    run = SimpleNamespace(runtime_run_id="run-parent-0001")
    service = RuntimeClarifyService(
        SimpleNamespace(),
        timeout_seconds=3600,
        command_client_factory=lambda: client,
    )

    assert await service._cancel_parent_run(
        binding=binding,
        run=run,
        request_id="clr_12345678",
    ) is True

    assert client.closed is True
    assert len(client.sent) == 1
    target, envelope = client.sent[0]
    assert target.runtime_id == "runtime-115"
    assert target.binding_id == "17"
    assert envelope.payload == CancelCommandPayload(
        run_id="run-parent-0001",
        clarify_request_id="clr_12345678",
    )
    assert envelope.command_id.startswith("cmd_")


@pytest.mark.asyncio
async def test_clarify_request_is_not_enabled_until_parent_cancel_is_acknowledged() -> None:
    timeline: list[str] = []
    binding = SimpleNamespace(
        id=17,
        org_id=1,
        user_id=2,
        runtime_id="runtime-115",
        endpoint_host_header="runtime-115.hermes-runtimes.hermes.local",
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-secret"),
    )
    session = SimpleNamespace(id=21, source_type="web")
    run = SimpleNamespace(id=22, runtime_run_id="run-parent-0001")

    class Db:
        def __init__(self) -> None:
            self.added = None

        async def scalar(self, statement):
            sql = str(statement)
            if "FROM hermes_runtime_binding" in sql:
                return binding
            if "FROM runtime_session" in sql:
                return session
            if "FROM runtime_run" in sql:
                return run
            if "FROM runtime_clarify_request" in sql:
                return None
            raise AssertionError(sql)

        def add(self, value) -> None:
            self.added = value

        async def commit(self) -> None:
            timeline.append("request-persisted")

        async def rollback(self) -> None:
            raise AssertionError("unexpected rollback")

    class Client:
        async def send(self, *, target, envelope):
            assert timeline == ["request-persisted"]
            assert envelope.payload == CancelCommandPayload(
                run_id="run-parent-0001",
                clarify_request_id=db.added.request_id,
            )
            timeline.append("parent-canceled")
            return RuntimeCommandAck(
                command_id=envelope.command_id,
                state="completed",
                run_id="run-parent-0001",
            )

        async def close(self) -> None:
            return None

    db = Db()
    receipt = await RuntimeClarifyService(
        db,  # type: ignore[arg-type]
        timeout_seconds=3600,
        command_client_factory=Client,
    ).accept_runtime_request(
        runtime_workspace_id=115,
        runtime_token="runtime-secret",
        payload=RuntimeClarifyCreateRequest(
            event_id="clarify:call-123",
            runtime_session_id="rs-115-test",
            tool_call_id="call-123",
            question="选择答案",
            choices=["A", "B"],
        ),
    )

    assert receipt.enabled is True
    assert receipt.request is db.added
    assert timeline == ["request-persisted", "parent-canceled"]
