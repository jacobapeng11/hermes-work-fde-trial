from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ExpertPackage,
    ExpertPackageVersion,
    ProfileKnowledgeBinding,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    RuntimeSession,
    TemplateReview,
)
from app.repositories.scheduled_task import ScheduledTaskRepository
from app.schemas.profile_desired import (
    ProfileDesiredCommand,
    ProfileDesiredDefinition,
    ProfileDesiredMcpRef,
    ProfileDesiredResult,
    ProfileDesiredSkillRef,
)
from app.services.profile_desired import ProfileDesiredService
from app.services.runtime_agent_profiles import (
    RuntimeAgentProfileError,
    SqlAlchemyRuntimeAgentProfileStore,
    normalize_profile_display_name,
)
from app.services.runtime_workspace import Principal


class ExpertAuthoringError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExpertAuthoringSaveResult:
    profile: RuntimeAgentProfile
    package: ExpertPackage
    version: ExpertPackageVersion
    desired: ProfileDesiredResult


class ExpertVersionAssembler:
    """Pure assembler for one immutable authoring/review version."""

    @staticmethod
    def assemble(
        *,
        package: ExpertPackage,
        profile: RuntimeAgentProfile,
        capabilities: dict[str, Any],
        version_number: int,
        status: str,
        change_summary: str | None,
    ) -> ExpertPackageVersion:
        basic = {
            "name": str(profile.display_name),
            "description": profile.description,
            "avatar_url": profile.avatar_url,
        }
        skills = [dict(item) for item in capabilities.get("skills", [])]
        mcps = [dict(item) for item in capabilities.get("mcps", [])]
        knowledge = [dict(item) for item in capabilities.get("knowledge_bases", [])]
        plugin_ids = sorted(
            {str(item) for item in capabilities.get("plugin_ids", []) if str(item)}
        )
        snapshot_capabilities = {
            "skills": skills,
            "mcps": mcps,
            "knowledge_bases": knowledge,
            "plugin_ids": plugin_ids,
            "enterprise_knowledge_enabled": bool(
                capabilities.get("enterprise_knowledge_enabled", False)
            ),
        }
        return ExpertPackageVersion(
            org_id=int(package.org_id),
            expert_package_id=int(package.id),
            version=version_number,
            manifest={
                "schema_version": "expert.profile/v2",
                "release_artifact_schema": "v1",
                "asset_identity_schema": "stable-catalog/v1",
                "basic_info": basic,
                "capabilities": snapshot_capabilities,
            },
            basic_info_snapshot=basic,
            soul_snapshot=str(profile.soul_content or ""),
            prompt_snapshot=None,
            skill_refs=[
                {
                    "source": str(item.get("source") or "public"),
                    "asset_id": int(item["asset_id"]),
                    **(
                        {"version_id": int(item["version_id"])}
                        if item.get("version_id") is not None
                        else {}
                    ),
                }
                for item in skills
            ],
            mcp_refs=[
                {
                    "source": str(item.get("source") or "public"),
                    "asset_id": int(item["asset_id"]),
                }
                for item in mcps
            ],
            knowledge_refs=[
                {
                    "source": str(item.get("source") or "public"),
                    "asset_id": int(item["asset_id"]),
                }
                for item in knowledge
            ],
            plugin_refs=[{"plugin_id": item} for item in plugin_ids],
            model_preferences=None,
            change_summary=change_summary,
            status=status,
        )


