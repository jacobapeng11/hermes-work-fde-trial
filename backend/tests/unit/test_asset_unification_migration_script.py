from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess

import pytest

from app.services.asset_unification_schema import AssetUnificationSchemaError
from scripts.migrate_asset_unification import (
    RECEIPT_VERSION,
    _load_receipt,
    _write_receipt,
)


def test_redacted_receipt_round_trip_is_private_and_tamper_evident(
    tmp_path: Path,
) -> None:
    path = tmp_path / "asset-unification.json"
    _write_receipt(
        path,
        {
            "receipt_version": RECEIPT_VERSION,
            "receipt_id": "receipt-1",
            "phase": "applied",
            "apply_attempts": 1,
            "result": {"migrated_profiles": 2},
            "source_tables": ["public_skill_asset"],
        },
    )

    receipt = _load_receipt(path)
    assert receipt["phase"] == "applied"
    assert receipt["checksum"].startswith("sha256:")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "password" not in path.read_text(encoding="utf-8").lower()

    tampered = {**receipt, "phase": "finalized"}
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(AssetUnificationSchemaError) as error:
        _load_receipt(path)
    assert error.value.code == "ASSET_MIGRATION_RECEIPT_CHECKSUM_INVALID"


def test_cli_exposes_separate_apply_and_finalize_modes() -> None:
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [
            str(root / "backend" / ".venv" / "bin" / "python"),
            str(root / "backend" / "scripts" / "migrate_asset_unification.py"),
            "--help",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "dry-run" in completed.stdout
    assert "apply" in completed.stdout
    assert "finalize" in completed.stdout
    assert "--receipt" in completed.stdout
    assert "--apply" not in completed.stdout
