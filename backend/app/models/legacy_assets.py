"""Migration-only mappings for the retired asset schema.

These mappings deliberately use separate metadata.  Importing the one-time
migration must never make application ``Base.metadata.create_all`` recreate
retired tables on a fresh database.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class LegacyAssetBase(DeclarativeBase):
    pass


class LegacyTimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class LegacyProfileAssetDraft(LegacyTimestampMixin, LegacyAssetBase):
    __tablename__ = "profile_asset_draft"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    profile_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    asset_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)


class LegacyPublicAsset(LegacyTimestampMixin, LegacyAssetBase):
    __tablename__ = "public_asset"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    publisher_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False)


class LegacyPublicSkillAsset(LegacyTimestampMixin, LegacyAssetBase):
    __tablename__ = "public_skill_asset"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)
    active_name_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_source: Mapped[str] = mapped_column(String(32), default="admin_upload")
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class LegacyPublicSkillVersion(LegacyTimestampMixin, LegacyAssetBase):
    __tablename__ = "public_skill_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    public_skill_asset_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_version: Mapped[str] = mapped_column(String(32), nullable=False)
    original_package_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    deployment_package_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    original_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skill_metadata: Mapped[dict] = mapped_column(JSON, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), default="admin_upload")
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LegacyUserRuntimeSkill(LegacyTimestampMixin, LegacyAssetBase):
    __tablename__ = "user_runtime_skill"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_workspace_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_agent_profile_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    public_skill_asset_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    installed_public_skill_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    available_public_skill_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    update_status: Mapped[str] = mapped_column(String(32), nullable=False)
    version_label: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    personal_name_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    description_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_key_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_category_key: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_directory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    runtime_skill_name: Mapped[str] = mapped_column(String(200), nullable=False)
    installed_package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    installed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class LegacyRuntimeSkillBinding(LegacyTimestampMixin, LegacyAssetBase):
    __tablename__ = "runtime_skill_binding"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_workspace_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_agent_profile_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    skill_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    skill_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    projection_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class LegacyPublicMcpAsset(LegacyTimestampMixin, LegacyAssetBase):
    __tablename__ = "public_mcp_asset"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    active_slug_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    permission_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class LegacyUserRuntimeMcp(LegacyTimestampMixin, LegacyAssetBase):
    __tablename__ = "user_runtime_mcp"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_workspace_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_agent_profile_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    public_mcp_asset_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    installed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class LegacyRuntimeMcpBinding(LegacyTimestampMixin, LegacyAssetBase):
    __tablename__ = "runtime_mcp_binding"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_workspace_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_agent_profile_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mcp_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    projection_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class LegacyRuntimeKbBinding(LegacyTimestampMixin, LegacyAssetBase):
    __tablename__ = "runtime_kb_binding"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_workspace_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_agent_profile_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    knowledge_base_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    projection_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
