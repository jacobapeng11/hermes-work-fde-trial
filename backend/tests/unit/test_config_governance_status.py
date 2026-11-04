from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "config-governance-status.py"


def test_config_governance_status_summarizes_registry_env_and_offline_render(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=dev\nOPENAI_COMPATIBLE_API_KEY=hidden-value\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(ROOT),
            "--environment",
            "test",
            "--env-file",
            str(env_file),
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"status": "cluster-infra-source-only"' in result.stdout
    payload = json.loads(result.stdout)
    registry = payload["configuration_checks"]["config_registry"]
    assert registry["status"] == "ok"
    assert registry["issues"] == 0
    assert registry["settings_code_fields"] == registry["settings_registry_fields"]
    assert '"backend_env_read": true' in result.stdout
    assert '"kubernetes_queried": false' in result.stdout
    assert '"drift_status": "live-state-not-compared"' in result.stdout
    assert "hidden-value" not in result.stdout


def test_config_governance_status_reports_missing_env_without_failing(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(ROOT),
            "--environment",
            "test",
            "--env-file",
            str(tmp_path / "missing.env"),
            "--format",
            "text",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "config_registry: status=ok" in result.stdout
    assert "active_env:" in result.stdout
    assert "status=not_available" in result.stdout
    assert "reason=env-file-missing" in result.stdout
