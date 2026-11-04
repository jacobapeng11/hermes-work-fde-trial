from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExpertPackage
from app.schemas.asset_rollout import AssetRolloutCommand, AssetRolloutResult
from app.services.asset_rollout import AssetRolloutService


class ExpertSecurityError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExpertSecurityService:
    """Own emergency execution state and its durable Profile fan-out."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def set_execution_enabled(
        self,
        *,
        org_id: int,
        expert_package_id: int,
        enabled: bool,
        reason: str,
        rollout_id: str,
        actor_user_id: int,
    ) -> AssetRolloutResult:
        package = await self.db.scalar(
            select(ExpertPackage)
            .where(
                ExpertPackage.id == expert_package_id,
                ExpertPackage.org_id == org_id,
            )
            .with_for_update()
        )
        if package is None:
            raise ExpertSecurityError("EXPERT_PACKAGE_NOT_FOUND")
        target_status = "enabled" if enabled else "force_disabled"
        if str(package.execution_status) != target_status:
            package.execution_status = target_status
            package.execution_status_changed_at = datetime.utcnow()
        package.execution_status_reason = None if enabled else reason
        return await AssetRolloutService(self.db).create(
            AssetRolloutCommand(
                rollout_id=rollout_id,
                org_id=org_id,
                asset_type="expert",
                asset_id=expert_package_id,
                action="enable" if enabled else "revoke",
                source_version_id=(
                    int(package.current_version_id)
                    if package.current_version_id is not None
                    else None
                ),
                created_by_user_id=actor_user_id,
                change_summary=reason,
            )
        )


__all__ = ["ExpertSecurityError", "ExpertSecurityService"]
