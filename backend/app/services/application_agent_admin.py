from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Role, RuntimeAuditLog, SysUser, User, UserRole
from app.services.admin_auth import AdminPrincipal
from app.services.runtime_workspace import Principal


APPLICATION_AGENT_ADMIN_ROLE = "application_agent_admin"


class ApplicationAgentAdminAuthorizationError(PermissionError):
    pass


class ApplicationAgentAdminGrantError(ValueError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ApplicationAgentAdminCandidate:
    user_id: int | None
    user_code: str
    name: str
    department_name: str | None
    status: str
    is_application_agent_admin: bool


@dataclass(frozen=True)
class ApplicationAgentAdminMembership:
    user_id: int
    user_code: str | None
    name: str
    department_name: str | None
    granted_by_user_id: int | None
    granted_at: datetime | None


@dataclass(frozen=True)
class ApplicationAgentAdminReplaceResult:
    members: list[ApplicationAgentAdminMembership]
    granted: list[ApplicationAgentAdminMembership]
    revoked: list[ApplicationAgentAdminMembership]


class ApplicationAgentRoleStore(Protocol):
    async def has_role(self, *, org_id: int, user_id: int) -> bool:
        ...


class ApplicationAgentAdminStore(ApplicationAgentRoleStore, Protocol):
    async def search_candidates(
        self,
        *,
        org_id: int,
        query: str,
        limit: int,
    ) -> list[ApplicationAgentAdminCandidate]:
        ...

    async def list_members(self, *, org_id: int) -> list[ApplicationAgentAdminMembership]:
        ...

    async def replace(
        self,
        *,
        org_id: int,
        user_codes: list[str],
        actor_user_id: int,
    ) -> ApplicationAgentAdminReplaceResult:
        ...


class ApplicationAgentAuthorizationService:
    def __init__(self, store: ApplicationAgentRoleStore) -> None:
        self.store = store

    async def can_manage(self, principal: Principal) -> bool:
        return await self.store.has_role(org_id=principal.org_id, user_id=principal.user_id)

    async def require_admin(self, principal: Principal) -> Principal:
        if not await self.can_manage(principal):
            raise ApplicationAgentAdminAuthorizationError("application_agent_admin_required")
        return principal


class ApplicationAgentAdminService:
    def __init__(self, store: ApplicationAgentAdminStore) -> None:
        self.store = store

    async def search_candidates(
        self,
        *,
        principal: AdminPrincipal,
        query: str,
        limit: int = 50,
    ) -> list[ApplicationAgentAdminCandidate]:
        return await self.store.search_candidates(
            org_id=principal.org_id,
            query=query.strip(),
            limit=max(1, min(limit, 50)),
        )

    async def list_members(
        self,
        *,
        principal: AdminPrincipal,
    ) -> list[ApplicationAgentAdminMembership]:
        return await self.store.list_members(org_id=principal.org_id)

    async def replace(
        self,
        *,
        principal: AdminPrincipal,
        user_codes: list[str],
    ) -> ApplicationAgentAdminReplaceResult:
        normalized = [user_code.strip() for user_code in user_codes]
        if any(not user_code for user_code in normalized):
            raise ApplicationAgentAdminGrantError(
                "application_agent_admin_user_code_required",
                status_code=422,
            )
        if len(set(normalized)) != len(normalized):
            raise ApplicationAgentAdminGrantError(
                "application_agent_admin_duplicate_user_code",
                status_code=422,
            )
        return await self.store.replace(
            org_id=principal.org_id,
            user_codes=normalized,
            actor_user_id=principal.user_id,
        )


class InMemoryApplicationAgentAdminStore:
    def __init__(self) -> None:
        self.candidates: dict[tuple[int, str], ApplicationAgentAdminCandidate] = {}
        self.memberships: dict[tuple[int, int], ApplicationAgentAdminMembership] = {}
        self.reset()

    def reset(self) -> None:
        self.candidates = {
            (settings.DEFAULT_ORG_ID, "dev-user"): ApplicationAgentAdminCandidate(
                user_id=settings.DEV_PRINCIPAL_USER_ID,
                user_code="dev-user",
                name="Development User",
                department_name=None,
                status="active",
                is_application_agent_admin=False,
            )
        }
        self.memberships.clear()

    async def has_role(self, *, org_id: int, user_id: int) -> bool:
        return (org_id, user_id) in self.memberships

    async def search_candidates(
        self,
        *,
        org_id: int,
        query: str,
        limit: int,
    ) -> list[ApplicationAgentAdminCandidate]:
        normalized = query.casefold()
        rows = [
            candidate
            for (candidate_org_id, _), candidate in self.candidates.items()
            if candidate_org_id == org_id
            and (
                not normalized
                or normalized in candidate.name.casefold()
                or normalized in candidate.user_code.casefold()
            )
        ]
        return sorted(rows, key=lambda row: (row.name, row.user_code))[:limit]

    async def list_members(self, *, org_id: int) -> list[ApplicationAgentAdminMembership]:
        return sorted(
            (membership for (member_org_id, _), membership in self.memberships.items() if member_org_id == org_id),
            key=lambda row: (row.name, row.user_id),
        )

    async def replace(
        self,
        *,
        org_id: int,
        user_codes: list[str],
        actor_user_id: int,
    ) -> ApplicationAgentAdminReplaceResult:
        candidates: list[ApplicationAgentAdminCandidate] = []
        for user_code in user_codes:
            candidate = self.candidates.get((org_id, user_code))
            if candidate is None:
                raise ApplicationAgentAdminGrantError(
                    "application_agent_admin_candidate_not_found",
                    status_code=404,
                )
            if candidate.status != "active" or candidate.user_id is None:
                raise ApplicationAgentAdminGrantError("application_agent_admin_candidate_inactive")
            candidates.append(candidate)

        current = {
            user_id: membership
            for (member_org_id, user_id), membership in self.memberships.items()
            if member_org_id == org_id
        }
        desired_user_ids = {int(candidate.user_id) for candidate in candidates if candidate.user_id is not None}
        revoked = [
            membership
            for user_id, membership in current.items()
            if user_id not in desired_user_ids
        ]
        granted: list[ApplicationAgentAdminMembership] = []
        next_memberships = {
            key: membership
            for key, membership in self.memberships.items()
            if key[0] != org_id
        }
        for candidate in candidates:
            assert candidate.user_id is not None
            membership = current.get(candidate.user_id)
            if membership is None:
                membership = ApplicationAgentAdminMembership(
                    user_id=candidate.user_id,
                    user_code=candidate.user_code,
                    name=candidate.name,
                    department_name=candidate.department_name,
                    granted_by_user_id=actor_user_id,
                    granted_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                granted.append(membership)
            next_memberships[(org_id, candidate.user_id)] = membership
        self.memberships = next_memberships
        self._refresh_candidate_flags()
        return ApplicationAgentAdminReplaceResult(
            members=await self.list_members(org_id=org_id),
            granted=granted,
            revoked=revoked,
        )

    def _refresh_candidate_flags(self) -> None:
        self.candidates = {
            key: ApplicationAgentAdminCandidate(
                user_id=candidate.user_id,
                user_code=candidate.user_code,
                name=candidate.name,
                department_name=candidate.department_name,
                status=candidate.status,
                is_application_agent_admin=(
                    candidate.user_id is not None and (key[0], candidate.user_id) in self.memberships
                ),
            )
            for key, candidate in self.candidates.items()
        }


class SqlAlchemyApplicationAgentAdminStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def has_role(self, *, org_id: int, user_id: int) -> bool:
        result = await self.db.execute(
            select(UserRole.id)
            .join(Role, Role.id == UserRole.role_id)
            .join(User, User.id == UserRole.user_id)
            .where(
                UserRole.org_id == org_id,
                UserRole.user_id == user_id,
                Role.org_id == org_id,
                Role.code == APPLICATION_AGENT_ADMIN_ROLE,
                Role.status == "active",
                User.org_id == org_id,
                User.status == "active",
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def search_candidates(
        self,
        *,
        org_id: int,
        query: str,
        limit: int,
    ) -> list[ApplicationAgentAdminCandidate]:
        pattern = _like_pattern(query)
        user_statement = select(User).where(User.org_id == org_id)
        directory_statement = select(SysUser).where(SysUser.code.is_not(None))
        if pattern is not None:
            user_statement = user_statement.where(
                or_(User.name.ilike(pattern, escape="\\"), User.user_code.ilike(pattern, escape="\\"))
            )
            directory_statement = directory_statement.where(
                or_(
                    SysUser.name.ilike(pattern, escape="\\"),
                    SysUser.username.ilike(pattern, escape="\\"),
                    SysUser.code.ilike(pattern, escape="\\"),
                )
            )
        users = list((await self.db.execute(user_statement.order_by(User.name, User.id).limit(limit))).scalars())
        directory_users = list(
            (
                await self.db.execute(
                    directory_statement.order_by(SysUser.name, SysUser.username, SysUser.user_id).limit(limit)
                )
            ).scalars()
        )
        admin_user_ids = await self._admin_user_ids(org_id=org_id)
        by_code: dict[str, ApplicationAgentAdminCandidate] = {}
        for user in users:
            code = str(user.user_code or "").strip()
            if not code:
                continue
            by_code[code] = ApplicationAgentAdminCandidate(
                user_id=int(user.id),
                user_code=code,
                name=str(user.name),
                department_name=None,
                status=str(user.status),
                is_application_agent_admin=int(user.id) in admin_user_ids,
            )
        for directory_user in directory_users:
            code = str(directory_user.code or "").strip()
            if not code:
                continue
            current = by_code.get(code)
            directory_status = _directory_status(directory_user)
            candidate = ApplicationAgentAdminCandidate(
                user_id=current.user_id if current else None,
                user_code=code,
                name=(str(directory_user.name or directory_user.username or code).strip() or code),
                department_name=str(directory_user.dept_name) if directory_user.dept_name else None,
                status=current.status if current and current.status != "active" else directory_status,
                is_application_agent_admin=current.is_application_agent_admin if current else False,
            )
            by_code[code] = candidate
        return sorted(by_code.values(), key=lambda row: (row.name, row.user_code))[:limit]

    async def list_members(self, *, org_id: int) -> list[ApplicationAgentAdminMembership]:
        role = await self._role(org_id=org_id, create=False)
        if role is None:
            return []
        rows = (
            await self.db.execute(
                select(UserRole, User)
                .join(User, User.id == UserRole.user_id)
                .where(
                    UserRole.org_id == org_id,
                    UserRole.role_id == role.id,
                    User.org_id == org_id,
                )
                .order_by(User.name, User.id)
            )
        ).all()
        actor_by_target = await self._latest_grant_actors(org_id=org_id)
        return [
            ApplicationAgentAdminMembership(
                user_id=int(user.id),
                user_code=str(user.user_code) if user.user_code else None,
                name=str(user.name),
                department_name=None,
                granted_by_user_id=actor_by_target.get(int(user.id)),
                granted_at=user_role.created_at,
            )
            for user_role, user in rows
        ]

    async def replace(
        self,
        *,
        org_id: int,
        user_codes: list[str],
        actor_user_id: int,
    ) -> ApplicationAgentAdminReplaceResult:
        try:
            users = list(
                (
                    await self.db.execute(
                        select(User).where(
                            User.org_id == org_id,
                            User.user_code.in_(user_codes),
                        )
                    )
                ).scalars()
            ) if user_codes else []
            users_by_code: dict[str, User] = {}
            for user in users:
                code = str(user.user_code or "").strip()
                if code in users_by_code:
                    raise ApplicationAgentAdminGrantError(
                        "application_agent_admin_identity_conflict"
                    )
                if user.status != "active":
                    raise ApplicationAgentAdminGrantError(
                        "application_agent_admin_candidate_inactive"
                    )
                users_by_code[code] = user

            missing_codes = [code for code in user_codes if code not in users_by_code]
            directory_users = list(
                (
                    await self.db.execute(
                        select(SysUser).where(SysUser.code.in_(missing_codes))
                    )
                ).scalars()
            ) if missing_codes else []
            directory_by_code: dict[str, SysUser] = {}
            for directory_user in directory_users:
                code = str(directory_user.code or "").strip()
                if code in directory_by_code:
                    raise ApplicationAgentAdminGrantError(
                        "application_agent_admin_identity_conflict"
                    )
                directory_by_code[code] = directory_user
            for code in missing_codes:
                directory_user = directory_by_code.get(code)
                if directory_user is None:
                    raise ApplicationAgentAdminGrantError(
                        "application_agent_admin_candidate_not_found",
                        status_code=404,
                    )
                if _directory_status(directory_user) != "active":
                    raise ApplicationAgentAdminGrantError(
                        "application_agent_admin_candidate_inactive"
                    )

            for code in missing_codes:
                directory_user = directory_by_code[code]
                user = User(
                    org_id=org_id,
                    user_code=code,
                    name=(
                        str(directory_user.name or directory_user.username or code).strip()
                        or code
                    ),
                    email=str(directory_user.email) if directory_user.email else None,
                    phone=str(directory_user.mobile) if directory_user.mobile else None,
                    hrm_user_id=str(directory_user.user_id),
                    status="active",
                )
                self.db.add(user)
                users_by_code[code] = user
            if missing_codes:
                await self.db.flush()

            role = await self._role(org_id=org_id, create=bool(user_codes))
            if role is None:
                return ApplicationAgentAdminReplaceResult(
                    members=[],
                    granted=[],
                    revoked=[],
                )

            rows = (
                await self.db.execute(
                    select(UserRole, User)
                    .join(User, User.id == UserRole.user_id)
                    .where(
                        UserRole.org_id == org_id,
                        UserRole.role_id == role.id,
                        User.org_id == org_id,
                    )
                )
            ).all()
            existing_by_user_id = {
                int(user.id): (user_role, user)
                for user_role, user in rows
            }
            desired_user_ids = {
                int(users_by_code[code].id)
                for code in user_codes
            }
            revoked = [
                ApplicationAgentAdminMembership(
                    user_id=user_id,
                    user_code=str(user.user_code) if user.user_code else None,
                    name=str(user.name),
                    department_name=None,
                    granted_by_user_id=None,
                    granted_at=user_role.created_at,
                )
                for user_id, (user_role, user) in existing_by_user_id.items()
                if user_id not in desired_user_ids
            ]
            for user_id, (user_role, _) in existing_by_user_id.items():
                if user_id not in desired_user_ids:
                    await self.db.delete(user_role)

            granted_user_ids: set[int] = set()
            for code in user_codes:
                user = users_by_code[code]
                user_id = int(user.id)
                if user_id not in existing_by_user_id:
                    self.db.add(
                        UserRole(
                            org_id=org_id,
                            user_id=user_id,
                            role_id=role.id,
                        )
                    )
                    granted_user_ids.add(user_id)

            await self.db.commit()
            members = await self.list_members(org_id=org_id)
            granted = [
                ApplicationAgentAdminMembership(
                    user_id=member.user_id,
                    user_code=member.user_code,
                    name=member.name,
                    department_name=None,
                    granted_by_user_id=actor_user_id,
                    granted_at=member.granted_at,
                )
                for member in members
                if member.user_id in granted_user_ids
            ]
            return ApplicationAgentAdminReplaceResult(
                members=members,
                granted=granted,
                revoked=revoked,
            )
        except Exception:
            await self.db.rollback()
            raise

    async def _role(self, *, org_id: int, create: bool) -> Role | None:
        role = (
            await self.db.execute(
                select(Role).where(
                    Role.org_id == org_id,
                    Role.code == APPLICATION_AGENT_ADMIN_ROLE,
                    Role.status == "active",
                )
            )
        ).scalar_one_or_none()
        if role is None and create:
            role = Role(
                org_id=org_id,
                code=APPLICATION_AGENT_ADMIN_ROLE,
                name="Application Agent Administrator",
                description="Can create, configure, publish, and operate application agents.",
                status="active",
            )
            self.db.add(role)
            await self.db.flush()
        return role

    async def _admin_user_ids(self, *, org_id: int) -> set[int]:
        role = await self._role(org_id=org_id, create=False)
        if role is None:
            return set()
        rows = await self.db.execute(
            select(UserRole.user_id).where(UserRole.org_id == org_id, UserRole.role_id == role.id)
        )
        return {int(row[0]) for row in rows.all()}

    async def _preprovision_user(self, *, org_id: int, user_code: str) -> User:
        directory_users = list(
            (await self.db.execute(select(SysUser).where(SysUser.code == user_code))).scalars()
        )
        if not directory_users:
            raise ApplicationAgentAdminGrantError("application_agent_admin_candidate_not_found", status_code=404)
        if len(directory_users) != 1:
            raise ApplicationAgentAdminGrantError("application_agent_admin_identity_conflict")
        directory_user = directory_users[0]
        if _directory_status(directory_user) != "active":
            raise ApplicationAgentAdminGrantError("application_agent_admin_candidate_inactive")
        user = User(
            org_id=org_id,
            user_code=user_code,
            name=(str(directory_user.name or directory_user.username or user_code).strip() or user_code),
            email=str(directory_user.email) if directory_user.email else None,
            phone=str(directory_user.mobile) if directory_user.mobile else None,
            hrm_user_id=str(directory_user.user_id),
            status="active",
        )
        self.db.add(user)
        await self.db.flush()
        return user

    async def _latest_grant_actors(self, *, org_id: int) -> dict[int, int | None]:
        rows = (
            await self.db.execute(
                select(RuntimeAuditLog)
                .where(
                    RuntimeAuditLog.org_id == org_id,
                    RuntimeAuditLog.action == "admin.application_agent_admin.granted",
                    RuntimeAuditLog.target_type == "user",
                )
                .order_by(RuntimeAuditLog.created_at.desc(), RuntimeAuditLog.id.desc())
            )
        ).scalars()
        actors: dict[int, int | None] = {}
        for row in rows:
            try:
                target_user_id = int(row.target_id)
            except (TypeError, ValueError):
                continue
            actors.setdefault(
                target_user_id,
                int(row.actor_user_id) if row.actor_user_id is not None else None,
            )
        return actors


def _like_pattern(query: str) -> str | None:
    normalized = query.strip()
    if not normalized:
        return None
    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _directory_status(user: SysUser) -> str:
    today = date.today()
    if user.status == 0:
        return "inactive"
    if user.dimission_date is not None and user.dimission_date <= today:
        return "inactive"
    return "active"
