from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.deps import get_admin_principal
from app.database import get_db
from app.models import RuntimeAdmissionAudit
from app.schemas.admin import AdminRuntimeAdmissionAuditSummary
from app.services.admin_auth import AdminPrincipal


router = APIRouter(prefix="/runtime-admission-audits", tags=["admin-runtime-admission-audits"])


@router.get("", response_model=list[AdminRuntimeAdmissionAuditSummary])
async def list_runtime_admission_audits(
    runtime_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    client_message_id: str | None = None,
    run_id: str | None = None,
    error_code: str | None = None,
    outcome: str | None = None,
    from_at: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> list[AdminRuntimeAdmissionAuditSummary]:
    now = datetime.now(timezone.utc)
    lower = from_at or now - timedelta(days=1)
    upper = to or now
    if upper < lower or upper - lower > timedelta(days=31):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="audit query window must be between zero and 31 days",
        )
    statement = select(RuntimeAdmissionAudit).where(
        RuntimeAdmissionAudit.org_id == principal.org_id,
        RuntimeAdmissionAudit.occurred_at >= lower,
        RuntimeAdmissionAudit.occurred_at <= upper,
    )
    for column, value in (
        (RuntimeAdmissionAudit.runtime_id, runtime_id),
        (RuntimeAdmissionAudit.runtime_session_id, session_id),
        (RuntimeAdmissionAudit.request_id, request_id),
        (RuntimeAdmissionAudit.client_message_id, client_message_id),
        (RuntimeAdmissionAudit.runtime_run_id, run_id),
        (RuntimeAdmissionAudit.error_code, error_code),
    ):
        if value is not None:
            statement = statement.where(column == value)
    if outcome is None:
        statement = statement.where(
            RuntimeAdmissionAudit.outcome.in_(("rejected", "internal_error"))
        )
    else:
        statement = statement.where(RuntimeAdmissionAudit.outcome == outcome)
    result = await db.execute(
        statement.order_by(
            RuntimeAdmissionAudit.occurred_at.desc(),
            RuntimeAdmissionAudit.id.desc(),
        ).limit(limit)
    )
    return [_summary(row) for row in result.scalars().all()]


def _summary(row: RuntimeAdmissionAudit) -> AdminRuntimeAdmissionAuditSummary:
    return AdminRuntimeAdmissionAuditSummary(
        event_id=row.event_id,
        runtime_id=row.runtime_id,
        runtime_workspace_id=row.runtime_workspace_id,
        user_id=row.user_id,
        session_id=row.runtime_session_id,
        request_id=row.request_id,
        client_message_id=row.client_message_id,
        run_id=row.runtime_run_id,
        operation=row.operation,
        status=row.status,
        outcome=row.outcome,
        stage=row.stage,
        error_code=row.error_code,
        exception_type=row.exception_type,
        profile_id=row.profile_id,
        model_id=row.model_id,
        config_revision=row.config_revision,
        attachment_count=row.attachment_count,
        prompt_bytes=row.prompt_bytes,
        upstream_status=row.upstream_status,
        upstream_path=row.upstream_path,
        occurred_at=row.occurred_at,
        reported_at=row.created_at,
    )
