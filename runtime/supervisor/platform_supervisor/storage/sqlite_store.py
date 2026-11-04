from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Final, Literal


DISPATCH_INTENT_DURABLE: Final = "DISPATCH_INTENT_DURABLE"
QUEUED: Final = "QUEUED"
ADMITTING: Final = "ADMITTING"
RECOVERING: Final = "RECOVERING"
ACTIVE_RUN_STATES: Final = frozenset(
    {ADMITTING, DISPATCH_INTENT_DURABLE, "ACKNOWLEDGED", "RUNNING", RECOVERING}
)
REDIRECT_PARENT_STATES: Final = frozenset(
    {ADMITTING, DISPATCH_INTENT_DURABLE, "ACKNOWLEDGED", "RUNNING"}
)
TERMINAL_STATES: Final = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "CANCELED",
        "FAILED_SUBMISSION_UNCERTAIN",
        "RUN_INTERRUPTED_BY_RUNTIME_RESTART",
        "RUN_INTERRUPTED_BY_RUNTIME_TERMINATION",
        "RUN_INTERRUPTED_BY_ADMIN_RESTART",
    }
)
REDIRECT_WAITING_STATES: Final = frozenset(
    {"WAITING_AGENT_READY", "WAITING_REDIRECT_BOUNDARY"}
)
REDIRECT_GROUP_UNRESOLVED_STATES: Final = frozenset(
    {
        "PREPARED",
        "DISPATCH_INTENT",
        "RPC_ACKNOWLEDGED",
        "ISOLATING_UNCERTAIN",
    }
)
REDIRECT_ORIGINS: Final = frozenset(
    {
        "user_web",
        "application_studio",
        "wecom_personal",
        "wecom_application",
    }
)
REDIRECT_MEMBER_SEPARATOR: Final = "\n\n[Additional user correction]\n"
MAX_REDIRECT_GROUP_MEMBERS: Final = 32
MAX_REDIRECT_WIRE_BYTES: Final = 1_048_576
class SQLiteStoreError(RuntimeError):
    pass


class IdempotencyConflict(SQLiteStoreError):
    pass


@dataclass(frozen=True, slots=True)
class DispatchIntent:
    run_id: str
    session_id: str
    client_message_id: str
    payload_digest: str
    config_revision: str
    source: str
    accepted_at: datetime
    user_id: int = 0
    org_id: int = 0
    profile_id: str = ""
    model_id: str = ""
    prompt: str = ""


@dataclass(frozen=True, slots=True)
class RunEnqueueIntent:
    run_id: str
    session_id: str
    client_message_id: str
    payload_digest: str
    config_revision: str
    source: str
    enqueued_at: datetime
    prompt: str
    audit_prompt: str
    identity: dict[str, Any]
    source_detail: dict[str, Any] | None = None
    scheduled_task_id: int | None = None
    scheduled_occurrence_key: str | None = None
    delivery_id: str | None = None


@dataclass(frozen=True, slots=True)
class StoredRun:
    run_id: str
    session_id: str
    client_message_id: str
    payload_digest: str
    config_revision: str
    source: str
    state: str
    accepted_at: datetime | None
    created: bool
    enqueued_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
    enqueue_seq: int = 0
    prompt: str = ""
    audit_prompt: str = ""
    identity: dict[str, Any] | None = None
    source_detail: dict[str, Any] | None = None
    scheduled_task_id: int | None = None
    scheduled_occurrence_key: str | None = None
    coalesced_count: int = 0
    last_coalesced_at: datetime | None = None
    history_message_count_before_run: int | None = None
    user_id: int = 0
    org_id: int = 0
    profile_id: str = ""
    model_id: str = ""
    runtime_provider_run_id: str | None = None
    cancellation_requested_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StoredRunEvent:
    run_id: str
    seq: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    run: StoredRun
    answer: str
    answer_through_seq: int
    events: tuple[StoredRunEvent, ...]


@dataclass(frozen=True, slots=True)
class StoredSessionEnvelope:
    session_id: str
    prepare_identity_digest: str
    state: str
    last_activity_at: datetime
    prepare_identity: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StoredCommand:
    command_id: str
    payload_digest: str
    command_type: str
    state: str
    result: dict[str, Any] | None
    created: bool


@dataclass(frozen=True, slots=True)
class StoredProjection:
    outbox_id: int
    projection_id: str
    projection_type: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredChannelDeliveryCheckpoint:
    run_id: str
    highest_contiguous_seq: int
    gateway_instance_id: str
    channel_lease_epoch: int
    provider_request_id: str
    provider_message_id: str | None
    projection_version: int
    confirmed_at: datetime


@dataclass(frozen=True, slots=True)
class StoredChannelFastLaneV2Delivery:
    delivery_id: str
    session_id: str
    client_message_id: str
    payload_digest: str
    target_kind: str
    run_id: str | None
    redirect_id: str | None
    canonical_run_id: str
    parent_run_id: str | None
    disposition_seq: int
    disposition_state: str
    start_after_seq: int | None
    promoted_run_id: str | None
    disposition_ack_seq: int
    provider_confirmed_seq: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredChannelFastLaneV2Checkpoint:
    canonical_run_id: str
    owner_delivery_id: str
    highest_contiguous_seq: int
    gateway_instance_id: str
    channel_lease_epoch: int
    provider_request_id: str
    provider_message_id: str | None
    projection_version: int
    confirmed_at: datetime


@dataclass(frozen=True, slots=True)
class RedirectAdmissionIntent:
    redirect_id: str
    session_id: str
    client_message_id: str
    request_digest: str
    parent_run_id: str
    interaction_origin: str
    content: str
    initial_state: str
    promotion_reserved_run_id: str
    promotion_run_intent: dict[str, Any]
    promotion_run_payload_digest: str
    admission_generation: int
    created_at: datetime
    delivery_id: str | None = None
    allow_terminal_parent: bool = False


@dataclass(frozen=True, slots=True)
class StoredRedirect:
    redirect_id: str
    session_id: str
    client_message_id: str
    request_digest: str
    parent_run_id: str
    input_admission_seq: int
    interaction_origin: str
    content: str
    state: str
    hermes_status: str | None
    dispatch_group_id: str | None
    dispatch_group_position: int | None
    pending_event_seq: int
    accepted_event_seq: int | None
    consumed_event_seq: int | None
    terminal_disposition_kind: str | None
    terminal_disposition_event_seq: int | None
    start_after_seq: int | None
    delivery_id: str | None
    promoted_run_id: str | None
    promotion_reserved_run_id: str
    promotion_run_intent: dict[str, Any]
    promotion_run_payload_digest: str
    promoted_pre_prompt_message_id: int | None
    promoted_pre_prompt_history_digest: str | None
    promoted_prompt_wire_digest: str | None
    promoted_proof_message_id: int | None
    promoted_proof_post_digest: str | None
    created_at: datetime
    updated_at: datetime
    created: bool = False
    continuation_client_message_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredFollowupHistoryEntry:
    redirect_id: str
    client_message_id: str
    parent_run_id: str
    input_admission_seq: int
    content: str
    state: str
    dispatch_group_id: str | None
    dispatch_group_position: int | None
    promoted_run_id: str | None
    group_wire_text: str | None
    group_proof_message_id: int | None
    group_proof_kind: str | None
    group_hermes_status: str | None
    promoted_history_message_index: int | None


@dataclass(frozen=True, slots=True)
class InputAdmissionReservation:
    input_admission_seq: int
    session_id: str
    kind: str
    run_id: str | None
    redirect_id: str | None
    client_message_id: str
    state: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredRedirectDispatchGroup:
    dispatch_group_id: str
    session_id: str
    parent_run_id: str
    aggregate_digest: str
    raw_aggregate_digest: str
    wire_text: str
    wire_text_digest: str
    member_count: int
    state: str
    hermes_status: str | None
    hermes_profile_key: str | None
    hermes_stored_session_id: str | None
    pre_dispatch_message_id: int | None
    pre_dispatch_history_digest: str | None
    pre_dispatch_model_request_seq: int | None
    pre_dispatch_model_request_digest: str | None
    dispatch_turn_id: str | None
    dispatch_turn_task_id: str | None
    dispatch_origin_evidence_digest: str | None
    dispatch_boundary_epoch_nonce: str | None
    dispatch_marker_request_nonce: str | None
    dispatch_peer_boot_id: str | None
    dispatch_peer_connection_id: str | None
    proof_message_id: int | None
    proof_kind: str | None
    proof_post_digest: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class _DispatchWrite:
    intent: DispatchIntent
    future: asyncio.Future[StoredRun]


MutationOperation = Literal[
    "run_enqueue",
    "run_claim",
    "run_requeue",
    "run_history_checkpoint",
    "run_cancel_queued_scheduled",
    "run_cancel_queued",
    "run_state",
    "run_provider",
    "run_cancel_request",
    "run_event",
    "answer_snapshot",
    "session",
    "command",
    "command_result",
    "command_abandon",
    "outbox",
    "outbox_ack",
    "revocation",
    "file_ticket",
    "delivery_ack",
    "channel_v2_bind_run",
    "channel_v2_bind_redirect",
    "channel_v2_refresh",
    "channel_v2_disposition_ack",
    "channel_v2_provider_ack",
    "redirect_admit",
    "redirect_group_prepare",
    "redirect_boundary_ready",
    "redirect_group_dispatch",
    "redirect_group_pre_rpc_rollback",
    "redirect_group_ack",
    "redirect_group_isolate",
    "redirect_group_uncertain",
    "redirect_group_failed",
    "redirect_group_consumed",
    "redirect_event_fence_release_complete",
    "redirect_group_promote",
    "redirect_parent_cancel",
]


@dataclass(slots=True)
class _MutationWrite:
    operation: MutationOperation
    values: tuple[Any, ...]
    future: asyncio.Future[Any]


_STOP = object()


