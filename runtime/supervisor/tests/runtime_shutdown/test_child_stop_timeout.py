from __future__ import annotations

from platform_supervisor.main import (
    IDLE_SHUTDOWN_CHILD_STOP_SECONDS,
    shutdown_child_stop_timeout,
)


def test_idle_shutdown_uses_fast_child_stop() -> None:
    assert (
        shutdown_child_stop_timeout(drained=True, active_runs=0)
        == IDLE_SHUTDOWN_CHILD_STOP_SECONDS
    )


def test_active_runs_keep_long_drain_timeout() -> None:
    assert shutdown_child_stop_timeout(drained=True, active_runs=1) == 30.0


def test_undrained_capacity_keeps_long_timeout() -> None:
    assert shutdown_child_stop_timeout(drained=False, active_runs=0) == 30.0
