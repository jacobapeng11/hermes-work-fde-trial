from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.hermes_runtime.constants import HERMES_WORKSPACE, runtime_session_workspace_path
from app.hermes_runtime.platform_session_contract import (
    platform_session_contract_digest,
)
from app.hermes_runtime.public_history_message import (
    normalize_internal_history_message,
)
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.hermes_runtime.events import PlatformStreamEvent
from app.hermes_runtime.dashboard_protocol import (
    HermesDashboardProtocolAdapter,
)
from app.models import (
    ApplicationAgent,
    ApplicationAgentClientGrant,
    ApplicationAgentConversation,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentStudioSession,
    ApplicationClient,
    ImInboundMessage,
    ImMessageAttachment,
    ModelEntry,
    RuntimeProjectionReceipt,
    User,
)
from app.schemas.application_agent_invocation import (
    ApplicationAgentProducedFile,
    ApplicationAgentResponseRequest,
)
from app.schemas.runtime_internal import (
    ChannelExecutionLease,
    InternalTurnCommandPayload,
    RuntimeTurnAttachment,
    RuntimeTurnIdentity,
)
from app.schemas.provider_routes import ProviderRouteGrantV1
from app.schemas.model_runtime_capabilities import (
    normalize_runtime_capabilities,
    supports_native_vision,
)
from app.services.application_agents import (
    InMemoryApplicationAgentService,
    _secret_digest,
)
from app.services.application_agent_execution_policy import (
    ApplicationAgentExecutionPolicyError,
    ApplicationAgentExecutionPolicyResolver,
    resolve_application_execution_config,
)
from app.services.application_agent_conversation_views import (
    SqlAlchemyApplicationAgentConversationViewService,
)
from app.services.application_agent_conversation_files import (
    ApplicationAgentConversationFileService,
    application_agent_file_download_url,
)
from app.services.runtime_chat_timing import RuntimeChatTiming
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec
from app.services.runtime_commands import (
    RuntimeCommandClient,
    RuntimeCommandError,
    RuntimeCommandTarget,
    build_runtime_command,
    deterministic_command_id,
    load_runtime_command_admission_generation,
)
from app.services.application_agent_execution_identity import application_asset_digests
from app.services.runtime_channel_fast_lane import (
    RuntimeChannelFastLaneClient,
    issue_channel_execution_lease,
)
from app.services.runtime_provider_routes import (
    ProviderRouteBindingIdentity,
    RuntimeProviderRouteError,
    RuntimeProviderRouteResolver,
)


