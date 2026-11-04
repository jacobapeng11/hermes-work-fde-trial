from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = (
    REPOSITORY_ROOT
    / "runtime"
    / "hermes-runtime-overlay"
    / "verify_upstream_environment.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "runtime_upstream_environment_verifier",
        MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _contract(path: Path) -> tuple[Path, dict]:
    payload = {
        "version": 1,
        "enabled": True,
        "upstream_version": "v0.19.1",
        "upstream_commit": "a" * 40,
        "compatibility_sha256": "b" * 64,
        "target": {
            "python_distributions": {"click": "8.4.2"},
            "node": {
                "package_json": (
                    "plugins/platforms/photon/sidecar/"
                    "node_modules/protobufjs/package.json"
                ),
                "package_name": "protobufjs",
                "version": "8.7.1",
            },
            "sqlite": {"version": "3.53.4"},
        },
    }
    payload["contract_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, payload


def test_environment_contract_digest_is_fail_closed(tmp_path: Path) -> None:
    module = _module()
    path, payload = _contract(tmp_path / "contract.json")

    assert module._load_contract(path) == payload
    payload["target"]["sqlite"]["version"] = "0.0.0"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        module.UpstreamEnvironmentError,
        match="digest mismatch",
    ):
        module._load_contract(path)


def test_node_contract_is_confined_to_the_official_photon_dependency_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "HERMES_ROOT", tmp_path)
    package = (
        tmp_path
        / "plugins"
        / "platforms"
        / "photon"
        / "sidecar"
        / "node_modules"
        / "protobufjs"
        / "package.json"
    )
    package.parent.mkdir(parents=True)
    package.write_text(
        json.dumps({"name": "protobufjs", "version": "8.7.1"}),
        encoding="utf-8",
    )
    target = _contract(tmp_path / "contract.json")[1]["target"]

    assert module._verify_node(target) == {
        "package_name": "protobufjs",
        "version": "8.7.1",
    }
    target["node"]["package_json"] = "../../etc/passwd"
    with pytest.raises(module.UpstreamEnvironmentError, match="allowed prefix"):
        module._verify_node(target)


def test_environment_verifier_is_read_only_and_reports_all_contract_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    contract, payload = _contract(tmp_path / "contract.json")
    monkeypatch.setattr(module, "_verify_python", lambda _target: {"click": "8.4.2"})
    monkeypatch.setattr(
        module,
        "_verify_node",
        lambda _target: {"package_name": "protobufjs", "version": "8.7.1"},
    )
    monkeypatch.setattr(
        module,
        "_verify_sqlite",
        lambda _target: {"version": "3.53.4", "fts5_trigram": "passed"},
    )

    report = module.verify(contract_path=contract)

    assert report["status"] == "passed"
    assert report["contract_sha256"] == payload["contract_sha256"]
    assert report["python_distributions"] == {"click": "8.4.2"}
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "subprocess",
        "delete_old_source",
        "apply_adaptation",
        "unlink(",
        "write_text(",
    ):
        assert forbidden not in source
