from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ExpertPackage,
    ExpertPackageVersion,
    InstalledExpertBinding,
    RuntimeAgentProfile,
)
from app.schemas.profile_desired import (
    ProfileDesiredCommand,
    ProfileDesiredDefinition,
    ProfileDesiredResult,
)
from app.services.expert_rollout import ExpertRolloutService
from app.services.expert_authoring import ExpertAuthoringError, ExpertAuthoringService
from app.services.profile_desired import ProfileDesiredService
from app.services.expert_visibility import ExpertVisibilityPolicy
from app.services.runtime_agent_profiles import (
    RuntimeAgentProfileError,
    SqlAlchemyRuntimeAgentProfileStore,
)
from app.services.runtime_workspace import Principal
from app.services.expert_errors import ExpertProfileError


class ExpertInstallationError(ExpertProfileError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExpertInstallationService:
    """Own the stable Workspace/Package slot and each installed Profile epoch."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def install(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        expert_package_id: int,
        operation_id: str,
        capability_config: dict | None = None,
    ) -> RuntimeAgentProfile:
        """Install a visible package; reinstall creates a fresh Profile identity."""
        binding = await self.db.scalar(
            select(InstalledExpertBinding)
            .where(
                InstalledExpertBinding.org_id == principal.org_id,
                InstalledExpertBinding.user_id == principal.user_id,
                InstalledExpertBinding.runtime_workspace_id
                == runtime_workspace_id,
                InstalledExpertBinding.expert_package_id == expert_package_id,
            )
            .with_for_update()
        )
        if binding is not None and binding.status == "enabled":
            raise ExpertInstallationError("EXPERT_ALREADY_INSTALLED")
        if not await ExpertVisibilityPolicy(self.db).can_view_package(
            principal=principal,
            expert_package_id=expert_package_id,
        ):
            raise ExpertInstallationError("EXPERT_MARKET_ITEM_NOT_FOUND")
        package = await self.db.scalar(
            select(ExpertPackage)
            .where(
                ExpertPackage.id == expert_package_id,
                ExpertPackage.org_id == principal.org_id,
                ExpertPackage.status == "published",
                ExpertPackage.execution_status == "enabled",
                ExpertPackage.current_version_id.is_not(None),
            )
            .with_for_update()
        )
        if package is None:
            raise ExpertInstallationError("EXPERT_MARKET_ITEM_NOT_FOUND")
        if int(package.owner_user_id) == principal.user_id:
            raise ExpertInstallationError("EXPERT_ALREADY_INSTALLED")
        version = await self.db.scalar(
            select(ExpertPackageVersion).where(
                ExpertPackageVersion.id == package.current_version_id,
                ExpertPackageVersion.expert_package_id == package.id,
                ExpertPackageVersion.status.in_(("approved", "published")),
            )
        )
        if version is None:
            raise ExpertInstallationError("EXPERT_MARKET_ITEM_NOT_FOUND")
        basic = dict(version.basic_info_snapshot or {})
        try:
            profile = await SqlAlchemyRuntimeAgentProfileStore(
                self.db
            ).create_expert_row(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                display_name=str(basic.get("name") or package.name),
                description=basic.get("description") or package.description,
                avatar_url=basic.get("avatar_url") or package.avatar_url,
                soul_content=str(version.soul_snapshot or ""),
                ownership_type="market_installed",
                expert_package_id=int(package.id),
                capability_config=capability_config,
            )
        except RuntimeAgentProfileError as exc:
            raise ExpertInstallationError(str(exc)) from exc
        if binding is None:
            binding = InstalledExpertBinding(
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=int(profile.id),
                expert_package_id=int(package.id),
                expert_package_version_id=int(version.id),
                source_type="market",
                status="enabled",
                installation_epoch=1,
                projection_config={"schema_version": "profile-desired/v1"},
            )
            self.db.add(binding)
        else:
            if binding.status != "disabled":
                raise ExpertInstallationError("EXPERT_INSTALLATION_STATE_INVALID")
            binding.runtime_agent_profile_id = int(profile.id)
            binding.expert_package_version_id = int(version.id)
            binding.installation_epoch = int(binding.installation_epoch or 1) + 1
            binding.status = "enabled"
        await self.db.flush()
        await ExpertRolloutService(self.db).apply_version(
            profile=profile,
            binding=binding,
            version=version,
            source_id=operation_id,
            created_by_user_id=principal.user_id,
            source_type="market_expert",
        )
        return profile

    async def uninstall(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        operation_id: str,
    ) -> ProfileDesiredResult:
        profile, binding = await self._profile_binding(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            statuses=("enabled",),
        )
        result = await ProfileDesiredService(self.db).replace(
            ProfileDesiredCommand(
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=runtime_agent_profile_id,
                definition=ProfileDesiredDefinition(
                    display_name=str(profile.display_name),
                    description=profile.description,
                    avatar_url=profile.avatar_url,
                    soul_content=str(profile.soul_content or ""),
                ),
                skills=[],
                mcps=[],
                knowledge_base_ids=[],
                source_type="market_expert",
                source_id=operation_id,
                created_by_user_id=user_id,
            )
        )
        binding.status = "disabled"
        profile.status = "disabled"
        profile.active_display_name_key = None
        return result

    async def remove(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
    ) -> None:
        """Remove one owned expert according to its ownership lifecycle."""
        record = await SqlAlchemyRuntimeAgentProfileStore(self.db).require_owned(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            ready_only=False,
        )
        profile = await self.db.get(RuntimeAgentProfile, record.runtime_agent_profile_id)
        if profile is None or profile.profile_kind != "expert":
            raise ExpertInstallationError("EXPERT_INSTALLATION_NOT_FOUND")
        if profile.ownership_type == "self_created":
            try:
                await ExpertAuthoringService(self.db).delete(
                    principal=principal,
                    runtime_workspace_id=runtime_workspace_id,
                    runtime_agent_profile_id=runtime_agent_profile_id,
                )
            except ExpertAuthoringError as exc:
                raise ExpertInstallationError(str(exc)) from exc
            return
        if profile.ownership_type != "market_installed":
            raise ExpertInstallationError("EXPERT_OWNERSHIP_INVALID")
        from app.services.expert_uninstall import ExpertUninstallService

        try:
            await ExpertUninstallService(self.db).retire(
                profile=profile,
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
            )
        except (ValueError, ExpertInstallationError) as exc:
            raise ExpertInstallationError(str(exc)) from exc

    async def _profile_binding(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        statuses: tuple[str, ...],
    ) -> tuple[RuntimeAgentProfile, InstalledExpertBinding]:
        row = (
            await self.db.execute(
                select(RuntimeAgentProfile, InstalledExpertBinding)
                .join(
                    InstalledExpertBinding,
                    InstalledExpertBinding.runtime_agent_profile_id
                    == RuntimeAgentProfile.id,
                )
                .where(
                    RuntimeAgentProfile.id == runtime_agent_profile_id,
                    RuntimeAgentProfile.org_id == org_id,
                    RuntimeAgentProfile.user_id == user_id,
                    RuntimeAgentProfile.runtime_workspace_id
                    == runtime_workspace_id,
                    RuntimeAgentProfile.profile_kind == "expert",
                    RuntimeAgentProfile.status != "deleted",
                    InstalledExpertBinding.status.in_(statuses),
                )
                .with_for_update()
            )
        ).first()
        if row is None:
            raise ExpertInstallationError("EXPERT_INSTALLATION_NOT_FOUND")
        return row[0], row[1]


__all__ = ["ExpertInstallationError", "ExpertInstallationService"]
