from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CloudHermesProfile
from app.services.runtime_workspace import Principal


class EnterpriseKnowledgeWorkspaceNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class EnterpriseKnowledgeSettingsRecord:
    runtime_workspace_id: int
    enabled: bool
    mode: str
    updated_at: datetime | None


def enterprise_knowledge_enabled(profile_config: dict | None) -> bool:
    if not isinstance(profile_config, dict):
        return False
    knowledge = profile_config.get("knowledge")
    if not isinstance(knowledge, dict):
        return False
    enterprise = knowledge.get("enterprise")
    return bool(enterprise.get("enabled")) if isinstance(enterprise, dict) else False


def merge_enterprise_knowledge_config(profile_config: dict | None, *, enabled: bool) -> dict:
    merged = deepcopy(profile_config) if isinstance(profile_config, dict) else {}
    knowledge = merged.get("knowledge")
    if not isinstance(knowledge, dict):
        knowledge = {}
        merged["knowledge"] = knowledge
    enterprise = knowledge.get("enterprise")
    if not isinstance(enterprise, dict):
        enterprise = {}
        knowledge["enterprise"] = enterprise
    enterprise.update({"enabled": enabled, "mode": "full"})
    return merged


class EnterpriseKnowledgeSettingsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _statement(principal: Principal, runtime_workspace_id: int, *, lock: bool = False):
        statement = select(CloudHermesProfile).where(
            CloudHermesProfile.id == runtime_workspace_id,
            CloudHermesProfile.org_id == principal.org_id,
            CloudHermesProfile.user_id == principal.user_id,
            CloudHermesProfile.profile_type == "default_chat_agent",
            CloudHermesProfile.status == "active",
        )
        return statement.with_for_update() if lock else statement

    async def _profile(
        self,
        principal: Principal,
        runtime_workspace_id: int,
        *,
        lock: bool = False,
    ) -> CloudHermesProfile:
        result = await self.session.execute(self._statement(principal, runtime_workspace_id, lock=lock))
        profile = result.scalar_one_or_none()
        if profile is None:
            raise EnterpriseKnowledgeWorkspaceNotFoundError("runtime workspace not found")
        return profile

    @staticmethod
    def _record(profile: CloudHermesProfile) -> EnterpriseKnowledgeSettingsRecord:
        return EnterpriseKnowledgeSettingsRecord(
            runtime_workspace_id=int(profile.id),
            enabled=enterprise_knowledge_enabled(profile.profile_config),
            mode="full",
            updated_at=profile.updated_at,
        )

    async def get(
        self,
        principal: Principal,
        runtime_workspace_id: int,
    ) -> EnterpriseKnowledgeSettingsRecord:
        return self._record(await self._profile(principal, runtime_workspace_id))

    async def update(
        self,
        principal: Principal,
        runtime_workspace_id: int,
        *,
        enabled: bool,
    ) -> EnterpriseKnowledgeSettingsRecord:
        profile = await self._profile(principal, runtime_workspace_id, lock=True)
        if enterprise_knowledge_enabled(profile.profile_config) == enabled:
            return self._record(profile)
        profile.profile_config = merge_enterprise_knowledge_config(profile.profile_config, enabled=enabled)
        await self.session.commit()
        await self.session.refresh(profile)
        return self._record(profile)
