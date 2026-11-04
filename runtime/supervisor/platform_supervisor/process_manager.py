from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
import ctypes
import errno
import fcntl
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys

from platform_supervisor.settings import SupervisorSettings
import yaml


logger = logging.getLogger(__name__)

MAIN_BOUNDARY_BOOTSTRAP_FD = 198
MAIN_BOUNDARY_BOOTSTRAP_MAX_BYTES = 4096
MANAGED_DASHBOARD_CONFIG_INVALID = "MANAGED_DASHBOARD_CONFIG_INVALID"

_MFD_CLOEXEC = 0x0001
_MFD_ALLOW_SEALING = 0x0002

_PR_SET_DUMPABLE = 4

KANBAN_STORAGE_ERROR = "KANBAN_STORAGE_PERMISSION_INVALID"
KANBAN_DIRECTORIES = (
    "kanban",
    "kanban/workspaces",
    "kanban/logs",
    "kanban/boards",
)
KANBAN_DATABASE_FILES = (
    "kanban.db",
    "kanban.db-wal",
    "kanban.db-shm",
)
_KANBAN_CHILD_OPEN_PROBE = (
    "import os, stat, sys\n"
    "path = sys.argv[1]\n"
    "descriptor = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))\n"
    "try:\n"
    "    if not stat.S_ISREG(os.fstat(descriptor).st_mode):\n"
    "        raise OSError('kanban database changed type')\n"
    "finally:\n"
    "    os.close(descriptor)\n"
)
HERMES_CHILD_HOME_DIRECTORIES = (
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
)
HERMES_CHILD_WORKSPACE_DIRECTORIES = (
    "sessions",
)
_RUNTIME_HOME_SOURCE_MODES = frozenset(
    {0o700, 0o750, 0o755, 0o770, 0o2770, 0o2775, 0o3770}
)
_RUNTIME_HOME_TARGET_MODE = 0o2770
_RUNTIME_CONFIG_TARGET_MODE = 0o660


class KanbanStoragePermissionError(RuntimeError):
    """The Runtime cannot safely use Hermes' native Kanban storage."""


