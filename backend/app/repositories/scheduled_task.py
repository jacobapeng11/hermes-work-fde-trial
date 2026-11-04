from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CloudHermesProfile,
    ImChannel,
    RuntimeSession,
    ScheduledTask,
    ScheduledTaskExecutionLease,
)
from app.im_gateway.scheduled_delivery import (
    WECOM_SCHEDULED_TARGET_NOT_READY,
    personal_wecom_channel_is_scheduled_task_eligible,
)


def _as_utc(value: datetime | None) -> datetime | None:
    """Restore scheduled instants read from MySQL ``DATETIME`` as UTC.

    The scheduled-task writer persists instants in UTC, while MySQL's
    timezone-naive ``DATETIME`` type discards the offset on read.  A missing
    offset must therefore mean UTC here, never the task's display timezone.
    """

    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ScheduledTaskNotFoundError(LookupError):
    """The task is not visible to this owner/workspace (including deleted)."""


class ScheduledTaskTargetNotFoundError(LookupError):
    """The requested workspace or Session is not owned by the caller."""


class ScheduledTaskWeComTargetNotReadyError(ValueError):
    code = WECOM_SCHEDULED_TARGET_NOT_READY

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class ScheduledTaskRecord:
    task_id: int
    org_id: int
    user_id: int
    runtime_workspace_id: int
    target_session_id: int
    target_runtime_session_id: str
    target_session_title: str | None
    target_session_status: str | None
    task_name: str
    prompt: str
    schedule_type: str
    schedule_config: dict[str, Any]
    cron_expr: str | None
    timezone: str
    status: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None
    last_error_code: str | None
    last_error: str | None
    last_runtime_run_id: str | None
    run_count: int
    created_at: datetime | None
    updated_at: datetime | None
    is_running: bool = False


