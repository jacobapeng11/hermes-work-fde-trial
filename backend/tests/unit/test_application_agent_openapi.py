from __future__ import annotations

import base64

import httpx
import pytest
from pydantic import ValidationError

from app.api.openapi.application_agents import (
    application_agent_invocation_memory_service,
)
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
)
from app.services.runtime_workspace import Principal


async def _principal() -> Principal:
    return Principal(org_id=1, user_id=1)


def _basic(client_id: str, client_secret: str) -> str:
    token = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("ascii")
    return f"Basic {token}"


def test_application_config_excludes_retired_personal_knowledge_fields() -> None:
    config = ApplicationAgentConfig(
        skill_asset_ids=[3, 3, 4],
        delegated_personal_knowledge_enabled=True,
        knowledge_bindings=[{
            "knowledge_base_id": 8,
            "access_mode": "delegated_user",
        }],
    )

    assert config.skill_asset_ids == [3, 4]
    assert "delegated_personal_knowledge_enabled" not in config.model_dump()
    assert "knowledge_bindings" not in config.model_dump()


@pytest.mark.asyncio
async def test_openapi_requires_client_and_verified_delegated_user_and_isolates_sessions() -> None:
    application_agent_memory_service.reset()
    application_agent_invocation_memory_service.reset()
    application_agent_authorization_memory_service.reset()
    principal = Principal(org_id=1, user_id=1)
    agent = await application_agent_memory_service.create_agent(
        principal=principal,
        payload=ApplicationAgentCreateRequest(
            slug="ticket-assistant",
            name="工单助手",
            config=ApplicationAgentConfig(),
        ),
    )
    client_credentials = await application_agent_memory_service.create_client(
        principal=principal,
        agent_id=agent.application_agent_id,
        payload=ApplicationClientCreateRequest(
            name="工单系统",
            allow_user_delegation=True,
            allow_service_invocation=False,
        ),
    )
    await application_agent_memory_service.publish(
        principal=principal,
        agent_id=agent.application_agent_id,
    )
    app.dependency_overrides[get_principal] = _principal
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as api:
            client_auth = _basic(
                client_credentials.client_id,
                str(client_credentials.client_secret),
            )
            missing_user = await api.post(
                "/openapi/v1/application-agents/ticket-assistant/responses",
                headers={"Authorization": client_auth},
                json={"input": "查询工单", "conversation_key": "ticket-42"},
            )
            assert missing_user.status_code == 401
            assert missing_user.json() == {
                "detail": "delegated_user_token_required"
            }

            authorization_code = await api.post(
                "/api/v1/user/application-agents/authorizations",
                json={
                    "application_agent": "ticket-assistant",
                    "client_id": client_credentials.client_id,
                },
            )
            assert authorization_code.status_code == 200
            token = await api.post(
                "/openapi/v1/application-agents/token",
                headers={"Authorization": client_auth},
                json={
                    "grant_type": "authorization_code",
                    "code": authorization_code.json()["code"],
                },
            )
            assert token.status_code == 200
            assert token.json()["identity"] == {
                "type": "user",
                "user_code": "1",
            }
            delegated_token = token.json()["access_token"]
            replay = await api.post(
                "/openapi/v1/application-agents/token",
                headers={"Authorization": client_auth},
                json={
                    "grant_type": "authorization_code",
                    "code": authorization_code.json()["code"],
                },
            )
            assert replay.status_code == 401
            first = await api.post(
                "/openapi/v1/application-agents/ticket-assistant/responses",
                headers={
                    "Authorization": client_auth,
                    "X-Hermes-Agent-Token": delegated_token,
                },
                json={"input": "查询工单", "conversation_key": "ticket-42"},
            )
            second = await api.post(
                "/openapi/v1/application-agents/ticket-assistant/responses",
                headers={
                    "Authorization": client_auth,
                    "X-Hermes-Agent-Token": delegated_token,
                },
                json={"input": "继续", "conversation_key": "ticket-42"},
            )
            standalone = await api.post(
                "/openapi/v1/application-agents/ticket-assistant/responses",
                headers={
                    "Authorization": client_auth,
                    "X-Hermes-Agent-Token": delegated_token,
                },
                json={"input": "单次问题"},
            )
            assert first.status_code == second.status_code == standalone.status_code == 200
            assert first.json()["identity"] == {
                "type": "user",
                "key": "user:1",
            }
            assert (
                first.json()["runtime_session_id"]
                == second.json()["runtime_session_id"]
            )
            assert (
                standalone.json()["runtime_session_id"]
                != first.json()["runtime_session_id"]
            )
            assert standalone.json()["conversation_key"] is None
    finally:
        app.dependency_overrides.pop(get_principal, None)
        application_agent_memory_service.reset()
        application_agent_invocation_memory_service.reset()
        application_agent_authorization_memory_service.reset()


