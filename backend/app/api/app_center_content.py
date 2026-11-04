from __future__ import annotations

from zipfile import BadZipFile

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.app_center import AppCenterError, AppCenterService
from app.services.app_center_content_policy import (
    with_platform_return_link as _with_platform_return_link,
)
from app.services.app_center_packages import (
    normalize_package_content_path,
    read_package_entry,
)


router = APIRouter(prefix="/app-center", tags=["app-center-content"])


def get_content_service(db: AsyncSession = Depends(get_db)) -> AppCenterService:
    return AppCenterService(db)


@router.get("/{app_id}/{path:path}")
async def get_app_center_content(
    app_id: int,
    path: str,
    request: Request,
    service: AppCenterService = Depends(get_content_service),
) -> Response:
    normalized = normalize_package_content_path(path)
    if normalized is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        package = await service.public_package(app_id=app_id)
        content, media_type = read_package_entry(package, normalized)
    except AppCenterError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "error_code": exc.error_code,
                "message": exc.message,
            },
        ) from exc
    except (BadZipFile, KeyError, RuntimeError, OSError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    csp = "sandbox allow-scripts"
    if media_type.startswith("text/html") and request.query_params.get("embed") != "thumbnail":
        content = _with_platform_return_link(content)
        csp += " allow-top-navigation-by-user-activation"

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": csp,
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
