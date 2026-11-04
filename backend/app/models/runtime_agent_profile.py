from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    ForeignKey,
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


class RuntimeAgentProfile(TimestampMixin, Base):
    """One Hermes execution profile inside a user's Runtime Workspace."""

    __tablename__ = "runtime_agent_profile"
    __table_args__ = (
        UniqueConstraint(
            "runtime_workspace_id",
            "hermes_profile_name",
            name="uq_runtime_agent_profile_workspace_name",
        ),
        UniqueConstraint(
            "default_workspace_key",
            name="uq_runtime_agent_profile_default_workspace",
        ),
        UniqueConstraint(
            "runtime_workspace_id",
            "active_display_name_key",
            name="uq_runtime_agent_profile_workspace_active_display_name",
        ),
        Index(
            "ix_runtime_agent_profile_owner_status",
            "org_id",
            "user_id",
            "status",
        ),
        Index(
            "ix_runtime_agent_profile_workspace_kind",
            "runtime_workspace_id",
            "profile_kind",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("user.id"),
        nullable=False,
        index=True,
    )
    runtime_workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cloud_hermes_profile.id"),
        nullable=False,
        index=True,
    )
    hermes_profile_name: Mapped[str] = mapped_column(String(128), nullable=False)
    profile_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="expert", index=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Only active expert Profiles populate this normalized key. MySQL permits
    # multiple NULL values, so disabled/deleted and the default Profile do not
    # hold a runnable-name lease.
    active_display_name_key: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ownership_type: Mapped[str] = mapped_column(String(32), nullable=False, default="self_created")
    inherited_from_profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("runtime_agent_profile.id"),
        nullable=True,
        index=True,
    )
    expert_package_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("expert_package.id"),
        nullable=True,
        index=True,
    )
    # MySQL allows multiple NULL values in a unique constraint.  Setting this
    # only for the default row gives each Workspace exactly one canonical
    # default without preventing any number of expert rows.
    default_workspace_key: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    config_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    applied_config_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    config_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    desired_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    applied_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    desired_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    applied_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    effective_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    capability_health: Mapped[list | None] = mapped_column(JSON, nullable=True)
    soul_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    capability_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Business lifecycle only. Projection/convergence health lives in the
    # revision/job/attempt records and must never be written into this field.
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
