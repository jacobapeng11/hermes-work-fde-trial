from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_auth_login_returns_token_and_me_uses_bearer() -> None:
    original_auth = settings.HERMES_AUTH_MODE
    original_admin = settings.HERMES_ADMIN_AUTH_MODE
    original_runtime = settings.RUNTIME_CHAT_MODE
    settings.HERMES_AUTH_MODE = "dev"
    settings.HERMES_ADMIN_AUTH_MODE = "inherit"
    settings.RUNTIME_CHAT_MODE = "contract"
    try:
        client = TestClient(app)
        login = client.post("/api/v1/auth/login", json={"identifier": "dev-user", "password": "dev-user"})
        payload = login.json()
        token = payload["access_token"]
        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    finally:
        settings.HERMES_AUTH_MODE = original_auth
        settings.HERMES_ADMIN_AUTH_MODE = original_admin
        settings.RUNTIME_CHAT_MODE = original_runtime

    assert login.status_code == 200
    assert payload["token_type"] == "bearer"
    assert payload["principal"]["user_id"] == settings.DEV_PRINCIPAL_USER_ID
    assert payload["principal"]["can_manage_application_agents"] is False
    assert payload["principal"]["is_admin"] is False
    assert me.status_code == 200
    assert me.json()["user_code"] == "dev-user"
    assert me.json()["can_manage_application_agents"] is False
    assert me.json()["is_admin"] is False


def test_user_api_requires_bearer_when_auth_mode_is_jwt() -> None:
    original = settings.HERMES_AUTH_MODE
    original_admin = settings.HERMES_ADMIN_AUTH_MODE
    original_runtime = settings.RUNTIME_CHAT_MODE
    settings.HERMES_AUTH_MODE = "jwt"
    settings.HERMES_ADMIN_AUTH_MODE = "inherit"
    settings.RUNTIME_CHAT_MODE = "contract"
    try:
        client = TestClient(app)
        missing = client.get("/api/v1/user/runtime-workspace")
        login = client.post("/api/v1/auth/login", json={"identifier": "dev-user", "password": "dev-user"})
        token = login.json()["access_token"]
        authed = client.get("/api/v1/user/runtime-workspace", headers={"Authorization": f"Bearer {token}"})
    finally:
        settings.HERMES_AUTH_MODE = original
        settings.HERMES_ADMIN_AUTH_MODE = original_admin
        settings.RUNTIME_CHAT_MODE = original_runtime

    assert missing.status_code == 401
    assert missing.json() == {"detail": "missing bearer token"}
    assert authed.status_code == 200
    assert authed.json()["user_id"] == settings.DEV_PRINCIPAL_USER_ID


def test_admin_api_allows_the_explicit_dev_mode_only() -> None:
    original = settings.HERMES_AUTH_MODE
    original_admin = settings.HERMES_ADMIN_AUTH_MODE
    original_runtime = settings.RUNTIME_CHAT_MODE
    settings.HERMES_AUTH_MODE = "jwt"
    settings.HERMES_ADMIN_AUTH_MODE = "dev"
    settings.RUNTIME_CHAT_MODE = "contract"
    try:
        client = TestClient(app)
        login = client.post("/api/v1/auth/login", json={"identifier": "admin", "password": "admin"})
        token = login.json()["access_token"]
        response = client.get("/api/v1/admin/me", headers={"Authorization": f"Bearer {token}"})
    finally:
        settings.HERMES_AUTH_MODE = original
        settings.HERMES_ADMIN_AUTH_MODE = original_admin
        settings.RUNTIME_CHAT_MODE = original_runtime

    assert response.status_code == 200
    assert response.json()["roles"] == ["admin"]
