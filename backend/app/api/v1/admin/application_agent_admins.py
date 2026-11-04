from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.audit_logs import get_admin_audit_log_service
from app.api.v1.admin.deps import get_admin_principal
from app.api.v1.application_agent_role_deps import application_agent_admin_memory_store
from app.config import settings
from app.database import get_db
from app.schemas.application_agent_admin import (
    ApplicationAgentAdminCandidateSummary,
    ApplicationAgentAdminReplaceRequest,
    ApplicationAgentAdminSummary,
)
from app.services.admin_audit import AdminAuditLogService
from app.services.admin_auth import AdminPrincipal
from app.services.application_agent_admin import (
    ApplicationAgentAdminCandidate,
    ApplicationAgentAdminGrantError,
    ApplicationAgentAdminMembership,
    ApplicationAgentAdminService,
    SqlAlchemyApplicationAgentAdminStore,
)


router = APIRouter(prefix="/application-agent-admins", tags=["admin-application-agent-admins"])


async def get_application_agent_admin_service(
    db: AsyncSession = Depends(get_db),
) -> ApplicationAgentAdminService:
    if settings.RUNTIME_CHAT_MODE == "real" or settings.HERMES_AUTH_MODE in {"enterprise_idpAES", "oauth2"}:
        return ApplicationAgentAdminService(SqlAlchemyApplicationAgentAdminStore(db))
    return ApplicationAgentAdminService(application_agent_admin_memory_store)


def _candidate_summary(candidate: ApplicationAgentAdminCandidate) -> ApplicationAgentAdminCandidateSummary:
    return ApplicationAgentAdminCandidateSummary(
        user_id=candidate.user_id,
        user_code=candidate.user_code,
        name=candidate.name,
        department_name=candidate.department_name,
        status=candidate.status,
        is_application_agent_admin=candidate.is_application_agent_admin,
    )


def _member_summary(member: ApplicationAgentAdminMembership) -> ApplicationAgentAdminSummary:
    return ApplicationAgentAdminSummary(
        user_id=member.user_id,
        user_code=member.user_code,
        name=member.name,
        department_name=member.department_name,
        granted_by_user_id=member.granted_by_user_id,
        granted_at=member.granted_at,
    )


def _grant_error(exc: ApplicationAgentAdminGrantError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.code)


@router.get("/candidates", response_model=list[ApplicationAgentAdminCandidateSummary])
async def search_application_agent_admin_candidates(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=50, ge=1, le=50),
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationAgentAdminService = Depends(get_application_agent_admin_service),
) -> list[ApplicationAgentAdminCandidateSummary]:
    return [
        _candidate_summary(candidate)
        for candidate in await service.search_candidates(principal=principal, query=q, limit=limit)
    ]


@router.get("", response_model=list[ApplicationAgentAdminSummary])
async def list_application_agent_admins(
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationAgentAdminService = Depends(get_application_agent_admin_service),
) -> list[ApplicationAgentAdminSummary]:
    return [_member_summary(member) for member in await service.list_members(principal=principal)]


@router.put("", response_model=list[ApplicationAgentAdminSummary])
async def replace_application_agent_admins(
    payload: ApplicationAgentAdminReplaceRequest,
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: ApplicationAgentAdminService = Depends(get_application_agent_admin_service),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> list[ApplicationAgentAdminSummary]:
    try:
        result = await service.replace(principal=principal, user_codes=payload.user_codes)
    except ApplicationAgentAdminGrantError as exc:
        raise _grant_error(exc) from exc
    for member in result.granted:
        await audit.record(
            principal=principal,
            action="admin.application_agent_admin.granted",
            target_type="user",
            target_id=member.user_id,
            detail={"user_code": member.user_code},
        )
    for member in result.revoked:
        await audit.record(
            principal=principal,
            action="admin.application_agent_admin.revoked",
            target_type="user",
            target_id=member.user_id,
            detail={"user_code": member.user_code},
        )
    return [_member_summary(member) for member in result.members]
