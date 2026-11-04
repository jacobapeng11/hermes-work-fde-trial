from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from sqlalchemy import JSON as SqlJSON
from sqlalchemy import func, insert, literal, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from redis.exceptions import RedisError

from app.config import settings
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models import (
    CloudHermesProfile,
    HermesRuntimeBinding,
    InstalledExpertBinding,
    ProfileKnowledgeBinding,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    RuntimeAuditLog,
    RuntimeRun,
    RuntimeRunEvent,
    RuntimeSession,
    ScheduledTask,
)
from app.services.asset_visibility import (
    AssetVisibilityService,
    SqlAlchemyAssetVisibilityStore,
)
from app.services.chat_log import validate_channel
from app.services.runtime_admission import (
    RuntimeAdmissionDecision,
    RuntimeAdmissionError,
    RuntimeAdmissionService,
)
from app.services.runtime_reload import ReloadDecisionInput, RuntimeReloadDecisionService, RuntimeStatus
from app.services.runtime_agent_profiles import executable_runtime_agent_profile_clauses
from app.services.runtime_chat_timing import RuntimeChatTiming
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec
from app.services.model_catalog_cache import ModelCacheInvalidationEvent, RedisModelRouteCache
from app.services.runtime_models import (
    RuntimeModelRecord,
    RuntimeTurnModelNotVisibleError,
    RuntimeTurnModelUnavailableError,
    SqlAlchemyRuntimeModelStore,
    resolve_default_model_entry_id,
    resolve_runtime_turn_model_entry_id,
)


logger = logging.getLogger(__name__)


def _mark_runtime_timing(
    timing: RuntimeChatTiming | None,
    stage: str,
) -> None:
    if timing is None:
        return
    try:
        timing.mark(stage)
    except Exception:
        # Observability must never replace a successful business operation.
        pass


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize MySQL's timezone-naive DATETIME values before comparisons."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RuntimeChatRequestIntent:
    runtime_workspace_id: int
    runtime_session_id: str
    message: str
    model_entry_id: int | None = None
    enabled_expert_ids: list[int] | None = None
    enabled_skill_ids: list[int] | None = None
    enabled_mcp_ids: list[int] | None = None
    enabled_knowledge_base_ids: list[int] | None = None
    source_type: str = "web"
    audit_message: str | None = None
    channel: str = "web"
    # Internal callers may reserve a deterministic platform Run ID. The
    # database's existing unique constraint then provides crash-safe
    # idempotency without exposing this control through user HTTP payloads.
    runtime_run_id: str | None = None
    attachment_file_ids: tuple[int, ...] = ()
    source_snapshot: dict | None = None


@dataclass(frozen=True)
class RuntimeChatPolicy:
    runtime_workspace_id: int
    session_id: int | None
    org_id: int
    user_id: int
    runtime_session_id: str
    session_model_entry_id: int | None
    default_model_entry_id: int | None
    visible_model_entry_ids: set[int]
    visible_expert_ids: set[int]
    visible_skill_ids: set[int]
    visible_mcp_ids: set[int]
    visible_knowledge_base_ids: set[int]
    runtime_ready: bool
    reload_decision_input: ReloadDecisionInput
    runtime_agent_profile_id: int | None = None
    hermes_profile_name: str = "default"
    visible_models: tuple[RuntimeModelRecord, ...] = ()


@dataclass(frozen=True)
class PreparedRuntimeRun:
    runtime_workspace_id: int
    runtime_session_id: str
    model_entry_id: int | None
    message: str
    source_type: str
    enabled_expert_ids: list[int]
    enabled_skill_ids: list[int]
    enabled_mcp_ids: list[int]
    enabled_knowledge_base_ids: list[int]
    allow_new_run: bool
    audit_message: str | None = None
    channel: str = "web"
    runtime_agent_profile_id: int | None = None
    hermes_profile_name: str = "default"
    runtime_run_id: str | None = None
    history_message_count_before_run: int | None = None
    client_request_digest: str | None = None
    attachment_file_ids: tuple[int, ...] = ()
    source_snapshot: dict | None = None


@dataclass(frozen=True)
class CreatedRuntimeRun:
    runtime_workspace_id: int
    runtime_session_id: str
    runtime_run_id: str
    model_entry_id: int | None
    status: str
    title: str | None = None
    session_materialized: bool = False
    runtime_agent_profile_id: int | None = None
    hermes_profile_name: str = "default"
    reused: bool = False


@dataclass(frozen=True)
class RuntimeRunControl:
    runtime_workspace_id: int
    runtime_session_id: str | None
    runtime_run_id: str
    runtime_provider_run_id: str | None
    model_entry_id: int | None
    status: str
    endpoint: RuntimeEndpoint


@dataclass(frozen=True)
class RuntimeRunReplayControl:
    runtime_workspace_id: int
    runtime_session_id: str | None
    runtime_run_id: str
    status: str


@dataclass(frozen=True)
class RuntimeActiveRun:
    runtime_workspace_id: int
    runtime_session_id: str
    runtime_run_id: str
    status: str
    last_seq: int
    history_message_count_before_run: int | None = None
    source: dict | None = None


@dataclass(frozen=True)
class RuntimeSessionHistoryControl:
    runtime_workspace_id: int
    runtime_session_id: str
    session_id: int
    endpoint: RuntimeEndpoint
    message_count: int = 0
    config_generation: int = 0
    effective_model_entry_id: int = 0
    model_policy_digest: str = ""
    runtime_agent_profile_id: int | None = None
    runtime_agent_profile_generation: int = 0
    hermes_profile_name: str = "default"

class RuntimeChatPreparationError(PermissionError):
    pass


class RuntimeExpertSessionDeletedError(RuntimeChatPreparationError):
    code = "expert_session_deleted"


class RuntimeChatActiveRunError(RuntimeChatPreparationError):
    pass


class RuntimeChatAdmissionError(RuntimeChatPreparationError):
    def __init__(self, decision: RuntimeAdmissionDecision) -> None:
        super().__init__(decision.code)
        self.decision = decision


class RuntimeChatUnsavedAssetChangesError(RuntimeChatPreparationError):
    pass


class RuntimeChatIdempotencyConflictError(RuntimeChatPreparationError):
    pass


class RuntimeChatDatabaseBusyError(RuntimeChatPreparationError):
    code = "RUNTIME_DATABASE_BUSY"

    def __init__(self, *, mysql_error_code: int) -> None:
        self.mysql_error_code = int(mysql_error_code)
        super().__init__("runtime database is busy")


