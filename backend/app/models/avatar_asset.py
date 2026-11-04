from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class AvatarAsset(TimestampMixin, Base):
    """A platform-persisted avatar that can be reused by profile surfaces."""

    __tablename__ = "avatar_asset"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_avatar_asset_public_id"),
        UniqueConstraint("avatar_url", name="uq_avatar_asset_url"),
        Index(
            "ix_avatar_asset_owner_status",
            "org_id",
            "owner_user_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=True,
        index=True,
    )
    source_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="user_upload",
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        index=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
