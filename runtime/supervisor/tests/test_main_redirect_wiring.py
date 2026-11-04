import base64
import json
from pathlib import Path

import pytest

import platform_supervisor.main as supervisor_main
from platform_supervisor.main import create_app
from platform_supervisor.health import StartupResourceSnapshot
from platform_supervisor.platform_control_identity import PlatformControlIdentityError
from platform_supervisor.settings import SupervisorSettings


def _settings(tmp_path: Path) -> SupervisorSettings:
    settings = SupervisorSettings(
        _env_file=None,
        RUNTIME_ID="runtime-application-3",
        RUNTIME_BINDING_ID="3",
        RUNTIME_MODE="application",
        HERMES_STARTUP_MODE="steady_state",
        API_SERVER_KEY="runtime-only-secret-0123456789abcdef",
        NEWAPI_API_KEY="model-gateway-only-secret-0123456789",
        NEWAPI_BASE_URL="https://newapi.example/v1",
        RUNTIME_DATA_ROOT=tmp_path / "runtime-data",
        HERMES_HOME=tmp_path / "hermes-home",
        WORKSPACE_DIR=tmp_path / "workspace",
        RUNTIME_PVC_STORAGE_IDENTITY_HASH="sha256:" + "d" * 64,
        HERMES_SUPERVISOR_POD_UID="pod-uid-steady-12345678",
        HERMES_KNATIVE_REVISION_NAME="revision-steady-12345678",
        PLATFORM_CONTROL_KEY_FILE=tmp_path / "control-keys.json",
    )
    _write_control_identity(settings)
    return settings


def _write_control_identity(settings: SupervisorSettings) -> None:
    encoded_key = base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode()
    settings.PLATFORM_CONTROL_KEY_FILE.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "runtime_id": settings.RUNTIME_ID,
                "binding_type": "application_runtime",
                "binding_id": 3,
                "pvc_storage_identity_hash": settings.RUNTIME_PVC_STORAGE_IDENTITY_HASH,
                "current_key_version": 1,
                "keys": {"1": encoded_key},
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    settings.PLATFORM_CONTROL_KEY_FILE.chmod(0o440)


def test_main_assembles_one_shared_redirect_boundary(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    assert app.state.runs.redirect_lifecycle is app.state.redirect_gate
    assert app.state.hermes_adapter.protocol.control_peers is app.state.control_peers
    assert app.state.model_relay._main_boundary is app.state.main_boundary
    assert app.state.redirect_gate._boundary is app.state.main_boundary
    assert app.state.main_boundary_boot.dashboard_boot_id == (
        app.state.process_manager.dashboard_boot_id
    )
    assert "/control-readyz" in {route.path for route in app.routes}


def test_all_startup_modes_require_binding_scoped_platform_control_identity(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.PLATFORM_CONTROL_KEY_FILE.unlink()

    with pytest.raises(
        PlatformControlIdentityError,
        match="platform control key file is unavailable",
    ):
        create_app(settings)


def test_steady_state_loads_provider_route_control_identity(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.PROVIDER_ROUTE_ACCEPTANCE_MODE = "required"

    app = create_app(settings)

    router = app.state.hermes_adapter.model_router
    binding = router._provider_route_binding
    assert app.state.health.control_ready is True
    assert router._provider_route_control_keys == {1: b"s" * 32}
    assert binding is not None
    assert binding.runtime_id == settings.RUNTIME_ID
    assert binding.binding_id == 3
    assert binding.pvc_storage_identity_hash == settings.RUNTIME_PVC_STORAGE_IDENTITY_HASH
    assert not settings.RUNTIME_DATA_ROOT.exists()


def test_fresh_bootstrap_uses_normal_execution_routes_without_migration_control(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.HERMES_STARTUP_MODE = "fresh_bootstrap"
    settings.RUNTIME_INITIAL_CONFIG_DOWNLOAD_URL = "http://backend/internal/runtime-config/3"
    settings.RUNTIME_INITIAL_CONFIG_DIGEST = "a" * 64
    settings.RUNTIME_BOOTSTRAP_INTENT_ID = "bootstrap-intent-3"
    settings.RUNTIME_BOOTSTRAP_AUTHORIZATION_DIGEST = "sha256:" + "b" * 64

    app = create_app(settings)
    paths = {route.path for route in app.routes}

    assert "/api/sessions" in paths
    assert "/internal/runtime-schema/intent" not in paths
    assert "/internal/runtime-schema/source-facts" not in paths
    assert "/internal/admission-barrier/status" not in paths
    assert not settings.RUNTIME_DATA_ROOT.exists()


@pytest.mark.asyncio
async def test_fresh_probe_is_read_only_ready_and_exposes_no_execution_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    settings.HERMES_STARTUP_MODE = "fresh_probe"
    settings.HERMES_HOME.mkdir()
    before = list(tmp_path.rglob("*"))
    monkeypatch.setattr(supervisor_main, "SIGTERM_RUN_DRAIN_SECONDS", 0)
    # The contract is validated against the Runtime container's limits.  Keep
    # this application-wiring test independent from a caller shell whose hard
    # nofile limit may be 65535 (one below the production floor).
    monkeypatch.setattr(
        supervisor_main,
        "probe_startup_resources",
        lambda *_args, **_kwargs: StartupResourceSnapshot(
            nofile_soft=65536,
            pids_limit=None,
            disk_used_percent=0.0,
            disk_free_bytes=3 * 1024 * 1024 * 1024,
            sqlite_ok=True,
        ),
    )

    app = create_app(settings)
    paths = {route.path for route in app.routes}
    assert "/api/sessions" not in paths
    assert "/internal/runtime-schema/intent" not in paths

    async with app.router.lifespan_context(app):
        assert app.state.health.ready is True
        assert not settings.RUNTIME_DATA_ROOT.exists()

    assert [path.relative_to(tmp_path) for path in tmp_path.rglob("*")] == [
        path.relative_to(tmp_path) for path in before
    ]
