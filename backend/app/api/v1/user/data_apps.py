from __future__ import annotations

from html import escape
from urllib.parse import quote, urlencode
from uuid import uuid4
from zipfile import BadZipFile

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import (
    _set_platform_session_cookie,
    get_auth_token_service,
    get_platform_session_store,
)
from app.config import settings
from app.database import get_db
from app.services.app_center import AppCenterError, AppCenterService
from app.services.app_center_content_policy import with_platform_return_link
from app.services.app_center_packages import (
    normalize_package_content_path,
    read_package_entry,
)
from app.services.auth import AuthenticatedUser, AuthTokenService, AuthenticationError
from app.services.data_reports import (
    DataReportPackageInvalid,
    DataReportRateLimited,
    DataReportService,
    DataReportUnavailable,
    live_manifest_for_html_path,
    live_manifest_for_package,
)
from app.services.platform_session import (
    PLATFORM_SESSION_COOKIE_NAME,
    PlatformSessionStore,
    PlatformSessionUnavailable,
)
from app.services.runtime_workspace import Principal


router = APIRouter(prefix="/data-apps", tags=["user-data-apps"])
launch_router = APIRouter(prefix="/data-app-launch", tags=["user-data-apps"])

DATA_APP_CSP = (
    "sandbox allow-scripts allow-top-navigation-by-user-activation; "
    "default-src 'none'; script-src 'unsafe-inline' 'self'; "
    "style-src 'unsafe-inline' 'self'; img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'; "
    "frame-src 'none'; worker-src 'none'; media-src 'none'; manifest-src 'none'"
)


def get_app_center_service(db: AsyncSession = Depends(get_db)) -> AppCenterService:
    return AppCenterService(db)


def get_data_report_service(
    db: AsyncSession = Depends(get_db),
) -> DataReportService:
    return DataReportService(db)


