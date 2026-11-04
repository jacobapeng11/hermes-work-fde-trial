import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1.user.chat import (
    _history_messages,
    get_chat_history,
    get_chat_log_projector,
    get_run_activity,
    get_runtime_chat_service,
    record_client_timing,
)
from app.api.v1.user.runtime_workspace import get_principal
from app.main import app
from app.schemas.runtime_chat import RuntimeChatClientTimingRequest
from app.services.chat_log import ChatLogHistoryControl, ChatLogSessionNotFound
from app.services.runtime_chat import (
    RuntimeActiveRun,
    RuntimeRunReplayControl,
)
from app.services.runtime_workspace import Principal


@pytest.fixture(autouse=True)
def default_principal_override():
    app.dependency_overrides[get_principal] = lambda: Principal(
        org_id=1,
        user_id=1,
    )
    app.dependency_overrides[get_chat_log_projector] = lambda: None
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_principal, None)
        app.dependency_overrides.pop(get_chat_log_projector, None)
        app.dependency_overrides.pop(get_runtime_chat_service, None)


def test_removed_backend_execution_routes_are_not_registered() -> None:
    paths = {
        route.path
        for route in app.routes
        if hasattr(route, "path")
    }
    assert "/api/v1/user/chat/stream" not in paths
    assert "/api/v1/user/chat/reconnect" not in paths
    assert "/api/v1/user/chat/stop" not in paths
    assert "/api/v1/user/chat" not in paths


