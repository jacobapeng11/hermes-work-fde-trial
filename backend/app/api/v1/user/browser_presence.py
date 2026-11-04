from __future__ import annotations

import asyncio
import json
import time
import logging
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.config import settings
from app.database import AsyncSessionLocal
from app.services.auth import (
    AuthenticationError,
    SqlAlchemyAuthUserStore,
    decode_access_token,
)
from app.services.platform_session import (
    PLATFORM_SESSION_COOKIE_NAME,
    PlatformSessionStore,
    PlatformSessionUnavailable,
)
from app.services.platform_presence import (
    ContractCrossDayPresenceRecorder,
    CrossDayPresenceRecorder,
    SqlAlchemyCrossDayPresenceRecorder,
    current_business_day,
    session_started_before_business_day,
)
from app.services.runtime_infrastructure import build_runtime_infrastructure
from app.services.runtime_workspace import Principal, RuntimeKeepaliveError
from app.api.v1.user.runtime_workspace import configured_dev_principal
from app.api.v1.auth import get_platform_session_store


router = APIRouter(prefix="/browser-presence", tags=["user-browser-presence"])
logger = logging.getLogger(__name__)


class PresenceRuntimeProbe(Protocol):
    async def touch(self, principal: Principal) -> str:
        ...


class DefaultPresenceRuntimeProbe:
    async def touch(self, principal: Principal) -> str:
        if settings.RUNTIME_CHAT_MODE != "real":
            return "ready"
        try:
            async with AsyncSessionLocal() as db:
                infrastructure = build_runtime_infrastructure(db)
                profile = await infrastructure.workspace_service.keepalive_default_for_user(
                    principal
                )
                endpoint = await infrastructure.workspace_store.get_runtime_endpoint(
                    profile
                )
                await asyncio.wait_for(
                    infrastructure.activator.activate_dashboard(endpoint),
                    timeout=settings.BROWSER_PRESENCE_FINAL_TOUCH_TIMEOUT_SECONDS,
                )
            return "ready"
        except (RuntimeKeepaliveError, TimeoutError):
            return "unavailable"
        except Exception:
            # Runtime state is deliberately isolated from authentication
            # presence. A later probe retries without closing the SSE.
            return "unavailable"


def get_presence_runtime_probe() -> PresenceRuntimeProbe:
    return DefaultPresenceRuntimeProbe()


@lru_cache(maxsize=1)
def get_cross_day_presence_recorder() -> CrossDayPresenceRecorder:
    if settings.RUNTIME_CHAT_MODE != "real":
        return ContractCrossDayPresenceRecorder()
    return SqlAlchemyCrossDayPresenceRecorder()


async def _authenticate_presence(
    authorization: str | None,
) -> Principal:
    if not authorization and settings.HERMES_AUTH_MODE == "dev":
        return configured_dev_principal()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "access_token_missing", "message": "missing bearer token"},
            headers={"Cache-Control": "no-store"},
        )
    try:
        principal = decode_access_token(
            authorization.removeprefix("Bearer ").strip()
        )
        async with AsyncSessionLocal() as db:
            await SqlAlchemyAuthUserStore(db).get_user(
                org_id=principal.org_id,
                user_id=principal.user_id,
            )
        return principal
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "access_token_invalid", "message": str(exc)},
            headers={"Cache-Control": "no-store"},
        ) from exc


def _sse(event: str, data: dict[str, object]) -> str:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _consume_probe_result(task: asyncio.Task[str]) -> None:
    try:
        task.result()
    except (Exception, asyncio.CancelledError):
        pass


def _resolve_client_type(
    client_type_header: str | None,
    user_agent: str | None,
) -> str:
    if client_type_header in {"web", "desktop"}:
        return client_type_header
    if client_type_header is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_client_type",
                "message": "client type must be web or desktop",
            },
            headers={"Cache-Control": "no-store"},
        )
    # Published desktop packages do not yet send the explicit header. Electron
    # remains visible in their user agent, while normal browsers do not use it.
    if user_agent and "electron" in user_agent.lower():
        return "desktop"
    return "web"


