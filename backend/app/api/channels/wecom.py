from __future__ import annotations

import secrets
import time

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openapi.application_agents import (
    application_agent_invocation_memory_service,
)
from app.api.v1.user.application_agent_wecom import (
    application_agent_wecom_memory_service,
)
from app.config import settings
from app.database import AsyncSessionLocal, get_db
from app.services.application_agent_wecom import (
    ApplicationAgentWeComError,
    SqlAlchemyApplicationAgentWeComService,
    WeComApiClient,
    binding_web_url,
    oauth_authorize_url,
)
from app.services.application_agent_wecom_callback import (
    InMemoryApplicationAgentWeComCallbackService,
    SqlAlchemyApplicationAgentWeComCallbackService,
    process_wecom_callback_event,
)
from app.services.application_agent_wecom_crypto import (
    encrypt_message,
    encrypted_xml,
    message_signature,
    text_reply_xml,
)


router = APIRouter(prefix="/channels/wecom", tags=["wecom-channel"])
wecom_callback_memory_service = InMemoryApplicationAgentWeComCallbackService(
    channels=application_agent_wecom_memory_service,
    invocation=application_agent_invocation_memory_service,
)


async def get_wecom_channel_service(db: AsyncSession = Depends(get_db)):
    if settings.RUNTIME_CHAT_MODE == "real":
        return SqlAlchemyApplicationAgentWeComService(db)
    return application_agent_wecom_memory_service


async def get_wecom_callback_service(db: AsyncSession = Depends(get_db)):
    if settings.RUNTIME_CHAT_MODE == "real":
        return SqlAlchemyApplicationAgentWeComCallbackService(db)
    return wecom_callback_memory_service


def _http_error(exc: ApplicationAgentWeComError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.code)


@router.get("/{access_key}/entry")
async def enter_wecom_channel(
    access_key: str,
    service=Depends(get_wecom_channel_service),
) -> RedirectResponse:
    try:
        channel = await service.find_channel_by_access_key(access_key)
        state, _ = await service.create_oauth_state(channel.channel_id)
    except ApplicationAgentWeComError as exc:
        raise _http_error(exc) from exc
    return RedirectResponse(
        oauth_authorize_url(channel=channel, state=state),
        status_code=302,
    )


@router.get("/{callback_key}/oauth")
async def complete_wecom_oauth(
    callback_key: str,
    code: str = Query(min_length=1, max_length=1024),
    state: str = Query(min_length=20, max_length=512),
    service=Depends(get_wecom_channel_service),
) -> RedirectResponse:
    try:
        channel = await service.consume_oauth_state(state)
        if not secrets.compare_digest(channel.callback_key, callback_key):
            raise ApplicationAgentWeComError(
                "wecom_oauth_state_invalid",
                status_code=401,
            )
        member_id = await WeComApiClient().resolve_oauth_member(
            channel=channel,
            code=code,
        )
        ticket = await service.create_binding_ticket(
            channel=channel,
            member_id=member_id,
        )
    except ApplicationAgentWeComError as exc:
        raise _http_error(exc) from exc
    return RedirectResponse(binding_web_url(ticket), status_code=302)


@router.get("/{callback_key}/callback", response_class=PlainTextResponse)
async def verify_wecom_callback_url(
    callback_key: str,
    msg_signature: str = Query(min_length=40, max_length=40),
    timestamp: str = Query(min_length=1, max_length=20),
    nonce: str = Query(min_length=1, max_length=256),
    echostr: str = Query(min_length=1, max_length=1_000_000),
    service=Depends(get_wecom_callback_service),
) -> str:
    try:
        return await service.verify_url(
            callback_key=callback_key,
            signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            echo=echostr,
        )
    except ApplicationAgentWeComError as exc:
        raise _http_error(exc) from exc


@router.post("/{callback_key}/callback")
async def receive_wecom_callback(
    callback_key: str,
    request: Request,
    background_tasks: BackgroundTasks,
    msg_signature: str = Query(min_length=40, max_length=40),
    timestamp: str = Query(min_length=1, max_length=20),
    nonce: str = Query(min_length=1, max_length=256),
    service=Depends(get_wecom_callback_service),
) -> Response:
    raw_body = await request.body()
    if len(raw_body) > 1_100_000:
        raise HTTPException(status_code=413, detail="wecom_payload_too_large")
    try:
        raw_xml = raw_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="wecom_payload_invalid") from exc
    try:
        accepted = await service.accept(
            callback_key=callback_key,
            signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            raw_xml=raw_xml,
        )
    except ApplicationAgentWeComError as exc:
        if exc.code == "wecom_message_type_unsupported":
            return PlainTextResponse("success")
        raise _http_error(exc) from exc
    if accepted.event_id is not None:
        background_tasks.add_task(_process_event, accepted.event_id)
        return PlainTextResponse("success")
    if accepted.passive_text is None:
        return PlainTextResponse("success")
    response_timestamp = str(int(time.time()))
    response_nonce = secrets.token_urlsafe(12)
    reply = text_reply_xml(
        corp_id=accepted.channel.corp_id,
        member_id=accepted.member_id or "",
        content=accepted.passive_text,
        timestamp=int(response_timestamp),
    )
    encrypted = encrypt_message(
        encoding_aes_key=accepted.channel.encoding_aes_key,
        plaintext=reply,
        receive_id=accepted.channel.corp_id,
    )
    signature = message_signature(
        token=accepted.channel.callback_token,
        timestamp=response_timestamp,
        nonce=response_nonce,
        encrypted=encrypted,
    )
    return Response(
        encrypted_xml(
            encrypted=encrypted,
            signature=signature,
            timestamp=response_timestamp,
            nonce=response_nonce,
        ),
        media_type="application/xml",
    )


async def _process_event(event_id: int) -> None:
    async with AsyncSessionLocal() as db:
        await process_wecom_callback_event(
            db=db,
            event_id=event_id,
        )
