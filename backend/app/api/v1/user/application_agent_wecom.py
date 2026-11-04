from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.application_agent_deps import (
    get_application_agent_admin_principal,
)
from app.api.v1.user.application_agents import application_agent_memory_service
from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.database import get_db
from app.schemas.application_agent_wecom import (
    ApplicationAgentWeComBindingRequest,
    ApplicationAgentWeComBindingResponse,
    ApplicationAgentWeComBindingSummary,
    ApplicationAgentWeComChannelSummary,
    ApplicationAgentWeComChannelUpsertRequest,
)
from app.services.application_agent_wecom import (
    ApplicationAgentWeComError,
    InMemoryApplicationAgentWeComService,
    SqlAlchemyApplicationAgentWeComService,
    mask_member_id,
)
from app.services.runtime_workspace import Principal


router = APIRouter(tags=["user-application-agent-wecom"])
application_agent_wecom_memory_service = InMemoryApplicationAgentWeComService(
    application_agent_memory_service
)


async def get_application_agent_wecom_service(
    db: AsyncSession = Depends(get_db),
):
    if settings.RUNTIME_CHAT_MODE == "real":
        return SqlAlchemyApplicationAgentWeComService(db)
    return application_agent_wecom_memory_service


def _summary(record) -> ApplicationAgentWeComChannelSummary:
    return ApplicationAgentWeComChannelSummary(
        channel_id=record.channel_id,
        application_agent_id=record.application_agent_id,
        name=record.name,
        corp_id=record.corp_id,
        wecom_agent_id=record.wecom_agent_id,
        status=record.status,
        callback_url=record.callback_url,
        entry_url=record.entry_url,
        secrets_configured=bool(
            record.callback_token
            and record.encoding_aes_key
            and record.agent_secret
        ),
        updated_at=record.updated_at,
    )


def _http_error(exc: ApplicationAgentWeComError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.code)


@router.get(
    "/application-agents/{agent_id}/wecom-channel",
    response_model=ApplicationAgentWeComChannelSummary | None,
)
async def get_wecom_channel(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_wecom_service),
):
    try:
        record = await service.get_channel(principal=principal, agent_id=agent_id)
    except ApplicationAgentWeComError as exc:
        raise _http_error(exc) from exc
    return _summary(record) if record is not None else None


@router.post(
    "/application-agents/{agent_id}/wecom-channel/rotate-entry",
    response_model=ApplicationAgentWeComChannelSummary,
)
async def rotate_wecom_entry(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_wecom_service),
) -> ApplicationAgentWeComChannelSummary:
    try:
        record = await service.rotate_entry(
            principal=principal,
            agent_id=agent_id,
        )
    except ApplicationAgentWeComError as exc:
        raise _http_error(exc) from exc
    return _summary(record)


@router.put(
    "/application-agents/{agent_id}/wecom-channel",
    response_model=ApplicationAgentWeComChannelSummary,
)
async def upsert_wecom_channel(
    agent_id: int,
    payload: ApplicationAgentWeComChannelUpsertRequest,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_wecom_service),
) -> ApplicationAgentWeComChannelSummary:
    try:
        record = await service.upsert_channel(
            principal=principal,
            agent_id=agent_id,
            payload=payload,
        )
    except ApplicationAgentWeComError as exc:
        raise _http_error(exc) from exc
    return _summary(record)


@router.get(
    "/application-agents/{agent_id}/wecom-bindings",
    response_model=list[ApplicationAgentWeComBindingSummary],
)
async def list_wecom_bindings(
    agent_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_wecom_service),
) -> list[ApplicationAgentWeComBindingSummary]:
    try:
        rows = await service.list_bindings(
            principal=principal,
            agent_id=agent_id,
        )
    except ApplicationAgentWeComError as exc:
        raise _http_error(exc) from exc
    return [
        ApplicationAgentWeComBindingSummary(
            binding_id=row.binding_id,
            wecom_member_id_masked=mask_member_id(row.member_id),
            platform_user_id=row.platform_user_id,
            status=row.status,
            bound_at=row.bound_at,
            last_used_at=row.last_used_at,
        )
        for row in rows
    ]


@router.delete(
    "/application-agents/{agent_id}/wecom-bindings/{binding_id}",
    status_code=204,
)
async def revoke_wecom_binding(
    agent_id: int,
    binding_id: int,
    principal: Principal = Depends(get_application_agent_admin_principal),
    service=Depends(get_application_agent_wecom_service),
) -> Response:
    try:
        await service.revoke_binding(
            principal=principal,
            agent_id=agent_id,
            binding_id=binding_id,
        )
    except ApplicationAgentWeComError as exc:
        raise _http_error(exc) from exc
    return Response(status_code=204)


@router.post(
    "/application-agent-wecom/bind",
    response_model=ApplicationAgentWeComBindingResponse,
)
async def bind_wecom_member(
    payload: ApplicationAgentWeComBindingRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
    service=Depends(get_application_agent_wecom_service),
) -> ApplicationAgentWeComBindingResponse:
    try:
        result = await service.bind(
            principal=principal,
            ticket=payload.ticket,
        )
    except ApplicationAgentWeComError as exc:
        raise _http_error(exc) from exc
    return ApplicationAgentWeComBindingResponse(
        application_agent_name=result.application_agent_name,
        channel_name=result.channel_name,
        wecom_member_id_masked=mask_member_id(result.member_id),
        bound_at=result.bound_at,
    )
