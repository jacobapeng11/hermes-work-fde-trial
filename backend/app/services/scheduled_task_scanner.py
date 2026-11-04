from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable

from app.config import settings
from app.database import AsyncSessionLocal
from app.repositories.scheduled_task import ScheduledTaskRecord
from app.services.scheduled_task_execution_leases import (
    ClaimedScheduledTask,
    ScheduledTaskExecutionGrant,
    ScheduledTaskExecutionLeaseStore,
)
from app.services.scheduled_task_launcher import RunnerRequest, ScheduledTaskLauncher, TaskSnapshot

logger = logging.getLogger(__name__)


class DueTaskScanner:
    """Lifespan-owned, cross-replica due-task scanner.

    ``fetch_due`` is injectable for deterministic unit tests. It may be either
    a normal function or an async callable returning ``ScheduledTaskRecord``
    instances. Production scans claim each row through a fenced database lease
    before handing it to the process-local Launcher.
    """

    def __init__(
        self,
        launcher: ScheduledTaskLauncher,
        *,
        session_factory=AsyncSessionLocal,
        fetch_due: Callable[[datetime], Iterable[ScheduledTaskRecord] | Awaitable[Iterable[ScheduledTaskRecord]]] | None = None,
        interval_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.launcher = launcher
        self.session_factory = session_factory
        self.fetch_due = fetch_due
        self.interval_seconds = interval_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._task: asyncio.Task[None] | None = None
        self.last_scan_at: datetime | None = None
        self.scan_error_count = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_forever(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.scan_error_count += 1
                logger.exception("scheduled task scan failed")
            await asyncio.sleep(self.interval_seconds)

    async def scan_once(self, now: datetime | None = None) -> int:
        scan_time = now or self.clock()
        if scan_time.tzinfo is None or scan_time.utcoffset() is None:
            scan_time = scan_time.replace(tzinfo=timezone.utc)
        scan_time = scan_time.astimezone(timezone.utc)
        self.last_scan_at = scan_time
        try:
            claimed = await self._fetch(scan_time)
        except Exception:
            self.scan_error_count += 1
            logger.exception("scheduled task scan query failed")
            return 0
        launched = 0
        for item in claimed:
            record = item.record
            grant = item.grant
            try:
                fire_at = record.next_run_at
                snapshot = TaskSnapshot.from_record(record)
                request = (
                    RunnerRequest.with_execution_grant(
                        task_id=record.task_id,
                        trigger_kind="scheduled",
                        scheduled_fire_at=fire_at,
                        task_snapshot=snapshot,
                        grant=grant,
                    )
                    if grant is not None
                    else RunnerRequest(
                        task_id=record.task_id,
                        trigger_kind="scheduled",
                        scheduled_fire_at=fire_at,
                        task_snapshot=snapshot,
                    )
                )
                if self.launcher.launch(request):
                    launched += 1
                elif grant is not None:
                    await self._release(grant, scan_time)
            except Exception:
                # One malformed row must not prevent other tasks in this round.
                self.scan_error_count += 1
                logger.exception("scheduled task dispatch failed", extra={"task_id": record.task_id})
                if grant is not None:
                    await self._release(grant, scan_time)
        return launched

    async def _fetch(
        self, now: datetime
    ) -> list[ClaimedScheduledTask]:
        if self.fetch_due is not None:
            result = self.fetch_due(now)
            if inspect.isawaitable(result):
                result = await result
            return [
                ClaimedScheduledTask(record=record, grant=None)
                for record in result
            ]
        async with self.session_factory() as db:
            return await ScheduledTaskExecutionLeaseStore(
                db,
                owner_instance_id=self.launcher.instance_id,
                lease_ttl_seconds=(
                    settings.SCHEDULED_TASK_EXECUTION_LEASE_SECONDS
                ),
            ).claim_due(
                now=now,
                limit=settings.SCHEDULED_TASK_SCAN_BATCH_SIZE,
            )

    async def _release(
        self,
        grant: ScheduledTaskExecutionGrant,
        now: datetime,
    ) -> None:
        try:
            async with self.session_factory() as db:
                await ScheduledTaskExecutionLeaseStore(
                    db,
                    owner_instance_id=self.launcher.instance_id,
                    lease_ttl_seconds=(
                        settings.SCHEDULED_TASK_EXECUTION_LEASE_SECONDS
                    ),
                ).release(grant, now=now)
        except Exception:
            logger.exception(
                "scheduled task execution lease release failed",
                extra={"task_id": grant.task_id},
            )

    @property
    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def metrics(self) -> dict[str, int | bool | datetime | None]:
        return {
            "scanner_alive": self.is_alive,
            "last_scan_at": self.last_scan_at,
            "scan_error_count": self.scan_error_count,
            "running_tasks": len(self.launcher.running_tasks),
        }
