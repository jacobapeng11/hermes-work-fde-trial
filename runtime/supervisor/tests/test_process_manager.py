from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import stat
import subprocess
from unittest.mock import AsyncMock

import pytest

from platform_supervisor.process_manager import (
    HERMES_CHILD_HOME_DIRECTORIES,
    HERMES_CHILD_WORKSPACE_DIRECTORIES,
    KanbanStoragePermissionError,
    MAIN_BOUNDARY_BOOTSTRAP_FD,
    ProcessManager,
    create_anonymous_memory_file,
    binding_cjk_fts_from_root_config,
    binding_cjk_fts_from_root_config_bytes,
    build_hermes_environment,
    ensure_kanban_storage_ready,
    ensure_hermes_child_home_ready,
    ensure_hermes_child_workspace_ready,
    harden_process_secret_boundary,
)
from platform_supervisor.settings import SupervisorSettings


def settings(**overrides: object) -> SupervisorSettings:
    values = {
        "RUNTIME_ID": "runtime-application-3",
        "RUNTIME_BINDING_ID": "binding-3",
        "RUNTIME_MODE": "application",
        "HERMES_STARTUP_MODE": "steady_state",
        "API_SERVER_KEY": "runtime-only-secret-0123456789abcdef",
        "NEWAPI_API_KEY": "model-gateway-only-secret-0123456789",
        "NEWAPI_BASE_URL": "https://newapi.example/v1",
    }
    values.update(overrides)
    return SupervisorSettings(_env_file=None, **values)


def test_hermes_environment_inherits_only_runtime_scoped_plugin_key() -> None:
    environment = build_hermes_environment(
        settings(),
        dashboard_session_token="ephemeral-dashboard-session-token",
        parent_environment={
            "PATH": "/usr/bin",
            "API_SERVER_KEY": "must-not-leak",
            "NEWAPI_API_KEY": "must-not-leak",
            "MYSQL_PASSWORD": "must-not-leak",
            "REDIS_URL": "must-not-leak",
            "ENTERPRISE_IDP_AES_KEY": "must-not-leak",
            "HERMES_RUNTIME_WORKSPACE_ID": "41",
            "HERMES_MANAGED": "platform",
        },
    )

    assert environment["PATH"] == "/usr/bin"
    assert environment["HOME"] == str(settings().HERMES_HOME)
    assert "HERMES_MANAGED" not in environment
    assert environment["HERMES_HOME_MODE"] == "2770"
    assert environment["HERMES_SKIP_CHMOD"] == "1"
    assert environment["HERMES_DASHBOARD_SESSION_TOKEN"] == (
        "ephemeral-dashboard-session-token"
    )
    assert "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" not in environment
    assert "HERMES_DASHBOARD_BASIC_AUTH_SECRET" not in environment
    assert environment["API_SERVER_KEY"] == (
        "runtime-only-secret-0123456789abcdef"
    )
    assert environment["HERMES_RUNTIME_WORKSPACE_ID"] == "41"
    assert environment["OPENAI_API_KEY"] == "local-model-relay-placeholder"
    assert environment["HERMES_CJK_FTS"] == "1"
    assert environment["HERMES_FTS5_CJK_SO"] == (
        "/opt/hermes-platform/lib/libfts5_cjk.so"
    )
    assert environment["HERMES_KANBAN_WORKSPACES_ROOT"] == (
        str(settings().WORKSPACE_DIR / ".kanban-workspaces")
    )
    assert "NEWAPI_API_KEY" not in environment
    assert "MYSQL_PASSWORD" not in environment
    assert "REDIS_URL" not in environment
    assert "ENTERPRISE_IDP_AES_KEY" not in environment


def test_binding_cjk_uses_root_config_and_target_safety_invariants(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """\
_config_version: 34
sessions:
  cjk_fts: false
display:
  busy_input_mode: queue
desktop:
  auto_continue:
    enabled: false
""",
        encoding="utf-8",
    )

    assert binding_cjk_fts_from_root_config(config, expected_schema=34) is False


def test_binding_cjk_bytes_use_same_default_as_dashboard_runtime() -> None:
    encoded = (
        b"_config_version: 34\n"
        b"sessions: {max_resume_messages: 0}\n"
        b"display: {busy_input_mode: queue}\n"
        b"desktop: {auto_continue: {enabled: false}}\n"
    )

    assert (
        binding_cjk_fts_from_root_config_bytes(encoded, expected_schema=34)
        is True
    )


