from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas.auth import (
    AdminLoginRequest,
    AuthPrincipalSummary,
    AuthTokenResponse,
    EnterpriseIdpTokenExchangeRequest,
    LoginRequest,
)
from app.services.admin_auth import AdminAuthorizationService, SqlAlchemyAdminRoleStore
from app.services.admin_work_no import (
    AdminWorkNoAuthorizationService,
    InMemoryAdminWorkNoStore,
    SqlAlchemyAdminWorkNoStore,
)
from app.services.application_agent_admin import (
    ApplicationAgentAuthorizationService,
    SqlAlchemyApplicationAgentAdminStore,
)
from app.services.auth import (
    AuthTokenService,
    AuthenticatedUser,
    AdminPasswordLoginService,
    AuthenticationError,
    InMemoryAuthUserStore,
    LoginCommand,
    SqlAlchemyAdminPasswordAuthStore,
    SqlAlchemyAuthUserStore,
    create_access_token,
)
from app.services.enterprise_idp import EnterpriseIdpAssertionService, EnterpriseIdpAuthenticationError
from app.services.platform_session import (
    PLATFORM_SESSION_COOKIE_NAME,
    PlatformSessionStore,
    PlatformSessionUnavailable,
    shared_platform_session_store,
)
from app.services.runtime_workspace import Principal
from app.services.sso_identity import SqlAlchemySsoIdentityRepository, SsoIdentityError, SsoIdentityService
from app.api.v1.application_agent_role_deps import get_application_agent_authorization_service

router = APIRouter(prefix="/auth", tags=["auth"])

_auth_store = InMemoryAuthUserStore()
_admin_work_no_store = InMemoryAdminWorkNoStore()


def get_platform_session_store() -> PlatformSessionStore:
    return shared_platform_session_store()


@router.get("/config")
async def auth_config() -> Response:
    """Public method discovery; no credentials, provider URLs or user data."""
    from fastapi.responses import JSONResponse
    return JSONResponse({"login_method": settings.HERMES_AUTH_MODE},
                        headers={"Cache-Control": "no-store"})


async def get_auth_token_service(db: AsyncSession = Depends(get_db)) -> AuthTokenService:
    if (
        settings.RUNTIME_CHAT_MODE == "real"
        or settings.HERMES_AUTH_MODE in {"enterprise_idpAES", "oauth2"}
        or settings.HERMES_ADMIN_AUTH_MODE == "password"
    ):
        return AuthTokenService(SqlAlchemyAuthUserStore(db))
    return AuthTokenService(_auth_store)


async def get_admin_password_login_service(
    db: AsyncSession = Depends(get_db),
) -> AdminPasswordLoginService:
    return AdminPasswordLoginService(
        SqlAlchemyAdminPasswordAuthStore(db),
        AdminAuthorizationService(SqlAlchemyAdminRoleStore(db)),
    )


async def get_admin_work_no_authorization_service(
    db: AsyncSession = Depends(get_db),
) -> AdminWorkNoAuthorizationService:
    if (
        settings.RUNTIME_CHAT_MODE != "real"
        and settings.HERMES_AUTH_MODE not in {"enterprise_idpAES", "oauth2"}
        and settings.HERMES_ADMIN_AUTH_MODE != "password"
    ):
        return AdminWorkNoAuthorizationService(_admin_work_no_store)
    return AdminWorkNoAuthorizationService(SqlAlchemyAdminWorkNoStore(db))


def _principal(
    user: AuthenticatedUser,
    *,
    can_manage_application_agents: bool = False,
    is_admin: bool = False,
) -> AuthPrincipalSummary:
    return AuthPrincipalSummary(
        org_id=user.org_id,
        user_id=user.user_id,
        name=user.name,
        email=user.email,
        user_code=user.user_code,
        can_manage_application_agents=can_manage_application_agents,
        is_admin=is_admin,
    )


def _set_platform_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=PLATFORM_SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        secure=settings.APP_ENV.lower() == "production",
        samesite="lax",
        path="/api/v1",
    )
    response.headers["Cache-Control"] = "no-store"


def _clear_platform_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=PLATFORM_SESSION_COOKIE_NAME,
        path="/api/v1",
        secure=settings.APP_ENV.lower() == "production",
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"


def _require_trusted_browser_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        if settings.APP_ENV.lower() == "production":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "invalid_origin", "message": "invalid browser origin"},
            )
        return
    allowed = {
        settings.PUBLIC_WEB_BASE.rstrip("/"),
        *{item.rstrip("/") for item in settings.cors_origins},
    }
    if origin.rstrip("/") not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "invalid_origin", "message": "invalid browser origin"},
        )


