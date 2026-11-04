"""Run the one-time legacy asset migration in explicit recoverable phases.

``dry-run`` is read-only. ``apply`` commits only stable catalog/Profile data and
writes a redacted receipt. ``finalize`` requires that receipt and is the only
phase allowed to tighten constraints or remove retired tables. No phase calls
Runtime, Dashboard, Kubernetes, or PVC APIs.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.services.asset_unification_schema import (  # noqa: E402
    AssetUnificationSchemaError,
    finalize_asset_unification_schema,
    inspect_asset_unification_schema,
)
from app.services.asset_unification_migration import (  # noqa: E402
    AssetUnificationMigrationError,
    AssetUnificationMigrationService,
)


RECEIPT_VERSION = "asset-unification-receipt/v1"


def _checksum(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "checksum"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {**payload, "checksum": _checksum(payload)}
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        raise AssetUnificationSchemaError(
            "ASSET_MIGRATION_RECEIPT_WRITE_FAILED",
            "migration receipt could not be written",
        ) from exc
    finally:
        if "temporary" in locals():
            temporary.unlink(missing_ok=True)


def _load_receipt(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AssetUnificationSchemaError(
            "ASSET_MIGRATION_RECEIPT_INVALID",
            "migration receipt is missing or invalid JSON",
        ) from exc
    if not isinstance(document, dict):
        raise AssetUnificationSchemaError(
            "ASSET_MIGRATION_RECEIPT_INVALID",
            "migration receipt must be a JSON object",
        )
    if document.get("receipt_version") != RECEIPT_VERSION:
        raise AssetUnificationSchemaError(
            "ASSET_MIGRATION_RECEIPT_VERSION_INVALID",
            "migration receipt version is unsupported",
        )
    if document.get("checksum") != _checksum(document):
        raise AssetUnificationSchemaError(
            "ASSET_MIGRATION_RECEIPT_CHECKSUM_INVALID",
            "migration receipt checksum does not match",
        )
    return document


def _print(payload: dict[str, Any], *, error: bool = False) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=sys.stderr if error else sys.stdout,
    )


async def _run_finalize(*, receipt_path: Path | None) -> int:
    if receipt_path is None:
        _print(
            {
                "mode": "blocked",
                "code": "ASSET_MIGRATION_RECEIPT_REQUIRED",
                "detail": "finalize requires --receipt from a successful apply",
            },
            error=True,
        )
        return 2
    try:
        receipt = _load_receipt(receipt_path)
        if receipt.get("phase") == "finalized":
            _print({"mode": "already_finalized", "receipt_id": receipt["receipt_id"]})
            return 0
        if receipt.get("phase") != "applied":
            raise AssetUnificationSchemaError(
                "ASSET_MIGRATION_RECEIPT_PHASE_INVALID",
                "finalize requires an applied receipt",
            )
        state = await inspect_asset_unification_schema(engine)
        if state.status == "finalized":
            dropped_tables: tuple[str, ...] = ()
        elif state.status == "ready":
            dropped_tables = await finalize_asset_unification_schema(engine)
        else:
            raise AssetUnificationSchemaError(
                "ASSET_SCHEMA_NOT_READY",
                json.dumps(asdict(state), ensure_ascii=False, sort_keys=True),
            )
        finalized = {
            **{key: value for key, value in receipt.items() if key != "checksum"},
            "phase": "finalized",
            "finalized_at": datetime.now(timezone.utc).isoformat(),
            "dropped_tables": list(dropped_tables),
        }
        _write_receipt(receipt_path, finalized)
        _print(
            {
                "mode": "finalize",
                "receipt_id": finalized["receipt_id"],
                "dropped_tables": list(dropped_tables),
            }
        )
        return 0
    except AssetUnificationSchemaError as exc:
        _print(
            {"mode": "blocked", "code": exc.code, "detail": str(exc)},
            error=True,
        )
        return 2


async def run(*, mode: str, receipt_path: Path | None = None) -> int:
    if mode not in {"dry-run", "apply", "finalize"}:
        raise ValueError(f"unsupported migration mode: {mode}")
    if mode == "finalize":
        return await _run_finalize(receipt_path=receipt_path)

    state = await inspect_asset_unification_schema(engine)
    if state.status == "finalized":
        _print({"mode": "already_finalized", **asdict(state)})
        return 0
    if state.status != "ready":
        _print(
            {"mode": "blocked", "code": "ASSET_SCHEMA_NOT_READY", **asdict(state)},
            error=True,
        )
        return 2
    if mode == "apply" and receipt_path is None:
        _print(
            {
                "mode": "blocked",
                "code": "ASSET_MIGRATION_RECEIPT_REQUIRED",
                "detail": "apply requires --receipt for later explicit finalize",
            },
            error=True,
        )
        return 2

    receipt_id = uuid4().hex
    existing_attempts = 0
    if mode == "apply" and receipt_path is not None and receipt_path.exists():
        try:
            existing = _load_receipt(receipt_path)
            if existing.get("phase") != "applied":
                raise AssetUnificationSchemaError(
                    "ASSET_MIGRATION_RECEIPT_PHASE_INVALID",
                    "apply cannot overwrite a finalized or incompatible receipt",
                )
            receipt_id = str(existing["receipt_id"])
            existing_attempts = int(existing.get("apply_attempts") or 1)
        except AssetUnificationSchemaError as exc:
            _print(
                {"mode": "blocked", "code": exc.code, "detail": str(exc)},
                error=True,
            )
            return 2

    async with AsyncSessionLocal() as db:
        service = AssetUnificationMigrationService(
            db,
            materialize_files=mode == "apply",
        )
        committed = False
        try:
            result = await service.migrate()
            if mode == "apply":
                await db.commit()
                committed = True
                receipt = {
                    "receipt_version": RECEIPT_VERSION,
                    "receipt_id": receipt_id,
                    "phase": "applied",
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                    "apply_attempts": existing_attempts + 1,
                    "result": asdict(result),
                    "source_tables": list(state.retired_tables),
                }
                assert receipt_path is not None
                _write_receipt(receipt_path, receipt)
                _print(
                    {
                        "mode": "apply",
                        **asdict(result),
                        "receipt_id": receipt_id,
                        "receipt": str(receipt_path.resolve()),
                        "finalized": False,
                    }
                )
            else:
                await db.rollback()
                service.cleanup_created_artifacts()
                _print({"mode": "dry-run", **asdict(result), "finalized": False})
            return 0
        except (AssetUnificationMigrationError, AssetUnificationSchemaError) as exc:
            await db.rollback()
            if not committed:
                service.cleanup_created_artifacts()
            _print(
                {"mode": "blocked", "code": exc.code, "detail": str(exc)},
                error=True,
            )
            return 2


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate legacy Skill/MCP/Profile identities to the stable catalog"
    )
    parser.add_argument(
        "--mode",
        choices=("dry-run", "apply", "finalize"),
        default="dry-run",
        help="dry-run is read-only; apply commits data; finalize removes retired schema",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        help="redacted receipt path required by apply and finalize",
    )
    args = parser.parse_args()

    async def execute() -> int:
        try:
            return await run(mode=args.mode, receipt_path=args.receipt)
        finally:
            await engine.dispose()

    raise SystemExit(asyncio.run(execute()))


if __name__ == "__main__":
    main()
