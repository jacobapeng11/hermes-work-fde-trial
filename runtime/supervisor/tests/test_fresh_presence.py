from pathlib import Path

import pytest

from platform_supervisor.fresh_presence import (
    FreshRuntimeStorageError,
    assert_fresh_runtime_home,
)


def test_fresh_probe_accepts_empty_home_without_writing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    assert_fresh_runtime_home(home)

    assert list(home.iterdir()) == []


def test_fresh_probe_accepts_only_known_empty_directory_scaffold(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for relative in (
        "platform",
        "skills",
        "plugins",
        "PERSONAL",
        "memories",
        "logs",
        "kanban/workspaces",
        "kanban/logs",
        "kanban/boards",
    ):
        (home / relative).mkdir(parents=True, exist_ok=True)
    before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))

    assert_fresh_runtime_home(home)

    assert sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) == before


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_fresh_probe_rejects_unexpected_or_linked_storage(
    tmp_path: Path,
    kind: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    if kind == "file":
        (home / "state.db").write_bytes(b"existing")
    else:
        (home / "platform").symlink_to(tmp_path)

    with pytest.raises(FreshRuntimeStorageError, match="FRESH_STORAGE_NOT_EMPTY"):
        assert_fresh_runtime_home(home)
