from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGBLOB, LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class AppCenterApp(TimestampMixin, Base):
    """A user-owned static application served from the platform package blob."""

    __tablename__ = "app_center_app"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "creator_user_id",
            "idempotency_key",
            name="uq_app_center_idempotency",
        ),
        Index("ix_app_center_org_creator", "org_id", "creator_user_id"),
        Index("ix_app_center_org_updated", "org_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(64), nullable=False, default="HTML 应用")
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    share_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    html_content: Mapped[str | None] = mapped_column(
        Text().with_variant(LONGTEXT, "mysql"),
        nullable=True,
    )
    package_blob: Mapped[bytes] = mapped_column(
        LargeBinary().with_variant(LONGBLOB, "mysql"),
        nullable=False,
    )
    access_url: Mapped[str] = mapped_column(String(512), nullable=False)
    demo_id: Mapped[str] = mapped_column(String(32), nullable=False)
    demo_code: Mapped[str] = mapped_column(String(64), nullable=False)
    publish_version_id: Mapped[str] = mapped_column(String(32), nullable=False)
    business_domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    creator_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
    )
    copied_from_app_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    copied_from_creator_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    source_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
