from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.v1.user.runtime_workspace import get_principal
from app.main import app
from app.services.runtime_workspace import Principal


def test_admin_model_write_operation_is_audited(admin_api_access) -> None:
    client = TestClient(app)

    update = client.put("/api/v1/admin/models/1/status", json={"status": "disabled"})
    logs = client.get("/api/v1/admin/audit-logs?action=admin.model.status_updated")

    assert update.status_code == 200
    assert logs.status_code == 200
    payload = logs.json()
    assert any(item["target_type"] == "model_entry" and item["target_id"] == "1" for item in payload)
    serialized = logs.text.lower()
    assert "api_key" not in serialized
    assert "provider_key" not in serialized
    assert "runtime_token" not in serialized


def test_admin_audit_logs_require_admin_role() -> None:
    async def ordinary_user() -> Principal:
        return Principal(org_id=1, user_id=2)

    app.dependency_overrides[get_principal] = ordinary_user
    try:
        client = TestClient(app)
        response = client.get("/api/v1/admin/audit-logs")
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert response.status_code == 403
    assert response.json() == {
        "detail": "admin work number permission is required"
    }
