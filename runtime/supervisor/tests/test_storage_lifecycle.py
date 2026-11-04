from __future__ import annotations

from pathlib import Path

import pytest

from platform_supervisor.storage.lifecycle import (
    GIB,
    MIB,
    RuntimeStorageLifecycle,
    RuntimeStorageLockError,
    StorageLevel,
    classify_storage,
)


@pytest.mark.parametrize(
    ("used_percent", "free_bytes", "expected"),
    [
        (69.9, 10 * GIB, StorageLevel.NORMAL),
        (70.0, 10 * GIB, StorageLevel.WARNING),
        (80.0, 10 * GIB, StorageLevel.UPLOAD_BLOCKED),
        (85.0, 10 * GIB, StorageLevel.ADMISSION_BLOCKED),
        (10.0, 2 * GIB - 1, StorageLevel.ADMISSION_BLOCKED),
        (95.0, 10 * GIB, StorageLevel.FAILED),
        (10.0, 512 * MIB - 1, StorageLevel.FAILED),
    ],
)
def test_storage_watermarks(
    used_percent: float,
    free_bytes: int,
    expected: StorageLevel,
) -> None:
    snapshot = classify_storage(
        used_percent=used_percent,
        free_bytes=free_bytes,
        reserve_present=True,
    )

    assert snapshot.level == expected
    assert snapshot.uploads_allowed is (
        expected in {StorageLevel.NORMAL, StorageLevel.WARNING}
    )
    assert snapshot.admissions_allowed is (
        expected
        in {
            StorageLevel.NORMAL,
            StorageLevel.WARNING,
            StorageLevel.UPLOAD_BLOCKED,
        }
    )


def test_runtime_storage_lock_is_exclusive(tmp_path: Path) -> None:
    first = RuntimeStorageLifecycle(tmp_path, reserve_bytes=0)
    second = RuntimeStorageLifecycle(tmp_path, reserve_bytes=0)
    first.acquire()

    with pytest.raises(
        RuntimeStorageLockError,
        match="PERSISTENT_STORAGE_LOCK_HELD",
    ):
        second.acquire()

    first.close()
    second.acquire()
    second.close()


def test_emergency_reserve_is_preallocated_and_recreatable(
    tmp_path: Path,
) -> None:
    lifecycle = RuntimeStorageLifecycle(tmp_path, reserve_bytes=64 * 1024)
    lifecycle.acquire()

    lifecycle.ensure_reserve()
    assert lifecycle.reserve_path.stat().st_size == 64 * 1024
    assert lifecycle.release_reserve() is True
    assert lifecycle.release_reserve() is False
    lifecycle.ensure_reserve()
    assert lifecycle.reserve_path.stat().st_size == 64 * 1024

    lifecycle.close()
