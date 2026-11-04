from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
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


BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


class RuntimeMaintenanceControl(TimestampMixin, Base):
    """Singleton authority for global Runtime admission maintenance."""

    __tablename__ = "runtime_maintenance_control"
    __table_args__ = (
        CheckConstraint(
            "maintenance_state IN ('open', 'frozen')",
            name="ck_runtime_maintenance_control_state",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    maintenance_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open"
    )
    maintenance_epoch: Mapped[str | None] = mapped_column(String(64), nullable=True)
    maintenance_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    admission_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1
    )
    grant_issuance_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )


class RuntimeBootstrapIntent(TimestampMixin, Base):
    """Idempotent Backend authorization for one fresh Runtime bootstrap."""

    __tablename__ = "runtime_bootstrap_intent"
    __table_args__ = (
        UniqueConstraint("intent_id", name="uq_runtime_bootstrap_intent_id"),
        UniqueConstraint(
            "authorization_digest",
            name="uq_runtime_bootstrap_intent_authorization_digest",
        ),
        Index(
            "ix_runtime_bootstrap_intent_binding",
            "runtime_binding_type",
            "runtime_binding_id",
            "created_at",
        ),
        CheckConstraint(
            "runtime_binding_type IN ('personal_runtime', 'application_runtime')",
            name="ck_runtime_bootstrap_intent_binding_type",
        ),
        CheckConstraint(
            "status IN ('authorized', 'completed', 'failed')",
            name="ck_runtime_bootstrap_intent_status",
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    intent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    runtime_binding_type: Mapped[str] = mapped_column(String(24), nullable=False)
    runtime_binding_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    runtime_id: Mapped[str] = mapped_column(String(128), nullable=False)
    pvc_storage_identity_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    target_image: Mapped[str] = mapped_column(String(1000), nullable=False)
    target_image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    authorization_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    authorization_document: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="authorized"
    )
    authorized_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
