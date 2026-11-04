from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class RuntimePlatformControlKey(TimestampMixin, Base):
    """Encrypted, per-Binding Supervisor control root key version."""

    __tablename__ = "runtime_platform_control_key"
    __table_args__ = (
        UniqueConstraint(
            "binding_type",
            "binding_id",
            "key_version",
            name="uq_runtime_platform_control_key_version",
        ),
        CheckConstraint(
            "binding_type IN ('personal_runtime', 'application_runtime')",
            name="ck_runtime_platform_control_key_binding_type",
        ),
        CheckConstraint(
            "status IN ('active', 'retired')",
            name="ck_runtime_platform_control_key_status",
        ),
        CheckConstraint(
            "key_version > 0",
            name="ck_runtime_platform_control_key_positive_version",
        ),
        Index(
            "ix_runtime_platform_control_key_active",
            "binding_type",
            "binding_id",
            "status",
        ),
        Index(
            "ix_runtime_platform_control_key_runtime",
            "runtime_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    binding_type: Mapped[str] = mapped_column(String(24), nullable=False)
    binding_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_id: Mapped[str] = mapped_column(String(128), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    root_key_encrypted: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