class ScheduledTaskRepository:
    """Database access for scheduled tasks.

    Every statement includes all three owner dimensions.  The repository does
    not call Runtime keepalive or prewarm and deliberately has no execution
    concerns; Runner work belongs to the next implementation phase.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def assert_workspace_owner(self, *, org_id: int, user_id: int, runtime_workspace_id: int) -> None:
        result = await self.db.execute(
            select(CloudHermesProfile.id).where(
                CloudHermesProfile.id == runtime_workspace_id,
                CloudHermesProfile.org_id == org_id,
                CloudHermesProfile.user_id == user_id,
                CloudHermesProfile.status == "active",
            )
        )
        if result.scalar_one_or_none() is None:
            raise ScheduledTaskTargetNotFoundError("runtime workspace not found")

    async def resolve_target_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSession:
        await self.assert_workspace_owner(
            org_id=org_id, user_id=user_id, runtime_workspace_id=runtime_workspace_id
        )
        result = await self.db.execute(
            select(RuntimeSession).where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
                RuntimeSession.status == "active",
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise ScheduledTaskTargetNotFoundError("runtime session not found")
        await self.assert_target_session_eligible(session)
        return session

    async def assert_target_session_eligible(
        self, session: RuntimeSession
    ) -> None:
        if session.source_type != "wecom":
            return
        channel = await self.db.scalar(
            select(ImChannel).where(
                ImChannel.runtime_session_row_id == int(session.id)
            )
        )
        if not personal_wecom_channel_is_scheduled_task_eligible(channel):
            raise ScheduledTaskWeComTargetNotReadyError()

    async def create(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        target_runtime_session_id: str,
        values: dict[str, Any],
    ) -> ScheduledTaskRecord:
        session = await self.resolve_target_session(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=target_runtime_session_id,
        )
        task = ScheduledTask(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            target_session_id=int(session.id),
            **values,
        )
        self.db.add(task)
        await self.db.commit()
        await self.db.refresh(task)
        return self._record(task, session)

    async def get(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        task_id: int,
    ) -> ScheduledTaskRecord:
        result = await self.db.execute(
            select(
                ScheduledTask,
                RuntimeSession,
                ScheduledTaskExecutionLease,
            )
            .join(RuntimeSession, RuntimeSession.id == ScheduledTask.target_session_id)
            .outerjoin(
                ScheduledTaskExecutionLease,
                ScheduledTaskExecutionLease.task_id == ScheduledTask.id,
            )
            .where(
                ScheduledTask.id == task_id,
                ScheduledTask.org_id == org_id,
                ScheduledTask.user_id == user_id,
                ScheduledTask.runtime_workspace_id == runtime_workspace_id,
                ScheduledTask.status != "deleted",
            )
        )
        row = result.first()
        if row is None:
            raise ScheduledTaskNotFoundError("scheduled task not found")
        task, session, lease = row
        return self._record(
            task,
            session,
            is_running=self._lease_is_running(lease),
        )

    async def list(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> list[ScheduledTaskRecord]:
        await self.assert_workspace_owner(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
        )
        # A CASE expression keeps the presentation order deterministic while
        # leaving runner-owned is_running registry data out of persistence.
        status_order = case(
            (ScheduledTask.status == "active", 0),
            (ScheduledTask.status == "paused", 1),
            (ScheduledTask.status == "completed", 2),
            else_=3,
        )
        result = await self.db.execute(
            select(
                ScheduledTask,
                RuntimeSession,
                ScheduledTaskExecutionLease,
            )
            .join(RuntimeSession, RuntimeSession.id == ScheduledTask.target_session_id)
            .outerjoin(
                ScheduledTaskExecutionLease,
                ScheduledTaskExecutionLease.task_id == ScheduledTask.id,
            )
            .where(
                ScheduledTask.org_id == org_id,
                ScheduledTask.user_id == user_id,
                ScheduledTask.runtime_workspace_id == runtime_workspace_id,
                ScheduledTask.status != "deleted",
            )
            .order_by(
                status_order,
                # MySQL does not support ``ASC NULLS LAST``.  Boolean ordering
                # puts False (a planned run) before True (no next run), then
                # preserves chronological order within the planned bucket.
                ScheduledTask.next_run_at.is_(None).asc(),
                ScheduledTask.next_run_at.asc(),
                ScheduledTask.updated_at.desc(),
            )
        )
        return [
            self._record(
                task,
                session,
                is_running=self._lease_is_running(lease),
            )
            for task, session, lease in result.all()
        ]

    async def update(self, *, task: ScheduledTask, session: RuntimeSession | None = None, **values: Any) -> ScheduledTaskRecord:
        for key, value in values.items():
            setattr(task, key, value)
        if session is None:
            result = await self.db.execute(select(RuntimeSession).where(RuntimeSession.id == task.target_session_id))
            session = result.scalar_one_or_none()
        await self.db.commit()
        await self.db.refresh(task)
        if session is None:
            raise ScheduledTaskTargetNotFoundError("runtime session not found")
        return self._record(task, session)

    async def finalize_run_summary(
        self,
        *,
        task_id: int,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        last_run_at: datetime,
        last_run_status: str,
        last_error_code: str | None,
        last_error: str | None,
        last_runtime_run_id: str | None,
        increment_run_count: int = 0,
    ) -> bool:
        """Persist a runner's terminal summary in its own short transaction.

        This update intentionally does not touch ``status`` or ``next_run_at``;
        the schedule transition is performed by ``advance_after_run`` with a
        compare-and-set guard so an edit/pause/delete cannot be overwritten by
        an older runner.
        """
        values: dict[str, Any] = {
            "last_run_at": last_run_at,
            "last_run_status": last_run_status,
            "last_error_code": last_error_code,
            "last_error": last_error,
            "last_runtime_run_id": last_runtime_run_id,
        }
        if increment_run_count:
            values["run_count"] = ScheduledTask.run_count + int(increment_run_count)
        result = await self.db.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == task_id,
                ScheduledTask.org_id == org_id,
                ScheduledTask.user_id == user_id,
                ScheduledTask.runtime_workspace_id == runtime_workspace_id,
            )
            .values(**values)
        )
        await self.db.commit()
        return bool(getattr(result, "rowcount", 0) == 1)

    async def advance_after_run(
        self,
        *,
        task_id: int,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        scheduled_fire_at: datetime,
        status: str,
        next_run_at: datetime | None,
    ) -> bool:
        """Conditionally apply the old trigger's status/plan transition."""
        result = await self.db.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == task_id,
                ScheduledTask.org_id == org_id,
                ScheduledTask.user_id == user_id,
                ScheduledTask.runtime_workspace_id == runtime_workspace_id,
                ScheduledTask.status == "active",
                ScheduledTask.next_run_at == scheduled_fire_at,
            )
            .values(status=status, next_run_at=next_run_at)
        )
        await self.db.commit()
        return bool(getattr(result, "rowcount", 0) == 1)

    async def get_model(
        self, *, org_id: int, user_id: int, runtime_workspace_id: int, task_id: int
    ) -> tuple[ScheduledTask, RuntimeSession]:
        result = await self.db.execute(
            select(ScheduledTask, RuntimeSession)
            .join(RuntimeSession, RuntimeSession.id == ScheduledTask.target_session_id)
            .where(
                ScheduledTask.id == task_id,
                ScheduledTask.org_id == org_id,
                ScheduledTask.user_id == user_id,
                ScheduledTask.runtime_workspace_id == runtime_workspace_id,
                ScheduledTask.status != "deleted",
            )
        )
        row = result.first()
        if row is None:
            raise ScheduledTaskNotFoundError("scheduled task not found")
        return row

    async def soft_delete(self, *, org_id: int, user_id: int, runtime_workspace_id: int, task_id: int) -> None:
        result = await self.db.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == task_id,
                ScheduledTask.org_id == org_id,
                ScheduledTask.user_id == user_id,
                ScheduledTask.runtime_workspace_id == runtime_workspace_id,
                ScheduledTask.status != "deleted",
            )
            .values(status="deleted", next_run_at=None)
        )
        if result.rowcount != 1:
            raise ScheduledTaskNotFoundError("scheduled task not found")
        await self.db.commit()

    async def pause_for_session(self, *, org_id: int, user_id: int, runtime_workspace_id: int, session_id: int) -> int:
        result = await self.db.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.org_id == org_id,
                ScheduledTask.user_id == user_id,
                ScheduledTask.runtime_workspace_id == runtime_workspace_id,
                ScheduledTask.target_session_id == session_id,
                ScheduledTask.status != "deleted",
            )
            .values(
                status="paused",
                next_run_at=None,
                last_error_code="SESSION_DELETED",
                last_error="The target session was deleted; choose another session before resuming.",
            )
        )
        # Small fake sessions used by unit tests may only expose scalar
        # accessors; a missing rowcount still means the UPDATE was staged.
        return int(getattr(result, "rowcount", 0) or 0)

    @staticmethod
    def _record(
        task: ScheduledTask,
        session: RuntimeSession,
        *,
        is_running: bool = False,
    ) -> ScheduledTaskRecord:
        return ScheduledTaskRecord(
            task_id=int(task.id),
            org_id=int(task.org_id),
            user_id=int(task.user_id),
            runtime_workspace_id=int(task.runtime_workspace_id),
            target_session_id=int(task.target_session_id),
            target_runtime_session_id=str(session.runtime_session_id),
            target_session_title=session.title_cache,
            target_session_status=session.status,
            task_name=task.task_name,
            prompt=task.prompt,
            schedule_type=task.schedule_type,
            schedule_config=dict(task.schedule_config),
            cron_expr=task.cron_expr,
            timezone=task.timezone,
            status=task.status,
            next_run_at=_as_utc(task.next_run_at),
            last_run_at=_as_utc(task.last_run_at),
            last_run_status=task.last_run_status,
            last_error_code=task.last_error_code,
            last_error=task.last_error,
            last_runtime_run_id=task.last_runtime_run_id,
            run_count=int(task.run_count),
            created_at=_as_utc(task.created_at),
            updated_at=_as_utc(task.updated_at),
            is_running=is_running,
        )

    @staticmethod
    def _lease_is_running(
        lease: ScheduledTaskExecutionLease | None,
    ) -> bool:
        return bool(
            lease is not None
            and lease.status in {"claimed", "running"}
            and (
                _as_utc(lease.lease_expires_at)
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            > datetime.now(timezone.utc)
        )
