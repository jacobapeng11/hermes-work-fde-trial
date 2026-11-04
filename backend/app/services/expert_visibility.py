from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExpertPackage, Permission, SysDept, SysUser, User
from app.services.runtime_workspace import Principal


class ExpertVisibilityPolicy:
    """Single read-only policy for expert catalog, detail, and first install."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def department_ancestor_ids(self, principal: Principal) -> frozenset[str]:
        user_code = await self.db.scalar(
            select(User.user_code).where(
                User.id == principal.user_id,
                User.org_id == principal.org_id,
                User.status == "active",
            )
        )
        if not user_code:
            return frozenset()
        directory_user = await self.db.execute(
            select(SysUser.dept_id, SysUser.com_id).where(SysUser.code == str(user_code))
        )
        directory_row = directory_user.first()
        current = str(directory_row.dept_id or "") if directory_row else ""
        company_root = str(directory_row.com_id or "") if directory_row else ""
        ancestors: set[str] = set()
        if company_root:
            ancestors.add(company_root)
        while current and current not in ancestors:
            ancestors.add(current)
            current = str(
                await self.db.scalar(
                    select(SysDept.parent_id).where(
                        SysDept.dept_id == current,
                        SysDept.status == 1,
                    )
                )
                or ""
            )
        return frozenset(ancestors)

    async def visible_package_ids(
        self,
        *,
        principal: Principal,
        package_ids: set[int],
    ) -> frozenset[int]:
        if not package_ids:
            return frozenset()
        ancestors = await self.department_ancestor_ids(principal)
        grantees = [
            (Permission.grantee_type == "all")
            & (Permission.grantee_id == "*"),
            (Permission.grantee_type == "user")
            & (Permission.grantee_id == str(principal.user_id)),
        ]
        if ancestors:
            grantees.append(
                (Permission.grantee_type == "department")
                & (Permission.grantee_id.in_(ancestors))
            )
        permitted = set(
            await self.db.scalars(
                select(Permission.asset_id).where(
                    Permission.org_id == principal.org_id,
                    Permission.asset_type == "expert_package",
                    Permission.asset_id.in_(package_ids),
                    Permission.kb_role.is_(None),
                    or_(*grantees),
                )
            )
        )
        owned = set(
            await self.db.scalars(
                select(ExpertPackage.id).where(
                    ExpertPackage.org_id == principal.org_id,
                    ExpertPackage.id.in_(package_ids),
                    ExpertPackage.owner_user_id == principal.user_id,
                )
            )
        )
        return frozenset(int(item) for item in permitted | owned)

    async def can_view_package(
        self,
        *,
        principal: Principal,
        expert_package_id: int,
    ) -> bool:
        return expert_package_id in await self.visible_package_ids(
            principal=principal,
            package_ids={expert_package_id},
        )


__all__ = ["ExpertVisibilityPolicy"]
