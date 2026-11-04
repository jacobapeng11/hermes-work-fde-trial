from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class ChatFileArtifact(TimestampMixin, Base):
    __tablename__ = "chat_file_artifact"
    __table_args__ = (
        Index("ix_chat_file_runtime_session", "runtime_workspace_id", "session_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False, index=True)
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("runtime_session.id"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("runtime_run.id"), nullable=True)
    relative_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256_digest: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="runtime")
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="owner")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)


class RuntimeAuditLog(TimestampMixin, Base):
    __tablename__ = "runtime_audit_log"
    __table_args__ = (
        Index("ix_runtime_audit_target", "org_id", "target_type", "target_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    runtime_workspace_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)


class RuntimeRunEvent(TimestampMixin, Base):
    """Short-retention platform event ledger for SSE replay and diagnostics."""

    __tablename__ = "runtime_run_event"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_runtime_run_event_sequence"),
        UniqueConstraint(
            "run_id",
            "dedupe_key",
            name="uq_runtime_run_event_dedupe",
        ),
        Index("ix_runtime_run_event_public_sequence", "runtime_run_public_id", "seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("runtime_run.id"), nullable=False, index=True)
    runtime_run_public_id: Mapped[str] = mapped_column(String(128), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_name: Mapped[str] = mapped_column(String(128), nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ModelUsageLog(TimestampMixin, Base):
    __tablename__ = "model_usage_log"
    __table_args__ = (
        Index("ix_model_usage_runtime", "org_id", "runtime_workspace_id", "created_at"),
        Index("ix_model_usage_model", "org_id", "model_entry_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    runtime_workspace_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    session_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    runtime_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_entry_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    provider_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