class SQLiteStore:
    """Single-writer SQLite journal with a bounded dispatch group-commit window."""

    def __init__(
        self,
        path: Path,
        *,
        group_commit_window_ms: int = 2,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = path
        self.group_commit_window_seconds = group_commit_window_ms / 1000
        self.busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None
        self._queue: asyncio.Queue[_DispatchWrite | _MutationWrite | object] = (
            asyncio.Queue()
        )
        self._writer_task: asyncio.Task[None] | None = None
        self._connection_lock = asyncio.Lock()
        self._commit_latency_ms: deque[float] = deque(maxlen=2048)

    async def start(self) -> None:
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = self._open_and_migrate()
        self._writer_task = asyncio.create_task(
            self._writer_loop(),
            name="supervisor-sqlite-writer",
        )

    async def close(self) -> None:
        if self._connection is None:
            return
        if self._writer_task is not None:
            await self._queue.put(_STOP)
            await self._writer_task
            self._writer_task = None
        connection = self._connection
        self._connection = None
        connection.close()

    async def quick_check(self) -> bool:
        return await asyncio.to_thread(self._quick_check_readonly)

    def _quick_check_readonly(self) -> bool:
        connection = sqlite3.connect(
            f"{self.path.resolve(strict=False).as_uri()}?mode=ro",
            timeout=self.busy_timeout_ms / 1000,
            uri=True,
        )
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
        return bool(row and row[0] == "ok")




    async def persist_dispatch_intent(self, intent: DispatchIntent) -> StoredRun:
        if intent.accepted_at.tzinfo is None:
            raise ValueError("accepted_at must be timezone-aware")
        self._require_connection()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[StoredRun] = loop.create_future()
        await self._queue.put(_DispatchWrite(intent=intent, future=future))
        return await future

    async def enqueue_run(self, intent: RunEnqueueIntent) -> StoredRun:
        if intent.enqueued_at.tzinfo is None:
            raise ValueError("enqueued_at must be timezone-aware")
        return await self._submit_mutation(
            "run_enqueue",
            intent,
        )

    async def admit_redirect(
        self,
        intent: RedirectAdmissionIntent,
    ) -> StoredRedirect:
        if intent.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if intent.interaction_origin not in REDIRECT_ORIGINS:
            raise ValueError("redirect interaction origin is invalid")
        if intent.initial_state not in REDIRECT_WAITING_STATES:
            raise ValueError("redirect initial state is invalid")
        if intent.admission_generation <= 0:
            raise ValueError("redirect admission generation must be positive")
        if not intent.content.strip():
            raise ValueError("redirect content must not be blank")
        for digest in (
            intent.request_digest,
            intent.promotion_run_payload_digest,
        ):
            self._validate_sha256_digest(digest)
        if not intent.promotion_reserved_run_id:
            raise ValueError("promotion reserved Run id must not be empty")
        if intent.delivery_id is not None and not str(
            intent.promotion_run_intent.get("surface_payload_digest") or ""
        ):
            raise ValueError(
                "Fast Lane v2 redirect surface payload digest is required"
            )
        return await self._submit_mutation("redirect_admit", intent)

    async def get_redirect_by_message(
        self,
        *,
        session_id: str,
        client_message_id: str,
    ) -> StoredRedirect | None:
        connection = self._require_connection()
        async with self._connection_lock:
            row = connection.execute(
                """
                SELECT * FROM run_redirects
                WHERE session_id = ? AND client_message_id = ?
                """,
                (session_id, client_message_id),
            ).fetchone()
            promoted_source_detail: str | None = None
            if row is not None and row["promoted_run_id"] is not None:
                promoted = connection.execute(
                    "SELECT source_detail_json FROM runs WHERE run_id = ?",
                    (str(row["promoted_run_id"]),),
                ).fetchone()
                if promoted is not None:
                    promoted_source_detail = promoted["source_detail_json"]
        if row is None:
            return None
        redirect = self._row_to_redirect(row, created=False)
        if promoted_source_detail:
            detail = json.loads(str(promoted_source_detail))
            raw_ids = detail.get("followup_client_message_ids", [])
            if isinstance(raw_ids, list) and all(
                isinstance(item, str) and item for item in raw_ids
            ):
                redirect = replace(
                    redirect,
                    continuation_client_message_ids=tuple(raw_ids),
                )
        return redirect

    async def list_session_followup_history(
        self,
        session_id: str,
    ) -> list[StoredFollowupHistoryEntry]:
        connection = self._require_connection()
        async with self._connection_lock:
            rows = connection.execute(
                """
                SELECT redirects.redirect_id, redirects.client_message_id,
                       redirects.parent_run_id,
                       COALESCE(
                         json_extract(
                           redirects.promotion_run_intent_json,
                           '$.source_detail.followup_original_parent_run_id'
                         ),
                         redirects.parent_run_id
                       ) AS public_parent_run_id,
                       redirects.input_admission_seq, redirects.content,
                       redirects.state, redirects.dispatch_group_id,
                       redirects.dispatch_group_position,
                       redirects.promoted_run_id,
                       groups.wire_text AS group_wire_text,
                       groups.proof_message_id AS group_proof_message_id,
                       groups.proof_kind AS group_proof_kind,
                       groups.hermes_status AS group_hermes_status,
                       promoted.history_message_count_before_run
                           AS promoted_history_message_index
                FROM run_redirects AS redirects
                LEFT JOIN run_redirect_dispatch_groups AS groups
                  ON groups.dispatch_group_id = redirects.dispatch_group_id
                LEFT JOIN runs AS promoted
                  ON promoted.run_id = redirects.promoted_run_id
                WHERE redirects.session_id = ?
                ORDER BY redirects.input_admission_seq
                """,
                (session_id,),
            ).fetchall()
        return [
            StoredFollowupHistoryEntry(
                redirect_id=str(row["redirect_id"]),
                client_message_id=str(row["client_message_id"]),
                parent_run_id=str(row["public_parent_run_id"]),
                input_admission_seq=int(row["input_admission_seq"]),
                content=str(row["content"]),
                state=str(row["state"]),
                dispatch_group_id=(
                    str(row["dispatch_group_id"])
                    if row["dispatch_group_id"] is not None
                    else None
                ),
                dispatch_group_position=(
                    int(row["dispatch_group_position"])
                    if row["dispatch_group_position"] is not None
                    else None
                ),
                promoted_run_id=(
                    str(row["promoted_run_id"])
                    if row["promoted_run_id"] is not None
                    else None
                ),
                group_wire_text=(
                    str(row["group_wire_text"])
                    if row["group_wire_text"] is not None
                    else None
                ),
                group_proof_message_id=(
                    int(row["group_proof_message_id"])
                    if row["group_proof_message_id"] is not None
                    else None
                ),
                group_proof_kind=(
                    str(row["group_proof_kind"])
                    if row["group_proof_kind"] is not None
                    else None
                ),
                group_hermes_status=(
                    str(row["group_hermes_status"])
                    if row["group_hermes_status"] is not None
                    else None
                ),
                promoted_history_message_index=(
                    int(row["promoted_history_message_index"])
                    if row["promoted_history_message_index"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    async def list_input_admission_reservations(
        self,
        session_id: str,
        *,
        include_completed: bool = False,
    ) -> list[InputAdmissionReservation]:
        connection = self._require_connection()
        predicate = "" if include_completed else "AND state <> 'completed'"
        async with self._connection_lock:
            rows = connection.execute(
                f"""
                SELECT input_admission_seq, session_id, kind, run_id,
                       redirect_id, client_message_id, state, created_at
                FROM input_admission_reservations
                WHERE session_id = ? {predicate}
                ORDER BY input_admission_seq
                """,
                (session_id,),
            ).fetchall()
        return [self._row_to_reservation(row) for row in rows]

    async def mark_redirect_boundary_ready(
        self,
        redirect_id: str,
        *,
        updated_at: datetime,
    ) -> StoredRedirect:
        if updated_at.tzinfo is None:
            raise ValueError("updated_at must be timezone-aware")
        return await self._submit_mutation(
            "redirect_boundary_ready",
            redirect_id,
            self._utc_iso(updated_at),
        )

    async def prepare_redirect_group(
        self,
        *,
        dispatch_group_id: str,
        parent_run_id: str,
        created_at: datetime,
        max_members: int = MAX_REDIRECT_GROUP_MEMBERS,
        max_wire_bytes: int = MAX_REDIRECT_WIRE_BYTES,
        allow_canceling_parent: bool = False,
        allow_terminal_parent: bool = False,
    ) -> StoredRedirectDispatchGroup | None:
        if created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if max_members <= 0 or max_members > MAX_REDIRECT_GROUP_MEMBERS:
            raise ValueError("redirect member limit is invalid")
        if max_wire_bytes <= 0 or max_wire_bytes > MAX_REDIRECT_WIRE_BYTES:
            raise ValueError("redirect wire byte limit is invalid")
        return await self._submit_mutation(
            "redirect_group_prepare",
            dispatch_group_id,
            parent_run_id,
            self._utc_iso(created_at),
            max_members,
            max_wire_bytes,
            allow_canceling_parent,
            allow_terminal_parent,
        )

    async def record_redirect_dispatch_intent(
        self,
        *,
        dispatch_group_id: str,
        hermes_profile_key: str,
        hermes_stored_session_id: str,
        pre_dispatch_message_id: int,
        pre_dispatch_history_digest: str,
        pre_dispatch_model_request_seq: int,
        pre_dispatch_model_request_digest: str,
        dispatch_turn_id: str,
        dispatch_turn_task_id: str,
        dispatch_origin_evidence_digest: str,
        dispatch_boundary_epoch_nonce: str,
        dispatch_marker_request_nonce: str,
        dispatch_peer_boot_id: str,
        dispatch_peer_connection_id: str,
        pre_fence_event_seq: int,
        pre_fence_token_snapshot_digest: str,
        recorded_at: datetime,
    ) -> StoredRedirectDispatchGroup:
        if recorded_at.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware")
        if pre_dispatch_message_id < 0 or pre_dispatch_model_request_seq < 0:
            raise ValueError("redirect dispatch anchor is invalid")
        if pre_fence_event_seq < 0:
            raise ValueError("redirect event fence anchor is invalid")
        for digest in (
            pre_dispatch_history_digest,
            pre_dispatch_model_request_digest,
            dispatch_origin_evidence_digest,
            pre_fence_token_snapshot_digest,
        ):
            self._validate_sha256_digest(digest)
        return await self._submit_mutation(
            "redirect_group_dispatch",
            dispatch_group_id,
            hermes_profile_key,
            hermes_stored_session_id,
            pre_dispatch_message_id,
            pre_dispatch_history_digest,
            pre_dispatch_model_request_seq,
            pre_dispatch_model_request_digest,
            dispatch_turn_id,
            dispatch_turn_task_id,
            dispatch_origin_evidence_digest,
            dispatch_boundary_epoch_nonce,
            dispatch_marker_request_nonce,
            dispatch_peer_boot_id,
            dispatch_peer_connection_id,
            pre_fence_event_seq,
            pre_fence_token_snapshot_digest,
            self._utc_iso(recorded_at),
        )

    async def acknowledge_redirect_group(
        self,
        dispatch_group_id: str,
        *,
        hermes_status: str,
        acknowledged_at: datetime,
        queued_is_accepted: bool = False,
    ) -> StoredRedirectDispatchGroup:
        if acknowledged_at.tzinfo is None:
            raise ValueError("acknowledged_at must be timezone-aware")
        if hermes_status not in {"redirected", "queued"}:
            raise ValueError("unsupported redirect acknowledgement")
        return await self._submit_mutation(
            "redirect_group_ack",
            dispatch_group_id,
            hermes_status,
            self._utc_iso(acknowledged_at),
            queued_is_accepted,
        )

    async def rollback_pre_rpc_redirect_group(
        self,
        dispatch_group_id: str,
        *,
        rolled_back_at: datetime,
    ) -> StoredRedirectDispatchGroup:
        """Return an unclaimed dispatch intent to PREPARED.

        This operation is only legal before the current-boot boundary handle
        is claimed.  The caller owns that in-memory proof; SQLite additionally
        rejects any group that already has a Hermes disposition.
        """

        if rolled_back_at.tzinfo is None:
            raise ValueError("redirect rollback time must be timezone-aware")
        return await self._submit_mutation(
            "redirect_group_pre_rpc_rollback",
            dispatch_group_id,
            self._utc_iso(rolled_back_at),
        )

    async def isolate_redirect_group(
        self,
        dispatch_group_id: str,
        *,
        isolated_at: datetime,
    ) -> StoredRedirectDispatchGroup:
        if isolated_at.tzinfo is None:
            raise ValueError("redirect isolation time must be timezone-aware")
        return await self._submit_mutation(
            "redirect_group_isolate",
            dispatch_group_id,
            self._utc_iso(isolated_at),
        )

    async def finalize_redirect_group_uncertain(
        self,
        dispatch_group_id: str,
        *,
        finalized_at: datetime,
    ) -> StoredRedirectDispatchGroup:
        """Commit the post-settle, still-unproven redirect disposition."""

        if finalized_at.tzinfo is None:
            raise ValueError(
                "redirect uncertainty finalization time must be timezone-aware"
            )
        return await self._submit_mutation(
            "redirect_group_uncertain",
            dispatch_group_id,
            self._utc_iso(finalized_at),
        )

    async def finalize_redirect_group_failed(
        self,
        dispatch_group_id: str,
        *,
        finalized_at: datetime,
        error_code: str,
        include_all_parent_followups: bool = False,
        hermes_status: str | None = None,
    ) -> StoredRedirectDispatchGroup:
        """Atomically fail an unproven follow-up without creating a Run."""

        if finalized_at.tzinfo is None:
            raise ValueError("redirect failure time must be timezone-aware")
        normalized_error = error_code.strip()
        if not normalized_error:
            raise ValueError("redirect failure error code is required")
        if hermes_status not in {None, "rejected"}:
            raise ValueError("unsupported failed redirect Hermes status")
        return await self._submit_mutation(
            "redirect_group_failed",
            dispatch_group_id,
            self._utc_iso(finalized_at),
            normalized_error,
            include_all_parent_followups,
            hermes_status,
        )

    async def consume_redirect_group(
        self,
        dispatch_group_id: str,
        *,
        proof_message_id: int,
        proof_post_digest: str,
        consumed_at: datetime,
    ) -> StoredRedirectDispatchGroup:
        if consumed_at.tzinfo is None:
            raise ValueError("consumed_at must be timezone-aware")
        if proof_message_id < 0:
            raise ValueError("redirect proof message id is invalid")
        self._validate_sha256_digest(proof_post_digest)
        return await self._submit_mutation(
            "redirect_group_consumed",
            dispatch_group_id,
            proof_message_id,
            proof_post_digest,
            self._utc_iso(consumed_at),
        )

    async def complete_redirect_event_fence_release(
        self,
        dispatch_group_id: str,
    ) -> bool:
        """Delete a release-ready fence after buffered events are durable."""

        return await self._submit_mutation(
            "redirect_event_fence_release_complete",
            dispatch_group_id,
        )

    async def promote_redirect_group(
        self,
        dispatch_group_id: str,
        *,
        promoted_at: datetime,
        include_all_parent_followups: bool = False,
    ) -> StoredRedirectDispatchGroup:
        if promoted_at.tzinfo is None:
            raise ValueError("promoted_at must be timezone-aware")
        return await self._submit_mutation(
            "redirect_group_promote",
            dispatch_group_id,
            self._utc_iso(promoted_at),
            include_all_parent_followups,
        )

    async def cancel_pre_rpc_redirects_for_parent(
        self,
        parent_run_id: str,
        *,
        canceled_at: datetime,
    ) -> int:
        if canceled_at.tzinfo is None:
            raise ValueError("canceled_at must be timezone-aware")
        return await self._submit_mutation(
            "redirect_parent_cancel",
            parent_run_id,
            self._utc_iso(canceled_at),
        )

    async def get_redirect_group(
        self,
        dispatch_group_id: str,
    ) -> StoredRedirectDispatchGroup | None:
        connection = self._require_connection()
        async with self._connection_lock:
            row = connection.execute(
                """
                SELECT * FROM run_redirect_dispatch_groups
                WHERE dispatch_group_id = ?
                """,
                (dispatch_group_id,),
            ).fetchone()
        return self._row_to_redirect_group(row) if row else None

    async def get_unresolved_redirect_group_for_parent(
        self,
        parent_run_id: str,
    ) -> StoredRedirectDispatchGroup | None:
        connection = self._require_connection()
        placeholders = ",".join("?" for _ in REDIRECT_GROUP_UNRESOLVED_STATES)
        async with self._connection_lock:
            row = connection.execute(
                f"""
                SELECT * FROM run_redirect_dispatch_groups
                WHERE parent_run_id = ? AND state IN ({placeholders})
                ORDER BY created_at, dispatch_group_id LIMIT 1
                """,
                (parent_run_id, *sorted(REDIRECT_GROUP_UNRESOLVED_STATES)),
            ).fetchone()
        return self._row_to_redirect_group(row) if row else None

    async def list_unresolved_redirect_groups(
        self,
    ) -> list[StoredRedirectDispatchGroup]:
        """Load durable redirect work that must be reconciled after boot loss."""

        connection = self._require_connection()
        placeholders = ",".join("?" for _ in REDIRECT_GROUP_UNRESOLVED_STATES)
        async with self._connection_lock:
            rows = connection.execute(
                f"""
                SELECT * FROM run_redirect_dispatch_groups
                WHERE state IN ({placeholders})
                ORDER BY created_at, dispatch_group_id
                """,
                tuple(sorted(REDIRECT_GROUP_UNRESOLVED_STATES)),
            ).fetchall()
        return [self._row_to_redirect_group(row) for row in rows]

    async def list_redirect_groups_with_release_ready_fences(
        self,
    ) -> list[StoredRedirectDispatchGroup]:
        """Load terminal groups whose in-memory fence release was interrupted."""

        connection = self._require_connection()
        async with self._connection_lock:
            rows = connection.execute(
                """
                SELECT groups.*
                FROM run_redirect_dispatch_groups AS groups
                JOIN redirect_event_fences AS fences
                  ON fences.dispatch_group_id = groups.dispatch_group_id
                WHERE fences.state = 'RELEASE_READY'
                ORDER BY groups.created_at, groups.dispatch_group_id
                """
            ).fetchall()
        return [self._row_to_redirect_group(row) for row in rows]






    async def claim_next_queued(
        self,
        session_id: str,
        *,
        accepted_at: datetime,
    ) -> StoredRun | None:
        if accepted_at.tzinfo is None:
            raise ValueError("accepted_at must be timezone-aware")
        return await self._submit_mutation(
            "run_claim",
            session_id,
            self._utc_iso(accepted_at),
        )

    async def requeue_admitting(self, run_id: str) -> StoredRun:
        return await self._submit_mutation("run_requeue", run_id)

    async def queue_summary(self, session_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        async with self._connection_lock:
            row = connection.execute(
                """
                SELECT COUNT(*) AS queued_count,
                       SUM(CASE WHEN scheduled_task_id IS NOT NULL THEN 1 ELSE 0 END)
                           AS scheduled_queued_count
                FROM runs
                WHERE session_id = ? AND state = ?
                """,
                (session_id, QUEUED),
            ).fetchone()
            queued_rows = connection.execute(
                """
                SELECT run_id, client_message_id, enqueue_seq, audit_prompt,
                       source_detail_json
                FROM runs
                WHERE session_id = ?
                  AND state = ?
                  AND source = 'web'
                  AND scheduled_task_id IS NULL
                ORDER BY enqueue_seq
                """,
                (session_id, QUEUED),
            ).fetchall()
            active_placeholders = ",".join("?" for _ in ACTIVE_RUN_STATES)
            active_row = connection.execute(
                f"""
                SELECT run_id, state, cancellation_requested_at
                FROM runs
                WHERE session_id = ?
                  AND state IN ({active_placeholders})
                ORDER BY enqueue_seq
                LIMIT 1
                """,
                (session_id, *sorted(ACTIVE_RUN_STATES)),
            ).fetchone()
        queued_runs: list[dict[str, Any]] = []
        for queued in queued_rows:
            attachment_file_ids: list[str] = []
            try:
                source_detail = json.loads(
                    str(queued["source_detail_json"] or "{}")
                )
            except json.JSONDecodeError:
                source_detail = {}
            if isinstance(source_detail, dict):
                raw_attachment_ids = source_detail.get("attachment_file_ids")
                if isinstance(raw_attachment_ids, list):
                    attachment_file_ids = [
                        str(item)
                        for item in raw_attachment_ids[:20]
                        if isinstance(item, (str, int))
                    ]
            queued_runs.append(
                {
                    "run_id": str(queued["run_id"]),
                    "client_message_id": str(queued["client_message_id"]),
                    "enqueue_seq": int(queued["enqueue_seq"]),
                    "prompt": str(queued["audit_prompt"] or ""),
                    "attachment_file_ids": attachment_file_ids,
                }
            )
        return {
            "queued_count": int(row["queued_count"] or 0),
            "scheduled_queued_count": int(
                row["scheduled_queued_count"] or 0
            ),
            "queued_runs": queued_runs,
            "active_run": (
                {
                    "run_id": str(active_row["run_id"]),
                    "state": str(active_row["state"]),
                    "cancellation_requested_at": active_row[
                        "cancellation_requested_at"
                    ],
                }
                if active_row is not None
                else None
            ),
        }

    async def list_queued_session_ids(self) -> list[str]:
        connection = self._require_connection()
        async with self._connection_lock:
            rows = connection.execute(
                """
                SELECT session_id, MIN(enqueue_seq) AS first_seq
                FROM runs WHERE state = ?
                GROUP BY session_id ORDER BY first_seq
                """,
                (QUEUED,),
            ).fetchall()
        return [str(row["session_id"]) for row in rows]


    async def record_run_history_checkpoint(
        self,
        run_id: str,
        *,
        history_message_count_before_run: int,
    ) -> StoredRun:
        if history_message_count_before_run < 0:
            raise ValueError("history message count must not be negative")
        return await self._submit_mutation(
            "run_history_checkpoint",
            run_id,
            history_message_count_before_run,
        )

    async def session_history_sources(
        self,
        session_id: str,
    ) -> dict[int, dict[str, Any]]:
        connection = self._require_connection()
        async with self._connection_lock:
            rows = connection.execute(
                """
                SELECT history_message_count_before_run, source_detail_json
                FROM runs
                WHERE session_id = ?
                  AND history_message_count_before_run IS NOT NULL
                  AND source_detail_json IS NOT NULL
                ORDER BY enqueue_seq
                """,
                (session_id,),
            ).fetchall()
        sources: dict[int, dict[str, Any]] = {}
        for row in rows:
            source = json.loads(str(row["source_detail_json"]))
            if isinstance(source, dict):
                sources[int(row["history_message_count_before_run"])] = source
        return sources

    async def cancel_queued_scheduled_task(
        self,
        *,
        session_id: str,
        scheduled_task_id: int,
    ) -> str:
        return await self._submit_mutation(
            "run_cancel_queued_scheduled",
            session_id,
            scheduled_task_id,
            self._utc_iso(datetime.now(timezone.utc)),
        )

    async def cancel_queued_run(self, run_id: str) -> str:
        return await self._submit_mutation(
            "run_cancel_queued",
            run_id,
            self._utc_iso(datetime.now(timezone.utc)),
        )

    async def get_run(self, run_id: str) -> StoredRun | None:
        connection = self._require_connection()
        async with self._connection_lock:
            row = connection.execute(
                """
                SELECT run_id, session_id, client_message_id, payload_digest,
                       config_revision, source, state, accepted_at,
                       enqueued_at, enqueue_seq, prompt, audit_prompt,
                       identity_json, source_detail_json, scheduled_task_id,
                       scheduled_occurrence_key, coalesced_count,
                       last_coalesced_at, history_message_count_before_run,
                       user_id, org_id, profile_id, model_id,
                       runtime_provider_run_id, cancellation_requested_at
                FROM runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return self._row_to_run(row, created=False) if row else None

    async def get_run_by_message(
        self,
        *,
        session_id: str,
        client_message_id: str,
    ) -> StoredRun | None:
        connection = self._require_connection()
        async with self._connection_lock:
            row = connection.execute(
                """
                SELECT run_id, session_id, client_message_id, payload_digest,
                       config_revision, source, state, accepted_at,
                       enqueued_at, enqueue_seq, prompt, audit_prompt,
                       identity_json, source_detail_json, scheduled_task_id,
                       scheduled_occurrence_key, coalesced_count,
                       last_coalesced_at, history_message_count_before_run,
                       user_id, org_id, profile_id, model_id,
                       runtime_provider_run_id, cancellation_requested_at
                FROM runs
                WHERE session_id = ? AND client_message_id = ?
                """,
                (session_id, client_message_id),
            ).fetchone()
        return self._row_to_run(row, created=False) if row else None

    async def load_unfinished_runs(self) -> list[StoredRun]:
        connection = self._require_connection()
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        async with self._connection_lock:
            rows = connection.execute(
                f"""
                SELECT run_id, session_id, client_message_id, payload_digest,
                       config_revision, source, state, accepted_at,
                       enqueued_at, enqueue_seq, prompt, audit_prompt,
                       identity_json, source_detail_json, scheduled_task_id,
                       scheduled_occurrence_key, coalesced_count,
                       last_coalesced_at, history_message_count_before_run,
                       user_id, org_id, profile_id, model_id,
                       runtime_provider_run_id, cancellation_requested_at
                FROM runs
                WHERE state NOT IN ({placeholders})
                ORDER BY enqueue_seq, run_id
                """,
                tuple(sorted(TERMINAL_STATES)),
            ).fetchall()
        return [self._row_to_run(row, created=False) for row in rows]

    async def load_run_metadata(self, run_id: str) -> dict[str, object] | None:
        """Read restart verification metadata without loading event payloads."""
        connection = self._require_connection()
        async with self._connection_lock:
            row = connection.execute(
                """
                SELECT run_id, session_id, state, answer_through_seq,
                       (SELECT COUNT(*) FROM run_events e WHERE e.run_id = runs.run_id)
                FROM runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": str(row["run_id"]),
            "session_id": str(row["session_id"]),
            "status": str(row["state"]),
            "through_seq": max(int(row["answer_through_seq"] or 0), int(row[4] or 0)),
            "event_count": int(row[4] or 0),
        }

    async def interrupt_unfinished_runs_for_restart(
        self,
        *,
        interrupted_at: datetime,
    ) -> list[dict[str, str]]:
        """Atomically terminalize all non-terminal Runs for a restart."""

        if interrupted_at.tzinfo is None:
            raise ValueError("interrupted_at must be timezone-aware")
        return await self._submit_mutation(
            "run_restart_interrupt",
            self._utc_iso(interrupted_at),
        )

    async def transition_run(
        self,
        run_id: str,
        state: str,
        *,
        acknowledged_at: datetime | None = None,
        terminal_at: datetime | None = None,
    ) -> StoredRun:
        return await self._submit_mutation(
            "run_state",
            run_id,
            state,
            self._utc_iso(acknowledged_at),
            self._utc_iso(terminal_at),
        )

    async def set_runtime_provider_run_id(
        self,
        run_id: str,
        runtime_provider_run_id: str,
    ) -> StoredRun:
        provider_id = str(runtime_provider_run_id or "").strip()
        if not provider_id:
            raise ValueError("runtime provider Run id must not be empty")
        return await self._submit_mutation(
            "run_provider",
            run_id,
            provider_id,
        )

    async def request_run_cancellation(
        self,
        run_id: str,
        *,
        requested_at: datetime,
    ) -> StoredRun:
        if requested_at.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware")
        return await self._submit_mutation(
            "run_cancel_request",
            run_id,
            self._utc_iso(requested_at),
        )

    async def append_run_event(
        self,
        event: StoredRunEvent,
        *,
        durable: bool,
    ) -> bool:
        if event.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        if event.seq <= 0:
            raise ValueError("event seq must be positive")
        if not durable:
            return False
        return await self._submit_mutation(
            "run_event",
            event.run_id,
            event.seq,
            event.event_type,
            self._json(event.payload),
            self._utc_iso(event.occurred_at),
        )

    async def save_answer_snapshot(
        self,
        run_id: str,
        *,
        answer: str,
        through_seq: int,
    ) -> bool:
        if through_seq < 0:
            raise ValueError("through_seq cannot be negative")
        return await self._submit_mutation(
            "answer_snapshot",
            run_id,
            answer,
            through_seq,
        )

    async def load_run_snapshot(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        max_events: int = 1000,
    ) -> RunSnapshot | None:
        if after_seq < 0:
            raise ValueError("after_seq cannot be negative")
        connection = self._require_connection()
        async with self._connection_lock:
            row = connection.execute(
                """
                SELECT run_id, session_id, client_message_id, payload_digest,
                       config_revision, source, state, accepted_at,
                       enqueued_at, enqueue_seq, prompt, audit_prompt,
                       identity_json, source_detail_json, scheduled_task_id,
                       scheduled_occurrence_key, coalesced_count,
                       last_coalesced_at, history_message_count_before_run,
                       answer_snapshot, answer_through_seq,
                       user_id, org_id, profile_id, model_id,
                       runtime_provider_run_id, cancellation_requested_at
                FROM runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            event_rows = connection.execute(
                """
                SELECT run_id, seq, event_type, payload_json, occurred_at
                FROM run_events
                WHERE run_id = ? AND seq > ?
                ORDER BY seq
                LIMIT ?
                """,
                (run_id, after_seq, max(1, min(max_events, 10_000))),
            ).fetchall()
        return RunSnapshot(
            run=self._row_to_run(row, created=False),
            answer=str(row["answer_snapshot"] or ""),
            answer_through_seq=int(row["answer_through_seq"] or 0),
            events=tuple(self._row_to_event(item) for item in event_rows),
        )

    async def next_run_event_seq(self, run_id: str) -> int:
        connection = self._require_connection()
        async with self._connection_lock:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0])

    async def upsert_session_envelope(
        self,
        *,
        session_id: str,
        prepare_identity_digest: str,
        state: str,
        last_activity_at: datetime,
        prepare_identity: dict[str, Any] | None = None,
    ) -> StoredSessionEnvelope:
        if last_activity_at.tzinfo is None:
            raise ValueError("last_activity_at must be timezone-aware")
        return await self._submit_mutation(
            "session",
            session_id,
            prepare_identity_digest,
            state,
            self._utc_iso(last_activity_at),
            self._json(prepare_identity) if prepare_identity is not None else None,
        )

    async def load_recent_sessions(
        self,
        *,
        active_since: datetime,
        limit: int,
    ) -> list[StoredSessionEnvelope]:
        if active_since.tzinfo is None:
            raise ValueError("active_since must be timezone-aware")
        connection = self._require_connection()
        async with self._connection_lock:
            rows = connection.execute(
                """
                SELECT session_id, prepare_identity_digest, state,
                       last_activity_at, prepare_identity_json
                FROM session_envelopes
                WHERE last_activity_at >= ?
                ORDER BY last_activity_at DESC, session_id
                LIMIT ?
                """,
                (self._utc_iso(active_since), max(0, min(limit, 10_000))),
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    async def get_session_envelope(
        self,
        session_id: str,
    ) -> StoredSessionEnvelope | None:
        connection = self._require_connection()
        async with self._connection_lock:
            row = connection.execute(
                """
                SELECT session_id, prepare_identity_digest, state,
                       last_activity_at, prepare_identity_json
                FROM session_envelopes
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._row_to_session(row) if row is not None else None

    async def persist_command(
        self,
        *,
        command_id: str,
        payload_digest: str,
        command_type: str,
        state: str = "accepted",
        result: dict[str, Any] | None = None,
    ) -> StoredCommand:
        return await self._submit_mutation(
            "command",
            command_id,
            payload_digest,
            command_type,
            state,
            self._json(result) if result is not None else None,
            self._utc_iso(datetime.now(timezone.utc)),
        )

    async def complete_command(
        self,
        *,
        command_id: str,
        state: str,
        result: dict[str, Any],
    ) -> StoredCommand:
        return await self._submit_mutation(
            "command_result",
            command_id,
            state,
            self._json(result),
            self._utc_iso(datetime.now(timezone.utc)),
        )

    async def abandon_command(
        self,
        *,
        command_id: str,
        payload_digest: str,
    ) -> bool:
        """Forget a command proven not to have crossed the admission boundary."""

        return await self._submit_mutation(
            "command_abandon",
            command_id,
            payload_digest,
        )

    async def enqueue_projection(
        self,
        *,
        projection_id: str,
        projection_type: str,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> bool:
        if created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return await self._submit_mutation(
            "outbox",
            projection_id,
            projection_type,
            self._json(payload),
            self._utc_iso(created_at),
        )

    async def load_projection_batch(
        self,
        *,
        max_items: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> list[StoredProjection]:
        connection = self._require_connection()
        async with self._connection_lock:
            rows = connection.execute(
                """
                SELECT outbox_id, projection_id, projection_type, payload_json,
                       created_at
                FROM projection_outbox
                WHERE acknowledged_at IS NULL
                ORDER BY outbox_id
                LIMIT ?
                """,
                (max(1, min(max_items, 100)),),
            ).fetchall()
        result: list[StoredProjection] = []
        size = 0
        for row in rows:
            row_size = len(str(row["payload_json"]).encode("utf-8"))
            if result and size + row_size > max_bytes:
                break
            size += row_size
            result.append(self._row_to_projection(row))
        return result

    async def acknowledge_projections(
        self,
        projection_ids: list[str],
        *,
        acknowledged_at: datetime,
    ) -> int:
        if acknowledged_at.tzinfo is None:
            raise ValueError("acknowledged_at must be timezone-aware")
        if not projection_ids:
            return 0
        return await self._submit_mutation(
            "outbox_ack",
            tuple(dict.fromkeys(projection_ids[:100])),
            self._utc_iso(acknowledged_at),
        )

    async def outbox_snapshot(self) -> dict[str, int]:
        connection = self._require_connection()
        async with self._connection_lock:
            row = connection.execute(
                """
                SELECT COUNT(*) AS pending_count,
                       MIN(created_at) AS oldest_created_at
                FROM projection_outbox
                WHERE acknowledged_at IS NULL
                """
            ).fetchone()
        pending_count = int(row["pending_count"] if row is not None else 0)
        oldest_age_seconds = 0
        if row is not None and row["oldest_created_at"]:
            oldest = datetime.fromisoformat(str(row["oldest_created_at"]))
            oldest_age_seconds = max(
                0,
                int((datetime.now(timezone.utc) - oldest).total_seconds()),
            )
        return {
            "pending_count": pending_count,
            "oldest_age_seconds": oldest_age_seconds,
            "writer_queue_depth": self._queue.qsize(),
        }

    async def acknowledge_channel_delivery(
        self,
        *,
        run_id: str,
        highest_contiguous_seq: int,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        provider_request_id: str,
        provider_message_id: str | None,
        projection_version: int,
        confirmed_at: datetime,
    ) -> StoredChannelDeliveryCheckpoint:
        if confirmed_at.tzinfo is None:
            raise ValueError("confirmed_at must be timezone-aware")
        if highest_contiguous_seq < 0 or channel_lease_epoch <= 0:
            raise ValueError("delivery checkpoint is invalid")
        return await self._submit_mutation(
            "delivery_ack",
            run_id,
            highest_contiguous_seq,
            gateway_instance_id,
            channel_lease_epoch,
            provider_request_id,
            provider_message_id,
            projection_version,
            self._utc_iso(confirmed_at),
        )

    async def get_channel_delivery_checkpoint(
        self,
        run_id: str,
    ) -> StoredChannelDeliveryCheckpoint | None:
        connection = self._require_connection()
        async with self._connection_lock:
            row = connection.execute(
                """
                SELECT run_id, highest_contiguous_seq, gateway_instance_id,
                       channel_lease_epoch, provider_request_id,
                       provider_message_id, projection_version, confirmed_at
                FROM channel_delivery_checkpoints
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return self._row_to_delivery_checkpoint(row) if row else None

    async def bind_channel_fast_lane_v2_run(
        self,
        *,
        delivery_id: str,
        run_id: str,
        payload_digest: str,
        bound_at: datetime,
    ) -> StoredChannelFastLaneV2Delivery:
        if bound_at.tzinfo is None:
            raise ValueError("bound_at must be timezone-aware")
        if not delivery_id:
            raise ValueError("delivery_id must not be empty")
        return await self._submit_mutation(
            "channel_v2_bind_run",
            delivery_id,
            run_id,
            payload_digest,
            self._utc_iso(bound_at),
        )

    async def bind_channel_fast_lane_v2_redirect(
        self,
        *,
        delivery_id: str,
        redirect_id: str,
        payload_digest: str,
        bound_at: datetime,
    ) -> StoredChannelFastLaneV2Delivery:
        if bound_at.tzinfo is None:
            raise ValueError("bound_at must be timezone-aware")
        if not delivery_id:
            raise ValueError("delivery_id must not be empty")
        return await self._submit_mutation(
            "channel_v2_bind_redirect",
            delivery_id,
            redirect_id,
            payload_digest,
            self._utc_iso(bound_at),
        )

    async def get_channel_fast_lane_v2_delivery(
        self,
        delivery_id: str,
    ) -> StoredChannelFastLaneV2Delivery | None:
        connection = self._require_connection()
        async with self._connection_lock:
            row = connection.execute(
                """
                SELECT * FROM channel_fast_lane_v2_deliveries
                WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
        return self._row_to_channel_fast_lane_v2_delivery(row) if row else None

    async def refresh_channel_fast_lane_v2_delivery(
        self,
        delivery_id: str,
        *,
        refreshed_at: datetime,
    ) -> StoredChannelFastLaneV2Delivery:
        if refreshed_at.tzinfo is None:
            raise ValueError("refreshed_at must be timezone-aware")
        return await self._submit_mutation(
            "channel_v2_refresh",
            delivery_id,
            self._utc_iso(refreshed_at),
        )

    async def acknowledge_channel_fast_lane_v2_disposition(
        self,
        *,
        delivery_id: str,
        disposition_seq: int,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        acknowledged_at: datetime,
    ) -> StoredChannelFastLaneV2Delivery:
        if acknowledged_at.tzinfo is None:
            raise ValueError("acknowledged_at must be timezone-aware")
        if disposition_seq <= 0 or channel_lease_epoch <= 0:
            raise ValueError("disposition acknowledgement is invalid")
        return await self._submit_mutation(
            "channel_v2_disposition_ack",
            delivery_id,
            disposition_seq,
            gateway_instance_id,
            channel_lease_epoch,
            self._utc_iso(acknowledged_at),
        )

    async def acknowledge_channel_fast_lane_v2_provider_delivery(
        self,
        *,
        owner_delivery_id: str,
        canonical_run_id: str,
        highest_contiguous_seq: int,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        provider_request_id: str,
        provider_message_id: str | None,
        projection_version: int,
        confirmed_at: datetime,
    ) -> StoredChannelFastLaneV2Checkpoint:
        if confirmed_at.tzinfo is None:
            raise ValueError("confirmed_at must be timezone-aware")
        if highest_contiguous_seq < 0 or channel_lease_epoch <= 0:
            raise ValueError("delivery checkpoint is invalid")
        return await self._submit_mutation(
            "channel_v2_provider_ack",
            owner_delivery_id,
            canonical_run_id,
            highest_contiguous_seq,
            gateway_instance_id,
            channel_lease_epoch,
            provider_request_id,
            provider_message_id,
            projection_version,
            self._utc_iso(confirmed_at),
        )

    def writer_metrics_snapshot(self) -> dict[str, int | float]:
        samples = sorted(self._commit_latency_ms)
        p95 = 0.0
        if samples:
            index = max(0, (95 * len(samples) + 99) // 100 - 1)
            p95 = samples[index]
        return {
            "commit_p95_ms": round(p95, 3),
            "commit_sample_count": len(samples),
            "writer_queue_depth": self._queue.qsize(),
        }

    async def record_revocation(
        self,
        *,
        command_id: str,
        scope: str,
        subject_id: str,
        revoked_at: datetime,
        cancel_active_runs: bool,
    ) -> bool:
        if revoked_at.tzinfo is None:
            raise ValueError("revoked_at must be timezone-aware")
        return await self._submit_mutation(
            "revocation",
            command_id,
            scope,
            subject_id,
            self._utc_iso(revoked_at),
            int(cancel_active_runs),
        )

    async def consume_file_transfer_ticket(
        self,
        *,
        ticket_id: str,
        operation: str,
        expires_at: int,
    ) -> bool:
        """Durably consume one signed upload/download ticket exactly once."""

        if not ticket_id or operation not in {"upload", "download"}:
            raise ValueError("file transfer ticket identity is invalid")
        return await self._submit_mutation(
            "file_ticket",
            ticket_id,
            operation,
            expires_at,
            int(time.time()),
            self._utc_iso(datetime.now(timezone.utc)),
        )

    async def is_revoked(
        self,
        *,
        user_id: int,
        model_id: str,
        application_id: str,
    ) -> bool:
        connection = self._require_connection()
        subjects = (
            ("user", str(user_id)),
            ("model", model_id),
            ("application", application_id),
        )
        async with self._connection_lock:
            return any(
                connection.execute(
                    """
                    SELECT 1 FROM revocations
                    WHERE scope = ? AND subject_id = ?
                    LIMIT 1
                    """,
                    subject,
                ).fetchone()
                is not None
                for subject in subjects
            )

    def _open_and_migrate(
        self,
        *,
        migration_timestamp: datetime | None = None,
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            connection.close()
            raise SQLiteStoreError("SQLite WAL mode is required")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS runtime_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_envelopes (
                session_id TEXT PRIMARY KEY,
                prepare_identity_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                prepare_identity_json TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                client_message_id TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                config_revision TEXT NOT NULL,
                source TEXT NOT NULL,
                state TEXT NOT NULL,
                accepted_at TEXT,
                enqueued_at TEXT NOT NULL,
                enqueue_seq INTEGER NOT NULL,
                prompt TEXT NOT NULL DEFAULT '',
                audit_prompt TEXT NOT NULL DEFAULT '',
                identity_json TEXT NOT NULL DEFAULT '{}',
                source_detail_json TEXT,
                scheduled_task_id INTEGER,
                scheduled_occurrence_key TEXT,
                coalesced_count INTEGER NOT NULL DEFAULT 0,
                last_coalesced_at TEXT,
                history_message_count_before_run INTEGER,
                user_id INTEGER NOT NULL DEFAULT 0,
                org_id INTEGER NOT NULL DEFAULT 0,
                profile_id TEXT NOT NULL DEFAULT '',
                model_id TEXT NOT NULL DEFAULT '',
                runtime_provider_run_id TEXT,
                cancellation_requested_at TEXT,
                acknowledged_at TEXT,
                terminal_at TEXT,
                answer_snapshot TEXT NOT NULL DEFAULT '',
                answer_through_seq INTEGER NOT NULL DEFAULT 0,
                UNIQUE(session_id, client_message_id)
            );
            CREATE TABLE IF NOT EXISTS run_events (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                PRIMARY KEY(run_id, seq),
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS internal_commands (
                command_id TEXT PRIMARY KEY,
                payload_digest TEXT NOT NULL,
                command_type TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projection_outbox (
                outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                projection_id TEXT NOT NULL UNIQUE,
                projection_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                acknowledged_at TEXT
            );
            CREATE TABLE IF NOT EXISTS projection_checkpoints (
                receiver TEXT PRIMARY KEY,
                checkpoint TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revocations (
                command_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                revoked_at TEXT NOT NULL,
                cancel_active_runs INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consumed_file_tickets (
                ticket_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                consumed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channel_delivery_checkpoints (
                run_id TEXT PRIMARY KEY,
                highest_contiguous_seq INTEGER NOT NULL,
                gateway_instance_id TEXT NOT NULL,
                channel_lease_epoch INTEGER NOT NULL,
                provider_request_id TEXT NOT NULL,
                provider_message_id TEXT,
                projection_version INTEGER NOT NULL,
                confirmed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );
            COMMIT;
            """
        )
        # A one-off field containment for pre-fix Relay IDs may have installed
        # this trigger on an existing PVC. Bounded IDs make it unnecessary;
        # remove it on the first startup of a fixed Supervisor.
        connection.execute(
            "DROP TRIGGER IF EXISTS "
            "platform_ack_overlong_run_metric_v1"
        )
        run_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        for column, definition in (
            ("enqueued_at", "TEXT"),
            ("enqueue_seq", "INTEGER"),
            ("prompt", "TEXT NOT NULL DEFAULT ''"),
            ("audit_prompt", "TEXT NOT NULL DEFAULT ''"),
            ("identity_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("source_detail_json", "TEXT"),
            ("scheduled_task_id", "INTEGER"),
            ("scheduled_occurrence_key", "TEXT"),
            ("coalesced_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_coalesced_at", "TEXT"),
            ("history_message_count_before_run", "INTEGER"),
            ("user_id", "INTEGER NOT NULL DEFAULT 0"),
            ("org_id", "INTEGER NOT NULL DEFAULT 0"),
            ("profile_id", "TEXT NOT NULL DEFAULT ''"),
            ("model_id", "TEXT NOT NULL DEFAULT ''"),
            ("runtime_provider_run_id", "TEXT"),
            ("cancellation_requested_at", "TEXT"),
        ):
            if column not in run_columns:
                connection.execute(
                    f"ALTER TABLE runs ADD COLUMN {column} {definition}"
                )
        connection.execute(
            "UPDATE runs SET enqueued_at = accepted_at "
            "WHERE enqueued_at IS NULL"
        )
        rows_missing_seq = connection.execute(
            "SELECT run_id FROM runs WHERE enqueue_seq IS NULL ORDER BY accepted_at, run_id"
        ).fetchall()
        next_seq_row = connection.execute(
            "SELECT COALESCE(MAX(enqueue_seq), 0) FROM runs"
        ).fetchone()
        next_seq = int(next_seq_row[0] or 0)
        for row in rows_missing_seq:
            next_seq += 1
            connection.execute(
                "UPDATE runs SET enqueue_seq = ? WHERE run_id = ?",
                (next_seq, str(row["run_id"])),
            )
        accepted_at_column = next(
            row
            for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            if str(row["name"]) == "accepted_at"
        )
        if int(accepted_at_column["notnull"]) == 1:
            connection.execute("PRAGMA foreign_keys=OFF")
            try:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE runs_v7 (
                        run_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        client_message_id TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        config_revision TEXT NOT NULL,
                        source TEXT NOT NULL,
                        state TEXT NOT NULL,
                        accepted_at TEXT,
                        enqueued_at TEXT NOT NULL,
                        enqueue_seq INTEGER NOT NULL,
                        prompt TEXT NOT NULL DEFAULT '',
                        audit_prompt TEXT NOT NULL DEFAULT '',
                        identity_json TEXT NOT NULL DEFAULT '{}',
                        source_detail_json TEXT,
                        scheduled_task_id INTEGER,
                        scheduled_occurrence_key TEXT,
                        coalesced_count INTEGER NOT NULL DEFAULT 0,
                        last_coalesced_at TEXT,
                        history_message_count_before_run INTEGER,
                        user_id INTEGER NOT NULL DEFAULT 0,
                        org_id INTEGER NOT NULL DEFAULT 0,
                        profile_id TEXT NOT NULL DEFAULT '',
                        model_id TEXT NOT NULL DEFAULT '',
                        runtime_provider_run_id TEXT,
                        cancellation_requested_at TEXT,
                        acknowledged_at TEXT,
                        terminal_at TEXT,
                        answer_snapshot TEXT NOT NULL DEFAULT '',
                        answer_through_seq INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(session_id, client_message_id)
                    );
                    INSERT INTO runs_v7(
                        run_id, session_id, client_message_id, payload_digest,
                        config_revision, source, state, accepted_at,
                        enqueued_at, enqueue_seq, prompt, audit_prompt,
                        identity_json, source_detail_json, scheduled_task_id,
                        scheduled_occurrence_key, coalesced_count,
                        last_coalesced_at, history_message_count_before_run,
                        user_id, org_id, profile_id, model_id,
                        runtime_provider_run_id, cancellation_requested_at,
                        acknowledged_at, terminal_at, answer_snapshot,
                        answer_through_seq
                    )
                    SELECT
                        run_id, session_id, client_message_id, payload_digest,
                        config_revision, source, state,
                        CASE WHEN state = 'QUEUED' THEN NULL ELSE accepted_at END,
                        enqueued_at, enqueue_seq, prompt, audit_prompt,
                        identity_json, source_detail_json, scheduled_task_id,
                        scheduled_occurrence_key, coalesced_count,
                        last_coalesced_at, history_message_count_before_run,
                        user_id, org_id, profile_id, model_id,
                        runtime_provider_run_id, cancellation_requested_at,
                        acknowledged_at, terminal_at, answer_snapshot,
                        answer_through_seq
                    FROM runs;
                    DROP TABLE runs;
                    ALTER TABLE runs_v7 RENAME TO runs;
                    COMMIT;
                    """
                )
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.execute("PRAGMA foreign_keys=ON")
            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_errors:
                raise SQLiteStoreError(
                    "SQLite Run migration failed foreign key validation"
                )
        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_one_active_per_session
            ON runs(session_id)
            WHERE state IN (
                'ADMITTING', 'DISPATCH_INTENT_DURABLE', 'ACKNOWLEDGED',
                'RUNNING', 'RECOVERING'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_one_queued_scheduled_task
            ON runs(session_id, scheduled_task_id)
            WHERE state = 'QUEUED' AND scheduled_task_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_runs_session_queue
            ON runs(session_id, state, enqueue_seq);
            CREATE INDEX IF NOT EXISTS ix_projection_outbox_pending
            ON projection_outbox(outbox_id)
            WHERE acknowledged_at IS NULL;
            """
        )
        session_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(session_envelopes)"
            ).fetchall()
        }
        if "prepare_identity_json" not in session_columns:
            connection.execute(
                "ALTER TABLE session_envelopes "
                "ADD COLUMN prepare_identity_json TEXT"
            )
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version > 9:
            connection.close()
            raise SQLiteStoreError("SQLite schema version is newer than supported")
        if user_version < 8:
            self._migrate_execution_plane_v8(
                connection,
                migration_timestamp=migration_timestamp,
            )
            user_version = 8
        if user_version < 9:
            self._finalize_execution_plane_v9(connection)
        return connection

    @classmethod
    def _migrate_execution_plane_v8(
        cls,
        connection: sqlite3.Connection,
        *,
        migration_timestamp: datetime | None = None,
    ) -> None:
        """Add execution-plane redirect ledgers without rewriting v7 authority."""

        protected_names = (
            "runs",
            "run_events",
            "projection_outbox",
            "projection_checkpoints",
            "channel_delivery_checkpoints",
        )
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        protected = tuple(name for name in protected_names if name in existing)
        before_digest = cls._canonical_tables_digest(connection, protected)
        now = cls._utc_iso(migration_timestamp or datetime.now(timezone.utc))
        assert now is not None

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS input_admission_counter (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    last_seq INTEGER NOT NULL CHECK(last_seq >= 0)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO input_admission_counter(singleton, last_seq)
                SELECT 1, (SELECT COALESCE(MAX(enqueue_seq), 0) FROM runs)
                WHERE NOT EXISTS (
                    SELECT 1 FROM input_admission_counter WHERE singleton = 1
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS input_admission_reservations (
                    input_admission_seq INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('ordinary_run', 'redirect')),
                    run_id TEXT,
                    redirect_id TEXT,
                    client_message_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(
                        state IN ('reserved', 'bound', 'promoted', 'completed')
                    ),
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, client_message_id),
                    UNIQUE(run_id),
                    UNIQUE(redirect_id),
                    CHECK(
                        (kind = 'ordinary_run' AND run_id IS NOT NULL
                         AND redirect_id IS NULL)
                        OR
                        (kind = 'redirect' AND run_id IS NULL
                         AND redirect_id IS NOT NULL)
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_redirects (
                    redirect_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    client_message_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    parent_run_id TEXT NOT NULL,
                    input_admission_seq INTEGER NOT NULL UNIQUE,
                    interaction_origin TEXT NOT NULL CHECK(
                        interaction_origin IN (
                            'user_web', 'application_studio',
                            'wecom_personal', 'wecom_application'
                        )
                    ),
                    content TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(
                        state IN (
                            'WAITING_AGENT_READY', 'WAITING_REDIRECT_BOUNDARY',
                            'DISPATCH_INTENT', 'RPC_ACKNOWLEDGED',
                            'ISOLATING_UNCERTAIN', 'CANONICAL_CONSUMED',
                            'PROMOTED_TO_RUN', 'SUBMISSION_UNCERTAIN',
                            'CANCELED_WITH_PARENT'
                        )
                    ),
                    hermes_status TEXT CHECK(
                        hermes_status IS NULL
                        OR hermes_status IN ('redirected', 'queued', 'rejected')
                    ),
                    dispatch_group_id TEXT,
                    dispatch_group_position INTEGER,
                    pending_event_seq INTEGER NOT NULL,
                    accepted_event_seq INTEGER,
                    consumed_event_seq INTEGER,
                    terminal_disposition_kind TEXT CHECK(
                        terminal_disposition_kind IS NULL
                        OR terminal_disposition_kind IN (
                            'promoted', 'canceled', 'uncertain'
                        )
                    ),
                    terminal_disposition_event_seq INTEGER,
                    start_after_seq INTEGER,
                    delivery_id TEXT,
                    promoted_run_id TEXT,
                    promotion_reserved_run_id TEXT NOT NULL,
                    promotion_run_intent_json TEXT NOT NULL,
                    promotion_run_payload_digest TEXT NOT NULL,
                    promoted_pre_prompt_message_id INTEGER,
                    promoted_pre_prompt_history_digest TEXT,
                    promoted_prompt_wire_digest TEXT,
                    promoted_proof_message_id INTEGER,
                    promoted_proof_post_digest TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_id, client_message_id),
                    CHECK(
                        (dispatch_group_id IS NULL
                         AND dispatch_group_position IS NULL)
                        OR
                        (dispatch_group_id IS NOT NULL
                         AND dispatch_group_position IS NOT NULL
                         AND dispatch_group_position >= 0)
                    ),
                    CHECK(
                        (terminal_disposition_kind IS NULL
                         AND terminal_disposition_event_seq IS NULL)
                        OR
                        (terminal_disposition_kind IS NOT NULL
                         AND terminal_disposition_event_seq IS NOT NULL)
                    ),
                    FOREIGN KEY(parent_run_id) REFERENCES runs(run_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_redirect_dispatch_groups (
                    dispatch_group_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    parent_run_id TEXT NOT NULL,
                    aggregate_digest TEXT NOT NULL,
                    raw_aggregate_digest TEXT NOT NULL,
                    wire_text TEXT NOT NULL,
                    wire_text_digest TEXT NOT NULL,
                    member_count INTEGER NOT NULL CHECK(member_count > 0),
                    hermes_profile_key TEXT,
                    hermes_stored_session_id TEXT,
                    pre_dispatch_message_id INTEGER,
                    pre_dispatch_history_digest TEXT,
                    pre_dispatch_model_request_seq INTEGER,
                    pre_dispatch_model_request_digest TEXT,
                    dispatch_turn_id TEXT,
                    dispatch_turn_task_id TEXT,
                    dispatch_origin_evidence_digest TEXT,
                    dispatch_boundary_epoch_nonce TEXT,
                    dispatch_marker_request_nonce TEXT,
                    dispatch_peer_boot_id TEXT,
                    dispatch_peer_connection_id TEXT,
                    proof_message_id INTEGER,
                    proof_kind TEXT CHECK(
                        proof_kind IS NULL OR proof_kind = 'correction_user_row'
                    ),
                    proof_post_digest TEXT,
                    state TEXT NOT NULL CHECK(
                        state IN (
                            'PREPARED', 'DISPATCH_INTENT', 'RPC_ACKNOWLEDGED',
                            'ISOLATING_UNCERTAIN', 'CANONICAL_CONSUMED',
                            'PROMOTED_TO_RUN', 'SUBMISSION_UNCERTAIN',
                            'CANCELED_WITH_PARENT'
                        )
                    ),
                    hermes_status TEXT CHECK(
                        hermes_status IS NULL
                        OR hermes_status IN ('redirected', 'queued', 'rejected')
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(parent_run_id) REFERENCES runs(run_id)
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_redirect_group_one_unresolved_parent
                ON run_redirect_dispatch_groups(parent_run_id)
                WHERE state IN (
                    'PREPARED', 'DISPATCH_INTENT', 'RPC_ACKNOWLEDGED',
                    'ISOLATING_UNCERTAIN'
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_redirects_session_admission
                ON run_redirects(session_id, input_admission_seq)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_input_admissions_session
                ON input_admission_reservations(
                    session_id, state, input_admission_seq
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS redirect_event_fences (
                    dispatch_group_id TEXT PRIMARY KEY,
                    parent_run_id TEXT NOT NULL,
                    pre_fence_event_seq INTEGER NOT NULL,
                    pre_fence_token_snapshot_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'RELEASE_READY')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(dispatch_group_id)
                        REFERENCES run_redirect_dispatch_groups(dispatch_group_id),
                    FOREIGN KEY(parent_run_id) REFERENCES runs(run_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS redirect_event_fence_snapshots (
                    dispatch_group_id TEXT PRIMARY KEY,
                    snapshot_generation INTEGER NOT NULL,
                    observed_order_high_water INTEGER NOT NULL,
                    assistant_snapshot TEXT NOT NULL,
                    reasoning_snapshot TEXT NOT NULL,
                    tool_state_snapshot TEXT NOT NULL,
                    semantic_frames_snapshot TEXT NOT NULL,
                    terminal_payload TEXT,
                    terminal_latched INTEGER NOT NULL CHECK(
                        terminal_latched IN (0, 1)
                    ),
                    payload_digest TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(dispatch_group_id)
                        REFERENCES redirect_event_fences(dispatch_group_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_fast_lane_v2_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    client_message_id TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    target_kind TEXT NOT NULL CHECK(
                        target_kind IN ('ordinary_run', 'redirect')
                    ),
                    run_id TEXT,
                    redirect_id TEXT,
                    canonical_run_id TEXT NOT NULL,
                    parent_run_id TEXT,
                    disposition_seq INTEGER NOT NULL CHECK(disposition_seq > 0),
                    disposition_state TEXT NOT NULL CHECK(
                        disposition_state IN (
                            'pending', 'ordinary_run_accepted',
                            'redirect_accepted', 'redirect_consumed',
                            'promoted', 'uncertain', 'canceled'
                        )
                    ),
                    start_after_seq INTEGER CHECK(start_after_seq >= 0),
                    promoted_run_id TEXT,
                    disposition_ack_seq INTEGER NOT NULL DEFAULT 0 CHECK(
                        disposition_ack_seq >= 0
                        AND disposition_ack_seq <= disposition_seq
                    ),
                    disposition_ack_gateway_instance_id TEXT,
                    disposition_ack_channel_lease_epoch INTEGER,
                    disposition_acked_at TEXT,
                    provider_confirmed_seq INTEGER NOT NULL DEFAULT 0 CHECK(
                        provider_confirmed_seq >= 0
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_id, client_message_id),
                    UNIQUE(run_id),
                    UNIQUE(redirect_id),
                    CHECK(
                        (target_kind = 'ordinary_run' AND run_id IS NOT NULL
                         AND redirect_id IS NULL)
                        OR
                        (target_kind = 'redirect' AND run_id IS NULL
                         AND redirect_id IS NOT NULL)
                    ),
                    CHECK(
                        (target_kind = 'ordinary_run'
                         AND disposition_state = 'ordinary_run_accepted'
                         AND parent_run_id IS NULL AND start_after_seq = 0
                         AND promoted_run_id IS NULL
                         AND canonical_run_id = run_id)
                        OR
                        (target_kind = 'redirect'
                         AND disposition_state IN (
                            'redirect_accepted', 'redirect_consumed'
                         ) AND parent_run_id IS NOT NULL
                         AND start_after_seq > 0
                         AND promoted_run_id IS NULL
                         AND canonical_run_id = parent_run_id)
                        OR
                        (target_kind = 'redirect'
                         AND disposition_state = 'promoted'
                         AND parent_run_id IS NOT NULL
                         AND start_after_seq = 0
                         AND promoted_run_id IS NOT NULL
                         AND canonical_run_id = promoted_run_id)
                        OR
                        (target_kind = 'redirect'
                         AND disposition_state = 'uncertain'
                         AND parent_run_id IS NOT NULL
                         AND (start_after_seq IS NULL OR start_after_seq > 0)
                         AND promoted_run_id IS NULL
                         AND canonical_run_id = parent_run_id)
                        OR
                        (target_kind = 'redirect'
                         AND disposition_state IN ('pending', 'canceled')
                         AND parent_run_id IS NOT NULL
                         AND start_after_seq IS NULL
                         AND promoted_run_id IS NULL
                         AND canonical_run_id = parent_run_id)
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_channel_fast_lane_v2_owner
                ON channel_fast_lane_v2_deliveries(
                    canonical_run_id, start_after_seq, delivery_id
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_fast_lane_v2_checkpoints (
                    canonical_run_id TEXT PRIMARY KEY,
                    owner_delivery_id TEXT NOT NULL,
                    highest_contiguous_seq INTEGER NOT NULL CHECK(
                        highest_contiguous_seq >= 0
                    ),
                    gateway_instance_id TEXT NOT NULL,
                    channel_lease_epoch INTEGER NOT NULL CHECK(
                        channel_lease_epoch > 0
                    ),
                    provider_request_id TEXT NOT NULL,
                    provider_message_id TEXT,
                    projection_version INTEGER NOT NULL CHECK(
                        projection_version > 0
                    ),
                    confirmed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            counter = connection.execute(
                "SELECT last_seq FROM input_admission_counter WHERE singleton = 1"
            ).fetchone()
            maximum = connection.execute(
                "SELECT COALESCE(MAX(enqueue_seq), 0) FROM runs"
            ).fetchone()
            if counter is None or int(counter["last_seq"]) < int(maximum[0]):
                raise SQLiteStoreError("input admission counter is inconsistent")

            after_digest = cls._canonical_tables_digest(connection, protected)
            if after_digest != before_digest:
                raise SQLiteStoreError(
                    "execution-plane schema migration changed existing authority"
                )
            connection.execute(
                """
                INSERT INTO runtime_meta(key, value, updated_at)
                VALUES ('execution_plane_v8_authority_digest', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                WHERE runtime_meta.value <> excluded.value
                """,
                (after_digest, now),
            )
            connection.execute("PRAGMA user_version=8")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _finalize_execution_plane_v9(connection: sqlite3.Connection) -> None:
        """Advance the schema marker without creating retired upgrade ledgers."""

        connection.execute("PRAGMA user_version=9")

    @classmethod
    def _canonical_tables_digest(
        cls,
        connection: sqlite3.Connection,
        table_names: tuple[str, ...],
    ) -> str:
        projection: list[dict[str, Any]] = []
        for table_name in sorted(table_names):
            columns = connection.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
            column_names = [str(row["name"]) for row in columns]
            primary_key = [
                str(item["name"])
                for item in sorted(columns, key=lambda item: int(item["pk"]))
                if int(item["pk"]) > 0
            ]
            order_by = primary_key or column_names
            quoted_columns = ", ".join(f'"{name}"' for name in column_names)
            quoted_order = ", ".join(f'"{name}"' for name in order_by)
            rows = connection.execute(
                f'SELECT {quoted_columns} FROM "{table_name}" '
                f"ORDER BY {quoted_order}"
            ).fetchall()
            projection.append(
                {
                    "table": table_name,
                    "columns": column_names,
                    "rows": [
                        [row[name] for name in column_names]
                        for row in rows
                    ],
                }
            )
        raw = cls._json(projection).encode("utf-8")
        return f"sha256:{hashlib.sha256(raw).hexdigest()}"

    async def _writer_loop(self) -> None:
        pending: _DispatchWrite | _MutationWrite | object | None = None
        while True:
            first = pending if pending is not None else await self._queue.get()
            pending = None
            if first is _STOP:
                return
            if isinstance(first, _MutationWrite):
                try:
                    async with self._connection_lock:
                        started_at = time.perf_counter()
                        result = self._execute_mutation(
                            first.operation,
                            first.values,
                        )
                        self._record_commit_latency(started_at)
                except BaseException as exc:
                    if not first.future.done():
                        first.future.set_exception(exc)
                else:
                    if not first.future.done():
                        first.future.set_result(result)
                continue
            assert isinstance(first, _DispatchWrite)
            writes = [first]
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.group_commit_window_seconds
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if item is _STOP:
                    pending = _STOP
                    break
                if isinstance(item, _MutationWrite):
                    pending = item
                    break
                assert isinstance(item, _DispatchWrite)
                writes.append(item)
            try:
                async with self._connection_lock:
                    started_at = time.perf_counter()
                    outcomes = self._commit_dispatch_batch(
                        [write.intent for write in writes]
                    )
                    self._record_commit_latency(started_at)
            except BaseException as exc:
                for write in writes:
                    if not write.future.done():
                        write.future.set_exception(exc)
                continue
            for write, outcome in zip(writes, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    write.future.set_exception(outcome)
                else:
                    write.future.set_result(outcome)

    def _record_commit_latency(self, started_at: float) -> None:
        self._commit_latency_ms.append(
            max(0.0, (time.perf_counter() - started_at) * 1000)
        )

    async def _submit_mutation(
        self,
        operation: MutationOperation,
        *values: Any,
    ) -> Any:
        self._require_connection()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put(
            _MutationWrite(
                operation=operation,
                values=values,
                future=future,
            )
        )
        return await future

    def _execute_mutation(
        self,
        operation: str,
        values: tuple[Any, ...],
    ) -> Any:
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            if operation == "run_enqueue":
                result = self._enqueue_run_sync(connection, *values)
            elif operation == "run_claim":
                result = self._claim_next_queued_sync(connection, *values)
            elif operation == "run_requeue":
                result = self._requeue_admitting_sync(connection, *values)
            elif operation == "run_history_checkpoint":
                result = self._record_run_history_checkpoint_sync(
                    connection, *values
                )
            elif operation == "run_cancel_queued_scheduled":
                result = self._cancel_queued_scheduled_task_sync(
                    connection, *values
                )
            elif operation == "run_cancel_queued":
                result = self._cancel_queued_run_sync(connection, *values)
            elif operation == "run_state":
                result = self._transition_run_sync(connection, *values)
            elif operation == "run_provider":
                result = self._set_run_provider_sync(connection, *values)
            elif operation == "run_cancel_request":
                result = self._request_run_cancellation_sync(
                    connection,
                    *values,
                )
            elif operation == "run_restart_interrupt":
                result = self._interrupt_unfinished_runs_for_restart_sync(
                    connection,
                    *values,
                )
            elif operation == "run_event":
                result = self._append_run_event_sync(connection, *values)
            elif operation == "answer_snapshot":
                result = self._save_answer_snapshot_sync(connection, *values)
            elif operation == "session":
                result = self._upsert_session_sync(connection, *values)
            elif operation == "command":
                result = self._persist_command_sync(connection, *values)
            elif operation == "command_result":
                result = self._complete_command_sync(connection, *values)
            elif operation == "command_abandon":
                result = self._abandon_command_sync(connection, *values)
            elif operation == "outbox":
                result = self._enqueue_projection_sync(connection, *values)
            elif operation == "outbox_ack":
                result = self._acknowledge_projections_sync(connection, *values)
            elif operation == "revocation":
                result = self._record_revocation_sync(connection, *values)
            elif operation == "file_ticket":
                result = self._consume_file_ticket_sync(connection, *values)
            elif operation == "delivery_ack":
                result = self._acknowledge_channel_delivery_sync(
                    connection,
                    *values,
                )
            elif operation == "channel_v2_bind_run":
                result = self._bind_channel_fast_lane_v2_run_sync(
                    connection,
                    *values,
                )
            elif operation == "channel_v2_bind_redirect":
                result = self._bind_channel_fast_lane_v2_redirect_sync(
                    connection,
                    *values,
                )
            elif operation == "channel_v2_refresh":
                result = self._refresh_channel_fast_lane_v2_delivery_sync(
                    connection,
                    *values,
                )
            elif operation == "channel_v2_disposition_ack":
                result = self._acknowledge_channel_fast_lane_v2_disposition_sync(
                    connection,
                    *values,
                )
            elif operation == "channel_v2_provider_ack":
                result = self._acknowledge_channel_fast_lane_v2_provider_sync(
                    connection,
                    *values,
                )
            elif operation == "redirect_admit":
                result = self._admit_redirect_sync(connection, *values)
            elif operation == "redirect_boundary_ready":
                result = self._mark_redirect_boundary_ready_sync(
                    connection,
                    *values,
                )
            elif operation == "redirect_group_prepare":
                result = self._prepare_redirect_group_sync(connection, *values)
            elif operation == "redirect_group_dispatch":
                result = self._record_redirect_dispatch_intent_sync(
                    connection,
                    *values,
                )
            elif operation == "redirect_group_pre_rpc_rollback":
                result = self._rollback_pre_rpc_redirect_group_sync(
                    connection,
                    *values,
                )
            elif operation == "redirect_group_ack":
                result = self._acknowledge_redirect_group_sync(
                    connection,
                    *values,
                )
            elif operation == "redirect_group_isolate":
                result = self._isolate_redirect_group_sync(
                    connection,
                    *values,
                )
            elif operation == "redirect_group_uncertain":
                result = self._finalize_redirect_group_uncertain_sync(
                    connection,
                    *values,
                )
            elif operation == "redirect_group_failed":
                result = self._finalize_redirect_group_failed_sync(
                    connection,
                    *values,
                )
            elif operation == "redirect_group_consumed":
                result = self._consume_redirect_group_sync(
                    connection,
                    *values,
                )
            elif operation == "redirect_event_fence_release_complete":
                result = self._complete_redirect_event_fence_release_sync(
                    connection,
                    *values,
                )
            elif operation == "redirect_group_promote":
                result = self._promote_redirect_group_sync(
                    connection,
                    *values,
                )
            elif operation == "redirect_parent_cancel":
                result = self._cancel_pre_rpc_redirects_for_parent_sync(
                    connection,
                    *values,
                )
            else:
                raise SQLiteStoreError(f"unknown writer operation: {operation}")
            connection.execute("COMMIT")
            return result
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @classmethod
    def _reserve_input_admission_sync(
        cls,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        client_message_id: str,
        kind: str,
        run_id: str | None,
        redirect_id: str | None,
        created_at: str,
        state: str,
    ) -> int:
        existing = connection.execute(
            """
            SELECT input_admission_seq, kind, run_id, redirect_id, state
            FROM input_admission_reservations
            WHERE session_id = ? AND client_message_id = ?
            """,
            (session_id, client_message_id),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["kind"]) != kind
                or existing["run_id"] != run_id
                or existing["redirect_id"] != redirect_id
            ):
                raise IdempotencyConflict(
                    "client_message_id has a conflicting admission reservation"
                )
            return int(existing["input_admission_seq"])

        row = connection.execute(
            "SELECT last_seq FROM input_admission_counter WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise SQLiteStoreError("input admission counter is missing")
        input_admission_seq = int(row["last_seq"]) + 1
        cursor = connection.execute(
            """
            UPDATE input_admission_counter SET last_seq = ?
            WHERE singleton = 1 AND last_seq = ?
            """,
            (input_admission_seq, input_admission_seq - 1),
        )
        if cursor.rowcount != 1:
            raise SQLiteStoreError("input admission counter CAS failed")
        connection.execute(
            """
            INSERT INTO input_admission_reservations(
                input_admission_seq, session_id, kind, run_id, redirect_id,
                client_message_id, state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                input_admission_seq,
                session_id,
                kind,
                run_id,
                redirect_id,
                client_message_id,
                state,
                created_at,
            ),
        )
        return input_admission_seq

    @classmethod
    def _admit_redirect_sync(
        cls,
        connection: sqlite3.Connection,
        intent: RedirectAdmissionIntent,
    ) -> StoredRedirect:
        existing = connection.execute(
            """
            SELECT * FROM run_redirects
            WHERE session_id = ? AND client_message_id = ?
            """,
            (intent.session_id, intent.client_message_id),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["request_digest"]) != intent.request_digest
                or str(existing["promotion_reserved_run_id"])
                != intent.promotion_reserved_run_id
                or str(existing["promotion_run_payload_digest"])
                != intent.promotion_run_payload_digest
            ):
                raise IdempotencyConflict(
                    "redirect client_message_id is bound to a different request"
                )
            if intent.delivery_id is not None:
                cls._bind_channel_fast_lane_v2_redirect_sync(
                    connection,
                    intent.delivery_id,
                    str(existing["redirect_id"]),
                    str(intent.promotion_run_intent["surface_payload_digest"]),
                    cls._utc_iso(intent.created_at) or "",
                )
            return cls._row_to_redirect(existing, created=False)

        ordinary = connection.execute(
            """
            SELECT run_id, payload_digest FROM runs
            WHERE session_id = ? AND client_message_id = ?
            """,
            (intent.session_id, intent.client_message_id),
        ).fetchone()
        if ordinary is not None:
            raise IdempotencyConflict(
                "client_message_id is already bound to an ordinary Run"
            )
        cls._require_new_admission_open_sync(
            connection,
            admission_generation=intent.admission_generation,
        )
        parent = connection.execute(
            """
            SELECT session_id, state, cancellation_requested_at
            FROM runs WHERE run_id = ?
            """,
            (intent.parent_run_id,),
        ).fetchone()
        if parent is None or str(parent["session_id"]) != intent.session_id:
            raise SQLiteStoreError("redirect parent Run is not in the Session")
        parent_state = str(parent["state"])
        if (
            parent_state not in REDIRECT_PARENT_STATES
            and not (
                intent.allow_terminal_parent
                and parent_state in TERMINAL_STATES
            )
        ) or (
            parent["cancellation_requested_at"] is not None
            and parent_state not in TERMINAL_STATES
        ):
            raise SQLiteStoreError("redirect parent Run is not active")

        created_at = cls._utc_iso(intent.created_at)
        assert created_at is not None
        input_admission_seq = cls._reserve_input_admission_sync(
            connection,
            session_id=intent.session_id,
            client_message_id=intent.client_message_id,
            kind="redirect",
            run_id=None,
            redirect_id=intent.redirect_id,
            created_at=created_at,
            state="bound",
        )
        event_row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?",
            (intent.parent_run_id,),
        ).fetchone()
        pending_event_seq = int(event_row[0])
        promotion_run_intent_json = cls._json(intent.promotion_run_intent)
        connection.execute(
            """
            INSERT INTO run_redirects(
                redirect_id, session_id, client_message_id, request_digest,
                parent_run_id, input_admission_seq, interaction_origin,
                content, state, hermes_status, dispatch_group_id,
                dispatch_group_position, pending_event_seq,
                accepted_event_seq, consumed_event_seq,
                terminal_disposition_kind, terminal_disposition_event_seq,
                start_after_seq, delivery_id, promoted_run_id,
                promotion_reserved_run_id, promotion_run_intent_json,
                promotion_run_payload_digest,
                promoted_pre_prompt_message_id,
                promoted_pre_prompt_history_digest,
                promoted_prompt_wire_digest, promoted_proof_message_id,
                promoted_proof_post_digest, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL,
                NULL, NULL, NULL, ?, NULL, ?, ?, ?, NULL, NULL, NULL, NULL,
                NULL, ?, ?
            )
            """,
            (
                intent.redirect_id,
                intent.session_id,
                intent.client_message_id,
                intent.request_digest,
                intent.parent_run_id,
                input_admission_seq,
                intent.interaction_origin,
                intent.content,
                intent.initial_state,
                pending_event_seq,
                intent.delivery_id,
                intent.promotion_reserved_run_id,
                promotion_run_intent_json,
                intent.promotion_run_payload_digest,
                created_at,
                created_at,
            ),
        )
        event_payload = {
            "followup_id": intent.redirect_id,
            "client_message_id": intent.client_message_id,
            "parent_run_id": intent.parent_run_id,
            "input_admission_seq": input_admission_seq,
            "dispatch_group_id": None,
            "dispatch_group_position": None,
            "event_seq": pending_event_seq,
            "start_after_seq": None,
            "content": intent.content,
            "interaction_origin": intent.interaction_origin,
        }
        inserted = cls._append_run_event_sync(
            connection,
            intent.parent_run_id,
            pending_event_seq,
            "followup.received",
            cls._json(event_payload),
            created_at,
        )
        if not inserted:
            raise SQLiteStoreError("redirect pending event seq conflicts")
        cls._enqueue_projection_sync(
            connection,
            f"{intent.parent_run_id}:followup:{intent.redirect_id}:received",
            "followup.received",
            cls._json(event_payload),
            created_at,
        )
        if intent.delivery_id is not None:
            cls._bind_channel_fast_lane_v2_redirect_sync(
                connection,
                intent.delivery_id,
                intent.redirect_id,
                str(intent.promotion_run_intent["surface_payload_digest"]),
                created_at,
            )
        row = connection.execute(
            "SELECT * FROM run_redirects WHERE redirect_id = ?",
            (intent.redirect_id,),
        ).fetchone()
        assert row is not None
        return cls._row_to_redirect(row, created=True)

    @classmethod
    def _mark_redirect_boundary_ready_sync(
        cls,
        connection: sqlite3.Connection,
        redirect_id: str,
        updated_at: str,
    ) -> StoredRedirect:
        row = connection.execute(
            "SELECT * FROM run_redirects WHERE redirect_id = ?",
            (redirect_id,),
        ).fetchone()
        if row is None:
            raise SQLiteStoreError("redirect does not exist")
        state = str(row["state"])
        if state == "WAITING_AGENT_READY":
            event_seq = cls._next_run_event_seq_sync(
                connection,
                str(row["parent_run_id"]),
            )
            cls._append_redirect_disposition_event_sync(
                connection,
                row,
                event_type="followup.applying",
                event_seq=event_seq,
                start_after_seq=event_seq,
                occurred_at=updated_at,
            )
            connection.execute(
                """
                UPDATE run_redirects
                SET state = 'WAITING_REDIRECT_BOUNDARY',
                    accepted_event_seq = ?, start_after_seq = ?, updated_at = ?
                WHERE redirect_id = ? AND state = 'WAITING_AGENT_READY'
                """,
                (event_seq, event_seq, updated_at, redirect_id),
            )
        elif state != "WAITING_REDIRECT_BOUNDARY":
            raise SQLiteStoreError("redirect is no longer waiting for a boundary")
        full = connection.execute(
            "SELECT * FROM run_redirects WHERE redirect_id = ?",
            (redirect_id,),
        ).fetchone()
        assert full is not None
        return cls._row_to_redirect(full, created=False)

    @classmethod
    def _prepare_redirect_group_sync(
        cls,
        connection: sqlite3.Connection,
        dispatch_group_id: str,
        parent_run_id: str,
        created_at: str,
        max_members: int,
        max_wire_bytes: int,
        allow_canceling_parent: bool = False,
        allow_terminal_parent: bool = False,
    ) -> StoredRedirectDispatchGroup | None:
        existing_id = connection.execute(
            """
            SELECT * FROM run_redirect_dispatch_groups
            WHERE dispatch_group_id = ?
            """,
            (dispatch_group_id,),
        ).fetchone()
        if existing_id is not None:
            if str(existing_id["parent_run_id"]) != parent_run_id:
                raise IdempotencyConflict(
                    "dispatch group id is bound to another parent Run"
                )
            return cls._row_to_redirect_group(existing_id)
        unresolved = connection.execute(
            """
            SELECT dispatch_group_id FROM run_redirect_dispatch_groups
            WHERE parent_run_id = ? AND state IN (
                'PREPARED', 'DISPATCH_INTENT', 'RPC_ACKNOWLEDGED',
                'ISOLATING_UNCERTAIN'
            )
            """,
            (parent_run_id,),
        ).fetchone()
        if unresolved is not None:
            raise SQLiteStoreError("parent Run already has an unresolved group")
        parent = connection.execute(
            """
            SELECT session_id, enqueue_seq, state, cancellation_requested_at
            FROM runs WHERE run_id = ?
            """,
            (parent_run_id,),
        ).fetchone()
        if parent is None:
            raise SQLiteStoreError("redirect parent Run does not exist")
        parent_state = str(parent["state"])
        if (
            parent_state not in REDIRECT_PARENT_STATES
            and not (
                allow_canceling_parent
                and parent_state == RECOVERING
            )
            and not (
                allow_terminal_parent
                and parent_state in TERMINAL_STATES
            )
        ) or (
            parent["cancellation_requested_at"] is not None
            and parent_state not in TERMINAL_STATES
            and not allow_canceling_parent
        ):
            raise SQLiteStoreError("redirect parent Run cannot accept a group")
        session_id = str(parent["session_id"])
        reservations = connection.execute(
            """
            SELECT input_admission_seq, kind, run_id, redirect_id, state
            FROM input_admission_reservations
            WHERE session_id = ? AND input_admission_seq > ?
              AND state <> 'completed'
            ORDER BY input_admission_seq
            """,
            (session_id, int(parent["enqueue_seq"])),
        ).fetchall()
        members: list[sqlite3.Row] = []
        for reservation in reservations:
            if str(reservation["kind"]) != "redirect":
                scheduled = connection.execute(
                    """
                    SELECT scheduled_task_id FROM runs
                    WHERE run_id = ? AND state = 'QUEUED'
                    """,
                    (str(reservation["run_id"] or ""),),
                ).fetchone()
                if scheduled is not None and scheduled["scheduled_task_id"] is not None:
                    continue
                break
            member = connection.execute(
                """
                SELECT redirect_id, request_digest, content,
                       input_admission_seq, state, dispatch_group_id
                FROM run_redirects WHERE redirect_id = ?
                """,
                (str(reservation["redirect_id"]),),
            ).fetchone()
            if (
                str(reservation["state"]) == "promoted"
                and member is not None
                and str(member["state"]) == "PROMOTED_TO_RUN"
            ):
                continue
            if (
                member is None
                or str(member["state"]) != "WAITING_REDIRECT_BOUNDARY"
                or member["dispatch_group_id"] is not None
            ):
                break
            candidate = [*members, member]
            raw_candidate = REDIRECT_MEMBER_SEPARATOR.join(
                str(item["content"]) for item in candidate
            )
            wire_candidate = raw_candidate.strip()
            if len(wire_candidate.encode("utf-8")) > max_wire_bytes:
                if not members:
                    raise SQLiteStoreError(
                        "oldest redirect exceeds the redirect wire limit"
                    )
                break
            members.append(member)
            if len(members) >= max_members:
                break
        if not members:
            return None

        digest_members = [
            {
                "position": position,
                "redirect_id": str(member["redirect_id"]),
                "request_digest": str(member["request_digest"]),
                "content": str(member["content"]),
            }
            for position, member in enumerate(members)
        ]
        raw_aggregate = REDIRECT_MEMBER_SEPARATOR.join(
            str(member["content"]) for member in members
        )
        wire_text = raw_aggregate.strip()
        aggregate_digest = cls._sha256_json(digest_members)
        raw_aggregate_digest = cls._sha256_bytes(raw_aggregate.encode("utf-8"))
        wire_text_digest = cls._sha256_bytes(wire_text.encode("utf-8"))
        connection.execute(
            """
            INSERT INTO run_redirect_dispatch_groups(
                dispatch_group_id, session_id, parent_run_id,
                aggregate_digest, raw_aggregate_digest, wire_text,
                wire_text_digest, member_count, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?)
            """,
            (
                dispatch_group_id,
                session_id,
                parent_run_id,
                aggregate_digest,
                raw_aggregate_digest,
                wire_text,
                wire_text_digest,
                len(members),
                created_at,
                created_at,
            ),
        )
        for position, member in enumerate(members):
            cursor = connection.execute(
                """
                UPDATE run_redirects
                SET dispatch_group_id = ?, dispatch_group_position = ?,
                    updated_at = ?
                WHERE redirect_id = ?
                  AND state = 'WAITING_REDIRECT_BOUNDARY'
                  AND dispatch_group_id IS NULL
                """,
                (
                    dispatch_group_id,
                    position,
                    created_at,
                    str(member["redirect_id"]),
                ),
            )
            if cursor.rowcount != 1:
                raise SQLiteStoreError("redirect group member changed during prepare")
        return cls._load_redirect_group_sync(connection, dispatch_group_id)

    @classmethod
    def _record_redirect_dispatch_intent_sync(
        cls,
        connection: sqlite3.Connection,
        dispatch_group_id: str,
        hermes_profile_key: str,
        hermes_stored_session_id: str,
        pre_dispatch_message_id: int,
        pre_dispatch_history_digest: str,
        pre_dispatch_model_request_seq: int,
        pre_dispatch_model_request_digest: str,
        dispatch_turn_id: str,
        dispatch_turn_task_id: str,
        dispatch_origin_evidence_digest: str,
        dispatch_boundary_epoch_nonce: str,
        dispatch_marker_request_nonce: str,
        dispatch_peer_boot_id: str,
        dispatch_peer_connection_id: str,
        pre_fence_event_seq: int,
        pre_fence_token_snapshot_digest: str,
        recorded_at: str,
    ) -> StoredRedirectDispatchGroup:
        group = connection.execute(
            "SELECT * FROM run_redirect_dispatch_groups WHERE dispatch_group_id = ?",
            (dispatch_group_id,),
        ).fetchone()
        if group is None:
            raise SQLiteStoreError("redirect dispatch group does not exist")
        if str(group["state"]) != "PREPARED":
            if str(group["state"]) == "DISPATCH_INTENT":
                expected = (
                    hermes_profile_key,
                    hermes_stored_session_id,
                    pre_dispatch_message_id,
                    pre_dispatch_history_digest,
                    dispatch_peer_boot_id,
                    dispatch_peer_connection_id,
                )
                actual = (
                    group["hermes_profile_key"],
                    group["hermes_stored_session_id"],
                    group["pre_dispatch_message_id"],
                    group["pre_dispatch_history_digest"],
                    group["dispatch_peer_boot_id"],
                    group["dispatch_peer_connection_id"],
                )
                if actual == expected:
                    return cls._row_to_redirect_group(group)
            raise SQLiteStoreError("redirect group is not prepared")
        latest_event = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?",
            (str(group["parent_run_id"]),),
        ).fetchone()
        if int(latest_event[0]) != pre_fence_event_seq:
            raise SQLiteStoreError("redirect event fence anchor is stale")
        connection.execute(
            """
            UPDATE run_redirect_dispatch_groups SET
                hermes_profile_key = ?, hermes_stored_session_id = ?,
                pre_dispatch_message_id = ?, pre_dispatch_history_digest = ?,
                pre_dispatch_model_request_seq = ?,
                pre_dispatch_model_request_digest = ?, dispatch_turn_id = ?,
                dispatch_turn_task_id = ?, dispatch_origin_evidence_digest = ?,
                dispatch_boundary_epoch_nonce = ?,
                dispatch_marker_request_nonce = ?, dispatch_peer_boot_id = ?,
                dispatch_peer_connection_id = ?, state = 'DISPATCH_INTENT',
                updated_at = ?
            WHERE dispatch_group_id = ? AND state = 'PREPARED'
            """,
            (
                hermes_profile_key,
                hermes_stored_session_id,
                pre_dispatch_message_id,
                pre_dispatch_history_digest,
                pre_dispatch_model_request_seq,
                pre_dispatch_model_request_digest,
                dispatch_turn_id,
                dispatch_turn_task_id,
                dispatch_origin_evidence_digest,
                dispatch_boundary_epoch_nonce,
                dispatch_marker_request_nonce,
                dispatch_peer_boot_id,
                dispatch_peer_connection_id,
                recorded_at,
                dispatch_group_id,
            ),
        )
        connection.execute(
            """
            UPDATE run_redirects
            SET state = 'DISPATCH_INTENT', updated_at = ?
            WHERE dispatch_group_id = ?
            """,
            (recorded_at, dispatch_group_id),
        )
        connection.execute(
            """
            INSERT INTO redirect_event_fences(
                dispatch_group_id, parent_run_id, pre_fence_event_seq,
                pre_fence_token_snapshot_digest, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)
            """,
            (
                dispatch_group_id,
                str(group["parent_run_id"]),
                pre_fence_event_seq,
                pre_fence_token_snapshot_digest,
                recorded_at,
                recorded_at,
            ),
        )
        return cls._load_redirect_group_sync(connection, dispatch_group_id)

    @classmethod
    def _rollback_pre_rpc_redirect_group_sync(
        cls,
        connection: sqlite3.Connection,
        dispatch_group_id: str,
        rolled_back_at: str,
    ) -> StoredRedirectDispatchGroup:
        group = cls._load_redirect_group_row_sync(connection, dispatch_group_id)
        state = str(group["state"])
        if state == "PREPARED":
            return cls._row_to_redirect_group(group)
        if state != "DISPATCH_INTENT" or group["hermes_status"] is not None:
            raise SQLiteStoreError(
                "redirect dispatch intent cannot be rolled back after RPC"
            )
        connection.execute(
            "DELETE FROM redirect_event_fence_snapshots WHERE dispatch_group_id = ?",
            (dispatch_group_id,),
        )
        connection.execute(
            "DELETE FROM redirect_event_fences WHERE dispatch_group_id = ?",
            (dispatch_group_id,),
        )
        connection.execute(
            """
            UPDATE run_redirects
            SET state = 'WAITING_REDIRECT_BOUNDARY', updated_at = ?
            WHERE dispatch_group_id = ? AND state = 'DISPATCH_INTENT'
            """,
            (rolled_back_at, dispatch_group_id),
        )
        connection.execute(
            """
            UPDATE run_redirect_dispatch_groups SET
                hermes_profile_key = NULL,
                hermes_stored_session_id = NULL,
                pre_dispatch_message_id = NULL,
                pre_dispatch_history_digest = NULL,
                pre_dispatch_model_request_seq = NULL,
                pre_dispatch_model_request_digest = NULL,
                dispatch_turn_id = NULL,
                dispatch_turn_task_id = NULL,
                dispatch_origin_evidence_digest = NULL,
                dispatch_boundary_epoch_nonce = NULL,
                dispatch_marker_request_nonce = NULL,
                dispatch_peer_boot_id = NULL,
                dispatch_peer_connection_id = NULL,
                state = 'PREPARED', updated_at = ?
            WHERE dispatch_group_id = ? AND state = 'DISPATCH_INTENT'
            """,
            (rolled_back_at, dispatch_group_id),
        )
        return cls._load_redirect_group_sync(connection, dispatch_group_id)

    @staticmethod
    def _complete_redirect_event_fence_release_sync(
        connection: sqlite3.Connection,
        dispatch_group_id: str,
    ) -> bool:
        fence = connection.execute(
            "SELECT state FROM redirect_event_fences WHERE dispatch_group_id = ?",
            (dispatch_group_id,),
        ).fetchone()
        if fence is None:
            return False
        if str(fence["state"]) != "RELEASE_READY":
            raise SQLiteStoreError("redirect event fence is not release-ready")
        connection.execute(
            "DELETE FROM redirect_event_fence_snapshots WHERE dispatch_group_id = ?",
            (dispatch_group_id,),
        )
        connection.execute(
            "DELETE FROM redirect_event_fences WHERE dispatch_group_id = ?",
            (dispatch_group_id,),
        )
        return True

    @classmethod
    def _isolate_redirect_group_sync(
        cls,
        connection: sqlite3.Connection,
        dispatch_group_id: str,
        isolated_at: str,
    ) -> StoredRedirectDispatchGroup:
        group = cls._load_redirect_group_row_sync(connection, dispatch_group_id)
        state = str(group["state"])
        if state == "ISOLATING_UNCERTAIN":
            return cls._row_to_redirect_group(group)
        if state not in {"DISPATCH_INTENT", "RPC_ACKNOWLEDGED"}:
            raise SQLiteStoreError("redirect group has not crossed the RPC boundary")
        fence = connection.execute(
            "SELECT state FROM redirect_event_fences WHERE dispatch_group_id = ?",
            (dispatch_group_id,),
        ).fetchone()
        if fence is None or str(fence["state"]) != "ACTIVE":
            raise SQLiteStoreError("redirect event fence is missing")
        connection.execute(
            """
            UPDATE run_redirect_dispatch_groups
            SET state = 'ISOLATING_UNCERTAIN', updated_at = ?
            WHERE dispatch_group_id = ?
            """,
            (isolated_at, dispatch_group_id),
        )
        connection.execute(
            """
            UPDATE run_redirects
            SET state = 'ISOLATING_UNCERTAIN', updated_at = ?
            WHERE dispatch_group_id = ?
              AND state IN ('DISPATCH_INTENT', 'RPC_ACKNOWLEDGED')
            """,
            (isolated_at, dispatch_group_id),
        )
        return cls._load_redirect_group_sync(connection, dispatch_group_id)

    @classmethod
    def _finalize_redirect_group_uncertain_sync(
        cls,
        connection: sqlite3.Connection,
        dispatch_group_id: str,
        finalized_at: str,
    ) -> StoredRedirectDispatchGroup:
        group = cls._load_redirect_group_row_sync(connection, dispatch_group_id)
        state = str(group["state"])
        if state == "SUBMISSION_UNCERTAIN":
            return cls._row_to_redirect_group(group)
        if state != "ISOLATING_UNCERTAIN":
            raise SQLiteStoreError(
                "redirect uncertainty requires an isolated dispatch group"
            )
        fence = connection.execute(
            "SELECT state FROM redirect_event_fences WHERE dispatch_group_id = ?",
            (dispatch_group_id,),
        ).fetchone()
        if fence is None or str(fence["state"]) != "ACTIVE":
            raise SQLiteStoreError("redirect event fence is missing")

        members = cls._load_redirect_group_members_sync(
            connection,
            dispatch_group_id,
        )
        for member in members:
            terminal_seq = member["terminal_disposition_event_seq"]
            if terminal_seq is None:
                terminal_seq = cls._next_run_event_seq_sync(
                    connection,
                    str(group["parent_run_id"]),
                )
                start_after_seq = (
                    int(member["accepted_event_seq"])
                    if member["accepted_event_seq"] is not None
                    else int(member["pending_event_seq"])
                )
                cls._append_redirect_disposition_event_sync(
                    connection,
                    member,
                    event_type="followup.failed",
                    event_seq=int(terminal_seq),
                    start_after_seq=start_after_seq,
                    occurred_at=finalized_at,
                    extra={"error_code": "FOLLOWUP_DELIVERY_UNRESOLVED"},
                )
                connection.execute(
                    """
                    UPDATE run_redirects SET
                        state = 'SUBMISSION_UNCERTAIN',
                        terminal_disposition_kind = 'uncertain',
                        terminal_disposition_event_seq = ?,
                        start_after_seq = COALESCE(start_after_seq, ?),
                        updated_at = ?
                    WHERE redirect_id = ?
                      AND state = 'ISOLATING_UNCERTAIN'
                      AND terminal_disposition_event_seq IS NULL
                    """,
                    (
                        int(terminal_seq),
                        start_after_seq,
                        finalized_at,
                        str(member["redirect_id"]),
                    ),
                )
            connection.execute(
                """
                UPDATE input_admission_reservations SET state = 'completed'
                WHERE redirect_id = ? AND kind = 'redirect'
                """,
                (str(member["redirect_id"]),),
            )

        connection.execute(
            """
            UPDATE run_redirect_dispatch_groups
            SET state = 'SUBMISSION_UNCERTAIN', updated_at = ?
            WHERE dispatch_group_id = ? AND state = 'ISOLATING_UNCERTAIN'
            """,
            (finalized_at, dispatch_group_id),
        )
        connection.execute(
            """
            UPDATE redirect_event_fences
            SET state = 'RELEASE_READY', updated_at = ?
            WHERE dispatch_group_id = ? AND state = 'ACTIVE'
            """,
            (finalized_at, dispatch_group_id),
        )
        cls._refresh_channel_fast_lane_v2_for_group_sync(
            connection,
            dispatch_group_id,
            finalized_at,
        )
        return cls._load_redirect_group_sync(connection, dispatch_group_id)

    @classmethod
    def _finalize_redirect_group_failed_sync(
        cls,
        connection: sqlite3.Connection,
        dispatch_group_id: str,
        finalized_at: str,
        error_code: str,
        include_all_parent_followups: bool = False,
        hermes_status: str | None = None,
    ) -> StoredRedirectDispatchGroup:
        """Write one terminal failure for every selected follow-up member.

        The existing ``SUBMISSION_UNCERTAIN`` ledger state is retained for
        protocol/schema compatibility.  The event error code carries the
        precise terminal reason; importantly, this mutation never reserves or
        creates a continuation Run.
        """

        group = cls._load_redirect_group_row_sync(connection, dispatch_group_id)
        state = str(group["state"])
        if state == "SUBMISSION_UNCERTAIN":
            return cls._row_to_redirect_group(group)
        if state not in {
            "PREPARED",
            "DISPATCH_INTENT",
            "RPC_ACKNOWLEDGED",
            "ISOLATING_UNCERTAIN",
        }:
            raise SQLiteStoreError("redirect group cannot be failed")

        parent_run_id = str(group["parent_run_id"])
        if include_all_parent_followups:
            members = connection.execute(
                """
                SELECT * FROM run_redirects
                WHERE parent_run_id = ?
                  AND terminal_disposition_event_seq IS NULL
                  AND state <> 'CANONICAL_CONSUMED'
                ORDER BY input_admission_seq
                """,
                (parent_run_id,),
            ).fetchall()
        else:
            members = cls._load_redirect_group_members_sync(
                connection,
                dispatch_group_id,
            )
            members = [
                member
                for member in members
                if member["terminal_disposition_event_seq"] is None
                and str(member["state"]) != "CANONICAL_CONSUMED"
            ]
        if not members:
            raise SQLiteStoreError("redirect group has no unterminalized members")

        logical_group_ids = list(
            dict.fromkeys(
                str(member["dispatch_group_id"])
                for member in members
                if member["dispatch_group_id"] is not None
            )
        )
        for member in members:
            terminal_seq = cls._next_run_event_seq_sync(
                connection,
                parent_run_id,
            )
            start_after_seq = (
                int(member["accepted_event_seq"])
                if member["accepted_event_seq"] is not None
                else int(member["pending_event_seq"])
            )
            cls._append_redirect_disposition_event_sync(
                connection,
                member,
                event_type="followup.failed",
                event_seq=terminal_seq,
                start_after_seq=start_after_seq,
                occurred_at=finalized_at,
                extra={"error_code": error_code},
            )
            connection.execute(
                """
                UPDATE run_redirects SET
                    state = 'SUBMISSION_UNCERTAIN',
                    hermes_status = COALESCE(?, hermes_status),
                    terminal_disposition_kind = 'uncertain',
                    terminal_disposition_event_seq = ?,
                    start_after_seq = COALESCE(start_after_seq, ?),
                    updated_at = ?
                WHERE redirect_id = ?
                  AND terminal_disposition_event_seq IS NULL
                  AND state <> 'CANONICAL_CONSUMED'
                """,
                (
                    hermes_status,
                    terminal_seq,
                    start_after_seq,
                    finalized_at,
                    str(member["redirect_id"]),
                ),
            )
            connection.execute(
                """
                UPDATE input_admission_reservations SET state = 'completed'
                WHERE redirect_id = ? AND kind = 'redirect'
                """,
                (str(member["redirect_id"]),),
            )

        placeholders = ",".join("?" for _ in logical_group_ids)
        if logical_group_ids:
            connection.execute(
                f"""
                UPDATE run_redirect_dispatch_groups SET
                    state = 'SUBMISSION_UNCERTAIN',
                    hermes_status = COALESCE(?, hermes_status),
                    updated_at = ?
                WHERE dispatch_group_id IN ({placeholders})
                  AND state IN (
                    'PREPARED', 'DISPATCH_INTENT', 'RPC_ACKNOWLEDGED',
                    'ISOLATING_UNCERTAIN'
                  )
                """,
                (hermes_status, finalized_at, *logical_group_ids),
            )
            connection.execute(
                f"""
                UPDATE redirect_event_fences
                SET state = 'RELEASE_READY', updated_at = ?
                WHERE dispatch_group_id IN ({placeholders})
                  AND state = 'ACTIVE'
                """,
                (finalized_at, *logical_group_ids),
            )
        if include_all_parent_followups:
            cls._refresh_channel_fast_lane_v2_for_parent_sync(
                connection,
                parent_run_id,
                finalized_at,
            )
        else:
            cls._refresh_channel_fast_lane_v2_for_group_sync(
                connection,
                dispatch_group_id,
                finalized_at,
            )
        return cls._load_redirect_group_sync(connection, dispatch_group_id)

    @classmethod
    def _cancel_pre_rpc_redirects_for_parent_sync(
        cls,
        connection: sqlite3.Connection,
        parent_run_id: str,
        canceled_at: str,
    ) -> int:
        crossed = connection.execute(
            """
            SELECT 1 FROM run_redirect_dispatch_groups
            WHERE parent_run_id = ? AND state IN (
                'DISPATCH_INTENT', 'RPC_ACKNOWLEDGED', 'ISOLATING_UNCERTAIN'
            ) LIMIT 1
            """,
            (parent_run_id,),
        ).fetchone()
        if crossed is not None:
            raise SQLiteStoreError("redirect crossed RPC before parent cancellation")
        members = connection.execute(
            """
            SELECT * FROM run_redirects
            WHERE parent_run_id = ? AND state IN (
                'WAITING_AGENT_READY', 'WAITING_REDIRECT_BOUNDARY'
            )
            ORDER BY input_admission_seq
            """,
            (parent_run_id,),
        ).fetchall()
        for member in members:
            event_seq = cls._next_run_event_seq_sync(connection, parent_run_id)
            cls._append_redirect_disposition_event_sync(
                connection,
                member,
                event_type="followup.failed",
                event_seq=event_seq,
                start_after_seq=int(member["pending_event_seq"]),
                occurred_at=canceled_at,
                extra={"error_code": "FOLLOWUP_PARENT_CANCELED"},
            )
            connection.execute(
                """
                UPDATE run_redirects SET
                    state = 'CANCELED_WITH_PARENT',
                    terminal_disposition_kind = 'canceled',
                    terminal_disposition_event_seq = ?, updated_at = ?
                WHERE redirect_id = ? AND terminal_disposition_event_seq IS NULL
                """,
                (event_seq, canceled_at, str(member["redirect_id"])),
            )
            connection.execute(
                """
                UPDATE input_admission_reservations SET state = 'completed'
                WHERE redirect_id = ? AND kind = 'redirect'
                """,
                (str(member["redirect_id"]),),
            )
        connection.execute(
            """
            UPDATE run_redirect_dispatch_groups
            SET state = 'CANCELED_WITH_PARENT', updated_at = ?
            WHERE parent_run_id = ? AND state = 'PREPARED'
            """,
            (canceled_at, parent_run_id),
        )
        cls._refresh_channel_fast_lane_v2_for_parent_sync(
            connection,
            parent_run_id,
            canceled_at,
        )
        return len(members)

    @classmethod
    def _acknowledge_redirect_group_sync(
        cls,
        connection: sqlite3.Connection,
        dispatch_group_id: str,
        hermes_status: str,
        acknowledged_at: str,
        queued_is_accepted: bool = False,
    ) -> StoredRedirectDispatchGroup:
        group = cls._load_redirect_group_row_sync(connection, dispatch_group_id)
        current = str(group["state"])
        if current in {"RPC_ACKNOWLEDGED", "ISOLATING_UNCERTAIN"}:
            if str(group["hermes_status"]) != hermes_status:
                raise IdempotencyConflict("redirect acknowledgement conflicts")
            return cls._row_to_redirect_group(group)
        if current != "DISPATCH_INTENT":
            raise SQLiteStoreError("redirect group has no dispatch intent")
        if hermes_status == "queued" and not queued_is_accepted:
            connection.execute(
                """
                UPDATE run_redirect_dispatch_groups
                SET state = 'ISOLATING_UNCERTAIN', hermes_status = 'queued',
                    updated_at = ?
                WHERE dispatch_group_id = ?
                """,
                (acknowledged_at, dispatch_group_id),
            )
            connection.execute(
                """
                UPDATE run_redirects
                SET state = 'ISOLATING_UNCERTAIN', hermes_status = 'queued',
                    updated_at = ?
                WHERE dispatch_group_id = ?
                """,
                (acknowledged_at, dispatch_group_id),
            )
            cls._refresh_channel_fast_lane_v2_for_group_sync(
                connection,
                dispatch_group_id,
                acknowledged_at,
            )
            return cls._load_redirect_group_sync(connection, dispatch_group_id)

        stored_hermes_status = "queued" if queued_is_accepted else "redirected"
        for member in cls._load_redirect_group_members_sync(
            connection,
            dispatch_group_id,
        ):
            event_seq = member["accepted_event_seq"]
            if event_seq is None:
                event_seq = cls._next_run_event_seq_sync(
                    connection,
                    str(group["parent_run_id"]),
                )
                cls._append_redirect_disposition_event_sync(
                    connection,
                    member,
                    event_type="followup.applying",
                    event_seq=int(event_seq),
                    start_after_seq=int(event_seq),
                    occurred_at=acknowledged_at,
                )
            connection.execute(
                """
                UPDATE run_redirects SET
                    state = 'RPC_ACKNOWLEDGED', hermes_status = ?,
                    accepted_event_seq = COALESCE(accepted_event_seq, ?),
                    start_after_seq = COALESCE(start_after_seq, ?),
                    updated_at = ?
                WHERE redirect_id = ?
                  AND state IN ('DISPATCH_INTENT', 'RPC_ACKNOWLEDGED')
                """,
                (
                    stored_hermes_status,
                    event_seq,
                    event_seq,
                    acknowledged_at,
                    str(member["redirect_id"]),
                ),
            )
        connection.execute(
            """
            UPDATE run_redirect_dispatch_groups
            SET state = 'RPC_ACKNOWLEDGED', hermes_status = ?,
                updated_at = ?
            WHERE dispatch_group_id = ?
            """,
            (stored_hermes_status, acknowledged_at, dispatch_group_id),
        )
        cls._refresh_channel_fast_lane_v2_for_group_sync(
            connection,
            dispatch_group_id,
            acknowledged_at,
        )
        return cls._load_redirect_group_sync(connection, dispatch_group_id)

    @classmethod
    def _consume_redirect_group_sync(
        cls,
        connection: sqlite3.Connection,
        dispatch_group_id: str,
        proof_message_id: int,
        proof_post_digest: str,
        consumed_at: str,
    ) -> StoredRedirectDispatchGroup:
        group = cls._load_redirect_group_row_sync(connection, dispatch_group_id)
        current = str(group["state"])
        if current == "CANONICAL_CONSUMED":
            if (
                int(group["proof_message_id"]) != proof_message_id
                or str(group["proof_post_digest"]) != proof_post_digest
            ):
                raise IdempotencyConflict("redirect canonical proof conflicts")
            return cls._row_to_redirect_group(group)
        if current not in {
            "DISPATCH_INTENT",
            "RPC_ACKNOWLEDGED",
            "ISOLATING_UNCERTAIN",
        }:
            raise SQLiteStoreError("redirect group has not crossed the RPC boundary")
        fence = connection.execute(
            "SELECT state FROM redirect_event_fences WHERE dispatch_group_id = ?",
            (dispatch_group_id,),
        ).fetchone()
        if fence is None or str(fence["state"]) != "ACTIVE":
            raise SQLiteStoreError("redirect event fence is missing")

        members = cls._load_redirect_group_members_sync(
            connection,
            dispatch_group_id,
        )
        for member in members:
            original_parent_run_id, continuation_run_id = (
                cls._continued_into_run_identity(member)
            )
            continued_into_existing = (
                original_parent_run_id is not None
                and continuation_run_id == str(group["parent_run_id"])
            )
            accepted_seq = member["accepted_event_seq"]
            if accepted_seq is None:
                accepted_seq = cls._next_run_event_seq_sync(
                    connection,
                    str(group["parent_run_id"]),
                )
                cls._append_redirect_disposition_event_sync(
                    connection,
                    member,
                    event_type="followup.applying",
                    event_seq=int(accepted_seq),
                    start_after_seq=int(accepted_seq),
                    occurred_at=consumed_at,
                )
            consumed_seq = cls._next_run_event_seq_sync(
                connection,
                str(group["parent_run_id"]),
            )
            cls._append_redirect_disposition_event_sync(
                connection,
                member,
                event_type=(
                    "followup.continued"
                    if continued_into_existing
                    else "followup.applied"
                ),
                event_seq=consumed_seq,
                start_after_seq=int(accepted_seq),
                occurred_at=consumed_at,
                extra=(
                    {
                        "continuation_run_id": continuation_run_id,
                        "member_client_message_ids": [
                            str(item["client_message_id"])
                            for item in members
                        ],
                        "proof_kind": "canonical_history",
                        "proof_digest": proof_post_digest,
                    }
                    if continued_into_existing
                    else {
                        "proof_kind": (
                            "steer_oob_marker"
                            if str(group["hermes_status"] or "") == "queued"
                            else "correction_user_row"
                        ),
                        "proof_digest": proof_post_digest,
                    }
                ),
            )
            if continued_into_existing:
                connection.execute(
                    """
                    UPDATE run_redirects SET
                        state = 'PROMOTED_TO_RUN',
                        hermes_status = CASE
                            WHEN hermes_status = 'queued' THEN 'queued'
                            ELSE 'redirected'
                        END,
                        accepted_event_seq = COALESCE(accepted_event_seq, ?),
                        consumed_event_seq = ?,
                        terminal_disposition_kind = 'promoted',
                        terminal_disposition_event_seq = ?,
                        start_after_seq = COALESCE(start_after_seq, ?),
                        promoted_run_id = ?, updated_at = ?
                    WHERE redirect_id = ? AND consumed_event_seq IS NULL
                    """,
                    (
                        int(accepted_seq),
                        consumed_seq,
                        consumed_seq,
                        int(accepted_seq),
                        continuation_run_id,
                        consumed_at,
                        str(member["redirect_id"]),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE run_redirects SET
                        state = 'CANONICAL_CONSUMED',
                        hermes_status = CASE
                            WHEN hermes_status = 'queued' THEN 'queued'
                            ELSE 'redirected'
                        END,
                        accepted_event_seq = COALESCE(accepted_event_seq, ?),
                        consumed_event_seq = ?,
                        start_after_seq = COALESCE(start_after_seq, ?),
                        updated_at = ?
                    WHERE redirect_id = ? AND consumed_event_seq IS NULL
                    """,
                    (
                        int(accepted_seq),
                        consumed_seq,
                        int(accepted_seq),
                        consumed_at,
                        str(member["redirect_id"]),
                    ),
                )
            connection.execute(
                """
                UPDATE input_admission_reservations
                SET state = ?
                WHERE redirect_id = ? AND kind = 'redirect'
                """,
                (
                    "promoted" if continued_into_existing else "completed",
                    str(member["redirect_id"]),
                ),
            )
            if continued_into_existing and continuation_run_id is not None:
                continuation_row = connection.execute(
                    "SELECT source_detail_json FROM runs WHERE run_id = ?",
                    (continuation_run_id,),
                ).fetchone()
                if continuation_row is not None:
                    raw_detail = json.loads(
                        str(continuation_row["source_detail_json"] or "{}")
                    )
                    detail = raw_detail if isinstance(raw_detail, dict) else {}
                    raw_ids = detail.get("followup_client_message_ids", [])
                    client_ids = (
                        [item for item in raw_ids if isinstance(item, str)]
                        if isinstance(raw_ids, list)
                        else []
                    )
                    client_message_id = str(member["client_message_id"])
                    if client_message_id not in client_ids:
                        client_ids.append(client_message_id)
                    detail["followup_client_message_ids"] = client_ids
                    detail["followup_member_count"] = len(client_ids)
                    connection.execute(
                        """
                        UPDATE runs SET source_detail_json = ?
                        WHERE run_id = ?
                        """,
                        (cls._json(detail), continuation_run_id),
                    )
        connection.execute(
            """
            UPDATE run_redirect_dispatch_groups SET
                state = 'CANONICAL_CONSUMED',
                hermes_status = CASE
                    WHEN hermes_status = 'queued' THEN 'queued'
                    ELSE 'redirected'
                END,
                proof_message_id = ?, proof_kind = CASE
                    WHEN hermes_status = 'queued' THEN NULL
                    ELSE 'correction_user_row'
                END,
                proof_post_digest = ?, updated_at = ?
            WHERE dispatch_group_id = ?
            """,
            (
                proof_message_id,
                proof_post_digest,
                consumed_at,
                dispatch_group_id,
            ),
        )
        connection.execute(
            """
            UPDATE redirect_event_fences
            SET state = 'RELEASE_READY', updated_at = ?
            WHERE dispatch_group_id = ? AND state = 'ACTIVE'
            """,
            (consumed_at, dispatch_group_id),
        )
        cls._refresh_channel_fast_lane_v2_for_group_sync(
            connection,
            dispatch_group_id,
            consumed_at,
        )
        return cls._load_redirect_group_sync(connection, dispatch_group_id)

    @classmethod
    def _promote_redirect_group_sync(
        cls,
        connection: sqlite3.Connection,
        dispatch_group_id: str,
        promoted_at: str,
        include_all_parent_followups: bool = False,
    ) -> StoredRedirectDispatchGroup:
        group = cls._load_redirect_group_row_sync(connection, dispatch_group_id)
        current = str(group["state"])
        if current == "PROMOTED_TO_RUN":
            return cls._row_to_redirect_group(group)
        if current not in {
            "PREPARED",
            "DISPATCH_INTENT",
            "ISOLATING_UNCERTAIN",
        }:
            raise SQLiteStoreError("redirect group cannot be promoted")
        if include_all_parent_followups:
            members = connection.execute(
                """
                SELECT * FROM run_redirects
                WHERE parent_run_id = ?
                  AND terminal_disposition_event_seq IS NULL
                  AND state <> 'CANONICAL_CONSUMED'
                ORDER BY input_admission_seq
                """,
                (str(group["parent_run_id"]),),
            ).fetchall()
        else:
            members = cls._load_redirect_group_members_sync(
                connection,
                dispatch_group_id,
            )
        if not members:
            raise SQLiteStoreError("redirect group has no members")
        disposition_members = members
        continued_into_targets = [
            cls._continued_into_run_identity(member)
            for member in disposition_members
        ]
        if continued_into_targets and all(
            original_parent is not None
            and continuation_run_id == str(group["parent_run_id"])
            for original_parent, continuation_run_id in continued_into_targets
        ):
            for member in disposition_members:
                event_seq = cls._next_run_event_seq_sync(
                    connection,
                    str(group["parent_run_id"]),
                )
                start_after_seq = (
                    int(member["accepted_event_seq"])
                    if member["accepted_event_seq"] is not None
                    else int(member["pending_event_seq"])
                )
                cls._append_redirect_disposition_event_sync(
                    connection,
                    member,
                    event_type="followup.failed",
                    event_seq=event_seq,
                    start_after_seq=start_after_seq,
                    occurred_at=promoted_at,
                    extra={
                        "error_code": "FOLLOWUP_CONTINUATION_DELIVERY_REJECTED"
                    },
                )
                connection.execute(
                    """
                    UPDATE run_redirects SET
                        state = 'SUBMISSION_UNCERTAIN',
                        terminal_disposition_kind = 'uncertain',
                        terminal_disposition_event_seq = ?,
                        start_after_seq = COALESCE(start_after_seq, ?),
                        updated_at = ?
                    WHERE redirect_id = ?
                      AND terminal_disposition_event_seq IS NULL
                    """,
                    (
                        event_seq,
                        start_after_seq,
                        promoted_at,
                        str(member["redirect_id"]),
                    ),
                )
                connection.execute(
                    """
                    UPDATE input_admission_reservations SET state = 'completed'
                    WHERE redirect_id = ? AND kind = 'redirect'
                    """,
                    (str(member["redirect_id"]),),
                )
            connection.execute(
                """
                UPDATE run_redirect_dispatch_groups
                SET state = 'SUBMISSION_UNCERTAIN', updated_at = ?
                WHERE dispatch_group_id = ?
                """,
                (promoted_at, dispatch_group_id),
            )
            connection.execute(
                """
                UPDATE redirect_event_fences
                SET state = 'RELEASE_READY', updated_at = ?
                WHERE dispatch_group_id = ? AND state = 'ACTIVE'
                """,
                (promoted_at, dispatch_group_id),
            )
            cls._refresh_channel_fast_lane_v2_for_parent_sync(
                connection,
                str(group["parent_run_id"]),
                promoted_at,
            )
            return cls._load_redirect_group_sync(connection, dispatch_group_id)
        reusable_continuation: sqlite3.Row | None = None
        if include_all_parent_followups:
            reusable_continuation = connection.execute(
                """
                SELECT runs.*
                FROM run_redirects AS redirects
                JOIN runs ON runs.run_id = redirects.promoted_run_id
                WHERE redirects.parent_run_id = ?
                  AND redirects.promoted_run_id IS NOT NULL
                  AND runs.state = 'QUEUED'
                ORDER BY redirects.input_admission_seq
                LIMIT 1
                """,
                (str(group["parent_run_id"]),),
            ).fetchone()
            if reusable_continuation is not None:
                members = connection.execute(
                    """
                    SELECT * FROM run_redirects
                    WHERE parent_run_id = ?
                      AND (
                        promoted_run_id = ?
                        OR (
                          terminal_disposition_event_seq IS NULL
                          AND state <> 'CANONICAL_CONSUMED'
                        )
                      )
                    ORDER BY input_admission_seq
                    """,
                    (
                        str(group["parent_run_id"]),
                        str(reusable_continuation["run_id"]),
                    ),
                ).fetchall()

        intents: list[dict[str, Any]] = []
        audit_prompts: list[str] = []
        attachment_file_ids: list[int] = []
        for member in members:
            raw_intent = json.loads(str(member["promotion_run_intent_json"]))
            if not isinstance(raw_intent, dict):
                raise SQLiteStoreError("promotion Run intent is not an object")
            identity = raw_intent.get("prepare_identity")
            if not isinstance(identity, dict):
                raise SQLiteStoreError("promotion PrepareIdentity is missing")
            payload_digest = str(member["promotion_run_payload_digest"])
            if raw_intent.get("payload_digest", payload_digest) != payload_digest:
                raise SQLiteStoreError("promotion payload digest conflicts")
            member_source_detail = raw_intent.get("source_detail")
            if member_source_detail is not None and not isinstance(
                member_source_detail, dict
            ):
                raise SQLiteStoreError("promotion source detail is invalid")
            member_prompt = str(raw_intent.get("prompt", member["content"]))
            if member_prompt != str(member["content"]):
                raise SQLiteStoreError("promotion prompt differs from redirect content")
            audit_prompts.append(
                str(raw_intent.get("audit_prompt", member_prompt))
            )
            if isinstance(member_source_detail, dict):
                raw_ids = member_source_detail.get("attachment_file_ids", [])
                if isinstance(raw_ids, list):
                    for raw_id in raw_ids:
                        if isinstance(raw_id, int) and raw_id not in attachment_file_ids:
                            attachment_file_ids.append(raw_id)
            intents.append(raw_intent)

        first = members[0]
        first_intent = intents[0]
        identity = first_intent["prepare_identity"]
        assert isinstance(identity, dict)
        for member, raw_intent in zip(members[1:], intents[1:], strict=True):
            if raw_intent.get("prepare_identity") != identity:
                raise SQLiteStoreError("redirect group identities conflict")
            if str(member["session_id"]) != str(first["session_id"]):
                raise SQLiteStoreError("redirect group Sessions conflict")

        run_id = (
            str(reusable_continuation["run_id"])
            if reusable_continuation is not None
            else str(first["promotion_reserved_run_id"])
        )
        source = str(first_intent.get("source") or "web")
        raw_source_detail = first_intent.get("source_detail")
        source_detail = dict(raw_source_detail) if isinstance(raw_source_detail, dict) else {}
        logical_group_ids = list(
            dict.fromkeys(
                str(member["dispatch_group_id"])
                for member in members
                if member["dispatch_group_id"] is not None
            )
        )
        source_detail.update(
            {
                "followup_group_id": dispatch_group_id,
                "followup_group_ids": logical_group_ids,
                "followup_parent_run_id": str(group["parent_run_id"]),
                "followup_member_count": len(members),
                "followup_client_message_ids": [
                    str(member["client_message_id"]) for member in members
                ],
            }
        )
        if attachment_file_ids:
            source_detail["attachment_file_ids"] = attachment_file_ids
        prompt = REDIRECT_MEMBER_SEPARATOR.join(
            str(member["content"]) for member in members
        ).strip()
        audit_prompt = REDIRECT_MEMBER_SEPARATOR.join(audit_prompts).strip()
        payload_digest = cls._sha256_json(
            {
                "contract_version": "followup-continuation-run-v2",
                "parent_run_id": str(group["parent_run_id"]),
                "dispatch_group_ids": logical_group_ids,
                "member_payload_digests": [
                    str(member["promotion_run_payload_digest"])
                    for member in members
                ],
            }
        )
        existing = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["session_id"]) != str(first["session_id"])
                or str(existing["client_message_id"])
                != str(first["client_message_id"])
                or int(existing["enqueue_seq"])
                != int(first["input_admission_seq"])
            ):
                raise SQLiteStoreError("promoted Run linked pair is corrupt")
            if reusable_continuation is None:
                if str(existing["payload_digest"]) != payload_digest:
                    raise SQLiteStoreError("promoted Run payload digest conflicts")
            else:
                connection.execute(
                    """
                    UPDATE runs
                    SET payload_digest = ?, prompt = ?, audit_prompt = ?,
                        source_detail_json = ?
                    WHERE run_id = ? AND state = 'QUEUED'
                    """,
                    (
                        payload_digest,
                        prompt,
                        audit_prompt,
                        cls._json(source_detail),
                        run_id,
                    ),
                )
        else:
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, session_id, client_message_id, payload_digest,
                    config_revision, source, state, accepted_at,
                    enqueued_at, enqueue_seq, prompt, audit_prompt,
                    identity_json, source_detail_json, scheduled_task_id,
                    scheduled_occurrence_key, coalesced_count,
                    last_coalesced_at, history_message_count_before_run,
                    user_id, org_id, profile_id, model_id,
                    runtime_provider_run_id, cancellation_requested_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, 'QUEUED', NULL, ?, ?, ?, ?, ?, ?,
                    NULL, NULL, 0, NULL, NULL, ?, ?, ?, ?, NULL, NULL
                )
                """,
                (
                    run_id,
                    str(first["session_id"]),
                    str(first["client_message_id"]),
                    payload_digest,
                    str(identity.get("config_revision") or ""),
                    source,
                    promoted_at,
                    int(first["input_admission_seq"]),
                    prompt,
                    audit_prompt,
                    cls._json(identity),
                    cls._json(source_detail),
                    int(identity.get("user_id") or 0),
                    int(identity.get("org_id") or 0),
                    str(identity.get("profile_id") or ""),
                    str(identity.get("model_id") or ""),
                ),
            )
            queued_payload = {
                "run_id": run_id,
                "session_id": str(first["session_id"]),
                "client_message_id": str(first["client_message_id"]),
                "config_revision": str(identity.get("config_revision") or ""),
                "source": source,
                "source_detail": source_detail,
                "enqueued_at": promoted_at,
                "enqueue_seq": int(first["input_admission_seq"]),
                "user_id": int(identity.get("user_id") or 0),
                "org_id": int(identity.get("org_id") or 0),
                "profile_id": str(identity.get("profile_id") or ""),
                "model_id": str(identity.get("model_id") or ""),
                "prompt": audit_prompt,
            }
            cls._enqueue_projection_sync(
                connection,
                f"{run_id}:queued",
                "run.queued",
                cls._json(queued_payload),
                promoted_at,
            )

        if reusable_continuation is not None:
            cls._enqueue_projection_sync(
                connection,
                f"{run_id}:followup-members:{len(members)}",
                "run.queued",
                cls._json(
                    {
                        "run_id": run_id,
                        "session_id": str(first["session_id"]),
                        "client_message_id": str(first["client_message_id"]),
                        "config_revision": str(identity.get("config_revision") or ""),
                        "source": source,
                        "source_detail": source_detail,
                        "enqueued_at": str(existing["enqueued_at"]),
                        "enqueue_seq": int(first["input_admission_seq"]),
                        "user_id": int(identity.get("user_id") or 0),
                        "org_id": int(identity.get("org_id") or 0),
                        "profile_id": str(identity.get("profile_id") or ""),
                        "model_id": str(identity.get("model_id") or ""),
                        "prompt": audit_prompt,
                    }
                ),
                promoted_at,
            )

        for member in disposition_members:
            event_seq = cls._next_run_event_seq_sync(
                connection,
                str(group["parent_run_id"]),
            )
            cls._append_redirect_disposition_event_sync(
                connection,
                member,
                event_type="followup.continued",
                event_seq=event_seq,
                start_after_seq=event_seq,
                occurred_at=promoted_at,
                extra={
                    "continuation_run_id": run_id,
                    "member_client_message_ids": [
                        str(item["client_message_id"]) for item in members
                    ],
                },
            )
            connection.execute(
                """
                UPDATE run_redirects SET
                    state = 'PROMOTED_TO_RUN',
                    hermes_status = CASE
                        WHEN ? THEN hermes_status
                        ELSE 'rejected'
                    END,
                    terminal_disposition_kind = 'promoted',
                    terminal_disposition_event_seq = ?, start_after_seq = ?,
                    promoted_run_id = ?, updated_at = ?
                WHERE redirect_id = ?
                  AND terminal_disposition_event_seq IS NULL
                """,
                (
                    1 if include_all_parent_followups else 0,
                    event_seq,
                    event_seq,
                    run_id,
                    promoted_at,
                    str(member["redirect_id"]),
                ),
            )
            connection.execute(
                """
                UPDATE input_admission_reservations SET state = 'promoted'
                WHERE redirect_id = ? AND kind = 'redirect' AND state = 'bound'
                """,
                (str(member["redirect_id"]),),
            )
        placeholders = ",".join("?" for _ in logical_group_ids)
        if logical_group_ids:
            connection.execute(
                f"""
                UPDATE run_redirect_dispatch_groups
                SET state = 'PROMOTED_TO_RUN',
                    hermes_status = CASE
                        WHEN ? THEN hermes_status
                        ELSE 'rejected'
                    END,
                    updated_at = ?
                WHERE dispatch_group_id IN ({placeholders})
                """,
                (
                    1 if include_all_parent_followups else 0,
                    promoted_at,
                    *logical_group_ids,
                ),
            )
            connection.execute(
                f"""
                UPDATE redirect_event_fences
                SET state = 'RELEASE_READY', updated_at = ?
                WHERE dispatch_group_id IN ({placeholders})
                  AND state = 'ACTIVE'
                """,
                (promoted_at, *logical_group_ids),
            )
        cls._refresh_channel_fast_lane_v2_for_parent_sync(
            connection,
            str(group["parent_run_id"]),
            promoted_at,
        )
        return cls._load_redirect_group_sync(connection, dispatch_group_id)

    @classmethod
    def _append_redirect_disposition_event_sync(
        cls,
        connection: sqlite3.Connection,
        member: sqlite3.Row,
        *,
        event_type: str,
        event_seq: int,
        start_after_seq: int,
        occurred_at: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        original_parent_run_id, _ = cls._continued_into_run_identity(member)
        payload = {
            "followup_id": str(member["redirect_id"]),
            "client_message_id": str(member["client_message_id"]),
            "parent_run_id": (
                original_parent_run_id or str(member["parent_run_id"])
            ),
            "input_admission_seq": int(member["input_admission_seq"]),
            "dispatch_group_id": (
                str(member["dispatch_group_id"])
                if member["dispatch_group_id"] is not None
                else None
            ),
            "dispatch_group_position": (
                int(member["dispatch_group_position"])
                if member["dispatch_group_position"] is not None
                else None
            ),
            "event_seq": event_seq,
            "start_after_seq": start_after_seq,
        }
        if extra:
            payload.update(extra)
        inserted = cls._append_run_event_sync(
            connection,
            str(member["parent_run_id"]),
            event_seq,
            event_type,
            cls._json(payload),
            occurred_at,
        )
        if not inserted:
            raise SQLiteStoreError("redirect disposition event seq conflicts")
        cls._enqueue_projection_sync(
            connection,
            (
                f"{member['parent_run_id']}:redirect:"
                f"{member['redirect_id']}:{event_type}"
            ),
            event_type,
            cls._json(payload),
            occurred_at,
        )

    @staticmethod
    def _continued_into_run_identity(
        member: sqlite3.Row,
    ) -> tuple[str | None, str | None]:
        raw = json.loads(str(member["promotion_run_intent_json"]))
        if not isinstance(raw, dict):
            return None, None
        detail = raw.get("source_detail")
        if not isinstance(detail, dict):
            return None, None
        original_parent = detail.get("followup_original_parent_run_id")
        continuation = detail.get("followup_continuation_run_id")
        if not isinstance(original_parent, str) or not original_parent:
            return None, None
        if not isinstance(continuation, str) or not continuation:
            return None, None
        return original_parent, continuation

    @staticmethod
    def _next_run_event_seq_sync(
        connection: sqlite3.Connection,
        run_id: str,
    ) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _load_redirect_group_members_sync(
        connection: sqlite3.Connection,
        dispatch_group_id: str,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT * FROM run_redirects
            WHERE dispatch_group_id = ?
            ORDER BY dispatch_group_position
            """,
            (dispatch_group_id,),
        ).fetchall()

    @staticmethod
    def _load_redirect_group_row_sync(
        connection: sqlite3.Connection,
        dispatch_group_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM run_redirect_dispatch_groups WHERE dispatch_group_id = ?",
            (dispatch_group_id,),
        ).fetchone()
        if row is None:
            raise SQLiteStoreError("redirect dispatch group does not exist")
        return row

    @classmethod
    def _load_redirect_group_sync(
        cls,
        connection: sqlite3.Connection,
        dispatch_group_id: str,
    ) -> StoredRedirectDispatchGroup:
        return cls._row_to_redirect_group(
            cls._load_redirect_group_row_sync(connection, dispatch_group_id)
        )














    @classmethod
    def _enqueue_run_sync(
        cls,
        connection: sqlite3.Connection,
        intent: RunEnqueueIntent,
    ) -> StoredRun:
        redirect = connection.execute(
            """
            SELECT redirect_id, request_digest, state, promoted_run_id
            FROM run_redirects
            WHERE session_id = ? AND client_message_id = ?
            """,
            (intent.session_id, intent.client_message_id),
        ).fetchone()
        if redirect is not None:
            raise IdempotencyConflict(
                "client_message_id is already bound to a redirect disposition"
            )
        existing = connection.execute(
            """
            SELECT run_id, session_id, client_message_id, payload_digest,
                   config_revision, source, state, accepted_at,
                   enqueued_at, enqueue_seq, prompt, audit_prompt,
                   identity_json, source_detail_json, scheduled_task_id,
                   scheduled_occurrence_key, coalesced_count,
                   last_coalesced_at, history_message_count_before_run,
                   user_id, org_id, profile_id, model_id,
                   runtime_provider_run_id, cancellation_requested_at
            FROM runs WHERE session_id = ? AND client_message_id = ?
            """,
            (intent.session_id, intent.client_message_id),
        ).fetchone()
        if existing is not None:
            if str(existing["payload_digest"]) != intent.payload_digest:
                raise IdempotencyConflict(
                    "client_message_id is already bound to a different payload"
                )
            if intent.delivery_id is not None:
                cls._bind_channel_fast_lane_v2_run_sync(
                    connection,
                    intent.delivery_id,
                    str(existing["run_id"]),
                    intent.payload_digest,
                    intent.enqueued_at.astimezone(timezone.utc).isoformat(),
                )
            return cls._row_to_run(existing, created=False)

        admission_generation = intent.identity.get("admission_generation")
        if isinstance(admission_generation, bool) or not isinstance(
            admission_generation,
            int,
        ):
            raise PermissionError("RUNTIME_ADMISSION_GENERATION_MISMATCH")
        cls._require_new_admission_open_sync(
            connection,
            admission_generation=admission_generation,
        )

        if intent.scheduled_task_id is not None:
            queued = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE session_id = ? AND scheduled_task_id = ? AND state = ?
                ORDER BY enqueue_seq LIMIT 1
                """,
                (intent.session_id, intent.scheduled_task_id, QUEUED),
            ).fetchone()
            if queued is not None:
                connection.execute(
                    """
                    UPDATE runs
                    SET coalesced_count = coalesced_count + 1,
                        last_coalesced_at = ?
                    WHERE run_id = ? AND state = ?
                    """,
                    (
                        intent.enqueued_at.astimezone(timezone.utc).isoformat(),
                        str(queued["run_id"]),
                        QUEUED,
                    ),
                )
                return cls._load_run_sync(
                    connection,
                    str(queued["run_id"]),
                )

        enqueued_at = intent.enqueued_at.astimezone(timezone.utc).isoformat()
        enqueue_seq = cls._reserve_input_admission_sync(
            connection,
            session_id=intent.session_id,
            client_message_id=intent.client_message_id,
            kind="ordinary_run",
            run_id=intent.run_id,
            redirect_id=None,
            created_at=enqueued_at,
            state="bound",
        )
        identity = dict(intent.identity)
        user_id = int(identity.get("user_id") or 0)
        org_id = int(identity.get("org_id") or 0)
        profile_id = str(identity.get("profile_id") or "")
        model_id = str(identity.get("model_id") or "")
        connection.execute(
            """
            INSERT INTO runs(
                run_id, session_id, client_message_id, payload_digest,
                config_revision, source, state, accepted_at,
                enqueued_at, enqueue_seq, prompt, audit_prompt,
                identity_json, source_detail_json, scheduled_task_id,
                scheduled_occurrence_key, coalesced_count,
                last_coalesced_at, history_message_count_before_run,
                user_id, org_id, profile_id, model_id,
                runtime_provider_run_id, cancellation_requested_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0,
                NULL, NULL, ?, ?, ?, ?, NULL, NULL
            )
            """,
            (
                intent.run_id,
                intent.session_id,
                intent.client_message_id,
                intent.payload_digest,
                intent.config_revision,
                intent.source,
                QUEUED,
                None,
                enqueued_at,
                enqueue_seq,
                intent.prompt,
                intent.audit_prompt,
                cls._json(identity),
                (
                    cls._json(intent.source_detail)
                    if intent.source_detail is not None
                    else None
                ),
                intent.scheduled_task_id,
                intent.scheduled_occurrence_key,
                user_id,
                org_id,
                profile_id,
                model_id,
            ),
        )
        if intent.delivery_id is not None:
            cls._bind_channel_fast_lane_v2_run_sync(
                connection,
                intent.delivery_id,
                intent.run_id,
                intent.payload_digest,
                enqueued_at,
            )
        queued_payload = {
            "run_id": intent.run_id,
            "session_id": intent.session_id,
            "client_message_id": intent.client_message_id,
            "config_revision": intent.config_revision,
            "source": intent.source,
            "source_detail": intent.source_detail,
            "enqueued_at": enqueued_at,
            "enqueue_seq": enqueue_seq,
            "user_id": user_id,
            "org_id": org_id,
            "profile_id": profile_id,
            "model_id": model_id,
            "prompt": intent.audit_prompt,
        }
        cls._enqueue_projection_sync(
            connection,
            f"{intent.run_id}:queued",
            "run.queued",
            cls._json(queued_payload),
            enqueued_at,
        )
        return cls._load_run_sync(connection, intent.run_id, created=True)

    @classmethod
    def _require_new_admission_open_sync(
        cls,
        connection: sqlite3.Connection,
        *,
        admission_generation: int,
    ) -> None:
        """Validate the Backend-issued generation carried by a new request.

        Global freeze and grant issuance are Backend authority. Historical
        Supervisor schema-upgrade ledgers must not influence current work.
        """

        del cls, connection
        if admission_generation <= 0:
            raise PermissionError("RUNTIME_ADMISSION_GENERATION_MISMATCH")

    @classmethod
    def _claim_next_queued_sync(
        cls,
        connection: sqlite3.Connection,
        session_id: str,
        accepted_at: str,
    ) -> StoredRun | None:
        placeholders = ",".join("?" for _ in ACTIVE_RUN_STATES)
        active = connection.execute(
            f"SELECT 1 FROM runs WHERE session_id = ? "
            f"AND state IN ({placeholders}) LIMIT 1",
            (session_id, *sorted(ACTIVE_RUN_STATES)),
        ).fetchone()
        if active is not None:
            return None
        head = connection.execute(
            """
            SELECT kind, run_id, redirect_id, state
            FROM input_admission_reservations
            WHERE session_id = ? AND state <> 'completed'
            ORDER BY input_admission_seq LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if head is None:
            return None
        promoted_candidate = connection.execute(
            """
            SELECT r.input_admission_seq, d.promoted_run_id
            FROM input_admission_reservations AS r
            JOIN run_redirects AS d ON d.redirect_id = r.redirect_id
            WHERE r.session_id = ? AND r.kind = 'redirect'
              AND r.state = 'promoted' AND d.state = 'PROMOTED_TO_RUN'
            ORDER BY r.input_admission_seq LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if promoted_candidate is not None:
            blockers = connection.execute(
                """
                SELECT r.kind, r.run_id, q.scheduled_task_id
                FROM input_admission_reservations AS r
                LEFT JOIN runs AS q ON q.run_id = r.run_id
                WHERE r.session_id = ? AND r.state <> 'completed'
                  AND r.input_admission_seq < ?
                ORDER BY r.input_admission_seq
                """,
                (session_id, int(promoted_candidate["input_admission_seq"])),
            ).fetchall()
            if all(
                str(blocker["kind"]) == "ordinary_run"
                and blocker["scheduled_task_id"] is not None
                for blocker in blockers
            ):
                run_id = str(promoted_candidate["promoted_run_id"] or "")
            else:
                run_id = ""
        else:
            run_id = ""
        if run_id:
            pass
        elif str(head["kind"]) == "ordinary_run":
            run_id = str(head["run_id"] or "")
        elif str(head["kind"]) == "redirect" and str(head["state"]) == "promoted":
            promoted = connection.execute(
                """
                SELECT promoted_run_id FROM run_redirects
                WHERE redirect_id = ? AND state = 'PROMOTED_TO_RUN'
                """,
                (str(head["redirect_id"]),),
            ).fetchone()
            run_id = str(promoted["promoted_run_id"] or "") if promoted else ""
        else:
            return None
        if not run_id:
            raise SQLiteStoreError("input admission head has no linked Run")
        row = connection.execute(
            """
            SELECT run_id FROM runs
            WHERE run_id = ? AND session_id = ? AND state = ?
            """,
            (run_id, session_id, QUEUED),
        ).fetchone()
        if row is None:
            raise SQLiteStoreError("input admission head Run is not queued")
        cursor = connection.execute(
            """
            UPDATE runs SET state = ?, accepted_at = ?
            WHERE run_id = ? AND state = ?
            """,
            (ADMITTING, accepted_at, run_id, QUEUED),
        )
        if cursor.rowcount != 1:
            return None
        connection.execute(
            """
            UPDATE input_admission_reservations
            SET state = 'completed'
            WHERE run_id = ? AND kind = 'ordinary_run' AND state = 'bound'
            """,
            (run_id,),
        )
        connection.execute(
            """
            UPDATE input_admission_reservations
            SET state = 'completed'
            WHERE kind = 'redirect' AND state = 'promoted'
              AND redirect_id IN (
                  SELECT redirect_id FROM run_redirects
                  WHERE promoted_run_id = ?
              )
            """,
            (run_id,),
        )
        return cls._load_run_sync(connection, run_id)

    @classmethod
    def _requeue_admitting_sync(
        cls,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> StoredRun:
        cursor = connection.execute(
            """
            UPDATE runs SET state = ?, accepted_at = NULL
            WHERE run_id = ? AND state = ?
            """,
            (QUEUED, run_id, ADMITTING),
        )
        if cursor.rowcount != 1:
            raise SQLiteStoreError("only an admitting Run can be requeued")
        connection.execute(
            """
            UPDATE input_admission_reservations
            SET state = 'bound'
            WHERE run_id = ? AND kind = 'ordinary_run' AND state = 'completed'
            """,
            (run_id,),
        )
        return cls._load_run_sync(connection, run_id)

    @classmethod
    def _record_run_history_checkpoint_sync(
        cls,
        connection: sqlite3.Connection,
        run_id: str,
        history_message_count_before_run: int,
    ) -> StoredRun:
        cursor = connection.execute(
            """
            UPDATE runs SET history_message_count_before_run = ?
            WHERE run_id = ? AND state = ?
              AND history_message_count_before_run IS NULL
            """,
            (history_message_count_before_run, run_id, ADMITTING),
        )
        if cursor.rowcount != 1:
            existing = cls._load_run_sync(connection, run_id)
            if (
                existing.history_message_count_before_run
                != history_message_count_before_run
            ):
                raise SQLiteStoreError("Run history checkpoint conflicts")
            return existing
        return cls._load_run_sync(connection, run_id)

    @classmethod
    def _cancel_queued_run_sync(
        cls,
        connection: sqlite3.Connection,
        run_id: str,
        canceled_at: str,
    ) -> str:
        queued = connection.execute(
            "SELECT run_id FROM runs WHERE run_id = ? AND state IN (?, ?)",
            (run_id, QUEUED, ADMITTING),
        ).fetchone()
        if queued is None:
            return "already_terminal"
        connection.execute(
            "UPDATE runs SET state = 'CANCELED' WHERE run_id = ? AND state IN (?, ?)",
            (run_id, QUEUED, ADMITTING),
        )
        connection.execute(
            "UPDATE input_admission_reservations SET state = 'completed' "
            "WHERE run_id = ? AND kind = 'ordinary_run'",
            (run_id,),
        )
        canceled = cls._load_run_sync(connection, run_id)
        cls._enqueue_projection_sync(
            connection,
            f"{run_id}:terminal",
            "run.terminal",
            cls._json(
                {
                    "run_id": canceled.run_id,
                    "session_id": canceled.session_id,
                    "state": "CANCELED",
                    "answer": "",
                    "terminal_seq": 0,
                    "accepted_at": canceled.enqueued_at.isoformat(),
                    "terminal_at": canceled_at,
                    "client_message_id": canceled.client_message_id,
                    "config_revision": canceled.config_revision,
                    "source": canceled.source,
                    "user_id": canceled.user_id,
                    "org_id": canceled.org_id,
                    "profile_id": canceled.profile_id,
                    "model_id": canceled.model_id,
                    "prompt": canceled.audit_prompt,
                }
            ),
            canceled_at,
        )
        return "canceled"

    @classmethod
    def _cancel_queued_scheduled_task_sync(
        cls,
        connection: sqlite3.Connection,
        session_id: str,
        scheduled_task_id: int,
        canceled_at: str,
    ) -> str:
        queued = connection.execute(
            """
            SELECT run_id FROM runs
            WHERE session_id = ? AND scheduled_task_id = ? AND state = ?
            """,
            (session_id, scheduled_task_id, QUEUED),
        ).fetchone()
        if queued is not None:
            run_id = str(queued["run_id"])
            connection.execute(
                "UPDATE runs SET state = ? WHERE run_id = ? AND state = ?",
                ("CANCELED", run_id, QUEUED),
            )
            connection.execute(
                """
                UPDATE input_admission_reservations
                SET state = 'completed'
                WHERE run_id = ? AND kind = 'ordinary_run'
                """,
                (run_id,),
            )
            canceled = cls._load_run_sync(connection, run_id)
            cls._enqueue_projection_sync(
                connection,
                f"{run_id}:terminal",
                "run.terminal",
                cls._json(
                    {
                        "run_id": run_id,
                        "session_id": canceled.session_id,
                        "state": "CANCELED",
                        "answer": "",
                        "terminal_seq": 0,
                        "accepted_at": canceled.enqueued_at.isoformat(),
                        "terminal_at": canceled_at,
                        "client_message_id": canceled.client_message_id,
                        "config_revision": canceled.config_revision,
                        "source": canceled.source,
                        "user_id": canceled.user_id,
                        "org_id": canceled.org_id,
                        "profile_id": canceled.profile_id,
                        "model_id": canceled.model_id,
                        "prompt": canceled.audit_prompt,
                    }
                ),
                canceled_at,
            )
            return "canceled"
        active = connection.execute(
            """
            SELECT 1 FROM runs
            WHERE session_id = ? AND scheduled_task_id = ?
              AND state IN (
                'ADMITTING', 'DISPATCH_INTENT_DURABLE', 'ACKNOWLEDGED',
                'RUNNING', 'RECOVERING'
              )
            LIMIT 1
            """,
            (session_id, scheduled_task_id),
        ).fetchone()
        return "already_running" if active is not None else "not_found"

    @classmethod
    def _acknowledge_channel_delivery_sync(
        cls,
        connection: sqlite3.Connection,
        run_id: str,
        highest_contiguous_seq: int,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        provider_request_id: str,
        provider_message_id: str | None,
        projection_version: int,
        confirmed_at: str,
    ) -> StoredChannelDeliveryCheckpoint:
        if connection.execute(
            "SELECT 1 FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone() is None:
            raise SQLiteStoreError("Run does not exist")
        now = cls._utc_iso(datetime.now(timezone.utc))
        connection.execute(
            """
            INSERT INTO channel_delivery_checkpoints(
                run_id, highest_contiguous_seq, gateway_instance_id,
                channel_lease_epoch, provider_request_id,
                provider_message_id, projection_version, confirmed_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                highest_contiguous_seq = excluded.highest_contiguous_seq,
                gateway_instance_id = excluded.gateway_instance_id,
                channel_lease_epoch = excluded.channel_lease_epoch,
                provider_request_id = excluded.provider_request_id,
                provider_message_id = excluded.provider_message_id,
                projection_version = excluded.projection_version,
                confirmed_at = excluded.confirmed_at,
                updated_at = excluded.updated_at
            WHERE excluded.highest_contiguous_seq >=
                  channel_delivery_checkpoints.highest_contiguous_seq
            """,
            (
                run_id,
                highest_contiguous_seq,
                gateway_instance_id,
                channel_lease_epoch,
                provider_request_id,
                provider_message_id,
                projection_version,
                confirmed_at,
                now,
            ),
        )
        row = connection.execute(
            """
            SELECT run_id, highest_contiguous_seq, gateway_instance_id,
                   channel_lease_epoch, provider_request_id,
                   provider_message_id, projection_version, confirmed_at
            FROM channel_delivery_checkpoints WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        assert row is not None
        return cls._row_to_delivery_checkpoint(row)

    @classmethod
    def _bind_channel_fast_lane_v2_run_sync(
        cls,
        connection: sqlite3.Connection,
        delivery_id: str,
        run_id: str,
        payload_digest: str,
        bound_at: str,
    ) -> StoredChannelFastLaneV2Delivery:
        run = connection.execute(
            """
            SELECT run_id, session_id, client_message_id, payload_digest
            FROM runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            raise SQLiteStoreError("Fast Lane v2 Run does not exist")
        if str(run["payload_digest"]) != payload_digest:
            raise IdempotencyConflict("Fast Lane v2 Run payload conflicts")
        return cls._bind_channel_fast_lane_v2_delivery_sync(
            connection,
            delivery_id=delivery_id,
            session_id=str(run["session_id"]),
            client_message_id=str(run["client_message_id"]),
            payload_digest=payload_digest,
            target_kind="ordinary_run",
            run_id=run_id,
            redirect_id=None,
            canonical_run_id=run_id,
            parent_run_id=None,
            disposition_state="ordinary_run_accepted",
            start_after_seq=0,
            promoted_run_id=None,
            bound_at=bound_at,
        )

    @classmethod
    def _bind_channel_fast_lane_v2_redirect_sync(
        cls,
        connection: sqlite3.Connection,
        delivery_id: str,
        redirect_id: str,
        payload_digest: str,
        bound_at: str,
    ) -> StoredChannelFastLaneV2Delivery:
        redirect = connection.execute(
            "SELECT * FROM run_redirects WHERE redirect_id = ?",
            (redirect_id,),
        ).fetchone()
        if redirect is None:
            raise SQLiteStoreError("Fast Lane v2 redirect does not exist")
        current_delivery_id = redirect["delivery_id"]
        if current_delivery_id is not None and str(current_delivery_id) != delivery_id:
            raise IdempotencyConflict("redirect is bound to a different delivery")
        if current_delivery_id is None:
            connection.execute(
                "UPDATE run_redirects SET delivery_id = ? WHERE redirect_id = ?",
                (delivery_id, redirect_id),
            )
        (
            disposition_state,
            canonical_run_id,
            parent_run_id,
            start_after_seq,
            promoted_run_id,
        ) = cls._redirect_fast_lane_v2_disposition_values(redirect)
        bound = cls._bind_channel_fast_lane_v2_delivery_sync(
            connection,
            delivery_id=delivery_id,
            session_id=str(redirect["session_id"]),
            client_message_id=str(redirect["client_message_id"]),
            payload_digest=payload_digest,
            target_kind="redirect",
            run_id=None,
            redirect_id=redirect_id,
            canonical_run_id=canonical_run_id,
            parent_run_id=parent_run_id,
            disposition_state=disposition_state,
            start_after_seq=start_after_seq,
            promoted_run_id=promoted_run_id,
            bound_at=bound_at,
        )
        return cls._refresh_channel_fast_lane_v2_delivery_sync(
            connection,
            bound.delivery_id,
            bound_at,
        )

    @classmethod
    def _bind_channel_fast_lane_v2_delivery_sync(
        cls,
        connection: sqlite3.Connection,
        *,
        delivery_id: str,
        session_id: str,
        client_message_id: str,
        payload_digest: str,
        target_kind: str,
        run_id: str | None,
        redirect_id: str | None,
        canonical_run_id: str,
        parent_run_id: str | None,
        disposition_state: str,
        start_after_seq: int | None,
        promoted_run_id: str | None,
        bound_at: str,
    ) -> StoredChannelFastLaneV2Delivery:
        existing = connection.execute(
            """
            SELECT * FROM channel_fast_lane_v2_deliveries
            WHERE delivery_id = ?
            """,
            (delivery_id,),
        ).fetchone()
        if existing is None:
            existing = connection.execute(
                """
                SELECT * FROM channel_fast_lane_v2_deliveries
                WHERE session_id = ? AND client_message_id = ?
                """,
                (session_id, client_message_id),
            ).fetchone()
            if existing is not None:
                raise IdempotencyConflict(
                    "client_message_id is bound to a different Fast Lane v2 delivery"
                )
        if existing is not None:
            if (
                str(existing["delivery_id"]) != delivery_id
                or str(existing["session_id"]) != session_id
                or str(existing["client_message_id"]) != client_message_id
                or str(existing["payload_digest"]) != payload_digest
                or str(existing["target_kind"]) != target_kind
                or existing["run_id"] != run_id
                or existing["redirect_id"] != redirect_id
            ):
                raise IdempotencyConflict("Fast Lane v2 delivery identity conflicts")
            return cls._row_to_channel_fast_lane_v2_delivery(existing)
        connection.execute(
            """
            INSERT INTO channel_fast_lane_v2_deliveries(
                delivery_id, session_id, client_message_id, payload_digest,
                target_kind, run_id, redirect_id, canonical_run_id,
                parent_run_id, disposition_seq, disposition_state,
                start_after_seq, promoted_run_id, disposition_ack_seq,
                provider_confirmed_seq, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                delivery_id,
                session_id,
                client_message_id,
                payload_digest,
                target_kind,
                run_id,
                redirect_id,
                canonical_run_id,
                parent_run_id,
                disposition_state,
                start_after_seq,
                promoted_run_id,
                bound_at,
                bound_at,
            ),
        )
        row = connection.execute(
            "SELECT * FROM channel_fast_lane_v2_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        assert row is not None
        return cls._row_to_channel_fast_lane_v2_delivery(row)

    @classmethod
    def _refresh_channel_fast_lane_v2_delivery_sync(
        cls,
        connection: sqlite3.Connection,
        delivery_id: str,
        refreshed_at: str,
    ) -> StoredChannelFastLaneV2Delivery:
        current = connection.execute(
            """
            SELECT * FROM channel_fast_lane_v2_deliveries
            WHERE delivery_id = ?
            """,
            (delivery_id,),
        ).fetchone()
        if current is None:
            raise SQLiteStoreError("Fast Lane v2 delivery does not exist")
        if str(current["target_kind"]) == "ordinary_run":
            return cls._row_to_channel_fast_lane_v2_delivery(current)
        redirect_id = str(current["redirect_id"] or "")
        redirect = connection.execute(
            "SELECT * FROM run_redirects WHERE redirect_id = ?",
            (redirect_id,),
        ).fetchone()
        if redirect is None:
            raise SQLiteStoreError("Fast Lane v2 redirect ledger is missing")
        (
            disposition_state,
            canonical_run_id,
            parent_run_id,
            start_after_seq,
            promoted_run_id,
        ) = cls._redirect_fast_lane_v2_disposition_values(redirect)
        exposed = (
            disposition_state,
            canonical_run_id,
            parent_run_id,
            start_after_seq,
            promoted_run_id,
        )
        persisted = (
            str(current["disposition_state"]),
            str(current["canonical_run_id"]),
            (
                str(current["parent_run_id"])
                if current["parent_run_id"] is not None
                else None
            ),
            (
                int(current["start_after_seq"])
                if current["start_after_seq"] is not None
                else None
            ),
            (
                str(current["promoted_run_id"])
                if current["promoted_run_id"] is not None
                else None
            ),
        )
        if exposed != persisted:
            canonical_changed = canonical_run_id != str(current["canonical_run_id"])
            provider_confirmed_seq = (
                0 if canonical_changed else int(current["provider_confirmed_seq"])
            )
            if start_after_seq is not None and disposition_state in {
                "ordinary_run_accepted",
                "redirect_accepted",
                "redirect_consumed",
                "promoted",
            }:
                checkpoint = connection.execute(
                    """
                    SELECT highest_contiguous_seq
                    FROM channel_fast_lane_v2_checkpoints
                    WHERE canonical_run_id = ?
                    """,
                    (canonical_run_id,),
                ).fetchone()
                if (
                    checkpoint is not None
                    and int(checkpoint["highest_contiguous_seq"])
                    > start_after_seq
                ):
                    provider_confirmed_seq = max(
                        provider_confirmed_seq,
                        int(checkpoint["highest_contiguous_seq"]),
                    )
            connection.execute(
                """
                UPDATE channel_fast_lane_v2_deliveries SET
                    canonical_run_id = ?, parent_run_id = ?,
                    disposition_seq = disposition_seq + 1,
                    disposition_state = ?, start_after_seq = ?,
                    promoted_run_id = ?,
                    provider_confirmed_seq = ?,
                    updated_at = ?
                WHERE delivery_id = ?
                """,
                (
                    canonical_run_id,
                    parent_run_id,
                    disposition_state,
                    start_after_seq,
                    promoted_run_id,
                    provider_confirmed_seq,
                    refreshed_at,
                    delivery_id,
                ),
            )
        row = connection.execute(
            "SELECT * FROM channel_fast_lane_v2_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        assert row is not None
        return cls._row_to_channel_fast_lane_v2_delivery(row)

    @staticmethod
    def _redirect_fast_lane_v2_disposition_values(
        redirect: sqlite3.Row,
    ) -> tuple[str, str, str, int | None, str | None]:
        state = str(redirect["state"])
        parent_run_id = str(redirect["parent_run_id"])
        if state in {
            "WAITING_AGENT_READY",
            "WAITING_REDIRECT_BOUNDARY",
            "DISPATCH_INTENT",
            "ISOLATING_UNCERTAIN",
        }:
            return "pending", parent_run_id, parent_run_id, None, None
        if state == "RPC_ACKNOWLEDGED":
            start_after_seq = redirect["start_after_seq"]
            if start_after_seq is None or int(start_after_seq) <= 0:
                raise SQLiteStoreError("redirect accepted boundary is missing")
            return (
                "redirect_accepted",
                parent_run_id,
                parent_run_id,
                int(start_after_seq),
                None,
            )
        if state == "CANONICAL_CONSUMED":
            start_after_seq = redirect["start_after_seq"]
            if start_after_seq is None or int(start_after_seq) <= 0:
                raise SQLiteStoreError("redirect consumed boundary is missing")
            return (
                "redirect_consumed",
                parent_run_id,
                parent_run_id,
                int(start_after_seq),
                None,
            )
        if state == "PROMOTED_TO_RUN":
            promoted_run_id = redirect["promoted_run_id"]
            if promoted_run_id is None:
                raise SQLiteStoreError("promoted redirect Run is missing")
            promoted = str(promoted_run_id)
            return "promoted", promoted, parent_run_id, 0, promoted
        if state == "SUBMISSION_UNCERTAIN":
            raw_start = redirect["start_after_seq"]
            start_after_seq = int(raw_start) if raw_start is not None else None
            return (
                "uncertain",
                parent_run_id,
                parent_run_id,
                start_after_seq,
                None,
            )
        if state == "CANCELED_WITH_PARENT":
            return "canceled", parent_run_id, parent_run_id, None, None
        raise SQLiteStoreError(f"unsupported redirect disposition state: {state}")

    @classmethod
    def _refresh_channel_fast_lane_v2_for_group_sync(
        cls,
        connection: sqlite3.Connection,
        dispatch_group_id: str,
        refreshed_at: str,
    ) -> None:
        rows = connection.execute(
            """
            SELECT delivery_id FROM channel_fast_lane_v2_deliveries
            WHERE redirect_id IN (
                SELECT redirect_id FROM run_redirects
                WHERE dispatch_group_id = ?
            )
            """,
            (dispatch_group_id,),
        ).fetchall()
        for row in rows:
            cls._refresh_channel_fast_lane_v2_delivery_sync(
                connection,
                str(row["delivery_id"]),
                refreshed_at,
            )

    @classmethod
    def _refresh_channel_fast_lane_v2_for_parent_sync(
        cls,
        connection: sqlite3.Connection,
        parent_run_id: str,
        refreshed_at: str,
    ) -> None:
        rows = connection.execute(
            """
            SELECT delivery_id FROM channel_fast_lane_v2_deliveries
            WHERE redirect_id IN (
                SELECT redirect_id FROM run_redirects
                WHERE parent_run_id = ?
            )
            """,
            (parent_run_id,),
        ).fetchall()
        for row in rows:
            cls._refresh_channel_fast_lane_v2_delivery_sync(
                connection,
                str(row["delivery_id"]),
                refreshed_at,
            )

    @classmethod
    def _acknowledge_channel_fast_lane_v2_disposition_sync(
        cls,
        connection: sqlite3.Connection,
        delivery_id: str,
        disposition_seq: int,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        acknowledged_at: str,
    ) -> StoredChannelFastLaneV2Delivery:
        current = cls._refresh_channel_fast_lane_v2_delivery_sync(
            connection,
            delivery_id,
            acknowledged_at,
        )
        if disposition_seq > current.disposition_seq:
            raise IdempotencyConflict(
                "Fast Lane v2 disposition acknowledgement is ahead of durable state"
            )
        if disposition_seq > current.disposition_ack_seq:
            connection.execute(
                """
                UPDATE channel_fast_lane_v2_deliveries SET
                    disposition_ack_seq = ?,
                    disposition_ack_gateway_instance_id = ?,
                    disposition_ack_channel_lease_epoch = ?,
                    disposition_acked_at = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (
                    disposition_seq,
                    gateway_instance_id,
                    channel_lease_epoch,
                    acknowledged_at,
                    acknowledged_at,
                    delivery_id,
                ),
            )
        row = connection.execute(
            "SELECT * FROM channel_fast_lane_v2_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        assert row is not None
        return cls._row_to_channel_fast_lane_v2_delivery(row)

    @classmethod
    def _acknowledge_channel_fast_lane_v2_provider_sync(
        cls,
        connection: sqlite3.Connection,
        owner_delivery_id: str,
        canonical_run_id: str,
        highest_contiguous_seq: int,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        provider_request_id: str,
        provider_message_id: str | None,
        projection_version: int,
        confirmed_at: str,
    ) -> StoredChannelFastLaneV2Checkpoint:
        owner = connection.execute(
            """
            SELECT delivery_id FROM channel_fast_lane_v2_deliveries
            WHERE canonical_run_id = ? AND start_after_seq IS NOT NULL
              AND disposition_state IN (
                  'ordinary_run_accepted', 'redirect_accepted',
                  'redirect_consumed', 'promoted'
              )
            ORDER BY start_after_seq, delivery_id LIMIT 1
            """,
            (canonical_run_id,),
        ).fetchone()
        if owner is None:
            raise SQLiteStoreError("Fast Lane v2 canonical owner does not exist")
        if str(owner["delivery_id"]) != owner_delivery_id:
            raise IdempotencyConflict("Fast Lane v2 canonical owner conflicts")
        now = cls._utc_iso(datetime.now(timezone.utc))
        assert now is not None
        connection.execute(
            """
            INSERT INTO channel_fast_lane_v2_checkpoints(
                canonical_run_id, owner_delivery_id, highest_contiguous_seq,
                gateway_instance_id, channel_lease_epoch,
                provider_request_id, provider_message_id, projection_version,
                confirmed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_run_id) DO UPDATE SET
                owner_delivery_id = excluded.owner_delivery_id,
                highest_contiguous_seq = excluded.highest_contiguous_seq,
                gateway_instance_id = excluded.gateway_instance_id,
                channel_lease_epoch = excluded.channel_lease_epoch,
                provider_request_id = excluded.provider_request_id,
                provider_message_id = excluded.provider_message_id,
                projection_version = excluded.projection_version,
                confirmed_at = excluded.confirmed_at,
                updated_at = excluded.updated_at
            WHERE excluded.highest_contiguous_seq >=
                  channel_fast_lane_v2_checkpoints.highest_contiguous_seq
            """,
            (
                canonical_run_id,
                owner_delivery_id,
                highest_contiguous_seq,
                gateway_instance_id,
                channel_lease_epoch,
                provider_request_id,
                provider_message_id,
                projection_version,
                confirmed_at,
                now,
            ),
        )
        checkpoint = connection.execute(
            """
            SELECT * FROM channel_fast_lane_v2_checkpoints
            WHERE canonical_run_id = ?
            """,
            (canonical_run_id,),
        ).fetchone()
        assert checkpoint is not None
        effective_seq = int(checkpoint["highest_contiguous_seq"])
        connection.execute(
            """
            UPDATE channel_fast_lane_v2_deliveries SET
                provider_confirmed_seq = CASE
                    WHEN provider_confirmed_seq < ? THEN ?
                    ELSE provider_confirmed_seq
                END,
                updated_at = ?
            WHERE canonical_run_id = ? AND start_after_seq IS NOT NULL
              AND start_after_seq < ?
              AND disposition_state IN (
                  'ordinary_run_accepted', 'redirect_accepted',
                  'redirect_consumed', 'promoted'
              )
            """,
            (
                effective_seq,
                effective_seq,
                now,
                canonical_run_id,
                effective_seq,
            ),
        )
        return cls._row_to_channel_fast_lane_v2_checkpoint(checkpoint)

    @staticmethod
    def _row_to_delivery_checkpoint(
        row: sqlite3.Row,
    ) -> StoredChannelDeliveryCheckpoint:
        return StoredChannelDeliveryCheckpoint(
            run_id=str(row["run_id"]),
            highest_contiguous_seq=int(row["highest_contiguous_seq"]),
            gateway_instance_id=str(row["gateway_instance_id"]),
            channel_lease_epoch=int(row["channel_lease_epoch"]),
            provider_request_id=str(row["provider_request_id"]),
            provider_message_id=(
                str(row["provider_message_id"])
                if row["provider_message_id"] is not None
                else None
            ),
            projection_version=int(row["projection_version"]),
            confirmed_at=datetime.fromisoformat(str(row["confirmed_at"])),
        )

    @staticmethod
    def _row_to_channel_fast_lane_v2_delivery(
        row: sqlite3.Row,
    ) -> StoredChannelFastLaneV2Delivery:
        return StoredChannelFastLaneV2Delivery(
            delivery_id=str(row["delivery_id"]),
            session_id=str(row["session_id"]),
            client_message_id=str(row["client_message_id"]),
            payload_digest=str(row["payload_digest"]),
            target_kind=str(row["target_kind"]),
            run_id=(str(row["run_id"]) if row["run_id"] is not None else None),
            redirect_id=(
                str(row["redirect_id"])
                if row["redirect_id"] is not None
                else None
            ),
            canonical_run_id=str(row["canonical_run_id"]),
            parent_run_id=(
                str(row["parent_run_id"])
                if row["parent_run_id"] is not None
                else None
            ),
            disposition_seq=int(row["disposition_seq"]),
            disposition_state=str(row["disposition_state"]),
            start_after_seq=(
                int(row["start_after_seq"])
                if row["start_after_seq"] is not None
                else None
            ),
            promoted_run_id=(
                str(row["promoted_run_id"])
                if row["promoted_run_id"] is not None
                else None
            ),
            disposition_ack_seq=int(row["disposition_ack_seq"]),
            provider_confirmed_seq=int(row["provider_confirmed_seq"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _row_to_channel_fast_lane_v2_checkpoint(
        row: sqlite3.Row,
    ) -> StoredChannelFastLaneV2Checkpoint:
        return StoredChannelFastLaneV2Checkpoint(
            canonical_run_id=str(row["canonical_run_id"]),
            owner_delivery_id=str(row["owner_delivery_id"]),
            highest_contiguous_seq=int(row["highest_contiguous_seq"]),
            gateway_instance_id=str(row["gateway_instance_id"]),
            channel_lease_epoch=int(row["channel_lease_epoch"]),
            provider_request_id=str(row["provider_request_id"]),
            provider_message_id=(
                str(row["provider_message_id"])
                if row["provider_message_id"] is not None
                else None
            ),
            projection_version=int(row["projection_version"]),
            confirmed_at=datetime.fromisoformat(str(row["confirmed_at"])),
        )

    def _transition_run_sync(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        state: str,
        acknowledged_at: str | None,
        terminal_at: str | None,
    ) -> StoredRun:
        row = connection.execute(
            "SELECT state FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise SQLiteStoreError("Run does not exist")
        current = str(row["state"])
        if current in TERMINAL_STATES and state != current:
            raise SQLiteStoreError("terminal Run state cannot transition")
        connection.execute(
            """
            UPDATE runs
            SET state = ?,
                acknowledged_at = COALESCE(?, acknowledged_at),
                terminal_at = COALESCE(?, terminal_at)
            WHERE run_id = ?
            """,
            (state, acknowledged_at, terminal_at, run_id),
        )
        if state in TERMINAL_STATES:
            connection.execute(
                """
                UPDATE input_admission_reservations
                SET state = 'completed'
                WHERE run_id = ? AND kind = 'ordinary_run'
                  AND state IN ('bound', 'promoted')
                """,
                (run_id,),
            )
        full = connection.execute(
            """
            SELECT run_id, session_id, client_message_id, payload_digest,
                   config_revision, source, state, accepted_at,
                   enqueued_at, enqueue_seq, prompt, audit_prompt,
                   identity_json, source_detail_json, scheduled_task_id,
                   scheduled_occurrence_key, coalesced_count,
                   last_coalesced_at, history_message_count_before_run,
                   user_id, org_id, profile_id, model_id,
                   runtime_provider_run_id, cancellation_requested_at
            FROM runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        assert full is not None
        return self._row_to_run(full, created=False)

    @classmethod
    def _interrupt_unfinished_runs_for_restart_sync(
        cls,
        connection: sqlite3.Connection,
        interrupted_at: str,
    ) -> list[dict[str, str]]:
        rows = connection.execute(
            """
            SELECT run_id, session_id, client_message_id, config_revision,
                   source, accepted_at, enqueued_at, audit_prompt,
                   user_id, org_id, profile_id, model_id, answer_snapshot,
                   answer_through_seq
            FROM runs
            WHERE state NOT IN (
                'COMPLETED','FAILED','CANCELED','FAILED_SUBMISSION_UNCERTAIN',
                'RUN_INTERRUPTED_BY_RUNTIME_RESTART',
                'RUN_INTERRUPTED_BY_RUNTIME_TERMINATION',
                'RUN_INTERRUPTED_BY_ADMIN_RESTART'
            )
            ORDER BY enqueue_seq, run_id
            """
        ).fetchall()
        receipts: list[dict[str, str]] = []
        for row in rows:
            run_id = str(row["run_id"])
            session_id = str(row["session_id"])
            seq_row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            seq = int(seq_row["next_seq"])
            event_payload = cls._json(
                {"status": "canceled", "reason": "runtime_restart"}
            )
            connection.execute(
                """
                INSERT INTO run_events(run_id, seq, event_type, payload_json, occurred_at)
                VALUES (?, ?, 'chat.done', ?, ?)
                ON CONFLICT(run_id, seq) DO NOTHING
                """,
                (run_id, seq, event_payload, interrupted_at),
            )
            connection.execute(
                """
                UPDATE runs
                SET state = 'RUN_INTERRUPTED_BY_RUNTIME_RESTART',
                    cancellation_requested_at = COALESCE(cancellation_requested_at, ?),
                    terminal_at = COALESCE(terminal_at, ?),
                    answer_through_seq = MAX(answer_through_seq, ?)
                WHERE run_id = ?
                """,
                (interrupted_at, interrupted_at, seq, run_id),
            )
            connection.execute(
                "UPDATE input_admission_reservations SET state='completed' WHERE run_id=? AND kind='ordinary_run' AND state IN ('bound','promoted')",
                (run_id,),
            )
            accepted_at = str(row["accepted_at"] or row["enqueued_at"] or interrupted_at)
            terminal_payload = cls._json(
                {
                    "run_id": run_id,
                    "session_id": session_id,
                    "state": "RUN_INTERRUPTED_BY_RUNTIME_RESTART",
                    "answer": str(row["answer_snapshot"] or ""),
                    "terminal_seq": seq,
                    "accepted_at": accepted_at,
                    "terminal_at": interrupted_at,
                    "client_message_id": str(row["client_message_id"]),
                    "config_revision": str(row["config_revision"]),
                    "source": str(row["source"]),
                    "user_id": int(row["user_id"]),
                    "org_id": int(row["org_id"]),
                    "profile_id": str(row["profile_id"]),
                    "model_id": str(row["model_id"]),
                    "prompt": str(row["audit_prompt"] or ""),
                }
            )
            connection.execute(
                """
                INSERT INTO projection_outbox(projection_id, projection_type, payload_json, created_at)
                VALUES (?, 'run.terminal', ?, ?)
                ON CONFLICT(projection_id) DO NOTHING
                """,
                (f"{run_id}:terminal", terminal_payload, interrupted_at),
            )
            receipts.append({"run_id": run_id, "session_id": session_id})
        return receipts

    @classmethod
    def _set_run_provider_sync(
        cls,
        connection: sqlite3.Connection,
        run_id: str,
        runtime_provider_run_id: str,
    ) -> StoredRun:
        cursor = connection.execute(
            """
            UPDATE runs
            SET runtime_provider_run_id = ?
            WHERE run_id = ?
            """,
            (runtime_provider_run_id, run_id),
        )
        if cursor.rowcount != 1:
            raise SQLiteStoreError("Run does not exist")
        return cls._load_run_sync(connection, run_id)

    @classmethod
    def _request_run_cancellation_sync(
        cls,
        connection: sqlite3.Connection,
        run_id: str,
        requested_at: str,
    ) -> StoredRun:
        cursor = connection.execute(
            """
            UPDATE runs
            SET cancellation_requested_at = COALESCE(
                cancellation_requested_at,
                ?
            )
            WHERE run_id = ?
            """,
            (requested_at, run_id),
        )
        if cursor.rowcount != 1:
            raise SQLiteStoreError("Run does not exist")
        return cls._load_run_sync(connection, run_id)

    @classmethod
    def _load_run_sync(
        cls,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        created: bool = False,
    ) -> StoredRun:
        row = connection.execute(
            """
            SELECT run_id, session_id, client_message_id, payload_digest,
                   config_revision, source, state, accepted_at,
                   enqueued_at, enqueue_seq, prompt, audit_prompt,
                   identity_json, source_detail_json, scheduled_task_id,
                   scheduled_occurrence_key, coalesced_count,
                   last_coalesced_at, history_message_count_before_run,
                   user_id, org_id, profile_id, model_id,
                   runtime_provider_run_id, cancellation_requested_at
            FROM runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise SQLiteStoreError("Run does not exist")
        return cls._row_to_run(row, created=created)

    @staticmethod
    def _append_run_event_sync(
        connection: sqlite3.Connection,
        run_id: str,
        seq: int,
        event_type: str,
        payload_json: str,
        occurred_at: str,
    ) -> bool:
        cursor = connection.execute(
            """
            INSERT INTO run_events(run_id, seq, event_type, payload_json, occurred_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id, seq) DO NOTHING
            """,
            (run_id, seq, event_type, payload_json, occurred_at),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _save_answer_snapshot_sync(
        connection: sqlite3.Connection,
        run_id: str,
        answer: str,
        through_seq: int,
    ) -> bool:
        cursor = connection.execute(
            """
            UPDATE runs
            SET answer_snapshot = ?, answer_through_seq = ?
            WHERE run_id = ? AND answer_through_seq <= ?
            """,
            (answer, through_seq, run_id, through_seq),
        )
        return cursor.rowcount == 1

    @classmethod
    def _upsert_session_sync(
        cls,
        connection: sqlite3.Connection,
        session_id: str,
        prepare_identity_digest: str,
        state: str,
        last_activity_at: str,
        prepare_identity_json: str | None,
    ) -> StoredSessionEnvelope:
        connection.execute(
            """
            INSERT INTO session_envelopes(
                session_id, prepare_identity_digest, state,
                last_activity_at, prepare_identity_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                prepare_identity_digest = excluded.prepare_identity_digest,
                state = excluded.state,
                last_activity_at = excluded.last_activity_at,
                prepare_identity_json = COALESCE(
                    excluded.prepare_identity_json,
                    session_envelopes.prepare_identity_json
                ),
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                prepare_identity_digest,
                state,
                last_activity_at,
                prepare_identity_json,
                last_activity_at,
            ),
        )
        row = connection.execute(
            """
            SELECT session_id, prepare_identity_digest, state,
                   last_activity_at, prepare_identity_json
            FROM session_envelopes WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        assert row is not None
        return cls._row_to_session(row)

    @classmethod
    def _persist_command_sync(
        cls,
        connection: sqlite3.Connection,
        command_id: str,
        payload_digest: str,
        command_type: str,
        state: str,
        result_json: str | None,
        updated_at: str,
    ) -> StoredCommand:
        row = connection.execute(
            """
            SELECT command_id, payload_digest, command_type, state, result_json
            FROM internal_commands WHERE command_id = ?
            """,
            (command_id,),
        ).fetchone()
        if row is not None:
            if row["payload_digest"] != payload_digest:
                raise IdempotencyConflict(
                    "command_id is already bound to a different payload"
                )
            return cls._row_to_command(row, created=False)
        connection.execute(
            """
            INSERT INTO internal_commands(
                command_id, payload_digest, command_type, state,
                result_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                command_id,
                payload_digest,
                command_type,
                state,
                result_json,
                updated_at,
            ),
        )
        return StoredCommand(
            command_id=command_id,
            payload_digest=payload_digest,
            command_type=command_type,
            state=state,
            result=json.loads(result_json) if result_json else None,
            created=True,
        )

    @classmethod
    def _complete_command_sync(
        cls,
        connection: sqlite3.Connection,
        command_id: str,
        state: str,
        result_json: str,
        updated_at: str,
    ) -> StoredCommand:
        cursor = connection.execute(
            """
            UPDATE internal_commands
            SET state = ?, result_json = ?, updated_at = ?
            WHERE command_id = ?
            """,
            (state, result_json, updated_at, command_id),
        )
        if cursor.rowcount != 1:
            raise SQLiteStoreError("Command does not exist")
        row = connection.execute(
            """
            SELECT command_id, payload_digest, command_type, state, result_json
            FROM internal_commands WHERE command_id = ?
            """,
            (command_id,),
        ).fetchone()
        assert row is not None
        return cls._row_to_command(row, created=False)

    @staticmethod
    def _abandon_command_sync(
        connection: sqlite3.Connection,
        command_id: str,
        payload_digest: str,
    ) -> bool:
        cursor = connection.execute(
            """
            DELETE FROM internal_commands
            WHERE command_id = ? AND payload_digest = ?
            """,
            (command_id, payload_digest),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _enqueue_projection_sync(
        connection: sqlite3.Connection,
        projection_id: str,
        projection_type: str,
        payload_json: str,
        created_at: str,
    ) -> bool:
        cursor = connection.execute(
            """
            INSERT INTO projection_outbox(
                projection_id, projection_type, payload_json, created_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(projection_id) DO NOTHING
            """,
            (projection_id, projection_type, payload_json, created_at),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _acknowledge_projections_sync(
        connection: sqlite3.Connection,
        projection_ids: tuple[str, ...],
        acknowledged_at: str,
    ) -> int:
        placeholders = ",".join("?" for _ in projection_ids)
        cursor = connection.execute(
            f"""
            UPDATE projection_outbox
            SET acknowledged_at = ?
            WHERE acknowledged_at IS NULL
              AND projection_id IN ({placeholders})
            """,
            (acknowledged_at, *projection_ids),
        )
        return int(cursor.rowcount)

    @staticmethod
    def _record_revocation_sync(
        connection: sqlite3.Connection,
        command_id: str,
        scope: str,
        subject_id: str,
        revoked_at: str,
        cancel_active_runs: int,
    ) -> bool:
        cursor = connection.execute(
            """
            INSERT INTO revocations(
                command_id, scope, subject_id, revoked_at, cancel_active_runs
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(command_id) DO NOTHING
            """,
            (command_id, scope, subject_id, revoked_at, cancel_active_runs),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _consume_file_ticket_sync(
        connection: sqlite3.Connection,
        ticket_id: str,
        operation: str,
        expires_at: int,
        now: int,
        consumed_at: str,
    ) -> bool:
        connection.execute(
            "DELETE FROM consumed_file_tickets WHERE expires_at < ?",
            (now,),
        )
        cursor = connection.execute(
            """
            INSERT INTO consumed_file_tickets(
                ticket_id, operation, expires_at, consumed_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticket_id) DO NOTHING
            """,
            (ticket_id, operation, expires_at, consumed_at),
        )
        return cursor.rowcount == 1

    def _commit_dispatch_batch(
        self,
        intents: list[DispatchIntent],
    ) -> list[StoredRun | BaseException]:
        connection = self._require_connection()
        outcomes: list[StoredRun | BaseException] = []
        connection.execute("BEGIN IMMEDIATE")
        try:
            for intent in intents:
                redirect = connection.execute(
                    """
                    SELECT redirect_id, state, promoted_run_id,
                           promotion_reserved_run_id,
                           promotion_run_payload_digest
                    FROM run_redirects
                    WHERE session_id = ? AND client_message_id = ?
                    """,
                    (intent.session_id, intent.client_message_id),
                ).fetchone()
                linked_promoted_run = redirect is not None and (
                    str(redirect["state"]) == "PROMOTED_TO_RUN"
                    and str(redirect["promoted_run_id"] or "")
                    == intent.run_id
                    and str(redirect["promotion_reserved_run_id"])
                    == intent.run_id
                )
                if redirect is not None and not linked_promoted_run:
                    outcomes.append(
                        IdempotencyConflict(
                            "client_message_id is already bound to a redirect disposition"
                        )
                    )
                    continue
                row = connection.execute(
                    """
                    SELECT run_id, session_id, client_message_id, payload_digest,
                           config_revision, source, state, accepted_at,
                           enqueued_at, enqueue_seq, prompt, audit_prompt,
                           identity_json, source_detail_json, scheduled_task_id,
                           scheduled_occurrence_key, coalesced_count,
                           last_coalesced_at, history_message_count_before_run,
                           user_id, org_id, profile_id, model_id,
                           runtime_provider_run_id, cancellation_requested_at
                    FROM runs
                    WHERE session_id = ? AND client_message_id = ?
                    """,
                    (intent.session_id, intent.client_message_id),
                ).fetchone()
                if row is not None:
                    if row["payload_digest"] != intent.payload_digest:
                        outcomes.append(
                            IdempotencyConflict(
                                "client_message_id is already bound to a different payload"
                            )
                        )
                        continue
                    if str(row["state"]) == ADMITTING:
                        connection.execute(
                            """
                            UPDATE runs
                            SET state = ?, accepted_at = ?
                            WHERE run_id = ? AND state = ?
                            """,
                            (
                                DISPATCH_INTENT_DURABLE,
                                intent.accepted_at.astimezone(timezone.utc).isoformat(),
                                str(row["run_id"]),
                                ADMITTING,
                            ),
                        )
                    elif str(row["state"]) == QUEUED:
                        outcomes.append(
                            SQLiteStoreError(
                                "queued Run must be claimed before dispatch"
                            )
                        )
                        continue
                    stored = self._load_run_sync(
                        connection,
                        str(row["run_id"]),
                    )
                else:
                    accepted_iso = intent.accepted_at.astimezone(
                        timezone.utc
                    ).isoformat()
                    enqueue_seq = self._reserve_input_admission_sync(
                        connection,
                        session_id=intent.session_id,
                        client_message_id=intent.client_message_id,
                        kind="ordinary_run",
                        run_id=intent.run_id,
                        redirect_id=None,
                        created_at=accepted_iso,
                        state="completed",
                    )
                    identity_json = self._json(
                        {
                            "runtime_id": "",
                            "session_id": intent.session_id,
                            "profile_id": intent.profile_id,
                            "model_id": intent.model_id,
                            "config_revision": intent.config_revision,
                            "user_id": intent.user_id,
                            "org_id": intent.org_id,
                        }
                    )
                    connection.execute(
                        """
                        INSERT INTO runs(
                            run_id, session_id, client_message_id, payload_digest,
                            config_revision, source, state, accepted_at,
                            enqueued_at, enqueue_seq, prompt, audit_prompt,
                            identity_json, source_detail_json, scheduled_task_id,
                            scheduled_occurrence_key, coalesced_count,
                            last_coalesced_at, history_message_count_before_run,
                            user_id, org_id, profile_id, model_id,
                            runtime_provider_run_id, cancellation_requested_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL,
                            NULL, NULL, 0, NULL, NULL, ?, ?, ?, ?, NULL, NULL
                        )
                        """,
                        (
                            intent.run_id,
                            intent.session_id,
                            intent.client_message_id,
                            intent.payload_digest,
                            intent.config_revision,
                            intent.source,
                            DISPATCH_INTENT_DURABLE,
                            accepted_iso,
                            accepted_iso,
                            enqueue_seq,
                            intent.prompt,
                            intent.prompt,
                            identity_json,
                            intent.user_id,
                            intent.org_id,
                            intent.profile_id,
                            intent.model_id,
                        ),
                    )
                    stored = self._load_run_sync(
                        connection,
                        intent.run_id,
                        created=True,
                    )
                accepted_payload = {
                    "run_id": stored.run_id,
                    "session_id": stored.session_id,
                    "client_message_id": stored.client_message_id,
                    "config_revision": stored.config_revision,
                    "source": stored.source,
                    "source_detail": stored.source_detail,
                    "history_message_count_before_run": (
                        stored.history_message_count_before_run
                    ),
                    "accepted_at": intent.accepted_at.astimezone(
                        timezone.utc
                    ).isoformat(),
                    "user_id": stored.user_id,
                    "org_id": stored.org_id,
                    "profile_id": stored.profile_id,
                    "model_id": stored.model_id,
                    "prompt": stored.audit_prompt or intent.prompt,
                }
                self._enqueue_projection_sync(
                    connection,
                    f"{stored.run_id}:accepted",
                    "run.accepted",
                    self._json(accepted_payload),
                    intent.accepted_at.astimezone(timezone.utc).isoformat(),
                )
                outcomes.append(stored)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return outcomes

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise SQLiteStoreError("SQLiteStore has not been started")
        return self._connection

    @staticmethod
    def _row_to_run(row: sqlite3.Row, *, created: bool) -> StoredRun:
        raw_identity = row["identity_json"]
        identity = json.loads(raw_identity) if raw_identity else None
        if identity is not None and not isinstance(identity, dict):
            raise SQLiteStoreError("stored Run identity is not an object")
        raw_source_detail = row["source_detail_json"]
        source_detail = json.loads(raw_source_detail) if raw_source_detail else None
        if source_detail is not None and not isinstance(source_detail, dict):
            raise SQLiteStoreError("stored Run source detail is not an object")
        state = str(row["state"])
        accepted_at = (
            datetime.fromisoformat(str(row["accepted_at"]))
            if row["accepted_at"] is not None and state != QUEUED
            else None
        )
        return StoredRun(
            run_id=row["run_id"],
            session_id=row["session_id"],
            client_message_id=row["client_message_id"],
            payload_digest=row["payload_digest"],
            config_revision=row["config_revision"],
            source=row["source"],
            state=state,
            accepted_at=accepted_at,
            created=created,
            enqueued_at=datetime.fromisoformat(str(row["enqueued_at"])),
            enqueue_seq=int(row["enqueue_seq"]),
            prompt=str(row["prompt"] or ""),
            audit_prompt=str(row["audit_prompt"] or ""),
            identity=identity,
            source_detail=source_detail,
            scheduled_task_id=(
                int(row["scheduled_task_id"])
                if row["scheduled_task_id"] is not None
                else None
            ),
            scheduled_occurrence_key=(
                str(row["scheduled_occurrence_key"])
                if row["scheduled_occurrence_key"] is not None
                else None
            ),
            coalesced_count=int(row["coalesced_count"] or 0),
            last_coalesced_at=(
                datetime.fromisoformat(str(row["last_coalesced_at"]))
                if row["last_coalesced_at"] is not None
                else None
            ),
            history_message_count_before_run=(
                int(row["history_message_count_before_run"])
                if row["history_message_count_before_run"] is not None
                else None
            ),
            user_id=int(row["user_id"]),
            org_id=int(row["org_id"]),
            profile_id=str(row["profile_id"]),
            model_id=str(row["model_id"]),
            runtime_provider_run_id=(
                str(row["runtime_provider_run_id"])
                if row["runtime_provider_run_id"] is not None
                else None
            ),
            cancellation_requested_at=(
                datetime.fromisoformat(row["cancellation_requested_at"])
                if row["cancellation_requested_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> StoredRunEvent:
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise SQLiteStoreError("stored Run event payload is not an object")
        return StoredRunEvent(
            run_id=str(row["run_id"]),
            seq=int(row["seq"]),
            event_type=str(row["event_type"]),
            payload=payload,
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
        )

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> StoredSessionEnvelope:
        raw_identity = row["prepare_identity_json"]
        identity = json.loads(raw_identity) if raw_identity else None
        if identity is not None and not isinstance(identity, dict):
            raise SQLiteStoreError(
                "stored Prepare identity is not an object"
            )
        return StoredSessionEnvelope(
            session_id=str(row["session_id"]),
            prepare_identity_digest=str(row["prepare_identity_digest"]),
            state=str(row["state"]),
            last_activity_at=datetime.fromisoformat(row["last_activity_at"]),
            prepare_identity=identity,
        )

    @staticmethod
    def _row_to_command(row: sqlite3.Row, *, created: bool) -> StoredCommand:
        raw_result = row["result_json"]
        result = json.loads(raw_result) if raw_result else None
        if result is not None and not isinstance(result, dict):
            raise SQLiteStoreError("stored command result is not an object")
        return StoredCommand(
            command_id=str(row["command_id"]),
            payload_digest=str(row["payload_digest"]),
            command_type=str(row["command_type"]),
            state=str(row["state"]),
            result=result,
            created=created,
        )

    @staticmethod
    def _row_to_projection(row: sqlite3.Row) -> StoredProjection:
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise SQLiteStoreError("stored projection payload is not an object")
        return StoredProjection(
            outbox_id=int(row["outbox_id"]),
            projection_id=str(row["projection_id"]),
            projection_type=str(row["projection_type"]),
            payload=payload,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_redirect(
        row: sqlite3.Row,
        *,
        created: bool,
    ) -> StoredRedirect:
        raw_intent = json.loads(str(row["promotion_run_intent_json"]))
        if not isinstance(raw_intent, dict):
            raise SQLiteStoreError("stored promotion Run intent is not an object")
        return StoredRedirect(
            redirect_id=str(row["redirect_id"]),
            session_id=str(row["session_id"]),
            client_message_id=str(row["client_message_id"]),
            request_digest=str(row["request_digest"]),
            parent_run_id=str(row["parent_run_id"]),
            input_admission_seq=int(row["input_admission_seq"]),
            interaction_origin=str(row["interaction_origin"]),
            content=str(row["content"]),
            state=str(row["state"]),
            hermes_status=(
                str(row["hermes_status"])
                if row["hermes_status"] is not None
                else None
            ),
            dispatch_group_id=(
                str(row["dispatch_group_id"])
                if row["dispatch_group_id"] is not None
                else None
            ),
            dispatch_group_position=(
                int(row["dispatch_group_position"])
                if row["dispatch_group_position"] is not None
                else None
            ),
            pending_event_seq=int(row["pending_event_seq"]),
            accepted_event_seq=(
                int(row["accepted_event_seq"])
                if row["accepted_event_seq"] is not None
                else None
            ),
            consumed_event_seq=(
                int(row["consumed_event_seq"])
                if row["consumed_event_seq"] is not None
                else None
            ),
            terminal_disposition_kind=(
                str(row["terminal_disposition_kind"])
                if row["terminal_disposition_kind"] is not None
                else None
            ),
            terminal_disposition_event_seq=(
                int(row["terminal_disposition_event_seq"])
                if row["terminal_disposition_event_seq"] is not None
                else None
            ),
            start_after_seq=(
                int(row["start_after_seq"])
                if row["start_after_seq"] is not None
                else None
            ),
            delivery_id=(
                str(row["delivery_id"])
                if row["delivery_id"] is not None
                else None
            ),
            promoted_run_id=(
                str(row["promoted_run_id"])
                if row["promoted_run_id"] is not None
                else None
            ),
            promotion_reserved_run_id=str(row["promotion_reserved_run_id"]),
            promotion_run_intent=raw_intent,
            promotion_run_payload_digest=str(
                row["promotion_run_payload_digest"]
            ),
            promoted_pre_prompt_message_id=(
                int(row["promoted_pre_prompt_message_id"])
                if row["promoted_pre_prompt_message_id"] is not None
                else None
            ),
            promoted_pre_prompt_history_digest=(
                str(row["promoted_pre_prompt_history_digest"])
                if row["promoted_pre_prompt_history_digest"] is not None
                else None
            ),
            promoted_prompt_wire_digest=(
                str(row["promoted_prompt_wire_digest"])
                if row["promoted_prompt_wire_digest"] is not None
                else None
            ),
            promoted_proof_message_id=(
                int(row["promoted_proof_message_id"])
                if row["promoted_proof_message_id"] is not None
                else None
            ),
            promoted_proof_post_digest=(
                str(row["promoted_proof_post_digest"])
                if row["promoted_proof_post_digest"] is not None
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            created=created,
        )

    @staticmethod
    def _row_to_reservation(
        row: sqlite3.Row,
    ) -> InputAdmissionReservation:
        return InputAdmissionReservation(
            input_admission_seq=int(row["input_admission_seq"]),
            session_id=str(row["session_id"]),
            kind=str(row["kind"]),
            run_id=str(row["run_id"]) if row["run_id"] is not None else None,
            redirect_id=(
                str(row["redirect_id"])
                if row["redirect_id"] is not None
                else None
            ),
            client_message_id=str(row["client_message_id"]),
            state=str(row["state"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _row_to_redirect_group(
        row: sqlite3.Row,
    ) -> StoredRedirectDispatchGroup:
        return StoredRedirectDispatchGroup(
            dispatch_group_id=str(row["dispatch_group_id"]),
            session_id=str(row["session_id"]),
            parent_run_id=str(row["parent_run_id"]),
            aggregate_digest=str(row["aggregate_digest"]),
            raw_aggregate_digest=str(row["raw_aggregate_digest"]),
            wire_text=str(row["wire_text"]),
            wire_text_digest=str(row["wire_text_digest"]),
            member_count=int(row["member_count"]),
            state=str(row["state"]),
            hermes_status=(
                str(row["hermes_status"])
                if row["hermes_status"] is not None
                else None
            ),
            hermes_profile_key=(
                str(row["hermes_profile_key"])
                if row["hermes_profile_key"] is not None
                else None
            ),
            hermes_stored_session_id=(
                str(row["hermes_stored_session_id"])
                if row["hermes_stored_session_id"] is not None
                else None
            ),
            pre_dispatch_message_id=(
                int(row["pre_dispatch_message_id"])
                if row["pre_dispatch_message_id"] is not None
                else None
            ),
            pre_dispatch_history_digest=(
                str(row["pre_dispatch_history_digest"])
                if row["pre_dispatch_history_digest"] is not None
                else None
            ),
            pre_dispatch_model_request_seq=(
                int(row["pre_dispatch_model_request_seq"])
                if row["pre_dispatch_model_request_seq"] is not None
                else None
            ),
            pre_dispatch_model_request_digest=(
                str(row["pre_dispatch_model_request_digest"])
                if row["pre_dispatch_model_request_digest"] is not None
                else None
            ),
            dispatch_turn_id=(
                str(row["dispatch_turn_id"])
                if row["dispatch_turn_id"] is not None
                else None
            ),
            dispatch_turn_task_id=(
                str(row["dispatch_turn_task_id"])
                if row["dispatch_turn_task_id"] is not None
                else None
            ),
            dispatch_origin_evidence_digest=(
                str(row["dispatch_origin_evidence_digest"])
                if row["dispatch_origin_evidence_digest"] is not None
                else None
            ),
            dispatch_boundary_epoch_nonce=(
                str(row["dispatch_boundary_epoch_nonce"])
                if row["dispatch_boundary_epoch_nonce"] is not None
                else None
            ),
            dispatch_marker_request_nonce=(
                str(row["dispatch_marker_request_nonce"])
                if row["dispatch_marker_request_nonce"] is not None
                else None
            ),
            dispatch_peer_boot_id=(
                str(row["dispatch_peer_boot_id"])
                if row["dispatch_peer_boot_id"] is not None
                else None
            ),
            dispatch_peer_connection_id=(
                str(row["dispatch_peer_connection_id"])
                if row["dispatch_peer_connection_id"] is not None
                else None
            ),
            proof_message_id=(
                int(row["proof_message_id"])
                if row["proof_message_id"] is not None
                else None
            ),
            proof_kind=(
                str(row["proof_kind"])
                if row["proof_kind"] is not None
                else None
            ),
            proof_post_digest=(
                str(row["proof_post_digest"])
                if row["proof_post_digest"] is not None
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )



    @classmethod
    def _sha256_json(cls, value: object) -> str:
        return cls._sha256_bytes(cls._json(value).encode("utf-8"))

    @staticmethod
    def _sha256_bytes(value: bytes) -> str:
        return f"sha256:{hashlib.sha256(value).hexdigest()}"

    @staticmethod
    def _validate_sha256_digest(value: str) -> None:
        if (
            len(value) != 71
            or not value.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in value[7:])
        ):
            raise ValueError("digest must use sha256:<64 lowercase hex>")




    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _utc_iso(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
