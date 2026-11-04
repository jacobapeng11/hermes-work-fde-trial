from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.v1.admin.deps import get_admin_principal
from app.main import app
from app.services.admin_auth import AdminPrincipal


def test_org_admin_cannot_update_other_org_model_contract_resource() -> None:
    async def org_two_admin() -> AdminPrincipal:
        return AdminPrincipal(org_id=2, user_id=20, roles=("admin",))

    app.dependency_overrides[get_admin_principal] = org_two_admin
    try:
        client = TestClient(app)
        list_response = client.get("/api/v1/admin/models")
        update_response = client.put("/api/v1/admin/models/1/status", json={"status": "disabled"})
    finally:
        app.dependency_overrides.pop(get_admin_principal, None)

    assert list_response.status_code == 200
    assert list_response.json() == []
    assert update_response.status_code == 404
    assert update_response.json() == {"detail": "model entry not found"}


def test_org_admin_audit_query_is_scoped_to_own_org(admin_api_access) -> None:
    client = TestClient(app)
    create_log = client.put("/api/v1/admin/models/1/status", json={"status": "disabled"})
    assert create_log.status_code == 200

    async def org_two_admin() -> AdminPrincipal:
        return AdminPrincipal(org_id=2, user_id=20, roles=("admin",))

    app.dependency_overrides[get_admin_principal] = org_two_admin
    try:
        org_two_logs = TestClient(app).get("/api/v1/admin/audit-logs")
    finally:
        app.dependency_overrides.pop(get_admin_principal, None)

    assert org_two_logs.status_code == 200
    assert org_two_logs.json() == []
