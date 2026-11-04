"""Align Skill Schema, public storage, and installed-asset rollout targets."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from sqlalchemy import text

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
# Settings resolves its declared `.env` relative to the Backend working root.
os.chdir(BACKEND_ROOT)

from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.services.skill_storage_migration import (  # noqa: E402
    SkillStorageMigrationError,
    SkillStorageMigrationService,
)


async def _schema_state() -> tuple[bool, bool]:
    async with engine.connect() as connection:
        column = await connection.scalar(
            text("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'skill' AND COLUMN_NAME = 'market_visible'")
        )
        index = await connection.scalar(
            text("SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'skill' AND INDEX_NAME = 'ix_skill_market_visible'")
        )
    return bool(column), bool(index)


async def _inventory() -> dict[str, object]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT asset_scope, status, COUNT(*) AS total "
                    "FROM skill GROUP BY asset_scope, status "
                    "ORDER BY asset_scope, status"
                )
            )
        ).mappings().all()
        missing_current = await connection.scalar(
            text(
                "SELECT COUNT(*) FROM skill "
                "WHERE asset_scope = 'public' AND status = 'published' "
                "AND current_version_id IS NULL"
            )
        )
    storage_root = Path(settings.HERMES_PUBLIC_ASSET_ROOT) / "public-skills"
    orphan_directories: list[str] = []
    if storage_root.is_dir():
        for org_dir in sorted(item for item in storage_root.iterdir() if item.is_dir()):
            for skill_dir in sorted(item for item in org_dir.iterdir() if item.is_dir()):
                if not (skill_dir / "current").is_dir():
                    orphan_directories.append(str(skill_dir))
    return {
        "status_counts": [dict(row) for row in rows],
        "published_without_current_version": int(missing_current or 0),
        "directories_without_current": orphan_directories,
    }


async def _align_schema() -> None:
    has_column, has_index = await _schema_state()
    if not has_column:
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE skill ADD COLUMN market_visible BOOLEAN NOT NULL DEFAULT TRUE"))
    if not has_index:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE INDEX ix_skill_market_visible ON skill (market_visible)"))


def _approved_ids(value: str) -> set[int]:
    if not value.strip():
        return set()
    values = {int(item.strip()) for item in value.split(",") if item.strip()}
    if any(item <= 0 for item in values):
        raise ValueError("approved revoked Skill IDs must be positive")
    return values


async def run(args: argparse.Namespace) -> int:
    try:
        approved = _approved_ids(args.approve_revoked)
    except ValueError as exc:
        print(json.dumps({"mode": "blocked", "detail": str(exc)}), file=sys.stderr)
        return 2
    has_column, has_index = await _schema_state()
    if not args.apply:
        report = await _inventory()
        print(json.dumps({"mode": "dry-run", "schema": {"market_visible": has_column, "index": has_index}, **report, "next": "run with --apply after reviewing the report"}, ensure_ascii=False))
        return 0
    if args.created_by_user_id is None:
        print(json.dumps({"mode": "blocked", "detail": "--apply requires --created-by-user-id"}), file=sys.stderr)
        return 2
    await _align_schema()
    batch_id = args.batch_id or f"skill-storage-{uuid4().hex}"
    async with AsyncSessionLocal() as db:
        service = SkillStorageMigrationService(
            db,
            storage_root=settings.HERMES_PUBLIC_ASSET_ROOT,
            application_root=settings.HERMES_APPLICATION_AGENT_RUNTIME_ROOT,
        )
        try:
            result = await service.apply(
                batch_id=batch_id,
                created_by_user_id=args.created_by_user_id,
                approved_revoked_ids=approved,
            )
            await db.commit()
            service.finalize_files(result)
            print(json.dumps({"mode": "apply", **asdict(result)}, ensure_ascii=False, default=str))
            return 0
        except SkillStorageMigrationError as exc:
            await db.rollback()
            print(json.dumps({"mode": "blocked", "code": exc.code, "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
        except Exception as exc:
            await db.rollback()
            print(json.dumps({"mode": "blocked", "error_type": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Skill Schema and public storage")
    parser.add_argument("--apply", action="store_true", help="commit migration and clean verified files")
    parser.add_argument("--created-by-user-id", type=int)
    parser.add_argument("--approve-revoked", default="", help="comma-separated historical revoked Skill IDs approved for reactivation")
    parser.add_argument("--batch-id")
    args = parser.parse_args()

    async def execute() -> int:
        try:
            return await run(args)
        finally:
            await engine.dispose()

    raise SystemExit(asyncio.run(execute()))


if __name__ == "__main__":
    main()
