from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.database import get_db
from app.schemas.runtime_edge import (
    RuntimeEdgeBootstrapResponse,
    RuntimeEdgeGrantRequest,
    RuntimeEdgeSessionGrantResponse,
)
from app.services.runtime_chat import RuntimeChatPreparationError
from app.services.runtime_edge_grants import (
    RuntimeEdgeGrantError,
    RuntimeEdgeGrantService,
)
from app.services.runtime_workspace import Principal


router = APIRouter(prefix="/runtime-edge", tags=["user-runtime-edge"])


def _trusted_origin(request: Request) -> str:
    origin = str(request.headers.get("origin") or "").rstrip("/")
    allowed = {
        settings.PUBLIC_WEB_BASE.rstrip("/"),
        *(item.rstrip("/") for item in settings.cors_origins),
    }
    if not origin:
        if settings.APP_ENV.lower() == "production":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Runtime Edge Origin is required",
            )
        return settings.PUBLIC_WEB_BASE.rstrip("/")
    if origin not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Runtime Edge Origin is not allowed",
        )
    return origin


@router.post("/prepare", response_model=RuntimeEdgeBootstrapResponse)
async def prepare_runtime_edge(
    payload: RuntimeEdgeGrantRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeEdgeBootstrapResponse:
    service = RuntimeEdgeGrantService(db)
    try:
        target = await service.target(principal=principal, request=payload)
    except (RuntimeChatPreparationError, RuntimeEdgeGrantError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return service.issue_bootstrap(
        principal=principal,
        target=target,
        allowed_origin=_trusted_origin(request),
    )


@router.post(
    "/refresh-grant",
    response_model=RuntimeEdgeSessionGrantResponse,
)
async def refresh_runtime_edge_grant(
    payload: RuntimeEdgeGrantRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeEdgeSessionGrantResponse:
    service = RuntimeEdgeGrantService(db)
    try:
        target = await service.target(principal=principal, request=payload)
    except (RuntimeChatPreparationError, RuntimeEdgeGrantError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return service.issue_session_grant(
        principal=principal,
        target=target,
    )
