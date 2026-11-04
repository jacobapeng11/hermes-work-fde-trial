from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AssetRolloutBatch,
    ExpertPackage,
    ExpertPackageVersion,
    InstalledExpertBinding,
    RuntimeAgentProfile,
    Skill,
)
from app.schemas.profile_desired import (
    ProfileDesiredCommand,
    ProfileDesiredDefinition,
    ProfileDesiredMcpRef,
    ProfileDesiredResult,
    ProfileDesiredSkillRef,
)
from app.services.profile_desired import ProfileDesiredService


class ExpertRolloutError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExpertRolloutService:
    """Resolve one approved expert version into a complete Profile Desired."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def apply(
        self,
        *,
        profile: RuntimeAgentProfile,
        batch: AssetRolloutBatch,
    ) -> ProfileDesiredResult:
        binding = await self.db.scalar(
            select(InstalledExpertBinding)
            .where(
                InstalledExpertBinding.runtime_agent_profile_id == profile.id,
                InstalledExpertBinding.expert_package_id == batch.asset_id,
                InstalledExpertBinding.status == "enabled",
            )
            .with_for_update()
        )
        if binding is None:
            raise ExpertRolloutError("EXPERT_INSTALLATION_NOT_FOUND")
        if str(batch.action) in {"revoke", "enable"}:
            raise ExpertRolloutError("EXPERT_EXECUTION_CONTROL_NOT_DESIRED")

        version = await self._target_version(batch)
        return await self.apply_version(
            profile=profile,
            binding=binding,
            version=version,
            source_id=str(batch.rollout_id),
            created_by_user_id=int(batch.created_by_user_id),
        )

    async def apply_version(
        self,
        *,
        profile: RuntimeAgentProfile,
        binding: InstalledExpertBinding,
        version: ExpertPackageVersion,
        source_id: str,
        created_by_user_id: int,
        source_type: str = "asset_rollout",
    ) -> ProfileDesiredResult:
        result = await ProfileDesiredService(self.db).replace(
            await self.build_desired_command(
                profile=profile,
                version=version,
                source_id=source_id,
                created_by_user_id=created_by_user_id,
                source_type=source_type,
            )
        )
        binding.expert_package_version_id = int(version.id)
        return result

    async def _target_version(
        self,
        batch: AssetRolloutBatch,
    ) -> ExpertPackageVersion:
        package = await self.db.get(ExpertPackage, int(batch.asset_id))
        if package is None or package.status in {"deleted", "revoked"}:
            raise ExpertRolloutError("EXPERT_PACKAGE_UNAVAILABLE")
        target_version_id = int(batch.target_version_id or 0)
        if int(package.current_version_id or 0) != target_version_id:
            raise ExpertRolloutError("ROLLOUT_SUPERSEDED")
        version = await self.db.scalar(
            select(ExpertPackageVersion).where(
                ExpertPackageVersion.id == target_version_id,
                ExpertPackageVersion.expert_package_id == package.id,
                ExpertPackageVersion.org_id == batch.org_id,
                ExpertPackageVersion.status.in_(("approved", "published")),
            )
        )
        if version is None:
            raise ExpertRolloutError("EXPERT_VERSION_NOT_APPROVED")
        return version

    async def build_desired_command(
        self,
        *,
        profile: RuntimeAgentProfile,
        version: ExpertPackageVersion,
        source_id: str,
        created_by_user_id: int,
        source_type: str,
    ) -> ProfileDesiredCommand:
        manifest = dict(version.manifest or {})
        capabilities = dict(manifest.get("capabilities") or {})
        basic = dict(version.basic_info_snapshot or manifest.get("basic_info") or {})
        referenced_skill_ids = {
            int(item["asset_id"])
            for item in version.skill_refs or []
            if isinstance(item, dict) and item.get("asset_id") is not None
        }
        available_skill_ids = set(
            await self.db.scalars(
                select(Skill.id).where(
                    Skill.org_id == profile.org_id,
                    Skill.id.in_(referenced_skill_ids),
                    Skill.status != "deleted",
                    Skill.revoked_at.is_(None),
                    Skill.current_version_id.is_not(None),
                )
            )
        ) if referenced_skill_ids else set()
        skills: list[ProfileDesiredSkillRef] = []
        for item in version.skill_refs or []:
            if not isinstance(item, dict) or item.get("asset_id") is None:
                continue
            source = str(item.get("source") or "public")
            source_skill_id = int(item["asset_id"])
            del source
            if source_skill_id not in available_skill_ids:
                # Approved expert versions remain immutable audit snapshots.
                # Effective Desired omits a dependency that its owner has
                # permanently deleted or execution-revoked.
                continue
            skills.append(ProfileDesiredSkillRef(skill_id=source_skill_id))
        mcps = [
            ProfileDesiredMcpRef(mcp_id=int(item["asset_id"]))
            for item in version.mcp_refs or []
            if isinstance(item, dict) and item.get("asset_id") is not None
        ]
        knowledge_ids = [
            int(item["asset_id"])
            for item in version.knowledge_refs or []
            if isinstance(item, dict) and item.get("asset_id") is not None
        ]
        plugin_ids = [
            str(item["plugin_id"])
            for item in version.plugin_refs or []
            if isinstance(item, dict) and item.get("plugin_id")
        ]
        return ProfileDesiredCommand(
            org_id=int(profile.org_id),
            user_id=int(profile.user_id),
            runtime_workspace_id=int(profile.runtime_workspace_id),
            runtime_agent_profile_id=int(profile.id),
            definition=ProfileDesiredDefinition(
                display_name=str(basic.get("name") or profile.display_name),
                description=basic.get("description"),
                avatar_url=basic.get("avatar_url"),
                soul_content=str(version.soul_snapshot or ""),
                system_prompt=version.prompt_snapshot,
                enterprise_knowledge_enabled=bool(
                    capabilities.get("enterprise_knowledge_enabled", False)
                ),
                plugin_ids=plugin_ids,
            ),
            skills=skills,
            mcps=mcps,
            knowledge_base_ids=sorted(set(knowledge_ids)),
            expert_package_version_id=int(version.id),
            source_type=source_type,
            source_id=source_id,
            created_by_user_id=created_by_user_id,
        )

__all__ = ["ExpertRolloutError", "ExpertRolloutService"]
