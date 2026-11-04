"""OAuth2 failures expose bounded diagnostics, never authentication payloads."""
import logging
from types import SimpleNamespace

import httpx
import pytest

from app.services.oauth2 import OAuth2Client, OAuth2Error

REQUEST_ID = "6b2d83c3995f83a86792ca7f"
PRIVATE = "PRIVATE-PAYLOAD-MUST-NOT-APPEAR"


@pytest.fixture
def config():
    return SimpleNamespace(
        OAUTH2_PKCE_ENABLED=False, OAUTH2_TOKEN_AUTH_METHOD="client_secret_basic",
        OAUTH2_CLIENT_ID="client", OAUTH2_CLIENT_SECRET=PRIVATE,
        OAUTH2_TOKEN_URL="https://sso.example/token?diagnostic=" + PRIVATE,
        OAUTH2_USERINFO_URL="https://sso.example/me?diagnostic=" + PRIVATE,
        OAUTH2_USERINFO_METHOD="GET", OAUTH2_HTTP_TIMEOUT_SECONDS=10,
        OAUTH2_REDIRECT_URI="https://platform.example/api/v1/auth/oauth2/callback",
    )


@pytest.mark.parametrize("stage,response,reason", [
    ("token", httpx.Response(401, json={"error": "invalid_client", "error_description": PRIVATE}), "http_status"),
    ("token", httpx.Response(200, text=PRIVATE), "content_type"),
    ("token", httpx.Response(200, json={"access_token": PRIVATE}), "invalid_token_response"),
    ("token", httpx.Response(200, json={"data": {"access_token": PRIVATE}}), "invalid_token_response"),
    ("token", httpx.Response(200, json=[PRIVATE]), "response_not_object"),
    ("token", httpx.Response(200, content=PRIVATE, headers={"content-type": "application/json"}), "invalid_json"),
    ("token", httpx.Response(200, content=PRIVATE, headers={"content-type": PRIVATE}), "content_type"),
    ("userinfo", httpx.Response(503, json={"message": PRIVATE}), "http_status"),
    ("userinfo", httpx.Response(200, json=[PRIVATE]), "response_not_object"),
])
async def test_failure_logs_stage_and_request_without_payload(config, caplog, stage, response, reason):
    caplog.set_level(logging.WARNING)

    def handle(request):
        if stage == "userinfo" and request.url.path == "/token":
            return httpx.Response(200, json={"access_token": PRIVATE, "token_type": "Bearer"})
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(OAuth2Error) as error:
            await OAuth2Client(config, client=http).user_info(code=PRIVATE, verifier=PRIVATE, request_id=REQUEST_ID)
    assert error.value.code == "oauth2_upstream_failed"
    messages = [r.getMessage() for r in caplog.records if r.name == "app.services.oauth2"]
    assert len(messages) == 1
    assert f"request_id={REQUEST_ID}" in messages[0]
    assert f"stage={stage}" in messages[0] and f"reason={reason}" in messages[0]
    assert PRIVATE not in messages[0]
    assert "Authorization" not in messages[0] and "Basic " not in messages[0]
    assert f'"http_status": {response.status_code}' in messages[0]


@pytest.mark.parametrize("error,reason", [
    (httpx.ConnectError(PRIVATE), "connect_error"),
    (httpx.ConnectError("CERTIFICATE_VERIFY_FAILED " + PRIVATE), "tls_verification"),
    (httpx.ReadTimeout(PRIVATE), "timeout"),
])
async def test_transport_details_and_untrusted_request_id_are_never_logged(config, caplog, error, reason):
    caplog.set_level(logging.WARNING)

    def fail(request):
        raise error

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        with pytest.raises(OAuth2Error):
            await OAuth2Client(config, client=http).user_info(code=PRIVATE, verifier=PRIVATE, request_id=PRIVATE)
    messages = [r.getMessage() for r in caplog.records if r.name == "app.services.oauth2"]
    assert len(messages) == 1 and "request_id=-" in messages[0]
    assert f"reason={reason}" in messages[0] and PRIVATE not in messages[0]
