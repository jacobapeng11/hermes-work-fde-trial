"""Read-only administrator review query model."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExpertPackage, InstalledExpertBinding, PlazaTemplate, TemplateReview
from app.schemas.experts import ExpertCapabilitySelection, ExpertReviewSummary, ExpertShareScope
from app.services.expert_profile_queries import ExpertProfileSummaryQueryService


class ExpertReviewQueryError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class ExpertReviewQueryService:
    """Project immutable review snapshots without changing review state."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.profile_queries = ExpertProfileSummaryQueryService(db)

    async def list_reviews(
        self,
        *,
        org_id: int,
        status: str | None = None,
    ) -> list[ExpertReviewSummary]:
        clauses = [
            TemplateReview.org_id == org_id,
            or_(
                TemplateReview.status != "approved",
                PlazaTemplate.status == "published",
            ),
        ]
        if status:
            clauses.append(TemplateReview.status == status)
        reviews = (
            (
                await self.db.execute(
                    select(TemplateReview)
                    .outerjoin(PlazaTemplate, TemplateReview.template_id == PlazaTemplate.id)
                    .where(*clauses)
                    .order_by(TemplateReview.created_at.desc(), TemplateReview.id.desc())
                )
            )
            .scalars()
            .all()
        )
        result: list[ExpertReviewSummary] = []
        for review in reviews:
            if dict(review.review_snapshot or {}).get("schema_version") != "expert.review/v2":
                continue
            result.append(await self.get_summary(review))
        return result

    async def get_review(self, *, org_id: int, review_id: int) -> ExpertReviewSummary:
        review = await self.db.scalar(
            select(TemplateReview).where(
                TemplateReview.org_id == org_id,
                TemplateReview.id == review_id,
            )
        )
        if review is None or dict(review.review_snapshot or {}).get("schema_version") != "expert.review/v2":
            raise ExpertReviewQueryError("expert_review_not_found", status_code=404)
        return await self.get_summary(review)

    async def get_summary(self, review: TemplateReview) -> ExpertReviewSummary:
        snapshot = dict(review.review_snapshot or {})
        basic = dict(snapshot.get("basic_info") or {})
        scope = snapshot.get("approved_share_scope") or snapshot.get("requested_share_scope") or {}
        share_scope = ExpertShareScope.model_validate(scope)
        package = await self.db.get(ExpertPackage, int(review.expert_package_id))
        active_installation_count = int(
            await self.db.scalar(
                select(func.count(InstalledExpertBinding.id)).where(
                    InstalledExpertBinding.expert_package_id == review.expert_package_id,
                    InstalledExpertBinding.status == "enabled",
                )
            )
            or 0
        )
        return ExpertReviewSummary(
            review_id=int(review.id),
            expert_package_id=int(review.expert_package_id),
            expert_package_version_id=int(review.expert_package_version_id or 0),
            submitter_user_id=int(review.submitter_user_id),
            name=str(basic.get("name") or ""),
            description=basic.get("description"),
            avatar_url=basic.get("avatar_url"),
            soul_content=str(snapshot.get("soul") or ""),
            system_prompt=(
                str(snapshot["system_prompt"])
                if snapshot.get("system_prompt") is not None
                else None
            ),
            capabilities=ExpertCapabilitySelection.model_validate(
                snapshot.get("capabilities") or {}
            ),
            share_scope=share_scope,
            share_scope_users=await self.profile_queries.selected_share_users(
                org_id=int(review.org_id),
                user_ids=share_scope.user_ids,
            ),
            change_summary=(
                str(snapshot["change_summary"])
                if snapshot.get("change_summary") is not None
                else None
            ),
            status=str(review.status),
            review_comment=review.review_comment,
            execution_status=(str(package.execution_status) if package is not None else "enabled"),
            execution_status_reason=(
                package.execution_status_reason if package is not None else None
            ),
            active_installation_count=active_installation_count,
            created_at=review.created_at,
            reviewed_at=review.reviewed_at,
            updated_at=review.updated_at,
        )


__all__ = ["ExpertReviewQueryError", "ExpertReviewQueryService"]
