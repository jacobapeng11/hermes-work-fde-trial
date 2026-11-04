from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.mixins import TimestampMixin


class TaskPromptConfig(TimestampMixin, Base):
    """The organization-wide welcome-task prompt configuration."""

    __tablename__ = "task_prompt_config"
    __table_args__ = (
        UniqueConstraint("org_id", name="uq_task_prompt_config_org"),
        Index("ix_task_prompt_config_org_updated", "org_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
    )
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
    )
