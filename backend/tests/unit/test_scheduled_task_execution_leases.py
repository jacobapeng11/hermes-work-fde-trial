from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    ScheduledTask,
    ScheduledTaskExecutionLease,
)
from app.services.scheduled_task_execution_leases import (
    ScheduledTaskExecutionGrant,
    ScheduledTaskExecutionMaintenanceError,
    ScheduledTaskExecutionLeaseStore,
)
from app.services.runtime_maintenance import RuntimeMaintenanceError
from app.services.scheduled_task_launcher import (
    RunnerRequest,
    TaskSnapshot,
)


class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _LeaseSession:
    def __init__(
        self, lease: ScheduledTaskExecutionLease | None
    ) -> None:
        self.lease = lease
        self.added = []
        self.flush_count = 0

    async def execute(self, _statement):
        return _ScalarResult(self.lease)

    def add(self, value) -> None:
        self.added.append(value)
        self.lease = value

    async def flush(self) -> None:
        self.flush_count += 1


class _MaintenanceSession:
    def __init__(self) -> None:
        self.execute_count = 0
        self.rollback_count = 0

    async def execute(self, _statement):
        self.execute_count += 1
        raise AssertionError("task rows must not be queried during maintenance")

    async def rollback(self) -> None:
        self.rollback_count += 1


def _task() -> ScheduledTask:
    return ScheduledTask(
        id=7,
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        target_session_id=11,
        task_name="lease test",
        prompt="run once",
        schedule_type="once",
        schedule_config={"type": "once"},
        timezone="Asia/Shanghai",
        status="active",
    )


@pytest.mark.asyncio
async def test_claim_fences_live_owner_and_preserves_run_on_takeover() -> None:
    fire_at = datetime(2026, 7, 29, 1, tzinfo=timezone.utc)
    existing = ScheduledTaskExecutionLease(
        task_id=7,
        claim_token="old-token",
        owner_instance_id="backend-a",
        lease_epoch=4,
        trigger_kind="scheduled",
        scheduled_fire_at=fire_at,
        status="running",
        lease_expires_at=fire_at + timedelta(seconds=90),
        heartbeat_at=fire_at,
        runtime_run_id="run-existing",
    )
    session = _LeaseSession(existing)
    store = ScheduledTaskExecutionLeaseStore(
        session,  # type: ignore[arg-type]
        owner_instance_id="backend-b",
        lease_ttl_seconds=90,
    )

    blocked = await store._claim_locked(
        task=_task(),
        trigger_kind="scheduled",
        scheduled_fire_at=fire_at,
        now=fire_at + timedelta(seconds=1),
    )
    assert blocked is None

    takeover = await store._claim_locked(
        task=_task(),
        trigger_kind="scheduled",
        scheduled_fire_at=fire_at,
        now=fire_at + timedelta(seconds=91),
    )
    assert takeover is not None
    assert takeover.owner_instance_id == "backend-b"
    assert takeover.lease_epoch == 5
    assert takeover.runtime_run_id == "run-existing"
    assert existing.claim_token == takeover.claim_token
    assert existing.status == "claimed"


@pytest.mark.asyncio
async def test_due_claims_are_skipped_while_runtime_maintenance_is_frozen(
    monkeypatch,
) -> None:
    from app.services import scheduled_task_execution_leases as module

    class FrozenMaintenance:
        def __init__(self, _db) -> None:
            pass

        async def require_admission_open(self, *, lock: bool):
            assert lock is True
            raise RuntimeMaintenanceError(
                "MAINTENANCE_ADMISSION_FROZEN",
                retryable=True,
            )

    session = _MaintenanceSession()
    monkeypatch.setattr(
        module,
        "RuntimeMaintenanceService",
        FrozenMaintenance,
    )
    store = ScheduledTaskExecutionLeaseStore(
        session,  # type: ignore[arg-type]
        owner_instance_id="backend-a",
        lease_ttl_seconds=90,
    )

    claimed = await store.claim_due(
        now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        limit=10,
    )

    assert claimed == []
    assert session.execute_count == 0
    assert session.rollback_count == 1


@pytest.mark.asyncio
async def test_manual_claim_is_rejected_while_runtime_maintenance_is_frozen(
    monkeypatch,
) -> None:
    from app.services import scheduled_task_execution_leases as module

    class FrozenMaintenance:
        def __init__(self, _db) -> None:
            pass

        async def require_admission_open(self, *, lock: bool):
            assert lock is True
            raise RuntimeMaintenanceError(
                "MAINTENANCE_ADMISSION_FROZEN",
                retryable=True,
            )

    session = _MaintenanceSession()
    monkeypatch.setattr(
        module,
        "RuntimeMaintenanceService",
        FrozenMaintenance,
    )
    store = ScheduledTaskExecutionLeaseStore(
        session,  # type: ignore[arg-type]
        owner_instance_id="backend-a",
        lease_ttl_seconds=90,
    )

    with pytest.raises(ScheduledTaskExecutionMaintenanceError):
        await store.claim_manual(
            task_id=7,
            org_id=1,
            user_id=2,
            runtime_workspace_id=3,
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )

    assert session.execute_count == 0
    assert session.rollback_count == 1


def test_execution_lease_model_and_runner_request_carry_fencing_identity() -> None:
    table = ScheduledTaskExecutionLease.__table__
    assert table.primary_key.columns.keys() == ["task_id"]
    assert {
        "claim_token",
        "owner_instance_id",
        "lease_epoch",
        "lease_expires_at",
        "runtime_run_id",
    }.issubset(table.columns.keys())

    fire_at = datetime(2026, 7, 29, 1, tzinfo=timezone.utc)
    grant = ScheduledTaskExecutionGrant(
        task_id=7,
        claim_token="token",
        owner_instance_id="backend-a",
        lease_epoch=3,
        trigger_kind="scheduled",
        scheduled_fire_at=fire_at,
        runtime_run_id="run-existing",
    )
    snapshot = TaskSnapshot(
        task_id=7,
        task_name="test",
        prompt="hello",
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        target_session_id=4,
        target_runtime_session_id="rs-1",
        target_session_status="active",
        status="active",
        schedule_type="once",
        schedule_config={"type": "once"},
        next_run_at=fire_at,
    )
    request = RunnerRequest.with_execution_grant(
        task_id=7,
        trigger_kind="scheduled",
        scheduled_fire_at=fire_at,
        task_snapshot=snapshot,
        grant=grant,
    )
    assert request.execution_grant() == grant