def runtime_chat_client_request_digest(
    *,
    org_id: int,
    user_id: int,
    intent: RuntimeChatRequestIntent,
    model_entry_id: int | None,
    runtime_agent_profile_id: int | None,
) -> str:
    """Digest stable client inputs without persisting another message copy."""

    return hashlib.sha256(
        json.dumps(
            {
                "org_id": int(org_id),
                "user_id": int(user_id),
                "runtime_workspace_id": int(intent.runtime_workspace_id),
                "runtime_session_id": str(intent.runtime_session_id),
                "model_entry_id": (
                    int(model_entry_id)
                    if model_entry_id is not None
                    else None
                ),
                "runtime_agent_profile_id": (
                    int(runtime_agent_profile_id)
                    if runtime_agent_profile_id is not None
                    else None
                ),
                "source_type": str(intent.source_type),
                "channel": validate_channel(intent.channel),
                "message": (
                    intent.audit_message
                    if intent.audit_message is not None
                    else intent.message
                ),
                "attachment_file_ids": sorted(
                    {int(file_id) for file_id in intent.attachment_file_ids}
                ),
                "source_snapshot": intent.source_snapshot,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class RuntimeChatStore(Protocol):
    async def get_chat_policy(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeChatPolicy | None:
        ...

    async def create_runtime_run(
        self,
        *,
        org_id: int,
        user_id: int,
        prepared: PreparedRuntimeRun,
        timing: RuntimeChatTiming | None = None,
    ) -> CreatedRuntimeRun:
        ...

    async def get_runtime_run_event_cursor(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
    ) -> int:
        ...

    async def append_runtime_run_event_at_sequence(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        seq: int,
        event_name: str,
        payload: dict,
        dedupe_key: str | None = None,
        runtime_provider_run_id: str | None = None,
    ) -> RuntimeRunEvent:
        ...

    async def find_idempotent_runtime_run(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        runtime_run_id: str,
        model_entry_id: int | None,
        source_type: str,
        audit_message: str,
        attachment_file_ids: tuple[int, ...],
    ) -> CreatedRuntimeRun | None:
        ...

    async def get_runtime_endpoint(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> RuntimeEndpoint:
        ...

    async def get_runtime_run_control(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
    ) -> RuntimeRunControl:
        ...

    async def get_runtime_run_replay_control(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
    ) -> RuntimeRunReplayControl:
        ...

    async def get_active_runtime_run(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeActiveRun | None:
        ...

    async def get_runtime_run_source(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
    ) -> dict | None:
        ...

    async def get_runtime_session_sources(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> dict[int, dict]:
        ...

    async def record_runtime_run_history_checkpoint(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        history_message_count_before_run: int,
        source_snapshot: dict | None = None,
    ) -> None:
        ...

    async def get_runtime_session_history_control(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        include_deleting: bool = False,
    ) -> RuntimeSessionHistoryControl:
        ...

    async def bind_runtime_provider_run(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        runtime_provider_run_id: str,
    ) -> None:
        ...

    async def mark_session_materialized(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        ...

    async def mark_hermes_session_materialized(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        ...

    async def mark_runtime_run_stopped(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        runtime_provider_run_id: str | None,
        provider_response: dict | None = None,
        error_message: str | None = None,
    ) -> CreatedRuntimeRun:
        ...

    async def mark_runtime_run_canceled(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        runtime_provider_run_id: str | None,
    ) -> CreatedRuntimeRun:
        ...

    async def mark_runtime_run_finished(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        runtime_provider_run_id: str | None,
        status: str,
        error_message: str | None = None,
    ) -> CreatedRuntimeRun:
        ...

    async def append_runtime_run_event(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        event_name: str,
        payload: dict,
    ) -> RuntimeRunEvent:
        ...

    async def list_runtime_run_events(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        after_seq: int = 0,
    ) -> list[RuntimeRunEvent]:
        ...

    async def settle_self_evolution_review(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
    ) -> None:
        ...


AssetStatusPublisher = Callable[[str, dict], Awaitable[None]]


class RuntimeChatService:
    def __init__(
        self,
        store: RuntimeChatStore,
        reload_decider: RuntimeReloadDecisionService | None = None,
    ) -> None:
        self.store = store
        self.reload_decider = reload_decider or RuntimeReloadDecisionService()

    async def prepare_run(
        self,
        *,
        principal: Principal,
        intent: RuntimeChatRequestIntent,
    ) -> PreparedRuntimeRun:
        if not intent.message.strip():
            raise RuntimeChatPreparationError("message is required")

        session_status_loader = getattr(self.store, "get_session_status", None)
        if callable(session_status_loader):
            session_status = await session_status_loader(
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=intent.runtime_workspace_id,
                runtime_session_id=intent.runtime_session_id,
            )
            if session_status == "expert_deleted":
                raise RuntimeExpertSessionDeletedError(
                    RuntimeExpertSessionDeletedError.code
                )

        policy_loader = self.store.get_chat_policy
        if (
            intent.channel == "web"
            and intent.source_type == "web"
            and not any((
                intent.enabled_expert_ids,
                intent.enabled_skill_ids,
                intent.enabled_mcp_ids,
                intent.enabled_knowledge_base_ids,
            ))
        ):
            web_policy_loader = getattr(self.store, "get_web_chat_policy", None)
            if callable(web_policy_loader):
                policy_loader = web_policy_loader
        policy = await policy_loader(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=intent.runtime_workspace_id,
            runtime_session_id=intent.runtime_session_id,
        )
        if policy is None:
            raise RuntimeChatPreparationError("runtime session not found")
        if policy.org_id != principal.org_id or policy.user_id != principal.user_id:
            raise RuntimeChatPreparationError("runtime session owner mismatch")
        if not policy.runtime_ready:
            raise RuntimeChatPreparationError("runtime is not ready")

        reload_decision = self.reload_decider.decide(policy.reload_decision_input)
        if not reload_decision.allow_new_run:
            raise RuntimeChatPreparationError(reload_decision.reason)

        visible_models = policy.visible_models or tuple(
            RuntimeModelRecord(
                model_entry_id=model_entry_id,
                model_key=str(model_entry_id),
                display_name=str(model_entry_id),
                provider_id=0,
                provider_display_name="",
                is_default=(
                    model_entry_id == policy.default_model_entry_id
                ),
            )
            for model_entry_id in sorted(
                policy.visible_model_entry_ids
            )
        )
        try:
            model_entry_id = resolve_runtime_turn_model_entry_id(
                requested_model_entry_id=intent.model_entry_id,
                session_model_entry_id=(
                    policy.session_model_entry_id
                ),
                workspace_default_model_entry_id=(
                    policy.default_model_entry_id
                ),
                visible_models=visible_models,
            )
        except RuntimeTurnModelNotVisibleError as exc:
            raise RuntimeChatPreparationError(
                "model entry is not visible"
            ) from exc
        except RuntimeTurnModelUnavailableError as exc:
            raise RuntimeChatPreparationError(
                "no visible model entry is available"
            ) from exc

        enabled_expert_ids = intent.enabled_expert_ids or []
        enabled_skill_ids = intent.enabled_skill_ids or []
        enabled_mcp_ids = intent.enabled_mcp_ids or []
        enabled_knowledge_base_ids = intent.enabled_knowledge_base_ids or []
        self._assert_subset("expert", enabled_expert_ids, policy.visible_expert_ids)
        self._assert_subset("skill", enabled_skill_ids, policy.visible_skill_ids)
        self._assert_subset("mcp", enabled_mcp_ids, policy.visible_mcp_ids)
        self._assert_subset("knowledge base", enabled_knowledge_base_ids, policy.visible_knowledge_base_ids)

        return PreparedRuntimeRun(
            runtime_workspace_id=intent.runtime_workspace_id,
            runtime_session_id=intent.runtime_session_id,
            model_entry_id=model_entry_id,
            message=intent.message,
            source_type=intent.source_type,
            enabled_expert_ids=enabled_expert_ids,
            enabled_skill_ids=enabled_skill_ids,
            enabled_mcp_ids=enabled_mcp_ids,
            enabled_knowledge_base_ids=enabled_knowledge_base_ids,
            allow_new_run=True,
            audit_message=intent.audit_message,
            channel=validate_channel(intent.channel),
            runtime_agent_profile_id=policy.runtime_agent_profile_id,
            hermes_profile_name=policy.hermes_profile_name,
            runtime_run_id=intent.runtime_run_id,
            attachment_file_ids=intent.attachment_file_ids,
            source_snapshot=(
                dict(intent.source_snapshot)
                if intent.source_snapshot is not None
                else None
            ),
            client_request_digest=(
                runtime_chat_client_request_digest(
                    org_id=principal.org_id,
                    user_id=principal.user_id,
                    intent=intent,
                    model_entry_id=model_entry_id,
                    runtime_agent_profile_id=(
                        policy.runtime_agent_profile_id
                    ),
                )
                if intent.runtime_run_id
                else None
            ),
        )

    async def create_run(
        self,
        *,
        principal: Principal,
        intent: RuntimeChatRequestIntent,
        timing: RuntimeChatTiming | None = None,
    ) -> CreatedRuntimeRun:
        prepared = await self.prepare_run(principal=principal, intent=intent)
        _mark_runtime_timing(timing, "run_prepare_done")
        if timing is not None:
            _mark_runtime_timing(timing, "run_profile_gate_done")
            return await self.store.create_runtime_run(
                org_id=principal.org_id,
                user_id=principal.user_id,
                prepared=prepared,
                timing=timing,
            )
        return await self.store.create_runtime_run(
            org_id=principal.org_id,
            user_id=principal.user_id,
            prepared=prepared,
        )

    async def find_existing_client_run(
        self,
        *,
        principal: Principal,
        intent: RuntimeChatRequestIntent,
    ) -> CreatedRuntimeRun | None:
        runtime_run_id = str(intent.runtime_run_id or "").strip()
        if not runtime_run_id:
            return None
        finder = getattr(
            self.store,
            "find_idempotent_runtime_run",
            None,
        )
        if not callable(finder):
            return None
        return await finder(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=intent.runtime_workspace_id,
            runtime_session_id=intent.runtime_session_id,
            runtime_run_id=runtime_run_id,
            model_entry_id=intent.model_entry_id,
            source_type=intent.source_type,
            audit_message=(
                intent.audit_message
                if intent.audit_message is not None
                else intent.message
            ),
            attachment_file_ids=intent.attachment_file_ids,
        )

    async def create_run_with_asset_events(
        self,
        *,
        principal: Principal,
        intent: RuntimeChatRequestIntent,
        publish_asset_event: AssetStatusPublisher,
    ) -> CreatedRuntimeRun:
        logger.debug(
            "chat profile-gated run preparing workspace_id=%s session_id=%s",
            intent.runtime_workspace_id,
            intent.runtime_session_id,
        )
        logger.debug(
            "chat profile-gated run prepare_start workspace_id=%s session_id=%s",
            intent.runtime_workspace_id,
            intent.runtime_session_id,
        )
        prepared = await asyncio.wait_for(
            self.prepare_run(principal=principal, intent=intent),
            timeout=30.0,
        )
        logger.debug(
            "chat profile-gated run prepare_done workspace_id=%s session_id=%s",
            intent.runtime_workspace_id,
            intent.runtime_session_id,
        )
        logger.debug(
            "chat profile-gated run create_start workspace_id=%s session_id=%s",
            intent.runtime_workspace_id,
            intent.runtime_session_id,
        )
        return await self.store.create_runtime_run(
            org_id=principal.org_id,
            user_id=principal.user_id,
            prepared=prepared,
        )

    async def get_runtime_endpoint(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
    ) -> RuntimeEndpoint:
        return await self.store.get_runtime_endpoint(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
        )

    async def get_run_control(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
    ) -> RuntimeRunControl:
        return await self.store.get_runtime_run_control(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
        )

    async def get_run_replay_control(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
    ) -> RuntimeRunReplayControl:
        return await self.store.get_runtime_run_replay_control(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
        )

    async def get_active_run(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeActiveRun | None:
        return await self.store.get_active_runtime_run(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )

    async def get_run_source(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
    ) -> dict | None:
        getter = getattr(self.store, "get_runtime_run_source", None)
        if not callable(getter):
            return None
        return await getter(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
        )

    async def get_history_sources(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> dict[int, dict]:
        getter = getattr(self.store, "get_runtime_session_sources", None)
        if not callable(getter):
            return {}
        return await getter(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )

    async def record_history_checkpoint(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
        history_message_count_before_run: int,
        source_snapshot: dict | None = None,
    ) -> None:
        await self.store.record_runtime_run_history_checkpoint(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
            history_message_count_before_run=history_message_count_before_run,
            source_snapshot=source_snapshot,
        )

    async def prepare_scheduled_source(
        self,
        *,
        principal: Principal,
        created: CreatedRuntimeRun,
        scheduled_task_id: int | None = None,
        scheduled_task_name: str | None = None,
        trigger_kind: str,
        snapshot: object | None = None,
    ) -> dict:
        """Persist the immutable scheduled source and history boundary.

        The history count is read through the short-lived RuntimeChat store
        transaction immediately before the source payload is committed.  The
        caller must complete this step before handing the Run to the stream
        executor so a missing checkpoint can never produce an untraceable
        assistant response.
        """
        if snapshot is not None:
            scheduled_task_id = getattr(snapshot, "task_id", scheduled_task_id)
            scheduled_task_name = getattr(snapshot, "task_name", scheduled_task_name)
        if scheduled_task_id is None or scheduled_task_name is None:
            raise RuntimeChatPreparationError("scheduled source snapshot is incomplete")
        history = await self.get_session_history_control(
            principal=principal,
            runtime_workspace_id=created.runtime_workspace_id,
            runtime_session_id=created.runtime_session_id,
        )
        source = {
            "type": "scheduled",
            "scheduled_task_id": int(scheduled_task_id),
            "scheduled_task_name": str(scheduled_task_name),
            "trigger_kind": str(trigger_kind),
            "task_available": True,
        }
        await self.store.record_runtime_run_history_checkpoint(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=created.runtime_run_id,
            history_message_count_before_run=int(history.message_count),
            source_snapshot=source,
        )
        return source

    async def get_session_history_control(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
        include_deleting: bool = False,
    ) -> RuntimeSessionHistoryControl:
        kwargs = {
            "org_id": principal.org_id,
            "user_id": principal.user_id,
            "runtime_workspace_id": runtime_workspace_id,
            "runtime_session_id": runtime_session_id,
        }
        if include_deleting:
            kwargs["include_deleting"] = True
        return await self.store.get_runtime_session_history_control(**kwargs)

    async def prepare_session_knowledge_context(
        self,
        *,
        runtime_workspace_id: int,
        runtime_session_id: str,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> None:
        # This work deliberately owns independent short-lived DB sessions so
        # enterprise and personal preparation can run concurrently without
        # sharing this service's request-scoped AsyncSession.
        from app.services.runtime_prepared_knowledge import (
            shared_runtime_prepared_knowledge_service,
        )

        await shared_runtime_prepared_knowledge_service().prepare_workspace(
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            runtime_token=runtime_token,
        )

    async def bind_provider_run(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
        runtime_provider_run_id: str,
    ) -> None:
        await self.store.bind_runtime_provider_run(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
            runtime_provider_run_id=runtime_provider_run_id,
        )

    async def bind_provider_run_and_append_event(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
        runtime_provider_run_id: str,
        event_name: str,
        payload: dict,
    ) -> RuntimeRunEvent:
        combined = getattr(
            self.store,
            "bind_runtime_provider_run_and_append_event",
            None,
        )
        if not callable(combined):
            await self.bind_provider_run(
                principal=principal,
                runtime_run_id=runtime_run_id,
                runtime_provider_run_id=runtime_provider_run_id,
            )
            return await self.append_event(
                principal=principal,
                runtime_run_id=runtime_run_id,
                event_name=event_name,
                payload=payload,
            )
        return await combined(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
            runtime_provider_run_id=runtime_provider_run_id,
            event_name=event_name,
            payload=payload,
        )

    async def mark_session_materialized(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        await self.store.mark_session_materialized(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )

    async def mark_hermes_session_materialized(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        await self.store.mark_hermes_session_materialized(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )

    async def mark_stopped(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
        runtime_provider_run_id: str | None,
        provider_response: dict | None = None,
        error_message: str | None = None,
    ) -> CreatedRuntimeRun:
        return await self.store.mark_runtime_run_stopped(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
            runtime_provider_run_id=runtime_provider_run_id,
            provider_response=provider_response,
            error_message=error_message,
        )

    async def mark_canceled(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
        runtime_provider_run_id: str | None,
    ) -> CreatedRuntimeRun:
        return await self.store.mark_runtime_run_canceled(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
            runtime_provider_run_id=runtime_provider_run_id,
        )

    async def mark_finished(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
        runtime_provider_run_id: str | None,
        status: str,
        error_message: str | None = None,
    ) -> CreatedRuntimeRun:
        if status not in {"completed", "failed"}:
            raise ValueError("runtime run terminal status must be completed or failed")
        result = await self.store.mark_runtime_run_finished(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
            runtime_provider_run_id=runtime_provider_run_id,
            status=status,
            error_message=error_message,
        )
        return result

    async def append_event(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
        event_name: str,
        payload: dict,
    ) -> RuntimeRunEvent:
        return await self.store.append_runtime_run_event(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
            event_name=event_name,
            payload=payload,
        )

    async def get_event_cursor(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
    ) -> int:
        return await self.store.get_runtime_run_event_cursor(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
        )

    async def append_event_at_sequence(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
        seq: int,
        event_name: str,
        payload: dict,
        dedupe_key: str | None = None,
        runtime_provider_run_id: str | None = None,
    ) -> RuntimeRunEvent:
        return await self.store.append_runtime_run_event_at_sequence(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
            seq=seq,
            event_name=event_name,
            payload=payload,
            dedupe_key=dedupe_key,
            runtime_provider_run_id=runtime_provider_run_id,
        )

    async def list_events(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
        after_seq: int = 0,
    ) -> list[RuntimeRunEvent]:
        return await self.store.list_runtime_run_events(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_run_id=runtime_run_id,
            after_seq=after_seq,
        )

    async def settle_self_evolution_review(
        self,
        *,
        principal: Principal,
        runtime_run_id: str,
    ) -> None:
        settle = getattr(self.store, "settle_self_evolution_review", None)
        if callable(settle):
            await settle(
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_run_id=runtime_run_id,
            )

    @staticmethod
    def _assert_subset(label: str, selected: list[int], visible: set[int]) -> None:
        missing = [item for item in selected if item not in visible]
        if missing:
            raise RuntimeChatPreparationError(f"{label} is not visible: {missing[0]}")


class SqlAlchemyRuntimeChatStore:
    def __init__(
        self,
        db: AsyncSession,
        *,
        api_key_codec: RuntimeApiKeyCodec | None = None,
        asset_visibility_service: AssetVisibilityService | None = None,
        route_cache: RedisModelRouteCache | None = None,
        event_write_max_attempts: int = 3,
        event_write_retry_base_delay_seconds: float = 0.02,
        run_create_max_attempts: int = 3,
        run_create_retry_base_delay_seconds: float = 0.02,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.db = db
        self.api_key_codec = api_key_codec or RuntimeApiKeyCodec()
        self.asset_visibility_service = asset_visibility_service or AssetVisibilityService(
            SqlAlchemyAssetVisibilityStore(db)
        )
        self.route_cache = route_cache
        self.event_write_max_attempts = max(1, event_write_max_attempts)
        self.event_write_retry_base_delay_seconds = max(
            0.0, event_write_retry_base_delay_seconds
        )
        self.run_create_max_attempts = max(1, run_create_max_attempts)
        self.run_create_retry_base_delay_seconds = max(
            0.0, run_create_retry_base_delay_seconds
        )
        self.sleep = sleep

    def _endpoint_from_binding(self, binding: HermesRuntimeBinding) -> RuntimeEndpoint:
        if not binding.endpoint_base_url or not binding.endpoint_host_header:
            raise RuntimeChatPreparationError("runtime endpoint is not ready")
        return RuntimeEndpoint(
            runtime_workspace_id=int(binding.runtime_workspace_id),
            runtime_id=str(binding.runtime_id),
            endpoint_base_url=str(binding.endpoint_base_url),
            endpoint_host_header=str(binding.endpoint_host_header),
            api_key=self.api_key_codec.decrypt(str(binding.api_key_encrypted)),
        )

    async def get_session_status(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> str | None:
        return await self.db.scalar(
            select(RuntimeSession.status).where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
            )
        )

    async def get_chat_policy(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeChatPolicy | None:
        return await self._load_chat_policy(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            load_bound_assets=True,
        )

    async def get_web_chat_policy(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeChatPolicy | None:
        return await self._load_chat_policy(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            load_bound_assets=False,
        )

    async def _load_chat_policy(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        load_bound_assets: bool,
    ) -> RuntimeChatPolicy | None:
        result = await self.db.execute(
            select(
                RuntimeSession,
                CloudHermesProfile,
                RuntimeAgentProfile,
                HermesRuntimeBinding,
            )
            .join(
                CloudHermesProfile,
                CloudHermesProfile.id == RuntimeSession.runtime_workspace_id,
            )
            .join(
                RuntimeAgentProfile,
                RuntimeAgentProfile.id == RuntimeSession.runtime_agent_profile_id,
            )
            .outerjoin(
                HermesRuntimeBinding,
                HermesRuntimeBinding.runtime_workspace_id == RuntimeSession.runtime_workspace_id,
            )
            .where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
                RuntimeSession.status == "active",
                CloudHermesProfile.status == "active",
                *executable_runtime_agent_profile_clauses(),
            )
        )
        row = result.first()
        if row is None:
            return None
        session, profile, agent_profile, binding = row
        visible_models = await self._visible_models(
            org_id=org_id,
            user_id=user_id,
        )
        visible_model_entry_ids = {
            model.model_entry_id for model in visible_models
        }
        visible_expert_ids: set[int] = set()
        visible_skill_ids: set[int] = set()
        visible_mcp_ids: set[int] = set()
        visible_knowledge_base_ids: set[int] = set()
        if load_bound_assets:
            principal = Principal(org_id=org_id, user_id=user_id)
            visible_expert_ids = await self._visible_bound_asset_ids(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=int(agent_profile.id),
                asset_type="expert_package",
            )
            visible_skill_ids = await self._visible_bound_asset_ids(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=int(agent_profile.id),
                asset_type="skill",
            )
            visible_mcp_ids = await self._visible_bound_asset_ids(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=int(agent_profile.id),
                asset_type="mcp",
            )
            visible_knowledge_base_ids = await self._visible_bound_asset_ids(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_agent_profile_id=int(agent_profile.id),
                asset_type="knowledge_base",
            )
        runtime_status = RuntimeStatus.ACTIVE if binding and binding.active_run_count > 0 else RuntimeStatus.READY_IDLE
        return RuntimeChatPolicy(
            runtime_workspace_id=int(runtime_workspace_id),
            session_id=int(session.id),
            org_id=int(session.org_id),
            user_id=int(session.user_id),
            runtime_session_id=session.runtime_session_id,
            session_model_entry_id=int(session.session_model_entry_id) if session.session_model_entry_id else None,
            default_model_entry_id=(
                int(profile.default_model_id)
                if profile.default_model_id
                else resolve_default_model_entry_id(visible_models)
            ),
            visible_model_entry_ids=visible_model_entry_ids,
            visible_expert_ids=visible_expert_ids,
            visible_skill_ids=visible_skill_ids,
            visible_mcp_ids=visible_mcp_ids,
            visible_knowledge_base_ids=visible_knowledge_base_ids,
            runtime_ready=bool(binding and binding.status == "ready"),
            reload_decision_input=ReloadDecisionInput(
                runtime_status=runtime_status,
                scopes=set(),
                hot_reload_supported=True,
                active_run_count=int(binding.active_run_count) if binding else 0,
            ),
            runtime_agent_profile_id=int(agent_profile.id),
            hermes_profile_name=str(agent_profile.hermes_profile_name),
            visible_models=tuple(visible_models),
        )

    async def create_runtime_run(
        self,
        *,
        org_id: int,
        user_id: int,
        prepared: PreparedRuntimeRun,
        timing: RuntimeChatTiming | None = None,
    ) -> CreatedRuntimeRun:
        generated_runtime_run_id = (
            str(prepared.runtime_run_id or "").strip()
            or f"rr-{prepared.runtime_workspace_id}-{uuid4().hex[:16]}"
        )
        last_error_code: int | None = None
        for attempt in range(1, self.run_create_max_attempts + 1):
            try:
                result = await self._create_runtime_run_once(
                    org_id=org_id,
                    user_id=user_id,
                    prepared=prepared,
                    generated_runtime_run_id=generated_runtime_run_id,
                    timing=timing,
                )
                if timing is not None:
                    timing.set_metadata("run_db_attempts", str(attempt))
                    timing.set_metadata(
                        "run_db_retry_count",
                        str(attempt - 1),
                    )
                    if last_error_code is not None:
                        timing.set_metadata(
                            "run_db_retry_error",
                            str(last_error_code),
                        )
                return result
            except DBAPIError as exc:
                error_code = self._mysql_error_code(exc)
                if error_code not in {1205, 1213}:
                    raise
                last_error_code = error_code
                await self.db.rollback()
                if timing is not None:
                    timing.set_metadata("run_db_attempts", str(attempt))
                    timing.set_metadata(
                        "run_db_retry_count",
                        str(max(0, attempt - 1)),
                    )
                    timing.set_metadata(
                        "run_db_retry_error",
                        str(error_code),
                    )
                if (
                    error_code == 1205
                    or attempt >= self.run_create_max_attempts
                ):
                    logger.error(
                        "runtime run create database busy run_id=%s "
                        "attempt=%s mysql_error=%s",
                        generated_runtime_run_id,
                        attempt,
                        error_code,
                    )
                    raise RuntimeChatDatabaseBusyError(
                        mysql_error_code=error_code
                    ) from exc
                delay = self._run_create_retry_delay(attempt)
                logger.warning(
                    "runtime run create retry run_id=%s attempt=%s "
                    "mysql_error=%s delay_ms=%.3f",
                    generated_runtime_run_id,
                    attempt,
                    error_code,
                    delay * 1000,
                )
                await self.sleep(delay)
        raise AssertionError(
            "runtime run create retry loop exhausted without an exception"
        )

    async def _create_runtime_run_once(
        self,
        *,
        org_id: int,
        user_id: int,
        prepared: PreparedRuntimeRun,
        generated_runtime_run_id: str,
        timing: RuntimeChatTiming | None = None,
    ) -> CreatedRuntimeRun:
        requested_run_id = str(
            prepared.runtime_run_id or ""
        ).strip()
        if requested_run_id and len(requested_run_id) > 128:
            raise RuntimeChatPreparationError(
                "runtime run id is too long"
            )
        if requested_run_id:
            existing = await self._find_prepared_runtime_run(
                runtime_run_id=requested_run_id,
            )
            _mark_runtime_timing(
                timing,
                "run_reserved_id_precheck_done",
            )
            if existing is not None:
                run, existing_session = existing
                created = self._created_from_existing_runtime_run(
                    run=run,
                    session=existing_session,
                    prepared=prepared,
                    org_id=org_id,
                    user_id=user_id,
                )
                await self.db.rollback()
                return created
        else:
            _mark_runtime_timing(
                timing,
                "run_reserved_id_precheck_done",
            )

        binding = (
            await self.db.execute(
                select(HermesRuntimeBinding)
                .where(
                    HermesRuntimeBinding.org_id == org_id,
                    HermesRuntimeBinding.user_id == user_id,
                    HermesRuntimeBinding.runtime_workspace_id
                    == prepared.runtime_workspace_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        _mark_runtime_timing(timing, "run_binding_lock_done")
        if binding is None:
            raise RuntimeChatPreparationError("runtime binding not found")
        try:
            RuntimeAdmissionService.require_ready(str(binding.status))
        except RuntimeAdmissionError as exc:
            raise RuntimeChatAdmissionError(exc.decision) from exc
        # The default MCP bootstrap claims the binding lock first and then
        # this profile row. Holding the same pair of locks here prevents a
        # first Run from slipping between its idle check and MCP reload.
        profile = (
            await self.db.execute(
                select(CloudHermesProfile)
                .where(
                    CloudHermesProfile.id == prepared.runtime_workspace_id,
                    CloudHermesProfile.org_id == org_id,
                    CloudHermesProfile.user_id == user_id,
                    CloudHermesProfile.profile_type == "default_chat_agent",
                    CloudHermesProfile.status == "active",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if profile is None:
            raise RuntimeChatPreparationError("runtime profile not found")
        bootstrap = (
            profile.profile_config.get("default_mcp_bootstrap")
            if profile is not None and isinstance(profile.profile_config, dict)
            else None
        )
        if isinstance(bootstrap, dict) and bootstrap.get("status") in {"pending", "installing"}:
            raise RuntimeChatPreparationError("default MCP is initializing")

        runtime_agent_profile = None
        if prepared.runtime_agent_profile_id is not None:
            runtime_agent_profile = (
                await self.db.execute(
                    select(RuntimeAgentProfile)
                    .where(
                        RuntimeAgentProfile.id
                        == prepared.runtime_agent_profile_id,
                        RuntimeAgentProfile.org_id == org_id,
                        RuntimeAgentProfile.user_id == user_id,
                        RuntimeAgentProfile.runtime_workspace_id
                        == prepared.runtime_workspace_id,
                        *executable_runtime_agent_profile_clauses(),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if runtime_agent_profile is None:
                raise RuntimeChatPreparationError(
                    "runtime agent profile not found"
                )
            if (
                str(runtime_agent_profile.hermes_profile_name)
                != prepared.hermes_profile_name
            ):
                raise RuntimeChatPreparationError(
                    "runtime profile identity changed"
                )

        session = (
            await self.db.execute(
                select(RuntimeSession).where(
                    RuntimeSession.org_id == org_id,
                    RuntimeSession.user_id == user_id,
                    RuntimeSession.runtime_workspace_id == prepared.runtime_workspace_id,
                    RuntimeSession.runtime_session_id == prepared.runtime_session_id,
                    RuntimeSession.status == "active",
                ).with_for_update()
            )
        ).scalar_one_or_none()
        _mark_runtime_timing(timing, "run_session_lock_done")
        if session is None:
            raise RuntimeChatPreparationError("runtime session not found")
        if (
            prepared.runtime_agent_profile_id is not None
            and int(session.runtime_agent_profile_id)
            != int(prepared.runtime_agent_profile_id)
        ):
            raise RuntimeChatPreparationError(
                "runtime Session profile changed"
            )
        _mark_runtime_timing(
            timing,
            "run_reserved_id_check_done",
        )
        active_run = None
        if prepared.source_type != "scheduled":
            active_run = (
                await self.db.execute(
                    select(RuntimeRun).where(
                        RuntimeRun.org_id == org_id,
                        RuntimeRun.user_id == user_id,
                        RuntimeRun.session_id == session.id,
                        RuntimeRun.status.in_(("accepted", "pending", "running")),
                    )
                )
            ).scalar_one_or_none()
        _mark_runtime_timing(timing, "run_active_check_done")
        if active_run is not None:
            if (
                requested_run_id
                and str(active_run.runtime_run_id)
                == requested_run_id
            ):
                created = self._created_from_existing_runtime_run(
                    run=active_run,
                    session=session,
                    prepared=prepared,
                    org_id=org_id,
                    user_id=user_id,
                )
                await self.db.rollback()
                return created
            raise RuntimeChatActiveRunError(
                "runtime session already has an active run"
            )

        runtime_run_id = (
            requested_run_id
            or generated_runtime_run_id
        )
        now = datetime.now(timezone.utc)
        run = RuntimeRun(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=prepared.runtime_workspace_id,
            session_id=session.id,
            runtime_session_id=prepared.runtime_session_id,
            runtime_run_id=runtime_run_id,
            model_entry_id=prepared.model_entry_id,
            status=(
                "pending"
                if prepared.source_type == "wecom"
                else "queued"
                if prepared.source_type == "scheduled"
                else "running"
            ),
            source_type=prepared.source_type,
            request_payload={
                "message": prepared.message,
                "audit_message": (
                    prepared.audit_message
                    if prepared.audit_message is not None
                    else prepared.message
                ),
                "channel": validate_channel(prepared.channel),
                "attachment_file_ids": sorted(
                    {
                        int(file_id)
                        for file_id in prepared.attachment_file_ids
                    }
                ),
                "runtime_agent_profile_id": (
                    int(prepared.runtime_agent_profile_id)
                    if prepared.runtime_agent_profile_id is not None
                    else None
                ),
                "hermes_profile_name": prepared.hermes_profile_name,
                **(
                    {
                        "history_message_count_before_run": (
                            prepared.history_message_count_before_run
                        )
                    }
                    if prepared.history_message_count_before_run
                    is not None
                    else {}
                ),
                **(
                    {
                        "client_request_digest": (
                            prepared.client_request_digest
                        )
                    }
                    if prepared.client_request_digest is not None
                    else {}
                ),
                **(
                    {
                        "source": dict(prepared.source_snapshot),
                        **dict(prepared.source_snapshot),
                    }
                    if prepared.source_snapshot is not None
                    else {}
                ),
            },
            started_at=(
                None
                if prepared.source_type in {"wecom", "scheduled"}
                else now
            ),
        )
        _mark_runtime_timing(timing, "run_insert_begin")
        if requested_run_id:
            try:
                async with self.db.begin_nested():
                    self.db.add(run)
                    await self.db.flush()
            except IntegrityError as exc:
                if not self._is_runtime_run_id_unique_violation(exc):
                    raise
                existing = await self._find_prepared_runtime_run(
                    runtime_run_id=requested_run_id,
                    for_update=True,
                )
                if existing is None:
                    raise
                existing_run, existing_session = existing
                created = self._created_from_existing_runtime_run(
                    run=existing_run,
                    session=existing_session,
                    prepared=prepared,
                    org_id=org_id,
                    user_id=user_id,
                )
                await self.db.rollback()
                return created
        else:
            self.db.add(run)
        _mark_runtime_timing(timing, "run_insert_done")
        session.last_active_at = now
        _mark_runtime_timing(timing, "run_commit_begin")
        await self.db.commit()
        _mark_runtime_timing(timing, "run_commit_done")
        return CreatedRuntimeRun(
            runtime_workspace_id=int(run.runtime_workspace_id),
            runtime_session_id=str(run.runtime_session_id),
            runtime_run_id=str(run.runtime_run_id),
            model_entry_id=int(run.model_entry_id) if run.model_entry_id else None,
            status=(
                "queued"
                if prepared.source_type == "scheduled"
                else "accepted"
            ),
            title=session.title_cache,
            session_materialized=session.hermes_materialized_at is not None,
            runtime_agent_profile_id=(
                int(session.runtime_agent_profile_id)
                if session.runtime_agent_profile_id is not None
                else prepared.runtime_agent_profile_id
            ),
            hermes_profile_name=prepared.hermes_profile_name,
        )

    async def _find_prepared_runtime_run(
        self,
        *,
        runtime_run_id: str,
        for_update: bool = False,
    ) -> tuple[RuntimeRun, RuntimeSession] | None:
        statement = (
            select(RuntimeRun, RuntimeSession)
            .join(
                RuntimeSession,
                RuntimeSession.id == RuntimeRun.session_id,
            )
            .where(
                RuntimeRun.runtime_run_id == runtime_run_id
            )
        )
        if for_update:
            statement = statement.with_for_update(
                of=RuntimeRun,
            )
        return (
            await self.db.execute(
                statement
            )
        ).one_or_none()

    @staticmethod
    def _created_from_existing_runtime_run(
        *,
        run: RuntimeRun,
        session: RuntimeSession,
        prepared: PreparedRuntimeRun,
        org_id: int,
        user_id: int,
    ) -> CreatedRuntimeRun:
        request_payload = (
            run.request_payload
            if isinstance(run.request_payload, dict)
            else {}
        )
        if (
            int(run.org_id) != org_id
            or int(run.user_id) != user_id
            or int(run.runtime_workspace_id)
            != prepared.runtime_workspace_id
            or int(run.session_id or 0) != int(session.id)
            or str(run.runtime_session_id or "")
            != prepared.runtime_session_id
            or str(session.runtime_session_id)
            != prepared.runtime_session_id
            or str(run.source_type) != prepared.source_type
            or (
                prepared.runtime_agent_profile_id is not None
                and int(session.runtime_agent_profile_id or 0)
                != int(prepared.runtime_agent_profile_id)
            )
            or (
                prepared.model_entry_id is not None
                and int(run.model_entry_id or 0)
                != int(prepared.model_entry_id)
            )
            or (
                prepared.client_request_digest is not None
                and str(
                    request_payload.get(
                        "client_request_digest"
                    )
                    or ""
                )
                != prepared.client_request_digest
            )
        ):
            raise RuntimeChatIdempotencyConflictError(
                "runtime run id is already in use"
            )
        return CreatedRuntimeRun(
            runtime_workspace_id=int(run.runtime_workspace_id),
            runtime_session_id=str(run.runtime_session_id),
            runtime_run_id=str(run.runtime_run_id),
            model_entry_id=(
                int(run.model_entry_id)
                if run.model_entry_id is not None
                else None
            ),
            status=str(run.status),
            title=session.title_cache,
            session_materialized=(
                session.hermes_materialized_at is not None
            ),
            runtime_agent_profile_id=(
                int(session.runtime_agent_profile_id)
                if session.runtime_agent_profile_id is not None
                else prepared.runtime_agent_profile_id
            ),
            hermes_profile_name=prepared.hermes_profile_name,
            reused=True,
        )

    @classmethod
    def _is_runtime_run_id_unique_violation(
        cls,
        exc: IntegrityError,
    ) -> bool:
        if cls._mysql_error_code(exc) != 1062:
            return False
        messages: list[str] = []
        current: object | None = exc
        for _ in range(4):
            messages.append(str(current))
            current = getattr(current, "orig", None)
            if current is None:
                break
        normalized = " ".join(messages).lower()
        return (
            "uq_runtime_run_runtime_id" in normalized
            or (
                "runtime_run.runtime_run_id" in normalized
                and "unique" in normalized
            )
        )

    def _run_create_retry_delay(self, failed_attempt: int) -> float:
        base = self.run_create_retry_base_delay_seconds
        return (
            base * (2 ** max(0, failed_attempt - 1))
            + random.uniform(0, base)
        )

    async def find_idempotent_runtime_run(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        runtime_run_id: str,
        model_entry_id: int | None,
        source_type: str,
        audit_message: str,
        attachment_file_ids: tuple[int, ...],
    ) -> CreatedRuntimeRun | None:
        row = (
            await self.db.execute(
                select(
                    RuntimeRun,
                    RuntimeSession,
                    RuntimeAgentProfile,
                    CloudHermesProfile,
                )
                .join(
                    RuntimeSession,
                    RuntimeSession.id == RuntimeRun.session_id,
                )
                .join(
                    RuntimeAgentProfile,
                    RuntimeAgentProfile.id
                    == RuntimeSession.runtime_agent_profile_id,
                )
                .join(
                    CloudHermesProfile,
                    CloudHermesProfile.id
                    == RuntimeRun.runtime_workspace_id,
                )
                .where(RuntimeRun.runtime_run_id == runtime_run_id)
            )
        ).one_or_none()
        if row is None:
            return None
        run, session, agent_profile, cloud_profile = row
        effective_model_entry_id = (
            int(model_entry_id)
            if model_entry_id is not None
            else int(
                session.session_model_entry_id
                or cloud_profile.default_model_id
                or 0
            )
        )
        intent = RuntimeChatRequestIntent(
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            message=audit_message,
            model_entry_id=(
                effective_model_entry_id
                if effective_model_entry_id
                else None
            ),
            source_type=source_type,
            audit_message=audit_message,
            channel="web",
            runtime_run_id=runtime_run_id,
            attachment_file_ids=attachment_file_ids,
        )
        expected_digest = runtime_chat_client_request_digest(
            org_id=org_id,
            user_id=user_id,
            intent=intent,
            model_entry_id=(
                effective_model_entry_id
                if effective_model_entry_id
                else None
            ),
            runtime_agent_profile_id=int(
                session.runtime_agent_profile_id
            ),
        )
        request_payload = (
            run.request_payload
            if isinstance(run.request_payload, dict)
            else {}
        )
        if (
            int(run.org_id) != int(org_id)
            or int(run.user_id) != int(user_id)
            or int(run.runtime_workspace_id)
            != int(runtime_workspace_id)
            or str(run.runtime_session_id or "")
            != str(runtime_session_id)
            or str(run.source_type) != str(source_type)
            or int(run.model_entry_id or 0)
            != int(effective_model_entry_id)
            or str(
                request_payload.get("client_request_digest") or ""
            )
            != expected_digest
        ):
            raise RuntimeChatIdempotencyConflictError(
                "runtime run id is already in use"
            )
        return CreatedRuntimeRun(
            runtime_workspace_id=int(run.runtime_workspace_id),
            runtime_session_id=str(run.runtime_session_id),
            runtime_run_id=str(run.runtime_run_id),
            model_entry_id=(
                int(run.model_entry_id)
                if run.model_entry_id is not None
                else None
            ),
            status=str(run.status),
            title=session.title_cache,
            session_materialized=(
                session.hermes_materialized_at is not None
            ),
            runtime_agent_profile_id=int(
                session.runtime_agent_profile_id
            ),
            hermes_profile_name=str(
                agent_profile.hermes_profile_name
            ),
            reused=True,
        )

    async def append_runtime_run_event(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        event_name: str,
        payload: dict,
    ) -> RuntimeRunEvent:
        for attempt in range(1, self.event_write_max_attempts + 1):
            try:
                return await self._append_runtime_run_event_once(
                    org_id=org_id,
                    user_id=user_id,
                    runtime_run_id=runtime_run_id,
                    event_name=event_name,
                    payload=payload,
                )
            except Exception as exc:
                await self.db.rollback()
                error_code = self._mysql_error_code(exc)
                if (
                    error_code not in {1205, 1213}
                    or attempt >= self.event_write_max_attempts
                ):
                    raise
                logger.warning(
                    "runtime event write retry run_id=%s attempt=%s mysql_error=%s",
                    runtime_run_id,
                    attempt,
                    error_code,
                )
                await self.sleep(self._event_write_retry_delay(attempt))
        raise AssertionError("event write retry loop exhausted without an exception")

    async def get_runtime_run_event_cursor(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
    ) -> int:
        latest_seq = await self.db.scalar(
            select(func.max(RuntimeRunEvent.seq))
            .select_from(RuntimeRun)
            .outerjoin(
                RuntimeRunEvent,
                RuntimeRunEvent.run_id == RuntimeRun.id,
            )
            .where(
                RuntimeRun.org_id == org_id,
                RuntimeRun.user_id == user_id,
                RuntimeRun.runtime_run_id == runtime_run_id,
            )
        )
        if latest_seq is None:
            exists = await self.db.scalar(
                select(RuntimeRun.id).where(
                    RuntimeRun.org_id == org_id,
                    RuntimeRun.user_id == user_id,
                    RuntimeRun.runtime_run_id == runtime_run_id,
                )
            )
            if exists is None:
                raise RuntimeChatPreparationError("runtime run not found")
            return 0
        return int(latest_seq)

    async def append_runtime_run_event_at_sequence(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        seq: int,
        event_name: str,
        payload: dict,
        dedupe_key: str | None = None,
        runtime_provider_run_id: str | None = None,
    ) -> RuntimeRunEvent:
        normalized_seq = int(seq)
        if normalized_seq <= 0:
            raise ValueError("runtime event seq must be positive")
        for attempt in range(1, self.event_write_max_attempts + 1):
            try:
                if runtime_provider_run_id is not None:
                    await self.db.execute(
                        update(RuntimeRun)
                        .where(
                            RuntimeRun.org_id == org_id,
                            RuntimeRun.user_id == user_id,
                            RuntimeRun.runtime_run_id == runtime_run_id,
                        )
                        .values(
                            runtime_provider_run_id=runtime_provider_run_id,
                            status="running",
                        )
                    )
                source = select(
                    RuntimeRun.id,
                    RuntimeRun.runtime_run_id,
                    literal(normalized_seq),
                    literal(event_name),
                    literal(dedupe_key),
                    literal(dict(payload), type_=SqlJSON),
                ).where(
                    RuntimeRun.org_id == org_id,
                    RuntimeRun.user_id == user_id,
                    RuntimeRun.runtime_run_id == runtime_run_id,
                )
                result = await self.db.execute(
                    insert(RuntimeRunEvent).from_select(
                        (
                            "run_id",
                            "runtime_run_public_id",
                            "seq",
                            "event_name",
                            "dedupe_key",
                            "payload",
                        ),
                        source,
                    )
                )
                if result.rowcount != 1:
                    await self.db.rollback()
                    raise RuntimeChatPreparationError(
                        "runtime run not found"
                    )
                await self.db.commit()
                return RuntimeRunEvent(
                    run_id=0,
                    runtime_run_public_id=runtime_run_id,
                    seq=normalized_seq,
                    event_name=event_name,
                    dedupe_key=dedupe_key,
                    payload=dict(payload),
                )
            except IntegrityError:
                await self.db.rollback()
                if dedupe_key is not None:
                    existing = (
                        await self.db.execute(
                            select(RuntimeRunEvent)
                            .join(
                                RuntimeRun,
                                RuntimeRun.id == RuntimeRunEvent.run_id,
                            )
                            .where(
                                RuntimeRun.org_id == org_id,
                                RuntimeRun.user_id == user_id,
                                RuntimeRun.runtime_run_id
                                == runtime_run_id,
                                RuntimeRunEvent.dedupe_key
                                == dedupe_key,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        return existing
                raise
            except Exception as exc:
                await self.db.rollback()
                error_code = self._mysql_error_code(exc)
                if (
                    error_code not in {1205, 1213}
                    or attempt >= self.event_write_max_attempts
                ):
                    raise
                await self.sleep(self._event_write_retry_delay(attempt))
        raise AssertionError(
            "fast event write retry loop exhausted without an exception"
        )

    async def bind_runtime_provider_run_and_append_event(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        runtime_provider_run_id: str,
        event_name: str,
        payload: dict,
    ) -> RuntimeRunEvent:
        for attempt in range(1, self.event_write_max_attempts + 1):
            try:
                return await self._append_runtime_run_event_once(
                    org_id=org_id,
                    user_id=user_id,
                    runtime_run_id=runtime_run_id,
                    event_name=event_name,
                    payload=payload,
                    runtime_provider_run_id=runtime_provider_run_id,
                )
            except Exception as exc:
                await self.db.rollback()
                error_code = self._mysql_error_code(exc)
                if (
                    error_code not in {1205, 1213}
                    or attempt >= self.event_write_max_attempts
                ):
                    raise
                logger.warning(
                    "runtime started event write retry run_id=%s attempt=%s mysql_error=%s",
                    runtime_run_id,
                    attempt,
                    error_code,
                )
                await self.sleep(self._event_write_retry_delay(attempt))
        raise AssertionError("started event write retry loop exhausted without an exception")

    async def _append_runtime_run_event_once(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        event_name: str,
        payload: dict,
        runtime_provider_run_id: str | None = None,
    ) -> RuntimeRunEvent:
        run = (
            await self.db.execute(
                select(RuntimeRun).where(
                    RuntimeRun.org_id == org_id,
                    RuntimeRun.user_id == user_id,
                    RuntimeRun.runtime_run_id == runtime_run_id,
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            raise RuntimeChatPreparationError("runtime run not found")
        if runtime_provider_run_id is not None:
            summary = dict(run.response_summary or {})
            summary["hermes_run_id"] = runtime_provider_run_id
            run.response_summary = summary
            run.runtime_provider_run_id = runtime_provider_run_id
            run.status = "running"
        if event_name == "chat.done":
            existing_done = (
                await self.db.execute(
                    select(RuntimeRunEvent)
                    .where(
                        RuntimeRunEvent.run_id == run.id,
                        RuntimeRunEvent.event_name == "chat.done",
                    )
                    .order_by(RuntimeRunEvent.seq.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing_done is not None:
                return existing_done
        latest_seq = (
            await self.db.execute(
                select(RuntimeRunEvent.seq)
                .where(RuntimeRunEvent.run_id == run.id)
                .order_by(RuntimeRunEvent.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none() or 0
        event = RuntimeRunEvent(
            run_id=int(run.id),
            runtime_run_public_id=str(run.runtime_run_id),
            seq=int(latest_seq) + 1,
            event_name=event_name,
            payload=dict(payload),
        )
        self.db.add(event)
        await self.db.commit()
        return event

    def _event_write_retry_delay(self, failed_attempt: int) -> float:
        base = self.event_write_retry_base_delay_seconds
        return base * (2 ** max(0, failed_attempt - 1)) + random.uniform(0, base)

    @staticmethod
    def _mysql_error_code(exc: Exception) -> int | None:
        if not isinstance(exc, DBAPIError):
            return None
        current: object | None = exc
        for _ in range(4):
            args = getattr(current, "args", ())
            if args and isinstance(args[0], int):
                return int(args[0])
            current = getattr(current, "orig", None)
            if current is None:
                break
        return None

    async def recover_runtime_run_failure(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        error_message: str,
    ) -> list[RuntimeRunEvent]:
        try:
            run = (
                await self.db.execute(
                    select(RuntimeRun)
                    .where(
                        RuntimeRun.org_id == org_id,
                        RuntimeRun.user_id == user_id,
                        RuntimeRun.runtime_run_id == runtime_run_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                raise RuntimeChatPreparationError("runtime run not found")
            if run.status in {"completed", "failed", "canceled", "cancel_failed"}:
                await self.db.commit()
                return []

            latest_seq = (
                await self.db.execute(
                    select(RuntimeRunEvent.seq)
                    .where(RuntimeRunEvent.run_id == run.id)
                    .order_by(RuntimeRunEvent.seq.desc())
                    .limit(1)
                )
            ).scalar_one_or_none() or 0
            payload = {
                "runtime_workspace_id": int(run.runtime_workspace_id),
                "runtime_session_id": str(run.runtime_session_id or ""),
                "runtime_run_id": str(run.runtime_run_id),
            }
            error_event = RuntimeRunEvent(
                run_id=int(run.id),
                runtime_run_public_id=str(run.runtime_run_id),
                seq=int(latest_seq) + 1,
                event_name="chat.error",
                payload={
                    **payload,
                    "message": error_message,
                    "code": "event_persistence_failed",
                },
            )
            done_event = RuntimeRunEvent(
                run_id=int(run.id),
                runtime_run_public_id=str(run.runtime_run_id),
                seq=int(latest_seq) + 2,
                event_name="chat.done",
                payload={**payload, "status": "failed"},
            )
            now = datetime.now(timezone.utc)
            summary = dict(run.response_summary or {})
            summary["stream"] = {"status": "failed", "error_message": error_message}
            run.status = "failed"
            run.finished_at = now
            run.error_message = error_message
            run.response_summary = summary
            self.db.add(error_event)
            self.db.add(done_event)
            self.db.add(
                RuntimeAuditLog(
                    org_id=org_id,
                    actor_user_id=None,
                    runtime_workspace_id=int(run.runtime_workspace_id),
                    action="runtime_run.persistence_recovered",
                    target_type="runtime_run",
                    target_id=str(runtime_run_id),
                    detail={"status": "failed", "error_code": "event_persistence_failed"},
                )
            )
            await self.db.commit()
            await self._invalidate_run_route(
                org_id=org_id,
                runtime_workspace_id=int(run.runtime_workspace_id),
                runtime_run_id=str(run.runtime_run_id),
                event_type="run_finished",
            )
            return [error_event, done_event]
        except Exception:
            await self.db.rollback()
            raise

    async def list_runtime_run_events(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        after_seq: int = 0,
    ) -> list[RuntimeRunEvent]:
        result = await self.db.execute(
            select(RuntimeRunEvent)
            .join(RuntimeRun, RuntimeRun.id == RuntimeRunEvent.run_id)
            .where(
                RuntimeRun.org_id == org_id,
                RuntimeRun.user_id == user_id,
                RuntimeRun.runtime_run_id == runtime_run_id,
                RuntimeRunEvent.seq > after_seq,
            )
            .order_by(RuntimeRunEvent.seq.asc())
        )
        return list(result.scalars().all())

    async def settle_self_evolution_review(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
    ) -> None:
        run = (
            await self.db.execute(
                select(RuntimeRun)
                .where(
                    RuntimeRun.org_id == org_id,
                    RuntimeRun.user_id == user_id,
                    RuntimeRun.runtime_run_id == runtime_run_id,
                    RuntimeRun.status == "completed",
                    RuntimeRun.self_evolution_status == "running",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            return
        now = datetime.now(timezone.utc)
        last_activity = _as_utc(run.self_evolution_last_activity_at)
        deadline = _as_utc(run.self_evolution_deadline_at)
        # A poll is only a safety-net: do not close an actually active review.
        # The review receives a short lease in the model proxy; once that lease
        # has been idle (or its hard deadline is reached), publish its terminal
        # state so a page refresh cannot leave a permanent spinner behind.
        if last_activity is None:
            await self.db.rollback()
            return
        idle_seconds = (now - last_activity).total_seconds()
        reached_hard_deadline = deadline is not None and now >= deadline
        if not reached_hard_deadline and idle_seconds <= settings.POST_TURN_REVIEW_IDLE_TIMEOUT_SECONDS:
            await self.db.rollback()
            return
        run.self_evolution_status = "completed"
        run.self_evolution_last_activity_at = now
        latest_seq = (
            await self.db.execute(
                select(RuntimeRunEvent.seq)
                .where(RuntimeRunEvent.run_id == run.id)
                .order_by(RuntimeRunEvent.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none() or 0
        self.db.add(
            RuntimeRunEvent(
                run_id=int(run.id),
                runtime_run_public_id=str(run.runtime_run_id),
                seq=int(latest_seq) + 1,
                event_name="chat.self_evolution.event",
                payload={"status": "completed", "activity": "本次执行复盘已完成"},
            )
        )
        await self.db.commit()

    async def get_runtime_endpoint(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> RuntimeEndpoint:
        binding = (
            await self.db.execute(
                select(HermesRuntimeBinding)
                .join(
                    CloudHermesProfile,
                    CloudHermesProfile.id == HermesRuntimeBinding.runtime_workspace_id,
                )
                .where(
                    HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id,
                    HermesRuntimeBinding.org_id == org_id,
                    HermesRuntimeBinding.user_id == user_id,
                    HermesRuntimeBinding.status == "ready",
                    CloudHermesProfile.status == "active",
                )
            )
        ).scalar_one_or_none()
        if binding is None:
            raise RuntimeChatPreparationError("runtime is not ready")
        return self._endpoint_from_binding(binding)

    async def get_runtime_run_control(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
    ) -> RuntimeRunControl:
        row = await self._runtime_run_control_row(
            org_id=org_id, user_id=user_id, runtime_run_id=runtime_run_id
        )
        if row is None:
            raise RuntimeChatPreparationError("runtime run not found")
        run, binding = row
        return RuntimeRunControl(
            runtime_workspace_id=int(run.runtime_workspace_id),
            runtime_session_id=str(run.runtime_session_id) if run.runtime_session_id else None,
            runtime_run_id=str(run.runtime_run_id),
            runtime_provider_run_id=self._runtime_provider_run_id(run),
            model_entry_id=int(run.model_entry_id) if run.model_entry_id else None,
            status=str(run.status),
            endpoint=self._endpoint_from_binding(binding),
        )

    async def get_runtime_run_replay_control(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
    ) -> RuntimeRunReplayControl:
        run = (
            await self.db.execute(
                select(RuntimeRun).join(
                    CloudHermesProfile,
                    CloudHermesProfile.id == RuntimeRun.runtime_workspace_id,
                ).where(
                    RuntimeRun.org_id == org_id,
                    RuntimeRun.user_id == user_id,
                    RuntimeRun.runtime_run_id == runtime_run_id,
                    CloudHermesProfile.status == "active",
                )
            )
        ).scalar_one_or_none()
        if run is None:
            raise RuntimeChatPreparationError("runtime run not found")
        return RuntimeRunReplayControl(
            runtime_workspace_id=int(run.runtime_workspace_id),
            runtime_session_id=str(run.runtime_session_id) if run.runtime_session_id else None,
            runtime_run_id=str(run.runtime_run_id),
            status=str(run.status),
        )

    async def get_active_runtime_run(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeActiveRun | None:
        run = (
            await self.db.execute(
                select(RuntimeRun)
                .where(
                    RuntimeRun.org_id == org_id,
                    RuntimeRun.user_id == user_id,
                    RuntimeRun.runtime_workspace_id == runtime_workspace_id,
                    RuntimeRun.runtime_session_id == runtime_session_id,
                    RuntimeRun.status.in_(("accepted", "pending", "running")),
                )
                .order_by(RuntimeRun.started_at.desc(), RuntimeRun.id.desc())
            )
        ).scalars().first()
        if run is None:
            return None
        last_seq = (
            await self.db.execute(
                select(func.coalesce(func.max(RuntimeRunEvent.seq), 0)).where(RuntimeRunEvent.run_id == run.id)
            )
        ).scalar_one()
        return RuntimeActiveRun(
            runtime_workspace_id=int(run.runtime_workspace_id),
            runtime_session_id=str(run.runtime_session_id),
            runtime_run_id=str(run.runtime_run_id),
            status=str(run.status),
            last_seq=int(last_seq),
            history_message_count_before_run=self._history_message_count_before_run(run),
            source=await self._source_for_run(run),
        )

    async def get_runtime_run_source(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
    ) -> dict | None:
        run = (
            await self.db.execute(
                select(RuntimeRun).where(
                    RuntimeRun.org_id == org_id,
                    RuntimeRun.user_id == user_id,
                    RuntimeRun.runtime_run_id == runtime_run_id,
                )
            )
        ).scalar_one_or_none()
        if run is None:
            return None
        return await self._source_for_run(run)

    async def get_runtime_session_sources(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> dict[int, dict]:
        runs = (
            await self.db.execute(
                select(RuntimeRun).where(
                    RuntimeRun.org_id == org_id,
                    RuntimeRun.user_id == user_id,
                    RuntimeRun.runtime_workspace_id == runtime_workspace_id,
                    RuntimeRun.runtime_session_id == runtime_session_id,
                    RuntimeRun.source_type == "scheduled",
                )
            )
        ).scalars().all()
        result: dict[int, dict] = {}
        for run in runs:
            count = self._history_message_count_before_run(run)
            source = await self._source_for_run(run)
            if count is not None and source is not None:
                result[count] = source
        return result

    async def _source_for_run(self, run: RuntimeRun) -> dict | None:
        if str(run.source_type) == "expert_collaboration":
            payload = run.request_payload or {}
            source = payload.get("source")
            if not isinstance(source, dict):
                return None
            collaboration_id = str(source.get("collaboration_id") or "").strip()
            origin_runtime_run_id = str(
                source.get("origin_runtime_run_id") or ""
            ).strip()
            if (
                source.get("type") != "expert_collaboration"
                or not collaboration_id
                or not origin_runtime_run_id
            ):
                return None
            return {
                "type": "expert_collaboration",
                "collaboration_id": collaboration_id,
                "origin_runtime_run_id": origin_runtime_run_id,
            }
        if str(run.source_type) != "scheduled":
            return None
        payload = run.request_payload or {}
        source = payload.get("source")
        if not isinstance(source, dict):
            # Task 1 stores a flat compatibility payload as well.  Accept only
            # the complete scheduled shape; never infer from prompt text.
            source = {
                "type": payload.get("type"),
                "scheduled_task_id": payload.get("scheduled_task_id"),
                "scheduled_task_name": payload.get("scheduled_task_name"),
                "trigger_kind": payload.get("trigger_kind"),
                "task_available": payload.get("task_available"),
            }
        required = ("type", "scheduled_task_id", "scheduled_task_name", "trigger_kind")
        if source.get("type") != "scheduled" or any(source.get(key) is None for key in required):
            return None
        try:
            task_id = int(source["scheduled_task_id"])
        except (TypeError, ValueError):
            return None
        available = (
            await self.db.execute(
                select(ScheduledTask.id).where(
                    ScheduledTask.id == task_id,
                    ScheduledTask.org_id == run.org_id,
                    ScheduledTask.user_id == run.user_id,
                    ScheduledTask.runtime_workspace_id == run.runtime_workspace_id,
                    ScheduledTask.status != "deleted",
                )
            )
        ).scalar_one_or_none() is not None
        return {
            "type": "scheduled",
            "scheduled_task_id": task_id,
            "scheduled_task_name": str(source["scheduled_task_name"]),
            "trigger_kind": str(source["trigger_kind"]),
            "task_available": available,
        }

    async def record_runtime_run_history_checkpoint(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        history_message_count_before_run: int,
        source_snapshot: dict | None = None,
    ) -> None:
        run = (
            await self.db.execute(
                select(RuntimeRun).where(
                    RuntimeRun.org_id == org_id,
                    RuntimeRun.user_id == user_id,
                    RuntimeRun.runtime_run_id == runtime_run_id,
                    RuntimeRun.status.in_(("accepted", "pending", "queued", "running")),
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            raise RuntimeChatPreparationError("runtime run not found")
        payload = dict(run.request_payload or {})
        payload["history_message_count_before_run"] = history_message_count_before_run
        if source_snapshot:
            # Keep the snapshot flat for compatibility with existing request
            # payload consumers while also retaining one canonical source
            # object for the chat projection layer.
            existing_source = payload.get("source")
            source = dict(source_snapshot)
            if isinstance(existing_source, dict):
                for key in (
                    "im_channel_id",
                    "im_bot_id_digest",
                    "im_target_digest",
                ):
                    if existing_source.get(key) is not None:
                        source[key] = existing_source[key]
            payload.update(source)
            payload["source"] = source
        run.request_payload = payload
        await self.db.commit()

    async def get_runtime_session_history_control(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        include_deleting: bool = False,
    ) -> RuntimeSessionHistoryControl:
        session_status = await self.db.scalar(
            select(RuntimeSession.status).where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
            )
        )
        if session_status == "expert_deleted":
            raise RuntimeExpertSessionDeletedError(
                RuntimeExpertSessionDeletedError.code
            )
        result = await self.db.execute(
            select(
                RuntimeSession,
                CloudHermesProfile,
                RuntimeAgentProfile,
                HermesRuntimeBinding,
            )
            .join(
                CloudHermesProfile,
                CloudHermesProfile.id == RuntimeSession.runtime_workspace_id,
            )
            .join(
                RuntimeAgentProfile,
                RuntimeAgentProfile.id == RuntimeSession.runtime_agent_profile_id,
            )
            .join(
                HermesRuntimeBinding,
                HermesRuntimeBinding.runtime_workspace_id == RuntimeSession.runtime_workspace_id,
            )
            .where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
                RuntimeSession.status.in_(("active", "deleting")) if include_deleting else RuntimeSession.status == "active",
                CloudHermesProfile.status == "active",
                *executable_runtime_agent_profile_clauses(),
            )
        )
        row = result.first()
        if row is None:
            raise RuntimeChatPreparationError("runtime session not found")
        session, profile, agent_profile, binding = row
        if binding.status != "ready":
            raise RuntimeChatPreparationError("runtime is not ready")
        if not binding.endpoint_base_url or not binding.endpoint_host_header:
            raise RuntimeChatPreparationError("runtime endpoint is not ready")
        return RuntimeSessionHistoryControl(
            runtime_workspace_id=int(session.runtime_workspace_id),
            runtime_session_id=str(session.runtime_session_id),
            session_id=int(session.id),
            endpoint=self._endpoint_from_binding(binding),
            message_count=int(session.message_count_cache),
            config_generation=int(binding.config_generation),
            effective_model_entry_id=int(
                session.session_model_entry_id or profile.default_model_id or 0
            ),
            runtime_agent_profile_id=int(agent_profile.id),
            runtime_agent_profile_generation=int(
                agent_profile.applied_revision
            ),
            hermes_profile_name=str(agent_profile.hermes_profile_name),
        )

    async def bind_runtime_provider_run(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        runtime_provider_run_id: str,
    ) -> None:
        row = await self._runtime_run_control_row(
            org_id=org_id, user_id=user_id, runtime_run_id=runtime_run_id, for_update=True
        )
        if row is None:
            raise RuntimeChatPreparationError("runtime run not found")
        run, _binding = row
        summary = dict(run.response_summary or {})
        if summary.get("hermes_run_id") == runtime_provider_run_id:
            if run.runtime_provider_run_id != runtime_provider_run_id:
                run.runtime_provider_run_id = runtime_provider_run_id
                await self.db.commit()
            return
        summary["hermes_run_id"] = runtime_provider_run_id
        run.response_summary = summary
        run.runtime_provider_run_id = runtime_provider_run_id
        run.status = "running"
        await self.db.commit()

    async def mark_session_materialized(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        session = (
            await self.db.execute(
                select(RuntimeSession).where(
                    RuntimeSession.org_id == org_id,
                    RuntimeSession.user_id == user_id,
                    RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                    RuntimeSession.runtime_session_id == runtime_session_id,
                    RuntimeSession.status == "active",
                )
            )
        ).scalar_one_or_none()
        if session is None:
            raise RuntimeChatPreparationError("runtime session not found")
        now = datetime.now(timezone.utc)
        session.message_count_cache = max(1, int(session.message_count_cache or 0))
        session.hermes_materialized_at = session.hermes_materialized_at or now
        session.last_active_at = now
        await self.db.commit()

    async def mark_hermes_session_materialized(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        session = (
            await self.db.execute(
                select(RuntimeSession).where(
                    RuntimeSession.org_id == org_id,
                    RuntimeSession.user_id == user_id,
                    RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                    RuntimeSession.runtime_session_id == runtime_session_id,
                    RuntimeSession.status == "active",
                )
            )
        ).scalar_one_or_none()
        if session is None:
            raise RuntimeChatPreparationError("runtime session not found")
        if session.hermes_materialized_at is None:
            session.hermes_materialized_at = datetime.now(timezone.utc)
        await self.db.commit()

    async def mark_runtime_run_stopped(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        runtime_provider_run_id: str | None,
        provider_response: dict | None = None,
        error_message: str | None = None,
    ) -> CreatedRuntimeRun:
        row = await self._runtime_run_control_row(
            org_id=org_id, user_id=user_id, runtime_run_id=runtime_run_id, for_update=True
        )
        if row is None:
            raise RuntimeChatPreparationError("runtime run not found")
        run, _binding = row
        if run.status in {"completed", "failed", "canceled", "cancel_failed"}:
            return CreatedRuntimeRun(
                runtime_workspace_id=int(run.runtime_workspace_id),
                runtime_session_id=str(run.runtime_session_id or ""),
                runtime_run_id=str(run.runtime_run_id),
                model_entry_id=int(run.model_entry_id) if run.model_entry_id else None,
                status=str(run.status),
            )
        now = datetime.now(timezone.utc)
        summary = dict(run.response_summary or {})
        if runtime_provider_run_id:
            summary["hermes_run_id"] = runtime_provider_run_id
            run.runtime_provider_run_id = runtime_provider_run_id
        summary["stop"] = {
            "requested": True,
            "provider_response": provider_response or {},
            "error_message": error_message,
        }
        if error_message is None:
            run.stop_requested_at = now
            run.stopped_by_user_id = user_id
        run.response_summary = summary
        self.db.add(
            RuntimeAuditLog(
                org_id=org_id,
                actor_user_id=user_id,
                runtime_workspace_id=int(run.runtime_workspace_id),
                action=(
                    "runtime_run.stop_failed"
                    if error_message
                    else "runtime_run.stop_requested"
                ),
                target_type="runtime_run",
                target_id=str(runtime_run_id),
                detail={
                    "runtime_provider_run_id": runtime_provider_run_id,
                    "status": "cancel_failed" if error_message else "canceling",
                    "error_message": error_message,
                },
            )
        )
        await self.db.commit()
        return CreatedRuntimeRun(
            runtime_workspace_id=int(run.runtime_workspace_id),
            runtime_session_id=str(run.runtime_session_id or ""),
            runtime_run_id=str(run.runtime_run_id),
            model_entry_id=int(run.model_entry_id) if run.model_entry_id else None,
            status="cancel_failed" if error_message else "canceling",
        )

    async def mark_runtime_run_canceled(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        runtime_provider_run_id: str | None,
    ) -> CreatedRuntimeRun:
        row = await self._runtime_run_control_row(
            org_id=org_id, user_id=user_id, runtime_run_id=runtime_run_id, for_update=True
        )
        if row is None:
            raise RuntimeChatPreparationError("runtime run not found")
        run, _binding = row
        if run.status not in {"completed", "failed", "canceled", "cancel_failed"}:
            now = datetime.now(timezone.utc)
            summary = dict(run.response_summary or {})
            if runtime_provider_run_id:
                summary["hermes_run_id"] = runtime_provider_run_id
                run.runtime_provider_run_id = runtime_provider_run_id
            stop_summary = dict(summary.get("stop") or {})
            stop_summary["finalized_at"] = now.isoformat()
            summary["stop"] = stop_summary
            run.status = "canceled"
            run.finished_at = now
            run.error_message = None
            run.response_summary = summary
            self.db.add(
                RuntimeAuditLog(
                    org_id=org_id,
                    actor_user_id=user_id,
                    runtime_workspace_id=int(run.runtime_workspace_id),
                    action="runtime_run.canceled",
                    target_type="runtime_run",
                    target_id=str(runtime_run_id),
                    detail={"runtime_provider_run_id": runtime_provider_run_id},
                )
            )
            await self.db.commit()
            await self._invalidate_run_route(
                org_id=org_id,
                runtime_workspace_id=int(run.runtime_workspace_id),
                runtime_run_id=str(run.runtime_run_id),
                event_type="run_finished",
            )
        return CreatedRuntimeRun(
            runtime_workspace_id=int(run.runtime_workspace_id),
            runtime_session_id=str(run.runtime_session_id or ""),
            runtime_run_id=str(run.runtime_run_id),
            model_entry_id=int(run.model_entry_id) if run.model_entry_id else None,
            status=str(run.status),
        )

    async def mark_runtime_run_finished(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        runtime_provider_run_id: str | None,
        status: str,
        error_message: str | None = None,
    ) -> CreatedRuntimeRun:
        if status not in {"completed", "failed"}:
            raise ValueError("runtime run terminal status must be completed or failed")
        row = await self._runtime_run_control_row(
            org_id=org_id, user_id=user_id, runtime_run_id=runtime_run_id, for_update=True
        )
        if row is None:
            raise RuntimeChatPreparationError("runtime run not found")
        run, _binding = row
        if run.status in {"completed", "failed", "canceled", "cancel_failed"}:
            await self._invalidate_run_route(
                org_id=org_id,
                runtime_workspace_id=int(run.runtime_workspace_id),
                runtime_run_id=str(run.runtime_run_id),
                event_type="run_finished",
            )
            return CreatedRuntimeRun(
                runtime_workspace_id=int(run.runtime_workspace_id),
                runtime_session_id=str(run.runtime_session_id or ""),
                runtime_run_id=str(run.runtime_run_id),
                model_entry_id=int(run.model_entry_id) if run.model_entry_id else None,
                status=str(run.status),
            )
        summary = dict(run.response_summary or {})
        if runtime_provider_run_id:
            summary["hermes_run_id"] = runtime_provider_run_id
            run.runtime_provider_run_id = runtime_provider_run_id
        summary["stream"] = {"status": status, "error_message": error_message}
        now = datetime.now(timezone.utc)
        run.finished_at = now
        run.status = status
        run.error_message = error_message
        if status == "completed":
            run.self_evolution_status = "eligible"
            run.self_evolution_activation_deadline_at = now + timedelta(
                seconds=settings.POST_TURN_REVIEW_ACTIVATION_WINDOW_SECONDS
            )
            run.self_evolution_started_at = None
            run.self_evolution_last_activity_at = None
            run.self_evolution_deadline_at = now + timedelta(
                seconds=settings.POST_TURN_REVIEW_MAX_DURATION_SECONDS
            )
            run.self_evolution_error_message = None
        else:
            run.self_evolution_status = None
            run.self_evolution_activation_deadline_at = None
            run.self_evolution_started_at = None
            run.self_evolution_last_activity_at = None
            run.self_evolution_deadline_at = None
            run.self_evolution_error_message = None
        run.response_summary = summary
        self.db.add(
            RuntimeAuditLog(
                org_id=org_id,
                actor_user_id=user_id,
                runtime_workspace_id=int(run.runtime_workspace_id),
                action="runtime_run.finished",
                target_type="runtime_run",
                target_id=str(runtime_run_id),
                detail={
                    "runtime_provider_run_id": runtime_provider_run_id,
                    "status": status,
                    "error_message": error_message,
                },
            )
        )
        await self.db.commit()
        await self._invalidate_run_route(
            org_id=org_id,
            runtime_workspace_id=int(run.runtime_workspace_id),
            runtime_run_id=str(run.runtime_run_id),
            event_type="run_finished",
        )
        return CreatedRuntimeRun(
            runtime_workspace_id=int(run.runtime_workspace_id),
            runtime_session_id=str(run.runtime_session_id or ""),
            runtime_run_id=str(run.runtime_run_id),
            model_entry_id=int(run.model_entry_id) if run.model_entry_id else None,
            status=str(run.status),
        )

    async def _runtime_run_control_row(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_run_id: str,
        for_update: bool = False,
    ):
        statement = (
            select(RuntimeRun, HermesRuntimeBinding)
            .join(
                CloudHermesProfile,
                CloudHermesProfile.id == RuntimeRun.runtime_workspace_id,
            )
            .join(
                HermesRuntimeBinding,
                HermesRuntimeBinding.runtime_workspace_id == RuntimeRun.runtime_workspace_id,
            )
            .where(
                RuntimeRun.org_id == org_id,
                RuntimeRun.user_id == user_id,
                RuntimeRun.runtime_run_id == runtime_run_id,
                CloudHermesProfile.status == "active",
                HermesRuntimeBinding.status == "ready",
            )
        )
        if for_update:
            statement = statement.with_for_update(of=RuntimeRun)
        result = await self.db.execute(statement)
        row = result.first()
        if row is None:
            return None
        run, binding = row
        if not binding.endpoint_base_url or not binding.endpoint_host_header:
            raise RuntimeChatPreparationError("runtime endpoint is not ready")
        return run, binding

    async def _invalidate_run_route(
        self,
        *,
        org_id: int,
        runtime_workspace_id: int,
        runtime_run_id: str,
        event_type: str,
    ) -> None:
        if self.route_cache is None:
            return
        try:
            factory = ModelCacheInvalidationEvent.run_created if event_type == "run_created" else ModelCacheInvalidationEvent.run_finished
            await self.route_cache.invalidate(
                factory(
                    org_id=org_id,
                    runtime_workspace_id=runtime_workspace_id,
                    runtime_run_id=runtime_run_id,
                )
            )
        except RedisError:
            return

    @staticmethod
    def _runtime_provider_run_id(run: RuntimeRun) -> str | None:
        if run.runtime_provider_run_id:
            return str(run.runtime_provider_run_id)
        summary = run.response_summary or {}
        value = summary.get("hermes_run_id") or summary.get("runtime_provider_run_id")
        return str(value) if value else None

    @staticmethod
    def _history_message_count_before_run(run: RuntimeRun) -> int | None:
        value = (run.request_payload or {}).get("history_message_count_before_run")
        return value if isinstance(value, int) and value >= 0 else None

    async def _visible_model_entry_ids(self, *, org_id: int, user_id: int) -> set[int]:
        models = await self._visible_models(org_id=org_id, user_id=user_id)
        return {
            int(model.model_entry_id)
            for model in models
        }

    async def _visible_models(
        self,
        *,
        org_id: int,
        user_id: int,
    ) -> list[RuntimeModelRecord]:
        return await SqlAlchemyRuntimeModelStore(
            self.db
        ).list_visible_models(org_id=org_id, user_id=user_id)

    async def _is_visible_model_entry(
        self,
        *,
        org_id: int,
        user_id: int,
        model_entry_id: int,
    ) -> bool:
        visible_model_ids = await self._visible_model_entry_ids(
            org_id=org_id,
            user_id=user_id,
        )
        return int(model_entry_id) in visible_model_ids

    async def _visible_bound_asset_ids(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None = None,
        asset_type: str,
    ) -> set[int]:
        bound_kwargs = {
            "org_id": principal.org_id,
            "runtime_workspace_id": runtime_workspace_id,
            "asset_type": asset_type,
        }
        if runtime_agent_profile_id is not None:
            bound_kwargs["runtime_agent_profile_id"] = runtime_agent_profile_id
        bound_ids = await self._runtime_bound_asset_ids(**bound_kwargs)
        if not bound_ids:
            return set()
        visible = await self.asset_visibility_service.list_visible_assets(
            principal=principal,
            asset_type=asset_type,
            asset_ids=bound_ids,
        )
        return {item.asset_id for item in visible}

    async def _runtime_bound_asset_ids(
        self,
        *,
        org_id: int,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None = None,
        asset_type: str,
    ) -> set[int]:
        binding_map = {
            "expert_package": (InstalledExpertBinding, InstalledExpertBinding.expert_package_id),
            "skill": (ProfileSkillBinding, ProfileSkillBinding.skill_id),
            "mcp": (ProfileMcpBinding, ProfileMcpBinding.mcp_id),
            "knowledge_base": (
                ProfileKnowledgeBinding,
                ProfileKnowledgeBinding.knowledge_base_id,
            ),
        }
        model_and_column = binding_map.get(asset_type)
        if model_and_column is None:
            return set()
        model, column = model_and_column
        clauses = [
            model.org_id == org_id,
            model.runtime_workspace_id == runtime_workspace_id,
            model.status == "enabled",
        ]
        if runtime_agent_profile_id is not None:
            clauses.append(
                model.runtime_agent_profile_id == runtime_agent_profile_id
            )
        result = await self.db.execute(select(column).where(*clauses))
        return {int(row[0]) for row in result.all()}

    @staticmethod
    def _first_or_none(values: set[int]) -> int | None:
        return min(values) if values else None
