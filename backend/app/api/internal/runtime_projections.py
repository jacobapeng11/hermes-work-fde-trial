from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.runtime_internal import (
    RuntimeProjectionAck,
    RuntimeProjectionBatch,
)
from app.services.runtime_projection import (
    RuntimeProjectionAuthenticationError,
    RuntimeProjectionBindingError,
    RuntimeProjectionService,
)


router = APIRouter(tags=["internal-runtime-projections"])


def _bearer_token(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime projection authorization required",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime projection authorization required",
        )
    return token


@router.post(
    "/v1/runtime-projections",
    response_model=RuntimeProjectionAck,
)
async def receive_runtime_projections(
    batch: RuntimeProjectionBatch,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> RuntimeProjectionAck:
    service = RuntimeProjectionService(db)
    try:
        await service.authenticate(
            runtime_id=batch.runtime_id,
            binding_id=batch.binding_id,
            api_server_key=_bearer_token(authorization),
        )
    except RuntimeProjectionAuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime projection authorization failed",
        ) from exc
    except RuntimeProjectionBindingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Runtime projection binding is stale",
        ) from exc
    result = await service.project_batch(batch)
    return RuntimeProjectionAck(
        runtime_id=batch.runtime_id,
        accepted_projection_ids=result.accepted_projection_ids,
        rejected=result.rejected,
    )
