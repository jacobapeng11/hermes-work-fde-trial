from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.application_agent_deps import (
    get_application_agent_admin_principal,
)
from app.database import get_db
from app.im_gateway.application_channel_service import (
    ApplicationAgentImChannelService,
)
from app.im_gateway.channel_service import (
    ImChannelConflictError,
    ImChannelNotConfiguredError,
    ImGatewayServiceError,
    ImOnboardingNotFoundError,
    ImOnboardingNotReadyError,
    ImUnsupportedConnectionModeError,
)
from app.im_gateway.identity import (
    OrganizationWeComIdentityService,
    WeComIdentityError,
)
from app.schemas.im_gateway import (
    ApplicationWeComChannelResponse,
    ApplicationWeComMessageListResponse,
    ApplicationWeComSettingsRequest,
    ApplicationWeComUserListResponse,
    ImQrOnboardingCreated,
    ImQrOnboardingStatus,
    RegisterWeComChannelRequest,
)
from app.services.runtime_workspace import Principal


router = APIRouter(tags=["user-application-agent-im-channels"])


def _http_error(exc: ImGatewayServiceError) -> HTTPException:
    if isinstance(exc, ImUnsupportedConnectionModeError):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    elif isinstance(
        exc,
        (ImChannelConflictError, ImOnboardingNotReadyError),
    ):
        code = status.HTTP_409_CONFLICT
    elif isinstance(
        exc,
        (ImChannelNotConfiguredError, ImOnboardingNotFoundError),
    ):
        code = status.HTTP_404_NOT_FOUND
    else:
        code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(
        status_code=code,
        detail={"code": exc.code, "message": str(exc)},
    )


async def _notify(
    *, channel_id: int | None, config_generation: int | None
) -> None:
    if channel_id is None or config_generation is None:
        return
    from app.im_gateway.coordinator import im_gateway

    await im_gateway.notify_config_changed(
        channel_id,
        config_generation,
    )


@router.get(
    "/application-agents/{agent_id}/im-channels/wecom",
    response_model=ApplicationWeComChannelResponse,
)
async def get_application_wecom_channel(
    agent_id: int,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    db: AsyncSession = Depends(get_db),
) -> ApplicationWeComChannelResponse:
    try:
        return await ApplicationAgentImChannelService(db).get_channel(
            principal=principal,
            agent_id=agent_id,
        )
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/application-agents/{agent_id}/im-channels/wecom/onboarding/qrcode",
    response_model=ImQrOnboardingCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_application_wecom_onboarding(
    agent_id: int,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    db: AsyncSession = Depends(get_db),
) -> ImQrOnboardingCreated:
    try:
        return await ApplicationAgentImChannelService(
            db
        ).create_qr_onboarding(
            principal=principal,
            agent_id=agent_id,
        )
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/application-agents/{agent_id}/im-channels/wecom/onboarding/{onboarding_id}",
    response_model=ImQrOnboardingStatus,
)
async def get_application_wecom_onboarding(
    agent_id: int,
    onboarding_id: str,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    db: AsyncSession = Depends(get_db),
) -> ImQrOnboardingStatus:
    try:
        return await ApplicationAgentImChannelService(
            db
        ).get_qr_onboarding(
            principal=principal,
            agent_id=agent_id,
            onboarding_id=onboarding_id,
        )
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.put(
    "/application-agents/{agent_id}/im-channels/wecom",
    response_model=ApplicationWeComChannelResponse,
)
async def register_application_wecom_channel(
    agent_id: int,
    payload: RegisterWeComChannelRequest,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    db: AsyncSession = Depends(get_db),
) -> ApplicationWeComChannelResponse:
    try:
        registered = await ApplicationAgentImChannelService(
            db
        ).register_wecom(
            principal=principal,
            agent_id=agent_id,
            request=payload,
        )
        response = registered.response
        await _notify(
            channel_id=registered.channel_id,
            config_generation=registered.config_generation,
        )
        return response
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.patch(
    "/application-agents/{agent_id}/im-channels/wecom/settings",
    response_model=ApplicationWeComChannelResponse,
)
async def update_application_wecom_settings(
    agent_id: int,
    payload: ApplicationWeComSettingsRequest,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    db: AsyncSession = Depends(get_db),
) -> ApplicationWeComChannelResponse:
    try:
        response = await ApplicationAgentImChannelService(
            db
        ).update_settings(
            principal=principal,
            agent_id=agent_id,
            direct_enabled=payload.direct_enabled,
            group_enabled=payload.group_enabled,
        )
        await _notify(
            channel_id=response.channel_id,
            config_generation=response.config_generation,
        )
        return response
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


