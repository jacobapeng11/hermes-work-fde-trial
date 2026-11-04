from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.database import get_db
from app.im_gateway.identity import OrganizationWeComIdentityService, WeComIdentityError
from app.schemas.auth import WeComBindRequest
from app.services.runtime_workspace import Principal


router = APIRouter(prefix="/wecom-bind", tags=["user-wecom-bind"])


@router.post("")
async def bind_wecom_identity(
    payload: WeComBindRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    try:
        await OrganizationWeComIdentityService(db).bind_qw_token(
            principal=principal,
            qw_token=payload.qw_token,
        )
    except WeComIdentityError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    return {"status": "bound", "qw_token": payload.qw_token.strip()}
