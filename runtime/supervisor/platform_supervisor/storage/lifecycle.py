from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import fcntl
import os
from pathlib import Path
import shutil
import time
from typing import BinaryIO


GIB = 1024 * 1024 * 1024
MIB = 1024 * 1024
DEFAULT_RESERVE_BYTES = 512 * MIB


class RuntimeStorageLockError(RuntimeError):
    pass


class StorageLevel(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    UPLOAD_BLOCKED = "upload_blocked"
    ADMISSION_BLOCKED = "admission_blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    used_percent: float
    free_bytes: int
    level: StorageLevel
    uploads_allowed: bool
    admissions_allowed: bool
    reserve_present: bool


def classify_storage(
    *,
    used_percent: float,
    free_bytes: int,
    reserve_present: bool,
) -> StorageSnapshot:
    if used_percent >= 95 or free_bytes < 512 * MIB:
        level = StorageLevel.FAILED
    elif used_percent >= 85 or free_bytes < 2 * GIB:
        level = StorageLevel.ADMISSION_BLOCKED
    elif used_percent >= 80:
        level = StorageLevel.UPLOAD_BLOCKED
    elif used_percent >= 70:
        level = StorageLevel.WARNING
    else:
        level = StorageLevel.NORMAL
    return StorageSnapshot(
        used_percent=used_percent,
        free_bytes=free_bytes,
        level=level,
        uploads_allowed=level in {StorageLevel.NORMAL, StorageLevel.WARNING},
        admissions_allowed=level in {
            StorageLevel.NORMAL,
            StorageLevel.WARNING,
            StorageLevel.UPLOAD_BLOCKED,
        },
        reserve_present=reserve_present,
    )


class RuntimeStorageLifecycle:
    """Own the PVC lock and its fixed emergency free-space reserve."""

    def __init__(
        self,
        data_root: Path,
        *,
        reserve_bytes: int = DEFAULT_RESERVE_BYTES,
    ) -> None:
        if reserve_bytes < 0:
            raise ValueError("reserve_bytes cannot be negative")
        self.data_root = data_root
        self.lock_path = data_root / "runtime.lock"
        self.reserve_path = data_root / ".emergency-reserve"
        self.reserve_bytes = reserve_bytes
        self._lock_file: BinaryIO | None = None

    @property
    def locked(self) -> bool:
        return self._lock_file is not None

    def acquire(self) -> None:
        if self._lock_file is not None:
            return
        self.data_root.mkdir(parents=True, exist_ok=True)
        lock_file = self.lock_path.open("a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise RuntimeStorageLockError("PERSISTENT_STORAGE_LOCK_HELD") from exc
        self._lock_file = lock_file

    def ensure_reserve(self) -> None:
        if not self.locked:
            raise RuntimeStorageLockError("PERSISTENT_STORAGE_LOCK_NOT_HELD")
        if self.reserve_bytes == 0:
            return
        try:
            if (
                self.reserve_path.is_file()
                and self.reserve_path.stat().st_size == self.reserve_bytes
            ):
                return
        except OSError:
            pass
        temporary_path = self.reserve_path.with_suffix(".tmp")
        try:
            with temporary_path.open("w+b") as reserve_file:
                os.posix_fallocate(
                    reserve_file.fileno(),
                    0,
                    self.reserve_bytes,
                )
                reserve_file.flush()
                os.fsync(reserve_file.fileno())
            os.replace(temporary_path, self.reserve_path)
            _fsync_directory(self.data_root)
        except Exception:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def release_reserve(self) -> bool:
        try:
            self.reserve_path.unlink()
        except FileNotFoundError:
            return False
        _fsync_directory(self.data_root)
        return True

    def snapshot(self) -> StorageSnapshot:
        usage = shutil.disk_usage(self.data_root)
        used_percent = (
            0.0 if usage.total == 0 else (usage.used / usage.total) * 100
        )
        return classify_storage(
            used_percent=used_percent,
            free_bytes=usage.free,
            reserve_present=self.reserve_path.is_file(),
        )

    def cleanup_stale_upload_parts(
        self,
        workspace_dir: Path,
        *,
        older_than_seconds: int = 600,
        max_removed: int = 1000,
    ) -> int:
        """Remove crash leftovers without following workspace symlinks."""

        if not self.locked:
            raise RuntimeStorageLockError(
                "PERSISTENT_STORAGE_LOCK_NOT_HELD"
            )
        cutoff = time.time() - max(0, older_than_seconds)
        removed = 0
        for directory, child_directories, filenames in os.walk(
            workspace_dir,
            followlinks=False,
        ):
            child_directories[:] = [
                name
                for name in child_directories
                if not (Path(directory) / name).is_symlink()
            ]
            for filename in filenames:
                if (
                    not filename.startswith(".")
                    or not filename.endswith(".part")
                ):
                    continue
                candidate = Path(directory) / filename
                try:
                    if candidate.is_symlink() or candidate.stat().st_mtime > cutoff:
                        continue
                    candidate.unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
                if removed >= max_removed:
                    return removed
        return removed

    def close(self) -> None:
        lock_file = self._lock_file
        self._lock_file = None
        if lock_file is None:
            return
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
