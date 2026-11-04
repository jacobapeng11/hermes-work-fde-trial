from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from platform_supervisor.session_permission_repair import (
    APPLY_CONFIRMATION,
    SessionPermissionRepair,
    SessionPermissionRepairError,
)


SUPERVISOR_UID = os.geteuid()
HERMES_UID = os.geteuid()
WORKSPACE_GID = os.getegid()


def _owned(
    path: Path,
    *,
    uid: int = SUPERVISOR_UID,
    gid: int = WORKSPACE_GID,
    mode: int,
) -> None:
    os.chown(path, uid, gid, follow_symlinks=False)
    path.chmod(mode, follow_symlinks=False)


def _repair(root: Path) -> SessionPermissionRepair:
    return SessionPermissionRepair(
        pvc_root=root,
        supervisor_uid=SUPERVISOR_UID,
        hermes_uid=HERMES_UID,
        workspace_gid=WORKSPACE_GID,
        identity_probe=lambda *_args, **_kwargs: True,
    )


def _legacy_session(root: Path, session_id: str = "rs-115-history") -> Path:
    sessions = root / "workspace" / "sessions"
    sessions.mkdir(parents=True)
    session = sessions / session_id
    work = session / ".work"
    nested = session / "nested"
    work.mkdir(parents=True)
    nested.mkdir()
    ordinary = nested / "answer.txt"
    executable = nested / "run.sh"
    ordinary.write_bytes(b"history-content")
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    link = session / "answer-link"
    link.symlink_to("nested/answer.txt")
    for directory in (session, work, nested):
        _owned(directory, mode=0o755)
    _owned(ordinary, mode=0o644)
    _owned(executable, mode=0o755)
    return session


@pytest.mark.skipif(os.geteuid() != 0, reason="requires ownership mutation")
def test_session_repair_is_dry_run_by_default_and_preserves_content(
    tmp_path: Path,
) -> None:
    session = _legacy_session(tmp_path)
    ordinary = session / "nested" / "answer.txt"
    executable = session / "nested" / "run.sh"
    link = session / "answer-link"
    before_content = ordinary.read_bytes()
    before_link = os.readlink(link)

    repair = _repair(tmp_path)
    dry_run = repair.scan(apply=False)

    assert dry_run.session_count == 1
    assert dry_run.changed_directories == 3
    assert dry_run.changed_files == 2
    assert stat.S_IMODE(session.stat().st_mode) == 0o755

    applied = repair.scan(apply=True, confirmation=APPLY_CONFIRMATION)

    assert applied.changed_directories == 3
    assert applied.changed_files == 2
    assert applied.hermes_probe_count == 2
    assert applied.supervisor_probe_count == 2
    assert stat.S_IMODE(session.stat().st_mode) == 0o2770
    assert session.stat().st_gid == WORKSPACE_GID
    assert stat.S_IMODE(ordinary.stat().st_mode) == 0o660
    assert stat.S_IMODE(executable.stat().st_mode) == 0o770
    assert ordinary.stat().st_uid == SUPERVISOR_UID
    assert ordinary.read_bytes() == before_content
    assert os.readlink(link) == before_link

    repeated = repair.scan(apply=True, confirmation=APPLY_CONFIRMATION)
    assert repeated.changed_directories == 0
    assert repeated.changed_files == 0


@pytest.mark.skipif(os.geteuid() != 0, reason="requires ownership mutation")
def test_session_repair_rejects_special_files_and_unsafe_session_ids(
    tmp_path: Path,
) -> None:
    session = _legacy_session(tmp_path)
    fifo = session / "unsafe-fifo"
    os.mkfifo(fifo)
    _owned(fifo, mode=0o600)

    with pytest.raises(SessionPermissionRepairError, match="unsupported object"):
        _repair(tmp_path).scan(apply=False)

    fifo.unlink()
    unsafe = tmp_path / "workspace" / "sessions" / "not a managed session"
    unsafe.mkdir()
    _owned(unsafe, mode=0o755)

    with pytest.raises(SessionPermissionRepairError, match="unsafe Session identity"):
        _repair(tmp_path).scan(apply=False)


@pytest.mark.skipif(os.geteuid() != 0, reason="requires ownership mutation")
def test_session_repair_rejects_symlinked_work_before_changes(tmp_path: Path) -> None:
    session = _legacy_session(tmp_path)
    work = session / ".work"
    work.rmdir()
    work.symlink_to(session / "nested", target_is_directory=True)

    with pytest.raises(SessionPermissionRepairError, match=".work"):
        _repair(tmp_path).scan(
            apply=True,
            confirmation=APPLY_CONFIRMATION,
        )

    assert stat.S_IMODE(session.stat().st_mode) == 0o755


@pytest.mark.skipif(os.geteuid() != 0, reason="requires ownership mutation")
def test_session_repair_ignores_user_files_beside_managed_session_roots(
    tmp_path: Path,
) -> None:
    _legacy_session(tmp_path)
    user_file = tmp_path / "workspace" / "sessions" / "用户文件.md"
    user_file.write_bytes(b"outside managed Session roots")

    report = _repair(tmp_path).scan(apply=False)

    assert report.session_count == 1
    assert user_file.read_bytes() == b"outside managed Session roots"

@pytest.mark.skipif(os.geteuid() != 0, reason="requires ownership mutation")
def test_session_repair_rejects_unknown_identity_before_any_change(
    tmp_path: Path,
) -> None:
    session = _legacy_session(tmp_path)
    with pytest.raises(SessionPermissionRepairError, match="unknown POSIX identity"):
        SessionPermissionRepair(pvc_root=tmp_path).scan(
            apply=True,
            confirmation=APPLY_CONFIRMATION,
        )

    assert stat.S_IMODE(session.stat().st_mode) == 0o755


@pytest.mark.skipif(os.geteuid() != 0, reason="requires ownership mutation")
def test_session_repair_requires_exact_apply_confirmation(tmp_path: Path) -> None:
    _legacy_session(tmp_path)

    with pytest.raises(SessionPermissionRepairError, match="confirmation"):
        _repair(tmp_path).scan(
            apply=True,
            confirmation="yes",
        )


@pytest.mark.skipif(os.geteuid() != 0, reason="requires ownership mutation")
def test_session_repair_includes_expert_session_roots(tmp_path: Path) -> None:
    session = _legacy_session(tmp_path)
    expert_sessions = tmp_path / "workspace" / "experts" / "89" / "sessions"
    expert_session = expert_sessions / "rs-115-expert-history"
    expert_work = expert_session / ".work"
    expert_work.mkdir(parents=True)
    for path in (
        tmp_path / "workspace" / "experts",
        tmp_path / "workspace" / "experts" / "89",
        expert_sessions,
        expert_session,
        expert_work,
    ):
        _owned(path, gid=WORKSPACE_GID, mode=0o2770 if path != expert_session else 0o755)

    report = _repair(tmp_path).scan(apply=False)

    assert report.session_count == 2
    assert report.changed_directories == 4  # ordinary tree plus expert Session root
    assert session.exists()
