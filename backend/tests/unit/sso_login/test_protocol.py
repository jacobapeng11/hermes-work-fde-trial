import asyncio
import base64
import json
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.config import Settings
from app.services.oauth2 import OAuth2Client, OAuth2Error, config_fingerprint
from app.services.oauth2_user_mapping import map_identity
from app.services.oauth_access_log import OAuthAccessLogFilter
from app.services.oauth_login_transactions import OAuthLoginTransactions, digest, safe_return_to


def connection(**overrides):
    values = {
        "HERMES_AUTH_MODE": "oauth2",
        "OAUTH2_PROVIDER_ID": "corp",
        "OAUTH2_CLIENT_ID": "client +:",
        "OAUTH2_CLIENT_SECRET": "secret +:",
        "OAUTH2_AUTHORIZATION_URL": "https://sso.example/authorize",
        "OAUTH2_TOKEN_URL": "https://sso.example/token",
        "OAUTH2_USERINFO_URL": "https://sso.example/me",
        "OAUTH2_REDIRECT_URI": "https://platform.example/api/v1/auth/oauth2/callback",
        "OAUTH2_USER_ID_FIELD": "data.id",
        "OAUTH2_USER_WORK_NO_FIELD": "data.employee",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    "method,path", [("client_secret_basic", "data.id"), ("client_secret_post", "id")]
)
@pytest.mark.parametrize("pkce_enabled", [True, False])
async def test_two_provider_contracts_share_client(method, path, pkce_enabled):
    config = connection(
        OAUTH2_PKCE_ENABLED=pkce_enabled,
        OAUTH2_TOKEN_AUTH_METHOD=method,
        OAUTH2_USER_ID_FIELD=path,
        OAUTH2_SCOPES="profile employee",
        OAUTH2_USERINFO_METHOD="POST",
    )
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path == "/token":
            assert request.method == "POST"
            fields = parse_qs(request.content.decode())
            assert fields["grant_type"] == ["authorization_code"]
            assert fields["redirect_uri"] == [config.OAUTH2_REDIRECT_URI]
            if pkce_enabled:
                assert fields["code_verifier"] == ["verifier"]
            else:
                assert "code_verifier" not in fields
            if method == "client_secret_basic":
                assert (
                    request.headers["authorization"]
                    == "Basic " + base64.b64encode(b"client+%2B%3A:secret+%2B%3A").decode()
                )
                assert "client_secret" not in fields
            else:
                assert "authorization" not in request.headers
                assert fields["client_secret"] == [config.OAUTH2_CLIENT_SECRET]
            return httpx.Response(
                200, json={"access_token": "upstream-token", "token_type": "Bearer"}
            )
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer upstream-token"
        return httpx.Response(
            200, json={"id": "subject", "data": {"id": "subject", "employee": "0007"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = OAuth2Client(config, client=http)
        url = parse_qs(urlsplit(client.authorization_url(state="state", verifier="verifier")).query)
        if pkce_enabled:
            assert url["code_challenge_method"] == ["S256"]
        else:
            assert "code_challenge" not in url
            assert "code_challenge_method" not in url
        assert url["scope"] == ["profile employee"]
        identity = map_identity(config, await client.user_info(code="code", verifier="verifier"))
    assert identity.work_no == "0007"
    assert len(seen) == 2


def test_pkce_defaults_to_s256_with_matching_challenge():
    config = connection()
    assert config.OAUTH2_PKCE_ENABLED is True
    url = OAuth2Client(config).authorization_url(
        state="state", verifier="dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    )
    params = parse_qs(urlsplit(url).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == ["E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"]


def test_pkce_configuration_parses_false_and_rejects_invalid_values():
    assert connection(OAUTH2_PKCE_ENABLED="false").OAUTH2_PKCE_ENABLED is False
    with pytest.raises(ValueError):
        connection(OAUTH2_PKCE_ENABLED="unsupported")
    assert config_fingerprint(connection()) != config_fingerprint(
        connection(OAUTH2_PKCE_ENABLED=False)
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://elsewhere.example"}),
        httpx.Response(200, json={"access_token": "x", "token_type": "unknown"}),
        httpx.Response(200, content=b"{", headers={"content-type": "application/json"}),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"access_token": "x\r\nsecret", "token_type": "Bearer"}),
        httpx.Response(
            200, content=b"x" * (1024 * 1024 + 1), headers={"content-type": "application/json"}
        ),
    ],
)
async def test_bad_token_responses_never_fetch_user_info(response):
    calls = []

    def handle(request):
        calls.append(request)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(OAuth2Error, match="认证服务"):
            await OAuth2Client(connection(), client=http).user_info(
                code="secret-code", verifier="v"
            )
    assert len(calls) == 1


@pytest.mark.parametrize("value", [None, "", "  ", True, 7, "x" * 33, "7\n"])
def test_work_number_is_required_and_never_replaced_by_subject(value):
    with pytest.raises(OAuth2Error) as error:
        map_identity(connection(), {"data": {"id": "subject", "employee": value}})
    assert error.value.code == "oauth2_identity_invalid"


def test_configuration_selection_rejects_legacy_enum_and_reserved_extensions():
    with pytest.raises(ValueError):
        Settings(_env_file=None, HERMES_AUTH_MODE="enterprise_idp")
    assert Settings(_env_file=None, HERMES_AUTH_MODE="enterprise_idpAES").OAUTH2_CLIENT_ID == ""
    with pytest.raises(ValueError):
        connection(OAUTH2_CLIENT_SECRET="")
    for field in (
        "state",
        "client_secret",
        "code_challenge",
        "code_challenge_method",
        "code_verifier",
        "STATE",
    ):
        with pytest.raises(ValueError):
            connection(
                OAUTH2_PKCE_ENABLED=False,
                OAUTH2_AUTHORIZATION_PARAMS=json.dumps({field: "bad"}),
            )
    with pytest.raises(ValueError):
        connection(OAUTH2_TOKEN_URL="https://name:password@sso.example/token")
    assert config_fingerprint(connection()) != config_fingerprint(
        connection(OAUTH2_CONFIG_REVISION="2")
    )
    assert config_fingerprint(connection()) == config_fingerprint(
        connection(OAUTH2_CLIENT_SECRET="rotated")
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example",
        "//evil.example",
        "/user/../admin",
        "/user/%5cevil",
        "/user/chat?token=x",
        "/user/chat?ticket=x",
        "/auth/complete",
    ],
)
def test_return_paths_refuse_redirects_and_credentials(value):
    with pytest.raises(OAuth2Error):
        safe_return_to(value)


class MemoryRedis:
    def __init__(self):
        self.records = {}
        self.lock = asyncio.Lock()

    async def set(self, key, value, ex):
        self.records[key] = value

    async def eval(self, script, count, key, binding, fingerprint):
        async with self.lock:
            raw = self.records.get(key)
            if raw is None:
                return None
            data = json.loads(raw)
            if data["binding"] != binding or data["fingerprint"] != fingerprint:
                return None
            return self.records.pop(key)


async def test_browser_binding_and_config_are_checked_before_single_consumption():
    redis = MemoryRedis()
    store = OAuthLoginTransactions(redis)
    nonce = await store.put(
        "login", {"binding": digest("browser"), "fingerprint": "revision"}, ttl=60
    )
    for binding, fingerprint in [("wrong", "revision"), ("browser", "old")]:
        with pytest.raises(OAuth2Error):
            await store.take("login", nonce, binding=binding, fingerprint=fingerprint)
    results = await asyncio.gather(
        *[store.take("login", nonce, binding="browser", fingerprint="revision") for _ in range(2)],
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, OAuth2Error) for result in results) == 1


