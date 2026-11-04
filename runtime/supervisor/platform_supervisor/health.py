from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import resource
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from platform_supervisor.storage.lifecycle import StorageSnapshot


GIB = 1024 * 1024 * 1024


class RuntimeState(str, Enum):
    STARTING = "STARTING"
    SUPERVISOR_READY = "SUPERVISOR_READY"
    HERMES_READY = "HERMES_READY"
    DRAINING = "DRAINING"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class StartupResourceSnapshot:
    nofile_soft: int
    pids_limit: int | None
    disk_used_percent: float
    disk_free_bytes: int
    sqlite_ok: bool


@dataclass(frozen=True, slots=True)
class StartupCheckResult:
    ready: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    snapshot: StartupResourceSnapshot


class RuntimeHealth:
    def __init__(self, *, require_admission_open: bool = False) -> None:
        self.state = RuntimeState.STARTING
        self.error_code: str | None = None
        self.startup_check: StartupCheckResult | None = None
        self.storage: StorageSnapshot | None = None
        self.control_ready = False
        self.execution_ready = False
        self.admission_open = not require_admission_open
        self.store_state: str = "unopened"

    def mark_control_ready(self) -> None:
        self.control_ready = True

    def mark_store_state(self, store_state: str) -> None:
        if store_state not in {
            "unopened",
            "source_validated",
            "migrated",
            "failed",
        }:
            raise ValueError("Runtime store state is invalid")
        self.store_state = store_state

    def mark_admission_open(self, value: bool) -> None:
        self.admission_open = bool(value)

    def mark_supervisor_ready(self, result: StartupCheckResult) -> None:
        self.startup_check = result
        self.state = RuntimeState.SUPERVISOR_READY
        self.error_code = None

    def mark_hermes_ready(self) -> None:
        self.state = RuntimeState.HERMES_READY
        self.error_code = None
        self.execution_ready = True

    def mark_draining(self) -> None:
        self.state = RuntimeState.DRAINING
        self.error_code = None
        self.execution_ready = False

    def mark_failed(self, error_code: str) -> None:
        self.state = RuntimeState.FAILED
        self.error_code = error_code
        self.execution_ready = False

    def update_storage(self, snapshot: StorageSnapshot) -> None:
        self.storage = snapshot

    @property
    def ready(self) -> bool:
        return self.execution_ready and self.admission_open

    def public_snapshot(self) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "state": self.state.value,
            "ready": self.ready,
            "control_ready": self.control_ready,
            "execution_ready": self.execution_ready,
            "admission_open": self.admission_open,
            "store_state": self.store_state,
            "error_code": self.error_code,
        }
        if self.storage is not None:
            snapshot["storage"] = {
                "level": self.storage.level.value,
                "used_percent": round(self.storage.used_percent, 2),
                "free_bytes": self.storage.free_bytes,
                "uploads_allowed": self.storage.uploads_allowed,
                "admissions_allowed": self.storage.admissions_allowed,
                "reserve_present": self.storage.reserve_present,
            }
        return snapshot


def raise_nofile_soft_limit(minimum: int = 65536) -> int:
    """Raise the process soft limit within its inherited hard boundary."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft >= minimum:
        return soft
    target = minimum if hard == resource.RLIM_INFINITY else min(minimum, hard)
    if target > soft:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    return resource.getrlimit(resource.RLIMIT_NOFILE)[0]


def evaluate_startup_resources(
    snapshot: StartupResourceSnapshot,
    *,
    min_nofile: int = 65536,
    min_pids: int = 4096,
) -> StartupCheckResult:
    errors: list[str] = []
    warnings: list[str] = []
    if snapshot.nofile_soft < min_nofile:
        errors.append("NOFILE_LIMIT_TOO_LOW")
    if snapshot.pids_limit is not None and snapshot.pids_limit < min_pids:
        errors.append("PIDS_LIMIT_TOO_LOW")
    if not snapshot.sqlite_ok:
        errors.append("SQLITE_QUICK_CHECK_FAILED")
    if snapshot.disk_used_percent >= 85 or snapshot.disk_free_bytes < 2 * GIB:
        errors.append("PERSISTENT_STORAGE_HARD_WATERMARK")
    elif snapshot.disk_used_percent >= 70:
        warnings.append("PERSISTENT_STORAGE_WARNING_WATERMARK")
    return StartupCheckResult(
        ready=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        snapshot=snapshot,
    )


def probe_startup_resources(
    data_root: Path,
    *,
    sqlite_ok: bool,
) -> StartupResourceSnapshot:
    nofile_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    pids_limit = _read_pids_limit()
    disk = shutil.disk_usage(data_root)
    used_percent = 0.0 if disk.total == 0 else (disk.used / disk.total) * 100
    return StartupResourceSnapshot(
        nofile_soft=nofile_soft,
        pids_limit=pids_limit,
        disk_used_percent=used_percent,
        disk_free_bytes=disk.free,
        sqlite_ok=sqlite_ok,
    )


def _read_pids_limit() -> int | None:
    for path in (
        Path("/sys/fs/cgroup/pids.max"),
        Path("/sys/fs/cgroup/pids/pids.max"),
    ):
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            return int(raw)
        except ValueError:
            return 0
    return None
