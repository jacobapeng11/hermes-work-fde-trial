from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.schemas.scheduled_task import ScheduledTaskCommand
from app.services.runtime_workspace import Principal


def _record(**overrides):
    from app.repositories.scheduled_task import ScheduledTaskRecord

    values = dict(
        task_id=1,
        org_id=1,
        user_id=1,
        runtime_workspace_id=10,
        target_session_id=11,
        target_runtime_session_id="rs-10-1",
        target_session_title="Chat",
        target_session_status="active",
        task_name="Daily",
        prompt="hello",
        schedule_type="interval",
        schedule_config={"type": "interval", "unit": "hours", "value": 1},
        cron_expr=None,
        timezone="Asia/Shanghai",
        status="active",
        next_run_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        last_run_at=None,
        last_run_status=None,
        last_error_code=None,
        last_error=None,
        last_runtime_run_id=None,
        run_count=0,
        created_at=None,
        updated_at=None,
        is_running=False,
    )
    values.update(overrides)
    return ScheduledTaskRecord(**values)


class FakeService:
    def __init__(self):
        self.record = _record()
        self.calls = []

    async def list_tasks(self, *, principal, runtime_workspace_id):
        self.calls.append(("list", principal, runtime_workspace_id))
        return [self.record]

    async def create_task(self, *, principal, runtime_workspace_id, command):
        self.calls.append(("create", principal, runtime_workspace_id, command))
        return self.record

    async def update_task(self, *, principal, runtime_workspace_id, task_id, command):
        self.calls.append(("update", principal, runtime_workspace_id, task_id, command))
        return self.record

    async def get_task(self, *, principal, runtime_workspace_id, task_id):
        self.calls.append(("get", principal, runtime_workspace_id, task_id))
        return self.record

    async def pause_task(self, *, principal, runtime_workspace_id, task_id):
        self.calls.append(("pause", principal, runtime_workspace_id, task_id))
        return self.record

    async def resume_task(self, *, principal, runtime_workspace_id, task_id, now):
        self.calls.append(("resume", principal, runtime_workspace_id, task_id, now))
        return self.record

    async def delete_task(self, *, principal, runtime_workspace_id, task_id):
        self.calls.append(("delete", principal, runtime_workspace_id, task_id))


@pytest.mark.asyncio
async def test_scheduled_task_routes_cover_crud_without_runtime_keepalive(
    monkeypatch,
):
    from app.api.v1.user import scheduled_tasks as module
    from app.api.v1.user.scheduled_tasks import (
        create_scheduled_task,
        delete_scheduled_task,
        list_scheduled_tasks,
        pause_scheduled_task,
        resume_scheduled_task,
        update_scheduled_task,
    )

    service = FakeService()
    cancellations: list[dict] = []

    async def cancel_candidate(**kwargs):
        cancellations.append(kwargs)
        return "canceled"

    monkeypatch.setattr(module, "_cancel_queued_candidate", cancel_candidate)
    principal = Principal(org_id=1, user_id=1)
    command = ScheduledTaskCommand(
        task_name="Daily",
        prompt="hello",
        target_runtime_session_id="rs-10-1",
        schedule_config={"type": "interval", "unit": "hours", "value": 1},
    )
    assert (await list_scheduled_tasks(10, principal, service)).tasks[0].task_id == 1
    assert (await create_scheduled_task(10, command, principal, service)).task_id == 1
    assert (await update_scheduled_task(10, 1, command, principal, service)).task_id == 1
    assert (await pause_scheduled_task(10, 1, principal, service)).task_id == 1
    assert (await resume_scheduled_task(10, 1, principal, service)).task_id == 1
    response = await delete_scheduled_task(10, 1, principal, service)
    assert response.status_code == 204
    assert [call[0] for call in service.calls] == [
        "list",
        "create",
        "update",
        "pause",
        "resume",
        "get",
        "delete",
    ]
    assert [item["task_id"] for item in cancellations] == [1, 1]
    assert all(
        item["runtime_session_id"] == "rs-10-1"
        for item in cancellations
    )


