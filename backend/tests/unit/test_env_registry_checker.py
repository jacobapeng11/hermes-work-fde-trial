from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check-env-registry.py"


def test_env_registry_checker_accepts_current_private_env() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(ROOT), "--format", "text"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "status: ok" in result.stdout
    assert "unknown_keys: 0" in result.stdout
    assert "duplicate_keys: 0" in result.stdout
    assert "values" not in result.stdout.lower()


def test_env_registry_checker_rejects_unregistered_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=dev\nUNREGISTERED_TEST_KEY=hidden\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(ROOT),
            "--env-file",
            str(env_file),
            "--format",
            "text",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "status: failed" in result.stdout
    assert "UNREGISTERED_TEST_KEY" in result.stdout
    assert "hidden" not in result.stdout


def test_env_registry_checker_reports_compatibility_key_drift(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=dev\n", encoding="utf-8")
    variant = ROOT / "backend" / ".env_pub"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(ROOT),
            "--env-file",
            str(env_file),
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"status": "ok"' in result.stdout
    assert variant.name in result.stdout
