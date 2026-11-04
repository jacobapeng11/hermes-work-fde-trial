from __future__ import annotations

import logging
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import (
    _require_trusted_browser_origin,
    _set_platform_session_cookie,
    get_platform_session_store,
)
from app.config import settings
from app.database import get_db
from app.services.oauth2 import OAuth2Client, OAuth2Error, config_fingerprint
from app.services.oauth2_user_mapping import map_identity
from app.services.oauth_login_transactions import (
    BINDING_COOKIE_PATH,
    COMPLETE_PATH,
    COMPLETION_COOKIE,
    TRANSACTION_COOKIE_PREFIX,
    OAuthLoginTransactions,
    digest,
    safe_return_to,
    shared_oauth_transactions,
    transaction_cookie,
    valid_nonce,
)
from app.services.platform_session import PlatformSessionUnavailable
from app.services.runtime_workspace import Principal
from app.services.sso_identity import (
    SqlAlchemySsoIdentityRepository,
    SsoIdentityError,
    SsoIdentityService,
)

router = APIRouter(prefix="/auth/oauth2", tags=["auth"])
logger = logging.getLogger(__name__)
SAFE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def get_oauth_client() -> OAuth2Client:
    return OAuth2Client(settings)


def selected() -> None:
    if settings.HERMES_AUTH_MODE != "oauth2":
        raise OAuth2Error("oauth2_not_selected", "当前部署未选择 OAuth2 登录", 404)


def error_response(error: OAuth2Error, request_id: str, *, redirect: bool = False) -> Response:
    logger.warning("oauth2_login_failed request_id=%s code=%s", request_id, error.code)
    if redirect:
        return RedirectResponse(
            f"/auth/error?reason={error.code}&request_id={request_id}",
            status_code=303,
            headers=SAFE_HEADERS,
        )
    return JSONResponse(
        {"detail": {"code": error.code, "message": error.message, "request_id": request_id}},
        status_code=error.status,
        headers=SAFE_HEADERS,
    )


def cookie(response: Response, name: str, value: str, path: str, ttl: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=ttl,
        path=path,
        httponly=True,
        secure=settings.APP_ENV.lower() == "production",
        samesite="lax",
    )


def one(request: Request, name: str, *, required: bool = False) -> str:
    values = request.query_params.getlist(name)
    if len(values) > 1 or (required and (not values or not values[0])):
        raise OAuth2Error("oauth2_invalid_request", "登录请求参数无效")
    value = values[0] if values else ""
    if len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise OAuth2Error("oauth2_invalid_request", "登录请求参数无效")
    return value


@router.get("/login")
async def login(
    request: Request,
    transactions: Annotated[OAuthLoginTransactions, Depends(shared_oauth_transactions)],
    client: Annotated[OAuth2Client, Depends(get_oauth_client)],
) -> Response:
    request_id = secrets.token_hex(12)
    try:
        selected()
        if any(key not in {"return_to", "desktop_flow"} for key in request.query_params):
            raise OAuth2Error("oauth2_invalid_request", "登录请求参数无效")
        if sum(name.startswith(TRANSACTION_COOKIE_PREFIX) for name in request.cookies) >= 8:
            raise OAuth2Error("oauth2_invalid_request", "未完成的登录过多，请稍后重试")
        return_to = safe_return_to(one(request, "return_to") or "/user/chat")
        desktop_flow = one(request, "desktop_flow")
        if desktop_flow and not valid_nonce(desktop_flow):
            raise OAuth2Error("oauth2_invalid_request", "桌面登录请求无效")
        binding = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48) if settings.OAUTH2_PKCE_ENABLED else ""
        state = await transactions.put(
            "login",
            {
                "binding": digest(binding),
                "fingerprint": config_fingerprint(settings),
                "verifier": verifier,
                "return_to": return_to,
                "desktop_flow": desktop_flow,
            },
            ttl=settings.OAUTH2_LOGIN_TRANSACTION_TTL_SECONDS,
        )
        response = RedirectResponse(
            client.authorization_url(state=state, verifier=verifier),
            status_code=302,
            headers=SAFE_HEADERS,
        )
        cookie(
            response,
            transaction_cookie(state),
            binding,
            BINDING_COOKIE_PATH,
            settings.OAUTH2_LOGIN_TRANSACTION_TTL_SECONDS,
        )
        return response
    except OAuth2Error as error:
        return error_response(error, request_id)