async def _presence_events(
    *,
    principal: Principal,
    session_id: str | None,
    session_store: PlatformSessionStore,
    runtime_probe: PresenceRuntimeProbe,
    client_type: str,
    session_created_at: int | None,
    cross_day_recorder: CrossDayPresenceRecorder,
) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    next_runtime_touch = 0.0
    runtime_state = "checking"
    session_state = "active"
    probe_task: asyncio.Task[str] | None = None
    recorded_business_day = None

    # Start the streaming response immediately through common reverse proxies.
    yield f": connected {' ' * 2048}\n\n"
    try:
        while True:
            if session_id:
                try:
                    if not await session_store.touch(session_id):
                        yield _sse(
                            "presence.session_expired",
                            {"timestamp": int(time.time())},
                        )
                        return
                    session_state = "active"
                except PlatformSessionUnavailable:
                    session_state = "temporarily_unavailable"
                else:
                    try:
                        await session_store.record_client_presence(
                            session_id,
                            principal,
                            client_type=client_type,
                        )
                    except PlatformSessionUnavailable:
                        session_state = "temporarily_unavailable"

            if probe_task is not None and probe_task.done():
                try:
                    runtime_state = probe_task.result()
                except Exception:
                    runtime_state = "unavailable"
                probe_task = None

            business_day = current_business_day()
            if (
                session_created_at is not None
                and session_started_before_business_day(session_created_at)
                and business_day != recorded_business_day
                and session_state == "active"
                and runtime_state == "ready"
            ):
                try:
                    await cross_day_recorder.record(principal)
                except Exception:
                    logger.warning(
                        "cross-day platform presence could not be recorded",
                        exc_info=True,
                    )
                else:
                    recorded_business_day = business_day

            now = loop.time()
            if probe_task is None and now >= next_runtime_touch:
                probe_task = asyncio.create_task(runtime_probe.touch(principal))
                probe_task.add_done_callback(_consume_probe_result)
                next_runtime_touch = (
                    now + settings.BROWSER_PRESENCE_RUNTIME_TOUCH_SECONDS
                )

            yield _sse(
                "presence.heartbeat",
                {
                    "timestamp": int(time.time()),
                    "session": session_state,
                    "runtime": runtime_state,
                },
            )
            await asyncio.sleep(settings.BROWSER_PRESENCE_HEARTBEAT_SECONDS)
    finally:
        if probe_task is not None and not probe_task.done():
            probe_task.cancel()
        final_touch = asyncio.create_task(runtime_probe.touch(principal))
        final_touch.add_done_callback(_consume_probe_result)


@router.get("/stream")
async def stream_browser_presence(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    client_type_header: str | None = Header(default=None, alias="X-Hermes-Client-Type"),
    user_agent: str | None = Header(default=None, alias="User-Agent"),
    session_store: PlatformSessionStore = Depends(get_platform_session_store),
    runtime_probe: PresenceRuntimeProbe = Depends(get_presence_runtime_probe),
    cross_day_recorder: CrossDayPresenceRecorder = Depends(
        get_cross_day_presence_recorder,
    ),
) -> StreamingResponse:
    principal = await _authenticate_presence(authorization)
    client_type = _resolve_client_type(client_type_header, user_agent)
    session_id = request.cookies.get(PLATFORM_SESSION_COOKIE_NAME)
    session_created_at: int | None = None
    if settings.HERMES_AUTH_MODE in {"enterprise_idpAES", "oauth2"}:
        if not session_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "session_expired", "message": "platform session expired"},
                headers={"Cache-Control": "no-store"},
            )
        try:
            platform_session = await session_store.get(session_id)
        except PlatformSessionUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "session_store_unavailable",
                    "message": "authentication session service is temporarily unavailable",
                },
                headers={"Cache-Control": "no-store"},
            ) from exc
        if platform_session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "session_expired", "message": "platform session expired"},
                headers={"Cache-Control": "no-store"},
            )
        if platform_session.principal != principal:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "session_principal_mismatch",
                    "message": "platform session does not match access token",
                },
                headers={"Cache-Control": "no-store"},
            )
        session_created_at = platform_session.created_at

    return StreamingResponse(
        _presence_events(
            principal=principal,
            session_id=session_id,
            session_store=session_store,
            runtime_probe=runtime_probe,
            client_type=client_type,
            session_created_at=session_created_at,
            cross_day_recorder=cross_day_recorder,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )
