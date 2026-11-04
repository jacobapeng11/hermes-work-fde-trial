from __future__ import annotations

from pathlib import Path

import pytest

from platform_supervisor.workspace_probe import (
    WorkspaceProbeError,
    probe_directory,
    probe_directories,
)


def test_workspace_probe_fsyncs_and_removes_temporary_file(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session"
    work = session / ".work"
    work.mkdir(parents=True)

    probe_directories((session, work))

    assert list(session.iterdir()) == [work]
    assert list(work.iterdir()) == []


def test_workspace_probe_rejects_symlinked_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(WorkspaceProbeError, match="not writable"):
        probe_directory(link)
