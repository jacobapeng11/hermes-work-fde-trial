from __future__ import annotations

from datetime import datetime
import hashlib
import json

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ExpertPackageVersion,
    Mcp,
    McpVersion,
    ProfileConvergenceJob,
    ProfileKnowledgeBinding,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    Skill,
    SkillVersion,
)
from app.schemas.profile_desired import (
    ProfileDesiredCommand,
    ProfileDesiredResult,
    ProfileDesiredSkillRef,
)
from app.schemas.mcp_connection import validate_mcp_connection_target
from app.services.runtime_mcp_registry import (
    RuntimeMcpRegistryDesiredService,
    RuntimeMcpRegistryError,
)
from app.services.mcp_identity import runtime_mcp_server_key
from app.services.plaza.public_catalog.package_parser import normalize_skill_name


class ProfileDesiredError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class ProfileDesiredService:
    """The only MySQL transaction boundary for one complete Profile Desired."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def replace(self, command: ProfileDesiredCommand) -> ProfileDesiredResult:
        profile = await self.db.scalar(
            select(RuntimeAgentProfile)
            .where(
                RuntimeAgentProfile.id == command.runtime_agent_profile_id,
                RuntimeAgentProfile.org_id == command.org_id,
                RuntimeAgentProfile.user_id == command.user_id,
                RuntimeAgentProfile.runtime_workspace_id
                == command.runtime_workspace_id,
                RuntimeAgentProfile.status != "deleted",
            )
            .with_for_update()
        )
        if profile is None:
            raise ProfileDesiredError(
                "PROFILE_NOT_FOUND",
                "Runtime Agent Profile is unavailable",
            )
        self._validate_profile_command(profile=profile, command=command)
        desired_revision = int(profile.desired_revision or 0)
        applied_revision = int(profile.applied_revision or 0)
        if applied_revision < 0 or desired_revision < applied_revision:
            raise ProfileDesiredError(
                "PROFILE_REVISION_INVALID",
                "Profile Desired/Applied revision order is invalid",
            )
        await self._validate_expert_version(profile=profile, command=command)

        existing_skill_ids = set(
            await self.db.scalars(
                select(ProfileSkillBinding.skill_id).where(
                    ProfileSkillBinding.runtime_agent_profile_id == profile.id,
                    ProfileSkillBinding.status == "enabled",
                )
            )
        )
        existing_mcp_ids = set(
            await self.db.scalars(
                select(ProfileMcpBinding.mcp_id).where(
                    ProfileMcpBinding.runtime_agent_profile_id == profile.id,
                    ProfileMcpBinding.status == "enabled",
                )
            )
        )
        skills = await self._resolve_skills(
            command=command,
            profile=profile,
            existing_skill_ids={int(item) for item in existing_skill_ids},
        )
        mcps = await self._resolve_mcps(
            command=command,
            existing_mcp_ids={int(item) for item in existing_mcp_ids},
        )
        knowledge = {
            "enterprise_knowledge_enabled": (
                command.definition.enterprise_knowledge_enabled
            ),
            "personal_knowledge_base_ids": command.knowledge_base_ids,
        }
        profile_config = {
            "display_name": command.definition.display_name,
            "description": command.definition.description,
            "avatar_url": command.definition.avatar_url,
            "soul_content": command.definition.soul_content,
            "system_prompt": command.definition.system_prompt,
            "plugin_ids": command.definition.plugin_ids,
        }
        snapshot = {
            "schema_version": 1,
            "profile": profile_config,
            "skills": skills,
            "mcps": mcps,
            "knowledge": knowledge,
            "expert_package_version_id": command.expert_package_version_id,
        }
        profile_digest = _digest(snapshot)
        skill_digest = _digest(skills)
        mcp_digest = _digest(mcps)
        knowledge_digest = _digest(knowledge)

        current_revision = desired_revision
        if profile.desired_digest == profile_digest and current_revision > 0:
            revision = await self.db.scalar(
                select(RuntimeAgentProfileRevision).where(
                    RuntimeAgentProfileRevision.runtime_agent_profile_id == profile.id,
                    RuntimeAgentProfileRevision.revision == current_revision,
                )
            )
            if revision is None or dict(revision.snapshot or {}) != snapshot:
                raise ProfileDesiredError(
                    "PROFILE_DESIRED_HEAD_INVALID",
                    "Profile Desired head does not match its immutable revision",
                )
            job = await self.db.scalar(
                select(ProfileConvergenceJob).where(
                    ProfileConvergenceJob.runtime_agent_profile_id == profile.id
                )
            )
            if applied_revision < current_revision:
                job = await self._ensure_convergence_job(
                    profile=profile,
                    target_revision=current_revision,
                )
            await self._reconcile_mcp_registry(command)
            return self._result(
                profile=profile,
                revision=revision,
                job=job,
                created_revision=False,
            )

        next_revision = current_revision + 1
        revision = RuntimeAgentProfileRevision(
            org_id=command.org_id,
            user_id=command.user_id,
            runtime_workspace_id=command.runtime_workspace_id,
            runtime_agent_profile_id=int(profile.id),
            revision=next_revision,
            profile_digest=profile_digest,
            skill_digest=skill_digest,
            mcp_digest=mcp_digest,
            knowledge_digest=knowledge_digest,
            snapshot=snapshot,
            source_type=command.source_type,
            source_id=command.source_id,
            expert_package_version_id=command.expert_package_version_id,
            created_by_user_id=command.created_by_user_id,
        )
        self.db.add(revision)
        await self._replace_bindings(
            profile=profile,
            command=command,
            skills=skills,
            mcps=mcps,
        )
        await self.db.flush()
        await self._reconcile_mcp_registry(command)
        profile.display_name = command.definition.display_name
        profile.description = command.definition.description
        profile.avatar_url = command.definition.avatar_url
        profile.soul_content = command.definition.soul_content
        # Merge the new legacy fields with any source-aware fields that were already
        # present in capability_config. The expert authoring path records
        # `skills`/`mcps`/`knowledge_bases` with explicit source attribution
        # (public/personal/default_installed); that attribution must survive the
        # ProfileDesired rewrite so release preflight and runtime projection can
        # distinguish personal assets from public ones.
        existing_config = dict(profile.capability_config or {})
        existing_config.update(
            {
                "schema_version": 1,
                "system_prompt": command.definition.system_prompt,
                "plugin_ids": command.definition.plugin_ids,
                "enterprise_knowledge_enabled": (
                    command.definition.enterprise_knowledge_enabled
                ),
                "skill_ids": [item["skill_id"] for item in skills],
                "mcp_ids": [item["mcp_id"] for item in mcps],
                "knowledge_base_ids": command.knowledge_base_ids,
            }
        )
        profile.capability_config = existing_config
        profile.desired_revision = next_revision
        profile.desired_digest = profile_digest
        job = await self._ensure_convergence_job(
            profile=profile,
            target_revision=next_revision,
        )
        await self.db.flush()
        return self._result(
            profile=profile,
            revision=revision,
            job=job,
            created_revision=True,
        )

    async def _resolve_skills(
        self,
        *,
        command: ProfileDesiredCommand,
        profile: RuntimeAgentProfile,
        existing_skill_ids: set[int],
    ) -> list[dict[str, object]]:
        resolved: list[dict[str, object]] = []
        names: set[str] = set()
        for reference in command.skills:
            skill = await self.db.scalar(
                select(Skill).where(
                    Skill.id == reference.skill_id,
                    Skill.org_id == command.org_id,
                    Skill.status != "deleted",
                )
            )
            if skill is None or skill.revoked_at is not None:
                raise ProfileDesiredError("SKILL_UNAVAILABLE", "Skill is unavailable")
            if (
                str(skill.asset_scope) == "public"
                and not bool(skill.market_visible)
                and int(skill.id) not in existing_skill_ids
                and command.source_type != "migration"
            ):
                raise ProfileDesiredError(
                    "SKILL_NOT_MARKET_VISIBLE",
                    "Unpublished public Skill cannot be newly installed",
                )
            normalized_name = normalize_skill_name(str(skill.name))
            if normalized_name in names:
                raise ProfileDesiredError(
                    "SKILL_NAME_CONFLICT",
                    "Profile cannot contain Skills with the same name",
                )
            names.add(normalized_name)
            if (
                skill.archived_at is not None
                and int(skill.id) not in existing_skill_ids
                and command.source_type != "migration"
            ):
                raise ProfileDesiredError(
                    "SKILL_ARCHIVED",
                    "Archived Skill cannot be added to a new Profile",
                )
            asset_scope = str(skill.asset_scope)
            if asset_scope == "personal" and int(skill.owner_user_id) != command.user_id:
                if not await self._market_profile_declares_personal_skill(
                    profile=profile,
                    command=command,
                    skill_id=int(skill.id),
                ):
                    raise ProfileDesiredError(
                        "PERSONAL_SKILL_FORBIDDEN",
                        "Personal Skill is not authorized for this managed Profile",
                    )
            if asset_scope not in {"public", "personal"}:
                raise ProfileDesiredError(
                    "SKILL_SCOPE_INVALID",
                    "Skill has an unsupported asset scope",
                )
            version_id = self._skill_version_id(skill=skill, reference=reference)
            version = await self.db.scalar(
                select(SkillVersion).where(
                    SkillVersion.id == version_id,
                    SkillVersion.skill_id == skill.id,
                    SkillVersion.org_id == command.org_id,
                    SkillVersion.status != "deleted",
                )
            )
            if version is None or not version.package_uri or not version.package_sha256:
                raise ProfileDesiredError(
                    "SKILL_VERSION_UNAVAILABLE",
                    "Skill version is incomplete or unavailable",
                )
            resolved.append(
                {
                    "skill_id": int(skill.id),
                    "skill_version_id": int(version.id),
                    "version_policy": reference.version_policy,
                    "asset_scope": str(skill.asset_scope),
                    "package_uri": str(version.package_uri),
                    "package_sha256": str(version.package_sha256).lower(),
                }
            )
        return resolved

    async def _market_profile_declares_personal_skill(
        self,
        *,
        profile: RuntimeAgentProfile,
        command: ProfileDesiredCommand,
        skill_id: int,
    ) -> bool:
        if (
            str(profile.ownership_type) != "market_installed"
            or command.expert_package_version_id is None
            or command.source_type not in {"market_expert", "asset_rollout", "migration"}
        ):
            return False
        version = await self.db.scalar(
            select(ExpertPackageVersion).where(
                ExpertPackageVersion.id == command.expert_package_version_id,
                ExpertPackageVersion.expert_package_id == profile.expert_package_id,
                ExpertPackageVersion.org_id == command.org_id,
                ExpertPackageVersion.status.in_(("approved", "published")),
            )
        )
        if version is None:
            return False
        return any(
            isinstance(item, dict)
            and str(item.get("source")) == "personal"
            and int(item.get("asset_id") or 0) == skill_id
            for item in (version.skill_refs or [])
        )

    @staticmethod
    def _skill_version_id(
        *,
        skill: Skill,
        reference: ProfileDesiredSkillRef,
    ) -> int:
        version_id = (
            reference.skill_version_id
            if reference.version_policy == "pinned"
            else skill.current_version_id
        )
        if version_id is None:
            raise ProfileDesiredError(
                "SKILL_VERSION_UNAVAILABLE",
                "Skill has no current version",
            )
        return int(version_id)

    async def _resolve_mcps(
        self,
        *,
        command: ProfileDesiredCommand,
        existing_mcp_ids: set[int],
    ) -> list[dict[str, object]]:
        resolved: list[dict[str, object]] = []
        for reference in command.mcps:
            mcp = await self.db.scalar(
                select(Mcp).where(
                    Mcp.id == reference.mcp_id,
                    Mcp.org_id == command.org_id,
                    Mcp.status != "deleted",
                )
            )
            if mcp is None or mcp.revoked_at is not None:
                raise ProfileDesiredError("MCP_UNAVAILABLE", "MCP is unavailable")
            if (
                mcp.archived_at is not None
                and int(mcp.id) not in existing_mcp_ids
                and command.source_type != "migration"
            ):
                raise ProfileDesiredError(
                    "MCP_ARCHIVED",
                    "Archived MCP cannot be added to a new Profile",
                )
            if str(mcp.visibility) == "private" and int(mcp.owner_user_id) != command.user_id:
                raise ProfileDesiredError("MCP_FORBIDDEN", "MCP belongs to another user")
            if mcp.current_version_id is None:
                raise ProfileDesiredError("MCP_VERSION_UNAVAILABLE", "MCP has no version")
            version = await self.db.scalar(
                select(McpVersion).where(
                    McpVersion.id == mcp.current_version_id,
                    McpVersion.mcp_id == mcp.id,
                    McpVersion.org_id == command.org_id,
                    McpVersion.status != "deleted",
                )
            )
            if version is None:
                raise ProfileDesiredError(
                    "MCP_VERSION_UNAVAILABLE",
                    "MCP version is unavailable",
                )
            try:
                validate_mcp_connection_target(
                    str(version.transport),
                    version.connection_config,
                )
            except ValueError as exc:
                raise ProfileDesiredError(
                    "MCP_VERSION_UNAVAILABLE",
                    "MCP version connection target is unavailable",
                ) from exc
            resolved.append(
                {
                    "mcp_id": int(mcp.id),
                    "mcp_version_id": int(version.id),
                    "server_slug": (
                        runtime_mcp_server_key(int(mcp.id))
                        if settings.mcp_runtime_identity_enabled(command.user_id)
                        else str(mcp.slug)
                    ),
                    "toolset_key": "mcp-" + (
                        runtime_mcp_server_key(int(mcp.id))
                        if settings.mcp_runtime_identity_enabled(command.user_id)
                        else str(mcp.slug)
                    ),
                    "transport": str(version.transport),
                    "connection_config": dict(version.connection_config or {}),
                    "credential_refs": dict(version.secret_refs or {}),
                    "config_digest": str(version.config_digest),
                }
            )
        return resolved

    async def _reconcile_mcp_registry(
        self,
        command: ProfileDesiredCommand,
    ) -> None:
        try:
            await RuntimeMcpRegistryDesiredService(
                self.db
            ).reconcile_workspace(
                org_id=command.org_id,
                user_id=command.user_id,
                runtime_workspace_id=command.runtime_workspace_id,
                source_type=command.source_type,
                source_id=command.source_id,
            )
        except RuntimeMcpRegistryError as exc:
            raise ProfileDesiredError(exc.code, str(exc)) from exc

    @staticmethod
    def _validate_profile_command(
        *,
        profile: RuntimeAgentProfile,
        command: ProfileDesiredCommand,
    ) -> None:
        profile_kind = str(profile.profile_kind)
        if profile_kind not in {"default", "expert"}:
            raise ProfileDesiredError(
                "PROFILE_KIND_INVALID",
                "Runtime Agent Profile kind is invalid",
            )
        if profile_kind == "default":
            if command.source_type in {"private_expert", "market_expert"}:
                raise ProfileDesiredError(
                    "PROFILE_COMMAND_SCOPE_INVALID",
                    "Expert configuration cannot target the default Profile",
                )
            if command.expert_package_version_id is not None:
                raise ProfileDesiredError(
                    "PROFILE_COMMAND_SCOPE_INVALID",
                    "Default Profile cannot bind an expert version",
                )
        elif command.source_type == "default_user":
            raise ProfileDesiredError(
                "PROFILE_COMMAND_SCOPE_INVALID",
                "Default asset command cannot target an expert Profile",
            )
        ownership_type = str(profile.ownership_type)
        if ownership_type == "market_installed" and command.source_type not in {
            "market_expert",
            "asset_rollout",
            "forced_revoke",
            "migration",
        }:
            raise ProfileDesiredError(
                "PROFILE_MANAGED_READ_ONLY",
                "Market-managed Profile configuration is read-only",
            )
        if ownership_type == "self_created" and command.source_type == "market_expert":
            raise ProfileDesiredError(
                "PROFILE_COMMAND_SCOPE_INVALID",
                "Market expert command cannot target a self-created Profile",
            )

    async def _validate_expert_version(
        self,
        *,
        profile: RuntimeAgentProfile,
        command: ProfileDesiredCommand,
    ) -> None:
        version_id = command.expert_package_version_id
        if version_id is None:
            return
        version = await self.db.scalar(
            select(ExpertPackageVersion).where(
                ExpertPackageVersion.id == version_id,
                ExpertPackageVersion.org_id == command.org_id,
                ExpertPackageVersion.expert_package_id == profile.expert_package_id,
                ExpertPackageVersion.status.notin_(("deleted", "revoked")),
            )
        )
        if version is None:
            raise ProfileDesiredError(
                "EXPERT_VERSION_UNAVAILABLE",
                "Expert version does not belong to this Profile",
            )
        if command.source_type == "market_expert" and str(version.status) not in {
            "approved",
            "published",
        }:
            raise ProfileDesiredError(
                "EXPERT_VERSION_NOT_APPROVED",
                "Market expert version is not approved",
            )

    async def _replace_bindings(
        self,
        *,
        profile: RuntimeAgentProfile,
        command: ProfileDesiredCommand,
        skills: list[dict[str, object]],
        mcps: list[dict[str, object]],
    ) -> None:
        profile_id = int(profile.id)
        for model in (ProfileSkillBinding, ProfileMcpBinding, ProfileKnowledgeBinding):
            await self.db.execute(
                delete(model).where(model.runtime_agent_profile_id == profile_id)
            )
        for item in skills:
            self.db.add(
                ProfileSkillBinding(
                    org_id=command.org_id,
                    runtime_workspace_id=command.runtime_workspace_id,
                    runtime_agent_profile_id=profile_id,
                    skill_id=int(item["skill_id"]),
                    skill_version_id=int(item["skill_version_id"]),
                    version_policy=str(item["version_policy"]),
                    status="enabled",
                    binding_config=None,
                )
            )
        for item in mcps:
            self.db.add(
                ProfileMcpBinding(
                    org_id=command.org_id,
                    runtime_workspace_id=command.runtime_workspace_id,
                    runtime_agent_profile_id=profile_id,
                    mcp_id=int(item["mcp_id"]),
                    mcp_version_id=int(item["mcp_version_id"]),
                    toolset_key=str(item["toolset_key"]),
                    status="enabled",
                )
            )
        for knowledge_base_id in command.knowledge_base_ids:
            self.db.add(
                ProfileKnowledgeBinding(
                    org_id=command.org_id,
                    runtime_workspace_id=command.runtime_workspace_id,
                    runtime_agent_profile_id=profile_id,
                    knowledge_base_id=knowledge_base_id,
                    status="enabled",
                )
            )

    async def _ensure_convergence_job(
        self,
        *,
        profile: RuntimeAgentProfile,
        target_revision: int,
    ) -> ProfileConvergenceJob:
        job = await self.db.scalar(
            select(ProfileConvergenceJob)
            .where(ProfileConvergenceJob.runtime_agent_profile_id == profile.id)
            .with_for_update()
        )
        now = datetime.utcnow()
        if job is None:
            job = ProfileConvergenceJob(
                org_id=int(profile.org_id),
                runtime_workspace_id=int(profile.runtime_workspace_id),
                runtime_agent_profile_id=int(profile.id),
                target_revision=target_revision,
                status="waiting",
                stage="resolve_desired",
                available_at=now,
            )
            self.db.add(job)
            return job
        job.target_revision = target_revision
        if job.status != "updating" or (
            job.lease_expires_at is not None and job.lease_expires_at <= now
        ):
            job.status = "waiting"
            job.stage = "resolve_desired"
            job.available_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error_code = None
            job.last_error_message = None
        return job

    @staticmethod
    def _result(
        *,
        profile: RuntimeAgentProfile,
        revision: RuntimeAgentProfileRevision,
        job: ProfileConvergenceJob | None,
        created_revision: bool,
    ) -> ProfileDesiredResult:
        applied_revision = int(profile.applied_revision or 0)
        if applied_revision == int(revision.revision):
            status = "applied"
        elif job is not None and job.status == "updating":
            status = "updating"
        else:
            status = "waiting"
        return ProfileDesiredResult(
            runtime_agent_profile_id=int(profile.id),
            desired_revision=int(revision.revision),
            applied_revision=applied_revision,
            profile_digest=str(revision.profile_digest),
            skill_digest=str(revision.skill_digest),
            mcp_digest=str(revision.mcp_digest),
            knowledge_digest=str(revision.knowledge_digest),
            convergence_status=status,
            created_revision=created_revision,
        )
