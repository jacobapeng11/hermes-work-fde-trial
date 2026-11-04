from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Literal, Protocol
from uuid import uuid4

from platform_supervisor.execution.capacity import CapacityBusy, CapacityGate, CapacityLease
from platform_supervisor.execution.artifacts import ArtifactWatcher
from platform_supervisor.execution.sessions import (
    PrepareIdentity,
    SessionCapacityBusy,
    SessionManager,
)
from platform_supervisor.contracts import RuntimeTurnAttachment
from platform_supervisor.hermes.events import PlatformStreamEvent
from platform_supervisor.hermes.skill_manage_observation import (
    ObservedSkillManageCompletion,
)
from platform_supervisor.storage import (
    DISPATCH_INTENT_DURABLE,
    MAX_REDIRECT_WIRE_BYTES,
    TERMINAL_STATES,
    DispatchIntent,
    IdempotencyConflict,
    RedirectAdmissionIntent,
    RunEnqueueIntent,
    SQLiteStore,
    SQLiteStoreError,
    StoredRedirect,
    StoredRedirectDispatchGroup,
    StoredRun,
    StoredRunEvent,
)


PER_RUN_REALTIME_LIMIT = 256 * 1024
GLOBAL_REALTIME_LIMIT = 128 * 1024 * 1024
SNAPSHOT_INTERVAL_SECONDS = 0.2
SNAPSHOT_BYTES = 8 * 1024
TERMINAL_FOLLOWUP_DRAIN_DELAY_SECONDS = 0.1
REALTIME_EVENT_TYPES = frozenset(
    {
        "chat.activity.delta",
        "chat.message.delta",
        "chat.reasoning.delta",
    }
)
DURABLE_EVENT_TYPES = frozenset(
    {
        "chat.run.started",
        "chat.message.started",
        "chat.message.completed",
        "chat.tool.event",
        "chat.clarify.requested",
        "chat.subagent.event",
        "chat.usage",
        "chat.error",
        "chat.run.completed",
        "chat.done",
        "runtime.run.canceled",
        "chat.artifact.created",
        "chat.artifact.updated",
        "chat.artifact.deleted",
    }
)


def _prefixed_jcs_digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _runtime_attachment_payloads(
    value: list[dict[str, object]] | tuple[dict[str, object], ...] | None,
) -> list[dict[str, object]]:
    if not value:
        return []
    return [
        RuntimeTurnAttachment.model_validate(item).model_dump(
            mode="json",
            exclude_none=True,
        )
        for item in value
    ]


def _source_detail_with_runtime_attachments(
    source_detail: dict[str, object] | None,
    attachments: list[dict[str, object]],
) -> dict[str, object] | None:
    if not attachments:
        return source_detail
    detail = dict(source_detail or {})
    detail["runtime_attachments"] = attachments
    detail.setdefault(
        "attachment_file_ids",
        [str(attachment["file_id"]) for attachment in attachments],
    )
    return detail

logger = logging.getLogger(__name__)


class SessionBusy(RuntimeError):
    pass


class RunNotFound(RuntimeError):
    pass


RunCancelStatus = Literal["accepted", "already_terminal", "failed"]


class ExecutionAdapter(Protocol):
    async def stream(
        self,
        *,
        identity: PrepareIdentity,
        run_id: str,
        prompt: str,
        trusted_context: dict[str, object] | None = None,
        prompt_submit_started_sink: (
            Callable[[], Awaitable[None] | None] | None
        ) = None,
        prompt_ack_sink: Callable[[], Awaitable[None]] | None = None,
        runtime_provider_run_id_sink: (
            Callable[[str], Awaitable[None]] | None
        ) = None,
    ) -> AsyncIterator[PlatformStreamEvent]: ...

    async def cancel(
        self,
        identity: PrepareIdentity,
        *,
        runtime_provider_run_id: str | None = None,
    ) -> None: ...


class ModelRouteController(Protocol):
    async def activate(self, identity: PrepareIdentity, run_id: str) -> None: ...

    async def revoke_run(self, run_id: str) -> None: ...

    async def finish_run(
        self,
        identity: PrepareIdentity,
        run_id: str,
        *,
        completed: bool,
    ) -> None: ...


