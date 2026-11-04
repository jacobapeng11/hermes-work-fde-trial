from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApplicationAgent, ApplicationAgentConfigRevision
from app.services.application_agent_asset_snapshot import (
    ApplicationAgentAssetSnapshotBuilder,
    ApplicationAgentAssetSnapshotError,
)
from app.services.application_agent_runtime import ApplicationAgentRuntimeError
from app.services.application_agents import ApplicationAgentError


logger = logging.getLogger(__name__)


def _config_hash(config: dict) -> str:
    payload = json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _delete_error_code(exc: Exception) -> str:
    """Keep driver classification codes out of the user-facing contract."""
    if isinstance(exc, (ApplicationAgentError, ApplicationAgentRuntimeError)):
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code.strip():
            return code.strip()
    return "application_agent_delete_failed"


class ApplicationAgentAssetRolloutService:
    """Fan out public asset versions to application Desired revisions.

    This adapter intentionally does not create personal Profile targets. It
    only advances application config heads; the application Runtime manager
    consumes the resulting ``applying`` rows independently.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def refresh_public_asset(
        self,
        *,
        org_id: int,
        asset_type: str,
        asset_id: int,
        created_by_user_id: int,
    ) -> int:
        if asset_type not in {"skill", "mcp"}:
            return 0
        agents = list(
            (
                await self.db.execute(
                    select(ApplicationAgent)
                    .where(ApplicationAgent.org_id == org_id)
                    .order_by(ApplicationAgent.id.asc())
                )
            ).scalars()
        )
        changed = 0
        builder = ApplicationAgentAssetSnapshotBuilder(self.db)
        for agent in agents:
            config = dict(agent.config or {})
            key = "skill_asset_ids" if asset_type == "skill" else "mcp_asset_ids"
            ids = {int(value) for value in (config.get(key) or [])}
            if int(asset_id) not in ids:
                continue
            try:
                snapshot = await builder.build(
                    org_id=int(agent.org_id),
                    application_agent_id=int(agent.id),
                    config=config,
                )
            except ApplicationAgentAssetSnapshotError:
                # A revoked/missing asset must not corrupt the current
                # Applied revision. The next explicit admin save reports the
                # actionable validation error.
                continue
            if snapshot == config:
                continue
            locked = await self.db.scalar(
                select(ApplicationAgent)
                .where(ApplicationAgent.id == int(agent.id))
                .with_for_update()
            )
            if locked is None:
                continue
            locked.config = snapshot
            locked.config_generation = int(locked.config_generation or 0) + 1
            locked.updated_by_user_id = int(created_by_user_id)
            locked.apply_status = (
                "applying" if locked.status == "published" else "not_applied"
            )
            revision = ApplicationAgentConfigRevision(
                org_id=int(locked.org_id),
                application_agent_id=int(locked.id),
                generation=int(locked.config_generation),
                config_snapshot=snapshot,
                config_hash=_config_hash(snapshot),
                created_by_user_id=int(locked.created_by_user_id),
                apply_status="not_applied",
            )
            self.db.add(revision)
            changed += 1
        if changed:
            await self.db.flush()
        return changed

    async def remove_public_asset(
        self,
        *,
        org_id: int,
        asset_type: str,
        asset_id: int,
        created_by_user_id: int,
    ) -> int:
        """Remove one terminal public asset without re-resolving unrelated pins."""

        if asset_type not in {"skill", "mcp"}:
            return 0
        agents = list(
            (
                await self.db.execute(
                    select(ApplicationAgent)
                    .where(ApplicationAgent.org_id == org_id)
                    .order_by(ApplicationAgent.id.asc())
                )
            ).scalars()
        )
        id_key = "skill_asset_ids" if asset_type == "skill" else "mcp_asset_ids"
        snapshot_key = "skills" if asset_type == "skill" else "mcps"
        changed = 0
        for agent in agents:
            locked = await self.db.scalar(
                select(ApplicationAgent)
                .where(ApplicationAgent.id == int(agent.id))
                .with_for_update()
            )
            if locked is None:
                continue
            config = dict(locked.config or {})
            ids = [int(value) for value in (config.get(id_key) or [])]
            if int(asset_id) not in ids:
                continue
            config[id_key] = [value for value in ids if value != int(asset_id)]
            snapshot = dict(config.get("asset_snapshot") or {})
            snapshot.setdefault("schema_version", 1)
            snapshot.setdefault("skills", [])
            snapshot.setdefault("application_skills", [])
            snapshot.setdefault("mcps", [])
            snapshot.setdefault("knowledge_base_ids", [])
            snapshot[snapshot_key] = [
                item
                for item in snapshot[snapshot_key]
                if int(item.get("asset_id", 0)) != int(asset_id)
            ]
            config["asset_snapshot"] = snapshot
            locked.config = config
            locked.config_generation = int(locked.config_generation or 0) + 1
            locked.updated_by_user_id = int(created_by_user_id)
            locked.apply_status = (
                "applying" if locked.status == "published" else "not_applied"
            )
            self.db.add(
                ApplicationAgentConfigRevision(
                    org_id=int(locked.org_id),
                    application_agent_id=int(locked.id),
                    generation=int(locked.config_generation),
                    config_snapshot=config,
                    config_hash=_config_hash(config),
                    created_by_user_id=int(locked.created_by_user_id),
                    apply_status="not_applied",
                )
            )
            changed += 1
        if changed:
            await self.db.flush()
        return changed


class ApplicationAgentAssetRolloutWorker:
    """Reconcile application Desired heads without touching personal Profiles."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        batch_size: int = 8,
    ) -> None:
        self.session_factory = session_factory
        self.batch_size = batch_size

    async def reconcile_once(self) -> int:
        from app.services.application_agent_runtime import (
            SqlAlchemyApplicationAgentRuntimeManager,
        )
        from app.services.application_agents import SqlAlchemyApplicationAgentService
        from app.services.runtime_workspace import Principal

        async with self.session_factory() as db:
            agents = list(
                (
                    await db.execute(
                    select(ApplicationAgent)
                        .where(
                            ApplicationAgent.apply_status == "applying",
                            ApplicationAgent.lifecycle_status == "active",
                        )
                        .order_by(ApplicationAgent.id.asc())
                        .limit(self.batch_size)
                    )
                ).scalars()
            )
            processed = 0
            deleting = list(
                (
                    await db.execute(
                        select(ApplicationAgent)
                        .where(ApplicationAgent.lifecycle_status == "deleting")
                        .order_by(ApplicationAgent.id.asc())
                        .limit(self.batch_size)
                    )
                ).scalars()
            )
            runtime_manager = SqlAlchemyApplicationAgentRuntimeManager(db)
            agent_service = SqlAlchemyApplicationAgentService(db)
            for agent in deleting:
                agent_id = int(agent.id)
                org_id = int(agent.org_id)
                try:
                    await runtime_manager.delete_runtime(
                        org_id=org_id,
                        agent_id=agent_id,
                    )
                    await agent_service.delete_agent(
                        principal=Principal(
                            org_id=org_id,
                            user_id=int(agent.created_by_user_id),
                        ),
                        agent_id=agent_id,
                    )
                    processed += 1
                except Exception as exc:
                    await db.rollback()
                    logger.exception(
                        "application Agent deletion failed",
                        extra={
                            "application_agent_id": agent_id,
                            "org_id": org_id,
                        },
                    )
                    await db.execute(
                        update(ApplicationAgent)
                        .where(ApplicationAgent.id == agent_id)
                        .values(
                            delete_last_error_code=_delete_error_code(exc)
                        )
                    )
                    await db.commit()
            for agent in agents:
                try:
                    if agent.lifecycle_status != "active":
                        continue
                    await runtime_manager.ensure_runtime(
                        org_id=int(agent.org_id),
                        agent_id=int(agent.id),
                    )
                except Exception:
                    # ensure_runtime records the failed Applied state; one app
                    # must not prevent unrelated application rollouts.
                    continue
                processed += 1
            return processed


__all__ = [
    "ApplicationAgentAssetRolloutService",
    "ApplicationAgentAssetRolloutWorker",
]
