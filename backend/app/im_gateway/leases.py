from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgent,
    ImChannel,
    ImChannelLease,
    RuntimeSession,
)
from app.services.managed_secret import ManagedSecretCodec


_MYSQL_DEADLOCK_ERROR_CODE = 1213
_LEASE_CLAIM_DEADLOCK_RETRY_SECONDS = 0.05


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_mysql_deadlock(error: OperationalError) -> bool:
    current: object | None = error.orig
    for _ in range(4):
        args = getattr(current, "args", ())
        if args and isinstance(args[0], int):
            return int(args[0]) == _MYSQL_DEADLOCK_ERROR_CODE
        current = getattr(current, "orig", None)
        if current is None:
            break
    return False


@dataclass(frozen=True)
class ImChannelLeaseGrant:
    channel_id: int
    owner_instance_id: str
    lease_epoch: int
    expires_at: datetime
    config_generation: int


@dataclass(frozen=True)
class OwnedImChannel:
    channel_id: int
    org_id: int
    user_id: int | None
    runtime_workspace_id: int | None
    runtime_session_row_id: int | None
    runtime_session_id: str | None
    bot_id: str
    secret: str
    config_generation: int
    lease_epoch: int
    status: str
    target_type: str = "personal_agent"
    created_by_user_id: int | None = None
    application_agent_id: int | None = None
    application_client_id: int | None = None
    direct_enabled: bool = True
    group_enabled: bool = True
    max_inflight_runs: int = 1


