from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class ExpertPackage(TimestampMixin, Base):
    __tablename__ = "expert_package"
    __table_args__ = (
        UniqueConstraint("org_id", "owner_user_id", "slug", name="uq_expert_owner_slug"),
        UniqueConstraint(
            "org_id",
            "owner_user_id",
            "origin_runtime_workspace_id",
            "active_name_key",
            name="uq_expert_owner_workspace_active_name",
        ),
        UniqueConstraint(
            "org_id",
            "market_name_key",
            name="uq_expert_market_name_lease",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False, index=True)
    # Stable creation scope.  Display names are only unique within this
    # owner/workspace tuple, never across the market.
    origin_runtime_workspace_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id"),
        nullable=True,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="private")
    # A normalized active name.  It is cleared on withdrawal/deletion so the
    # name can be reused without changing the immutable package identity.
    active_name_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # A published market name remains leased even when the package is
    # unlisted/deleted while active installations can still reference it.
    market_name_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Catalog lifecycle and emergency execution denial are orthogonal facts.
    execution_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="enabled", index=True
    )
    execution_status_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    execution_status_reason: Mapped[str | None] = mapped_column(
        String(1000), nullable=True
    )
    current_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)


class ExpertPackageVersion(TimestampMixin, Base):
    __tablename__ = "expert_package_version"
    __table_args__ = (
        UniqueConstraint("expert_package_id", "version", name="uq_expert_package_version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    expert_package_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("expert_package.id"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    basic_info_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    soul_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    mcp_refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    knowledge_refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    plugin_refs: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Kept only for reading legacy package rows.  New expert APIs never accept
    # or publish model preferences; model selection remains Backend-governed.
    model_preferences: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)


class ExpertVersionArtifact(TimestampMixin, Base):
    """Immutable release material used to project a market expert locally."""

    __tablename__ = "expert_version_artifact"
    __table_args__ = (
        Index("ix_expert_version_artifact_version_type", "expert_package_version_id", "artifact_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    expert_package_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("expert_package_version.id"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_asset_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Must contain only secret references for MCP artifacts, never secret values.
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    storage_uri: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ExpertVersionKnowledgeSnapshot(TimestampMixin, Base):
    """Legacy release table retained for database compatibility.

    New expert versions use ``knowledge_reference`` artifacts and delegate
    access control to the knowledge-base module instead of cloning datasets.
    """

    __tablename__ = "expert_version_knowledge_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "expert_package_version_id",
            "source_knowledge_base_id",
            name="uq_expert_version_knowledge_source",
        ),
        Index(
            "ix_expert_version_knowledge_snapshot_kb_status",
            "snapshot_knowledge_base_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    expert_package_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("expert_package_version.id"),
        nullable=False,
        index=True,
    )
    source_knowledge_base_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("knowledge_base.id"),
        nullable=False,
        index=True,
    )
    artifact_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("expert_version_artifact.id"),
        nullable=True,
        index=True,
    )
    snapshot_knowledge_base_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("knowledge_base.id"),
        nullable=True,
        index=True,
    )
    index_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="preparing", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class InstalledExpertBinding(TimestampMixin, Base):
    __tablename__ = "installed_expert_binding"
    __table_args__ = (
        UniqueConstraint(
            "runtime_workspace_id",
            "expert_package_id",
            name="uq_installed_expert_workspace_package",
        ),
        Index("ix_installed_expert_workspace_status", "runtime_workspace_id", "status"),
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
    expert_package_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("expert_package.id"),
        nullable=False,
        index=True,
    )
    expert_package_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("expert_package_version.id"),
        nullable=False,
        index=True,
    )
    runtime_agent_profile_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("runtime_agent_profile.id"),
        nullable=False,
        index=True,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="personal")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="enabled", index=True)
    installation_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    projection_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
