from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.database import AsyncSessionLocal, get_db
from app.hermes_runtime.public_history_message import (
    normalize_internal_history_message,
)
from app.hermes_runtime.public_tool_payload import (
    sanitize_public_tool_history_message,
    sanitize_public_tool_text,
)
from app.hermes_runtime.events import PlatformStreamEvent
from app.models import ApplicationAgent, ApplicationAgentRun
from app.schemas.expert_collaborations import ExpertCollaborationListResponse
from app.schemas.runtime_chat import (
    RuntimeChatClientTimingRequest,
    RuntimeChatHistoryResponse,
    RuntimeRunActivityEvent,
    RuntimeRunActivityResponse,
)
from app.services.chat_log import (
    ChatLogProjector,
    ChatLogSessionNotFound,
    SqlAlchemyChatLogStore,
)
from app.services.expert_collaborations import ExpertCollaborationService
from app.services.runtime_chat import (
    RuntimeChatPreparationError,
    RuntimeChatService,
    RuntimeExpertSessionDeletedError,
    SqlAlchemyRuntimeChatStore,
)
from app.services.runtime_workspace import Principal


router = APIRouter(prefix="/chat", tags=["user-chat"])
timing_logger = logging.getLogger("uvicorn.error.runtime_timing")

_CHAT_SSE_HEARTBEAT_SECONDS = 15.0
_CHAT_SSE_HEARTBEAT = ": keepalive\n\n"
_ACTIVITY_EVENT_NAMES = frozenset(
    {
        "chat.reasoning.delta",
        "chat.activity.delta",
        "chat.progress.delta",
        "chat.message.delta",
        "chat.tool.event",
        "chat.subagent.event",
        "chat.turn.parked",
        "chat.turn.resumed",
        "chat.transport.reconnecting",
        "chat.transport.resumed",
        "chat.self_evolution.event",
        "chat.error",
        "chat.done",
    }
)


def _safe_timing_label(value: str | None) -> str:
    if (
        value
        and len(value) <= 100
        and all(
            character.isascii()
            and (character.isalnum() or character in "._:-")
            for character in value
        )
    ):
        return value
    return "unknown"


async def _owned_background_collaborations(
    *,
    principal: Principal,
    runtime_workspace_id: int,
    runtime_session_id: str | None,
) -> ExpertCollaborationListResponse:
    async with AsyncSessionLocal() as db:
        return await ExpertCollaborationService(db).list_owned(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )


