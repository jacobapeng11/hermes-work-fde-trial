from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.mixins import TimestampMixin


class Organization(TimestampMixin, Base):
    __tablename__ = "organization"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)


class User(TimestampMixin, Base):
    __tablename__ = "user"
    __table_args__ = (
        UniqueConstraint("org_id", "user_code", name="uq_user_org_code"),
        UniqueConstraint("org_id", "sso_subject", name="uq_user_org_sso_subject"),
        Index("ix_user_org_qw_token", "org_id", "qw_token"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
        index=True,
    )
    user_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sso_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hrm_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    qw_token: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    login_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_daily_presence_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    organization: Mapped[Organization] = relationship()


class Role(TimestampMixin, Base):
    __tablename__ = "role"
    __table_args__ = (UniqueConstraint("org_id", "code", name="uq_role_org_code"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class UserRole(TimestampMixin, Base):
    __tablename__ = "user_role"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_role"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("user.id"), nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("role.id"), nullable=False)


class AdminWorkNoPermission(TimestampMixin, Base):
    """A manually maintained EnterpriseIdp work-number grant for the admin console."""

    __tablename__ = "admin_work_no_permissions"
    __table_args__ = (
        UniqueConstraint("org_id", "work_no", name="uq_admin_work_no_permissions_org_work_no"),
        Index("ix_admin_work_no_permissions_org_work_no_status", "org_id", "work_no", "status"),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_admin_work_no_permissions_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organization.id"),
        nullable=False,
        index=True,
    )
    work_no: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Permission(TimestampMixin, Base):
    __tablename__ = "permission"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "asset_type",
            "asset_id",
            "grantee_type",
            "grantee_id",
            name="uq_permission_grantee",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    asset_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    grantee_type: Mapped[str] = mapped_column(String(32), nullable=False)
    grantee_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kb_role: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SysUser(Base):
    __tablename__ = "sys_user"

    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dept_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    dept_code: Mapped[str | None] = mapped_column(String(45), nullable=True)
    dept_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    modified_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    position_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    entry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    dimission_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    superior_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    com_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SysDept(Base):
    __tablename__ = "sys_dept"

    dept_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    parent_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    code: Mapped[str | None] = mapped_column(String(45), nullable=True, unique=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(250), nullable=True)
    order_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    dep_grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    director_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    director_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    com_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
