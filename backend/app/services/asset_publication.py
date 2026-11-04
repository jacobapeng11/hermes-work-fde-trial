from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Mcp, Skill
from app.schemas.asset_catalog import (
    AssetCatalogResult,
    PublishMcpVersionCommand,
    PublishSkillVersionCommand,
)
from app.schemas.asset_rollout import AssetRolloutCommand, AssetRolloutResult
from app.services.asset_catalog import AssetCatalogService
from app.services.asset_rollout import AssetRolloutService
from app.services.application_agent_asset_rollout import (
    ApplicationAgentAssetRolloutService,
)


@dataclass(frozen=True)
class AssetPublicationResult:
    catalog: AssetCatalogResult | None
    rollout: AssetRolloutResult | None


class PublicAssetPublicationService:
    """Atomically publish/revoke one public asset and freeze its rollout targets."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.catalog = AssetCatalogService(db)
        self.rollouts = AssetRolloutService(db)
        self.application_rollouts = ApplicationAgentAssetRolloutService(db)

    async def publish_skill(
        self,
        *,
        command: PublishSkillVersionCommand,
        rollout_id: str,
        created_by_user_id: int,
        action: Literal["publish", "rollback"] = "publish",
        change_summary: str | None = None,
    ) -> AssetPublicationResult:
        skill = await self.db.get(Skill, command.skill_id)
        source_version_id = int(skill.current_version_id) if skill and skill.current_version_id else None
        result = await self.catalog.publish_skill_version(command)
        skill = await self.db.get(Skill, command.skill_id)
        if (
            skill is None
            or str(skill.asset_scope) != "public"
            or not bool(skill.market_visible)
            or source_version_id == result.version_id
        ):
            return AssetPublicationResult(catalog=result, rollout=None)
        rollout = await self.rollouts.create(
            AssetRolloutCommand(
                rollout_id=rollout_id,
                org_id=command.org_id,
                asset_type="skill",
                asset_id=command.skill_id,
                action=action,
                source_version_id=source_version_id,
                target_version_id=result.version_id,
                created_by_user_id=created_by_user_id,
                change_summary=change_summary,
            )
        )
        await self.application_rollouts.refresh_public_asset(
            org_id=command.org_id,
            asset_type="skill",
            asset_id=command.skill_id,
            created_by_user_id=created_by_user_id,
        )
        return AssetPublicationResult(catalog=result, rollout=rollout)

    async def republish_skill(
        self,
        *,
        org_id: int,
        skill_id: int,
        rollout_id: str,
        created_by_user_id: int,
        change_summary: str | None = None,
    ) -> AssetPublicationResult:
        result = await self.catalog.republish_skill(org_id=org_id, skill_id=skill_id)
        rollout = await self.rollouts.create(
            AssetRolloutCommand(
                rollout_id=rollout_id,
                org_id=org_id,
                asset_type="skill",
                asset_id=skill_id,
                action="publish",
                target_version_id=result.version_id,
                created_by_user_id=created_by_user_id,
                change_summary=change_summary,
            )
        )
        await self.application_rollouts.refresh_public_asset(
            org_id=org_id,
            asset_type="skill",
            asset_id=skill_id,
            created_by_user_id=created_by_user_id,
        )
        return AssetPublicationResult(catalog=result, rollout=rollout)

    async def publish_mcp(
        self,
        *,
        command: PublishMcpVersionCommand,
        rollout_id: str,
        created_by_user_id: int,
        action: Literal["publish", "rollback"] = "publish",
        change_summary: str | None = None,
    ) -> AssetPublicationResult:
        mcp = await self.db.get(Mcp, command.mcp_id)
        source_version_id = int(mcp.current_version_id) if mcp and mcp.current_version_id else None
        result = await self.catalog.publish_mcp_version(command)
        mcp = await self.db.get(Mcp, command.mcp_id)
        if (
            mcp is None
            or str(mcp.visibility) == "private"
            or source_version_id == result.version_id
        ):
            return AssetPublicationResult(catalog=result, rollout=None)
        rollout = await self.rollouts.create(
            AssetRolloutCommand(
                rollout_id=rollout_id,
                org_id=command.org_id,
                asset_type="mcp",
                asset_id=command.mcp_id,
                action=action,
                source_version_id=source_version_id,
                target_version_id=result.version_id,
                created_by_user_id=created_by_user_id,
                change_summary=change_summary,
            )
        )
        await self.application_rollouts.refresh_public_asset(
            org_id=command.org_id,
            asset_type="mcp",
            asset_id=command.mcp_id,
            created_by_user_id=created_by_user_id,
        )
        return AssetPublicationResult(catalog=result, rollout=rollout)

    async def revoke_skill(
        self,
        *,
        org_id: int,
        skill_id: int,
        rollout_id: str,
        created_by_user_id: int,
        change_summary: str | None = None,
    ) -> AssetPublicationResult:
        skill = await self.db.get(Skill, skill_id)
        source_version_id = int(skill.current_version_id) if skill and skill.current_version_id else None
        await self.catalog.revoke_skill(org_id=org_id, skill_id=skill_id)
        rollout = await self.rollouts.create(
            AssetRolloutCommand(
                rollout_id=rollout_id,
                org_id=org_id,
                asset_type="skill",
                asset_id=skill_id,
                action="revoke",
                source_version_id=source_version_id,
                created_by_user_id=created_by_user_id,
                change_summary=change_summary,
            )
        )
        return AssetPublicationResult(catalog=None, rollout=rollout)

    async def revoke_mcp(
        self,
        *,
        org_id: int,
        mcp_id: int,
        rollout_id: str,
        created_by_user_id: int,
        change_summary: str | None = None,
    ) -> AssetPublicationResult:
        mcp = await self.db.get(Mcp, mcp_id)
        source_version_id = int(mcp.current_version_id) if mcp and mcp.current_version_id else None
        await self.catalog.revoke_mcp(org_id=org_id, mcp_id=mcp_id)
        rollout = await self.rollouts.create(
            AssetRolloutCommand(
                rollout_id=rollout_id,
                org_id=org_id,
                asset_type="mcp",
                asset_id=mcp_id,
                action="revoke",
                source_version_id=source_version_id,
                created_by_user_id=created_by_user_id,
                change_summary=change_summary,
            )
        )
        return AssetPublicationResult(catalog=None, rollout=rollout)

    async def set_skill_execution_enabled(
        self,
        *,
        org_id: int,
        skill_id: int,
        enabled: bool,
        rollout_id: str,
        created_by_user_id: int,
        reason: str,
    ) -> AssetPublicationResult:
        skill = await self.catalog.set_skill_execution_enabled(
            org_id=org_id,
            skill_id=skill_id,
            enabled=enabled,
        )
        rollout = await self.rollouts.create(
            AssetRolloutCommand(
                rollout_id=rollout_id,
                org_id=org_id,
                asset_type="skill",
                asset_id=skill_id,
                action="enable" if enabled else "revoke",
                source_version_id=(
                    int(skill.current_version_id) if skill.current_version_id else None
                ),
                created_by_user_id=created_by_user_id,
                change_summary=reason,
            )
        )
        return AssetPublicationResult(catalog=None, rollout=rollout)

    async def set_mcp_execution_enabled(
        self,
        *,
        org_id: int,
        mcp_id: int,
        enabled: bool,
        rollout_id: str,
        created_by_user_id: int,
        reason: str,
    ) -> AssetPublicationResult:
        mcp = await self.catalog.set_mcp_execution_enabled(
            org_id=org_id,
            mcp_id=mcp_id,
            enabled=enabled,
        )
        rollout = await self.rollouts.create(
            AssetRolloutCommand(
                rollout_id=rollout_id,
                org_id=org_id,
                asset_type="mcp",
                asset_id=mcp_id,
                action="enable" if enabled else "revoke",
                source_version_id=(
                    int(mcp.current_version_id) if mcp.current_version_id else None
                ),
                created_by_user_id=created_by_user_id,
                change_summary=reason,
            )
        )
        return AssetPublicationResult(catalog=None, rollout=rollout)

    async def permanently_delete_skill(
        self,
        *,
        org_id: int,
        skill_id: int,
        created_by_user_id: int,
    ) -> AssetPublicationResult:
        skill = await self.catalog.permanently_delete_public_skill(
            org_id=org_id,
            skill_id=skill_id,
        )
        rollout = await self.rollouts.create(
            AssetRolloutCommand(
                rollout_id=f"skill-delete-{skill_id}",
                org_id=org_id,
                asset_type="skill",
                asset_id=skill_id,
                action="delete",
                source_version_id=(
                    int(skill.current_version_id) if skill.current_version_id else None
                ),
                created_by_user_id=created_by_user_id,
            )
        )
        await self.application_rollouts.remove_public_asset(
            org_id=org_id,
            asset_type="skill",
            asset_id=skill_id,
            created_by_user_id=created_by_user_id,
        )
        return AssetPublicationResult(catalog=None, rollout=rollout)

    async def permanently_delete_mcp(
        self,
        *,
        org_id: int,
        mcp_id: int,
        created_by_user_id: int,
    ) -> AssetPublicationResult:
        mcp = await self.catalog.permanently_delete_public_mcp(
            org_id=org_id,
            mcp_id=mcp_id,
        )
        rollout = await self.rollouts.create(
            AssetRolloutCommand(
                rollout_id=f"mcp-delete-{mcp_id}",
                org_id=org_id,
                asset_type="mcp",
                asset_id=mcp_id,
                action="delete",
                source_version_id=(
                    int(mcp.current_version_id) if mcp.current_version_id else None
                ),
                created_by_user_id=created_by_user_id,
            )
        )
        await self.application_rollouts.remove_public_asset(
            org_id=org_id,
            asset_type="mcp",
            asset_id=mcp_id,
            created_by_user_id=created_by_user_id,
        )
        return AssetPublicationResult(catalog=None, rollout=rollout)


__all__ = ["AssetPublicationResult", "PublicAssetPublicationService"]
