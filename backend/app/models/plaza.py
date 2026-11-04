from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class PlazaTemplate(TimestampMixin, Base):
    __tablename__ = "plaza_template"
    __table_args__ = (UniqueConstraint("org_id", "slug", name="uq_plaza_template_org_slug"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="private")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    current_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class TemplateVersion(TimestampMixin, Base):
    __tablename__ = "template_version"
    __table_args__ = (UniqueConstraint("template_id", "version", name="uq_template_version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    template_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("plaza_template.id"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_expert_package_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("expert_package.id"),
        nullable=True,
        index=True,
    )
    source_expert_package_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("expert_package_version.id"),
        nullable=True,
        index=True,
    )
    config_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    skill_snapshots: Mapped[list | None] = mapped_column(JSON, nullable=True)
    mcp_snapshots: Mapped[list | None] = mapped_column(JSON, nullable=True)
    knowledge_snapshots: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="published", index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TemplateReview(TimestampMixin, Base):
    __tablename__ = "template_review"
    __table_args__ = (
        Index("ix_template_review_org_status_created_at", "org_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    submitter_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    expert_package_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("expert_package.id"),
        nullable=False,
        index=True,
    )
    expert_package_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("expert_package_version.id"),
        nullable=True,
        index=True,
    )
    template_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("plaza_template.id"),
        nullable=True,
        index=True,
    )
    review_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    reviewer_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
