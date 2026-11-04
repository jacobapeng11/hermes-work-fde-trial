from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
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


class RuntimeAgentProfileRevision(TimestampMixin, Base):
    """Immutable complete Desired snapshot for one Runtime Agent Profile."""

    __tablename__ = "runtime_agent_profile_revision"
    __table_args__ = (
        UniqueConstraint(
            "runtime_agent_profile_id",
            "revision",
            name="uq_runtime_agent_profile_revision",
        ),
        CheckConstraint("revision > 0", name="ck_profile_revision_positive"),
        Index(
            "ix_profile_revision_profile_created",
            "runtime_agent_profile_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
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
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    skill_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    mcp_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    knowledge_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expert_package_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("expert_package_version.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )


class ProfileSkillBinding(TimestampMixin, Base):
    __tablename__ = "profile_skill_binding"
    __table_args__ = (
        UniqueConstraint(
            "runtime_agent_profile_id",
            "skill_id",
            name="uq_profile_skill_binding_asset",
        ),
        CheckConstraint(
            "version_policy IN ('follow_latest', 'pinned')",
            name="ck_profile_skill_binding_policy",
        ),
        Index(
            "ix_profile_skill_binding_profile_status",
            "runtime_agent_profile_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
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
    skill_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("skill.id"), nullable=False, index=True
    )
    skill_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("skill_version.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    version_policy: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="enabled", index=True
    )
    binding_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ProfileMcpBinding(TimestampMixin, Base):
    __tablename__ = "profile_mcp_binding"
    __table_args__ = (
        UniqueConstraint(
            "runtime_agent_profile_id",
            "mcp_id",
            name="uq_profile_mcp_binding_asset",
        ),
        Index(
            "ix_profile_mcp_binding_profile_status",
            "runtime_agent_profile_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
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
    mcp_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("mcp.id"), nullable=False, index=True
    )
    mcp_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("mcp_version.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    toolset_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="enabled", index=True
    )


class ProfileKnowledgeBinding(TimestampMixin, Base):
    __tablename__ = "profile_knowledge_binding"
    __table_args__ = (
        UniqueConstraint(
            "runtime_agent_profile_id",
            "knowledge_base_id",
            name="uq_profile_knowledge_binding_asset",
        ),
        Index(
            "ix_profile_knowledge_binding_profile_status",
            "runtime_agent_profile_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
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
    knowledge_base_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("knowledge_base.id"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="enabled", index=True
    )


class ProfileConvergenceJob(TimestampMixin, Base):
    """One durable latest-target coordinator per Profile."""

    __tablename__ = "profile_convergence_job"
    __table_args__ = (
        UniqueConstraint(
            "runtime_agent_profile_id",
            name="uq_profile_convergence_job_profile",
        ),
        Index(
            "ix_profile_convergence_job_claim",
            "status",
            "available_at",
            "lease_expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
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
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="waiting", index=True
    )
    stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="resolve_desired"
    )
    available_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProfileConvergenceAttempt(TimestampMixin, Base):
    __tablename__ = "profile_convergence_attempt"
    __table_args__ = (
        UniqueConstraint(
            "profile_convergence_job_id",
            "attempt_number",
            name="uq_profile_convergence_attempt_number",
        ),
        Index(
            "ix_profile_convergence_attempt_profile_created",
            "runtime_agent_profile_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    profile_convergence_job_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("profile_convergence_job.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_agent_profile_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("runtime_agent_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    target_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_applied_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ProfileExecutionControlJob(TimestampMixin, Base):
    """Latest durable revoke/enable target for one Runtime Agent Profile.

    Projection convergence and execution admission are intentionally separate:
    a normal Desired failure must never clear an emergency revoke checkpoint.
    """

    __tablename__ = "profile_execution_control_job"
    __table_args__ = (
        UniqueConstraint(
            "runtime_agent_profile_id",
            name="uq_profile_execution_control_job_profile",
        ),
        UniqueConstraint(
            "operation_id",
            name="uq_profile_execution_control_job_operation",
        ),
        CheckConstraint(
            "target_action IN ('revoke', 'enable')",
            name="ck_profile_execution_control_job_action",
        ),
        CheckConstraint(
            "target_generation > 0",
            name="ck_profile_execution_control_job_generation",
        ),
        Index(
            "ix_profile_execution_control_job_claim",
            "status",
            "available_at",
            "lease_expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
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
    source_rollout_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_action: Mapped[str] = mapped_column(String(16), nullable=False)
    target_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    cancel_active_runs: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="waiting", index=True
    )
    available_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    applied_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    applied_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
