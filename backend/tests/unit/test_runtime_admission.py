from __future__ import annotations

import pytest

from app.services.runtime_admission import (
    RuntimeAdmissionError,
    RuntimeAdmissionService,
)
from app.services.runtime_maintenance import RuntimeMaintenanceError


class UpdateResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class AdmissionSession:
    def __init__(self, rowcounts: list[int]) -> None:
        self.rowcounts = list(rowcounts)
        self.statements: list[object] = []
        self.commits = 0

    async def execute(self, statement: object) -> UpdateResult:
        self.statements.append(statement)
        return UpdateResult(self.rowcounts.pop(0))

    async def commit(self) -> None:
        self.commits += 1


class OpenMaintenanceControl:
    def __init__(self) -> None:
        self.lock_requests: list[bool] = []

    async def require_admission_open(self, *, lock: bool = False) -> None:
        self.lock_requests.append(lock)
        return None


class FrozenMaintenanceControl:
    async def require_admission_open(self, *, lock: bool = False) -> None:
        del lock
        raise RuntimeMaintenanceError(
            "MAINTENANCE_ADMISSION_FROZEN",
            retryable=True,
        )


@pytest.mark.parametrize(
    ("status", "allowed", "retryable"),
    [
        ("ready", True, False),
        ("force_stopped", False, False),
        ("force_stop_requested", False, False),
        ("force_stop_failed", False, False),
        ("restart_requested", False, True),
        ("starting", False, True),
        ("failed", False, True),
        ("scale_to_zero_failed", False, True),
    ],
)
def test_runtime_admission_matrix(
    status: str,
    allowed: bool,
    retryable: bool,
) -> None:
    decision = RuntimeAdmissionService.decide(status)

    assert decision.allowed is allowed
    assert decision.retryable is retryable
    assert decision.code.startswith("RUNTIME_")


def test_runtime_admission_require_ready_carries_decision() -> None:
    with pytest.raises(RuntimeAdmissionError) as error:
        RuntimeAdmissionService.require_ready("restart_requested")

    assert error.value.decision.allowed is False
    assert error.value.decision.retryable is True
    assert error.value.decision.code == "RUNTIME_RESTARTING"


async def test_only_one_controller_acquires_reconcile_ownership() -> None:
    session = AdmissionSession([1, 0])
    control = OpenMaintenanceControl()
    admission = RuntimeAdmissionService(session, maintenance_control=control)

    first = await admission.begin_reconcile(7, "restart_requested")
    second = await admission.begin_reconcile(7, "restart_requested")

    assert first is True
    assert second is False
    assert control.lock_requests == [True, True]
    assert session.commits == 2
    sql = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "runtime_workspace_id = 7" in sql
    assert "status IN ('ready', 'failed')" in sql
    assert "status='restart_requested'" in sql.replace(" ", "")


async def test_reconcile_ownership_can_atomically_record_target_release() -> None:
    session = AdmissionSession([1])

    acquired = await RuntimeAdmissionService(
        session,
        maintenance_control=OpenMaintenanceControl(),
    ).begin_reconcile(7, "starting", target_image="runtime:0.19")

    assert acquired is True
    sql = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    compact = sql.replace(" ", "")
    assert "image='runtime:0.19'" in compact


async def test_maintenance_gate_blocks_reconcile_before_binding_update() -> None:
    session = AdmissionSession([1])

    with pytest.raises(RuntimeAdmissionError) as error:
        await RuntimeAdmissionService(
            session,
            maintenance_control=FrozenMaintenanceControl(),
        ).begin_reconcile(7, "starting")

    assert error.value.decision.code == "MAINTENANCE_ADMISSION_FROZEN"
    assert error.value.decision.retryable is True
    assert session.statements == []
    assert session.commits == 0


async def test_reconcile_target_status_is_restricted() -> None:
    session = AdmissionSession([])

    with pytest.raises(ValueError, match="target status"):
        await RuntimeAdmissionService(session).begin_reconcile(7, "ready")

    assert session.statements == []
