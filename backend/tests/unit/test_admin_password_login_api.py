from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.v1.auth import get_admin_password_login_service
from app.config import settings
from app.main import app
from app.services.auth import AuthenticatedUser, AuthenticationError


class _SuccessfulAdminLogin:
    async def login(self, *, username: str, password: str) -> tuple[str, AuthenticatedUser]:
        assert username == "admin"
        assert password == "managed-password"
        return (
            "issued-admin-token",
            AuthenticatedUser(
                org_id=1,
                user_id=2,
                name="Administrator",
                email=None,
                user_code="admin",
            ),
        )


class _RejectedAdminLogin:
    async def login(self, *, username: str, password: str) -> tuple[str, AuthenticatedUser]:
        raise AuthenticationError("invalid credentials")


def test_admin_password_login_returns_a_no_store_platform_session() -> None:
    original = settings.HERMES_ADMIN_AUTH_MODE
    original_auth = settings.HERMES_AUTH_MODE
    original_runtime = settings.RUNTIME_CHAT_MODE
    settings.HERMES_ADMIN_AUTH_MODE = "password"
    settings.HERMES_AUTH_MODE = "dev"
    settings.RUNTIME_CHAT_MODE = "contract"
    app.dependency_overrides[get_admin_password_login_service] = lambda: _SuccessfulAdminLogin()
    try:
        response = TestClient(app).post(
            "/api/v1/auth/admin/login",
            json={"username": "admin", "password": "managed-password"},
        )
    finally:
        settings.HERMES_ADMIN_AUTH_MODE = original
        settings.HERMES_AUTH_MODE = original_auth
        settings.RUNTIME_CHAT_MODE = original_runtime
        app.dependency_overrides.pop(get_admin_password_login_service, None)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["access_token"] == "issued-admin-token"
    assert response.json()["principal"]["user_code"] == "admin"


def test_admin_password_login_uses_one_generic_unauthorized_response() -> None:
    original = settings.HERMES_ADMIN_AUTH_MODE
    original_auth = settings.HERMES_AUTH_MODE
    original_runtime = settings.RUNTIME_CHAT_MODE
    settings.HERMES_ADMIN_AUTH_MODE = "password"
    settings.HERMES_AUTH_MODE = "dev"
    settings.RUNTIME_CHAT_MODE = "contract"
    app.dependency_overrides[get_admin_password_login_service] = lambda: _RejectedAdminLogin()
    try:
        response = TestClient(app).post(
            "/api/v1/auth/admin/login",
            json={"username": "operator", "password": "wrong-password"},
        )
    finally:
        settings.HERMES_ADMIN_AUTH_MODE = original
        settings.HERMES_AUTH_MODE = original_auth
        settings.RUNTIME_CHAT_MODE = original_runtime
        app.dependency_overrides.pop(get_admin_password_login_service, None)

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "用户名或密码错误"}


def test_admin_password_login_is_not_available_outside_password_mode() -> None:
    original = settings.HERMES_ADMIN_AUTH_MODE
    original_auth = settings.HERMES_AUTH_MODE
    original_runtime = settings.RUNTIME_CHAT_MODE
    settings.HERMES_ADMIN_AUTH_MODE = "dev"
    settings.HERMES_AUTH_MODE = "dev"
    settings.RUNTIME_CHAT_MODE = "contract"
    try:
        response = TestClient(app).post(
            "/api/v1/auth/admin/login",
            json={"username": "admin", "password": "managed-password"},
        )
    finally:
        settings.HERMES_ADMIN_AUTH_MODE = original
        settings.HERMES_AUTH_MODE = original_auth
        settings.RUNTIME_CHAT_MODE = original_runtime

    assert response.status_code == 404
