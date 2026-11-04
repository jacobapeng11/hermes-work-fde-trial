from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ChatLog,
    CloudHermesProfile,
    RuntimeRun,
    RuntimeRunEvent,
    RuntimeSession,
)
from app.hermes_runtime.public_history_message import normalize_internal_history_message


logger = logging.getLogger(__name__)

_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_ACTIVE_RUN_STATUSES = {"accepted", "pending", "running"}
_TERMINAL_RUN_STATUSES = {"completed", "failed", "canceled", "cancel_failed"}
_SUBAGENT_EVENT_TYPES = {
    "subagent.start",
    "subagent.progress",
    "subagent.complete",
    "subagent.failed",
    "subagent.error",
    "subagent.interrupted",
    "subagent.canceled",
}


class ChatLogSessionNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatLogHistoryControl:
    runtime_workspace_id: int
    runtime_session_id: str
    session_id: int
    complete: bool
    has_active_run: bool
    messages: list[dict]


class ChatLogStore(Protocol):
    async def list_pending_session_ids(self, *, limit: int) -> list[int]:
        ...

    async def get_owned_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSession | None:
        ...

    async def lock_owned_session_for_projection(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSession | None:
        ...

    async def lock_session_for_projection(
        self,
        *,
        session_id: int,
    ) -> RuntimeSession | None:
        ...

    async def list_session_runs(self, *, session_id: int) -> list[RuntimeRun]:
        ...

    async def list_session_logs(self, *, session_id: int) -> list[ChatLog]:
        ...

    async def list_run_events(self, *, run_id: int) -> list[RuntimeRunEvent]:
        ...

    def add_log(self, record: ChatLog) -> None:
        ...

    async def commit(self) -> None:
        ...

    async def rollback(self) -> None:
        ...


def assistant_text(events: Sequence[RuntimeRunEvent]) -> str:
    chunks: list[str] = []
    for event in sorted(events, key=lambda item: int(item.seq)):
        if event.event_name != "chat.message.delta":
            continue
        delta = (event.payload or {}).get("delta")
        if isinstance(delta, str):
            chunks.append(delta)
    return "".join(chunks)


def terminal_assistant_text(events: Sequence[RuntimeRunEvent]) -> str:
    for event in reversed(sorted(events, key=lambda item: int(item.seq))):
        if event.event_name != "chat.message.completed":
            continue
        if (event.payload or {}).get("interim") is True:
            continue
        text = (event.payload or {}).get("text")
        if isinstance(text, str) and text.strip():
            return text
    return assistant_text(events)


def reasoning_text(events: Sequence[RuntimeRunEvent]) -> str:
    chunks: list[str] = []
    for event in sorted(events, key=lambda item: int(item.seq)):
        if event.event_name != "chat.reasoning.delta":
            continue
        delta = (event.payload or {}).get("delta")
        if isinstance(delta, str):
            chunks.append(delta)
    return "".join(chunks)


def _stable_subagent_task_id(payload: dict[str, Any]) -> str | None:
    if str(payload.get("type") or "") not in _SUBAGENT_EVENT_TYPES:
        return None
    for key in ("subagent_id", "delegation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def run_activity_history_messages(
    events: Sequence[RuntimeRunEvent],
    *,
    runtime_run_id: str | None = None,
) -> list[dict]:
    """Rebuild user-visible technical activity for a completed RuntimeRun.

    The result intentionally contains raw tool fields only inside the service
    boundary.  ``get_chat_history`` applies the same public sanitizer used for
    Hermes-provided history before this list reaches a browser.
    """
    messages: list[dict] = []
    legacy_message_deltas_are_activity = any(
        event.event_name == "chat.message.completed"
        and (event.payload or {}).get("interim") is not True
        for event in events
    )
    reasoning_chunks: list[str] = []
    reasoning_event_seq: int | None = None
    activity_chunks: list[str] = []
    activity_event_seq: int | None = None
    stable_tool_indexes: dict[str, int] = {}
    unstable_running_tool_indexes: dict[str, list[int]] = {}
    background_indexes: dict[str, int] = {}

    def flush_reasoning() -> None:
        nonlocal reasoning_event_seq
        if reasoning_chunks:
            messages.append({
                "role": "assistant",
                "kind": "reasoning",
                "text": "".join(reasoning_chunks),
                **({"runtimeRunId": runtime_run_id} if runtime_run_id else {}),
                **(
                    {"eventSeq": reasoning_event_seq}
                    if runtime_run_id and reasoning_event_seq is not None
                    else {}
                ),
            })
            reasoning_chunks.clear()
            reasoning_event_seq = None

    def flush_activity() -> None:
        nonlocal activity_event_seq
        if activity_chunks:
            messages.append({
                "role": "assistant",
                "kind": "activity",
                "text": "".join(activity_chunks),
                **({"runtimeRunId": runtime_run_id} if runtime_run_id else {}),
                **(
                    {"eventSeq": activity_event_seq}
                    if runtime_run_id and activity_event_seq is not None
                    else {}
                ),
            })
            activity_chunks.clear()
            activity_event_seq = None

    def trim_terminal_activity_suffix(terminal_text: str) -> None:
        if not terminal_text or not messages:
            return
        previous = messages[-1]
        if previous.get("kind") != "activity":
            return
        activity_text = previous.get("text")
        if not isinstance(activity_text, str) or not activity_text.endswith(terminal_text):
            return
        remaining_text = activity_text[: -len(terminal_text)]
        if remaining_text:
            messages[-1] = {**previous, "text": remaining_text}
        else:
            messages.pop()

    for event in sorted(events, key=lambda item: int(item.seq)):
        payload = event.payload or {}
        if event.event_name == "chat.reasoning.delta":
            flush_activity()
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                if reasoning_event_seq is None:
                    reasoning_event_seq = int(event.seq)
                reasoning_chunks.append(delta)
            continue

        if event.event_name == "chat.message.completed":
            if payload.get("interim") is not True:
                flush_activity()
                flush_reasoning()
                terminal_text = payload.get("text")
                if isinstance(terminal_text, str):
                    trim_terminal_activity_suffix(terminal_text)
            continue

        # ``chat.message.delta`` is the legacy spelling for pre-terminal
        # Agent progress.  A terminal answer is always sourced from the
        # non-interim ``chat.message.completed`` event instead.
        if event.event_name == "chat.activity.delta" or (
            event.event_name == "chat.message.delta" and legacy_message_deltas_are_activity
        ):
            flush_reasoning()
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                if activity_event_seq is None:
                    activity_event_seq = int(event.seq)
                activity_chunks.append(delta)
            continue

        if event.event_name == "chat.tool.event":
            flush_activity()
            flush_reasoning()
            name = str(payload.get("name") or payload.get("toolName") or payload.get("tool_name") or "tool")
            provider_call_id = payload.get("callId") or payload.get("toolCallId") or payload.get("tool_call_id") or payload.get("call_id")
            provider_call_id = provider_call_id if isinstance(provider_call_id, str) and provider_call_id else None
            stable = bool(payload.get("hasStableCallId") or payload.get("has_stable_call_id") or provider_call_id)
            status = str(payload.get("status") or "running")
            if status not in {"completed", "failed"}:
                status = "running"
            current = {
                "role": "tool",
                "name": name,
                "status": status,
                "hasStableCallId": stable,
                **({"runtimeRunId": runtime_run_id} if runtime_run_id else {}),
                **({"eventSeq": int(event.seq)} if runtime_run_id else {}),
            }
            if provider_call_id:
                current["callId"] = provider_call_id
            for field in ("input", "preview", "result"):
                if payload.get(field) is not None:
                    current[field] = payload[field]

            index: int | None = None
            if stable and provider_call_id:
                index = stable_tool_indexes.get(provider_call_id)
                if index is None:
                    stable_tool_indexes[provider_call_id] = len(messages)
            elif status == "running":
                unstable_running_tool_indexes.setdefault(name, []).append(len(messages))
            else:
                running = unstable_running_tool_indexes.get(name, [])
                index = running.pop(0) if running else None

            if index is None:
                messages.append(current)
            else:
                messages[index] = {**messages[index], **current}
            continue

        if event.event_name == "chat.subagent.event":
            flush_activity()
            flush_reasoning()
            task_id = _stable_subagent_task_id(payload)
            if task_id is None:
                continue
            event_type = str(payload.get("type") or "")
            raw_status = str(payload.get("status") or "").lower()
            terminal_by_type = {
                "subagent.complete": "completed",
                "subagent.failed": "failed",
                "subagent.error": "failed",
                "subagent.interrupted": "interrupted",
                "subagent.canceled": "interrupted",
            }
            if raw_status in {"failed", "error", "timeout", "failure"}:
                status = "failed"
            elif raw_status in {"interrupted", "canceled", "cancelled"}:
                status = "interrupted"
            elif raw_status in {"queued", "running", "completed"}:
                status = raw_status
            else:
                status = terminal_by_type.get(event_type, "running")
            current = {
                "role": "system",
                "kind": "background",
                "taskId": task_id,
                "status": status,
                **({"runtimeRunId": runtime_run_id} if runtime_run_id else {}),
                **({"eventSeq": int(event.seq)} if runtime_run_id else {}),
            }
            for field in ("goal", "summary"):
                if isinstance(payload.get(field), str) and payload[field]:
                    current[field] = payload[field]
            activity = payload.get("text") or payload.get("tool_preview")
            if isinstance(activity, str) and activity:
                current["activity"] = activity
            index = background_indexes.get(task_id)
            if index is None:
                background_indexes[task_id] = len(messages)
                messages.append(current)
            else:
                messages[index] = {**messages[index], **current}

        if event.event_name == "chat.self_evolution.event":
            flush_activity()
            flush_reasoning()
            raw_status = str(payload.get("status") or "")
            status = raw_status if raw_status in {"queued", "running", "completed", "failed", "interrupted"} else "completed"
            current = {
                "role": "system",
                "kind": "background",
                "taskId": "self-evolution",
                "status": status,
                **({"runtimeRunId": runtime_run_id} if runtime_run_id else {}),
                **({"eventSeq": int(event.seq)} if runtime_run_id else {}),
            }
            for field in ("activity", "summary"):
                if isinstance(payload.get(field), str) and payload[field]:
                    current[field] = payload[field]
            index = background_indexes.get("self-evolution")
            if index is None:
                background_indexes["self-evolution"] = len(messages)
                messages.append(current)
            else:
                messages[index] = {**messages[index], **current}

    flush_activity()
    flush_reasoning()
    return messages


def public_source_detail(value: object) -> dict | None:
    if not isinstance(value, dict) or value.get("type") != "scheduled":
        return None
    task_id = value.get("scheduled_task_id")
    task_name = value.get("scheduled_task_name")
    trigger_kind = value.get("trigger_kind")
    if not isinstance(task_id, int) or not isinstance(task_name, str) or not isinstance(trigger_kind, str):
        return None
    return {
        "type": "scheduled",
        "scheduled_task_id": task_id,
        "scheduled_task_name": task_name,
        "trigger_kind": trigger_kind,
        "task_available": value.get("task_available") is True,
    }


def public_history_messages(items: list[dict]) -> list[dict]:
    messages: list[dict] = []
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            continue
        item = normalize_internal_history_message(dict(raw_item), index)
        if item is None:
            continue
        role = str(item.get("role") or "")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str) or not content:
            continue
        message = {"role": role, "content": content}
        source = public_source_detail(item.get("source")) if role == "user" else None
        if source is not None:
            message["source"] = source
        messages.append(message)
    return messages