def _latest_run_record(run: ApplicationAgentRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "runtime_run_id": str(run.runtime_run_id),
        "status": str(run.status),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


_APPLICATION_INVOCATION_TIMING_PAIRS = (
    ("invocation_authorized", "conversation_resolved"),
    ("application_authorized", "conversation_resolved"),
    ("conversation_resolved", "run_committed"),
    ("run_committed", "running_committed"),
    ("running_committed", "session_import_done"),
    ("running_committed", "prepared_context_ready"),
    ("prepared_context_ready", "runtime_submitted"),
    ("running_committed", "runtime_submitted"),
    ("session_import_done", "runtime_submitted"),
    ("runtime_submitted", "dashboard_connection_acquired"),
    ("dashboard_connection_acquired", "workspace_ready"),
    ("workspace_ready", "session_resumed"),
    ("session_resumed", "model_ready"),
    ("model_ready", "cwd_ready"),
    ("cwd_ready", "prompt_submit_begin"),
    ("prompt_submit_begin", "prompt_ack"),
    ("prompt_ack", "first_visible_event_received"),
    ("runtime_submitted", "first_visible_event_received"),
)
_APPLICATION_SUBAGENT_VISIBLE_EVENT_TYPES = frozenset({
    "subagent.start",
    "subagent.progress",
    "subagent.complete",
    "subagent.failed",
    "subagent.error",
    "subagent.interrupted",
    "subagent.canceled",
})
_APPLICATION_ARTIFACT_EVENT_TYPES = frozenset({
    "chat.artifact.created",
    "chat.artifact.updated",
    "chat.artifact.deleted",
})
_SAFE_TIMING_CORRELATION_ID = re.compile(
    r"[A-Za-z0-9._:-]{1,128}\Z"
)
timing_logger = logging.getLogger("uvicorn.error.runtime_timing")
file_logger = logging.getLogger("uvicorn.error.application_agent_files")


def _safe_timing_correlation_id(value: object) -> str | None:
    candidate = str(value or "")
    if not _SAFE_TIMING_CORRELATION_ID.fullmatch(candidate):
        return None
    return candidate


def _timing_mark(
    timing: RuntimeChatTiming | None,
    stage: str,
) -> bool:
    if timing is None:
        return False
    try:
        return timing.mark(stage)
    except Exception:
        return False


def _timing_bind_run(
    timing: RuntimeChatTiming | None,
    runtime_run_id: str,
) -> None:
    if timing is None:
        return
    try:
        timing.bind_run(runtime_run_id)
    except Exception:
        pass


def _timing_metadata(
    timing: RuntimeChatTiming | None,
    name: str,
    value: object,
) -> None:
    if timing is None:
        return
    candidate = _safe_timing_correlation_id(value)
    if candidate is None:
        return
    try:
        timing.set_metadata(name, candidate)
    except Exception:
        pass


def _log_invocation_timing(
    timing: RuntimeChatTiming | None,
    *,
    stage: str,
    status: str,
    channel: str,
    request_id: str,
    runtime_session_id: str,
    model_entry_id: str | None,
    turn_id: str | None = None,
    error_type: str | None = None,
) -> None:
    if timing is None:
        return
    try:
        snapshot = timing.safe_snapshot(
            duration_pairs=_APPLICATION_INVOCATION_TIMING_PAIRS,
        )
        duration_ms = None
        if stage == "first_visible_event_received":
            duration_ms = timing.duration_ms(
                "prompt_ack",
                "first_visible_event_received",
            )
            if duration_ms is None:
                duration_ms = timing.duration_ms(
                    "runtime_submitted",
                    "first_visible_event_received",
                )
        timing_logger.info(
            "application agent invocation timing=%s",
            {
                "component": "application_agent_invocation",
                "channel": _safe_timing_correlation_id(channel),
                "stage": stage,
                "status": status,
                "request_id": _safe_timing_correlation_id(request_id),
                "runtime_run_id": _safe_timing_correlation_id(
                    snapshot.get("runtime_run_id")
                ),
                "runtime_session_id": _safe_timing_correlation_id(
                    runtime_session_id
                ),
                "turn_id": _safe_timing_correlation_id(turn_id),
                "model_entry_id": _safe_timing_correlation_id(
                    model_entry_id
                ),
                "duration_ms": duration_ms,
                "durations_ms": snapshot.get("durations_ms", {}),
                **({"error_type": error_type} if error_type else {}),
            },
        )
    except Exception:
        pass


def _is_application_ttft_visible_output(
    event: PlatformStreamEvent,
) -> bool:
    if event.event in {
        "chat.reasoning.delta",
        "chat.activity.delta",
        "chat.message.delta",
    }:
        delta = event.data.get("delta")
        return isinstance(delta, str) and bool(delta.strip())
    if event.event == "chat.message.completed":
        text = event.data.get("text")
        return isinstance(text, str) and bool(text.strip())
    if event.event == "chat.tool.event":
        return any(
            isinstance(event.data.get(key), str)
            and bool(str(event.data[key]).strip())
            for key in ("delta", "preview", "result", "name", "tool_name")
        )
    if event.event == "chat.subagent.event":
        if (
            event.data.get("type")
            not in _APPLICATION_SUBAGENT_VISIBLE_EVENT_TYPES
        ):
            return False
        return any(
            isinstance(event.data.get(key), str)
            and bool(str(event.data[key]).strip())
            for key in ("subagent_id", "delegation_id")
        )
    return False


def _snapshot_has_artifact_event(snapshot: dict) -> bool:
    events = snapshot.get("events")
    if not isinstance(events, list):
        return False
    return any(
        isinstance(event, dict)
        and str(event.get("event_type") or "") in _APPLICATION_ARTIFACT_EVENT_TYPES
        for event in events
    )


class ApplicationAgentInvocationError(RuntimeError):
    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ApplicationAgentSubject:
    subject_type: str
    subject_key: str
    user_id: int | None = None
    service_id: str | None = None

    @property
    def public_key(self) -> str:
        if self.subject_type == "external_user" and self.service_id is not None:
            return f"external_user:{self.service_id}"
        return self.subject_key


@dataclass(frozen=True)
class ApplicationAgentInvocationResult:
    response_id: str
    request_id: str
    agent_slug: str
    conversation_key: str | None
    runtime_session_id: str
    subject: ApplicationAgentSubject
    output_text: str
    produced_files: list[ApplicationAgentProducedFile] | None = None


@dataclass(frozen=True)
class ApplicationChannelSessionPlan:
    org_id: int
    subject_user_id: int
    application_agent_id: int
    application_client_id: int
    conversation_id: int
    conversation_created: bool
    runtime_session_id: str
    runtime_root_host_path: str
    endpoint: RuntimeEndpoint
    config_generation: int
    model_entry_id: str | None
    prompt_contract_version: int
    prompt_contract_digest: str
    include_prompt_contract: bool


@dataclass(frozen=True)
class PreparedApplicationChannelInput:
    prompt: str
    attachments: tuple[RuntimeTurnAttachment, ...] = ()


@dataclass(frozen=True)
class ApplicationChannelFastLanePlan:
    target: RuntimeCommandTarget
    identity: RuntimeTurnIdentity
    application_agent_id: int
    application_client_id: int
    subject_user_id: int
    runtime_session_id: str
    config_generation: int


@dataclass(frozen=True)
class _ApplicationChannelContext:
    agent: ApplicationAgent
    binding: ApplicationAgentRuntimeBinding
    client: ApplicationClient
    conversation: ApplicationAgentConversation
    user: User
    subject: ApplicationAgentSubject
    plan: ApplicationChannelSessionPlan


class ApplicationAgentInvocationService(Protocol):
    async def invoke(
        self,
        *,
        slug: str,
        client_id: str,
        client_secret: str,
        delegated_principal: Principal | None,
        payload: ApplicationAgentResponseRequest,
        channel: str = "openapi",
    ) -> ApplicationAgentInvocationResult:
        ...


class ApplicationAgentPreparedKnowledgeService(Protocol):
    async def prepare_application_agent(
        self,
        *,
        application_agent_id: int,
        runtime_session_id: str,
        runtime_run_id: str,
        runtime_token: str,
    ) -> None:
        ...

class InMemoryApplicationAgentInvocationService:
    def __init__(self, management: InMemoryApplicationAgentService) -> None:
        self.management = management
        self.conversations: dict[tuple[int, int, str, str], str] = {}
        self.studio_messages: dict[str, list[dict[str, str]]] = {}
        self.studio_session_titles: dict[str, str] = {}

    def reset(self) -> None:
        self.conversations.clear()
        self.studio_messages.clear()
        self.studio_session_titles.clear()

    async def invoke(
        self,
        *,
        slug: str,
        client_id: str,
        client_secret: str,
        delegated_principal: Principal | None,
        payload: ApplicationAgentResponseRequest,
        channel: str = "openapi",
    ) -> ApplicationAgentInvocationResult:
        agent = next(
            (
                item
                for item in self.management.agents.values()
                if item.slug == slug and item.status == "published"
            ),
            None,
        )
        if agent is None:
            raise ApplicationAgentInvocationError(
                "application_agent_not_found",
                status_code=404,
            )
        client = next(
            (
                item
                for item in self.management.clients.get(
                    agent.application_agent_id,
                    [],
                )
                if item.client_id == client_id
                and item.status == "active"
                and item.credential_digest
                and hmac.compare_digest(
                    item.credential_digest,
                    _secret_digest(client_secret),
                )
            ),
            None,
        )
        if client is None or "responses:create" not in client.scopes:
            raise ApplicationAgentInvocationError(
                "application_client_unauthorized",
                status_code=401,
            )
        subject = _subject_for_grant(
            delegated_principal=delegated_principal,
            org_id=agent.org_id,
            client_id=client.client_id,
            allow_user_delegation=client.allow_user_delegation,
            allow_service_invocation=client.allow_service_invocation,
            external_user_id=payload.external_user_id,
        )
        conversation_key = _effective_conversation_key(
            subject=subject,
            requested=payload.conversation_key,
        )
        if conversation_key is None:
            runtime_session_id = _runtime_session_id(agent.application_agent_id)
        else:
            key = (
                agent.application_agent_id,
                client.application_client_id,
                subject.subject_key,
                conversation_key,
            )
            runtime_session_id = self.conversations.setdefault(
                key,
                _runtime_session_id(agent.application_agent_id),
            )
        request_id = payload.request_id or f"req_{secrets.token_urlsafe(16)}"
        return ApplicationAgentInvocationResult(
            response_id=f"resp_{secrets.token_urlsafe(16)}",
            request_id=request_id,
            agent_slug=agent.slug,
            conversation_key=conversation_key,
            runtime_session_id=runtime_session_id,
            subject=subject,
            output_text=f"已收到：{payload.input}",
        )

    async def invoke_studio(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str | None,
        payload: ApplicationAgentResponseRequest,
        event_sink: Callable[[PlatformStreamEvent], Awaitable[None]] | None = None,
        timing: RuntimeChatTiming | None = None,
    ) -> ApplicationAgentInvocationResult:
        agent = await self.management.get_agent(
            principal=principal,
            agent_id=agent_id,
        )
        _timing_mark(timing, "invocation_authorized")
        session_id = _studio_runtime_session_id(
            agent_id=agent.application_agent_id,
            user_id=principal.user_id,
            requested=runtime_session_id,
        )
        _timing_mark(timing, "conversation_resolved")
        request_id = payload.request_id or f"req_{secrets.token_urlsafe(16)}"
        output_text = f"已收到：{payload.input}"
        runtime_run_id = f"app_run_{secrets.token_urlsafe(18)}"
        _timing_bind_run(timing, runtime_run_id)
        _timing_metadata(timing, "runtime_session_id", session_id)
        if event_sink is not None:
            await event_sink(
                PlatformStreamEvent(
                    event="chat.run.accepted",
                    data={
                        "runtime_run_id": runtime_run_id,
                        "runtime_session_id": session_id,
                    },
                )
            )
            visible_event = PlatformStreamEvent(
                event="chat.message.delta",
                data={
                    "runtime_run_id": runtime_run_id,
                    "runtime_session_id": session_id,
                    "delta": output_text,
                    "source": "agent",
                },
            )
            _timing_mark(timing, "first_visible_event_received")
            await event_sink(
                visible_event
            )
            await event_sink(
                PlatformStreamEvent(
                    event="chat.done",
                    data={
                        "runtime_run_id": runtime_run_id,
                        "runtime_session_id": session_id,
                        "status": "completed",
                    },
                )
            )
        messages = self.studio_messages.setdefault(session_id, [])
        self.studio_session_titles.setdefault(session_id, "新任务")
        messages.extend(
            (
                {"role": "user", "content": payload.input},
                {"role": "assistant", "content": output_text},
            )
        )
        return ApplicationAgentInvocationResult(
            response_id=f"resp_{secrets.token_urlsafe(16)}",
            request_id=request_id,
            agent_slug=agent.slug,
            conversation_key=None,
            runtime_session_id=session_id,
            subject=ApplicationAgentSubject(
                subject_type="user",
                subject_key=f"user:{principal.user_id}",
                user_id=principal.user_id,
            ),
            output_text=output_text,
        )

    async def studio_history(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str,
    ) -> list[dict[str, str]]:
        await self.management.get_agent(
            principal=principal,
            agent_id=agent_id,
        )
        own_prefix = f"studio-{agent_id}-{principal.user_id}-"
        app_prefix = f"studio-{agent_id}-"
        if not runtime_session_id.startswith(app_prefix):
            raise ApplicationAgentInvocationError(
                "application_agent_studio_session_invalid",
                status_code=403,
            )
        if (
            not runtime_session_id.startswith(own_prefix)
            and runtime_session_id not in self.studio_messages
        ):
            raise ApplicationAgentInvocationError(
                "application_agent_studio_session_invalid",
                status_code=403,
            )
        session_id = runtime_session_id
        return _public_studio_history(
            self.studio_messages.get(session_id, [])
        )

    async def list_studio_sessions(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> list[dict]:
        await self.management.get_agent(principal=principal, agent_id=agent_id)
        prefix = f"studio-{agent_id}-"
        own_prefix = f"studio-{agent_id}-{principal.user_id}-"
        return [
            {
                "runtime_session_id": session_id,
                "title": self.studio_session_titles.get(
                    session_id,
                    "新任务",
                ),
                "message_count": len(messages),
                "is_read_only": not session_id.startswith(own_prefix),
                "origin_label": (
                    None
                    if session_id.startswith(own_prefix)
                    else _memory_studio_owner_label(session_id, agent_id)
                ),
            }
            for session_id, messages in reversed(
                list(self.studio_messages.items())
            )
            if session_id.startswith(prefix)
        ]

    async def create_studio_session(
        self,
        *,
        principal: Principal,
        agent_id: int,
        title: str | None,
    ) -> dict:
        await self.management.get_agent(principal=principal, agent_id=agent_id)
        session_id = _studio_runtime_session_id(
            agent_id=agent_id,
            user_id=principal.user_id,
            requested=None,
        )
        self.studio_messages[session_id] = []
        self.studio_session_titles[session_id] = title or "新任务"
        return {
            "runtime_session_id": session_id,
            "title": self.studio_session_titles[session_id],
            "message_count": 0,
        }

    async def rename_studio_session(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str,
        title: str,
    ) -> dict:
        session_id = _studio_runtime_session_id(
            agent_id=agent_id,
            user_id=principal.user_id,
            requested=runtime_session_id,
        )
        await self.management.get_agent(principal=principal, agent_id=agent_id)
        self.studio_session_titles[session_id] = title
        return {
            "runtime_session_id": session_id,
            "title": title,
            "message_count": len(
                self.studio_messages.get(session_id, [])
            ),
        }

    async def delete_studio_session(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str,
    ) -> None:
        session_id = _studio_runtime_session_id(
            agent_id=agent_id,
            user_id=principal.user_id,
            requested=runtime_session_id,
        )
        await self.management.get_agent(principal=principal, agent_id=agent_id)
        self.studio_messages.pop(session_id, None)
        self.studio_session_titles.pop(session_id, None)

    async def prewarm_studio_session(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str,
    ) -> None:
        _studio_runtime_session_id(
            agent_id=agent_id,
            user_id=principal.user_id,
            requested=runtime_session_id,
        )
        await self.management.get_agent(principal=principal, agent_id=agent_id)


class SqlAlchemyApplicationAgentInvocationService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        prepared_knowledge_service: ApplicationAgentPreparedKnowledgeService,
        codec: RuntimeApiKeyCodec | None = None,
    ) -> None:
        self.db = db
        self.codec = codec or RuntimeApiKeyCodec()
        self.prepared_knowledge_service = prepared_knowledge_service
        self.conversation_views = (
            SqlAlchemyApplicationAgentConversationViewService(db)
        )

    async def invoke(
        self,
        *,
        slug: str,
        client_id: str,
        client_secret: str,
        delegated_principal: Principal | None,
        payload: ApplicationAgentResponseRequest,
        channel: str = "openapi",
    ) -> ApplicationAgentInvocationResult:
        row = (
            await self.db.execute(
                select(
                    ApplicationAgent,
                    ApplicationClient,
                    ApplicationAgentClientGrant,
                    ApplicationAgentRuntimeBinding,
                )
                .join(
                    ApplicationAgentClientGrant,
                    ApplicationAgentClientGrant.application_agent_id
                    == ApplicationAgent.id,
                )
                .join(
                    ApplicationClient,
                    ApplicationClient.id
                    == ApplicationAgentClientGrant.application_client_id,
                )
                .join(
                    ApplicationAgentRuntimeBinding,
                    ApplicationAgentRuntimeBinding.application_agent_id
                    == ApplicationAgent.id,
                )
                .where(
                    ApplicationAgent.slug == slug,
                    ApplicationAgent.status == "published",
                    ApplicationClient.client_id == client_id,
                    ApplicationClient.status == "active",
                    ApplicationAgentClientGrant.status == "active",
                    ApplicationAgentRuntimeBinding.status == "ready",
                )
            )
        ).first()
        if row is None:
            raise ApplicationAgentInvocationError(
                "application_agent_or_client_not_found",
                status_code=404,
            )
        agent, client, grant, binding = row
        if not hmac.compare_digest(
            str(client.credential_digest_or_reference),
            _secret_digest(client_secret),
        ) or "responses:create" not in set(grant.scopes or []):
            raise ApplicationAgentInvocationError(
                "application_client_unauthorized",
                status_code=401,
            )
        subject = _subject_for_grant(
            delegated_principal=delegated_principal,
            org_id=int(agent.org_id),
            client_id=str(client.client_id),
            allow_user_delegation=bool(grant.allow_user_delegation),
            allow_service_invocation=bool(grant.allow_service_invocation),
            external_user_id=payload.external_user_id,
        )
        if subject.user_id is not None:
            exists = (
                await self.db.execute(
                    select(User.id).where(
                        User.id == subject.user_id,
                        User.org_id == agent.org_id,
                        User.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise ApplicationAgentInvocationError(
                    "delegated_user_not_found",
                    status_code=401,
                )

        conversation_key = _effective_conversation_key(
            subject=subject,
            requested=payload.conversation_key,
        )
        conversation, created = await self._conversation(
            agent=agent,
            client=client,
            subject=subject,
            conversation_key=conversation_key,
        )
        runtime_session_id = (
            str(conversation.runtime_session_id)
            if conversation is not None
            else _runtime_session_id(int(agent.id))
        )
        request_id = payload.request_id or f"req_{secrets.token_urlsafe(16)}"
        payload_digest = _application_request_digest(
            input_text=payload.input,
            conversation_key=conversation_key,
            subject=subject,
            model_entry_id=(agent.config or {}).get("model_entry_id"),
            config_revision=(
                f"generation-{max(int(agent.config_generation or 0), int(binding.config_generation or 0))}"
            ),
        )
        deterministic_run_id = "app_run_" + hashlib.sha256(
            (
                f"{int(agent.id)}\0{int(client.id)}\0{channel}\0"
                f"{request_id}"
            ).encode("utf-8")
        ).hexdigest()[:40]
        return await self._execute_invocation(
            agent=agent,
            binding=binding,
            client=client,
            conversation=conversation,
            subject=subject,
            runtime_session_id=runtime_session_id,
            request_id=request_id,
            input_text=payload.input,
            conversation_key=conversation_key,
            channel=channel,
            create_session=created or conversation is None,
            idempotency_key=request_id,
            deterministic_runtime_run_id=deterministic_run_id,
            request_payload_digest=payload_digest,
        )

    async def _prepare_channel_context(
        self,
        *,
        application_agent_id: int,
        application_client_id: int,
        delegated_principal: Principal,
        conversation_key: str,
        timing: RuntimeChatTiming | None = None,
    ) -> _ApplicationChannelContext:
        row = (
            await self.db.execute(
                select(
                    ApplicationAgent,
                    ApplicationClient,
                    ApplicationAgentClientGrant,
                    ApplicationAgentRuntimeBinding,
                )
                .join(
                    ApplicationAgentClientGrant,
                    ApplicationAgentClientGrant.application_agent_id
                    == ApplicationAgent.id,
                )
                .join(
                    ApplicationClient,
                    ApplicationClient.id
                    == ApplicationAgentClientGrant.application_client_id,
                )
                .join(
                    ApplicationAgentRuntimeBinding,
                    ApplicationAgentRuntimeBinding.application_agent_id
                    == ApplicationAgent.id,
                )
                .where(
                    ApplicationAgent.id == application_agent_id,
                    ApplicationAgent.org_id == delegated_principal.org_id,
                    ApplicationAgent.status == "published",
                    ApplicationClient.id == application_client_id,
                    ApplicationClient.org_id == delegated_principal.org_id,
                    ApplicationClient.status == "active",
                    ApplicationClient.auth_method == "internal_channel",
                    ApplicationAgentClientGrant.application_agent_id
                    == application_agent_id,
                    ApplicationAgentClientGrant.application_client_id
                    == application_client_id,
                    ApplicationAgentClientGrant.status == "active",
                    ApplicationAgentClientGrant.allow_user_delegation.is_(
                        True
                    ),
                    ApplicationAgentRuntimeBinding.status == "ready",
                )
            )
        ).first()
        if row is None:
            raise ApplicationAgentInvocationError(
                "application_channel_unauthorized",
                status_code=403,
            )
        agent, client, grant, binding = row
        if "responses:create" not in set(grant.scopes or []):
            raise ApplicationAgentInvocationError(
                "application_channel_scope_missing",
                status_code=403,
            )
        user = (
            await self.db.execute(
                select(User).where(
                    User.id == delegated_principal.user_id,
                    User.org_id == delegated_principal.org_id,
                    User.status == "active",
                )
            )
        ).scalar_one_or_none()
        if user is None:
            raise ApplicationAgentInvocationError(
                "delegated_user_not_found",
                status_code=401,
            )
        _timing_mark(timing, "application_authorized")
        subject = ApplicationAgentSubject(
            subject_type="user",
            subject_key=f"user:{delegated_principal.user_id}",
            user_id=delegated_principal.user_id,
        )
        conversation, created = await self._conversation(
            agent=agent,
            client=client,
            subject=subject,
            conversation_key=conversation_key,
        )
        if conversation is None:
            raise ApplicationAgentInvocationError(
                "application_channel_conversation_required",
                status_code=409,
            )
        endpoint = self._endpoint(agent=agent, binding=binding)
        try:
            applied_config = await resolve_application_execution_config(
                self.db,
                agent=agent,
                binding=binding,
            )
        except ApplicationAgentExecutionPolicyError as exc:
            raise ApplicationAgentInvocationError(
                exc.code,
                status_code=503,
            ) from exc
        model_entry_id = await self._application_model_entry_id(
            agent,
            config=applied_config.config,
        )
        plan = self._channel_session_plan(
            agent=agent,
            binding=binding,
            client=client,
            conversation=conversation,
            conversation_created=created,
            subject=subject,
            endpoint=endpoint,
            model_entry_id=model_entry_id,
            config_generation=applied_config.generation,
        )
        _timing_mark(timing, "conversation_resolved")
        return _ApplicationChannelContext(
            agent=agent,
            binding=binding,
            client=client,
            conversation=conversation,
            user=user,
            subject=subject,
            plan=plan,
        )

    async def prepare_channel_fast_lane(
        self,
        *,
        application_agent_id: int,
        application_client_id: int,
        delegated_principal: Principal,
        conversation_key: str,
    ) -> ApplicationChannelFastLanePlan:
        """Prepare current channel identity without creating a Backend Run."""

        context = await self._prepare_channel_context(
            application_agent_id=application_agent_id,
            application_client_id=application_client_id,
            delegated_principal=delegated_principal,
            conversation_key=conversation_key,
        )
        model_route = await self._resolve_model_route(
            model_entry_id=context.plan.model_entry_id,
            org_id=int(context.agent.org_id),
            binding=context.binding,
        )
        if model_route is None:
            raise ApplicationAgentInvocationError(
                "application_agent_model_unavailable",
                status_code=503,
            )
        profile_digest, skill_digest, mcp_digest, knowledge_digest = (
            application_asset_digests(dict(getattr(context.agent, "config", {}) or {}))
        )
        admission_generation = (
            await load_runtime_command_admission_generation(self.db)
        )
        target = RuntimeCommandTarget(
            runtime_id=str(context.binding.runtime_id),
            binding_id=str(context.binding.id),
            endpoint_host_header=str(
                context.binding.endpoint_host_header
            ),
            api_server_key=str(context.plan.endpoint.api_key),
            admission_generation=admission_generation,
        )
        identity = RuntimeTurnIdentity(
            user_id=int(context.subject.user_id or 0),
            org_id=int(context.agent.org_id),
            session_id=context.plan.runtime_session_id,
            profile_id=f"application-{int(context.agent.id)}",
            runtime_agent_profile_id=0,
            application_agent_id=int(context.agent.id),
            profile_desired_revision=int(
                getattr(context.agent, "config_generation", context.plan.config_generation)
                or 0
            ),
            profile_applied_revision=int(context.plan.config_generation),
            profile_digest=profile_digest,
            skill_digest=skill_digest,
            mcp_digest=mcp_digest,
            knowledge_digest=knowledge_digest,
            model_id=context.plan.model_entry_id or "default",
            upstream_model_id=model_route[0],
            context_length=model_route[1],
            runtime_capabilities=model_route[2],
            provider_route=model_route[3],
            config_revision=(
                f"generation-{context.plan.config_generation}"
            ),
            max_prompt_bytes=settings.RUNTIME_EDGE_MAX_PROMPT_BYTES,
            max_output_tokens=settings.RUNTIME_EDGE_MAX_OUTPUT_TOKENS,
            admission_generation=admission_generation,
        )
        await self.db.commit()
        return ApplicationChannelFastLanePlan(
            target=target,
            identity=identity,
            application_agent_id=int(context.agent.id),
            application_client_id=int(context.client.id),
            subject_user_id=int(context.subject.user_id or 0),
            runtime_session_id=context.plan.runtime_session_id,
            config_generation=context.plan.config_generation,
        )

    async def invoke_channel(
        self,
        *,
        application_agent_id: int,
        application_client_id: int,
        delegated_principal: Principal,
        payload: ApplicationAgentResponseRequest,
        channel: str,
        trusted_context: dict[str, object],
        event_sink: Callable[
            [PlatformStreamEvent], Awaitable[None]
        ]
        | None = None,
        run_created_sink: Callable[
            [ApplicationAgentRun], Awaitable[None]
        ]
        | None = None,
        before_execute_sink: Callable[
            [ApplicationChannelSessionPlan],
            Awaitable[str | PreparedApplicationChannelInput],
        ]
        | None = None,
        runtime_submitted_sink: Callable[[], None] | None = None,
        channel_lease_sink: Callable[
            [
                RuntimeCommandTarget,
                ChannelExecutionLease,
                str,
                RuntimeTurnIdentity,
            ],
            None,
        ]
        | None = None,
        idempotency_key: str | None = None,
        deterministic_runtime_run_id: str | None = None,
        timing: RuntimeChatTiming | None = None,
    ) -> ApplicationAgentInvocationResult:
        """Invoke through a database-authorized internal channel grant."""

        if payload.conversation_key is None:
            raise ApplicationAgentInvocationError(
                "application_channel_conversation_required",
                status_code=409,
            )
        context = await self._prepare_channel_context(
            application_agent_id=application_agent_id,
            application_client_id=application_client_id,
            delegated_principal=delegated_principal,
            conversation_key=payload.conversation_key,
            timing=timing,
        )
        agent = context.agent
        binding = context.binding
        client = context.client
        conversation = context.conversation
        subject = context.subject
        user = context.user
        plan = context.plan
        runtime_session_id = plan.runtime_session_id
        request_id = payload.request_id or f"req_{secrets.token_urlsafe(16)}"
        _timing_metadata(timing, "request_id", request_id)
        _timing_metadata(
            timing,
            "runtime_session_id",
            runtime_session_id,
        )
        input_text = payload.input
        channel_attachments: tuple[RuntimeTurnAttachment, ...] = ()
        create_session = plan.conversation_created
        if before_execute_sink is not None:
            prepared_input = await before_execute_sink(plan)
            if isinstance(prepared_input, PreparedApplicationChannelInput):
                input_text = prepared_input.prompt
                channel_attachments = prepared_input.attachments
            else:
                input_text = prepared_input
            create_session = False
        return await self._execute_invocation(
            agent=agent,
            binding=binding,
            client=client,
            conversation=conversation,
            subject=subject,
            runtime_session_id=runtime_session_id,
            request_id=request_id,
            input_text=input_text,
            attachments=channel_attachments,
            conversation_key=payload.conversation_key,
            channel=channel,
            create_session=create_session,
            event_sink=event_sink,
            run_created_sink=run_created_sink,
            runtime_submitted_sink=runtime_submitted_sink,
            channel_lease_sink=channel_lease_sink,
            timing=timing,
            trusted_context={
                **trusted_context,
                "org_id": int(user.org_id),
                "platform_user_id": int(user.id),
                "user_name": str(user.name),
                "user_code": (
                    str(user.user_code)
                    if user.user_code is not None
                    else None
                ),
            },
            platform_contract_version=plan.prompt_contract_version,
            platform_contract_digest=plan.prompt_contract_digest,
            include_platform_contract=plan.include_prompt_contract,
            idempotency_key=idempotency_key,
            deterministic_runtime_run_id=(
                deterministic_runtime_run_id
            ),
        )

    async def invoke_studio(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str | None,
        payload: ApplicationAgentResponseRequest,
        event_sink: Callable[[PlatformStreamEvent], Awaitable[None]] | None = None,
        timing: RuntimeChatTiming | None = None,
    ) -> ApplicationAgentInvocationResult:
        row = (
            await self.db.execute(
                select(
                    ApplicationAgent,
                    ApplicationAgentRuntimeBinding,
                )
                .join(
                    ApplicationAgentRuntimeBinding,
                    ApplicationAgentRuntimeBinding.application_agent_id
                    == ApplicationAgent.id,
                )
                .where(
                    ApplicationAgent.id == agent_id,
                    ApplicationAgent.org_id == principal.org_id,
                    ApplicationAgentRuntimeBinding.status == "ready",
                )
            )
        ).first()
        if row is None:
            raise ApplicationAgentInvocationError(
                "application_agent_unavailable",
                status_code=503,
            )
        agent, binding = row
        _timing_mark(timing, "invocation_authorized")
        session_id = _studio_runtime_session_id(
            agent_id=int(agent.id),
            user_id=principal.user_id,
            requested=runtime_session_id,
        )
        _timing_mark(timing, "conversation_resolved")
        request_id = payload.request_id or f"req_{secrets.token_urlsafe(16)}"
        model_entry_id = await self._application_model_entry_id(agent)
        _timing_metadata(timing, "request_id", request_id)
        _timing_metadata(timing, "runtime_session_id", session_id)
        _timing_metadata(timing, "model_entry_id", model_entry_id or "")
        return await self._execute_invocation(
            agent=agent,
            binding=binding,
            client=None,
            conversation=None,
            subject=ApplicationAgentSubject(
                subject_type="user",
                subject_key=f"user:{principal.user_id}",
                user_id=principal.user_id,
            ),
            runtime_session_id=session_id,
            request_id=request_id,
            input_text=payload.input,
            conversation_key=None,
            channel="studio",
            # Dashboard session import is idempotent and reports an existing
            # deterministic Studio session as skipped.
            create_session=True,
            event_sink=event_sink,
            timing=timing,
        )

    async def studio_history(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str,
    ) -> list[dict]:
        agent, binding = await self._studio_agent_binding(
            principal=principal,
            agent_id=agent_id,
        )
        session_id = await self._readable_debug_session_id(
            principal=principal,
            agent_id=int(agent.id),
            runtime_session_id=runtime_session_id,
        )
        runs = list(
            (
                await self.db.scalars(
                    select(ApplicationAgentRun)
                    .where(
                        ApplicationAgentRun.org_id == principal.org_id,
                        ApplicationAgentRun.application_agent_id == agent_id,
                        ApplicationAgentRun.runtime_session_id == session_id,
                    )
                    .order_by(
                        ApplicationAgentRun.created_at.asc(),
                        ApplicationAgentRun.id.asc(),
                    )
                )
            ).all()
        )
        run_ids = [str(run.runtime_run_id) for run in runs]
        receipts = (
            list(
                (
                    await self.db.scalars(
                        select(RuntimeProjectionReceipt)
                        .where(
                            RuntimeProjectionReceipt.runtime_id
                            == str(binding.runtime_id),
                            RuntimeProjectionReceipt.run_id.in_(run_ids),
                            RuntimeProjectionReceipt.projection_type.in_(
                                ("run.accepted", "run.terminal")
                            ),
                        )
                        .order_by(RuntimeProjectionReceipt.occurred_at.asc())
                    )
                ).all()
            )
            if run_ids
            else []
        )
        by_run: dict[str, dict[str, dict]] = {}
        for receipt in receipts:
            if receipt.run_id is None:
                continue
            by_run.setdefault(str(receipt.run_id), {})[
                str(receipt.projection_type)
            ] = dict(receipt.payload or {})
        missing_prompt_run_ids = [
            int(run.id)
            for run in runs
            if not str(
                by_run.get(
                    str(run.runtime_run_id),
                    {},
                ).get("run.accepted", {}).get("prompt")
                or ""
            )
        ]
        legacy_prompts: dict[int, str] = {}
        if missing_prompt_run_ids:
            legacy_rows = (
                await self.db.execute(
                    select(
                        ImInboundMessage.application_agent_run_id,
                        ImInboundMessage.normalized_content,
                    ).where(
                        ImInboundMessage.application_agent_run_id.in_(
                            missing_prompt_run_ids
                        )
                    )
                )
            ).all()
            for run_row_id, content in legacy_rows:
                if run_row_id is None:
                    continue
                prompt = str(
                    (content or {}).get("text")
                    if isinstance(content, dict)
                    else ""
                )
                if prompt:
                    legacy_prompts[int(run_row_id)] = prompt
        attachments_by_run: dict[int, list[dict[str, object]]] = {}
        if runs:
            attachment_rows = (
                await self.db.execute(
                    select(
                        ImMessageAttachment,
                        ImInboundMessage.application_agent_run_id,
                    )
                    .join(
                        ImInboundMessage,
                        ImInboundMessage.id
                        == ImMessageAttachment.inbound_message_id,
                    )
                    .where(
                        ImInboundMessage.application_agent_run_id.in_(
                            [int(run.id) for run in runs]
                        ),
                        ImMessageAttachment.staging_status == "ready",
                    )
                    .order_by(
                        ImMessageAttachment.inbound_message_id,
                        ImMessageAttachment.ordinal,
                    )
                )
            ).all()
            for attachment, run_row_id in attachment_rows:
                if run_row_id is None:
                    continue
                attachments_by_run.setdefault(int(run_row_id), []).append(
                    {
                        "attachmentId": int(attachment.id),
                        "displayName": str(
                            attachment.display_name
                            or attachment.runtime_relative_path
                        ),
                        "mimeType": attachment.mime_type,
                    }
                )
        messages: list[dict[str, object]] = []
        for run in runs:
            projected = by_run.get(str(run.runtime_run_id), {})
            prompt = str(
                projected.get("run.accepted", {}).get("prompt")
                or legacy_prompts.get(int(run.id))
                or ""
            )
            if prompt:
                messages.append(
                    {
                        "role": "user",
                        "content": prompt,
                        **(
                            {
                                "attachments": attachments_by_run.get(
                                    int(run.id),
                                    [],
                                )
                            }
                            if attachments_by_run.get(int(run.id))
                            else {}
                        ),
                    }
                )
            terminal = projected.get("run.terminal", {})
            answer = str(
                terminal.get("answer")
                or (run.response_snapshot or {}).get("output_text")
                or ""
            )
            if answer and str(run.status) == "completed":
                messages.append({"role": "assistant", "content": answer})
        return messages

    async def list_studio_sessions(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> list[dict]:
        agent, _binding = await self._studio_agent_binding(
            principal=principal,
            agent_id=agent_id,
        )
        studio_sessions = list((await self.db.scalars(
                select(ApplicationAgentStudioSession)
                .where(
                    ApplicationAgentStudioSession.org_id
                    == principal.org_id,
                    ApplicationAgentStudioSession.application_agent_id
                    == agent_id,
                    ApplicationAgentStudioSession.status == "active",
                )
                .order_by(
                    ApplicationAgentStudioSession.last_active_at.desc(),
                    ApplicationAgentStudioSession.id.desc(),
                )
                .limit(200)
            )).all())
        studio_runs = await self.db.scalars(
                select(ApplicationAgentRun).where(
                    ApplicationAgentRun.org_id == principal.org_id,
                    ApplicationAgentRun.application_agent_id == agent_id,
                    ApplicationAgentRun.channel == "studio",
                )
            )
        user_ids = {int(row.user_id) for row in studio_sessions}
        users = (
            list(
                (
                    await self.db.scalars(
                        select(User).where(
                            User.org_id == principal.org_id,
                            User.id.in_(user_ids),
                        )
                    )
                ).all()
            )
            if user_ids
            else []
        )
        user_labels = {
            int(user.id): (
                f"管理员：{user.name}（{user.user_code}）"
                if user.user_code
                else f"管理员：{user.name}"
            )
            for user in users
        }
        studio_sessions.sort(
            key=lambda row: (
                int(row.user_id) != principal.user_id,
                -(row.last_active_at.timestamp() if row.last_active_at else 0),
            )
        )
        read_only_sessions = await self.conversation_views.list_read_only_sessions(
            principal=principal,
            agent_id=agent_id,
        )
        counts: dict[str, int] = {}
        latest_runs: dict[str, ApplicationAgentRun] = {}
        for run in studio_runs.all():
            counts[str(run.runtime_session_id)] = (
                counts.get(str(run.runtime_session_id), 0) + 2
            )
            current = latest_runs.get(str(run.runtime_session_id))
            if current is None or (run.created_at, run.id) > (current.created_at, current.id):
                latest_runs[str(run.runtime_session_id)] = run
        result: list[dict] = [
            {
                "runtime_session_id": str(row.runtime_session_id),
                "title": (
                    str(row.title)
                    if int(row.user_id) == principal.user_id
                    else f"{user_labels.get(int(row.user_id), '其他管理员')} · {row.title}"
                ),
                "message_count": counts.get(
                    str(row.runtime_session_id),
                    0,
                ),
                "is_read_only": int(row.user_id) != principal.user_id,
                "origin_label": (
                    user_labels.get(int(row.user_id), "其他管理员")
                    if int(row.user_id) != principal.user_id
                    else None
                ),
                "last_active_at": row.last_active_at,
                "latest_run": _latest_run_record(latest_runs.get(str(row.runtime_session_id))),
            }
            for row in studio_sessions
        ]
        merged: list[dict] = []
        seen: set[str] = set()
        for item in sorted(
            result + read_only_sessions,
            key=lambda item: (item.get("last_active_at") or datetime.min.replace(tzinfo=timezone.utc), item["runtime_session_id"]),
            reverse=True,
        ):
            sid = str(item["runtime_session_id"])
            if sid in seen:
                continue
            seen.add(sid)
            merged.append(item)
            if len(merged) >= 50:
                break
        return merged

    async def create_studio_session(
        self,
        *,
        principal: Principal,
        agent_id: int,
        title: str | None,
    ) -> dict:
        await self._studio_agent_binding(
            principal=principal,
            agent_id=agent_id,
        )
        session_id = _studio_runtime_session_id(
            agent_id=agent_id,
            user_id=principal.user_id,
            requested=None,
        )
        visible_title = title or "新任务"
        now = datetime.now(timezone.utc)
        self.db.add(
            ApplicationAgentStudioSession(
                org_id=principal.org_id,
                application_agent_id=agent_id,
                user_id=principal.user_id,
                runtime_session_id=session_id,
                title=visible_title,
                status="active",
                last_active_at=now,
            )
        )
        await self.db.commit()
        return {
            "runtime_session_id": session_id,
            "title": visible_title,
            "message_count": 0,
        }

    async def rename_studio_session(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str,
        title: str,
    ) -> dict:
        agent, binding = await self._studio_agent_binding(
            principal=principal,
            agent_id=agent_id,
        )
        session_id = _studio_runtime_session_id(
            agent_id=agent_id,
            user_id=principal.user_id,
            requested=runtime_session_id,
        )
        row = await self._owned_studio_session(
            principal=principal,
            agent_id=agent_id,
            runtime_session_id=session_id,
        )
        message_count = int(
            await self.db.scalar(
                select(func.count(ApplicationAgentRun.id)).where(
                    ApplicationAgentRun.application_agent_id == agent_id,
                    ApplicationAgentRun.runtime_session_id == session_id,
                )
            )
            or 0
        ) * 2
        if message_count:
            await self._mutate_runtime_session(
                agent=agent,
                binding=binding,
                operation="rename",
                session_id=session_id,
                title=title,
            )
        row.title = title
        row.last_active_at = datetime.now(timezone.utc)
        await self.db.commit()
        return {
            "runtime_session_id": session_id,
            "title": title,
            "message_count": message_count,
        }

    async def delete_studio_session(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str,
    ) -> None:
        agent, binding = await self._studio_agent_binding(
            principal=principal,
            agent_id=agent_id,
        )
        session_id = _studio_runtime_session_id(
            agent_id=agent_id,
            user_id=principal.user_id,
            requested=runtime_session_id,
        )
        row = await self._owned_studio_session(
            principal=principal,
            agent_id=agent_id,
            runtime_session_id=session_id,
        )
        materialized = bool(
            await self.db.scalar(
                select(ApplicationAgentRun.id)
                .where(
                    ApplicationAgentRun.application_agent_id == agent_id,
                    ApplicationAgentRun.runtime_session_id == session_id,
                )
                .limit(1)
            )
        )
        if materialized:
            await self._mutate_runtime_session(
                agent=agent,
                binding=binding,
                operation="delete",
                session_id=session_id,
            )
        row.status = "deleted"
        row.last_active_at = datetime.now(timezone.utc)
        await self.db.commit()

    async def _mutate_runtime_session(
        self,
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
        operation: str,
        session_id: str,
        title: str | None = None,
    ) -> None:
        endpoint = self._endpoint(agent=agent, binding=binding)
        admission_generation = await load_runtime_command_admission_generation(
            self.db
        )
        target = RuntimeCommandTarget(
            runtime_id=str(binding.runtime_id),
            binding_id=str(binding.id),
            endpoint_host_header=str(binding.endpoint_host_header),
            api_server_key=str(endpoint.api_key),
            admission_generation=admission_generation,
        )
        client = self._runtime_command_client()
        try:
            await client.mutate_session(
                target=target,
                operation=operation,
                session_id=session_id,
                profile_id=f"application-{int(agent.id)}",
                title=title,
            )
        except RuntimeCommandError as exc:
            raise ApplicationAgentInvocationError(
                "application_agent_runtime_session_mutation_failed",
                status_code=502,
            ) from exc
        finally:
            await client.close()

    async def prewarm_studio_session(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str,
    ) -> None:
        await self._studio_agent_binding(
            principal=principal,
            agent_id=agent_id,
        )
        session_id = _studio_runtime_session_id(
            agent_id=agent_id,
            user_id=principal.user_id,
            requested=runtime_session_id,
        )
        await self._owned_studio_session(
            principal=principal,
            agent_id=agent_id,
            runtime_session_id=session_id,
        )

    async def _owned_studio_session(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str,
    ) -> ApplicationAgentStudioSession:
        row = await self.db.scalar(
            select(ApplicationAgentStudioSession).where(
                ApplicationAgentStudioSession.org_id == principal.org_id,
                ApplicationAgentStudioSession.application_agent_id
                == agent_id,
                ApplicationAgentStudioSession.user_id == principal.user_id,
                ApplicationAgentStudioSession.runtime_session_id
                == runtime_session_id,
                ApplicationAgentStudioSession.status == "active",
            )
        )
        if row is None:
            raise ApplicationAgentInvocationError(
                "application_agent_studio_session_invalid",
                status_code=404,
            )
        return row

    async def _studio_agent_binding(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> tuple[ApplicationAgent, ApplicationAgentRuntimeBinding]:
        row = (
            await self.db.execute(
                select(
                    ApplicationAgent,
                    ApplicationAgentRuntimeBinding,
                )
                .join(
                    ApplicationAgentRuntimeBinding,
                    ApplicationAgentRuntimeBinding.application_agent_id
                    == ApplicationAgent.id,
                )
                .where(
                    ApplicationAgent.id == agent_id,
                    ApplicationAgent.org_id == principal.org_id,
                    ApplicationAgentRuntimeBinding.status == "ready",
                )
            )
        ).first()
        if row is None:
            raise ApplicationAgentInvocationError(
                "application_agent_unavailable",
                status_code=503,
            )
        return row[0], row[1]

    async def _readable_debug_session_id(
        self,
        *,
        principal: Principal,
        agent_id: int,
        runtime_session_id: str,
    ) -> str:
        try:
            session_id = _studio_runtime_session_id(
                agent_id=agent_id,
                user_id=principal.user_id,
                requested=runtime_session_id,
            )
            await self._owned_studio_session(
                principal=principal,
                agent_id=agent_id,
                runtime_session_id=session_id,
            )
            return session_id
        except ApplicationAgentInvocationError:
            pass
        studio_session = await self.db.scalar(
            select(ApplicationAgentStudioSession.id).where(
                ApplicationAgentStudioSession.org_id == principal.org_id,
                ApplicationAgentStudioSession.application_agent_id == agent_id,
                ApplicationAgentStudioSession.runtime_session_id
                == runtime_session_id,
                ApplicationAgentStudioSession.status == "active",
            )
        )
        if studio_session is not None:
            return runtime_session_id
        if not await self.conversation_views.is_readable_session(
            principal=principal,
            agent_id=agent_id,
            runtime_session_id=runtime_session_id,
        ):
            raise ApplicationAgentInvocationError(
                "application_agent_studio_session_invalid",
                status_code=403,
            )
        return runtime_session_id

    async def _execute_invocation(
        self,
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
        client: ApplicationClient | None,
        conversation: ApplicationAgentConversation | None,
        subject: ApplicationAgentSubject,
        runtime_session_id: str,
        request_id: str,
        input_text: str,
        conversation_key: str | None,
        channel: str,
        create_session: bool,
        event_sink: Callable[[PlatformStreamEvent], Awaitable[None]] | None = None,
        run_created_sink: Callable[
            [ApplicationAgentRun], Awaitable[None]
        ]
        | None = None,
        runtime_submitted_sink: Callable[[], None] | None = None,
        channel_lease_sink: Callable[
            [
                RuntimeCommandTarget,
                ChannelExecutionLease,
                str,
                RuntimeTurnIdentity,
            ],
            None,
        ]
        | None = None,
        trusted_context: dict[str, object] | None = None,
        attachments: tuple[RuntimeTurnAttachment, ...] = (),
        timing: RuntimeChatTiming | None = None,
        platform_contract_version: int | None = None,
        platform_contract_digest: str | None = None,
        include_platform_contract: bool = True,
        idempotency_key: str | None = None,
        deterministic_runtime_run_id: str | None = None,
        request_payload_digest: str | None = None,
    ) -> ApplicationAgentInvocationResult:
        try:
            applied_config = await resolve_application_execution_config(
                self.db,
                agent=agent,
                binding=binding,
            )
        except ApplicationAgentExecutionPolicyError as exc:
            raise ApplicationAgentInvocationError(
                exc.code,
                status_code=503,
            ) from exc
        if request_payload_digest is not None:
            request_payload_digest = _application_request_digest(
                input_text=input_text,
                conversation_key=conversation_key,
                subject=subject,
                model_entry_id=applied_config.config.get("model_entry_id"),
                config_revision=f"generation-{applied_config.generation}",
            )
        agent_slug = str(agent.slug)
        conversation_row_id = (
            int(conversation.id)
            if conversation is not None
            else None
        )
        runtime_run_id = (
            str(deterministic_runtime_run_id)
            if deterministic_runtime_run_id
            else f"app_run_{secrets.token_urlsafe(18)}"
        )
        model_entry_id = await self._application_model_entry_id(
            agent,
            config=applied_config.config,
        )
        _timing_bind_run(timing, runtime_run_id)
        _timing_metadata(timing, "request_id", request_id)
        _timing_metadata(timing, "runtime_session_id", runtime_session_id)
        _timing_metadata(timing, "model_entry_id", model_entry_id or "")
        run = ApplicationAgentRun(
            org_id=agent.org_id,
            application_agent_id=agent.id,
            runtime_binding_id=binding.id,
            application_client_id=client.id if client is not None else None,
            conversation_id=conversation.id if conversation is not None else None,
            runtime_session_id=runtime_session_id,
            active_session_key=runtime_session_id,
            runtime_run_id=runtime_run_id,
            turn_id=None,
            subject_type=subject.subject_type,
            subject_user_id=subject.user_id,
            subject_service_id=subject.service_id,
            channel=channel,
            status="queued",
            request_id=request_id,
            idempotency_key=idempotency_key,
            payload_digest=request_payload_digest,
            submission_state="reserved",
            started_at=_now(),
        )
        self.db.add(run)
        resumed_idempotent_run = False
        try:
            await self.db.commit()
            _timing_mark(timing, "run_committed")
        except IntegrityError as exc:
            await self.db.rollback()
            # A transaction rollback expires every ORM object loaded by this
            # AsyncSession. Refresh the invocation context explicitly before
            # reading it again; implicit lazy refresh would perform I/O from
            # a synchronous attribute access and raise MissingGreenlet.
            for record in (agent, binding, client, conversation):
                if record is not None:
                    await self.db.refresh(record)
            existing = None
            if idempotency_key:
                existing = (
                    await self.db.execute(
                        select(ApplicationAgentRun).where(
                            ApplicationAgentRun.application_agent_id
                            == agent.id,
                            ApplicationAgentRun.channel == channel,
                            ApplicationAgentRun.idempotency_key
                            == idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
            if existing is not None:
                valid_existing = (
                    int(existing.runtime_binding_id)
                    == int(binding.id)
                    and (
                        int(existing.application_client_id)
                        if existing.application_client_id is not None
                        else None
                    )
                    == (
                        int(client.id)
                        if client is not None
                        else None
                    )
                    and (
                        int(existing.conversation_id)
                        if existing.conversation_id is not None
                        else None
                    )
                    == (
                        int(conversation.id)
                        if conversation is not None
                        else None
                    )
                    and str(existing.runtime_session_id)
                    == runtime_session_id
                    and str(existing.runtime_run_id)
                    == runtime_run_id
                    and str(existing.subject_type)
                    == subject.subject_type
                    and existing.subject_user_id == subject.user_id
                    and existing.subject_service_id
                    == subject.service_id
                    and (
                        request_payload_digest is None
                        or str(existing.payload_digest or "")
                        == request_payload_digest
                    )
                )
                resumable = (
                    str(existing.status) == "queued"
                    and str(existing.submission_state or "reserved")
                    == "reserved"
                    and existing.submit_intent_at is None
                )
                if not valid_existing:
                    raise ApplicationAgentInvocationError(
                        "application_agent_idempotency_conflict",
                        status_code=409,
                    ) from exc
                if not resumable:
                    return await self._resume_existing_invocation(
                        agent=agent,
                        binding=binding,
                        run=existing,
                        subject=subject,
                        conversation_key=conversation_key,
                        conversation=conversation,
                    )
                run = existing
                resumed_idempotent_run = True
                _timing_mark(timing, "run_committed")
            else:
                _log_invocation_timing(
                    timing,
                    stage="run_commit",
                    status="error",
                    channel=channel,
                    request_id=request_id,
                    runtime_session_id=runtime_session_id,
                    model_entry_id=model_entry_id,
                    error_type=type(exc).__name__,
                )
                raise ApplicationAgentInvocationError(
                    "conversation_busy",
                    status_code=409,
                ) from exc
        if resumed_idempotent_run:
            _timing_metadata(
                timing,
                "idempotent_run_resumed",
                "true",
            )
        run_row_id = int(run.id)
        run_turn_id = (
            str(run.turn_id)
            if run.turn_id is not None
            else None
        )
        terminal_status = "failed"
        terminal_error_code: str | None = (
            "application_agent_runtime_failed"
        )
        retryable_before_admission = False
        try:
            run.status = "running"
            await self.db.commit()
            _timing_mark(timing, "running_committed")
            if run_created_sink is not None:
                await run_created_sink(run)
            endpoint = self._endpoint(agent=agent, binding=binding)
            prepared_knowledge_task: asyncio.Task[None] | None = None
            if subject.subject_type == "user":
                prepared_knowledge_task = asyncio.create_task(
                    self.prepared_knowledge_service.prepare_application_agent(
                        application_agent_id=int(agent.id),
                        runtime_session_id=runtime_session_id,
                        runtime_run_id=runtime_run_id,
                        runtime_token=str(endpoint.api_key),
                    ),
                    name=(
                        "application-prepared-knowledge:"
                        f"{runtime_run_id}"
                    ),
                )
            if prepared_knowledge_task is not None:
                await prepared_knowledge_task
            _timing_mark(timing, "prepared_context_ready")
            run.submission_state = "submitting"
            run.submit_intent_at = run.submit_intent_at or _now()
            await self.db.commit()
            output: list[str] = []
            saw_done = False
            saw_artifact_event = False
            admission_generation = (
                await load_runtime_command_admission_generation(self.db)
            )
            command_target = RuntimeCommandTarget(
                runtime_id=str(binding.runtime_id),
                binding_id=str(binding.id),
                endpoint_host_header=str(binding.endpoint_host_header),
                api_server_key=str(endpoint.api_key),
                admission_generation=admission_generation,
            )
            model_route = await self._resolve_model_route(
                model_entry_id=model_entry_id,
                org_id=int(agent.org_id),
                binding=binding,
            )
            if model_route is None:
                raise ApplicationAgentInvocationError(
                    "application_agent_model_unavailable"
                )
            if (
                any(
                    attachment.mime_type
                    and attachment.mime_type.startswith("image/")
                    for attachment in attachments
                )
                and not supports_native_vision(model_route[2])
            ):
                raise ApplicationAgentInvocationError(
                    "MODEL_VISION_UNSUPPORTED",
                    status_code=409,
                )
            channel_lease = None
            if channel == "wecom":
                channel_context = trusted_context or {}
                channel_id = int(channel_context.get("im_channel_id") or 0)
                gateway_instance_id = str(
                    channel_context.get("im_gateway_instance_id") or ""
                )
                channel_lease_epoch = int(
                    channel_context.get("im_channel_lease_epoch") or 0
                )
                channel_generation = int(
                    channel_context.get("im_channel_generation") or 0
                )
                if (
                    channel_id <= 0
                    or len(gateway_instance_id) < 8
                    or channel_lease_epoch <= 0
                    or channel_generation <= 0
                ):
                    raise ApplicationAgentInvocationError(
                        "application_agent_channel_lease_unavailable",
                        status_code=503,
                    )
                channel_lease = issue_channel_execution_lease(
                    target=command_target,
                    org_id=int(agent.org_id),
                    channel_id=channel_id,
                    target_type="application_agent",
                    target_id=int(agent.id),
                    subject_user_id=int(subject.user_id or 0),
                    runtime_session_id=runtime_session_id,
                    model_entry_id=str(model_entry_id or "default"),
                    profile_id=f"application-{int(agent.id)}",
                    application_agent_id=int(agent.id),
                    gateway_instance_id=gateway_instance_id,
                    channel_lease_epoch=channel_lease_epoch,
                    channel_generation=channel_generation,
                    config_generation=applied_config.generation,
                )
            profile_digest, skill_digest, mcp_digest, knowledge_digest = (
                application_asset_digests(applied_config.config)
            )
            turn_identity = RuntimeTurnIdentity(
                user_id=int(subject.user_id or 0),
                org_id=int(agent.org_id),
                session_id=runtime_session_id,
                profile_id=f"application-{int(agent.id)}",
                runtime_agent_profile_id=0,
                application_agent_id=int(agent.id),
                profile_desired_revision=int(agent.config_generation or 0),
                profile_applied_revision=int(applied_config.generation),
                profile_digest=profile_digest,
                skill_digest=skill_digest,
                mcp_digest=mcp_digest,
                knowledge_digest=knowledge_digest,
                model_id=model_entry_id or "default",
                upstream_model_id=model_route[0],
                context_length=model_route[1],
                runtime_capabilities=model_route[2],
                provider_route=model_route[3],
                config_revision=f"generation-{applied_config.generation}",
                max_prompt_bytes=settings.RUNTIME_EDGE_MAX_PROMPT_BYTES,
                max_output_tokens=settings.RUNTIME_EDGE_MAX_OUTPUT_TOKENS,
                admission_generation=admission_generation,
            )
            command_payload = InternalTurnCommandPayload(
                source=_internal_turn_source(channel),
                run_id=runtime_run_id,
                client_message_id=deterministic_command_id(
                    "application-message",
                    request_id,
                ),
                identity=turn_identity,
                prompt=input_text,
                attachment_file_ids=[
                    attachment.file_id for attachment in attachments
                ],
                attachments=list(attachments),
                channel_execution_lease=channel_lease,
            )
            if channel_lease is not None and channel_lease_sink is not None:
                channel_lease_sink(
                    command_target,
                    channel_lease,
                    runtime_run_id,
                    turn_identity,
                )
            envelope = build_runtime_command(
                target=command_target,
                command_id=deterministic_command_id(
                    "application-turn",
                    binding.id,
                    runtime_run_id,
                ),
                payload=command_payload,
                deadline_seconds=600,
            )
            command_client = self._runtime_command_client()

            async def project_event(
                event_name: str,
                event_data: dict[str, object],
            ) -> None:
                nonlocal run_turn_id, saw_done, saw_artifact_event
                event = PlatformStreamEvent(
                    event=event_name,
                    data={
                        **event_data,
                        "runtime_run_id": runtime_run_id,
                        "runtime_session_id": runtime_session_id,
                    },
                )
                event_turn_id = event.data.get("turn_id")
                if isinstance(event_turn_id, str) and event_turn_id:
                    _timing_metadata(timing, "turn_id", event_turn_id)
                if event.event == "chat.run.started":
                    _timing_mark(timing, "prompt_ack")
                first_visible_received = (
                    _is_application_ttft_visible_output(event)
                    and _timing_mark(
                        timing,
                        "first_visible_event_received",
                    )
                )
                if first_visible_received:
                    _log_invocation_timing(
                        timing,
                        stage="first_visible_event_received",
                        status="success",
                        channel=channel,
                        request_id=request_id,
                        runtime_session_id=runtime_session_id,
                        model_entry_id=model_entry_id,
                        turn_id=(
                            event_turn_id
                            if isinstance(event_turn_id, str)
                            and event_turn_id
                            else run_turn_id
                        ),
                    )
                if event_sink is not None:
                    await event_sink(event)
                if event.event == "chat.done":
                    saw_done = True
                if event.event in _APPLICATION_ARTIFACT_EVENT_TYPES:
                    saw_artifact_event = True
                if event.event == "chat.message.completed":
                    text = event.data.get("text")
                    if isinstance(text, str):
                        output.clear()
                        output.append(text)
                elif event.event in {
                    "chat.activity.delta",
                    "chat.message.delta",
                }:
                    delta = event.data.get("delta")
                    if isinstance(delta, str):
                        output.append(delta)
                turn_id = event.data.get("turn_id")
                if (
                    run_turn_id is None
                    and isinstance(turn_id, str)
                    and turn_id
                ):
                    run_turn_id = turn_id

            try:
                ack = await command_client.send(
                    target=command_target,
                    envelope=envelope,
                )
                if ack.state != "accepted" or ack.run_id != runtime_run_id:
                    capacity_rejected = ack.error_code in {
                        "APPLICATION_CAPACITY_BUSY",
                        "PERSONAL_CAPACITY_BUSY",
                    }
                    runtime_draining = ack.error_code == "RUNTIME_DRAINING"
                    session_busy = ack.error_code in {
                        "SESSION_BUSY",
                        "SESSION_CAPACITY_BUSY",
                    }
                    if idempotency_key and (
                        capacity_rejected
                        or runtime_draining
                        or session_busy
                    ):
                        run.status = "queued"
                        run.submission_state = "reserved"
                        run.submit_intent_at = None
                        await self.db.commit()
                        retryable_before_admission = True
                    raise ApplicationAgentInvocationError(
                        (
                            "application_agent_capacity_busy"
                            if capacity_rejected
                            else "application_agent_session_busy"
                            if session_busy
                            else "application_agent_runtime_draining"
                            if runtime_draining
                            else ack.error_code
                            or "application_agent_runtime_failed"
                        ),
                        status_code=(
                            429
                            if capacity_rejected
                            else 503
                            if runtime_draining or session_busy
                            else 502
                        ),
                    )
                accepted_at = _now()
                run.submission_state = "accepted"
                run.dashboard_accepted_at = (
                    run.dashboard_accepted_at or accepted_at
                )
                if (
                    conversation is not None
                    and platform_contract_version is not None
                    and include_platform_contract
                    and platform_contract_digest
                ):
                    conversation.prompt_contract_version = (
                        platform_contract_version
                    )
                    conversation.prompt_contract_digest = (
                        platform_contract_digest
                    )
                    conversation.prompt_contract_confirmed_at = accepted_at
                await self.db.commit()
                _timing_mark(timing, "runtime_submitted")
                if runtime_submitted_sink is not None:
                    try:
                        runtime_submitted_sink()
                    except Exception:
                        pass
                if event_sink is not None:
                    await event_sink(
                        PlatformStreamEvent(
                            event="chat.run.accepted",
                            data={
                                "runtime_run_id": runtime_run_id,
                                "runtime_session_id": runtime_session_id,
                            },
                        )
                    )
                snapshot = await command_client.wait_run(
                    target=command_target,
                    run_id=runtime_run_id,
                    event_sink=project_event,
                    timeout_seconds=600,
                )
            except RuntimeCommandError as exc:
                if idempotency_key and run.dashboard_accepted_at is None:
                    # Delivery may have failed before the ACK reached us. The
                    # deterministic command/run identity makes resubmission
                    # safe, so preserve the reservation for the IM queue
                    # instead of terminalizing a message we promised to
                    # continue after Runtime recovery.
                    run.status = "queued"
                    run.submission_state = "reserved"
                    run.submit_intent_at = None
                    await self.db.commit()
                    retryable_before_admission = True
                raise ApplicationAgentInvocationError(
                    "application_agent_runtime_unavailable",
                    status_code=503,
                ) from exc
            finally:
                await command_client.close()
            snapshot_state = str(snapshot.get("status") or "").upper()
            terminal_status = (
                "completed"
                if snapshot_state == "COMPLETED"
                else "canceled"
                if snapshot_state == "CANCELED"
                else "failed"
            )
            terminal_error_code = (
                None
                if terminal_status == "completed"
                else "application_agent_runtime_failed"
            )
            answer = snapshot.get("answer")
            if isinstance(answer, str) and answer:
                output.clear()
                output.append(answer)
            if event_sink is not None and not saw_done:
                await event_sink(
                    PlatformStreamEvent(
                        event="chat.done",
                        data={
                            "runtime_run_id": runtime_run_id,
                            "runtime_session_id": runtime_session_id,
                            "status": terminal_status,
                        },
                    )
                )
            if terminal_status == "failed":
                raise ApplicationAgentInvocationError(
                    "application_agent_runtime_failed",
                    status_code=502,
                )
            produced_files = (
                await self._collect_openapi_produced_files(
                    agent=agent,
                    binding=binding,
                    conversation=conversation,
                    run=run,
                )
                if terminal_status == "completed"
                and channel == "openapi"
                and conversation is not None
                and saw_artifact_event
                else None
            )
            result = ApplicationAgentInvocationResult(
                response_id=f"resp_{secrets.token_urlsafe(16)}",
                request_id=request_id,
                agent_slug=agent_slug,
                conversation_key=conversation_key,
                runtime_session_id=runtime_session_id,
                subject=subject,
                output_text="".join(output),
                produced_files=produced_files,
            )
            run.response_snapshot = {
                "response_id": result.response_id,
                "request_id": result.request_id,
                "agent_slug": result.agent_slug,
                "conversation_key": result.conversation_key,
                "runtime_session_id": result.runtime_session_id,
                "output_text": result.output_text,
                "produced_files": (
                    [item.model_dump() for item in produced_files]
                    if produced_files is not None
                    else None
                ),
            }
            await self.db.commit()
            return result
        except Exception as exc:
            _log_invocation_timing(
                timing,
                stage="invocation",
                status="error",
                channel=channel,
                request_id=request_id,
                runtime_session_id=runtime_session_id,
                model_entry_id=model_entry_id,
                turn_id=run_turn_id,
                error_type=type(exc).__name__,
            )
            raise
        finally:
            if not retryable_before_admission:
                await self._finalize_invocation_run(
                    run_id=run_row_id,
                    conversation_id=conversation_row_id,
                    terminal_status=terminal_status,
                    error_code=terminal_error_code,
                    turn_id=run_turn_id,
                )

    async def _collect_openapi_produced_files(
        self,
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
        conversation: ApplicationAgentConversation | None,
        run: ApplicationAgentRun,
    ) -> list[ApplicationAgentProducedFile] | None:
        if conversation is None or str(run.channel) != "openapi":
            return None
        try:
            artifacts = await HermesDashboardProtocolAdapter().list_session_artifacts(
                self._endpoint(agent=agent, binding=binding),
                runtime_session_id=str(conversation.runtime_session_id),
            )
            records = await ApplicationAgentConversationFileService(
                self.db
            ).sync_runtime_artifacts(
                agent=agent,
                binding=binding,
                conversation=conversation,
                run=run,
                artifacts=artifacts,
            )
            return [
                ApplicationAgentProducedFile(
                    file_id=record.file_id,
                    display_name=record.display_name,
                    mime_type=record.mime_type,
                    size_bytes=record.size_bytes,
                    download_url=application_agent_file_download_url(
                        slug=str(agent.slug),
                        file_id=record.file_id,
                    ),
                )
                for record in records
            ]
        except Exception:
            await self.db.rollback()
            file_logger.exception(
                "OpenAPI produced file synchronization failed"
            )
            return None

    async def _resume_existing_invocation(
        self,
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
        run: ApplicationAgentRun,
        subject: ApplicationAgentSubject,
        conversation_key: str | None,
        conversation: ApplicationAgentConversation | None,
    ) -> ApplicationAgentInvocationResult:
        stored_response = dict(run.response_snapshot or {})
        if str(run.status) == "completed" and stored_response:
            return ApplicationAgentInvocationResult(
                response_id=str(stored_response["response_id"]),
                request_id=str(run.request_id),
                agent_slug=str(agent.slug),
                conversation_key=conversation_key,
                runtime_session_id=str(run.runtime_session_id),
                subject=subject,
                output_text=str(stored_response.get("output_text") or ""),
                produced_files=[
                    ApplicationAgentProducedFile.model_validate(item)
                    for item in stored_response.get("produced_files", [])
                ]
                or None,
            )
        if str(run.status) in {"failed", "canceled"}:
            raise ApplicationAgentInvocationError(
                str(run.error_code or "application_agent_runtime_failed"),
                status_code=502,
            )
        endpoint = self._endpoint(agent=agent, binding=binding)
        admission_generation = await load_runtime_command_admission_generation(
            self.db
        )
        target = RuntimeCommandTarget(
            runtime_id=str(binding.runtime_id),
            binding_id=str(binding.id),
            endpoint_host_header=str(binding.endpoint_host_header),
            api_server_key=str(endpoint.api_key),
            admission_generation=admission_generation,
        )
        client = self._runtime_command_client()
        try:
            snapshot = await client.wait_run(
                target=target,
                run_id=str(run.runtime_run_id),
                timeout_seconds=600,
            )
        except RuntimeCommandError as exc:
            raise ApplicationAgentInvocationError(
                "application_agent_run_reconciliation_required",
                status_code=503,
            ) from exc
        finally:
            await client.close()
        if str(snapshot.get("status") or "").upper() != "COMPLETED":
                raise ApplicationAgentInvocationError(
                    "application_agent_runtime_failed",
                    status_code=502,
                )
        produced_files = (
            await self._collect_openapi_produced_files(
                agent=agent,
                binding=binding,
                conversation=conversation,
                run=run,
            )
            if conversation is not None
            and _snapshot_has_artifact_event(snapshot)
            else None
        )
        result = ApplicationAgentInvocationResult(
            response_id=str(
                stored_response.get("response_id")
                or f"resp_{secrets.token_urlsafe(16)}"
            ),
            request_id=str(run.request_id),
            agent_slug=str(agent.slug),
            conversation_key=conversation_key,
            runtime_session_id=str(run.runtime_session_id),
            subject=subject,
            output_text=str(snapshot.get("answer") or ""),
            produced_files=produced_files,
        )
        run.response_snapshot = {
            "response_id": result.response_id,
            "request_id": result.request_id,
            "agent_slug": result.agent_slug,
            "conversation_key": result.conversation_key,
            "runtime_session_id": result.runtime_session_id,
            "output_text": result.output_text,
            "produced_files": (
                [item.model_dump() for item in produced_files]
                if produced_files is not None
                else None
            ),
        }
        await self.db.commit()
        return result

    def _runtime_command_client(
        self,
    ) -> RuntimeCommandClient | RuntimeChannelFastLaneClient:
        return RuntimeChannelFastLaneClient()

    async def _application_model_entry_id(
        self,
        agent: ApplicationAgent,
        *,
        config: dict[str, object] | None = None,
    ) -> str:
        try:
            policy = await ApplicationAgentExecutionPolicyResolver(
                self.db
            ).resolve_model(
                org_id=int(agent.org_id),
                owner_user_id=int(agent.created_by_user_id),
                config=dict(agent.config or {} if config is None else config),
            )
        except ApplicationAgentExecutionPolicyError as exc:
            raise ApplicationAgentInvocationError(
                exc.code,
                status_code=503,
            ) from exc
        return str(policy.model_entry_id)

    async def _resolve_model_route(
        self,
        *,
        model_entry_id: str | None,
        org_id: int,
        binding: ApplicationAgentRuntimeBinding,
    ) -> tuple[
        str,
        int,
        dict[str, str],
        ProviderRouteGrantV1 | None,
    ] | None:
        if model_entry_id is None:
            return None
        if settings.provider_route_issuance_enabled(str(binding.runtime_id)):
            try:
                route = await RuntimeProviderRouteResolver(self.db).resolve(
                    org_id=org_id,
                    model_entry_id=int(model_entry_id),
                    binding=ProviderRouteBindingIdentity(
                        runtime_id=str(binding.runtime_id),
                        binding_type="application_runtime",
                        binding_id=int(binding.id),
                        pvc_storage_identity_hash=str(
                            binding.pvc_storage_identity_hash or ""
                        ),
                    ),
                )
            except (RuntimeProviderRouteError, ValueError):
                return None
            return (
                route.upstream_model_id,
                route.context_length,
                route.runtime_capabilities,
                route.provider_route,
            )
        scalar = getattr(self.db, "scalar", None)
        if scalar is None:
            # Lightweight unit-test session doubles predate AsyncSession.scalar.
            return model_entry_id, 128_000, normalize_runtime_capabilities(None), None
        result = await self.db.execute(
            select(
                ModelEntry.model_key,
                ModelEntry.context_length,
                ModelEntry.runtime_capabilities,
            ).where(
                ModelEntry.id == int(model_entry_id),
                ModelEntry.org_id == org_id,
                ModelEntry.status == "active",
            )
        )
        row = result.one_or_none()
        if row is None or not row[0]:
            return None
        context_length = int(row[1]) if int(row[1] or 0) > 0 else 128_000
        return (
            str(row[0]),
            context_length,
            normalize_runtime_capabilities(row[2]),
            None,
        )

    def _endpoint(
        self,
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
    ) -> RuntimeEndpoint:
        return RuntimeEndpoint(
            runtime_workspace_id=int(agent.id),
            runtime_id=str(binding.runtime_id),
            endpoint_base_url=str(binding.endpoint_base_url),
            endpoint_host_header=str(binding.endpoint_host_header),
            api_key=self.codec.decrypt(str(binding.api_key_encrypted)),
            timeout_seconds=60.0,
            coordination_scope_key=(
                f"application:{binding.runtime_id}:{int(agent.id)}"
            ),
        )

    @staticmethod
    def _channel_session_plan(
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
        client: ApplicationClient,
        conversation: ApplicationAgentConversation,
        conversation_created: bool,
        subject: ApplicationAgentSubject,
        endpoint: RuntimeEndpoint,
        model_entry_id: str | None,
        config_generation: int,
    ) -> ApplicationChannelSessionPlan:
        if subject.user_id is None:
            raise ApplicationAgentInvocationError(
                "application_channel_user_required",
                status_code=401,
            )
        if model_entry_id is None:
            # A published Application Agent may intentionally inherit the
            # Runtime's default model.  Keep that policy as a stable identity
            # instead of rejecting an already-valid Agent configuration.
            effective_model_entry_id = 0
        else:
            try:
                effective_model_entry_id = int(str(model_entry_id))
            except ValueError as exc:
                raise ApplicationAgentInvocationError(
                    "application_agent_model_invalid",
                    status_code=503,
                ) from exc
            if effective_model_entry_id <= 0:
                raise ApplicationAgentInvocationError(
                    "application_agent_model_unavailable",
                    status_code=503,
                )

        def digest(*values: str) -> str:
            return hashlib.sha256(
                "\n".join(values).encode("utf-8")
            ).hexdigest()

        model_policy_digest = digest(
            json.dumps(
                {
                    "model_entry_id": effective_model_entry_id,
                    "agent_config_generation": config_generation,
                    "binding_config_generation": config_generation,
                    "provider_policy": "application:platform",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        prompt_contract_version = 2
        prompt_workspace = runtime_session_workspace_path(
            HERMES_WORKSPACE,
            str(conversation.runtime_session_id),
        )
        base_prompt_contract_digest = platform_session_contract_digest(
            prompt_workspace
        )
        prompt_contract_digest = digest(
            "platform-session-contract-v2",
            base_prompt_contract_digest,
            str(config_generation),
            model_policy_digest,
        )
        include_prompt_contract = (
            int(
                getattr(
                    conversation,
                    "prompt_contract_version",
                    0,
                )
                or 0
            )
            != prompt_contract_version
            or str(
                getattr(
                    conversation,
                    "prompt_contract_digest",
                    "",
                )
                or ""
            )
            != prompt_contract_digest
        )
        return ApplicationChannelSessionPlan(
            org_id=int(agent.org_id),
            subject_user_id=int(subject.user_id),
            application_agent_id=int(agent.id),
            application_client_id=int(client.id),
            conversation_id=int(conversation.id),
            conversation_created=bool(conversation_created),
            runtime_session_id=str(conversation.runtime_session_id),
            runtime_root_host_path=str(
                binding.runtime_root_host_path or ""
            ),
            endpoint=endpoint,
            config_generation=config_generation,
            model_entry_id=model_entry_id,
            prompt_contract_version=prompt_contract_version,
            prompt_contract_digest=prompt_contract_digest,
            include_prompt_contract=include_prompt_contract,
        )

    async def _conversation(
        self,
        *,
        agent: ApplicationAgent,
        client: ApplicationClient,
        subject: ApplicationAgentSubject,
        conversation_key: str | None,
    ) -> tuple[ApplicationAgentConversation | None, bool]:
        if conversation_key is None:
            return None, True
        conversation = (
            await self.db.execute(
                select(ApplicationAgentConversation).where(
                    ApplicationAgentConversation.org_id == agent.org_id,
                    ApplicationAgentConversation.application_agent_id == agent.id,
                    ApplicationAgentConversation.application_client_id == client.id,
                    ApplicationAgentConversation.subject_key == subject.subject_key,
                    ApplicationAgentConversation.conversation_key
                    == conversation_key,
                )
            )
        ).scalar_one_or_none()
        if conversation is not None:
            return conversation, False
        conversation = ApplicationAgentConversation(
            org_id=agent.org_id,
            application_agent_id=agent.id,
            application_client_id=client.id,
            subject_type=subject.subject_type,
            subject_user_id=subject.user_id,
            subject_service_id=subject.service_id,
            subject_key=subject.subject_key,
            conversation_key=conversation_key,
            runtime_session_id=_runtime_session_id(int(agent.id)),
            status="active",
            last_active_at=_now(),
        )
        self.db.add(conversation)
        await self.db.flush()
        return conversation, True

    async def _finalize_invocation_run(
        self,
        *,
        run_id: int,
        conversation_id: int | None,
        terminal_status: str,
        error_code: str | None,
        turn_id: str | None,
    ) -> None:
        """Commit the Backend business projection of a terminal Runtime Run."""

        for attempt in range(3):
            try:
                with self.db.no_autoflush:
                    run = (
                        await self.db.execute(
                            select(ApplicationAgentRun)
                            .where(ApplicationAgentRun.id == run_id)
                            .with_for_update()
                        )
                    ).scalar_one()
                    conversation = None
                    if conversation_id is not None:
                        conversation = (
                            await self.db.execute(
                                select(ApplicationAgentConversation)
                                .where(
                                    ApplicationAgentConversation.id
                                    == conversation_id
                                )
                                .with_for_update()
                            )
                        ).scalar_one_or_none()
                now = _now()
                run.status = terminal_status
                run.submission_state = "terminal"
                run.active_session_key = None
                if run.turn_id is None and turn_id is not None:
                    run.turn_id = turn_id
                run.finished_at = run.finished_at or now
                run.error_code = error_code
                if (
                    terminal_status == "completed"
                    and conversation is not None
                ):
                    conversation.last_active_at = now
                await self.db.commit()
                return
            except OperationalError:
                await self.db.rollback()
                if attempt >= 2:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))

def _public_studio_history(messages: object) -> list[dict]:
    if not isinstance(messages, list):
        return []
    public: list[dict] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            continue
        normalized = normalize_internal_history_message(item, index)
        if normalized is not None:
            public.append(normalized)
    return public


def _internal_turn_source(
    channel: str,
) -> Literal["application_wecom", "openapi"]:
    return "application_wecom" if channel == "wecom" else "openapi"


def _application_request_digest(
    *,
    input_text: str,
    conversation_key: str | None,
    subject: ApplicationAgentSubject,
    model_entry_id: object,
    config_revision: str,
) -> str:
    payload = {
        "config_revision": config_revision,
        "conversation_key": conversation_key,
        "input": input_text,
        "model_entry_id": model_entry_id,
        "subject_key": subject.subject_key,
        "subject_type": subject.subject_type,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _subject_for_grant(
    *,
    delegated_principal: Principal | None,
    org_id: int,
    client_id: str,
    allow_user_delegation: bool,
    allow_service_invocation: bool,
    external_user_id: str | None = None,
) -> ApplicationAgentSubject:
    if delegated_principal is not None and external_user_id is not None:
        raise ApplicationAgentInvocationError(
            "application_agent_identity_conflict",
            status_code=409,
        )
    if delegated_principal is not None:
        if delegated_principal.org_id != org_id or not allow_user_delegation:
            raise ApplicationAgentInvocationError(
                "delegated_user_unauthorized",
                status_code=403,
            )
        return ApplicationAgentSubject(
            subject_type="user",
            subject_key=f"user:{delegated_principal.user_id}",
            user_id=delegated_principal.user_id,
        )
    if not allow_service_invocation:
        raise ApplicationAgentInvocationError(
            "delegated_user_token_required",
            status_code=401,
        )
    if external_user_id is not None:
        subject_digest = hashlib.sha256(
            f"{client_id}\0{external_user_id}".encode("utf-8")
        ).hexdigest()
        return ApplicationAgentSubject(
            subject_type="external_user",
            subject_key=f"external_user:{subject_digest}",
            service_id=external_user_id,
        )
    return ApplicationAgentSubject(
        subject_type="service",
        subject_key=f"service:{client_id}",
        service_id=client_id,
    )


def _effective_conversation_key(
    *,
    subject: ApplicationAgentSubject,
    requested: str | None,
) -> str | None:
    if requested is not None:
        return requested
    if subject.subject_type == "external_user":
        return "default"
    return None


def _runtime_session_id(agent_id: int) -> str:
    return f"app-{agent_id}-{secrets.token_urlsafe(18)}"


def _studio_runtime_session_id(
    *,
    agent_id: int,
    user_id: int,
    requested: str | None,
) -> str:
    prefix = f"studio-{agent_id}-{user_id}-"
    if requested is None:
        return f"{prefix}{secrets.token_urlsafe(12)}"
    if (
        len(requested) > 128
        or not requested.startswith(prefix)
        or re.fullmatch(r"[A-Za-z0-9_-]+", requested) is None
    ):
        raise ApplicationAgentInvocationError(
            "application_agent_studio_session_invalid",
            status_code=403,
        )
    return requested


def _memory_studio_owner_label(runtime_session_id: str, agent_id: int) -> str:
    match = re.match(
        rf"^studio-{agent_id}-(\d+)-",
        runtime_session_id,
    )
    return f"管理员：用户 {match.group(1)}" if match else "其他管理员"


def _visible_studio_title(value: object, runtime_session_id: str) -> str:
    title = str(value or "新任务").strip() or "新任务"
    suffix = f" [{runtime_session_id}]"
    return title[:-len(suffix)] if title.endswith(suffix) else title




def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
