from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Role, UserRole
from app.services.runtime_workspace import Principal


class AdminAuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class AdminPrincipal:
    org_id: int
    user_id: int
    roles: tuple[str, ...]


class AdminRoleStore(Protocol):
    async def role_codes_for_user(self, *, org_id: int, user_id: int) -> set[str]:
        ...


class AdminAuthorizationService:
    ADMIN_ROLES = {"admin", "administrator", "platform_admin"}

    def __init__(self, store: AdminRoleStore) -> None:
        self.store = store

    async def require_admin(self, principal: Principal) -> AdminPrincipal:
        roles = await self.store.role_codes_for_user(org_id=principal.org_id, user_id=principal.user_id)
        if not self.ADMIN_ROLES.intersection(role.lower() for role in roles):
            raise AdminAuthorizationError("admin role is required")
        return AdminPrincipal(
            org_id=principal.org_id,
            user_id=principal.user_id,
            roles=tuple(sorted(roles)),
        )


class InMemoryAdminRoleStore:
    def __init__(self, admin_users: set[tuple[int, int]] | None = None) -> None:
        self.admin_users = admin_users or {
            (settings.DEFAULT_ORG_ID, settings.DEV_PRINCIPAL_USER_ID)
        }

    async def role_codes_for_user(self, *, org_id: int, user_id: int) -> set[str]:
        if (org_id, user_id) in self.admin_users:
            return {"admin"}
        return set()


class SqlAlchemyAdminRoleStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def role_codes_for_user(self, *, org_id: int, user_id: int) -> set[str]:
        result = await self.db.execute(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(
                Role.org_id == org_id,
                Role.status == "active",
                UserRole.org_id == org_id,
                UserRole.user_id == user_id,
            )
        )
        return {str(row[0]) for row in result.all()}
