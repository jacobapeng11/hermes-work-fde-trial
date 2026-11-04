from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.database import get_db
from app.schemas.enterprise_knowledge import (
    EnterpriseKnowledgeSettingsResponse,
    EnterpriseKnowledgeSettingsUpdate,
)
from app.services.enterprise_knowledge_settings import (
    EnterpriseKnowledgeSettingsService,
    EnterpriseKnowledgeWorkspaceNotFoundError,
)
from app.services.runtime_workspace import Principal


router = APIRouter(
    prefix="/runtime-workspace/{runtime_workspace_id}/enterprise-knowledge",
    tags=["user-enterprise-knowledge"],
)


def get_enterprise_knowledge_settings_service(
    db: AsyncSession = Depends(get_db),
) -> EnterpriseKnowledgeSettingsService:
    return EnterpriseKnowledgeSettingsService(db)


def _response(record) -> EnterpriseKnowledgeSettingsResponse:
    return EnterpriseKnowledgeSettingsResponse(
        runtime_workspace_id=record.runtime_workspace_id,
        enabled=record.enabled,
        mode=record.mode,
        updated_at=record.updated_at,
    )


@router.get("", response_model=EnterpriseKnowledgeSettingsResponse)
async def get_enterprise_knowledge_settings(
    runtime_workspace_id: int,
    principal: Principal = Depends(get_principal),
    service: EnterpriseKnowledgeSettingsService = Depends(get_enterprise_knowledge_settings_service),
) -> EnterpriseKnowledgeSettingsResponse:
    try:
        return _response(await service.get(principal, runtime_workspace_id))
    except EnterpriseKnowledgeWorkspaceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.put("", response_model=EnterpriseKnowledgeSettingsResponse)
async def update_enterprise_knowledge_settings(
    runtime_workspace_id: int,
    payload: EnterpriseKnowledgeSettingsUpdate,
    principal: Principal = Depends(get_principal),
    service: EnterpriseKnowledgeSettingsService = Depends(get_enterprise_knowledge_settings_service),
) -> EnterpriseKnowledgeSettingsResponse:
    try:
        return _response(
            await service.update(principal, runtime_workspace_id, enabled=payload.enabled)
        )
    except EnterpriseKnowledgeWorkspaceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
