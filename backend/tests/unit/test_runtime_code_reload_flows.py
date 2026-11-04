from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from app.services.runtime_image_config import RuntimeImageConfig
from app.services.runtime_reload import RuntimeReloadBinding


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TARGET = RuntimeImageConfig(
    image="kind.local/hermes-agent:code-new",
    manifest_digest="sha256:" + "a" * 64,
    config_id="sha256:" + "b" * 64,
)


def _load_script(name: str):
    path = REPOSITORY_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_backend_script(name: str):
    path = REPOSITORY_ROOT / "backend" / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ConfigStore:
    def __init__(self, _path: Path) -> None:
        pass

    def read(self) -> RuntimeImageConfig:
        return TARGET


def _binding(binding_type: str, binding_id: int, runtime_id: str) -> dict[str, object]:
    return {
        "binding_type": binding_type,
        "binding_id": binding_id,
        "runtime_id": runtime_id,
        "namespace": "hermes-runtimes",
        "service_name": f"runtime-{binding_id}",
        "pvc_name": f"runtime-{binding_id}-root",
        "pvc_uid": f"pvc-uid-{binding_id}",
        "pvc_storage_identity_hash": "sha256:" + f"{binding_id:x}".zfill(64),
        "image": "kind.local/hermes-agent:old",
        "image_digest": "sha256:" + "c" * 64,
        "status": "ready",
    }


def _classify_active(bindings: list[dict[str, object]]) -> str:
    return json.dumps(
        [
            {
                "binding_type": binding["binding_type"],
                "binding_id": binding["binding_id"],
                "runtime_id": binding["runtime_id"],
                "service_name": binding["service_name"],
                "running_pod_count": 1,
            }
            for binding in bindings
        ]
    )


def _single_flow_args(
    *,
    runtime_id: str,
    binding_type: str,
) -> argparse.Namespace:
    return argparse.Namespace(
        env_file=Path("backend/.env"),
        runtime_id=runtime_id,
        binding_type=binding_type,
        skip_backend_restart=True,
        backend_lifecycle="prod",
        maintenance_epoch=None,
        supersede_failed_target=False,
    )


