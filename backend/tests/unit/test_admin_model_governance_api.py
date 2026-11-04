from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.v1.user.runtime_workspace import get_principal
from app.main import app
from app.services.runtime_workspace import Principal


def test_admin_model_list_excludes_provider_secret_fields(admin_api_access) -> None:
    client = TestClient(app)

    response = client.get("/api/v1/admin/models")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["model_entry_id"] == 1
    serialized = response.text.lower()
    assert "provider_key" not in serialized
    assert "api_key" not in serialized
    assert "api_endpoint" not in serialized
    assert "provider_base_url" not in serialized


def test_admin_model_status_update_and_default_update_return_upgrade_log(
    admin_api_access,
) -> None:
    client = TestClient(app)

    disable = client.put("/api/v1/admin/models/2/status", json={"status": "disabled"})
    enable = client.put("/api/v1/admin/models/2/status", json={"status": "active"})
    set_default = client.put("/api/v1/admin/models/2/default")

    assert disable.status_code == 200
    assert disable.json()["status"] == "disabled"
    assert disable.json()["upgrade_log_id"] is not None
    assert enable.status_code == 200
    assert set_default.status_code == 200
    assert set_default.json()["model_entry_id"] == 2
    assert set_default.json()["is_default"] is True


def test_admin_model_api_rejects_invalid_status(admin_api_access) -> None:
    client = TestClient(app)

    response = client.put("/api/v1/admin/models/1/status", json={"status": "deleted"})

    assert response.status_code == 400
    assert response.json() == {"detail": "model status must be active or disabled"}


def test_admin_model_api_requires_admin_role() -> None:
    async def ordinary_user() -> Principal:
        return Principal(org_id=1, user_id=2)

    app.dependency_overrides[get_principal] = ordinary_user
    try:
        client = TestClient(app)
        response = client.get("/api/v1/admin/models")
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert response.status_code == 403
    assert response.json() == {
        "detail": "admin work number permission is required"
    }