@router.get("/callback")
async def callback(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    transactions: Annotated[OAuthLoginTransactions, Depends(shared_oauth_transactions)],
    client: Annotated[OAuth2Client, Depends(get_oauth_client)],
    sessions: Annotated[Any, Depends(get_platform_session_store)],
) -> Response:
    request_id, state = secrets.token_hex(12), ""
    try:
        selected()
        state, code, error = (
            one(request, "state", required=True),
            one(request, "code"),
            one(request, "error"),
        )
        if bool(code) == bool(error):
            raise OAuth2Error("oauth2_invalid_request", "登录回调参数无效")
        transaction = await transactions.take(
            "login",
            state,
            binding=request.cookies.get(transaction_cookie(state), ""),
            fingerprint=config_fingerprint(settings),
        )
        if error:
            raise OAuth2Error("oauth2_access_denied", "登录授权未完成，请重试", 401)
        info = await client.user_info(code=code, verifier=transaction["verifier"], request_id=request_id)
        external = map_identity(settings, info)
        identity = await SsoIdentityService(SqlAlchemySsoIdentityRepository(db)).resolve_or_create(
            org_id=settings.DEFAULT_ORG_ID, work_no=external.work_no,
            display_name=external.display_name, email=external.email,
        )
        session = await sessions.create(Principal(org_id=identity.org_id, user_id=identity.user_id))
        completion = await transactions.put(
            "completion",
            {
                "binding": digest(session.session_id),
                "fingerprint": config_fingerprint(settings),
                "return_to": transaction["return_to"],
                "desktop_flow": transaction["desktop_flow"],
            },
            ttl=60,
        )
        target = "/auth/desktop-complete" if transaction["desktop_flow"] else "/auth/complete"
        response = RedirectResponse(target, status_code=303, headers=SAFE_HEADERS)
        _set_platform_session_cookie(response, session.session_id)
        cookie(response, COMPLETION_COOKIE, completion, COMPLETE_PATH, 60)
    except OAuth2Error as error:
        response = error_response(error, request_id, redirect=error.status != 404)
    except (SsoIdentityError, PlatformSessionUnavailable) as error:
        failure = (
            OAuth2Error("oauth2_store_unavailable", "认证会话存储暂时不可用", 503)
            if isinstance(error, PlatformSessionUnavailable)
            else OAuth2Error("oauth2_identity_invalid", "平台身份不可用，请核验工号", 401)
        )
        response = error_response(failure, request_id, redirect=True)
    if valid_nonce(state):
        response.delete_cookie(
            transaction_cookie(state),
            path=BINDING_COOKIE_PATH,
            secure=settings.APP_ENV.lower() == "production",
            httponly=True,
            samesite="lax",
        )
    return response


@router.post("/complete")
async def complete(
    request: Request,
    transactions: Annotated[OAuthLoginTransactions, Depends(shared_oauth_transactions)],
) -> Response:
    from app.services.platform_session import PLATFORM_SESSION_COOKIE_NAME

    try:
        selected()
        _require_trusted_browser_origin(request)
        record = await transactions.take(
            "completion",
            request.cookies.get(COMPLETION_COOKIE, ""),
            binding=request.cookies.get(PLATFORM_SESSION_COOKIE_NAME, ""),
            fingerprint=config_fingerprint(settings),
        )
        flow = request.headers.get("x-hermes-desktop-flow", "")
        if not secrets.compare_digest(record["desktop_flow"], flow):
            raise OAuth2Error("oauth2_transaction_invalid", "登录完成事务不匹配")
        response = JSONResponse({"return_to": record["return_to"]}, headers=SAFE_HEADERS)
    except OAuth2Error as error:
        response = error_response(error, secrets.token_hex(12))
    response.delete_cookie(
        COMPLETION_COOKIE,
        path=COMPLETE_PATH,
        secure=settings.APP_ENV.lower() == "production",
        httponly=True,
        samesite="lax",
    )
    return response
