from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeBase, Permission, SysDept, SysUser, User
from app.services.runtime_workspace import Principal


EffectiveRole = Literal["owner", "download", "preview", "retrieve"]
RequiredCapability = Literal["retrieve", "preview", "download", "manage"]

ROLE_RANK: dict[str, int] = {
    "retrieve": 1,
    "preview": 2,
    "download": 3,
    "owner": 4,
}
CAPABILITY_RANK: dict[str, int] = {
    "retrieve": 1,
    "preview": 2,
    "download": 3,
    "manage": 4,
}


class PersonalKnowledgeAclError(RuntimeError):
    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def highest_role(roles: list[str]) -> EffectiveRole | None:
    allowed = [role for role in roles if role in ROLE_RANK]
    if not allowed:
        return None
    return max(allowed, key=ROLE_RANK.__getitem__)  # type: ignore[return-value]


def role_allows(role: str | None, capability: str) -> bool:
    return (
        role in ROLE_RANK
        and capability in CAPABILITY_RANK
        and ROLE_RANK[role] >= CAPABILITY_RANK[capability]
    )


class PersonalKnowledgeAclService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_knowledge_base(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
    ) -> KnowledgeBase:
        knowledge_base = await self.session.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.org_id == principal.org_id,
                KnowledgeBase.kind == "personal",
            )
        )
        if knowledge_base is None:
            raise PersonalKnowledgeAclError(
                "personal_knowledge_not_found",
                status_code=404,
            )
        return knowledge_base

    async def _department_ids(self, principal: Principal) -> set[str]:
        user = await self.session.scalar(
            select(User).where(
                User.id == principal.user_id,
                User.org_id == principal.org_id,
                User.status == "active",
            )
        )
        if user is None or not user.user_code:
            return set()
        directory_user = await self.session.scalar(
            select(SysUser).where(SysUser.code == user.user_code)
        )
        current = str(directory_user.dept_id or "") if directory_user else ""
        department_ids: set[str] = set()
        if directory_user is not None and directory_user.com_id:
            department_ids.add(str(directory_user.com_id))
        while current and current not in department_ids:
            department_ids.add(current)
            parent_id = await self.session.scalar(
                select(SysDept.parent_id).where(SysDept.dept_id == current)
            )
            current = str(parent_id or "")
        return department_ids

    async def effective_role(
        self,
        *,
        principal: Principal,
        knowledge_base: KnowledgeBase,
    ) -> EffectiveRole | None:
        if knowledge_base.org_id != principal.org_id:
            return None
        if knowledge_base.owner_user_id == principal.user_id:
            return "owner"
        department_ids = await self._department_ids(principal)
        target_filters = [
            (Permission.grantee_type == "user")
            & (Permission.grantee_id == str(principal.user_id)),
            (Permission.grantee_type == "all")
            & (Permission.grantee_id == "*"),
        ]
        if department_ids:
            target_filters.append(
                (Permission.grantee_type == "department")
                & (Permission.grantee_id.in_(department_ids))
            )
        roles = (
            await self.session.execute(
                select(Permission.kb_role).where(
                    Permission.org_id == principal.org_id,
                    Permission.asset_type == "knowledge_base",
                    Permission.asset_id == knowledge_base.id,
                    or_(*target_filters),
                )
            )
        ).scalars()
        return highest_role([str(role) for role in roles if role])

    async def effective_roles(
        self,
        *,
        principal: Principal,
        knowledge_bases: Sequence[KnowledgeBase],
    ) -> dict[int, EffectiveRole]:
        roles_by_knowledge_base_id: dict[int, EffectiveRole] = {}
        candidate_ids: set[int] = set()
        for knowledge_base in knowledge_bases:
            if knowledge_base.org_id != principal.org_id:
                continue
            knowledge_base_id = int(knowledge_base.id)
            if knowledge_base.owner_user_id == principal.user_id:
                roles_by_knowledge_base_id[knowledge_base_id] = "owner"
            else:
                candidate_ids.add(knowledge_base_id)

        if not candidate_ids:
            return roles_by_knowledge_base_id

        department_ids = await self._department_ids(principal)
        target_filters = [
            (Permission.grantee_type == "user")
            & (Permission.grantee_id == str(principal.user_id)),
            (Permission.grantee_type == "all")
            & (Permission.grantee_id == "*"),
        ]
        if department_ids:
            target_filters.append(
                (Permission.grantee_type == "department")
                & (Permission.grantee_id.in_(department_ids))
            )
        permission_rows = (
            await self.session.execute(
                select(Permission.asset_id, Permission.kb_role).where(
                    Permission.org_id == principal.org_id,
                    Permission.asset_type == "knowledge_base",
                    Permission.asset_id.in_(candidate_ids),
                    or_(*target_filters),
                )
            )
        ).all()
        granted_roles: dict[int, list[str]] = defaultdict(list)
        for asset_id, role in permission_rows:
            if role:
                granted_roles[int(asset_id)].append(str(role))
        for knowledge_base_id, roles in granted_roles.items():
            role = highest_role(roles)
            if role is not None:
                roles_by_knowledge_base_id[knowledge_base_id] = role
        return roles_by_knowledge_base_id

    async def accessible(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
        capability: RequiredCapability = "retrieve",
    ) -> tuple[KnowledgeBase, EffectiveRole]:
        knowledge_base = await self.get_knowledge_base(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
        )
        role = await self.effective_role(
            principal=principal,
            knowledge_base=knowledge_base,
        )
        self.require_role(role, capability)
        assert role is not None
        return knowledge_base, role

    @staticmethod
    def require_role(
        role: EffectiveRole | None,
        capability: RequiredCapability,
    ) -> None:
        if role is None:
            raise PersonalKnowledgeAclError(
                "personal_knowledge_not_found",
                status_code=404,
            )
        if not role_allows(role, capability):
            raise PersonalKnowledgeAclError(
                "personal_knowledge_forbidden",
                status_code=403,
            )
