from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.v1.admin.knowledge_providers import (
    get_weknora_rerank_service,
    get_weknora_rerank_setting_service,
)
from app.main import app
from app.services.weknora_rerank_governance import (
    WeKnoraRerankConfigSnapshot,
    WeKnoraRerankGovernanceError,
    WeKnoraRerankGovernanceService,
    WeKnoraRerankModel,
)


class FakeService(WeKnoraRerankGovernanceService):
    def __init__(self, snapshot: WeKnoraRerankConfigSnapshot) -> None:
        super().__init__(api_base_url="http://unused", api_key="unused", client=None)
        self.snapshot = snapshot
        self.updated_with: list[str] = []

    async def get_config(self) -> WeKnoraRerankConfigSnapshot:
        return self.snapshot

    async def update_config(self, *, rerank_model_id: str) -> WeKnoraRerankConfigSnapshot:
        self.updated_with.append(rerank_model_id)
        return WeKnoraRerankConfigSnapshot(
            configured_model_id=rerank_model_id,
            effective_model_id=rerank_model_id,
            config_source="tenant",
            models=self.snapshot.models,
        )


class FakeSettingService:
    def __init__(self, mode: str = "model") -> None:
        self.mode = mode
        self.updated_modes: list[tuple[str, int]] = []

    async def get_mode(self) -> str:
        return self.mode

    async def set_mode(self, *, mode: str, updated_by_user_id: int) -> str:
        self.updated_modes.append((mode, updated_by_user_id))
        self.mode = mode
        return mode


def snapshot() -> WeKnoraRerankConfigSnapshot:
    return WeKnoraRerankConfigSnapshot(
        configured_model_id="rerank-a",
        effective_model_id="rerank-a",
        config_source="tenant",
        models=(
            WeKnoraRerankModel(
                id="rerank-a",
                name="qwen3-rerank",
                display_name="qwen3-rerank",
                source="remote",
                status="active",
            ),
        ),
    )


def install_service(
    service: WeKnoraRerankGovernanceService,
    settings: FakeSettingService,
) -> None:
    app.dependency_overrides[get_weknora_rerank_service] = lambda: service
    app.dependency_overrides[get_weknora_rerank_setting_service] = lambda: settings


def restore_services() -> None:
    app.dependency_overrides.pop(get_weknora_rerank_service, None)
    app.dependency_overrides.pop(get_weknora_rerank_setting_service, None)


def test_admin_api_returns_sanitized_rerank_config(admin_api_access) -> None:
    install_service(FakeService(snapshot()), FakeSettingService())
    try:
        response = TestClient(app).get(
            "/api/v1/admin/knowledge-providers/weknora/rerank-config"
        )
    finally:
        restore_services()

    assert response.status_code == 200
    assert response.json() == {
        "provider": "weknora",
        "mode": "model",
        "configured_model_id": "rerank-a",
        "effective_model_id": "rerank-a",
        "config_source": "tenant",
        "models": [
            {
                "id": "rerank-a",
                "name": "qwen3-rerank",
                "display_name": "qwen3-rerank",
                "source": "remote",
                "status": "active",
            }
        ],
    }
    serialized = response.text.lower()
    assert "api_key" not in serialized
    assert "base_url" not in serialized
    assert "credentials" not in serialized


def test_admin_api_updates_config_and_records_audit(admin_api_access) -> None:
    service = FakeService(snapshot())
    settings = FakeSettingService()
    install_service(service, settings)
    try:
        response = TestClient(app).put(
            "/api/v1/admin/knowledge-providers/weknora/rerank-config",
            json={"mode": "model", "rerank_model_id": "rerank-a"},
        )
        logs = TestClient(app).get(
            "/api/v1/admin/audit-logs",
            params={"action": "admin.weknora.rerank_config_updated"},
        )
    finally:
        restore_services()

    assert response.status_code == 200
    assert response.json()["mode"] == "model"
    assert response.json()["config_source"] == "tenant"
    assert service.updated_with == ["rerank-a"]
    assert settings.updated_modes == [("model", 1)]
    assert logs.status_code == 200
    assert len(logs.json()) == 1
    assert logs.json()[0]["detail"]["configured_model_id"] == "rerank-a"


def test_admin_api_disable_does_not_touch_weknora_model(admin_api_access) -> None:
    service = FakeService(snapshot())
    settings = FakeSettingService()
    install_service(service, settings)
    try:
        response = TestClient(app).put(
            "/api/v1/admin/knowledge-providers/weknora/rerank-config",
            json={"mode": "disabled"},
        )
    finally:
        restore_services()

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "disabled"
    assert payload["config_source"] == "disabled"
    assert payload["effective_model_id"] == ""
    assert service.updated_with == []
    assert settings.updated_modes == [("disabled", 1)]


def test_admin_api_rejects_disabled_mode_with_model(admin_api_access) -> None:
    install_service(FakeService(snapshot()), FakeSettingService())
    try:
        response = TestClient(app).put(
            "/api/v1/admin/knowledge-providers/weknora/rerank-config",
            json={"mode": "disabled", "rerank_model_id": "rerank-a"},
        )
    finally:
        restore_services()

    assert response.status_code == 422


def test_admin_api_returns_stable_error_contract(admin_api_access) -> None:
    class InvalidModelService(FakeService):
        async def update_config(self, *, rerank_model_id: str) -> WeKnoraRerankConfigSnapshot:
            raise WeKnoraRerankGovernanceError(
                code="weknora_rerank_model_invalid",
                message="请选择一个可用的 WeKnora Rerank 模型",
                status_code=409,
            )

    install_service(InvalidModelService(snapshot()), FakeSettingService())
    try:
        response = TestClient(app).put(
            "/api/v1/admin/knowledge-providers/weknora/rerank-config",
            json={"mode": "model", "rerank_model_id": "bad-model"},
        )
    finally:
        restore_services()

    assert response.status_code == 409
    payload = response.json()
    assert payload["code"] == "weknora_rerank_model_invalid"
    assert payload["error_code"] == -13006
    assert payload["message"] == "请选择一个可用的 WeKnora Rerank 模型"
    assert payload["request_id"]


def test_admin_api_maps_unavailable_error(admin_api_access) -> None:
    class UnavailableService(FakeService):
        async def get_config(self) -> WeKnoraRerankConfigSnapshot:
            raise WeKnoraRerankGovernanceError(
                code="weknora_rerank_config_unavailable",
                message="知识库底座 Rerank 配置暂不可用",
                status_code=502,
            )

    install_service(UnavailableService(snapshot()), FakeSettingService())
    try:
        response = TestClient(app).get(
            "/api/v1/admin/knowledge-providers/weknora/rerank-config"
        )
    finally:
        restore_services()

    assert response.status_code == 502
    payload = response.json()
    assert payload["code"] == "weknora_rerank_config_unavailable"
    assert payload["error_code"] == -13005
    assert payload["request_id"]
