from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
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


class AssetRolloutBatch(TimestampMixin, Base):
    """Immutable impact snapshot for one admin asset publication action."""

    __tablename__ = "asset_rollout_batch"
    __table_args__ = (
        Index("ix_asset_rollout_batch_org_status", "org_id", "status"),
        Index(
            "ix_asset_rollout_batch_asset_created",
            "asset_type",
            "asset_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rollout_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    asset_type: Mapped[str] = mapped_column(String(24), nullable=False)
    asset_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    source_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False, index=True
    )
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_user_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    affected_profile_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    waiting_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updating_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    applied_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    superseded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revoked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="waiting", index=True
    )
    snapshot_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AssetRolloutTarget(TimestampMixin, Base):
    __tablename__ = "asset_rollout_target"
    __table_args__ = (
        UniqueConstraint(
            "asset_rollout_batch_id",
            "runtime_agent_profile_id",
            name="uq_asset_rollout_target_profile",
        ),
        Index(
            "ix_asset_rollout_target_batch_status",
            "asset_rollout_batch_id",
            "status",
        ),
        Index(
            "ix_asset_rollout_target_user_status",
            "user_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    asset_rollout_batch_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("asset_rollout_batch.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False, index=True
    )
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id"),
        nullable=False,
        index=True,
    )
    runtime_agent_profile_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("runtime_agent_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    convergence_job_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("profile_convergence_job.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="waiting", index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