class RedirectLifecycleController(Protocol):
    async def redirect_admitted(self, parent_run_id: str) -> None: ...

    async def reconcile_redirect(self, parent_run_id: str) -> bool: ...

    async def cancel_parent_pre_rpc(self, parent_run_id: str) -> bool: ...

    async def prepare_parent_stop(self, parent_run_id: str) -> bool: ...

    async def prepare_parent_terminal(self, parent_run_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class RunSubmission:
    run: StoredRun
    created: bool


InteractiveSubmission = RunSubmission | StoredRedirect


@dataclass(slots=True)
class _Subscriber:
    queue: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    queued_bytes: int = 0
    last_progress_at: float = field(default_factory=time.monotonic)
    closed: bool = False


@dataclass(slots=True)
class _RunContext:
    run: StoredRun
    identity: PrepareIdentity
    prompt: str
    audit_prompt: str
    capacity_lease: CapacityLease
    seq: int = 0
    answer: str = ""
    answer_bytes_since_snapshot: int = 0
    last_snapshot_at: float = field(default_factory=time.monotonic)
    replay: deque[tuple[int, bytes]] = field(default_factory=deque)
    replay_bytes: int = 0
    subscribers: set[int] = field(default_factory=set)
    subscriber_by_id: dict[int, _Subscriber] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None
    prompt_attempted: bool = False
    terminal: bool = False
    released: bool = False
    event_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    publish_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    artifact_watcher: ArtifactWatcher | None = None
    runtime_provider_run_id: str | None = None
    cancellation_requested_at: datetime | None = None
    clarify_request_id: str | None = None
    clarify_event_recorded: bool = False
    interrupt_delivered: bool = False
    cancel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    redirect_fence_group_id: str | None = None
    redirect_fence_releasing: bool = False
    redirect_fenced_events: deque[PlatformStreamEvent] = field(
        default_factory=deque
    )
    redirect_pending_terminal_state: str | None = None


class RunManager:
    def __init__(
        self,
        store: SQLiteStore,
        sessions: SessionManager,
        capacity: CapacityGate,
        adapter: ExecutionAdapter,
        *,
        model_router: ModelRouteController | None = None,
        redirect_lifecycle: RedirectLifecycleController | None = None,
        profile_identity_resolver: (
            Callable[[PrepareIdentity], Awaitable[PrepareIdentity | None]] | None
        ) = None,
        artifact_workspace_dir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.capacity = capacity
        self.adapter = adapter
        self.model_router = model_router
        self.redirect_lifecycle = redirect_lifecycle
        self.profile_identity_resolver = profile_identity_resolver
        self.artifact_workspace_dir = artifact_workspace_dir
        self.clock = clock
        self._contexts: dict[str, _RunContext] = {}
        self._retired_contexts: dict[str, _RunContext] = {}
        self._active_by_session: dict[str, str] = {}
        self._draining_sessions: set[str] = set()
        self._drain_tasks: set[asyncio.Task[None]] = set()
        self._run_available: dict[str, asyncio.Event] = {}
        self._idempotency_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._idempotency_lock_users: dict[tuple[str, str], int] = {}
        self._recovered_cancel_locks: dict[str, asyncio.Lock] = {}
        self._recovered_cancel_lock_users: dict[str, int] = {}
        self._guard = asyncio.Lock()
        self._closed = False
        self._global_replay_order: deque[tuple[str, int, int]] = deque()
        self._global_replay_keys: set[tuple[str, int]] = set()
        self._realtime_buffer_bytes = 0
        self._slow_subscriber_disconnects = 0
        self._queue_update_sink: (
            Callable[[str, dict[str, int]], Awaitable[None]] | None
        ) = None

    def set_redirect_lifecycle(
        self,
        lifecycle: RedirectLifecycleController | None,
    ) -> None:
        self.redirect_lifecycle = lifecycle

    def set_queue_update_sink(
        self,
        sink: Callable[[str, dict[str, int]], Awaitable[None]] | None,
    ) -> None:
        self._queue_update_sink = sink

    @property
    def active_count(self) -> int:
        return len(self._contexts)

    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._contexts))

    @property
    def realtime_buffer_bytes(self) -> int:
        return self._realtime_buffer_bytes

    def realtime_snapshot(self) -> dict[str, int]:
        return {
            "buffer_bytes": self._realtime_buffer_bytes,
            "buffer_limit_bytes": GLOBAL_REALTIME_LIMIT,
            "per_run_limit_bytes": PER_RUN_REALTIME_LIMIT,
            "slow_client_disconnects": (
                self._slow_subscriber_disconnects
            ),
        }

    async def restore(self) -> None:
        unfinished = await self.store.load_unfinished_runs()
        now = datetime.now(timezone.utc)
        recovering: list[StoredRun] = []
        queued_sessions: set[str] = set()
        for run in unfinished:
            if run.state == "QUEUED":
                queued_sessions.add(run.session_id)
                continue
            if run.state == "ADMITTING":
                restored = await self.store.requeue_admitting(run.run_id)
                queued_sessions.add(restored.session_id)
                continue
            if run.state in {
                DISPATCH_INTENT_DURABLE,
                "ACKNOWLEDGED",
                "RUNNING",
                "RECOVERING",
            }:
                recovered = await self.store.transition_run(
                    run.run_id,
                    "RECOVERING",
                )
                recovering.append(recovered)
                self._active_by_session[recovered.session_id] = recovered.run_id
                continue
            terminal_state = (
                "FAILED_SUBMISSION_UNCERTAIN"
                if run.state == DISPATCH_INTENT_DURABLE
                else "RUN_INTERRUPTED_BY_RUNTIME_RESTART"
            )
            snapshot = await self.store.load_run_snapshot(run.run_id)
            recovered = await self.store.transition_run(
                run.run_id,
                terminal_state,
                terminal_at=now,
            )
            # A Runtime restart can happen after the accepted/event
            # projections were durably queued but before their terminal
            # projection.  Reconcile the platform record as part of startup
            # recovery; otherwise the backend permanently reports this Run as
            # active although this manager has already released it.
            await self.store.enqueue_projection(
                projection_id=f"{recovered.run_id}:terminal",
                projection_type="run.terminal",
                payload={
                    "run_id": recovered.run_id,
                    "session_id": recovered.session_id,
                    "state": terminal_state,
                    "answer": snapshot.answer if snapshot is not None else "",
                    "terminal_seq": (
                        snapshot.answer_through_seq
                        if snapshot is not None
                        else 0
                    ),
                    "accepted_at": recovered.accepted_at.isoformat(),
                    "terminal_at": now.isoformat(),
                    "client_message_id": recovered.client_message_id,
                    "config_revision": recovered.config_revision,
                    "source": recovered.source,
                    "user_id": recovered.user_id,
                    "org_id": recovered.org_id,
                    "profile_id": recovered.profile_id,
                    "model_id": recovered.model_id,
                    "prompt": "",
                },
                created_at=now,
            )
        await self.capacity.restore(len(recovering))
        for session_id in sorted(queued_sessions):
            await self._drain_session(session_id)

    async def submit(
        self,
        *,
        identity: PrepareIdentity,
        client_message_id: str,
        payload_digest: str,
        prompt: str,
        audit_prompt: str | None = None,
        source: str,
        runtime_attachments: (
            list[dict[str, object]] | tuple[dict[str, object], ...]
        ) = (),
        run_id: str | None = None,
        source_detail: dict[str, object] | None = None,
        scheduled_task_id: int | None = None,
        scheduled_occurrence_key: str | None = None,
        delivery_id: str | None = None,
    ) -> RunSubmission:
        if self._closed:
            raise RuntimeError("Run manager is closed")
        if self.capacity.draining:
            raise CapacityBusy("RUNTIME_DRAINING")
        runtime_attachment_payloads = _runtime_attachment_payloads(
            runtime_attachments
        )
        if await self.store.is_revoked(
            user_id=identity.user_id,
            model_id=identity.model_id,
            application_id=identity.runtime_id,
        ):
            raise PermissionError("RUNTIME_ACCESS_REVOKED")
        key = (identity.session_id, client_message_id)
        async with self._guard:
            lock = self._idempotency_locks.setdefault(key, asyncio.Lock())
            self._idempotency_lock_users[key] = (
                self._idempotency_lock_users.get(key, 0) + 1
            )
        try:
            async with lock:
                existing = await self.store.get_run_by_message(
                    session_id=identity.session_id,
                    client_message_id=client_message_id,
                )
                if existing is not None:
                    if existing.payload_digest != payload_digest:
                        raise IdempotencyConflict(
                            "client_message_id is already bound to a different payload"
                        )
                    if delivery_id is not None:
                        await self.store.bind_channel_fast_lane_v2_run(
                            delivery_id=delivery_id,
                            run_id=existing.run_id,
                            payload_digest=payload_digest,
                            bound_at=datetime.now(timezone.utc),
                        )
                    return RunSubmission(run=existing, created=False)

                queue_admission = await self.capacity.acquire_queue_admission()
                try:
                    reserved_run_id = run_id or f"run_{uuid4().hex}"
                    enqueued = await self.store.enqueue_run(
                        RunEnqueueIntent(
                            run_id=reserved_run_id,
                            session_id=identity.session_id,
                            client_message_id=client_message_id,
                            payload_digest=payload_digest,
                            config_revision=identity.config_revision,
                            source=source,
                            enqueued_at=datetime.now(timezone.utc),
                            prompt=prompt,
                            audit_prompt=(
                                audit_prompt
                                if audit_prompt is not None
                                else prompt
                            ),
                            identity=asdict(identity),
                            source_detail=_source_detail_with_runtime_attachments(
                                source_detail,
                                runtime_attachment_payloads,
                            ),
                            scheduled_task_id=scheduled_task_id,
                            scheduled_occurrence_key=(
                                scheduled_occurrence_key
                            ),
                            delivery_id=delivery_id,
                        )
                    )
                finally:
                    await queue_admission.release()
                async with self._guard:
                    self._run_available.setdefault(
                        enqueued.run_id,
                        asyncio.Event(),
                    )
                await self._drain_session(identity.session_id)
                await self._notify_queue_updated(
                    identity.session_id,
                    only_if_queued=True,
                )
                current = await self.store.get_run(enqueued.run_id)
                assert current is not None
                return RunSubmission(run=current, created=enqueued.created)
        finally:
            async with self._guard:
                remaining = self._idempotency_lock_users[key] - 1
                if remaining == 0:
                    self._idempotency_lock_users.pop(key, None)
                    self._idempotency_locks.pop(key, None)
                else:
                    self._idempotency_lock_users[key] = remaining

    async def submit_interactive(
        self,
        *,
        identity: PrepareIdentity,
        client_message_id: str,
        payload_digest: str,
        prompt: str,
        audit_prompt: str | None = None,
        attachment_file_ids: tuple[int, ...] | list[int] = (),
        runtime_attachments: (
            list[dict[str, object]] | tuple[dict[str, object], ...]
        ) = (),
        redirect_eligible: bool = True,
        expected_parent_run_id: str | None = None,
        interrupt_parent: bool = False,
        admission_generation: int,
        interaction_origin: str = "user_web",
        source: str = "web",
        source_detail: dict[str, object] | None = None,
        run_id: str | None = None,
        delivery_id: str | None = None,
    ) -> InteractiveSubmission:
        """Durably admit an interactive turn as replacement or redirect Run.

        The first disposition wins for ``session_id + client_message_id``.
        Attachments, oversized prompts, recovering/canceling parents, and an
        idle Session stay on the ordinary FIFO path.  A redirect admission
        only records intent; the model-boundary dispatcher owns the later
        ``session.redirect`` RPC and canonical-history proof.  Browser and
        Channel Fast Lane interruption requests instead persist their FIFO
        replacement first, then cancel the active parent so Session drain can
        execute the accepted text exactly once.
        """

        if admission_generation != identity.admission_generation:
            raise PermissionError("RUNTIME_ADMISSION_GENERATION_MISMATCH")
        runtime_attachment_payloads = _runtime_attachment_payloads(
            runtime_attachments
        )
        if attachment_file_ids or runtime_attachment_payloads:
            source_detail = dict(source_detail or {})
            if attachment_file_ids:
                source_detail["attachment_file_ids"] = list(attachment_file_ids)
            if runtime_attachment_payloads:
                source_detail["runtime_attachments"] = runtime_attachment_payloads
        if interrupt_parent:
            async with self._guard:
                parent_run_id = self._active_by_session.get(identity.session_id)
            if parent_run_id is None and expected_parent_run_id is not None:
                expected_parent = await self.store.get_run(
                    expected_parent_run_id
                )
                if (
                    expected_parent is None
                    or expected_parent.session_id != identity.session_id
                ):
                    raise SessionBusy(
                        "replacement parent Run does not belong to this Session"
                    )
                if expected_parent.state not in TERMINAL_STATES:
                    parent_run_id = expected_parent.run_id
            if parent_run_id is None:
                return await self.submit(
                    identity=identity,
                    client_message_id=client_message_id,
                    payload_digest=payload_digest,
                    prompt=prompt,
                    audit_prompt=audit_prompt,
                    runtime_attachments=runtime_attachment_payloads,
                    source=source,
                    source_detail=source_detail,
                    run_id=run_id,
                    delivery_id=delivery_id,
                )

            replacement_detail = dict(source_detail or {})
            replacement_detail["replacement_parent_run_id"] = parent_run_id
            submission = await self.submit(
                identity=identity,
                client_message_id=client_message_id,
                payload_digest=payload_digest,
                prompt=prompt,
                audit_prompt=audit_prompt,
                runtime_attachments=runtime_attachment_payloads,
                source=source,
                source_detail=replacement_detail,
                run_id=run_id,
                delivery_id=delivery_id,
            )
            if submission.run.run_id == parent_run_id:
                return submission
            cancel_status = await self.cancel(parent_run_id)
            if cancel_status == "failed":
                raise SessionBusy(
                    "replacement parent Run interrupt delivery failed"
                )
            return submission
        redirect_candidate = (
            redirect_eligible
            and bool(prompt.strip())
            and not runtime_attachment_payloads
            and len(prompt.encode("utf-8")) <= MAX_REDIRECT_WIRE_BYTES
        )
        if not redirect_candidate:
            return await self.submit(
                identity=identity,
                client_message_id=client_message_id,
                payload_digest=payload_digest,
                prompt=prompt,
                audit_prompt=audit_prompt,
                source=source,
                runtime_attachments=runtime_attachment_payloads,
                run_id=run_id,
                source_detail=source_detail,
                delivery_id=delivery_id,
            )

        request_digest = _prefixed_jcs_digest(
            {
                "session_id": identity.session_id,
                "interaction_origin": interaction_origin,
                "content": prompt,
            }
        )
        key = (identity.session_id, client_message_id)
        fallback_to_fifo = False
        terminal_parent = False
        active_continuation_run_id: str | None = None
        async with self._guard:
            lock = self._idempotency_locks.setdefault(key, asyncio.Lock())
            self._idempotency_lock_users[key] = (
                self._idempotency_lock_users.get(key, 0) + 1
            )
        try:
            async with lock:
                existing_redirect = await self.store.get_redirect_by_message(
                    session_id=identity.session_id,
                    client_message_id=client_message_id,
                )
                if existing_redirect is not None:
                    if existing_redirect.request_digest != request_digest:
                        raise IdempotencyConflict(
                            "client_message_id is already bound to a different redirect"
                        )
                    if delivery_id is not None:
                        await self.store.bind_channel_fast_lane_v2_redirect(
                            delivery_id=delivery_id,
                            redirect_id=existing_redirect.redirect_id,
                            payload_digest=payload_digest,
                            bound_at=datetime.now(timezone.utc),
                        )
                    return existing_redirect
                existing_run = await self.store.get_run_by_message(
                    session_id=identity.session_id,
                    client_message_id=client_message_id,
                )
                if existing_run is not None:
                    if existing_run.payload_digest != payload_digest:
                        raise IdempotencyConflict(
                            "client_message_id is already bound to a different payload"
                        )
                    if delivery_id is not None:
                        await self.store.bind_channel_fast_lane_v2_run(
                            delivery_id=delivery_id,
                            run_id=existing_run.run_id,
                            payload_digest=payload_digest,
                            bound_at=datetime.now(timezone.utc),
                        )
                    return RunSubmission(run=existing_run, created=False)

                async with self._guard:
                    parent_run_id = self._active_by_session.get(identity.session_id)
                if expected_parent_run_id is not None and (
                    parent_run_id != expected_parent_run_id
                ):
                    expected_parent = await self.store.get_run(
                        expected_parent_run_id
                    )
                    if (
                        expected_parent is None
                        or expected_parent.session_id != identity.session_id
                        or expected_parent.state not in TERMINAL_STATES
                    ):
                        raise SessionBusy(
                            "follow-up parent Run is no longer active"
                        )
                    active_continuation = (
                        await self.store.get_run(parent_run_id)
                        if parent_run_id is not None
                        else None
                    )
                    continuation_detail = (
                        active_continuation.source_detail
                        if active_continuation is not None
                        else None
                    )
                    if (
                        active_continuation is not None
                        and continuation_detail is not None
                        and continuation_detail.get("followup_parent_run_id")
                        == expected_parent_run_id
                    ):
                        active_continuation_run_id = active_continuation.run_id
                    else:
                        parent_run_id = expected_parent_run_id
                        terminal_parent = True
                if parent_run_id is None:
                    fallback_to_fifo = True
                elif not terminal_parent:
                    earlier = await self.store.list_input_admission_reservations(
                        identity.session_id
                    )
                    for item in earlier:
                        if item.kind == "redirect" and item.state != "promoted":
                            continue
                        if item.kind == "ordinary_run" and item.run_id:
                            earlier_run = await self.store.get_run(item.run_id)
                            if (
                                earlier_run is not None
                                and earlier_run.scheduled_task_id is not None
                            ):
                                continue
                        fallback_to_fifo = True
                        break
                    unresolved = (
                        await self.store.get_unresolved_redirect_group_for_parent(
                            parent_run_id
                        )
                    )
                    if (
                        unresolved is not None
                        and unresolved.state == "ISOLATING_UNCERTAIN"
                    ):
                        fallback_to_fifo = True
                if terminal_parent:
                    histories = await self.store.list_session_followup_history(
                        identity.session_id
                    )
                    continuation_ids = {
                        entry.promoted_run_id
                        for entry in histories
                        if entry.parent_run_id == parent_run_id
                        and entry.promoted_run_id is not None
                    }
                    for continuation_id in continuation_ids:
                        continuation = await self.store.get_run(continuation_id)
                        if continuation is not None and continuation.state != "QUEUED":
                            raise SessionBusy(
                                "follow-up continuation Run has already started"
                            )
                if not fallback_to_fifo and parent_run_id is not None:
                    reserved_run_id = run_id or f"run_{uuid4().hex}"
                    if active_continuation_run_id is not None:
                        source_detail = dict(source_detail or {})
                        source_detail.update(
                            {
                                "followup_original_parent_run_id": (
                                    expected_parent_run_id
                                ),
                                "followup_continuation_run_id": (
                                    active_continuation_run_id
                                ),
                            }
                        )
                    promotion_intent: dict[str, object] = {
                        "contract_version": "promotion-run-intent-v1",
                        "prepare_identity": asdict(identity),
                        "source": source,
                        "source_detail": source_detail,
                        "prompt": prompt,
                        "audit_prompt": (
                            audit_prompt if audit_prompt is not None else prompt
                        ),
                        "surface_payload_digest": payload_digest,
                    }
                    promotion_digest = _prefixed_jcs_digest(promotion_intent)
                    promotion_intent["payload_digest"] = promotion_digest
                    redirect_intent = RedirectAdmissionIntent(
                                redirect_id=f"redirect_{uuid4().hex}",
                                session_id=identity.session_id,
                                client_message_id=client_message_id,
                                request_digest=request_digest,
                                parent_run_id=parent_run_id,
                                interaction_origin=interaction_origin,
                                content=prompt,
                                initial_state="WAITING_AGENT_READY",
                                promotion_reserved_run_id=reserved_run_id,
                                promotion_run_intent=promotion_intent,
                                promotion_run_payload_digest=promotion_digest,
                                admission_generation=admission_generation,
                                created_at=datetime.now(timezone.utc),
                                delivery_id=delivery_id,
                                allow_terminal_parent=terminal_parent,
                            )
                    try:
                        admitted = await self.store.admit_redirect(
                            redirect_intent
                        )
                        if terminal_parent:
                            admitted = await self._promote_terminal_followup(
                                admitted
                            )
                        elif self.redirect_lifecycle is not None:
                            try:
                                await self.redirect_lifecycle.redirect_admitted(
                                    admitted.parent_run_id
                                )
                            except Exception:
                                # The pending disposition is already durable;
                                # message.start or a retry can safely re-arm.
                                logger.exception(
                                    "redirect_boundary_arm_failed",
                                    extra={
                                        "event": "redirect_boundary_arm_failed",
                                        "run_id": admitted.parent_run_id,
                                        "redirect_id": admitted.redirect_id,
                                    },
                                )
                        return admitted
                    except SQLiteStoreError as exc:
                        if str(exc) != "redirect parent Run is not active":
                            raise
                        raced_parent = await self.store.get_run(parent_run_id)
                        if (
                            expected_parent_run_id == parent_run_id
                            and raced_parent is not None
                            and raced_parent.state in TERMINAL_STATES
                        ):
                            admitted = await self.store.admit_redirect(
                                replace(
                                    redirect_intent,
                                    allow_terminal_parent=True,
                                )
                            )
                            return await self._promote_terminal_followup(
                                admitted
                            )
                        fallback_to_fifo = True
        finally:
            async with self._guard:
                remaining = self._idempotency_lock_users[key] - 1
                if remaining == 0:
                    self._idempotency_lock_users.pop(key, None)
                    self._idempotency_locks.pop(key, None)
                else:
                    self._idempotency_lock_users[key] = remaining

        if fallback_to_fifo:
            return await self.submit(
                identity=identity,
                client_message_id=client_message_id,
                payload_digest=payload_digest,
                prompt=prompt,
                audit_prompt=audit_prompt,
                source=source,
                run_id=run_id,
                source_detail=source_detail,
                delivery_id=delivery_id,
            )
        raise RuntimeError("interactive submission did not resolve a disposition")

    async def _promote_terminal_followup(
        self,
        admitted: StoredRedirect,
    ) -> StoredRedirect:
        """Preserve explicit Follow-up semantics after the parent closes."""

        if admitted.state == "WAITING_AGENT_READY":
            admitted = await self.store.mark_redirect_boundary_ready(
                admitted.redirect_id,
                updated_at=datetime.now(timezone.utc),
            )
        group = await self.store.get_unresolved_redirect_group_for_parent(
            admitted.parent_run_id
        )
        if group is None:
            group = await self.store.prepare_redirect_group(
                dispatch_group_id=f"redirect_group_{uuid4().hex}",
                parent_run_id=admitted.parent_run_id,
                created_at=datetime.now(timezone.utc),
                allow_terminal_parent=True,
            )
        if group is None:
            raise RuntimeError("terminal Follow-up has no promotable member")
        if group.state != "PREPARED":
            raise RuntimeError("terminal Follow-up group crossed an RPC boundary")
        await self.store.promote_redirect_group(
            group.dispatch_group_id,
            promoted_at=datetime.now(timezone.utc),
            include_all_parent_followups=True,
        )
        refreshed = await self.store.get_redirect_by_message(
            session_id=admitted.session_id,
            client_message_id=admitted.client_message_id,
        )
        if refreshed is None or refreshed.state != "PROMOTED_TO_RUN":
            raise RuntimeError("terminal Follow-up promotion did not converge")
        self._schedule_drain(
            admitted.session_id,
            delay_seconds=TERMINAL_FOLLOWUP_DRAIN_DELAY_SECONDS,
        )
        return refreshed

    async def _drain_session(self, session_id: str) -> None:
        async with self._guard:
            if (
                self._closed
                or session_id in self._active_by_session
                or session_id in self._draining_sessions
            ):
                return
            self._draining_sessions.add(session_id)
        capacity_lease: CapacityLease | None = None
        claimed: StoredRun | None = None
        route_activated = False
        session_started = False
        continue_after_failure = False
        admission_stage = "capacity_acquire"
        try:
            try:
                capacity_lease = await self.capacity.try_acquire()
            except CapacityBusy:
                return
            claimed = await self.store.claim_next_queued(
                session_id,
                accepted_at=datetime.now(timezone.utc),
            )
            if claimed is None:
                await capacity_lease.release()
                capacity_lease = None
                return
            identity = self._identity_from_run(claimed)
            async with self._guard:
                self._active_by_session[session_id] = claimed.run_id
            try:
                if self.profile_identity_resolver is not None:
                    admission_stage = "profile_asset_gate"
                    resolved_identity = await self.profile_identity_resolver(identity)
                    if resolved_identity is None:
                        await self._rollback_admission(
                            claimed,
                            identity,
                            capacity_lease,
                            route_activated=False,
                            session_started=False,
                            requeue=True,
                        )
                        capacity_lease = None
                        return
                    identity = resolved_identity
                admission_stage = "session_prepare"
                await self.sessions.prepare(identity)
                admission_stage = "session_mark_started"
                await self.sessions.mark_run_started(identity)
                session_started = True
                if claimed.source_detail is not None:
                    admission_stage = "history_checkpoint"
                    history_getter = getattr(
                        self.adapter,
                        "get_session_messages",
                        None,
                    )
                    history_count = 0
                    if callable(history_getter):
                        history = await history_getter(
                            session_id=identity.session_id,
                            profile_name=identity.profile_id,
                        )
                        raw_messages = (
                            history.get("messages", history.get("data", []))
                            if isinstance(history, dict)
                            else []
                        )
                        if isinstance(raw_messages, list):
                            history_count = len(raw_messages)
                    claimed = await self.store.record_run_history_checkpoint(
                        claimed.run_id,
                        history_message_count_before_run=history_count,
                    )
                if self.model_router is not None:
                    admission_stage = "model_route_activate"
                    await self.model_router.activate(identity, claimed.run_id)
                    route_activated = True
                accepted_at = claimed.accepted_at or datetime.now(timezone.utc)
                admission_stage = "dispatch_intent"
                stored = await self.store.persist_dispatch_intent(
                    DispatchIntent(
                        run_id=claimed.run_id,
                        session_id=claimed.session_id,
                        client_message_id=claimed.client_message_id,
                        payload_digest=claimed.payload_digest,
                        config_revision=claimed.config_revision,
                        source=claimed.source,
                        accepted_at=accepted_at,
                        user_id=claimed.user_id,
                        org_id=claimed.org_id,
                        profile_id=claimed.profile_id,
                        model_id=claimed.model_id,
                        prompt=claimed.audit_prompt,
                    )
                )
            except (CapacityBusy, SessionCapacityBusy):
                await self._rollback_admission(
                    claimed,
                    identity,
                    capacity_lease,
                    route_activated=route_activated,
                    session_started=session_started,
                    requeue=True,
                )
                capacity_lease = None
                continue_after_failure = True
                return
            except Exception as exc:
                logger.exception(
                    "runtime_run_admission_failed",
                    extra={
                        "event": "runtime_run_admission_failed",
                        "run_id": claimed.run_id,
                        "session_id": claimed.session_id,
                        "admission_stage": admission_stage,
                        "exception_type": type(exc).__name__,
                    },
                )
                await self._rollback_admission(
                    claimed,
                    identity,
                    capacity_lease,
                    route_activated=route_activated,
                    session_started=session_started,
                    requeue=False,
                    failure_stage=admission_stage,
                )
                capacity_lease = None
                continue_after_failure = True
                return
            context = _RunContext(
                run=stored,
                identity=identity,
                prompt=claimed.prompt,
                audit_prompt=claimed.audit_prompt,
                capacity_lease=capacity_lease,
                last_snapshot_at=self.clock(),
            )
            async with self._guard:
                self._contexts[stored.run_id] = context
                self._run_available.setdefault(
                    stored.run_id,
                    asyncio.Event(),
                ).set()
            context.task = asyncio.create_task(
                self._execute(context),
                name=f"runtime-run-{stored.run_id}",
            )
            capacity_lease = None
        finally:
            if capacity_lease is not None:
                await capacity_lease.release()
            async with self._guard:
                self._draining_sessions.discard(session_id)
            if continue_after_failure and not self._closed:
                await self._drain_session(session_id)

    async def wake_queued_sessions(self) -> None:
        if self._closed:
            return
        for session_id in await self.store.list_queued_session_ids():
            self._schedule_drain(session_id)

    async def _rollback_admission(
        self,
        run: StoredRun,
        identity: PrepareIdentity,
        capacity_lease: CapacityLease,
        *,
        route_activated: bool,
        session_started: bool,
        requeue: bool,
        failure_stage: str | None = None,
    ) -> None:
        cleanup: list[Awaitable[object]] = [capacity_lease.release()]
        if route_activated and self.model_router is not None:
            cleanup.append(self.model_router.revoke_run(run.run_id))
        if session_started:
            cleanup.append(self.sessions.mark_run_terminal(identity.session_id))
        await asyncio.gather(*cleanup, return_exceptions=True)
        if requeue:
            await self.store.requeue_admitting(run.run_id)
        else:
            terminal_at = datetime.now(timezone.utc)
            normalized_stage = str(failure_stage or "unknown").strip().lower()
            error_code = (
                "RUNTIME_ADMISSION_"
                + normalized_stage.upper().replace("-", "_")
                + "_FAILED"
            )
            error_message = (
                f"Run admission failed during {normalized_stage}"
            )
            await self.store.append_run_event(
                StoredRunEvent(
                    run_id=run.run_id,
                    seq=1,
                    event_type="chat.error",
                    payload={
                        "code": error_code,
                        "message": error_message,
                        "admission_stage": normalized_stage,
                    },
                    occurred_at=terminal_at,
                ),
                durable=True,
            )
            await self.store.append_run_event(
                StoredRunEvent(
                    run_id=run.run_id,
                    seq=2,
                    event_type="chat.done",
                    payload={
                        "status": "failed",
                        "code": error_code,
                        "admission_stage": normalized_stage,
                    },
                    occurred_at=terminal_at,
                ),
                durable=True,
            )
            failed = await self.store.transition_run(
                run.run_id,
                "FAILED",
                terminal_at=terminal_at,
            )
            await self.store.enqueue_projection(
                projection_id=f"{failed.run_id}:terminal",
                projection_type="run.terminal",
                payload={
                    "run_id": failed.run_id,
                    "session_id": failed.session_id,
                    "state": "FAILED",
                    "answer": "",
                    "terminal_seq": 2,
                    "error_code": error_code,
                    "error_message": error_message,
                    "admission_stage": normalized_stage,
                    "accepted_at": (
                        failed.accepted_at or terminal_at
                    ).isoformat(),
                    "terminal_at": terminal_at.isoformat(),
                    "client_message_id": failed.client_message_id,
                    "config_revision": failed.config_revision,
                    "source": failed.source,
                    "user_id": failed.user_id,
                    "org_id": failed.org_id,
                    "profile_id": failed.profile_id,
                    "model_id": failed.model_id,
                    "prompt": failed.audit_prompt,
                },
                created_at=terminal_at,
            )
        async with self._guard:
            if not requeue:
                self._run_available.setdefault(run.run_id, asyncio.Event()).set()
            if self._active_by_session.get(identity.session_id) == run.run_id:
                self._active_by_session.pop(identity.session_id, None)

    @staticmethod
    def _identity_from_run(run: StoredRun) -> PrepareIdentity:
        payload = dict(run.identity or {})
        allowed = PrepareIdentity.__dataclass_fields__
        values = {key: value for key, value in payload.items() if key in allowed}
        values.setdefault("runtime_id", "")
        values.setdefault("session_id", run.session_id)
        values.setdefault("profile_id", run.profile_id)
        values.setdefault("model_id", run.model_id)
        values.setdefault("config_revision", run.config_revision)
        values.setdefault("context_length", 128_000)
        values.setdefault("user_id", run.user_id)
        values.setdefault("org_id", run.org_id)
        return PrepareIdentity(**values)

    async def cancel(
        self,
        run_id: str,
        *,
        clarify_request_id: str | None = None,
    ) -> RunCancelStatus:
        async with self._guard:
            context = self._contexts.get(run_id)
        if context is None:
            return await self._cancel_without_context(run_id)
        async with context.cancel_lock:
            if context.terminal:
                return "already_terminal"
            requested_at = datetime.now(timezone.utc)
            if clarify_request_id is not None:
                context.clarify_request_id = clarify_request_id
            context.run = await self.store.request_run_cancellation(
                run_id,
                requested_at=requested_at,
            )
            context.cancellation_requested_at = (
                context.run.cancellation_requested_at or requested_at
            )
            if context.terminal:
                return "already_terminal"
            redirect_session_settled = False
            if self.redirect_lifecycle is not None:
                prepare_parent_stop = getattr(
                    self.redirect_lifecycle,
                    "prepare_parent_stop",
                    None,
                )
                try:
                    if callable(prepare_parent_stop):
                        redirect_session_settled = bool(
                            await prepare_parent_stop(run_id)
                        )
                    else:
                        await self.redirect_lifecycle.cancel_parent_pre_rpc(
                            run_id
                        )
                except Exception:
                    logger.exception(
                        "redirect_parent_stop_prepare_failed",
                        extra={
                            "event": "redirect_parent_stop_prepare_failed",
                            "run_id": run_id,
                        },
                    )
                    if callable(prepare_parent_stop):
                        return "failed"
            if redirect_session_settled:
                context.interrupt_delivered = True
            if not context.prompt_attempted:
                if context.task is not None:
                    context.task.cancel()
                return "accepted"
            if context.interrupt_delivered:
                return "accepted"
            if context.runtime_provider_run_id is None:
                return "accepted"
            try:
                await self.adapter.cancel(
                    context.identity,
                    runtime_provider_run_id=(
                        context.runtime_provider_run_id
                    ),
                )
            except Exception:
                return "failed"
            context.interrupt_delivered = True
            return "accepted"

    async def _cancel_without_context(self, run_id: str) -> RunCancelStatus:
        """Settle a boot-recovered Run that has no executable context."""

        async with self._guard:
            lock = self._recovered_cancel_locks.setdefault(
                run_id,
                asyncio.Lock(),
            )
            self._recovered_cancel_lock_users[run_id] = (
                self._recovered_cancel_lock_users.get(run_id, 0) + 1
            )
        try:
            async with lock:
                stored = await self.store.get_run(run_id)
                if stored is None:
                    raise RunNotFound(run_id)
                if stored.state != "RECOVERING":
                    return "already_terminal"

                now = datetime.now(timezone.utc)
                stored = await self.store.request_run_cancellation(
                    run_id,
                    requested_at=now,
                )
                if self.redirect_lifecycle is None:
                    await self.store.cancel_pre_rpc_redirects_for_parent(
                        run_id,
                        canceled_at=now,
                    )
                else:
                    prepare_parent_stop = getattr(
                        self.redirect_lifecycle,
                        "prepare_parent_stop",
                        None,
                    )
                    try:
                        if callable(prepare_parent_stop):
                            await prepare_parent_stop(run_id)
                        else:
                            await self.redirect_lifecycle.cancel_parent_pre_rpc(
                                run_id
                            )
                    except Exception:
                        logger.exception(
                            "recovered_run_cancel_redirect_prepare_failed",
                            extra={
                                "event": (
                                    "recovered_run_cancel_redirect_prepare_failed"
                                ),
                                "run_id": run_id,
                            },
                        )
                        # Restart cancellation is authoritative even when a
                        # stale redirect group cannot be promoted.  Continue
                        # the terminal transition so the Run cannot be
                        # resurrected by restore(); the conflict remains in
                        # the journal for later repair.

                snapshot = await self.store.load_run_snapshot(run_id)
                if snapshot is None:
                    raise RunNotFound(run_id)
                terminal_seq = max(
                    snapshot.answer_through_seq + 1,
                    await self.store.next_run_event_seq(run_id),
                )
                event_data = {"status": "canceled"}
                await self.store.append_run_event(
                    StoredRunEvent(
                        run_id=run_id,
                        seq=terminal_seq,
                        event_type="chat.done",
                        payload=event_data,
                        occurred_at=now,
                    ),
                    durable=True,
                )
                await self.store.enqueue_projection(
                    projection_id=f"{run_id}:event:{terminal_seq}",
                    projection_type="run.event",
                    payload={
                        "run_id": run_id,
                        "session_id": stored.session_id,
                        "seq": terminal_seq,
                        "event_type": "chat.done",
                        "event_data": event_data,
                        "occurred_at": now.isoformat(),
                        "config_revision": stored.config_revision,
                        "source": stored.source,
                        "user_id": stored.user_id,
                        "org_id": stored.org_id,
                        "profile_id": stored.profile_id,
                        "model_id": stored.model_id,
                    },
                    created_at=now,
                )
                canceled = await self.store.transition_run(
                    run_id,
                    "CANCELED",
                    terminal_at=now,
                )
                await self.store.enqueue_projection(
                    projection_id=f"{run_id}:terminal",
                    projection_type="run.terminal",
                    payload={
                        "run_id": run_id,
                        "session_id": canceled.session_id,
                        "state": "CANCELED",
                        "answer": snapshot.answer,
                        "terminal_seq": terminal_seq,
                        "accepted_at": (
                            canceled.accepted_at or canceled.enqueued_at
                        ).isoformat(),
                        "terminal_at": now.isoformat(),
                        "client_message_id": canceled.client_message_id,
                        "config_revision": canceled.config_revision,
                        "source": canceled.source,
                        "user_id": canceled.user_id,
                        "org_id": canceled.org_id,
                        "profile_id": canceled.profile_id,
                        "model_id": canceled.model_id,
                        "prompt": canceled.audit_prompt,
                    },
                    created_at=now,
                )
                if self.model_router is not None:
                    await self.model_router.revoke_run(run_id)
                async with self._guard:
                    if self._active_by_session.get(stored.session_id) == run_id:
                        self._active_by_session.pop(stored.session_id, None)
                    self._run_available.setdefault(run_id, asyncio.Event()).set()
                await self.capacity.release_recovered()
                await self._drain_session(stored.session_id)
                await self._notify_queue_updated(stored.session_id)
                return "accepted"
        finally:
            async with self._guard:
                remaining = self._recovered_cancel_lock_users[run_id] - 1
                if remaining == 0:
                    self._recovered_cancel_lock_users.pop(run_id, None)
                    self._recovered_cancel_locks.pop(run_id, None)
                else:
                    self._recovered_cancel_lock_users[run_id] = remaining

    async def cancel_scheduled_candidate(
        self,
        *,
        session_id: str,
        scheduled_task_id: int,
    ) -> str:
        result = await self.store.cancel_queued_scheduled_task(
            session_id=session_id,
            scheduled_task_id=scheduled_task_id,
        )
        await self._notify_queue_updated(session_id)
        return result

    async def cancel_matching(self, *, scope: str, subject_id: str) -> int:
        async with self._guard:
            contexts = tuple(self._contexts.values())
        matching = [
            context
            for context in contexts
            if (
                (scope == "user" and str(context.identity.user_id) == subject_id)
                or (
                    scope == "model"
                    and context.identity.model_id == subject_id
                )
                or (
                    scope == "application"
                    and context.identity.runtime_id == subject_id
                )
            )
            and not context.terminal
        ]
        results = await asyncio.gather(
            *(self.cancel(context.run.run_id) for context in matching),
            return_exceptions=True,
        )
        return sum(result == "accepted" for result in results)

    async def cancel_active_runs(self) -> list[dict[str, str]]:
        """Interrupt all non-terminal Runs before an authorized restart.

        The identities are an ephemeral receipt for the post-restart
        Session/history probe; no restart-specific state is persisted.
        """
        identities = await self.store.interrupt_unfinished_runs_for_restart(
            interrupted_at=datetime.now(timezone.utc),
        )
        # Stop in-memory work after the durable barrier.  The database
        # transition is already terminal, so no Run can be resurrected by
        # restore() if the process is stopped immediately afterwards.
        async with self._guard:
            contexts = [
                context
                for context in self._contexts.values()
                if context.run.run_id in {item["run_id"] for item in identities}
            ]
            for context in contexts:
                if context.task is not None:
                    context.task.cancel()
                self._active_by_session.pop(context.run.session_id, None)
        for session_id in {item["session_id"] for item in identities}:
            await self._notify_queue_updated(session_id)
        return identities

    async def subscribe(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
    ) -> AsyncIterator[bytes]:
        await self.wait_until_admitted(run_id)
        async with self._guard:
            context = self._contexts.get(run_id) or self._retired_contexts.get(
                run_id
            )
            if context is None:
                raise RunNotFound(run_id)
            subscriber = _Subscriber(last_progress_at=self.clock())
            subscriber_id = id(subscriber)
            context.subscribers.add(subscriber_id)
            context.subscriber_by_id[subscriber_id] = subscriber
            replay = [chunk for seq, chunk in context.replay if seq > after_seq]
            for chunk in replay:
                subscriber.queue.put_nowait(chunk)
                subscriber.queued_bytes += len(chunk)
        try:
            while True:
                chunk = await subscriber.queue.get()
                if chunk is None:
                    return
                subscriber.queued_bytes = max(0, subscriber.queued_bytes - len(chunk))
                subscriber.last_progress_at = self.clock()
                yield chunk
        finally:
            async with self._guard:
                context = self._contexts.get(run_id) or self._retired_contexts.get(
                    run_id
                )
                if context is not None:
                    context.subscribers.discard(subscriber_id)
                    context.subscriber_by_id.pop(subscriber_id, None)
                    if context.terminal and not context.subscribers:
                        self._retired_contexts.pop(run_id, None)
                        self._drop_replay(context)

    async def wait_until_admitted(self, run_id: str) -> StoredRun:
        while True:
            stored = await self.store.get_run(run_id)
            if stored is None:
                raise RunNotFound(run_id)
            if stored.state not in {"QUEUED", "ADMITTING"}:
                return stored
            async with self._guard:
                event = self._run_available.setdefault(
                    run_id,
                    asyncio.Event(),
                )
            await event.wait()

    async def begin_redirect_event_fence(
        self,
        *,
        parent_run_id: str,
        dispatch_group_id: str,
        recorder: Callable[
            [int, str], Awaitable[StoredRedirectDispatchGroup]
        ],
    ) -> None:
        """Commit a redirect dispatch anchor before fencing live events."""

        async with self._guard:
            context = self._contexts.get(parent_run_id)
        if context is None or context.terminal:
            raise RunNotFound(parent_run_id)
        async with context.event_lock:
            if (
                context.redirect_fence_group_id is not None
                and context.redirect_fence_group_id != dispatch_group_id
            ):
                raise RuntimeError("parent Run already owns a redirect event fence")
            if await self.store.get_run(parent_run_id) is None:
                raise RunNotFound(parent_run_id)
            token_digest = _prefixed_jcs_digest(
                {
                    "contract_version": "redirect-token-snapshot-v1",
                    "answer": context.answer,
                    "answer_through_seq": context.seq,
                }
            )
            await recorder(context.seq, token_digest)
            context.redirect_fence_group_id = dispatch_group_id

    async def release_redirect_event_fence(
        self,
        *,
        parent_run_id: str,
        dispatch_group_id: str,
    ) -> None:
        """Release buffered frames after the disposition transaction commits."""

        async with self._guard:
            context = self._contexts.get(parent_run_id)
        if context is None or context.terminal:
            await self.store.complete_redirect_event_fence_release(
                dispatch_group_id
            )
            return
        async with context.event_lock:
            if context.redirect_fence_group_id not in {
                None,
                dispatch_group_id,
            }:
                raise RuntimeError("redirect event fence identity conflicts")
            context.redirect_fence_group_id = None
            context.redirect_fence_releasing = True

        await self.publish_durable_followup_events(parent_run_id)

        while True:
            async with context.event_lock:
                if not context.redirect_fenced_events:
                    context.redirect_fence_releasing = False
                    terminal_state = context.redirect_pending_terminal_state
                    context.redirect_pending_terminal_state = None
                    break
                buffered = list(context.redirect_fenced_events)
                context.redirect_fenced_events.clear()
            for event in buffered:
                await self._record_event(
                    context,
                    event,
                    bypass_redirect_fence=True,
                )

        await self.store.complete_redirect_event_fence_release(
            dispatch_group_id
        )
        if terminal_state is not None and not context.terminal:
            await self._finish(context, terminal_state)

    async def publish_durable_followup_events(
        self,
        parent_run_id: str,
    ) -> None:
        """Replay ledger-owned Follow-up dispositions to live subscribers.

        Redirect transactions allocate and persist their own Run event
        sequence.  Publishing those existing rows must not append them again;
        it only advances the in-memory stream through the durable sequence.
        """

        async with self._guard:
            context = self._contexts.get(parent_run_id)
        if context is None or context.terminal:
            return
        async with context.event_lock:
            if context.redirect_fence_group_id is not None:
                return
            while True:
                snapshot = await self.store.load_run_snapshot(
                    parent_run_id,
                    after_seq=context.seq,
                    max_events=10_000,
                )
                if snapshot is None or not snapshot.events:
                    return
                for event in snapshot.events:
                    if event.seq <= context.seq:
                        continue
                    context.seq = event.seq
                    envelope = {
                        "schema_version": 1,
                        "type": "run.event",
                        "run_id": parent_run_id,
                        "seq": event.seq,
                        "event_type": event.event_type,
                        "emitted_at": event.occurred_at.isoformat(),
                        "data": event.payload,
                    }
                    chunk = json.dumps(
                        envelope,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self._append_replay(context, chunk)
                    await self._publish(
                        context,
                        chunk,
                        event_type=event.event_type,
                    )
                if len(snapshot.events) < 10_000:
                    return

    async def close(self) -> None:
        self._closed = True
        async with self._guard:
            contexts = list(self._contexts.values())
            drain_tasks = list(self._drain_tasks)
        for task in drain_tasks:
            task.cancel()
        for context in contexts:
            if context.task is not None:
                context.task.cancel()
        await asyncio.gather(
            *drain_tasks,
            *(context.task for context in contexts if context.task is not None),
            return_exceptions=True,
        )
        async with self._guard:
            for context in tuple(self._retired_contexts.values()):
                for subscriber in tuple(context.subscriber_by_id.values()):
                    self._close_subscriber(subscriber)
                self._drop_replay(context)
            self._retired_contexts.clear()
            for event in self._run_available.values():
                event.set()
            self._run_available.clear()

    async def _execute(self, context: _RunContext) -> None:
        async def acknowledged() -> None:
            context.run = await self.store.transition_run(
                context.run.run_id,
                "ACKNOWLEDGED",
                acknowledged_at=datetime.now(timezone.utc),
            )

        async def prompt_submit_started() -> None:
            async with context.cancel_lock:
                if context.cancellation_requested_at is not None:
                    raise asyncio.CancelledError
                context.prompt_attempted = True

        async def runtime_provider_ready(
            runtime_provider_run_id: str,
        ) -> None:
            provider_id = str(runtime_provider_run_id or "").strip()
            if not provider_id:
                return
            async with context.cancel_lock:
                context.run = await self.store.set_runtime_provider_run_id(
                    context.run.run_id,
                    provider_id,
                )
                context.runtime_provider_run_id = provider_id
                if (
                    context.cancellation_requested_at is not None
                    and context.prompt_attempted
                    and not context.interrupt_delivered
                ):
                    try:
                        await self.adapter.cancel(
                            context.identity,
                            runtime_provider_run_id=provider_id,
                        )
                    except Exception:
                        logger.warning(
                            "runtime_cancel_delivery_failed_after_mapping",
                            extra={
                                "event": (
                                    "runtime_cancel_delivery_failed_after_mapping"
                                ),
                                "run_id": context.run.run_id,
                            },
                        )
                    else:
                        context.interrupt_delivered = True

        try:
            await self._start_artifact_watcher(context)
            trusted_context = None
            if (
                context.run.source_detail is not None
                and context.run.source_detail.get("followup_recovery") is True
            ):
                trusted_context = {
                    "followup_delivery": "uncertain",
                    "instruction": (
                        "Check the actual conversation and tool state first. "
                        "Do not repeat work or side effects already completed; "
                        "continue only work that is still missing."
                    ),
                }
            raw_runtime_attachments = (
                context.run.source_detail.get("runtime_attachments")
                if context.run.source_detail is not None
                else None
            )
            runtime_attachments = _runtime_attachment_payloads(
                raw_runtime_attachments
                if isinstance(raw_runtime_attachments, list)
                else ()
            )
            adapter_stream = self.adapter.stream(
                identity=context.identity,
                run_id=context.run.run_id,
                prompt=context.prompt,
                attachments=runtime_attachments,
                trusted_context=trusted_context,
                prompt_submit_started_sink=prompt_submit_started,
                prompt_ack_sink=acknowledged,
                runtime_provider_run_id_sink=runtime_provider_ready,
            )
            async for event in adapter_stream:
                if event.event == "chat.run.started":
                    live_session_id = event.data.get("run_id")
                    if isinstance(live_session_id, str) and live_session_id.strip():
                        await runtime_provider_ready(
                            live_session_id.strip()
                        )
                if event.event == "chat.done":
                    await self._record_clarify_request_if_needed(context)
                    close_stream = getattr(adapter_stream, "aclose", None)
                    if callable(close_stream):
                        await close_stream()
                    await self._stop_artifact_watcher(context)
                fenced = await self._record_event(context, event)
                if event.event == "chat.done":
                    status = str(event.data.get("status") or "completed").lower()
                    terminal = (
                        "PARKED_FOR_CLARIFY"
                        if status == "parked_for_clarify"
                        else "CANCELED" if status in {"canceled", "cancelled"} else (
                            "FAILED" if status == "failed" else "COMPLETED"
                        )
                    )
                    if fenced and await self._latch_redirect_terminal(
                        context,
                        terminal,
                    ):
                        return
                    await self._finish(context, terminal)
                    return
            if not context.terminal:
                await self._record_event(
                    context,
                    PlatformStreamEvent(
                        event="chat.error",
                        data={
                            "code": "runtime_stream_ended_without_terminal",
                            "message": "Runtime stream ended before a terminal event",
                        },
                    ),
                )
                await self._stop_artifact_watcher(context)
                fenced = await self._record_event(
                    context,
                    PlatformStreamEvent(
                        event="chat.done",
                        data={"status": "failed"},
                    ),
                )
                if fenced and await self._latch_redirect_terminal(
                    context,
                    "FAILED",
                ):
                    return
                await self._finish(context, "FAILED")
        except asyncio.CancelledError:
            if not context.terminal:
                await self._stop_artifact_watcher(context)
                if context.cancellation_requested_at is not None:
                    await self._record_clarify_request_if_needed(context)
                    fenced = await self._record_event(
                        context,
                        PlatformStreamEvent(
                            event="chat.done",
                            data={"status": "canceled"},
                        ),
                    )
                    if fenced and await self._latch_redirect_terminal(
                        context,
                        "CANCELED",
                    ):
                        return
                    await self._finish(context, "CANCELED")
                    return
                fenced = await self._record_event(
                    context,
                    PlatformStreamEvent(
                        event="chat.done",
                        data={
                            "status": "failed",
                            "code": "runtime_termination",
                        },
                    ),
                )
                if fenced and await self._latch_redirect_terminal(
                    context,
                    "RUN_INTERRUPTED_BY_RUNTIME_TERMINATION",
                ):
                    raise
                await self._finish(
                    context,
                    "RUN_INTERRUPTED_BY_RUNTIME_TERMINATION",
                )
            raise
        except BaseException as exc:
            fenced = await self._record_event(
                context,
                PlatformStreamEvent(
                    event="chat.error",
                    data={
                        "code": "runtime_execution_failed",
                        "message": str(exc) or type(exc).__name__,
                    },
                ),
            )
            await self._stop_artifact_watcher(context)
            await self._record_event(
                context,
                PlatformStreamEvent(
                    event="chat.done",
                    data={
                        "status": "failed",
                        "code": "runtime_execution_failed",
                    },
                ),
            )
            terminal = (
                "FAILED_SUBMISSION_UNCERTAIN"
                if context.prompt_attempted
                and context.run.state != "ACKNOWLEDGED"
                else "FAILED"
            )
            if fenced and await self._latch_redirect_terminal(
                context,
                terminal,
            ):
                return
            await self._finish(context, terminal)

    async def _latch_redirect_terminal(
        self,
        context: _RunContext,
        terminal_state: str,
    ) -> bool:
        async with context.event_lock:
            if (
                context.redirect_fence_group_id is None
                and not context.redirect_fence_releasing
            ):
                return False
            existing = context.redirect_pending_terminal_state
            if existing is not None and existing != terminal_state:
                raise RuntimeError("redirect terminal latch conflicts")
            context.redirect_pending_terminal_state = terminal_state
        if self.redirect_lifecycle is not None:
            prepare_parent_terminal = getattr(
                self.redirect_lifecycle,
                "prepare_parent_terminal",
                None,
            )
            if callable(prepare_parent_terminal):
                await prepare_parent_terminal(context.run.run_id)
        return True

    async def _record_event(
        self,
        context: _RunContext,
        event: PlatformStreamEvent,
        *,
        bypass_redirect_fence: bool = False,
    ) -> bool:
        # The adapter stream and ArtifactWatcher are concurrent producers.
        # Preserve one monotonic Run sequence across durable storage, outbox,
        # replay and browser delivery.
        if (
            not bypass_redirect_fence
            and context.redirect_fence_group_id is not None
            and self.redirect_lifecycle is not None
        ):
            await self.redirect_lifecycle.reconcile_redirect(
                context.run.run_id
            )
        async with context.event_lock:
            if not bypass_redirect_fence and (
                context.redirect_fence_group_id is not None
                or context.redirect_fence_releasing
            ):
                if len(context.redirect_fenced_events) >= 4096:
                    raise RuntimeError(
                        "redirect event fence semantic frame limit exceeded"
                    )
                context.redirect_fenced_events.append(event)
                return True
            if event.event == "chat.clarify.requested":
                context.clarify_event_recorded = True
            context.seq += 1
            now = datetime.now(timezone.utc)
            envelope = {
                "schema_version": 1,
                "type": "run.event",
                "run_id": context.run.run_id,
                "seq": context.seq,
                "event_type": event.event,
                "emitted_at": now.isoformat(),
                "data": event.data,
            }
            chunk = json.dumps(
                envelope,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self._append_replay(context, chunk)

            if (
                event.event == "chat.message.completed"
                and event.data.get("interim") is not True
            ):
                terminal_text = event.data.get("text")
                if isinstance(terminal_text, str):
                    context.answer = terminal_text
                    context.answer_bytes_since_snapshot += len(
                        terminal_text.encode("utf-8")
                    )
            if event.event in DURABLE_EVENT_TYPES:
                stored = await self.store.append_run_event(
                    StoredRunEvent(
                        run_id=context.run.run_id,
                        seq=context.seq,
                        event_type=event.event,
                        payload=event.data,
                        occurred_at=now,
                    ),
                    durable=True,
                )
                if stored:
                    await self.store.enqueue_projection(
                        projection_id=f"{context.run.run_id}:event:{context.seq}",
                        projection_type="run.event",
                        payload={
                            "run_id": context.run.run_id,
                            "session_id": context.run.session_id,
                            "seq": context.seq,
                            "event_type": event.event,
                            "event_data": event.data,
                            "occurred_at": now.isoformat(),
                            "config_revision": context.run.config_revision,
                            "source": context.run.source,
                            "user_id": context.run.user_id,
                            "org_id": context.run.org_id,
                            "profile_id": context.run.profile_id,
                            "model_id": context.run.model_id,
                        },
                        created_at=now,
                    )
                    observation = event.internal
                    if isinstance(
                        observation,
                        ObservedSkillManageCompletion,
                    ):
                        projection_key = (
                            f"{context.run.run_id}:{context.seq}"
                        )
                        await self.store.enqueue_projection(
                            projection_id=(
                                "skill-manage:"
                                + hashlib.sha256(
                                    projection_key.encode("utf-8")
                                ).hexdigest()
                            ),
                            projection_type="skill.manage.observed",
                            payload={
                                "run_id": context.run.run_id,
                                "session_id": context.run.session_id,
                                "profile_id": context.run.profile_id,
                                "event_seq": context.seq,
                                "action": observation.action,
                                "skill_name": observation.skill_name,
                                **({"description": observation.description} if observation.description is not None else {}),
                                **({"summary": observation.summary} if observation.summary is not None else {}),
                                "tool_call_id": observation.tool_call_id,
                                "duration_ms": observation.duration_ms,
                                "occurred_at": (
                                    observation.observed_at.isoformat()
                                ),
                                "config_revision": (
                                    context.run.config_revision
                                ),
                            },
                            created_at=now,
                        )
            if (
                context.answer_bytes_since_snapshot >= SNAPSHOT_BYTES
                or self.clock() - context.last_snapshot_at >= SNAPSHOT_INTERVAL_SECONDS
                or event.event in {"chat.message.completed", "chat.done", "chat.error"}
            ):
                await self.store.save_answer_snapshot(
                    context.run.run_id,
                    answer=context.answer,
                    through_seq=context.seq,
                )
                context.answer_bytes_since_snapshot = 0
                context.last_snapshot_at = self.clock()
            await self._publish(
                context,
                chunk,
                event_type=event.event,
            )
            return False

    async def _record_clarify_request_if_needed(
        self,
        context: _RunContext,
    ) -> None:
        if (
            context.clarify_request_id is None
            or context.clarify_event_recorded
        ):
            return
        await self._record_event(
            context,
            PlatformStreamEvent(
                event="chat.clarify.requested",
                data={"request_id": context.clarify_request_id},
            ),
        )

    async def _start_artifact_watcher(self, context: _RunContext) -> None:
        if self.artifact_workspace_dir is None:
            return
        try:
            watcher = ArtifactWatcher(
                workspace_dir=self.artifact_workspace_dir,
                identity=context.identity,
                emit=lambda event: self._record_event(context, event),
            )
            context.artifact_watcher = watcher
            await watcher.start()
        except Exception as exc:
            # Artifact observation is additive; it must never break a user
            # reply. A later Run's restricted reconciliation can compensate.
            context.artifact_watcher = None
            logger.warning(
                "artifact_watcher_start_failed",
                extra={
                    "event": "artifact_watcher_start_failed",
                    "run_id": context.run.run_id,
                    "error_type": type(exc).__name__,
                },
            )

    async def _stop_artifact_watcher(self, context: _RunContext) -> None:
        watcher = context.artifact_watcher
        if watcher is None:
            return
        context.artifact_watcher = None
        try:
            await watcher.stop()
        except Exception as exc:
            logger.warning(
                "artifact_watcher_stop_failed",
                extra={
                    "event": "artifact_watcher_stop_failed",
                    "run_id": context.run.run_id,
                    "error_type": type(exc).__name__,
                },
            )

    async def _publish(
        self,
        context: _RunContext,
        chunk: bytes,
        *,
        event_type: str,
    ) -> None:
        async with context.publish_lock:
            self._publish_now(context, chunk)
        if event_type in REALTIME_EVENT_TYPES:
            # Upstream clients can decode many SSE events from one network
            # read. Hand off after every realtime delta so Runtime Edge and
            # Channel Fast Lane senders can transmit it before the next frame.
            await asyncio.sleep(0)

    def _publish_now(self, context: _RunContext, chunk: bytes) -> None:
        slow: list[_Subscriber] = []
        for subscriber in tuple(context.subscriber_by_id.values()):
            if subscriber.closed:
                continue
            if subscriber.queued_bytes + len(chunk) > PER_RUN_REALTIME_LIMIT:
                slow.append(subscriber)
                continue
            subscriber.queue.put_nowait(chunk)
            subscriber.queued_bytes += len(chunk)
        for subscriber in slow:
            self._slow_subscriber_disconnects += 1
            self._close_subscriber(subscriber)

    async def _finish(self, context: _RunContext, state: str) -> None:
        if context.terminal:
            return
        if state == "CANCELED" or context.cancellation_requested_at is not None:
            if self.redirect_lifecycle is None:
                await self.store.cancel_pre_rpc_redirects_for_parent(
                    context.run.run_id,
                    canceled_at=datetime.now(timezone.utc),
                )
            else:
                prepare_parent_stop = getattr(
                    self.redirect_lifecycle,
                    "prepare_parent_stop",
                    None,
                )
                if callable(prepare_parent_stop):
                    redirect_session_settled = bool(
                        await prepare_parent_stop(context.run.run_id)
                    )
                    if redirect_session_settled:
                        context.interrupt_delivered = True
                elif not await self.redirect_lifecycle.cancel_parent_pre_rpc(
                    context.run.run_id
                ):
                    raise RuntimeError(
                        "redirect crossed RPC before parent Run cancellation"
                    )
        else:
            prepare_parent_terminal = (
                getattr(
                    self.redirect_lifecycle,
                    "prepare_parent_terminal",
                    None,
                )
                if self.redirect_lifecycle is not None
                else None
            )
            if callable(prepare_parent_terminal):
                await prepare_parent_terminal(context.run.run_id)
            else:
                await self._promote_pre_rpc_redirects(context)
            # Terminal Follow-up dispositions are committed by the Store, not
            # by _record_event.  Publish those exact rows before closing live
            # subscribers so chat.done cannot hide the business terminal.
            await self.publish_durable_followup_events(context.run.run_id)
        context.terminal = True
        await self._stop_artifact_watcher(context)
        now = datetime.now(timezone.utc)
        context.run = await self.store.transition_run(
            context.run.run_id,
            state,
            terminal_at=now,
        )
        await self.store.save_answer_snapshot(
            context.run.run_id,
            answer=context.answer,
            through_seq=context.seq,
        )
        await self.store.enqueue_projection(
            projection_id=f"{context.run.run_id}:terminal",
            projection_type="run.terminal",
            payload={
                "run_id": context.run.run_id,
                "session_id": context.run.session_id,
                "state": state,
                "answer": context.answer,
                "terminal_seq": context.seq,
                "accepted_at": context.run.accepted_at.isoformat(),
                "terminal_at": now.isoformat(),
                "client_message_id": context.run.client_message_id,
                "config_revision": context.run.config_revision,
                "source": context.run.source,
                "user_id": context.run.user_id,
                "org_id": context.run.org_id,
                "profile_id": context.run.profile_id,
                "model_id": context.run.model_id,
                "prompt": context.audit_prompt,
            },
            created_at=now,
        )
        for subscriber in tuple(context.subscriber_by_id.values()):
            if not subscriber.closed:
                subscriber.closed = True
                subscriber.queue.put_nowait(None)
        await self._release_context(context)

    async def _promote_pre_rpc_redirects(self, context: _RunContext) -> None:
        """Move corrections that never crossed RPC onto their reserved FIFO Runs."""

        reservations = await self.store.list_input_admission_reservations(
            context.identity.session_id
        )
        saw_parent_redirect = False
        for reservation in reservations:
            if reservation.kind != "redirect" or reservation.state == "promoted":
                break
            redirect = await self.store.get_redirect_by_message(
                session_id=context.identity.session_id,
                client_message_id=reservation.client_message_id,
            )
            if redirect is None:
                raise RuntimeError("redirect reservation has no ledger row")
            if redirect.parent_run_id != context.run.run_id:
                break
            saw_parent_redirect = True
            if redirect.state == "WAITING_AGENT_READY":
                await self.store.mark_redirect_boundary_ready(
                    redirect.redirect_id,
                    updated_at=datetime.now(timezone.utc),
                )

        if not saw_parent_redirect:
            return

        while True:
            unresolved = (
                await self.store.get_unresolved_redirect_group_for_parent(
                    context.run.run_id
                )
            )
            if unresolved is not None:
                if unresolved.state != "PREPARED":
                    raise RuntimeError(
                        "redirect crossed RPC before parent Run terminal proof"
                    )
                await self.store.promote_redirect_group(
                    unresolved.dispatch_group_id,
                    promoted_at=datetime.now(timezone.utc),
                )
                continue
            group = await self.store.prepare_redirect_group(
                dispatch_group_id=f"redirect_group_{uuid4().hex}",
                parent_run_id=context.run.run_id,
                created_at=datetime.now(timezone.utc),
            )
            if group is None:
                return
            await self.store.promote_redirect_group(
                group.dispatch_group_id,
                promoted_at=datetime.now(timezone.utc),
            )

    async def _release_context(self, context: _RunContext) -> None:
        if context.released:
            return
        context.released = True
        try:
            try:
                await self.sessions.mark_run_terminal(context.identity.session_id)
            finally:
                if self.model_router is not None:
                    await self.model_router.finish_run(
                        context.identity,
                        context.run.run_id,
                        completed=context.run.state == "COMPLETED",
                    )
        finally:
            await context.capacity_lease.release()
            async with self._guard:
                if (
                    self._active_by_session.get(context.identity.session_id)
                    == context.run.run_id
                ):
                    self._active_by_session.pop(context.identity.session_id, None)
                self._contexts.pop(context.run.run_id, None)
                if context.subscribers:
                    self._retired_contexts[context.run.run_id] = context
                else:
                    self._drop_replay(context)
                self._idempotency_locks.pop(
                    (
                        context.run.session_id,
                        context.run.client_message_id,
                    ),
                    None,
                )
        if not self._closed:
            await self._drain_session(context.identity.session_id)
            await self._notify_queue_updated(context.identity.session_id)
            for queued_session_id in await self.store.list_queued_session_ids():
                if queued_session_id == context.identity.session_id:
                    continue
                self._schedule_drain(queued_session_id)

    async def _notify_queue_updated(
        self,
        session_id: str,
        *,
        only_if_queued: bool = False,
    ) -> None:
        sink = self._queue_update_sink
        if sink is None:
            return
        summary = await self.store.queue_summary(session_id)
        if only_if_queued and summary["queued_count"] == 0:
            return
        await sink(
            session_id,
            {
                "queued_count": int(summary["queued_count"]),
                "scheduled_queued_count": int(
                    summary["scheduled_queued_count"]
                ),
            },
        )

    def _schedule_drain(
        self,
        session_id: str,
        *,
        delay_seconds: float = 0.0,
    ) -> None:
        if self._closed:
            return

        async def drain() -> None:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            await self._drain_session(session_id)

        task = asyncio.create_task(
            drain(),
            name=f"runtime-session-drain-{session_id}",
        )
        self._drain_tasks.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            self._drain_tasks.discard(done)
            if not done.cancelled():
                exception = done.exception()
                if exception is not None:
                    logger.error(
                        "runtime_session_queue_drain_failed",
                        exc_info=exception,
                        extra={"session_id": session_id},
                    )

        task.add_done_callback(completed)

    def _append_replay(self, context: _RunContext, chunk: bytes) -> None:
        key = (context.run.run_id, context.seq)
        context.replay.append((context.seq, chunk))
        context.replay_bytes += len(chunk)
        self._global_replay_order.append(
            (context.run.run_id, context.seq, len(chunk))
        )
        self._global_replay_keys.add(key)
        self._realtime_buffer_bytes += len(chunk)
        while context.replay and context.replay_bytes > PER_RUN_REALTIME_LIMIT:
            removed_seq, removed = context.replay.popleft()
            context.replay_bytes -= len(removed)
            self._discard_global_replay(
                context.run.run_id,
                removed_seq,
                len(removed),
            )
        self._enforce_global_replay_limit()
        if (
            len(self._global_replay_order)
            > len(self._global_replay_keys) * 2 + 1024
        ):
            self._global_replay_order = deque(
                item
                for item in self._global_replay_order
                if (item[0], item[1]) in self._global_replay_keys
            )

    def _enforce_global_replay_limit(self) -> None:
        while (
            self._realtime_buffer_bytes > GLOBAL_REALTIME_LIMIT
            and self._global_replay_order
        ):
            run_id, seq, size = self._global_replay_order.popleft()
            key = (run_id, seq)
            if key not in self._global_replay_keys:
                continue
            self._global_replay_keys.discard(key)
            self._realtime_buffer_bytes = max(
                0,
                self._realtime_buffer_bytes - size,
            )
            context = self._contexts.get(run_id) or self._retired_contexts.get(
                run_id
            )
            if context is None:
                continue
            if context.replay and context.replay[0][0] == seq:
                _, removed = context.replay.popleft()
                context.replay_bytes = max(
                    0,
                    context.replay_bytes - len(removed),
                )
            else:
                retained = deque(
                    item for item in context.replay if item[0] != seq
                )
                if len(retained) != len(context.replay):
                    context.replay = retained
                    context.replay_bytes = sum(
                        len(item[1]) for item in retained
                    )
            for subscriber in tuple(context.subscriber_by_id.values()):
                self._slow_subscriber_disconnects += 1
                self._close_subscriber(subscriber)

    def _drop_replay(self, context: _RunContext) -> None:
        while context.replay:
            seq, chunk = context.replay.popleft()
            self._discard_global_replay(
                context.run.run_id,
                seq,
                len(chunk),
            )
        context.replay_bytes = 0

    def _discard_global_replay(
        self,
        run_id: str,
        seq: int,
        size: int,
    ) -> None:
        key = (run_id, seq)
        if key in self._global_replay_keys:
            self._global_replay_keys.discard(key)
            self._realtime_buffer_bytes = max(
                0,
                self._realtime_buffer_bytes - size,
            )

    @staticmethod
    def _close_subscriber(subscriber: _Subscriber) -> None:
        if subscriber.closed:
            return
        subscriber.closed = True
        while True:
            try:
                subscriber.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        subscriber.queued_bytes = 0
        subscriber.queue.put_nowait(None)
