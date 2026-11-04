from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Mcp,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    Skill,
)
from app.schemas.profile_desired import (
    DefaultProfileAssetMutation,
    ProfileDesiredCommand,
    ProfileDesiredDefinition,
    ProfileDesiredMcpRef,
    ProfileDesiredResult,
    ProfileDesiredSkillRef,
)
from app.services.profile_desired import ProfileDesiredError, ProfileDesiredService


class ProfileDesiredMutationService:
    """Translate distinct business commands into one complete Desired snapshot."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.desired = ProfileDesiredService(db)

    async def mutate_default_asset(
        self,
        mutation: DefaultProfileAssetMutation,
    ) -> ProfileDesiredResult:
        return await self._mutate_asset(
            mutation,
            required_profile_kind="default",
        )

    async def mutate_profile_asset(
        self,
        mutation: DefaultProfileAssetMutation,
    ) -> ProfileDesiredResult:
        """Mutate one asset on an already-authorized default or expert Profile."""
        return await self._mutate_asset(mutation, required_profile_kind=None)

    async def _mutate_asset(
        self,
        mutation: DefaultProfileAssetMutation,
        *,
        required_profile_kind: str | None,
    ) -> ProfileDesiredResult:
        clauses = [
            RuntimeAgentProfile.id == mutation.runtime_agent_profile_id,
            RuntimeAgentProfile.org_id == mutation.org_id,
            RuntimeAgentProfile.user_id == mutation.user_id,
            RuntimeAgentProfile.runtime_workspace_id
            == mutation.runtime_workspace_id,
            RuntimeAgentProfile.status != "deleted",
        ]
        if required_profile_kind is not None:
            clauses.append(RuntimeAgentProfile.profile_kind == required_profile_kind)
        profile = await self.db.scalar(
            select(RuntimeAgentProfile)
            .where(*clauses)
            .with_for_update()
        )
        if profile is None:
            raise ProfileDesiredError(
                "DEFAULT_PROFILE_NOT_FOUND",
                "Default Runtime Agent Profile is unavailable",
            )
        snapshot = await self._current_snapshot(profile)
        skill_refs = self._skill_refs(snapshot)
        mcp_refs = self._mcp_refs(snapshot)
        knowledge_ids = self._knowledge_ids(snapshot)

        if mutation.asset_type == "skill":
            skill_refs = self._mutate_skills(
                skill_refs,
                action=mutation.action,
                asset_id=mutation.asset_id,
            )
        elif mutation.asset_type == "mcp":
            mcp_refs = self._mutate_mcps(
                mcp_refs,
                action=mutation.action,
                asset_id=mutation.asset_id,
            )
        else:
            knowledge_ids = self._mutate_ids(
                knowledge_ids,
                action=mutation.action,
                asset_id=mutation.asset_id,
            )

        return await self.desired.replace(
            ProfileDesiredCommand(
                org_id=mutation.org_id,
                user_id=mutation.user_id,
                runtime_workspace_id=mutation.runtime_workspace_id,
                runtime_agent_profile_id=mutation.runtime_agent_profile_id,
                definition=self._definition(profile=profile, snapshot=snapshot),
                skills=skill_refs,
                mcps=mcp_refs,
                knowledge_base_ids=knowledge_ids,
                source_type="default_user",
                source_id=mutation.operation_id,
                created_by_user_id=mutation.user_id,
            )
        )

    async def save_expert_complete(
        self,
        command: ProfileDesiredCommand,
    ) -> ProfileDesiredResult:
        if command.source_type not in {"private_expert", "market_expert"}:
            raise ProfileDesiredError(
                "EXPERT_COMMAND_SOURCE_INVALID",
                "Expert save requires an expert source type",
            )
        return await self.desired.replace(command)

    async def refresh_asset_for_rollout(
        self,
        *,
        profile: RuntimeAgentProfile,
        asset_type: str,
        asset_id: int,
        action: str,
        rollout_id: str,
        created_by_user_id: int,
    ) -> ProfileDesiredResult:
        if asset_type not in {"skill", "mcp"}:
            raise ProfileDesiredError(
                "ROLLOUT_ASSET_TYPE_INVALID",
                "Only Skill and MCP use the stable-asset refresh path",
            )
        snapshot = await self._current_snapshot(profile)
        if not snapshot:
            raise ProfileDesiredError(
                "PROFILE_DESIRED_HEAD_INVALID",
                "Rollout target has no Desired revision",
            )
        skills = self._skill_refs(snapshot)
        mcps = self._mcp_refs(snapshot)
        preserved_bindings: list[ProfileSkillBinding | ProfileMcpBinding] = []
        if action in {"revoke", "delete"}:
            # Permanent deletion removes the configured binding. Forced revoke
            # uses the same effective snapshot but restores the binding below
            # so enable can bring it back later.
            if action == "revoke":
                configured_binding = await self._configured_binding(
                    profile=profile,
                    asset_type=asset_type,
                    asset_id=asset_id,
                )
                if configured_binding is not None:
                    preserved_bindings.append(configured_binding)
            if asset_type == "skill":
                skills = await self._surviving_skill_refs_for_delete(
                    skills=skills,
                    asset_id=asset_id,
                )
            else:
                mcps = [item for item in mcps if item.mcp_id != asset_id]
        elif action == "enable":
            if asset_type == "skill" and all(
                item.skill_id != asset_id for item in skills
            ):
                skills.append(ProfileDesiredSkillRef(skill_id=asset_id))
            if asset_type == "mcp" and all(
                item.mcp_id != asset_id for item in mcps
            ):
                mcps.append(ProfileDesiredMcpRef(mcp_id=asset_id))
        elif asset_type == "skill":
            # All enabled bindings follow the stable Skill's active version.
            # ``pinned`` remains readable only until X01 converts legacy rows.
            skills = [
                ProfileDesiredSkillRef(skill_id=item.skill_id)
                if item.skill_id == asset_id
                else item
                for item in skills
            ]
        preserved_bindings.extend(
            await self._other_revoked_bindings(
                profile=profile,
                excluded_asset_type=asset_type if action == "revoke" else None,
                excluded_asset_id=asset_id if action == "revoke" else None,
            )
        )
        result = await self.desired.replace(
            ProfileDesiredCommand(
                org_id=int(profile.org_id),
                user_id=int(profile.user_id),
                runtime_workspace_id=int(profile.runtime_workspace_id),
                runtime_agent_profile_id=int(profile.id),
                definition=self._definition(profile=profile, snapshot=snapshot),
                skills=skills,
                mcps=mcps,
                knowledge_base_ids=self._knowledge_ids(snapshot),
                expert_package_version_id=snapshot.get(
                    "expert_package_version_id"
                ),
                source_type=(
                    "forced_revoke" if action == "revoke" else "asset_rollout"
                ),
                source_id=rollout_id,
                created_by_user_id=created_by_user_id,
            )
        )
        if preserved_bindings and result.created_revision:
            await self.db.flush()
            self.db.add_all(preserved_bindings)
            await self.db.flush()
        return result

    async def _surviving_skill_refs_for_delete(
        self,
        *,
        skills: list[ProfileDesiredSkillRef],
        asset_id: int,
    ) -> list[ProfileDesiredSkillRef]:
        """Coalesce rapid personal-Skill deletes into one valid snapshot."""

        candidate_ids = {
            item.skill_id for item in skills if item.skill_id != asset_id
        }
        if not candidate_ids:
            return []
        available_ids = set(
            await self.db.scalars(
                select(Skill.id).where(
                    Skill.id.in_(candidate_ids),
                    Skill.status != "deleted",
                    Skill.revoked_at.is_(None),
                )
            )
        )
        return [
            item
            for item in skills
            if item.skill_id != asset_id and item.skill_id in available_ids
        ]

    async def _other_revoked_bindings(
        self,
        *,
        profile: RuntimeAgentProfile,
        excluded_asset_type: str | None,
        excluded_asset_id: int | None,
    ) -> list[ProfileSkillBinding | ProfileMcpBinding]:
        skill_rows = list(
            await self.db.scalars(
                select(ProfileSkillBinding)
                .join(Skill, Skill.id == ProfileSkillBinding.skill_id)
                .where(
                    ProfileSkillBinding.runtime_agent_profile_id == profile.id,
                    ProfileSkillBinding.status == "enabled",
                    Skill.revoked_at.is_not(None),
                    Skill.status != "deleted",
                )
            )
        )
        mcp_rows = list(
            await self.db.scalars(
                select(ProfileMcpBinding)
                .join(Mcp, Mcp.id == ProfileMcpBinding.mcp_id)
                .where(
                    ProfileMcpBinding.runtime_agent_profile_id == profile.id,
                    ProfileMcpBinding.status == "enabled",
                    Mcp.revoked_at.is_not(None),
                    Mcp.status != "deleted",
                )
            )
        )
        preserved: list[ProfileSkillBinding | ProfileMcpBinding] = []
        for row in skill_rows:
            if excluded_asset_type == "skill" and int(row.skill_id) == excluded_asset_id:
                continue
            preserved.append(self._copy_skill_binding(row))
        for row in mcp_rows:
            if excluded_asset_type == "mcp" and int(row.mcp_id) == excluded_asset_id:
                continue
            preserved.append(self._copy_mcp_binding(row))
        return preserved

    async def _configured_binding(
        self,
        *,
        profile: RuntimeAgentProfile,
        asset_type: str,
        asset_id: int,
    ) -> ProfileSkillBinding | ProfileMcpBinding | None:
        if asset_type == "skill":
            row = await self.db.scalar(
                select(ProfileSkillBinding).where(
                    ProfileSkillBinding.runtime_agent_profile_id == profile.id,
                    ProfileSkillBinding.skill_id == asset_id,
                    ProfileSkillBinding.status == "enabled",
                )
            )
            if row is None:
                return None
            return self._copy_skill_binding(row)
        row = await self.db.scalar(
            select(ProfileMcpBinding).where(
                ProfileMcpBinding.runtime_agent_profile_id == profile.id,
                ProfileMcpBinding.mcp_id == asset_id,
                ProfileMcpBinding.status == "enabled",
            )
        )
        if row is None:
            return None
        return self._copy_mcp_binding(row)

    @staticmethod
    def _copy_skill_binding(row: ProfileSkillBinding) -> ProfileSkillBinding:
        return ProfileSkillBinding(
            org_id=int(row.org_id),
            runtime_workspace_id=int(row.runtime_workspace_id),
            runtime_agent_profile_id=int(row.runtime_agent_profile_id),
            skill_id=int(row.skill_id),
            skill_version_id=row.skill_version_id,
            version_policy=str(row.version_policy),
            status="enabled",
            binding_config=dict(row.binding_config or {}),
        )

    @staticmethod
    def _copy_mcp_binding(row: ProfileMcpBinding) -> ProfileMcpBinding:
        return ProfileMcpBinding(
            org_id=int(row.org_id),
            runtime_workspace_id=int(row.runtime_workspace_id),
            runtime_agent_profile_id=int(row.runtime_agent_profile_id),
            mcp_id=int(row.mcp_id),
            mcp_version_id=row.mcp_version_id,
            toolset_key=str(row.toolset_key),
            status="enabled",
        )

    async def refresh_mcp_identity(
        self,
        *,
        profile: RuntimeAgentProfile,
        operation_id: str,
        created_by_user_id: int,
    ) -> ProfileDesiredResult:
        """Rebuild a complete Desired snapshot using the current MCP key scheme."""

        snapshot = await self._current_snapshot(profile)
        if not snapshot:
            raise ProfileDesiredError(
                "PROFILE_DESIRED_HEAD_INVALID",
                "MCP identity migration target has no Desired revision",
            )
        return await self.desired.replace(
            ProfileDesiredCommand(
                org_id=int(profile.org_id),
                user_id=int(profile.user_id),
                runtime_workspace_id=int(profile.runtime_workspace_id),
                runtime_agent_profile_id=int(profile.id),
                definition=self._definition(profile=profile, snapshot=snapshot),
                skills=self._skill_refs(snapshot),
                mcps=self._mcp_refs(snapshot),
                knowledge_base_ids=self._knowledge_ids(snapshot),
                expert_package_version_id=snapshot.get("expert_package_version_id"),
                source_type="migration",
                source_id=operation_id,
                created_by_user_id=created_by_user_id,
            )
        )

    async def remove_expert_skill(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_agent_profile_id: int,
        skill_id: int,
        operation_id: str,
    ) -> ProfileDesiredResult:
        profile = await self.db.scalar(
            select(RuntimeAgentProfile)
            .where(
                RuntimeAgentProfile.id == runtime_agent_profile_id,
                RuntimeAgentProfile.org_id == org_id,
                RuntimeAgentProfile.user_id == user_id,
                RuntimeAgentProfile.profile_kind == "expert",
                RuntimeAgentProfile.status != "deleted",
            )
            .with_for_update()
        )
        if profile is None:
            raise ProfileDesiredError(
                "EXPERT_PROFILE_NOT_FOUND",
                "Expert Runtime Agent Profile is unavailable",
            )
        snapshot = await self._current_snapshot(profile)
        if not snapshot:
            raise ProfileDesiredError(
                "PROFILE_DESIRED_HEAD_INVALID",
                "Expert Profile has no Desired revision",
            )
        return await self.desired.replace(
            ProfileDesiredCommand(
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=int(profile.runtime_workspace_id),
                runtime_agent_profile_id=int(profile.id),
                definition=self._definition(profile=profile, snapshot=snapshot),
                skills=[
                    item
                    for item in self._skill_refs(snapshot)
                    if item.skill_id != skill_id
                ],
                mcps=self._mcp_refs(snapshot),
                knowledge_base_ids=self._knowledge_ids(snapshot),
                expert_package_version_id=snapshot.get("expert_package_version_id"),
                source_type="private_expert",
                source_id=operation_id,
                created_by_user_id=user_id,
            )
        )

    async def _current_snapshot(self, profile: RuntimeAgentProfile) -> dict:
        desired_revision = int(profile.desired_revision or 0)
        if desired_revision == 0:
            if profile.desired_digest is not None:
                raise ProfileDesiredError(
                    "PROFILE_DESIRED_HEAD_INVALID",
                    "Empty Profile Desired head has a digest",
                )
            return {}
        revision = await self.db.scalar(
            select(RuntimeAgentProfileRevision).where(
                RuntimeAgentProfileRevision.runtime_agent_profile_id == profile.id,
                RuntimeAgentProfileRevision.revision == desired_revision,
            )
        )
        if revision is None or str(revision.profile_digest) != profile.desired_digest:
            raise ProfileDesiredError(
                "PROFILE_DESIRED_HEAD_INVALID",
                "Profile Desired head does not match its immutable revision",
            )
        return dict(revision.snapshot or {})

    @staticmethod
    def _definition(
        *,
        profile: RuntimeAgentProfile,
        snapshot: dict,
    ) -> ProfileDesiredDefinition:
        profile_snapshot = dict(snapshot.get("profile") or {})
        knowledge_snapshot = dict(snapshot.get("knowledge") or {})
        capability_config = dict(profile.capability_config or {})
        return ProfileDesiredDefinition(
            display_name=str(profile_snapshot.get("display_name") or profile.display_name),
            description=profile_snapshot.get("description", profile.description),
            avatar_url=profile_snapshot.get("avatar_url", profile.avatar_url),
            soul_content=str(
                profile_snapshot.get("soul_content", profile.soul_content or "")
            ),
            system_prompt=profile_snapshot.get(
                "system_prompt", capability_config.get("system_prompt")
            ),
            enterprise_knowledge_enabled=bool(
                knowledge_snapshot.get(
                    "enterprise_knowledge_enabled",
                    capability_config.get("enterprise_knowledge_enabled", False),
                )
            ),
            plugin_ids=list(
                profile_snapshot.get("plugin_ids")
                or capability_config.get("plugin_ids", [])
            ),
        )

    @staticmethod
    def _skill_refs(snapshot: dict) -> list[ProfileDesiredSkillRef]:
        return [
            ProfileDesiredSkillRef(
                skill_id=int(item["skill_id"]),
                version_policy=str(item["version_policy"]),
                skill_version_id=(
                    int(item["skill_version_id"])
                    if item["version_policy"] == "pinned"
                    else None
                ),
            )
            for item in snapshot.get("skills", [])
        ]

    @staticmethod
    def _mcp_refs(snapshot: dict) -> list[ProfileDesiredMcpRef]:
        return [
            ProfileDesiredMcpRef(mcp_id=int(item["mcp_id"]))
            for item in snapshot.get("mcps", [])
        ]

    @staticmethod
    def _knowledge_ids(snapshot: dict) -> list[int]:
        knowledge = dict(snapshot.get("knowledge") or {})
        return [int(item) for item in knowledge.get("personal_knowledge_base_ids", [])]

    @staticmethod
    def _mutate_skills(
        current: list[ProfileDesiredSkillRef],
        *,
        action: str,
        asset_id: int,
    ) -> list[ProfileDesiredSkillRef]:
        by_id = {item.skill_id: item for item in current}
        if action == "install":
            by_id[asset_id] = ProfileDesiredSkillRef(skill_id=asset_id)
        else:
            by_id.pop(asset_id, None)
        return [by_id[key] for key in sorted(by_id)]

    @staticmethod
    def _mutate_mcps(
        current: list[ProfileDesiredMcpRef],
        *,
        action: str,
        asset_id: int,
    ) -> list[ProfileDesiredMcpRef]:
        by_id = {item.mcp_id: item for item in current}
        if action == "install":
            by_id[asset_id] = ProfileDesiredMcpRef(mcp_id=asset_id)
        else:
            by_id.pop(asset_id, None)
        return [by_id[key] for key in sorted(by_id)]

    @staticmethod
    def _mutate_ids(current: list[int], *, action: str, asset_id: int) -> list[int]:
        values = set(current)
        if action == "install":
            values.add(asset_id)
        else:
            values.discard(asset_id)
        return sorted(values)
