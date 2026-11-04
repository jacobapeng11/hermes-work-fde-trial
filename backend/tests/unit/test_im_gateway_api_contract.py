from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.v1.user.application_agent_im_channels import (
    _http_error as application_agent_http_error,
)
from app.im_gateway.channel_service import ImGatewayServiceError
from app.main import app
from app.schemas.im_gateway import (
    ApplicationWeComChannelResponse,
    RegisterWeComChannelRequest,
    WeComChannelResponse,
)


def test_im_gateway_routes_and_secret_redaction_are_in_openapi() -> None:
    schema = app.openapi()
    paths = schema["paths"]

    assert {
        "/api/v1/user/im-channels",
        "/api/v1/user/im-channels/wecom/onboarding/qrcode",
        "/api/v1/user/im-channels/wecom/onboarding/{onboarding_id}",
        "/api/v1/user/im-channels/wecom",
        "/api/v1/user/im-channels/wecom/enabled",
        "/api/v1/health/im-gateway",
    }.issubset(paths)
    properties = schema["components"]["schemas"][
        "WeComChannelResponse"
    ]["properties"]
    assert "secret" not in properties
    assert "secret_encrypted" not in properties


def test_wecom_registration_accepts_exactly_one_credential_source() -> None:
    manual = RegisterWeComChannelRequest(
        connection_mode="websocket",
        bot_id="bot-id",
        secret="secret",
    )
    quick = RegisterWeComChannelRequest(
        connection_mode="websocket",
        onboarding_id="imo-one",
    )

    assert manual.secret == "secret"
    assert quick.onboarding_id == "imo-one"
    with pytest.raises(ValidationError):
        RegisterWeComChannelRequest(
            connection_mode="websocket",
            bot_id="bot-id",
        )
    with pytest.raises(ValidationError):
        RegisterWeComChannelRequest(
            connection_mode="websocket",
            bot_id="bot-id",
            secret="secret",
            onboarding_id="imo-one",
        )


def test_wecom_response_never_has_a_secret_field() -> None:
    response = WeComChannelResponse(
        available=True,
        enabled=True,
        status="connected",
        bot_id="bot-id",
        secret_configured=True,
    )

    assert response.model_dump() == {
        "provider": "wecom",
        "display_name": "企微",
        "available": True,
        "enabled": True,
        "status": "connected",
        "connection_mode": None,
        "bot_id": "bot-id",
        "secret_configured": True,
        "runtime_session_id": None,
        "config_generation": None,
        "last_connected_at": None,
        "last_message_at": None,
        "last_error_code": None,
        "last_error_message": None,
    }


def test_application_agent_upstream_failure_maps_to_bad_gateway() -> None:
    error = application_agent_http_error(
        ImGatewayServiceError("wecom_qr_upstream_unavailable")
    )

    assert error.status_code == 502
    assert error.detail == {
        "code": "im_gateway_error",
        "message": "wecom_qr_upstream_unavailable",
    }


def test_application_wecom_routes_and_responses_never_expose_secrets() -> None:
    schema = app.openapi()
    paths = schema["paths"]
    base = "/api/v1/user/application-agents/{agent_id}/im-channels/wecom"

    assert {
        base,
        f"{base}/onboarding/qrcode",
        f"{base}/onboarding/{{onboarding_id}}",
        f"{base}/settings",
        f"{base}/enable",
        f"{base}/disable",
        f"{base}/reconnect",
        f"{base}/rotate",
        f"{base}/users",
        f"{base}/messages",
        "/api/v1/user/wecom-identities/{identity_id}/revoke",
    }.issubset(paths)
    assert f"{base}/invitation" not in paths
    response = ApplicationWeComChannelResponse(
        channel_id=7,
        application_agent_id=9,
        status="connected",
        bot_id="bot-id",
        secret_configured=True,
    ).model_dump()
    assert "secret" not in response
    assert "secret_encrypted" not in response
    assert "member_access_url" not in response
    assert response["target_type"] == "application_agent"
