import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.api.v1.user.runtime_sessions as runtime_sessions_api

from app.api.v1.user.runtime_sessions import (
    create_runtime_session,
    delete_runtime_session,
    get_runtime_session_service,
    rename_runtime_session,
)
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.main import app
from app.config import settings
from app.schemas.runtime_chat import (
    CreateRuntimeSessionRequest,
    RenameRuntimeSessionRequest,
    RuntimeSessionSummary,
)
from app.services.runtime_models import RuntimeModelRecord
from app.services.runtime_sessions import (
    RuntimeSessionActiveRunError,
    RuntimeSessionLatestRunRecord,
    RuntimeSessionRecord,
)
from app.services.runtime_workspace import Principal


MATERIALIZED_AT = datetime(2026, 7, 23, tzinfo=timezone.utc)


def test_runtime_session_summary_serializes_latest_run_projection() -> None:
    payload = RuntimeSessionSummary.model_validate(
        RuntimeSessionRecord(
            session_id=1,
            runtime_workspace_id=10,
            org_id=7,
            user_id=9,
            runtime_session_id="rs-10-1",
            title="Projected",
            source_type="web",
            message_count=2,
            session_model_entry_id=3,
            status="active",
            latest_run=RuntimeSessionLatestRunRecord(
                runtime_run_id="run-1",
                status="completed",
                started_at=MATERIALIZED_AT,
                finished_at=MATERIALIZED_AT,
            ),
        )
    ).model_dump(mode="json")

    assert payload["latest_run"] == {
        "runtime_run_id": "run-1",
        "status": "completed",
        "started_at": "2026-07-23T00:00:00Z",
        "finished_at": "2026-07-23T00:00:00Z",
    }


def test_runtime_sessions_api_creates_and_lists_platform_session_shape() -> None:
    client = TestClient(app)

    create_response = client.post(
        "/api/v1/user/runtime-workspace/1/sessions",
        json={"title": "First chat", "model_entry_id": 1, "source_type": "web"},
    )

    assert create_response.status_code == 200
    created = create_response.json()
    assert created["runtime_workspace_id"] == 1
    assert created["runtime_session_id"].startswith("rs-1-")
    assert created["session_model_entry_id"] == 1
    assert created["message_count"] == 0
    assert "workspace_id" not in created
    assert "copaw_session_id" not in created

    list_response = client.get("/api/v1/user/runtime-workspace/1/sessions")

    assert list_response.status_code == 200
    payload = list_response.json()
    assert any(item["runtime_session_id"] == created["runtime_session_id"] for item in payload)
    assert "api_key" not in payload[0]


@pytest.mark.asyncio
async def test_runtime_session_without_explicit_model_uses_admin_default(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "HERMES_RUNTIME_STORAGE_MAINTENANCE",
        False,
    )
    captured_model_id: int | None = None

    class ModelService:
        async def list_models(self, _principal):
            return [
                RuntimeModelRecord(
                    model_entry_id=7,
                    model_key="model-a",
                    display_name="Model A",
                    provider_id=1,
                    provider_display_name="Provider",
                ),
                RuntimeModelRecord(
                    model_entry_id=21,
                    model_key="model-b",
                    display_name="Model B",
                    provider_id=1,
                    provider_display_name="Provider",
                    is_default=True,
                ),
            ]

    class SessionService:
        async def create_session(self, *, command, **_kwargs):
            nonlocal captured_model_id
            captured_model_id = command.model_entry_id
            return RuntimeSessionRecord(
                session_id=1,
                runtime_workspace_id=command.runtime_workspace_id,
                org_id=1,
                user_id=1,
                runtime_session_id="rs-1-default",
                title=None,
                source_type="web",
                message_count=0,
                session_model_entry_id=command.model_entry_id,
                status="active",
            )

    result = await create_runtime_session(
        runtime_workspace_id=1,
        payload=CreateRuntimeSessionRequest(source_type="web"),
        principal=Principal(org_id=1, user_id=1),
        service=SessionService(),
        model_service=ModelService(),
    )

    assert captured_model_id == 21
    assert result.session_model_entry_id == 21


