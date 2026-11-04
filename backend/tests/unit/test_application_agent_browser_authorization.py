from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from pydantic import ValidationError

from app.api.v1.user.application_agents import (
    application_agent_authorization_memory_service,
    application_agent_memory_service,
)
from app.api.v1.user.runtime_workspace import get_principal
from app.main import app
from app.schemas.application_agent import (
    ApplicationAgentConfig,
    ApplicationAgentCreateRequest,
    ApplicationClientCreateRequest,
    ApplicationClientUpdateRequest,
)
from app.services.application_agent_authorization import (
    ApplicationAgentAuthorizationError,
    InMemoryApplicationAgentAuthorizationService,
    SqlAlchemyApplicationAgentAuthorizationService,
)
from app.services.application_agents import (
    ApplicationAgentError,
    InMemoryApplicationAgentService,
    _secret_digest,
)
from app.services.runtime_workspace import Principal


async def _principal() -> Principal:
    return Principal(org_id=1, user_id=7)


def test_application_client_redirect_uris_require_safe_exact_urls() -> None:
    payload = ApplicationClientCreateRequest(
        name="SQLBot",
        allowed_redirect_uris=[
            "https://sqlbot.example.com/login/callback",
            "https://sqlbot.example.com/login/callback",
            "http://127.0.0.1:8080/callback",
        ],
    )

    assert payload.allowed_redirect_uris == [
        "https://sqlbot.example.com/login/callback",
        "http://127.0.0.1:8080/callback",
    ]
    for invalid in (
        "https://*.example.com/callback",
        "https://sqlbot.example.com/callback?state=fixed",
        "https://sqlbot.example.com/callback#fragment",
    ):
        with pytest.raises(ValidationError):
            ApplicationClientCreateRequest(
                name="SQLBot",
                allowed_redirect_uris=[invalid],
            )


@pytest.mark.asyncio
async def test_only_configured_non_loopback_http_redirect_can_be_registered() -> None:
    callback = (
        "http://192.0.2.80:8000/api/v1/hermes/browser-callback"
    )
    management = InMemoryApplicationAgentService(
        insecure_redirect_uri_allowlist=[callback]
    )
    authorization = InMemoryApplicationAgentAuthorizationService(management)
    principal = Principal(org_id=1, user_id=7)
    agent = await management.create_agent(
        principal=principal,
        payload=ApplicationAgentCreateRequest(
            slug="sqlbot2",
            name="SQLBot2",
            config=ApplicationAgentConfig(),
        ),
    )

    client = await management.create_client(
        principal=principal,
        agent_id=agent.application_agent_id,
        payload=ApplicationClientCreateRequest(
            name="SQLBot",
            allowed_redirect_uris=[callback],
        ),
    )
    await management.publish(
        principal=principal,
        agent_id=agent.application_agent_id,
    )
    code, _expires_in = await authorization.authorize(
        principal=principal,
        agent_slug="sqlbot2",
        client_id=client.client_id,
        redirect_uri=callback,
    )
    assert code.startswith("hca_code_")

    for rejected in (
        "http://192.0.2.80:8001/api/v1/hermes/browser-callback",
        "http://192.0.2.80:8000/api/v1/hermes/other-callback",
        "http://192.0.2.81:8000/api/v1/hermes/browser-callback",
    ):
        with pytest.raises(ApplicationAgentError) as exc:
            await management.update_client(
                principal=principal,
                agent_id=agent.application_agent_id,
                client_pk=client.application_client_id,
                payload=ApplicationClientUpdateRequest(
                    name="SQLBot",
                    allow_user_delegation=True,
                    allow_service_invocation=False,
                    allowed_redirect_uris=[rejected],
                ),
            )
        assert exc.value.code == "application_redirect_uri_insecure"
        assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_token_exchange_requires_bound_user_code_before_consuming_code() -> None:
    management = InMemoryApplicationAgentService()
    authorization = InMemoryApplicationAgentAuthorizationService(
        management,
        user_codes={(1, 7): None},
    )
    principal = Principal(org_id=1, user_id=7)
    agent = await management.create_agent(
        principal=principal,
        payload=ApplicationAgentCreateRequest(
            slug="sqlbot2",
            name="SQLBot2",
            config=ApplicationAgentConfig(),
        ),
    )
    client = await management.create_client(
        principal=principal,
        agent_id=agent.application_agent_id,
        payload=ApplicationClientCreateRequest(
            name="SQLBot",
            allow_user_delegation=True,
            allow_service_invocation=False,
        ),
    )
    await management.publish(
        principal=principal,
        agent_id=agent.application_agent_id,
    )
    code, _expires_in = await authorization.authorize(
        principal=principal,
        agent_slug="sqlbot2",
        client_id=client.client_id,
    )

    for _attempt in range(2):
        with pytest.raises(ApplicationAgentAuthorizationError) as exc:
            await authorization.exchange(
                code=code,
                client_id=client.client_id,
                client_secret=str(client.client_secret),
            )
        assert exc.value.code == "delegated_user_identity_unavailable"
        assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "user_code"),
    [("disabled", "61715"), ("active", None), ("active", "   ")],
)
async def test_sql_token_exchange_rejects_unusable_bound_user_identity(
    status: str,
    user_code: str | None,
) -> None:
    authorization = SimpleNamespace(
        code_digest="digest",
        org_id=1,
        user_id=7,
        expires_at=None,
        consumed_at=None,
    )
    result = SimpleNamespace(
        first=lambda: (
            authorization,
            SimpleNamespace(id=43, slug="sqlbot2"),
            SimpleNamespace(
                id=45,
                client_id="client-id",
                credential_digest_or_reference=_secret_digest("secret"),
            ),
            SimpleNamespace(scopes=["responses:create"]),
            SimpleNamespace(status=status, user_code=user_code),
        )
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(),
    )

    with pytest.raises(ApplicationAgentAuthorizationError) as exc:
        await SqlAlchemyApplicationAgentAuthorizationService(db).exchange(
            code="hca_code_example",
            client_id="client-id",
            client_secret="secret",
        )

    assert exc.value.code == "delegated_user_identity_unavailable"
    assert exc.value.status_code == 403
    assert authorization.consumed_at is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_authorization_requires_registered_redirect_and_updates_client() -> None:
    application_agent_memory_service.reset()
    application_agent_authorization_memory_service.reset()
    principal = Principal(org_id=1, user_id=7)
    agent = await application_agent_memory_service.create_agent(
        principal=principal,
        payload=ApplicationAgentCreateRequest(
            slug="sqlbot2",
            name="SQLBot2",
            config=ApplicationAgentConfig(),
        ),
    )
    client = await application_agent_memory_service.create_client(
        principal=principal,
        agent_id=agent.application_agent_id,
        payload=ApplicationClientCreateRequest(
                name="SQLBot",
                allow_user_delegation=True,
                allow_service_invocation=False,
            allowed_redirect_uris=[
                "https://sqlbot.example.com/login/callback"
            ],
        ),
    )
    await application_agent_memory_service.publish(
        principal=principal,
        agent_id=agent.application_agent_id,
    )

    code, expires_in = await application_agent_authorization_memory_service.authorize(
        principal=principal,
        agent_slug="sqlbot2",
        client_id=client.client_id,
        redirect_uri="https://sqlbot.example.com/login/callback",
    )

    assert code.startswith("hca_code_")
    assert expires_in > 0
    with pytest.raises(ApplicationAgentAuthorizationError) as exc:
        await application_agent_authorization_memory_service.authorize(
            principal=principal,
            agent_slug="sqlbot2",
            client_id=client.client_id,
            redirect_uri="https://evil.example.com/callback",
        )
    assert exc.value.code == "application_redirect_uri_not_allowed"
    assert exc.value.redirect_uri is None

    updated = await application_agent_memory_service.update_client(
        principal=principal,
        agent_id=agent.application_agent_id,
        client_pk=client.application_client_id,
        payload=ApplicationClientUpdateRequest(
            name="SQLBot",
            allow_user_delegation=True,
            allow_service_invocation=False,
            allowed_redirect_uris=[
                "https://sqlbot.example.com/auth/hermes/callback"
            ],
        ),
    )

    assert updated.allow_service_invocation is False
    assert updated.allowed_redirect_uris == [
        "https://sqlbot.example.com/auth/hermes/callback"
    ]


