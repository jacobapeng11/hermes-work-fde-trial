from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from sqlalchemy import text


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import engine  # noqa: E402


PROVIDER_DOCUMENT_INDEX = "uq_kb_file_provider_document"


def planned_statements(
    *,
    columns: dict[str, bool],
    indexes: set[str],
) -> list[str]:
    statements: list[str] = []
    additions = (
        ("provider_document_id", "VARCHAR(64) NULL"),
        ("provider_status", "VARCHAR(32) NULL"),
        ("provider_error", "TEXT NULL"),
        ("provider_synced_at", "DATETIME NULL"),
    )
    for name, definition in additions:
        if name not in columns:
            statements.append(
                f"ALTER TABLE knowledge_base_file ADD COLUMN {name} {definition}"
            )
    if columns.get("oss_url") is False:
        statements.append(
            "ALTER TABLE knowledge_base_file "
            "MODIFY COLUMN oss_url VARCHAR(1000) NULL"
        )
    if PROVIDER_DOCUMENT_INDEX not in indexes:
        statements.append(
            "ALTER TABLE knowledge_base_file "
            f"ADD UNIQUE INDEX {PROVIDER_DOCUMENT_INDEX} (provider_document_id)"
        )
    return statements


async def run(*, apply: bool) -> int:
    async with engine.begin() as connection:
        database = await connection.scalar(text("SELECT DATABASE()"))
        column_rows = (
            await connection.execute(
                text(
                    "SELECT COLUMN_NAME, IS_NULLABLE "
                    "FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = 'knowledge_base_file'"
                )
            )
        ).all()
        columns = {str(name): nullable == "YES" for name, nullable in column_rows}
        if not columns:
            raise RuntimeError("knowledge_base_file table is missing")
        index_rows = (
            await connection.execute(
                text(
                    "SELECT DISTINCT INDEX_NAME "
                    "FROM INFORMATION_SCHEMA.STATISTICS "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = 'knowledge_base_file'"
                )
            )
        ).scalars().all()
        statements = planned_statements(
            columns=columns,
            indexes={str(value) for value in index_rows},
        )
        mapping_predicate = (
            "AND files.provider_document_id IS NULL"
            if "provider_document_id" in columns
            else ""
        )
        mapping_count = await connection.scalar(
            text(
                "SELECT COUNT(*) "
                "FROM knowledge_base_file files "
                "JOIN knowledge_base bases ON bases.id = files.kb_id "
                "WHERE bases.provider = 'weknora' "
                "AND files.is_deleted = 0 "
                f"{mapping_predicate}"
            )
        )
        print(
            json.dumps(
                {
                    "mode": "apply" if apply else "dry-run",
                    "database": database,
                    "table": "knowledge_base_file",
                    "pending_statement_count": len(statements),
                    "statements": statements,
                    "weknora_files_requiring_mapping_review": mapping_count,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        if apply:
            for statement in statements:
                await connection.execute(text(statement))
            print(
                json.dumps(
                    {"mode": "applied", "statement_count": len(statements)},
                    sort_keys=True,
                )
            )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate Hermes personal knowledge files for direct WeKnora access"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-migrate", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.confirm_migrate:
        raise SystemExit("apply requires --confirm-migrate")

    async def execute() -> int:
        try:
            return await run(apply=args.apply)
        finally:
            await engine.dispose()

    raise SystemExit(asyncio.run(execute()))


if __name__ == "__main__":
    main()
