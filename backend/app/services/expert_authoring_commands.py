from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ExpertPackage,
    ExpertPackageVersion,
    ExpertProfileOperation,
    KnowledgeBase,
    Mcp,
    Permission,
    PlazaTemplate,
    ProfileMcpBinding,
    ProfileKnowledgeBinding,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    Skill,
    SkillVersion,
    SysDept,
    SysUser,
    TemplateReview,
    TemplateVersion,
    User,
)
from app.schemas.experts import (
    CreateExpertRequest,
    ExpertCapabilitySelection,
    ExpertShareScope,
    RuntimeAgentProfileSummary,
    UpdateExpertConfigurationRequest,
    UpdateExpertRequest,
)
from app.schemas.profile_desired import (
    ProfileDesiredCommand,
    ProfileDesiredDefinition,
    ProfileDesiredMcpRef,
    ProfileDesiredSkillRef,
)
from app.services.user_directory import COMPANY_ROOT_DEPARTMENT_IDS
from app.services.avatar_assets import AvatarAssetError, validate_avatar_value
from app.services.expert_authoring import (
    ExpertAuthoringError,
    ExpertAuthoringService,
    ExpertVersionAssembler,
)
from app.services.expert_review import ExpertReviewService
from app.services.profile_desired_mutations import ProfileDesiredMutationService
from app.services.runtime_agent_profiles import (
    RuntimeAgentProfileError,
    RuntimeAgentProfileNotFoundError,
    SqlAlchemyRuntimeAgentProfileStore,
    normalize_profile_display_name,
)
from app.services.plaza.user_asset_scope import (
    UserAssetScopeError,
    resolve_default_user_asset_scope,
)
from app.services.runtime_workspace import Principal
from app.services.expert_profile_operations import ExpertProfileOperationService
from app.services.expert_profile_queries import ExpertProfileSummaryQueryService
from app.services.expert_errors import ExpertProfileError, derived_expert_plugin_ids


logger = logging.getLogger(__name__)


APPROVED_EXPERT_PLUGIN_IDS = frozenset(
    {
        "enterprise-knowledge",
        "personal-knowledge",
    }
)
NONTERMINAL_RUN_STATUSES = frozenset({"accepted", "pending", "running"})