class SqlAlchemyImChannelLeaseStore:
    def __init__(
        self,
        db: AsyncSession,
        *,
        owner_instance_id: str,
        lease_ttl_seconds: int,
        secret_codec: ManagedSecretCodec | None = None,
    ) -> None:
        self.db = db
        self.owner_instance_id = owner_instance_id
        self.lease_ttl_seconds = lease_ttl_seconds
        self.secret_codec = secret_codec or ManagedSecretCodec()

    async def claim_available(
        self, *, limit: int, now: datetime | None = None
    ) -> list[ImChannelLeaseGrant]:
        if limit <= 0:
            return []
        current = now or utc_now()
        for attempt in range(2):
            try:
                return await self._claim_available_once(
                    limit=limit,
                    current=current,
                )
            except OperationalError as exc:
                if not _is_mysql_deadlock(exc):
                    raise
                await self.db.rollback()
                if attempt >= 1:
                    raise
                await asyncio.sleep(
                    _LEASE_CLAIM_DEADLOCK_RETRY_SECONDS
                )
        raise AssertionError(
            "lease claim retry loop exhausted without an exception"
        )

    async def _claim_available_once(
        self,
        *,
        limit: int,
        current: datetime,
    ) -> list[ImChannelLeaseGrant]:
        candidates = (
            await self.db.execute(
                select(ImChannel, ImChannelLease)
                .outerjoin(
                    ImChannelLease,
                    ImChannelLease.channel_id == ImChannel.id,
                )
                .outerjoin(
                    ApplicationAgent,
                    ApplicationAgent.id
                    == ImChannel.application_agent_id,
                )
                .where(
                    ImChannel.enabled.is_(True),
                    ImChannel.bot_id.is_not(None),
                    ImChannel.secret_encrypted.is_not(None),
                    ImChannel.status.not_in(("disabled", "revoked")),
                    or_(
                        ImChannel.target_type != "application_agent",
                        ApplicationAgent.status == "published",
                    ),
                    or_(
                        ImChannelLease.channel_id.is_(None),
                        ImChannelLease.expires_at <= current,
                    ),
                )
                .order_by(ImChannel.id)
                .limit(limit)
                # Production currently runs MySQL 5.7, which has no
                # SKIP LOCKED syntax.  A blocking current read still
                # serializes lease claims correctly at the present scale.
                .with_for_update()
            )
        ).all()
        grants: list[ImChannelLeaseGrant] = []
        expires_at = current + timedelta(
            seconds=self.lease_ttl_seconds
        )
        for channel, lease in candidates:
            if lease is None:
                lease = ImChannelLease(
                    channel_id=int(channel.id),
                    owner_instance_id=self.owner_instance_id,
                    lease_epoch=1,
                    expires_at=expires_at,
                    heartbeat_at=current,
                )
                self.db.add(lease)
            elif lease.owner_instance_id != self.owner_instance_id:
                lease.owner_instance_id = self.owner_instance_id
                lease.lease_epoch = int(lease.lease_epoch) + 1
                lease.expires_at = expires_at
                lease.heartbeat_at = current
            else:
                lease.expires_at = expires_at
                lease.heartbeat_at = current
            grants.append(
                ImChannelLeaseGrant(
                    channel_id=int(channel.id),
                    owner_instance_id=self.owner_instance_id,
                    lease_epoch=int(lease.lease_epoch),
                    expires_at=expires_at,
                    config_generation=int(channel.config_generation),
                )
            )
        await self.db.commit()
        return grants

    async def heartbeat(
        self,
        *,
        channel_epochs: dict[int, int],
        now: datetime | None = None,
    ) -> set[int]:
        if not channel_epochs:
            return set()
        current = now or utc_now()
        expires_at = current + timedelta(
            seconds=self.lease_ttl_seconds
        )
        rows = (
            await self.db.execute(
                select(ImChannelLease)
                .where(
                    ImChannelLease.channel_id.in_(
                        tuple(channel_epochs.keys())
                    ),
                    ImChannelLease.owner_instance_id
                    == self.owner_instance_id,
                )
                .with_for_update()
            )
        ).scalars()
        retained: set[int] = set()
        for row in rows:
            channel_id = int(row.channel_id)
            if int(row.lease_epoch) != channel_epochs.get(channel_id):
                continue
            row.heartbeat_at = current
            row.expires_at = expires_at
            retained.add(channel_id)
        await self.db.commit()
        return retained

    async def owned_channels(self) -> list[OwnedImChannel]:
        now = utc_now()
        rows = (
            await self.db.execute(
                select(
                    ImChannel,
                    ImChannelLease,
                    RuntimeSession,
                    ApplicationAgent,
                )
                .join(
                    ImChannelLease,
                    ImChannelLease.channel_id == ImChannel.id,
                )
                .outerjoin(
                    RuntimeSession,
                    RuntimeSession.id == ImChannel.runtime_session_row_id,
                )
                .outerjoin(
                    ApplicationAgent,
                    ApplicationAgent.id
                    == ImChannel.application_agent_id,
                )
                .where(
                    ImChannelLease.owner_instance_id
                    == self.owner_instance_id,
                    ImChannelLease.expires_at > now,
                    ImChannel.enabled.is_(True),
                    ImChannel.bot_id.is_not(None),
                    ImChannel.secret_encrypted.is_not(None),
                    ImChannel.status.not_in(("disabled", "revoked")),
                    or_(
                        ImChannel.target_type != "application_agent",
                        ApplicationAgent.status == "published",
                    ),
                )
                .order_by(ImChannel.id)
            )
        ).all()
        owned: list[OwnedImChannel] = []
        for channel, lease, session, application_agent in rows:
            if not channel.bot_id or not channel.secret_encrypted:
                continue
            owned.append(
                OwnedImChannel(
                    channel_id=int(channel.id),
                    org_id=int(channel.org_id),
                    user_id=(
                        int(channel.user_id)
                        if channel.user_id is not None
                        else None
                    ),
                    runtime_workspace_id=(
                        int(channel.runtime_workspace_id)
                        if channel.runtime_workspace_id is not None
                        else None
                    ),
                    runtime_session_row_id=(
                        int(channel.runtime_session_row_id)
                        if channel.runtime_session_row_id is not None
                        else None
                    ),
                    runtime_session_id=(
                        str(session.runtime_session_id)
                        if session is not None
                        else None
                    ),
                    bot_id=str(channel.bot_id),
                    secret=self.secret_codec.decrypt(
                        channel.secret_encrypted
                    ),
                    config_generation=int(
                        channel.config_generation
                    ),
                    lease_epoch=int(lease.lease_epoch),
                    status=str(channel.status),
                    target_type=str(channel.target_type),
                    created_by_user_id=int(channel.created_by_user_id),
                    application_agent_id=(
                        int(channel.application_agent_id)
                        if channel.application_agent_id is not None
                        else None
                    ),
                    application_client_id=(
                        int(channel.application_client_id)
                        if channel.application_client_id is not None
                        else None
                    ),
                    direct_enabled=bool(channel.direct_enabled),
                    group_enabled=bool(channel.group_enabled),
                    max_inflight_runs=(
                        settings.APPLICATION_AGENT_MAX_INFLIGHT_RUNS
                        if application_agent is not None
                        else 1
                    ),
                )
            )
        return owned

    async def release(
        self, *, channel_id: int, lease_epoch: int
    ) -> None:
        await self.db.execute(
            delete(ImChannelLease).where(
                ImChannelLease.channel_id == channel_id,
                ImChannelLease.owner_instance_id
                == self.owner_instance_id,
                ImChannelLease.lease_epoch == lease_epoch,
            )
        )
        await self.db.commit()
