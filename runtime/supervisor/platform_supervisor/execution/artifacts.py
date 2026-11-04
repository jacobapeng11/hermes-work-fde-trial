from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import mimetypes
from pathlib import Path, PurePosixPath
import re
import threading

from platform_supervisor.execution.sessions import PrepareIdentity
from platform_supervisor.hermes.events import PlatformStreamEvent


logger = logging.getLogger(__name__)

_FORBIDDEN_PATH_NAMES = frozenset({".env", "config.yaml"})
_FORBIDDEN_DIRECTORIES = frozenset(
    {"home", "logs", "log", ".work", ".venv", "__pycache__"}
)
_FORBIDDEN_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3", ".part"})
_HIDDEN_TEMP_PREFIXES = (".", "~")
_STABLE_DELAY_SECONDS = 0.5
_MAX_PENDING_PATHS = 256


class ArtifactWatcherPathError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactRevision:
    size_bytes: int
    mtime_ns: int
    inode: int


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    relative_path: str
    revision: ArtifactRevision
    mime_type: str | None


ArtifactEmitter = Callable[[PlatformStreamEvent], Awaitable[None]]


def session_artifact_root(workspace_dir: Path, identity: PrepareIdentity) -> Path:
    """Resolve exactly the Runtime-side session layout; never trust a raw path."""

    safe_session_id = re.sub(r"[^A-Za-z0-9._-]", "_", identity.session_id)
    if not safe_session_id:
        raise ArtifactWatcherPathError("invalid runtime session id")
    root = workspace_dir
    if identity.profile_id not in {"", "default"} and not identity.profile_id.startswith(
        "application-"
    ):
        profile_number = _profile_number(identity.profile_id)
        if profile_number is None:
            raise ArtifactWatcherPathError("invalid expert profile id")
        root = root / "experts" / str(profile_number)
    target = root / "sessions" / safe_session_id
    try:
        target.resolve().relative_to(workspace_dir.resolve())
    except ValueError as exc:
        raise ArtifactWatcherPathError("session directory escapes workspace") from exc
    return target


def _profile_number(profile_id: str) -> int | None:
    if not profile_id or profile_id in {"", "default"} or profile_id.startswith(
        "application-"
    ):
        return None
    match = re.fullmatch(r"expert-(\d+)", profile_id)
    return int(match.group(1)) if match else None