def test_runtime_sessions_api_deletes_empty_platform_session() -> None:
    client = TestClient(app)
    created = client.post(
        "/api/v1/user/runtime-workspace/1/sessions",
        json={"title": "Disposable", "source_type": "web"},
    ).json()

    response = client.delete(
        f"/api/v1/user/runtime-workspace/1/sessions/{created['runtime_session_id']}"
    )

    assert response.status_code == 204
    listed = client.get("/api/v1/user/runtime-workspace/1/sessions").json()
    assert all(item["runtime_session_id"] != created["runtime_session_id"] for item in listed)


@pytest.mark.asyncio
async def test_session_delete_commits_without_backend_hot_channel(monkeypatch) -> None:
    calls: list[str] = []

    class SessionService:
        async def delete_session(self, **_kwargs):
            calls.append("delete")

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "contract")

    response = await delete_runtime_session(
        runtime_workspace_id=1,
        runtime_session_id="rs-1-delete",
        principal=Principal(org_id=1, user_id=1),
        service=SessionService(),
        chat_service=object(),
    )

    assert response.status_code == 204
    assert calls == ["delete"]


@pytest.mark.asyncio
async def test_materialized_session_delete_treats_missing_hermes_row_as_success(
    monkeypatch,
) -> None:
    calls: list[str] = []
    record = RuntimeSessionRecord(
        session_id=13,
        runtime_workspace_id=1,
        org_id=1,
        user_id=1,
        runtime_session_id="rs-1-gone",
        title="Gone",
        source_type="web",
        message_count=0,
        session_model_entry_id=1,
        status="active",
        hermes_materialized_at=MATERIALIZED_AT,
    )

    class SessionService:
        async def get_session(self, **_kwargs):
            calls.append("platform.get")
            return record

        async def mark_session_deleting(self, **_kwargs):
            calls.append("platform.deleting")

        async def delete_session(self, **_kwargs):
            calls.append("platform.delete")

    class ChatService:
        async def get_session_history_control(self, **kwargs):
            assert kwargs["include_deleting"] is True
            calls.append("runtime.resolve")
            return SimpleNamespace(
                endpoint=RuntimeEndpoint(
                    runtime_workspace_id=1,
                    runtime_id="runtime-1",
                    endpoint_base_url="http://runtime",
                    endpoint_host_header="runtime.internal",
                    api_key="secret",
                ),
                hermes_profile_name="default",
            )

    async def command_target(*_args, **_kwargs):
        return object()

    class CommandClient:
        async def mutate_session(self, **kwargs):
            assert kwargs["operation"] == "delete"
            assert kwargs["session_id"] == "rs-1-gone"
            calls.append("runtime.delete")
            raise runtime_sessions_api.RuntimeCommandError(
                "Runtime command rejected with HTTP 404"
            )

        async def close(self):
            calls.append("runtime.close")

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    monkeypatch.setattr(
        runtime_sessions_api,
            "load_personal_runtime_command_target",
        command_target,
    )
    monkeypatch.setattr(runtime_sessions_api, "RuntimeCommandClient", CommandClient)

    response = await delete_runtime_session(
        runtime_workspace_id=1,
        runtime_session_id="rs-1-gone",
        principal=Principal(org_id=1, user_id=1),
        db=object(),
        service=SessionService(),
        chat_service=ChatService(),
    )

    assert response.status_code == 204
    assert calls == [
        "platform.get",
        "platform.deleting",
        "runtime.resolve",
        "runtime.delete",
        "runtime.close",
        "platform.delete",
    ]


@pytest.mark.asyncio
async def test_deleted_expert_session_delete_skips_removed_hermes_profile(monkeypatch) -> None:
    calls: list[str] = []
    record = RuntimeSessionRecord(
        session_id=14,
        runtime_workspace_id=1,
        org_id=1,
        user_id=1,
        runtime_session_id="rs-1-deleted-expert",
        title="Expert history",
        source_type="web",
        message_count=2,
        session_model_entry_id=1,
        status="expert_deleted",
        hermes_materialized_at=MATERIALIZED_AT,
    )

    class SessionService:
        async def get_session(self, **_kwargs):
            calls.append("platform.get")
            return record

        async def mark_session_deleting(self, **_kwargs):
            calls.append("platform.deleting")

        async def delete_session(self, **_kwargs):
            calls.append("platform.delete")

    class ChatService:
        async def get_session_history_control(self, **_kwargs):
            calls.append("runtime.resolve")
            return SimpleNamespace(endpoint=object())

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")

    response = await delete_runtime_session(
        runtime_workspace_id=1,
        runtime_session_id="rs-1-deleted-expert",
        principal=Principal(org_id=1, user_id=1),
        db=object(),
        service=SessionService(),
        chat_service=ChatService(),
    )

    assert response.status_code == 204
    assert calls == ["platform.get", "platform.delete"]


def test_runtime_sessions_api_renames_empty_platform_session() -> None:
    record = RuntimeSessionRecord(
        session_id=6,
        runtime_workspace_id=1,
        org_id=1,
        user_id=1,
        runtime_session_id="rs-1-empty",
        title="Draft",
        source_type="web",
        message_count=0,
        session_model_entry_id=1,
        status="active",
    )

    class SessionService:
        async def get_session(self, **_kwargs):
            return record

        async def assert_title_available(self, **_kwargs):
            return None

        async def rename_session(self, *, title: str, **_kwargs):
            return replace(record, title=title)

    class UnusedDependency:
        def __getattr__(self, _name):
            raise AssertionError("empty session rename must not contact Hermes")

    response = asyncio.run(rename_runtime_session(
        runtime_workspace_id=1,
        runtime_session_id="rs-1-empty",
        payload=RenameRuntimeSessionRequest(title="  Release   plan  "),
        principal=Principal(org_id=1, user_id=1),
        db=object(),
        service=SessionService(),
        chat_service=UnusedDependency(),
    ))

    assert response.title == "Release plan"


def test_runtime_sessions_api_validates_rename_title() -> None:
    with pytest.raises(ValueError):
        RenameRuntimeSessionRequest(title="   ")
    with pytest.raises(ValueError):
        RenameRuntimeSessionRequest(title="x" * 101)


def test_runtime_sessions_api_syncs_materialized_title_to_runtime(
    monkeypatch,
) -> None:
    calls: list[str] = []
    record = RuntimeSessionRecord(
        session_id=7,
        runtime_workspace_id=1,
        org_id=1,
        user_id=1,
        runtime_session_id="rs-1-materialized",
        title="Draft",
        source_type="web",
        message_count=2,
        session_model_entry_id=1,
        status="active",
        hermes_materialized_at=MATERIALIZED_AT,
    )

    class SessionService:
        async def get_session(self, **_kwargs):
            calls.append("platform.get")
            return record

        async def assert_title_available(self, **_kwargs):
            calls.append("platform.validate")

        async def rename_session(self, *, title: str, **_kwargs):
            calls.append("platform.rename")
            return replace(record, title=title)

    class ChatService:
        async def get_session_history_control(self, **_kwargs):
            calls.append("runtime.resolve")
            return SimpleNamespace(
                endpoint=RuntimeEndpoint(
                    runtime_workspace_id=1,
                    runtime_id="runtime-1",
                    endpoint_base_url="http://runtime",
                    endpoint_host_header="runtime.internal",
                    api_key="secret",
                ),
                hermes_profile_name="default",
            )

    async def command_target(*_args, **_kwargs):
        return object()

    class CommandClient:
        async def mutate_session(self, **kwargs):
            calls.append("runtime.rename")
            assert kwargs["operation"] == "rename"
            assert kwargs["session_id"] == "rs-1-materialized"
            assert kwargs["profile_id"] == "default"
            assert kwargs["title"] == "Release plan"

        async def close(self):
            calls.append("runtime.close")

    monkeypatch.setattr(
        runtime_sessions_api,
            "load_personal_runtime_command_target",
        command_target,
    )
    monkeypatch.setattr(runtime_sessions_api, "RuntimeCommandClient", CommandClient)

    response = asyncio.run(rename_runtime_session(
        runtime_workspace_id=1,
        runtime_session_id="rs-1-materialized",
        payload=RenameRuntimeSessionRequest(title="Release plan"),
        principal=Principal(org_id=1, user_id=1),
        db=object(),
        service=SessionService(),
        chat_service=ChatService(),
    ))

    assert response.title == "Release plan"
    assert calls == [
        "platform.get",
        "platform.validate",
        "runtime.resolve",
        "runtime.rename",
        "runtime.close",
        "platform.rename",
    ]