async def _set_enabled(
    *,
    agent_id: int,
    enabled: bool,
    principal: Principal,
    db: AsyncSession,
) -> ApplicationWeComChannelResponse:
    try:
        response = await ApplicationAgentImChannelService(db).set_enabled(
            principal=principal,
            agent_id=agent_id,
            enabled=enabled,
        )
        await _notify(
            channel_id=response.channel_id,
            config_generation=response.config_generation,
        )
        return response
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/application-agents/{agent_id}/im-channels/wecom/enable",
    response_model=ApplicationWeComChannelResponse,
)
async def enable_application_wecom_channel(
    agent_id: int,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    db: AsyncSession = Depends(get_db),
) -> ApplicationWeComChannelResponse:
    return await _set_enabled(
        agent_id=agent_id,
        enabled=True,
        principal=principal,
        db=db,
    )


@router.post(
    "/application-agents/{agent_id}/im-channels/wecom/disable",
    response_model=ApplicationWeComChannelResponse,
)
async def disable_application_wecom_channel(
    agent_id: int,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    db: AsyncSession = Depends(get_db),
) -> ApplicationWeComChannelResponse:
    return await _set_enabled(
        agent_id=agent_id,
        enabled=False,
        principal=principal,
        db=db,
    )


@router.post(
    "/application-agents/{agent_id}/im-channels/wecom/reconnect",
    response_model=ApplicationWeComChannelResponse,
)
async def reconnect_application_wecom_channel(
    agent_id: int,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    db: AsyncSession = Depends(get_db),
) -> ApplicationWeComChannelResponse:
    try:
        response = await ApplicationAgentImChannelService(db).reconnect(
            principal=principal,
            agent_id=agent_id,
        )
        await _notify(
            channel_id=response.channel_id,
            config_generation=response.config_generation,
        )
        return response
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/application-agents/{agent_id}/im-channels/wecom/rotate",
    response_model=ApplicationWeComChannelResponse,
)
async def rotate_application_wecom_channel(
    agent_id: int,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    db: AsyncSession = Depends(get_db),
) -> ApplicationWeComChannelResponse:
    try:
        response = await ApplicationAgentImChannelService(db).rotate(
            principal=principal,
            agent_id=agent_id,
        )
        await _notify(
            channel_id=response.channel_id,
            config_generation=response.config_generation,
        )
        return response
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/application-agents/{agent_id}/im-channels/wecom/users",
    response_model=ApplicationWeComUserListResponse,
)
async def list_application_wecom_users(
    agent_id: int,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=200),
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    db: AsyncSession = Depends(get_db),
) -> ApplicationWeComUserListResponse:
    try:
        return await ApplicationAgentImChannelService(db).users(
            principal=principal,
            agent_id=agent_id,
            query=q,
            limit=limit,
        )
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/application-agents/{agent_id}/im-channels/wecom/messages",
    response_model=ApplicationWeComMessageListResponse,
)
async def list_application_wecom_messages(
    agent_id: int,
    limit: int = Query(default=100, ge=1, le=200),
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    db: AsyncSession = Depends(get_db),
) -> ApplicationWeComMessageListResponse:
    try:
        return await ApplicationAgentImChannelService(db).messages(
            principal=principal,
            agent_id=agent_id,
            limit=limit,
        )
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/wecom-identities/{identity_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_organization_wecom_identity(
    identity_id: int,
    principal: Principal = Depends(
        get_application_agent_admin_principal
    ),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        await OrganizationWeComIdentityService(db).revoke(
            principal=principal,
            identity_id=identity_id,
        )
    except WeComIdentityError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