def test_scheduled_task_summary_serializes_utc_instants_with_z() -> None:
    from app.api.v1.user.scheduled_tasks import _summary

    payload = _summary(
        _record(
            last_run_at=datetime(2026, 7, 18, 0, 50, tzinfo=timezone.utc),
            created_at=datetime(2026, 7, 17, 16, 50, tzinfo=timezone.utc),
            updated_at=datetime(2026, 7, 18, 0, 50, tzinfo=timezone.utc),
        )
    ).model_dump_json()

    assert '"last_run_at":"2026-07-18T00:50:00Z"' in payload
    assert '"created_at":"2026-07-17T16:50:00Z"' in payload


@pytest.mark.asyncio
async def test_scheduled_task_api_maps_missing_resource_to_404():
    from app.api.v1.user.scheduled_tasks import get_scheduled_task
    from app.repositories.scheduled_task import ScheduledTaskNotFoundError

    class MissingService(FakeService):
        async def get_task(self, **_kwargs):
            raise ScheduledTaskNotFoundError("not found")

    with pytest.raises(HTTPException) as error:
        await get_scheduled_task(10, 1, Principal(org_id=1, user_id=1), MissingService())
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_create_maps_unconfirmed_wecom_target_to_stable_conflict_code():
    from app.api.v1.user.scheduled_tasks import create_scheduled_task
    from app.repositories.scheduled_task import (
        ScheduledTaskWeComTargetNotReadyError,
    )

    class UnconfirmedWeComService(FakeService):
        async def create_task(self, **_kwargs):
            raise ScheduledTaskWeComTargetNotReadyError()

    with pytest.raises(HTTPException) as error:
        await create_scheduled_task(
            10,
            SimpleNamespace(),
            Principal(org_id=1, user_id=1),
            UnconfirmedWeComService(),
        )
    assert error.value.status_code == 409
    assert error.value.detail == "SCHEDULED_TASK_WECOM_TARGET_NOT_READY"


@pytest.mark.asyncio
async def test_trigger_returns_202_and_conflict_when_already_running():
    from app.api.v1.user.scheduled_tasks import trigger_scheduled_task
    from app.services.scheduled_task_launcher import ScheduledTaskLauncher

    class TriggerService(FakeService):
        async def trigger_task(self, **kwargs):
            launcher = kwargs["launcher"]
            record = self.record
            from app.services.scheduled_task_launcher import RunnerRequest, TaskSnapshot

            request = RunnerRequest(
                record.task_id,
                "manual",
                None,
                TaskSnapshot.from_record(record),
            )
            if not launcher.launch(request):
                from app.services.scheduled_task_service import ScheduledTaskTriggerConflictError

                raise ScheduledTaskTriggerConflictError("already running")
            return record

    async def runner(_request):
        await asyncio.Event().wait()

    import asyncio

    launcher = ScheduledTaskLauncher(runner)
    service = TriggerService()
    response = await trigger_scheduled_task(10, 1, Principal(org_id=1, user_id=1), service, launcher)
    assert response.task_id == 1
    with pytest.raises(HTTPException) as error:
        await trigger_scheduled_task(10, 1, Principal(org_id=1, user_id=1), service, launcher)
    assert error.value.status_code == 409
    await launcher.stop()


@pytest.mark.asyncio
async def test_trigger_maps_runtime_maintenance_to_503():
    from app.api.v1.user.scheduled_tasks import trigger_scheduled_task
    from app.services.scheduled_task_service import (
        ScheduledTaskTriggerMaintenanceError,
    )

    class MaintenanceService(FakeService):
        async def trigger_task(self, **_kwargs):
            raise ScheduledTaskTriggerMaintenanceError

    with pytest.raises(HTTPException) as error:
        await trigger_scheduled_task(
            10,
            1,
            Principal(org_id=1, user_id=1),
            MaintenanceService(),
            SimpleNamespace(),
        )

    assert error.value.status_code == 503
    assert error.value.detail == "系统维护中，请稍后重试"