@pytest.mark.parametrize("binding_type", ["personal_runtime", "application_runtime"])
def test_single_flow_targets_exact_binding_and_supports_restart_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    binding_type: str,
) -> None:
    module = _load_script("reload-one-runtime-from-env-dev.py")
    binding = _binding(binding_type, 41, "rw-test")
    calls: list[list[str]] = []
    env_files: list[Path] = []
    checked: list[list[str]] = []

    def control(arguments: list[str], *, env_file: Path) -> str:
        calls.append(arguments)
        env_files.append(env_file)
        if arguments[0] == "list":
            return json.dumps([binding])
        if arguments[0] == "verify":
            return json.dumps({"ready": True, "live_image_ids": [TARGET.config_id]})
        return "{}"

    monkeypatch.setattr(module, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(module, "RuntimeImageConfigStore", _ConfigStore)
    monkeypatch.setattr(module, "_control", control)
    monkeypatch.setattr(module, "_checked", lambda command: checked.append(command) or "")

    result = module._run(
        _single_flow_args(runtime_id="rw-test", binding_type=binding_type)
    )

    assert result == 0
    assert [call[0] for call in calls] == [
        "list",
        "isolate",
        "cancel-active",
        "stop",
        "reload",
        "verify-sessions",
        "verify",
    ]
    expected_selector = ["--binding-type", binding_type, "--binding-id", "41"]
    assert calls[1] == ["isolate", *expected_selector]
    assert calls[2] == ["cancel-active", *expected_selector]
    assert calls[3] == ["stop", *expected_selector]
    assert calls[4] == ["reload", *expected_selector]
    assert calls[5][:3] == ["verify-sessions", *expected_selector[:2]]
    assert calls[6] == ["verify", *expected_selector]
    assert env_files == [(tmp_path / "backend/.env").resolve()] * len(calls)
    assert len(checked) == 1
    checkpoint = json.loads(
        (
            tmp_path
            / ".runtime/runtime-code-cutover"
            / f"{binding_type}-rw-test.json"
        ).read_text(encoding="utf-8")
    )
    assert checkpoint["status"] == "completed"
    assert checkpoint["checks"]["technical_acceptance"]["ready"] is True


def test_single_control_commands_run_from_backend_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("reload-one-runtime-from-env-dev.py")
    observed: list[Path | None] = []

    def run(*_args, **kwargs):
        observed.append(kwargs.get("cwd"))
        return subprocess.CompletedProcess([], 0, stdout="ok", stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)

    assert module._checked(["runtime-control"]) == "ok"
    assert observed == [module.BACKEND_ROOT]


def test_single_flow_recovers_failed_checkpoint_when_live_target_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("reload-one-runtime-from-env-dev.py")
    binding = {
        **_binding("personal_runtime", 41, "rw-test"),
        "image": TARGET.image,
        "image_digest": TARGET.config_id,
        "status": "ready",
    }
    calls: list[list[str]] = []

    def control(arguments: list[str], *, env_file: Path) -> str:
        assert env_file == (tmp_path / "backend/.env").resolve()
        calls.append(arguments)
        if arguments[0] == "list":
            return json.dumps([binding])
        if arguments[0] == "verify":
            return json.dumps({"ready": True, "live_image_ids": [TARGET.config_id]})
        raise AssertionError(f"unexpected control call: {arguments}")

    monkeypatch.setattr(module, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(module, "RuntimeImageConfigStore", _ConfigStore)
    monkeypatch.setattr(module, "_control", control)
    monkeypatch.setattr(module, "_checked", lambda _command: "")

    checkpoint_path = (
        tmp_path
        / ".runtime/runtime-code-cutover/personal_runtime-rw-test.json"
    )
    checkpoint = module.RuntimeCutoverCheckpointStore(checkpoint_path)
    checkpoint.initialize(flow="single", target=TARGET, bindings=[binding])
    checkpoint.advance(
        stage="failed",
        status="failed",
        error_summary="transient startup overlap",
    )

    result = module._run(
        _single_flow_args(runtime_id="rw-test", binding_type="personal_runtime")
    )

    assert result == 0
    assert [call[0] for call in calls] == ["list", "verify"]
    document = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert document["status"] == "completed"
    assert document["stage"] == "verified"
    assert document["checks"]["technical_acceptance"]["ready"] is True


def test_single_flow_archives_completed_old_target_before_new_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("reload-one-runtime-from-env-dev.py")
    binding = _binding("personal_runtime", 41, "rw-test")
    calls: list[list[str]] = []

    def control(arguments: list[str], *, env_file: Path) -> str:
        assert env_file == (tmp_path / "backend/.env").resolve()
        calls.append(arguments)
        if arguments[0] == "list":
            return json.dumps([binding])
        if arguments[0] == "verify":
            return json.dumps({"ready": True, "live_image_ids": [TARGET.config_id]})
        return "{}"

    monkeypatch.setattr(module, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(module, "RuntimeImageConfigStore", _ConfigStore)
    monkeypatch.setattr(module, "_control", control)
    monkeypatch.setattr(module, "_checked", lambda _command: "")
    checkpoint_path = (
        tmp_path / ".runtime/runtime-code-cutover/personal_runtime-rw-test.json"
    )
    old_target = RuntimeImageConfig(
        image="kind.local/hermes-agent:code-old",
        manifest_digest="sha256:" + "d" * 64,
        config_id="sha256:" + "e" * 64,
    )
    checkpoint = module.RuntimeCutoverCheckpointStore(checkpoint_path)
    checkpoint.initialize(flow="single", target=old_target, bindings=[binding])
    checkpoint.advance(stage="verified", status="completed")

    assert module._run(
        _single_flow_args(runtime_id="rw-test", binding_type="personal_runtime")
    ) == 0

    assert [call[0] for call in calls] == [
        "list",
        "isolate",
        "cancel-active",
        "stop",
        "reload",
        "verify-sessions",
        "verify",
    ]
    assert len(list(checkpoint_path.parent.glob(checkpoint_path.name + ".completed-*"))) == 1
    assert json.loads(checkpoint_path.read_text())["target"]["image"] == TARGET.image


def test_single_flow_archives_failed_old_target_after_live_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("reload-one-runtime-from-env-dev.py")
    old_target = RuntimeImageConfig(
        image="kind.local/hermes-agent:code-old",
        manifest_digest="sha256:" + "d" * 64,
        config_id="sha256:" + "e" * 64,
    )
    binding = {
        **_binding("application_runtime", 41, "app-test"),
        "image": old_target.image,
        "image_digest": old_target.config_id,
    }
    calls: list[list[str]] = []

    def control(arguments: list[str], *, env_file: Path) -> str:
        assert env_file == (tmp_path / "backend/.env").resolve()
        calls.append(arguments)
        if arguments[0] == "list":
            return json.dumps([binding])
        if arguments[0] == "verify":
            return json.dumps({"ready": True, "live_image_ids": [TARGET.config_id]})
        return "{}"

    monkeypatch.setattr(module, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(module, "RuntimeImageConfigStore", _ConfigStore)
    monkeypatch.setattr(module, "_control", control)
    monkeypatch.setattr(module, "_checked", lambda _command: "")
    checkpoint_path = (
        tmp_path / ".runtime/runtime-code-cutover/application_runtime-app-test.json"
    )
    checkpoint = module.RuntimeCutoverCheckpointStore(checkpoint_path)
    checkpoint.initialize(flow="single", target=old_target, bindings=[binding])
    checkpoint.advance(
        stage="failed",
        status="failed",
        error_summary="old flow failed before later recovery",
    )

    assert module._run(
        _single_flow_args(runtime_id="app-test", binding_type="application_runtime")
    ) == 0

    assert [call[0] for call in calls] == [
        "list",
        "isolate",
        "cancel-active",
        "stop",
        "reload",
        "verify-sessions",
        "verify",
    ]
    assert len(
        list(checkpoint_path.parent.glob(checkpoint_path.name + ".failed-recovered-*"))
    ) == 1
    assert json.loads(checkpoint_path.read_text())["target"]["image"] == TARGET.image


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("reload-one-runtime-from-env-dev.py", "restart-public-prod.sh"),
        ("reload-all-runtimes-from-env-dev.py", "restart-public-prod.sh"),
    ],
)
def test_auto_backend_restart_uses_registered_formal_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    expected: str,
) -> None:
    module = _load_script(script)

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout="", stderr=""),
    )

    actual = module._backend_restart_command("auto")
    expected_command = [str(module.REPOSITORY_ROOT / "scripts" / expected)]
    if script == "reload-all-runtimes-from-env-dev.py" and expected == "restart-public-backend-dev.sh":
        expected_command.append("--from-prod")
    assert actual == expected_command


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("reload-one-runtime-from-env-dev.py", "restart-public-backend-dev.sh"),
        ("reload-all-runtimes-from-env-dev.py", "restart-public-backend-dev.sh"),
    ],
)
def test_auto_backend_restart_falls_back_to_dev_only_when_formal_not_registered(
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    expected: str,
) -> None:
    module = _load_script(script)

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, stdout="", stderr=""),
    )

    actual = module._backend_restart_command("auto")
    expected_command = [str(module.REPOSITORY_ROOT / "scripts" / expected)]
    if script == "reload-all-runtimes-from-env-dev.py" and expected == "restart-public-backend-dev.sh":
        expected_command.append("--from-prod")
    assert actual == expected_command


