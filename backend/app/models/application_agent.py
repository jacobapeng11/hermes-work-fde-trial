from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class ApplicationAgent(TimestampMixin, Base):
    __tablename__ = "application_agent"
    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_application_agent_org_slug"),
        CheckConstraint(
            "status IN ('unpublished', 'published', 'disabled')",
            name="ck_application_agent_status",
        ),
        CheckConstraint(
            "apply_status IN ('not_applied', 'applying', 'applied', 'failed')",
            name="ck_application_agent_apply_status",
        ),
        CheckConstraint(
            "lifecycle_status IN ('active', 'deleting')",
            name="ck_application_agent_lifecycle_status",
        ),
        Index("ix_application_agent_org_status_updated", "org_id", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unpublished", index=True)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False)
    updated_by_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    config_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    personal_knowledge_binding_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    applied_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    apply_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_applied")
    last_apply_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    delete_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delete_last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApplicationAgentPersonalKnowledgeBinding(TimestampMixin, Base):
    __tablename__ = "application_agent_personal_knowledge_binding"
    __table_args__ = (
        UniqueConstraint(
            "application_agent_id",
            "knowledge_base_id",
            name="uq_application_agent_personal_kb",
        ),
        CheckConstraint(
            "status IN ('enabled', 'permission_revoked', 'disabled')",
            name="ck_application_agent_personal_kb_status",
        ),
        Index(
            "ix_application_agent_personal_kb_status",
            "application_agent_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
        index=True,
    )
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    knowledge_base_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
    )
    selected_by_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="enabled",
        index=True,
    )
    selected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApplicationAgentConfigRevision(TimestampMixin, Base):
    __tablename__ = "application_agent_config_revision"
    __table_args__ = (
        UniqueConstraint(
            "application_agent_id",
            "generation",
            name="uq_application_agent_config_generation",
        ),
        Index(
            "ix_application_agent_config_revision_agent_created",
            "application_agent_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id"),
        nullable=False,
        index=True,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    config_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False)
    apply_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_applied")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ApplicationAgentSkill(TimestampMixin, Base):
    __tablename__ = "application_agent_skill"
    __table_args__ = (
        UniqueConstraint(
            "application_agent_id",
            "slug",
            name="uq_application_agent_skill_agent_slug",
        ),
        Index(
            "ix_application_agent_skill_org_agent",
            "org_id",
            "application_agent_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
        index=True,
    )
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="personal",
    )
    package_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class ApplicationAgentRuntimeBinding(TimestampMixin, Base):
    __tablename__ = "application_agent_runtime_binding"
    __table_args__ = (
        UniqueConstraint("application_agent_id", name="uq_application_agent_runtime_binding_agent"),
        UniqueConstraint("runtime_id", name="uq_application_agent_runtime_binding_runtime"),
        CheckConstraint(
            "status IN ('absent', 'starting', 'ready', 'updating', 'failed', 'stopped')",
            name="ck_application_agent_runtime_binding_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id"),
        nullable=False,
        index=True,
    )
    runtime_id: Mapped[str] = mapped_column(String(128), nullable=False)
    k8s_namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    knative_service: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_root_pvc: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_root_pvc_uid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pvc_storage_identity_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    runtime_root_host_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    config_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    image: Mapped[str] = mapped_column(String(1000), nullable=False)
    image_digest: Mapped[str | None] = mapped_column(String(255), nullable=True)
    endpoint_base_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    endpoint_host_header: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="absent", index=True)
    active_run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ApplicationClient(TimestampMixin, Base):
    __tablename__ = "application_client"
    __table_args__ = (
        UniqueConstraint("org_id", "client_id", name="uq_application_client_org_client_id"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_application_client_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    auth_method: Mapped[str] = mapped_column(String(32), nullable=False, default="client_secret")
    credential_digest_or_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    allowed_redirect_uris: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rate_limit_policy: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False)


class ApplicationAgentClientGrant(TimestampMixin, Base):
    __tablename__ = "application_agent_client_grant"
    __table_args__ = (
        UniqueConstraint(
            "application_agent_id",
            "application_client_id",
            name="uq_application_agent_client_grant",
        ),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_application_agent_client_grant_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id"),
        nullable=False,
        index=True,
    )
    application_client_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_client.id"),
        nullable=False,
        index=True,
    )
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    allow_user_delegation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_service_invocation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class ApplicationAgentAuthorizationCode(TimestampMixin, Base):
    __tablename__ = "application_agent_authorization_code"
    __table_args__ = (
        UniqueConstraint(
            "code_digest",
            name="uq_application_agent_authorization_code_digest",
        ),
        Index(
            "ix_application_agent_authorization_code_expiry",
            "expires_at",
            "consumed_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id"),
        nullable=False,
        index=True,
    )
    application_client_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_client.id"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )


class ApplicationAgentConversation(TimestampMixin, Base):
    __tablename__ = "application_agent_conversation"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "application_agent_id",
            "application_client_id",
            "subject_key",
            "conversation_key",
            name="uq_application_agent_conversation_scope",
        ),
        Index(
            "ix_application_agent_conversation_recent",
            "application_agent_id",
            "last_active_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id"),
        nullable=False,
        index=True,
    )
    application_client_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_client.id"),
        nullable=False,
        index=True,
    )
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=True)
    subject_service_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subject_key: Mapped[str] = mapped_column(String(160), nullable=False)
    conversation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    runtime_session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    prompt_contract_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    prompt_contract_digest: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    prompt_contract_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )


class ApplicationAgentStudioSession(TimestampMixin, Base):
    __tablename__ = "application_agent_studio_session"
    __table_args__ = (
        UniqueConstraint(
            "application_agent_id",
            "user_id",
            "runtime_session_id",
            name="uq_application_agent_studio_session",
        ),
        Index(
            "ix_application_agent_studio_session_recent",
            "application_agent_id",
            "user_id",
            "last_active_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    runtime_session_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="新任务",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        index=True,
    )
    last_active_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )


class ApplicationAgentStudioFileArtifact(TimestampMixin, Base):
    __tablename__ = "application_agent_studio_file_artifact"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'active', 'failed', 'deleted')",
            name="ck_application_agent_studio_file_status",
        ),
        CheckConstraint(
            "source_type IN ('upload', 'runtime')",
            name="ck_application_agent_studio_file_source",
        ),
        Index(
            "ix_application_agent_studio_file_session",
            "studio_session_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_application_agent_studio_file_run",
            "application_agent_run_id",
        ),
        Index(
            "ix_application_agent_studio_file_path",
            "studio_session_id",
            "relative_path",
            mysql_length={"relative_path": 766},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    studio_session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent_studio_session.id"),
        nullable=False,
    )
    application_agent_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("application_agent_run.id"),
        nullable=True,
    )
    relative_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)


class ApplicationAgentRun(TimestampMixin, Base):
    __tablename__ = "application_agent_run"
    __table_args__ = (
        UniqueConstraint("runtime_run_id", name="uq_application_agent_run_runtime_run_id"),
        UniqueConstraint(
            "application_agent_id",
            "application_client_id",
            "channel",
            "idempotency_key",
            name="uq_application_agent_run_channel_idempotency",
        ),
        UniqueConstraint(
            "runtime_binding_id",
            "active_session_key",
            name="uq_application_agent_run_active_session",
        ),
        Index(
            "ix_application_agent_run_active_identity",
            "runtime_binding_id",
            "runtime_session_id",
            "turn_id",
            "status",
        ),
        Index("ix_application_agent_run_agent_created", "application_agent_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id"),
        nullable=False,
        index=True,
    )
    runtime_binding_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent_runtime_binding.id"),
        nullable=False,
        index=True,
    )
    application_client_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("application_client.id"),
        nullable=True,
        index=True,
    )
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("application_agent_conversation.id"),
        nullable=True,
        index=True,
    )
    runtime_session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    active_session_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    runtime_run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    turn_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=True)
    subject_service_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    payload_digest: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    response_snapshot: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
    )
    submission_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="reserved",
    )
    submit_intent_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    dashboard_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    capacity_released_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ApplicationAgentConversationFileArtifact(TimestampMixin, Base):
    """Control-plane index for files produced in one OpenAPI Conversation."""

    __tablename__ = "application_agent_conversation_file_artifact"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "relative_path_sha256",
            name="uq_application_agent_conversation_file_path",
        ),
        CheckConstraint(
            "source_type IN ('runtime')",
            name="ck_application_agent_conversation_file_source",
        ),
        CheckConstraint(
            "status IN ('active', 'deleted')",
            name="ck_application_agent_conversation_file_status",
        ),
        Index(
            "ix_application_agent_conversation_file_list",
            "conversation_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_application_agent_conversation_file_run",
            "application_agent_run_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
        index=True,
    )
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent_conversation.id"),
        nullable=False,
    )
    application_agent_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent_run.id"),
        nullable=False,
    )
    relative_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    relative_path_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="runtime")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class ApplicationAgentMemory(TimestampMixin, Base):
    __tablename__ = "application_agent_memory"
    __table_args__ = (
        UniqueConstraint(
            "application_agent_id",
            "application_client_id",
            "subject_key",
            "memory_key",
            name="uq_application_agent_memory_scope_key",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id"),
        nullable=False,
        index=True,
    )
    application_client_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_client.id"),
        nullable=False,
        index=True,
    )
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=True)
    subject_service_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subject_key: Mapped[str] = mapped_column(String(160), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(255), nullable=False)
    memory_value: Mapped[dict] = mapped_column(JSON, nullable=False)


