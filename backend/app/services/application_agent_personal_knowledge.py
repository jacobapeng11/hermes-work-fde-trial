from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApplicationAgent,
    ApplicationAgentConfigRevision,
    ApplicationAgentPersonalKnowledgeBinding,
    KnowledgeBase,
)
from app.services.application_agents import ApplicationAgentError
from app.services.application_agent_asset_snapshot import (
    ApplicationAgentAssetSnapshotBuilder,
)
from app.services.personal_knowledge import PersonalKnowledgeService
from app.services.personal_knowledge_acl import (
    PersonalKnowledgeAclError,
    PersonalKnowledgeAclService,
)
from app.services.runtime_workspace import Principal


@dataclass(frozen=True)
class ApplicationPersonalKnowledgeCandidate:
    knowledge_base_id: int
    name: str
    description: str | None
    owner_user_id: int
    owner_name: str
    effective_role: str
    file_count: int
    ready_file_count: int
    visibility: str
    status: str
    created_at: datetime
    updated_at: datetime
    selected: bool


@dataclass(frozen=True)
class ApplicationPersonalKnowledgeCatalog:
    items: list[ApplicationPersonalKnowledgeCandidate]
    enabled_count: int
    revoked_count: int
    config_generation: int


class ApplicationAgentPersonalKnowledgeService:
    """Application selection authority layered over personal knowledge ACL."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.acl = PersonalKnowledgeAclService(db)
        self.personal = PersonalKnowledgeService(db, acl=self.acl)

    async def catalog(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> ApplicationPersonalKnowledgeCatalog:
        agent = await self._owned_agent(principal, agent_id, for_update=True)
        summaries = await self.personal.list_for_user(
            principal=principal,
            runtime_workspace_id=None,
            keyword=None,
            ownership="all",
        )
        visible_ids = {int(item.knowledge_base_id) for item in summaries}
        bindings = await self._bindings(agent_id)
        changed = False
        now = _now()
        for binding in bindings:
            if (
                binding.status == "enabled"
                and int(binding.knowledge_base_id) not in visible_ids
            ):
                binding.status = "permission_revoked"
                binding.revoked_at = now
                changed = True
        if changed:
            await self._advance_generation(agent, principal.user_id)
            await self.db.commit()
            bindings = await self._bindings(agent_id)

        status_by_id = {
            int(binding.knowledge_base_id): str(binding.status)
            for binding in bindings
        }
        items = [
            ApplicationPersonalKnowledgeCandidate(
                knowledge_base_id=int(item.knowledge_base_id),
                name=item.name,
                description=item.description,
                owner_user_id=item.owner_user_id,
                owner_name=item.owner_name,
                effective_role=item.effective_role,
                file_count=item.file_count,
                ready_file_count=item.ready_file_count,
                visibility=item.visibility,
                status=item.status,
                created_at=item.created_at,
                updated_at=item.updated_at,
                selected=(
                    status_by_id.get(int(item.knowledge_base_id)) == "enabled"
                ),
            )
            for item in summaries
        ]
        return ApplicationPersonalKnowledgeCatalog(
            items=items,
            enabled_count=sum(
                binding.status == "enabled" for binding in bindings
            ),
            revoked_count=sum(
                binding.status == "permission_revoked"
                for binding in bindings
            ),
            config_generation=int(agent.config_generation),
        )

    async def select(
        self,
        *,
        principal: Principal,
        agent_id: int,
        knowledge_base_id: int,
    ) -> ApplicationPersonalKnowledgeCatalog:
        agent = await self._owned_agent(principal, agent_id, for_update=True)
        try:
            knowledge_base, _role = await self.acl.accessible(
                principal=principal,
                knowledge_base_id=knowledge_base_id,
                capability="retrieve",
            )
        except PersonalKnowledgeAclError as exc:
            raise ApplicationAgentError(
                "application_agent_personal_knowledge_unavailable",
                status_code=exc.status_code,
            ) from exc
        if (
            knowledge_base.status != "active"
            or not knowledge_base.dataset_id
        ):
            raise ApplicationAgentError(
                "application_agent_personal_knowledge_unavailable",
                status_code=409,
            )
        binding = await self._binding(
            agent_id,
            knowledge_base_id,
            for_update=True,
        )
        now = _now()
        if binding is None:
            binding = ApplicationAgentPersonalKnowledgeBinding(
                org_id=principal.org_id,
                application_agent_id=agent_id,
                knowledge_base_id=knowledge_base_id,
                selected_by_user_id=principal.user_id,
                status="enabled",
                selected_at=now,
                revoked_at=None,
            )
            self.db.add(binding)
        elif binding.status != "enabled":
            binding.status = "enabled"
            binding.selected_by_user_id = principal.user_id
            binding.selected_at = now
            binding.revoked_at = None
        else:
            return await self.catalog(
                principal=principal,
                agent_id=agent_id,
            )
        await self._advance_generation(agent, principal.user_id)
        await self.db.commit()
        return await self.catalog(principal=principal, agent_id=agent_id)

    async def disable(
        self,
        *,
        principal: Principal,
        agent_id: int,
        knowledge_base_id: int,
    ) -> ApplicationPersonalKnowledgeCatalog:
        agent = await self._owned_agent(principal, agent_id, for_update=True)
        binding = await self._binding(
            agent_id,
            knowledge_base_id,
            for_update=True,
        )
        if binding is not None and binding.status != "disabled":
            binding.status = "disabled"
            binding.revoked_at = None
            await self._advance_generation(agent, principal.user_id)
            await self.db.commit()
        return await self.catalog(principal=principal, agent_id=agent_id)

    async def _owned_agent(
        self,
        principal: Principal,
        agent_id: int,
        *,
        for_update: bool,
    ) -> ApplicationAgent:
        statement = select(ApplicationAgent).where(
            ApplicationAgent.id == agent_id,
            ApplicationAgent.org_id == principal.org_id,
        )
        if for_update:
            statement = statement.with_for_update()
        agent = (await self.db.execute(statement)).scalar_one_or_none()
        if agent is None:
            raise ApplicationAgentError(
                "application_agent_not_found",
                status_code=404,
            )
        if int(agent.created_by_user_id) != principal.user_id:
            raise ApplicationAgentError(
                "application_agent_owner_required",
                status_code=403,
            )
        return agent

    async def _bindings(
        self,
        agent_id: int,
    ) -> list[ApplicationAgentPersonalKnowledgeBinding]:
        return list(
            (
                await self.db.execute(
                    select(ApplicationAgentPersonalKnowledgeBinding)
                    .where(
                        ApplicationAgentPersonalKnowledgeBinding
                        .application_agent_id == agent_id
                    )
                    .order_by(
                        ApplicationAgentPersonalKnowledgeBinding
                        .knowledge_base_id
                    )
                )
            ).scalars()
        )

    async def _binding(
        self,
        agent_id: int,
        knowledge_base_id: int,
        *,
        for_update: bool,
    ) -> ApplicationAgentPersonalKnowledgeBinding | None:
        statement = select(
            ApplicationAgentPersonalKnowledgeBinding
        ).where(
            ApplicationAgentPersonalKnowledgeBinding.application_agent_id
            == agent_id,
            ApplicationAgentPersonalKnowledgeBinding.knowledge_base_id
            == knowledge_base_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self.db.execute(statement)).scalar_one_or_none()

    async def _advance_generation(
        self,
        agent: ApplicationAgent,
        actor_user_id: int,
    ) -> None:
        await self.db.flush()
        bindings = await self._bindings(int(agent.id))
        digest = hashlib.sha256(
            json.dumps(
                [
                    [int(item.knowledge_base_id), str(item.status)]
                    for item in bindings
                ],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if digest == str(agent.personal_knowledge_binding_digest):
            return
        agent.personal_knowledge_binding_digest = digest
        agent.config_generation = int(agent.config_generation) + 1
        agent.updated_by_user_id = actor_user_id
        if agent.status == "published":
            agent.apply_status = "applying"
        cleaned_config = _clean_config(agent.config or {})
        if cleaned_config != (agent.config or {}):
            agent.config = cleaned_config
        snapshot = await ApplicationAgentAssetSnapshotBuilder(
            self.db
        ).build(
            org_id=int(agent.org_id),
            application_agent_id=int(agent.id),
            config=cleaned_config,
        )
        snapshot["personal_knowledge_binding_digest"] = digest
        self.db.add(
            ApplicationAgentConfigRevision(
                org_id=int(agent.org_id),
                application_agent_id=int(agent.id),
                generation=int(agent.config_generation),
                config_snapshot=snapshot,
                config_hash=hashlib.sha256(
                    json.dumps(
                        snapshot,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                created_by_user_id=actor_user_id,
                apply_status=str(agent.apply_status),
            )
        )


async def binding_counts(
    db: AsyncSession,
    *,
    application_agent_id: int,
) -> tuple[int, int]:
    rows = (
        await db.execute(
            select(
                ApplicationAgentPersonalKnowledgeBinding.status,
                func.count(
                    ApplicationAgentPersonalKnowledgeBinding.id
                ),
            )
            .where(
                ApplicationAgentPersonalKnowledgeBinding
                .application_agent_id == application_agent_id
            )
            .group_by(
                ApplicationAgentPersonalKnowledgeBinding.status
            )
        )
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    return counts.get("enabled", 0), counts.get("permission_revoked", 0)


async def mark_creator_permission_revoked(
    db: AsyncSession,
    *,
    application_agent_id: int,
    knowledge_base_ids: set[int],
) -> None:
    if not knowledge_base_ids:
        return
    agent = (
        await db.execute(
            select(ApplicationAgent)
            .where(ApplicationAgent.id == application_agent_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if agent is None:
        return
    bindings = list(
        (
            await db.execute(
                select(ApplicationAgentPersonalKnowledgeBinding)
                .where(
                    ApplicationAgentPersonalKnowledgeBinding
                    .application_agent_id == application_agent_id,
                    ApplicationAgentPersonalKnowledgeBinding
                    .knowledge_base_id.in_(knowledge_base_ids),
                    ApplicationAgentPersonalKnowledgeBinding.status
                    == "enabled",
                )
                .with_for_update()
            )
        ).scalars()
    )
    if not bindings:
        return
    now = _now()
    for binding in bindings:
        binding.status = "permission_revoked"
        binding.revoked_at = now
    await ApplicationAgentPersonalKnowledgeService(
        db
    )._advance_generation(
        agent,
        int(agent.created_by_user_id),
    )
    await db.commit()


async def reconcile_bindings_after_acl_change(
    db: AsyncSession,
    *,
    knowledge_base: KnowledgeBase,
) -> None:
    """Persist creator ACL loss while keeping revoked metadata opaque."""

    rows = (
        await db.execute(
            select(
                ApplicationAgentPersonalKnowledgeBinding,
                ApplicationAgent,
            )
            .join(
                ApplicationAgent,
                ApplicationAgent.id
                == ApplicationAgentPersonalKnowledgeBinding
                .application_agent_id,
            )
            .where(
                ApplicationAgentPersonalKnowledgeBinding
                .knowledge_base_id == int(knowledge_base.id),
                ApplicationAgentPersonalKnowledgeBinding.status
                == "enabled",
            )
            .with_for_update()
        )
    ).all()
    if not rows:
        return
    service = ApplicationAgentPersonalKnowledgeService(db)
    now = _now()
    changed_agents: dict[int, ApplicationAgent] = {}
    for binding, agent in rows:
        role = await service.acl.effective_role(
            principal=Principal(
                org_id=int(agent.org_id),
                user_id=int(agent.created_by_user_id),
            ),
            knowledge_base=knowledge_base,
        )
        if role is not None:
            continue
        binding.status = "permission_revoked"
        binding.revoked_at = now
        changed_agents[int(agent.id)] = agent
    for agent in changed_agents.values():
        await service._advance_generation(
            agent,
            int(agent.created_by_user_id),
        )


async def revoke_bindings_for_deleted_knowledge(
    db: AsyncSession,
    *,
    knowledge_base_id: int,
) -> None:
    """Preserve opaque revoked bindings before deleting a personal library."""

    rows = (
        await db.execute(
            select(
                ApplicationAgentPersonalKnowledgeBinding,
                ApplicationAgent,
            )
            .join(
                ApplicationAgent,
                ApplicationAgent.id
                == ApplicationAgentPersonalKnowledgeBinding
                .application_agent_id,
            )
            .where(
                ApplicationAgentPersonalKnowledgeBinding
                .knowledge_base_id == knowledge_base_id,
                ApplicationAgentPersonalKnowledgeBinding.status
                == "enabled",
            )
            .with_for_update()
        )
    ).all()
    if not rows:
        return
    service = ApplicationAgentPersonalKnowledgeService(db)
    now = _now()
    changed_agents: dict[int, ApplicationAgent] = {}
    for binding, agent in rows:
        binding.status = "permission_revoked"
        binding.revoked_at = now
        changed_agents[int(agent.id)] = agent
    for agent in changed_agents.values():
        await service._advance_generation(
            agent,
            int(agent.created_by_user_id),
        )


def _clean_config(config: dict) -> dict:
    result = dict(config)
    result.pop("knowledge_bindings", None)
    result.pop("delegated_personal_knowledge_enabled", None)
    return result


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
