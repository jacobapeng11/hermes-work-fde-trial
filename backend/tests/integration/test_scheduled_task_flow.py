"""Deterministic scheduled-task flow gate.

This intentionally uses the production control-plane, launcher and Runner
protocol seams with an in-memory store.  It is not a replacement for the
staging smoke script: its job is to make the API/service hand-off repeatable
without waiting for the ten-second Scanner interval or contacting a Runtime.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import pytest
from fastapi import HTTPException

from app.api.v1.user.scheduled_tasks import trigger_scheduled_task
from app.repositories.scheduled_task import (
    ScheduledTaskNotFoundError,
    ScheduledTaskRecord,
    ScheduledTaskTargetNotFoundError,
)
from app.schemas.scheduled_task import ScheduledTaskCommand
from app.services.runtime_chat import CreatedRuntimeRun
from app.services.runtime_workspace import Principal
from app.services.scheduled_task_launcher import RunnerRequest, ScheduledTaskLauncher, TaskSnapshot
from app.services.scheduled_task_runner import RuntimeRunTerminal, ScheduledTaskRunner
from app.services.scheduled_task_service import ScheduledTaskService


NOW = datetime(2026, 7, 17, 4, 0, tzinfo=timezone.utc)
OWNER = Principal(org_id=1, user_id=10)
OTHER_OWNER = Principal(org_id=1, user_id=11)
WORKSPACE_ID = 7


@dataclass
class _Session:
    id: int
    org_id: int
    user_id: int
    runtime_workspace_id: int
    runtime_session_id: str
    status: str = "active"
    title: str | None = "集成测试对话"


class InMemoryScheduledTaskStore:
    """Small substitute for the repository/session boundary used by this gate."""

    def __init__(self) -> None:
        self.sessions = {
            1: _Session(1, 1, 10, WORKSPACE_ID, "session-owner"),
            2: _Session(2, 1, 11, WORKSPACE_ID, "session-other"),
        }
        self.records: dict[int, ScheduledTaskRecord] = {}
        self._next_id = 1

    def _session(self, *, org_id: int, user_id: int, runtime_workspace_id: int, runtime_session_id: str) -> _Session:
        for session in self.sessions.values():
            if (
                session.org_id == org_id
                and session.user_id == user_id
                and session.runtime_workspace_id == runtime_workspace_id
                and session.runtime_session_id == runtime_session_id
                and session.status == "active"
            ):
                return session
        raise ScheduledTaskTargetNotFoundError("runtime session not found")

    async def list(self, *, org_id: int, user_id: int, runtime_workspace_id: int) -> list[ScheduledTaskRecord]:
        if not any(
            session.org_id == org_id and session.user_id == user_id and session.runtime_workspace_id == runtime_workspace_id
            for session in self.sessions.values()
        ):
            raise ScheduledTaskTargetNotFoundError("runtime workspace not found")
        return [
            record
            for record in self.records.values()
            if record.org_id == org_id
            and record.user_id == user_id
            and record.runtime_workspace_id == runtime_workspace_id
            and record.status != "deleted"
        ]

    async def get(self, *, org_id: int, user_id: int, runtime_workspace_id: int, task_id: int) -> ScheduledTaskRecord:
        record = self.records.get(task_id)
        if (
            record is None
            or record.org_id != org_id
            or record.user_id != user_id
            or record.runtime_workspace_id != runtime_workspace_id
            or record.status == "deleted"
        ):
            raise ScheduledTaskNotFoundError("scheduled task not found")
        session = self.sessions[record.target_session_id]
        return replace(record, target_session_status=session.status, target_session_title=session.title)

    async def get_model(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        task_id: int,
    ) -> tuple[ScheduledTaskRecord, _Session]:
        record = await self.get(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            task_id=task_id,
        )
        return record, self.sessions[record.target_session_id]

    async def assert_target_session_eligible(self, _session: _Session) -> None:
        return None

    async def create(self, *, org_id: int, user_id: int, runtime_workspace_id: int, target_runtime_session_id: str, values: dict) -> ScheduledTaskRecord:
        session = self._session(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=target_runtime_session_id,
        )
        task_id = self._next_id
        self._next_id += 1
        record = ScheduledTaskRecord(
            task_id=task_id,
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            target_session_id=session.id,
            target_runtime_session_id=session.runtime_session_id,
            target_session_title=session.title,
            target_session_status=session.status,
            task_name=values["task_name"],
            prompt=values["prompt"],
            schedule_type=values["schedule_type"],
            schedule_config=dict(values["schedule_config"]),
            cron_expr=values["cron_expr"],
            timezone=values["timezone"],
            status=values["status"],
            next_run_at=values["next_run_at"],
            last_run_at=None,
            last_run_status=None,
            last_error_code=None,
            last_error=None,
            last_runtime_run_id=None,
            run_count=0,
            created_at=NOW,
            updated_at=NOW,
        )
        self.records[task_id] = record
        return record

    async def finalize_run_summary(self, *, task_id: int, org_id: int, user_id: int, runtime_workspace_id: int, **values: object) -> bool:
        record = await self.get(
            org_id=org_id, user_id=user_id, runtime_workspace_id=runtime_workspace_id, task_id=task_id
        )
        self.records[task_id] = replace(
            record,
            last_run_at=values["last_run_at"],
            last_run_status=str(values["last_run_status"]),
            last_error_code=values["last_error_code"],
            last_error=values["last_error"],
            last_runtime_run_id=values["last_runtime_run_id"],
            run_count=record.run_count + int(values.get("increment_run_count", 0)),
        )
        return True

    async def advance_after_run(self, *, task_id: int, org_id: int, user_id: int, runtime_workspace_id: int, scheduled_fire_at: datetime, status: str, next_run_at: datetime | None) -> bool:
        record = await self.get(
            org_id=org_id, user_id=user_id, runtime_workspace_id=runtime_workspace_id, task_id=task_id
        )
        if record.status != "active" or record.next_run_at != scheduled_fire_at:
            return False
        self.records[task_id] = replace(record, status=status, next_run_at=next_run_at)
        return True

    async def pause_for_session(self, *, org_id: int, user_id: int, runtime_workspace_id: int, session_id: int) -> int:
        paused = 0
        for task_id, record in list(self.records.items()):
            if (
                record.org_id == org_id
                and record.user_id == user_id
                and record.runtime_workspace_id == runtime_workspace_id
                and record.target_session_id == session_id
                and record.status != "deleted"
            ):
                self.records[task_id] = replace(
                    record,
                    status="paused",
                    next_run_at=None,
                    last_error_code="SESSION_DELETED",
                    last_error="The target session was deleted; choose another session before resuming.",
                )
                paused += 1
        return paused

    async def mark_session_deleting(self, *, org_id: int, user_id: int, runtime_workspace_id: int, runtime_session_id: str) -> None:
        session = self._session(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
        await self.pause_for_session(
            org_id=org_id, user_id=user_id, runtime_workspace_id=runtime_workspace_id, session_id=session.id
        )
        session.status = "deleting"


@pytest.fixture
def scheduled_fixture() -> tuple[InMemoryScheduledTaskStore, ScheduledTaskService]:
    store = InMemoryScheduledTaskStore()
    return store, ScheduledTaskService(store, clock=lambda: NOW)


def _command(*, session_id: str = "session-owner") -> ScheduledTaskCommand:
    return ScheduledTaskCommand(
        task_name="每日工作总结",
        prompt="请生成今天的工作总结",
        target_runtime_session_id=session_id,
        schedule_config={"type": "once", "run_at": (NOW + timedelta(minutes=5)).isoformat()},
    )


@pytest.mark.asyncio
async def test_once_flow_finalizes_and_projects_only_frozen_source(scheduled_fixture) -> None:
    store, service = scheduled_fixture
    task = await service.create_task(principal=OWNER, runtime_workspace_id=WORKSPACE_ID, command=_command())
    history: list[dict] = []

    async def source_preparer(_principal, _created, snapshot, trigger_kind):
        history.append({
            "role": "user",
            "content": snapshot.prompt,
            "source": {
                "type": "scheduled",
                "scheduled_task_id": snapshot.task_id,
                "scheduled_task_name": snapshot.task_name,
                "trigger_kind": trigger_kind,
            },
        })

    runner = ScheduledTaskRunner(
        task_loader=lambda request: store.get(
            org_id=OWNER.org_id, user_id=OWNER.user_id, runtime_workspace_id=WORKSPACE_ID, task_id=request.task_id
        ),
        runtime_activator=lambda _principal, _snapshot: "https://runtime.example.invalid",
        run_creator=lambda _principal, snapshot, _endpoint: CreatedRuntimeRun(
            WORKSPACE_ID, snapshot.target_runtime_session_id, "run-once", None, "accepted"
        ),
        source_preparer=source_preparer,
        run_executor=lambda created, _snapshot, _endpoint: RuntimeRunTerminal(created.runtime_run_id, "completed"),
        task_updater=lambda request, terminal: service.finalize_run(request=request, terminal=terminal),
        clock=lambda: NOW + timedelta(minutes=6),
    )
    request = RunnerRequest(task.task_id, "scheduled", task.next_run_at, TaskSnapshot.from_record(task))
    await runner.run(request)

    completed = await service.get_task(principal=OWNER, runtime_workspace_id=WORKSPACE_ID, task_id=task.task_id)
    assert completed.status == "completed"
    assert completed.next_run_at is None
    assert completed.last_run_status == "success"
    assert completed.last_runtime_run_id == "run-once"
    assert completed.run_count == 1
    assert history == [{
        "role": "user",
        "content": "请生成今天的工作总结",
        "source": {
            "type": "scheduled",
            "scheduled_task_id": task.task_id,
            "scheduled_task_name": "每日工作总结",
            "trigger_kind": "scheduled",
        },
    }]
    assert "runtime_workspace_id" not in history[0]["source"]


@pytest.mark.asyncio
async def test_owner_isolation_and_session_delete_pauses_only_its_tasks(scheduled_fixture) -> None:
    store, service = scheduled_fixture
    own_task = await service.create_task(principal=OWNER, runtime_workspace_id=WORKSPACE_ID, command=_command())
    other_task = await service.create_task(
        principal=OTHER_OWNER, runtime_workspace_id=WORKSPACE_ID, command=_command(session_id="session-other")
    )

    with pytest.raises(ScheduledTaskNotFoundError):
        await service.get_task(principal=OTHER_OWNER, runtime_workspace_id=WORKSPACE_ID, task_id=own_task.task_id)

    await store.mark_session_deleting(
        org_id=OWNER.org_id,
        user_id=OWNER.user_id,
        runtime_workspace_id=WORKSPACE_ID,
        runtime_session_id="session-owner",
    )
    paused = await service.get_task(principal=OWNER, runtime_workspace_id=WORKSPACE_ID, task_id=own_task.task_id)
    untouched = await service.get_task(principal=OTHER_OWNER, runtime_workspace_id=WORKSPACE_ID, task_id=other_task.task_id)
    assert (paused.status, paused.next_run_at, paused.last_error_code) == ("paused", None, "SESSION_DELETED")
    assert untouched.status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "completed"])
async def test_manual_terminal_summary_never_moves_paused_or_completed_plan(scheduled_fixture, status: str) -> None:
    store, service = scheduled_fixture
    task = await service.create_task(principal=OWNER, runtime_workspace_id=WORKSPACE_ID, command=_command())
    planned_next = task.next_run_at
    store.records[task.task_id] = replace(task, status=status, next_run_at=None if status == "paused" else planned_next)
    current = await service.get_task(principal=OWNER, runtime_workspace_id=WORKSPACE_ID, task_id=task.task_id)
    request = RunnerRequest(task.task_id, "manual", None, TaskSnapshot.from_record(current))
    await service.finalize_run(request=request, terminal=RuntimeRunTerminal("manual-run", "completed"))

    after = await service.get_task(principal=OWNER, runtime_workspace_id=WORKSPACE_ID, task_id=task.task_id)
    assert after.status == status
    assert after.next_run_at == current.next_run_at
    assert after.last_run_status == "success"
    assert after.run_count == 1


@pytest.mark.asyncio
async def test_double_manual_trigger_maps_second_request_to_409(scheduled_fixture) -> None:
    _store, service = scheduled_fixture
    task = await service.create_task(principal=OWNER, runtime_workspace_id=WORKSPACE_ID, command=_command())
    running = asyncio.Event()

    async def block(_request: RunnerRequest) -> None:
        running.set()
        await asyncio.Event().wait()

    launcher = ScheduledTaskLauncher(block)
    first = await trigger_scheduled_task(WORKSPACE_ID, task.task_id, OWNER, service, launcher)
    assert first.task_id == task.task_id
    await running.wait()
    with pytest.raises(HTTPException) as error:
        await trigger_scheduled_task(WORKSPACE_ID, task.task_id, OWNER, service, launcher)
    assert error.value.status_code == 409
    await launcher.stop()
