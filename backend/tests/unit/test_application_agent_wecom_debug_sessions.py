from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles

from app.api.v1.user.application_agents import _studio_session
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models import (
    ApplicationAgent,
    ApplicationAgentConversation,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentStudioSession,
    ApplicationClient,
    ImInboundMessage,
    Organization,
    RuntimeProjectionReceipt,
    User,
)
from app.models.base import Base
from app.services.application_agent_invocation import (
    ApplicationAgentInvocationError,
    SqlAlchemyApplicationAgentInvocationService,
)
from app.services.application_agent_conversation_views import (
    _wecom_debug_session_presentation,
)
from app.services.runtime_workspace import Principal


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_integer(
    _type: BigInteger,
    _compiler: object,
    **_kwargs: object,
) -> str:
    return "INTEGER"

class _InvocationService(SqlAlchemyApplicationAgentInvocationService):
    def _endpoint(
        self,
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
    ) -> RuntimeEndpoint:
        return RuntimeEndpoint(
            runtime_workspace_id=int(agent.id),
            runtime_id=str(binding.runtime_id),
            endpoint_base_url="http://runtime.test",
            endpoint_host_header="runtime.test",
            api_key="test-only",
        )


async def _database() -> tuple[
    Any,
    async_sessionmaker[AsyncSession],
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                Organization.__table__,
                User.__table__,
                ApplicationAgent.__table__,
                ApplicationAgentRuntimeBinding.__table__,
                ApplicationClient.__table__,
                ApplicationAgentConversation.__table__,
                ApplicationAgentRun.__table__,
                ApplicationAgentStudioSession.__table__,
                RuntimeProjectionReceipt.__table__,
                ImInboundMessage.__table__,
            ],
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(db: AsyncSession) -> None:
    db.add_all([
        Organization(id=1, code="org-1", name="测试企业"),
        User(
            id=1,
            org_id=1,
            user_code="ADMIN",
            name="管理员",
            status="active",
        ),
        User(
            id=2,
            org_id=1,
            user_code="10002",
            name="张三",
            status="active",
        ),
        User(
            id=3,
            org_id=1,
            user_code="48201",
            name="李四",
            status="active",
        ),
    ])
    await db.commit()
    db.add_all([
        ApplicationAgent(
            id=25,
            org_id=1,
            slug="ticket-agent",
            name="工单助手",
            status="published",
            created_by_user_id=1,
            updated_by_user_id=1,
            config={},
            config_generation=1,
            applied_generation=1,
            apply_status="applied",
        ),
        ApplicationAgent(
            id=26,
            org_id=1,
            slug="other-agent",
            name="其他助手",
            status="published",
            created_by_user_id=1,
            updated_by_user_id=1,
            config={},
            config_generation=1,
            applied_generation=1,
            apply_status="applied",
        ),
        ApplicationClient(
            id=40,
            org_id=1,
            client_id="wecom-client",
            name="企微渠道",
            auth_method="internal_channel",
            credential_digest_or_reference="managed",
            status="active",
            allowed_redirect_uris=[],
            rate_limit_policy={},
            created_by_user_id=1,
        ),
        ApplicationClient(
            id=41,
            org_id=1,
            client_id="ticket-portal",
            name="工单门户",
            auth_method="client_secret_basic",
            credential_digest_or_reference="managed",
            status="active",
            allowed_redirect_uris=[],
            rate_limit_policy={},
            created_by_user_id=1,
        ),
    ])
    await db.commit()
    db.add_all([
        ApplicationAgentRuntimeBinding(
            id=30,
            org_id=1,
            application_agent_id=25,
            runtime_id="application-agent-25",
            k8s_namespace="test",
            knative_service="application-agent-25",
            runtime_root_pvc="test",
            runtime_root_host_path="/tmp/application-agent-25",
            config_generation=1,
            image="test",
            api_key_encrypted="test",
            status="ready",
        ),
        ApplicationAgentRuntimeBinding(
            id=31,
            org_id=1,
            application_agent_id=26,
            runtime_id="application-agent-26",
            k8s_namespace="test",
            knative_service="application-agent-26",
            runtime_root_pvc="test",
            runtime_root_host_path="/tmp/application-agent-26",
            config_generation=1,
            image="test",
            api_key_encrypted="test",
            status="ready",
        ),
    ])
    await db.commit()
    db.add_all([
        ApplicationAgentConversation(
            id=50,
            org_id=1,
            application_agent_id=25,
            application_client_id=40,
            subject_type="user",
            subject_user_id=2,
            subject_key="user:2",
            conversation_key="wecom:app:25:7:direct:zhangsan",
            runtime_session_id="app-25-direct",
            status="active",
            last_active_at=datetime(2026, 7, 30, 10, 0, 0),
        ),
        ApplicationAgentConversation(
            id=51,
            org_id=1,
            application_agent_id=25,
            application_client_id=40,
            subject_type="user",
            subject_user_id=3,
            subject_key="user:3",
            conversation_key="wecom:app:25:7:group:room-1:lisi",
            runtime_session_id="app-25-group",
            status="active",
            last_active_at=datetime(2026, 7, 30, 11, 0, 0),
        ),
        ApplicationAgentConversation(
            id=52,
            org_id=1,
            application_agent_id=25,
            application_client_id=41,
            subject_type="external_user",
            subject_service_id="customer-1001",
            subject_key="external_user:digest-1001",
            conversation_key="default",
            runtime_session_id="app-25-openapi",
            status="active",
            last_active_at=datetime(2026, 7, 30, 12, 0, 0),
        ),
        ApplicationAgentConversation(
            id=53,
            org_id=1,
            application_agent_id=26,
            application_client_id=40,
            subject_type="user",
            subject_user_id=2,
            subject_key="user:2",
            conversation_key="wecom:app:26:8:direct:zhangsan",
            runtime_session_id="app-26-direct",
            status="active",
            last_active_at=datetime(2026, 7, 30, 13, 0, 0),
        ),
    ])
    await db.commit()
    db.add_all([
        ApplicationAgentStudioSession(
            id=70,
            org_id=1,
            application_agent_id=25,
            user_id=1,
            runtime_session_id="studio-25-1-web",
            title="Web 调试",
            status="active",
            last_active_at=datetime(2026, 7, 30, 12, 30, 0),
        ),
        RuntimeProjectionReceipt(
            id=71,
            projection_id="run-group:accepted",
            runtime_id="application-agent-25",
            binding_id="30",
            projection_type="run.accepted",
            run_id="run-group",
            seq=1,
            config_revision="generation-1",
            payload_digest="a" * 64,
            payload={"prompt": "群聊问题"},
            occurred_at=datetime(2026, 7, 30, 11, 0, 0),
        ),
        RuntimeProjectionReceipt(
            id=72,
            projection_id="run-openapi:accepted",
            runtime_id="application-agent-25",
            binding_id="30",
            projection_type="run.accepted",
            run_id="run-openapi",
            seq=1,
            config_revision="generation-1",
            payload_digest="b" * 64,
            payload={"prompt": "API 问题"},
            occurred_at=datetime(2026, 7, 30, 12, 0, 0),
        ),
    ])
    await db.commit()
    db.add_all([
        ApplicationAgentRun(
            id=60,
            org_id=1,
            application_agent_id=25,
            runtime_binding_id=30,
            application_client_id=40,
            conversation_id=50,
            runtime_session_id="app-25-direct",
            runtime_run_id="run-direct",
            subject_type="user",
            subject_user_id=2,
            channel="wecom",
            status="completed",
            request_id="request-direct",
            response_snapshot={"output_text": "企微回复"},
        ),
        ApplicationAgentRun(
            id=61,
            org_id=1,
            application_agent_id=25,
            runtime_binding_id=30,
            application_client_id=40,
            conversation_id=51,
            runtime_session_id="app-25-group",
            runtime_run_id="run-group",
            subject_type="user",
            subject_user_id=3,
            channel="wecom",
            status="completed",
            request_id="request-group",
        ),
        ApplicationAgentRun(
            id=62,
            org_id=1,
            application_agent_id=25,
            runtime_binding_id=30,
            application_client_id=41,
            conversation_id=52,
            runtime_session_id="app-25-openapi",
            runtime_run_id="run-openapi",
            subject_type="external_user",
            subject_service_id="customer-1001",
            channel="openapi",
            status="completed",
            request_id="request-openapi",
            response_snapshot={"output_text": "API 回复"},
        ),
        ApplicationAgentRun(
            id=63,
            org_id=1,
            application_agent_id=26,
            runtime_binding_id=31,
            application_client_id=40,
            conversation_id=53,
            runtime_session_id="app-26-direct",
            runtime_run_id="run-other-agent",
            subject_type="user",
            subject_user_id=2,
            channel="wecom",
            status="completed",
            request_id="request-other-agent",
        ),
    ])
    await db.commit()
    db.add(
        ImInboundMessage(
            id=80,
            channel_id=900,
            provider="wecom",
            target_type="application_agent",
            application_agent_id=25,
            external_message_id="legacy-direct-message",
            chat_type="direct",
            identity_status="resolved",
            message_type="text",
            normalized_content={"text": "企微问题"},
            status="completed",
            application_agent_run_id=60,
            received_at=datetime(2026, 7, 30, 10, 0, 0),
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_lists_all_authorized_wecom_sessions_with_identity_metadata(
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db)
            service = _InvocationService(
                db,
                prepared_knowledge_service=SimpleNamespace(),
            )

            records = await service.list_studio_sessions(
                principal=Principal(org_id=1, user_id=1),
                agent_id=25,
            )

            assert [
                record["runtime_session_id"] for record in records
            ] == [
                "studio-25-1-web",
                "app-25-openapi",
                "app-25-group",
                "app-25-direct",
            ]
            assert records[0]["title"] == "Web 调试"
            assert records[1] == {
                "runtime_session_id": "app-25-openapi",
                "title": "外部用户 · customer-1001",
                "message_count": 2,
                "source_type": "openapi",
                "last_active_at": datetime(2026, 7, 30, 12, 0, 0),
                "is_read_only": True,
                "is_immutable": True,
                "origin_label": "API · 工单门户",
            }
            assert records[2] == {
                "runtime_session_id": "app-25-group",
                "title": "李四（48201） · 群聊",
                "message_count": 2,
                "source_type": "wecom",
                "last_active_at": datetime(2026, 7, 30, 11, 0, 0),
                "is_read_only": True,
                "is_immutable": True,
                "origin_label": "企微群聊",
            }
            assert records[3]["title"] == "张三（10002） · 单聊"
            assert records[3]["message_count"] == 2
            summary = _studio_session(records[3], agent_id=25)
            assert summary.source_type == "wecom"
            assert summary.is_read_only
            assert summary.is_immutable
            assert summary.origin_label == "企微单聊"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_wecom_history_requires_exact_agent_scoped_database_mapping(
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db)
            service = _InvocationService(
                db,
                prepared_knowledge_service=SimpleNamespace(),
            )
            principal = Principal(org_id=1, user_id=1)

            history = await service.studio_history(
                principal=principal,
                agent_id=25,
                runtime_session_id="app-25-group",
            )
            assert [item["content"] for item in history] == ["群聊问题"]

            legacy_history = await service.studio_history(
                principal=principal,
                agent_id=25,
                runtime_session_id="app-25-direct",
            )
            assert [item["content"] for item in legacy_history] == [
                "企微问题",
                "企微回复",
            ]

            openapi_history = await service.studio_history(
                principal=principal,
                agent_id=25,
                runtime_session_id="app-25-openapi",
            )
            assert [item["content"] for item in openapi_history] == [
                "API 问题",
                "API 回复",
            ]

            for invalid_session_id in (
                "app-25-forged",
                "app-26-direct",
            ):
                with pytest.raises(
                    ApplicationAgentInvocationError,
                    match="application_agent_studio_session_invalid",
                ):
                    await service.studio_history(
                        principal=principal,
                        agent_id=25,
                        runtime_session_id=invalid_session_id,
                    )
            with pytest.raises(
                ApplicationAgentInvocationError,
                match="application_agent_studio_session_invalid",
            ):
                await service.prewarm_studio_session(
                    principal=principal,
                    agent_id=25,
                    runtime_session_id="app-25-group",
                )
    finally:
        await engine.dispose()


def test_unknown_wecom_conversation_kind_remains_visible() -> None:
    assert _wecom_debug_session_presentation(
        conversation_key="legacy-wecom-conversation",
        user_name=None,
        user_code=None,
    ) == ("企业用户 · 企微会话", "企微")
