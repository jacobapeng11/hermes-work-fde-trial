from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class CloudHermesProfile(TimestampMixin, Base):
    __tablename__ = "cloud_hermes_profile"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", "profile_type", name="uq_cloud_profile_owner_type"),
        Index(
            "ix_cloud_profile_default_chat_agent",
            "org_id",
            "user_id",
            "profile_type",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("organization.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False, index=True)
    profile_type: Mapped[str] = mapped_column(String(64), nullable=False, default="default_chat_agent")
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    default_model_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("model_entry.id"),
        nullable=True,
        index=True,
    )
    profile_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