def _platform_access_token(principal: Principal) -> tuple[str, int]:
    expires_in = settings.PLATFORM_ACCESS_TOKEN_EXPIRE_MINUTES * 60
    now = int(time.time())
    return (
        create_access_token(
            principal,
            issued_at=now,
            expires_at=now + expires_in,
        ),
        expires_in,
    )


@router.post("/login", response_model=AuthTokenResponse)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
    service: AuthTokenService = Depends(get_auth_token_service),
    application_agent_authorization: ApplicationAgentAuthorizationService = Depends(
        get_application_agent_authorization_service
    ),
    admin_authorization: AdminWorkNoAuthorizationService = Depends(
        get_admin_work_no_authorization_service
    ),
) -> AuthTokenResponse:
    if settings.HERMES_AUTH_MODE in {"enterprise_idpAES", "oauth2"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="password login is disabled")
    try:
        token, user = await service.login(
            LoginCommand(
                identifier=payload.identifier,
                password=payload.password,
                org_code=payload.org_code,
            )
        )
        if isinstance(service.store, SqlAlchemyAuthUserStore):
            await db.execute(
                update(User)
                .where(User.id == user.user_id, User.org_id == user.org_id)
                .values(last_login_at=func.now(), login_count=User.login_count + 1)
            )
            await db.commit()
        can_manage = await application_agent_authorization.can_manage(
            Principal(org_id=user.org_id, user_id=user.user_id)
        )
        is_admin = await admin_authorization.is_admin_work_no(
            org_id=user.org_id,
            work_no=user.user_code,
        )
        return AuthTokenResponse(
            access_token=token,
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            principal=_principal(
                user,
                can_manage_application_agents=can_manage,
                is_admin=is_admin,
            ),
        )
    except AuthenticationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@router.post("/admin/login", response_model=AuthTokenResponse)
async def admin_password_login(
    payload: AdminLoginRequest,
    response: Response,
    service: AdminPasswordLoginService = Depends(get_admin_password_login_service),
    application_agent_authorization: ApplicationAgentAuthorizationService = Depends(
        get_application_agent_authorization_service
    ),
    admin_authorization: AdminWorkNoAuthorizationService = Depends(
        get_admin_work_no_authorization_service
    ),
) -> AuthTokenResponse:
    if settings.HERMES_ADMIN_AUTH_MODE != "password":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    try:
        token, user = await service.login(
            username=payload.username,
            password=payload.password,
        )
        can_manage = await application_agent_authorization.can_manage(
            Principal(org_id=user.org_id, user_id=user.user_id)
        )
        is_admin = await admin_authorization.is_admin_work_no(
            org_id=user.org_id,
            work_no=user.user_code,
        )
        response.headers["Cache-Control"] = "no-store"
        return AuthTokenResponse(
            access_token=token,
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            principal=_principal(
                user,
                can_manage_application_agents=can_manage,
                is_admin=is_admin,
            ),
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"Cache-Control": "no-store"},
        ) from exc


@router.get("/me", response_model=AuthPrincipalSummary)
async def me(
    authorization: str | None = Header(default=None, alias="Authorization"),
    service: AuthTokenService = Depends(get_auth_token_service),
    application_agent_authorization: ApplicationAgentAuthorizationService = Depends(
        get_application_agent_authorization_service
    ),
    admin_authorization: AdminWorkNoAuthorizationService = Depends(
        get_admin_work_no_authorization_service
    ),
) -> AuthPrincipalSummary:
    try:
        principal = await service.principal_from_bearer(authorization)
        user = await service.store.get_user(org_id=principal.org_id, user_id=principal.user_id)
        can_manage = await application_agent_authorization.can_manage(principal)
        is_admin = await admin_authorization.is_admin(principal)
        return _principal(
            user,
            can_manage_application_agents=can_manage,
            is_admin=is_admin,
        )
    except AuthenticationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@router.post("/enterprise_idp/exchange", response_model=AuthTokenResponse)
