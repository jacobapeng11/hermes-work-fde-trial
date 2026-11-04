from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.hermes_019_pvc_preflight import (
    LEGACY_MARKER_RELATIVE_PATH,
    MARKER_RELATIVE_PATH,
    Hermes019PreflightError,
    run_preflight,
)

LEGACY_COMMIT = "f4df260f26c93f15694698869f3ea8e965eea301"


def _write_config(home: Path, version: int = 33) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(f"_config_version: {version}\n", encoding="utf-8")


def _migrating_factory(expected_version: int, calls: list[Path]):
    class Database:
        def __init__(self, path: Path) -> None:
            calls.append(path)
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            connection.execute("INSERT INTO schema_version VALUES (?)", (expected_version,))
            connection.commit()
            connection.close()

        def close(self) -> None:
            return None

    return Database


def test_missing_config_fails_without_creating_marker(tmp_path: Path) -> None:
    with pytest.raises(Hermes019PreflightError, match="cannot read Hermes config"):
        run_preflight(
            tmp_path,
            expected_config_schema=33,
            expected_state_schema=22,
            image_commit=LEGACY_COMMIT,
        )
    assert not (tmp_path / MARKER_RELATIVE_PATH).exists()


def test_wrong_config_schema_fails(tmp_path: Path) -> None:
    _write_config(tmp_path, 32)
    with pytest.raises(Hermes019PreflightError, match="schema 33"):
        run_preflight(
            tmp_path,
            expected_config_schema=33,
            expected_state_schema=22,
            image_commit=LEGACY_COMMIT,
        )


def test_successful_migration_writes_versioned_marker_atomically(tmp_path: Path) -> None:
    _write_config(tmp_path)
    calls: list[Path] = []
    marker = run_preflight(
        tmp_path,
        expected_config_schema=33,
        expected_state_schema=22,
        image_commit=LEGACY_COMMIT,
        session_db_factory=_migrating_factory(22, calls),
    )

    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert calls == [tmp_path / "state.db"]
    assert payload["config_schema"] == 33
    assert payload["state_schema"] == 22
    assert payload["image_commit"] == LEGACY_COMMIT
    assert payload["completed_at"]
    assert list(marker.parent.glob("*.tmp")) == []


def test_matching_marker_is_idempotent_and_does_not_remigrate(tmp_path: Path) -> None:
    _write_config(tmp_path)
    calls: list[Path] = []
    factory = _migrating_factory(22, calls)
    marker = run_preflight(
        tmp_path,
        expected_config_schema=33,
        expected_state_schema=22,
        image_commit=LEGACY_COMMIT,
        session_db_factory=factory,
    )
    first = marker.read_text(encoding="utf-8")
    run_preflight(
        tmp_path,
        expected_config_schema=33,
        expected_state_schema=22,
        image_commit=LEGACY_COMMIT,
        session_db_factory=lambda _path: pytest.fail("matching marker must not remigrate"),
    )
    assert marker.read_text(encoding="utf-8") == first
    assert len(calls) == 1


def test_state_schema_upgrade_replaces_legacy_marker(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path)
    legacy = tmp_path / LEGACY_MARKER_RELATIVE_PATH
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            {
                "config_schema": 33,
                "state_schema": 22,
                "image_commit": LEGACY_COMMIT,
            }
        ),
        encoding="utf-8",
    )
    connection = sqlite3.connect(tmp_path / "state.db")
    connection.execute(
        "CREATE TABLE schema_version (version INTEGER NOT NULL)"
    )
    connection.execute("INSERT INTO schema_version VALUES (22)")
    connection.commit()
    connection.close()
    calls: list[Path] = []

    def migrate(path: Path):
        calls.append(path)
        database = sqlite3.connect(path)
        database.execute("UPDATE schema_version SET version = 23")
        database.commit()
        return database

    marker = run_preflight(
        tmp_path,
        expected_config_schema=33,
        expected_state_schema=23,
        image_commit="c" * 40,
        session_db_factory=migrate,
    )

    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert calls == [tmp_path / "state.db"]
    assert payload["state_schema"] == 23
    assert payload["image_commit"] == "c" * 40


def test_integrity_failure_writes_no_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path)
    calls: list[Path] = []

    class BadConnection:
        def execute(self, statement: str):
            class Cursor:
                def fetchone(self):
                    return (22,)

                def fetchall(self):
                    return [("corrupt",)]

            return Cursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: BadConnection())
    with pytest.raises(Hermes019PreflightError, match="integrity check failed"):
        run_preflight(
            tmp_path,
            expected_config_schema=33,
            expected_state_schema=22,
            image_commit=LEGACY_COMMIT,
            session_db_factory=lambda path: calls.append(path),
        )
    assert calls == [tmp_path / "state.db"]
    assert not (tmp_path / MARKER_RELATIVE_PATH).exists()
