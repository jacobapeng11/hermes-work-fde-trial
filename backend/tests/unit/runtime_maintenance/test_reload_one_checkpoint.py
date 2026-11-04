from __future__ import annotations

import argparse
from importlib.util import module_from_spec
from importlib.util import spec_from_file_location
import json
from pathlib import Path

from app.services.runtime_image_config import RuntimeImageConfig


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
TARGET = RuntimeImageConfig(
    image="kind.local/hermes-agent:code-new",
    manifest_digest="sha256:" + "a" * 64,
    config_id="sha256:" + "b" * 64,
)


def _module():
    path = REPOSITORY_ROOT / "scripts" / "reload-one-runtime-from-env-dev.py"
    spec = spec_from_file_location("reload_one_checkpoint", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_single_flow_archives_stale_failed_target_after_fleet_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    binding = {
        "binding_type": "personal_runtime",
        "binding_id": 41,
        "runtime_id": "rw-test",
        "namespace": "hermes-runtimes",
        "service_name": "runtime-test",
        "pvc_name": "runtime-test-root",
        "pvc_uid": "pvc-uid-41",
        "pvc_storage_identity_hash": "sha256:" + "1" * 64,
        "image": TARGET.image,
        "image_digest": TARGET.config_id,
        "status": "ready",
    }
    old_target = RuntimeImageConfig(
        image="kind.local/hermes-agent:code-old",
        manifest_digest="sha256:" + "d" * 64,
        config_id="sha256:" + "e" * 64,
    )
    calls: list[str] = []

    class _ConfigStore:
        def __init__(self, _path: Path) -> None:
            pass

        def read(self) -> RuntimeImageConfig:
            return TARGET

    def control(arguments: list[str], *, env_file: Path) -> str:
        assert env_file == (tmp_path / "backend/.env").resolve()
        calls.append(arguments[0])
        if arguments[0] == "list":
            return json.dumps([binding])
        if arguments[0] == "verify":
            return json.dumps({"ready": True})
        return "{}"

    monkeypatch.setattr(module, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(module, "RuntimeImageConfigStore", _ConfigStore)
    monkeypatch.setattr(module, "_control", control)
    monkeypatch.setattr(module, "_checked", lambda _command: "")
    checkpoint_path = (
        tmp_path / ".runtime/runtime-code-cutover/personal_runtime-rw-test.json"
    )
    checkpoint = module.RuntimeCutoverCheckpointStore(checkpoint_path)
    checkpoint.initialize(flow="single", target=old_target, bindings=[binding])
    checkpoint.advance(stage="failed", status="failed", error_summary="old target")

    args = argparse.Namespace(
        env_file=Path("backend/.env"),
        runtime_id="rw-test",
        binding_type="personal_runtime",
        skip_backend_restart=True,
        backend_lifecycle="dev",
        maintenance_epoch=None,
        supersede_failed_target=False,
    )

    assert module._run(args) == 0

    assert calls == [
        "list",
        "isolate",
        "cancel-active",
        "stop",
        "reload",
        "verify-sessions",
        "verify",
    ]
    assert len(
        list(checkpoint_path.parent.glob(checkpoint_path.name + ".failed-stale-*"))
    ) == 1