def test_fleet_failure_stays_frozen_and_same_target_resume_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("reload-all-runtimes-from-env-dev.py")
    bindings = [
        _binding("personal_runtime", 1, "rw-one"),
        _binding("application_runtime", 2, "app-two"),
    ]
    calls: list[list[str]] = []
    reload_attempts = {"app-two": 0}
    frozen = {"value": False}
    frozen_epoch = {"value": None}

    def runtime_for(arguments: list[str]) -> str | None:
        if "--binding-id" not in arguments:
            return None
        binding_id = int(arguments[arguments.index("--binding-id") + 1])
        return "rw-one" if binding_id == 1 else "app-two"

    def control(arguments: list[str]) -> str:
        calls.append(arguments)
        command = arguments[0]
        if command == "list":
            return json.dumps(bindings)
        if command == "classify":
            return _classify_active(bindings)
        if command == "classify":
            return _classify_active(bindings)
        if command == "freeze":
            frozen["value"] = True
            frozen_epoch["value"] = arguments[arguments.index("--maintenance-epoch") + 1]
        elif command == "maintenance-status":
            return json.dumps(
                {
                    "state": "frozen" if frozen["value"] else "open",
                    "epoch": frozen_epoch["value"],
                }
            )
        elif command == "assert-frozen":
            if not frozen["value"]:
                raise RuntimeError("not frozen")
        elif command == "unfreeze":
            frozen["value"] = False
        elif command == "reload" and runtime_for(arguments) == "app-two":
            reload_attempts["app-two"] += 1
            if reload_attempts["app-two"] == 1:
                raise RuntimeError("simulated reload boundary failure")
        elif command == "verify":
            return json.dumps(
                {"runtime_id": runtime_for(arguments), "live_image_ids": [TARGET.config_id]}
            )
        return "{}"

    checkpoint_path = tmp_path / "fleet.json"
    monkeypatch.setattr(module, "CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(module, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(module, "RuntimeImageConfigStore", _ConfigStore)
    monkeypatch.setattr(module, "_control", control)
    monkeypatch.setattr(module, "_checked", lambda _command: "")
    args = argparse.Namespace(
        concurrency=1,
        activation_concurrency=1,
        backend_lifecycle="prod",
        supersede_failed_target=False,
        repair_session_permissions=False,
        force=False,
        reexecute_completed=False,
    )

    with pytest.raises(
        module.RuntimeFleetCommandError,
        match="application_runtime Binding 2",
    ):
        module._run(args)

    failed = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert frozen["value"] is True
    assert not any(call[0] == "unfreeze" for call in calls)
    assert failed["checks"]["reload_completed"] == ["personal_runtime:1:rw-one"]
    assert failed["checks"]["failed_binding"] == "application_runtime:2:app-two"
    assert failed["checks"]["failed_command"] == "reload"
    assert failed["checks"]["failed_error_type"] == "RuntimeError"

    calls.clear()
    assert module._run(args) == 0

    completed = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert completed["status"] == "completed"
    assert frozen["value"] is False
    assert [call[0] for call in calls].count("reload") == 1
    assert any(call[0] == "assert-frozen" for call in calls)
    assert calls[-1][0] == "unfreeze"

    calls.clear()
    args.reexecute_completed = True
    assert module._run(args) == 0
    assert [call[0] for call in calls].count("stop") == 2
    assert [call[0] for call in calls].count("reload") == 2


def test_fleet_archives_failed_checkpoint_when_gate_is_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("reload-all-runtimes-from-env-dev.py")
    bindings = [_binding("personal_runtime", 1, "rw-one")]
    calls: list[list[str]] = []
    frozen = {"value": False}
    epochs: list[str] = []

    def control(arguments: list[str]) -> str:
        calls.append(arguments)
        command = arguments[0]
        if command == "list":
            return json.dumps(bindings)
        if command == "maintenance-status":
            return json.dumps({"state": "open", "epoch": "old-epoch"})
        if command == "freeze":
            frozen["value"] = True
            epochs.append(arguments[arguments.index("--maintenance-epoch") + 1])
        elif command == "unfreeze":
            frozen["value"] = False
        elif command == "verify":
            return json.dumps({"ready": True, "live_image_ids": [TARGET.config_id]})
        return "{}"

    checkpoint_path = tmp_path / "fleet.json"
    monkeypatch.setattr(module, "CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(module, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(module, "RuntimeImageConfigStore", _ConfigStore)
    monkeypatch.setattr(module, "_control", control)
    monkeypatch.setattr(module, "_checked", lambda _command: "")
    store = module.RuntimeCutoverCheckpointStore(checkpoint_path)
    store.initialize(flow="fleet", target=TARGET, bindings=bindings)
    store.advance(
        stage="failed",
        status="failed",
        checks={"maintenance_epoch": "old-epoch"},
        error_summary="stale failure",
    )
    args = argparse.Namespace(
        concurrency=1,
        activation_concurrency=1,
        backend_lifecycle="prod",
        supersede_failed_target=False,
        repair_session_permissions=False,
        force=False,
        reexecute_completed=False,
    )

    assert module._run(args) == 0

    document = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert document["status"] == "completed"
    assert document["checks"]["maintenance_epoch"] != "old-epoch"
    assert epochs and epochs[0] != "old-epoch"
    assert not frozen["value"]
    assert len(list(tmp_path.glob("fleet.json.failed-stale-*"))) == 1
    assert calls[0][0] == "maintenance-status"


def test_fleet_supersede_archives_old_epoch_when_gate_is_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("reload-all-runtimes-from-env-dev.py")
    bindings = [_binding("application_runtime", 2, "app-two")]
    old_target = RuntimeImageConfig(
        image="kind.local/hermes-agent:code-old",
        manifest_digest="sha256:" + "c" * 64,
        config_id="sha256:" + "d" * 64,
    )
    calls: list[list[str]] = []

    def control(arguments: list[str]) -> str:
        calls.append(arguments)
        if arguments[0] == "list":
            return json.dumps(bindings)
        if arguments[0] == "classify":
            return _classify_active(bindings)
        if arguments[0] == "maintenance-status":
            return json.dumps({"state": "open", "epoch": "newer-epoch"})
        if arguments[0] == "verify":
            return json.dumps({"ready": True, "live_image_ids": [TARGET.config_id]})
        return "{}"

    checkpoint_path = tmp_path / "fleet.json"
    monkeypatch.setattr(module, "CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(module, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(module, "RuntimeImageConfigStore", _ConfigStore)
    monkeypatch.setattr(module, "_control", control)
    monkeypatch.setattr(module, "_checked", lambda _command: "")
    store = module.RuntimeCutoverCheckpointStore(checkpoint_path)
    store.initialize(flow="fleet", target=old_target, bindings=bindings)
    store.advance(
        stage="failed",
        status="failed",
        checks={"maintenance_epoch": "old-epoch"},
        error_summary="stale old target",
    )
    args = argparse.Namespace(
        concurrency=1,
        activation_concurrency=1,
        backend_lifecycle="prod",
        supersede_failed_target=True,
        repair_session_permissions=False,
        force=False,
        reexecute_completed=False,
    )

    assert module._run(args) == 0

    document = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert document["status"] == "completed"
    assert document["target"]["config_id"] == TARGET.config_id
    assert len(list(tmp_path.glob("fleet.json.failed-*"))) == 1
    assert not any(call[0] == "assert-frozen" for call in calls)


def test_fleet_permission_repair_runs_for_personal_and_application_before_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("reload-all-runtimes-from-env-dev.py")
    bindings = [
        _binding("personal_runtime", 1, "rw-one"),
        _binding("application_runtime", 2, "app-two"),
    ]
    calls: list[list[str]] = []

    def control(arguments: list[str]) -> str:
        calls.append(arguments)
        if arguments[0] == "list":
            return json.dumps(bindings)
        if arguments[0] == "classify":
            return _classify_active(bindings)
        if arguments[0] == "repair-session-permissions":
            binding_id = int(arguments[arguments.index("--binding-id") + 1])
            return json.dumps(
                {
                    "runtime_id": "rw-one" if binding_id == 1 else "app-two",
                    "dry_run": {"ok": True},
                    "applied": {"ok": True},
                }
            )
        if arguments[0] == "verify":
            return json.dumps({"ready": True, "live_image_ids": [TARGET.config_id]})
        return "{}"

    checkpoint_path = tmp_path / "fleet.json"
    monkeypatch.setattr(module, "CHECKPOINT_PATH", checkpoint_path)
    monkeypatch.setattr(module, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(module, "RuntimeImageConfigStore", _ConfigStore)
    monkeypatch.setattr(module, "_control", control)
    monkeypatch.setattr(module, "_checked", lambda _command: "")
    args = argparse.Namespace(
        concurrency=1,
        activation_concurrency=1,
        backend_lifecycle="prod",
        supersede_failed_target=False,
        repair_session_permissions=True,
        force=False,
    )

    assert module._run(args) == 0

    commands = [call[0] for call in calls]
    assert commands.count("stop") == 2
    assert commands.count("repair-session-permissions") == 2
    assert max(index for index, value in enumerate(commands) if value == "stop") < min(
        index
        for index, value in enumerate(commands)
        if value == "repair-session-permissions"
    )
    assert max(
        index
        for index, value in enumerate(commands)
        if value == "repair-session-permissions"
    ) < min(index for index, value in enumerate(commands) if value == "reload")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert len(checkpoint["checks"]["session_permission_repairs"]) == 2


async def test_fleet_audit_rejects_unknown_runtime_kservice() -> None:
    module = _load_backend_script("runtime_code_reload.py")
    binding = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))

    class Service:
        async def list_bindings(self):
            return [binding]

    class Kubernetes:
        async def list_runtime_kservices(self, *, namespace: str):
            assert namespace == "hermes-runtimes"
            return [
                {
                    "metadata": {
                        "name": "runtime-unknown",
                        "labels": {"hermes.platform/runtime-id": "rw-unknown"},
                    }
                }
            ]

        async def list_runtime_pods(self, *, namespace: str):
            return []

    with pytest.raises(RuntimeError, match="unknown Runtime KService"):
        await module._audit_fleet(Kubernetes(), Service(), [binding])


async def test_fleet_audit_allows_explicitly_skipped_unavailable_binding() -> None:
    module = _load_backend_script("runtime_code_reload.py")
    expected = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))
    skipped = RuntimeReloadBinding(
        **{**_binding("personal_runtime", 2, "rw-two"), "status": "unavailable"}
    )

    class Service:
        async def list_bindings(self):
            return [expected, skipped]

    class Kubernetes:
        async def list_runtime_kservices(self, *, namespace: str):
            return []

        async def list_runtime_pods(self, *, namespace: str):
            return []

    with pytest.raises(RuntimeError, match="closed Runtime Binding list"):
        await module._audit_fleet(Kubernetes(), Service(), [expected])

    evidence = await module._audit_fleet(
        Kubernetes(), Service(), [expected], allow_unavailable=True
    )
    assert evidence["binding_count"] == 1
    assert evidence["unavailable_binding_count"] == 1


