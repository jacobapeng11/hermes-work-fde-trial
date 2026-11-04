"""Cold-start phase timing for Supervisor startup attribution.

The collector is intentionally dependency-free and fail-open: it must never
block or fail the Runtime startup path. Phase names are stable identifiers and
log output must not contain configuration, credentials, paths or user data.
"""

from __future__ import annotations

import json
import logging
from threading import Lock
import sys
import time
from typing import Any


_BASELINE = time.monotonic()
_lock = Lock()
_marks: list[tuple[str, float]] = []
_handler_configured = False


def mark(name: str, *, overwrite: bool = False) -> None:
    """Record one startup phase boundary.

    ``overwrite`` is opt-in for lifecycle boundaries that can legitimately be
    reached more than once (for example test-driven resets); normal startup
    keeps the first observation of a phase.
    """

    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError("startup timing phase name must be a stable identifier")
    now = time.monotonic()
    with _lock:
        if not overwrite:
            if any(existing == name for existing, _ in _marks):
                return
        else:
            _marks[:] = [item for item in _marks if item[0] != name]
        _marks.append((name, now))


def phases() -> dict[str, Any]:
    """Return per-phase durations and cumulative offsets from process baseline."""

    with _lock:
        snapshot = list(_marks)
    result: dict[str, Any] = {
        "phase_count": len(snapshot),
        "phases": {},
    }
    previous = _BASELINE
    for name, observed in snapshot:
        result["phases"][name] = {
            "since_previous_ms": round((observed - previous) * 1000, 1),
            "since_process_start_ms": round((observed - _BASELINE) * 1000, 1),
        }
        previous = observed
    return result


def summary(*, mode: str) -> dict[str, Any]:
    with _lock:
        snapshot = list(_marks)
    return {
        "event": "supervisor_startup_phases",
        "startup_mode": mode,
        "startup_total_ms": (
            round((snapshot[-1][1] - _BASELINE) * 1000, 1) if snapshot else None
        ),
        **phases(),
    }


def log_summary(*, mode: str) -> None:
    """Emit the collected phases as one structured, secret-free log line."""

    _ensure_summary_handler()
    logging.getLogger("platform_supervisor.startup_timing").info(
        json.dumps(summary(mode=mode), sort_keys=True)
    )


def _ensure_summary_handler() -> None:
    """Guarantee delivery under uvicorn's default logger configuration."""

    global _handler_configured
    if _handler_configured:
        return
    startup_logger = logging.getLogger("platform_supervisor.startup_timing")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    startup_logger.addHandler(handler)
    startup_logger.setLevel(logging.INFO)
    startup_logger.propagate = False
    _handler_configured = True


def reset_for_tests() -> None:
    with _lock:
        _marks.clear()
