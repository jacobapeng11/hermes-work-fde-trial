from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import parse_initial_admin_work_nos, settings
from app.database import AsyncSessionLocal
from app.models import (
    AdminWorkNoPermission,
    ModelEntry,
    ModelProvider,
    Organization,
    Permission,
    Role,
    User,
    UserRole,
)
from app.services.auth import hash_password, is_password_hash_usable

DEFAULT_ORG_CODE = "default"
DEFAULT_ROLES = (
    ("admin", "Administrator"),
    ("user", "User"),
    ("application_agent_admin", "Application Agent Administrator"),
)
DEFAULT_PERMISSION_ASSETS = (
    "user",
    "organization",
    "model_entry",
    "skill",
    "mcp",
    "knowledge_base",
    "expert_package",
    "template_review",
    "runtime_workspace",
    "audit_log",
    "usage",
)


@dataclass(frozen=True)
class SeedSummary:
    organization_id: int
    roles: int
    permissions: int
    initial_admin_created: bool
    default_model_created: bool


async def seed_defaults(db: AsyncSession) -> SeedSummary:
    organization = await _get_or_create_organization(db)
    roles = await _get_or_create_roles(db, organization.id)
    await _assert_identity_conflicts(db, organization.id)
    permissions = await _get_or_create_permissions(db, organization.id)
    await _ensure_initial_admin_work_no_permissions(db, organization.id)
    await _ensure_default_user_roles(db, organization.id, roles["user"].id)
    admin_created = await _ensure_password_mode_admin(
        db,
        organization.id,
        roles["admin"].id,
    )
    model_created = await _maybe_create_default_model(db, organization.id)
    await db.commit()
    return SeedSummary(
        organization_id=int(organization.id),
        roles=len(roles),
        permissions=permissions,
        initial_admin_created=admin_created,
        default_model_created=model_created,
    )


async def _assert_identity_conflicts(db: AsyncSession, org_id: int) -> None:
    for column_name, column in (("user_code", User.user_code), ("sso_subject", User.sso_subject)):
        result = await db.execute(
            select(column, func.count(User.id))
            .where(User.org_id == org_id, column.is_not(None))
            .group_by(column)
            .having(func.count(User.id) > 1)
        )
        if result.first() is not None:
            raise RuntimeError(f"duplicate active identity field detected: {column_name}")


async def _ensure_default_user_roles(db: AsyncSession, org_id: int, user_role_id: int) -> int:
    result = await db.execute(
        select(User.id)
        .outerjoin(UserRole, (UserRole.user_id == User.id) & (UserRole.org_id == org_id))
        .where(User.org_id == org_id, User.status == "active", UserRole.id.is_(None))
    )
    user_ids = [int(row[0]) for row in result.all()]
    for user_id in user_ids:
        db.add(UserRole(org_id=org_id, user_id=user_id, role_id=user_role_id))
    return len(user_ids)


async def _get_or_create_organization(db: AsyncSession) -> Organization:
    result = await db.execute(select(Organization).where(Organization.code == DEFAULT_ORG_CODE))
    organization = result.scalar_one_or_none()
    if organization is not None:
        return organization

    organization = Organization(
        id=settings.DEFAULT_ORG_ID,
        code=DEFAULT_ORG_CODE,
        name="Default Organization",
        status="active",
    )
    db.add(organization)
    await db.flush()
    return organization


async def _get_or_create_roles(db: AsyncSession, org_id: int) -> dict[str, Role]:
    roles: dict[str, Role] = {}
    for code, name in DEFAULT_ROLES:
        result = await db.execute(select(Role).where(Role.org_id == org_id, Role.code == code))
        role = result.scalar_one_or_none()
        if role is None:
            role = Role(org_id=org_id, code=code, name=name, status="active")
            db.add(role)
            await db.flush()
        roles[code] = role
    return roles


async def _get_or_create_permissions(db: AsyncSession, org_id: int) -> int:
    count = 0
    for asset_type in DEFAULT_PERMISSION_ASSETS:
        result = await db.execute(
            select(Permission).where(
                Permission.org_id == org_id,
                Permission.asset_type == asset_type,
                Permission.asset_id == 0,
                Permission.grantee_type == "all",
                Permission.grantee_id == "*",
            )
        )
        permission = result.scalar_one_or_none()
        if permission is None:
            db.add(
                Permission(
                    org_id=org_id,
                    asset_type=asset_type,
                    asset_id=0,
                    grantee_type="all",
                    grantee_id="*",
                )
            )
            count += 1
    return count