class ArtifactWatcher:
    """Per-Run, event-driven session artifact observer with a bounded fallback.

    The watcher only observes the current session root.  It emits metadata, not
    file contents; Backend remains the authority that creates downloadable IDs.
    """

    def __init__(
        self,
        *,
        workspace_dir: Path,
        identity: PrepareIdentity,
        emit: ArtifactEmitter,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.root = session_artifact_root(workspace_dir, identity)
        self.emit = emit
        self._baseline: dict[str, ArtifactEntry] = {}
        self._published: dict[str, ArtifactEntry] = {}
        self._generation: dict[str, int] = {}
        self._pending: dict[str, asyncio.Task[None]] = {}
        self._watch_task: asyncio.Task[None] | None = None
        self._stop_event = threading.Event()
        self.available = False
        self.overflowed = False

    async def start(self) -> None:
        self._baseline = self.snapshot()
        # Files already present when this Run starts belong to session
        # history, not to the new Run. Treat them as published baseline state
        # so only additions, updates, or deletions observed during this Run
        # emit artifact events.
        self._published = dict(self._baseline)
        try:
            from watchfiles import awatch  # type: ignore[import-not-found]
        except ImportError:
            logger.warning(
                "artifact_watcher_unavailable",
                extra={"event": "artifact_watcher_unavailable"},
            )
            return
        self.available = True
        self._watch_task = asyncio.create_task(
            self._watch(awatch),
            name=f"artifact-watcher-{self.root.name}",
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._watch_task is not None:
            self._watch_task.cancel()
            await asyncio.gather(self._watch_task, return_exceptions=True)
        tasks = tuple(self._pending.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        await self.reconcile()

    def snapshot(self) -> dict[str, ArtifactEntry]:
        if not self.root.exists():
            return {}
        entries: dict[str, ArtifactEntry] = {}
        try:
            candidates = self.root.rglob("*")
            for candidate in candidates:
                entry = self._entry_for_path(candidate)
                if entry is not None:
                    entries[entry.relative_path] = entry
        except OSError as exc:
            logger.warning(
                "artifact_watcher_snapshot_failed",
                extra={"event": "artifact_watcher_snapshot_failed", "error_type": type(exc).__name__},
            )
        return entries

    async def reconcile(self) -> None:
        current = self.snapshot()
        for relative_path in sorted(set(self._published) | set(current)):
            await self._publish_change(relative_path, current.get(relative_path))

    async def observe_path(self, path: Path) -> None:
        """Schedule one path after an fs event; kept public for deterministic tests."""

        relative_path = self._relative_path(path)
        if relative_path is None:
            return
        if (
            relative_path not in self._pending
            and len(self._pending) >= _MAX_PENDING_PATHS
        ):
            self.overflowed = True
            logger.warning(
                "artifact_watcher_queue_overflow",
                extra={"event": "artifact_watcher_queue_overflow"},
            )
            return
        generation = self._generation.get(relative_path, 0) + 1
        self._generation[relative_path] = generation
        previous = self._pending.pop(relative_path, None)
        if previous is not None:
            previous.cancel()
        task = asyncio.create_task(
            self._confirm_stable(relative_path, generation),
            name=f"artifact-stable-{self.root.name}-{generation}",
        )
        self._pending[relative_path] = task

    async def _watch(self, awatch: Callable[..., object]) -> None:
        try:
            # Watch the workspace when the session directory does not yet
            # exist, otherwise watch only its root. Both stay inside this PVC.
            watch_root = self.root if self.root.exists() else self.workspace_dir
            async for changes in awatch(
                watch_root,
                recursive=True,
                stop_event=self._stop_event,
                debounce=100,
                step=50,
            ):
                for _change, raw_path in changes:
                    await self.observe_path(Path(raw_path))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.available = False
            logger.warning(
                "artifact_watcher_failed",
                extra={"event": "artifact_watcher_failed", "error_type": type(exc).__name__},
            )

    async def _confirm_stable(self, relative_path: str, generation: int) -> None:
        try:
            await asyncio.sleep(_STABLE_DELAY_SECONDS)
            first = self._entry_for_relative_path(relative_path)
            await asyncio.sleep(_STABLE_DELAY_SECONDS)
            second = self._entry_for_relative_path(relative_path)
            if self._generation.get(relative_path) != generation:
                return
            if first is None and second is None:
                path = self._path_for_relative_path(relative_path)
                if not path.exists():
                    await self._publish_change(relative_path, None)
                    # A move-out can be reported only for its directory. The
                    # individual children may no longer be observable, so
                    # clear every indexed descendant without a full scan.
                    prefix = f"{relative_path}/"
                    for published_path in tuple(self._published):
                        if published_path.startswith(prefix):
                            await self._publish_change(published_path, None)
            elif first is not None and first == second:
                await self._publish_change(relative_path, second)
            else:
                # A write is still in progress. A new kernel event normally
                # follows; reschedule defensively in case it is coalesced.
                await self.observe_path(self._path_for_relative_path(relative_path))
        except asyncio.CancelledError:
            raise
        finally:
            task = self._pending.get(relative_path)
            if task is asyncio.current_task():
                self._pending.pop(relative_path, None)

    async def _publish_change(
        self,
        relative_path: str,
        current: ArtifactEntry | None,
    ) -> None:
        prior = self._published.get(relative_path)
        if current is None:
            if prior is None and relative_path not in self._baseline:
                return
            self._published.pop(relative_path, None)
            await self.emit(
                PlatformStreamEvent(
                    event="chat.artifact.deleted",
                    data={"relative_path": relative_path},
                )
            )
            return
        if prior is not None and prior.revision == current.revision:
            return
        self._published[relative_path] = current
        await self.emit(
            PlatformStreamEvent(
                event=("chat.artifact.updated" if prior is not None else "chat.artifact.created"),
                data={
                    "relative_path": current.relative_path,
                    "mime_type": current.mime_type,
                    "size_bytes": current.revision.size_bytes,
                    "mtime_ns": current.revision.mtime_ns,
                },
            )
        )

    def _entry_for_relative_path(self, relative_path: str) -> ArtifactEntry | None:
        return self._entry_for_path(self._path_for_relative_path(relative_path))

    def _path_for_relative_path(self, relative_path: str) -> Path:
        workspace_relative = PurePosixPath(relative_path).relative_to("workspace")
        session_relative = workspace_relative.relative_to(
            PurePosixPath(*self.root.relative_to(self.workspace_dir).parts)
        )
        return self.root / session_relative

    def _entry_for_path(self, path: Path) -> ArtifactEntry | None:
        relative_path = self._relative_path(path)
        if relative_path is None:
            return None
        try:
            if path.is_symlink() or not path.is_file():
                return None
            stat = path.stat()
        except OSError:
            return None
        return ArtifactEntry(
            relative_path=relative_path,
            revision=ArtifactRevision(
                size_bytes=int(stat.st_size),
                mtime_ns=int(stat.st_mtime_ns),
                inode=int(stat.st_ino),
            ),
            mime_type=mimetypes.guess_type(path.name)[0],
        )

    def _relative_path(self, path: Path) -> str | None:
        try:
            relative = path.resolve(strict=False).relative_to(self.root.resolve())
        except ValueError:
            return None
        if not relative.parts or not self._safe_relative_parts(relative.parts):
            return None
        return PurePosixPath("workspace", *self.root.relative_to(self.workspace_dir).parts, *relative.parts).as_posix()

    @staticmethod
    def _safe_relative_parts(parts: tuple[str, ...]) -> bool:
        lowered = tuple(part.casefold() for part in parts)
        if any(part in _FORBIDDEN_DIRECTORIES for part in lowered):
            return False
        if any(part.startswith(_HIDDEN_TEMP_PREFIXES) for part in lowered):
            return False
        name = lowered[-1]
        if name in _FORBIDDEN_PATH_NAMES:
            return False
        return not any(name.endswith(suffix) for suffix in _FORBIDDEN_SUFFIXES)
