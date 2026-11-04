from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import UniqueConstraint

from app.models.base import Base
from app.models.mixins import TimestampMixin


class ModelProvider(TimestampMixin, Base):
    __tablename__ = "model_provider"
    __table_args__ = (UniqueConstraint("org_id", "provider_key", name="uq_provider_org_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    provider_key: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False, default="openai_compatible")
    api_endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key_encrypted: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    chat_model: Mapped[str] = mapped_column(String(100), nullable=False, default="OpenAIChatModel")
    generate_kwargs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    # The version of the last atomically published organization catalog that
    # included this provider.  Zero is the legacy/unpublished baseline.
    catalog_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)


class ModelEntry(TimestampMixin, Base):
    __tablename__ = "model_entry"
    __table_args__ = (
        UniqueConstraint("provider_id", "model_key", name="uq_model_entry_provider_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    provider_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("model_provider.id"),
        nullable=False,
        index=True,
    )
    model_key: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    tags: Mapped[str | None] = mapped_column(String(500), nullable=True)
    context_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Runtime-only wire requirements.  Null means the backwards-compatible
    # default and is normalized at the signed-grant boundary.
    runtime_capabilities: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    # See ModelProvider.catalog_version.  User reads must not expose entries
    # from an uncommitted/newer configuration once a published catalog exists.
    catalog_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)


class ModelUpgradeLog(Base):
    __tablename__ = "model_upgrade_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    operator_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_model_entry_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    target_model_entry_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    scope_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    affected_user_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    affected_runtime_workspace_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    partial_errors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    catalog_version: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    previous_catalog_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
