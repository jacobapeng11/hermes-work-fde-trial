from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class ImChannel(TimestampMixin, Base):
    """One durable provider connection for a personal or application Agent.

    Credentials are always stored through ``ManagedSecretCodec``.  Personal
    targets retain their fixed Runtime Session; application targets resolve a
    delegated user and conversation for every inbound message.
    """

    __tablename__ = "im_channel"
    __table_args__ = (
        UniqueConstraint(
            "runtime_workspace_id",
            "provider",
            name="uq_im_channel_workspace_provider",
        ),
        UniqueConstraint("provider", "bot_id", name="uq_im_channel_provider_bot"),
        UniqueConstraint(
            "runtime_session_row_id",
            name="uq_im_channel_runtime_session",
        ),
        UniqueConstraint(
            "application_agent_id",
            "provider",
            name="uq_im_channel_application_provider",
        ),
        CheckConstraint(
            "("
            "target_type = 'personal_agent' "
            "AND user_id IS NOT NULL "
            "AND runtime_workspace_id IS NOT NULL "
            "AND runtime_session_row_id IS NOT NULL "
            "AND application_agent_id IS NULL "
            "AND application_client_id IS NULL"
            ") OR ("
            "target_type = 'application_agent' "
            "AND runtime_workspace_id IS NULL "
            "AND runtime_session_row_id IS NULL "
            "AND application_agent_id IS NOT NULL "
            "AND application_client_id IS NOT NULL"
            ")",
            name="ck_im_channel_target",
        ),
        Index("ix_im_channel_reconcile", "enabled", "status", "updated_at"),
        Index("ix_im_channel_owner", "org_id", "user_id", "provider"),
        Index(
            "ix_im_channel_application",
            "org_id",
            "application_agent_id",
            "provider",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="personal_agent", index=True
    )
    created_by_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False, index=True
    )
    # Kept as the personal-target owner for backward compatibility.  It is
    # deliberately null for an application target because every message has
    # its own delegated sender.
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=True, index=True
    )
    runtime_workspace_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    application_agent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    application_client_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("application_client.id"),
        nullable=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="wecom"
    )
    connection_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="websocket"
    )
    bot_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    proactive_target_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    proactive_target_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    proactive_target_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="connecting", index=True
    )
    config_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    runtime_session_row_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("runtime_session.id", ondelete="RESTRICT"),
        nullable=True,
    )
    direct_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    group_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ImChannelLease(TimestampMixin, Base):
    """Fenced ownership of one provider connection."""

    __tablename__ = "im_channel_lease"
    __table_args__ = (
        Index("ix_im_channel_lease_expiry", "expires_at"),
        Index(
            "ix_im_channel_lease_owner",
            "owner_instance_id",
            "expires_at",
        ),
    )

    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("im_channel.id", ondelete="CASCADE"),
        primary_key=True,
    )
    owner_instance_id: Mapped[str] = mapped_column(
        String(160), nullable=False
    )
    lease_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ImOnboardingSession(TimestampMixin, Base):
    """Short-lived QR onboarding state owned by one authenticated user."""

    __tablename__ = "im_onboarding_session"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_im_onboarding_public_id"),
        Index(
            "ix_im_onboarding_owner",
            "org_id",
            "user_id",
            "provider",
            "status",
        ),
        Index("ix_im_onboarding_expiry", "status", "expires_at"),
        Index(
            "ix_im_onboarding_application",
            "application_agent_id",
            "provider",
            "status",
        ),
        CheckConstraint(
            "("
            "target_type = 'personal_agent' "
            "AND runtime_workspace_id IS NOT NULL "
            "AND application_agent_id IS NULL"
            ") OR ("
            "target_type = 'application_agent' "
            "AND runtime_workspace_id IS NULL "
            "AND application_agent_id IS NOT NULL"
            ")",
            name="ck_im_onboarding_target",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False, index=True
    )
    target_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="personal_agent", index=True
    )
    runtime_workspace_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    application_agent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="wecom"
    )
    method: Mapped[str] = mapped_column(
        String(32), nullable=False, default="qrcode"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )
    qr_scode_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    qr_scode_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    qr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    bot_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    credentials_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    poll_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    poll_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ImExecutionBatch(TimestampMixin, Base):
    """Durable preemptive execution unit for one WeCom session lane."""

    __tablename__ = "im_execution_batch"
    __table_args__ = (
        UniqueConstraint(
            "execution_slot_key",
            name="uq_im_execution_batch_execution_slot",
        ),
        UniqueConstraint(
            "collection_slot_key",
            name="uq_im_execution_batch_collection_slot",
        ),
        Index(
            "ix_im_execution_batch_recovery",
            "state",
            "next_attempt_at",
            "updated_at",
        ),
        Index(
            "ix_im_execution_batch_lane",
            "channel_id",
            "ordering_key",
            "id",
        ),
        CheckConstraint(
            "state IN ('collecting', 'interrupting', 'ready', "
            "'submitting', 'running', 'terminal')",
            name="ck_im_execution_batch_state",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("im_channel.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    ordering_key: Mapped[str] = mapped_column(String(255), nullable=False)
    execution_slot_key: Mapped[str | None] = mapped_column(
        String(320), nullable=True
    )
    collection_slot_key: Mapped[str | None] = mapped_column(
        String(320), nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="collecting", index=True
    )
    response_inbound_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    predecessor_batch_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("im_execution_batch.id", ondelete="SET NULL"),
        nullable=True,
    )
    predecessor_run_kind: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    predecessor_run_row_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    predecessor_runtime_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    execution_run_kind: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    execution_run_row_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    execution_runtime_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    cancel_command_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    payload_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    sealed_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    processing_owner: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    processing_lease_epoch: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    interrupt_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    predecessor_released_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    terminal_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    terminal_reason: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ImInboundMessage(TimestampMixin, Base):
    """A de-duplicated provider message and its batch membership."""

    __tablename__ = "im_inbound_message"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "channel_id",
            "external_message_id",
            name="uq_im_inbound_provider_message",
        ),
        UniqueConstraint(
            "application_agent_run_id",
            name="uq_im_inbound_application_agent_run",
        ),
        Index(
            "ix_im_inbound_channel_fifo",
            "channel_id",
            "status",
            "next_attempt_at",
            "received_at",
            "id",
        ),
        Index("ix_im_inbound_runtime_run", "runtime_run_row_id"),
        UniqueConstraint(
            "execution_batch_id",
            "batch_ordinal",
            name="uq_im_inbound_batch_ordinal",
        ),
        Index(
            "ix_im_inbound_ordering_fifo",
            "channel_id",
            "ordering_key",
            "status",
            "next_attempt_at",
            "received_at",
            "id",
        ),
        Index(
            "ix_im_inbound_application",
            "application_agent_id",
            "received_at",
        ),
        UniqueConstraint(
            "channel_id",
            "ingress_ordering_key",
            "ingress_ticket",
            name="uq_im_inbound_ingress_ticket",
        ),
        Index(
            "ix_im_inbound_dispatch_lane",
            "dispatch_lane",
            "ingress_ticket_state",
            "next_attempt_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("im_channel.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="personal_agent", index=True
    )
    application_agent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    external_message_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    external_conversation_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    chat_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="direct"
    )
    sender_member_id_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    sender_member_id_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    sender_platform_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=True, index=True
    )
    ordering_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    # Immutable, commit-ordered ingress identity. ``ordering_key`` may be
    # rebound after application-user resolution; this key never changes.
    ingress_ordering_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    ingress_ticket: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    delivery_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True, unique=True
    )
    client_message_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )
    dispatch_lane: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    ingress_ticket_state: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    ingress_payload_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    terminal_outcome_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    runtime_disposition_seq: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    runtime_disposition_state: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    runtime_canonical_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    runtime_parent_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    runtime_start_after_seq: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    execution_batch_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("im_execution_batch.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    batch_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    identity_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_required", index=True
    )
    provider_request_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_content: Mapped[dict] = mapped_column(JSON, nullable=False)
    quote_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="received", index=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    capacity_wait_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    capacity_wait_started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    runtime_run_row_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("runtime_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    application_agent_run_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("application_agent_run.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    processing_owner: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    processing_lease_epoch: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    db_committed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    scheduler_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    identity_resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    run_created_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    runtime_submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    first_runtime_public_event_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    first_provider_dispatch_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    first_provider_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ImIngressTicketCounter(TimestampMixin, Base):
    """Commit-ordered allocator for one channel conversation/session lane."""

    __tablename__ = "im_ingress_ticket_counter"

    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("im_channel.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ingress_ordering_key: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )
    last_ticket: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )


class ImMessageAttachment(TimestampMixin, Base):
    """Provider media metadata and its server-controlled staging result."""

    __tablename__ = "im_message_attachment"
    __table_args__ = (
        UniqueConstraint(
            "inbound_message_id",
            "ordinal",
            name="uq_im_attachment_message_ordinal",
        ),
        Index(
            "ix_im_attachment_staging",
            "staging_status",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    inbound_message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("im_inbound_message.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_media_metadata_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    staging_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )
    chat_file_artifact_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("chat_file_artifact.id", ondelete="SET NULL"),
        nullable=True,
    )
    runtime_relative_path: Mapped[str | None] = mapped_column(
        String(1000), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ImDeliveryCheckpoint(TimestampMixin, Base):
    """Confirmed provider-delivery cursor for replay after reconnect."""

    __tablename__ = "im_delivery_checkpoint"
    __table_args__ = (
        UniqueConstraint(
            "inbound_message_id",
            name="uq_im_delivery_inbound",
        ),
        Index("ix_im_delivery_status", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    inbound_message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("im_inbound_message.id", ondelete="CASCADE"),
        nullable=False,
    )
    runtime_run_row_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("runtime_run.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider_request_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    provider_stream_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    last_runtime_event_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    delivery_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="passive_stream"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )
    # Cumulative public text is persisted to make a retried cumulative WeCom
    # stream deterministic. It never contains private Runtime logs.
    reasoning_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    activity_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    provider_delivered_text: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    # Runtime artifacts confirmed by WeCom (or by an explicit WebUI fallback
    # notice) are skipped during delivery recovery. A provider crash between
    # send and checkpoint can still duplicate one item, but ordinary retries
    # never resend already-confirmed artifacts.
    delivered_artifact_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True
    )
    last_delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ImOutboundDelivery(TimestampMixin, Base):
    """Durable proactive delivery created from a scheduled Run terminal."""

    __tablename__ = "im_outbound_delivery"
    __table_args__ = (
        UniqueConstraint("delivery_key", name="uq_im_outbound_delivery_key"),
        Index(
            "ix_im_outbound_delivery_claim",
            "channel_id",
            "status",
            "next_attempt_at",
            "updated_at",
        ),
        Index("ix_im_outbound_delivery_run", "runtime_run_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    delivery_key: Mapped[str] = mapped_column(String(255), nullable=False)
    delivery_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="scheduled_task_result"
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("im_channel.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scheduled_task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("scheduled_task.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_run_row_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("runtime_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    bot_id_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    target_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    delivered_artifact_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True
    )
    provider_delivered_text: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    processing_owner: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    processing_lease_epoch: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrganizationWeComIdentity(TimestampMixin, Base):
    """Organization-wide mapping from an encrypted WeCom member to a User."""

    __tablename__ = "organization_wecom_identity"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "member_id_digest",
            name="uq_org_wecom_identity_member",
        ),
        UniqueConstraint(
            "org_id",
            "platform_user_id",
            name="uq_org_wecom_identity_user",
        ),
        CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_org_wecom_identity_status",
        ),
        Index(
            "ix_org_wecom_identity_hot_path",
            "org_id",
            "member_id_digest",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    member_id_digest: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    member_id_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    platform_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", index=True
    )
    bound_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    binding_source_channel_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("im_channel.id", ondelete="SET NULL"),
        nullable=True,
    )
    bound_user_name_snapshot: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    bound_user_code_snapshot: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )


class ApplicationAgentWeComUserAccess(TimestampMixin, Base):
    """Agent-scoped usage projection; never the authentication authority."""

    __tablename__ = "application_agent_wecom_user_access"
    __table_args__ = (
        UniqueConstraint(
            "application_agent_id",
            "organization_wecom_identity_id",
            name="uq_application_agent_wecom_access_identity",
        ),
        Index(
            "ix_application_agent_wecom_access_seen",
            "application_agent_id",
            "last_seen_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("im_channel.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_wecom_identity_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization_wecom_identity.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False
    )
    last_chat_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="direct"
    )
    last_external_conversation_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )


class WeComIdentityBindingTicket(TimestampMixin, Base):
    """Short-lived, one-time bridge from a WeCom sender to platform SSO."""

    __tablename__ = "wecom_identity_binding_ticket"
    __table_args__ = (
        UniqueConstraint(
            "ticket_digest",
            name="uq_wecom_identity_binding_ticket_digest",
        ),
        Index(
            "ix_wecom_identity_binding_ticket_expiry",
            "expires_at",
            "consumed_at",
        ),
        Index(
            "ix_wecom_identity_binding_ticket_source",
            "source_application_agent_id",
            "source_channel_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    ticket_digest: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    member_id_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    member_id_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    source_application_agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_channel_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("im_channel.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_chat_type: Mapped[str] = mapped_column(
        String(16), nullable=False
    )
    source_external_message_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    result_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=True
    )