def _error_page(
    *,
    status_code: int,
    code: str,
    error_code: int,
    message: str,
    request_id: str,
) -> HTMLResponse:
    safe_message = escape(message)
    safe_code = escape(code)
    safe_request_id = escape(request_id)
    return HTMLResponse(
        content=(
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{safe_message}</title></head><body data-error-code=\"{error_code}\" "
            f"data-error-name=\"{safe_code}\" data-request-id=\"{safe_request_id}\">"
            f"<main><h1>{safe_message}</h1><p>请返回平台后重试。</p></main></body></html>"
        ),
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": DATA_APP_CSP,
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _clear_session_cookie(response: HTMLResponse) -> None:
    response.delete_cookie(
        key=PLATFORM_SESSION_COOKIE_NAME,
        path="/api/v1",
        secure=settings.APP_ENV.lower() == "production",
        httponly=True,
        samesite="lax",
    )


def _data_app_authentication_redirect(app_id: int, path: str) -> RedirectResponse:
    return_to = f"/api/v1/user/data-apps/{app_id}/{quote(path, safe='/')}"
    query = urlencode({"return_to": return_to})
    response = RedirectResponse(
        url=f"/user/data-app-auth?{query}",
        status_code=303,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
    _clear_session_cookie(response)
    return response


async def _identity_from_session(
    *,
    request: Request,
    auth_service: AuthTokenService,
    session_store: PlatformSessionStore,
) -> tuple[Principal, AuthenticatedUser]:
    session_id = request.cookies.get(PLATFORM_SESSION_COOKIE_NAME, "")
    if not session_id:
        raise AuthenticationError("missing platform session")
    platform_session = await session_store.get(session_id)
    if platform_session is None:
        raise AuthenticationError("platform session expired")
    user = await auth_service.store.get_user(
        org_id=platform_session.org_id,
        user_id=platform_session.user_id,
    )
    if not await session_store.touch(session_id):
        raise AuthenticationError("platform session expired")
    return platform_session.principal, user


@launch_router.get("/redeem", response_model=None)
async def redeem_data_app_launch(
    ticket: str = "",
    app_service: AppCenterService = Depends(get_app_center_service),
    auth_service: AuthTokenService = Depends(get_auth_token_service),
    session_store: PlatformSessionStore = Depends(get_platform_session_store),
) -> HTMLResponse | Response:
    request_id = uuid4().hex
    try:
        launch = await session_store.consume_data_app_launch(ticket)
        if launch is None:
            return _error_page(
                status_code=401,
                code="app_launch_expired",
                error_code=-10002,
                message="应用启动链接已失效，请返回桌面端重试",
                request_id=request_id,
            )
        await app_service.require_consumer_access(
            principal=launch.principal,
            app_id=launch.app_id,
        )
        await auth_service.store.get_user(
            org_id=launch.org_id,
            user_id=launch.user_id,
        )
        platform_session = await session_store.create(launch.principal)
    except AppCenterError as exc:
        if exc.code == "APP_CENTER_PERMISSION_DENIED":
            return _error_page(
                status_code=403,
                code=exc.code,
                error_code=exc.error_code,
                message="你没有该应用的访问权限",
                request_id=request_id,
            )
        return _error_page(
            status_code=404,
            code="APP_CENTER_APP_NOT_FOUND",
            error_code=-14003,
            message="应用不存在或已删除",
            request_id=request_id,
        )
    except AuthenticationError:
        return _error_page(
            status_code=401,
            code="session_expired",
            error_code=-10002,
            message="登录已失效，请返回平台重新认证",
            request_id=request_id,
        )
    except PlatformSessionUnavailable:
        return _error_page(
            status_code=503,
            code="session_store_unavailable",
            error_code=-10000,
            message="认证服务暂时不可用，请稍后重试",
            request_id=request_id,
        )

    response = RedirectResponse(
        url=f"/api/v1/user/data-apps/{launch.app_id}/index.html",
        status_code=303,
        headers={
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
    _set_platform_session_cookie(response, platform_session.session_id)
    return response


@router.get("/{app_id}/{path:path}", response_model=None)
async def get_data_app(
    app_id: int,
    path: str,
    request: Request,
    app_service: AppCenterService = Depends(get_app_center_service),
    report_service: DataReportService = Depends(get_data_report_service),
    auth_service: AuthTokenService = Depends(get_auth_token_service),
    session_store: PlatformSessionStore = Depends(get_platform_session_store),
) -> HTMLResponse | Response:
    normalized = normalize_package_content_path(path)
    if normalized is None:
        return _error_page(
            status_code=404,
            code="APP_CENTER_APP_NOT_FOUND",
            error_code=-14003,
            message="应用页面不存在或已删除",
            request_id=uuid4().hex,
        )
    try:
        package = await app_service.public_package(app_id=app_id)
    except AppCenterError:
        return _error_page(
            status_code=404,
            code="APP_CENTER_APP_NOT_FOUND",
            error_code=-14003,
            message="应用不存在或已删除",
            request_id=uuid4().hex,
        )

    # Existing snapshot applications remain on the original anonymous route even
    # when a platform session has expired. Live apps authenticate below.
    if live_manifest_for_package(package) is None:
        return RedirectResponse(
            url=f"/api/v1/app-center/{app_id}/{quote(normalized, safe='/')}",
            status_code=307,
            headers={"Cache-Control": "no-store"},
        )

    request_id = uuid4().hex
    try:
        principal, user = await _identity_from_session(
            request=request,
            auth_service=auth_service,
            session_store=session_store,
        )
    except AuthenticationError:
        return _data_app_authentication_redirect(app_id, normalized)
    except PlatformSessionUnavailable:
        return _error_page(
            status_code=503,
            code="session_store_unavailable",
            error_code=-10000,
            message="认证服务暂时不可用，请稍后重试",
            request_id=request_id,
        )

    try:
        authorized_package = await app_service.consumer_package(
            principal=principal,
            app_id=app_id,
        )
    except AppCenterError as exc:
        if exc.code == "APP_CENTER_PERMISSION_DENIED":
            return _error_page(
                status_code=403,
                code=exc.code,
                error_code=exc.error_code,
                message="你没有该应用的访问权限",
                request_id=request_id,
            )
        return _error_page(
            status_code=404,
            code="APP_CENTER_APP_NOT_FOUND",
            error_code=-14003,
            message="应用不存在或已删除",
            request_id=request_id,
        )

    # A package can be updated concurrently. If it just became a snapshot app,
    # preserve the original route behavior.
    if live_manifest_for_package(authorized_package) is None:
        return RedirectResponse(
            url=f"/api/v1/app-center/{app_id}/{quote(normalized, safe='/')}",
            status_code=307,
            headers={"Cache-Control": "no-store"},
        )

    try:
        content, media_type = read_package_entry(authorized_package, normalized)
        page_manifest = live_manifest_for_html_path(
            authorized_package,
            normalized,
        )
        rendered = None
        if page_manifest is not None:
            rendered = await report_service.render_live_report(
                principal=principal,
                user=user,
                app_id=app_id,
                package=authorized_package,
                html_path=normalized,
            )
    except DataReportRateLimited:
        return _error_page(
            status_code=429,
            code=DataReportRateLimited.code,
            error_code=DataReportRateLimited.error_code,
            message="刷新过于频繁，请稍后再重试",
            request_id=request_id,
        )
    except (DataReportUnavailable, DataReportPackageInvalid):
        return _error_page(
            status_code=503,
            code=DataReportUnavailable.code,
            error_code=DataReportUnavailable.error_code,
            message="实时数据暂时不可用，请稍后重试",
            request_id=request_id,
        )
    except (BadZipFile, KeyError, RuntimeError, OSError):
        return _error_page(
            status_code=404,
            code="APP_CENTER_APP_NOT_FOUND",
            error_code=-14003,
            message="应用页面不存在或已删除",
            request_id=request_id,
        )

    if rendered is not None:
        return HTMLResponse(
            content=with_platform_return_link(rendered.html),
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": DATA_APP_CSP,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Hermes-Request-Id": rendered.request_id,
            },
        )

    if media_type.startswith("text/html"):
        content = with_platform_return_link(content)
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": DATA_APP_CSP if media_type.startswith("text/html") or media_type == "image/svg+xml" else "sandbox",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
