from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Literal
from uuid import uuid4

from app.repositories.scheduled_task import ScheduledTaskRecord
from app.services.scheduled_task_execution_leases import (
    ScheduledTaskExecutionGrant,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskSnapshot:
    """Immutable task data captured at dispatch time.

    Runner code intentionally receives this object instead of a live ORM
    instance so edits made after acceptance cannot alter the submitted prompt
    or target Session.
    """

    task_id: int
    task_name: str
    prompt: str
    org_id: int
    user_id: int
    runtime_workspace_id: int
    target_session_id: int
    target_runtime_session_id: str
    target_session_status: str | None
    status: str
    schedule_type: str
    schedule_config: dict
    next_run_at: datetime | None

    @classmethod
    def from_record(cls, record: ScheduledTaskRecord) -> "TaskSnapshot":
        return cls(
            task_id=record.task_id,
            task_name=record.task_name,
            prompt=record.prompt,
            org_id=record.org_id,
            user_id=record.user_id,
            runtime_workspace_id=record.runtime_workspace_id,
            target_session_id=record.target_session_id,
            target_runtime_session_id=record.target_runtime_session_id,
            target_session_status=record.target_session_status,
            status=record.status,
            schedule_type=record.schedule_type,
            schedule_config=dict(record.schedule_config),
            next_run_at=record.next_run_at,
        )


@dataclass(frozen=True)
class RunnerRequest:
    task_id: int
    trigger_kind: Literal["scheduled", "manual"]
    scheduled_fire_at: datetime | None
    task_snapshot: TaskSnapshot
    execution_claim_token: str | None = None
    execution_owner_instance_id: str | None = None
    execution_lease_epoch: int | None = None
    claimed_runtime_run_id: str | None = None

    @classmethod
    def with_execution_grant(
        cls,
        *,
        task_id: int,
        trigger_kind: Literal["scheduled", "manual"],
        scheduled_fire_at: datetime | None,
        task_snapshot: TaskSnapshot,
        grant: ScheduledTaskExecutionGrant,
    ) -> "RunnerRequest":
        return cls(
            task_id=task_id,
            trigger_kind=trigger_kind,
            scheduled_fire_at=scheduled_fire_at,
            task_snapshot=task_snapshot,
            execution_claim_token=grant.claim_token,
            execution_owner_instance_id=grant.owner_instance_id,
            execution_lease_epoch=grant.lease_epoch,
            claimed_runtime_run_id=grant.runtime_run_id,
        )

    def execution_grant(
        self,
    ) -> ScheduledTaskExecutionGrant | None:
        if (
            not self.execution_claim_token
            or not self.execution_owner_instance_id
            or self.execution_lease_epoch is None
        ):
            return None
        return ScheduledTaskExecutionGrant(
            task_id=self.task_id,
            claim_token=self.execution_claim_token,
            owner_instance_id=self.execution_owner_instance_id,
            lease_epoch=self.execution_lease_epoch,
            trigger_kind=self.trigger_kind,
            scheduled_fire_at=self.scheduled_fire_at,
            runtime_run_id=self.claimed_runtime_run_id,
        )


async def _default_runner(_request: RunnerRequest) -> None:
    """No-op until the execution runner is installed by the next phase."""


RunnerFactory = Callable[[RunnerRequest], Awaitable[None]]


class ScheduledTaskLauncher:
    """Single-process registry shared by Scanner and HTTP trigger.

    The check/create/register sequence deliberately contains no ``await``;
    asyncio therefore cannot interleave two launch calls for the same task.
    """

    def __init__(
        self,
        runner_factory: RunnerFactory | None = None,
        *,
        runner: RunnerFactory | None = None,
        instance_id: str | None = None,
    ) -> None:
        if runner_factory is not None and runner is not None:
            raise TypeError("pass runner_factory or runner, not both")
        self._runner_factory: RunnerFactory = runner or runner_factory or _default_runner
        self.instance_id = instance_id or (
            f"{socket.gethostname()}:{os.getpid()}:scheduled:"
            f"{uuid4().hex[:8]}"
        )
        self.running_tasks: dict[int, asyncio.Task[None]] = {}
        self._stopping = False

    def start(self) -> None:
        """Re-arm a launcher after a previous lifespan shutdown."""
        self._stopping = False

    def launch(self, request: RunnerRequest) -> bool:
        if self._stopping or request.task_id in self.running_tasks:
            return False
        try:
            coroutine = self._runner_factory(request)
            if not inspect.isawaitable(coroutine):
                raise TypeError("runner_factory must return an awaitable")
            task = asyncio.create_task(coroutine)
        except Exception:
            logger.exception("scheduled task runner could not be created", extra={"task_id": request.task_id})
            return False
        self.running_tasks[request.task_id] = task
        task.add_done_callback(lambda done, task_id=request.task_id: self._on_done(task_id, done))
        return True

    def _on_done(self, task_id: int, task: asyncio.Task[None]) -> None:
        # Do not let a stale callback remove a newer task occupying the slot.
        if self.running_tasks.get(task_id) is task:
            self.running_tasks.pop(task_id, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            # Exception reprs may include provider payloads or credentials.
            logger.error(
                "scheduled task runner failed",
                extra={"scheduled_task_id": task_id, "error_code": type(error).__name__.upper()},
            )

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self.running_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.running_tasks.clear()

    @property
    def is_running(self) -> bool:
        return bool(self.running_tasks)

    @property
    def metrics(self) -> dict[str, int | bool]:
        """Small in-memory view consumed by diagnostics/health checks."""
        return {"running_tasks": len(self.running_tasks), "launcher_stopping": self._stopping}
