from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Set

from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from app.database import engine
from app.models import Base


FORBIDDEN_RUNTIME_COLUMNS = {
    "hermes_runtime_binding": {
        "runtime_" + "generation",
        "latest_created_revision",
        "latest_ready_revision",
        "traffic_revision",
    },
    "application_agent_runtime_binding": {
        "runtime_" + "generation",
    },
    "model_usage_log": {
        "runtime_" + "generation",
    },
}


def expected_schema() -> dict[str, set[str]]:
    return {
        table_name: {column.name for column in table.columns}
        for table_name, table in Base.metadata.tables.items()
    }


def schema_errors(
    expected: Mapping[str, Set[str]],
    actual: Mapping[str, Set[str]],
) -> list[str]:
    errors: list[str] = []
    for table_name, expected_columns in expected.items():
        if table_name not in actual:
            errors.append(f"missing table: {table_name}")
            continue
        missing = sorted(set(expected_columns) - set(actual[table_name]))
        if missing:
            errors.append(
                f"{table_name} missing columns: {', '.join(missing)}"
            )
    return errors


def _actual_schema(connection: Connection) -> dict[str, set[str]]:
    inspector = inspect(connection)
    tables = inspector.get_table_names()
    return {
        table_name: {
            str(column["name"])
            for column in inspector.get_columns(table_name)
        }
        for table_name in tables
    }


async def create_schema() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def verify_schema() -> None:
    async with engine.connect() as connection:
        actual = await connection.run_sync(_actual_schema)

    errors = schema_errors(expected_schema(), actual)
    for table_name, forbidden_columns in FORBIDDEN_RUNTIME_COLUMNS.items():
        present = sorted(forbidden_columns.intersection(actual.get(table_name, set())))
        if present:
            errors.append(
                f"{table_name} contains retired columns: {', '.join(present)}"
            )
    if errors:
        raise RuntimeError("; ".join(errors))


async def _run(command: str) -> None:
    try:
        if command == "create":
            await create_schema()
        elif command == "verify":
            await verify_schema()
        else:  # pragma: no cover - argparse prevents this branch
            raise RuntimeError(f"unknown command: {command}")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap and verify the Hermes schema")
    parser.add_argument("command", choices=("create", "verify"))
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.command))
    except RuntimeError as exc:
        parser.exit(2, f"database schema bootstrap failed: {exc}\n")


if __name__ == "__main__":
    main()