async def exchange_enterprise_idp_token(
    payload: EnterpriseIdpTokenExchangeRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    session_store: PlatformSessionStore = Depends(get_platform_session_store),
) -> AuthTokenResponse:
    """Exchange one Enterprise SSO callback token for a platform JWT without persisting either token."""
    if settings.HERMES_AUTH_MODE != "enterprise_idpAES":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="enterprise_idpAES login is disabled")
    try:
        assertion = EnterpriseIdpAssertionService().decrypt_and_validate(payload.token)
        identity = await SsoIdentityService(SqlAlchemySsoIdentityRepository(db)).resolve_or_create(
            org_id=settings.DEFAULT_ORG_ID,
            work_no=assertion.work_no,
        )
        can_manage = await ApplicationAgentAuthorizationService(
            SqlAlchemyApplicationAgentAdminStore(db)
        ).can_manage(Principal(org_id=identity.org_id, user_id=identity.user_id))
        is_admin = await AdminWorkNoAuthorizationService(
            SqlAlchemyAdminWorkNoStore(db)
        ).is_admin_work_no(org_id=identity.org_id, work_no=identity.user_code)
        principal = Principal(org_id=identity.org_id, user_id=identity.user_id)
        platform_session = await session_store.create_from_enterprise_idp(
            principal,
            token_fingerprint=assertion.token_fingerprint,
            assertion_ttl_seconds=max(1, assertion.expires_at_epoch - int(time.time())),
        )
        if platform_session is None:
            raise EnterpriseIdpAuthenticationError("invalid EnterpriseIdp assertion")
        access_token, expires_in = _platform_access_token(principal)
        _set_platform_session_cookie(
            response,
            platform_session.session_id,
        )
        return AuthTokenResponse(
            access_token=access_token,
            expires_in=expires_in,
            principal=AuthPrincipalSummary(
                org_id=identity.org_id,
                user_id=identity.user_id,
                name=identity.name,
                email=identity.email,
                user_code=identity.user_code,
                can_manage_application_agents=can_manage,
                is_admin=is_admin,
            ),
        )
    except PlatformSessionUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "session_store_unavailable",
                "message": "authentication session service is temporarily unavailable",
            },
            headers={"Cache-Control": "no-store"},
        ) from exc
    except (EnterpriseIdpAuthenticationError, SsoIdentityError, AuthenticationError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@router.post("/refresh", response_model=AuthTokenResponse)
async def refresh_platform_session(
    request: Request,
    response: Response,
    service: AuthTokenService = Depends(get_auth_token_service),
    application_agent_authorization: ApplicationAgentAuthorizationService = Depends(
        get_application_agent_authorization_service
    ),
    admin_authorization: AdminWorkNoAuthorizationService = Depends(
        get_admin_work_no_authorization_service
    ),
    session_store: PlatformSessionStore = Depends(get_platform_session_store),
) -> AuthTokenResponse:
    _require_trusted_browser_origin(request)
    session_id = request.cookies.get(PLATFORM_SESSION_COOKIE_NAME, "")
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "session_expired", "message": "platform session expired"},
            headers={"Cache-Control": "no-store"},
        )
    try:
        platform_session = await session_store.get(session_id)
        if platform_session is None:
            _clear_platform_session_cookie(response)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "session_expired", "message": "platform session expired"},
                headers={"Cache-Control": "no-store"},
            )
        user = await service.store.get_user(
            org_id=platform_session.org_id,
            user_id=platform_session.user_id,
        )
        principal = platform_session.principal
        can_manage = await application_agent_authorization.can_manage(principal)
        is_admin = await admin_authorization.is_admin(principal)
        if not await session_store.touch(session_id):
            _clear_platform_session_cookie(response)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "session_expired", "message": "platform session expired"},
                headers={"Cache-Control": "no-store"},
            )
        access_token, expires_in = _platform_access_token(principal)
        response.headers["Cache-Control"] = "no-store"
        return AuthTokenResponse(
            access_token=access_token,
            expires_in=expires_in,
            principal=_principal(
                user,
                can_manage_application_agents=can_manage,
                is_admin=is_admin,
            ),
        )
    except PlatformSessionUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "session_store_unavailable",
                "message": "authentication session service is temporarily unavailable",
            },
            headers={"Cache-Control": "no-store"},
        ) from exc
    except AuthenticationError as exc:
        try:
            await session_store.revoke(session_id)
        except PlatformSessionUnavailable:
            pass
        _clear_platform_session_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "user_disabled", "message": "user is no longer active"},
            headers={"Cache-Control": "no-store"},
        ) from exc


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    session_store: PlatformSessionStore = Depends(get_platform_session_store),
) -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    session_id = request.cookies.get(PLATFORM_SESSION_COOKIE_NAME, "")
    if session_id:
        try:
            await session_store.revoke(session_id)
        except PlatformSessionUnavailable:
            # The browser cookie is still removed. Its short server TTL is the
            # fallback if Redis becomes available after this response.
            pass
    _clear_platform_session_cookie(response)
    return response
