import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "preflight-asset-unification.py"
FIXTURE = (
    ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "asset_unification"
    / "consistent-duplicate.json"
)


def test_asset_unification_preflight_is_read_only_and_reports_json() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "analyze_asset_unification_snapshot" in source
    assert "model_dump_json" in source
    assert "--snapshot" in source
    for forbidden in (
        ".commit(",
        ".delete(",
        ".add(",
        "reload.mcp",
        "session.resume",
        "kubectl",
    ):
        assert forbidden not in source


def test_asset_unification_preflight_cli_accepts_root_relative_snapshot() -> None:
    completed = subprocess.run(
        [
            str(ROOT / "backend" / ".venv" / "bin" / "python"),
            str(SCRIPT),
            "--snapshot",
            str(FIXTURE.relative_to(ROOT)),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(completed.stdout)
    assert report["ready"] is True
    assert report["mergeable_duplicate_count"] == 1
