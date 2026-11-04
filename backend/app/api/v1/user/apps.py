from __future__ import annotations

import json
import uuid
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_platform_session_store
from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.database import get_db
from app.schemas.app_center import (
    AppCenterBrowserLaunchResponse,
    AppCenterCreateRequest,
    AppCenterListItem,
    AppCenterPackageUpdateRequest,
    AppCenterPermissionsRequest,
    AppCenterUploadMetadata,
)
from app.services.app_center import AppCenterError, AppCenterService
from app.services.platform_session import PlatformSessionStore, PlatformSessionUnavailable
from app.services.runtime_workspace import Principal


router = APIRouter(prefix="/apps", tags=["user-app-center"])


def get_app_center_service(
    db: AsyncSession = Depends(get_db),
) -> AppCenterService:
    return AppCenterService(db)


def _error(exc: Exception) -> None:
    if isinstance(exc, AppCenterError):
        code = exc.code
        error_code = exc.error_code
        message = exc.message
        http_status = exc.status_code
    else:
        code = "APP_CENTER_OPERATION_FAILED"
        error_code = -14007
        message = "应用操作失败，请稍后重试"
        http_status = 500
    raise HTTPException(
        status_code=http_status,
        detail={
            "code": code,
            "error_code": error_code,
            "message": message,
            "request_id": uuid.uuid4().hex,
        },
    ) from exc


@router.post("", response_model=AppCenterListItem, status_code=status.HTTP_201_CREATED)
async def create_app(
    payload: AppCenterCreateRequest,
    principal: Principal = Depends(get_principal),
    service: AppCenterService = Depends(get_app_center_service),
) -> AppCenterListItem:
    try:
        return await service.create_from_html(
            principal=principal,
            name=payload.name,
            description=payload.description,
            html_content=payload.html_content,
            share_mode=payload.share_mode,
            scope=payload.scope,
            idempotency_key=payload.idempotency_key,
            source_session_id=payload.source_session_id,
            source_run_id=payload.source_run_id,
        )
    except AppCenterError as exc:
        _error(exc)


@router.post(
    "/upload",
    response_model=AppCenterListItem,
    status_code=status.HTTP_201_CREATED,
)
async def upload_app(
    metadata: str = Form(...),
    file: UploadFile = File(...),
    principal: Principal = Depends(get_principal),
    service: AppCenterService = Depends(get_app_center_service),
) -> AppCenterListItem:
    try:
        command = AppCenterUploadMetadata.model_validate_json(metadata)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "APP_CENTER_PACKAGE_INVALID",
                "error_code": -14001,
                "message": "应用上传参数无效",
                "request_id": uuid.uuid4().hex,
            },
        ) from exc
    content = await file.read(settings.APP_CENTER_MAX_PACKAGE_BYTES + 1)
    try:
        return await service.create_from_package(
            principal=principal,
            name=command.name,
            description=command.description,
            package=content,
            share_mode=command.share_mode,
            scope=command.scope,
            idempotency_key=command.idempotency_key,
            source_session_id=command.source_session_id,
            source_run_id=command.source_run_id,
        )
    except AppCenterError as exc:
        _error(exc)


@router.get("", response_model=list[AppCenterListItem])
async def list_apps(
    response: Response,
    principal: Principal = Depends(get_principal),
    service: AppCenterService = Depends(get_app_center_service),
) -> list[AppCenterListItem]:
    response.headers["Cache-Control"] = "private, no-store"
    return await service.list_for_user(principal=principal)


@router.post(
    "/{app_id}/browser-launch",
    response_model=AppCenterBrowserLaunchResponse,
)
async def create_app_browser_launch(
    app_id: int,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: AppCenterService = Depends(get_app_center_service),
    session_store: PlatformSessionStore = Depends(get_platform_session_store),
) -> AppCenterBrowserLaunchResponse:
    try:
        await service.require_consumer_access(principal=principal, app_id=app_id)
        ticket = await session_store.create_data_app_launch(
            principal,
            app_id=app_id,
            ttl_seconds=settings.APP_CENTER_LAUNCH_TICKET_TTL_SECONDS,
        )
    except AppCenterError as exc:
        _error(exc)
    except PlatformSessionUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "session_store_unavailable",
                "error_code": -10000,
                "message": "认证服务暂时不可用，请稍后重试",
                "request_id": uuid.uuid4().hex,
            },
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    query = urlencode({"ticket": ticket})
    return AppCenterBrowserLaunchResponse(
        launch_url=(
            f"{settings.PUBLIC_WEB_BASE.rstrip('/')}"
            f"/api/v1/user/data-app-launch/redeem?{query}"
        ),
        expires_in=settings.APP_CENTER_LAUNCH_TICKET_TTL_SECONDS,
    )


@router.post("/{app_id}/copy", response_model=AppCenterListItem)
async def copy_app(
    app_id: int,
    principal: Principal = Depends(get_principal),
    service: AppCenterService = Depends(get_app_center_service),
) -> AppCenterListItem:
    try:
        return await service.copy(principal=principal, app_id=app_id)
    except AppCenterError as exc:
        _error(exc)


@router.put("/{app_id}/permissions", response_model=AppCenterListItem)
async def update_app_permissions(
    app_id: int,
    payload: AppCenterPermissionsRequest,
    principal: Principal = Depends(get_principal),
    service: AppCenterService = Depends(get_app_center_service),
) -> AppCenterListItem:
    try:
        return await service.replace_permissions(
            principal=principal,
            app_id=app_id,
            scope=payload.scope,
        )
    except AppCenterError as exc:
        _error(exc)


@router.put("/{app_id}/package", response_model=AppCenterListItem)
async def update_app_package(
    app_id: int,
    request: Request,
    principal: Principal = Depends(get_principal),
    service: AppCenterService = Depends(get_app_center_service),
) -> AppCenterListItem:
    content_type = request.headers.get("content-type", "").casefold()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, StarletteUploadFile):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "APP_CENTER_PACKAGE_INVALID",
                    "error_code": -14001,
                    "message": "请上传新的 ZIP 应用包",
                    "request_id": uuid.uuid4().hex,
                },
            )
        package = await upload.read(settings.APP_CENTER_MAX_PACKAGE_BYTES + 1)
        try:
            return await service.update_from_package(
                principal=principal,
                app_id=app_id,
                package=package,
            )
        except AppCenterError as exc:
            _error(exc)

    try:
        payload = AppCenterPackageUpdateRequest.model_validate(await request.json())
    except (ValidationError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "APP_CENTER_PACKAGE_INVALID",
                "error_code": -14001,
                "message": "请提供完整的 HTML 文档",
                "request_id": uuid.uuid4().hex,
            },
        ) from exc
    try:
        return await service.update_from_html(
            principal=principal,
            app_id=app_id,
            html_content=payload.html_content,
            name=payload.name,
            description=payload.description,
        )
    except AppCenterError as exc:
        _error(exc)


@router.get("/{app_id}/package")
async def download_app_package(
    app_id: int,
    principal: Principal = Depends(get_principal),
    service: AppCenterService = Depends(get_app_center_service),
) -> Response:
    try:
        filename, package = await service.package(principal=principal, app_id=app_id)
    except AppCenterError as exc:
        _error(exc)
    return Response(
        content=package,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                "attachment; filename*=UTF-8''" + quote(filename)
            )
        },
    )


@router.delete(
    "/{app_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_app(
    app_id: int,
    principal: Principal = Depends(get_principal),
    service: AppCenterService = Depends(get_app_center_service),
) -> Response:
    try:
        await service.delete(principal=principal, app_id=app_id)
    except AppCenterError as exc:
        _error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
