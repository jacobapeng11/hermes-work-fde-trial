from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import text


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import engine  # noqa: E402


CAPABILITY_ADDITIONS = (
    ("provider_capabilities", "JSON NULL"),
    ("provider_capabilities_synced_at", "DATETIME NULL"),
)
CAPABILITY_KEYS = ("vector", "keyword", "wiki", "graph")


def planned_statements(*, columns: set[str]) -> list[str]:
    return [
        f"ALTER TABLE knowledge_base ADD COLUMN {name} {definition}"
        for name, definition in CAPABILITY_ADDITIONS
        if name not in columns
    ]


def _normalize_capabilities(value: object) -> dict[str, bool] | None:
    if not isinstance(value, dict):
        return None
    if any(not isinstance(value.get(key), bool) for key in CAPABILITY_KEYS):
        return None
    return {key: value[key] for key in CAPABILITY_KEYS}


def capability_backfill(
    *,
    knowledge_bases: list[dict[str, Any]],
    datasets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    capabilities_by_dataset = {
        str(dataset.get("id") or ""): _normalize_capabilities(
            dataset.get("capabilities")
        )
        for dataset in datasets
        if isinstance(dataset, dict)
    }
    updates: list[dict[str, Any]] = []
    for knowledge_base in knowledge_bases:
        dataset_id = str(knowledge_base.get("dataset_id") or "")
        capabilities = capabilities_by_dataset.get(dataset_id)
        if not dataset_id or capabilities is None:
            continue
        updates.append(
            {
                "knowledge_base_id": int(knowledge_base["id"]),
                "capabilities": capabilities,
            }
        )
    return updates


async def run(*, apply: bool) -> int:
    async with engine.begin() as connection:
        database = await connection.scalar(text("SELECT DATABASE()"))
        column_rows = (
            await connection.execute(
                text(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME = 'knowledge_base'"
                )
            )
        ).scalars().all()
        columns = {str(value) for value in column_rows}
        if not columns:
            raise RuntimeError("knowledge_base table is missing")
        statements = planned_statements(columns=columns)
        capability_predicate = (
            "AND provider_capabilities IS NULL"
            if "provider_capabilities" in columns
            else ""
        )
        review_count = await connection.scalar(
            text(
                "SELECT COUNT(*) FROM knowledge_base "
                "WHERE provider = 'weknora' "
                f"{capability_predicate}"
            )
        )
        print(
            json.dumps(
                {
                    "mode": "apply" if apply else "dry-run",
                    "database": database,
                    "table": "knowledge_base",
                    "pending_statement_count": len(statements),
                    "statements": statements,
                    "weknora_kbs_requiring_capability_review": review_count,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        if not apply:
            return 0
        for statement in statements:
            await connection.execute(text(statement))

    from app.services.personal_knowledge_provider import (
        WeKnoraPersonalKnowledgeClient,
    )

    datasets = await WeKnoraPersonalKnowledgeClient().list_datasets()
    async with engine.begin() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT id, ragflow_id AS dataset_id FROM knowledge_base "
                    "WHERE provider = 'weknora' AND ragflow_id IS NOT NULL"
                )
            )
        ).mappings().all()
        updates = capability_backfill(
            knowledge_bases=[dict(row) for row in rows],
            datasets=datasets,
        )
        for update in updates:
            await connection.execute(
                text(
                    "UPDATE knowledge_base "
                    "SET provider_capabilities = :capabilities, "
                    "provider_capabilities_synced_at = UTC_TIMESTAMP() "
                    "WHERE id = :knowledge_base_id"
                ),
                {
                    "knowledge_base_id": update["knowledge_base_id"],
                    "capabilities": json.dumps(
                        update["capabilities"],
                        sort_keys=True,
                    ),
                },
            )
    print(
        json.dumps(
            {
                "mode": "applied",
                "statement_count": len(statements),
                "capability_backfill_count": len(updates),
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill Hermes personal knowledge WeKnora capabilities"
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
