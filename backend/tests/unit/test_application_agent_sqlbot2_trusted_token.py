from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest

from app.api.openapi.application_agents import (
    application_agent_invocation_memory_service,
)
from app.api.v1.user.application_agents import (
    application_agent_authorization_memory_service,
    application_agent_memory_service,
)
from app.config import settings
from app.main import app
from app.schemas.application_agent import (
    ApplicationAgentCreateRequest,
    ApplicationClientCreateRequest,
)
from app.services.application_agent_authorization import (
    ApplicationAgentAuthorizationError,
    SqlAlchemyApplicationAgentAuthorizationService,
)
from app.services.application_agents import _secret_digest
from app.services.runtime_workspace import Principal


def _basic(client_id: str, client_secret: str) -> str:
    token = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("ascii")
    return f"Basic {token}"


async def _create_published_agent_and_client(
    *,
    slug: str,
) -> tuple[object, object]:
    principal = Principal(org_id=1, user_id=1)
    agent = await application_agent_memory_service.create_agent(
        principal=principal,
        payload=ApplicationAgentCreateRequest(
            slug=slug,
            name=slug,
        ),
    )
    credentials = await application_agent_memory_service.create_client(
        principal=principal,
        agent_id=agent.application_agent_id,
        payload=ApplicationClientCreateRequest(
            name=f"{slug}-client",
            allow_user_delegation=True,
            allow_service_invocation=False,
        ),
    )
    await application_agent_memory_service.publish(
        principal=principal,
        agent_id=agent.application_agent_id,
    )
    return agent, credentials


def _reset_memory_services() -> None:
    application_agent_memory_service.reset()
    application_agent_invocation_memory_service.reset()
    application_agent_authorization_memory_service.reset()


