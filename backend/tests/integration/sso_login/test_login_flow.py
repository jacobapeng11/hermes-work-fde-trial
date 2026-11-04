import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import auth, oauth2_login
from app.config import settings
from app.database import get_db
from app.services.auth import AuthenticatedUser, AuthTokenService
from app.services.oauth2 import OAuth2Client
from app.services.oauth_login_transactions import OAuthLoginTransactions, shared_oauth_transactions
from app.services.platform_session import InMemoryPlatformSessionStore
from app.services.sso_identity import SsoIdentity


class RedisDouble:
    def __init__(self):
        self.records = {}

    async def set(self, key, value, ex):
        self.records[key] = value

    async def eval(self, script, count, key, binding, fingerprint):
        raw = self.records.get(key)
        if raw:
            value = json.loads(raw)
            if value["binding"] == binding and value["fingerprint"] == fingerprint:
                return self.records.pop(key)


class Users:
    async def get_user(self, *, org_id, user_id):
        return AuthenticatedUser(
            org_id=org_id, user_id=user_id, user_code="0007", name="员工", email=None
        )


class Permissions:
    async def can_manage(self, principal):
        return False

    async def is_admin(self, principal):
        return False


@pytest.fixture
def environment(monkeypatch):
    values = {
        "HERMES_AUTH_MODE": "oauth2",
        "OAUTH2_PROVIDER_ID": "corp",
        "OAUTH2_CLIENT_ID": "client",
        "OAUTH2_CLIENT_SECRET": "secret",
        "OAUTH2_AUTHORIZATION_URL": "https://sso.example/authorize",
        "OAUTH2_TOKEN_URL": "https://sso.example/token",
        "OAUTH2_USERINFO_URL": "https://sso.example/me",
        "OAUTH2_REDIRECT_URI": "https://platform.example/api/v1/auth/oauth2/callback",
        "OAUTH2_USER_ID_FIELD": "sub",
        "OAUTH2_USER_WORK_NO_FIELD": "employee",
        "OAUTH2_PKCE_ENABLED": True,
        "PUBLIC_WEB_BASE": "https://platform.example",
        "APP_ENV": "production",
    }
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)
    upstream, identities, token_fields, upstream_schemes = [], [], [], []
    info = {"sub": "subject", "employee": "0007"}
    token_reply = {"access_token": "upstream-token", "token_type": "Bearer"}

    def handle(request):
        upstream.append(request.url.path)
        upstream_schemes.append(request.url.scheme)
        if request.url.path == "/token":
            token_fields.append(parse_qs(request.content.decode(), keep_blank_values=True))
        return httpx.Response(
            200,
            json=token_reply if request.url.path == "/token" else info,
        )

    class Identity:
        def __init__(self, repository):
            pass

        async def resolve_or_create(self, *, org_id, work_no, **profile):
            identities.append((org_id, work_no))
            return SsoIdentity(org_id=org_id, user_id=7, user_code=work_no, name="员工", email=None)

    monkeypatch.setattr(oauth2_login, "SsoIdentityService", Identity)
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(oauth2_login.router, prefix="/api/v1")
    redis = RedisDouble()
    sessions = InMemoryPlatformSessionStore(idle_ttl_seconds=180)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))

    async def database():
        yield object()

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[shared_oauth_transactions] = lambda: OAuthLoginTransactions(redis)
    app.dependency_overrides[oauth2_login.get_oauth_client] = lambda: OAuth2Client(
        settings, client=http
    )
    app.dependency_overrides[auth.get_platform_session_store] = lambda: sessions
    app.dependency_overrides[auth.get_auth_token_service] = lambda: AuthTokenService(Users())
    app.dependency_overrides[auth.get_application_agent_authorization_service] = lambda: (
        Permissions()
    )
    app.dependency_overrides[auth.get_admin_work_no_authorization_service] = lambda: Permissions()
    with TestClient(app, base_url="https://platform.example", follow_redirects=False) as client:
        yield SimpleNamespace(
            client=client,
            upstream=upstream,
            identities=identities,
            info=info,
            redis=redis,
            token_fields=token_fields,
            upstream_schemes=upstream_schemes,
            token_reply=token_reply,
        )