def test_production_oauth2_does_not_require_aes_login_credentials_or_expose_secrets():
    configured = connection(
        APP_ENV="production",
        JWT_SECRET_KEY="managed-jwt-key",
        AES_ENCRYPTION_KEY="managed-platform-key",
        RUNTIME_CHAT_MODE="real",
        FILE_DOWNLOAD_SIGNING_SECRET="f" * 32,
    )
    assert configured.ENTERPRISE_IDP_AES_KEY == ""
    with pytest.raises(ValueError) as error:
        connection(
            OAUTH2_CLIENT_SECRET="never-print-this-secret", OAUTH2_TOKEN_URL="http://invalid"
        )
    assert "never-print-this-secret" not in str(error.value)


def test_access_log_removes_callback_code_state_and_desktop_flow():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("client", "GET", "/api/v1/auth/oauth2/callback?code=secret&state=private", "1.1", 303),
        None,
    )
    assert OAuthAccessLogFilter().filter(record)
    assert "secret" not in record.getMessage()
    assert "private" not in record.getMessage()
    assert "callback?[redacted]" in record.getMessage()


def test_optional_profile_mapping_is_bounded_and_never_changes_work_number():
    config = connection(
        OAUTH2_USER_NAME_FIELD="profile.name", OAUTH2_USER_EMAIL_FIELD="profile.email"
    )
    mapped = map_identity(
        config,
        {
            "data": {"id": 7, "employee": "0007"},
            "profile": {"name": "员工", "email": "user@example.invalid"},
        },
    )
    assert mapped.display_name == "员工"
    assert mapped.email == "user@example.invalid"
    assert mapped.work_no == "0007"
