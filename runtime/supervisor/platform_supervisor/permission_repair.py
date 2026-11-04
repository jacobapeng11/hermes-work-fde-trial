"""Offline, allowlisted POSIX metadata repair for an already-drained Runtime.

This module deliberately has no startup integration.  It is run only by a
short-lived maintenance Job which mounts one already-identified PVC after its
old Runtime Pod has reached zero.  It never opens SQLite through sqlite3 or
reads ordinary-file contents.

Capability limitation: this is intentionally a narrow, conservative scanner,
not a general legacy-permission remediation engine.  Its automatic rules do
not cover every historically observed safe-looking owner/GID/mode shape.
Never invoke it automatically during startup, recovery, or fleet maintenance.
Before each future invocation, the operator must first confirm the exact PVC
scope and the proposed handling of any rule-external findings; do not infer
that confirmation from a previous maintenance window.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Iterable, Literal


SUPERVISOR_UID = 10000
HERMES_UID = 10001
WORKSPACE_GID = 1000
_HOME_DIRECTORIES = (
    "cron",
    "sessions",
    "logs",
    "logs/curator",
    "memories",
    "pairing",
    "hooks",
    "image_cache",
    "audio_cache",
    "skills",
    "profiles",
    "kanban",
    "kanban/workspaces",
    "kanban/logs",
    "kanban/boards",
)
_WORKSPACE_DIRECTORIES = ("sessions", ".kanban-workspaces")
_DATABASE_BASENAMES = (
    "state.db",
    "state.db-wal",
    "state.db-shm",
    "kanban.db",
    "kanban.db-wal",
    "kanban.db-shm",
)


class PermissionRepairError(RuntimeError):
    """The closed repair shape was not safe to apply."""


@dataclass(frozen=True)
class RepairRecord:
    path_key: str
    object_type: Literal["directory", "file", "missing"]
    before_uid: int | None
    before_gid: int | None
    before_mode: str | None
    child_writable_before: bool | None
    supervisor_writable_before: bool | None
    action: Literal["unchanged", "repair_directory", "repair_file", "manual_review"]
    after_uid: int | None = None
    after_gid: int | None = None
    after_mode: str | None = None
    child_writable_after: bool | None = None
    supervisor_writable_after: bool | None = None
    reason: str | None = None


def _mode(metadata: os.stat_result) -> int:
    return stat.S_IMODE(metadata.st_mode)


def _metadata(path: Path, *, expected_directory: bool) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        raise PermissionRepairError("allowlisted path disappeared") from None
    if stat.S_ISLNK(metadata.st_mode):
        raise PermissionRepairError("allowlisted path is a symlink")
    if expected_directory and not stat.S_ISDIR(metadata.st_mode):
        raise PermissionRepairError("allowlisted path is not a directory")
    if not expected_directory and not stat.S_ISREG(metadata.st_mode):
        raise PermissionRepairError("allowlisted path is not a regular file")
    return metadata


def _child_probe(path: Path, *, directory: bool, uid: int, gid: int) -> bool:
    """Probe with the real consumer uid without observing file content."""

    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    probe_directory_fd = os.open(
        path if directory else path.parent,
        flags | os.O_DIRECTORY,
    )
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - parent observes all outcomes
        try:
            os.close(read_fd)
            os.setgroups([WORKSPACE_GID])
            os.setgid(gid)
            os.setuid(uid)
            if directory:
                name = ".permission-repair-probe-" + secrets.token_hex(8)
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=probe_directory_fd,
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.unlink(name, dir_fd=probe_directory_fd)
            else:
                descriptor = os.open(
                    path.name,
                    os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=probe_directory_fd,
                )
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise OSError("file type changed during probe")
                finally:
                    os.close(descriptor)
        except BaseException:
            os.write(write_fd, b"0")
        else:
            os.write(write_fd, b"1")
        finally:
            os.close(probe_directory_fd)
            os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 1) == b"1"
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    os.close(probe_directory_fd)
    return result and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def _path_key(root: Path, path: Path, *, expert_id: int | None = None) -> str:
    if expert_id is not None:
        suffix = path.relative_to(root / "workspace" / "experts" / str(expert_id))
        suffix_key = "" if str(suffix) == "." else "." + str(suffix).replace("/", ".")
        return "workspace.expert." + str(expert_id) + suffix_key
    relative = path.relative_to(root)
    return str(relative).replace("/", ".")


class RuntimePermissionRepair:
    def __init__(self, *, pvc_root: Path, expert_profile_ids: Iterable[int]) -> None:
        self.pvc_root = pvc_root.resolve(strict=True)
        self.expert_profile_ids = tuple(sorted({int(value) for value in expert_profile_ids if int(value) > 0}))

    def scan(self, *, apply: bool) -> list[RepairRecord]:
        # Keep this call explicitly operator-driven.  A manual-review record is
        # diagnostic evidence, never authority to broaden the repair rules.
        if os.geteuid() != 0:
            raise PermissionRepairError("permission repair must start as root")
        records: list[RepairRecord] = []
        for path, key, shared in self._directories():
            try:
                records.append(self._repair_directory(path, key, shared=shared, apply=apply))
            except PermissionRepairError as exc:
                if str(exc) != "allowlisted path disappeared":
                    raise
                records.append(self._missing_record(key))
        for path, key in self._files():
            records.append(self._repair_file(path, key, apply=apply))
        return records

    def _directories(self) -> Iterable[tuple[Path, str, bool]]:
        home = self.pvc_root / "home"
        workspace = self.pvc_root / "workspace"
        yield home, "home", False
        for relative in _HOME_DIRECTORIES:
            path = home / relative
            yield path, "home." + relative.replace("/", "."), False
        profiles = home / "profiles"
        for profile_id in self.expert_profile_ids:
            profile = profiles / f"expert-{profile_id}"
            if profile.exists() or profile.is_symlink():
                yield profile, f"home.profiles.expert-{profile_id}", False
        yield workspace, "workspace", True
        for relative in _WORKSPACE_DIRECTORIES:
            path = workspace / relative
            yield path, "workspace." + relative.replace("/", "."), True
        experts = workspace / "experts"
        if experts.exists() or experts.is_symlink():
            yield experts, "workspace.experts", True
        for profile_id in self.expert_profile_ids:
            expert = workspace / "experts" / str(profile_id)
            if expert.exists() or expert.is_symlink():
                yield expert, _path_key(self.pvc_root, expert, expert_id=profile_id), True
            for relative in ("shared", "sessions", "skills"):
                path = expert / relative
                if path.exists() or path.is_symlink():
                    yield path, _path_key(self.pvc_root, path, expert_id=profile_id), True
        supervisor = self.pvc_root / "supervisor"
        yield supervisor, "supervisor", False

    def _files(self) -> Iterable[tuple[Path, str]]:
        home = self.pvc_root / "home"
        for basename in _DATABASE_BASENAMES:
            path = home / basename
            if path.exists() or path.is_symlink():
                yield path, "home." + basename
        for profile_id in self.expert_profile_ids:
            profile = home / "profiles" / f"expert-{profile_id}"
            for basename in ("state.db", "state.db-wal", "state.db-shm"):
                path = profile / basename
                if path.exists() or path.is_symlink():
                    yield path, f"home.profiles.expert-{profile_id}.{basename}"

    def _repair_directory(
        self, path: Path, key: str, *, shared: bool, apply: bool
    ) -> RepairRecord:
        metadata = _metadata(path, expected_directory=True)
        mode = _mode(metadata)
        child_before = _child_probe(path, directory=True, uid=HERMES_UID, gid=HERMES_UID)
        supervisor_before = (
            _child_probe(path, directory=True, uid=SUPERVISOR_UID, gid=SUPERVISOR_UID)
            if shared
            else None
        )
        safe_identity = (
            metadata.st_uid in {SUPERVISOR_UID, HERMES_UID}
            and metadata.st_gid == WORKSPACE_GID
        )
        unsafe_bits = bool(mode & stat.S_ISVTX) or bool(mode & 0o002)
        needs_repair = not child_before or (shared and not supervisor_before)
        if not safe_identity or unsafe_bits:
            return self._record(
                path,
                key,
                "directory",
                metadata,
                child_before,
                supervisor_before,
                "manual_review",
                reason="unsafe_identity_or_mode",
            )
        if not needs_repair:
            return self._record(path, key, "directory", metadata, child_before, supervisor_before, "unchanged")
        if not apply:
            return self._record(
                path,
                key,
                "directory",
                metadata,
                child_before,
                supervisor_before,
                "repair_directory",
            )
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            current = os.fstat(descriptor)
            if current.st_ino != metadata.st_ino:
                raise PermissionRepairError("directory inode changed during repair")
            os.fchown(descriptor, -1, WORKSPACE_GID)
            os.fchmod(descriptor, 0o2770)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        after = _metadata(path, expected_directory=True)
        child_after = _child_probe(path, directory=True, uid=HERMES_UID, gid=HERMES_UID)
        supervisor_after = (
            _child_probe(path, directory=True, uid=SUPERVISOR_UID, gid=SUPERVISOR_UID)
            if shared
            else None
        )
        if not child_after or (shared and not supervisor_after):
            raise PermissionRepairError("directory consumer probe failed after repair")
        return self._record(
            path,
            key,
            "directory",
            metadata,
            child_before,
            supervisor_before,
            "repair_directory",
            after=after,
            child_after=child_after,
            supervisor_after=supervisor_after,
        )

    def _repair_file(self, path: Path, key: str, *, apply: bool) -> RepairRecord:
        metadata = _metadata(path, expected_directory=False)
        mode = _mode(metadata)
        child_before = _child_probe(path, directory=False, uid=HERMES_UID, gid=HERMES_UID)
        safe_identity = (
            metadata.st_uid in {SUPERVISOR_UID, HERMES_UID}
            and metadata.st_gid == WORKSPACE_GID
        )
        unsafe_bits = bool(mode & 0o7000) or bool(mode & 0o002)
        if not safe_identity or unsafe_bits:
            return self._record(
                path,
                key,
                "file",
                metadata,
                child_before,
                None,
                "manual_review",
                reason="unsafe_identity_or_mode",
            )
        if child_before:
            return self._record(path, key, "file", metadata, child_before, None, "unchanged")
        if metadata.st_uid != SUPERVISOR_UID:
            return self._record(
                path,
                key,
                "file",
                metadata,
                child_before,
                None,
                "manual_review",
                reason="child_owned_file_not_writable",
            )
        if not apply:
            return self._record(path, key, "file", metadata, child_before, None, "repair_file")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        try:
            current = os.fstat(descriptor)
            if current.st_ino != metadata.st_ino:
                raise PermissionRepairError("file inode changed during repair")
            os.fchown(descriptor, -1, WORKSPACE_GID)
            os.fchmod(descriptor, 0o660)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        after = _metadata(path, expected_directory=False)
        child_after = _child_probe(path, directory=False, uid=HERMES_UID, gid=HERMES_UID)
        if not child_after:
            raise PermissionRepairError("file child probe failed after repair")
        return self._record(
            path,
            key,
            "file",
            metadata,
            child_before,
            None,
            "repair_file",
            after=after,
            child_after=child_after,
        )

    @staticmethod
    def _missing_record(key: str) -> RepairRecord:
        """Report, but never create, a missing fixed allowlist member."""

        return RepairRecord(
            path_key=key,
            object_type="missing",
            before_uid=None,
            before_gid=None,
            before_mode=None,
            child_writable_before=None,
            supervisor_writable_before=None,
            action="manual_review",
            reason="allowlisted_path_missing",
        )

    @staticmethod
    def _record(
        path: Path,
        key: str,
        object_type: Literal["directory", "file"],
        before: os.stat_result,
        child_before: bool,
        supervisor_before: bool | None,
        action: Literal["unchanged", "repair_directory", "repair_file", "manual_review"],
        *,
        after: os.stat_result | None = None,
        child_after: bool | None = None,
        supervisor_after: bool | None = None,
        reason: str | None = None,
    ) -> RepairRecord:
        del path
        return RepairRecord(
            key,
            object_type,
            before.st_uid,
            before.st_gid,
            format(_mode(before), "04o"),
            child_before,
            supervisor_before,
            action,
            after.st_uid if after else None,
            after.st_gid if after else None,
            format(_mode(after), "04o") if after else None,
            child_after,
            supervisor_after,
            reason,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="offline Runtime permission repair")
    parser.add_argument("--pvc-root", required=True, type=Path)
    parser.add_argument("--expert-profile-id", action="append", type=int, default=[])
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    try:
        records = RuntimePermissionRepair(
            pvc_root=arguments.pvc_root,
            expert_profile_ids=arguments.expert_profile_id,
        ).scan(apply=arguments.apply)
    except PermissionRepairError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 70
    print(
        json.dumps(
            {
                "ok": not any(record.action == "manual_review" for record in records),
                "applied": arguments.apply,
                "records": [asdict(record) for record in records],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if not any(record.action == "manual_review" for record in records) else 70


if __name__ == "__main__":
    raise SystemExit(main())
