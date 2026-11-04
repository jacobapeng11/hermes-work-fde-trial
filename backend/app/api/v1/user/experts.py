from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.database import get_db
from app.models import ExpertPackage, ExpertPackageVersion
from app.schemas.experts import (
    CreateExpertRequest,
    ExpertCapabilityCatalog,
    ExpertMarketDetail,
    ExpertMarketItem,
    ExpertWorkspaceRequest,
    RuntimeAgentProfileSummary,
    SubmitExpertReviewRequest,
    UpdateExpertCapabilitiesRequest,
    UpdateExpertConfigurationRequest,
    UpdateExpertPermissionsRequest,
    UpdateExpertRequest,
)
from app.services.expert_catalog import ExpertCatalogError, ExpertCatalogQueryService
from app.services.expert_capability_catalog import ExpertCapabilityCatalogQueryService
from app.services.expert_authoring_commands import (
    ExpertAuthoringCommandService,
)
from app.services.expert_errors import ExpertProfileError as AuthoringProfileError
from app.services.expert_installations import ExpertInstallationError, ExpertInstallationService
from app.services.expert_rollout import ExpertRolloutError
from app.services.expert_profile_queries import ExpertProfileQueryError
from app.services.expert_profile_queries import ExpertProfileSummaryQueryService
from app.services.runtime_workspace import Principal


router = APIRouter(prefix="/experts", tags=["user-experts"])


def _http_error(
    exc: AuthoringProfileError
    | ExpertInstallationError
    | ExpertRolloutError
    | ExpertCatalogError
    | ExpertProfileQueryError,
) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.code)


