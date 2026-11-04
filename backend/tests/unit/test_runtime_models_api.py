from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.v1.user.runtime_models as runtime_models_api
from app.api.v1.user.runtime_models import get_runtime_model_service
from app.api.v1.user.runtime_models import select_runtime_session_model
from app.config import settings
from app.schemas.runtime_chat import SessionModelSelectionRequest
from app.main import app
from app.services.model_catalog_cache import ModelCatalogCacheFillThrottledError
from app.services.runtime_models import RuntimeModelNotVisibleError, RuntimeSessionModelSwitchNotAllowedError
from app.services.runtime_workspace import Principal


def test_runtime_models_api_returns_visible_models_without_provider_secret(monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_AUTH_MODE", "dev")
    client = TestClient(app)

    response = client.get("/api/v1/user/runtime-workspace/models")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["model_entry_id"] == 1
    assert payload[0]["model_key"] == "gpt-4.1-mini"
    assert payload[0]["provider_display_name"] == "Platform OpenAI"
    assert payload[0]["is_default"] is True
    assert "api_key" not in payload[0]
    assert "api_key_encrypted" not in payload[0]
    assert "provider_base_url" not in payload[0]


def test_user_model_api_does_not_expose_personal_default_model_path(monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_AUTH_MODE", "dev")
    client = TestClient(app)

    response = client.put("/api/v1/user/runtime-workspace/default-model", json={"model_entry_id": 2})

    assert response.status_code == 404


def test_runtime_models_api_maps_catalog_refill_throttling_to_503(monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_AUTH_MODE", "dev")

    class ThrottledModelService:
        async def list_models(self, _principal):
            raise ModelCatalogCacheFillThrottledError("model catalog cache refill is in progress")

    async def service_override():
        return ThrottledModelService()

    app.dependency_overrides[get_runtime_model_service] = service_override
    try:
        response = TestClient(app).get("/api/v1/user/runtime-workspace/models")
    finally:
        app.dependency_overrides.pop(get_runtime_model_service, None)

    assert response.status_code == 503
    assert response.json() == {"detail": "model catalog cache refill is in progress"}


def test_session_model_api_rejects_invisible_model_with_403(monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_AUTH_MODE", "dev")

    class InvisibleModelService:
        async def select_session_model(self, **_kwargs):
            raise RuntimeModelNotVisibleError("model entry is not visible to this user")

    async def service_override():
        return InvisibleModelService()

    app.dependency_overrides[get_runtime_model_service] = service_override
    try:
        response = TestClient(app).put(
            "/api/v1/user/runtime-workspace/1/sessions/rs-1-1/model",
            json={"model_entry_id": 99},
        )
    finally:
        app.dependency_overrides.pop(get_runtime_model_service, None)

    assert response.status_code == 403
    assert response.json() == {"detail": "model entry is not visible"}


def test_session_model_api_rejects_switch_while_run_is_active_with_409(monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_AUTH_MODE", "dev")

    class ActiveRunModelService:
        async def select_session_model(self, **_kwargs):
            raise RuntimeSessionModelSwitchNotAllowedError(
                "cannot switch session model while a run is active"
            )

    async def service_override():
        return ActiveRunModelService()

    app.dependency_overrides[get_runtime_model_service] = service_override
    try:
        response = TestClient(app).put(
            "/api/v1/user/runtime-workspace/1/sessions/rs-1-1/model",
            json={"model_entry_id": 2},
        )
    finally:
        app.dependency_overrides.pop(get_runtime_model_service, None)

    assert response.status_code == 409
    assert response.json() == {"detail": "cannot switch session model while a run is active"}


@pytest.mark.asyncio
async def test_successful_model_commit_invalidates_session_then_returns_success(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Service:
        async def select_session_model(self, **_kwargs):
            calls.append("commit")
            return SimpleNamespace(
                runtime_workspace_id=1,
                runtime_session_id="rs-1-1",
                model_entry_id=2,
                applies_to="next_run",
                active_run_count=0,
            )

    class Client:
        async def mutate_session(self, **_kwargs):
            calls.append("invalidate")

        async def close(self):
            calls.append("close")

    async def load_target(_db, **_kwargs):
        calls.append("load")
        return SimpleNamespace()

    monkeypatch.setattr(
        runtime_models_api,
        "load_personal_runtime_command_target",
        load_target,
    )
    monkeypatch.setattr(runtime_models_api, "RuntimeCommandClient", Client)

    response = await select_runtime_session_model(
        runtime_workspace_id=1,
        runtime_session_id="rs-1-1",
        payload=SessionModelSelectionRequest(model_entry_id=2),
        principal=Principal(org_id=1, user_id=1),
        service=Service(),
        db=object(),
    )

    assert response.model_entry_id == 2
    assert calls == ["commit", "load", "invalidate", "close"]