async def test_stop_waits_until_terminating_runtime_pod_really_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_script("runtime_code_reload.py")
    binding = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))

    class Kubernetes:
        list_calls = 0

        async def kservice_exists(self, *, namespace: str, name: str) -> bool:
            return True

        async def delete_kservice(
            self,
            *,
            namespace: str,
            name: str,
            wait: bool = True,
        ) -> None:
            return None

        async def list_kservice_pods(self, *, namespace: str, service_name: str):
            self.list_calls += 1
            if self.list_calls == 1:
                return [
                    {
                        "metadata": {
                            "name": "runtime-1-terminating",
                            "deletionTimestamp": "2026-08-27T09:40:21Z",
                        },
                        "status": {"phase": "Running"},
                    }
                ]
            return []

    kubernetes = Kubernetes()
    monkeypatch.setattr(module.asyncio, "sleep", lambda _seconds: _noop())

    await module._stop(kubernetes, binding, timeout=1)

    assert kubernetes.list_calls == 2


async def test_dev_stop_deletes_exact_runtime_pod_after_kservice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_script("runtime_code_reload.py")
    binding = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))

    class Kubernetes:
        calls: list[tuple[str, str]] = []
        list_calls = 0

        async def kservice_exists(self, *, namespace: str, name: str) -> bool:
            return True

        async def delete_kservice(
            self,
            *,
            namespace: str,
            name: str,
            wait: bool = True,
        ) -> None:
            self.calls.append(("kservice", name))

        async def list_kservice_pods(self, *, namespace: str, service_name: str):
            self.list_calls += 1
            if self.list_calls == 1:
                return [
                    {
                        "metadata": {"name": "runtime-1-user-deployment-pod"},
                        "status": {"phase": "Running"},
                    }
                ]
            return []

        async def delete_pod(
            self,
            *,
            namespace: str,
            name: str,
            grace_period_seconds: int,
            force: bool = False,
            wait: bool = True,
        ) -> None:
            assert grace_period_seconds == 1
            assert force is False
            assert wait is True
            self.calls.append(("pod", name))

    kubernetes = Kubernetes()
    monkeypatch.setattr(module.asyncio, "sleep", lambda _seconds: _noop())

    await module._stop(kubernetes, binding, timeout=1, delete_pods=True)

    assert kubernetes.calls == [
        ("kservice", "runtime-1"),
        ("pod", "runtime-1-user-deployment-pod"),
    ]


