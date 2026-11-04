from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Integer,
    CheckConstraint,
    ForeignKey,
    Index,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class ChatLog(TimestampMixin, Base):
    __tablename__ = "chat_log"
    __table_args__ = (
        UniqueConstraint("runtime_run_id", "role", name="uq_chat_log_run_role"),
        UniqueConstraint("session_id", "message_seq", name="uq_chat_log_session_seq"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_log_role"),
        CheckConstraint(
            "content_format IN ('text', 'markdown')",
            name="ck_chat_log_content_format",
        ),
        CheckConstraint(
            "message_status IN ('accepted', 'completed')",
            name="ck_chat_log_message_status",
        ),
        CheckConstraint(
            "ingest_source IN ('runtime_run', 'hermes_backfill')",
            name="ck_chat_log_ingest_source",
        ),
        Index(
            "ix_chat_log_owner_session_seq",
            "org_id",
            "user_id",
            "session_id",
            "message_seq",
        ),
        Index(
            "ix_chat_log_org_channel_occurred",
            "org_id",
            "channel",
            "occurred_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("runtime_session.id"),
        nullable=False,
        index=True,
    )
    runtime_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    message_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    content_format: Mapped[str] = mapped_column(String(16), nullable=False)
    message_status: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    context_runtime_agent_profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
    )
    model_entry_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_entry.id"),
        nullable=True,
        index=True,
    )
    ingest_source: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
