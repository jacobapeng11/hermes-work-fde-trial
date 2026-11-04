from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExpertPackage, ExpertPackageVersion, TemplateReview
from app.schemas.asset_rollout import AssetRolloutCommand
from app.services.asset_rollout import AssetRolloutService
from app.services.runtime_agent_profiles import normalize_profile_display_name


class ExpertReviewError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExpertReviewService:
    """Own immutable review and approved-market state transitions only."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    @staticmethod
    def mark_pending(package: ExpertPackage) -> None:
        # An update under review must not hide the previously approved version.
        package.status = (
            "published" if package.current_version_id is not None else "review_pending"
        )

    async def acquire_market_name(
        self,
        *,
        package: ExpertPackage,
        version: ExpertPackageVersion,
    ) -> None:
        name = str((version.basic_info_snapshot or {}).get("name") or package.name)
        try:
            async with self.db.begin_nested():
                package.market_name_key = normalize_profile_display_name(name)
                await self.db.flush()
        except IntegrityError as exc:
            raise ExpertReviewError("EXPERT_MARKET_NAME_CONFLICT") from exc

    async def approve(
        self,
        *,
        package: ExpertPackage,
        version: ExpertPackageVersion,
        review: TemplateReview,
        reviewer_user_id: int,
        review_comment: str | None,
    ) -> None:
        previous_version_id = package.current_version_id
        package.status = "published"
        package.visibility = "shared"
        package.current_version_id = int(version.id)
        version.status = "published"
        review.status = "approved"
        review.reviewer_user_id = reviewer_user_id
        review.review_comment = review_comment
        review.reviewed_at = datetime.now(timezone.utc)
        await AssetRolloutService(self.db).create(
            AssetRolloutCommand(
                rollout_id=f"expert-{int(package.id)}-version-{int(version.id)}",
                org_id=int(package.org_id),
                asset_type="expert",
                asset_id=int(package.id),
                action="publish",
                source_version_id=(
                    int(previous_version_id)
                    if previous_version_id is not None
                    else None
                ),
                target_version_id=int(version.id),
                created_by_user_id=reviewer_user_id,
                change_summary=review_comment,
            )
        )

    @staticmethod
    def reject(
        *,
        package: ExpertPackage | None,
        version: ExpertPackageVersion | None,
        review: TemplateReview,
        reviewer_user_id: int,
        review_comment: str,
    ) -> None:
        review.status = "rejected"
        review.reviewer_user_id = reviewer_user_id
        review.review_comment = review_comment.strip()
        review.reviewed_at = datetime.now(timezone.utc)
        if version is not None:
            version.status = "rejected"
        if package is not None and package.status == "review_pending":
            package.status = (
                "published" if package.current_version_id is not None else "active"
            )


__all__ = ["ExpertReviewError", "ExpertReviewService"]