async def test_fast_stop_terminates_task_before_api_force_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_script("runtime_code_reload.py")
    binding = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))

    class Kubernetes:
        list_calls = 0
        delete_kwargs: dict[str, object] | None = None

        async def kservice_exists(self, *, namespace: str, name: str) -> bool:
            return True

        async def delete_kservice(
            self,
            *,
            namespace: str,
            name: str,
            wait: bool = True,
        ) -> None:
            assert wait is False

        async def list_kservice_pods(self, *, namespace: str, service_name: str):
            self.list_calls += 1
            if self.list_calls == 1:
                return [
                    {
                        "metadata": {"name": "runtime-1-pod", "uid": "uid-1"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [{
                                "name": "user-container",
                                "containerID": "containerd://" + "a" * 64,
                            }],
                        },
                    }
                ]
            return []

        async def delete_pod(self, **kwargs: object) -> None:
            self.delete_kwargs = kwargs

    task_calls: list[tuple[object, ...]] = []

    async def terminate_task(*args: object, **_kwargs: object):
        task_calls.append(args)
        return {"signal_method": "ctr-task", "escalated": False}

    kubernetes = Kubernetes()
    monkeypatch.setattr(module, "_force_terminate_pod_task", terminate_task)
    monkeypatch.setattr(module.asyncio, "sleep", lambda _seconds: _noop())

    await module._stop(
        kubernetes,
        binding,
        timeout=1,
        delete_pods=True,
        fast_test_mode=True,
    )

    assert kubernetes.delete_kwargs is not None
    assert kubernetes.delete_kwargs["grace_period_seconds"] == 0
    assert kubernetes.delete_kwargs["wait"] is False
    assert kubernetes.delete_kwargs["force"] is True
    assert len(task_calls) == 1


