"""Explicit failed-Profile cleanup boundary used by operator tooling."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.hermes_runtime.profile_adapter import HermesProfileAdapter
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models import RuntimeAgentProfile
from app.services.expert_authoring import ExpertAuthoringService
from app.services.expert_installations import ExpertInstallationService
from app.services.runtime_workspace import Principal


class ExpertProfileCleanupError(RuntimeError):
    pass


class FailedExpertProfileCleanupService:
    """Delete a confirmed failed Profile from Hermes and tombstone Backend state."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        hermes_profiles: HermesProfileAdapter,
    ) -> None:
        self.db = db
        self.hermes_profiles = hermes_profiles

    async def uninstall(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        endpoint: RuntimeEndpoint,
    ) -> None:
        profile = await self.db.scalar(
            select(RuntimeAgentProfile)
            .where(
                RuntimeAgentProfile.id == runtime_agent_profile_id,
                RuntimeAgentProfile.org_id == principal.org_id,
                RuntimeAgentProfile.user_id == principal.user_id,
                RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
                RuntimeAgentProfile.profile_kind == "expert",
                RuntimeAgentProfile.status != "deleted",
            )
            .with_for_update()
        )
        if profile is None:
            raise ExpertProfileCleanupError("expert profile not found")
        await self.hermes_profiles.delete_profile(
            endpoint,
            name=str(profile.hermes_profile_name),
        )
        if profile.ownership_type == "self_created":
            await ExpertAuthoringService(self.db).delete(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=runtime_agent_profile_id,
            )
        elif profile.ownership_type == "market_installed":
            await ExpertInstallationService(self.db).uninstall(
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=runtime_agent_profile_id,
                operation_id=f"failed-cleanup-{runtime_agent_profile_id}",
            )
            profile.status = "deleted"
            profile.active_display_name_key = None
        else:
            raise ExpertProfileCleanupError("unsupported expert ownership")
        await self.db.commit()


__all__ = ["ExpertProfileCleanupError", "FailedExpertProfileCleanupService"]
