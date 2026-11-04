from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.identity import User
from app.services.platform_session import (
    PlatformPresenceSummary,
    PlatformSessionStore,
)
from app.services.runtime_workspace import Principal


@dataclass(frozen=True)
class PlatformPresenceUser:
    user_id: int
    name: str
    user_code: str | None
    last_login_at: datetime | None = None

    @property
    def display_name(self) -> str:
        return f"{self.name}（{self.user_code or '工号缺失'}）"


@dataclass(frozen=True)
class PlatformPresenceDashboard:
    web_users: tuple[PlatformPresenceUser, ...]
    desktop_users: tuple[PlatformPresenceUser, ...]
    daily_login_users: tuple[PlatformPresenceUser, ...]
    daily_login_since: datetime
    window_seconds: int
    observed_at: datetime


class CrossDayPresenceRecorder(Protocol):
    async def record(self, principal: Principal) -> bool:
        ...


class SqlAlchemyCrossDayPresenceRecorder:
    """Persist carry-over presence once per user and business day."""

    def __init__(self, session_factory=AsyncSessionLocal):
        self.session_factory = session_factory

    async def record(self, principal: Principal) -> bool:
        daily_since = _business_day_start()
        since_utc = daily_since.astimezone(timezone.utc).replace(tzinfo=None)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        async with self.session_factory() as db:
            result = await db.execute(
                update(User)
                .where(
                    User.org_id == principal.org_id,
                    User.id == principal.user_id,
                    or_(
                        User.last_daily_presence_at.is_(None),
                        User.last_daily_presence_at < since_utc,
                    ),
                )
                .values(last_daily_presence_at=now_utc)
            )
            await db.commit()
            return bool(result.rowcount)


class ContractCrossDayPresenceRecorder:
    """Keep contract-mode presence streams independent from MySQL."""

    async def record(self, _principal: Principal) -> bool:
        return True


class PlatformPresenceMonitor:
    """Cache short-lived aggregate presence counts for admin polling."""

    def __init__(
        self,
        session_store: PlatformSessionStore,
        *,
        window_seconds: int,
        cache_ttl_seconds: int,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_store = session_store
        self.window_seconds = window_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self.now = now
        self._cache: dict[int, tuple[float, PlatformPresenceSummary]] = {}

    async def summary(self, *, org_id: int) -> PlatformPresenceSummary:
        observed_at = self.now()
        cached = self._cache.get(org_id)
        if (
            cached
            and self.cache_ttl_seconds > 0
            and observed_at - cached[0] < self.cache_ttl_seconds
        ):
            return cached[1]

        summary = await self.session_store.presence_summary(
            org_id,
            window_seconds=self.window_seconds,
        )
        self._cache[org_id] = (observed_at, summary)
        return summary


class PlatformPresenceDirectoryService:
    """Resolve presence user IDs and daily login identities from MySQL."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def dashboard(
        self,
        presence: PlatformPresenceSummary,
        *,
        org_id: int,
    ) -> PlatformPresenceDashboard:
        daily_since = _business_day_start()
        daily_users = await self._daily_login_users(
            org_id,
            since_utc=daily_since.astimezone(timezone.utc).replace(tzinfo=None),
        )
        all_online_ids = set(presence.web_user_ids) | set(presence.desktop_user_ids)
        users_by_id = await self._users_by_id(org_id, all_online_ids)

        return PlatformPresenceDashboard(
            web_users=_ordered_users(users_by_id, presence.web_user_ids),
            desktop_users=_ordered_users(users_by_id, presence.desktop_user_ids),
            daily_login_users=daily_users,
            daily_login_since=daily_since,
            window_seconds=presence.window_seconds,
            observed_at=presence.observed_at,
        )

    async def _daily_login_users(
        self,
        org_id: int,
        *,
        since_utc: datetime,
    ) -> tuple[PlatformPresenceUser, ...]:
        result = await self.db.execute(
            select(User)
            .where(
                User.org_id == org_id,
                or_(
                    User.last_login_at.is_not(None),
                    User.last_daily_presence_at.is_not(None),
                ),
                or_(
                    User.last_login_at >= since_utc,
                    User.last_daily_presence_at >= since_utc,
                ),
            )
            .order_by(User.last_login_at.desc(), User.id.desc())
        )
        return tuple(_presence_user(user) for user in result.scalars().all())

    async def _users_by_id(
        self,
        org_id: int,
        user_ids: Iterable[int],
    ) -> dict[int, PlatformPresenceUser]:
        ids = list(user_ids)
        users: list[User] = []
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            if not chunk:
                continue
            result = await self.db.execute(
                select(User)
                .where(User.org_id == org_id, User.id.in_(chunk))
                .order_by(User.name.asc(), User.user_code.asc(), User.id.asc())
            )
            users.extend(result.scalars().all())
        indexed = {
            int(user.id): _presence_user(user)
            for user in users
        }
        for user_id in ids:
            indexed.setdefault(
                user_id,
                PlatformPresenceUser(
                    user_id=user_id,
                    name="未知用户",
                    user_code=None,
                ),
            )
        return indexed


def _presence_user(user: User) -> PlatformPresenceUser:
    return PlatformPresenceUser(
        user_id=int(user.id),
        name=str(user.name or "未知用户"),
        user_code=user.user_code,
        last_login_at=user.last_login_at,
    )


def _ordered_users(
    users_by_id: dict[int, PlatformPresenceUser],
    user_ids: Iterable[int],
) -> tuple[PlatformPresenceUser, ...]:
    return tuple(
        sorted(
            (users_by_id[user_id] for user_id in user_ids),
            key=lambda user: (user.display_name, user.user_id),
        )
    )


def _business_day_start(now: datetime | None = None) -> datetime:
    zone = ZoneInfo(settings.ENTERPRISE_IDP_TIMEZONE)
    current = now or datetime.now(timezone.utc)
    local = current.astimezone(zone)
    return local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=zone,
    )


def current_business_day(now: datetime | None = None) -> date:
    return _business_day_start(now).date()


def session_started_before_business_day(
    created_at: int,
    *,
    now: datetime | None = None,
) -> bool:
    day_start = _business_day_start(now)
    created_local = (
        datetime.fromtimestamp(created_at, tz=timezone.utc)
        .astimezone(day_start.tzinfo)
    )
    return created_local < day_start
