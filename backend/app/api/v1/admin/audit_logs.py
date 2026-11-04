from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.deps import get_admin_principal
from app.config import settings
from app.database import get_db
from app.schemas.admin import AdminAuditLogSummary
from app.services.admin_audit import (
    AdminAuditLogRecord,
    AdminAuditLogService,
    InMemoryAdminAuditLogStore,
    SqlAlchemyAdminAuditLogStore,
)
from app.services.admin_auth import AdminPrincipal

router = APIRouter(prefix="/audit-logs", tags=["admin-audit-logs"])

_audit_store = InMemoryAdminAuditLogStore()


async def get_admin_audit_log_service(db: AsyncSession = Depends(get_db)) -> AdminAuditLogService:
    if settings.RUNTIME_CHAT_MODE == "real":
        return AdminAuditLogService(SqlAlchemyAdminAuditLogStore(db))
    return AdminAuditLogService(_audit_store)


def _audit_summary(record: AdminAuditLogRecord) -> AdminAuditLogSummary:
    return AdminAuditLogSummary(
        audit_log_id=record.audit_log_id,
        org_id=record.org_id,
        actor_user_id=record.actor_user_id,
        runtime_workspace_id=record.runtime_workspace_id,
        action=record.action,
        target_type=record.target_type,
        target_id=record.target_id,
        detail=record.detail,
        created_at=record.created_at,
    )


@router.get("", response_model=list[AdminAuditLogSummary])
async def list_admin_audit_logs(
    action: str | None = None,
    target_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> list[AdminAuditLogSummary]:
    return [
        _audit_summary(record)
        for record in await service.list_logs(
            principal=principal,
            action=action,
            target_type=target_type,
            limit=limit,
        )
    ]