async def _ensure_initial_admin_work_no_permissions(db: AsyncSession, org_id: int) -> int:
    """Seed only missing grants; operators retain all later database edits."""
    created = 0
    for work_no in parse_initial_admin_work_nos(settings.ADMIN_INITIAL_WORK_NOS):
        result = await db.execute(
            select(AdminWorkNoPermission).where(
                AdminWorkNoPermission.org_id == org_id,
                AdminWorkNoPermission.work_no == work_no,
            )
        )
        if result.scalar_one_or_none() is None:
            db.add(
                AdminWorkNoPermission(
                    org_id=org_id,
                    work_no=work_no,
                    status="active",
                    remark="客户初始化管理员授权",
                )
            )
            created += 1
    return created


async def _ensure_password_mode_admin(
    db: AsyncSession,
    org_id: int,
    admin_role_id: int,
) -> bool:
    if settings.HERMES_ADMIN_AUTH_MODE != "password":
        return False

    result = await db.execute(
        select(User).where(User.org_id == org_id, User.user_code == "admin")
    )
    user = result.scalar_one_or_none()
    if user is None:
        password = settings.ADMIN_INITIAL_PASSWORD
        if not password:
            raise RuntimeError(
                "password admin initialization requires ADMIN_INITIAL_PASSWORD"
            )
        user = User(
            org_id=org_id,
            user_code="admin",
            name="Administrator",
            status="active",
            password_hash=hash_password(password),
        )
        db.add(user)
        await db.flush()
        created = True
    else:
        if not is_password_hash_usable(user.password_hash):
            raise RuntimeError(
                "password admin initialization found an unusable password hash for user_code=admin"
            )
        created = False

    result = await db.execute(
        select(UserRole).where(UserRole.org_id == org_id, UserRole.user_id == user.id, UserRole.role_id == admin_role_id)
    )
    if result.scalar_one_or_none() is None:
        db.add(UserRole(org_id=org_id, user_id=user.id, role_id=admin_role_id))
    return created


async def _maybe_create_default_model(db: AsyncSession, org_id: int) -> bool:
    provider_key = os.getenv("SEED_MODEL_PROVIDER_KEY", "").strip()
    provider_name = os.getenv("SEED_MODEL_PROVIDER_NAME", provider_key).strip()
    model_key = os.getenv("SEED_MODEL_KEY", "").strip()
    api_endpoint = os.getenv("SEED_MODEL_API_ENDPOINT", "").strip()
    api_key_encrypted = os.getenv("SEED_MODEL_API_KEY_ENCRYPTED", "").strip()
    if not provider_key or not model_key:
        return False

    result = await db.execute(
        select(ModelProvider).where(
            ModelProvider.org_id == org_id,
            ModelProvider.provider_key == provider_key,
        )
    )
    provider = result.scalar_one_or_none()
    if provider is None:
        provider = ModelProvider(
            org_id=org_id,
            provider_key=provider_key,
            display_name=provider_name or provider_key,
            api_endpoint=api_endpoint or None,
            api_key_encrypted=api_key_encrypted or None,
            status="active",
        )
        db.add(provider)
        await db.flush()

    result = await db.execute(
        select(ModelEntry).where(
            ModelEntry.provider_id == provider.id,
            ModelEntry.model_key == model_key,
        )
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        db.add(
            ModelEntry(
                org_id=org_id,
                provider_id=provider.id,
                model_key=model_key,
                display_name=os.getenv("SEED_MODEL_DISPLAY_NAME", model_key),
                is_default=True,
                is_restricted=False,
                status="active",
            )
        )
    return True


async def main() -> None:
    async with AsyncSessionLocal() as db:
        summary = await seed_defaults(db)
        print(_format_summary(summary))


def _format_summary(summary: SeedSummary) -> dict[str, Any]:
    return {
        "organization_id": summary.organization_id,
        "roles": summary.roles,
        "permissions_created": summary.permissions,
        "initial_admin_created": summary.initial_admin_created,
        "default_model_created": summary.default_model_created,
    }


if __name__ == "__main__":
    asyncio.run(main())
