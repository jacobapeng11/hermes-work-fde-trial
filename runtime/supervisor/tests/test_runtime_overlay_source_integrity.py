from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = (
    REPOSITORY_ROOT
    / "runtime"
    / "hermes-runtime-overlay"
    / "source_integrity.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "runtime_overlay_source_integrity",
        MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(tmp_path: Path, root: Path) -> Path:
    source = root / "tui_gateway" / "compute_host.py"
    files = {
        "tui_gateway/compute_host.py": {
            "kind": "file",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "mode": source.stat().st_mode & 0o777,
        }
    }
    manifest = {
        "manifest_version": 1,
        "upstream_commit": "a" * 40,
        "files": files,
        "manifest_sha256": hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_source_integrity_accepts_only_known_runtime_additions(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "hermes"
    source = root / "tui_gateway" / "compute_host.py"
    source.parent.mkdir(parents=True)
    source.write_text("official\n", encoding="utf-8")
    manifest = _manifest(tmp_path, root)
    extension = root / "platform-default-skills" / "browser" / "SKILL.md"
    extension.parent.mkdir(parents=True)
    extension.write_text("platform\n", encoding="utf-8")

    report = module.verify(manifest_path=manifest, root=root)

    assert report["status"] == "passed"


def test_source_integrity_rejects_modified_deleted_and_unknown_source(
    tmp_path: Path,
) -> None:
    module = _module()
    root = tmp_path / "hermes"
    source = root / "tui_gateway" / "compute_host.py"
    source.parent.mkdir(parents=True)
    source.write_text("official\n", encoding="utf-8")
    manifest = _manifest(tmp_path, root)

    source.write_text("patched\n", encoding="utf-8")
    unknown = root / "tui_gateway" / "overlay.py"
    unknown.write_text("unexpected\n", encoding="utf-8")
    report = module.verify(manifest_path=manifest, root=root)

    assert report["status"] == "failed"
    assert report["modified"] == ["tui_gateway/compute_host.py"]
    assert report["unexpected"] == ["tui_gateway/overlay.py"]

    source.unlink()
    unknown.unlink()
    report = module.verify(manifest_path=manifest, root=root)
    assert report["status"] == "failed"
    assert report["missing"] == ["tui_gateway/compute_host.py"]
