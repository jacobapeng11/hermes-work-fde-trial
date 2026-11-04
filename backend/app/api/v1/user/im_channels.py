from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import (
    get_principal,
    get_runtime_workspace_service,
)
from app.database import get_db
from app.im_gateway.channel_service import (
    ImChannelConflictError,
    ImChannelNotConfiguredError,
    ImChannelService,
    ImDefaultProfileNotReadyError,
    ImGatewayServiceError,
    ImOnboardingNotFoundError,
    ImOnboardingNotReadyError,
    ImUnsupportedConnectionModeError,
)
from app.schemas.im_gateway import (
    ImChannelListResponse,
    ImQrOnboardingCreated,
    ImQrOnboardingStatus,
    RegisterWeComChannelRequest,
    RevokeImChannelRequest,
    SetImChannelEnabledRequest,
    WeComChannelResponse,
)
from app.services.runtime_workspace import (
    Principal,
    RuntimeWorkspaceService,
)


router = APIRouter(prefix="/im-channels", tags=["user-im-channels"])


def _http_error(exc: ImGatewayServiceError) -> HTTPException:
    if isinstance(exc, ImUnsupportedConnectionModeError):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    elif isinstance(
        exc,
        (
            ImChannelConflictError,
            ImOnboardingNotReadyError,
            ImDefaultProfileNotReadyError,
        ),
    ):
        code = status.HTTP_409_CONFLICT
    elif isinstance(
        exc, (ImChannelNotConfiguredError, ImOnboardingNotFoundError)
    ):
        code = status.HTTP_404_NOT_FOUND
    else:
        code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(
        status_code=code,
        detail={"code": exc.code, "message": str(exc)},
    )


async def _workspace_id(
    principal: Principal, workspace_service: RuntimeWorkspaceService
) -> int:
    workspace = await workspace_service.ensure_default_for_user(principal)
    return int(workspace.runtime_workspace_id)


@router.get("", response_model=ImChannelListResponse)
async def list_im_channels(
    principal: Principal = Depends(get_principal),
    workspace_service: RuntimeWorkspaceService = Depends(
        get_runtime_workspace_service
    ),
    db: AsyncSession = Depends(get_db),
) -> ImChannelListResponse:
    try:
        return await ImChannelService(db).list_cards(
            principal=principal,
            runtime_workspace_id=await _workspace_id(
                principal, workspace_service
            ),
        )
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/wecom/onboarding/qrcode",
    response_model=ImQrOnboardingCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_wecom_qr_onboarding(
    principal: Principal = Depends(get_principal),
    workspace_service: RuntimeWorkspaceService = Depends(
        get_runtime_workspace_service
    ),
    db: AsyncSession = Depends(get_db),
) -> ImQrOnboardingCreated:
    try:
        return await ImChannelService(db).create_qr_onboarding(
            principal=principal,
            runtime_workspace_id=await _workspace_id(
                principal, workspace_service
            ),
        )
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/wecom/onboarding/{onboarding_id}",
    response_model=ImQrOnboardingStatus,
)
async def get_wecom_qr_onboarding(
    onboarding_id: str,
    principal: Principal = Depends(get_principal),
    workspace_service: RuntimeWorkspaceService = Depends(
        get_runtime_workspace_service
    ),
    db: AsyncSession = Depends(get_db),
) -> ImQrOnboardingStatus:
    try:
        return await ImChannelService(db).get_qr_onboarding(
            principal=principal,
            runtime_workspace_id=await _workspace_id(
                principal, workspace_service
            ),
            onboarding_id=onboarding_id,
        )
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.put("/wecom", response_model=WeComChannelResponse)
async def register_wecom_channel(
    payload: RegisterWeComChannelRequest,
    principal: Principal = Depends(get_principal),
    workspace_service: RuntimeWorkspaceService = Depends(
        get_runtime_workspace_service
    ),
    db: AsyncSession = Depends(get_db),
) -> WeComChannelResponse:
    try:
        registered = await ImChannelService(db).register_wecom(
            principal=principal,
            runtime_workspace_id=await _workspace_id(
                principal, workspace_service
            ),
            request=payload,
        )
        from app.im_gateway.coordinator import im_gateway

        await im_gateway.notify_config_changed(
            registered.channel_id, registered.config_generation
        )
        return registered.response
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.patch("/wecom/enabled", response_model=WeComChannelResponse)
async def set_wecom_channel_enabled(
    payload: SetImChannelEnabledRequest,
    principal: Principal = Depends(get_principal),
    workspace_service: RuntimeWorkspaceService = Depends(
        get_runtime_workspace_service
    ),
    db: AsyncSession = Depends(get_db),
) -> WeComChannelResponse:
    try:
        registered = await ImChannelService(db).set_wecom_enabled(
            principal=principal,
            runtime_workspace_id=await _workspace_id(
                principal, workspace_service
            ),
            enabled=payload.enabled,
        )
        from app.im_gateway.coordinator import im_gateway

        await im_gateway.notify_config_changed(
            registered.channel_id, registered.config_generation
        )
        return registered.response
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc


@router.delete("/wecom", response_model=WeComChannelResponse)
async def revoke_wecom_channel(
    _payload: RevokeImChannelRequest,
    principal: Principal = Depends(get_principal),
    workspace_service: RuntimeWorkspaceService = Depends(
        get_runtime_workspace_service
    ),
    db: AsyncSession = Depends(get_db),
) -> WeComChannelResponse:
    try:
        registered = await ImChannelService(db).revoke_wecom(
            principal=principal,
            runtime_workspace_id=await _workspace_id(
                principal, workspace_service
            ),
        )
        from app.im_gateway.coordinator import im_gateway

        await im_gateway.notify_config_changed(
            registered.channel_id, registered.config_generation
        )
        return registered.response
    except ImGatewayServiceError as exc:
        raise _http_error(exc) from exc