def begin(env, **kwargs):
    response = env.client.get(
        "/api/v1/auth/oauth2/login", params={"return_to": "/admin/models", **kwargs}
    )
    assert response.status_code == 302
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    params = parse_qs(urlsplit(response.headers["location"]).query)
    if settings.OAUTH2_PKCE_ENABLED:
        assert params["code_challenge_method"] == ["S256"]
        assert params["code_challenge"]
    else:
        assert "code_challenge" not in params
        assert "code_challenge_method" not in params
    return params["state"][0]


@pytest.mark.parametrize("pkce_enabled", [True, False])
def test_browser_login_cookie_completion_refresh_and_replay(environment, monkeypatch, pkce_enabled):
    env = environment
    monkeypatch.setattr(settings, "OAUTH2_PKCE_ENABLED", pkce_enabled)
    assert env.client.get("/api/v1/auth/config").json() == {"login_method": "oauth2"}
    state = begin(env)
    record = json.loads(next(iter(env.redis.records.values())))
    assert bool(record["verifier"]) is pkce_enabled
    callback = f"/api/v1/auth/oauth2/callback?state={state}&code=valid-code"
    response = env.client.get(callback)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/complete"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "valid-code" not in response.text
    assert env.identities == [(settings.DEFAULT_ORG_ID, "0007")]
    assert env.upstream == ["/token", "/me"]
    if pkce_enabled:
        assert env.token_fields[0]["code_verifier"] == [record["verifier"]]
    else:
        assert "code_verifier" not in env.token_fields[0]
    assert "oauth2_transaction_invalid" in env.client.get(callback).headers["location"]
    headers = {"Origin": "https://platform.example"}
    finished = env.client.post("/api/v1/auth/oauth2/complete", headers=headers)
    assert finished.json() == {"return_to": "/admin/models"}
    assert env.client.post("/api/v1/auth/oauth2/complete", headers=headers).status_code == 400
    refreshed = env.client.post("/api/v1/auth/refresh", headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.json()["principal"]["user_id"] == 7
    assert refreshed.json()["principal"]["is_admin"] is False
    assert refreshed.json()["access_token"] != "upstream-token"


@pytest.mark.parametrize("pkce_enabled", [True, False])
def test_missing_browser_binding_never_exchanges_or_creates_account(
    environment, monkeypatch, pkce_enabled
):
    env = environment
    monkeypatch.setattr(settings, "OAUTH2_PKCE_ENABLED", pkce_enabled)
    state = begin(env)
    env.client.cookies.clear()
    response = env.client.get(f"/api/v1/auth/oauth2/callback?state={state}&code=x")
    assert "oauth2_transaction_invalid" in response.headers["location"]
    assert env.upstream == env.identities == []


@pytest.mark.parametrize("pkce_enabled", [True, False])
def test_pkce_change_invalidates_pending_login_before_token_exchange(
    environment, monkeypatch, pkce_enabled
):
    env = environment
    monkeypatch.setattr(settings, "OAUTH2_PKCE_ENABLED", pkce_enabled)
    state = begin(env)
    monkeypatch.setattr(settings, "OAUTH2_PKCE_ENABLED", not pkce_enabled)
    response = env.client.get(f"/api/v1/auth/oauth2/callback?state={state}&code=x")
    assert "oauth2_transaction_invalid" in response.headers["location"]
    assert env.upstream == env.identities == []


def test_dev_http_backchannel_keeps_browser_authorization_and_completion_https(environment, monkeypatch):
    from app.services.oauth2 import validate_oauth2_settings

    env = environment
    for key, value in {
        "APP_ENV": "dev", "OAUTH2_ALLOW_INSECURE_HTTP": True, "OAUTH2_PKCE_ENABLED": False,
        "OAUTH2_TOKEN_URL": "http://sso.example:444/token",
        "OAUTH2_USERINFO_URL": "http://sso.example:444/me",
    }.items():
        monkeypatch.setattr(settings, key, value)
    validate_oauth2_settings(settings)
    response = env.client.get("/api/v1/auth/oauth2/login", params={"return_to": "/user/chat"})
    assert response.status_code == 302
    authorization = urlsplit(response.headers["location"])
    assert authorization.scheme == "https"
    params = parse_qs(authorization.query)
    assert params["redirect_uri"] == ["https://platform.example/api/v1/auth/oauth2/callback"]
    assert "code_challenge" not in params
    response = env.client.get("/api/v1/auth/oauth2/callback", params={"state": params["state"][0], "code": "test-code"})
    assert response.status_code == 303 and response.headers["location"] == "/auth/complete"
    assert env.upstream_schemes == ["http", "http"]
    assert env.identities == [(settings.DEFAULT_ORG_ID, "0007")]
    completed = env.client.post("/api/v1/auth/oauth2/complete", headers={"Origin": "https://platform.example"})
    assert completed.status_code == 200 and completed.json() == {"return_to": "/user/chat"}


def test_upstream_failure_log_matches_error_page_request_id_without_credentials(environment, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    env = environment
    private = "PRIVATE-CODE-OR-PROVIDER-MESSAGE"
    env.token_reply.clear()
    env.token_reply.update(error="invalid_client", error_description=private)
    state = begin(env)
    response = env.client.get("/api/v1/auth/oauth2/callback", params={"state": state, "code": private})
    assert response.status_code == 303
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["reason"] == ["oauth2_upstream_failed"]
    request_id = params["request_id"][0]
    assert env.upstream == ["/token"] and env.identities == []
    messages = [r.getMessage() for r in caplog.records if r.name == "app.services.oauth2"]
    assert len(messages) == 1 and f"request_id={request_id}" in messages[0]
    assert "stage=token reason=invalid_token_response" in messages[0]
    assert '"oauth_error": "invalid_client"' in messages[0]
    assert private not in caplog.text


def test_missing_work_number_does_not_create_platform_session(environment):
    env = environment
    env.info.pop("employee")
    state = begin(env)
    response = env.client.get(f"/api/v1/auth/oauth2/callback?state={state}&code=x")
    assert "oauth2_identity_invalid" in response.headers["location"]
    assert env.identities == []
    assert env.client.cookies.get("hermes_platform_session") is None


def test_wrong_protocol_and_stale_configuration_never_call_upstream(environment, monkeypatch):
    env = environment
    state = begin(env)
    monkeypatch.setattr(settings, "HERMES_AUTH_MODE", "enterprise_idpAES")
    assert env.client.get(f"/api/v1/auth/oauth2/callback?state={state}&code=x").status_code == 404
    assert env.client.get("/api/v1/auth/oauth2/login").status_code == 404
    monkeypatch.setattr(settings, "HERMES_AUTH_MODE", "oauth2")
    monkeypatch.setattr(settings, "OAUTH2_CONFIG_REVISION", "999")
    assert (
        "oauth2_transaction_invalid"
        in env.client.get(f"/api/v1/auth/oauth2/callback?state={state}&code=x").headers["location"]
    )
    assert env.upstream == []
    assert env.client.post("/api/v1/auth/enterprise_idp/exchange", json={"token": "x"}).status_code == 404


@pytest.mark.parametrize("pkce_enabled", [True, False])
def test_desktop_completion_requires_current_flow_and_new_session(
    environment, monkeypatch, pkce_enabled
):
    env = environment
    monkeypatch.setattr(settings, "OAUTH2_PKCE_ENABLED", pkce_enabled)
    flow = "a" * 43
    state = begin(env, desktop_flow=flow)
    response = env.client.get(f"/api/v1/auth/oauth2/callback?state={state}&code=x")
    assert response.headers["location"] == "/auth/desktop-complete"
    completed = env.client.post(
        "/api/v1/auth/oauth2/complete",
        headers={"Origin": "https://platform.example", "X-Hermes-Desktop-Flow": flow},
    )
    assert completed.status_code == 200


def test_desktop_wrong_flow_cannot_complete_even_with_an_existing_session(environment):
    env = environment
    state = begin(env, desktop_flow="a" * 43)
    env.client.get(f"/api/v1/auth/oauth2/callback?state={state}&code=x")
    completed = env.client.post(
        "/api/v1/auth/oauth2/complete",
        headers={"Origin": "https://platform.example", "X-Hermes-Desktop-Flow": "b" * 43},
    )
    assert completed.status_code == 400


def test_login_rejects_client_endpoints_and_caps_pending_cookie_transactions(environment):
    env = environment
    assert (
        env.client.get("/api/v1/auth/oauth2/login?endpoint=https://evil.example").status_code == 400
    )
    for _ in range(8):
        begin(env)
    assert env.client.get("/api/v1/auth/oauth2/login").status_code == 400
    assert env.upstream == []


@pytest.mark.parametrize(
    "query", ["code=a&code=b", "code=a&error=denied", "", "error=denied&state=x&state=y"]
)
def test_malformed_callbacks_do_not_exchange(environment, query):
    env = environment
    env.client.get(f"/api/v1/auth/oauth2/callback?{query}")
    assert env.upstream == env.identities == []
