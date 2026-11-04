from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


EXPERT_PROFILE_OPERATION_ACTIONS = (
    "create",
    "install",
    "update",
    "configure",
    "delete",
)
EXPERT_PROFILE_OPERATION_STATUSES = (
    "running",
    "succeeded",
    "failed",
)


class ExpertProfileOperation(TimestampMixin, Base):
    """Durable lifecycle ledger for one Backend-orchestrated Hermes Profile.

    ``runtime_agent_profile_id`` intentionally is not a foreign key: a
    successful irreversible uninstall removes the Profile row while retaining
    this small audit/reconciliation record. ``active_profile_key`` is populated
    only while an operation is running, giving every Profile a database-backed
    single-writer gate without preventing historical rows.
    """

    __tablename__ = "expert_profile_operation"
    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            name="uq_expert_profile_operation_public_id",
        ),
        UniqueConstraint(
            "active_profile_key",
            name="uq_expert_profile_operation_active_profile",
        ),
        CheckConstraint(
            "action IN ('create', 'install', 'update', 'configure', 'delete')",
            name="ck_expert_profile_operation_action",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_expert_profile_operation_status",
        ),
        Index(
            "ix_expert_profile_operation_profile_created",
            "runtime_agent_profile_id",
            "created_at",
        ),
        Index(
            "ix_expert_profile_operation_workspace_status",
            "runtime_workspace_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
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
    runtime_agent_profile_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
    )
    active_profile_key: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="running",
        index=True,
    )
    step: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="prepared",
    )
    rollback_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