def test_runtime_sessions_api_keeps_platform_title_when_runtime_rename_fails(
    monkeypatch,
) -> None:
    renamed = False
    record = RuntimeSessionRecord(
        session_id=8,
        runtime_workspace_id=1,
        org_id=1,
        user_id=1,
        runtime_session_id="rs-1-failing",
        title="Draft",
        source_type="web",
        message_count=1,
        session_model_entry_id=1,
        status="active",
        hermes_materialized_at=MATERIALIZED_AT,
    )

    class SessionService:
        async def get_session(self, **_kwargs):
            return record

        async def assert_title_available(self, **_kwargs):
            return None

        async def rename_session(self, **_kwargs):
            nonlocal renamed
            renamed = True
            return record

    class ChatService:
        async def get_session_history_control(self, **_kwargs):
            return SimpleNamespace(
                endpoint=object(),
                hermes_profile_name="default",
            )

    async def command_target(*_args, **_kwargs):
        return object()

    class CommandClient:
        async def mutate_session(self, **_kwargs):
            raise runtime_sessions_api.RuntimeCommandError(
                "Runtime command rejected with HTTP 409"
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        runtime_sessions_api,
            "load_personal_runtime_command_target",
        command_target,
    )
    monkeypatch.setattr(runtime_sessions_api, "RuntimeCommandClient", CommandClient)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(rename_runtime_session(
            runtime_workspace_id=1,
            runtime_session_id="rs-1-failing",
            payload=RenameRuntimeSessionRequest(title="Conflicting"),
            principal=Principal(org_id=1, user_id=1),
            db=object(),
            service=SessionService(),
            chat_service=ChatService(),
        ))

    assert raised.value.status_code == 502
    assert renamed is False


def test_runtime_sessions_api_rejects_delete_when_session_has_active_run() -> None:
    class ActiveRunService:
        async def delete_session(self, **_kwargs):
            raise RuntimeSessionActiveRunError("runtime session has an active run")

    async def service_override():
        return ActiveRunService()

    app.dependency_overrides[get_runtime_session_service] = service_override
    try:
        response = TestClient(app, raise_server_exceptions=False).delete(
            "/api/v1/user/runtime-workspace/1/sessions/rs-1-active"
        )
    finally:
        app.dependency_overrides.pop(get_runtime_session_service, None)

    assert response.status_code == 409
    assert response.json() == {"detail": "runtime session has an active run"}


def test_runtime_sessions_api_rejects_create_during_storage_maintenance(monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_RUNTIME_STORAGE_MAINTENANCE", True)

    response = TestClient(app).post("/api/v1/user/runtime-workspace/1/sessions", json={"source_type": "web"})

    assert response.status_code == 503
    assert response.json() == {"detail": "runtime storage maintenance is in progress"}
