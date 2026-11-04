"""Read-only query services for expert Profiles.

This module is deliberately separate from expert authoring and installation
commands.  It builds the user-facing Profile summary from current MySQL
control-plane state and never mutates Desired, bindings, or Runtime state.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ExpertPackage,
    ExpertPackageVersion,
    ExpertProfileOperation,
    InstalledExpertBinding,
    ProfileConvergenceJob,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    TemplateReview,
    User,
)
from app.schemas.experts import (
    ExpertCapabilitySelection,
    ExpertShareScope,
    RuntimeAgentProfileSummary,
)
from app.schemas.personal_knowledge import PersonalKnowledgeSelectedUser
from app.services.expert_actions import ExpertActionContext, decide_expert_actions


class ExpertProfileQueryError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class ExpertProfileSummaryQueryService:
    """Project one Profile and its current control-plane state into a DTO."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_summary(
        self,
        profile: RuntimeAgentProfile,
    ) -> RuntimeAgentProfileSummary:
        latest_operation = await self.db.scalar(
            select(ExpertProfileOperation)
            .where(ExpertProfileOperation.runtime_agent_profile_id == profile.id)
            .order_by(
                ExpertProfileOperation.created_at.desc(),
                ExpertProfileOperation.id.desc(),
            )
            .limit(1)
        )
        binding = await self.db.scalar(
            select(InstalledExpertBinding).where(
                InstalledExpertBinding.runtime_agent_profile_id == profile.id,
                InstalledExpertBinding.status == "enabled",
            )
        )
        package = (
            await self.db.get(ExpertPackage, profile.expert_package_id)
            if profile.expert_package_id is not None
            else None
        )
        latest_review = None
        if package is not None:
            latest_review = await self.db.scalar(
                select(TemplateReview)
                .where(
                    TemplateReview.org_id == profile.org_id,
                    TemplateReview.expert_package_id == package.id,
                )
                .order_by(TemplateReview.id.desc())
                .limit(1)
            )
            if (
                latest_review is not None
                and latest_review.status == "approved"
                and package.status != "published"
            ):
                latest_review = None
        installed_version_id = (
            int(binding.expert_package_version_id) if binding is not None else None
        )
        installed_version = (
            await self.db.scalar(
                select(ExpertPackageVersion.version).where(
                    ExpertPackageVersion.id == binding.expert_package_version_id
                )
            )
            if binding is not None
            else None
        )
        local_version = (
            int(
                await self.db.scalar(
                    select(func.max(ExpertPackageVersion.version)).where(
                        ExpertPackageVersion.expert_package_id == package.id
                    )
                )
                or 0
            )
            if package is not None and profile.ownership_type == "self_created"
            else None
        )
        available_version_id = (
            int(package.current_version_id)
            if package is not None and package.current_version_id is not None
            else None
        )
        share_scope = (
            self._scope_from_review(latest_review)
            if latest_review is not None
            else self._requested_scope(profile)
        )
        desired_revision_number = int(profile.desired_revision or 0)
        applied_revision_number = int(profile.applied_revision or 0)
        revision_numbers = {
            desired_revision_number,
            applied_revision_number,
        } - {0}
        revision_rows = list(
            await self.db.scalars(
                select(RuntimeAgentProfileRevision).where(
                    RuntimeAgentProfileRevision.runtime_agent_profile_id == profile.id,
                    RuntimeAgentProfileRevision.revision.in_(revision_numbers),
                )
            )
        )
        revision_by_number = {int(item.revision): item for item in revision_rows}
        configured_version_id = (
            revision_by_number[desired_revision_number].expert_package_version_id
            if desired_revision_number in revision_by_number
            else None
        )
        applied_version_id = (
            revision_by_number[applied_revision_number].expert_package_version_id
            if applied_revision_number in revision_by_number
            else None
        )
        convergence_job = await self.db.scalar(
            select(ProfileConvergenceJob).where(
                ProfileConvergenceJob.runtime_agent_profile_id == profile.id
            )
        )
        if desired_revision_number == 0:
            convergence_status = "unconfigured"
        elif applied_revision_number == desired_revision_number:
            convergence_status = "applied"
        elif convergence_job is not None and str(convergence_job.status) == "failed":
            convergence_status = "failed"
        elif convergence_job is not None and str(convergence_job.status) == "updating":
            convergence_status = "updating"
        else:
            convergence_status = "waiting"
        effective_status = (
            str(profile.status)
            if str(profile.status) != "active"
            else convergence_status
        )
        return RuntimeAgentProfileSummary(
            runtime_agent_profile_id=int(profile.id),
            runtime_workspace_id=int(profile.runtime_workspace_id),
            hermes_profile_name=str(profile.hermes_profile_name),
            profile_kind=str(profile.profile_kind),
            display_name=str(profile.display_name),
            description=profile.description,
            avatar_url=profile.avatar_url,
            soul_content=profile.soul_content or "",
            ownership_type=str(profile.ownership_type),
            inherited_from_profile_id=(
                int(profile.inherited_from_profile_id)
                if profile.inherited_from_profile_id is not None
                else None
            ),
            expert_package_id=(
                int(profile.expert_package_id)
                if profile.expert_package_id is not None
                else None
            ),
            local_version=local_version,
            installed_version_id=installed_version_id,
            installed_version=(
                int(installed_version) if installed_version is not None else None
            ),
            available_version_id=available_version_id,
            update_available=bool(
                installed_version_id is not None
                and available_version_id is not None
                and installed_version_id != available_version_id
                and package is not None
                and package.status == "published"
            ),
            desired_revision=desired_revision_number,
            applied_revision=applied_revision_number,
            configured_expert_version_id=(
                int(configured_version_id) if configured_version_id is not None else None
            ),
            applied_expert_version_id=(
                int(applied_version_id) if applied_version_id is not None else None
            ),
            convergence_status=convergence_status,
            effective_status=effective_status,
            market_removed=bool(
                binding is not None
                and package is not None
                and package.status != "published"
            ),
            capabilities=ExpertCapabilitySelection(
                **self.normalized_capabilities(profile.capability_config or {})
            ),
            share_scope=share_scope,
            share_scope_users=await self.selected_share_users(
                org_id=int(profile.org_id),
                user_ids=share_scope.user_ids,
            ),
            review_id=int(latest_review.id) if latest_review is not None else None,
            review_status=(
                str(latest_review.status) if latest_review is not None else None
            ),
            review_comment=(
                latest_review.review_comment if latest_review is not None else None
            ),
            last_operation_id=(
                str(latest_operation.operation_id) if latest_operation is not None else None
            ),
            last_operation_action=(
                str(latest_operation.action) if latest_operation is not None else None
            ),
            last_operation_status=(
                str(latest_operation.status) if latest_operation is not None else None
            ),
            last_operation_step=(
                str(latest_operation.step) if latest_operation is not None else None
            ),
            last_operation_error=(
                latest_operation.error_message if latest_operation is not None else None
            ),
            allowed_actions=decide_expert_actions(
                ExpertActionContext(
                    ownership_type=str(profile.ownership_type),
                    lifecycle_status=str(profile.status),
                    applied_revision=applied_revision_number,
                    operation_running=(
                        latest_operation is not None
                        and str(latest_operation.status) == "running"
                    ),
                    review_status=(
                        str(latest_review.status)
                        if latest_review is not None
                        else None
                    ),
                )
            ),
            status=str(profile.status),
            last_error=(
                str(convergence_job.last_error_message or convergence_job.last_error_code)
                if convergence_job is not None
                and (convergence_job.last_error_message or convergence_job.last_error_code)
                else None
            ),
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )

    @staticmethod
    def normalized_capabilities(value: dict[str, Any]) -> dict[str, Any]:
        def references(key: str, legacy_key: str) -> list[dict[str, Any]]:
            raw = value.get(key)
            source = (
                raw
                if isinstance(raw, list) and raw
                else [
                    {"source": "public", "asset_id": item}
                    for item in value.get(legacy_key, [])
                ]
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
                        int(item["version_id"])
                        if item.get("version_id") is not None
                        else None
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
                for item in sorted(
                    unique,
                    key=lambda item: (item[0], item[1], item[2] or 0),
                )
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
            "knowledge_base_ids": sorted(
                {int(item["asset_id"]) for item in knowledge_bases}
            ),
            "enterprise_knowledge_enabled": bool(
                value.get("enterprise_knowledge_enabled", False)
            ),
            "plugin_ids": sorted(
                {str(item) for item in value.get("plugin_ids", []) if str(item)}
            ),
            "skills": skills,
            "mcps": mcps,
            "knowledge_bases": knowledge_bases,
        }

    @classmethod
    def capabilities_from_version(
        cls,
        version: ExpertPackageVersion,
    ) -> dict[str, Any]:
        def refs(values: list[Any] | None, legacy_key: str) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            for item in values or []:
                if not isinstance(item, dict):
                    continue
                if item.get("asset_id") is not None:
                    result.append(
                        {
                            "source": str(item.get("source") or "public"),
                            "asset_id": item["asset_id"],
                            **(
                                {"version_id": item["version_id"]}
                                if item.get("version_id") is not None
                                else {}
                            ),
                        }
                    )
                elif item.get(legacy_key) is not None:
                    result.append({"source": "public", "asset_id": item[legacy_key]})
            return result

        manifest_capabilities = dict((version.manifest or {}).get("capabilities") or {})
        return cls.normalized_capabilities(
            {
                "skills": refs(version.skill_refs, "skill_id"),
                "mcps": refs(version.mcp_refs, "mcp_id"),
                "knowledge_bases": refs(version.knowledge_refs, "knowledge_base_id"),
                "plugin_ids": [
                    item.get("plugin_id")
                    for item in version.plugin_refs or []
                    if isinstance(item, dict) and item.get("plugin_id")
                ],
                "enterprise_knowledge_enabled": bool(
                    manifest_capabilities.get("enterprise_knowledge_enabled", False)
                ),
            }
        )

    @staticmethod
    def _scope_from_review(review: TemplateReview) -> ExpertShareScope:
        snapshot = dict(review.review_snapshot or {})
        scope = snapshot.get("approved_share_scope") or snapshot.get(
            "requested_share_scope"
        ) or {}
        return ExpertShareScope.model_validate(scope)

    @staticmethod
    def _requested_scope(profile: RuntimeAgentProfile) -> ExpertShareScope:
        config = dict(profile.capability_config or {})
        return ExpertShareScope.model_validate(config.get("requested_share_scope") or {})

    async def selected_share_users(
        self,
        *,
        org_id: int,
        user_ids: list[int],
    ) -> list[PersonalKnowledgeSelectedUser]:
        if not user_ids:
            return []
        rows = (
            await self.db.execute(
                select(User.id, User.name, User.user_code, User.status).where(
                    User.org_id == org_id,
                    User.id.in_(user_ids),
                )
            )
        ).all()
        users_by_id = {int(row.id): row for row in rows}
        result: list[PersonalKnowledgeSelectedUser] = []
        for user_id in user_ids:
            row = users_by_id.get(user_id)
            if row is None:
                result.append(PersonalKnowledgeSelectedUser(id=user_id, status="missing"))
                continue
            result.append(
                PersonalKnowledgeSelectedUser(
                    id=user_id,
                    name=str(row.name),
                    user_code=str(row.user_code) if row.user_code else None,
                    status="active" if row.status == "active" else "inactive",
                )
            )
        return result


__all__ = ["ExpertProfileQueryError", "ExpertProfileSummaryQueryService"]
