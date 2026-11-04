from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InstalledExpertBinding, RuntimeAgentProfile, RuntimeSession
from app.repositories.scheduled_task import ScheduledTaskRepository
from app.schemas.profile_desired import ProfileDesiredCommand, ProfileDesiredDefinition
from app.services.expert_installations import ExpertInstallationService
from app.services.profile_desired_mutations import ProfileDesiredMutationService
from app.services.runtime_sessions import EXPERT_DELETED_SESSION_STATUS


class ExpertUninstallService:
    """Retire one expert's execution identity while preserving its history."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def retire(
        self,
        *,
        profile: RuntimeAgentProfile,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> None:
        if profile.ownership_type == "market_installed":
            await self._disable_market_installation(
                profile=profile,
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=runtime_workspace_id,
            )
        elif profile.ownership_type == "self_created":
            await self._disable_private_profile(
                profile=profile,
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=runtime_workspace_id,
            )
        else:
            raise ValueError("unsupported expert ownership type")

        await self._retire_sessions(
            profile=profile,
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
        )
        profile.status = "disabled"
        profile.active_display_name_key = None
        profile.last_error = None

    async def _disable_market_installation(
        self,
        *,
        profile: RuntimeAgentProfile,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> None:
        binding = await self.db.scalar(
            select(InstalledExpertBinding)
            .where(
                InstalledExpertBinding.runtime_agent_profile_id == profile.id,
                InstalledExpertBinding.org_id == org_id,
                InstalledExpertBinding.user_id == user_id,
                InstalledExpertBinding.runtime_workspace_id == runtime_workspace_id,
            )
            .with_for_update()
        )
        if binding is None:
            raise ValueError("market expert installation is unavailable")
        if binding.status == "disabled":
            return
        if binding.status != "enabled":
            raise ValueError("market expert installation cannot be uninstalled")
        await ExpertInstallationService(self.db).uninstall(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=int(profile.id),
            operation_id=f"expert-uninstall-{uuid4().hex}",
        )

    async def _disable_private_profile(
        self,
        *,
        profile: RuntimeAgentProfile,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> None:
        if profile.status in {"disabled", "deleted"}:
            return
        await ProfileDesiredMutationService(self.db).save_expert_complete(
            ProfileDesiredCommand(
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=int(profile.id),
                definition=ProfileDesiredDefinition(
                    display_name=str(profile.display_name),
                    description=profile.description,
                    avatar_url=profile.avatar_url,
                    soul_content=str(profile.soul_content or ""),
                ),
                source_type="private_expert",
                source_id=f"expert-disable-{uuid4().hex}",
                created_by_user_id=user_id,
            )
        )

    async def _retire_sessions(
        self,
        *,
        profile: RuntimeAgentProfile,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> None:
        sessions = list(
            await self.db.scalars(
                select(RuntimeSession)
                .where(
                    RuntimeSession.org_id == org_id,
                    RuntimeSession.user_id == user_id,
                    RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                    RuntimeSession.runtime_agent_profile_id == profile.id,
                    RuntimeSession.status == "active",
                )
                .with_for_update()
            )
        )
        scheduled_tasks = ScheduledTaskRepository(self.db)
        for session in sessions:
            await scheduled_tasks.pause_for_session(
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=runtime_workspace_id,
                session_id=int(session.id),
            )
            session.status = EXPERT_DELETED_SESSION_STATUS


__all__ = ["ExpertUninstallService"]