@pytest.mark.parametrize(
    "config,reason",
    [
        (
            "_config_version: 34\nsessions: {cjk_fts: true}\n"
            "display: {busy_input_mode: interrupt}\n"
            "desktop: {auto_continue: {enabled: false}}\n",
            "display_busy_input_mode_not_queue",
        ),
        (
            "_config_version: 34\nsessions: {cjk_fts: true}\n"
            "display: {busy_input_mode: queue}\n"
            "desktop: {auto_continue: {enabled: true}}\n",
            "desktop_auto_continue_not_disabled",
        ),
        (
            "_config_version: 34\nsessions: {cjk_fts: 'false'}\n"
            "display: {busy_input_mode: queue}\n"
            "desktop: {auto_continue: {enabled: false}}\n",
            "sessions_cjk_fts_not_boolean",
        ),
    ],
)
def test_target_dashboard_config_fails_closed(
    tmp_path: Path,
    config: str,
    reason: str,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(config, encoding="utf-8")

    with pytest.raises(RuntimeError, match=reason):
        binding_cjk_fts_from_root_config(path, expected_schema=34)


def test_target_dashboard_rejects_missing_committed_config(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="config_missing"):
        binding_cjk_fts_from_root_config(
            tmp_path / "config.yaml",
            expected_schema=34,
        )


@pytest.mark.asyncio
async def test_dashboard_receives_binding_level_cjk_after_config_commit(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    (home / "config.yaml").write_text(
        """\
_config_version: 34
sessions: {cjk_fts: false}
display: {busy_input_mode: queue}
desktop: {auto_continue: {enabled: false}}
""",
        encoding="utf-8",
    )
    manager = ProcessManager(
        settings(
            HERMES_HOME=home,
            WORKSPACE_DIR=workspace,
            HERMES_EXPECTED_CONFIG_SCHEMA=34,
        ),
        parent_environment={"HERMES_CJK_FTS": "1"},
    )
    dashboard = object()
    manager._spawn = AsyncMock(return_value=dashboard)
    manager._monitor_dashboard = AsyncMock()

    await manager.start()

    assert manager.environment["HERMES_CJK_FTS"] == "0"
    assert manager.environment["HERMES_FTS5_CJK_SO"] == (
        "/opt/hermes-platform/lib/libfts5_cjk.so"
    )


def test_kanban_workspace_root_is_derived_from_runtime_workspace() -> None:
    runtime_workspace = Path("/data/workspace")

    environment = build_hermes_environment(
        settings(WORKSPACE_DIR=runtime_workspace),
        dashboard_session_token="ephemeral-dashboard-session-token",
        parent_environment={
            "HERMES_KANBAN_WORKSPACES_ROOT": "/opt/data/kanban/workspaces",
        },
    )

    assert environment["HERMES_WRITE_SAFE_ROOT"] == str(runtime_workspace)
    assert environment["HERMES_KANBAN_WORKSPACES_ROOT"] == (
        "/data/workspace/.kanban-workspaces"
    )


def test_dashboard_is_localhost_and_kanban_uses_official_cli() -> None:
    manager = ProcessManager(
        settings(HERMES_KANBAN_ORCHESTRATOR_ENABLED=True),
        parent_environment={},
    )

    assert manager.dashboard_argv[1:] == (
        "dashboard",
        "--host",
        "127.0.0.1",
        "--port",
        "9119",
        "--no-open",
        "--skip-build",
    )
    assert manager.kanban_argv[1:4] == ("kanban", "daemon", "--force")
    assert manager.kanban_argv[-3:-1] == (
        "--pidfile",
        "/tmp/hermes-kanban-dispatcher.pid",
    )


def test_hermes_child_home_preflight_repairs_known_legacy_metadata(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    for relative_path in HERMES_CHILD_HOME_DIRECTORIES:
        path = home / relative_path
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    config_path = home / "config.yaml"
    config_bytes = b"_config_version: 34\n"
    config_path.write_bytes(config_bytes)
    config_path.chmod(0o640)

    ensure_hermes_child_home_ready(
        home,
        supervisor_uid=os.geteuid(),
        child_uid=10001,
        workspace_gid=os.getegid(),
        require_config=True,
    )

    assert stat.S_IMODE(home.stat().st_mode) == 0o2770
    for relative_path in HERMES_CHILD_HOME_DIRECTORIES:
        metadata = (home / relative_path).stat()
        assert metadata.st_uid == os.geteuid()
        assert metadata.st_gid == os.getegid()
        assert stat.S_IMODE(metadata.st_mode) == 0o2770
    assert config_path.read_bytes() == config_bytes
    assert config_path.stat().st_gid == os.getegid()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o660


def test_hermes_child_home_preflight_accepts_child_owned_shared_config(
    tmp_path: Path,
) -> None:
    if os.geteuid() != 0:
        pytest.skip("child-owned config test requires the root test container")
    home = tmp_path / "home"
    home.mkdir(mode=0o2770)
    config_path = home / "config.yaml"
    config_path.write_text("_config_version: 34\n", encoding="utf-8")
    os.chown(config_path, 10001, os.getegid())
    config_path.chmod(0o660)

    ensure_hermes_child_home_ready(
        home,
        supervisor_uid=os.geteuid(),
        child_uid=10001,
        workspace_gid=os.getegid(),
        require_config=True,
    )

    metadata = config_path.stat()
    assert metadata.st_uid == 10001
    assert metadata.st_gid == os.getegid()
    assert stat.S_IMODE(metadata.st_mode) == 0o660


def test_hermes_child_home_preflight_rejects_child_private_config(
    tmp_path: Path,
) -> None:
    if os.geteuid() != 0:
        pytest.skip("child-owned config test requires the root test container")
    home = tmp_path / "home"
    home.mkdir(mode=0o2770)
    config_path = home / "config.yaml"
    config_path.write_text("_config_version: 34\n", encoding="utf-8")
    os.chown(config_path, 10001, os.getegid())
    config_path.chmod(0o600)

    with pytest.raises(
        KanbanStoragePermissionError,
        match="KANBAN_STORAGE_PERMISSION_INVALID: config.yaml: mode_mismatch",
    ):
        ensure_hermes_child_home_ready(
            home,
            supervisor_uid=os.geteuid(),
            child_uid=10001,
            workspace_gid=os.getegid(),
            require_config=True,
        )


def test_hermes_child_workspace_preflight_repairs_session_parent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o770)
    sessions = workspace / "sessions"
    sessions.mkdir(mode=0o755)

    ensure_hermes_child_workspace_ready(workspace)

    assert stat.S_IMODE(workspace.stat().st_mode) == 0o2770
    for relative_path in HERMES_CHILD_WORKSPACE_DIRECTORIES:
        path = workspace / relative_path
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o2770


def test_hermes_child_home_preflight_repairs_config_workspace_group(
    tmp_path: Path,
) -> None:
    if os.geteuid() != 0:
        pytest.skip("group migration test requires the root test container")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    config_path = home / "config.yaml"
    config_path.write_text("_config_version: 34\n", encoding="utf-8")
    wrong_gid = 10000 if os.getegid() != 10000 else 10001
    os.chown(config_path, os.geteuid(), wrong_gid)
    config_path.chmod(0o660)

    ensure_hermes_child_home_ready(
        home,
        supervisor_uid=os.geteuid(),
        child_uid=10001,
        workspace_gid=os.getegid(),
        require_config=True,
    )

    assert config_path.stat().st_gid == os.getegid()


def test_hermes_child_home_preflight_rejects_symlinked_known_directory(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / "logs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        KanbanStoragePermissionError,
        match="KANBAN_STORAGE_PERMISSION_INVALID: logs: not_directory",
    ):
        ensure_hermes_child_home_ready(home)

    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


def test_hermes_child_home_preflight_rejects_symlinked_config(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside.yaml"
    home.mkdir()
    outside.write_text("secret: unchanged\n", encoding="utf-8")
    (home / "config.yaml").symlink_to(outside)

    with pytest.raises(
        KanbanStoragePermissionError,
        match="KANBAN_STORAGE_PERMISSION_INVALID: config.yaml: not_regular_file",
    ):
        ensure_hermes_child_home_ready(home, require_config=True)

    assert outside.read_text(encoding="utf-8") == "secret: unchanged\n"


def test_kanban_storage_preflight_creates_exact_directories_and_cleans_probes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o700)

    ensure_kanban_storage_ready(home)

    assert stat.S_IMODE(home.stat().st_mode) == 0o2770
    for relative in (
        "kanban",
        "kanban/workspaces",
        "kanban/logs",
        "kanban/boards",
    ):
        path = home / relative
        metadata = path.stat()
        assert metadata.st_uid == os.geteuid()
        assert metadata.st_gid == os.getegid()
        assert stat.S_IMODE(metadata.st_mode) == 0o2770
    assert list((home / "kanban" / "workspaces").iterdir()) == []
    assert list((home / "kanban" / "logs").iterdir()) == []


def test_kanban_storage_preflight_accepts_native_sqlite_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    ensure_kanban_storage_ready(home)
    database = home / "kanban.db"
    database.write_bytes(b"sqlite")
    database.chmod(0o660)

    ensure_kanban_storage_ready(home)

    assert database.read_bytes() == b"sqlite"
    assert stat.S_IMODE(database.stat().st_mode) == 0o660


def test_kanban_storage_preflight_accepts_child_owned_private_database(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    child_uid = os.geteuid()
    workspace_gid = os.getegid()
    ensure_kanban_storage_ready(
        home,
        supervisor_uid=child_uid,
        child_uid=child_uid,
        workspace_gid=workspace_gid,
    )
    database = home / "kanban.db"
    database.write_bytes(b"sqlite")
    database.chmod(0o640)

    probed: list[Path] = []

    def child_open_probe(path: Path) -> None:
        probed.append(path)
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        os.close(descriptor)

    ensure_kanban_storage_ready(
        home,
        supervisor_uid=child_uid,
        child_uid=child_uid,
        workspace_gid=workspace_gid,
        child_open_probe=child_open_probe,
    )

    assert probed == [database]
    assert stat.S_IMODE(database.stat().st_mode) == 0o640


def test_kanban_storage_preflight_accepts_normalized_workspace_group(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workspace_gid = os.getegid()
    ensure_kanban_storage_ready(
        home,
        supervisor_uid=os.geteuid(),
        child_uid=10001,
        workspace_gid=workspace_gid,
    )
    database = home / "kanban.db"
    database.write_bytes(b"sqlite")
    database.chmod(0o660)

    ensure_kanban_storage_ready(
        home,
        supervisor_uid=os.geteuid(),
        child_uid=10001,
        workspace_gid=workspace_gid,
    )

    assert stat.S_IMODE((home / "kanban").stat().st_mode) == 0o2770
    assert database.stat().st_gid == workspace_gid


def test_kanban_storage_preflight_rejects_symlink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / "kanban").symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        KanbanStoragePermissionError,
        match="KANBAN_STORAGE_PERMISSION_INVALID: kanban: not_directory",
    ):
        ensure_kanban_storage_ready(home)

    assert list(outside.iterdir()) == []


def test_kanban_storage_preflight_rejects_unsafe_database_mode(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    ensure_kanban_storage_ready(home)
    database = home / "kanban.db"
    database.write_bytes(b"sqlite")
    database.chmod(0o666)

    with pytest.raises(
        KanbanStoragePermissionError,
        match="KANBAN_STORAGE_PERMISSION_INVALID: kanban.db: unsafe_mode",
    ):
        ensure_kanban_storage_ready(home)


def test_kanban_child_probe_uses_isolated_hermes_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ProcessManager(
        settings(HERMES_HOME=tmp_path / "home"),
        parent_environment={},
    )
    calls: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        calls["args"] = args
        calls["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=(), returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    manager._probe_kanban_database_as_hermes_child(tmp_path / "home" / "kanban.db")

    command = calls["args"][0]
    assert command[:8] == (
        "/opt/hermes/.venv/bin/platform-hermes-child-exec",
        "--uid",
        "10001",
        "--gid",
        "10001",
        "--supplemental-gid",
        "1000",
        "--",
    )
    assert calls["kwargs"] == {
        "check": False,
        "close_fds": True,
        "env": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }


@pytest.mark.asyncio
async def test_kanban_storage_failure_prevents_all_hermes_processes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    ensure_kanban_storage_ready(home)
    (home / "kanban" / "workspaces").chmod(0o755)
    manager = ProcessManager(
        settings(
            HERMES_HOME=home,
            WORKSPACE_DIR=workspace,
            HERMES_KANBAN_ORCHESTRATOR_ENABLED=True,
        ),
        parent_environment={},
    )
    manager._spawn = AsyncMock()

    with pytest.raises(
        KanbanStoragePermissionError,
        match="KANBAN_STORAGE_PERMISSION_INVALID",
    ):
        await manager.start()

    manager._spawn.assert_not_awaited()
    assert manager.dashboard is None
    assert manager.kanban is None


@pytest.mark.asyncio
async def test_application_runtime_skips_kanban_storage_preflight(
    tmp_path: Path,
) -> None:
    home = tmp_path / "missing-home"
    workspace = tmp_path / "workspace"
    manager = ProcessManager(
        settings(
            HERMES_HOME=home,
            WORKSPACE_DIR=workspace,
            HERMES_KANBAN_ORCHESTRATOR_ENABLED=False,
        ),
        parent_environment={},
    )
    dashboard = object()
    manager._spawn = AsyncMock(return_value=dashboard)
    manager._monitor_dashboard = AsyncMock()

    await manager.start()

    manager._spawn.assert_awaited_once_with(
        manager.dashboard_argv,
        pass_fds=(MAIN_BOUNDARY_BOOTSTRAP_FD,),
    )
    assert manager.dashboard is dashboard
    assert manager.kanban is None


@pytest.mark.asyncio
async def test_gate_dashboard_only_start_never_starts_kanban(
    tmp_path: Path,
) -> None:
    home = tmp_path / "missing-home"
    workspace = tmp_path / "workspace"
    manager = ProcessManager(
        settings(
            HERMES_HOME=home,
            WORKSPACE_DIR=workspace,
            HERMES_KANBAN_ORCHESTRATOR_ENABLED=True,
        ),
        parent_environment={},
    )
    dashboard = object()
    manager._spawn_dashboard = AsyncMock(return_value=dashboard)
    manager._spawn = AsyncMock()
    manager._monitor_dashboard = AsyncMock()

    await manager.start_dashboard_only()

    manager._spawn_dashboard.assert_awaited_once_with()
    manager._spawn.assert_not_awaited()
    assert manager.dashboard is dashboard
    assert manager.kanban is None


@pytest.mark.asyncio
async def test_dashboard_boundary_secret_uses_one_shot_fixed_fd_only(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    captured: dict[str, object] = {}
    dashboard = object()
    manager = ProcessManager(
        settings(
            HERMES_HOME=home,
            WORKSPACE_DIR=workspace,
            HERMES_KANBAN_ORCHESTRATOR_ENABLED=False,
        ),
        parent_environment={},
    )

    async def spawn(
        argv: tuple[str, ...],
        *,
        pass_fds: tuple[int, ...] = (),
    ) -> object:
        captured["argv"] = argv
        captured["pass_fds"] = pass_fds
        captured["bootstrap"] = json.loads(
            os.read(MAIN_BOUNDARY_BOOTSTRAP_FD, 4096).decode("ascii")
        )
        return dashboard

    manager._spawn = AsyncMock(side_effect=spawn)
    manager._monitor_dashboard = AsyncMock()

    await manager.start()

    assert captured["argv"] == manager.dashboard_argv
    assert captured["pass_fds"] == (MAIN_BOUNDARY_BOOTSTRAP_FD,)
    bootstrap = captured["bootstrap"]
    assert isinstance(bootstrap, dict)
    assert bootstrap == {
        "algorithm": "hmac-sha256",
        "boundary_epoch_nonce": manager.main_boundary_epoch_nonce,
        "contract_version": 1,
        "key_base64url": base64.urlsafe_b64encode(
            manager.main_boundary_marker_key
        )
        .rstrip(b"=")
        .decode("ascii"),
    }
    assert len(manager.main_boundary_marker_key) == 32
    assert "main_boundary" not in " ".join(manager.environment).lower()
    with pytest.raises(OSError):
        os.fstat(MAIN_BOUNDARY_BOOTSTRAP_FD)


def test_supervisor_disables_same_uid_process_introspection() -> None:
    harden_process_secret_boundary()


def test_anonymous_memory_file_uses_libc_when_python_lacks_memfd_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "memfd_create", raising=False)

    descriptor = create_anonymous_memory_file("hermes-boundary-test", flags=1)
    try:
        os.write(descriptor, b"marker")
        os.lseek(descriptor, 0, os.SEEK_SET)
        assert os.read(descriptor, 6) == b"marker"
        assert os.readlink(f"/proc/self/fd/{descriptor}").startswith(
            "/memfd:hermes-boundary-test"
        )
    finally:
        os.close(descriptor)


@pytest.mark.asyncio
async def test_every_hermes_child_uses_fixed_unprivileged_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = ProcessManager(
        settings(WORKSPACE_DIR=workspace),
        parent_environment={},
    )
    spawned = object()
    create = AsyncMock(return_value=spawned)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    result = await manager._spawn(("/opt/hermes/.venv/bin/hermes", "dashboard"))

    assert result is spawned
    args, kwargs = create.call_args
    assert args == (
        "/opt/hermes/.venv/bin/platform-hermes-child-exec",
        "--uid",
        "10001",
        "--gid",
        "10001",
        "--supplemental-gid",
        "1000",
        "--",
        "/opt/hermes/.venv/bin/hermes",
        "dashboard",
    )
    assert "user" not in kwargs
    assert "group" not in kwargs
    assert "preexec_fn" not in kwargs
