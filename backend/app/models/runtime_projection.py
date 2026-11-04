from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class RuntimeProjectionReceipt(TimestampMixin, Base):
    """Idempotent control-plane receipt for one Runtime Outbox record."""

    __tablename__ = "runtime_projection_receipt"
    __table_args__ = (
        UniqueConstraint(
            "projection_id",
            name="uq_runtime_projection_receipt_projection",
        ),
        UniqueConstraint(
            "runtime_id",
            "run_id",
            "seq",
            name="uq_runtime_projection_receipt_run_seq",
        ),
        Index(
            "ix_runtime_projection_receipt_runtime_occurred",
            "runtime_id",
            "occurred_at",
        ),
        Index(
            "ix_runtime_projection_receipt_type_created",
            "projection_type",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    projection_id: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_id: Mapped[str] = mapped_column(String(128), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(128), nullable=False)
    projection_type: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    config_revision: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
