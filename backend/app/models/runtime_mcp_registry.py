from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
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


class RuntimeMcpRegistryState(TimestampMixin, Base):
    """One process-global MCP Registry head per personal Runtime Binding."""

    __tablename__ = "runtime_mcp_registry_state"
    __table_args__ = (
        UniqueConstraint(
            "runtime_binding_id", name="uq_runtime_mcp_registry_state_binding"
        ),
        UniqueConstraint(
            "runtime_workspace_id",
            name="uq_runtime_mcp_registry_state_workspace",
        ),
        Index(
            "ix_runtime_mcp_registry_state_status_updated",
            "status",
            "updated_at",
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
    runtime_binding_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("hermes_runtime_binding.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    desired_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    applied_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    desired_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    applied_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="ready", index=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_reload_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reload_affected_run_ids: Mapped[list | None] = mapped_column(
        JSON,
        nullable=True,
    )


class RuntimeMcpRegistryRevision(TimestampMixin, Base):
    __tablename__ = "runtime_mcp_registry_revision"
    __table_args__ = (
        UniqueConstraint(
            "runtime_mcp_registry_state_id",
            "revision",
            name="uq_runtime_mcp_registry_revision",
        ),
        Index(
            "ix_runtime_mcp_registry_revision_state_created",
            "runtime_mcp_registry_state_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    runtime_mcp_registry_state_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("runtime_mcp_registry_state.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    registry_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    normalized_servers: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class RuntimeMcpRegistryEntry(TimestampMixin, Base):
    __tablename__ = "runtime_mcp_registry_entry"
    __table_args__ = (
        UniqueConstraint(
            "runtime_mcp_registry_state_id",
            "mcp_id",
            name="uq_runtime_mcp_registry_entry_asset",
        ),
        UniqueConstraint(
            "runtime_mcp_registry_state_id",
            "server_slug",
            name="uq_runtime_mcp_registry_entry_slug",
        ),
        Index(
            "ix_runtime_mcp_registry_entry_state_status",
            "runtime_mcp_registry_state_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    runtime_mcp_registry_state_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("runtime_mcp_registry_state.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mcp_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("mcp.id"), nullable=False, index=True
    )
    mcp_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("mcp_version.id"), nullable=False, index=True
    )
    server_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    config_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    tools_schema_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    tools_schema: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="installing", index=True
    )
    initialized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tools_listed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class RuntimeMcpRegistryJob(TimestampMixin, Base):
    __tablename__ = "runtime_mcp_registry_job"
    __table_args__ = (
        UniqueConstraint(
            "runtime_mcp_registry_state_id",
            name="uq_runtime_mcp_registry_job_state",
        ),
        Index(
            "ix_runtime_mcp_registry_job_claim",
            "status",
            "available_at",
            "lease_expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    runtime_mcp_registry_state_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("runtime_mcp_registry_state.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="waiting", index=True
    )
    stage: Mapped[str] = mapped_column(
        String(32), nullable=False, default="resolve_registry"
    )
    available_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