class ExpertAuthoringCommandService:
    """Own self-created expert command workflows; no market installation logic."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.agent_profiles = SqlAlchemyRuntimeAgentProfileStore(db)
        self.summary_queries = ExpertProfileSummaryQueryService(db)
        self.operations = ExpertProfileOperationService(db)

    @staticmethod
    def _profile_desired_command(
        *,
        profile: RuntimeAgentProfile,
        capabilities: dict[str, Any],
        source_type: str,
        source_id: str,
        created_by_user_id: int,
        expert_package_version_id: int | None,
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
                int(item["asset_id"]) for item in capabilities.get("knowledge_bases", [])
            ],
            expert_package_version_id=expert_package_version_id,
            source_type=source_type,
            source_id=source_id,
            created_by_user_id=created_by_user_id,
        )


    async def create(
        self,
        *,
        principal: Principal,
        command: CreateExpertRequest,
    ) -> RuntimeAgentProfileSummary:
        avatar_value = await self._validated_avatar_value(
            principal=principal,
            avatar_value=command.avatar_url,
        )
        capabilities = await self._validated_capabilities(
            principal=principal,
            capabilities=command.capabilities,
        )
        inherited: RuntimeAgentProfile | None = None
        if command.inherited_from_profile_id is not None:
            await self.agent_profiles.require_owned(
                principal=principal,
                runtime_workspace_id=command.runtime_workspace_id,
                runtime_agent_profile_id=command.inherited_from_profile_id,
                ready_only=False,
            )
            inherited = await self.db.get(
                RuntimeAgentProfile,
                command.inherited_from_profile_id,
            )
        if inherited is not None:
            if not any(capabilities.values()):
                capabilities = await self._validated_capabilities(
                    principal=principal,
                    capabilities=await self._inherited_capabilities(inherited),
                )
            inherited_soul = inherited.soul_content
            soul_content = command.soul_content or inherited_soul or ""
        else:
            soul_content = command.soul_content

        await self._assert_active_name_available(
            principal=principal,
            runtime_workspace_id=command.runtime_workspace_id,
            name=command.name,
        )

        package = ExpertPackage(
            org_id=principal.org_id,
            owner_user_id=principal.user_id,
            origin_runtime_workspace_id=command.runtime_workspace_id,
            slug=f"expert-{uuid4().hex}",
            name=command.name,
            description=command.description,
            avatar_url=avatar_value,
            status="draft",
            visibility="private",
            active_name_key=self._active_name_key(command.name),
        )
        self.db.add(package)
        await self.db.flush()
        try:
            profile = await self.agent_profiles.create_expert_row(
                principal=principal,
                runtime_workspace_id=command.runtime_workspace_id,
                display_name=command.name,
                description=command.description,
                avatar_url=avatar_value,
                soul_content=soul_content,
                ownership_type="self_created",
                inherited_from_profile_id=command.inherited_from_profile_id,
                expert_package_id=int(package.id),
                capability_config=capabilities,
            )
        except RuntimeAgentProfileError as exc:
            raise ExpertProfileError("expert_name_conflict", status_code=409) from exc
        version = self._new_package_version(
            package=package,
            version_number=1,
            profile=profile,
            status=("private" if command.share_scope.is_private else "draft"),
            change_summary=(
                self._normalized_change_summary(command.change_summary)
                or ("初始发布" if not command.share_scope.is_private else None)
            ),
        )
        self.db.add(version)
        await self.db.flush()
        try:
            await ProfileDesiredMutationService(self.db).save_expert_complete(
                self._profile_desired_command(
                    profile=profile,
                    capabilities=capabilities,
                    source_type="private_expert",
                    source_id=f"expert-create-{version.id}",
                    created_by_user_id=principal.user_id,
                    expert_package_version_id=int(version.id),
                )
            )
            package.status = "active"
            await self.db.commit()
        except Exception as exc:
            await self.db.rollback()
            raise ExpertProfileError(
                "expert_profile_configuration_failed",
                status_code=409,
            ) from exc

        if not command.share_scope.is_private:
            await self.submit_review(
                principal=principal,
                runtime_workspace_id=command.runtime_workspace_id,
                runtime_agent_profile_id=int(profile.id),
                share_scope=command.share_scope,
                reuse_version_id=int(version.id),
                change_summary=version.change_summary,
                allow_activation_pending=True,
            )
        return await self.summary_queries.get_summary(profile)


    async def replace_share_scope(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        share_scope: ExpertShareScope,
        change_summary: str | None = None,
    ) -> RuntimeAgentProfileSummary:
        await self._validate_share_scope(principal=principal, scope=share_scope)
        profile = await self._owned_editable_profile(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
        package = await self.db.get(ExpertPackage, profile.expert_package_id)
        if package is None:
            raise ExpertProfileError("expert_package_not_found", status_code=404)
        self._require_change_summary_if_shared(
            scope=share_scope,
            change_summary=change_summary,
            package=package,
        )
        previous_config = dict(profile.capability_config or {})
        operation = await self.operations.begin(
            profile=profile,
            action="configure",
            rollback_snapshot={"capability_config": previous_config},
        )
        operation_db_id = int(operation.id)
        config = dict(previous_config)
        config["requested_share_scope"] = share_scope.model_dump()
        profile.capability_config = config
        self.operations.succeeded(operation, step="permissions_saved")
        try:
            await self.db.commit()
        except Exception as exc:
            await self.db.rollback()
            persisted_operation = await self.db.get(
                ExpertProfileOperation,
                operation_db_id,
            )
            if persisted_operation is not None:
                self.operations.failed(
                    persisted_operation,
                    error_code="expert_permissions_save_failed",
                    error=exc,
                    step="permissions_failed",
                )
                await self.db.commit()
            raise ExpertProfileError(
                "expert_permissions_save_failed",
                status_code=500,
            ) from exc
        version = await self._record_configuration_version(
            package=package,
            profile=profile,
            scope=share_scope,
            change_summary=change_summary,
        )
        if share_scope.is_private:
            if package.status == "published":
                await self._unpublish_package(package)
                await self.db.commit()
        else:
            await self.submit_review(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=int(profile.id),
                share_scope=share_scope,
                reuse_version_id=int(version.id),
                change_summary=version.change_summary,
            )
        return await self.summary_queries.get_summary(profile)


    async def submit_review(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        share_scope: ExpertShareScope | None = None,
        reuse_version_id: int | None = None,
        change_summary: str | None = None,
        allow_activation_pending: bool = False,
    ) -> RuntimeAgentProfileSummary:
        profile = await self._owned_editable_profile(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            allow_active_operation=allow_activation_pending,
        )
        package = await self.db.get(ExpertPackage, profile.expert_package_id)
        if package is None:
            raise ExpertProfileError("expert_package_not_found", status_code=404)
        scope = share_scope or self._requested_scope(profile)
        if scope.is_private:
            raise ExpertProfileError("expert_share_scope_required")
        await self._validate_share_scope(principal=principal, scope=scope)
        self._require_change_summary_if_shared(
            scope=scope,
            change_summary=change_summary,
            package=package,
        )
        pending_reviews = (
            (
                await self.db.execute(
                    select(TemplateReview).where(
                        TemplateReview.org_id == principal.org_id,
                        TemplateReview.expert_package_id == package.id,
                        TemplateReview.status == "pending",
                    )
                )
            )
            .scalars()
            .all()
        )
        matching_pending = next(
            (
                review
                for review in pending_reviews
                if reuse_version_id is not None
                and review.expert_package_version_id == reuse_version_id
            ),
            None,
        )
        if matching_pending is not None:
            version = await self.db.get(ExpertPackageVersion, reuse_version_id)
            if version is None or version.expert_package_id != package.id:
                raise ExpertProfileError("expert_version_not_found", status_code=404)
            config = dict(profile.capability_config or {})
            config["requested_share_scope"] = scope.model_dump()
            profile.capability_config = config
            ExpertReviewService.mark_pending(package)
            await self.db.commit()
            return await self.summary_queries.get_summary(profile)
        for pending in pending_reviews:
            pending.status = "superseded"
            pending.reviewed_at = datetime.now(timezone.utc)
            snapshot = dict(pending.review_snapshot or {})
            snapshot["superseded_at"] = pending.reviewed_at.isoformat()
            pending.review_snapshot = snapshot
            if pending.expert_package_version_id is not None:
                prior_version = await self.db.get(
                    ExpertPackageVersion,
                    pending.expert_package_version_id,
                )
                if prior_version is not None and prior_version.status == "pending_review":
                    prior_version.status = "superseded"
        if reuse_version_id is not None:
            version = await self.db.get(ExpertPackageVersion, reuse_version_id)
            if version is None or version.expert_package_id != package.id:
                raise ExpertProfileError("expert_version_not_found", status_code=404)
        else:
            next_version = (
                int(
                    await self.db.scalar(
                        select(func.max(ExpertPackageVersion.version)).where(
                            ExpertPackageVersion.expert_package_id == package.id
                        )
                    )
                    or 0
                )
                + 1
            )
            version = self._new_package_version(
                package=package,
                version_number=next_version,
                profile=profile,
                status="draft",
                change_summary=self._normalized_change_summary(change_summary),
            )
            self.db.add(version)
            await self.db.flush()
        if change_summary is not None:
            version.change_summary = self._normalized_change_summary(change_summary)
        try:
            await self._prepare_release_artifacts(version=version)
        except Exception as exc:
            version.status = "failed"
            await self.db.commit()
            raise ExpertProfileError(
                "expert_release_artifact_prepare_failed",
                status_code=409,
            ) from exc
        version.status = "pending_review"
        review = TemplateReview(
            org_id=principal.org_id,
            submitter_user_id=principal.user_id,
            expert_package_id=int(package.id),
            expert_package_version_id=int(version.id),
            review_snapshot=self._review_snapshot(
                package=package,
                version=version,
                scope=scope,
            ),
            status="pending",
        )
        self.db.add(review)
        ExpertReviewService.mark_pending(package)
        config = dict(profile.capability_config or {})
        config["requested_share_scope"] = scope.model_dump()
        profile.capability_config = config
        await self.db.commit()
        return await self.summary_queries.get_summary(profile)


    async def unpublish_owned(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
    ) -> RuntimeAgentProfileSummary:
        profile = await self._owned_editable_profile(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
        package = await self.db.get(ExpertPackage, profile.expert_package_id)
        if package is None:
            raise ExpertProfileError("expert_package_not_found", status_code=404)
        await self._unpublish_package(package)
        await self.db.commit()
        return await self.summary_queries.get_summary(profile)


    async def _owned_expert(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        allow_active_operation: bool = False,
    ) -> RuntimeAgentProfile:
        try:
            await self.agent_profiles.require_owned(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=runtime_agent_profile_id,
                ready_only=False,
            )
        except RuntimeAgentProfileNotFoundError as exc:
            raise ExpertProfileError("expert_not_found", status_code=404) from exc
        profile = await self.db.get(RuntimeAgentProfile, runtime_agent_profile_id)
        if profile is None or profile.profile_kind != "expert":
            raise ExpertProfileError("expert_not_found", status_code=404)
        active_operation = await self.db.scalar(
            select(ExpertProfileOperation.id)
            .where(
                ExpertProfileOperation.active_profile_key == profile.id,
                ExpertProfileOperation.status == "running",
            )
            .limit(1)
        )
        if active_operation is not None and not allow_active_operation:
            raise ExpertProfileError("expert_operation_in_progress", status_code=409)
        return profile


    async def _owned_editable_profile(self, **kwargs: Any) -> RuntimeAgentProfile:
        allow_activation_pending = bool(kwargs.get("allow_active_operation", False))
        profile = await self._owned_expert(**kwargs)
        if profile.ownership_type == "market_installed":
            raise ExpertProfileError("market_expert_is_not_editable", status_code=409)
        if profile.status != "active" and not allow_activation_pending:
            raise ExpertProfileError("expert_disabled", status_code=409)
        return profile


    async def _validated_capabilities(
        self,
        *,
        principal: Principal,
        capabilities: ExpertCapabilitySelection,
    ) -> dict[str, Any]:
        return await self._validated_stable_capabilities(
            principal=principal,
            capabilities=capabilities,
        )


    async def _validated_stable_capabilities(
        self,
        *,
        principal: Principal,
        capabilities: ExpertCapabilitySelection,
    ) -> dict[str, Any]:
        """Validate editor selections after installation IDs have been retired."""
        skill_refs = list(capabilities.skills)
        mcp_refs = list(capabilities.mcps)
        knowledge_refs = list(capabilities.knowledge_bases)
        if (
            any(
                item.source not in {"public", "personal", "default_installed"}
                for item in skill_refs
            )
            or any(item.source not in {"public", "default_installed"} for item in mcp_refs)
            or any(item.source not in {"public", "personal"} for item in knowledge_refs)
        ):
            raise ExpertProfileError("expert_capability_not_visible", status_code=403)

        default_skill_ids = {
            item.asset_id for item in skill_refs if item.source == "default_installed"
        }
        default_mcp_ids = {item.asset_id for item in mcp_refs if item.source == "default_installed"}
        if default_skill_ids or default_mcp_ids:
            try:
                default_scope = await resolve_default_user_asset_scope(
                    self.db,
                    org_id=principal.org_id,
                    user_id=principal.user_id,
                )
            except UserAssetScopeError as exc:
                raise ExpertProfileError(
                    "expert_default_profile_unavailable", status_code=409
                ) from exc
            bound_skill_ids = set(
                await self.db.scalars(
                    select(ProfileSkillBinding.skill_id).where(
                        ProfileSkillBinding.runtime_agent_profile_id
                        == default_scope.runtime_agent_profile_id,
                        ProfileSkillBinding.skill_id.in_(default_skill_ids),
                        ProfileSkillBinding.status == "enabled",
                    )
                )
            )
            bound_mcp_ids = set(
                await self.db.scalars(
                    select(ProfileMcpBinding.mcp_id).where(
                        ProfileMcpBinding.runtime_agent_profile_id
                        == default_scope.runtime_agent_profile_id,
                        ProfileMcpBinding.mcp_id.in_(default_mcp_ids),
                        ProfileMcpBinding.status == "enabled",
                    )
                )
            )
            if bound_skill_ids != default_skill_ids or bound_mcp_ids != default_mcp_ids:
                raise ExpertProfileError("expert_default_installed_asset_stale", status_code=409)

        selected_skill_ids = {item.asset_id for item in skill_refs}
        skill_rows = (
            (
                await self.db.execute(
                    select(Skill, SkillVersion)
                    .join(SkillVersion, SkillVersion.skill_id == Skill.id)
                    .where(
                        Skill.org_id == principal.org_id,
                        Skill.id.in_(selected_skill_ids),
                        Skill.status.notin_(("deleted", "revoked", "archived")),
                        SkillVersion.status != "deleted",
                    )
                )
            ).all()
            if selected_skill_ids
            else []
        )
        skill_versions = {
            (int(skill.id), int(version.id)): (skill, version) for skill, version in skill_rows
        }
        resolved_skills: list[dict[str, Any]] = []
        semantic_skill_ids: set[int] = set()
        runtime_names: set[str] = set()
        for reference in skill_refs:
            matching = [
                (skill, version)
                for (skill_id, _version_id), (skill, version) in skill_versions.items()
                if skill_id == reference.asset_id
            ]
            if not matching:
                raise ExpertProfileError("expert_capability_not_visible", status_code=403)
            skill = matching[0][0]
            is_public_scope = (
                str(skill.asset_scope) == "public" and str(skill.visibility) != "private"
            )
            is_personal_owned = (
                str(skill.asset_scope) == "personal"
                and str(skill.visibility) == "private"
                and int(skill.owner_user_id) == principal.user_id
            )
            effective_source = reference.source
            if reference.source == "personal":
                if not is_personal_owned:
                    raise ExpertProfileError("expert_capability_not_visible", status_code=403)
            elif reference.source == "default_installed":
                if not is_public_scope:
                    raise ExpertProfileError("expert_capability_not_visible", status_code=403)
            elif is_personal_owned:
                # Legacy `skill_ids` entry or client-supplied source="public" that actually
                # references a personal Skill owned by the creator: recover the attribution
                # so downstream version assembly records source="personal" instead of
                # mis-labeling it as public (which later breaks release preflight).
                effective_source = "personal"
            elif not is_public_scope:
                raise ExpertProfileError("expert_capability_not_visible", status_code=403)
            version_id = int(reference.version_id or skill.current_version_id or 0)
            if (int(skill.id), version_id) not in skill_versions:
                raise ExpertProfileError("expert_capability_not_visible", status_code=403)
            if int(skill.id) in semantic_skill_ids or str(skill.slug) in runtime_names:
                raise ExpertProfileError("expert_duplicate_skill_identity", status_code=422)
            semantic_skill_ids.add(int(skill.id))
            runtime_names.add(str(skill.slug))
            resolved_skills.append(
                {
                    "source": effective_source,
                    "asset_id": int(skill.id),
                    "version_id": version_id,
                }
            )

        selected_mcp_ids = {item.asset_id for item in mcp_refs}
        visible_mcps = set(
            await self.db.scalars(
                select(Mcp.id).where(
                    Mcp.org_id == principal.org_id,
                    Mcp.id.in_(selected_mcp_ids),
                    Mcp.visibility != "private",
                    Mcp.status == "published",
                    Mcp.revoked_at.is_(None),
                )
            )
        )
        if visible_mcps != selected_mcp_ids:
            raise ExpertProfileError("expert_capability_not_visible", status_code=403)

        if len({item.asset_id for item in knowledge_refs}) != len(knowledge_refs):
            raise ExpertProfileError("expert_duplicate_knowledge_base_identity", status_code=422)
        selected_knowledge_ids = {item.asset_id for item in knowledge_refs}
        visible_knowledge_ids = await self._visible_knowledge_base_ids(
            principal=principal,
            asset_ids=selected_knowledge_ids,
        )
        if visible_knowledge_ids != selected_knowledge_ids:
            raise ExpertProfileError("expert_capability_not_visible", status_code=403)
        personal_knowledge_ids = {
            item.asset_id for item in knowledge_refs if item.source == "personal"
        }
        if personal_knowledge_ids:
            owned = set(
                await self.db.scalars(
                    select(KnowledgeBase.id).where(
                        KnowledgeBase.org_id == principal.org_id,
                        KnowledgeBase.owner_user_id == principal.user_id,
                        KnowledgeBase.id.in_(personal_knowledge_ids),
                        KnowledgeBase.kind == "personal",
                        KnowledgeBase.status == "active",
                    )
                )
            )
            if owned != personal_knowledge_ids:
                raise ExpertProfileError("expert_capability_not_visible", status_code=403)
        kinds = set(
            await self.db.scalars(
                select(KnowledgeBase.kind).where(
                    KnowledgeBase.org_id == principal.org_id,
                    KnowledgeBase.id.in_(selected_knowledge_ids),
                )
            )
        )
        plugin_ids = derived_expert_plugin_ids(
            knowledge_kinds=kinds,
            enterprise_knowledge_enabled=capabilities.enterprise_knowledge_enabled,
            submitted_plugin_ids=capabilities.plugin_ids,
        )
        return {
            "skill_ids": sorted(semantic_skill_ids),
            "mcp_ids": sorted(selected_mcp_ids),
            "knowledge_base_ids": sorted(selected_knowledge_ids),
            "enterprise_knowledge_enabled": capabilities.enterprise_knowledge_enabled,
            "plugin_ids": plugin_ids,
            "skills": sorted(resolved_skills, key=lambda item: int(item["asset_id"])),
            "mcps": [
                {"source": "public", "asset_id": item.asset_id}
                for item in sorted(mcp_refs, key=lambda item: item.asset_id)
            ],
            "knowledge_bases": [
                item.model_dump(exclude_none=True)
                for item in sorted(knowledge_refs, key=lambda item: item.asset_id)
            ],
        }


    async def _validated_avatar_value(
        self,
        *,
        principal: Principal,
        avatar_value: str | None,
    ) -> str | None:
        try:
            return await validate_avatar_value(
                self.db,
                principal=principal,
                avatar_value=avatar_value,
            )
        except AvatarAssetError as exc:
            raise ExpertProfileError(
                exc.code,
                status_code=exc.status_code,
            ) from exc


    async def _inherited_capabilities(
        self,
        profile: RuntimeAgentProfile,
    ) -> ExpertCapabilitySelection:
        principal = Principal(org_id=int(profile.org_id), user_id=int(profile.user_id))
        if profile.profile_kind == "expert":
            source = self._normalized_capabilities(profile.capability_config or {})
            validated = await self._validated_capabilities(
                principal=principal,
                capabilities=ExpertCapabilitySelection(**source),
            )
            return ExpertCapabilitySelection(**validated)
        skill_ids = {
            int(item)
            for item in await self.db.scalars(
                select(ProfileSkillBinding.skill_id)
                .join(Skill, Skill.id == ProfileSkillBinding.skill_id)
                .where(
                    ProfileSkillBinding.runtime_agent_profile_id == profile.id,
                    ProfileSkillBinding.status == "enabled",
                    Skill.status.notin_(("deleted", "revoked", "archived")),
                )
            )
        }
        mcp_ids = {
            int(item)
            for item in await self.db.scalars(
                select(ProfileMcpBinding.mcp_id)
                .join(Mcp, Mcp.id == ProfileMcpBinding.mcp_id)
                .where(
                    ProfileMcpBinding.runtime_agent_profile_id == profile.id,
                    ProfileMcpBinding.status == "enabled",
                    Mcp.status == "published",
                    Mcp.revoked_at.is_(None),
                )
            )
        }
        knowledge_ids = {
            int(item)
            for item in await self.db.scalars(
                select(ProfileKnowledgeBinding.knowledge_base_id)
                .join(
                    KnowledgeBase,
                    KnowledgeBase.id == ProfileKnowledgeBinding.knowledge_base_id,
                )
                .where(
                    ProfileKnowledgeBinding.runtime_agent_profile_id == profile.id,
                    ProfileKnowledgeBinding.status == "enabled",
                    KnowledgeBase.status == "active",
                )
            )
        }
        knowledge_rows = (
            (
                await self.db.execute(
                    select(KnowledgeBase.id, KnowledgeBase.kind).where(
                        KnowledgeBase.org_id == profile.org_id,
                        KnowledgeBase.id.in_(knowledge_ids),
                        KnowledgeBase.status == "active",
                    )
                )
            ).all()
            if knowledge_ids
            else []
        )
        visible_knowledge = {int(item.id): str(item.kind) for item in knowledge_rows}
        return ExpertCapabilitySelection(
            skills=[
                {"source": "default_installed", "asset_id": item} for item in sorted(skill_ids)
            ],
            mcps=[{"source": "default_installed", "asset_id": item} for item in sorted(mcp_ids)],
            knowledge_bases=[
                {
                    "source": "personal" if visible_knowledge[item] == "personal" else "public",
                    "asset_id": item,
                }
                for item in sorted(visible_knowledge)
            ],
            enterprise_knowledge_enabled=False,
        )


    async def _visible_knowledge_base_ids(
        self,
        *,
        principal: Principal,
        asset_ids: set[int] | None = None,
    ) -> set[int]:
        clauses = [
            KnowledgeBase.org_id == principal.org_id,
            KnowledgeBase.status == "active",
        ]
        if asset_ids is not None:
            if not asset_ids:
                return set()
            clauses.append(KnowledgeBase.id.in_(asset_ids))
        knowledge_bases = (
            (await self.db.execute(select(KnowledgeBase).where(*clauses))).scalars().all()
        )
        candidate_ids = {int(item.id) for item in knowledge_bases}
        if not candidate_ids:
            return set()
        department_ids = await self._department_ids(principal)
        target_filters = [
            (Permission.grantee_type == "user") & (Permission.grantee_id == str(principal.user_id)),
            (Permission.grantee_type == "all") & (Permission.grantee_id == "*"),
        ]
        if department_ids:
            target_filters.append(
                (Permission.grantee_type == "department")
                & (Permission.grantee_id.in_(department_ids))
            )
        permitted_ids = set(
            (
                await self.db.execute(
                    select(Permission.asset_id).where(
                        Permission.org_id == principal.org_id,
                        Permission.asset_type == "knowledge_base",
                        Permission.asset_id.in_(candidate_ids),
                        or_(*target_filters),
                        or_(
                            Permission.kb_role.is_(None),
                            Permission.kb_role.in_(("retrieve", "preview", "download", "owner")),
                        ),
                    )
                )
            ).scalars()
        )
        return {
            int(item.id)
            for item in knowledge_bases
            if int(item.owner_user_id) == principal.user_id
            or str(item.visibility).lower() in {"organization", "org", "public"}
            or int(item.id) in permitted_ids
        }


    async def _validate_share_scope(
        self,
        *,
        principal: Principal,
        scope: ExpertShareScope,
    ) -> None:
        if scope.user_ids:
            valid_users = set(
                (
                    await self.db.execute(
                        select(User.id).where(
                            User.org_id == principal.org_id,
                            User.id.in_(scope.user_ids),
                            User.status == "active",
                        )
                    )
                ).scalars()
            )
            if valid_users != set(scope.user_ids):
                raise ExpertProfileError("invalid_expert_share_user")
        if scope.department_ids:
            # Defense-in-depth: reject any synthetic/virtual directory node id before the
            # DB lookup. The organization-directory tree emits `virtual:…` placeholders
            # for users without a real department; those must never become share targets.
            if any(str(dept_id).startswith("virtual:") for dept_id in scope.department_ids):
                raise ExpertProfileError("invalid_expert_share_department")
            # 公司虚拟根（com_id 即根部门 ID）是合法的部门授权目标，
            # 勾选公司根等价于授权该公司全部人员。
            requested = set(str(dept_id) for dept_id in scope.department_ids)
            valid_departments = set(
                (
                    await self.db.execute(
                        select(SysDept.dept_id).where(
                            SysDept.dept_id.in_(scope.department_ids),
                            SysDept.status == 1,
                        )
                    )
                ).scalars()
            )
            if (valid_departments | COMPANY_ROOT_DEPARTMENT_IDS) & requested != requested:
                raise ExpertProfileError("invalid_expert_share_department")


    async def _department_ids(self, principal: Principal) -> set[str]:
        user = await self.db.scalar(
            select(User).where(
                User.id == principal.user_id,
                User.org_id == principal.org_id,
                User.status == "active",
            )
        )
        if user is None or not user.user_code:
            return set()
        directory_user = await self.db.scalar(select(SysUser).where(SysUser.code == user.user_code))
        current = str(directory_user.dept_id or "") if directory_user else ""
        department_ids: set[str] = set()
        if directory_user is not None and directory_user.com_id:
            department_ids.add(str(directory_user.com_id))
        while current and current not in department_ids:
            department_ids.add(current)
            current = str(
                await self.db.scalar(select(SysDept.parent_id).where(SysDept.dept_id == current))
                or ""
            )
        return department_ids


    @staticmethod
    def _normalized_capabilities(value: dict[str, Any]) -> dict[str, Any]:
        def references(
            key: str,
            legacy_key: str,
        ) -> list[dict[str, Any]]:
            raw = value.get(key)
            source = (
                raw
                if isinstance(raw, list) and raw
                else [{"source": "public", "asset_id": item} for item in value.get(legacy_key, [])]
            )
            unique: dict[tuple[str, int, int | None], dict[str, Any]] = {}
            for item in source:
                if not isinstance(item, dict):
                    continue
                try:
                    asset_id = int(item.get("asset_id"))
                except (TypeError, ValueError):
                    continue
                if asset_id <= 0:
                    continue
                source_type = str(item.get("source") or "public")
                if source_type not in {"public", "personal", "default_installed"}:
                    continue
                try:
                    version_id = (
                        int(item["version_id"]) if item.get("version_id") is not None else None
                    )
                except (TypeError, ValueError):
                    continue
                if version_id is not None and version_id <= 0:
                    continue
                unique[(source_type, asset_id, version_id)] = {
                    "source": source_type,
                    "asset_id": asset_id,
                    **({"version_id": version_id} if version_id is not None else {}),
                }
            return [
                unique[item]
                for item in sorted(unique, key=lambda item: (item[0], item[1], item[2] or 0))
            ]

        skills = references("skills", "skill_ids")
        mcps = references("mcps", "mcp_ids")
        knowledge_bases = references("knowledge_bases", "knowledge_base_ids")
        return {
            "skill_ids": sorted(
                {int(item["asset_id"]) for item in skills if item["source"] == "public"}
            ),
            "mcp_ids": sorted(
                {int(item["asset_id"]) for item in mcps if item["source"] == "public"}
            ),
            "knowledge_base_ids": sorted({int(item["asset_id"]) for item in knowledge_bases}),
            "enterprise_knowledge_enabled": bool(value.get("enterprise_knowledge_enabled", False)),
            "plugin_ids": sorted({str(item) for item in value.get("plugin_ids", []) if str(item)}),
            "skills": skills,
            "mcps": mcps,
            "knowledge_bases": knowledge_bases,
        }

    @staticmethod
    def _active_name_key(name: str) -> str:
        return " ".join(name.split()).casefold()

    @staticmethod
    def _normalized_change_summary(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


    async def _assert_active_name_available(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        name: str,
        excluding_package_id: int | None = None,
    ) -> None:
        profile_query = select(RuntimeAgentProfile.id).where(
            RuntimeAgentProfile.org_id == principal.org_id,
            RuntimeAgentProfile.user_id == principal.user_id,
            RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
            RuntimeAgentProfile.active_display_name_key
            == normalize_profile_display_name(name),
            RuntimeAgentProfile.status == "active",
        )
        if excluding_package_id is not None:
            profile_query = profile_query.where(
                or_(
                    RuntimeAgentProfile.expert_package_id.is_(None),
                    RuntimeAgentProfile.expert_package_id != excluding_package_id,
                )
            )
        if await self.db.scalar(profile_query.limit(1)) is not None:
            raise ExpertProfileError("expert_name_conflict", status_code=409)

        query = select(ExpertPackage.id).where(
            ExpertPackage.org_id == principal.org_id,
            ExpertPackage.owner_user_id == principal.user_id,
            ExpertPackage.origin_runtime_workspace_id == runtime_workspace_id,
            ExpertPackage.active_name_key == self._active_name_key(name),
        )
        if excluding_package_id is not None:
            query = query.where(ExpertPackage.id != excluding_package_id)
        if await self.db.scalar(query.limit(1)) is not None:
            raise ExpertProfileError("expert_name_conflict", status_code=409)


    def _require_change_summary_if_shared(
        self,
        *,
        scope: ExpertShareScope,
        change_summary: str | None,
        package: ExpertPackage,
    ) -> None:
        if scope.is_private:
            return
        # The first shared version keeps a readable default for backwards
        # compatible clients.  Every later shared save must explain the change.
        if package.current_version_id is not None and not self._normalized_change_summary(
            change_summary
        ):
            raise ExpertProfileError("expert_change_summary_required", status_code=422)


    async def _record_configuration_version(
        self,
        *,
        package: ExpertPackage,
        profile: RuntimeAgentProfile,
        scope: ExpertShareScope,
        change_summary: str | None,
        commit: bool = True,
    ) -> ExpertPackageVersion:
        next_version = (
            int(
                await self.db.scalar(
                    select(func.max(ExpertPackageVersion.version)).where(
                        ExpertPackageVersion.expert_package_id == package.id
                    )
                )
                or 0
            )
            + 1
        )
        version = self._new_package_version(
            package=package,
            version_number=next_version,
            profile=profile,
            status="private" if scope.is_private else "draft",
            change_summary=(
                self._normalized_change_summary(change_summary)
                or ("初始发布" if not scope.is_private else None)
            ),
        )
        self.db.add(version)
        if commit:
            await self.db.commit()
        else:
            await self.db.flush()
        return version

    @classmethod
    def _new_package_version(
        cls,
        *,
        package: ExpertPackage,
        version_number: int,
        profile: RuntimeAgentProfile,
        status: str,
        change_summary: str | None = None,
    ) -> ExpertPackageVersion:
        return ExpertVersionAssembler.assemble(
            package=package,
            profile=profile,
            capabilities=cls._normalized_capabilities(
                profile.capability_config or {}
            ),
            version_number=version_number,
            status=status,
            change_summary=change_summary,
        )

    @staticmethod
    def _review_snapshot(
        *,
        package: ExpertPackage,
        version: ExpertPackageVersion,
        scope: ExpertShareScope,
    ) -> dict[str, Any]:
        return {
            "schema_version": "expert.review/v2",
            "basic_info": dict(version.basic_info_snapshot or {}),
            "soul": version.soul_snapshot or "",
            "system_prompt": version.prompt_snapshot,
            "capabilities": dict(version.manifest.get("capabilities") or {}),
            "requested_share_scope": scope.model_dump(),
            "change_summary": version.change_summary,
            "source": {
                "expert_package_id": int(package.id),
                "expert_package_version_id": int(version.id),
                "owner_user_id": int(package.owner_user_id),
            },
        }

    @staticmethod
    def _requested_scope(profile: RuntimeAgentProfile) -> ExpertShareScope:
        config = dict(profile.capability_config or {})
        return ExpertShareScope.model_validate(config.get("requested_share_scope") or {})

    @classmethod
    def _capabilities_from_version(
        cls,
        version: ExpertPackageVersion,
    ) -> dict[str, Any]:
        return ExpertProfileSummaryQueryService.capabilities_from_version(version)

    async def _prepare_release_artifacts(
        self,
        *,
        version: ExpertPackageVersion,
    ) -> None:
        """Validate stable release references without cloning personal assets."""
        package = await self.db.get(ExpertPackage, version.expert_package_id)
        if package is None:
            raise ValueError("expert package is unavailable for release")
        capabilities = self._capabilities_from_version(version)
        for reference in capabilities["skills"]:
            skill_id = int(reference["asset_id"])
            skill = await self.db.scalar(
                select(Skill).where(
                    Skill.id == skill_id,
                    Skill.org_id == version.org_id,
                    Skill.status.notin_(("deleted", "revoked", "archived")),
                )
            )
            if skill is None or skill.current_version_id is None:
                raise ValueError("selected expert Skill is unavailable for release")
            source_version_id = int(reference.get("version_id") or skill.current_version_id)
            source_version = await self.db.scalar(
                select(SkillVersion).where(
                    SkillVersion.id == source_version_id,
                    SkillVersion.skill_id == skill.id,
                    SkillVersion.status != "deleted",
                )
            )
            if (
                source_version is None
                or not source_version.package_uri
                or not source_version.package_sha256
            ):
                raise ValueError("selected expert Skill version is unavailable for release")
            reference_source = str(reference.get("source") or "public")
            is_public_scope = (
                str(skill.asset_scope) == "public" and str(skill.visibility) != "private"
            )
            is_personal_owned = (
                str(skill.asset_scope) == "personal"
                and str(skill.visibility) == "private"
                and int(skill.owner_user_id) == int(package.owner_user_id)
            )
            if reference_source != "personal":
                if is_personal_owned:
                    # Legacy stored skill_refs may label a personal Skill owned by the
                    # package creator as source="public"; recover the attribution so the
                    # release preflight matches the save-time rule.
                    reference_source = "personal"
                elif not is_public_scope:
                    raise ValueError("selected expert Skill is not public")
                continue
            if not is_personal_owned:
                raise ValueError("selected personal expert Skill is unavailable for release")
        mcp_ids = {
            int(item["asset_id"])
            for item in capabilities["mcps"]
            if isinstance(item, dict) and item.get("asset_id") is not None
        }
        if mcp_ids:
            visible_mcps = set(
                await self.db.scalars(
                    select(Mcp.id).where(
                        Mcp.org_id == version.org_id,
                        Mcp.id.in_(mcp_ids),
                        Mcp.visibility != "private",
                        Mcp.status == "published",
                        Mcp.revoked_at.is_(None),
                        Mcp.current_version_id.is_not(None),
                    )
                )
            )
            if {int(item) for item in visible_mcps} != mcp_ids:
                raise ValueError("selected expert MCP is unavailable for release")
        knowledge_ids = {
            int(item["asset_id"])
            for item in capabilities["knowledge_bases"]
            if isinstance(item, dict) and item.get("asset_id") is not None
        }
        if knowledge_ids:
            available = set(
                await self.db.scalars(
                    select(KnowledgeBase.id).where(
                        KnowledgeBase.org_id == version.org_id,
                        KnowledgeBase.id.in_(knowledge_ids),
                        KnowledgeBase.status == "active",
                    )
                )
            )
            if {int(item) for item in available} != knowledge_ids:
                raise ValueError("selected expert knowledge base is unavailable for release")
        await self.db.flush()


    async def _assert_release_artifacts_ready(
        self,
        *,
        version: ExpertPackageVersion,
    ) -> None:
        await self._prepare_release_artifacts(version=version)


    async def _save_owned_profile_desired(
        self,
        *,
        principal: Principal,
        profile: RuntimeAgentProfile,
        package: ExpertPackage,
        scope: ExpertShareScope,
        change_summary: str | None,
    ) -> ExpertPackageVersion:
        """Persist one complete expert version and its sole Profile Desired intent."""
        capabilities = self._normalized_capabilities(profile.capability_config or {})
        normalized_summary = self._normalized_change_summary(change_summary) or (
            "初始发布" if not scope.is_private else None
        )
        try:
            result = await ExpertAuthoringService(self.db).save_existing(
                principal=principal,
                profile=profile,
                package=package,
                capabilities=capabilities,
                desired_command=self._profile_desired_command(
                    profile=profile,
                    capabilities=capabilities,
                    source_type="private_expert",
                    source_id=f"expert-save-{uuid4().hex}",
                    created_by_user_id=principal.user_id,
                    expert_package_version_id=None,
                ),
                version_status="private" if scope.is_private else "draft",
                change_summary=normalized_summary,
            )
        except ExpertAuthoringError as exc:
            raise ExpertProfileError(exc.code.lower(), status_code=409) from exc
        await self.db.commit()
        return result.version


    async def update(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        command: UpdateExpertRequest,
    ) -> RuntimeAgentProfileSummary:
        """Update expert identity through Profile Desired; never project to Dashboard/PVC."""
        profile = await self._owned_editable_profile(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
        package = await self.db.get(ExpertPackage, profile.expert_package_id)
        if package is None:
            raise ExpertProfileError("expert_package_not_found", status_code=404)
        scope = self._requested_scope(profile)
        self._require_change_summary_if_shared(
            scope=scope,
            change_summary=command.change_summary,
            package=package,
        )
        if command.name is not None and command.name != profile.display_name:
            await self._assert_active_name_available(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                name=command.name,
                excluding_package_id=int(package.id),
            )
            profile.display_name = command.name
            profile.active_display_name_key = normalize_profile_display_name(
                command.name
            )
            package.name = command.name
            package.active_name_key = self._active_name_key(command.name)
        if "description" in command.model_fields_set:
            profile.description = command.description
            package.description = command.description
        if "avatar_url" in command.model_fields_set:
            avatar_value = await self._validated_avatar_value(
                principal=principal,
                avatar_value=command.avatar_url,
            )
            profile.avatar_url = avatar_value
            package.avatar_url = avatar_value
        if command.soul_content is not None:
            profile.soul_content = command.soul_content
        package.origin_runtime_workspace_id = runtime_workspace_id
        version = await self._save_owned_profile_desired(
            principal=principal,
            profile=profile,
            package=package,
            scope=scope,
            change_summary=command.change_summary,
        )
        if not scope.is_private:
            await self.submit_review(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=int(profile.id),
                share_scope=scope,
                reuse_version_id=int(version.id),
                change_summary=version.change_summary,
                allow_activation_pending=True,
            )
        return await self.summary_queries.get_summary(profile)


    async def replace_capabilities(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        capabilities: ExpertCapabilitySelection,
        change_summary: str | None = None,
    ) -> RuntimeAgentProfileSummary:
        """Replace the expert capability set as one stable-ID Desired snapshot."""
        profile = await self._owned_editable_profile(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
        package = await self.db.get(ExpertPackage, profile.expert_package_id)
        if package is None:
            raise ExpertProfileError("expert_package_not_found", status_code=404)
        scope = self._requested_scope(profile)
        self._require_change_summary_if_shared(
            scope=scope,
            change_summary=change_summary,
            package=package,
        )
        next_capabilities = await self._validated_capabilities(
            principal=principal,
            capabilities=capabilities,
        )
        previous = dict(profile.capability_config or {})
        if previous.get("requested_share_scope") is not None:
            next_capabilities["requested_share_scope"] = previous["requested_share_scope"]
        profile.capability_config = next_capabilities
        version = await self._save_owned_profile_desired(
            principal=principal,
            profile=profile,
            package=package,
            scope=scope,
            change_summary=change_summary,
        )
        if not scope.is_private:
            await self.submit_review(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=int(profile.id),
                share_scope=scope,
                reuse_version_id=int(version.id),
                change_summary=version.change_summary,
                allow_activation_pending=True,
            )
        return await self.summary_queries.get_summary(profile)


    async def save_configuration(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        command: UpdateExpertConfigurationRequest,
    ) -> RuntimeAgentProfileSummary:
        """Save the complete expert editor draft atomically through Profile Desired."""
        profile = await self._owned_editable_profile(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
        package = await self.db.get(ExpertPackage, profile.expert_package_id)
        if package is None:
            raise ExpertProfileError("expert_package_not_found", status_code=404)
        await self._validate_share_scope(principal=principal, scope=command.share_scope)
        next_capabilities = await self._validated_capabilities(
            principal=principal,
            capabilities=command.capabilities,
        )
        avatar_value = await self._validated_avatar_value(
            principal=principal,
            avatar_value=command.avatar_url,
        )
        previous_scope = self._requested_scope(profile)
        normalized_next = self._normalized_capabilities(next_capabilities)
        unchanged = (
            profile.display_name == command.name
            and profile.description == command.description
            and profile.avatar_url == avatar_value
            and (profile.soul_content or "") == command.soul_content
            and self._normalized_capabilities(profile.capability_config or {}) == normalized_next
            and previous_scope.model_dump() == command.share_scope.model_dump()
        )
        if unchanged:
            raise ExpertProfileError("expert_configuration_unchanged", status_code=422)
        self._require_change_summary_if_shared(
            scope=command.share_scope,
            change_summary=command.change_summary,
            package=package,
        )
        if profile.display_name != command.name:
            await self._assert_active_name_available(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                name=command.name,
                excluding_package_id=int(package.id),
            )
        next_config = dict(normalized_next)
        next_config["requested_share_scope"] = command.share_scope.model_dump()
        profile.display_name = command.name
        profile.active_display_name_key = normalize_profile_display_name(command.name)
        profile.description = command.description
        profile.avatar_url = avatar_value
        profile.soul_content = command.soul_content
        profile.capability_config = next_config
        package.name = command.name
        package.description = command.description
        package.avatar_url = avatar_value
        package.origin_runtime_workspace_id = runtime_workspace_id
        package.active_name_key = self._active_name_key(command.name)
        version = await self._save_owned_profile_desired(
            principal=principal,
            profile=profile,
            package=package,
            scope=command.share_scope,
            change_summary=command.change_summary,
        )
        if command.share_scope.is_private:
            if package.status == "published":
                await self._unpublish_package(package)
                await self.db.commit()
        else:
            await self.submit_review(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=int(profile.id),
                share_scope=command.share_scope,
                reuse_version_id=int(version.id),
                change_summary=version.change_summary,
                allow_activation_pending=True,
            )
        return await self.summary_queries.get_summary(profile)


    async def copy_owned(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
    ) -> RuntimeAgentProfileSummary:
        try:
            result = await ExpertAuthoringService(self.db).copy(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=runtime_agent_profile_id,
            )
            await self.db.commit()
        except ExpertAuthoringError as exc:
            await self.db.rollback()
            raise ExpertProfileError(exc.code.lower(), status_code=409) from exc
        return await self.summary_queries.get_summary(result.profile)


    async def _unpublish_package(self, package: ExpertPackage) -> None:
        await self._hide_market_package(package)
        package.status = "active"
        package.visibility = "private"


    async def _hide_market_package(self, package: ExpertPackage) -> None:
        templates = (
            (
                await self.db.execute(
                    select(PlazaTemplate)
                    .join(
                        TemplateVersion,
                        TemplateVersion.template_id == PlazaTemplate.id,
                    )
                    .where(
                        PlazaTemplate.org_id == package.org_id,
                        TemplateVersion.source_expert_package_id == package.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for template in templates:
            template.status = "unpublished"
        await self.db.execute(
            delete(Permission).where(
                Permission.org_id == package.org_id,
                Permission.asset_type == "expert_package",
                Permission.asset_id == package.id,
            )
        )


__all__ = ["ExpertAuthoringCommandService"]