async def test_fast_stop_force_deletes_pod_without_user_container_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_script("runtime_code_reload.py")
    binding = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))

    class Kubernetes:
        list_calls = 0
        delete_kwargs: dict[str, object] | None = None

        async def kservice_exists(self, *, namespace: str, name: str) -> bool:
            return True

        async def delete_kservice(self, *, namespace: str, name: str, wait: bool = True) -> None:
            assert wait is False

        async def list_kservice_pods(self, *, namespace: str, service_name: str):
            self.list_calls += 1
            if self.list_calls == 1:
                return [{
                    "metadata": {"name": "runtime-1-pod", "uid": "uid-1"},
                    "spec": {"nodeName": "test-node"},
                    "status": {"phase": "Running", "containerStatuses": []},
                }]
            return []

        async def delete_pod(self, **kwargs: object) -> None:
            self.delete_kwargs = kwargs

    async def should_not_terminate(*_args: object, **_kwargs: object):
        raise AssertionError("incomplete identity must not inspect host task")

    kubernetes = Kubernetes()
    monkeypatch.setattr(module, "_force_terminate_pod_task", should_not_terminate)
    monkeypatch.setattr(module.asyncio, "sleep", lambda _seconds: _noop())

    receipts = await module._stop(
        kubernetes,
        binding,
        timeout=1,
        delete_pods=True,
        fast_test_mode=True,
    )

    assert kubernetes.delete_kwargs is not None
    assert kubernetes.delete_kwargs["force"] is True
    assert receipts[0]["signal_method"] == "api-force-no-user-container"


async def test_force_termination_uses_ctr_when_crictl_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_script("runtime_code_reload.py")
    binding = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))
    container_id = "a" * 64
    pod_uid = "pod-uid-1"
    pod = {
        "metadata": {"uid": pod_uid},
        "spec": {"nodeName": "test-node"},
        "status": {
            "containerStatuses": [
                {"name": "user-container", "containerID": f"containerd://{container_id}"}
            ]
        },
    }
    commands: list[list[str]] = []

    monkeypatch.setattr(module.shutil, "which", lambda name: None if name == "crictl" else "/usr/bin/ctr")
    monkeypatch.setattr(module, "_local_node_matches", lambda _node: True)
    monkeypatch.setattr(module, "_read_proc_cgroup", lambda _pid: f"{pod_uid} {container_id}")
    monkeypatch.setattr(module, "_find_runtime_lock_fds", lambda _pid: [])

    async def host_command(command: list[str], *, timeout: float) -> str:
        commands.append(command)
        if command[-2:] == ["tasks", "ls"]:
            return f"TASK PID STATUS\n{container_id} 1234 RUNNING\n"
        if command[-3:] == ["tasks", "ls", "-q"]:
            return container_id + "\n"
        if command[3:7] == ["task", "kill", "--signal", "TERM"]:
            return ""
        raise AssertionError(command)

    monkeypatch.setattr(module, "_host_command", host_command)
    monkeypatch.setattr(module, "_host_target_present", lambda *_args: _noop())

    receipt = await module._force_terminate_pod_task(
        pod,
        binding,
        timeout=1,
        identity_revalidated=True,
    )

    assert receipt["inspect_method"] == "ctr"
    assert receipt["pid"] == 1234
    assert receipt["escalated"] is False
    assert commands[0][:4] == ["ctr", "-n", "k8s.io", "tasks"]


