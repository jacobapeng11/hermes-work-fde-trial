from __future__ import annotations

from platform_supervisor.health import RuntimeHealth, RuntimeState


def test_target_health_keeps_execution_and_admission_separate() -> None:
    health = RuntimeHealth(require_admission_open=True)
    health.mark_control_ready()
    health.mark_store_state("migrated")
    health.mark_hermes_ready()

    assert health.state == RuntimeState.HERMES_READY
    assert health.execution_ready is True
    assert health.admission_open is False
    assert health.ready is False

    health.mark_admission_open(True)

    assert health.ready is True
    assert health.public_snapshot() == {
        "state": "HERMES_READY",
        "ready": True,
        "control_ready": True,
        "execution_ready": True,
        "admission_open": True,
        "store_state": "migrated",
        "error_code": None,
    }


def test_legacy_health_remains_ready_after_hermes_start() -> None:
    health = RuntimeHealth()

    health.mark_hermes_ready()

    assert health.ready is True


def test_failed_target_stays_control_ready_for_recovery() -> None:
    health = RuntimeHealth(require_admission_open=True)
    health.mark_control_ready()
    health.mark_store_state("failed")
    health.mark_failed("INTENT_INVALID")

    assert health.control_ready is True
    assert health.execution_ready is False
    assert health.ready is False
