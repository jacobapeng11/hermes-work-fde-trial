from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class Skill(TimestampMixin, Base):
    __tablename__ = "skill"
    __table_args__ = (
        UniqueConstraint("org_id", "owner_user_id", "slug", name="uq_skill_owner_slug"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False, index=True
    )
    application_agent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_key: Mapped[str] = mapped_column(
        String(64), nullable=False, default="other", index=True
    )
    asset_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="personal", index=True
    )
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="private", index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    # Market visibility is deliberately independent from runtime execution.
    # Unpublishing must not revoke already installed bindings.
    market_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    current_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SkillVersion(TimestampMixin, Base):
    __tablename__ = "skill_version"
    __table_args__ = (UniqueConstraint("skill_id", "version", name="uq_skill_version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    skill_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("skill.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    package_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    package_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Mcp(TimestampMixin, Base):
    __tablename__ = "mcp"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False, index=True
    )
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="private", index=True
    )
    current_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    transport: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class McpSlugLease(TimestampMixin, Base):
    """The active business-name lease for one MCP namespace.

    Historical MCP rows deliberately keep their original slug.  Only this
    table reserves a slug, so revoking an MCP can release the business name
    without deleting its audit history or immutable versions.
    """

    __tablename__ = "mcp_slug_lease"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "namespace_key",
            "slug",
            name="uniq_mcp_slug_lease_namespace",
        ),
        UniqueConstraint("mcp_id", name="uniq_mcp_slug_lease_asset"),
        Index("ix_mcp_slug_lease_org_asset", "org_id", "mcp_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    namespace_key: Mapped[str] = mapped_column(String(64), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    mcp_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("mcp.id", ondelete="CASCADE"),
        nullable=False,
    )


class McpVersion(TimestampMixin, Base):
    """Immutable MCP connection and discovered-tool contract."""

    __tablename__ = "mcp_version"
    __table_args__ = (
        UniqueConstraint("mcp_id", "version", name="uq_mcp_version"),
        UniqueConstraint("mcp_id", "config_digest", name="uq_mcp_version_digest"),
        Index("ix_mcp_version_mcp_status", "mcp_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    mcp_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("mcp.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    transport: Mapped[str] = mapped_column(String(64), nullable=False)
    connection_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    secret_refs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    config_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    tools_schema: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tools_schema_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class KnowledgeBase(TimestampMixin, Base):
    __tablename__ = "knowledge_base"
    __table_args__ = (UniqueConstraint("org_id", "owner_user_id", "slug", name="uq_kb_owner_slug"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False, index=True
    )
    application_agent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("application_agent.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="personal")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="filebase")
    dataset_id: Mapped[str | None] = mapped_column(
        "ragflow_id",
        String(255),
        nullable=True,
        index=True,
    )
    provider_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_capabilities: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    provider_capabilities_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="private")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)


class KnowledgeBaseFile(TimestampMixin, Base):
    __tablename__ = "knowledge_base_file"
    __table_args__ = (
        Index("ix_kb_file_org_kb_deleted", "org_id", "kb_id", "is_deleted"),
        Index("ix_kb_file_kb_status", "kb_id", "file_status"),
        Index("uq_kb_file_provider_document", "provider_document_id", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
        index=True,
    )
    knowledge_base_id: Mapped[int] = mapped_column(
        "kb_id",
        BigInteger,
        ForeignKey("knowledge_base.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    oss_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    oss_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    provider_document_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    provider_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploader_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    updater_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    is_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SkillCategory(TimestampMixin, Base):
    __tablename__ = "skill_category"
    __table_args__ = (
        UniqueConstraint("org_id", "category_key", name="uq_skill_category_org_key"),
        Index("ix_skill_category_org_market_visible", "org_id", "is_market_visible"),
        Index("ix_skill_category_org_personal_visible", "org_id", "is_personal_visible"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    category_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_market_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_personal_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PersonalSkillCategory(TimestampMixin, Base):
    __tablename__ = "personal_skill_category"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "owner_user_id",
            "category_key",
            name="uq_personal_skill_category_owner_key",
        ),
        UniqueConstraint(
            "org_id",
            "owner_user_id",
            "normalized_name",
            name="uq_personal_skill_category_owner_name",
        ),
        Index(
            "ix_personal_skill_category_owner_display",
            "org_id",
            "owner_user_id",
            "display_name",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organization.id"), nullable=False, index=True
    )
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False, index=True
    )
    category_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(100), nullable=False)


class UserSkillHubInstallation(TimestampMixin, Base):
    """Private upstream facts linked to the stable Skill catalog."""

    __tablename__ = "user_skillhub_installation"
    __table_args__ = (
        UniqueConstraint("skill_id", name="uq_user_skillhub_installation_skill"),
        UniqueConstraint(
            "runtime_agent_profile_id",
            "namespace",
            "public_slug",
            name="uq_user_skillhub_installation_profile_coordinate",
        ),
        Index(
            "ix_user_skillhub_installation_profile_checked",
            "runtime_agent_profile_id",
            "last_version_checked_at",
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
        BigInteger, ForeignKey("cloud_hermes_profile.id"), nullable=False, index=True
    )
    runtime_agent_profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runtime_agent_profile.id"), nullable=False, index=True
    )
    skill_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("skill.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    installed_skill_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("skill_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="current", index=True
    )
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    public_slug: Mapped[str] = mapped_column(String(200), nullable=False)
    upstream_internal_slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    installed_upstream_version: Mapped[str] = mapped_column(String(128), nullable=False)
    available_upstream_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    upstream_category_snapshot: Mapped[str | None] = mapped_column(String(128), nullable=True)
    upstream_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    upstream_description_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_key_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signature_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_version_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    package_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
