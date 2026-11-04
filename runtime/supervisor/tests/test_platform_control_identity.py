from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from platform_supervisor.platform_control_identity import (
    PlatformControlIdentityError,
    load_platform_control_identity,
)
from platform_supervisor.secret_handoff import PLATFORM_CONTROL_KEY_FD_ENV
from platform_supervisor.settings import SupervisorSettings


PVC_DIGEST = "sha256:" + "a" * 64


def _settings(tmp_path: Path, **overrides: object) -> SupervisorSettings:
    values: dict[str, object] = {
        "_env_file": None,
        "RUNTIME_ID": "runtime-personal-7",
        "RUNTIME_BINDING_ID": "7",
        "RUNTIME_MODE": "personal",
        "HERMES_STARTUP_MODE": "steady_state",
        "API_SERVER_KEY": "runtime-only-secret-0123456789abcdef",
        "NEWAPI_API_KEY": "model-gateway-only-secret-0123456789",
        "NEWAPI_BASE_URL": "https://newapi.example/v1",
        "RUNTIME_DATA_ROOT": tmp_path / "runtime-data",
        "HERMES_HOME": tmp_path / "hermes-home",
        "WORKSPACE_DIR": tmp_path / "workspace",
        "HERMES_EXPECTED_CONFIG_SCHEMA": 34,
        "PLATFORM_CONTROL_KEY_FILE": tmp_path / "control-keys.json",
        "RUNTIME_PVC_STORAGE_IDENTITY_HASH": PVC_DIGEST,
        "HERMES_SUPERVISOR_POD_UID": "pod-uid-12345678",
        "HERMES_KNATIVE_REVISION_NAME": "runtime-revision-00001",
    }
    values.update(overrides)
    return SupervisorSettings(**values)


def _write_key_document(path: Path, *, runtime_id: str = "runtime-personal-7") -> None:
    root_key = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode()
    path.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "runtime_id": runtime_id,
                "binding_type": "personal_runtime",
                "binding_id": 7,
                "pvc_storage_identity_hash": PVC_DIGEST,
                "current_key_version": 2,
                "keys": {"1": root_key, "2": root_key},
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    path.chmod(0o440)


def test_target_loader_requires_live_control_identity(tmp_path: Path) -> None:
    settings = _settings(tmp_path, HERMES_SUPERVISOR_POD_UID=None)

    with pytest.raises(
        PlatformControlIdentityError,
        match="live Pod control identity is incomplete",
    ):
        load_platform_control_identity(
            settings,
            supervisor_boot_id="supervisor-boot-12345678",
        )


def test_loads_closed_binding_scoped_key_ring(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_key_document(settings.PLATFORM_CONTROL_KEY_FILE)

    loaded = load_platform_control_identity(
        settings,
        supervisor_boot_id="supervisor-boot-12345678",
    )

    assert loaded.current_key_version == 2
    assert loaded.control_root_keys == {1: b"k" * 32, 2: b"k" * 32}
    assert loaded.identity.runtime_id == settings.RUNTIME_ID
    assert loaded.identity.binding_type == "personal_runtime"
    assert loaded.identity.binding_id == 7
    assert loaded.identity.pod_uid == "pod-uid-12345678"


def test_loads_inherited_control_fd_once_and_closes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    inherited = tmp_path / "inherited-control-keys.json"
    _write_key_document(inherited)
    inherited.chmod(0o400)
    descriptor = os.open(inherited, os.O_RDONLY)
    monkeypatch.setenv(PLATFORM_CONTROL_KEY_FD_ENV, str(descriptor))

    loaded = load_platform_control_identity(
        settings,
        supervisor_boot_id="supervisor-boot-12345678",
    )

    assert loaded.current_key_version == 2
    assert PLATFORM_CONTROL_KEY_FD_ENV not in os.environ
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert not settings.PLATFORM_CONTROL_KEY_FILE.exists()


def test_identity_mismatch_and_symlink_fail_closed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    real = tmp_path / "real-control-keys.json"
    _write_key_document(real, runtime_id="runtime-other")
    settings.PLATFORM_CONTROL_KEY_FILE.symlink_to(real)

    with pytest.raises(
        PlatformControlIdentityError,
        match="key file is unavailable",
    ):
        load_platform_control_identity(
            settings,
            supervisor_boot_id="supervisor-boot-12345678",
        )


def test_group_writable_key_file_is_rejected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_key_document(settings.PLATFORM_CONTROL_KEY_FILE)
    settings.PLATFORM_CONTROL_KEY_FILE.chmod(0o460)

    with pytest.raises(
        PlatformControlIdentityError,
        match="permissions are too broad",
    ):
        load_platform_control_identity(
            settings,
            supervisor_boot_id="supervisor-boot-12345678",
        )


def test_schema_33_control_identity_is_usable_for_required_provider_routes(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        HERMES_EXPECTED_CONFIG_SCHEMA=33,
        PROVIDER_ROUTE_ACCEPTANCE_MODE="required",
    )
    _write_key_document(settings.PLATFORM_CONTROL_KEY_FILE)

    loaded = load_platform_control_identity(
        settings,
        supervisor_boot_id="supervisor-boot-12345678",
    )

    assert loaded.identity.runtime_id == settings.RUNTIME_ID
