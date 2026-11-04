from __future__ import annotations

from pathlib import Path
import stat


FRESH_RUNTIME_HOME_SCAFFOLD = frozenset(
    {
        "platform",
        "skills",
        "plugins",
        "PERSONAL",
        "memories",
        "logs",
        "kanban",
        "kanban/workspaces",
        "kanban/logs",
        "kanban/boards",
    }
)


class FreshRuntimeStorageError(RuntimeError):
    code = "FRESH_STORAGE_NOT_EMPTY"


def assert_fresh_runtime_home(pvc_root: Path) -> None:
    """Read-only proof that the Hermes home is empty or known empty scaffold."""

    try:
        root_stat = pvc_root.lstat()
    except FileNotFoundError as exc:
        raise FreshRuntimeStorageError("FRESH_STORAGE_NOT_EMPTY: Hermes home is absent") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise FreshRuntimeStorageError("FRESH_STORAGE_NOT_EMPTY: Hermes home is not a directory")

    for path in pvc_root.rglob("*"):
        relative = path.relative_to(pvc_root).as_posix()
        metadata = path.lstat()
        if (
            relative not in FRESH_RUNTIME_HOME_SCAFFOLD
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
        ):
            raise FreshRuntimeStorageError(f"FRESH_STORAGE_NOT_EMPTY: unexpected path {relative}")


__all__ = [
    "FRESH_RUNTIME_HOME_SCAFFOLD",
    "FreshRuntimeStorageError",
    "assert_fresh_runtime_home",
]
