from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from platform_supervisor.permission_repair import (
    HERMES_UID,
    SUPERVISOR_UID,
    WORKSPACE_GID,
    PermissionRepairError,
    RuntimePermissionRepair,
    _HOME_DIRECTORIES,
    _WORKSPACE_DIRECTORIES,
)


def _chown(path: Path, uid: int, mode: int) -> None:
    os.chown(path, uid, WORKSPACE_GID)
    path.chmod(mode)


def _runtime_tree(root: Path) -> tuple[Path, Path]:
    _chown(root, SUPERVISOR_UID, 0o2770)
    home = root / "home"
    workspace = root / "workspace"
    supervisor = root / "supervisor"
    for directory in (home, workspace, supervisor):
        directory.mkdir()
        _chown(directory, SUPERVISOR_UID, 0o2770)
    for relative in _HOME_DIRECTORIES:
        path = home / relative
        path.mkdir(parents=True, exist_ok=True)
        _chown(path, SUPERVISOR_UID, 0o2770)
    for relative in _WORKSPACE_DIRECTORIES:
        path = workspace / relative
        path.mkdir(parents=True, exist_ok=True)
        _chown(path, SUPERVISOR_UID, 0o2770)
    expert_workspace = workspace / "experts" / "89"
    for path in (workspace / "experts", expert_workspace):
        path.mkdir(parents=True, exist_ok=True)
        _chown(path, SUPERVISOR_UID, 0o2770)
    for relative in ("shared", "sessions", "skills"):
        path = expert_workspace / relative
        path.mkdir(parents=True)
        _chown(path, SUPERVISOR_UID, 0o2770)
    profile = home / "profiles" / "expert-89"
    profile.mkdir()
    _chown(profile, HERMES_UID, 0o2770)
    state_db = profile / "state.db"
    state_db.touch()
    _chown(state_db, HERMES_UID, 0o640)
    return expert_workspace, state_db


@pytest.mark.skipif(os.geteuid() != 0, reason="requires identity probes")
def test_repairs_only_legacy_expert_directories_and_preserves_child_db(
    tmp_path: Path,
) -> None:
    expert_workspace, state_db = _runtime_tree(tmp_path)
    sessions = expert_workspace / "sessions"
    _chown(expert_workspace, SUPERVISOR_UID, 0o2755)
    _chown(sessions, SUPERVISOR_UID, 0o2755)

    repair = RuntimePermissionRepair(pvc_root=tmp_path, expert_profile_ids=[89])
    dry_run = repair.scan(apply=False)
    planned = {record.path_key: record for record in dry_run}

    assert planned["workspace.expert.89"].action == "repair_directory"
    assert planned["workspace.expert.89.sessions"].action == "repair_directory"
    assert planned["home.profiles.expert-89.state.db"].action == "unchanged"
    assert planned["home.profiles.expert-89.state.db"].before_mode == "0640"

    applied = {record.path_key: record for record in repair.scan(apply=True)}

    assert applied["workspace.expert.89"].after_mode == "2770"
    assert applied["workspace.expert.89"].child_writable_after is True
    assert applied["workspace.expert.89"].supervisor_writable_after is True
    assert applied["workspace.expert.89.sessions"].after_mode == "2770"
    metadata = state_db.stat()
    assert metadata.st_uid == HERMES_UID
    assert metadata.st_gid == WORKSPACE_GID
    assert stat.S_IMODE(metadata.st_mode) == 0o640


@pytest.mark.skipif(os.geteuid() != 0, reason="requires ownership mutation")
def test_symlinked_allowlist_member_fails_closed(tmp_path: Path) -> None:
    _runtime_tree(tmp_path)
    (tmp_path / "workspace" / "experts" / "89" / "sessions").rmdir()
    (tmp_path / "workspace" / "experts" / "89" / "sessions").symlink_to(
        tmp_path / "home"
    )

    repair = RuntimePermissionRepair(pvc_root=tmp_path, expert_profile_ids=[89])

    with pytest.raises(PermissionRepairError, match="symlink"):
        repair.scan(apply=False)


@pytest.mark.skipif(os.geteuid() != 0, reason="requires identity probes")
def test_runtime_without_experts_scans_only_shared_runtime_paths(tmp_path: Path) -> None:
    _runtime_tree(tmp_path)

    records = RuntimePermissionRepair(pvc_root=tmp_path, expert_profile_ids=[]).scan(
        apply=False
    )

    assert records
    assert not any(record.path_key.startswith("workspace.expert.") for record in records)


@pytest.mark.skipif(os.geteuid() != 0, reason="requires identity probes")
def test_missing_fixed_allowlist_member_is_reported_for_manual_review(tmp_path: Path) -> None:
    _runtime_tree(tmp_path)
    (tmp_path / "home" / "kanban" / "boards").rmdir()

    records = RuntimePermissionRepair(pvc_root=tmp_path, expert_profile_ids=[]).scan(
        apply=False
    )

    missing = next(record for record in records if record.path_key == "home.kanban.boards")
    assert missing.object_type == "missing"
    assert missing.action == "manual_review"
    assert missing.reason == "allowlisted_path_missing"
    assert missing.before_uid is None
