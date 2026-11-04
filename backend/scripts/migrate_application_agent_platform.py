from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hashlib
import json

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import engine
from app.models import (
    ApplicationAgent,
    ApplicationAgentConfigRevision,
    ApplicationAgentSkill,
    Base,
    Skill,
    SkillVersion,
)
from app.services.application_agent_asset_snapshot import (
    ApplicationAgentAssetSnapshotBuilder,
)


APPLICATION_AGENT_TABLES = (
    "application_agent",
    "application_client",
    "application_agent_client_grant",
    "application_agent_authorization_code",
    "application_agent_config_revision",
    "application_agent_skill",
    "skill",
    "application_agent_runtime_binding",
    "application_agent_conversation",
    "application_agent_run",
    "application_agent_memory",
    "application_agent_wecom_channel",
    "application_agent_wecom_oauth_state",
    "application_agent_wecom_binding_ticket",
    "application_agent_wecom_user_binding",
    "application_agent_wecom_callback_event",
    "im_channel",
    "im_channel_lease",
    "im_onboarding_session",
    "im_inbound_message",
    "im_message_attachment",
    "im_delivery_checkpoint",
    "organization_wecom_identity",
    "application_agent_wecom_user_access",
    "wecom_identity_binding_ticket",
    "knowledge_base",
)


def _schema_state(connection: Connection) -> dict[str, set[str]]:
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    return {
        table_name: {
            str(column["name"])
            for column in inspector.get_columns(table_name)
        }
        for table_name in APPLICATION_AGENT_TABLES
        if table_name in existing
    }


def schema_errors(actual: dict[str, set[str]]) -> list[str]:
    errors: list[str] = []
    for table_name in APPLICATION_AGENT_TABLES:
        table = Base.metadata.tables[table_name]
        if table_name not in actual:
            errors.append(f"missing table: {table_name}")
            continue
        missing_columns = sorted(
            {column.name for column in table.columns} - actual[table_name]
        )
        if missing_columns:
            errors.append(
                f"{table_name} missing columns: {', '.join(missing_columns)}"
            )
    return errors


def _create_missing(connection: Connection) -> None:
    selected = set(APPLICATION_AGENT_TABLES)
    for table in Base.metadata.sorted_tables:
        if table.name in selected:
            table.create(connection, checkfirst=True)


def _upgrade_existing(connection: Connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for table_name in ("knowledge_base", "skill"):
        if table_name not in tables:
            continue
        columns = {
            str(column["name"])
            for column in inspect(connection).get_columns(table_name)
        }
        if "application_agent_id" not in columns:
            connection.execute(text(
                f"ALTER TABLE {table_name} "
                "ADD COLUMN application_agent_id BIGINT NULL"
            ))
        index_name = f"ix_{table_name}_application_agent_id"
        indexes = {
            str(index["name"])
            for index in inspect(connection).get_indexes(table_name)
        }
        if index_name not in indexes:
            connection.execute(text(
                f"CREATE INDEX {index_name} "
                f"ON {table_name} (application_agent_id)"
            ))
        if connection.dialect.name == "mysql":
            foreign_keys = {
                str(item.get("name") or "")
                for item in inspect(connection).get_foreign_keys(table_name)
            }
            constraint = f"fk_{table_name}_application_agent_id"
            if constraint not in foreign_keys:
                connection.execute(text(
                    f"ALTER TABLE {table_name} ADD CONSTRAINT "
                    f"{constraint} FOREIGN KEY (application_agent_id) "
                    "REFERENCES application_agent (id) ON DELETE CASCADE"
                ))


async def check_schema() -> None:
    async with engine.connect() as connection:
        actual = await connection.run_sync(_schema_state)
    errors = schema_errors(actual)
    if errors:
        raise RuntimeError("; ".join(errors))


async def apply_schema() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_create_missing)
        await connection.run_sync(_upgrade_existing)
    await check_schema()
    await migrate_legacy_application_skills()