class ApplicationAgentWeComChannel(TimestampMixin, Base):
    __tablename__ = "application_agent_wecom_channel"
    __table_args__ = (
        UniqueConstraint(
            "application_agent_id",
            name="uq_application_agent_wecom_channel_agent",
        ),
        UniqueConstraint(
            "callback_key",
            name="uq_application_agent_wecom_channel_callback_key",
        ),
        UniqueConstraint(
            "access_key_digest",
            name="uq_application_agent_wecom_channel_access_key",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_application_agent_wecom_channel_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
        index=True,
    )
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id"),
        nullable=False,
        index=True,
    )
    application_client_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_client.id"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    corp_id: Mapped[str] = mapped_column(String(128), nullable=False)
    wecom_agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    callback_key: Mapped[str] = mapped_column(String(64), nullable=False)
    access_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    access_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    internal_client_secret_encrypted: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    callback_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    encoding_aes_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    agent_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="disabled",
        index=True,
    )


class ApplicationAgentWeComOAuthState(TimestampMixin, Base):
    __tablename__ = "application_agent_wecom_oauth_state"
    __table_args__ = (
        UniqueConstraint(
            "state_digest",
            name="uq_application_agent_wecom_oauth_state_digest",
        ),
        Index(
            "ix_application_agent_wecom_oauth_state_expiry",
            "expires_at",
            "consumed_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent_wecom_channel.id"),
        nullable=False,
        index=True,
    )
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApplicationAgentWeComBindingTicket(TimestampMixin, Base):
    __tablename__ = "application_agent_wecom_binding_ticket"
    __table_args__ = (
        UniqueConstraint(
            "ticket_digest",
            name="uq_application_agent_wecom_binding_ticket_digest",
        ),
        Index(
            "ix_application_agent_wecom_binding_ticket_expiry",
            "expires_at",
            "consumed_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent_wecom_channel.id"),
        nullable=False,
        index=True,
    )
    wecom_member_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ticket_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApplicationAgentWeComUserBinding(TimestampMixin, Base):
    __tablename__ = "application_agent_wecom_user_binding"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "wecom_member_id",
            name="uq_application_agent_wecom_user_binding_member",
        ),
        CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_application_agent_wecom_user_binding_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent_wecom_channel.id"),
        nullable=False,
        index=True,
    )
    wecom_member_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
    )
    platform_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    bound_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ApplicationAgentWeComCallbackEvent(TimestampMixin, Base):
    __tablename__ = "application_agent_wecom_callback_event"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "message_id",
            name="uq_application_agent_wecom_callback_event_message",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'ignored')",
            name="ck_application_agent_wecom_callback_event_status",
        ),
        Index(
            "ix_application_agent_wecom_callback_event_status_created",
            "status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent_wecom_channel.id"),
        nullable=False,
        index=True,
    )
    message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    wecom_member_id: Mapped[str] = mapped_column(String(128), nullable=False)
    message_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    runtime_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
