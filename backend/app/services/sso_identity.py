from __future__ import annotations

import asyncio
import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CloudHermesProfile, HermesRuntimeBinding, Role, SysUser, User, UserRole
from app.services.personal_config_bundle import bootstrap_config_revision


logger = logging.getLogger(__name__)
_MEMORY_IDENTITY_MARKERS = (
    "PLATFORM_IDENTITY_BEGIN",
    "PLATFORM_IDENTITY_END",
)


class SsoIdentityError(PermissionError):
    pass


@dataclass(frozen=True)
class SsoIdentity:
    org_id: int
    user_id: int
    user_code: str
    name: str
    email: str | None


class SsoIdentityRepository(Protocol):
    async def find_hrm_users(self, *, work_no: str) -> list[SysUser]:
        ...

    async def find_candidates(self, *, org_id: int, sub_account: str) -> list[User]:
        ...

    async def add_user(self, user: User) -> None:
        ...

    async def ensure_user_role(self, *, org_id: int, user_id: int, role_code: str) -> None:
        ...

    async def advance_personal_runtime_identity(
        self,
        *,
        org_id: int,
        user_id: int,
    ) -> None:
        ...

    async def commit(self) -> None:
        ...


class SsoIdentityService:
    def __init__(self, repository: SsoIdentityRepository) -> None:
        self.repository = repository

    async def resolve_or_create(self, *, org_id: int, work_no: str,
                                display_name: str | None = None, email: str | None = None) -> SsoIdentity:
        # Retry only the identity transaction, never an OAuth2 code exchange.
        for attempt in range(3):
            try:
                return await self._resolve_or_create_once(org_id=org_id, work_no=work_no,
                                                          display_name=display_name, email=email)
            except (IntegrityError, OperationalError) as exc:
                if not isinstance(self.repository, SqlAlchemySsoIdentityRepository):
                    raise
                await self.repository.db.rollback()
                if isinstance(exc, OperationalError) and exc.orig.args[0] not in (1205, 1213):
                    raise
                if attempt == 2:
                    raise SsoIdentityError("platform identity transaction conflicted") from None
                await asyncio.sleep(0.02 * (attempt + 1))
        raise AssertionError("unreachable")

    async def _resolve_or_create_once(self, *, org_id: int, work_no: str,
                                      display_name: str | None = None, email: str | None = None) -> SsoIdentity:
        work_no = work_no.strip()
        if not work_no or len(work_no) > 32 or _contains_control_character(work_no):
            raise SsoIdentityError("invalid EnterpriseIdp workNo")

        hrm_rows = await self.repository.find_hrm_users(work_no=work_no)
        hrm_row = hrm_rows[0] if len(hrm_rows) == 1 else None
        hrm_name = _hrm_display_name(hrm_row) if hrm_row is not None else None

        candidates = await self.repository.find_candidates(org_id=org_id, sub_account=work_no)
        if len(candidates) > 1:
            raise SsoIdentityError("conflicting platform identity")

        if candidates:
            user = candidates[0]
            if user.status != "active":
                raise SsoIdentityError("platform user is disabled")
            if (user.user_code and user.user_code != work_no) or (
                user.sso_subject and user.sso_subject != work_no
            ):
                raise SsoIdentityError("conflicting platform identity")
            previous_name = str(user.name)
            user.user_code = work_no
            user.sso_subject = work_no
            if hrm_row is not None and hrm_name is not None:
                user.name = hrm_name
                user.hrm_user_id = hrm_row.user_id
            if hrm_name is not None and previous_name != str(user.name):
                # 进入个人 Runtime Bundle 的身份实质变化必须推进配置代数，
                # 否则同代数 Bundle 摘要随实时身份漂移并触发下载 409。
                await self.repository.advance_personal_runtime_identity(
                    org_id=org_id,
                    user_id=int(user.id),
                )
        else:
            user = User(
                org_id=org_id,
                user_code=work_no,
                sso_subject=work_no,
                name=hrm_name or display_name or "用户",
                email=email,
                status="active",
                password_hash=None,
                hrm_user_id=(
                    hrm_row.user_id
                    if hrm_row is not None and hrm_name is not None
                    else None
                ),
                last_login_at=_now(),
            )
            await self.repository.add_user(user)

        user.last_login_at = _now()
        user.login_count = int(user.login_count or 0) + 1
        await self.repository.ensure_user_role(org_id=org_id, user_id=int(user.id), role_code="user")
        await self.repository.commit()
        return SsoIdentity(
            org_id=org_id,
            user_id=int(user.id),
            user_code=work_no,
            name=user.name,
            email=user.email,
        )


class SqlAlchemySsoIdentityRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def find_hrm_users(self, *, work_no: str) -> list[SysUser]:
        try:
            result = await self.db.execute(select(SysUser).where(SysUser.code == work_no))
        except SQLAlchemyError:
            await self.db.rollback()
            logger.warning("HRM identity lookup unavailable")
            return []
        return list(result.scalars().all())

    async def find_candidates(self, *, org_id: int, sub_account: str) -> list[User]:
        result = await self.db.execute(
            select(User).where(
                User.org_id == org_id,
                or_(User.user_code == sub_account, User.sso_subject == sub_account),
            ).with_for_update()
        )
        return list(result.scalars().all())

    async def add_user(self, user: User) -> None:
        self.db.add(user)
        await self.db.flush()

    async def ensure_user_role(self, *, org_id: int, user_id: int, role_code: str) -> None:
        role_result = await self.db.execute(
            select(Role).where(Role.org_id == org_id, Role.code == role_code, Role.status == "active")
        )
        role = role_result.scalar_one_or_none()
        if role is None:
            raise SsoIdentityError("default user role is unavailable")
        user_role_result = await self.db.execute(
            select(UserRole).where(
                UserRole.org_id == org_id,
                UserRole.user_id == user_id,
                UserRole.role_id == role.id,
            )
        )
        if user_role_result.scalar_one_or_none() is None:
            self.db.add(UserRole(org_id=org_id, user_id=user_id, role_id=role.id))

    async def advance_personal_runtime_identity(
        self,
        *,
        org_id: int,
        user_id: int,
    ) -> None:
        """同一事务内把个人 Runtime Desired 状态推进到新的身份代数。

        ready/error Binding 直接递增 generation 并写 bootstrap revision；
        starting/updating Binding 不与进行中的准备器竞争目标，仅记录
        pending identity 意图，由后续生命周期在最新身份上收敛。
        """
        profile = await self.db.scalar(
            select(CloudHermesProfile)
            .where(
                CloudHermesProfile.org_id == org_id,
                CloudHermesProfile.user_id == user_id,
                CloudHermesProfile.profile_type == "default_chat_agent",
                CloudHermesProfile.status == "active",
            )
            .with_for_update()
        )
        if profile is None:
            return
        binding = await self.db.scalar(
            select(HermesRuntimeBinding)
            .where(
                HermesRuntimeBinding.runtime_workspace_id == int(profile.id),
            )
            .with_for_update()
        )
        if binding is None:
            return
        scopes = set(binding.pending_reload_scopes or [])
        scopes.add("identity")
        binding.pending_reload_scopes = sorted(scopes)
        binding.pending_reload = True
        if str(binding.status) in ("ready", "error"):
            generation = int(binding.config_generation or 0) + 1
            binding.config_generation = generation
            binding.config_hash = bootstrap_config_revision(generation)
            binding.last_error = None

    async def commit(self) -> None:
        await self.db.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _hrm_display_name(row: SysUser) -> str | None:
    for value in (row.name, row.username):
        normalized = (value or "").strip()
        if (
            normalized
            and not _contains_control_character(normalized)
            and not any(marker in normalized for marker in _MEMORY_IDENTITY_MARKERS)
        ):
            return normalized
    return None
