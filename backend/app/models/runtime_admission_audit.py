from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class RuntimeAdmissionAudit(TimestampMixin, Base):
    """Queryable Runtime Edge admission evidence, including pre-Run rejects."""

    __tablename__ = "runtime_admission_audit"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_runtime_admission_audit_event"),
        Index("ix_runtime_admission_audit_org_occurred", "org_id", "occurred_at"),
        Index("ix_runtime_admission_audit_runtime_occurred", "runtime_id", "occurred_at"),
        Index("ix_runtime_admission_audit_session_occurred", "runtime_session_id", "occurred_at"),
        Index("ix_runtime_admission_audit_message_occurred", "client_message_id", "occurred_at"),
        Index("ix_runtime_admission_audit_run_occurred", "runtime_run_id", "occurred_at"),
        Index("ix_runtime_admission_audit_error_occurred", "outcome", "error_code", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_id: Mapped[str] = mapped_column(String(128), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(128), nullable=False)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    runtime_workspace_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    runtime_session_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    client_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    runtime_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(String(128), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exception_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    config_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attachment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upstream_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upstream_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
