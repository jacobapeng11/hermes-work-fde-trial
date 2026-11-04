from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import BigInteger, func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles

from app.models import (
    ApplicationAgent,
    ApplicationAgentClientGrant,
    ApplicationAgentConversation,
    ApplicationAgentConversationFileArtifact,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentStudioFileArtifact,
    ApplicationAgentStudioSession,
    ApplicationAgentWeComUserAccess,
    ApplicationClient,
    ImChannel,
    ImChannelLease,
    ImDeliveryCheckpoint,
    ImInboundMessage,
    ImMessageAttachment,
    ImOnboardingSession,
    ModelUsageLog,
    Organization,
    OrganizationWeComIdentity,
    ProfileSkillBinding,
    RuntimeAuditLog,
    Skill,
    SkillVersion,
    UserSkillHubInstallation,
    User,
    WeComIdentityBindingTicket,
)
from app.models.base import Base
from app.services.application_agents import (
    SqlAlchemyApplicationAgentService,
)
from app.services.application_agent_asset_rollout import (
    ApplicationAgentAssetRolloutWorker,
)
from app.services.runtime_workspace import Principal


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_integer(
    _type: BigInteger, _compiler: object, **_kwargs: object
) -> str:
    return "INTEGER"


async def _database() -> tuple[
    Any, async_sessionmaker[AsyncSession]
]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    table_names = (
        "organization",
        "user",
        "application_agent",
        "application_agent_config_revision",
        "application_agent_personal_knowledge_binding",
        "application_agent_skill",
        "application_agent_runtime_binding",
        "application_client",
        "application_agent_client_grant",
        "application_agent_authorization_code",
        "application_agent_conversation",
        "application_agent_conversation_file_artifact",
        "application_agent_run",
        "application_agent_studio_session",
        "application_agent_studio_file_artifact",
        "application_agent_memory",
        "application_agent_wecom_channel",
        "application_agent_wecom_oauth_state",
        "application_agent_wecom_binding_ticket",
        "application_agent_wecom_user_binding",
        "application_agent_wecom_callback_event",
        "skill",
        "skill_version",
        "profile_skill_binding",
        "user_skillhub_installation",
        "im_channel",
        "im_channel_lease",
        "im_onboarding_session",
        "im_inbound_message",
        "im_message_attachment",
        "im_delivery_checkpoint",
        "organization_wecom_identity",
        "application_agent_wecom_user_access",
        "wecom_identity_binding_ticket",
        "model_usage_log",
        "runtime_audit_log",
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                Base.metadata.tables[table_name]
                for table_name in table_names
            ],
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _count(db: AsyncSession, model: type[Any]) -> int:
    return int(
        await db.scalar(select(func.count()).select_from(model)) or 0
    )