def validate_channel(value: object, *, fallback: str = "unknown") -> str:
    candidate = str(value or "").strip().lower()
    return candidate if _CHANNEL_RE.fullmatch(candidate) else fallback


def _run_audit_message(run: RuntimeRun) -> str:
    payload = run.request_payload or {}
    value = payload.get("audit_message")
    if not isinstance(value, str):
        value = payload.get("message")
    return value if isinstance(value, str) else ""


def _run_terminal_answer(run: RuntimeRun) -> str:
    value = (run.response_summary or {}).get("answer")
    return value if isinstance(value, str) else ""


def _run_source_detail(run: RuntimeRun) -> dict | None:
    detail = public_source_detail((run.request_payload or {}).get("source"))
    attachment_ids = sorted(
        {
            int(file_id)
            for file_id in (run.request_payload or {}).get(
                "attachment_file_ids", []
            )
            if isinstance(file_id, int)
        }
    )
    if not attachment_ids:
        return detail
    return {
        **(detail or {}),
        "attachment_file_ids": attachment_ids,
    }


def _history_message(record: ChatLog) -> dict:
    message = {
        "role": str(record.role),
        "content": str(record.content),
        "messageSeq": int(record.message_seq),
        **(
            {"contextAgentProfileId": int(record.context_runtime_agent_profile_id)}
            if record.context_runtime_agent_profile_id is not None
            else {}
        ),
        **(
            {"runtimeRunId": str(record.runtime_run_id)}
            if record.runtime_run_id
            else {}
        ),
    }
    if record.role == "user" and record.source_detail:
        source = public_source_detail(record.source_detail)
        if source is not None:
            message["source"] = source
    return message


