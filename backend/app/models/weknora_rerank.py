from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class WeKnoraRerankConfig(TimestampMixin, Base):
    """Hermes-side request policy for the configured WeKnora workspace."""

    __tablename__ = "weknora_rerank_config"
    __table_args__ = (
        UniqueConstraint("provider", name="uq_weknora_rerank_config_provider"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="weknora")
    rerank_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
    )
