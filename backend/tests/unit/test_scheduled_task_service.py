from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.schemas.scheduled_task import ScheduledTaskCommand
from app.services.runtime_workspace import Principal
from app.services.scheduled_task_service import (
    ScheduleConflictError,
    ScheduleValidationError,
    ScheduledTaskService,
    normalize_schedule,
)
from app.services.scheduled_task_launcher import RunnerRequest, TaskSnapshot
from app.services.scheduled_task_runner import RuntimeRunTerminal


NOW = datetime(2026, 7, 17, 4, 0, tzinfo=timezone.utc)


def _finalizer_request(*, trigger_kind="scheduled", schedule_type="once", schedule_config=None, fire_at=None):
    fire_at = fire_at or datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc)
    snapshot = TaskSnapshot(
        task_id=7,
        task_name="daily",
        prompt="summarize",
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        target_session_id=4,
        target_runtime_session_id="rs-7",
        target_session_status="active",
        status="active",
        schedule_type=schedule_type,
        schedule_config=schedule_config or {"type": schedule_type, "run_at": fire_at.isoformat()},
        next_run_at=fire_at,
    )
    return RunnerRequest(7, trigger_kind, fire_at if trigger_kind == "scheduled" else None, snapshot)


class _FinalizerRepository:
    def __init__(self, *, status="active", next_run_at=None, run_count=0):
        self.task = SimpleNamespace(
            status=status,
            next_run_at=next_run_at,
            last_run_at=None,
            last_run_status=None,
            last_error_code=None,
            last_error=None,
            last_runtime_run_id=None,
            run_count=run_count,
        )

    async def finalize_run_summary(self, **values):
        for key, value in values.items():
            if key == "increment_run_count":
                self.task.run_count += int(value)
            else:
                setattr(self.task, key, value)
        return self.task

    async def advance_after_run(self, **values):
        expected = values["scheduled_fire_at"]
        if self.task.status != "active" or self.task.next_run_at != expected:
            return False
        self.task.status = values["status"]
        self.task.next_run_at = values["next_run_at"]
        return True


@pytest.mark.asyncio
async def test_finalize_once_success_completes_and_records_summary() -> None:
    fire_at = datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc)
    repository = _FinalizerRepository(next_run_at=fire_at)
    service = ScheduledTaskService(repository, clock=lambda: datetime(2026, 7, 17, 5, 1, tzinfo=timezone.utc))

    await service.finalize_run(
        request=_finalizer_request(fire_at=fire_at),
        terminal=RuntimeRunTerminal("rr-7", "completed"),
    )

    assert repository.task.status == "completed"
    assert repository.task.next_run_at is None
    assert repository.task.last_run_status == "success"
    assert repository.task.last_runtime_run_id == "rr-7"
    assert repository.task.run_count == 1


@pytest.mark.asyncio
async def test_finalize_queued_receipt_advances_plan_without_claiming_run_success() -> None:
    fire_at = datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc)
    repository = _FinalizerRepository(next_run_at=fire_at)
    service = ScheduledTaskService(
        repository,
        clock=lambda: datetime(2026, 7, 17, 5, 1, tzinfo=timezone.utc),
    )

    await service.finalize_run(
        request=_finalizer_request(fire_at=fire_at),
        terminal=RuntimeRunTerminal("rr-queued", "queued"),
    )

    assert repository.task.status == "completed"
    assert repository.task.next_run_at is None
    assert repository.task.last_run_status == "queued"
    assert repository.task.last_runtime_run_id == "rr-queued"
    assert repository.task.run_count == 0
    assert repository.task.last_error_code is None


@pytest.mark.asyncio
async def test_finalize_once_failure_pauses_and_sanitizes_error() -> None:
    fire_at = datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc)
    repository = _FinalizerRepository(next_run_at=fire_at)
    service = ScheduledTaskService(repository, clock=lambda: datetime(2026, 7, 17, 5, 1, tzinfo=timezone.utc))

    await service.finalize_run(
        request=_finalizer_request(fire_at=fire_at),
        terminal=RuntimeRunTerminal("", "failed", "RUNTIME_FAILED", "Bearer super-secret-token"),
    )

    assert repository.task.status == "paused"
    assert repository.task.next_run_at is None
    assert repository.task.last_run_status == "failed"
    assert repository.task.last_error_code == "RUNTIME_FAILED"
    assert "super-secret-token" not in (repository.task.last_error or "")


@pytest.mark.asyncio
async def test_finalize_interval_skips_missed_windows_and_manual_keeps_plan() -> None:
    fire_at = datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc)
    repository = _FinalizerRepository(next_run_at=fire_at)
    service = ScheduledTaskService(repository, clock=lambda: datetime(2026, 7, 17, 5, 32, tzinfo=timezone.utc))
    request = _finalizer_request(
        fire_at=fire_at,
        schedule_type="interval",
        schedule_config={"type": "interval", "unit": "minutes", "value": 10},
    )
    await service.finalize_run(request=request, terminal=RuntimeRunTerminal("rr-7", "completed"))
    assert repository.task.next_run_at == datetime(2026, 7, 17, 5, 40, tzinfo=timezone.utc)

    manual_repo = _FinalizerRepository(next_run_at=fire_at)
    manual_service = ScheduledTaskService(manual_repo, clock=lambda: datetime(2026, 7, 17, 5, 32, tzinfo=timezone.utc))
    await manual_service.finalize_run(
        request=_finalizer_request(trigger_kind="manual", fire_at=fire_at, schedule_type="interval", schedule_config={"type": "interval", "unit": "minutes", "value": 10}),
        terminal=RuntimeRunTerminal("rr-8", "completed"),
    )
    assert manual_repo.task.status == "active"
    assert manual_repo.task.next_run_at == fire_at
    assert manual_repo.task.run_count == 1


