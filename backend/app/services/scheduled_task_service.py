from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.config import settings
from app.repositories.scheduled_task import (
    ScheduledTaskRecord,
    ScheduledTaskRepository,
)
from app.schemas.scheduled_task import ScheduledTaskCommand
from app.services.runtime_workspace import Principal
from app.services.scheduled_task_execution_leases import (
    ScheduledTaskExecutionBusyError,
    ScheduledTaskExecutionGrant,
    ScheduledTaskExecutionLeaseStore,
    ScheduledTaskExecutionMaintenanceError,
)
from app.services.scheduled_task_launcher import RunnerRequest, ScheduledTaskLauncher, TaskSnapshot


BUSINESS_TIMEZONE = "Asia/Shanghai"
_BUSINESS_ZONE = ZoneInfo(BUSINESS_TIMEZONE)
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_INTERVALS = {
    "minutes": frozenset({5, 10, 15, 30, 45}),
    "hours": frozenset({1, 2, 3, 4, 6, 8, 12}),
}


class ScheduleValidationError(ValueError):
    code = "INVALID_SCHEDULE"


class ScheduleConflictError(ValueError):
    code = "SCHEDULE_CONFLICT"


class ScheduledTaskStateConflictError(ValueError):
    code = "TASK_STATE_CONFLICT"


class ScheduledTaskTriggerConflictError(ValueError):
    code = "TASK_TRIGGER_CONFLICT"


class ScheduledTaskTriggerMaintenanceError(RuntimeError):
    code = "SCHEDULED_TASK_RUNTIME_MAINTENANCE"


@dataclass(frozen=True)
class NormalizedSchedule:
    schedule_type: str
    schedule_config: dict[str, Any]
    cron_expr: str | None
    next_run_at: datetime
    schedule_summary: str


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleValidationError("datetime must include a UTC offset")
    return value.astimezone(timezone.utc)


def _ensure_keys(config: dict[str, Any], expected: set[str]) -> None:
    if set(config) != expected:
        raise ScheduleValidationError("unsupported schedule fields")


def _parse_time(value: Any) -> tuple[int, int]:
    if not isinstance(value, str) or not _TIME_PATTERN.fullmatch(value):
        raise ScheduleValidationError("time must use HH:mm")
    hour, minute = value.split(":")
    return int(hour), int(minute)


def normalize_schedule(config: dict[str, Any], *, now: datetime) -> NormalizedSchedule:
    """Validate a wire schedule and calculate its first UTC occurrence.

    `now` is injected by callers/tests and may be any offset-aware datetime;
    all comparisons and results are converted to UTC.
    """
    if not isinstance(config, dict):
        raise ScheduleValidationError("schedule_config must be an object")
    now_utc = _utc(now)
    schedule_type = config.get("type")
    if schedule_type == "once":
        _ensure_keys(config, {"type", "run_at"})
        run_at = _utc_from_value(config.get("run_at"))
        if run_at <= now_utc:
            raise ScheduleConflictError("once run_at must be in the future")
        return NormalizedSchedule(
            "once", dict(config), None, run_at, "执行一次",
        )

    if schedule_type == "interval":
        _ensure_keys(config, {"type", "unit", "value"})
        unit, value = config.get("unit"), config.get("value")
        if (
            unit not in _INTERVALS
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value not in _INTERVALS[unit]
        ):
            raise ScheduleValidationError("unsupported interval")
        delta = timedelta(**{unit: int(value)})
        return NormalizedSchedule(
            "interval", dict(config), None, now_utc + delta,
            f"每 {value} {'分钟' if unit == 'minutes' else '小时'}",
        )

    if schedule_type == "cron":
        preset = config.get("preset")
        if preset == "daily":
            _ensure_keys(config, {"type", "preset", "time"})
            weekday = None
        elif preset == "weekly":
            _ensure_keys(config, {"type", "preset", "weekday", "time"})
            weekday = config.get("weekday")
            if isinstance(weekday, bool) or not isinstance(weekday, int) or not 1 <= weekday <= 7:
                raise ScheduleValidationError("weekday must be between 1 and 7")
        else:
            raise ScheduleValidationError("unsupported cron preset")
        hour, minute = _parse_time(config.get("time"))
        local_now = now_utc.astimezone(_BUSINESS_ZONE)
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if preset == "daily":
            if candidate <= local_now:
                candidate += timedelta(days=1)
            cron_expr = f"{minute} {hour} * * *"
            summary = f"每天 {config['time']}"
        else:
            # Python weekday is Monday=0; wire weekday is Monday=1..Sunday=7.
            days_ahead = (weekday - 1 - local_now.weekday()) % 7
            candidate += timedelta(days=days_ahead)
            if candidate <= local_now:
                candidate += timedelta(days=7)
            # Cron accepts both 0 and 7 for Sunday.  Keep the wire's frozen
            # 1..7 convention in the derived expression for transparent
            # round-tripping (weekday 7 is Sunday).
            cron_expr = f"{minute} {hour} * * {weekday}"
            names = ("一", "二", "三", "四", "五", "六", "日")
            summary = f"每周{names[weekday - 1]} {config['time']}"
        return NormalizedSchedule("cron", dict(config), cron_expr, candidate.astimezone(timezone.utc), summary)

    raise ScheduleValidationError("unsupported schedule type")