@pytest.mark.asyncio
async def test_delete_application_agent_purges_all_scoped_im_and_audit_data(
) -> None:
    engine, sessions = await _database()
    now = datetime.now(timezone.utc)
    try:
        async with sessions() as db:
            db.add_all([
                Organization(id=10, code="org-10", name="测试企业"),
                User(
                    id=20,
                    org_id=10,
                    user_code="E0020",
                    name="张三",
                    status="active",
                ),
            ])
            await db.commit()
            db.add_all([
                ApplicationAgent(
                    id=50,
                    org_id=10,
                    slug="delete-agent",
                    name="待删除助手",
                    status="disabled",
                    created_by_user_id=20,
                    updated_by_user_id=20,
                    config={},
                    config_generation=1,
                    applied_generation=1,
                    apply_status="applied",
                ),
                ApplicationClient(
                    id=60,
                    org_id=10,
                    client_id="wecom-delete-client",
                    name="企微渠道",
                    auth_method="managed",
                    credential_digest_or_reference="managed",
                    status="disabled",
                    allowed_redirect_uris=[],
                    rate_limit_policy={},
                    created_by_user_id=20,
                ),
                ApplicationAgentRuntimeBinding(
                    id=80,
                    org_id=10,
                    application_agent_id=50,
                    runtime_id="app-runtime-50",
                    k8s_namespace="test",
                    knative_service="app-runtime-50",
                    runtime_root_pvc="test",
                    runtime_root_host_path="/tmp/app-runtime-50",
                    config_generation=1,
                    image="test",
                    api_key_encrypted="test",
                    status="stopped",
                ),
            ])
            await db.commit()
            db.add_all([
                Skill(
                    id=110,
                    org_id=10,
                    owner_user_id=20,
                    application_agent_id=50,
                    slug="app-50-private-skill",
                    name="应用私有 Skill",
                    description="随应用 Agent 删除",
                    category_key="personal",
                    asset_scope="application",
                    visibility="private",
                    status="private",
                    current_version_id=112,
                ),
                SkillVersion(
                    id=111,
                    org_id=10,
                    skill_id=110,
                    version=1,
                    package_uri="/tmp/app-50-private-skill",
                    package_sha256="a" * 64,
                    manifest={"kind": "skill"},
                    status="deleted",
                ),
                SkillVersion(
                    id=112,
                    org_id=10,
                    skill_id=110,
                    version=2,
                    package_uri="/tmp/app-50-private-skill-v2",
                    package_sha256="b" * 64,
                    manifest={"kind": "skill", "version": 2},
                    status="private",
                ),
            ])
            await db.commit()
            db.add_all([
                ApplicationAgentClientGrant(
                    id=61,
                    org_id=10,
                    application_agent_id=50,
                    application_client_id=60,
                    scopes=["responses:write"],
                    allow_user_delegation=True,
                    allow_service_invocation=False,
                    status="disabled",
                ),
                ApplicationAgentConversation(
                    id=81,
                    org_id=10,
                    application_agent_id=50,
                    application_client_id=60,
                    subject_type="user",
                    subject_user_id=20,
                    subject_key="user:20",
                    conversation_key="wecom:group:room-1",
                    runtime_session_id="app-agent-50-room-1",
                    status="active",
                    last_active_at=now,
                ),
                ImChannel(
                    id=70,
                    org_id=10,
                    target_type="application_agent",
                    created_by_user_id=20,
                    application_agent_id=50,
                    application_client_id=60,
                    provider="wecom",
                    connection_mode="websocket",
                    bot_id="delete-bot",
                    secret_encrypted="encrypted",
                    enabled=False,
                    status="disabled",
                    config_generation=2,
                ),
            ])
            await db.commit()
            db.add_all([
                ApplicationAgentRun(
                    id=90,
                    org_id=10,
                    application_agent_id=50,
                    runtime_binding_id=80,
                    application_client_id=60,
                    conversation_id=81,
                    runtime_session_id="app-agent-50-room-1",
                    active_session_key=None,
                    runtime_run_id="app_run_delete_1",
                    subject_type="user",
                    subject_user_id=20,
                    channel="wecom",
                    status="completed",
                    request_id="im-wecom-100",
                    started_at=now,
                    finished_at=now,
                ),
                ApplicationAgentStudioSession(
                    id=91,
                    org_id=10,
                    application_agent_id=50,
                    user_id=20,
                    runtime_session_id="app-agent-50-studio-session",
                    title="Studio 删除回归",
                    status="active",
                    last_active_at=now,
                ),
                ApplicationAgentStudioFileArtifact(
                    id=92,
                    studio_session_id=91,
                    application_agent_run_id=90,
                    relative_path="workspace/sessions/app-agent-50-studio-session/report.md",
                    mime_type="text/markdown",
                    size_bytes=24,
                    sha256_digest="e" * 64,
                    source_type="runtime",
                    status="active",
                ),
                OrganizationWeComIdentity(
                    id=95,
                    org_id=10,
                    member_id_digest="a" * 64,
                    member_id_encrypted="encrypted",
                    platform_user_id=20,
                    status="active",
                    bound_at=now,
                    last_seen_at=now,
                    binding_source_channel_id=70,
                    bound_user_name_snapshot="张三",
                    bound_user_code_snapshot="E0020",
                ),
                ImInboundMessage(
                    id=100,
                    channel_id=70,
                    provider="wecom",
                    target_type="application_agent",
                    application_agent_id=50,
                    external_message_id="message-100",
                    external_conversation_id="room-1",
                    chat_type="group",
                    sender_member_id_digest="a" * 64,
                    sender_member_id_encrypted="encrypted",
                    sender_platform_user_id=20,
                    ordering_key="group:room-1",
                    identity_status="resolved",
                    message_type="text",
                    normalized_content={"text": "必须被永久删除的消息"},
                    status="completed",
                    application_agent_run_id=90,
                    received_at=now,
                    finished_at=now,
                ),
            ])
            await db.commit()
            db.add_all([
                ImChannelLease(
                    channel_id=70,
                    owner_instance_id="backend-a",
                    lease_epoch=1,
                    expires_at=now + timedelta(seconds=30),
                    heartbeat_at=now,
                ),
                ImMessageAttachment(
                    id=101,
                    inbound_message_id=100,
                    ordinal=0,
                    media_type="file",
                    display_name="secret.txt",
                    provider_media_metadata_encrypted="encrypted",
                    staging_status="ready",
                    runtime_relative_path="workspace/secret.txt",
                ),
                ImDeliveryCheckpoint(
                    id=102,
                    inbound_message_id=100,
                    last_runtime_event_seq=7,
                    delivery_mode="passive_stream",
                    status="completed",
                    reasoning_text="思考内容",
                    message_text="回复内容",
                    final_text="最终回复",
                    provider_delivered_text="最终回复",
                ),
                ApplicationAgentWeComUserAccess(
                    id=103,
                    application_agent_id=50,
                    channel_id=70,
                    organization_wecom_identity_id=95,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_chat_type="group",
                    last_external_conversation_digest="b" * 64,
                ),
                ApplicationAgentConversationFileArtifact(
                    id=110,
                    org_id=10,
                    application_agent_id=50,
                    conversation_id=81,
                    application_agent_run_id=90,
                    relative_path="workspace/sessions/session-80/report.pdf",
                    relative_path_sha256="a" * 64,
                    mime_type="application/pdf",
                    size_bytes=12,
                    source_type="runtime",
                    status="active",
                ),
                WeComIdentityBindingTicket(
                    id=104,
                    ticket_digest="c" * 64,
                    org_id=10,
                    member_id_digest="d" * 64,
                    member_id_encrypted="encrypted",
                    source_application_agent_id=50,
                    source_channel_id=70,
                    source_chat_type="group",
                    source_external_message_id="message-101",
                    expires_at=now + timedelta(minutes=5),
                ),
                ImOnboardingSession(
                    id=105,
                    public_id="onboarding-delete",
                    org_id=10,
                    user_id=20,
                    target_type="application_agent",
                    application_agent_id=50,
                    provider="wecom",
                    method="qrcode",
                    status="completed",
                    expires_at=now + timedelta(minutes=5),
                ),
                ModelUsageLog(
                    id=106,
                    org_id=10,
                    user_id=20,
                    runtime_workspace_id=50,
                    runtime_run_id="app_run_delete_1",
                    request_id="request-delete",
                    input_tokens=10,
                    output_tokens=20,
                    latency_ms=30,
                    status="success",
                ),
                ModelUsageLog(
                    id=107,
                    org_id=10,
                    user_id=20,
                    runtime_workspace_id=999,
                    runtime_run_id="unrelated-run",
                    request_id="request-keep",
                    input_tokens=1,
                    output_tokens=2,
                    latency_ms=3,
                    status="success",
                ),
                RuntimeAuditLog(
                    id=108,
                    org_id=10,
                    actor_user_id=20,
                    action="application_agent.invoke",
                    target_type="application_agent",
                    target_id="50",
                    detail={"message": "必须删除"},
                ),
                RuntimeAuditLog(
                    id=109,
                    org_id=10,
                    actor_user_id=20,
                    action="personal_agent.invoke",
                    target_type="runtime_workspace",
                    target_id="999",
                    detail={"message": "必须保留"},
                ),
            ])
            await db.commit()

            await SqlAlchemyApplicationAgentService(db).delete_agent(
                principal=Principal(org_id=10, user_id=20),
                agent_id=50,
            )

            for model in (
                ApplicationAgent,
                ApplicationClient,
                ApplicationAgentClientGrant,
                ApplicationAgentConversation,
                ApplicationAgentConversationFileArtifact,
                ApplicationAgentRun,
                ApplicationAgentStudioFileArtifact,
                ApplicationAgentStudioSession,
                ApplicationAgentRuntimeBinding,
                Skill,
                SkillVersion,
                ProfileSkillBinding,
                UserSkillHubInstallation,
                ImChannel,
                ImChannelLease,
                ImInboundMessage,
                ImMessageAttachment,
                ImDeliveryCheckpoint,
                ApplicationAgentWeComUserAccess,
                WeComIdentityBindingTicket,
                ImOnboardingSession,
            ):
                assert await _count(db, model) == 0
            assert await _count(db, OrganizationWeComIdentity) == 1
            assert await _count(db, ModelUsageLog) == 1
            assert await db.scalar(
                select(ModelUsageLog.runtime_run_id)
            ) == "unrelated-run"
            assert await _count(db, RuntimeAuditLog) == 1
            assert await db.scalar(
                select(RuntimeAuditLog.target_id)
            ) == "999"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rollout_worker_purges_deleting_application_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    deleted_runtimes: list[tuple[int, int]] = []

    class FakeRuntimeManager:
        def __init__(self, _db: AsyncSession) -> None:
            return None

        async def delete_runtime(self, *, org_id: int, agent_id: int) -> None:
            deleted_runtimes.append((org_id, agent_id))

    monkeypatch.setattr(
        "app.services.application_agent_runtime."
        "SqlAlchemyApplicationAgentRuntimeManager",
        FakeRuntimeManager,
    )
    try:
        async with sessions() as db:
            db.add_all([
                Organization(id=10, code="org-10", name="测试企业"),
                User(
                    id=20,
                    org_id=10,
                    user_code="E0020",
                    name="张三",
                    status="active",
                ),
                ApplicationAgent(
                    id=50,
                    org_id=10,
                    slug="deleting-agent",
                    name="删除中助手",
                    status="disabled",
                    created_by_user_id=20,
                    updated_by_user_id=20,
                    config={},
                    config_generation=1,
                    apply_status="applied",
                    lifecycle_status="deleting",
                    delete_requested_at=datetime.now(timezone.utc),
                ),
                ApplicationAgentRuntimeBinding(
                    id=80,
                    org_id=10,
                    application_agent_id=50,
                    runtime_id="app-runtime-50",
                    k8s_namespace="test",
                    knative_service="app-runtime-50",
                    runtime_root_pvc="test",
                    runtime_root_host_path="/tmp/app-runtime-50",
                    config_generation=1,
                    image="test",
                    api_key_encrypted="test",
                    status="stopped",
                ),
            ])
            await db.commit()

        processed = await ApplicationAgentAssetRolloutWorker(
            session_factory=sessions,
            batch_size=4,
        ).reconcile_once()

        async with sessions() as db:
            assert processed == 1
            assert deleted_runtimes == [(10, 50)]
            assert await db.get(ApplicationAgent, 50) is None
            assert await db.get(ApplicationAgentRuntimeBinding, 80) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rollout_worker_does_not_persist_driver_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()

    class FakeRuntimeManager:
        def __init__(self, _db: AsyncSession) -> None:
            return None

        async def delete_runtime(self, *, org_id: int, agent_id: int) -> None:
            del org_id, agent_id
            error = RuntimeError("FOREIGN KEY constraint failed")
            error.code = "gkpj"  # type: ignore[attr-defined]
            raise error

    monkeypatch.setattr(
        "app.services.application_agent_runtime."
        "SqlAlchemyApplicationAgentRuntimeManager",
        FakeRuntimeManager,
    )
    try:
        async with sessions() as db:
            db.add_all([
                Organization(id=10, code="org-10", name="测试企业"),
                User(
                    id=20,
                    org_id=10,
                    user_code="E0020",
                    name="张三",
                    status="active",
                ),
                ApplicationAgent(
                    id=50,
                    org_id=10,
                    slug="deleting-agent",
                    name="删除中助手",
                    status="disabled",
                    created_by_user_id=20,
                    updated_by_user_id=20,
                    config={},
                    config_generation=1,
                    apply_status="applied",
                    lifecycle_status="deleting",
                    delete_requested_at=datetime.now(timezone.utc),
                ),
            ])
            await db.commit()

        processed = await ApplicationAgentAssetRolloutWorker(
            session_factory=sessions,
            batch_size=4,
        ).reconcile_once()

        async with sessions() as db:
            agent = await db.get(ApplicationAgent, 50)
            assert processed == 0
            assert agent is not None
            assert agent.lifecycle_status == "deleting"
            assert agent.delete_last_error_code == (
                "application_agent_delete_failed"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlalchemy_disable_and_delete_have_distinct_lifecycle_states() -> None:
    """Disabling remains reversible; DELETE is the only deleting transition."""
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            db.add_all([
                Organization(id=10, code="org-10", name="测试企业"),
                User(
                    id=20,
                    org_id=10,
                    user_code="E0020",
                    name="张三",
                    status="active",
                ),
                ApplicationAgent(
                    id=50,
                    org_id=10,
                    slug="lifecycle-agent",
                    name="生命周期助手",
                    status="unpublished",
                    created_by_user_id=20,
                    updated_by_user_id=20,
                    config={},
                    config_generation=1,
                    apply_status="applied",
                    lifecycle_status="active",
                ),
            ])
            await db.commit()

            service = SqlAlchemyApplicationAgentService(db)
            principal = Principal(org_id=10, user_id=20)

            disabled = await service.disable(principal=principal, agent_id=50)
            assert disabled.status == "disabled"
            assert disabled.lifecycle_status == "active"
            assert disabled.delete_requested_at is None

            await service.prepare_delete(principal=principal, agent_id=50)
            deleting = await service.get_agent(principal=principal, agent_id=50)
            assert deleting.status == "disabled"
            assert deleting.lifecycle_status == "deleting"
            assert deleting.delete_requested_at is not None
            assert deleting.delete_last_error_code is None
    finally:
        await engine.dispose()
