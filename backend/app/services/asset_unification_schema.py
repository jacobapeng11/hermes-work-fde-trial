"""One-time MySQL schema finalization for the stable asset catalog."""

from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


RETIRED_ASSET_TABLES = (
    "runtime_skill_manage_event",
    "runtime_skill_reconcile_state",
    "profile_asset_operations",
    "profile_asset_draft",
    "profile_config_state",
    "profile_config_generation",
    "runtime_skill_binding",
    "runtime_mcp_binding",
    "runtime_kb_binding",
    "user_runtime_mcp",
    "user_runtime_skill",
    "public_skill_version",
    "public_skill_asset",
    "public_mcp_asset",
    "public_asset",
    "asset_review",
)

MIGRATION_SOURCE_TABLES = frozenset(
    {
        "profile_asset_draft",
        "runtime_skill_binding",
        "runtime_mcp_binding",
        "runtime_kb_binding",
        "user_runtime_mcp",
        "user_runtime_skill",
        "public_skill_version",
        "public_skill_asset",
        "public_mcp_asset",
        "public_asset",
    }
)

_STABLE_TABLES = frozenset(
    {
        "skill",
        "skill_version",
        "mcp",
        "mcp_version",
        "runtime_agent_profile_revision",
        "profile_skill_binding",
        "profile_mcp_binding",
        "profile_knowledge_binding",
        "user_skillhub_installation",
    }
)
_SKILLHUB_STABLE_COLUMNS = frozenset(
    {"skill_id", "installed_skill_version_id", "source_status"}
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


class AssetUnificationSchemaError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class AssetUnificationSchemaState:
    status: str
    retired_tables: tuple[str, ...]
    missing_source_tables: tuple[str, ...]
    missing_stable_tables: tuple[str, ...]
    missing_skillhub_columns: tuple[str, ...]


def _quoted(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise AssetUnificationSchemaError(
            "ASSET_SCHEMA_IDENTIFIER_INVALID", f"unsafe SQL identifier: {value}"
        )
    return f"`{value}`"


def _schema_snapshot(sync_connection) -> tuple[set[str], dict[str, set[str]]]:
    inspector = inspect(sync_connection)
    tables = set(inspector.get_table_names())
    columns = {
        table_name: {str(item["name"]) for item in inspector.get_columns(table_name)}
        for table_name in tables
    }
    return tables, columns


async def inspect_asset_unification_schema(
    engine: AsyncEngine,
) -> AssetUnificationSchemaState:
    async with engine.connect() as connection:
        tables, columns = await connection.run_sync(_schema_snapshot)
    retired = tuple(name for name in RETIRED_ASSET_TABLES if name in tables)
    missing_stable = tuple(sorted(_STABLE_TABLES - tables))
    missing_columns = tuple(
        sorted(
            _SKILLHUB_STABLE_COLUMNS
            - columns.get("user_skillhub_installation", set())
        )
    )
    if not retired:
        status = "finalized"
        missing_sources: tuple[str, ...] = ()
    else:
        missing_sources = tuple(sorted(MIGRATION_SOURCE_TABLES - tables))
        status = "ready" if not missing_sources and not missing_stable and not missing_columns else "blocked"
    return AssetUnificationSchemaState(
        status=status,
        retired_tables=retired,
        missing_source_tables=missing_sources,
        missing_stable_tables=missing_stable,
        missing_skillhub_columns=missing_columns,
    )


def _skillhub_constraints(sync_connection) -> tuple[list[str], list[str], set[str], set[str]]:
    inspector = inspect(sync_connection)
    foreign_keys = [
        str(item["name"])
        for item in inspector.get_foreign_keys("user_skillhub_installation")
        if item.get("name")
        and "user_runtime_skill_id" in set(item.get("constrained_columns") or [])
    ]
    indexes_to_drop = {
        str(item["name"])
        for item in inspector.get_unique_constraints("user_skillhub_installation")
        if item.get("name")
        and "user_runtime_skill_id" in set(item.get("column_names") or [])
    }
    indexes_to_drop.update(
        str(item["name"])
        for item in inspector.get_indexes("user_skillhub_installation")
        if item.get("name")
        and "user_runtime_skill_id" in set(item.get("column_names") or [])
    )
    all_indexes = {
        str(item["name"])
        for item in inspector.get_indexes("user_skillhub_installation")
        if item.get("name")
    }
    all_foreign_keys = {
        str(item["name"])
        for item in inspector.get_foreign_keys("user_skillhub_installation")
        if item.get("name")
    }
    return foreign_keys, sorted(indexes_to_drop), all_indexes, all_foreign_keys


def _external_retired_references(sync_connection) -> list[str]:
    inspector = inspect(sync_connection)
    tables = set(inspector.get_table_names())
    retired = set(RETIRED_ASSET_TABLES)
    references: list[str] = []
    for table_name in sorted(tables - retired):
        for item in inspector.get_foreign_keys(table_name):
            referred = item.get("referred_table")
            if referred in retired:
                references.append(
                    f"{table_name}.{','.join(item.get('constrained_columns') or [])}"
                    f"->{referred}"
                )
    return references


async def _finalize_skillhub(connection: AsyncConnection) -> None:
    invalid_count = int(
        await connection.scalar(
            text(
                "SELECT COUNT(*) FROM user_skillhub_installation "
                "WHERE skill_id IS NULL OR installed_skill_version_id IS NULL "
                "OR source_status IS NULL"
            )
        )
        or 0
    )
    if invalid_count:
        raise AssetUnificationSchemaError(
            "SKILLHUB_STABLE_IDENTITY_INCOMPLETE",
            f"{invalid_count} SkillHub installation rows lack stable identity",
        )

    tables, columns = await connection.run_sync(_schema_snapshot)
    if "user_skillhub_installation" not in tables:
        raise AssetUnificationSchemaError(
            "SKILLHUB_TABLE_MISSING", "user_skillhub_installation is missing"
        )
    if "user_runtime_skill_id" in columns["user_skillhub_installation"]:
        foreign_keys, indexes, _, _ = await connection.run_sync(_skillhub_constraints)
        for name in foreign_keys:
            await connection.execute(
                text(
                    "ALTER TABLE user_skillhub_installation DROP FOREIGN KEY "
                    + _quoted(name)
                )
            )
        for name in indexes:
            await connection.execute(
                text(
                    "ALTER TABLE user_skillhub_installation DROP INDEX "
                    + _quoted(name)
                )
            )
        await connection.execute(
            text(
                "ALTER TABLE user_skillhub_installation "
                "DROP COLUMN user_runtime_skill_id"
            )
        )

    await connection.execute(
        text(
            "ALTER TABLE user_skillhub_installation "
            "MODIFY skill_id BIGINT NOT NULL, "
            "MODIFY installed_skill_version_id BIGINT NOT NULL, "
            "MODIFY source_status VARCHAR(32) NOT NULL"
        )
    )
    _, _, indexes, foreign_keys = await connection.run_sync(_skillhub_constraints)
    statements = []
    if "uq_user_skillhub_installation_skill" not in indexes:
        statements.append(
            "ADD CONSTRAINT uq_user_skillhub_installation_skill UNIQUE (skill_id)"
        )
    if "ix_user_skillhub_installation_installed_skill_version_id" not in indexes:
        statements.append(
            "ADD INDEX ix_user_skillhub_installation_installed_skill_version_id "
            "(installed_skill_version_id)"
        )
    if "ix_user_skillhub_installation_source_status" not in indexes:
        statements.append(
            "ADD INDEX ix_user_skillhub_installation_source_status (source_status)"
        )
    if "fk_user_skillhub_installation_skill" not in foreign_keys:
        statements.append(
            "ADD CONSTRAINT fk_user_skillhub_installation_skill "
            "FOREIGN KEY (skill_id) REFERENCES skill(id) ON DELETE CASCADE"
        )
    if "fk_user_skillhub_installation_version" not in foreign_keys:
        statements.append(
            "ADD CONSTRAINT fk_user_skillhub_installation_version "
            "FOREIGN KEY (installed_skill_version_id) REFERENCES skill_version(id) "
            "ON DELETE RESTRICT"
        )
    if statements:
        await connection.execute(
            text("ALTER TABLE user_skillhub_installation " + ", ".join(statements))
        )


async def finalize_asset_unification_schema(engine: AsyncEngine) -> tuple[str, ...]:
    """Finalize stable constraints, then remove legacy tables with no outside refs."""

    async with engine.begin() as connection:
        await _finalize_skillhub(connection)
        references = await connection.run_sync(_external_retired_references)
        if references:
            raise AssetUnificationSchemaError(
                "RETIRED_ASSET_TABLE_STILL_REFERENCED",
                "; ".join(references),
            )
        tables, columns = await connection.run_sync(_schema_snapshot)
        for column_name in (
            "skill_inventory_initialized_at",
            "skill_inventory_last_attempt_at",
            "skill_inventory_last_error",
        ):
            if column_name in columns.get("hermes_runtime_binding", set()):
                await connection.execute(
                    text(
                        "ALTER TABLE hermes_runtime_binding DROP COLUMN "
                        + _quoted(column_name)
                    )
                )
        dropped = tuple(name for name in RETIRED_ASSET_TABLES if name in tables)
        await connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            for table_name in dropped:
                await connection.execute(text("DROP TABLE " + _quoted(table_name)))
        finally:
            await connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    return dropped


__all__ = [
    "AssetUnificationSchemaError",
    "AssetUnificationSchemaState",
    "MIGRATION_SOURCE_TABLES",
    "RETIRED_ASSET_TABLES",
    "finalize_asset_unification_schema",
    "inspect_asset_unification_schema",
]