async def test_force_termination_accepts_task_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_script("runtime_code_reload.py")
    binding = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))
    container_id = "b" * 64
    pod = {
        "metadata": {"uid": "pod-uid-2"},
        "spec": {"nodeName": "test-node"},
        "status": {
            "containerStatuses": [
                {"name": "user-container", "containerID": f"containerd://{container_id}"}
            ]
        },
    }
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(module, "_local_node_matches", lambda _node: True)

    async def host_command(command: list[str], *, timeout: float) -> str:
        assert command == ["ctr", "-n", "k8s.io", "tasks", "ls"]
        return "TASK PID STATUS\nother 1234 RUNNING\n"

    monkeypatch.setattr(module, "_host_command", host_command)

    receipt = await module._force_terminate_pod_task(
        pod,
        binding,
        timeout=1,
        identity_revalidated=True,
    )

    assert receipt["signal_method"] == "ctr-task-absent"
    assert receipt["pid"] is None


async def test_force_termination_retries_cgroup_race_then_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_script("runtime_code_reload.py")
    binding = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))
    container_id = "c" * 64
    pod_uid = "pod-uid-retry"
    pod = {
        "metadata": {"uid": pod_uid},
        "spec": {"nodeName": "test-node"},
        "status": {
            "containerStatuses": [
                {"name": "user-container", "containerID": f"containerd://{container_id}"}
            ]
        },
    }
    reads = 0
    commands: list[list[str]] = []
    test_pid = os.getpid()

    monkeypatch.setattr(module.shutil, "which", lambda name: None if name == "crictl" else "/usr/bin/ctr")
    monkeypatch.setattr(module, "_local_node_matches", lambda _node: True)
    monkeypatch.setattr(module, "_find_runtime_lock_fds", lambda _pid: [])
    monkeypatch.setattr(module.asyncio, "sleep", lambda _seconds: _noop())

    def read_cgroup(_pid: int) -> str:
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError(f"cannot read cgroup for verified PID {test_pid}")
        return f"{pod_uid} {container_id}"

    monkeypatch.setattr(module, "_read_proc_cgroup", read_cgroup)

    async def host_command(command: list[str], *, timeout: float) -> str:
        commands.append(command)
        if command[-2:] == ["tasks", "ls"]:
            return f"TASK PID STATUS\n{container_id} {test_pid} RUNNING\n"
        if command[-3:] == ["tasks", "ls", "-q"]:
            return container_id + "\n"
        if command[3:7] == ["task", "kill", "--signal", "TERM"]:
            return ""
        raise AssertionError(command)

    monkeypatch.setattr(module, "_host_command", host_command)
    monkeypatch.setattr(module, "_host_target_present", lambda *_args: _noop())

    receipt = await module._force_terminate_pod_task(
        pod,
        binding,
        timeout=1,
        identity_revalidated=True,
    )

    assert reads == 2
    assert receipt["signal_method"] == "ctr-task"
    assert [command[3:7] for command in commands if len(command) >= 7] == [
        ["task", "kill", "--signal", "TERM"]
    ]


async def test_force_termination_accepts_cgroup_race_when_task_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_script("runtime_code_reload.py")
    binding = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))
    container_id = "d" * 64
    pod = {
        "metadata": {"uid": "pod-uid-race"},
        "spec": {"nodeName": "test-node"},
        "status": {
            "containerStatuses": [
                {"name": "user-container", "containerID": f"containerd://{container_id}"}
            ]
        },
    }
    commands: list[list[str]] = []

    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(module, "_local_node_matches", lambda _node: True)
    monkeypatch.setattr(module, "_read_proc_cgroup", lambda _pid: (_ for _ in ()).throw(
        RuntimeError("cannot read cgroup for verified PID")
    ))
    monkeypatch.setattr(module.asyncio, "sleep", lambda _seconds: _noop())

    async def host_command(command: list[str], *, timeout: float) -> str:
        commands.append(command)
        if command[-2:] == ["tasks", "ls"]:
            return f"TASK PID STATUS\n{container_id} {os.getpid()} RUNNING\n"
        if command[-3:] == ["tasks", "ls", "-q"]:
            return "other-container\n"
        raise AssertionError(command)

    monkeypatch.setattr(module, "_host_command", host_command)

    receipt = await module._force_terminate_pod_task(
        pod,
        binding,
        timeout=1,
        identity_revalidated=True,
    )

    assert receipt["signal_method"] == "proc-race-task-gone"
    assert receipt["pid"] is None
    assert not any("kill" in command for command in commands)


async def test_force_termination_fails_closed_when_cgroup_unknown_and_task_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_script("runtime_code_reload.py")
    binding = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))
    container_id = "e" * 64
    pod = {
        "metadata": {"uid": "pod-uid-unknown"},
        "spec": {"nodeName": "test-node"},
        "status": {
            "containerStatuses": [
                {"name": "user-container", "containerID": f"containerd://{container_id}"}
            ]
        },
    }
    commands: list[list[str]] = []

    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(module, "_local_node_matches", lambda _node: True)
    monkeypatch.setattr(module, "_read_proc_cgroup", lambda _pid: (_ for _ in ()).throw(
        RuntimeError("cannot read cgroup for verified PID")
    ))
    monkeypatch.setattr(module.asyncio, "sleep", lambda _seconds: _noop())

    async def host_command(command: list[str], *, timeout: float) -> str:
        commands.append(command)
        if command[-2:] == ["tasks", "ls"]:
            return f"TASK PID STATUS\n{container_id} {os.getpid()} RUNNING\n"
        if command[-3:] == ["tasks", "ls", "-q"]:
            return container_id + "\n"
        raise AssertionError(command)

    monkeypatch.setattr(module, "_host_command", host_command)

    with pytest.raises(RuntimeError, match="cannot read cgroup"):
        await module._force_terminate_pod_task(
            pod,
            binding,
            timeout=1,
            identity_revalidated=True,
        )

    assert not any("kill" in command for command in commands)