class ChatLogProjector:
    def __init__(self, store: ChatLogStore) -> None:
        self.store = store

    async def project_owned_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> int:
        session = await self.store.get_owned_session(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
        if session is None:
            return 0
        return await self.project_session_pending(session_id=int(session.id))

    async def project_session_pending(self, *, session_id: int) -> int:
        session = await self.store.lock_session_for_projection(
            session_id=session_id
        )
        if session is None or session.status != "active" or session.chat_log_backfilled_at is None:
            await self.store.rollback()
            return 0

        runs = await self.store.list_session_runs(session_id=session_id)
        logs = await self.store.list_session_logs(session_id=session_id)
        existing = {
            (str(item.runtime_run_id), str(item.role))
            for item in logs
            if item.runtime_run_id
        }
        next_seq = max((int(item.message_seq) for item in logs), default=-1) + 1
        added = 0
        changed = False
        now = datetime.now(timezone.utc)

        for run in runs:
            if run.chat_log_projected_at is not None:
                continue
            channel = validate_channel((run.request_payload or {}).get("channel"))
            source_detail = _run_source_detail(run)
            audit_message = _run_audit_message(run)
            user_key = (str(run.runtime_run_id), "user")
            if audit_message and user_key not in existing:
                self.store.add_log(
                    self._run_log(
                        session=session,
                        run=run,
                        message_seq=next_seq,
                        role="user",
                        content=audit_message,
                        channel=channel,
                        source_detail=source_detail,
                    )
                )
                existing.add(user_key)
                next_seq += 1
                added += 1
                changed = True

            if run.status == "completed":
                assistant_key = (str(run.runtime_run_id), "assistant")
                if assistant_key not in existing:
                    text = _run_terminal_answer(run)
                    if not text:
                        text = terminal_assistant_text(
                            await self.store.list_run_events(
                                run_id=int(run.id)
                            )
                        )
                    if text:
                        self.store.add_log(
                            self._run_log(
                                session=session,
                                run=run,
                                message_seq=next_seq,
                                role="assistant",
                                content=text,
                                channel=channel,
                                source_detail=None,
                            )
                        )
                        existing.add(assistant_key)
                        next_seq += 1
                        added += 1
                run.chat_log_projected_at = now
                changed = True
            elif run.status in _TERMINAL_RUN_STATUSES:
                run.chat_log_projected_at = now
                changed = True

        if changed:
            await self.store.commit()
        else:
            await self.store.rollback()
        return added

    async def project_pending_sessions(self, *, limit: int = 50) -> int:
        projected = 0
        for session_id in await self.store.list_pending_session_ids(limit=limit):
            try:
                projected += await self.project_session_pending(session_id=session_id)
            except Exception as exc:
                await self.store.rollback()
                logger.error(
                    "chat log projection failed session_id=%s error_type=%s",
                    session_id,
                    type(exc).__name__,
                )
        return projected

    async def history_for_owner(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> ChatLogHistoryControl:
        session = await self.store.get_owned_session(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
        if session is None:
            raise ChatLogSessionNotFound("runtime session not found")
        runs = await self.store.list_session_runs(session_id=int(session.id))
        has_active_run = any(
            str(run.status) in _ACTIVE_RUN_STATUSES
            for run in runs
        )
        if session.chat_log_backfilled_at is None:
            return ChatLogHistoryControl(
                runtime_workspace_id=int(session.runtime_workspace_id),
                runtime_session_id=str(session.runtime_session_id),
                session_id=int(session.id),
                complete=False,
                has_active_run=has_active_run,
                messages=[],
            )

        logs = await self.store.list_session_logs(session_id=int(session.id))
        runs_by_public_id = {str(item.runtime_run_id): item for item in runs}
        events_by_run_id: dict[int, list[RuntimeRunEvent]] = {}

        async def events_for(run: RuntimeRun) -> list[RuntimeRunEvent]:
            run_id = int(run.id)
            if run_id not in events_by_run_id:
                events_by_run_id[run_id] = await self.store.list_run_events(run_id=run_id)
            return events_by_run_id[run_id]

        messages: list[dict] = []
        for item in logs:
            message = _history_message(item)
            run = runs_by_public_id.get(str(item.runtime_run_id)) if item.runtime_run_id else None
            if run is not None and item.role == "user":
                client_message_id = (run.request_payload or {}).get("client_message_id")
                if isinstance(client_message_id, str) and client_message_id:
                    message["clientMessageId"] = client_message_id
            if item.role == "assistant" and run is not None:
                messages.extend(run_activity_history_messages(
                    await events_for(run),
                    runtime_run_id=str(run.runtime_run_id),
                ))
            messages.append(message)
        existing = {
            (str(item.runtime_run_id), str(item.role))
            for item in logs
            if item.runtime_run_id
        }
        for run in runs:
            if run.chat_log_projected_at is not None:
                continue
            user_key = (str(run.runtime_run_id), "user")
            audit_message = _run_audit_message(run)
            if audit_message and user_key not in existing:
                message = {
                    "role": "user",
                    "content": audit_message,
                    "runtimeRunId": str(run.runtime_run_id),
                }
                client_message_id = (run.request_payload or {}).get("client_message_id")
                if isinstance(client_message_id, str) and client_message_id:
                    message["clientMessageId"] = client_message_id
                source = _run_source_detail(run)
                if source is not None:
                    message["source"] = source
                messages.append(message)
                existing.add(user_key)
            assistant_key = (str(run.runtime_run_id), "assistant")
            if run.status == "completed" and assistant_key not in existing:
                events = await events_for(run)
                text = _run_terminal_answer(run)
                if not text:
                    text = terminal_assistant_text(events)
                if text:
                    message = {
                        "role": "assistant",
                        "content": text,
                        "runtimeRunId": str(run.runtime_run_id),
                    }
                    messages.extend(run_activity_history_messages(
                        events,
                        runtime_run_id=str(run.runtime_run_id),
                    ))
                    messages.append(message)
                    existing.add(assistant_key)

        return ChatLogHistoryControl(
            runtime_workspace_id=int(session.runtime_workspace_id),
            runtime_session_id=str(session.runtime_session_id),
            session_id=int(session.id),
            complete=True,
            has_active_run=has_active_run,
            messages=messages,
        )

    async def backfill_snapshot(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        messages: list[dict],
    ) -> bool:
        session = await self.store.lock_owned_session_for_projection(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
        if session is None or session.status != "active":
            await self.store.rollback()
            return False
        if session.chat_log_backfilled_at is not None:
            await self.store.rollback()
            return True

        runs = await self.store.list_session_runs(session_id=int(session.id))
        if any(run.status in _ACTIVE_RUN_STATUSES for run in runs):
            await self.store.rollback()
            return False

        public = public_history_messages(messages)
        existing = {
            int(item.message_seq): item
            for item in await self.store.list_session_logs(session_id=int(session.id))
        }
        now = datetime.now(timezone.utc)
        for message_seq, message in enumerate(public):
            current = existing.get(message_seq)
            if current is not None:
                if current.role != message["role"] or current.content != message["content"]:
                    await self.store.rollback()
                    return False
                continue
            source_detail = public_source_detail(message.get("source"))
            self.store.add_log(
                ChatLog(
                    org_id=int(session.org_id),
                    user_id=int(session.user_id),
                    runtime_workspace_id=int(session.runtime_workspace_id),
                    session_id=int(session.id),
                    runtime_session_id=str(session.runtime_session_id),
                    runtime_run_id=None,
                    message_seq=message_seq,
                    role=str(message["role"]),
                    content=str(message["content"]),
                    content_format="text" if message["role"] == "user" else "markdown",
                    message_status="accepted" if message["role"] == "user" else "completed",
                    channel="unknown",
                    source_type=(
                        str(source_detail["type"])
                        if source_detail is not None
                        else str(session.source_type or "web")
                    ),
                    source_detail=source_detail,
                    context_runtime_agent_profile_id=int(
                        session.runtime_agent_profile_id
                    ),
                    model_entry_id=None,
                    ingest_source="hermes_backfill",
                    occurred_at=now,
                    completed_at=now if message["role"] == "assistant" else None,
                )
            )

        session.chat_log_backfilled_at = now
        for run in runs:
            if run.status in _TERMINAL_RUN_STATUSES:
                run.chat_log_projected_at = now
        await self.store.commit()
        return True

    @staticmethod
    def _run_log(
        *,
        session: RuntimeSession,
        run: RuntimeRun,
        message_seq: int,
        role: str,
        content: str,
        channel: str,
        source_detail: dict | None,
    ) -> ChatLog:
        occurred_at = run.started_at or run.created_at or datetime.now(timezone.utc)
        return ChatLog(
            org_id=int(run.org_id),
            user_id=int(run.user_id),
            runtime_workspace_id=int(run.runtime_workspace_id),
            session_id=int(session.id),
            runtime_session_id=str(run.runtime_session_id or session.runtime_session_id),
            runtime_run_id=str(run.runtime_run_id),
            message_seq=message_seq,
            role=role,
            content=content,
            content_format="text" if role == "user" else "markdown",
            message_status="accepted" if role == "user" else "completed",
            channel=channel,
            channel_account_id=(run.request_payload or {}).get("channel_account_id"),
            external_conversation_id=(run.request_payload or {}).get(
                "external_conversation_id"
            ),
            external_message_id=(run.request_payload or {}).get("external_message_id"),
            source_type=str(run.source_type or "web"),
            source_detail=source_detail,
            context_runtime_agent_profile_id=int(session.runtime_agent_profile_id),
            model_entry_id=int(run.model_entry_id) if role == "assistant" and run.model_entry_id else None,
            ingest_source="runtime_run",
            occurred_at=occurred_at,
            completed_at=run.finished_at if role == "assistant" else occurred_at,
        )


class SqlAlchemyChatLogStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_pending_session_ids(self, *, limit: int) -> list[int]:
        result = await self.db.execute(
            select(RuntimeRun.session_id)
            .join(RuntimeSession, RuntimeSession.id == RuntimeRun.session_id)
            .where(
                RuntimeRun.session_id.is_not(None),
                RuntimeRun.chat_log_projected_at.is_(None),
                RuntimeSession.status == "active",
                RuntimeSession.chat_log_backfilled_at.is_not(None),
            )
            .distinct()
            .order_by(RuntimeRun.session_id.asc())
            .limit(limit)
        )
        return [int(row[0]) for row in result.all() if row[0] is not None]

    async def get_owned_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSession | None:
        return (
            await self.db.execute(
                select(RuntimeSession).where(
                    RuntimeSession.org_id == org_id,
                    RuntimeSession.user_id == user_id,
                    RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                    RuntimeSession.runtime_session_id == runtime_session_id,
                    RuntimeSession.status.in_(("active", "expert_deleted")),
                )
            )
        ).scalar_one_or_none()

    async def lock_owned_session_for_projection(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSession | None:
        if not await self._lock_projection_parent(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
        ):
            return None
        return (
            await self.db.execute(
                select(RuntimeSession)
                .where(
                    RuntimeSession.org_id == org_id,
                    RuntimeSession.user_id == user_id,
                    RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                    RuntimeSession.runtime_session_id == runtime_session_id,
                    RuntimeSession.status == "active",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def lock_session_for_projection(
        self,
        *,
        session_id: int,
    ) -> RuntimeSession | None:
        scope = (
            await self.db.execute(
                select(
                    RuntimeSession.org_id,
                    RuntimeSession.user_id,
                    RuntimeSession.runtime_workspace_id,
                ).where(RuntimeSession.id == session_id)
            )
        ).one_or_none()
        if scope is None:
            return None
        if not await self._lock_projection_parent(
            org_id=int(scope.org_id),
            user_id=int(scope.user_id),
            runtime_workspace_id=int(scope.runtime_workspace_id),
        ):
            return None
        return (
            await self.db.execute(
                select(RuntimeSession)
                .where(
                    RuntimeSession.id == session_id,
                    RuntimeSession.status == "active",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def _lock_projection_parent(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> bool:
        parent_id = await self.db.scalar(
            select(CloudHermesProfile.id)
            .where(
                CloudHermesProfile.id == runtime_workspace_id,
                CloudHermesProfile.org_id == org_id,
                CloudHermesProfile.user_id == user_id,
            )
            .with_for_update(read=True)
        )
        return parent_id is not None

    async def list_session_runs(self, *, session_id: int) -> list[RuntimeRun]:
        result = await self.db.execute(
            select(RuntimeRun)
            .where(RuntimeRun.session_id == session_id)
            .order_by(RuntimeRun.id.asc())
        )
        return list(result.scalars().all())

    async def list_session_logs(self, *, session_id: int) -> list[ChatLog]:
        result = await self.db.execute(
            select(ChatLog)
            .where(ChatLog.session_id == session_id)
            .order_by(ChatLog.message_seq.asc())
        )
        return list(result.scalars().all())

    async def list_run_events(self, *, run_id: int) -> list[RuntimeRunEvent]:
        result = await self.db.execute(
            select(RuntimeRunEvent)
            .where(RuntimeRunEvent.run_id == run_id)
            .order_by(RuntimeRunEvent.seq.asc())
        )
        return list(result.scalars().all())

    def add_log(self, record: ChatLog) -> None:
        self.db.add(record)

    async def commit(self) -> None:
        await self.db.commit()

    async def rollback(self) -> None:
        await self.db.rollback()
