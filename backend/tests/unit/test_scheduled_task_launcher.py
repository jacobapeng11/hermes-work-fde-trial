import asyncio
from datetime import datetime, timezone

import pytest

from app.repositories.scheduled_task import ScheduledTaskRecord
from app.services.scheduled_task_launcher import (
    RunnerRequest,
    ScheduledTaskLauncher,
    TaskSnapshot,
)


def _request(task_id: int = 1) -> RunnerRequest:
    snapshot = TaskSnapshot(
        task_id=task_id,
        task_name="demo",
        prompt="hello",
        org_id=1,
        user_id=1,
        runtime_workspace_id=10,
        target_session_id=11,
        target_runtime_session_id="rs-1",
        target_session_status="active",
        status="active",
        schedule_type="once",
        schedule_config={"type": "once"},
        next_run_at=None,
    )
    return RunnerRequest(task_id, "manual", None, snapshot)


@pytest.mark.asyncio
async def test_launcher_deduplicates_and_stop_clears_registry():
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(_request):
        started.set()
        await release.wait()

    launcher = ScheduledTaskLauncher(runner)
    assert launcher.launch(_request()) is True
    assert launcher.launch(_request()) is False
    await started.wait()
    await launcher.stop()
    assert launcher.running_tasks == {}
    release.set()


@pytest.mark.asyncio
async def test_launcher_done_callback_removes_only_same_task():
    async def runner(_request):
        return None

    launcher = ScheduledTaskLauncher(runner)
    assert launcher.launch(_request())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert launcher.running_tasks == {}
