from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from .operation_lock import RuntimeOperationLocks


@dataclass(frozen=True)
class OperationSnapshot:
    operation_id: str
    workspace_id: int
    action: str
    target_key: str
    status: str
    submitted_at: datetime
    org_id: int | None = None
    user_id: int | None = None
    runtime_agent_profile_id: int | None = None
    error_code: str | None = None
    error_message: str | None = None


OperationHandler = Callable[[OperationSnapshot], Awaitable[None]]


class UserSkillOperationQueue:
    """Single-process FIFO queue, one worker per user Runtime."""

    def __init__(self, *, locks: RuntimeOperationLocks | None = None) -> None:
        self._locks = locks or RuntimeOperationLocks()
        self._queues: dict[int, asyncio.Queue[tuple[OperationSnapshot, OperationHandler]]] = {}
        self._workers: dict[int, asyncio.Task[None]] = {}
        # The key deliberately omits the action.  An install and an uninstall
        # of the same logical asset must never occupy the shared FIFO queue at
        # the same time.  A failed record is retained until that target is
        # explicitly submitted again; successful records are removed.
        self._operations: dict[tuple[int, str], OperationSnapshot] = {}
        self._guard = asyncio.Lock()

    async def submit(
        self,
        *,
        workspace_id: int,
        action: str,
        target_key: str,
        handler: OperationHandler,
        org_id: int | None = None,
        user_id: int | None = None,
        runtime_agent_profile_id: int | None = None,
    ) -> OperationSnapshot:
        dedupe_key = (workspace_id, target_key)
        async with self._guard:
            existing = self._operations.get(dedupe_key)
            if existing is not None and existing.status in {"queued", "running"}:
                return existing
            snapshot = OperationSnapshot(
                operation_id=uuid.uuid4().hex,
                workspace_id=workspace_id,
                action=action,
                target_key=target_key,
                status="queued",
                submitted_at=datetime.utcnow(),
                org_id=org_id,
                user_id=user_id,
                runtime_agent_profile_id=runtime_agent_profile_id,
            )
            queue = self._queues.setdefault(workspace_id, asyncio.Queue())
            await queue.put((snapshot, handler))
            self._operations[dedupe_key] = snapshot
            if workspace_id not in self._workers:
                self._workers[workspace_id] = asyncio.create_task(self._run(workspace_id))
            return snapshot

    def pending(
        self,
        workspace_id: int,
        *,
        org_id: int | None = None,
        user_id: int | None = None,
        runtime_agent_profile_id: int | None = None,
    ) -> list[OperationSnapshot]:
        return [
            item
            for item in self._operations.values()
            if item.workspace_id == workspace_id
            and (org_id is None or item.org_id == org_id)
            and (user_id is None or item.user_id == user_id)
            and (
                runtime_agent_profile_id is None
                or item.runtime_agent_profile_id == runtime_agent_profile_id
            )
        ]

    async def _run(self, workspace_id: int) -> None:
        queue = self._queues[workspace_id]
        lock = await self._locks.for_workspace(workspace_id)
        try:
            while True:
                try:
                    snapshot, handler = await asyncio.wait_for(queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    if queue.empty():
                        break
                    continue
                running = OperationSnapshot(**{**snapshot.__dict__, "status": "running"})
                self._replace(running)
                try:
                    async with lock:
                        await handler(running)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._replace(
                        OperationSnapshot(
                            **{
                                **running.__dict__,
                                "status": "failed",
                                "error_code": str(getattr(exc, "code", "operation_failed")),
                                "error_message": str(exc) or "operation_failed",
                            }
                        )
                    )
                else:
                    self._operations.pop((workspace_id, snapshot.target_key), None)
                finally:
                    queue.task_done()
        finally:
            self._workers.pop(workspace_id, None)

    def _replace(self, snapshot: OperationSnapshot) -> None:
        self._operations[(snapshot.workspace_id, snapshot.target_key)] = snapshot