@pytest.mark.asyncio
async def test_finalize_does_not_overwrite_edited_plan() -> None:
    fire_at = datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc)
    repository = _FinalizerRepository(next_run_at=datetime(2026, 7, 17, 6, 0, tzinfo=timezone.utc))
    service = ScheduledTaskService(repository, clock=lambda: datetime(2026, 7, 17, 5, 1, tzinfo=timezone.utc))
    await service.finalize_run(request=_finalizer_request(fire_at=fire_at), terminal=RuntimeRunTerminal("rr-7", "completed"))
    assert repository.task.status == "active"
    assert repository.task.next_run_at == datetime(2026, 7, 17, 6, 0, tzinfo=timezone.utc)


def test_once_requires_future_offset_datetime_and_stores_utc() -> None:
    result = normalize_schedule(
        {"type": "once", "run_at": "2026-07-18T10:30:00+08:00"}, now=NOW
    )
    assert result.schedule_type == "once"
    assert result.next_run_at == datetime(2026, 7, 18, 2, 30, tzinfo=timezone.utc)
    assert result.cron_expr is None

    with pytest.raises(ScheduleValidationError):
        normalize_schedule({"type": "once", "run_at": "2026-07-18T10:30:00"}, now=NOW)
    with pytest.raises(ScheduleConflictError):
        normalize_schedule({"type": "once", "run_at": "2026-07-17T11:59:00+08:00"}, now=NOW)


@pytest.mark.parametrize(
    "config",
    [
        {"type": "interval", "unit": "minutes", "value": 5},
        {"type": "interval", "unit": "minutes", "value": 45},
        {"type": "interval", "unit": "hours", "value": 12},
    ],
)
def test_interval_uses_only_controlled_values(config: dict) -> None:
    result = normalize_schedule(config, now=NOW)
    assert result.schedule_type == "interval"
    assert result.next_run_at > NOW


def test_interval_rejects_uncontrolled_values() -> None:
    with pytest.raises(ScheduleValidationError):
        normalize_schedule({"type": "interval", "unit": "seconds", "value": 1}, now=NOW)
    with pytest.raises(ScheduleValidationError):
        normalize_schedule({"type": "interval", "unit": "minutes", "value": 7}, now=NOW)


def test_daily_and_weekly_cron_are_generated_in_business_timezone() -> None:
    daily = normalize_schedule({"type": "cron", "preset": "daily", "time": "12:30"}, now=NOW)
    assert daily.cron_expr == "30 12 * * *"
    assert daily.next_run_at == datetime(2026, 7, 17, 4, 30, tzinfo=timezone.utc)

    weekly = normalize_schedule(
        {"type": "cron", "preset": "weekly", "weekday": 7, "time": "09:15"}, now=NOW
    )
    assert weekly.cron_expr == "15 9 * * 7"
    assert weekly.next_run_at == datetime(2026, 7, 19, 1, 15, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "config",
    [
        {"type": "cron", "preset": "daily", "time": "9:15"},
        {"type": "cron", "preset": "weekly", "weekday": 0, "time": "09:15"},
        {"type": "cron", "preset": "weekly", "weekday": 1, "time": "24:00"},
    ],
)
def test_cron_rejects_invalid_weekday_or_time(config: dict) -> None:
    with pytest.raises(ScheduleValidationError):
        normalize_schedule(config, now=NOW)


def test_command_strips_name_but_preserves_prompt_body() -> None:
    command = ScheduledTaskCommand(
        task_name="  Daily  ",
        prompt="  hello\nworld  ",
        target_runtime_session_id="rs-1-a",
        schedule_config={"type": "interval", "unit": "hours", "value": 1},
    )
    assert command.task_name == "Daily"
    assert command.prompt == "  hello\nworld  "


@pytest.mark.asyncio
async def test_active_expired_once_allows_non_schedule_edit() -> None:
    class FakeRepository:
        def __init__(self) -> None:
            self.task = SimpleNamespace(
                id=1,
                status="active",
                schedule_type="once",
                schedule_config={"type": "once", "run_at": "2026-07-17T05:00:00+00:00"},
                cron_expr=None,
            )
            self.session = SimpleNamespace(id=42, runtime_session_id="rs-1-a")
            self.values = None

        async def get_model(self, **kwargs):
            return self.task, self.session

        async def assert_target_session_eligible(self, session):
            assert session is self.session

        async def update(self, *, task, session, **values):
            self.values = values
            return values

    repository = FakeRepository()
    service = ScheduledTaskService(repository, clock=lambda: NOW.replace(hour=6))
    command = ScheduledTaskCommand(
        task_name="Renamed",
        prompt="still the same plan",
        target_runtime_session_id="rs-1-a",
        schedule_config=repository.task.schedule_config,
    )
    result = await service.update_task(
        principal=Principal(org_id=1, user_id=2), runtime_workspace_id=3, task_id=1, command=command
    )
    assert result["task_name"] == "Renamed"
    assert "next_run_at" not in repository.values
