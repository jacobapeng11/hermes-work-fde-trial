from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ExpertPackage,
    ExpertPackageVersion,
    ExpertVersionArtifact,
    InstalledExpertBinding,
    PlazaTemplate,
    RuntimeAgentProfile,
    TemplateVersion,
    User,
)
from app.schemas.experts import (
    ExpertAllowedActions,
    ExpertMarketCapabilitySummary,
    ExpertMarketDetail,
    ExpertMarketItem,
    RuntimeAgentProfileSummary,
)
from app.services.expert_visibility import ExpertVisibilityPolicy
from app.services.runtime_agent_profiles import (
    SqlAlchemyRuntimeAgentProfileStore,
    executable_runtime_agent_profile_clauses,
)
from app.services.runtime_workspace import Principal
from app.services.expert_profile_queries import ExpertProfileSummaryQueryService
from app.services.expert_errors import ExpertProfileError


@dataclass(frozen=True)
class ExpertCatalogPage:
    items: tuple[ExpertMarketItem, ...]
    next_cursor: str | None


class ExpertCatalogError(ExpertProfileError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class ExpertCatalogQueryService:
    """Read-only Mine/Market query model; never resolves private capabilities."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        visibility: ExpertVisibilityPolicy | None = None,
    ) -> None:
        self.db = db
        self.visibility = visibility or ExpertVisibilityPolicy(db)

    async def list_mine(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
    ) -> list[RuntimeAgentProfileSummary]:
        profiles = await self.list_mine_profiles(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
        )
        summaries = ExpertProfileSummaryQueryService(self.db)
        return [await summaries.get_summary(profile) for profile in profiles]

    async def list_runnable(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
    ) -> list[RuntimeAgentProfileSummary]:
        profiles = await self.db.scalars(
            select(RuntimeAgentProfile)
            .where(
                RuntimeAgentProfile.org_id == principal.org_id,
                RuntimeAgentProfile.user_id == principal.user_id,
                RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
                RuntimeAgentProfile.profile_kind == "expert",
                *executable_runtime_agent_profile_clauses(),
            )
            .order_by(RuntimeAgentProfile.display_name.asc(), RuntimeAgentProfile.id.asc())
        )
        summaries = ExpertProfileSummaryQueryService(self.db)
        return [await summaries.get_summary(profile) for profile in profiles]

    async def list_inheritance_sources(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
    ) -> list[RuntimeAgentProfileSummary]:
        records = await SqlAlchemyRuntimeAgentProfileStore(self.db).list_owned(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
        )
        summaries = ExpertProfileSummaryQueryService(self.db)
        result: list[RuntimeAgentProfileSummary] = []
        for record in records:
            profile = await self.db.get(RuntimeAgentProfile, record.runtime_agent_profile_id)
            if profile is not None:
                result.append(await summaries.get_summary(profile))
        return result

    async def get_profile(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
    ) -> RuntimeAgentProfileSummary:
        await SqlAlchemyRuntimeAgentProfileStore(self.db).require_owned(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            ready_only=False,
        )
        profile = await self.db.get(RuntimeAgentProfile, runtime_agent_profile_id)
        if profile is None:
            raise ExpertCatalogError("expert_profile_not_found", status_code=404)
        return await ExpertProfileSummaryQueryService(self.db).get_summary(profile)

    async def market_detail(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        expert_package_id: int,
    ) -> ExpertMarketDetail:
        page = await self.market_page(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
        )
        item = next(
            (candidate for candidate in page.items if candidate.expert_package_id == expert_package_id),
            None,
        )
        if item is None:
            raise ExpertCatalogError("market_expert_not_found", status_code=404)
        version = await self.db.get(
            ExpertPackageVersion,
            item.expert_package_version_id,
        )
        if (
            version is None
            or int(version.org_id) != principal.org_id
            or int(version.expert_package_id) != item.expert_package_id
        ):
            raise ExpertCatalogError("market_expert_not_found", status_code=404)

        artifact_rows = (
            await self.db.execute(
                select(
                    ExpertVersionArtifact.artifact_type,
                    ExpertVersionArtifact.name,
                    ExpertVersionArtifact.description,
                )
                .where(
                    ExpertVersionArtifact.org_id == principal.org_id,
                    ExpertVersionArtifact.expert_package_version_id == version.id,
                    ExpertVersionArtifact.status == "ready",
                    ExpertVersionArtifact.artifact_type.in_(
                        ("skill_package", "mcp_config", "knowledge_reference")
                    ),
                )
                .order_by(
                    ExpertVersionArtifact.artifact_type.asc(),
                    ExpertVersionArtifact.name.asc(),
                    ExpertVersionArtifact.id.asc(),
                )
            )
        ).all()
        grouped: dict[str, list[ExpertMarketCapabilitySummary]] = {
            "skill_package": [],
            "mcp_config": [],
            "knowledge_reference": [],
        }
        for artifact_type, name, description in artifact_rows:
            grouped[str(artifact_type)].append(
                ExpertMarketCapabilitySummary(
                    name=str(name),
                    description=str(description) if description is not None else None,
                )
            )

        return ExpertMarketDetail(
            **item.model_dump(exclude={"operation", "updated_at"}),
            soul_content=str(version.soul_snapshot or ""),
            system_prompt=(
                str(version.prompt_snapshot)
                if version.prompt_snapshot is not None
                else None
            ),
            skills=grouped["skill_package"],
            mcps=grouped["mcp_config"],
            knowledge_bases=grouped["knowledge_reference"],
        )

    async def list_mine_profiles(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
    ) -> tuple[RuntimeAgentProfile, ...]:
        rows = await self.db.scalars(
            select(RuntimeAgentProfile)
            .where(
                RuntimeAgentProfile.org_id == principal.org_id,
                RuntimeAgentProfile.user_id == principal.user_id,
                RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
                RuntimeAgentProfile.profile_kind == "expert",
                RuntimeAgentProfile.ownership_type == "self_created",
                RuntimeAgentProfile.status != "deleted",
            )
            .order_by(
                RuntimeAgentProfile.updated_at.desc(),
                RuntimeAgentProfile.id.desc(),
            )
        )
        return tuple(rows)

    async def market_page(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ExpertCatalogPage:
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        visible_rows = (
            await self.db.execute(
                select(ExpertPackage, ExpertPackageVersion)
                .join(
                    ExpertPackageVersion,
                    ExpertPackageVersion.id == ExpertPackage.current_version_id,
                )
                .join(
                    TemplateVersion,
                    TemplateVersion.source_expert_package_version_id
                    == ExpertPackageVersion.id,
                )
                .join(
                    PlazaTemplate,
                    PlazaTemplate.id == TemplateVersion.template_id,
                )
                .where(
                    ExpertPackage.org_id == principal.org_id,
                    ExpertPackage.status == "published",
                    ExpertPackageVersion.status.in_(("approved", "published")),
                    PlazaTemplate.status == "published",
                    TemplateVersion.status == "published",
                )
            )
        ).all()
        visible_candidates = {int(package.id) for package, _ in visible_rows}
        visible_ids = await self.visibility.visible_package_ids(
            principal=principal,
            package_ids=visible_candidates,
        )

        installed_rows = (
            await self.db.execute(
                select(
                    InstalledExpertBinding,
                    ExpertPackage,
                    ExpertPackageVersion,
                    RuntimeAgentProfile,
                )
                .join(
                    ExpertPackage,
                    ExpertPackage.id == InstalledExpertBinding.expert_package_id,
                )
                .join(
                    ExpertPackageVersion,
                    ExpertPackageVersion.id
                    == InstalledExpertBinding.expert_package_version_id,
                )
                .join(
                    RuntimeAgentProfile,
                    RuntimeAgentProfile.id
                    == InstalledExpertBinding.runtime_agent_profile_id,
                )
                .where(
                    InstalledExpertBinding.org_id == principal.org_id,
                    InstalledExpertBinding.user_id == principal.user_id,
                    InstalledExpertBinding.runtime_workspace_id
                    == runtime_workspace_id,
                    InstalledExpertBinding.status == "enabled",
                )
            )
        ).all()
        installed_by_package = {
            int(package.id): (binding, package, version, profile)
            for binding, package, version, profile in installed_rows
        }
        package_versions = {
            int(package.id): (package, version)
            for package, version in visible_rows
            if int(package.id) in visible_ids
        }
        for package_id, (_binding, package, version, _profile) in installed_by_package.items():
            package_versions[package_id] = (package, version)

        owner_ids = {int(package.owner_user_id) for package, _ in package_versions.values()}
        provider_names = {
            int(user_id): str(name)
            for user_id, name in (
                await self.db.execute(
                    select(User.id, User.name).where(User.id.in_(owner_ids))
                )
            ).all()
        }
        items: list[ExpertMarketItem] = []
        for package_id, (package, version) in package_versions.items():
            installed = installed_by_package.get(package_id)
            binding = installed[0] if installed else None
            profile = installed[3] if installed else None
            lifecycle = str(package.status)
            catalog_status = (
                "published"
                if lifecycle == "published"
                else "deleted"
                if lifecycle == "deleted"
                else "unlisted"
            )
            force_disabled = str(package.execution_status) == "force_disabled"
            executable = bool(
                profile is not None
                and profile.status == "active"
                and int(profile.desired_revision or 0)
                == int(profile.applied_revision or 0)
                and not force_disabled
            )
            items.append(
                ExpertMarketItem(
                    expert_package_id=package_id,
                    expert_package_version_id=int(version.id),
                    name=str(package.name),
                    description=package.description,
                    avatar_url=package.avatar_url,
                    provider_name=provider_names.get(
                        int(package.owner_user_id), "专家市场"
                    ),
                    version=int(version.version),
                    installed=binding is not None,
                    installed_runtime_agent_profile_id=(
                        int(profile.id) if profile is not None else None
                    ),
                    catalog_status=catalog_status,
                    execution_status=(
                        "force_disabled" if force_disabled else "enabled"
                    ),
                    allowed_actions=ExpertAllowedActions(
                        view_detail=True,
                        install=(
                            binding is None
                            and package_id in visible_ids
                            and lifecycle == "published"
                            and int(package.owner_user_id) != principal.user_id
                            and not force_disabled
                        ),
                        uninstall=binding is not None,
                        summon=executable,
                    ),
                    updated_at=package.updated_at,
                )
            )
        items.sort(
            key=lambda item: (
                not item.installed,
                item.name.casefold(),
                item.expert_package_id,
            )
        )
        start = int(cursor) if cursor else 0
        if start < 0:
            raise ValueError("cursor must be non-negative")
        page_items = items[start : start + limit]
        next_offset = start + len(page_items)
        return ExpertCatalogPage(
            items=tuple(page_items),
            next_cursor=str(next_offset) if next_offset < len(items) else None,
        )


__all__ = ["ExpertCatalogError", "ExpertCatalogPage", "ExpertCatalogQueryService"]
