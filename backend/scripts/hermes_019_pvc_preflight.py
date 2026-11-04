#!/usr/bin/env python3
"""Run the single-writer Hermes 0.19 PVC state migration and seal it."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml


MARKER_RELATIVE_PATH = Path(".platform/hermes-runtime-schema.json")
LEGACY_MARKER_RELATIVE_PATH = Path(
    ".platform/hermes-runtime-schema-22.json"
)


class Hermes019PreflightError(RuntimeError):
    pass


def expected_marker(*, config_schema: int, state_schema: int, image_commit: str) -> dict[str, Any]:
    return {
        "config_schema": config_schema,
        "state_schema": state_schema,
        "image_commit": image_commit,
    }


def marker_matches(
    home: Path,
    *,
    config_schema: int,
    state_schema: int,
    image_commit: str,
    require_image_commit: bool = True,
) -> bool:
    documents: list[dict[str, Any]] = []
    for relative_path in (
        MARKER_RELATIVE_PATH,
        LEGACY_MARKER_RELATIVE_PATH,
    ):
        try:
            document = json.loads(
                (home / relative_path).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(document, dict):
            documents.append(document)
    expected = expected_marker(
        config_schema=config_schema,
        state_schema=state_schema,
        image_commit=image_commit,
    )
    keys = ("config_schema", "state_schema", "image_commit")
    if not require_image_commit:
        keys = ("config_schema", "state_schema")
    return any(
        all(document.get(key) == expected[key] for key in keys)
        for document in documents
    )


def _validate_config(home: Path, expected_schema: int) -> None:
    path = home / "config.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise Hermes019PreflightError(f"cannot read Hermes config {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("_config_version") != expected_schema:
        raise Hermes019PreflightError(f"Hermes config must use schema {expected_schema}")


def _inspect_database(path: Path, expected_schema: int) -> None:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            version = int(row[0]) if row else None
            integrity = [str(item[0]) for item in connection.execute("PRAGMA integrity_check").fetchall()]
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise Hermes019PreflightError(f"cannot validate migrated state.db: {exc}") from exc
    if version != expected_schema:
        raise Hermes019PreflightError(
            f"Hermes state schema is {version!r}, expected {expected_schema}"
        )
    if integrity != ["ok"]:
        raise Hermes019PreflightError(f"Hermes state integrity check failed: {integrity!r}")


def _write_marker(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_preflight(
    home: Path,
    *,
    expected_config_schema: int,
    expected_state_schema: int,
    image_commit: str,
    session_db_factory: Callable[[Path], Any] | None = None,
) -> Path:
    home = home.resolve()
    _validate_config(home, expected_config_schema)
    state_path = home / "state.db"
    marker_path = home / MARKER_RELATIVE_PATH

    if not marker_matches(
        home,
        config_schema=expected_config_schema,
        state_schema=expected_state_schema,
        image_commit=image_commit,
    ):
        if session_db_factory is None:
            from hermes_state import SessionDB

            def session_db_factory(path: Path) -> Any:
                return SessionDB(db_path=path)
        database = session_db_factory(state_path)
        close = getattr(database, "close", None)
        if callable(close):
            close()

    _inspect_database(state_path, expected_state_schema)
    if not marker_matches(
        home,
        config_schema=expected_config_schema,
        state_schema=expected_state_schema,
        image_commit=image_commit,
    ):
        document = expected_marker(
            config_schema=expected_config_schema,
            state_schema=expected_state_schema,
            image_commit=image_commit,
        )
        document["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_marker(marker_path, document)
    return marker_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--expected-config-schema", type=int, required=True)
    parser.add_argument("--expected-state-schema", type=int, required=True)
    parser.add_argument("--image-commit", required=True)
    args = parser.parse_args()
    run_preflight(
        args.hermes_home,
        expected_config_schema=args.expected_config_schema,
        expected_state_schema=args.expected_state_schema,
        image_commit=args.image_commit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
