"""Offline repair for managed historical Runtime Session workspace trees.

The module is intentionally absent from Supervisor startup and request paths.
It may run only in a short-lived root maintenance process after the owning
Runtime Pod has reached strict zero.  It never follows symbolic links or reads
ordinary-file content.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Callable, Iterable, Literal

from platform_supervisor.permission_repair import (
    HERMES_UID,
    SUPERVISOR_UID,
    WORKSPACE_GID,
    _child_probe,
)


APPLY_CONFIRMATION = "REPAIR_ALL_MANAGED_SESSIONS"
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
class SessionPermissionRepairError(RuntimeError):
    """The closed historical Session repair shape is unsafe or incomplete."""


@dataclass(frozen=True, slots=True)
class SessionRepairReport:
    applied: bool
    session_count: int
    directory_count: int
    file_count: int
    symlink_count: int
    changed_directories: int
    changed_files: int
    hermes_probe_count: int
    supervisor_probe_count: int


@dataclass(frozen=True, slots=True)
class _PlanItem:
    path: Path
    object_type: Literal["directory", "file", "symlink"]
    device: int
    inode: int
    owner_uid: int
    before_gid: int
    before_mode: int
    after_gid: int
    after_mode: int

    @property
    def changed(self) -> bool:
        return self.before_gid != self.after_gid or self.before_mode != self.after_mode


class SessionPermissionRepair:
    """Plan and apply one PVC's closed set of managed Session trees."""

    def __init__(
        self,
        *,
        pvc_root: Path,
        supervisor_uid: int = SUPERVISOR_UID,
        hermes_uid: int = HERMES_UID,
        workspace_gid: int = WORKSPACE_GID,
        identity_probe: Callable[..., bool] = _child_probe,
    ) -> None:
        unresolved = Path(pvc_root)
        metadata = os.lstat(unresolved)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise SessionPermissionRepairError("PVC root is not a safe directory")
        self.pvc_root = unresolved.resolve(strict=True)
        self.supervisor_uid = int(supervisor_uid)
        self.hermes_uid = int(hermes_uid)
        self.workspace_gid = int(workspace_gid)
        self._safe_uids = frozenset({self.supervisor_uid, self.hermes_uid})
        self._safe_gids = frozenset(
            {self.workspace_gid, self.supervisor_uid, self.hermes_uid}
        )
        self._identity_probe = identity_probe

    def scan(
        self,
        *,
        apply: bool,
        confirmation: str | None = None,
    ) -> SessionRepairReport:
        if os.geteuid() != 0:
            raise SessionPermissionRepairError("Session repair must start as root")
        if apply and confirmation != APPLY_CONFIRMATION:
            raise SessionPermissionRepairError("Session repair apply confirmation is invalid")

        session_roots = tuple(self._session_roots())
        plan = tuple(self._plan_session_tree(path) for path in session_roots)
        flattened = tuple(item for tree in plan for item in tree)
        # Validate every post-repair probe target before the first metadata
        # write.  In particular, a symlinked ``.work`` must fail the preflight
        # instead of being discovered after sibling objects were changed.
        probe_paths = tuple(self._probe_paths(session_roots))
        report = self._report(
            apply=apply,
            session_count=len(session_roots),
            plan=flattened,
            hermes_probe_count=0,
            supervisor_probe_count=0,
        )
        if not apply:
            return report

        for item in flattened:
            if item.changed:
                self._apply_item(item)

        verification = tuple(
            item
            for session_root in session_roots
            for item in self._plan_session_tree(session_root)
        )
        if any(item.changed for item in verification):
            raise SessionPermissionRepairError("Session metadata did not converge")

        for path in probe_paths:
            if not self._identity_probe(
                path,
                directory=True,
                uid=self.hermes_uid,
                gid=self.hermes_uid,
            ):
                raise SessionPermissionRepairError("Hermes Session probe failed after repair")
            if not self._identity_probe(
                path,
                directory=True,
                uid=self.supervisor_uid,
                gid=self.supervisor_uid,
            ):
                raise SessionPermissionRepairError(
                    "Supervisor Session probe failed after repair"
                )
        return self._report(
            apply=True,
            session_count=len(session_roots),
            plan=flattened,
            hermes_probe_count=len(probe_paths),
            supervisor_probe_count=len(probe_paths),
        )

    def _session_roots(self) -> Iterable[Path]:
        default_root = self.pvc_root / "workspace" / "sessions"
        yield from self._children(default_root, identity="Session")

        experts_root = self.pvc_root / "workspace" / "experts"
        if not experts_root.exists():
            return
        self._require_directory(experts_root, label="experts root")
        with os.scandir(experts_root) as entries:
            experts = sorted(entries, key=lambda item: item.name)
        for expert in experts:
            metadata = expert.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode) or not expert.name.isdigit() or int(expert.name) <= 0:
                raise SessionPermissionRepairError("unsafe expert identity in workspace")
            yield from self._children(
                Path(expert.path) / "sessions",
                identity="Session",
            )

    def _children(self, root: Path, *, identity: str) -> Iterable[Path]:
        if not root.exists():
            return
        self._require_directory(root, label=f"{identity} root")
        with os.scandir(root) as entries:
            children = sorted(entries, key=lambda item: item.name)
        for child in children:
            metadata = child.stat(follow_symlinks=False)
            # Only directories can be managed Session roots.  Historical user
            # files placed beside them remain outside this repair's authority.
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                continue
            if not _SAFE_SESSION_ID.fullmatch(child.name):
                raise SessionPermissionRepairError("unsafe Session identity in workspace")
            yield Path(child.path)

    @staticmethod
    def _require_directory(path: Path, *, label: str) -> None:
        metadata = os.lstat(path)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise SessionPermissionRepairError(f"{label} is not a safe directory")

    def _plan_session_tree(self, session_root: Path) -> tuple[_PlanItem, ...]:
        planned: list[_PlanItem] = []
        pending = [session_root]
        while pending:
            path = pending.pop()
            metadata = os.lstat(path)
            object_type = self._object_type(metadata, path)
            planned.append(self._plan_item(path, metadata, object_type))
            if object_type != "directory":
                continue
            with os.scandir(path) as entries:
                children = sorted(entries, key=lambda item: item.name, reverse=True)
            pending.extend(Path(item.path) for item in children)
        return tuple(planned)

    def _object_type(
        self,
        metadata: os.stat_result,
        path: Path,
    ) -> Literal["directory", "file", "symlink"]:
        if stat.S_ISDIR(metadata.st_mode):
            return "directory"
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise SessionPermissionRepairError(
                    "managed Session regular file has an unsafe hard link"
                )
            return "file"
        if stat.S_ISLNK(metadata.st_mode):
            return "symlink"
        raise SessionPermissionRepairError(
            "unsupported object in managed Session tree: " + self._path_digest(path)
        )

    def _plan_item(
        self,
        path: Path,
        metadata: os.stat_result,
        object_type: Literal["directory", "file", "symlink"],
    ) -> _PlanItem:
        mode = stat.S_IMODE(metadata.st_mode)
        if object_type == "symlink":
            return _PlanItem(
                path,
                object_type,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_uid,
                metadata.st_gid,
                mode,
                metadata.st_gid,
                mode,
            )
        if metadata.st_uid not in self._safe_uids or metadata.st_gid not in self._safe_gids:
            raise SessionPermissionRepairError(
                "unknown POSIX identity in managed Session tree: "
                + self._path_digest(path)
            )
        if object_type == "file" and mode & 0o7000:
            raise SessionPermissionRepairError(
                "unsafe special mode in managed Session tree: "
                + self._path_digest(path)
            )
        target_mode = 0o2770 if object_type == "directory" else (
            0o770 if mode & 0o111 else 0o660
        )
        return _PlanItem(
            path,
            object_type,
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_uid,
            metadata.st_gid,
            mode,
            self.workspace_gid,
            target_mode,
        )

    @staticmethod
    def _apply_item(item: _PlanItem) -> None:
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        if item.object_type == "directory":
            flags |= os.O_DIRECTORY
        descriptor = os.open(item.path, flags)
        try:
            current = os.fstat(descriptor)
            if (
                current.st_dev != item.device
                or current.st_ino != item.inode
                or current.st_uid != item.owner_uid
                or stat.S_IMODE(current.st_mode) != item.before_mode
                or current.st_gid != item.before_gid
            ):
                raise SessionPermissionRepairError(
                    "managed Session object changed during repair"
                )
            os.fchown(descriptor, -1, item.after_gid)
            os.fchmod(descriptor, item.after_mode)
            os.fsync(descriptor)
            after = os.fstat(descriptor)
            if (
                after.st_uid != item.owner_uid
                or after.st_gid != item.after_gid
                or stat.S_IMODE(after.st_mode) != item.after_mode
            ):
                raise SessionPermissionRepairError(
                    "managed Session object mode was not preserved"
                )
        finally:
            os.close(descriptor)

    @staticmethod
    def _probe_paths(session_roots: Iterable[Path]) -> Iterable[Path]:
        for root in session_roots:
            yield root
            work = root / ".work"
            if work.exists():
                metadata = os.lstat(work)
                if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise SessionPermissionRepairError(
                        "managed Session .work is not a safe directory"
                    )
                yield work

    def _path_digest(self, path: Path) -> str:
        relative = path.relative_to(self.pvc_root).as_posix().encode("utf-8")
        return "sha256:" + hashlib.sha256(relative).hexdigest()

    @staticmethod
    def _report(
        *,
        apply: bool,
        session_count: int,
        plan: tuple[_PlanItem, ...],
        hermes_probe_count: int,
        supervisor_probe_count: int,
    ) -> SessionRepairReport:
        directories = tuple(item for item in plan if item.object_type == "directory")
        files = tuple(item for item in plan if item.object_type == "file")
        return SessionRepairReport(
            applied=apply,
            session_count=session_count,
            directory_count=len(directories),
            file_count=len(files),
            symlink_count=sum(item.object_type == "symlink" for item in plan),
            changed_directories=sum(item.changed for item in directories),
            changed_files=sum(item.changed for item in files),
            hermes_probe_count=hermes_probe_count,
            supervisor_probe_count=supervisor_probe_count,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="offline repair for all managed Runtime Session trees"
    )
    parser.add_argument("--pvc-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    arguments = parser.parse_args(argv)
    try:
        report = SessionPermissionRepair(pvc_root=arguments.pvc_root).scan(
            apply=arguments.apply,
            confirmation=arguments.confirm,
        )
    except SessionPermissionRepairError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 70
    except OSError as exc:
        print(
            json.dumps(
                {"ok": False, "error_type": type(exc).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 70
    print(json.dumps({"ok": True, **asdict(report)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
