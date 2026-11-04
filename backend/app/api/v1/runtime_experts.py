from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.runtime_experts import RuntimeExpertRosterResponse
from app.services.runtime_expert_roster import (
    RuntimeExpertRosterError,
    RuntimeExpertRosterService,
)


router = APIRouter(
    prefix="/runtime-experts/{runtime_workspace_id}",
    tags=["runtime-experts"],
)


def _bearer(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail={"code": "missing_runtime_token"},
        )
    return token


@router.get("/roster", response_model=RuntimeExpertRosterResponse)
async def runtime_expert_roster(
    runtime_workspace_id: int,
    runtime_agent_profile_id: int | None = Query(default=None, ge=1),
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> RuntimeExpertRosterResponse:
    try:
        return await RuntimeExpertRosterService(db).roster(
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            runtime_token=_bearer(authorization),
        )
    except RuntimeExpertRosterError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code},
        ) from exc
