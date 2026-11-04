from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.deps import get_admin_principal
from app.api.v1.admin.audit_logs import get_admin_audit_log_service
from app.config import settings
from app.database import get_db
from app.schemas.admin import AdminModelStatusRequest, AdminModelSummary, AdminModelUpdateResponse
from app.services.admin_audit import AdminAuditLogService
from app.services.admin_auth import AdminPrincipal
from app.services.admin_models import (
    AdminModelGovernanceError,
    AdminModelGovernanceService,
    AdminModelRecord,
    AdminModelUpdateIntent,
    InMemoryAdminModelGovernanceStore,
    SqlAlchemyAdminModelGovernanceStore,
)
from app.services.model_catalog_cache import ModelCacheInvalidationEvent, RedisModelCatalogCache, shared_model_catalog_cache

router = APIRouter(prefix="/models", tags=["admin-models"])

_model_store = InMemoryAdminModelGovernanceStore()


async def get_admin_model_governance_service(db: AsyncSession = Depends(get_db)) -> AdminModelGovernanceService:
    if settings.RUNTIME_CHAT_MODE == "real":
        return AdminModelGovernanceService(SqlAlchemyAdminModelGovernanceStore(db))
    return AdminModelGovernanceService(_model_store)


def _model_summary(record: AdminModelRecord) -> AdminModelSummary:
    return AdminModelSummary(
        model_entry_id=record.model_entry_id,
        model_key=record.model_key,
        display_name=record.display_name,
        provider_id=record.provider_id,
        provider_display_name=record.provider_display_name,
        status=record.status,
        is_default=record.is_default,
        is_restricted=record.is_restricted,
        context_length=record.context_length,
        tags=record.tags,
    )


def _update_summary(intent: AdminModelUpdateIntent) -> AdminModelUpdateResponse:
    return AdminModelUpdateResponse(
        model_entry_id=intent.model_entry_id,
        status=intent.status,
        is_default=intent.is_default,
        upgrade_log_id=intent.upgrade_log_id,
        affected_runtime_workspace_count=intent.affected_runtime_workspace_count,
    )


@router.get("", response_model=list[AdminModelSummary])
async def list_admin_models(
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminModelGovernanceService = Depends(get_admin_model_governance_service),
) -> list[AdminModelSummary]:
    return [_model_summary(record) for record in await service.list_models(principal)]


@router.put("/{model_entry_id}/status", response_model=AdminModelUpdateResponse)
async def set_admin_model_status(
    model_entry_id: int,
    payload: AdminModelStatusRequest,
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminModelGovernanceService = Depends(get_admin_model_governance_service),
    audit_service: AdminAuditLogService = Depends(get_admin_audit_log_service),
    model_cache: RedisModelCatalogCache = Depends(shared_model_catalog_cache),
) -> AdminModelUpdateResponse:
    try:
        intent = await service.set_model_status(
            principal=principal,
            model_entry_id=model_entry_id,
            status=payload.status,
        )
        await audit_service.record(
            principal=principal,
            action="admin.model.status_updated",
            target_type="model_entry",
            target_id=model_entry_id,
            detail={
                "status": intent.status,
                "is_default": intent.is_default,
                "upgrade_log_id": intent.upgrade_log_id,
                "affected_runtime_workspace_count": intent.affected_runtime_workspace_count,
            },
        )
        if settings.RUNTIME_CHAT_MODE == "real":
            try:
                await model_cache.invalidate(
                    ModelCacheInvalidationEvent.model_status_changed(
                        org_id=principal.org_id,
                        model_entry_id=model_entry_id,
                    )
                )
            except RedisError:
                # Database state is authoritative; TTL remains the fallback
                # when Redis is temporarily unavailable after a committed update.
                pass
        return _update_summary(intent)
    except AdminModelGovernanceError as exc:
        status_code = status.HTTP_404_NOT_FOUND if "not found" in str(exc) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.put("/{model_entry_id}/default", response_model=AdminModelUpdateResponse)
async def set_admin_default_model(
    model_entry_id: int,
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminModelGovernanceService = Depends(get_admin_model_governance_service),
    audit_service: AdminAuditLogService = Depends(get_admin_audit_log_service),
    model_cache: RedisModelCatalogCache = Depends(shared_model_catalog_cache),
) -> AdminModelUpdateResponse:
    try:
        intent = await service.set_default_model(
            principal=principal,
            model_entry_id=model_entry_id,
        )
        await audit_service.record(
            principal=principal,
            action="admin.model.default_updated",
            target_type="model_entry",
            target_id=model_entry_id,
            detail={
                "status": intent.status,
                "is_default": intent.is_default,
                "upgrade_log_id": intent.upgrade_log_id,
                "affected_runtime_workspace_count": intent.affected_runtime_workspace_count,
            },
        )
        if settings.RUNTIME_CHAT_MODE == "real":
            try:
                await model_cache.invalidate(
                    ModelCacheInvalidationEvent.model_status_changed(
                        org_id=principal.org_id,
                        model_entry_id=model_entry_id,
                    )
                )
            except RedisError:
                pass
        return _update_summary(intent)
    except AdminModelGovernanceError as exc:
        status_code = status.HTTP_404_NOT_FOUND if "not found" in str(exc) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
