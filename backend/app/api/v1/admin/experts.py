from __future__ import annotations

from hashlib import sha256

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.deps import get_admin_principal
from app.database import get_db
from app.schemas.experts import (
    ApproveExpertReviewRequest,
    ExpertExecutionSecurityRequest,
    ExpertReviewSummary,
    RejectExpertReviewRequest,
)
from app.schemas.asset_rollout import AssetRolloutResult
from app.api.v1.user.user_directory import DirectoryUserProvisionRequest
from app.schemas.user_directory import DepartmentTreeNode, UserDirectorySearchItem
from app.services.admin_auth import AdminPrincipal
from app.services.expert_review_commands import (
    ExpertReviewCommandService,
)
from app.services.expert_errors import ExpertProfileError as ReviewCommandError
from app.services.expert_review_queries import ExpertReviewQueryError, ExpertReviewQueryService
from app.services.expert_security import ExpertSecurityError, ExpertSecurityService
from app.services.runtime_workspace import Principal
from app.services.user_directory import UserDirectoryService


router = APIRouter(prefix="/experts", tags=["admin-experts"])


def _raise(exc: ReviewCommandError | ExpertReviewQueryError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc


def _security_rollout_id(expert_package_id: int, idempotency_key: str) -> str:
    digest = sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"expert-security-{expert_package_id}-{digest}"


@router.get("", response_model=list[ExpertReviewSummary])
async def list_expert_reviews(
    review_status: str | None = None,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> list[ExpertReviewSummary]:
    return await ExpertReviewQueryService(db).list_reviews(
        org_id=principal.org_id,
        status=review_status,
    )


@router.get("/directory/users/search", response_model=list[UserDirectorySearchItem])
async def search_expert_directory_users(
    keyword: str = Query(default="", max_length=100),
    limit: int = Query(default=50, ge=1, le=50),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> list[UserDirectorySearchItem]:
    return await UserDirectoryService(db).search_users(
        principal=Principal(
            org_id=principal.org_id,
            user_id=principal.user_id,
        ),
        keyword=keyword,
        limit=limit,
    )


@router.post("/directory/users/provision", response_model=list[UserDirectorySearchItem])
async def provision_expert_directory_users(
    payload: DirectoryUserProvisionRequest,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> list[UserDirectorySearchItem]:
    service = UserDirectoryService(db)
    try:
        items = await service.provision_users_by_codes(
            principal=Principal(
                org_id=principal.org_id,
                user_id=principal.user_id,
            ),
            user_codes=payload.user_codes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return items


@router.get("/directory/departments/tree", response_model=list[DepartmentTreeNode])
async def get_expert_directory_department_tree(
    _principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> list[DepartmentTreeNode]:
    return await UserDirectoryService(db).department_tree()


@router.get("/reviews/{review_id}", response_model=ExpertReviewSummary)
async def get_expert_review(
    review_id: int,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> ExpertReviewSummary:
    try:
        return await ExpertReviewQueryService(db).get_review(
            org_id=principal.org_id,
            review_id=review_id,
        )
    except ExpertReviewQueryError as exc:
        _raise(exc)


@router.post("/reviews/{review_id}/approve", response_model=ExpertReviewSummary)
async def approve_expert_review(
    review_id: int,
    payload: ApproveExpertReviewRequest,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> ExpertReviewSummary:
    try:
        return await ExpertReviewCommandService(db).approve_review(
            org_id=principal.org_id,
            reviewer_user_id=principal.user_id,
            review_id=review_id,
            command=payload,
        )
    except ReviewCommandError as exc:
        _raise(exc)


@router.post("/reviews/{review_id}/reject", response_model=ExpertReviewSummary)
async def reject_expert_review(
    review_id: int,
    payload: RejectExpertReviewRequest,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> ExpertReviewSummary:
    try:
        return await ExpertReviewCommandService(db).reject_review(
            org_id=principal.org_id,
            reviewer_user_id=principal.user_id,
            review_id=review_id,
            review_comment=payload.review_comment,
        )
    except ReviewCommandError as exc:
        _raise(exc)


@router.post(
    "/packages/{expert_package_id}/unpublish",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def unpublish_expert_package(
    expert_package_id: int,
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        await ExpertReviewCommandService(db).admin_unpublish(
            org_id=principal.org_id,
            expert_package_id=expert_package_id,
        )
    except ReviewCommandError as exc:
        _raise(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/packages/{expert_package_id}/force-disable",
    response_model=AssetRolloutResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def force_disable_expert_package(
    expert_package_id: int,
    payload: ExpertExecutionSecurityRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetRolloutResult:
    try:
        result = await ExpertSecurityService(db).set_execution_enabled(
            org_id=principal.org_id,
            expert_package_id=expert_package_id,
            enabled=False,
            reason=payload.reason,
            rollout_id=_security_rollout_id(expert_package_id, idempotency_key),
            actor_user_id=principal.user_id,
        )
        await db.commit()
        return result
    except ExpertSecurityError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=exc.code) from exc


@router.post(
    "/packages/{expert_package_id}/enable",
    response_model=AssetRolloutResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def enable_expert_package(
    expert_package_id: int,
    payload: ExpertExecutionSecurityRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> AssetRolloutResult:
    try:
        result = await ExpertSecurityService(db).set_execution_enabled(
            org_id=principal.org_id,
            expert_package_id=expert_package_id,
            enabled=True,
            reason=payload.reason,
            rollout_id=_security_rollout_id(expert_package_id, idempotency_key),
            actor_user_id=principal.user_id,
        )
        await db.commit()
        return result
    except ExpertSecurityError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=exc.code) from exc