@pytest.mark.asyncio
async def test_sqlbot2_trusted_user_token_is_bound_and_can_invoke() -> None:
    _reset_memory_services()
    agent, credentials = await _create_published_agent_and_client(
        slug="sqlbot2"
    )
    client_auth = _basic(
        credentials.client_id,
        str(credentials.client_secret),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as api:
            token_response = await api.post(
                "/openapi/v1/application-agents/sqlbot2/trusted-user-token",
                headers={"Authorization": client_auth},
                json={"user_code": "61715"},
            )

            assert token_response.status_code == 200
            token_body = token_response.json()
            assert token_body["token_type"] == "Agent"
            assert token_body["expires_in"] == 3600
            assert token_body["identity"] == {
                "type": "user",
                "user_code": "61715",
            }
            claims = jwt.decode(
                token_body["access_token"],
                settings.RUNTIME_TOKEN_SECRET,
                algorithms=["HS256"],
                audience="hermes-application-agent",
            )
            assert claims["sub"] == "61715"
            assert claims["org_id"] == 1
            assert claims["client_id"] == credentials.client_id
            assert claims["application_agent_id"] == (
                agent.application_agent_id
            )
            assert claims["application_agent"] == "sqlbot2"
            assert claims["scope"] == ["responses:create"]
            assert claims["exp"] - claims["iat"] == 3600

            invocation = await api.post(
                "/openapi/v1/application-agents/sqlbot2/responses",
                headers={
                    "Authorization": client_auth,
                    "X-Hermes-Agent-Token": token_body["access_token"],
                },
                json={"input": "查询销售额"},
            )
            assert invocation.status_code == 200
            assert invocation.json()["identity"] == {
                "type": "user",
                "key": "user:61715",
            }
    finally:
        _reset_memory_services()


@pytest.mark.asyncio
async def test_generic_trusted_user_token_is_bound_to_requested_agent() -> None:
    _reset_memory_services()
    agent, credentials = await _create_published_agent_and_client(
        slug="sales-agent"
    )
    client_auth = _basic(
        credentials.client_id,
        str(credentials.client_secret),
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as api:
            token_response = await api.post(
                "/openapi/v1/application-agents/sales-agent/trusted-user-token",
                headers={"Authorization": client_auth},
                json={"user_code": "61715"},
            )

            assert token_response.status_code == 200
            token_body = token_response.json()
            assert token_body["identity"] == {
                "type": "user",
                "user_code": "61715",
            }
            claims = jwt.decode(
                token_body["access_token"],
                settings.RUNTIME_TOKEN_SECRET,
                algorithms=["HS256"],
                audience="hermes-application-agent",
            )
            assert claims["application_agent"] == "sales-agent"
            assert claims["application_agent_id"] == agent.application_agent_id
            assert claims["client_id"] == credentials.client_id
            assert claims["sub"] == "61715"
    finally:
        _reset_memory_services()


@pytest.mark.asyncio
async def test_sqlbot2_trusted_token_ttl_is_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_memory_services()
    _agent, credentials = await _create_published_agent_and_client(
        slug="sqlbot2"
    )
    monkeypatch.setattr(
        settings,
        "APPLICATION_AGENT_TOKEN_TTL_SECONDS",
        600,
    )
    monkeypatch.setattr(
        settings,
        "APPLICATION_AGENT_SQLBOT2_TRUSTED_TOKEN_TTL_SECONDS",
        3600,
    )
    principal = Principal(org_id=1, user_id=1)
    try:
        code, _code_ttl = (
            await application_agent_authorization_memory_service.authorize(
                principal=principal,
                agent_slug="sqlbot2",
                client_id=credentials.client_id,
            )
        )
        generic = await application_agent_authorization_memory_service.exchange(
            code=code,
            client_id=credentials.client_id,
            client_secret=str(credentials.client_secret),
        )
        trusted = (
            await application_agent_authorization_memory_service.issue_sqlbot2_trusted_user_token(
                user_code="1",
                client_id=credentials.client_id,
                client_secret=str(credentials.client_secret),
            )
        )

        assert generic.expires_in == 600
        assert trusted.expires_in == 3600
    finally:
        _reset_memory_services()


@pytest.mark.asyncio
async def test_sqlbot2_trusted_user_token_rejects_other_clients_and_users() -> None:
    _reset_memory_services()
    _agent, sqlbot_credentials = await _create_published_agent_and_client(
        slug="sqlbot2"
    )
    _other, other_credentials = await _create_published_agent_and_client(
        slug="other-agent"
    )
    endpoint = (
        "/openapi/v1/application-agents/sqlbot2/trusted-user-token"
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as api:
            other_client = await api.post(
                endpoint,
                headers={
                    "Authorization": _basic(
                        other_credentials.client_id,
                        str(other_credentials.client_secret),
                    )
                },
                json={"user_code": "61715"},
            )
            wrong_secret = await api.post(
                endpoint,
                headers={
                    "Authorization": _basic(
                        sqlbot_credentials.client_id,
                        "wrong-secret",
                    )
                },
                json={"user_code": "61715"},
            )
            unknown_user = await api.post(
                endpoint,
                headers={
                    "Authorization": _basic(
                        sqlbot_credentials.client_id,
                        str(sqlbot_credentials.client_secret),
                    )
                },
                json={"user_code": "not-a-platform-user"},
            )
            blank_user = await api.post(
                endpoint,
                headers={
                    "Authorization": _basic(
                        sqlbot_credentials.client_id,
                        str(sqlbot_credentials.client_secret),
                    )
                },
                json={"user_code": "   "},
            )

            assert other_client.status_code == 403
            assert other_client.json() == {
                "detail": (
                    "sqlbot2_trusted_user_delegation_not_allowed"
                )
            }
            assert wrong_secret.status_code == 401
            assert wrong_secret.json() == {
                "detail": "application_client_unauthorized"
            }
            assert unknown_user.status_code == 403
            assert unknown_user.json() == {
                "detail": "delegated_user_identity_unavailable"
            }
            assert blank_user.status_code == 422
    finally:
        _reset_memory_services()


@pytest.mark.asyncio
async def test_sqlalchemy_sqlbot2_trusted_user_token_uses_active_org_user() -> None:
    agent = SimpleNamespace(id=43, org_id=1, slug="sqlbot2")
    client = SimpleNamespace(
        id=45,
        client_id="sqlbot-client",
        credential_digest_or_reference=_secret_digest("secret"),
    )
    grant = SimpleNamespace(scopes=["responses:create"])
    user = SimpleNamespace(id=7, org_id=1, user_code="61715")
    client_result = SimpleNamespace(all=lambda: [(agent, client, grant)])
    user_result = SimpleNamespace(scalar_one_or_none=lambda: user)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[client_result, user_result])
    )

    result = await SqlAlchemyApplicationAgentAuthorizationService(
        db
    ).issue_trusted_user_token(
        agent_slug="sqlbot2",
        user_code=" 61715 ",
        client_id="sqlbot-client",
        client_secret="secret",
    )

    assert result.expires_in == 3600
    assert result.user_code == "61715"
    claims = jwt.decode(
        result.access_token,
        settings.RUNTIME_TOKEN_SECRET,
        algorithms=["HS256"],
        audience="hermes-application-agent",
    )
    assert claims["sub"] == "7"
    assert claims["application_agent"] == "sqlbot2"


@pytest.mark.asyncio
async def test_sqlalchemy_sqlbot2_trusted_user_token_rejects_missing_user() -> None:
    agent = SimpleNamespace(id=43, org_id=1, slug="sqlbot2")
    client = SimpleNamespace(
        id=45,
        client_id="sqlbot-client",
        credential_digest_or_reference=_secret_digest("secret"),
    )
    grant = SimpleNamespace(scopes=["responses:create"])
    client_result = SimpleNamespace(all=lambda: [(agent, client, grant)])
    user_result = SimpleNamespace(scalar_one_or_none=lambda: None)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[client_result, user_result])
    )

    with pytest.raises(ApplicationAgentAuthorizationError) as exc:
        await SqlAlchemyApplicationAgentAuthorizationService(
            db
        ).issue_trusted_user_token(
            agent_slug="sqlbot2",
            user_code="missing",
            client_id="sqlbot-client",
            client_secret="secret",
        )

    assert exc.value.code == "delegated_user_identity_unavailable"
    assert exc.value.status_code == 403
