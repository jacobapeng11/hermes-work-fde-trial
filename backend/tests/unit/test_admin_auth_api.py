from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.v1.admin.deps import get_admin_authorization_service
from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.main import app
from app.services.admin_work_no import AdminWorkNoAuthorizationService, InMemoryAdminWorkNoStore
from app.services.runtime_workspace import Principal


def test_admin_me_returns_admin_principal_shape() -> None:
    original = settings.HERMES_ADMIN_AUTH_MODE
    settings.HERMES_ADMIN_AUTH_MODE = "dev"
    try:
        response = TestClient(app).get("/api/v1/admin/me")
    finally:
        settings.HERMES_ADMIN_AUTH_MODE = original

    assert response.status_code == 200
    assert response.json() == {
        "org_id": 1,
        "user_id": settings.DEV_PRINCIPAL_USER_ID,
        "roles": ["admin"],
    }


def test_admin_routes_reject_non_admin_principal() -> None:
    async def ordinary_user() -> Principal:
        return Principal(org_id=1, user_id=2)

    async def no_admin_work_number() -> AdminWorkNoAuthorizationService:
        return AdminWorkNoAuthorizationService(InMemoryAdminWorkNoStore())

    app.dependency_overrides[get_principal] = ordinary_user
    app.dependency_overrides[get_admin_authorization_service] = no_admin_work_number
    try:
        client = TestClient(app)
        me_response = client.get("/api/v1/admin/me")
        resources_response = client.get("/api/v1/admin/resources/runtime-workspaces")
        expert_response = client.get("/api/v1/admin/experts")
    finally:
        app.dependency_overrides.pop(get_principal, None)
        app.dependency_overrides.pop(get_admin_authorization_service, None)

    assert me_response.status_code == 403
    assert me_response.json() == {"detail": "admin work number permission is required"}
    assert resources_response.status_code == 403
    assert resources_response.json() == {"detail": "admin work number permission is required"}
    assert expert_response.status_code == 403
    assert expert_response.json() == {"detail": "admin work number permission is required"}
