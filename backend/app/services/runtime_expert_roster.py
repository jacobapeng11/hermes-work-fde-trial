from __future__ import annotations

from dataclasses import dataclass
import hmac
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CloudHermesProfile,
    HermesRuntimeBinding,
    KnowledgeBase,
    Mcp,
    RuntimeAgentProfile,
    Skill,
    User,
)
from app.schemas.runtime_experts import (
    RuntimeExpertCapabilities,
    RuntimeExpertRosterItem,
    RuntimeExpertRosterResponse,
)
from app.services.runtime_agent_profiles import executable_runtime_agent_profile_clauses
from app.services.runtime_workspace import RuntimeApiKeyCodec


class RuntimeExpertRosterError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class RuntimeExpertRosterIdentity:
    org_id: int
    user_id: int
    runtime_workspace_id: int
    runtime_agent_profile_id: int


def _positive_ids(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()
    result: set[int] = set()
    for item in value:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            result.add(parsed)
    return result


def _plugin_ids(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


class RuntimeExpertRosterService:
    """Expose ready expert facts to an authorized Profile in the Runtime."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        codec: RuntimeApiKeyCodec | None = None,
    ) -> None:
        self.session = session
        self.codec = codec or RuntimeApiKeyCodec()

    async def roster(
        self,
        *,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> RuntimeExpertRosterResponse:
        identity = await self._authenticate(
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            runtime_token=runtime_token,
        )
        experts = (
            await self.session.execute(
                select(RuntimeAgentProfile)
                .where(
                    RuntimeAgentProfile.org_id == identity.org_id,
                    RuntimeAgentProfile.user_id == identity.user_id,
                    RuntimeAgentProfile.runtime_workspace_id
                    == identity.runtime_workspace_id,
                    RuntimeAgentProfile.profile_kind == "expert",
                    *executable_runtime_agent_profile_clauses(),
                )
                .order_by(
                    RuntimeAgentProfile.display_name.asc(),
                    RuntimeAgentProfile.id.asc(),
                )
            )
        ).scalars().all()
        skill_ids: set[int] = set()
        mcp_ids: set[int] = set()
        knowledge_ids: set[int] = set()
        normalized: dict[int, dict[str, Any]] = {}
        for expert in experts:
            config = dict(expert.capability_config or {})
            current = {
                "skill_ids": _positive_ids(config.get("skill_ids")),
                "mcp_ids": _positive_ids(config.get("mcp_ids")),
                "knowledge_base_ids": _positive_ids(
                    config.get("knowledge_base_ids")
                ),
                "plugin_ids": _plugin_ids(config.get("plugin_ids")),
            }
            normalized[int(expert.id)] = current
            skill_ids.update(current["skill_ids"])
            mcp_ids.update(current["mcp_ids"])
            knowledge_ids.update(current["knowledge_base_ids"])

        skill_names = await self._skill_names(identity.org_id, skill_ids)
        mcp_names = await self._mcp_names(identity.org_id, mcp_ids)
        knowledge_names = await self._knowledge_names(
            identity.org_id,
            knowledge_ids,
        )
        return RuntimeExpertRosterResponse(
            experts=[
                RuntimeExpertRosterItem(
                    display_name=str(expert.display_name),
                    assignee=str(expert.hermes_profile_name),
                    description=str(expert.description or ""),
                    capabilities=RuntimeExpertCapabilities(
                        skills=[
                            skill_names[item]
                            for item in sorted(normalized[int(expert.id)]["skill_ids"])
                            if item in skill_names
                        ],
                        mcps=[
                            mcp_names[item]
                            for item in sorted(normalized[int(expert.id)]["mcp_ids"])
                            if item in mcp_names
                        ],
                        knowledge_bases=[
                            knowledge_names[item]
                            for item in sorted(
                                normalized[int(expert.id)]["knowledge_base_ids"]
                            )
                            if item in knowledge_names
                        ],
                        plugins=list(normalized[int(expert.id)]["plugin_ids"]),
                    ),
                )
                for expert in experts
            ]
        )

    async def _authenticate(
        self,
        *,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> RuntimeExpertRosterIdentity:
        row = (
            await self.session.execute(
                select(
                    HermesRuntimeBinding,
                    CloudHermesProfile,
                    User,
                    RuntimeAgentProfile,
                )
                .join(
                    CloudHermesProfile,
                    CloudHermesProfile.id
                    == HermesRuntimeBinding.runtime_workspace_id,
                )
                .join(User, User.id == HermesRuntimeBinding.user_id)
                .join(
                    RuntimeAgentProfile,
                    RuntimeAgentProfile.runtime_workspace_id
                    == HermesRuntimeBinding.runtime_workspace_id,
                )
                .where(
                    HermesRuntimeBinding.runtime_workspace_id
                    == runtime_workspace_id,
                    HermesRuntimeBinding.status.in_(("starting", "ready")),
                    CloudHermesProfile.status == "active",
                    User.status == "active",
                    RuntimeAgentProfile.org_id == HermesRuntimeBinding.org_id,
                    RuntimeAgentProfile.user_id == HermesRuntimeBinding.user_id,
                    *executable_runtime_agent_profile_clauses(),
                    RuntimeAgentProfile.profile_kind.in_(("default", "expert")),
                    (
                        RuntimeAgentProfile.id == runtime_agent_profile_id
                        if runtime_agent_profile_id is not None
                        else RuntimeAgentProfile.profile_kind == "default"
                    ),
                )
            )
        ).first()
        if row is None:
            raise RuntimeExpertRosterError(
                "runtime_expert_roster_unauthorized",
                status_code=401,
            )
        binding, workspace, user, profile = row
        if not (
            binding.org_id == workspace.org_id == user.org_id == profile.org_id
            and binding.user_id == workspace.user_id == user.id == profile.user_id
        ):
            raise RuntimeExpertRosterError(
                "runtime_expert_roster_unauthorized",
                status_code=401,
            )
        expected = self.codec.decrypt(str(binding.api_key_encrypted))
        if not hmac.compare_digest(runtime_token, expected):
            raise RuntimeExpertRosterError(
                "runtime_expert_roster_unauthorized",
                status_code=401,
            )
        return RuntimeExpertRosterIdentity(
            org_id=int(user.org_id),
            user_id=int(user.id),
            runtime_workspace_id=int(workspace.id),
            runtime_agent_profile_id=int(profile.id),
        )

    async def _skill_names(
        self,
        org_id: int,
        asset_ids: set[int],
    ) -> dict[int, str]:
        if not asset_ids:
            return {}
        rows = (
            await self.session.execute(
                select(Skill).where(
                    Skill.org_id == org_id,
                    Skill.id.in_(asset_ids),
                    Skill.status.in_(("published", "archived")),
                )
            )
        ).scalars().all()
        return {int(item.id): str(item.name) for item in rows}

    async def _mcp_names(
        self,
        org_id: int,
        asset_ids: set[int],
    ) -> dict[int, str]:
        if not asset_ids:
            return {}
        rows = (
            await self.session.execute(
                select(Mcp).where(
                    Mcp.org_id == org_id,
                    Mcp.id.in_(asset_ids),
                    Mcp.status.in_(("published", "archived")),
                )
            )
        ).scalars().all()
        return {int(item.id): str(item.name) for item in rows}

    async def _knowledge_names(
        self,
        org_id: int,
        asset_ids: set[int],
    ) -> dict[int, str]:
        if not asset_ids:
            return {}
        rows = (
            await self.session.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.org_id == org_id,
                    KnowledgeBase.id.in_(asset_ids),
                    KnowledgeBase.status == "active",
                )
            )
        ).scalars().all()
        return {int(item.id): str(item.name) for item in rows}
