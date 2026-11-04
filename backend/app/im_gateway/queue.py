from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, exists, func, not_, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    ApplicationAgentRun,
    ImChannel,
    ImChannelLease,
    ImDeliveryCheckpoint,
    ImExecutionBatch,
    ImIngressTicketCounter,
    ImInboundMessage,
    ImMessageAttachment,
)
from app.config import settings
from app.services.managed_secret import ManagedSecretCodec

from .messages import NormalizedWeComMessage
from .identity import wecom_member_digest


_UNFINISHED_INBOUND_STATUSES = (
    "received",
    "awaiting_identity",
    "staging_media",
    "queued",
    "running",
)
_COMPAT_CLAIMABLE_BATCH_STATES = ("submitting", "running")
_INGRESS_SETTLED_STATES = (
    "runtime_durable",
    "validation_rejected",
    "staging_failed",
    "dead_letter",
    "canceled",
)
_TERMINAL_FAST_LANE_CHECKPOINT_STATES = (
    "completed",
    "delivered",
)
_timing_logger = logging.getLogger("uvicorn.error.runtime_timing")
_logger = logging.getLogger(__name__)
_SAFE_CORRELATION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_MYSQL_DEADLOCK_ERROR_CODE = 1213
_APPLICATION_COMPLETION_DEADLOCK_RETRY_SECONDS = 0.05
_FAST_LANE_ROUTE_MAX_ATTEMPTS = 5


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_mysql_deadlock(error: OperationalError) -> bool:
    current: object | None = error.orig
    for _ in range(4):
        args = getattr(current, "args", ())
        if args and isinstance(args[0], int):
            return int(args[0]) == _MYSQL_DEADLOCK_ERROR_CODE
        current = getattr(current, "orig", None)
        if current is None:
            break
    return False


def _duration_ms(
    start: datetime | None,
    end: datetime | None,
) -> float | None:
    if start is None or end is None:
        return None
    elapsed = (aware_utc(end) - aware_utc(start)).total_seconds()
    if elapsed < 0:
        return None
    return round(elapsed * 1000, 3)


def im_timing_payload(
    row: ImInboundMessage,
    *,
    stage: str,
    status: str = "success",
    runtime_run_id: str | None = None,
    error_type: str | None = None,
) -> dict[str, object]:
    """Build a content-free timing payload from durable IM timestamps."""

    timestamps = {
        "provider_received": row.received_at,
        "db_committed": row.db_committed_at,
        "identity_resolved": row.identity_resolved_at,
        "scheduler_claimed": row.scheduler_claimed_at,
        "run_created": row.run_created_at,
        "runtime_submitted": row.runtime_submitted_at,
        "first_runtime_public_event": (
            row.first_runtime_public_event_at
        ),
        "first_provider_dispatch": row.first_provider_dispatch_at,
        "first_provider_confirmed": row.first_provider_confirmed_at,
    }
    pairs = (
        ("provider_received", "db_committed"),
        ("db_committed", "identity_resolved"),
        ("db_committed", "scheduler_claimed"),
        ("identity_resolved", "scheduler_claimed"),
        ("scheduler_claimed", "run_created"),
        ("run_created", "runtime_submitted"),
        ("runtime_submitted", "first_runtime_public_event"),
        ("first_runtime_public_event", "first_provider_dispatch"),
        ("first_provider_dispatch", "first_provider_confirmed"),
        ("provider_received", "first_provider_confirmed"),
    )
    durations: dict[str, float] = {}
    for start_name, end_name in pairs:
        value = _duration_ms(
            timestamps[start_name],
            timestamps[end_name],
        )
        if value is not None:
            durations[f"{start_name}_to_{end_name}"] = value
    target_type = str(row.target_type or "personal_agent")
    payload: dict[str, object] = {
        "component": (
            "application_agent_im"
            if target_type == "application_agent"
            else "personal_agent_im"
        ),
        "channel": "wecom",
        "stage": stage,
        "status": status,
        "inbound_message_id": int(row.id),
        "durations_ms": durations,
    }
    if target_type == "application_agent":
        payload["request_id"] = f"im-wecom-{int(row.id)}"
    if (
        runtime_run_id
        and _SAFE_CORRELATION_ID.fullmatch(runtime_run_id)
    ):
        payload["runtime_run_id"] = runtime_run_id
    if (
        error_type
        and _SAFE_CORRELATION_ID.fullmatch(error_type)
    ):
        payload["error_type"] = error_type
    return payload


def log_im_timing(
    row: ImInboundMessage,
    *,
    stage: str,
    status: str = "success",
    runtime_run_id: str | None = None,
    error_type: str | None = None,
) -> None:
    try:
        _timing_logger.info(
            "agent ttft timing=%s",
            im_timing_payload(
                row,
                stage=stage,
                status=status,
                runtime_run_id=runtime_run_id,
                error_type=error_type,
            ),
        )
    except Exception:
        # Observability must never replace queue state or provider output.
        pass


def _provisional_ordering_key(
    *,
    channel_id: int,
    chat_type: str,
    external_conversation_id: str,
    member_digest: str,
) -> str:
    if chat_type == "group":
        conversation_digest = hashlib.sha256(
            external_conversation_id.encode("utf-8")
        ).hexdigest()[:24]
        return (
            f"group:{channel_id}:{conversation_digest}:"
            f"member:{member_digest[:24]}"
        )
    return f"direct:{channel_id}:member:{member_digest[:24]}"


def _resolved_ordering_key(
    *,
    channel_id: int,
    chat_type: str,
    external_conversation_id: str,
    platform_user_id: int,
) -> str:
    if chat_type == "group":
        conversation_digest = hashlib.sha256(
            external_conversation_id.encode("utf-8")
        ).hexdigest()[:24]
        return (
            f"group:{channel_id}:{conversation_digest}:"
            f"{platform_user_id}"
        )
    return f"direct:{channel_id}:{platform_user_id}"


def _wecom_runtime_run_id(
    *,
    target_type: str,
    channel_id: int,
    inbound_message_id: int,
) -> str:
    if target_type == "application_agent":
        idempotency_key = (
            f"wecom:{channel_id}:{inbound_message_id}"
        )
        digest = hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()
        return f"app_run_wecom_{digest[:40]}"
    return f"rr-im-wecom-{inbound_message_id}"


def _wecom_provider_stream_id(
    *,
    target_type: str,
    channel_id: int,
    inbound_message_id: int,
    external_message_id: str = "",
    external_conversation_id: str = "",
) -> str:
    if external_message_id:
        digest = hashlib.sha256(
            (
                f"{channel_id}\0{external_message_id}\0"
                f"{external_conversation_id}"
            ).encode("utf-8")
        ).hexdigest()
        return f"im-{digest[:48]}"
    runtime_run_id = _wecom_runtime_run_id(
        target_type=target_type,
        channel_id=channel_id,
        inbound_message_id=inbound_message_id,
    )
    digest = hashlib.sha256(
        (
            f"{channel_id}:{inbound_message_id}:"
            f"{runtime_run_id}"
        ).encode("utf-8")
    ).hexdigest()
    return f"im-{digest[:48]}"


def _wecom_ingress_ids(
    *,
    channel_id: int,
    external_message_id: str,
) -> tuple[str, str]:
    digest = hashlib.sha256(
        f"wecom\0{channel_id}\0{external_message_id}".encode("utf-8")
    ).hexdigest()
    return (f"im_delivery_{digest[:48]}", f"im_message_{digest[:48]}")


