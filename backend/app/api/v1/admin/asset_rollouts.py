from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.deps import get_admin_principal
from app.database import get_db
from app.schemas.asset_rollout import (
    AssetRolloutBatchSummary,
    AssetRolloutDetail,
    AssetRolloutRetryResult,
)
from app.services.asset_rollout import AssetRolloutError, AssetRolloutService
from app.services.admin_auth import AdminPrincipal
from app.services.asset_rollout_queries import (
    AssetRolloutQueryError,
    AssetRolloutQueryService,
)


router = APIRouter(prefix="/asset-rollouts", tags=["admin-asset-rollouts"])


@router.get("", response_model=list[AssetRolloutBatchSummary])
async def list_asset_rollouts(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> list[AssetRolloutBatchSummary]:
    return await AssetRolloutQueryService(db).list_batches(
        org_id=principal.org_id,
        offset=offset,
        limit=limit,
    )


@router.get("/{rollout_id}", response_model=AssetRolloutDetail)
async def get_asset_rollout(
    rollout_id: str,
    target_status: str | None = None,
    user_id: int | None = None,
    profile_kind: str | None = None,
    error_code: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetRolloutDetail:
    try:
        return await AssetRolloutQueryService(db).detail(
            org_id=principal.org_id,
            rollout_id=rollout_id,
            status=target_status,
            user_id=user_id,
            profile_kind=profile_kind,
            error_code=error_code,
            offset=offset,
            limit=limit,
        )
    except AssetRolloutQueryError as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc


@router.post("/{rollout_id}/retry-failed", response_model=AssetRolloutRetryResult)
async def retry_failed_rollout_targets(
    rollout_id: str,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetRolloutRetryResult:
    try:
        count = await AssetRolloutService(db).retry_failed(
            org_id=principal.org_id,
            rollout_id=rollout_id,
        )
        await db.commit()
        return AssetRolloutRetryResult(
            rollout_id=rollout_id,
            queued_target_count=count,
        )
    except AssetRolloutError as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc


@router.post(
    "/{rollout_id}/targets/{target_id}/retry",
    response_model=AssetRolloutRetryResult,
)
async def retry_failed_rollout_target(
    rollout_id: str,
    target_id: int,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetRolloutRetryResult:
    try:
        count = await AssetRolloutService(db).retry_failed(
            org_id=principal.org_id,
            rollout_id=rollout_id,
            target_id=target_id,
        )
        await db.commit()
        return AssetRolloutRetryResult(
            rollout_id=rollout_id,
            queued_target_count=count,
        )
    except AssetRolloutError as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
