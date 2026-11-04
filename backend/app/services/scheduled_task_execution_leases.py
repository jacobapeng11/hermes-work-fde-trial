from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    RuntimeSession,
    ScheduledTask,
    ScheduledTaskExecutionLease,
)
from app.repositories.scheduled_task import (
    ScheduledTaskRecord,
    ScheduledTaskRepository,
)
from app.services.runtime_maintenance import (
    RuntimeMaintenanceError,
    RuntimeMaintenanceService,
)


_ACTIVE_STATUSES = frozenset({"claimed", "running"})


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    return _utc(left) == _utc(right)


@dataclass(frozen=True)
class ScheduledTaskExecutionGrant:
    task_id: int
    claim_token: str
    owner_instance_id: str
    lease_epoch: int
    trigger_kind: str
    scheduled_fire_at: datetime | None
    runtime_run_id: str | None


@dataclass(frozen=True)
class ClaimedScheduledTask:
    record: ScheduledTaskRecord
    grant: ScheduledTaskExecutionGrant | None


class ScheduledTaskExecutionBusyError(RuntimeError):
    pass


class ScheduledTaskExecutionMaintenanceError(RuntimeError):
    code = "SCHEDULED_TASK_RUNTIME_MAINTENANCE"


class ScheduledTaskExecutionLeaseStore:
    """Database-backed task execution ownership shared by all Backends."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        owner_instance_id: str,
        lease_ttl_seconds: int,
    ) -> None:
        self.db = db
        self.owner_instance_id = owner_instance_id
        self.lease_ttl_seconds = max(30, int(lease_ttl_seconds))

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[ClaimedScheduledTask]:
        current = _utc(now)
        if current is None or limit <= 0:
            return []
        try:
            await RuntimeMaintenanceService(
                self.db
            ).require_admission_open(lock=True)
        except RuntimeMaintenanceError as exc:
            await self.db.rollback()
            if exc.code == "MAINTENANCE_ADMISSION_FROZEN":
                return []
            raise
        rows = (
            await self.db.execute(
                select(ScheduledTask, RuntimeSession)
                .join(
                    RuntimeSession,
                    RuntimeSession.id == ScheduledTask.target_session_id,
                )
                .where(
                    ScheduledTask.status == "active",
                    ScheduledTask.next_run_at.is_not(None),
                    ScheduledTask.next_run_at <= current,
                    RuntimeSession.status == "active",
                )
                .order_by(ScheduledTask.next_run_at, ScheduledTask.id)
                .limit(limit)
                # MySQL 5.7 lacks SKIP LOCKED.  Blocking row locks preserve
                # exactly-once claim correctness, with serialized scanners.
                .with_for_update()
            )
        ).all()
        claimed: list[ClaimedScheduledTask] = []
        for task, session in rows:
            grant = await self._claim_locked(
                task=task,
                trigger_kind="scheduled",
                scheduled_fire_at=_utc(task.next_run_at),
                now=current,
            )
            if grant is None:
                continue
            claimed.append(
                ClaimedScheduledTask(
                    record=ScheduledTaskRepository._record(task, session),
                    grant=grant,
                )
            )
        await self.db.commit()
        return claimed

    async def claim_manual(
        self,
        *,
        task_id: int,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        now: datetime,
    ) -> ScheduledTaskExecutionGrant:
        current = _utc(now)
        if current is None:
            raise ValueError("execution claim time is required")
        try:
            await RuntimeMaintenanceService(
                self.db
            ).require_admission_open(lock=True)
        except RuntimeMaintenanceError as exc:
            await self.db.rollback()
            if exc.code == "MAINTENANCE_ADMISSION_FROZEN":
                raise ScheduledTaskExecutionMaintenanceError(
                    "runtime maintenance is active"
                ) from exc
            raise
        row = (
            await self.db.execute(
                select(ScheduledTask, RuntimeSession)
                .join(
                    RuntimeSession,
                    RuntimeSession.id == ScheduledTask.target_session_id,
                )
                .where(
                    ScheduledTask.id == task_id,
                    ScheduledTask.org_id == org_id,
                    ScheduledTask.user_id == user_id,
                    ScheduledTask.runtime_workspace_id
                    == runtime_workspace_id,
                    ScheduledTask.status != "deleted",
                    RuntimeSession.status == "active",
                )
                .with_for_update()
            )
        ).first()
        if row is None:
            raise ScheduledTaskExecutionBusyError(
                "scheduled task is no longer executable"
            )
        task, _session = row
        grant = await self._claim_locked(
            task=task,
            trigger_kind="manual",
            scheduled_fire_at=None,
            now=current,
        )
        if grant is None:
            await self.db.rollback()
            raise ScheduledTaskExecutionBusyError(
                "scheduled task is already running"
            )
        await self.db.commit()
        return grant

    async def _claim_locked(
        self,
        *,
        task: ScheduledTask,
        trigger_kind: str,
        scheduled_fire_at: datetime | None,
        now: datetime,
    ) -> ScheduledTaskExecutionGrant | None:
        lease = (
            await self.db.execute(
                select(ScheduledTaskExecutionLease)
                .where(
                    ScheduledTaskExecutionLease.task_id == int(task.id)
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            lease is not None
            and lease.status in _ACTIVE_STATUSES
            and (_utc(lease.lease_expires_at) or now) > now
        ):
            return None

        preserve_run = bool(
            lease is not None
            and trigger_kind == "scheduled"
            and lease.trigger_kind == "scheduled"
            and _same_instant(
                lease.scheduled_fire_at, scheduled_fire_at
            )
        )
        token = uuid4().hex
        expires_at = now + timedelta(seconds=self.lease_ttl_seconds)
        if lease is None:
            lease = ScheduledTaskExecutionLease(
                task_id=int(task.id),
                claim_token=token,
                owner_instance_id=self.owner_instance_id,
                lease_epoch=1,
                trigger_kind=trigger_kind,
                scheduled_fire_at=scheduled_fire_at,
                status="claimed",
                lease_expires_at=expires_at,
                heartbeat_at=now,
            )
            self.db.add(lease)
        else:
            lease.claim_token = token
            lease.owner_instance_id = self.owner_instance_id
            lease.lease_epoch = int(lease.lease_epoch) + 1
            lease.trigger_kind = trigger_kind
            lease.scheduled_fire_at = scheduled_fire_at
            lease.status = "claimed"
            lease.lease_expires_at = expires_at
            lease.heartbeat_at = now
            lease.runtime_run_id = (
                lease.runtime_run_id if preserve_run else None
            )
            lease.finished_at = None
            lease.last_error_code = None
        await self.db.flush()
        return ScheduledTaskExecutionGrant(
            task_id=int(task.id),
            claim_token=token,
            owner_instance_id=self.owner_instance_id,
            lease_epoch=int(lease.lease_epoch),
            trigger_kind=trigger_kind,
            scheduled_fire_at=scheduled_fire_at,
            runtime_run_id=lease.runtime_run_id,
        )

    async def begin(
        self, grant: ScheduledTaskExecutionGrant, *, now: datetime
    ) -> bool:
        return await self._renew(
            grant, now=now, next_status="running"
        )

    async def heartbeat(
        self, grant: ScheduledTaskExecutionGrant, *, now: datetime
    ) -> bool:
        return await self._renew(grant, now=now, next_status=None)

    async def _renew(
        self,
        grant: ScheduledTaskExecutionGrant,
        *,
        now: datetime,
        next_status: str | None,
    ) -> bool:
        current = _utc(now)
        if current is None:
            return False
        values: dict[str, object] = {
            "heartbeat_at": current,
            "lease_expires_at": current
            + timedelta(seconds=self.lease_ttl_seconds),
        }
        if next_status is not None:
            values["status"] = next_status
        result = await self.db.execute(
            update(ScheduledTaskExecutionLease)
            .where(
                ScheduledTaskExecutionLease.task_id == grant.task_id,
                ScheduledTaskExecutionLease.claim_token
                == grant.claim_token,
                ScheduledTaskExecutionLease.owner_instance_id
                == grant.owner_instance_id,
                ScheduledTaskExecutionLease.lease_epoch
                == grant.lease_epoch,
                ScheduledTaskExecutionLease.status.in_(
                    tuple(_ACTIVE_STATUSES)
                ),
            )
            .values(**values)
        )
        await self.db.commit()
        return bool(getattr(result, "rowcount", 0) == 1)

    async def bind_runtime_run(
        self,
        grant: ScheduledTaskExecutionGrant,
        *,
        runtime_run_id: str,
    ) -> bool:
        result = await self.db.execute(
            update(ScheduledTaskExecutionLease)
            .where(
                ScheduledTaskExecutionLease.task_id == grant.task_id,
                ScheduledTaskExecutionLease.claim_token
                == grant.claim_token,
                ScheduledTaskExecutionLease.owner_instance_id
                == grant.owner_instance_id,
                ScheduledTaskExecutionLease.lease_epoch
                == grant.lease_epoch,
                ScheduledTaskExecutionLease.status.in_(
                    tuple(_ACTIVE_STATUSES)
                ),
            )
            .values(runtime_run_id=runtime_run_id, status="running")
        )
        await self.db.commit()
        return bool(getattr(result, "rowcount", 0) == 1)

    async def finish(
        self,
        grant: ScheduledTaskExecutionGrant,
        *,
        success: bool,
        error_code: str | None = None,
        now: datetime,
    ) -> bool:
        return await self._terminal(
            grant,
            status="completed" if success else "failed",
            error_code=error_code,
            now=now,
        )

    async def supersede(
        self,
        grant: ScheduledTaskExecutionGrant,
        *,
        now: datetime,
    ) -> bool:
        return await self._terminal(
            grant,
            status="superseded",
            error_code=None,
            now=now,
        )

    async def release(
        self,
        grant: ScheduledTaskExecutionGrant,
        *,
        now: datetime,
    ) -> bool:
        return await self._terminal(
            grant,
            status="released",
            error_code=None,
            now=now,
        )

    async def _terminal(
        self,
        grant: ScheduledTaskExecutionGrant,
        *,
        status: str,
        error_code: str | None,
        now: datetime,
    ) -> bool:
        current = _utc(now)
        if current is None:
            return False
        result = await self.db.execute(
            update(ScheduledTaskExecutionLease)
            .where(
                ScheduledTaskExecutionLease.task_id == grant.task_id,
                ScheduledTaskExecutionLease.claim_token
                == grant.claim_token,
                ScheduledTaskExecutionLease.owner_instance_id
                == grant.owner_instance_id,
                ScheduledTaskExecutionLease.lease_epoch
                == grant.lease_epoch,
            )
            .values(
                status=status,
                finished_at=current,
                heartbeat_at=current,
                lease_expires_at=current,
                last_error_code=(
                    str(error_code)[:100] if error_code else None
                ),
            )
        )
        await self.db.commit()
        return bool(getattr(result, "rowcount", 0) == 1)