class ExpertAuthoringService:
    """Own self-created expert save/copy/delete database transactions."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.profiles = SqlAlchemyRuntimeAgentProfileStore(db)

    async def save_existing(
        self,
        *,
        principal: Principal,
        profile: RuntimeAgentProfile,
        package: ExpertPackage,
        capabilities: dict[str, Any],
        desired_command: ProfileDesiredCommand,
        version_status: str,
        change_summary: str | None,
    ) -> ExpertAuthoringSaveResult:
        locked = await self._locked_self_created(
            principal=principal,
            runtime_workspace_id=int(profile.runtime_workspace_id),
            runtime_agent_profile_id=int(profile.id),
        )
        if int(locked.expert_package_id or 0) != int(package.id):
            raise ExpertAuthoringError("EXPERT_PACKAGE_MISMATCH")
        next_version = int(
            await self.db.scalar(
                select(func.max(ExpertPackageVersion.version)).where(
                    ExpertPackageVersion.expert_package_id == package.id
                )
            )
            or 0
        ) + 1
        version = ExpertVersionAssembler.assemble(
            package=package,
            profile=locked,
            capabilities=capabilities,
            version_number=next_version,
            status=version_status,
            change_summary=change_summary,
        )
        self.db.add(version)
        await self.db.flush()
        desired = await ProfileDesiredService(self.db).replace(
            desired_command.model_copy(
                update={"expert_package_version_id": int(version.id)}
            )
        )
        return ExpertAuthoringSaveResult(
            profile=locked,
            package=package,
            version=version,
            desired=desired,
        )

    async def copy(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
    ) -> ExpertAuthoringSaveResult:
        source = await self._locked_self_created(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
        source_package = await self.db.get(ExpertPackage, source.expert_package_id)
        if source_package is None:
            raise ExpertAuthoringError("EXPERT_PACKAGE_NOT_FOUND")
        capabilities = self._normalized_capabilities(source.capability_config or {})
        last_conflict: RuntimeAgentProfileError | None = None
        for index in range(1, 101):
            name = self.copy_name(str(source.display_name), index)
            try:
                copied_profile = await self.profiles.create_expert_row(
                    principal=principal,
                    runtime_workspace_id=runtime_workspace_id,
                    display_name=name,
                    description=source.description,
                    avatar_url=source.avatar_url,
                    soul_content=str(source.soul_content or ""),
                    ownership_type="self_created",
                    inherited_from_profile_id=int(source.id),
                    capability_config={
                        **capabilities,
                        "requested_share_scope": {},
                    },
                )
            except RuntimeAgentProfileError as exc:
                last_conflict = exc
                continue
            package = ExpertPackage(
                org_id=principal.org_id,
                owner_user_id=principal.user_id,
                origin_runtime_workspace_id=runtime_workspace_id,
                slug=f"expert-{uuid4().hex}",
                name=name,
                description=source.description,
                avatar_url=source.avatar_url,
                status="active",
                visibility="private",
                active_name_key=normalize_profile_display_name(name),
            )
            self.db.add(package)
            await self.db.flush()
            copied_profile.expert_package_id = int(package.id)
            version = ExpertVersionAssembler.assemble(
                package=package,
                profile=copied_profile,
                capabilities=capabilities,
                version_number=1,
                status="private",
                change_summary=None,
            )
            self.db.add(version)
            await self.db.flush()
            desired = await ProfileDesiredService(self.db).replace(
                self._desired_command(
                    profile=copied_profile,
                    capabilities=capabilities,
                    version_id=int(version.id),
                    created_by_user_id=principal.user_id,
                    source_id=f"expert-copy-{int(source.id)}-{int(version.id)}",
                )
            )
            return ExpertAuthoringSaveResult(
                profile=copied_profile,
                package=package,
                version=version,
                desired=desired,
            )
        raise ExpertAuthoringError("EXPERT_NAME_CONFLICT") from last_conflict

    async def delete(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
    ) -> RuntimeAgentProfile:
        profile = await self._locked_self_created(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            require_active=False,
        )
        package = await self.db.get(ExpertPackage, profile.expert_package_id)
        # Explicitly clear asset bindings: profile is soft-deleted (status='deleted'),
        # so the ON DELETE CASCADE on binding FKs never fires.
        profile_id = int(profile.id)
        for model in (ProfileSkillBinding, ProfileMcpBinding, ProfileKnowledgeBinding):
            await self.db.execute(
                delete(model).where(model.runtime_agent_profile_id == profile_id)
            )
        profile.status = "deleted"
        profile.active_display_name_key = None
        if package is not None:
            package.status = "deleted"
            package.visibility = "private"
            package.active_name_key = None
            pending_reviews = list(
                await self.db.scalars(
                    select(TemplateReview)
                    .where(
                        TemplateReview.org_id == principal.org_id,
                        TemplateReview.expert_package_id == package.id,
                        TemplateReview.status == "pending",
                    )
                    .with_for_update()
                )
            )
            for review in pending_reviews:
                review.status = "superseded"
                review.reviewed_at = datetime.now(timezone.utc)
                snapshot = dict(review.review_snapshot or {})
                snapshot["superseded_reason"] = "owner_deleted"
                snapshot["superseded_at"] = review.reviewed_at.isoformat()
                review.review_snapshot = snapshot
                if review.expert_package_version_id is not None:
                    version = await self.db.get(
                        ExpertPackageVersion,
                        review.expert_package_version_id,
                    )
                    if version is not None and version.status == "pending_review":
                        version.status = "superseded"
        sessions = list(
            await self.db.scalars(
                select(RuntimeSession)
                .where(
                    RuntimeSession.org_id == principal.org_id,
                    RuntimeSession.user_id == principal.user_id,
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
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                session_id=int(session.id),
            )
            session.status = "expert_deleted"
        return profile

    async def _locked_self_created(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        require_active: bool = True,
    ) -> RuntimeAgentProfile:
        statement = select(RuntimeAgentProfile).where(
                RuntimeAgentProfile.id == runtime_agent_profile_id,
                RuntimeAgentProfile.org_id == principal.org_id,
                RuntimeAgentProfile.user_id == principal.user_id,
                RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
                RuntimeAgentProfile.profile_kind == "expert",
                RuntimeAgentProfile.ownership_type == "self_created",
                RuntimeAgentProfile.status != "deleted",
            )
        if require_active:
            statement = statement.where(RuntimeAgentProfile.status == "active")
        profile = await self.db.scalar(statement.with_for_update())
        if profile is None:
            raise ExpertAuthoringError("EXPERT_NOT_EDITABLE")
        return profile

    @staticmethod
    def copy_name(source_name: str, index: int) -> str:
        if index < 1:
            raise ValueError("copy index must be positive")
        suffix = "-副本" if index == 1 else f"-副本{index}"
        return f"{source_name[: max(1, 200 - len(suffix))]}{suffix}"

    @staticmethod
    def _normalized_capabilities(value: dict[str, Any]) -> dict[str, Any]:
        def refs(key: str, legacy: str) -> list[dict[str, Any]]:
            raw = value.get(key)
            if isinstance(raw, list) and raw:
                return [dict(item) for item in raw if isinstance(item, dict)]
            return [
                {"source": "public", "asset_id": int(item)}
                for item in value.get(legacy, [])
            ]

        return {
            "skills": refs("skills", "skill_ids"),
            "mcps": refs("mcps", "mcp_ids"),
            "knowledge_bases": refs("knowledge_bases", "knowledge_base_ids"),
            "plugin_ids": list(value.get("plugin_ids") or []),
            "enterprise_knowledge_enabled": bool(
                value.get("enterprise_knowledge_enabled", False)
            ),
            "system_prompt": value.get("system_prompt"),
        }

    @staticmethod
    def _desired_command(
        *,
        profile: RuntimeAgentProfile,
        capabilities: dict[str, Any],
        version_id: int,
        created_by_user_id: int,
        source_id: str,
    ) -> ProfileDesiredCommand:
        return ProfileDesiredCommand(
            org_id=int(profile.org_id),
            user_id=int(profile.user_id),
            runtime_workspace_id=int(profile.runtime_workspace_id),
            runtime_agent_profile_id=int(profile.id),
            definition=ProfileDesiredDefinition(
                display_name=str(profile.display_name),
                description=profile.description,
                avatar_url=profile.avatar_url,
                soul_content=str(profile.soul_content or ""),
                system_prompt=capabilities.get("system_prompt"),
                enterprise_knowledge_enabled=bool(
                    capabilities.get("enterprise_knowledge_enabled", False)
                ),
                plugin_ids=list(capabilities.get("plugin_ids") or []),
            ),
            skills=[
                ProfileDesiredSkillRef(skill_id=int(item["asset_id"]))
                for item in capabilities.get("skills", [])
            ],
            mcps=[
                ProfileDesiredMcpRef(mcp_id=int(item["asset_id"]))
                for item in capabilities.get("mcps", [])
            ],
            knowledge_base_ids=[
                int(item["asset_id"])
                for item in capabilities.get("knowledge_bases", [])
            ],
            expert_package_version_id=version_id,
            source_type="private_expert",
            source_id=source_id,
            created_by_user_id=created_by_user_id,
        )


__all__ = [
    "ExpertAuthoringError",
    "ExpertAuthoringSaveResult",
    "ExpertAuthoringService",
    "ExpertVersionAssembler",
]
