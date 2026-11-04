"""Explicit dev-only HTTP backchannel; browser URLs remain HTTPS."""
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.config import Settings
from app.services.oauth2 import OAuth2Client, config_fingerprint


def connection(**overrides):
    values = {
        "APP_ENV": "dev", "HERMES_AUTH_MODE": "oauth2",
        "OAUTH2_PROVIDER_ID": "corp", "OAUTH2_CLIENT_ID": "client",
        "OAUTH2_CLIENT_SECRET": "test-secret",
        "OAUTH2_AUTHORIZATION_URL": "https://sso.example:444/authorize",
        "OAUTH2_TOKEN_URL": "https://sso.example:444/token",
        "OAUTH2_USERINFO_URL": "https://sso.example:444/me",
        "OAUTH2_REDIRECT_URI": "https://platform.example/api/v1/auth/oauth2/callback",
        "OAUTH2_USER_ID_FIELD": "sub", "OAUTH2_USER_WORK_NO_FIELD": "employee",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("key", ["OAUTH2_TOKEN_URL", "OAUTH2_USERINFO_URL"])
def test_http_is_rejected_by_default_even_in_dev(key):
    assert connection().OAUTH2_ALLOW_INSECURE_HTTP is False
    with pytest.raises(ValueError):
        connection(**{key: "http://sso.example:444/endpoint"})


def test_dev_http_opt_in_changes_transaction_fingerprint():
    config = connection(
        OAUTH2_ALLOW_INSECURE_HTTP="true",
        OAUTH2_TOKEN_URL="http://sso.example:444/token",
        OAUTH2_USERINFO_URL="http://sso.example:444/me",
    )
    assert config.OAUTH2_ALLOW_INSECURE_HTTP is True
    assert config_fingerprint(connection()) != config_fingerprint(
        connection(OAUTH2_ALLOW_INSECURE_HTTP=True)
    )
    with pytest.raises(ValueError):
        connection(OAUTH2_ALLOW_INSECURE_HTTP="unsupported")


@pytest.mark.parametrize("environment", ["production", "prod", "test", "uat", "development"])
def test_http_opt_in_is_rejected_outside_explicit_dev(environment):
    with pytest.raises(ValueError, match="only allowed.*dev"):
        connection(APP_ENV=environment, OAUTH2_ALLOW_INSECURE_HTTP=True)


@pytest.mark.parametrize("key", ["OAUTH2_AUTHORIZATION_URL", "OAUTH2_REDIRECT_URI"])
def test_browser_urls_always_require_https(key):
    with pytest.raises(ValueError):
        connection(OAUTH2_ALLOW_INSECURE_HTTP=True, **{key: "http://platform.example/api/v1/auth/oauth2/callback"})


@pytest.mark.parametrize("url", [
    "http://name:password@sso.example/token", "http:///token",
    "http://sso.example/token#fragment", "http://sso.example/token?client_secret=secret",
    "ftp://sso.example/token", "http://sso.example:invalid/token",
])
def test_http_opt_in_preserves_url_validation(url):
    with pytest.raises(ValueError):
        connection(OAUTH2_ALLOW_INSECURE_HTTP=True, OAUTH2_TOKEN_URL=url)


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
async def test_only_backchannel_uses_http_and_keeps_post_bearer_contract(method):
    config = connection(
        OAUTH2_ALLOW_INSECURE_HTTP=True, OAUTH2_PKCE_ENABLED=False,
        OAUTH2_TOKEN_URL="http://sso.example:444/token",
        OAUTH2_USERINFO_URL="http://sso.example:444/me",
        OAUTH2_TOKEN_AUTH_METHOD=method,
    )
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.scheme == "http" and request.url.port == 444
        if request.url.path == "/token":
            assert request.method == "POST"
            form = parse_qs(request.content.decode())
            assert form["redirect_uri"] == [config.OAUTH2_REDIRECT_URI]
            assert "code_verifier" not in form
            if method == "client_secret_basic":
                assert request.headers["Authorization"].startswith("Basic ")
                assert "client_secret" not in form
            else:
                assert "Authorization" not in request.headers
                assert form["client_secret"] == [config.OAUTH2_CLIENT_SECRET]
            return httpx.Response(200, json={"access_token": "test-token", "token_type": "Bearer"})
        assert request.method == "GET" and request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"sub": "subject", "employee": "0007"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = OAuth2Client(config, client=http)
        authorization = urlsplit(client.authorization_url(state="state", verifier=""))
        assert authorization.scheme == "https"
        assert parse_qs(authorization.query)["redirect_uri"] == [config.OAUTH2_REDIRECT_URI]
        assert (await client.user_info(code="test-code", verifier=""))["employee"] == "0007"
    assert len(requests) == 2
