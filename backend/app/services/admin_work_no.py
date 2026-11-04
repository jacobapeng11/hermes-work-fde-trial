from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminWorkNoPermission, User
from app.services.admin_auth import AdminPrincipal
from app.services.runtime_workspace import Principal


class AdminWorkNoAuthorizationError(PermissionError):
    pass


class AdminWorkNoStore(Protocol):
    async def is_active_work_no(self, *, org_id: int, work_no: str) -> bool:
        ...

    async def work_no_for_user(self, *, org_id: int, user_id: int) -> str | None:
        ...


class AdminWorkNoAuthorizationService:
    def __init__(self, store: AdminWorkNoStore) -> None:
        self.store = store

    async def is_admin_work_no(self, *, org_id: int, work_no: str | None) -> bool:
        normalized = (work_no or "").strip()
        return bool(normalized) and await self.store.is_active_work_no(
            org_id=org_id,
            work_no=normalized,
        )

    async def is_admin(self, principal: Principal) -> bool:
        work_no = await self.store.work_no_for_user(
            org_id=principal.org_id,
            user_id=principal.user_id,
        )
        return await self.is_admin_work_no(org_id=principal.org_id, work_no=work_no)

    async def require_admin(self, principal: Principal) -> AdminPrincipal:
        if not await self.is_admin(principal):
            raise AdminWorkNoAuthorizationError("admin work number permission is required")
        return AdminPrincipal(
            org_id=principal.org_id,
            user_id=principal.user_id,
            roles=("admin",),
        )


class SqlAlchemyAdminWorkNoStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def is_active_work_no(self, *, org_id: int, work_no: str) -> bool:
        try:
            result = await self.db.execute(
                select(AdminWorkNoPermission.id).where(
                    AdminWorkNoPermission.org_id == org_id,
                    AdminWorkNoPermission.work_no == work_no,
                    AdminWorkNoPermission.status == "active",
                )
            )
        except SQLAlchemyError:
            # Existing deployments may not yet have the manually maintained
            # permission table. Fail closed for the admin boundary while
            # keeping ordinary user traffic available; this code never tries
            # to create or initialize database objects.
            return False
        return result.scalar_one_or_none() is not None

    async def work_no_for_user(self, *, org_id: int, user_id: int) -> str | None:
        result = await self.db.execute(
            select(User.user_code).where(
                User.org_id == org_id,
                User.id == user_id,
                User.status == "active",
            )
        )
        value = result.scalar_one_or_none()
        return str(value).strip() if value else None


class InMemoryAdminWorkNoStore:
    def __init__(
        self,
        active_work_nos: set[tuple[int, str]] | None = None,
        user_work_nos: dict[tuple[int, int], str] | None = None,
    ) -> None:
        self.active_work_nos = active_work_nos or set()
        self.user_work_nos = user_work_nos or {}

    async def is_active_work_no(self, *, org_id: int, work_no: str) -> bool:
        return (org_id, work_no) in self.active_work_nos

    async def work_no_for_user(self, *, org_id: int, user_id: int) -> str | None:
        return self.user_work_nos.get((org_id, user_id))