def _base64url_no_pad(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _main_boundary_bootstrap_bytes(
    *,
    marker_key: bytes,
    boundary_epoch_nonce: str,
) -> bytes:
    if len(marker_key) != 32:
        raise ValueError("main boundary marker key must contain exactly 256 bits")
    payload = json.dumps(
        {
            "algorithm": "hmac-sha256",
            "boundary_epoch_nonce": boundary_epoch_nonce,
            "contract_version": 1,
            "key_base64url": _base64url_no_pad(marker_key),
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(payload) > MAIN_BOUNDARY_BOOTSTRAP_MAX_BYTES:
        raise ValueError("main boundary bootstrap exceeds its closed wire limit")
    return payload


def create_anonymous_memory_file(name: str, *, flags: int) -> int:
    """Create a Linux memfd even when CPython omitted its optional wrapper."""

    encoded_name = name.encode("utf-8")
    if not encoded_name or b"\0" in encoded_name:
        raise ValueError("anonymous memory file name is invalid")
    python_wrapper = getattr(os, "memfd_create", None)
    if callable(python_wrapper):
        return int(python_wrapper(name, flags=flags))
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        memfd_create = libc.memfd_create
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "memfd_create is unavailable") from exc
    memfd_create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    memfd_create.restype = ctypes.c_int
    descriptor = int(memfd_create(encoded_name, flags))
    if descriptor < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return descriptor


def _kanban_storage_error(relative_path: str, reason: str) -> KanbanStoragePermissionError:
    logger.error(
        "Runtime Kanban storage preflight failed",
        extra={
            "component": "process_manager",
            "error_code": KANBAN_STORAGE_ERROR,
            "relative_path": relative_path,
            "reason": reason,
        },
    )
    return KanbanStoragePermissionError(
        f"{KANBAN_STORAGE_ERROR}: {relative_path}: {reason}"
    )


def _open_directory_no_follow(path: Path, relative_path: str) -> int:
    try:
        return os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise _kanban_storage_error(relative_path, "unavailable") from exc


def _normalize_hermes_child_directory(
    path: Path,
    relative_path: str,
    *,
    supervisor_uid: int,
    child_uid: int,
    workspace_gid: int,
    create: bool,
) -> None:
    created = False
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise _kanban_storage_error(relative_path, "unavailable") from None
        try:
            path.mkdir(mode=_RUNTIME_HOME_TARGET_MODE)
            created = True
            metadata = os.lstat(path)
        except OSError as exc:
            raise _kanban_storage_error(relative_path, "cannot_create") from exc
    except OSError as exc:
        raise _kanban_storage_error(relative_path, "unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise _kanban_storage_error(relative_path, "not_directory")
    if metadata.st_uid not in {supervisor_uid, child_uid}:
        raise _kanban_storage_error(relative_path, "identity_mismatch")
    if not created and stat.S_IMODE(metadata.st_mode) not in _RUNTIME_HOME_SOURCE_MODES:
        raise _kanban_storage_error(relative_path, "unsafe_mode")

    descriptor = _open_directory_no_follow(path, relative_path)
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode) or current.st_ino != metadata.st_ino:
            raise _kanban_storage_error(relative_path, "changed_type")
        target_matches = (
            current.st_gid == workspace_gid
            and stat.S_IMODE(current.st_mode) == _RUNTIME_HOME_TARGET_MODE
        )
        if not target_matches:
            if current.st_uid != supervisor_uid:
                raise _kanban_storage_error(relative_path, "identity_mismatch")
            try:
                os.fchown(descriptor, -1, workspace_gid)
                os.fchmod(descriptor, _RUNTIME_HOME_TARGET_MODE)
                os.fsync(descriptor)
            except OSError as exc:
                raise _kanban_storage_error(relative_path, "cannot_set_mode") from exc
        final = os.fstat(descriptor)
        if (
            final.st_uid not in {supervisor_uid, child_uid}
            or final.st_gid != workspace_gid
        ):
            raise _kanban_storage_error(relative_path, "identity_mismatch")
        if stat.S_IMODE(final.st_mode) != _RUNTIME_HOME_TARGET_MODE:
            raise _kanban_storage_error(relative_path, "mode_mismatch")
    finally:
        os.close(descriptor)
    if created:
        parent_descriptor = _open_directory_no_follow(
            path.parent,
            f"{relative_path}:parent",
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)


def _normalize_hermes_root_config(
    path: Path,
    *,
    supervisor_uid: int,
    child_uid: int,
    workspace_gid: int,
    required: bool,
) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise _kanban_storage_error("config.yaml", "unavailable") from None
        return
    except OSError as exc:
        raise _kanban_storage_error("config.yaml", "unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise _kanban_storage_error("config.yaml", "not_regular_file")
    if metadata.st_uid not in {supervisor_uid, child_uid}:
        raise _kanban_storage_error("config.yaml", "identity_mismatch")
    child_owned = metadata.st_uid == child_uid
    if child_owned and metadata.st_gid != workspace_gid:
        raise _kanban_storage_error("config.yaml", "identity_mismatch")
    if child_owned and stat.S_IMODE(metadata.st_mode) != _RUNTIME_CONFIG_TARGET_MODE:
        raise _kanban_storage_error("config.yaml", "mode_mismatch")
    try:
        descriptor = os.open(
            path,
            (os.O_RDONLY if child_owned else os.O_RDWR)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise _kanban_storage_error("config.yaml", "unavailable") from exc
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode) or current.st_ino != metadata.st_ino:
            raise _kanban_storage_error("config.yaml", "changed_type")
        if current.st_uid == supervisor_uid:
            try:
                if current.st_gid != workspace_gid:
                    os.fchown(descriptor, -1, workspace_gid)
                if stat.S_IMODE(current.st_mode) != _RUNTIME_CONFIG_TARGET_MODE:
                    os.fchmod(descriptor, _RUNTIME_CONFIG_TARGET_MODE)
                os.fsync(descriptor)
            except OSError as exc:
                raise _kanban_storage_error("config.yaml", "cannot_set_mode") from exc
        final = os.fstat(descriptor)
        if final.st_uid not in {supervisor_uid, child_uid}:
            raise _kanban_storage_error("config.yaml", "identity_mismatch")
        if final.st_gid != workspace_gid:
            raise _kanban_storage_error("config.yaml", "identity_mismatch")
        if stat.S_IMODE(final.st_mode) != _RUNTIME_CONFIG_TARGET_MODE:
            raise _kanban_storage_error("config.yaml", "mode_mismatch")
    finally:
        os.close(descriptor)


def ensure_hermes_child_home_ready(
    home: Path,
    *,
    supervisor_uid: int | None = None,
    child_uid: int = 10001,
    workspace_gid: int | None = None,
    require_config: bool = False,
) -> None:
    """Normalize only the upstream-owned Home skeleton before child startup."""

    selected_supervisor_uid = os.geteuid() if supervisor_uid is None else supervisor_uid
    selected_workspace_gid = os.getegid() if workspace_gid is None else workspace_gid
    _normalize_hermes_child_directory(
        home,
        "home",
        supervisor_uid=selected_supervisor_uid,
        child_uid=child_uid,
        workspace_gid=selected_workspace_gid,
        create=False,
    )
    for relative_path in HERMES_CHILD_HOME_DIRECTORIES:
        _normalize_hermes_child_directory(
            home / relative_path,
            relative_path,
            supervisor_uid=selected_supervisor_uid,
            child_uid=child_uid,
            workspace_gid=selected_workspace_gid,
            create=True,
        )
    _normalize_hermes_root_config(
        home / "config.yaml",
        supervisor_uid=selected_supervisor_uid,
        child_uid=child_uid,
        workspace_gid=selected_workspace_gid,
        required=require_config,
    )


def ensure_hermes_child_workspace_ready(
    workspace: Path,
    *,
    supervisor_uid: int | None = None,
    child_uid: int = 10001,
    workspace_gid: int | None = None,
) -> None:
    """Normalize the bounded workspace skeleton used by Dashboard sessions."""

    selected_supervisor_uid = os.geteuid() if supervisor_uid is None else supervisor_uid
    selected_workspace_gid = os.getegid() if workspace_gid is None else workspace_gid
    _normalize_hermes_child_directory(
        workspace,
        "workspace",
        supervisor_uid=selected_supervisor_uid,
        child_uid=child_uid,
        workspace_gid=selected_workspace_gid,
        create=True,
    )
    for relative_path in HERMES_CHILD_WORKSPACE_DIRECTORIES:
        _normalize_hermes_child_directory(
            workspace / relative_path,
            f"workspace/{relative_path}",
            supervisor_uid=selected_supervisor_uid,
            child_uid=child_uid,
            workspace_gid=selected_workspace_gid,
            create=True,
        )


def _require_kanban_directory(
    path: Path,
    relative_path: str,
    *,
    supervisor_uid: int,
    child_uid: int,
    workspace_gid: int,
) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise _kanban_storage_error(relative_path, "unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise _kanban_storage_error(relative_path, "not_directory")
    if (
        metadata.st_uid not in {supervisor_uid, child_uid}
        or metadata.st_gid != workspace_gid
    ):
        raise _kanban_storage_error(relative_path, "identity_mismatch")
    if stat.S_IMODE(metadata.st_mode) != 0o2770:
        raise _kanban_storage_error(relative_path, "mode_mismatch")


def _ensure_kanban_directory(
    path: Path,
    relative_path: str,
    *,
    supervisor_uid: int,
    child_uid: int,
    workspace_gid: int,
) -> None:
    created = False
    try:
        path.mkdir(mode=0o770)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise _kanban_storage_error(relative_path, "cannot_create") from exc
    if created:
        try:
            os.chown(path, supervisor_uid, workspace_gid, follow_symlinks=False)
            os.chmod(path, 0o2770, follow_symlinks=False)
        except (NotImplementedError, OSError) as exc:
            raise _kanban_storage_error(relative_path, "cannot_set_mode") from exc
    _require_kanban_directory(
        path,
        relative_path,
        supervisor_uid=supervisor_uid,
        child_uid=child_uid,
        workspace_gid=workspace_gid,
    )


def _probe_kanban_directory(path: Path, relative_path: str) -> None:
    probe = path / f".platform-write-probe-{secrets.token_hex(8)}"
    descriptor: int | None = None
    created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(probe, flags, 0o600)
        created = True
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        probe.unlink()
        created = False
    except OSError as exc:
        raise _kanban_storage_error(relative_path, "write_probe_failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                probe.unlink()
            except OSError:
                logger.exception(
                    "Runtime Kanban write probe cleanup failed",
                    extra={
                        "component": "process_manager",
                        "error_code": KANBAN_STORAGE_ERROR,
                        "relative_path": relative_path,
                    },
                )


def _validate_kanban_database(
    path: Path,
    relative_path: str,
    *,
    supervisor_uid: int,
    child_uid: int,
    workspace_gid: int,
    child_open_probe: Callable[[Path], None],
) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _kanban_storage_error(relative_path, "unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise _kanban_storage_error(relative_path, "not_regular_file")
    if (
        metadata.st_uid not in {supervisor_uid, child_uid}
        or metadata.st_gid != workspace_gid
    ):
        raise _kanban_storage_error(relative_path, "identity_mismatch")
    if metadata.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise _kanban_storage_error(relative_path, "unsafe_mode")
    if stat.S_IMODE(metadata.st_mode) & stat.S_IWOTH:
        raise _kanban_storage_error(relative_path, "unsafe_mode")
    try:
        child_open_probe(path)
    except OSError as exc:
        raise _kanban_storage_error(relative_path, "not_writable") from exc


def _probe_kanban_database_as_current_process(path: Path) -> None:
    """Test/default probe; production supplies the isolated Hermes-child probe."""

    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("kanban database changed type")
    finally:
        os.close(descriptor)


def ensure_kanban_storage_ready(
    home: Path,
    *,
    supervisor_uid: int | None = None,
    child_uid: int = 10001,
    workspace_gid: int | None = None,
    child_open_probe: Callable[[Path], None] | None = None,
) -> None:
    """Create and validate the bounded storage used by native Kanban workers."""
    selected_supervisor_uid = os.geteuid() if supervisor_uid is None else supervisor_uid
    selected_workspace_gid = os.getegid() if workspace_gid is None else workspace_gid
    selected_child_open_probe = (
        _probe_kanban_database_as_current_process
        if child_open_probe is None
        else child_open_probe
    )
    ensure_hermes_child_home_ready(
        home,
        supervisor_uid=selected_supervisor_uid,
        child_uid=child_uid,
        workspace_gid=selected_workspace_gid,
    )
    for relative_path in KANBAN_DIRECTORIES:
        _ensure_kanban_directory(
            home / relative_path,
            relative_path,
            supervisor_uid=selected_supervisor_uid,
            child_uid=child_uid,
            workspace_gid=selected_workspace_gid,
        )
    for relative_path in ("kanban/workspaces", "kanban/logs"):
        _probe_kanban_directory(home / relative_path, relative_path)
    for relative_path in KANBAN_DATABASE_FILES:
        _validate_kanban_database(
            home / relative_path,
            relative_path,
            supervisor_uid=selected_supervisor_uid,
            child_uid=child_uid,
            workspace_gid=selected_workspace_gid,
            child_open_probe=selected_child_open_probe,
        )


def harden_process_secret_boundary() -> None:
    """Prevent same-UID Hermes tools from reading Supervisor-only memory/env."""
    if sys.platform != "linux":
        raise RuntimeError("Supervisor secret isolation requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number or errno.EPERM,
            "cannot disable Supervisor process dumpability",
        )


SAFE_PARENT_ENVIRONMENT = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "HERMES_TUI_WS_ORPHAN_REAP_GRACE_S",
        "ENTERPRISE_KNOWLEDGE_ROOT",
        "HERMES_PLATFORM_API_BASE_URL",
        "HERMES_RUNTIME_WORKSPACE_ID",
        "HERMES_APPLICATION_AGENT_ID",
        "HERMES_RUNTIME_SUPERVISOR_URL",
    }
)


def build_hermes_environment(
    settings: SupervisorSettings,
    *,
    dashboard_session_token: str,
    binding_cjk_fts: bool = True,
    parent_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = parent_environment if parent_environment is not None else os.environ
    environment = {
        key: value
        for key, value in source.items()
        if key in SAFE_PARENT_ENVIRONMENT
    }
    environment.update(
        {
            # The isolated Hermes uid intentionally has no host passwd entry.
            # Upstream calls Path.home() during CLI import before it resolves
            # HERMES_HOME, so pin the standard home variable to the same
            # managed Runtime home instead of inheriting Supervisor's home.
            "HOME": str(settings.HERMES_HOME),
            "HERMES_HOME": str(settings.HERMES_HOME),
            # Upstream ensure_hermes_home() otherwise resets the shared
            # Runtime home to owner-only 0700 on every Dashboard start.  The
            # isolated Hermes child (UID 10001) reaches this home only via the
            # workspace group, so preserve the migration's setgid,
            # group-writable directory contract.  Do not add the sticky bit:
            # Linux protected_regular rejects upstream's O_CREAT reopen of
            # Supervisor-owned lock/database files in a sticky directory.
            # Do not set HERMES_MANAGED here: upstream interprets every
            # non-empty value as package-manager ownership and silently skips
            # save_config(), which makes Profile config/MCP writes return HTTP
            # success without persistence. The fixed child umask and this
            # explicit mode preserve the shared-group storage contract.
            "HERMES_HOME_MODE": "2770",
            # Hermes' supported opt-out prevents save_config() from forcing
            # the shared root config back to owner-only 0600 when cgroup v2
            # exposes /proc/1/cgroup as ``0::/`` and upstream container
            # detection cannot recognize Kubernetes.  atomic_yaml_write()
            # preserves the existing 0660 mode across Dashboard writes.
            "HERMES_SKIP_CHMOD": "1",
            "WORKSPACE_DIR": str(settings.WORKSPACE_DIR),
            "TERMINAL_CWD": str(settings.WORKSPACE_DIR),
            "HERMES_WRITE_SAFE_ROOT": str(settings.WORKSPACE_DIR),
            "HERMES_DASHBOARD_FILES_ROOT": str(settings.WORKSPACE_DIR),
            # Scratch Kanban workers must live below the same writable root
            # exposed to Hermes tools.  The Supervisor intentionally filters
            # its parent environment, so derive this child-process contract
            # from WORKSPACE_DIR instead of relying on a KService env var to
            # survive the secret-boundary allowlist.
            "HERMES_KANBAN_WORKSPACES_ROOT": str(
                settings.WORKSPACE_DIR / ".kanban-workspaces"
            ),
            "HERMES_SKIP_CONFIG_MIGRATION": "1",
            # Hermes' root config is the only effective CJK switch for the
            # single Dashboard process.  Profile SessionDB instances inherit
            # this same process-level value; they do not get an independent
            # environment.  Always pin the immutable image path instead of
            # allowing upstream to guess below the writable HERMES_HOME.
            "HERMES_CJK_FTS": "1" if binding_cjk_fts else "0",
            "HERMES_FTS5_CJK_SO": settings.HERMES_FTS5_CJK_SO,
            # Dashboard is bound to loopback.  Hermes protects that mode with
            # one ephemeral session token, not the remote Basic/Cookie gate.
            "HERMES_DASHBOARD_SESSION_TOKEN": dashboard_session_token,
            # Built-in platform plugins run inside Hermes and use the one
            # existing per-Runtime, scope-limited Backend key.  Do not add
            # model, database, Redis, WeCom, or Admin credentials here.
            "API_SERVER_KEY": settings.API_SERVER_KEY.get_secret_value(),
            "HERMES_RUNTIME_SUPERVISOR_URL": (
                f"http://127.0.0.1:{settings.RUNTIME_EDGE_PORT}"
            ),
            "OPENAI_API_KEY": "local-model-relay-placeholder",
        }
    )
    return environment


def _managed_config_error(reason: str) -> RuntimeError:
    return RuntimeError(f"{MANAGED_DASHBOARD_CONFIG_INVALID}: {reason}")


def binding_cjk_fts_from_root_config(
    config_path: Path,
    *,
    expected_schema: int,
) -> bool:
    """Return the one effective CJK flag without changing managed config.

    A missing file is tolerated only so the pure ProcessManager can still be
    unit-tested in isolation.  The production startup order runs the config
    controller first, so a managed Runtime always reaches this function with
    a committed ``config.yaml``.
    """

    try:
        encoded = config_path.read_bytes()
    except FileNotFoundError:
        if expected_schema >= 34:
            raise _managed_config_error("config_missing") from None
        return True
    except OSError as exc:
        raise _managed_config_error("config_unreadable") from exc
    return binding_cjk_fts_from_root_config_bytes(
        encoded,
        expected_schema=expected_schema,
    )


def binding_cjk_fts_from_root_config_bytes(
    encoded: bytes,
    *,
    expected_schema: int,
) -> bool:
    """Parse the process-wide CJK policy from already identity-bound bytes."""

    try:
        document = yaml.safe_load(encoded.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise _managed_config_error("config_yaml_invalid") from exc
    if not isinstance(document, dict):
        raise _managed_config_error("config_not_mapping")
    if document.get("_config_version") != expected_schema:
        raise _managed_config_error("config_schema_mismatch")

    sessions = document.get("sessions", {})
    if not isinstance(sessions, dict):
        raise _managed_config_error("sessions_not_mapping")
    cjk_fts = sessions.get("cjk_fts", True)
    if not isinstance(cjk_fts, bool):
        raise _managed_config_error("sessions_cjk_fts_not_boolean")

    if expected_schema >= 34:
        display = document.get("display")
        if not isinstance(display, dict) or display.get("busy_input_mode") != "queue":
            raise _managed_config_error("display_busy_input_mode_not_queue")
        desktop = document.get("desktop")
        auto_continue = (
            desktop.get("auto_continue") if isinstance(desktop, dict) else None
        )
        if (
            not isinstance(auto_continue, dict)
            or auto_continue.get("enabled") is not False
        ):
            raise _managed_config_error("desktop_auto_continue_not_disabled")
    return cjk_fts


class ProcessManager:
    def __init__(
        self,
        settings: SupervisorSettings,
        *,
        parent_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.dashboard_session_token = secrets.token_urlsafe(32)
        self.dashboard_boot_id = f"dashboard-{secrets.token_hex(16)}"
        self.main_boundary_marker_key = secrets.token_bytes(32)
        self.main_boundary_epoch_nonce = _base64url_no_pad(
            secrets.token_bytes(16)
        )
        self.environment = build_hermes_environment(
            settings,
            dashboard_session_token=self.dashboard_session_token,
            parent_environment=parent_environment,
        )
        self.dashboard: asyncio.subprocess.Process | None = None
        self.kanban: asyncio.subprocess.Process | None = None
        self._stopping = False
        self._monitor_task: asyncio.Task[None] | None = None

    def refresh_managed_dashboard_environment(self) -> None:
        binding_cjk_fts = binding_cjk_fts_from_root_config(
            self.settings.HERMES_HOME / "config.yaml",
            expected_schema=self.settings.HERMES_EXPECTED_CONFIG_SCHEMA,
        )
        self.environment["HERMES_CJK_FTS"] = "1" if binding_cjk_fts else "0"
        self.environment["HERMES_FTS5_CJK_SO"] = self.settings.HERMES_FTS5_CJK_SO

    @property
    def dashboard_argv(self) -> tuple[str, ...]:
        return (
            str(self.settings.HERMES_EXECUTABLE),
            "dashboard",
            "--host",
            self.settings.HERMES_DASHBOARD_HOST,
            "--port",
            str(self.settings.HERMES_DASHBOARD_PORT),
            "--no-open",
            "--skip-build",
        )

    @property
    def kanban_argv(self) -> tuple[str, ...]:
        return (
            str(self.settings.HERMES_EXECUTABLE),
            "kanban",
            "daemon",
            "--force",
            "--interval",
            str(self.settings.HERMES_KANBAN_DISPATCH_INTERVAL_SECONDS),
            "--pidfile",
            "/tmp/hermes-kanban-dispatcher.pid",
            "--verbose",
        )

    async def start(self) -> None:
        await self._start(start_kanban=self.settings.HERMES_KANBAN_ORCHESTRATOR_ENABLED)

    async def start_dashboard_only(self) -> None:
        """Start the Dashboard without admitting new Kanban daemon work."""

        await self._start(start_kanban=False)

    async def _start(self, *, start_kanban: bool) -> None:
        if self.dashboard is not None:
            raise RuntimeError("Hermes Dashboard process was already started")
        if (
            self.settings.HERMES_HOME.exists()
            or self.settings.HERMES_EXPECTED_CONFIG_SCHEMA >= 34
        ):
            ensure_hermes_child_home_ready(
                self.settings.HERMES_HOME,
                supervisor_uid=os.geteuid(),
                child_uid=self.settings.HERMES_CHILD_UID,
                workspace_gid=self.settings.HERMES_WORKSPACE_GID,
                require_config=self.settings.HERMES_EXPECTED_CONFIG_SCHEMA >= 34,
            )
        self.refresh_managed_dashboard_environment()
        ensure_hermes_child_workspace_ready(
            self.settings.WORKSPACE_DIR,
            supervisor_uid=os.geteuid(),
            child_uid=self.settings.HERMES_CHILD_UID,
            workspace_gid=self.settings.HERMES_WORKSPACE_GID,
        )
        if start_kanban:
            ensure_kanban_storage_ready(
                self.settings.HERMES_HOME,
                supervisor_uid=os.geteuid(),
                child_uid=self.settings.HERMES_CHILD_UID,
                workspace_gid=self.settings.HERMES_WORKSPACE_GID,
                child_open_probe=self._probe_kanban_database_as_hermes_child,
            )
        self.dashboard = await self._spawn_dashboard()
        if start_kanban:
            self.kanban = await self._spawn(self.kanban_argv)
        self._monitor_task = asyncio.create_task(
            self._monitor_dashboard(),
            name="hermes-dashboard-monitor",
        )

    def _probe_kanban_database_as_hermes_child(self, path: Path) -> None:
        """Verify the fixed DB path with the actual Hermes child identity.

        The Supervisor itself is UID10000, while a legacy Kanban DB may be
        private to UID10001.  Only the child can authoritatively establish
        whether that file is writable by the process which will consume it.
        """

        try:
            completed = subprocess.run(
                (
                    str(self.settings.HERMES_CHILD_EXECUTABLE),
                    "--uid",
                    str(self.settings.HERMES_CHILD_UID),
                    "--gid",
                    str(self.settings.HERMES_CHILD_GID),
                    "--supplemental-gid",
                    str(self.settings.HERMES_WORKSPACE_GID),
                    "--",
                    sys.executable,
                    "-c",
                    _KANBAN_CHILD_OPEN_PROBE,
                    os.fspath(path),
                ),
                check=False,
                close_fds=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise OSError(
                exc.errno or errno.EACCES,
                "cannot execute Hermes child Kanban access probe",
            ) from exc
        if completed.returncode != 0:
            raise OSError(errno.EACCES, "Hermes child cannot write Kanban database")

    async def stop(self, *, timeout_seconds: float = 30) -> None:
        self._stopping = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        for process in (self.kanban, self.dashboard):
            if process is not None and process.returncode is None:
                self._signal_process_group(process, signal.SIGTERM)
        pending = [
            process.wait()
            for process in (self.kanban, self.dashboard)
            if process is not None and process.returncode is None
        ]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                for process in (self.kanban, self.dashboard):
                    if process is not None and process.returncode is None:
                        self._signal_process_group(process, signal.SIGKILL)
                await asyncio.gather(
                    *(
                        process.wait()
                        for process in (self.kanban, self.dashboard)
                        if process is not None
                    ),
                    return_exceptions=True,
                )

    async def wait_until_dashboard_listens(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.HERMES_STARTUP_TIMEOUT_SECONDS
        while loop.time() < deadline:
            if self.dashboard is None or self.dashboard.returncode is not None:
                raise RuntimeError("Hermes Dashboard exited before becoming ready")
            try:
                _, writer = await asyncio.open_connection(
                    self.settings.HERMES_DASHBOARD_HOST,
                    self.settings.HERMES_DASHBOARD_PORT,
                )
            except OSError:
                await asyncio.sleep(0.1)
                continue
            writer.close()
            await writer.wait_closed()
            return
        raise TimeoutError("Hermes Dashboard did not become ready before startup timeout")

    async def _spawn_dashboard(self) -> asyncio.subprocess.Process:
        bootstrap = _main_boundary_bootstrap_bytes(
            marker_key=self.main_boundary_marker_key,
            boundary_epoch_nonce=self.main_boundary_epoch_nonce,
        )
        memfd_flags = getattr(os, "MFD_CLOEXEC", _MFD_CLOEXEC) | getattr(
            os,
            "MFD_ALLOW_SEALING",
            _MFD_ALLOW_SEALING,
        )
        descriptor = create_anonymous_memory_file(
            "hermes-main-boundary-bootstrap",
            flags=memfd_flags,
        )
        fixed_descriptor_open = False
        try:
            written = 0
            while written < len(bootstrap):
                count = os.write(descriptor, bootstrap[written:])
                if count <= 0:
                    raise OSError("cannot write main boundary bootstrap")
                written += count
            os.lseek(descriptor, 0, os.SEEK_SET)
            seal_mask = 0
            for name in (
                "F_SEAL_SEAL",
                "F_SEAL_SHRINK",
                "F_SEAL_GROW",
                "F_SEAL_WRITE",
            ):
                seal_mask |= int(getattr(fcntl, name, 0))
            if seal_mask and hasattr(fcntl, "F_ADD_SEALS"):
                fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seal_mask)

            if descriptor != MAIN_BOUNDARY_BOOTSTRAP_FD:
                try:
                    os.fstat(MAIN_BOUNDARY_BOOTSTRAP_FD)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise
                else:
                    raise RuntimeError(
                        "main boundary bootstrap descriptor is already occupied"
                    )
                os.dup2(
                    descriptor,
                    MAIN_BOUNDARY_BOOTSTRAP_FD,
                    inheritable=True,
                )
            else:
                os.set_inheritable(descriptor, True)
            fixed_descriptor_open = True
            return await self._spawn(
                self.dashboard_argv,
                pass_fds=(MAIN_BOUNDARY_BOOTSTRAP_FD,),
            )
        finally:
            if fixed_descriptor_open:
                os.close(MAIN_BOUNDARY_BOOTSTRAP_FD)
            if descriptor != MAIN_BOUNDARY_BOOTSTRAP_FD or not fixed_descriptor_open:
                os.close(descriptor)

    async def _spawn(
        self,
        argv: tuple[str, ...],
        *,
        pass_fds: tuple[int, ...] = (),
    ) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            str(self.settings.HERMES_CHILD_EXECUTABLE),
            "--uid",
            str(self.settings.HERMES_CHILD_UID),
            "--gid",
            str(self.settings.HERMES_CHILD_GID),
            "--supplemental-gid",
            str(self.settings.HERMES_WORKSPACE_GID),
            "--",
            *argv,
            cwd=self.settings.WORKSPACE_DIR,
            env=self.environment,
            start_new_session=True,
            pass_fds=pass_fds,
        )

    async def _monitor_dashboard(self) -> None:
        assert self.dashboard is not None
        return_code = await self.dashboard.wait()
        if self._stopping:
            return
        logger.critical(
            "Hermes Dashboard exited unexpectedly",
            extra={"component": "process_manager", "return_code": return_code},
        )
        os.kill(os.getpid(), signal.SIGTERM)

    @staticmethod
    def _signal_process_group(
        process: asyncio.subprocess.Process,
        process_signal: signal.Signals,
    ) -> None:
        try:
            os.killpg(process.pid, process_signal)
        except ProcessLookupError:
            return