def _utc_from_value(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ScheduleValidationError("run_at must be an ISO8601 datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduleValidationError("run_at must be an ISO8601 datetime") from exc
    return _utc(parsed)


def _next_schedule_after(
    schedule_type: str,
    schedule_config: dict[str, Any],
    scheduled_fire_at: datetime,
    now: datetime,
) -> datetime | None:
    """Return the first occurrence strictly after ``now``.

    The calculation starts at the captured fire time rather than at ``now``;
    this makes a restart that missed several interval windows advance exactly
    once to the first future occurrence.
    """
    fire_at = _utc(scheduled_fire_at)
    now_utc = _utc(now)
    if schedule_type == "interval":
        unit = schedule_config.get("unit")
        value = schedule_config.get("value")
        if unit not in _INTERVALS or value not in _INTERVALS[unit]:
            raise ScheduleValidationError("unsupported interval")
        delta = timedelta(**{unit: int(value)})
        candidate = fire_at + delta
        while candidate <= now_utc:
            candidate += delta
        return candidate
    if schedule_type == "cron":
        return normalize_schedule(schedule_config, now=now_utc).next_run_at
    return None


_SENSITIVE_ERROR_RE = re.compile(
    r"(?i)(?:bearer\s+|(?:api[_ -]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+"
)


def _sanitize_run_error(value: Any) -> str | None:
    """Keep a short operator hint while removing credentials/internal blobs."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    text = _SENSITIVE_ERROR_RE.sub("[REDACTED]", text)
    # Avoid persisting a full provider response or an accidentally echoed
    # prompt.  The UI only needs a bounded, safe fallback next to error_code.
    return text[:512] if text else None


class ScheduledTaskService:
    def __init__(self, repository: ScheduledTaskRepository, *, clock: Callable[[], datetime] | None = None) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def list_tasks(self, *, principal: Principal, runtime_workspace_id: int) -> list[ScheduledTaskRecord]:
        return await self.repository.list(
            org_id=principal.org_id, user_id=principal.user_id, runtime_workspace_id=runtime_workspace_id
        )

    async def get_task(self, *, principal: Principal, runtime_workspace_id: int, task_id: int) -> ScheduledTaskRecord:
        return await self.repository.get(
            org_id=principal.org_id, user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id, task_id=task_id,
        )

    async def create_task(
        self, *, principal: Principal, runtime_workspace_id: int, command: ScheduledTaskCommand
    ) -> ScheduledTaskRecord:
        normalized = normalize_schedule(command.schedule_config, now=_utc(self.clock()))
        return await self.repository.create(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            target_runtime_session_id=command.target_runtime_session_id,
            values={
                "task_name": command.task_name,
                "prompt": command.prompt,
                "schedule_type": normalized.schedule_type,
                "schedule_config": normalized.schedule_config,
                "cron_expr": normalized.cron_expr,
                "timezone": BUSINESS_TIMEZONE,
                "status": "active",
                "next_run_at": normalized.next_run_at,
            },
        )

    async def update_task(
        self, *, principal: Principal, runtime_workspace_id: int, task_id: int, command: ScheduledTaskCommand
    ) -> ScheduledTaskRecord:
        task, session = await self.repository.get_model(
            org_id=principal.org_id, user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id, task_id=task_id,
        )
        # An active task may have an already-expired once timestamp while a
        # runner is finalizing it.  Editing its name/prompt/target must not
        # revalidate that old timestamp: only a plan edit asks for a new
        # future occurrence.  Completed tasks are the exception because any
        # edit is explicitly the transition back to active.
        schedule_changed = command.schedule_config != task.schedule_config
        normalized: NormalizedSchedule | None = None
        if schedule_changed or task.status == "completed":
            normalized = normalize_schedule(command.schedule_config, now=_utc(self.clock()))
        target_changed = command.target_runtime_session_id != session.runtime_session_id
        if target_changed:
            session = await self.repository.resolve_target_session(
                org_id=principal.org_id, user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=command.target_runtime_session_id,
            )
        else:
            await self.repository.assert_target_session_eligible(session)
        values: dict[str, Any] = {
            "task_name": command.task_name,
            "prompt": command.prompt,
            "target_session_id": int(session.id),
        }
        if normalized is not None:
            values.update(
                schedule_type=normalized.schedule_type,
                schedule_config=normalized.schedule_config,
                cron_expr=normalized.cron_expr,
            )
        if task.status == "completed":
            values.update(status="active", next_run_at=normalized.next_run_at)
        elif task.status == "paused":
            values.update(next_run_at=None)
        elif task.status == "active" and schedule_changed and normalized is not None:
            values.update(next_run_at=normalized.next_run_at)
        return await self.repository.update(task=task, session=session, **values)

    async def pause_task(self, *, principal: Principal, runtime_workspace_id: int, task_id: int) -> ScheduledTaskRecord:
        task, session = await self.repository.get_model(
            org_id=principal.org_id, user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id, task_id=task_id,
        )
        if task.status == "paused":
            return await self.repository.update(task=task, session=session)
        if task.status != "active":
            raise ScheduledTaskStateConflictError("only active tasks can be paused")
        task.status = "paused"
        task.next_run_at = None
        return await self.repository.update(task=task, session=session)

    async def resume_task(
        self, *, principal: Principal, runtime_workspace_id: int, task_id: int, now: datetime
    ) -> ScheduledTaskRecord:
        task, session = await self.repository.get_model(
            org_id=principal.org_id, user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id, task_id=task_id,
        )
        if task.status != "paused":
            raise ScheduledTaskStateConflictError("only paused tasks can be resumed")
        await self.repository.assert_target_session_eligible(session)
        normalized = normalize_schedule(task.schedule_config, now=_utc(now))
        task.status = "active"
        task.next_run_at = normalized.next_run_at
        return await self.repository.update(task=task, session=session)

    async def delete_task(self, *, principal: Principal, runtime_workspace_id: int, task_id: int) -> None:
        await self.repository.soft_delete(
            org_id=principal.org_id, user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id, task_id=task_id,
        )

    async def trigger_task(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        task_id: int,
        launcher: ScheduledTaskLauncher,
        now: datetime | None = None,
    ) -> ScheduledTaskRecord:
        """Validate and register a manual trigger without changing the plan."""
        record = await self.get_task(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            task_id=task_id,
        )
        # The target Session is checked before registration so deleting/deleted
        # sessions never produce a misleading 202 response.
        if record.target_session_status != "active":
            raise ScheduledTaskTriggerConflictError("target runtime session is not active")
        task, target_session = await self.repository.get_model(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            task_id=task_id,
        )
        del task
        await self.repository.assert_target_session_eligible(target_session)
        claim_time = _utc(now or self.clock())
        grant: ScheduledTaskExecutionGrant | None = None
        lease_store: ScheduledTaskExecutionLeaseStore | None = None
        db = getattr(self.repository, "db", None)
        if db is not None:
            lease_store = ScheduledTaskExecutionLeaseStore(
                db,
                owner_instance_id=launcher.instance_id,
                lease_ttl_seconds=(
                    settings.SCHEDULED_TASK_EXECUTION_LEASE_SECONDS
                ),
            )
            try:
                grant = await lease_store.claim_manual(
                    task_id=record.task_id,
                    org_id=record.org_id,
                    user_id=record.user_id,
                    runtime_workspace_id=record.runtime_workspace_id,
                    now=claim_time,
                )
            except ScheduledTaskExecutionBusyError as exc:
                raise ScheduledTaskTriggerConflictError(str(exc)) from exc
            except ScheduledTaskExecutionMaintenanceError as exc:
                raise ScheduledTaskTriggerMaintenanceError from exc
        snapshot = TaskSnapshot.from_record(record)
        request = (
            RunnerRequest.with_execution_grant(
                task_id=record.task_id,
                trigger_kind="manual",
                scheduled_fire_at=None,
                task_snapshot=snapshot,
                grant=grant,
            )
            if grant is not None
            else RunnerRequest(
                task_id=record.task_id,
                trigger_kind="manual",
                scheduled_fire_at=None,
                task_snapshot=snapshot,
            )
        )
        if not launcher.launch(request):
            if lease_store is not None and grant is not None:
                await lease_store.release(grant, now=claim_time)
            raise ScheduledTaskTriggerConflictError("scheduled task is already running")
        return record

    async def finalize_run(self, *, request: RunnerRequest, terminal: Any) -> None:
        """Write a terminal summary, then conditionally advance the plan.

        The two repository calls deliberately use separate short transactions.
        The first one is safe to apply even when the user edited or paused the
        task while a Runner was in flight.  The second is a compare-and-set on
        ``status=active`` and the captured ``scheduled_fire_at``; a stale
        Runner therefore cannot replace a newer user plan.
        """
        snapshot = request.task_snapshot
        now = _utc(self.clock())
        status = str(getattr(terminal, "status", "failed")).lower()
        success = status in {"success", "completed"}
        accepted = success or status == "queued"
        last_status = "success" if success else "queued" if accepted else "failed"
        error_code = getattr(terminal, "error_code", None) or (
            "RUN_FAILED" if not accepted else None
        )
        error_text = getattr(terminal, "error_message", None)
        safe_error = _sanitize_run_error(error_text)
        runtime_run_id = getattr(terminal, "runtime_run_id", None) or None

        summary_values = {
            "task_id": snapshot.task_id,
            "org_id": snapshot.org_id,
            "user_id": snapshot.user_id,
            "runtime_workspace_id": snapshot.runtime_workspace_id,
            "last_run_at": now,
            "last_run_status": last_status,
            "last_error_code": str(error_code)[:100] if error_code else None,
            "last_error": safe_error,
            "last_runtime_run_id": str(runtime_run_id)[:128] if runtime_run_id else None,
            "increment_run_count": 1 if success else 0,
        }
        finalize_summary = getattr(self.repository, "finalize_run_summary", None)
        if finalize_summary is not None:
            await finalize_summary(**summary_values)
        else:  # compatibility with lightweight repositories used by integrations
            task, session = await self.repository.get_model(
                org_id=snapshot.org_id,
                user_id=snapshot.user_id,
                runtime_workspace_id=snapshot.runtime_workspace_id,
                task_id=snapshot.task_id,
            )
            task.last_run_at = now
            task.last_run_status = last_status
            task.last_error_code = summary_values["last_error_code"]
            task.last_error = safe_error
            task.last_runtime_run_id = summary_values["last_runtime_run_id"]
            if success:
                task.run_count = int(getattr(task, "run_count", 0)) + 1
            await self.repository.update(task=task, session=session)

        # Manual execution writes the summary/counter only and never moves the
        # normal plan.  A missing fire timestamp has the same semantics.
        if request.trigger_kind != "scheduled" or request.scheduled_fire_at is None:
            return

        fire_at = _utc(request.scheduled_fire_at)
        if snapshot.schedule_type == "once":
            next_status, next_run_at = (
                ("completed", None) if accepted else ("paused", None)
            )
        else:
            next_status = "active"
            next_run_at = _next_schedule_after(
                snapshot.schedule_type,
                snapshot.schedule_config,
                fire_at,
                now,
            )

        advance = getattr(self.repository, "advance_after_run", None)
        if advance is not None:
            await advance(
                task_id=snapshot.task_id,
                org_id=snapshot.org_id,
                user_id=snapshot.user_id,
                runtime_workspace_id=snapshot.runtime_workspace_id,
                scheduled_fire_at=fire_at,
                status=next_status,
                next_run_at=next_run_at,
            )

    # Name kept as an explicit alias for existing dependency wiring and tests.
    async def update_run_final_state(self, *, request: RunnerRequest, terminal: Any) -> None:
        await self.finalize_run(request=request, terminal=terminal)


# Functional aliases make the service convenient for API dependency wiring and
# preserve the small function-oriented interface used by early callers.
async def create_task(*, principal: Principal, runtime_workspace_id: int, command: ScheduledTaskCommand, service: ScheduledTaskService) -> ScheduledTaskRecord:
    return await service.create_task(principal=principal, runtime_workspace_id=runtime_workspace_id, command=command)


async def update_task(*, principal: Principal, runtime_workspace_id: int, task_id: int, command: ScheduledTaskCommand, service: ScheduledTaskService) -> ScheduledTaskRecord:
    return await service.update_task(principal=principal, runtime_workspace_id=runtime_workspace_id, task_id=task_id, command=command)


async def pause_task(*, principal: Principal, runtime_workspace_id: int, task_id: int, service: ScheduledTaskService) -> ScheduledTaskRecord:
    return await service.pause_task(principal=principal, runtime_workspace_id=runtime_workspace_id, task_id=task_id)


async def resume_task(*, principal: Principal, runtime_workspace_id: int, task_id: int, now: datetime, service: ScheduledTaskService) -> ScheduledTaskRecord:
    return await service.resume_task(principal=principal, runtime_workspace_id=runtime_workspace_id, task_id=task_id, now=now)