@pytest.mark.asyncio
async def test_browser_authorization_api_echoes_only_registered_redirect() -> None:
    application_agent_memory_service.reset()
    application_agent_authorization_memory_service.reset()
    principal = await _principal()
    agent = await application_agent_memory_service.create_agent(
        principal=principal,
        payload=ApplicationAgentCreateRequest(
            slug="sqlbot2",
            name="SQLBot2",
            config=ApplicationAgentConfig(),
        ),
    )
    client = await application_agent_memory_service.create_client(
        principal=principal,
        agent_id=agent.application_agent_id,
        payload=ApplicationClientCreateRequest(
            name="SQLBot",
            allowed_redirect_uris=[
                "https://sqlbot.example.com/login/callback"
            ],
        ),
    )
    await application_agent_memory_service.publish(
        principal=principal,
        agent_id=agent.application_agent_id,
    )
    app.dependency_overrides[get_principal] = _principal
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as api:
            accepted = await api.post(
                "/api/v1/user/application-agents/authorizations",
                json={
                    "application_agent": "sqlbot2",
                    "client_id": client.client_id,
                    "redirect_uri": (
                        "https://sqlbot.example.com/login/callback"
                    ),
                },
            )
            rejected = await api.post(
                "/api/v1/user/application-agents/authorizations",
                json={
                    "application_agent": "sqlbot2",
                    "client_id": client.client_id,
                    "redirect_uri": "https://evil.example.com/callback",
                },
            )
            safe_failure = await api.post(
                "/api/v1/user/application-agents/authorizations",
                json={
                    "application_agent": "missing-agent",
                    "client_id": client.client_id,
                    "redirect_uri": (
                        "https://sqlbot.example.com/login/callback"
                    ),
                },
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)
        application_agent_memory_service.reset()
        application_agent_authorization_memory_service.reset()

    assert accepted.status_code == 200
    assert accepted.json()["code"].startswith("hca_code_")
    assert accepted.json()["redirect_uri"] == (
        "https://sqlbot.example.com/login/callback"
    )
    assert rejected.status_code == 403
    assert rejected.json() == {
        "detail": "application_redirect_uri_not_allowed"
    }
    assert safe_failure.status_code == 404
    assert safe_failure.json() == {
        "detail": {
            "code": "application_agent_not_found",
            "redirect_uri": "https://sqlbot.example.com/login/callback",
        }
    }