@router.get("/market", response_model=list[ExpertMarketItem])
async def list_expert_market(
    runtime_workspace_id: int,
    response: Response,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> list[ExpertMarketItem]:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return list(
            (
                await ExpertCatalogQueryService(db).market_page(
                    principal=principal,
                    runtime_workspace_id=runtime_workspace_id,
                )
            ).items
        )
    except (ExpertCatalogError, ExpertProfileQueryError) as exc:
        raise _http_error(exc) from exc


@router.get("/market/{expert_package_id}", response_model=ExpertMarketDetail)
async def get_market_expert_detail(
    expert_package_id: int,
    runtime_workspace_id: int,
    response: Response,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> ExpertMarketDetail:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await ExpertCatalogQueryService(db).market_detail(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            expert_package_id=expert_package_id,
        )
    except (ExpertCatalogError, ExpertProfileQueryError) as exc:
        raise _http_error(exc) from exc


@router.get("/mine", response_model=list[RuntimeAgentProfileSummary])
async def list_my_experts(
    runtime_workspace_id: int,
    response: Response,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> list[RuntimeAgentProfileSummary]:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await ExpertCatalogQueryService(db).list_mine(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
        )
    except (ExpertCatalogError, ExpertProfileQueryError) as exc:
        raise _http_error(exc) from exc


@router.get("/runnable", response_model=list[RuntimeAgentProfileSummary])
async def list_runnable_experts(
    runtime_workspace_id: int,
    response: Response,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> list[RuntimeAgentProfileSummary]:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await ExpertCatalogQueryService(db).list_runnable(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
        )
    except (ExpertCatalogError, ExpertProfileQueryError) as exc:
        raise _http_error(exc) from exc


@router.get(
    "/inheritance-sources",
    response_model=list[RuntimeAgentProfileSummary],
)
async def list_expert_inheritance_sources(
    runtime_workspace_id: int,
    response: Response,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> list[RuntimeAgentProfileSummary]:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await ExpertCatalogQueryService(db).list_inheritance_sources(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
        )
    except (ExpertCatalogError, ExpertProfileQueryError) as exc:
        raise _http_error(exc) from exc


@router.get(
    "/capability-catalog",
    response_model=ExpertCapabilityCatalog,
)
async def get_expert_capability_catalog(
    response: Response,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> ExpertCapabilityCatalog:
    response.headers["Cache-Control"] = "private, no-store"
    return await ExpertCapabilityCatalogQueryService(db).list_for(principal=principal)


@router.post(
    "",
    response_model=RuntimeAgentProfileSummary,
    status_code=status.HTTP_201_CREATED,
)
async def create_expert(
    payload: CreateExpertRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeAgentProfileSummary:
    try:
        return await ExpertAuthoringCommandService(db).create(
            principal=principal,
            command=payload,
        )
    except AuthoringProfileError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{runtime_agent_profile_id}",
    response_model=RuntimeAgentProfileSummary,
)
async def get_expert(
    runtime_agent_profile_id: int,
    runtime_workspace_id: int,
    response: Response,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeAgentProfileSummary:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await ExpertCatalogQueryService(db).get_profile(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
    except (ExpertCatalogError, ExpertProfileQueryError) as exc:
        raise _http_error(exc) from exc


@router.patch(
    "/{runtime_agent_profile_id}",
    response_model=RuntimeAgentProfileSummary,
)
async def update_expert(
    runtime_agent_profile_id: int,
    runtime_workspace_id: int,
    payload: UpdateExpertRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeAgentProfileSummary:
    try:
        return await ExpertAuthoringCommandService(db).update(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            command=payload,
        )
    except AuthoringProfileError as exc:
        raise _http_error(exc) from exc


@router.put(
    "/{runtime_agent_profile_id}/configuration",
    response_model=RuntimeAgentProfileSummary,
)
async def save_expert_configuration(
    runtime_agent_profile_id: int,
    runtime_workspace_id: int,
    payload: UpdateExpertConfigurationRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeAgentProfileSummary:
    try:
        return await ExpertAuthoringCommandService(db).save_configuration(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            command=payload,
        )
    except AuthoringProfileError as exc:
        raise _http_error(exc) from exc


@router.put(
    "/{runtime_agent_profile_id}/capabilities",
    response_model=RuntimeAgentProfileSummary,
)
async def replace_expert_capabilities(
    runtime_agent_profile_id: int,
    runtime_workspace_id: int,
    payload: UpdateExpertCapabilitiesRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeAgentProfileSummary:
    try:
        return await ExpertAuthoringCommandService(db).replace_capabilities(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            capabilities=payload.capabilities,
            change_summary=payload.change_summary,
        )
    except AuthoringProfileError as exc:
        raise _http_error(exc) from exc


@router.put(
    "/{runtime_agent_profile_id}/permissions",
    response_model=RuntimeAgentProfileSummary,
)
async def replace_expert_permissions(
    runtime_agent_profile_id: int,
    runtime_workspace_id: int,
    payload: UpdateExpertPermissionsRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeAgentProfileSummary:
    try:
        return await ExpertAuthoringCommandService(db).replace_share_scope(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            share_scope=payload.share_scope,
            change_summary=payload.change_summary,
        )
    except AuthoringProfileError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/{runtime_agent_profile_id}/submit-review",
    response_model=RuntimeAgentProfileSummary,
)
async def submit_expert_review(
    runtime_agent_profile_id: int,
    payload: SubmitExpertReviewRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeAgentProfileSummary:
    try:
        return await ExpertAuthoringCommandService(db).submit_review(
            principal=principal,
            runtime_workspace_id=payload.runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            share_scope=payload.share_scope,
            change_summary=payload.change_summary,
        )
    except AuthoringProfileError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/market/{expert_package_id}/install",
    response_model=RuntimeAgentProfileSummary,
    status_code=status.HTTP_201_CREATED,
)
async def install_market_expert(
    expert_package_id: int,
    payload: ExpertWorkspaceRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeAgentProfileSummary:
    try:
        package = await db.get(ExpertPackage, expert_package_id)
        version = (
            await db.get(ExpertPackageVersion, package.current_version_id)
            if package is not None and package.current_version_id is not None
            else None
        )
        profile = await ExpertInstallationService(db).install(
            principal=principal,
            runtime_workspace_id=payload.runtime_workspace_id,
            expert_package_id=expert_package_id,
            operation_id=f"expert-install-{uuid4().hex}",
            capability_config=(
                ExpertProfileSummaryQueryService.capabilities_from_version(version)
                if version is not None
                else None
            ),
        )
        await db.commit()
        return await ExpertProfileSummaryQueryService(db).get_summary(profile)
    except (ExpertInstallationError, ExpertRolloutError) as exc:
        await db.rollback()
        code = exc.code.lower()
        raise _http_error(
            AuthoringProfileError(
                code,
                status_code=404 if exc.code == "EXPERT_MARKET_ITEM_NOT_FOUND" else 409,
            )
        ) from exc
    except (AuthoringProfileError, ExpertInstallationError) as exc:
        raise _http_error(exc) from exc


@router.post(
    "/{runtime_agent_profile_id}/copy",
    response_model=RuntimeAgentProfileSummary,
    status_code=status.HTTP_201_CREATED,
)
async def copy_expert(
    runtime_agent_profile_id: int,
    payload: ExpertWorkspaceRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeAgentProfileSummary:
    try:
        return await ExpertAuthoringCommandService(db).copy_owned(
            principal=principal,
            runtime_workspace_id=payload.runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
    except AuthoringProfileError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/{runtime_agent_profile_id}/unpublish",
    response_model=RuntimeAgentProfileSummary,
)
async def unpublish_expert(
    runtime_agent_profile_id: int,
    payload: ExpertWorkspaceRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeAgentProfileSummary:
    try:
        return await ExpertAuthoringCommandService(db).unpublish_owned(
            principal=principal,
            runtime_workspace_id=payload.runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
    except AuthoringProfileError as exc:
        raise _http_error(exc) from exc


@router.delete(
    "/{runtime_agent_profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def uninstall_expert(
    runtime_agent_profile_id: int,
    runtime_workspace_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        await ExpertInstallationService(db).remove(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
        )
        await db.commit()
    except ExpertInstallationError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
