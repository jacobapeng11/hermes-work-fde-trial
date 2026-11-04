from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ProfileMcpBinding, RuntimeAgentProfile
from app.services.profile_desired_mutations import ProfileDesiredMutationService


@dataclass(frozen=True, slots=True)
class McpIdentityMigrationResult:
    selected_user_count: int
    selected_workspace_count: int
    selected_profile_count: int
    refreshed_profile_count: int
    profile_ids: tuple[int, ...]


class McpIdentityMigrationService:
    """Plan or rebuild complete MCP-bearing Profiles for one bounded cohort."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def run(
        self,
        *,
        operation_id: str,
        created_by_user_id: int,
        user_id: int | None,
        apply: bool,
    ) -> McpIdentityMigrationResult:
        statement = (
            select(RuntimeAgentProfile)
            .join(
                ProfileMcpBinding,
                ProfileMcpBinding.runtime_agent_profile_id == RuntimeAgentProfile.id,
            )
            .where(
                RuntimeAgentProfile.status != "deleted",
                ProfileMcpBinding.status == "enabled",
            )
            .order_by(
                RuntimeAgentProfile.user_id,
                RuntimeAgentProfile.runtime_workspace_id,
                RuntimeAgentProfile.id,
            )
        )
        if user_id is not None:
            statement = statement.where(RuntimeAgentProfile.user_id == user_id)
        if apply:
            statement = statement.with_for_update()
        profiles = list((await self.db.execute(statement)).scalars().unique().all())
        disabled_users = sorted(
            {
                int(profile.user_id)
                for profile in profiles
                if not settings.mcp_runtime_identity_enabled(int(profile.user_id))
            }
        )
        if apply and disabled_users:
            raise RuntimeError(
                "MCP_RUNTIME_IDENTITY_MODE does not enable selected users: "
                + ",".join(str(item) for item in disabled_users)
            )
        refreshed = 0
        if apply:
            mutations = ProfileDesiredMutationService(self.db)
            for profile in profiles:
                result = await mutations.refresh_mcp_identity(
                    profile=profile,
                    operation_id=operation_id,
                    created_by_user_id=created_by_user_id,
                )
                refreshed += int(result.created_revision)
        return McpIdentityMigrationResult(
            selected_user_count=len({int(item.user_id) for item in profiles}),
            selected_workspace_count=len(
                {int(item.runtime_workspace_id) for item in profiles}
            ),
            selected_profile_count=len(profiles),
            refreshed_profile_count=refreshed,
            profile_ids=tuple(int(item.id) for item in profiles),
        )


__all__ = ["McpIdentityMigrationResult", "McpIdentityMigrationService"]