def _wecom_ingress_payload_digest(
    *,
    channel_id: int,
    message: NormalizedWeComMessage,
    sender_member_id_digest: str | None,
) -> str:
    """Hash immutable, non-secret provider facts for idempotency comparison."""

    payload = {
        "channel_id": channel_id,
        "provider": "wecom",
        "external_message_id": message.external_message_id,
        "external_conversation_id": message.external_conversation_id,
        "chat_type": message.chat_type,
        "sender_member_id_digest": sender_member_id_digest,
        "message_type": message.message_type,
        "normalized_content": message.normalized_content,
        "quote_payload": message.quote_payload,
        "media": [
            {
                "ordinal": item.ordinal,
                "media_type": item.media_type,
                "quoted": item.quoted,
                "display_name": item.display_name,
            }
            for item in sorted(message.media, key=lambda item: item.ordinal)
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class InboundReceipt:
    inbound_message_id: int
    duplicate: bool
    status: str
    stream_id: str
    has_unfinished_predecessor: bool
    delivery_id: str | None = None
    client_message_id: str | None = None
    ingress_ticket: int | None = None
    dispatch_lane: str | None = None
    ingress_ticket_state: str | None = None


@dataclass(frozen=True)
class QueuedBatchMessage:
    inbound_message_id: int
    batch_ordinal: int
    normalized_content: dict[str, Any]
    quote_payload: dict[str, Any] | None
    provider_request_id: str = ""
    stream_id: str = ""


@dataclass(frozen=True)
class QueuedInbound:
    inbound_message_id: int
    channel_id: int
    org_id: int
    user_id: int | None
    runtime_workspace_id: int | None
    runtime_session_row_id: int | None
    external_conversation_id: str
    provider_request_id: str
    message_type: str
    normalized_content: dict[str, Any]
    quote_payload: dict[str, Any] | None
    attempt_count: int
    stream_id: str
    generation: int
    owner_instance_id: str
    lease_epoch: int
    execution_batch_id: int | None = None
    batch_messages: tuple[QueuedBatchMessage, ...] = ()
    runtime_run_row_id: int | None = None
    target_type: str = "personal_agent"
    application_agent_id: int | None = None
    application_client_id: int | None = None
    sender_platform_user_id: int | None = None
    chat_type: str = "direct"
    ordering_key: str | None = None
    application_agent_run_id: int | None = None
    external_message_id: str = ""


@dataclass(frozen=True)
class QueuedFastLaneInbound:
    inbound_message_id: int
    channel_id: int
    delivery_id: str
    client_message_id: str
    ingress_ordering_key: str
    ingress_ticket: int
    provider_request_id: str
    provider_stream_id: str
    external_message_id: str
    external_conversation_id: str
    message_type: str
    normalized_content: dict[str, Any]
    quote_payload: dict[str, Any] | None
    target_type: str
    sender_platform_user_id: int | None
    owner_instance_id: str
    lease_epoch: int
    runtime_disposition_seq: int | None = None
    runtime_canonical_run_id: str | None = None
    runtime_start_after_seq: int | None = None


@dataclass(frozen=True)
class DeliveryProgressResult:
    persisted: bool
    first_provider_confirmation_recorded: bool = False
    inbound_message: ImInboundMessage | None = None


class ImInboundQueue:
    def __init__(
        self,
        db: AsyncSession,
        *,
        secret_codec: ManagedSecretCodec | None = None,
    ) -> None:
        self.db = db
        self.secret_codec = secret_codec or ManagedSecretCodec()

    @staticmethod
    def _personal_ordering_key(channel: ImChannel) -> str:
        if channel.runtime_session_row_id is None:
            raise ValueError("personal IM channel has no Runtime Session")
        return f"personal-session:{int(channel.runtime_session_row_id)}"

    async def _allocate_ingress_ticket(
        self,
        *,
        channel_id: int,
        ingress_ordering_key: str,
    ) -> int:
        counter = await self.db.scalar(
            select(ImIngressTicketCounter)
            .where(
                ImIngressTicketCounter.channel_id == channel_id,
                ImIngressTicketCounter.ingress_ordering_key
                == ingress_ordering_key,
            )
            .with_for_update()
        )
        if counter is None:
            # The caller already holds the channel row lock. That lock covers
            # first-counter creation as well as the normal increment path, so
            # a later ingress transaction cannot commit a higher ticket first.
            counter = ImIngressTicketCounter(
                channel_id=channel_id,
                ingress_ordering_key=ingress_ordering_key,
                last_ticket=1,
            )
            self.db.add(counter)
            await self.db.flush()
            return 1
        counter.last_ticket = int(counter.last_ticket) + 1
        await self.db.flush()
        return int(counter.last_ticket)

    async def _attach_message_to_batch(
        self,
        *,
        channel: ImChannel,
        row: ImInboundMessage,
        attachments_ready: bool,
    ) -> ImExecutionBatch:
        """Attach one mutable inbound message to the lane's collection slot.

        The caller holds the channel row lock, which serializes batch creation
        and ordinals across Backend replicas without relying on process memory.
        """

        ordering_key = str(
            row.ordering_key
            or self._personal_ordering_key(channel)
        )
        row.ordering_key = ordering_key
        slot_key = f"{int(channel.id)}:{ordering_key}"
        batch = await self.db.scalar(
            select(ImExecutionBatch)
            .where(ImExecutionBatch.collection_slot_key == slot_key)
            .with_for_update()
        )
        if batch is not None and batch.state not in {
            "collecting",
            "interrupting",
            "ready",
        }:
            # A sealed or terminal batch is immutable. Repair a stale
            # collection slot under the channel lock before creating the
            # lane's next mutable batch.
            batch.collection_slot_key = None
            await self.db.flush()
            batch = None
        if batch is None:
            predecessor = await self.db.scalar(
                select(ImExecutionBatch)
                .where(ImExecutionBatch.execution_slot_key == slot_key)
                .with_for_update()
            )
            state = (
                "interrupting"
                if predecessor is not None
                else "ready"
                if attachments_ready
                else "collecting"
            )
            now = utc_now()
            batch = ImExecutionBatch(
                channel_id=int(channel.id),
                target_type=str(channel.target_type),
                ordering_key=ordering_key,
                collection_slot_key=slot_key,
                state=state,
                predecessor_batch_id=(
                    int(predecessor.id)
                    if predecessor is not None
                    else None
                ),
                predecessor_run_kind=(
                    predecessor.execution_run_kind
                    if predecessor is not None
                    else None
                ),
                predecessor_run_row_id=(
                    predecessor.execution_run_row_id
                    if predecessor is not None
                    else None
                ),
                predecessor_runtime_run_id=(
                    predecessor.execution_runtime_run_id
                    if predecessor is not None
                    else None
                ),
                collected_at=now,
                interrupt_requested_at=(
                    now if predecessor is not None else None
                ),
            )
            self.db.add(batch)
            await self.db.flush()

        row.execution_batch_id = int(batch.id)
        row.batch_ordinal = None
        await self.db.flush()
        members = list(
            (
                await self.db.execute(
                    select(ImInboundMessage)
                    .where(
                        ImInboundMessage.execution_batch_id == int(batch.id)
                    )
                    .order_by(
                        ImInboundMessage.received_at,
                        ImInboundMessage.id,
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        # Identity lookup can complete out of order. Clear the nullable unique
        # ordinals before reassigning them by the durable receive boundary.
        for member in members:
            member.batch_ordinal = None
        await self.db.flush()
        for ordinal, member in enumerate(members, start=1):
            member.batch_ordinal = ordinal
        batch.response_inbound_message_id = int(members[-1].id)
        if batch.state == "collecting" and attachments_ready:
            staging_count = await self.db.scalar(
                select(func.count(ImInboundMessage.id)).where(
                    ImInboundMessage.execution_batch_id == int(batch.id),
                    ImInboundMessage.status == "staging_media",
                )
            )
            if int(staging_count or 0) == 0:
                batch.state = (
                    "interrupting"
                    if batch.predecessor_batch_id is not None
                    else "ready"
                )
        return batch

    async def ingest(
        self,
        *,
        channel_id: int,
        message: NormalizedWeComMessage,
    ) -> InboundReceipt:
        # Serialize ingress for one Channel before deciding what immediate
        # status the user should see. This keeps concurrent Backend frames
        # from both claiming that they can start immediately.
        channel = await self.db.scalar(
            select(ImChannel)
            .where(ImChannel.id == channel_id)
            .with_for_update()
        )
        if channel is None:
            raise ValueError("IM channel not found")
        member_digest = (
            wecom_member_digest(message.sender_member_id)
            if channel.target_type == "application_agent"
            and message.sender_member_id
            else None
        )
        provisional_ordering_key = (
            _provisional_ordering_key(
                channel_id=channel_id,
                chat_type=message.chat_type,
                external_conversation_id=message.external_conversation_id,
                member_digest=member_digest,
            )
            if member_digest
            else None
        )
        ingress_ordering_key = (
            provisional_ordering_key
            if channel.target_type == "application_agent"
            else self._personal_ordering_key(channel)
        )
        if not ingress_ordering_key:
            raise ValueError("IM inbound has no stable ingress ordering key")
        delivery_id, client_message_id = _wecom_ingress_ids(
            channel_id=channel_id,
            external_message_id=message.external_message_id,
        )
        ingress_payload_digest = _wecom_ingress_payload_digest(
            channel_id=channel_id,
            message=message,
            sender_member_id_digest=member_digest,
        )
        existing = (
            await self.db.execute(
                select(ImInboundMessage, ImDeliveryCheckpoint)
                .join(
                    ImDeliveryCheckpoint,
                    ImDeliveryCheckpoint.inbound_message_id
                    == ImInboundMessage.id,
                )
                .where(
                    ImInboundMessage.provider == "wecom",
                    ImInboundMessage.channel_id == channel_id,
                    ImInboundMessage.external_message_id
                    == message.external_message_id,
                )
            )
        ).first()
        if existing is not None:
            row, checkpoint = existing
            if (
                row.ingress_payload_digest is not None
                and row.ingress_payload_digest != ingress_payload_digest
            ):
                raise ValueError("IM inbound idempotency payload conflict")
            return InboundReceipt(
                inbound_message_id=int(row.id),
                duplicate=True,
                status=str(row.status),
                stream_id=str(checkpoint.provider_stream_id),
                has_unfinished_predecessor=False,
                delivery_id=row.delivery_id,
                client_message_id=row.client_message_id,
                ingress_ticket=row.ingress_ticket,
                dispatch_lane=row.dispatch_lane,
                ingress_ticket_state=row.ingress_ticket_state,
            )
        ingress_ticket = await self._allocate_ingress_ticket(
            channel_id=channel_id,
            ingress_ordering_key=ingress_ordering_key,
        )
        predecessor_statement = select(ImInboundMessage.id).where(
                ImInboundMessage.provider == "wecom",
                ImInboundMessage.channel_id == channel_id,
                ImInboundMessage.status.in_(
                    _UNFINISHED_INBOUND_STATUSES
                ),
        )
        if channel.target_type == "application_agent":
            predecessor_statement = predecessor_statement.where(
                ImInboundMessage.ordering_key
                == provisional_ordering_key
            )
        predecessor_id = await self.db.scalar(
            predecessor_statement.limit(1)
        )
        has_unfinished_predecessor = predecessor_id is not None
        now = utc_now()
        application_capacity_full = False
        if (
            channel.target_type == "application_agent"
            and channel.application_agent_id is not None
        ):
            waiting = int(
                await self.db.scalar(
                    select(func.count(ImInboundMessage.id)).where(
                        ImInboundMessage.application_agent_id
                        == channel.application_agent_id,
                        ImInboundMessage.target_type
                        == "application_agent",
                        ImInboundMessage.status.in_(
                            _UNFINISHED_INBOUND_STATUSES
                        ),
                    )
                )
                or 0
            )
            application_capacity_full = waiting >= (
                settings.APPLICATION_AGENT_MAX_INFLIGHT_RUNS * 4
            )
        row = ImInboundMessage(
            channel_id=channel_id,
            provider="wecom",
            target_type=str(channel.target_type),
            application_agent_id=channel.application_agent_id,
            external_message_id=message.external_message_id,
            external_conversation_id=message.external_conversation_id,
            chat_type=message.chat_type,
            sender_member_id_digest=member_digest,
            sender_member_id_encrypted=(
                self.secret_codec.encrypt(message.sender_member_id)
                if member_digest
                else None
            ),
            ordering_key=provisional_ordering_key,
            ingress_ordering_key=ingress_ordering_key,
            ingress_ticket=ingress_ticket,
            delivery_id=delivery_id,
            client_message_id=client_message_id,
            dispatch_lane=(
                "ordinary_attachment" if message.media else "fast_lane_v2"
            ),
            ingress_ticket_state=(
                "dead_letter" if application_capacity_full else "pending"
            ),
            ingress_payload_digest=ingress_payload_digest,
            terminal_outcome_digest=(
                self._terminal_outcome_digest(
                    state="dead_letter",
                    error_code="APPLICATION_CAPACITY_QUEUE_FULL",
                    error_message="application capacity queue is full",
                )
                if application_capacity_full
                else None
            ),
            identity_status=(
                "pending"
                if channel.target_type == "application_agent"
                else "not_required"
            ),
            provider_request_id=message.provider_request_id,
            message_type=message.message_type,
            normalized_content=message.normalized_content,
            quote_payload=message.quote_payload,
            status=(
                "dead_letter"
                if application_capacity_full
                else "awaiting_identity"
                if channel.target_type == "application_agent"
                else (
                    "staging_media" if message.media else "queued"
                )
            ),
            attempt_count=0,
            capacity_wait_attempt_count=0,
            received_at=now,
            db_committed_at=None,
            finished_at=now if application_capacity_full else None,
            error_code=(
                "APPLICATION_CAPACITY_QUEUE_FULL"
                if application_capacity_full
                else None
            ),
            error_message=(
                "application capacity queue is full"
                if application_capacity_full
                else None
            ),
        )
        self.db.add(row)
        await self.db.flush()
        stream_id = _wecom_provider_stream_id(
            target_type=str(channel.target_type),
            channel_id=channel_id,
            inbound_message_id=int(row.id),
            external_message_id=message.external_message_id,
            external_conversation_id=message.external_conversation_id,
        )
        self.db.add(
            ImDeliveryCheckpoint(
                inbound_message_id=int(row.id),
                provider_request_id=message.provider_request_id,
                provider_stream_id=stream_id,
                delivery_mode="passive_stream",
                status="pending",
            )
        )
        if not application_capacity_full:
            for item in message.media:
                metadata = self.secret_codec.encrypt(
                    json.dumps(
                        {
                            "url": item.url,
                            "aes_key": item.aes_key,
                            "quoted": item.quoted,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                self.db.add(
                    ImMessageAttachment(
                        inbound_message_id=int(row.id),
                        ordinal=item.ordinal,
                        media_type=item.media_type,
                        display_name=item.display_name,
                        provider_media_metadata_encrypted=metadata,
                        staging_status="pending",
                    )
                )
            if (
                channel.target_type == "personal_agent"
                and message.media
            ):
                await self._attach_message_to_batch(
                    channel=channel,
                    row=row,
                    attachments_ready=not bool(message.media),
                )
        if (
            channel.target_type == "personal_agent"
            and message.chat_type == "direct"
            and message.sender_member_id
        ):
            proactive_target = message.sender_member_id.strip()
            proactive_digest = wecom_member_digest(proactive_target)
            if channel.proactive_target_digest is None:
                channel.proactive_target_encrypted = (
                    self.secret_codec.encrypt(proactive_target)
                )
                channel.proactive_target_digest = proactive_digest
                channel.proactive_target_confirmed_at = now
            elif channel.proactive_target_digest == proactive_digest:
                channel.proactive_target_confirmed_at = now
            else:
                # A personal bot has one fixed recipient.  Never silently
                # retarget scheduled results when another WeCom member sends
                # a frame to a mis-shared bot.
                _logger.warning(
                    "personal WeCom proactive target mismatch",
                    extra={"channel_id": int(channel.id)},
                )
        channel.last_message_at = now
        row.db_committed_at = utc_now()
        await self.db.commit()
        return InboundReceipt(
            inbound_message_id=int(row.id),
            duplicate=False,
            status=str(row.status),
            stream_id=stream_id,
            has_unfinished_predecessor=(
                has_unfinished_predecessor
            ),
            delivery_id=delivery_id,
            client_message_id=client_message_id,
            ingress_ticket=ingress_ticket,
            dispatch_lane=(
                "ordinary_attachment" if message.media else "fast_lane_v2"
            ),
            ingress_ticket_state=(
                "dead_letter" if application_capacity_full else "pending"
            ),
        )

    async def release_predecessor(
        self,
        *,
        execution_batch_id: int,
        predecessor_terminal_status: str,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        """Atomically terminalize a predecessor and release its successor."""

        batch_identity = (
            await self.db.execute(
                select(
                    ImExecutionBatch.channel_id,
                    ImExecutionBatch.predecessor_batch_id,
                ).where(
                    ImExecutionBatch.id == execution_batch_id
                )
            )
        ).one_or_none()
        if batch_identity is None or batch_identity[1] is None:
            await self.db.rollback()
            return False
        channel_id = int(batch_identity[0])
        predecessor_batch_id = int(batch_identity[1])
        now = utc_now()
        lease = await self.db.scalar(
            select(ImChannelLease)
            .where(
                ImChannelLease.channel_id == channel_id,
                ImChannelLease.owner_instance_id == owner_instance_id,
                ImChannelLease.lease_epoch == lease_epoch,
                ImChannelLease.expires_at > now,
            )
            .with_for_update()
        )
        if lease is None:
            await self.db.rollback()
            return False
        predecessor = await self.db.scalar(
            select(ImExecutionBatch)
            .where(
                ImExecutionBatch.id == predecessor_batch_id,
                ImExecutionBatch.state.in_(("submitting", "running")),
            )
            .with_for_update()
        )
        if predecessor is None:
            await self.db.rollback()
            return False
        successor = await self.db.scalar(
            select(ImExecutionBatch)
            .where(
                ImExecutionBatch.id == execution_batch_id,
                ImExecutionBatch.predecessor_batch_id
                == predecessor_batch_id,
                ImExecutionBatch.state == "interrupting",
            )
            .with_for_update()
        )
        if successor is None:
            await self.db.rollback()
            return False

        predecessor.state = "terminal"
        predecessor.execution_slot_key = None
        predecessor.terminal_at = now
        predecessor.terminal_status = predecessor_terminal_status[:32]
        if predecessor_terminal_status == "canceled":
            predecessor.terminal_reason = "PREEMPTED_BY_NEW_IM_MESSAGE"
        successor.state = "ready"
        successor.predecessor_released_at = now
        successor.error_code = None
        successor.error_message = None
        await self.db.commit()
        return True

    async def mark_capacity_wait(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
        timeout_seconds: int = 60,
    ) -> str:
        """Requeue Supervisor capacity rejection without spending failure attempts."""

        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return "stale"
        now = utc_now()
        started_at = row.capacity_wait_started_at or now
        attempts = int(row.capacity_wait_attempt_count or 0) + 1
        timed_out = (
            now - aware_utc(started_at)
        ).total_seconds() >= timeout_seconds
        row.attempt_count = max(0, int(row.attempt_count or 0) - 1)
        row.capacity_wait_started_at = started_at
        row.capacity_wait_attempt_count = attempts
        row.status = "dead_letter" if timed_out else "queued"
        row.next_attempt_at = (
            None
            if timed_out
            else now
            + timedelta(
                seconds=(
                    (1, 2, 4, 8, 15)[min(attempts - 1, 4)]
                    + (int(row.id) % 251) / 1000
                )
            )
        )
        row.finished_at = now if timed_out else None
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.started_at = None
        row.error_code = (
            "CAPACITY_TIMEOUT"
            if timed_out
            else "APPLICATION_CAPACITY_BUSY"
        )
        row.error_message = (
            "application capacity wait exceeded 60 seconds"
            if timed_out
            else "application capacity is temporarily busy"
        )
        await self.db.commit()
        return "capacity_timeout" if timed_out else "capacity_wait"

    async def mark_session_busy_wait(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        """Keep a retryable Supervisor session conflict out of dead letter."""

        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return False
        row.status = "queued"
        row.attempt_count = max(0, int(row.attempt_count or 0) - 1)
        row.next_attempt_at = utc_now() + timedelta(seconds=1)
        row.started_at = None
        row.finished_at = None
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.error_code = "SESSION_BUSY"
        row.error_message = "waiting for the authoritative Runtime Session"
        await self.db.commit()
        return True

    async def mark_identity_resolved(
        self,
        *,
        inbound_message_id: int,
        platform_user_id: int,
    ) -> str:
        channel_id = await self.db.scalar(
            select(ImInboundMessage.channel_id).where(
                ImInboundMessage.id == inbound_message_id
            )
        )
        if channel_id is None:
            await self.db.rollback()
            return "stale"
        channel = await self.db.scalar(
            select(ImChannel)
            .where(ImChannel.id == int(channel_id))
            .with_for_update()
        )
        row = (
            await self.db.execute(
                select(ImInboundMessage)
                .where(
                    ImInboundMessage.id == inbound_message_id,
                    ImInboundMessage.target_type
                    == "application_agent",
                    ImInboundMessage.status == "awaiting_identity",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            await self.db.rollback()
            return "stale"
        row.sender_platform_user_id = platform_user_id
        row.identity_status = "resolved"
        row.identity_resolved_at = utc_now()
        row.ordering_key = _resolved_ordering_key(
            channel_id=int(row.channel_id),
            chat_type=str(row.chat_type),
            external_conversation_id=str(
                row.external_conversation_id or ""
            ),
            platform_user_id=platform_user_id,
        )
        # Application attachments are staged into the delegated
        # conversation's Runtime Session immediately before invocation.
        # The session does not exist at ingress time.
        row.status = "queued"
        if channel is None:
            await self.db.rollback()
            return "stale"
        if row.dispatch_lane != "fast_lane_v2":
            await self._attach_message_to_batch(
                channel=channel,
                row=row,
                attachments_ready=True,
            )
        await self.db.commit()
        log_im_timing(row, stage="identity_resolved")
        return str(row.status)

    async def mark_identity_required(
        self,
        *,
        inbound_message_id: int,
    ) -> bool:
        row = (
            await self.db.execute(
                select(ImInboundMessage)
                .where(
                    ImInboundMessage.id == inbound_message_id,
                    ImInboundMessage.target_type
                    == "application_agent",
                    ImInboundMessage.status == "awaiting_identity",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            await self.db.rollback()
            return False
        row.status = "identity_required"
        row.identity_status = "binding_required"
        row.ingress_ticket_state = "validation_rejected"
        row.terminal_outcome_digest = self._terminal_outcome_digest(
            state="validation_rejected",
            error_code="IDENTITY_REQUIRED",
            error_message="enterprise identity binding is required",
        )
        row.finished_at = utc_now()
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is not None:
            checkpoint.status = "completed"
        await self.db.commit()
        return True

    async def pending_attachments(
        self,
        inbound_message_id: int,
        *,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> list[tuple[ImMessageAttachment, dict[str, Any]]]:
        now = utc_now()
        rows = (
            await self.db.execute(
                select(ImMessageAttachment)
                .join(
                    ImInboundMessage,
                    ImInboundMessage.id
                    == ImMessageAttachment.inbound_message_id,
                )
                .join(
                    ImChannelLease,
                    ImChannelLease.channel_id
                    == ImInboundMessage.channel_id,
                )
                .where(
                    ImMessageAttachment.inbound_message_id
                    == inbound_message_id,
                    ImMessageAttachment.staging_status == "pending",
                    ImInboundMessage.status == "staging_media",
                    ImInboundMessage.processing_owner
                    == owner_instance_id,
                    ImInboundMessage.processing_lease_epoch
                    == lease_epoch,
                    ImChannelLease.owner_instance_id
                    == owner_instance_id,
                    ImChannelLease.lease_epoch == lease_epoch,
                    ImChannelLease.expires_at > now,
                )
                .order_by(ImMessageAttachment.ordinal)
            )
        ).scalars()
        result: list[
            tuple[ImMessageAttachment, dict[str, Any]]
        ] = []
        for row in rows:
            encrypted = row.provider_media_metadata_encrypted
            if not encrypted:
                continue
            result.append(
                (
                    row,
                    json.loads(self.secret_codec.decrypt(encrypted)),
                )
            )
        return result

    async def mark_attachment_ready(
        self,
        *,
        attachment_id: int,
        display_name: str,
        mime_type: str | None,
        size_bytes: int,
        sha256: str,
        chat_file_artifact_id: int,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        now = utc_now()
        result = (
            await self.db.execute(
                select(
                    ImMessageAttachment,
                    ImInboundMessage,
                    ImChannelLease,
                )
                .join(
                    ImInboundMessage,
                    ImInboundMessage.id
                    == ImMessageAttachment.inbound_message_id,
                )
                .join(
                    ImChannelLease,
                    ImChannelLease.channel_id
                    == ImInboundMessage.channel_id,
                )
                .where(
                    ImMessageAttachment.id == attachment_id,
                    ImMessageAttachment.inbound_message_id
                    == inbound_message_id,
                    ImMessageAttachment.staging_status == "pending",
                    ImInboundMessage.status == "staging_media",
                    ImInboundMessage.processing_owner
                    == owner_instance_id,
                    ImInboundMessage.processing_lease_epoch
                    == lease_epoch,
                    ImChannelLease.owner_instance_id
                    == owner_instance_id,
                    ImChannelLease.lease_epoch == lease_epoch,
                    ImChannelLease.expires_at > now,
                )
                .with_for_update()
            )
        ).first()
        if result is None:
            await self.db.rollback()
            return False
        row, _message, _lease = result
        row.display_name = display_name
        row.mime_type = mime_type
        row.size_bytes = size_bytes
        row.sha256 = sha256
        row.chat_file_artifact_id = chat_file_artifact_id
        row.staging_status = "ready"
        row.provider_media_metadata_encrypted = None
        await self.db.commit()
        return True

    async def finish_staging(
        self,
        inbound_message_id: int,
        *,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="staging_media",
        )
        if row is None:
            return False
        row.status = "queued"
        # Attachment retries are independent from Runtime/Provider delivery
        # retries. Once every attachment is ready, start that budget at zero.
        row.attempt_count = 0
        row.next_attempt_at = None
        row.finished_at = None
        row.error_code = None
        row.error_message = None
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.started_at = None
        if row.execution_batch_id is not None:
            batch = await self.db.scalar(
                select(ImExecutionBatch)
                .where(ImExecutionBatch.id == row.execution_batch_id)
                .with_for_update()
            )
            remaining = await self.db.scalar(
                select(func.count(ImInboundMessage.id)).where(
                    ImInboundMessage.execution_batch_id
                    == row.execution_batch_id,
                    ImInboundMessage.id != row.id,
                    ImInboundMessage.status == "staging_media",
                )
            )
            if (
                batch is not None
                and batch.state == "collecting"
                and int(remaining or 0) == 0
            ):
                batch.state = (
                    "interrupting"
                    if batch.predecessor_batch_id is not None
                    else "ready"
                )
        await self.db.commit()
        return True

    async def mark_staging_failed(
        self,
        inbound_message_id: int,
        *,
        owner_instance_id: str,
        lease_epoch: int,
        error_code: str,
        error_message: str,
        max_attempts: int,
    ) -> str:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="staging_media",
        )
        if row is None:
            return "stale"
        terminal = int(row.attempt_count) >= max_attempts
        row.status = "dead_letter" if terminal else "staging_media"
        row.next_attempt_at = (
            None
            if terminal
            else utc_now()
            + timedelta(seconds=min(60, 2 ** int(row.attempt_count)))
        )
        row.finished_at = utc_now() if terminal else None
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.started_at = None
        row.error_code = error_code[:100]
        row.error_message = error_message[:2000]
        if terminal:
            row.ingress_ticket_state = "staging_failed"
            row.terminal_outcome_digest = self._terminal_outcome_digest(
                state="staging_failed",
                error_code=error_code,
                error_message=error_message,
            )
            attachments = (
                await self.db.execute(
                    select(ImMessageAttachment).where(
                        ImMessageAttachment.inbound_message_id
                        == inbound_message_id,
                        ImMessageAttachment.staging_status == "pending",
                    )
                )
            ).scalars()
            for attachment in attachments:
                attachment.staging_status = "failed"
                attachment.error_code = error_code[:100]
                attachment.error_message = error_message[:2000]
                # Provider URLs and AES keys are temporary secrets. Retain
                # them only while an automatic retry is still possible.
                attachment.provider_media_metadata_encrypted = None
        await self.db.commit()
        return str(row.status)

    @staticmethod
    def _terminal_outcome_digest(
        *,
        state: str,
        error_code: str,
        error_message: str,
    ) -> str:
        canonical = json.dumps(
            {
                "state": state,
                "error_code": error_code,
                "error_message": error_message,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    async def _ingress_predecessor_blocks(
        self,
        row: ImInboundMessage,
    ) -> bool:
        if (
            row.ingress_ordering_key is None
            or row.ingress_ticket is None
        ):
            return False
        predecessor = aliased(ImInboundMessage)
        blocked = await self.db.scalar(
            select(
                exists().where(
                    predecessor.channel_id == row.channel_id,
                    predecessor.ingress_ordering_key
                    == row.ingress_ordering_key,
                    predecessor.ingress_ticket.is_not(None),
                    predecessor.ingress_ticket < row.ingress_ticket,
                    predecessor.status.notin_(
                        ("completed", "failed", "dead_letter", "canceled")
                    ),
                    or_(
                        predecessor.ingress_ticket_state.is_(None),
                        predecessor.ingress_ticket_state.not_in(
                            _INGRESS_SETTLED_STATES
                        ),
                    ),
                )
            )
        )
        return bool(blocked)

    async def claim_next_fast_lane(
        self,
        *,
        channel_id: int,
        owner_instance_id: str,
        lease_epoch: int,
        active_delivery_ids: set[str] | frozenset[str] | None = None,
    ) -> QueuedFastLaneInbound | None:
        """Claim one durable v2 delivery without entering the ordinary poller."""

        now = utc_now()
        lease = await self.db.scalar(
            select(ImChannelLease)
            .where(
                ImChannelLease.channel_id == channel_id,
                ImChannelLease.owner_instance_id == owner_instance_id,
                ImChannelLease.lease_epoch == lease_epoch,
                ImChannelLease.expires_at > now,
            )
            .with_for_update()
        )
        if lease is None:
            await self.db.rollback()
            return None
        candidates = list(
            (
                await self.db.execute(
                    select(ImInboundMessage)
                    .where(
                        ImInboundMessage.channel_id == channel_id,
                        ImInboundMessage.dispatch_lane == "fast_lane_v2",
                        ImInboundMessage.status.in_(("queued", "running")),
                        ImInboundMessage.ingress_ticket_state.in_(
                            ("pending", "retry_wait", "runtime_durable")
                        ),
                        or_(
                            ImInboundMessage.next_attempt_at.is_(None),
                            ImInboundMessage.next_attempt_at <= now,
                        ),
                    )
                    .order_by(
                        ImInboundMessage.received_at,
                        ImInboundMessage.id,
                    )
                    .limit(32)
                    .with_for_update()
                )
            ).scalars()
        )
        row: ImInboundMessage | None = None
        checkpoint: ImDeliveryCheckpoint | None = None
        settled_terminal = False
        for candidate in candidates:
            if (
                candidate.status == "running"
                and candidate.processing_owner == owner_instance_id
                and int(candidate.processing_lease_epoch or -1)
                == lease_epoch
                and (
                    active_delivery_ids is None
                    or str(candidate.delivery_id or "")
                    in active_delivery_ids
                )
            ):
                continue
            candidate_checkpoint: ImDeliveryCheckpoint | None = None
            if candidate.ingress_ticket_state == "runtime_durable":
                candidate_checkpoint = await self.db.scalar(
                    select(ImDeliveryCheckpoint)
                    .where(
                        ImDeliveryCheckpoint.inbound_message_id
                        == candidate.id
                    )
                    .with_for_update()
                )
                if self._settle_terminal_fast_lane_checkpoint(
                    row=candidate,
                    checkpoint=candidate_checkpoint,
                    now=now,
                ):
                    settled_terminal = True
                    continue
            if not await self._ingress_predecessor_blocks(candidate):
                row = candidate
                checkpoint = candidate_checkpoint
                break
        if row is None:
            if settled_terminal:
                await self.db.commit()
            else:
                await self.db.rollback()
            return None
        if not all(
            (
                row.delivery_id,
                row.client_message_id,
                row.ingress_ordering_key,
                row.ingress_ticket is not None,
            )
        ):
            await self.db.rollback()
            return None
        row.status = "running"
        if row.ingress_ticket_state != "runtime_durable":
            row.ingress_ticket_state = "retry_wait"
        row.processing_owner = owner_instance_id
        row.processing_lease_epoch = lease_epoch
        row.scheduler_claimed_at = now
        row.started_at = row.started_at or now
        row.next_attempt_at = None
        row.attempt_count = int(row.attempt_count) + 1
        if checkpoint is None:
            checkpoint = await self.db.scalar(
                select(ImDeliveryCheckpoint).where(
                    ImDeliveryCheckpoint.inbound_message_id == row.id
                )
            )
        if checkpoint is None:
            await self.db.rollback()
            return None
        provider_stream_id = str(checkpoint.provider_stream_id)
        await self.db.commit()
        return QueuedFastLaneInbound(
            inbound_message_id=int(row.id),
            channel_id=int(row.channel_id),
            delivery_id=str(row.delivery_id),
            client_message_id=str(row.client_message_id),
            ingress_ordering_key=str(row.ingress_ordering_key),
            ingress_ticket=int(row.ingress_ticket),
            provider_request_id=str(row.provider_request_id or ""),
            provider_stream_id=provider_stream_id,
            external_message_id=str(row.external_message_id),
            external_conversation_id=str(row.external_conversation_id or ""),
            message_type=str(row.message_type),
            normalized_content=dict(row.normalized_content or {}),
            quote_payload=row.quote_payload,
            target_type=str(row.target_type),
            sender_platform_user_id=(
                int(row.sender_platform_user_id)
                if row.sender_platform_user_id is not None
                else None
            ),
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            runtime_disposition_seq=(
                int(row.runtime_disposition_seq)
                if row.runtime_disposition_seq is not None
                else None
            ),
            runtime_canonical_run_id=(
                str(row.runtime_canonical_run_id)
                if row.runtime_canonical_run_id
                else None
            ),
            runtime_start_after_seq=(
                int(row.runtime_start_after_seq)
                if row.runtime_start_after_seq is not None
                else None
            ),
        )

    @staticmethod
    def _settle_terminal_fast_lane_checkpoint(
        *,
        row: ImInboundMessage,
        checkpoint: ImDeliveryCheckpoint | None,
        now: datetime,
    ) -> bool:
        """Converge a queue wrapper after Provider terminal confirmation."""

        if (
            checkpoint is None
            or row.dispatch_lane != "fast_lane_v2"
            or row.status not in {"queued", "running"}
            or row.ingress_ticket_state != "runtime_durable"
            or str(checkpoint.status or "")
            not in _TERMINAL_FAST_LANE_CHECKPOINT_STATES
        ):
            return False
        row.status = "completed"
        row.next_attempt_at = None
        row.finished_at = row.finished_at or now
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.error_code = None
        row.error_message = None
        checkpoint.status = "delivered"
        checkpoint.error_code = None
        checkpoint.error_message = None
        return True

    async def mark_fast_lane_runtime_durable(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
        disposition_seq: int,
        disposition_state: str,
        canonical_run_id: str,
        parent_run_id: str | None,
        start_after_seq: int | None,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None or row.dispatch_lane != "fast_lane_v2":
            await self.db.rollback()
            return False
        if disposition_seq <= 0 or not canonical_run_id:
            await self.db.rollback()
            raise ValueError("Fast Lane disposition facts are invalid")
        current_seq = int(row.runtime_disposition_seq or 0)
        facts = (
            disposition_state,
            canonical_run_id,
            parent_run_id,
            start_after_seq,
        )
        current_facts = (
            row.runtime_disposition_state,
            row.runtime_canonical_run_id,
            row.runtime_parent_run_id,
            row.runtime_start_after_seq,
        )
        if current_seq == disposition_seq and current_seq > 0:
            if current_facts != facts:
                await self.db.rollback()
                raise ValueError(
                    "Fast Lane disposition sequence conflicts"
                )
        elif disposition_seq > current_seq:
            row.runtime_disposition_seq = disposition_seq
            row.runtime_disposition_state = disposition_state
            row.runtime_canonical_run_id = canonical_run_id
            row.runtime_parent_run_id = parent_run_id
            row.runtime_start_after_seq = start_after_seq
        row.ingress_ticket_state = "runtime_durable"
        row.runtime_submitted_at = row.runtime_submitted_at or utc_now()
        await self.db.commit()
        return True

    async def mark_fast_lane_validation_rejected(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
        error_code: str,
        error_message: str,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if (
            row is None
            or row.dispatch_lane != "fast_lane_v2"
            or row.ingress_ticket_state == "runtime_durable"
        ):
            await self.db.rollback()
            return False
        row.status = "failed"
        row.ingress_ticket_state = "validation_rejected"
        row.next_attempt_at = None
        row.finished_at = utc_now()
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.started_at = None
        row.error_code = error_code[:100]
        row.error_message = error_message[:2000]
        row.terminal_outcome_digest = self._terminal_outcome_digest(
            state="validation_rejected",
            error_code=str(row.error_code),
            error_message=str(row.error_message),
        )
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is not None:
            checkpoint.status = "completed"
            checkpoint.error_code = error_code[:100]
            checkpoint.error_message = error_message[:2000]
        await self.db.commit()
        return True

    async def is_fast_lane_delivery_owner(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if (
            row is None
            or row.dispatch_lane != "fast_lane_v2"
            or not row.runtime_canonical_run_id
            or row.runtime_start_after_seq is None
        ):
            await self.db.rollback()
            return False
        owner_id = await self.db.scalar(
            select(ImInboundMessage.id)
            .where(
                ImInboundMessage.channel_id == row.channel_id,
                ImInboundMessage.dispatch_lane == "fast_lane_v2",
                ImInboundMessage.ingress_ticket_state == "runtime_durable",
                ImInboundMessage.runtime_canonical_run_id
                == row.runtime_canonical_run_id,
                ImInboundMessage.runtime_start_after_seq.is_not(None),
            )
            .order_by(
                ImInboundMessage.runtime_start_after_seq,
                ImInboundMessage.delivery_id,
            )
            .limit(1)
        )
        current_id = int(row.id)
        await self.db.rollback()
        return owner_id is not None and int(owner_id) == current_id

    async def mark_fast_lane_retry_wait(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
        error_code: str,
        error_message: str,
    ) -> bool | str:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None or row.dispatch_lane != "fast_lane_v2":
            await self.db.rollback()
            return False
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if self._settle_terminal_fast_lane_checkpoint(
            row=row,
            checkpoint=checkpoint,
            now=utc_now(),
        ):
            await self.db.commit()
            return True
        if (
            error_code == "FAST_LANE_ROUTE_UNAVAILABLE"
            and row.target_type == "application_agent"
            and int(row.attempt_count or 0) >= _FAST_LANE_ROUTE_MAX_ATTEMPTS
        ):
            row.status = "failed"
            # A terminal Fast Lane failure must also settle the ingress ticket.
            # Leaving it in retry_wait makes _ingress_predecessor_blocks treat
            # this row as an unfinished predecessor forever, so every later
            # message in the same application conversation remains queued.
            row.ingress_ticket_state = "dead_letter"
            row.next_attempt_at = None
            row.finished_at = row.finished_at or utc_now()
            row.processing_owner = None
            row.processing_lease_epoch = None
            row.error_code = "APPLICATION_RUNTIME_UNAVAILABLE"
            row.error_message = (
                "应用 Agent Runtime 连续多次不可用，请重新发布后重试"
            )
            if checkpoint is not None:
                checkpoint.status = "delivery_failed"
                checkpoint.error_code = row.error_code
                checkpoint.error_message = row.error_message
            await self._terminalize_batch(
                row=row,
                terminal_status="failed",
                terminal_reason=row.error_code,
            )
            await self.db.commit()
            return "failed"
        row.status = "queued"
        if row.ingress_ticket_state != "runtime_durable":
            row.ingress_ticket_state = "retry_wait"
        row.next_attempt_at = utc_now() + timedelta(
            seconds=min(30, 2 ** min(int(row.attempt_count), 4))
        )
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.started_at = None
        row.error_code = error_code[:100]
        row.error_message = error_message[:2000]
        await self.db.commit()
        return True

    async def claim_staging(
        self,
        *,
        channel_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> int | None:
        """Fence and recover the oldest attachment staging operation."""

        now = utc_now()
        lease = (
            await self.db.execute(
                select(ImChannelLease)
                .where(
                    ImChannelLease.channel_id == channel_id,
                    ImChannelLease.owner_instance_id
                    == owner_instance_id,
                    ImChannelLease.lease_epoch == lease_epoch,
                    ImChannelLease.expires_at > now,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if lease is None:
            await self.db.rollback()
            return None
        row = (
            await self.db.execute(
                select(ImInboundMessage)
                .where(
                    ImInboundMessage.channel_id == channel_id,
                    ImInboundMessage.status.in_(
                        ("staging_media", "queued")
                    ),
                    or_(
                        ImInboundMessage.dispatch_lane.is_(None),
                        ImInboundMessage.dispatch_lane
                        == "ordinary_attachment",
                    ),
                )
                .order_by(
                    ImInboundMessage.received_at,
                    ImInboundMessage.id,
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            await self.db.rollback()
            return None
        if row.status != "staging_media":
            await self.db.rollback()
            return None
        if await self._ingress_predecessor_blocks(row):
            await self.db.rollback()
            return None
        if (
            row.next_attempt_at is not None
            and aware_utc(row.next_attempt_at) > now
        ):
            await self.db.rollback()
            return None
        if (
            row.processing_owner == owner_instance_id
            and int(row.processing_lease_epoch or -1) == lease_epoch
        ):
            await self.db.rollback()
            return None
        row.processing_owner = owner_instance_id
        row.processing_lease_epoch = lease_epoch
        row.started_at = now
        row.next_attempt_at = None
        row.attempt_count = int(row.attempt_count) + 1
        await self.db.commit()
        return int(row.id)

    async def claim_next(
        self,
        *,
        channel_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> QueuedInbound | None:
        now = utc_now()
        lease = (
            await self.db.execute(
                select(ImChannelLease)
                .where(
                    ImChannelLease.channel_id == channel_id,
                    ImChannelLease.owner_instance_id
                    == owner_instance_id,
                    ImChannelLease.lease_epoch == lease_epoch,
                    ImChannelLease.expires_at > now,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if lease is None:
            await self.db.rollback()
            return None
        active = (
            await self.db.execute(
                select(ImInboundMessage)
                .where(
                    ImInboundMessage.channel_id == channel_id,
                    ImInboundMessage.status == "running",
                    or_(
                        ImInboundMessage.dispatch_lane.is_(None),
                        ImInboundMessage.dispatch_lane
                        != "fast_lane_v2",
                    ),
                )
                .order_by(
                    ImInboundMessage.received_at,
                    ImInboundMessage.id,
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            active is not None
            and active.processing_owner == owner_instance_id
            and int(active.processing_lease_epoch or -1) == lease_epoch
        ):
            await self.db.rollback()
            return None
        if active is not None:
            checkpoint = await self.db.scalar(
                select(ImDeliveryCheckpoint).where(
                    ImDeliveryCheckpoint.inbound_message_id
                    == active.id
                )
            )
            channel = await self.db.get(ImChannel, channel_id)
            if checkpoint is None or channel is None:
                active.status = "dead_letter"
                active.finished_at = now
                active.error_code = "im_recovery_state_missing"
                active.error_message = (
                    "durable delivery state is unavailable"
                )
                await self.db.commit()
                return None
            if active.runtime_run_row_id is None:
                # The old replica stopped before binding the Runtime Run.
                # Requeueing is safe even if it created the row: the Runtime
                # bridge reserves rr-im-wecom-{inbound_id}, so the next owner
                # reuses that same database Run instead of submitting twice.
                active.status = "queued"
                active.processing_owner = None
                active.processing_lease_epoch = None
                active.started_at = None
                await self.db.commit()
                return None
            active.processing_owner = owner_instance_id
            active.processing_lease_epoch = lease_epoch
            active.scheduler_claimed_at = now
            await self.db.commit()
            log_im_timing(active, stage="scheduler_claimed")
            return self._queued(
                message=active,
                channel=channel,
                checkpoint=checkpoint,
                owner_instance_id=owner_instance_id,
                lease_epoch=lease_epoch,
                runtime_run_row_id=int(active.runtime_run_row_id),
                execution_batch_id=(
                    int(active.execution_batch_id)
                    if active.execution_batch_id is not None
                    else None
                ),
                batch_messages=await self._batch_messages(active),
            )
        batch_result = await self._claim_ready_batch(
            channel_id=channel_id,
            channel=await self.db.get(ImChannel, channel_id),
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            now=now,
        )
        if batch_result is not None:
            await self.db.commit()
            log_im_timing(
                batch_result[1], stage="scheduler_claimed"
            )
            return batch_result[0]
        row = (
            await self.db.execute(
                select(
                    ImInboundMessage,
                    ImChannel,
                    ImDeliveryCheckpoint,
                )
                .join(
                    ImChannel,
                    ImChannel.id == ImInboundMessage.channel_id,
                )
                .join(
                    ImDeliveryCheckpoint,
                    ImDeliveryCheckpoint.inbound_message_id
                    == ImInboundMessage.id,
                )
                .where(
                    ImInboundMessage.channel_id == channel_id,
                    ImInboundMessage.status.in_(
                        ("staging_media", "queued")
                    ),
                    or_(
                        ImInboundMessage.dispatch_lane.is_(None),
                        ImInboundMessage.dispatch_lane
                        != "fast_lane_v2",
                    ),
                )
                .order_by(
                    ImInboundMessage.received_at,
                    ImInboundMessage.id,
                )
                .limit(1)
                .with_for_update()
            )
        ).first()
        if row is None:
            await self.db.rollback()
            return None
        message, channel, checkpoint = row
        # Compatibility path for pre-batch rows and sealed-batch retries. A
        # mutable successor never reaches this branch while its predecessor
        # still owns the execution slot.
        if message.status == "staging_media" or (
            message.next_attempt_at is not None
            and aware_utc(message.next_attempt_at) > now
        ) or not await self._compat_batch_retry_is_claimable(message):
            await self.db.rollback()
            return None
        if await self._ingress_predecessor_blocks(message):
            await self.db.rollback()
            return None
        message.status = "running"
        message.started_at = now
        message.scheduler_claimed_at = now
        message.processing_owner = owner_instance_id
        message.processing_lease_epoch = lease_epoch
        message.attempt_count = int(message.attempt_count) + 1
        await self.db.commit()
        log_im_timing(message, stage="scheduler_claimed")
        return self._queued(
            message=message,
            channel=channel,
            checkpoint=checkpoint,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            runtime_run_row_id=(
                int(message.runtime_run_row_id)
                if message.runtime_run_row_id is not None
                else None
            ),
            execution_batch_id=(
                int(message.execution_batch_id)
                if message.execution_batch_id is not None
                else None
            ),
            batch_messages=await self._batch_messages(message),
        )

    async def _claim_ready_batch(
        self,
        *,
        channel_id: int,
        channel: ImChannel | None,
        owner_instance_id: str,
        lease_epoch: int,
        now: datetime,
    ) -> tuple[QueuedInbound, ImInboundMessage] | None:
        if channel is None:
            return None
        batch = await self.db.scalar(
            select(ImExecutionBatch)
            .where(
                ImExecutionBatch.channel_id == channel_id,
                ImExecutionBatch.state == "ready",
                or_(
                    ImExecutionBatch.next_attempt_at.is_(None),
                    ImExecutionBatch.next_attempt_at <= now,
                ),
            )
            .order_by(ImExecutionBatch.collected_at, ImExecutionBatch.id)
            .limit(1)
            .with_for_update()
        )
        if batch is None or batch.response_inbound_message_id is None:
            return None
        members = list(
            (
                await self.db.execute(
                    select(ImInboundMessage)
                    .where(
                        ImInboundMessage.execution_batch_id == batch.id
                    )
                    .order_by(
                        ImInboundMessage.batch_ordinal,
                        ImInboundMessage.received_at,
                        ImInboundMessage.id,
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        if not members:
            return None
        response = next(
            (
                member
                for member in members
                if int(member.id)
                == int(batch.response_inbound_message_id)
            ),
            None,
        )
        if response is None or any(
            member.status == "staging_media" for member in members
        ):
            return None
        ticketed_members = [
            member for member in members if member.ingress_ticket is not None
        ]
        if ticketed_members and await self._ingress_predecessor_blocks(
            min(ticketed_members, key=lambda member: int(member.ingress_ticket or 0))
        ):
            return None
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id == response.id
            )
            .with_for_update()
        )
        if checkpoint is None:
            return None

        canonical_messages = [
            {
                "inbound_message_id": int(member.id),
                "batch_ordinal": int(member.batch_ordinal or 0),
                "normalized_content": member.normalized_content,
                "quote_payload": member.quote_payload,
            }
            for member in members
        ]
        canonical = json.dumps(
            canonical_messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        batch.payload_digest = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()
        batch.sealed_payload = {"messages": canonical_messages}
        batch.sealed_at = now
        batch.state = "submitting"
        batch.execution_slot_key = batch.collection_slot_key
        batch.collection_slot_key = None
        batch.processing_owner = owner_instance_id
        batch.processing_lease_epoch = lease_epoch

        for member in members:
            member.scheduler_claimed_at = now
            if member is response:
                member.status = "running"
                member.started_at = now
                member.processing_owner = owner_instance_id
                member.processing_lease_epoch = lease_epoch
                member.attempt_count = int(member.attempt_count) + 1
            else:
                member.status = "batched"
                member.finished_at = now
                member.processing_owner = None
                member.processing_lease_epoch = None

        queued_messages = tuple(
            QueuedBatchMessage(
                inbound_message_id=int(member.id),
                batch_ordinal=int(member.batch_ordinal or 0),
                normalized_content=dict(member.normalized_content or {}),
                quote_payload=member.quote_payload,
                provider_request_id=str(
                    member.provider_request_id or ""
                ),
                stream_id=_wecom_provider_stream_id(
                    target_type=str(member.target_type),
                    channel_id=int(member.channel_id),
                    inbound_message_id=int(member.id),
                    external_message_id=str(
                        member.external_message_id or ""
                    ),
                    external_conversation_id=str(
                        member.external_conversation_id or ""
                    ),
                ),
            )
            for member in members
        )
        return (
            self._queued(
                message=response,
                channel=channel,
                checkpoint=checkpoint,
                owner_instance_id=owner_instance_id,
                lease_epoch=lease_epoch,
                runtime_run_row_id=(
                    int(response.runtime_run_row_id)
                    if response.runtime_run_row_id is not None
                    else None
                ),
                execution_batch_id=int(batch.id),
                batch_messages=queued_messages,
            ),
            response,
        )

    async def claim_staging_application(
        self,
        *,
        channel_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> int | None:
        """Claim media for one unblocked application conversation."""

        now = utc_now()
        channel = (
            await self.db.execute(
                select(ImChannel)
                .where(
                    ImChannel.id == channel_id,
                    ImChannel.target_type == "application_agent",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if channel is None:
            await self.db.rollback()
            return None
        lease = (
            await self.db.execute(
                select(ImChannelLease)
                .where(
                    ImChannelLease.channel_id == channel.id,
                    ImChannelLease.owner_instance_id
                    == owner_instance_id,
                    ImChannelLease.lease_epoch == lease_epoch,
                    ImChannelLease.expires_at > now,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if lease is None:
            await self.db.rollback()
            return None
        candidate_id = await self._application_candidate_id(
            channel_id=channel_id,
            status="staging_media",
            now=now,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
        )
        if candidate_id is None:
            await self.db.rollback()
            return None
        row = (
            await self.db.execute(
                select(ImInboundMessage)
                .where(
                    ImInboundMessage.id == candidate_id,
                    ImInboundMessage.channel_id == channel.id,
                    ImInboundMessage.status == "staging_media",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or (
            row.next_attempt_at is not None
            and aware_utc(row.next_attempt_at) > now
        ):
            await self.db.rollback()
            return None
        if await self._ingress_predecessor_blocks(row):
            await self.db.rollback()
            return None
        row.processing_owner = owner_instance_id
        row.processing_lease_epoch = lease_epoch
        row.started_at = now
        row.scheduler_claimed_at = now
        row.next_attempt_at = None
        row.attempt_count = int(row.attempt_count) + 1
        await self.db.commit()
        return int(row.id)

    async def claim_next_application(
        self,
        *,
        channel_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> QueuedInbound | None:
        """Fairly claim one ready application conversation head."""

        now = utc_now()
        channel = (
            await self.db.execute(
                select(ImChannel)
                .where(
                    ImChannel.id == channel_id,
                    ImChannel.target_type == "application_agent",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if channel is None:
            await self.db.rollback()
            return None
        lease = (
            await self.db.execute(
                select(ImChannelLease)
                .where(
                    ImChannelLease.channel_id == channel.id,
                    ImChannelLease.owner_instance_id
                    == owner_instance_id,
                    ImChannelLease.lease_epoch == lease_epoch,
                    ImChannelLease.expires_at > now,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if lease is None:
            await self.db.rollback()
            return None
        settled_terminals = await self._settle_terminal_application_failures(
            channel_id=channel_id,
            now=now,
        )
        settled_deliveries = (
            await self._settle_delivered_application_receipts(
                channel_id=channel_id,
                now=now,
            )
        )
        active_rows = (
            await self.db.execute(
                select(ImInboundMessage)
                .where(
                    ImInboundMessage.channel_id == channel_id,
                    ImInboundMessage.status == "running",
                )
                .order_by(
                    ImInboundMessage.received_at,
                    ImInboundMessage.id,
                )
            )
        ).scalars().all()
        active_keys = {
            str(active.ordering_key)
            for active in active_rows
            if active.ordering_key
            and active.processing_owner == owner_instance_id
            and int(active.processing_lease_epoch or -1) == lease_epoch
        }
        stale_active_id = next(
            (
                int(active.id)
                for active in active_rows
                if not (
                    active.processing_owner == owner_instance_id
                    and int(active.processing_lease_epoch or -1)
                    == lease_epoch
                )
            ),
            None,
        )
        if stale_active_id is not None:
            active = (
                await self.db.execute(
                    select(ImInboundMessage)
                    .where(
                        ImInboundMessage.id == stale_active_id,
                        ImInboundMessage.status == "running",
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if active is None:
                await self.db.rollback()
                return None
            checkpoint = await self.db.scalar(
                select(ImDeliveryCheckpoint)
                .where(
                    ImDeliveryCheckpoint.inbound_message_id
                    == active.id
                )
                .with_for_update()
            )
            if checkpoint is None:
                active.status = "dead_letter"
                active.finished_at = now
                active.error_code = "im_recovery_state_missing"
                active.error_message = (
                    "durable delivery state is unavailable"
                )
                await self.db.commit()
                return None
            elif active.application_agent_run_id is None:
                active.status = "queued"
                active.processing_owner = None
                active.processing_lease_epoch = None
                active.started_at = None
                await self.db.commit()
            else:
                active.processing_owner = owner_instance_id
                active.processing_lease_epoch = lease_epoch
                active.scheduler_claimed_at = now
                await self.db.commit()
                log_im_timing(active, stage="scheduler_claimed")
                return self._queued(
                    message=active,
                    channel=channel,
                    checkpoint=checkpoint,
                    owner_instance_id=owner_instance_id,
                    lease_epoch=lease_epoch,
                    runtime_run_row_id=None,
                    execution_batch_id=(
                        int(active.execution_batch_id)
                        if active.execution_batch_id is not None
                        else None
                    ),
                    batch_messages=await self._batch_messages(active),
                )
        batch_result = await self._claim_ready_batch(
            channel_id=channel_id,
            channel=channel,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            now=now,
        )
        if batch_result is not None:
            await self.db.commit()
            log_im_timing(
                batch_result[1], stage="scheduler_claimed"
            )
            return batch_result[0]
        candidate_id = await self._application_candidate_id(
            channel_id=channel_id,
            status="queued",
            now=now,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
        )
        if candidate_id is None:
            if settled_terminals or settled_deliveries:
                await self.db.commit()
            else:
                await self.db.rollback()
            return None
        message = (
            await self.db.execute(
                select(ImInboundMessage)
                .where(
                    ImInboundMessage.id == candidate_id,
                    ImInboundMessage.channel_id == channel.id,
                    ImInboundMessage.status == "queued",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if message is None or (
            message.next_attempt_at is not None
            and aware_utc(message.next_attempt_at) > now
        ):
            await self.db.rollback()
            return None
        if await self._ingress_predecessor_blocks(message):
            await self.db.rollback()
            return None
        if not await self._compat_batch_retry_is_claimable(message):
            await self.db.rollback()
            return None
        key = str(message.ordering_key or "")
        if not key or key in active_keys:
            await self.db.rollback()
            return None
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == message.id
            )
            .with_for_update()
        )
        if checkpoint is None:
            await self.db.rollback()
            return None
        message.status = "running"
        message.started_at = now
        message.scheduler_claimed_at = now
        message.processing_owner = owner_instance_id
        message.processing_lease_epoch = lease_epoch
        message.attempt_count = int(message.attempt_count) + 1
        await self.db.commit()
        log_im_timing(message, stage="scheduler_claimed")
        return self._queued(
            message=message,
            channel=channel,
            checkpoint=checkpoint,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            runtime_run_row_id=None,
            execution_batch_id=(
                int(message.execution_batch_id)
                if message.execution_batch_id is not None
                else None
            ),
            batch_messages=await self._batch_messages(message),
        )

    async def _settle_delivered_application_receipts(
        self,
        *,
        channel_id: int,
        now: datetime,
    ) -> bool:
        """Repair rows whose provider delivery already reached terminal.

        Older Backend versions could persist a delivered checkpoint and then
        leave the inbound row queued when a later Runtime-control cleanup
        failed.  Re-executing that prompt would duplicate user work; leaving
        it queued blocks the conversation FIFO.  Provider-delivered plus a
        bound Application Run is sufficient durable proof to terminalize only
        the queue wrapper.
        """

        rows = list(
            (
                await self.db.execute(
                    select(ImInboundMessage)
                    .join(
                        ImDeliveryCheckpoint,
                        ImDeliveryCheckpoint.inbound_message_id
                        == ImInboundMessage.id,
                    )
                    .where(
                        ImInboundMessage.channel_id == channel_id,
                        ImInboundMessage.target_type
                        == "application_agent",
                        ImInboundMessage.status.in_(("queued", "running")),
                        ImInboundMessage.application_agent_run_id.is_not(
                            None
                        ),
                        ImDeliveryCheckpoint.status == "delivered",
                    )
                    .order_by(
                        ImInboundMessage.received_at,
                        ImInboundMessage.id,
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        for row in rows:
            row.status = "completed"
            row.next_attempt_at = None
            row.finished_at = row.finished_at or now
            row.processing_owner = None
            row.processing_lease_epoch = None
            if (
                row.ingress_ordering_key is not None
                and row.ingress_ticket is not None
            ):
                row.ingress_ticket_state = "runtime_durable"
            await self._terminalize_batch(
                row=row,
                terminal_status="completed",
                terminal_reason=None,
            )
        return bool(rows)

    async def _settle_terminal_application_failures(
        self,
        *,
        channel_id: int,
        now: datetime,
    ) -> bool:
        """Release wrappers whose exact Application Run is terminal.

        Runtime projections and Fast Lane dispositions are independently
        durable. A process loss after a failed/canceled Application Run can
        otherwise leave the ingress wrapper unfinished forever. Reconcile
        only an explicit FK or an ordinary/promoted canonical Run identity;
        a redirect that still points at its parent is not sufficient proof.
        """

        terminal_run = aliased(ApplicationAgentRun)
        matches = list(
            (
                await self.db.execute(
                    select(
                        ImInboundMessage,
                        terminal_run.id,
                        terminal_run.status,
                        terminal_run.error_code,
                    )
                    .join(
                        terminal_run,
                        or_(
                            ImInboundMessage.application_agent_run_id
                            == terminal_run.id,
                            and_(
                                ImInboundMessage.application_agent_run_id.is_(
                                    None
                                ),
                                ImInboundMessage.runtime_disposition_state.in_(
                                    (
                                        "ordinary_run_accepted",
                                        "promoted",
                                    )
                                ),
                                ImInboundMessage.runtime_canonical_run_id
                                == terminal_run.runtime_run_id,
                            ),
                        ),
                    )
                    .where(
                        ImInboundMessage.channel_id == channel_id,
                        ImInboundMessage.target_type == "application_agent",
                        ImInboundMessage.status.in_(
                            _UNFINISHED_INBOUND_STATUSES
                        ),
                        terminal_run.status.in_(("failed", "canceled")),
                    )
                    .order_by(
                        ImInboundMessage.received_at,
                        ImInboundMessage.id,
                    )
                    .with_for_update()
                )
            ).all()
        )
        seen: set[int] = set()
        for row, run_id, run_status, run_error_code in matches:
            if int(row.id) in seen:
                await self.db.rollback()
                raise ValueError(
                    "Application inbound matches multiple terminal Runs"
                )
            seen.add(int(row.id))
            terminal_status = (
                "canceled" if str(run_status) == "canceled" else "failed"
            )
            error_code = str(
                run_error_code
                or (
                    "APPLICATION_RUNTIME_CANCELED"
                    if terminal_status == "canceled"
                    else "APPLICATION_RUNTIME_FAILED"
                )
            )[:100]
            error_message = (
                "reconciled from exact durable Application Run terminal"
            )
            row.status = terminal_status
            row.application_agent_run_id = int(run_id)
            row.finished_at = row.finished_at or now
            row.next_attempt_at = None
            row.processing_owner = None
            row.processing_lease_epoch = None
            row.error_code = error_code
            row.error_message = error_message
            row.terminal_outcome_digest = self._terminal_outcome_digest(
                state=terminal_status,
                error_code=error_code,
                error_message=error_message,
            )
            checkpoint = await self.db.scalar(
                select(ImDeliveryCheckpoint)
                .where(
                    ImDeliveryCheckpoint.inbound_message_id == row.id
                )
                .with_for_update()
            )
            if checkpoint is not None and str(
                checkpoint.status or ""
            ) not in {"delivered", "completed"}:
                checkpoint.status = "delivery_failed"
                checkpoint.error_code = error_code
                checkpoint.error_message = error_message
            await self._terminalize_batch(
                row=row,
                terminal_status=terminal_status,
                terminal_reason=error_code,
            )
        return bool(matches)

    async def _application_candidate_id(
        self,
        *,
        channel_id: int,
        status: str,
        now: datetime,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> int | None:
        candidate = aliased(ImInboundMessage)
        predecessor = aliased(ImInboundMessage)
        active = aliased(ImInboundMessage)
        blocking_statuses = (
            ("running",)
            if status == "staging_media"
            else ("staging_media", "running")
        )
        earlier = or_(
            predecessor.received_at < candidate.received_at,
            and_(
                predecessor.received_at == candidate.received_at,
                predecessor.id < candidate.id,
            ),
        )
        conditions = [
            candidate.channel_id == channel_id,
            candidate.target_type == "application_agent",
            candidate.status == status,
            candidate.ordering_key.is_not(None),
            or_(
                candidate.dispatch_lane.is_(None),
                candidate.dispatch_lane != "fast_lane_v2",
            ),
            or_(
                candidate.next_attempt_at.is_(None),
                candidate.next_attempt_at <= now,
            ),
            not_(
                exists(
                    select(1).where(
                        predecessor.channel_id == candidate.channel_id,
                        predecessor.ordering_key
                        == candidate.ordering_key,
                        predecessor.status.in_(
                            _UNFINISHED_INBOUND_STATUSES
                        ),
                        earlier,
                    )
                )
            ),
            not_(
                exists(
                    select(1).where(
                        active.channel_id == candidate.channel_id,
                        active.ordering_key == candidate.ordering_key,
                        active.id != candidate.id,
                        active.status.in_(blocking_statuses),
                    )
                )
            ),
        ]
        if status == "queued":
            conditions.append(
                or_(
                    candidate.execution_batch_id.is_(None),
                    exists(
                        select(1).where(
                            ImExecutionBatch.id
                            == candidate.execution_batch_id,
                            ImExecutionBatch.state.in_(
                                _COMPAT_CLAIMABLE_BATCH_STATES
                            ),
                        )
                    ),
                )
            )
        if status == "staging_media":
            conditions.append(
                or_(
                    candidate.processing_owner.is_(None),
                    candidate.processing_owner != owner_instance_id,
                    candidate.processing_lease_epoch.is_(None),
                    candidate.processing_lease_epoch != lease_epoch,
                )
            )
        return await self.db.scalar(
            select(candidate.id)
            .where(*conditions)
            .order_by(candidate.received_at, candidate.id)
            .limit(1)
        )

    async def _compat_batch_retry_is_claimable(
        self,
        message: ImInboundMessage,
    ) -> bool:
        if message.execution_batch_id is None:
            return True
        state = await self.db.scalar(
            select(ImExecutionBatch.state)
            .where(
                ImExecutionBatch.id == message.execution_batch_id
            )
            .with_for_update()
        )
        return str(state or "") in _COMPAT_CLAIMABLE_BATCH_STATES

    async def _batch_messages(
        self,
        message: ImInboundMessage,
    ) -> tuple[QueuedBatchMessage, ...]:
        if message.execution_batch_id is None:
            return ()
        members = list(
            (
                await self.db.execute(
                    select(ImInboundMessage)
                    .where(
                        ImInboundMessage.execution_batch_id
                        == message.execution_batch_id
                    )
                    .order_by(
                        ImInboundMessage.batch_ordinal,
                        ImInboundMessage.received_at,
                        ImInboundMessage.id,
                    )
                )
            ).scalars()
        )
        return tuple(
            QueuedBatchMessage(
                inbound_message_id=int(member.id),
                batch_ordinal=int(member.batch_ordinal or 0),
                normalized_content=dict(member.normalized_content or {}),
                quote_payload=(
                    dict(member.quote_payload)
                    if member.quote_payload
                    else None
                ),
                provider_request_id=str(
                    member.provider_request_id or ""
                ),
                stream_id=_wecom_provider_stream_id(
                    target_type=str(member.target_type),
                    channel_id=int(member.channel_id),
                    inbound_message_id=int(member.id),
                ),
            )
            for member in members
        )

    @staticmethod
    def _queued(
        *,
        message: ImInboundMessage,
        channel: ImChannel,
        checkpoint: ImDeliveryCheckpoint,
        owner_instance_id: str,
        lease_epoch: int,
        runtime_run_row_id: int | None,
        execution_batch_id: int | None = None,
        batch_messages: tuple[QueuedBatchMessage, ...] = (),
    ) -> QueuedInbound:
        return QueuedInbound(
            inbound_message_id=int(message.id),
            channel_id=int(channel.id),
            org_id=int(channel.org_id),
            user_id=(
                int(channel.user_id)
                if channel.user_id is not None
                else None
            ),
            runtime_workspace_id=(
                int(channel.runtime_workspace_id)
                if channel.runtime_workspace_id is not None
                else None
            ),
            runtime_session_row_id=(
                int(channel.runtime_session_row_id)
                if channel.runtime_session_row_id is not None
                else None
            ),
            external_conversation_id=str(
                message.external_conversation_id or ""
            ),
            provider_request_id=str(
                message.provider_request_id or ""
            ),
            message_type=str(message.message_type),
            normalized_content=dict(
                message.normalized_content or {}
            ),
            quote_payload=(
                dict(message.quote_payload)
                if message.quote_payload
                else None
            ),
            attempt_count=int(message.attempt_count),
            stream_id=str(checkpoint.provider_stream_id),
            generation=int(channel.config_generation),
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            execution_batch_id=execution_batch_id,
            batch_messages=batch_messages,
            runtime_run_row_id=runtime_run_row_id,
            target_type=str(channel.target_type),
            application_agent_id=(
                int(channel.application_agent_id)
                if channel.application_agent_id is not None
                else None
            ),
            application_client_id=(
                int(channel.application_client_id)
                if channel.application_client_id is not None
                else None
            ),
            sender_platform_user_id=(
                int(message.sender_platform_user_id)
                if message.sender_platform_user_id is not None
                else None
            ),
            chat_type=str(message.chat_type),
            ordering_key=(
                str(message.ordering_key)
                if message.ordering_key
                else None
            ),
            application_agent_run_id=(
                int(message.application_agent_run_id)
                if message.application_agent_run_id is not None
                else None
            ),
            external_message_id=str(message.external_message_id or ""),
        )

    async def mark_completed(
        self,
        *,
        inbound_message_id: int,
        runtime_run_row_id: int | None,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return False
        row.status = "completed"
        row.runtime_run_row_id = runtime_run_row_id
        row.next_attempt_at = None
        row.finished_at = utc_now()
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.error_code = None
        row.error_message = None
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is not None:
            checkpoint.runtime_run_row_id = runtime_run_row_id
            checkpoint.status = "delivered"
        await self._terminalize_batch(
            row=row,
            terminal_status="completed",
            terminal_reason=None,
        )
        await self.db.commit()
        return True

    async def mark_batched_delivered(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        now = utc_now()
        channel_id = await self.db.scalar(
            select(ImInboundMessage.channel_id).where(
                ImInboundMessage.id == inbound_message_id
            )
        )
        if channel_id is None:
            await self.db.rollback()
            return False
        lease = await self.db.scalar(
            select(ImChannelLease)
            .where(
                ImChannelLease.channel_id == int(channel_id),
                ImChannelLease.owner_instance_id == owner_instance_id,
                ImChannelLease.lease_epoch == lease_epoch,
                ImChannelLease.expires_at > now,
            )
            .with_for_update()
        )
        if lease is None:
            await self.db.rollback()
            return False
        row = await self.db.scalar(
            select(ImInboundMessage)
            .where(
                ImInboundMessage.id == inbound_message_id,
                ImInboundMessage.status == "batched",
            )
            .with_for_update()
        )
        if row is None:
            await self.db.rollback()
            return False
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is None:
            await self.db.rollback()
            return False
        checkpoint.status = "completed"
        checkpoint.final_text = "已接入后续消息，将统一处理。"
        checkpoint.provider_delivered_text = checkpoint.final_text
        checkpoint.last_delivered_at = now
        await self.db.commit()
        return True

    async def bind_runtime_run(
        self,
        *,
        inbound_message_id: int,
        runtime_run_row_id: int,
        runtime_run_id: str | None = None,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return False
        if (
            row.runtime_run_row_id is not None
            and int(row.runtime_run_row_id) != runtime_run_row_id
        ):
            await self.db.rollback()
            return False
        row.runtime_run_row_id = runtime_run_row_id
        if row.execution_batch_id is not None:
            batch = await self.db.scalar(
                select(ImExecutionBatch)
                .where(ImExecutionBatch.id == row.execution_batch_id)
                .with_for_update()
            )
            if batch is not None:
                batch.execution_run_kind = "personal"
                batch.execution_run_row_id = runtime_run_row_id
                batch.execution_runtime_run_id = runtime_run_id
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is not None:
            checkpoint.runtime_run_row_id = runtime_run_row_id
            checkpoint.status = "streaming"
        row.run_created_at = row.run_created_at or utc_now()
        await self.db.commit()
        log_im_timing(row, stage="run_created")
        return True

    async def bind_application_run(
        self,
        *,
        inbound_message_id: int,
        application_agent_run_id: int,
        runtime_run_id: str | None = None,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return False
        if (
            row.application_agent_run_id is not None
            and int(row.application_agent_run_id)
            != application_agent_run_id
        ):
            await self.db.rollback()
            return False
        row.application_agent_run_id = application_agent_run_id
        if (
            row.dispatch_lane != "fast_lane_v2"
            and row.ingress_ordering_key is not None
            and row.ingress_ticket is not None
        ):
            # The ordinary Run now has a durable Runtime identity.  This is
            # the same ticket-release boundary used by a Fast Lane
            # disposition and lets the next message proceed without waiting
            # for model completion. Ordinary attachments are included.
            row.ingress_ticket_state = "runtime_durable"
            row.runtime_disposition_state = "ordinary_run_accepted"
            row.runtime_canonical_run_id = runtime_run_id
        if row.execution_batch_id is not None:
            batch = await self.db.scalar(
                select(ImExecutionBatch)
                .where(ImExecutionBatch.id == row.execution_batch_id)
                .with_for_update()
            )
            if batch is not None:
                batch.execution_run_kind = "application"
                batch.execution_run_row_id = application_agent_run_id
                batch.execution_runtime_run_id = runtime_run_id
        row.run_created_at = row.run_created_at or utc_now()
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is not None:
            checkpoint.status = "streaming"
        await self.db.commit()
        log_im_timing(row, stage="run_created")
        return True

    async def mark_batch_accepted(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return False
        if row.execution_batch_id is None:
            await self.db.rollback()
            return True
        batch = await self.db.scalar(
            select(ImExecutionBatch)
            .where(ImExecutionBatch.id == row.execution_batch_id)
            .with_for_update()
        )
        if batch is None:
            await self.db.rollback()
            return False
        if batch.state == "submitting":
            batch.state = "running"
            batch.accepted_at = batch.accepted_at or utc_now()
        if (
            row.ingress_ordering_key is not None
            and row.ingress_ticket is not None
        ):
            # A personal ordinary batch becomes durable once Runtime has
            # accepted its command. Successors must not wait for the full
            # model response, matching the Fast Lane release boundary.
            row.ingress_ticket_state = "runtime_durable"
        await self.db.commit()
        return batch.state in {"running", "terminal"}

    async def mark_batch_runtime_terminal(
        self,
        *,
        inbound_message_id: int,
        terminal_status: str,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return False
        await self._terminalize_batch(
            row=row,
            terminal_status=terminal_status,
            terminal_reason=(
                "PREEMPTED_BY_NEW_IM_MESSAGE"
                if terminal_status == "canceled"
                else None
            ),
        )
        await self.db.commit()
        return True

    async def _terminalize_batch(
        self,
        *,
        row: ImInboundMessage,
        terminal_status: str,
        terminal_reason: str | None,
    ) -> None:
        if row.execution_batch_id is None:
            return
        batch = await self.db.scalar(
            select(ImExecutionBatch)
            .where(ImExecutionBatch.id == row.execution_batch_id)
            .with_for_update()
        )
        if batch is None:
            return
        now = utc_now()
        # Slot ownership is invalid for every terminal batch. Keep this
        # outside the state transition so a repeated terminalization also
        # repairs rows written by an older Backend version.
        batch.execution_slot_key = None
        batch.collection_slot_key = None
        if batch.state != "terminal":
            batch.state = "terminal"
            batch.terminal_at = now
            batch.terminal_status = terminal_status[:32]
            batch.terminal_reason = terminal_reason
        successor = await self.db.scalar(
            select(ImExecutionBatch)
            .where(
                ImExecutionBatch.predecessor_batch_id == batch.id,
                ImExecutionBatch.state == "interrupting",
            )
            .with_for_update()
        )
        if successor is not None:
            successor.state = "ready"
            successor.predecessor_released_at = now

    async def mark_runtime_submitted(
        self,
        *,
        inbound_message_id: int,
        occurred_at: datetime,
    ) -> bool:
        """Persist the pre-Dashboard submit instant without gating execution."""

        row = await self.db.get(ImInboundMessage, inbound_message_id)
        if row is None:
            await self.db.rollback()
            return False
        current = row.runtime_submitted_at
        if (
            current is None
            or aware_utc(occurred_at) < aware_utc(current)
        ):
            row.runtime_submitted_at = occurred_at
        await self.db.commit()
        log_im_timing(row, stage="runtime_submitted")
        return True

    async def mark_first_runtime_public_event(
        self,
        *,
        inbound_message_id: int,
        occurred_at: datetime,
    ) -> bool:
        """Persist the first public Runtime event outside its delivery path."""

        row = await self.db.get(ImInboundMessage, inbound_message_id)
        if row is None:
            await self.db.rollback()
            return False
        if row.first_runtime_public_event_at is None:
            row.first_runtime_public_event_at = occurred_at
        await self.db.commit()
        log_im_timing(row, stage="first_runtime_public_event")
        return True

    async def mark_ready_to_deliver(
        self,
        *,
        inbound_message_id: int,
        final_text: str,
        runtime_run_id: str,
        owner_instance_id: str,
        lease_epoch: int,
        runtime_run_row_id: int | None = None,
        application_agent_run_id: int | None = None,
        artifact_ids: list[int] | None = None,
    ) -> str | None:
        """Durably seal one Runtime result before contacting WeCom."""

        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return None
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is None:
            await self.db.rollback()
            return None
        normalized_artifacts = sorted(
            {int(value) for value in artifact_ids or []}
        )
        digest_payload = json.dumps(
            {
                "runtime_run_id": runtime_run_id,
                "final_text": final_text,
                "artifact_ids": normalized_artifacts,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        delivery_digest = hashlib.sha256(
            digest_payload.encode("utf-8")
        ).hexdigest()
        if (
            checkpoint.delivery_digest
            and checkpoint.delivery_digest != delivery_digest
        ):
            await self.db.rollback()
            raise ValueError(
                "a different terminal result is already sealed for delivery"
            )
        if runtime_run_row_id is not None:
            if (
                row.runtime_run_row_id is not None
                and int(row.runtime_run_row_id) != runtime_run_row_id
            ):
                await self.db.rollback()
                raise ValueError("Runtime Run binding changed before delivery")
            row.runtime_run_row_id = runtime_run_row_id
            checkpoint.runtime_run_row_id = runtime_run_row_id
        if application_agent_run_id is not None:
            if (
                row.application_agent_run_id is not None
                and int(row.application_agent_run_id)
                != application_agent_run_id
            ):
                await self.db.rollback()
                raise ValueError(
                    "Application Agent Run binding changed before delivery"
                )
            row.application_agent_run_id = application_agent_run_id
        # Existing in-flight records keep the already exposed stream ID.
        # Newly ingested records are deterministic from the beginning.
        expected_stream_id = str(
            checkpoint.provider_stream_id
            or _wecom_provider_stream_id(
                target_type=str(row.target_type),
                channel_id=int(row.channel_id),
                inbound_message_id=int(row.id),
            )
        )
        checkpoint.provider_stream_id = expected_stream_id
        checkpoint.final_text = final_text
        checkpoint.delivery_digest = delivery_digest
        checkpoint.delivered_artifact_ids = normalized_artifacts
        if str(checkpoint.status or "") not in {
            "delivered",
            "completed",
            "delivery_uncertain",
        }:
            checkpoint.status = "ready_to_deliver"
            checkpoint.error_code = None
            checkpoint.error_message = None
        # This timestamp is the Runtime terminal boundary.  It remains stable
        # after provider delivery and therefore also bounds delivery retries.
        row.finished_at = row.finished_at or utc_now()
        await self.db.commit()
        return expected_stream_id

    async def begin_delivery(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
        max_attempts: int = 3,
        deadline_seconds: int = 300,
    ) -> str:
        """Move a sealed answer to DISPATCHING under the current Lease."""

        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return "stale"
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is None:
            await self.db.rollback()
            return "stale"
        current = str(checkpoint.status or "pending")
        if current in {"delivered", "completed"}:
            await self.db.rollback()
            return "delivered"
        if current == "delivery_uncertain":
            await self.db.rollback()
            return "delivery_uncertain"
        terminal_at = aware_utc(row.finished_at or utc_now())
        expired = (
            utc_now() - terminal_at
        ).total_seconds() > deadline_seconds
        if (
            expired
            or int(checkpoint.attempt_count or 0) >= max_attempts
        ):
            checkpoint.status = "delivery_failed"
            checkpoint.error_code = "WECOM_DELIVERY_EXHAUSTED"
            checkpoint.error_message = (
                "WeCom delivery retry limit or deadline was reached"
            )
            await self.db.commit()
            return "delivery_failed"
        if current not in {
            "ready_to_deliver",
            "delivery_failed",
            "dispatching",
        }:
            await self.db.rollback()
            return "not_ready"
        checkpoint.status = "dispatching"
        checkpoint.attempt_count = int(
            checkpoint.attempt_count or 0
        ) + 1
        checkpoint.error_code = None
        checkpoint.error_message = None
        await self.db.commit()
        return "dispatching"

    async def retry_or_finish_delivery(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
        uncertain: bool = False,
        max_attempts: int = 3,
        deadline_seconds: int = 300,
    ) -> str:
        """Retry provider delivery only; never make the Runtime prompt runnable."""

        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return "stale"
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is None:
            await self.db.rollback()
            return "stale"
        now = utc_now()
        terminal_at = aware_utc(row.finished_at or now)
        exhausted = (
            int(checkpoint.attempt_count or 0) >= max_attempts
            or (now - terminal_at).total_seconds() > deadline_seconds
        )
        if uncertain:
            checkpoint.status = "delivery_uncertain"
            row.status = "failed"
        elif exhausted:
            checkpoint.status = "delivery_failed"
            row.status = "failed"
        else:
            checkpoint.status = "ready_to_deliver"
            row.status = "queued"
            row.next_attempt_at = now + timedelta(
                seconds=min(
                    15,
                    2 ** max(0, int(checkpoint.attempt_count or 1) - 1),
                )
            )
        if row.status == "failed":
            row.next_attempt_at = None
        row.processing_owner = None
        row.processing_lease_epoch = None
        await self.db.commit()
        return str(checkpoint.status)

    async def mark_application_completed(
        self,
        *,
        inbound_message_id: int,
        application_agent_run_id: int | None,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        for attempt in range(2):
            try:
                return await self._mark_application_completed_once(
                    inbound_message_id=inbound_message_id,
                    application_agent_run_id=application_agent_run_id,
                    owner_instance_id=owner_instance_id,
                    lease_epoch=lease_epoch,
                )
            except OperationalError as exc:
                if not _is_mysql_deadlock(exc):
                    raise
                await self.db.rollback()
                if attempt >= 1:
                    raise
                await asyncio.sleep(
                    _APPLICATION_COMPLETION_DEADLOCK_RETRY_SECONDS
                )
        raise AssertionError(
            "application completion retry loop exhausted without an exception"
        )

    async def _mark_application_completed_once(
        self,
        *,
        inbound_message_id: int,
        application_agent_run_id: int | None,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return False
        row.status = "completed"
        row.application_agent_run_id = application_agent_run_id
        row.next_attempt_at = None
        row.finished_at = utc_now()
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.error_code = None
        row.error_message = None
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is not None:
            checkpoint.status = "delivered"
            checkpoint.error_code = None
            checkpoint.error_message = None
        await self._terminalize_batch(
            row=row,
            terminal_status="completed",
            terminal_reason=None,
        )
        await self.db.commit()
        return True

    async def persist_delivery_progress(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
        last_runtime_event_seq: int,
        reasoning_text: str,
        activity_text: str,
        message_text: str,
        final_text: str,
        provider_delivered_text: str,
        delivery_mode: str,
        status: str,
        error_code: str | None,
        error_message: str | None,
        first_provider_dispatch_at: datetime | None,
        first_provider_confirmed_at: datetime | None,
    ) -> DeliveryProgressResult:
        """Persist projector state in Lease→Inbound→Checkpoint lock order."""

        inbound = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if inbound is None:
            return DeliveryProgressResult(persisted=False)
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is None:
            await self.db.rollback()
            return DeliveryProgressResult(persisted=False)

        checkpoint.last_runtime_event_seq = max(
            int(checkpoint.last_runtime_event_seq or 0),
            int(last_runtime_event_seq),
        )
        checkpoint.reasoning_text = reasoning_text
        checkpoint.activity_text = activity_text
        checkpoint.message_text = message_text
        checkpoint.final_text = final_text
        checkpoint.provider_delivered_text = provider_delivered_text
        checkpoint.delivery_mode = delivery_mode
        checkpoint.status = status
        checkpoint.error_code = error_code
        checkpoint.error_message = (
            error_message[:2000] if error_message else None
        )
        if (
            provider_delivered_text
            and int(checkpoint.last_runtime_event_seq or 0)
            == int(last_runtime_event_seq)
        ):
            checkpoint.last_delivered_at = utc_now()

        if (
            inbound.first_provider_dispatch_at is None
            and first_provider_dispatch_at is not None
        ):
            inbound.first_provider_dispatch_at = (
                first_provider_dispatch_at
            )
        first_confirmation_recorded = False
        if (
            inbound.first_provider_confirmed_at is None
            and first_provider_confirmed_at is not None
        ):
            inbound.first_provider_confirmed_at = (
                first_provider_confirmed_at
            )
            first_confirmation_recorded = True
        await self.db.commit()
        return DeliveryProgressResult(
            persisted=True,
            first_provider_confirmation_recorded=(
                first_confirmation_recorded
            ),
            inbound_message=inbound,
        )

    async def mark_artifact_delivered(
        self,
        *,
        inbound_message_id: int,
        artifact_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return False
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is None:
            await self.db.rollback()
            return False
        delivered = {
            int(value)
            for value in (checkpoint.delivered_artifact_ids or [])
            if isinstance(value, int)
            or (isinstance(value, str) and value.isdigit())
        }
        delivered.add(int(artifact_id))
        checkpoint.delivered_artifact_ids = sorted(delivered)
        await self.db.commit()
        return True

    async def clear_artifact_delivery_failure(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return False
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is None:
            await self.db.rollback()
            return False
        if str(checkpoint.error_code or "").startswith(
            "WECOM_MEDIA_"
        ):
            checkpoint.error_code = None
            checkpoint.error_message = None
        await self.db.commit()
        return True

    async def mark_artifact_delivery_failed(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
        error_code: str,
        error_message: str,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return False
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is None:
            await self.db.rollback()
            return False
        checkpoint.attempt_count = int(checkpoint.attempt_count) + 1
        checkpoint.error_code = f"WECOM_MEDIA_{error_code}"[:100]
        checkpoint.error_message = error_message[:2000]
        await self.db.commit()
        return True

    async def mark_runtime_terminal_failed(
        self,
        *,
        inbound_message_id: int,
        error_code: str,
        error_message: str,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return False
        row.status = "failed"
        row.finished_at = utc_now()
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.error_code = error_code[:100]
        row.error_message = error_message[:2000]
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint)
            .where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
            .with_for_update()
        )
        if checkpoint is not None:
            if str(checkpoint.status or "") not in {
                "delivered",
                "completed",
            }:
                checkpoint.status = "delivery_failed"
        await self._terminalize_batch(
            row=row,
            terminal_status="failed",
            terminal_reason=error_code[:100],
        )
        await self.db.commit()
        return True

    async def preempted_final_text(
        self,
        *,
        inbound_message_id: int,
    ) -> str | None:
        row = await self.db.get(ImInboundMessage, inbound_message_id)
        if row is None or row.execution_batch_id is None:
            return None
        batch = await self.db.get(
            ImExecutionBatch, int(row.execution_batch_id)
        )
        if (
            batch is None
            or batch.terminal_reason
            != "PREEMPTED_BY_NEW_IM_MESSAGE"
        ):
            return None
        checkpoint = await self.db.scalar(
            select(ImDeliveryCheckpoint).where(
                ImDeliveryCheckpoint.inbound_message_id
                == inbound_message_id
            )
        )
        partial = str(
            (checkpoint.message_text if checkpoint is not None else "")
            or ""
        )
        return (
            f"{partial}\n\n本轮已被新消息打断。"
            if partial
            else "本轮已被新消息打断。"
        )

    async def mark_preempted(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return False
        row.status = "canceled"
        row.finished_at = utc_now()
        row.next_attempt_at = None
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.error_code = "PREEMPTED_BY_NEW_IM_MESSAGE"
        row.error_message = "superseded by a newer WeCom message"
        await self._terminalize_batch(
            row=row,
            terminal_status="canceled",
            terminal_reason="PREEMPTED_BY_NEW_IM_MESSAGE",
        )
        await self.db.commit()
        return True

    async def mark_failed(
        self,
        *,
        inbound_message_id: int,
        error_code: str,
        error_message: str,
        max_attempts: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> str:
        for attempt in range(2):
            try:
                return await self._mark_failed_once(
                    inbound_message_id=inbound_message_id,
                    error_code=error_code,
                    error_message=error_message,
                    max_attempts=max_attempts,
                    owner_instance_id=owner_instance_id,
                    lease_epoch=lease_epoch,
                )
            except OperationalError as exc:
                if not _is_mysql_deadlock(exc):
                    raise
                await self.db.rollback()
                if attempt >= 1:
                    raise
                await asyncio.sleep(
                    _APPLICATION_COMPLETION_DEADLOCK_RETRY_SECONDS
                )
        raise AssertionError(
            "application failure retry loop exhausted without an exception"
        )

    async def _mark_failed_once(
        self,
        *,
        inbound_message_id: int,
        error_code: str,
        error_message: str,
        max_attempts: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> str:
        row = await self._owned_message(
            inbound_message_id=inbound_message_id,
            owner_instance_id=owner_instance_id,
            lease_epoch=lease_epoch,
            expected_status="running",
        )
        if row is None:
            return "stale"
        terminal = int(row.attempt_count) >= max_attempts
        row.status = "dead_letter" if terminal else "queued"
        row.next_attempt_at = (
            None
            if terminal
            else utc_now()
            + timedelta(seconds=min(60, 2 ** int(row.attempt_count)))
        )
        row.finished_at = utc_now() if terminal else None
        row.processing_owner = None
        row.processing_lease_epoch = None
        row.error_code = error_code[:100]
        row.error_message = error_message[:2000]
        if terminal:
            if (
                row.ingress_ordering_key is not None
                and row.ingress_ticket is not None
                and row.ingress_ticket_state != "runtime_durable"
            ):
                row.ingress_ticket_state = "dead_letter"
                row.terminal_outcome_digest = (
                    self._terminal_outcome_digest(
                        state="dead_letter",
                        error_code=str(row.error_code),
                        error_message=str(row.error_message),
                    )
                )
            await self._terminalize_batch(
                row=row,
                terminal_status="dead_letter",
                terminal_reason=error_code[:100],
            )
        await self.db.commit()
        log_im_timing(
            row,
            stage="failed",
            status="error",
            error_type=error_code,
        )
        return str(row.status)

    async def owns_processing(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
        expected_status: str = "running",
    ) -> bool:
        now = utc_now()
        value = await self.db.scalar(
            select(ImInboundMessage.id)
            .join(
                ImChannelLease,
                ImChannelLease.channel_id
                == ImInboundMessage.channel_id,
            )
            .where(
                ImInboundMessage.id == inbound_message_id,
                ImInboundMessage.status == expected_status,
                ImInboundMessage.processing_owner == owner_instance_id,
                ImInboundMessage.processing_lease_epoch == lease_epoch,
                ImChannelLease.owner_instance_id == owner_instance_id,
                ImChannelLease.lease_epoch == lease_epoch,
                ImChannelLease.expires_at > now,
            )
        )
        return value is not None

    async def _owned_message(
        self,
        *,
        inbound_message_id: int,
        owner_instance_id: str,
        lease_epoch: int,
        expected_status: str,
    ) -> ImInboundMessage | None:
        now = utc_now()
        channel_id = await self.db.scalar(
            select(ImInboundMessage.channel_id).where(
                ImInboundMessage.id == inbound_message_id,
            )
        )
        if channel_id is None:
            await self.db.rollback()
            return None
        lease = await self.db.scalar(
            select(ImChannelLease)
            .where(
                ImChannelLease.channel_id == int(channel_id),
                ImChannelLease.owner_instance_id
                == owner_instance_id,
                ImChannelLease.lease_epoch == lease_epoch,
                ImChannelLease.expires_at > now,
            )
            .with_for_update()
        )
        if lease is None:
            await self.db.rollback()
            return None
        row = (
            await self.db.execute(
                select(ImInboundMessage)
                .where(
                    ImInboundMessage.id == inbound_message_id,
                    ImInboundMessage.channel_id == int(channel_id),
                    ImInboundMessage.status == expected_status,
                    ImInboundMessage.processing_owner
                    == owner_instance_id,
                    ImInboundMessage.processing_lease_epoch
                    == lease_epoch,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            await self.db.rollback()
        return row
