from __future__ import annotations

import base64
import re
import time
from urllib.parse import urlparse

import httpx
import pytest

from app.api.channels.wecom import wecom_callback_memory_service
from app.api.openapi.application_agents import (
    application_agent_invocation_memory_service,
)
from app.api.v1.application_agent_role_deps import (
    application_agent_admin_memory_store,
    get_application_agent_authorization_service,
)
from app.api.v1.user.application_agent_wecom import (
    application_agent_wecom_memory_service,
)
from app.api.v1.user.application_agents import (
    application_agent_memory_service,
    get_application_agent_service,
)
from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.main import app
from app.services.application_agent_admin import (
    ApplicationAgentAuthorizationService,
)
from app.services.application_agent_wecom_crypto import (
    decrypt_message,
    encrypt_message,
    extract_encrypted_xml,
    message_signature,
)
from app.services.runtime_workspace import Principal


ENCODING_AES_KEY = base64.b64encode(b"w" * 32).decode("ascii").rstrip("=")


async def _principal() -> Principal:
    return Principal(org_id=1, user_id=settings.DEV_PRINCIPAL_USER_ID)


async def _authorization_service() -> ApplicationAgentAuthorizationService:
    return ApplicationAgentAuthorizationService(
        application_agent_admin_memory_store
    )


async def _agent_service():
    return application_agent_memory_service


def _callback_request(
    *,
    callback_path: str,
    message_id: str,
    content: str,
) -> tuple[str, str]:
    timestamp = str(int(time.time()))
    nonce = f"nonce-{message_id}"
    plaintext = (
        "<xml><ToUserName>ww-corp</ToUserName>"
        "<FromUserName>zhangsan</FromUserName>"
        f"<CreateTime>{timestamp}</CreateTime><MsgType>text</MsgType>"
        f"<Content>{content}</Content><MsgId>{message_id}</MsgId>"
        "<AgentID>1000002</AgentID></xml>"
    )
    encrypted = encrypt_message(
        encoding_aes_key=ENCODING_AES_KEY,
        plaintext=plaintext,
        receive_id="ww-corp",
    )
    signature = message_signature(
        token="callback-token",
        timestamp=timestamp,
        nonce=nonce,
        encrypted=encrypted,
    )
    body = f"<xml><Encrypt>{encrypted}</Encrypt></xml>"
    url = (
        f"{callback_path}?msg_signature={signature}"
        f"&timestamp={timestamp}&nonce={nonce}"
    )
    return url, body


def _decrypt_callback_response(raw_xml: str) -> str:
    return decrypt_message(
        encoding_aes_key=ENCODING_AES_KEY,
        encrypted=extract_encrypted_xml(raw_xml),
        receive_id="ww-corp",
    )


@pytest.mark.asyncio
async def test_wecom_channel_requires_admin_and_reuses_legacy_sso_user_binding() -> None:
    application_agent_admin_memory_store.reset()
    application_agent_memory_service.reset()
    application_agent_invocation_memory_service.reset()
    application_agent_wecom_memory_service.reset()
    wecom_callback_memory_service.reset()
    app.dependency_overrides[get_principal] = _principal
    app.dependency_overrides[
        get_application_agent_authorization_service
    ] = _authorization_service
    app.dependency_overrides[get_application_agent_service] = _agent_service
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            await application_agent_admin_memory_store.replace(
                org_id=1,
                user_codes=["dev-user"],
                actor_user_id=settings.DEV_PRINCIPAL_USER_ID,
            )
            created = await client.post(
                "/api/v1/user/application-agents",
                json={
                    "slug": "wecom-agent",
                    "name": "企微助手",
                    "config": {},
                },
            )
            agent_id = created.json()["application_agent_id"]
            await client.post(
                f"/api/v1/user/application-agents/{agent_id}/clients",
                json={
                    "name": "验收调用方",
                    "allow_user_delegation": True,
                    "allow_service_invocation": False,
                    "scopes": ["responses:create"],
                },
            )
            await client.post(
                f"/api/v1/user/application-agents/{agent_id}/publish"
            )
            configured = await client.put(
                f"/api/v1/user/application-agents/{agent_id}/wecom-channel",
                json={
                    "name": "工单企微入口",
                    "corp_id": "ww-corp",
                    "wecom_agent_id": 1000002,
                    "callback_token": "callback-token",
                    "encoding_aes_key": ENCODING_AES_KEY,
                    "agent_secret": "managed-agent-secret",
                    "enabled": True,
                },
            )
            assert configured.status_code == 200
            summary = configured.json()
            assert summary["status"] == "active"
            assert summary["secrets_configured"] is True
            assert "callback_token" not in summary
            assert "agent_secret" not in summary
            old_entry_path = urlparse(summary["entry_url"]).path
            rotated = await client.post(
                f"/api/v1/user/application-agents/{agent_id}"
                "/wecom-channel/rotate-entry"
            )
            assert rotated.status_code == 200
            assert rotated.json()["entry_url"] != summary["entry_url"]
            assert (await client.get(old_entry_path)).status_code == 404
            current_entry = await client.get(
                urlparse(rotated.json()["entry_url"]).path
            )
            assert current_entry.status_code == 302
            assert "open.weixin.qq.com/connect/oauth2/authorize" in (
                current_entry.headers["location"]
            )

            callback_path = urlparse(summary["callback_url"]).path
            first_url, first_body = _callback_request(
                callback_path=callback_path,
                message_id="msg-1",
                content="查询工单",
            )
            first = await client.post(
                first_url,
                content=first_body,
                headers={"Content-Type": "application/xml"},
            )
            assert first.status_code == 200
            first_plaintext = _decrypt_callback_response(first.text)
            ticket_match = re.search(r"ticket=([^<]+)", first_plaintext)
            assert ticket_match is not None

            bound = await client.post(
                "/api/v1/user/application-agent-wecom/bind",
                json={"ticket": ticket_match.group(1)},
            )
            assert bound.status_code == 200
            assert bound.json()["application_agent_name"] == "企微助手"
            assert bound.json()["wecom_member_id_masked"] == "zh****an"
            replay = await client.post(
                "/api/v1/user/application-agent-wecom/bind",
                json={"ticket": ticket_match.group(1)},
            )
            assert replay.status_code == 401

            second_url, second_body = _callback_request(
                callback_path=callback_path,
                message_id="msg-2",
                content="继续分析",
            )
            second = await client.post(
                second_url,
                content=second_body,
                headers={"Content-Type": "application/xml"},
            )
            assert second.status_code == 200
            assert "已收到：继续分析" in _decrypt_callback_response(second.text)
            assert len(application_agent_invocation_memory_service.conversations) == 1
    finally:
        for dependency in (
            get_principal,
            get_application_agent_authorization_service,
            get_application_agent_service,
        ):
            app.dependency_overrides.pop(dependency, None)
        application_agent_admin_memory_store.reset()
        application_agent_memory_service.reset()
        application_agent_invocation_memory_service.reset()
        application_agent_wecom_memory_service.reset()
        wecom_callback_memory_service.reset()