def _config_hash(config: dict) -> str:
    payload = json.dumps(
        config,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def migrate_legacy_application_skills() -> int:
    """Copy legacy one-row Skills into stable Skill/SkillVersion identities."""

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as db:
        legacy_rows = list(
            (
                await db.execute(
                    select(ApplicationAgentSkill).order_by(
                        ApplicationAgentSkill.application_agent_id.asc(),
                        ApplicationAgentSkill.id.asc(),
                    )
                )
            ).scalars()
        )
        replacements: dict[tuple[int, int], int] = {}
        for legacy in legacy_rows:
            catalog_slug = (
                f"app-{int(legacy.application_agent_id)}-{str(legacy.slug)}"
            )[:128].rstrip("-")
            skill = await db.scalar(
                select(Skill).where(
                    Skill.org_id == legacy.org_id,
                    Skill.application_agent_id == legacy.application_agent_id,
                    Skill.asset_scope == "application",
                    Skill.slug == catalog_slug,
                    Skill.status != "deleted",
                )
            )
            if skill is None:
                skill = Skill(
                    org_id=int(legacy.org_id),
                    owner_user_id=int(legacy.created_by_user_id),
                    application_agent_id=int(legacy.application_agent_id),
                    slug=catalog_slug,
                    name=str(legacy.name),
                    description=legacy.description,
                    category_key=str(legacy.category_key or "personal"),
                    asset_scope="application",
                    visibility="private",
                    status="private",
                )
                db.add(skill)
                await db.flush()
            version = await db.scalar(
                select(SkillVersion).where(
                    SkillVersion.skill_id == skill.id,
                    SkillVersion.package_sha256 == legacy.package_sha256,
                    SkillVersion.status != "deleted",
                )
            )
            if version is None:
                next_version = int(
                    await db.scalar(
                        select(SkillVersion.version)
                        .where(SkillVersion.skill_id == skill.id)
                        .order_by(SkillVersion.version.desc())
                        .limit(1)
                    )
                    or 0
                ) + 1
                version = SkillVersion(
                    org_id=int(legacy.org_id),
                    skill_id=int(skill.id),
                    version=next_version,
                    package_uri=str(legacy.package_uri),
                    package_sha256=str(legacy.package_sha256),
                    manifest=dict(legacy.manifest or {}),
                    status="private",
                    published_at=legacy.created_at or datetime.utcnow(),
                )
                db.add(version)
                await db.flush()
            skill.current_version_id = int(version.id)
            replacements[
                (int(legacy.application_agent_id), int(legacy.id))
            ] = int(skill.id)

        changed = 0
        agent_ids = sorted({agent_id for agent_id, _ in replacements})
        for agent_id in agent_ids:
            agent = await db.get(ApplicationAgent, agent_id)
            if agent is None:
                continue
            config = dict(agent.config or {})
            current_ids = [int(item) for item in config.get("application_skill_ids") or []]
            replacement_ids = [
                replacements.get((agent_id, item), item)
                for item in current_ids
            ]
            replacement_ids = list(dict.fromkeys(replacement_ids))
            if replacement_ids == current_ids:
                continue
            config["application_skill_ids"] = replacement_ids
            config = await ApplicationAgentAssetSnapshotBuilder(db).build(
                org_id=int(agent.org_id),
                application_agent_id=int(agent.id),
                config=config,
            )
            agent.config = config
            agent.config_generation = int(agent.config_generation or 0) + 1
            agent.updated_by_user_id = int(agent.created_by_user_id)
            agent.apply_status = (
                "applying" if str(agent.status) == "published" else "not_applied"
            )
            db.add(
                ApplicationAgentConfigRevision(
                    org_id=int(agent.org_id),
                    application_agent_id=int(agent.id),
                    generation=int(agent.config_generation),
                    config_snapshot=config,
                    config_hash=_config_hash(config),
                    created_by_user_id=int(agent.created_by_user_id),
                    apply_status="not_applied",
                )
            )
            changed += 1
        return changed


async def _run(command: str) -> None:
    try:
        if command == "check":
            await check_schema()
        elif command == "apply":
            await apply_schema()
        else:  # pragma: no cover - argparse owns the choices
            raise RuntimeError(f"unknown command: {command}")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Check or create only the Application Agent domain tables. "
            "Existing tables are never dropped or rewritten."
        )
    )
    parser.add_argument("command", choices=("check", "apply"))
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.command))
    except RuntimeError as exc:
        parser.exit(2, f"application Agent schema migration failed: {exc}\n")


if __name__ == "__main__":
    main()
