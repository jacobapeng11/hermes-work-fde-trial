from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class ScheduledTaskScheduleType(str, Enum):
    ONCE = "once"
    INTERVAL = "interval"
    CRON = "cron"


class ScheduledTaskStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    DELETED = "deleted"


class ScheduledTaskLastRunStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


SCHEDULED_TASK_SCHEDULE_TYPES = tuple(item.value for item in ScheduledTaskScheduleType)
SCHEDULED_TASK_STATUSES = tuple(item.value for item in ScheduledTaskStatus)
SCHEDULED_TASK_LAST_RUN_STATUSES = tuple(item.value for item in ScheduledTaskLastRunStatus)


class ScheduledTask(TimestampMixin, Base):
    """Persisted control-plane configuration for one scheduled prompt."""

    __tablename__ = "scheduled_task"
    __table_args__ = (
        CheckConstraint(
            "schedule_type IN ('once', 'interval', 'cron')",
            name="ck_scheduled_task_schedule_type",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'deleted')",
            name="ck_scheduled_task_status",
        ),
        CheckConstraint(
            "last_run_status IS NULL OR last_run_status IN ('success', 'failed')",
            name="ck_scheduled_task_last_run_status",
        ),
        CheckConstraint(
            "timezone = 'Asia/Shanghai'",
            name="ck_scheduled_task_timezone",
        ),
        Index("ix_scheduled_task_status_next_run", "status", "next_run_at"),
        Index("ix_scheduled_task_owner_status_updated", "org_id", "user_id", "status", "updated_at"),
        Index("ix_scheduled_task_target_session", "target_session_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False)
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id"),
        nullable=False,
    )
    target_session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("runtime_session.id"),
        nullable=False,
    )

    task_name: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(16), nullable=False)
    schedule_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    cron_expr: Mapped[str | None] = mapped_column(String(100), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ScheduledTaskStatus.ACTIVE.value)
    next_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_runtime_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ScheduledTaskExecutionLease(TimestampMixin, Base):
    """Fenced, cross-replica ownership of one task execution.

    One row per task serializes scheduled and manual triggers.  An expired
    lease may be taken over without losing the already-created Runtime Run,
    allowing the new Backend replica to finalize the original execution
    instead of submitting the prompt twice.
    """

    __tablename__ = "scheduled_task_execution_lease"
    __table_args__ = (
        CheckConstraint(
            "trigger_kind IN ('scheduled', 'manual')",
            name="ck_scheduled_task_execution_lease_trigger",
        ),
        CheckConstraint(
            "status IN ('claimed', 'running', 'completed', 'failed', 'superseded', 'released')",
            name="ck_scheduled_task_execution_lease_status",
        ),
        Index(
            "ix_scheduled_task_execution_lease_expiry",
            "status",
            "lease_expires_at",
        ),
        Index(
            "ix_scheduled_task_execution_lease_owner",
            "owner_instance_id",
            "lease_expires_at",
        ),
    )

    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("scheduled_task.id", ondelete="CASCADE"),
        primary_key=True,
    )
    claim_token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    owner_instance_id: Mapped[str] = mapped_column(
        String(160), nullable=False
    )
    lease_epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1
    )
    trigger_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    scheduled_fire_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="claimed", index=True
    )
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False
    )
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    runtime_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
