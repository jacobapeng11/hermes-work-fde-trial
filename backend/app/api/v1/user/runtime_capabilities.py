from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.database import get_db
from app.models import (
    CloudHermesProfile,
    ExpertPackage,
    InstalledExpertBinding,
    KnowledgeBase,
    Mcp,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    Skill,
)
from app.schemas.runtime_workspace import RuntimeCapabilitiesResponse, RuntimeCapabilitySummary
from app.services.runtime_workspace import Principal

router = APIRouter(prefix="/runtime-workspace", tags=["user-runtime-capabilities"])


@dataclass(frozen=True)
class RuntimeCapabilityRecord:
    asset_type: str
    asset_id: int
    display_name: str
    status: str
    source_type: str | None = None


class InMemoryRuntimeCapabilityStore:
    def __init__(self) -> None:
        self._applied_assets: dict[str, set[int]] | None = None

    def set_applied_lock(self, lock: dict) -> None:
        assets = {"expert_package": set(), "skill": set(), "mcp": set(), "knowledge_base": set()}
        for item in lock.get("assets", []) if isinstance(lock, dict) else []:
            if (
                isinstance(item, dict)
                and item.get("action") == "install"
                and item.get("asset_type") in assets
            ):
                if isinstance(item.get("asset_id"), int):
                    assets[item["asset_type"]].add(item["asset_id"])
        self._applied_assets = assets

    async def list_capabilities(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> dict[str, list[RuntimeCapabilityRecord]]:
        if org_id != 1 or user_id != 1 or runtime_workspace_id != 1:
            return {"experts": [], "skills": [], "mcps": [], "knowledge_bases": []}
        if self._applied_assets is not None:
            return {
                "experts": [
                    RuntimeCapabilityRecord(
                        "expert_package", asset_id, f"Expert {asset_id}", "enabled", "applied"
                    )
                    for asset_id in sorted(self._applied_assets["expert_package"])
                ],
                "skills": [
                    RuntimeCapabilityRecord(
                        "skill", asset_id, f"Skill {asset_id}", "enabled", "applied"
                    )
                    for asset_id in sorted(self._applied_assets["skill"])
                ],
                "mcps": [
                    RuntimeCapabilityRecord(
                        "mcp", asset_id, f"MCP {asset_id}", "enabled", "applied"
                    )
                    for asset_id in sorted(self._applied_assets["mcp"])
                ],
                "knowledge_bases": [
                    RuntimeCapabilityRecord(
                        "knowledge_base",
                        asset_id,
                        f"Knowledge Base {asset_id}",
                        "enabled",
                        "applied",
                    )
                    for asset_id in sorted(self._applied_assets["knowledge_base"])
                ],
            }
        return {
            "experts": [
                RuntimeCapabilityRecord(
                    asset_type="expert_package",
                    asset_id=1,
                    display_name="Sales Expert",
                    status="enabled",
                    source_type="personal",
                )
            ],
            "skills": [],
            "mcps": [],
            "knowledge_bases": [],
        }


class SqlAlchemyRuntimeCapabilityStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_capabilities(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> dict[str, list[RuntimeCapabilityRecord]]:
        profile = await self.db.scalar(
            select(CloudHermesProfile.id).where(
                CloudHermesProfile.id == runtime_workspace_id,
                CloudHermesProfile.org_id == org_id,
                CloudHermesProfile.user_id == user_id,
                CloudHermesProfile.status == "active",
            )
        )
        if profile is None:
            raise PermissionError("runtime workspace not found")

        applied_assets = await self._applied_asset_ids(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
        )
        if applied_assets is None:
            # No Runtime-confirmed generation means no user asset is ready for
            # the chat capability area. Legacy bindings are not an applied
            # generation and must not leak into the new global contract.
            return {"experts": [], "skills": [], "mcps": [], "knowledge_bases": []}

        return {
            "experts": await self._experts(
                org_id=org_id,
                runtime_workspace_id=runtime_workspace_id,
                asset_ids=applied_assets["expert_package"],
            ),
            "skills": await self._skills(
                org_id=org_id,
                runtime_workspace_id=runtime_workspace_id,
                asset_ids=applied_assets["skill"],
            ),
            "mcps": await self._mcps(
                org_id=org_id,
                runtime_workspace_id=runtime_workspace_id,
                asset_ids=applied_assets["mcp"],
            ),
            "knowledge_bases": await self._knowledge_bases(
                org_id=org_id,
                runtime_workspace_id=runtime_workspace_id,
                asset_ids=applied_assets["knowledge_base"],
            ),
        }

    async def _applied_asset_ids(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> dict[str, set[int]] | None:
        profile = await self.db.scalar(
            select(RuntimeAgentProfile).where(
                RuntimeAgentProfile.org_id == org_id,
                RuntimeAgentProfile.user_id == user_id,
                RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
                RuntimeAgentProfile.profile_kind == "default",
                RuntimeAgentProfile.status != "deleted",
            )
        )
        if profile is None or int(profile.applied_revision or 0) == 0:
            return None
        revision = await self.db.scalar(
            select(RuntimeAgentProfileRevision).where(
                RuntimeAgentProfileRevision.runtime_agent_profile_id == profile.id,
                RuntimeAgentProfileRevision.revision == profile.applied_revision,
            )
        )
        if revision is None or str(revision.profile_digest) != str(profile.applied_digest):
            return None
        snapshot = dict(revision.snapshot or {})
        knowledge = dict(snapshot.get("knowledge") or {})
        return {
            "expert_package": set(),
            "skill": {
                int(item["skill_id"])
                for item in snapshot.get("skills", [])
                if isinstance(item, dict) and item.get("skill_id") is not None
            },
            "mcp": {
                int(item["mcp_id"])
                for item in snapshot.get("mcps", [])
                if isinstance(item, dict) and item.get("mcp_id") is not None
            },
            "knowledge_base": {
                int(item) for item in knowledge.get("personal_knowledge_base_ids", [])
            },
        }

    async def _experts(
        self, *, org_id: int, runtime_workspace_id: int, asset_ids: set[int]
    ) -> list[RuntimeCapabilityRecord]:
        result = await self.db.execute(
            select(InstalledExpertBinding, ExpertPackage)
            .join(ExpertPackage, ExpertPackage.id == InstalledExpertBinding.expert_package_id)
            .where(
                InstalledExpertBinding.org_id == org_id,
                InstalledExpertBinding.runtime_workspace_id == runtime_workspace_id,
                InstalledExpertBinding.status == "enabled",
                InstalledExpertBinding.expert_package_id.in_(asset_ids),
            )
        )
        return [
            RuntimeCapabilityRecord(
                asset_type="expert_package",
                asset_id=int(expert.id),
                display_name=str(expert.name),
                status=str(binding.status),
                source_type=str(binding.source_type),
            )
            for binding, expert in result.all()
        ]

    async def _skills(
        self, *, org_id: int, runtime_workspace_id: int, asset_ids: set[int]
    ) -> list[RuntimeCapabilityRecord]:
        result = await self.db.execute(
            select(Skill).where(
                Skill.org_id == org_id,
                Skill.id.in_(asset_ids),
                Skill.status.notin_(("deleted", "revoked")),
                Skill.revoked_at.is_(None),
            )
        )
        return [
            RuntimeCapabilityRecord(
                asset_type="skill",
                asset_id=int(skill.id),
                display_name=str(skill.name),
                status="enabled",
                source_type="default_profile",
            )
            for skill in result.scalars().all()
        ]

    async def _mcps(
        self, *, org_id: int, runtime_workspace_id: int, asset_ids: set[int]
    ) -> list[RuntimeCapabilityRecord]:
        result = await self.db.execute(
            select(Mcp).where(
                Mcp.org_id == org_id,
                Mcp.id.in_(asset_ids),
                Mcp.status == "published",
                Mcp.revoked_at.is_(None),
            )
        )
        return [
            RuntimeCapabilityRecord(
                asset_type="mcp",
                asset_id=int(mcp.id),
                display_name=str(mcp.name),
                status="enabled",
                source_type="default_profile",
            )
            for mcp in result.scalars().all()
        ]

    async def _knowledge_bases(
        self, *, org_id: int, runtime_workspace_id: int, asset_ids: set[int]
    ) -> list[RuntimeCapabilityRecord]:
        result = await self.db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.org_id == org_id,
                KnowledgeBase.id.in_(asset_ids),
                KnowledgeBase.status == "active",
            )
        )
        return [
            RuntimeCapabilityRecord(
                asset_type="knowledge_base",
                asset_id=int(knowledge_base.id),
                display_name=str(knowledge_base.name),
                status="enabled",
                source_type="default_profile",
            )
            for knowledge_base in result.scalars().all()
        ]


_runtime_capability_store = InMemoryRuntimeCapabilityStore()


async def get_runtime_capability_store(db: AsyncSession = Depends(get_db)):
    if settings.RUNTIME_CHAT_MODE == "real":
        return SqlAlchemyRuntimeCapabilityStore(db)
    return _runtime_capability_store


def _summary(record: RuntimeCapabilityRecord) -> RuntimeCapabilitySummary:
    return RuntimeCapabilitySummary(
        asset_type=record.asset_type,
        asset_id=record.asset_id,
        display_name=record.display_name,
        status=record.status,
        source_type=record.source_type,
    )


@router.get("/{runtime_workspace_id}/capabilities", response_model=RuntimeCapabilitiesResponse)
async def list_runtime_capabilities(
    runtime_workspace_id: int,
    principal: Principal = Depends(get_principal),
    store=Depends(get_runtime_capability_store),
) -> RuntimeCapabilitiesResponse:
    try:
        groups = await store.list_capabilities(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return RuntimeCapabilitiesResponse(
        runtime_workspace_id=runtime_workspace_id,
        experts=[_summary(record) for record in groups["experts"]],
        skills=[_summary(record) for record in groups["skills"]],
        mcps=[_summary(record) for record in groups["mcps"]],
        knowledge_bases=[_summary(record) for record in groups["knowledge_bases"]],
    )
