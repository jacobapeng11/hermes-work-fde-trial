from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.internal.runtime_projections import _bearer_token
from app.database import get_db
from app.schemas.runtime_internal import (
    RuntimeAdmissionAuditAck,
    RuntimeAdmissionAuditBatch,
)
from app.services.runtime_admission_audit import (
    RuntimeAdmissionAuditService,
    RuntimeProjectionAuthenticationError,
    RuntimeProjectionBindingError,
)


router = APIRouter(tags=["internal-runtime-admission-audits"])


@router.post("/v1/runtime-admission-audits", response_model=RuntimeAdmissionAuditAck)
async def receive_runtime_admission_audits(
    batch: RuntimeAdmissionAuditBatch,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> RuntimeAdmissionAuditAck:
    service = RuntimeAdmissionAuditService(db)
    try:
        await service.authenticate(
            runtime_id=batch.runtime_id,
            binding_id=batch.binding_id,
            api_server_key=_bearer_token(authorization),
        )
    except RuntimeProjectionAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime admission audit authorization failed",
        ) from exc
    except RuntimeProjectionBindingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Runtime admission audit binding is stale",
        ) from exc
    result = await service.record_batch(batch)
    return RuntimeAdmissionAuditAck(
        runtime_id=batch.runtime_id,
        accepted_event_ids=result.accepted_event_ids,
        duplicate_event_ids=result.duplicate_event_ids,
        rejected_event_ids=result.rejected_event_ids,
    )