async def test_orphan_scan_skips_unreadable_cgroup_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_script("runtime_code_reload.py")

    monkeypatch.setattr(module, "_read_proc_cgroup", lambda _pid: (_ for _ in ()).throw(
        RuntimeError("cannot read cgroup for verified PID")
    ))
    monkeypatch.setattr(module.asyncio, "sleep", lambda _seconds: _noop())

    result, cgroup_text = await module._read_cgroup_or_confirm_task_exit(
        os.getpid(),
        None,
    )

    assert result == "cgroup-unreadable"
    assert cgroup_text is None


async def test_force_termination_does_not_retry_unavailable_final_ctr_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_script("runtime_code_reload.py")
    binding = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))
    container_id = "0" * 64
    pod = {
        "metadata": {"uid": "pod-uid-ctr-timeout"},
        "spec": {"nodeName": "test-node"},
        "status": {
            "containerStatuses": [
                {"name": "user-container", "containerID": f"containerd://{container_id}"}
            ]
        },
    }
    commands: list[list[str]] = []

    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(module, "_local_node_matches", lambda _node: True)
    monkeypatch.setattr(module, "_read_proc_cgroup", lambda _pid: (_ for _ in ()).throw(
        RuntimeError("cannot read cgroup for verified PID")
    ))
    monkeypatch.setattr(module.asyncio, "sleep", lambda _seconds: _noop())

    async def host_command(command: list[str], *, timeout: float) -> str:
        commands.append(command)
        if command[-2:] == ["tasks", "ls"]:
            return f"TASK PID STATUS\n{container_id} {os.getpid()} RUNNING\n"
        if command[-3:] == ["tasks", "ls", "-q"]:
            raise RuntimeError("ctr unavailable")
        raise AssertionError(command)

    monkeypatch.setattr(module, "_host_command", host_command)

    with pytest.raises(RuntimeError, match="cannot read cgroup"):
        await module._force_terminate_pod_task(
            pod,
            binding,
            timeout=1,
            identity_revalidated=True,
        )

    assert len(commands) == 2
    assert not any("kill" in command for command in commands)


async def test_force_termination_accepts_proc_disappearing_before_cgroup_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_backend_script("runtime_code_reload.py")
    binding = RuntimeReloadBinding(**_binding("personal_runtime", 1, "rw-one"))
    container_id = "f" * 64
    pod = {
        "metadata": {"uid": "pod-uid-proc-absent"},
        "spec": {"nodeName": "test-node"},
        "status": {
            "containerStatuses": [
                {"name": "user-container", "containerID": f"containerd://{container_id}"}
            ]
        },
    }

    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(module, "_local_node_matches", lambda _node: True)

    async def host_command(command: list[str], *, timeout: float) -> str:
        assert command == ["ctr", "-n", "k8s.io", "tasks", "ls"]
        return f"TASK PID STATUS\n{container_id} 999999999 RUNNING\n"

    monkeypatch.setattr(module, "_host_command", host_command)

    receipt = await module._force_terminate_pod_task(
        pod,
        binding,
        timeout=1,
        identity_revalidated=True,
    )

    assert receipt["signal_method"] == "proc-absent"
    assert receipt["pid"] is None


def test_ctr_task_pid_requires_exact_container_id() -> None:
    module = _load_backend_script("runtime_code_reload.py")
    container_id = "a" * 64

    assert module._ctr_task_pid(
        f"TASK PID STATUS\n{container_id} 1234 RUNNING\n", container_id
    ) == 1234
    with pytest.raises(RuntimeError, match="not present"):
        module._ctr_task_pid("TASK PID STATUS\nother 1234 RUNNING\n", container_id)


async def _noop() -> None:
    return None


def test_fleet_parallel_stops_submitting_after_first_failure() -> None:
    module = _load_script("reload-all-runtimes-from-env-dev.py")
    bindings = [_binding("personal_runtime", index, f"rw-{index}") for index in range(1, 6)]
    calls: list[str] = []

    def control(arguments: list[str]) -> str:
        runtime_id = arguments[arguments.index("--binding-id") + 1]
        calls.append(runtime_id)
        if runtime_id == "2":
            raise RuntimeError("first failure")
        time.sleep(0.05)
        return "{}"

    module._control = control
    with pytest.raises(module.RuntimeFleetCommandError, match="personal_runtime Binding 2"):
        module._parallel(bindings, "reload", concurrency=2)

    assert "3" not in calls
    assert "4" not in calls
    assert "5" not in calls


def test_fast_mode_requires_explicit_delete_pods() -> None:
    module = _load_backend_script("runtime_code_reload.py")

    with pytest.raises(RuntimeError, match="fast mode requires --delete-pods"):
        module._require_fast_mode(delete_pods=False)