@pytest.mark.asyncio
async def test_openapi_allows_explicit_service_identity_grant() -> None:
    application_agent_memory_service.reset()
    application_agent_invocation_memory_service.reset()
    application_agent_authorization_memory_service.reset()
    principal = Principal(org_id=1, user_id=1)
    agent = await application_agent_memory_service.create_agent(
        principal=principal,
        payload=ApplicationAgentCreateRequest(
            slug="batch-agent",
            name="批处理助手",
        ),
    )
    credentials = await application_agent_memory_service.create_client(
        principal=principal,
        agent_id=agent.application_agent_id,
        payload=ApplicationClientCreateRequest(
            name="夜间批处理",
            allow_user_delegation=False,
            allow_service_invocation=True,
        ),
    )
    await application_agent_memory_service.publish(
        principal=principal,
        agent_id=agent.application_agent_id,
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as api:
            response = await api.post(
                "/openapi/v1/application-agents/batch-agent/responses",
                headers={
                    "Authorization": _basic(
                        credentials.client_id,
                        str(credentials.client_secret),
                    )
                },
                json={"input": "执行汇总", "conversation_key": "job-20260728"},
            )
            assert response.status_code == 200
            assert response.json()["identity"] == {
                "type": "service",
                "key": f"service:{credentials.client_id}",
            }
    finally:
        application_agent_memory_service.reset()
        application_agent_invocation_memory_service.reset()
        application_agent_authorization_memory_service.reset()


@pytest.mark.asyncio
async def test_openapi_external_users_get_stable_isolated_conversations() -> None:
    application_agent_memory_service.reset()
    application_agent_invocation_memory_service.reset()
    application_agent_authorization_memory_service.reset()
    principal = Principal(org_id=1, user_id=1)
    agent = await application_agent_memory_service.create_agent(
        principal=principal,
        payload=ApplicationAgentCreateRequest(
            slug="customer-assistant",
            name="客户助手",
        ),
    )
    credentials = await application_agent_memory_service.create_client(
        principal=principal,
        agent_id=agent.application_agent_id,
        payload=ApplicationClientCreateRequest(
            name="客户门户",
            allow_user_delegation=False,
            allow_service_invocation=True,
        ),
    )
    second_credentials = await application_agent_memory_service.create_client(
        principal=principal,
        agent_id=agent.application_agent_id,
        payload=ApplicationClientCreateRequest(
            name="客服工作台",
            allow_user_delegation=False,
            allow_service_invocation=True,
        ),
    )
    delegated_credentials = await application_agent_memory_service.create_client(
        principal=principal,
        agent_id=agent.application_agent_id,
        payload=ApplicationClientCreateRequest(
            name="用户委托入口",
            allow_user_delegation=True,
            allow_service_invocation=False,
        ),
    )
    await application_agent_memory_service.publish(
        principal=principal,
        agent_id=agent.application_agent_id,
    )
    app.dependency_overrides[get_principal] = _principal
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as api:
            client_auth = _basic(
                credentials.client_id,
                str(credentials.client_secret),
            )
            first = await api.post(
                "/openapi/v1/application-agents/customer-assistant/responses",
                headers={"Authorization": client_auth},
                json={"input": "第一次", "external_user_id": "customer-1001"},
            )
            second = await api.post(
                "/openapi/v1/application-agents/customer-assistant/responses",
                headers={"Authorization": client_auth},
                json={"input": "继续", "external_user_id": "customer-1001"},
            )
            another_user = await api.post(
                "/openapi/v1/application-agents/customer-assistant/responses",
                headers={"Authorization": client_auth},
                json={"input": "另一个人", "external_user_id": "customer-1002"},
            )
            another_thread = await api.post(
                "/openapi/v1/application-agents/customer-assistant/responses",
                headers={"Authorization": client_auth},
                json={
                    "input": "售后话题",
                    "external_user_id": "customer-1001",
                    "conversation_key": "after-sales",
                },
            )
            another_client = await api.post(
                "/openapi/v1/application-agents/customer-assistant/responses",
                headers={
                    "Authorization": _basic(
                        second_credentials.client_id,
                        str(second_credentials.client_secret),
                    )
                },
                json={"input": "另一个调用方", "external_user_id": "customer-1001"},
            )

            assert first.status_code == second.status_code == 200
            assert (
                another_user.status_code
                == another_thread.status_code
                == another_client.status_code
                == 200
            )
            assert first.json()["identity"] == {
                "type": "external_user",
                "key": "external_user:customer-1001",
            }
            assert first.json()["conversation_key"] == "default"
            assert first.json()["runtime_session_id"] == second.json()[
                "runtime_session_id"
            ]
            assert first.json()["runtime_session_id"] != another_user.json()[
                "runtime_session_id"
            ]
            assert first.json()["runtime_session_id"] != another_thread.json()[
                "runtime_session_id"
            ]
            assert first.json()["runtime_session_id"] != another_client.json()[
                "runtime_session_id"
            ]

            authorization_code = await api.post(
                "/api/v1/user/application-agents/authorizations",
                json={
                    "application_agent": "customer-assistant",
                    "client_id": delegated_credentials.client_id,
                },
            )
            delegated_auth = _basic(
                delegated_credentials.client_id,
                str(delegated_credentials.client_secret),
            )
            token = await api.post(
                "/openapi/v1/application-agents/token",
                headers={"Authorization": delegated_auth},
                json={
                    "grant_type": "authorization_code",
                    "code": authorization_code.json()["code"],
                },
            )
            conflict = await api.post(
                "/openapi/v1/application-agents/customer-assistant/responses",
                headers={
                    "Authorization": delegated_auth,
                    "X-Hermes-Agent-Token": token.json()["access_token"],
                },
                json={"input": "冲突", "external_user_id": "customer-1001"},
            )
            assert conflict.status_code == 409
            assert conflict.json() == {
                "detail": "application_agent_identity_conflict"
            }
    finally:
        app.dependency_overrides.pop(get_principal, None)
        application_agent_memory_service.reset()
        application_agent_invocation_memory_service.reset()
        application_agent_authorization_memory_service.reset()


def test_external_user_id_rejects_blank_value() -> None:
    with pytest.raises(ValidationError):
        from app.schemas.application_agent_invocation import (
            ApplicationAgentResponseRequest,
        )

        ApplicationAgentResponseRequest(input="查询", external_user_id="   ")
