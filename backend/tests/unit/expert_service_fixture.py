"""Explicit test composition for the split expert services.

This is test wiring only; production routes compose the same concrete services
directly and never import this module.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExpertPackage, ExpertPackageVersion, RuntimeAgentProfile
from app.services.expert_authoring_commands import ExpertAuthoringCommandService
from app.services.expert_capability_catalog import ExpertCapabilityCatalogQueryService
from app.services.expert_catalog import ExpertCatalogQueryService
from app.services.expert_installations import ExpertInstallationService
from app.services.expert_installations import ExpertInstallationError
from app.services.expert_catalog import ExpertCatalogError
from app.services.expert_errors import ExpertProfileError
from app.services.expert_profile_queries import ExpertProfileSummaryQueryService
from app.services.expert_review_commands import ExpertReviewCommandService
from app.services.runtime_workspace import Principal


class ExpertServiceFixture:
    """Compose split services for legacy behavior-focused unit scenarios."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.authoring = ExpertAuthoringCommandService(db)
        self.catalog = ExpertCatalogQueryService(db)
        self.capabilities = ExpertCapabilityCatalogQueryService(db)
        self.installations = ExpertInstallationService(db)
        self.reviews = ExpertReviewCommandService(db)
        self.summaries = ExpertProfileSummaryQueryService(db)

    async def create(self, **kwargs):
        return await self.authoring.create(**kwargs)

    async def update(self, **kwargs):
        return await self.authoring.update(**kwargs)

    async def save_configuration(self, **kwargs):
        return await self.authoring.save_configuration(**kwargs)

    async def replace_capabilities(self, **kwargs):
        return await self.authoring.replace_capabilities(**kwargs)

    async def replace_share_scope(self, **kwargs):
        return await self.authoring.replace_share_scope(**kwargs)

    async def submit_review(self, **kwargs):
        return await self.authoring.submit_review(**kwargs)

    async def copy_owned(self, **kwargs):
        return await self.authoring.copy_owned(**kwargs)

    async def unpublish_owned(self, **kwargs):
        return await self.authoring.unpublish_owned(**kwargs)

    async def list_market(self, **kwargs):
        return list((await self.catalog.market_page(**kwargs)).items)

    async def market_detail(self, **kwargs):
        try:
            return await self.catalog.market_detail(**kwargs)
        except ExpertCatalogError as exc:
            raise ExpertProfileError(exc.code) from exc

    async def list_mine(self, **kwargs):
        return await self.catalog.list_mine(**kwargs)

    async def list_runnable(self, **kwargs):
        return await self.catalog.list_runnable(**kwargs)

    async def list_inheritance_sources(self, **kwargs):
        return await self.catalog.list_inheritance_sources(**kwargs)

    async def get_profile(self, **kwargs):
        return await self.catalog.get_profile(**kwargs)

    async def capability_catalog(self, **kwargs):
        return await self.capabilities.list_for(**kwargs)

    async def list_admin_reviews(self, **kwargs):
        return await self.reviews.list_admin_reviews(**kwargs)

    async def approve_review(self, **kwargs):
        return await self.reviews.approve_review(**kwargs)

    async def reject_review(self, **kwargs):
        return await self.reviews.reject_review(**kwargs)

    async def admin_unpublish(self, **kwargs):
        return await self.reviews.admin_unpublish(**kwargs)

    async def install_market(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        expert_package_id: int,
    ):
        package = await self.db.get(ExpertPackage, expert_package_id)
        version = (
            await self.db.get(ExpertPackageVersion, package.current_version_id)
            if package is not None and package.current_version_id is not None
            else None
        )
        try:
            profile = await self.installations.install(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                expert_package_id=expert_package_id,
                operation_id=f"expert-install-{uuid4().hex}",
                capability_config=(
                    self.summaries.capabilities_from_version(version)
                    if version is not None
                    else None
                ),
            )
        except ExpertInstallationError as exc:
            raise ExpertProfileError(exc.code.lower()) from exc
        await self.db.commit()
        return await self.summaries.get_summary(profile)

    async def uninstall(self, **kwargs):
        await self.installations.remove(**kwargs)
        await self.db.commit()

    async def list_installed(self, *, principal: Principal, runtime_workspace_id: int):
        profiles = await self.db.scalars(
            select(RuntimeAgentProfile)
            .where(
                RuntimeAgentProfile.org_id == principal.org_id,
                RuntimeAgentProfile.user_id == principal.user_id,
                RuntimeAgentProfile.runtime_workspace_id == runtime_workspace_id,
                RuntimeAgentProfile.profile_kind == "expert",
                RuntimeAgentProfile.status == "active",
            )
            .order_by(RuntimeAgentProfile.updated_at.desc(), RuntimeAgentProfile.id.desc())
        )
        return [await self.summaries.get_summary(profile) for profile in profiles]

    async def _validate_capability_share_coverage(self, **kwargs):
        return await self.reviews._validate_capability_share_coverage(**kwargs)

    async def _inherited_capabilities(self, profile):
        return await self.authoring._inherited_capabilities(profile)


__all__ = ["ExpertServiceFixture"]
