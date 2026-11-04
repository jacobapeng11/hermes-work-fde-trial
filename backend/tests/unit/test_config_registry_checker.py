from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check-config-registry.py"
REGISTRY = ROOT / "docs" / "architecture" / "current" / "configuration" / "registry.yaml"


def test_config_registry_checker_accepts_current_registry() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(ROOT), "--format", "text"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "status: ok" in result.stdout
    assert "issues: 0" in result.stdout
    assert "backend/.env" not in result.stdout


def test_config_registry_checker_rejects_missing_settings_key(tmp_path: Path) -> None:
    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    first_domain = registry["settings_domains"][0]
    removed_key = first_domain["keys"].pop()
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(registry, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(ROOT),
            "--registry",
            str(registry_path),
            "--format",
            "text",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "status: failed" in result.stdout
    assert "settings-registry-key-missing" in result.stdout
    assert removed_key in result.stdout


def test_config_registry_checker_rejects_wrong_settings_module(tmp_path: Path) -> None:
    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    registry["settings_domains"][0]["source"]["module"] = (
        "backend/config_governance/runtime.py"
    )
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(registry, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(ROOT),
            "--registry",
            str(registry_path),
            "--format",
            "text",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "settings-domain-module-mismatch" in result.stdout
    assert "APP_ENV" in result.stdout
