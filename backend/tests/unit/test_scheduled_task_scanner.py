import asyncio
from datetime import datetime, timezone

import pytest

from app.repositories.scheduled_task import ScheduledTaskRecord
from app.services.scheduled_task_launcher import ScheduledTaskLauncher
from app.services.scheduled_task_scanner import DueTaskScanner


def _record(task_id: int) -> ScheduledTaskRecord:
    return ScheduledTaskRecord(
        task_id=task_id,
        org_id=1,
        user_id=1,
        runtime_workspace_id=10,
        target_session_id=11,
        target_runtime_session_id=f"rs-{task_id}",
        target_session_title="Chat",
        target_session_status="active",
        task_name="demo",
        prompt="hello",
        schedule_type="once",
        schedule_config={"type": "once"},
        cron_expr=None,
        timezone="Asia/Shanghai",
        status="active",
        next_run_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_run_at=None,
        last_run_status=None,
        last_error_code=None,
        last_error=None,
        last_runtime_run_id=None,
        run_count=0,
        created_at=None,
        updated_at=None,
    )


@pytest.mark.asyncio
async def test_scanner_launches_all_due_tasks_without_waiting():
    calls = []

    async def runner(request):
        calls.append(request.task_id)
        await asyncio.sleep(0)

    launcher = ScheduledTaskLauncher(runner)
    scanner = DueTaskScanner(
        launcher,
        fetch_due=lambda _now: [_record(1), _record(2)],
    )
    await scanner.scan_once()
    assert calls == []
    await asyncio.sleep(0)
    assert sorted(calls) == [1, 2]


@pytest.mark.asyncio
async def test_scanner_exception_isolated_to_next_round():
    rounds = 0

    async def fetch(_now):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            raise RuntimeError("db unavailable")
        return []

    scanner = DueTaskScanner(ScheduledTaskLauncher(lambda _request: asyncio.sleep(0)), fetch_due=fetch)
    await scanner.scan_once()
    await scanner.scan_once()
    assert rounds == 2