def test_active_run_api_returns_null_when_session_is_idle() -> None:
    response = TestClient(app).get(
        "/api/v1/user/chat/runs/active",
        params={
            "runtime_workspace_id": 1,
            "runtime_session_id": "rs-1-1",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"run": None}


def test_active_run_api_exposes_checkpoint_and_source() -> None:
    source = {
        "type": "scheduled",
        "scheduled_task_id": 123,
        "scheduled_task_name": "每日工作总结",
    }

    class ActiveRunService:
        async def get_active_run(self, **_kwargs):
            return RuntimeActiveRun(
                1,
                "rs-1-1",
                "rr-1-1",
                "running",
                4,
                2,
                source,
            )

    app.dependency_overrides[get_runtime_chat_service] = (
        lambda: ActiveRunService()
    )
    response = TestClient(app).get(
        "/api/v1/user/chat/runs/active",
        params={
            "runtime_workspace_id": 1,
            "runtime_session_id": "rs-1-1",
        },
    )
    assert response.status_code == 200
    assert response.json()["run"]["history_message_count_before_run"] == 2
    assert response.json()["run"]["source"] == source


@pytest.mark.asyncio
async def test_client_timing_validates_personal_owner_without_ledger_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []
    caplog.set_level(logging.INFO, logger="uvicorn.error.runtime_timing")

    class TimingService:
        async def get_run_replay_control(self, **kwargs):
            calls.append(kwargs["runtime_run_id"])
            return RuntimeRunReplayControl(
                1,
                "rs-1-1",
                kwargs["runtime_run_id"],
                "running",
            )

    response = await record_client_timing(
        runtime_run_id="rr-1",
        payload=RuntimeChatClientTimingRequest(
            sample_kind="lifecycle",
            request_to_first_event_ms=20.0,
            request_to_first_visible_received_ms=30.0,
            request_to_first_visible_commit_ms=35.0,
            request_to_first_visible_paint_ms=48.0,
            document_visibility="visible",
            reconnect_before_first_visible=False,
            send_overlapped_prewarm=False,
            required_prewarm_wait_ms=123.456,
        ),
        principal=Principal(org_id=1, user_id=1),
        service=TimingService(),  # type: ignore[arg-type]
    )
    assert response.status_code == 204
    assert calls == ["rr-1"]
    assert "required_prewarm_wait_ms=123.456" in caplog.text


@pytest.mark.asyncio
async def test_client_timing_application_owner_boundary() -> None:
    class TimingService:
        async def get_run_replay_control(self, **_kwargs):
            raise AssertionError("application Run must not use personal lookup")

    class TimingDb:
        def __init__(self, owned: bool) -> None:
            self.owned = owned

        async def scalar(self, _statement):
            return 17 if self.owned else None

    payload = RuntimeChatClientTimingRequest(
        request_to_first_visible_received_ms=30.0,
        request_to_first_visible_commit_ms=35.0,
        request_to_first_visible_paint_ms=48.0,
        document_visibility="visible",
    )
    response = await record_client_timing(
        runtime_run_id="app_run_owned",
        payload=payload,
        principal=Principal(org_id=1, user_id=1),
        service=TimingService(),  # type: ignore[arg-type]
        db=TimingDb(True),  # type: ignore[arg-type]
    )
    assert response.status_code == 204

    with pytest.raises(HTTPException) as raised:
        await record_client_timing(
            runtime_run_id="app_run_unowned",
            payload=payload,
            principal=Principal(org_id=1, user_id=1),
            service=TimingService(),  # type: ignore[arg-type]
            db=TimingDb(False),  # type: ignore[arg-type]
        )
    assert raised.value.status_code == 404


def test_client_timing_rejects_unbounded_or_out_of_order_sample() -> None:
    with pytest.raises(ValidationError):
        RuntimeChatClientTimingRequest(
            request_to_first_visible_received_ms=40.0,
            request_to_first_visible_commit_ms=35.0,
            request_to_first_visible_paint_ms=3_600_001,
            document_visibility="visible",
        )


@pytest.mark.asyncio
async def test_run_activity_returns_only_sanitized_public_events() -> None:
    class ReplayService:
        async def get_run_replay_control(self, **_kwargs):
            return RuntimeRunReplayControl(
                1,
                "rs-1-1",
                "rr-activity",
                "completed",
            )

        async def list_events(self, **_kwargs):
            return [
                SimpleNamespace(
                    seq=1,
                    event_name="chat.reasoning.delta",
                    payload={"delta": "分析资料"},
                ),
                SimpleNamespace(
                    seq=2,
                    event_name="chat.tool.event",
                    payload={
                        "name": "web_search",
                        "status": "completed",
                        "result": '{"token":"secret","results":2}',
                    },
                ),
                SimpleNamespace(
                    seq=3,
                    event_name="internal.debug",
                    payload={"trace": "private"},
                ),
                SimpleNamespace(
                    seq=4,
                    event_name="chat.done",
                    payload={"status": "completed"},
                ),
            ]

    result = await get_run_activity(
        runtime_run_id="rr-activity",
        runtime_workspace_id=1,
        runtime_session_id="rs-1-1",
        principal=Principal(org_id=1, user_id=1),
        service=ReplayService(),  # type: ignore[arg-type]
    )
    assert [event.event for event in result.events] == [
        "chat.reasoning.delta",
        "chat.tool.event",
        "chat.done",
    ]
    assert result.events[1].data["result"] == (
        '{\n  "token": "••••",\n  "results": 2\n}'
    )


@pytest.mark.asyncio
async def test_run_activity_enforces_session_and_retention_boundaries() -> None:
    class ReplayService:
        async def get_run_replay_control(self, **_kwargs):
            return RuntimeRunReplayControl(
                1,
                "rs-1-1",
                "rr-activity",
                "completed",
            )

        async def list_events(self, **_kwargs):
            return []

    with pytest.raises(HTTPException) as mismatched:
        await get_run_activity(
            runtime_run_id="rr-activity",
            runtime_workspace_id=1,
            runtime_session_id="rs-other",
            principal=Principal(org_id=1, user_id=1),
            service=ReplayService(),  # type: ignore[arg-type]
        )
    assert mismatched.value.status_code == 404

    with pytest.raises(HTTPException) as expired:
        await get_run_activity(
            runtime_run_id="rr-activity",
            runtime_workspace_id=1,
            runtime_session_id="rs-1-1",
            principal=Principal(org_id=1, user_id=1),
            service=ReplayService(),  # type: ignore[arg-type]
        )
    assert expired.value.status_code == 410


def test_chat_history_contract_mode_shape() -> None:
    response = TestClient(app).get(
        "/api/v1/user/chat/history",
        params={
            "runtime_workspace_id": 1,
            "runtime_session_id": "rs-1-1",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "runtime_workspace_id": 1,
        "runtime_session_id": "rs-1-1",
        "source": "mysql",
        "consistency": "projection_preview",
        "generated_at": payload["generated_at"],
        "has_active_run": False,
        "messages": [],
    }
    assert payload["generated_at"].endswith("Z")


@pytest.mark.asyncio
async def test_chat_history_reads_mysql_projection_without_runtime() -> None:
    class Projector:
        async def project_owned_session(self, **_kwargs):
            return 0

        async def history_for_owner(self, **_kwargs):
            return ChatLogHistoryControl(
                runtime_workspace_id=1,
                runtime_session_id="rs-1-1",
                session_id=5,
                complete=True,
                has_active_run=False,
                messages=[
                    {"role": "user", "content": "问题"},
                    {
                        "role": "tool",
                        "name": "write_file",
                        "status": "completed",
                        "result": (
                            '{"token":"secret",'
                            '"path":"/tmp/demo.pptx"}'
                        ),
                    },
                    {"role": "assistant", "content": "答案"},
                ],
            )

    class UnexpectedService:
        async def get_session_history_control(self, **_kwargs):
            raise AssertionError("history must not query Runtime")

    result = await get_chat_history(
        runtime_workspace_id=1,
        runtime_session_id="rs-1-1",
        principal=Principal(org_id=1, user_id=1),
        service=UnexpectedService(),  # type: ignore[arg-type]
        chat_log_projector=Projector(),  # type: ignore[arg-type]
    )
    assert result.source == "mysql"
    assert result.consistency == "projection_preview"
    assert result.has_active_run is False
    assert result.generated_at.tzinfo is not None
    assert result.messages[0] == {"role": "user", "content": "问题"}
    assert result.messages[1]["result"] == (
        '{\n  "token": "••••",\n  "path": "<path>/demo.pptx"\n}'
    )
    assert result.messages[2] == {"role": "assistant", "content": "答案"}


@pytest.mark.asyncio
async def test_chat_history_preserves_owner_not_found_boundary() -> None:
    class Projector:
        async def project_owned_session(self, **_kwargs):
            return 0

        async def history_for_owner(self, **_kwargs):
            raise ChatLogSessionNotFound("runtime session not found")

    with pytest.raises(HTTPException) as raised:
        await get_chat_history(
            runtime_workspace_id=1,
            runtime_session_id="rs-other-user",
            principal=Principal(org_id=1, user_id=1),
            service=object(),  # type: ignore[arg-type]
            chat_log_projector=Projector(),  # type: ignore[arg-type]
        )
    assert raised.value.status_code == 404


def test_history_messages_sanitize_only_tool_messages() -> None:
    messages = _history_messages(
        {
            "messages": [
                {"role": "user", "content": "/root/user-visible-text"},
                {
                    "role": "tool",
                    "name": "read_file",
                    "content": {
                        "path": "/root/output.txt",
                        "api_key": "secret",
                    },
                    "token": "drop-me",
                },
            ]
        }
    )
    assert messages[0] == {
        "role": "user",
        "content": "/root/user-visible-text",
    }
    assert messages[1]["result"] == (
        '{\n  "path": "<path>/output.txt",\n  "api_key": "••••"\n}'
    )


def test_chat_history_rejects_unknown_contract_session() -> None:
    response = TestClient(app).get(
        "/api/v1/user/chat/history",
        params={
            "runtime_workspace_id": 1,
            "runtime_session_id": "missing",
        },
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "runtime session not found"}