def _activity_text(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _sanitize_activity_tool_text(value: object) -> str | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            pass
    return sanitize_public_tool_text(value)


def _public_activity_event(
    *,
    event_name: str,
    payload: dict,
    seq: int,
) -> RuntimeRunActivityEvent | None:
    if event_name not in _ACTIVITY_EVENT_NAMES:
        return None
    data: dict[str, object] = {"seq": seq}
    if event_name in {
        "chat.reasoning.delta",
        "chat.activity.delta",
        "chat.progress.delta",
        "chat.message.delta",
    }:
        delta = _activity_text(payload, "delta")
        if delta is not None:
            data["delta"] = delta
        if event_name == "chat.progress.delta":
            for key in ("source", "state"):
                value = _activity_text(payload, key)
                if value:
                    data[key] = value
    elif event_name == "chat.tool.event":
        sanitized = sanitize_public_tool_history_message(
            {"role": "tool", **payload}, seq
        )
        data.update(
            {key: value for key, value in sanitized.items() if key != "role"}
        )
        for key in ("input", "preview", "result"):
            if key not in payload:
                continue
            safe_text = _sanitize_activity_tool_text(payload[key])
            if safe_text:
                data[key] = safe_text
            else:
                data.pop(key, None)
    elif event_name == "chat.subagent.event":
        for key in (
            "type",
            "status",
            "subagent_id",
            "delegation_id",
            "task_index",
            "goal",
            "text",
            "tool_preview",
            "summary",
        ):
            value = payload.get(key)
            if isinstance(value, (str, int, float, bool)):
                data[key] = value
    elif event_name == "chat.turn.parked":
        for key in ("task_id", "activity", "summary", "goal"):
            value = _activity_text(payload, key)
            if value:
                data[key] = value
    elif event_name == "chat.self_evolution.event":
        status_value = _activity_text(payload, "status")
        if status_value in {"running", "completed", "failed", "expired"}:
            data["status"] = status_value
        for key in ("activity", "summary"):
            value = _activity_text(payload, key)
            if value:
                data[key] = value
    elif event_name == "chat.error":
        for key in ("message", "code"):
            value = _activity_text(payload, key)
            if value:
                data[key] = value
    elif event_name == "chat.done":
        status_value = _activity_text(payload, "status")
        if status_value:
            data["status"] = status_value
    return RuntimeRunActivityEvent(event=event_name, data=data)


class InMemoryRuntimeChatStore:
    async def get_runtime_run_replay_control(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
    ):
        from app.services.runtime_chat import RuntimeRunReplayControl

        if org_id != 1 or user_id != 1:
            raise RuntimeChatPreparationError("runtime run not found")
        return RuntimeRunReplayControl(1, "rs-1-1", runtime_run_id, "completed")

    async def get_active_runtime_run(self, **_kwargs):
        return None

    async def list_runtime_run_events(self, **_kwargs):
        return []

    async def get_runtime_session_history_control(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ):
        from app.hermes_runtime.schemas import RuntimeEndpoint
        from app.services.runtime_chat import RuntimeSessionHistoryControl

        if (
            org_id != 1
            or user_id != 1
            or runtime_workspace_id != 1
            or runtime_session_id != "rs-1-1"
        ):
            raise RuntimeChatPreparationError("runtime session not found")
        return RuntimeSessionHistoryControl(
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            session_id=1,
            endpoint=RuntimeEndpoint(
                runtime_workspace_id=runtime_workspace_id,
                runtime_id=f"rt-{runtime_workspace_id}",
                endpoint_base_url="http://runtime.example",
                endpoint_host_header="runtime.example",
                api_key="runtime-token",
            ),
        )


_runtime_chat_store = InMemoryRuntimeChatStore()


async def get_runtime_chat_service(
    db: AsyncSession = Depends(get_db),
) -> RuntimeChatService:
    if settings.RUNTIME_CHAT_MODE == "real":
        return RuntimeChatService(SqlAlchemyRuntimeChatStore(db))
    return RuntimeChatService(_runtime_chat_store)


async def get_chat_log_projector():
    if settings.RUNTIME_CHAT_MODE != "real":
        yield None
        return
    async with AsyncSessionLocal() as db:
        yield ChatLogProjector(SqlAlchemyChatLogStore(db))


def _history_messages(payload: dict) -> list[dict]:
    messages = payload.get("messages", payload.get("data", []))
    if not isinstance(messages, list):
        return []
    public_messages: list[dict] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            continue
        normalized = normalize_internal_history_message(item, index)
        if normalized is None:
            continue
        if str(normalized.get("role") or "") == "tool":
            normalized = sanitize_public_tool_history_message(normalized, index)
        public_messages.append(normalized)
    return public_messages


@router.get("/runs/active")
async def get_active_chat_run(
    runtime_workspace_id: int,
    runtime_session_id: str,
    principal: Principal = Depends(get_principal),
    service: RuntimeChatService = Depends(get_runtime_chat_service),
) -> dict:
    active = await service.get_active_run(
        principal=principal,
        runtime_workspace_id=runtime_workspace_id,
        runtime_session_id=runtime_session_id,
    )
    if active is None:
        return {"run": None}
    return {
        "run": {
            "runtime_workspace_id": active.runtime_workspace_id,
            "runtime_session_id": active.runtime_session_id,
            "runtime_run_id": active.runtime_run_id,
            "status": active.status,
            "last_seq": active.last_seq,
            "history_message_count_before_run": (
                active.history_message_count_before_run
            ),
            **({"source": active.source} if active.source is not None else {}),
        }
    }


@router.get(
    "/background-collaborations",
    response_model=ExpertCollaborationListResponse,
)
async def list_background_collaborations(
    runtime_workspace_id: int,
    runtime_session_id: str | None = None,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> ExpertCollaborationListResponse:
    return await ExpertCollaborationService(db).list_owned(
        org_id=principal.org_id,
        user_id=principal.user_id,
        runtime_workspace_id=runtime_workspace_id,
        runtime_session_id=runtime_session_id,
    )


@router.get("/background-collaborations/stream")
async def stream_background_collaborations(
    runtime_workspace_id: int,
    runtime_session_id: str | None = Query(default=None),
    principal: Principal = Depends(get_principal),
) -> StreamingResponse:
    poll_seconds = settings.EXPERT_COLLABORATION_SSE_POLL_SECONDS

    async def event_stream():
        seen: dict[str, tuple[int, str | None]] = {}
        heartbeat_elapsed = 0.0
        first = True
        while True:
            current = await _owned_background_collaborations(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=runtime_session_id,
            )
            if first:
                yield PlatformStreamEvent(
                    event="chat.background.snapshot",
                    data=current.model_dump(mode="json"),
                ).to_sse()
                seen.update(
                    {
                        item.collaboration_id: (
                            int(item.revision),
                            item.final_runtime_run_id,
                        )
                        for item in current.collaborations
                    }
                )
                first = False
            for item in reversed(current.collaborations):
                marker = (int(item.revision), item.final_runtime_run_id)
                if seen.get(item.collaboration_id) == marker:
                    continue
                seen[item.collaboration_id] = marker
                event_name = (
                    "chat.background.run.created"
                    if item.final_runtime_run_id
                    else "chat.background.status"
                )
                yield PlatformStreamEvent(
                    event=event_name,
                    data=item.model_dump(mode="json"),
                ).to_sse()
            if current.pending_count == 0:
                yield PlatformStreamEvent(
                    event="chat.background.done",
                    data={
                        "runtime_workspace_id": runtime_workspace_id,
                        "runtime_session_id": runtime_session_id,
                    },
                ).to_sse()
                return
            await asyncio.sleep(poll_seconds)
            heartbeat_elapsed += poll_seconds
            if heartbeat_elapsed >= _CHAT_SSE_HEARTBEAT_SECONDS:
                heartbeat_elapsed = 0.0
                yield _CHAT_SSE_HEARTBEAT

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/runs/{runtime_run_id}/client-timing",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def record_client_timing(
    runtime_run_id: str,
    payload: RuntimeChatClientTimingRequest,
    principal: Principal = Depends(get_principal),
    service: RuntimeChatService = Depends(get_runtime_chat_service),
    db: AsyncSession = Depends(get_db),
) -> Response:
    if runtime_run_id.startswith("app_run_"):
        owned_studio_run_id = await db.scalar(
            select(ApplicationAgentRun.id)
            .join(
                ApplicationAgent,
                ApplicationAgent.id == ApplicationAgentRun.application_agent_id,
            )
            .where(
                ApplicationAgentRun.runtime_run_id == runtime_run_id,
                ApplicationAgentRun.org_id == principal.org_id,
                ApplicationAgentRun.channel == "studio",
                ApplicationAgent.org_id == principal.org_id,
                ApplicationAgent.created_by_user_id == principal.user_id,
            )
        )
        if owned_studio_run_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="runtime run not found",
            )
    else:
        try:
            await service.get_run_replay_control(
                principal=principal,
                runtime_run_id=runtime_run_id,
            )
        except RuntimeExpertSessionDeletedError as exc:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=exc.code,
            ) from exc
        except RuntimeChatPreparationError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="runtime run not found",
            ) from exc
    timing_logger.info(
        "runtime client timing run_id=%s sample_kind=%s visibility=%s paint_ms=%.3f commit_ms=%.3f "
        "received_ms=%.3f dispatch_paint_ms=%s dispatch_commit_ms=%s "
        "dispatch_received_ms=%s queued_wait_ms=%.3f receipt_age_ms=%s "
        "first_event_ms=%s reconnect=%s prewarm_overlap=%s "
        "required_prewarm_wait_ms=%s build=%s",
        runtime_run_id,
        payload.sample_kind,
        payload.document_visibility,
        payload.request_to_first_visible_paint_ms,
        payload.request_to_first_visible_commit_ms,
        payload.request_to_first_visible_received_ms,
        payload.dispatch_to_first_visible_paint_ms,
        payload.dispatch_to_first_visible_commit_ms,
        payload.dispatch_to_first_visible_received_ms,
        payload.queued_wait_ms,
        payload.receipt_age_ms,
        payload.request_to_first_event_ms,
        payload.reconnect_before_first_visible,
        payload.send_overlapped_prewarm,
        payload.required_prewarm_wait_ms,
        _safe_timing_label(payload.frontend_build),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/history", response_model=RuntimeChatHistoryResponse)
async def get_chat_history(
    runtime_workspace_id: int,
    runtime_session_id: str,
    principal: Principal = Depends(get_principal),
    service: RuntimeChatService = Depends(get_runtime_chat_service),
    chat_log_projector: ChatLogProjector | None = Depends(get_chat_log_projector),
) -> RuntimeChatHistoryResponse:
    if chat_log_projector is not None:
        try:
            await chat_log_projector.project_owned_session(
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=runtime_session_id,
            )
            chat_log_history = await chat_log_projector.history_for_owner(
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=runtime_session_id,
            )
        except ChatLogSessionNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        return RuntimeChatHistoryResponse(
            runtime_workspace_id=chat_log_history.runtime_workspace_id,
            runtime_session_id=chat_log_history.runtime_session_id,
            source="mysql",
            consistency="projection_preview",
            generated_at=datetime.now(timezone.utc),
            has_active_run=chat_log_history.has_active_run,
            messages=_history_messages(
                {"messages": chat_log_history.messages}
            ),
        )

    try:
        control = await service.get_session_history_control(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
    except RuntimeExpertSessionDeletedError as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=exc.code,
        ) from exc
    except RuntimeChatPreparationError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return RuntimeChatHistoryResponse(
        runtime_workspace_id=control.runtime_workspace_id,
        runtime_session_id=control.runtime_session_id,
        source="mysql",
        consistency="projection_preview",
        generated_at=datetime.now(timezone.utc),
        has_active_run=False,
        messages=[],
    )


@router.get(
    "/runs/{runtime_run_id}/activity",
    response_model=RuntimeRunActivityResponse,
)
async def get_run_activity(
    runtime_run_id: str,
    runtime_workspace_id: int,
    runtime_session_id: str,
    principal: Principal = Depends(get_principal),
    service: RuntimeChatService = Depends(get_runtime_chat_service),
) -> RuntimeRunActivityResponse:
    try:
        control = await service.get_run_replay_control(
            principal=principal,
            runtime_run_id=runtime_run_id,
        )
        if (
            control.runtime_workspace_id != runtime_workspace_id
            or control.runtime_session_id != runtime_session_id
        ):
            raise RuntimeChatPreparationError("runtime run not found")
        settle_self_evolution = getattr(
            service, "settle_self_evolution_review", None
        )
        if callable(settle_self_evolution):
            await settle_self_evolution(
                principal=principal,
                runtime_run_id=runtime_run_id,
            )
        events = await service.list_events(
            principal=principal,
            runtime_run_id=runtime_run_id,
        )
    except RuntimeChatPreparationError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    public_events = [
        event
        for record in events
        if (
            event := _public_activity_event(
                event_name=record.event_name,
                payload=record.payload or {},
                seq=int(record.seq),
            )
        )
        is not None
    ]
    if not public_events:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="trace_expired",
        )
    return RuntimeRunActivityResponse(
        runtime_workspace_id=control.runtime_workspace_id,
        runtime_session_id=runtime_session_id,
        runtime_run_id=control.runtime_run_id,
        status=control.status,
        events=public_events,
    )
