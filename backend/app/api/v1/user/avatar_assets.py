from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.database import get_db
from app.schemas.avatar_assets import AvatarAssetSummary
from app.services.avatar_assets import AvatarAssetError, AvatarAssetService
from app.services.runtime_workspace import Principal


router = APIRouter(prefix="/avatar-assets", tags=["user-avatar-assets"])


def get_avatar_asset_service(
    db: AsyncSession = Depends(get_db),
) -> AvatarAssetService:
    return AvatarAssetService(db)


@router.get("", response_model=list[AvatarAssetSummary])
async def list_avatar_assets(
    principal: Principal = Depends(get_principal),
    service: AvatarAssetService = Depends(get_avatar_asset_service),
) -> list[AvatarAssetSummary]:
    return await service.list_available(principal=principal)


@router.post(
    "",
    response_model=AvatarAssetSummary,
    status_code=status.HTTP_201_CREATED,
)
async def upload_avatar_asset(
    file: UploadFile = File(...),
    principal: Principal = Depends(get_principal),
    service: AvatarAssetService = Depends(get_avatar_asset_service),
) -> AvatarAssetSummary:
    content = await file.read(settings_limit_bytes() + 1)
    try:
        return await service.upload(
            principal=principal,
            filename=file.filename or "avatar",
            content_type=file.content_type,
            content=content,
        )
    except AvatarAssetError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc


def settings_limit_bytes() -> int:
    from app.config import settings

    return settings.AVATAR_MAX_UPLOAD_MB * 1024 * 1024
