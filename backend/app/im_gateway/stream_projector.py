from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.hermes_runtime.events import PlatformStreamEvent
from app.models import (
    ImDeliveryCheckpoint,
    RuntimeRunEvent,
)

from .node_bridge import WeComBridgeError, WeComNodeBridge
from .queue import ImInboundQueue, log_im_timing
from .wecom_projection import WeComProjection


_PASSIVE_STREAM_LIMIT_BYTES = 19_000
_ACTIVE_MARKDOWN_LIMIT_BYTES = 3_500
_ACTIVE_DELIVERY_MODES = {"active_buffered", "active_markdown"}
_timing_logger = logging.getLogger("uvicorn.error.runtime_timing")


@dataclass(frozen=True)
class ProjectorIdentity:
    inbound_message_id: int
    channel_id: int
    generation: int
    owner_instance_id: str
    lease_epoch: int
    provider_request_id: str
    provider_stream_id: str
    chat_id: str
    target_type: str = "personal_agent"


@dataclass
class ProjectorState:
    last_seq: int = 0
    projection: WeComProjection = field(
        default_factory=WeComProjection
    )
    logical_text: str = ""
    delivered_text: str = ""
    delivery_mode: str = "passive_stream"
    status: str = "pending"

    @property
    def reasoning_text(self) -> str:
        return self.projection.reasoning_text

    @property
    def activity_text(self) -> str:
        return self.projection.serialize_activity()

    @property
    def message_text(self) -> str:
        return self.projection.message_text


class WeComStreamProjector:
    """Project persisted public Platform events into an ordered WeCom reply."""

    def __init__(
        self,
        *,
        bridge: WeComNodeBridge,
        identity: ProjectorIdentity,
        flush_seconds: float | None = None,
        delivery_ack_sink: Callable[[int, datetime], Awaitable[None]]
        | None = None,
        durable_projection: bool = True,
    ) -> None:
        self.bridge = bridge
        self.identity = identity
        self.flush_seconds = (
            settings.IM_GATEWAY_REASONING_FLUSH_SECONDS
            if flush_seconds is None
            else flush_seconds
        )
        self.state = ProjectorState()
        self._last_flush = 0.0
        self._lock = asyncio.Lock()
        self._artifacts: list[int] = []
        self._first_provider_dispatch_at: datetime | None = None
        self._first_provider_confirmed_at: datetime | None = None
        self._terminal_delivery_protocol = False
        self._delivery_ack_sink = delivery_ack_sink
        self._durable_projection = durable_projection

    async def load(self) -> None:
        if not self._durable_projection:
            return
        async with AsyncSessionLocal() as db:
            row = await db.scalar(
                select(ImDeliveryCheckpoint).where(
                    ImDeliveryCheckpoint.inbound_message_id
                    == self.identity.inbound_message_id
                )
            )
            if row is None:
                return

            row_status = str(row.status or "pending")
            projection = WeComProjection.restore(
                reasoning_text=row.reasoning_text,
                message_text=row.message_text,
                activity_text=row.activity_text,
            )
            keep_terminal_snapshot = (
                projection is None
                and (
                    row_status == "completed"
                    or (
                        bool(getattr(row, "delivery_digest", None))
                        and row_status
                        in {
                            "ready_to_deliver",
                            "dispatching",
                            "delivered",
                            "delivery_failed",
                            "delivery_uncertain",
                        }
                    )
                )
            )
            if projection is None:
                projection = WeComProjection()
                rebuilt = False
                if (
                    not keep_terminal_snapshot
                    and row.runtime_run_row_id is not None
                    and int(row.last_runtime_event_seq or 0) > 0
                ):
                    events = (
                        await db.execute(
                            select(RuntimeRunEvent)
                            .where(
                                RuntimeRunEvent.run_id
                                == int(row.runtime_run_row_id),
                                RuntimeRunEvent.seq
                                <= int(row.last_runtime_event_seq),
                            )
                            .order_by(RuntimeRunEvent.seq)
                        )
                    ).scalars().all()
                    for event in events:
                        projection.apply(
                            str(event.event_name),
                            dict(event.payload or {}),
                        )
                        rebuilt = True
                if not rebuilt and not keep_terminal_snapshot:
                    # A pre-v2 in-flight checkpoint may outlive the Runtime
                    # event retention window. Preserve its safe semantic
                    # fields, but deliberately drop legacy raw tool output.
                    projection.reasoning_text = str(
                        row.reasoning_text or ""
                    )
                    projection.message_text = str(row.message_text or "")
                    projection.reasoning_closed = bool(
                        projection.message_text
                        or "</think>" in str(row.final_text or "")
                    )

            delivery_mode = str(
                row.delivery_mode or "passive_stream"
            )
            if delivery_mode not in {
                "passive_stream",
                "active_buffered",
                "active_markdown",
            }:
                delivery_mode = "passive_stream"
            if (
                delivery_mode == "active_markdown"
                and row_status != "completed"
            ):
                # Active messages cannot be mutated in place. On takeover,
                # wait for the terminal state and send one coherent snapshot.
                delivery_mode = "active_buffered"

            logical_text = (
                str(row.final_text or "")
                if keep_terminal_snapshot
                else projection.render_passive()
            )
            self.state = ProjectorState(
                last_seq=int(row.last_runtime_event_seq or 0),
                projection=projection,
                logical_text=logical_text,
                delivered_text=str(
                    row.provider_delivered_text or ""
                ),
                delivery_mode=delivery_mode,
                status=row_status,
            )

    async def deliver_final(self, final_text: str) -> str:
        """Deliver one already-sealed terminal answer without token replay."""

        async with self._lock:
            if self._durable_projection:
                async with AsyncSessionLocal() as db:
                    state = await ImInboundQueue(db).begin_delivery(
                        inbound_message_id=self.identity.inbound_message_id,
                        owner_instance_id=self.identity.owner_instance_id,
                        lease_epoch=self.identity.lease_epoch,
                    )
                if state != "dispatching":
                    self.state.status = state
                    return state
            self._terminal_delivery_protocol = True
            self.state.projection.apply(
                "chat.message.completed",
                {"text": final_text},
            )
            self.state.logical_text = (
                self.state.projection.render_passive()
            )
            await self._flush(finish=True)
            return self.state.status

    async def on_event(self, event: PlatformStreamEvent) -> None:
        async with self._lock:
            seq = int(event.data.get("seq") or 0)
            if seq and seq <= self.state.last_seq:
                return

            terminal = event.event == "chat.done"
            changed = self.state.projection.apply(
                event.event, event.data
            )
            if seq:
                # Projection state and its replay cursor are persisted
                # atomically, so takeover never applies a public delta twice.
                self.state.last_seq = max(self.state.last_seq, seq)
            if event.event == "chat.artifact.created":
                file_id = event.data.get("file_id")
                if isinstance(file_id, int):
                    self._artifacts.append(file_id)

            if changed or terminal:
                self.state.logical_text = (
                    self.state.projection.render_passive()
                )
            if terminal:
                await self._flush(finish=True)
                return

            self.state.status = "streaming"
            if not changed:
                await self._persist(last_seq=self.state.last_seq)
                return
            if self.state.delivery_mode in _ACTIVE_DELIVERY_MODES:
                await self._persist(last_seq=self.state.last_seq)
                return

            now = time.monotonic()
            if now - self._last_flush < self.flush_seconds:
                await self._persist(last_seq=self.state.last_seq)
                return
            if (
                len(self.state.logical_text.encode("utf-8"))
                > _PASSIVE_STREAM_LIMIT_BYTES
            ):
                # The final flush will close a syntactically valid native
                # reasoning card and move the remainder to active Markdown.
                await self._persist(last_seq=self.state.last_seq)
                return
            await self._flush(finish=False)

    async def _flush(self, *, finish: bool) -> None:
        logical = self.state.projection.render_passive()
        self.state.logical_text = logical
        if not await self._ownership_current():
            self.state.status = "delivery_failed"
            return

        if self.state.delivery_mode in _ACTIVE_DELIVERY_MODES:
            if not finish:
                await self._persist(last_seq=self.state.last_seq)
                return
            await self._finish_active(logical)
            return

        if (
            not finish
            and len(logical.encode("utf-8"))
            > _PASSIVE_STREAM_LIMIT_BYTES
        ):
            await self._persist(last_seq=self.state.last_seq)
            return
        if (
            finish
            and len(logical.encode("utf-8"))
            > _PASSIVE_STREAM_LIMIT_BYTES
        ):
            await self._finish_oversized_passive(logical)
            return

        try:
            self._mark_provider_dispatch_started()
            delivered = await self.bridge.reply_stream(
                channel_id=self.identity.channel_id,
                generation=self.identity.generation,
                lease_epoch=self.identity.lease_epoch,
                provider_request_id=self.identity.provider_request_id,
                stream_id=self.identity.provider_stream_id,
                content=logical or (
                    "处理完成。" if finish else "<think></think>"
                ),
                finish=finish,
                non_blocking=not finish,
            )
        except WeComBridgeError as exc:
            await self._handle_passive_error(
                exc, logical=logical, finish=finish
            )
            return

        if not delivered:
            if finish:
                self.state.status = "delivery_failed"
                await self._persist(
                    last_seq=self.state.last_seq,
                    error_code="WECOM_STREAM_SKIPPED",
                    error_message=(
                        "WeCom skipped the terminal stream reply"
                    ),
                )
            else:
                await self._persist(last_seq=self.state.last_seq)
            return

        self._mark_provider_confirmed()
        self.state.delivered_text = logical
        await self._mark_success(finish=finish)

    async def _finish_oversized_passive(
        self, logical: str
    ) -> None:
        passive, continuation = (
            self.state.projection.split_oversized_terminal(
                _PASSIVE_STREAM_LIMIT_BYTES
            )
        )
        try:
            self._mark_provider_dispatch_started()
            delivered = await self.bridge.reply_stream(
                channel_id=self.identity.channel_id,
                generation=self.identity.generation,
                lease_epoch=self.identity.lease_epoch,
                provider_request_id=self.identity.provider_request_id,
                stream_id=self.identity.provider_stream_id,
                content=passive,
                finish=True,
                non_blocking=False,
            )
        except WeComBridgeError as exc:
            await self._handle_passive_error(
                exc, logical=logical, finish=True
            )
            return
        if not delivered:
            self.state.status = "delivery_failed"
            await self._persist(
                last_seq=self.state.last_seq,
                error_code="WECOM_STREAM_SKIPPED",
                error_message="WeCom skipped the terminal stream reply",
            )
            return

        self._mark_provider_confirmed()
        self.state.delivered_text = passive
        if continuation:
            # The passive frame is already terminal at this point. Record
            # this before active delivery so a retry cannot try to mutate it.
            self.state.delivery_mode = "active_markdown"
            try:
                await self._send_active_content(continuation)
            except WeComBridgeError as exc:
                self.state.status = (
                    "delivery_uncertain"
                    if self._terminal_delivery_protocol
                    else "delivery_failed"
                )
                await self._persist(
                    last_seq=self.state.last_seq,
                    error_code=exc.code,
                    error_message=str(exc),
                )
                return

        self.state.delivered_text = logical
        await self._mark_success(finish=True)

    async def _handle_passive_error(
        self,
        exc: WeComBridgeError,
        *,
        logical: str,
        finish: bool,
    ) -> None:
        # Callback frames expire. Active Markdown cannot update an existing
        # message, so intermediate states are buffered and exactly one clean
        # terminal snapshot is sent.
        self.state.delivery_mode = "active_buffered"
        if not finish:
            self.state.status = "streaming"
            await self._persist(
                last_seq=self.state.last_seq,
                error_code=exc.code,
                error_message=str(exc),
            )
            return
        await self._finish_active(logical, passive_error=exc)

    async def _finish_active(
        self,
        logical: str,
        *,
        passive_error: WeComBridgeError | None = None,
    ) -> None:
        content = self.state.projection.render_active() or "处理完成。"
        try:
            await self._send_active_content(content)
        except WeComBridgeError as exc:
            self.state.status = (
                "delivery_uncertain"
                if self._terminal_delivery_protocol
                else "delivery_failed"
            )
            await self._persist(
                last_seq=self.state.last_seq,
                error_code=exc.code or (
                    passive_error.code if passive_error else None
                ),
                error_message=str(exc),
            )
            return
        self.state.delivery_mode = "active_markdown"
        self.state.delivered_text = logical
        await self._mark_success(finish=True)

    async def _send_active_content(self, content: str) -> None:
        for chunk in _semantic_chunks(
            content, _ACTIVE_MARKDOWN_LIMIT_BYTES
        ):
            self._mark_provider_dispatch_started()
            await self.bridge.send_markdown(
                channel_id=self.identity.channel_id,
                generation=self.identity.generation,
                lease_epoch=self.identity.lease_epoch,
                chat_id=self.identity.chat_id,
                content=chunk,
            )
            self._mark_provider_confirmed()

    def _mark_provider_dispatch_started(self) -> None:
        if self._first_provider_dispatch_at is not None:
            return
        self._first_provider_dispatch_at = datetime.now(timezone.utc)
        try:
            payload: dict[str, object] = {
                "component": (
                    "application_agent_im"
                    if self.identity.target_type
                    == "application_agent"
                    else "personal_agent_im"
                ),
                "channel": "wecom",
                "stage": "first_provider_dispatch",
                "status": "success",
                "inbound_message_id": (
                    self.identity.inbound_message_id
                ),
            }
            if self.identity.target_type == "application_agent":
                payload["request_id"] = (
                    "im-wecom-"
                    f"{self.identity.inbound_message_id}"
                )
            _timing_logger.info("agent ttft timing=%s", payload)
        except Exception:
            pass

    def _mark_provider_confirmed(self) -> None:
        if self._first_provider_confirmed_at is None:
            self._first_provider_confirmed_at = datetime.now(
                timezone.utc
            )

    async def _mark_success(self, *, finish: bool) -> None:
        self.state.status = (
            "delivered"
            if finish and self._terminal_delivery_protocol
            else "completed"
            if finish
            else "streaming"
        )
        self._last_flush = time.monotonic()
        await self._persist(last_seq=self.state.last_seq)
        if (
            self._delivery_ack_sink is not None
            and self.state.last_seq > 0
            and self._first_provider_confirmed_at is not None
        ):
            try:
                await self._delivery_ack_sink(
                    self.state.last_seq,
                    datetime.now(timezone.utc),
                )
            except Exception as exc:
                _timing_logger.warning(
                    "WeCom Fast Lane delivery checkpoint deferred: %s",
                    type(exc).__name__,
                )

    async def _persist(
        self,
        *,
        last_seq: int,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if not self._durable_projection:
            return
        async with AsyncSessionLocal() as db:
            result = await ImInboundQueue(db).persist_delivery_progress(
                inbound_message_id=self.identity.inbound_message_id,
                owner_instance_id=self.identity.owner_instance_id,
                lease_epoch=self.identity.lease_epoch,
                last_runtime_event_seq=last_seq,
                reasoning_text=self.state.reasoning_text,
                activity_text=self.state.activity_text,
                message_text=self.state.message_text,
                final_text=self.state.logical_text,
                provider_delivered_text=self.state.delivered_text,
                delivery_mode=self.state.delivery_mode,
                status=self.state.status,
                error_code=error_code,
                error_message=error_message,
                first_provider_dispatch_at=(
                    self._first_provider_dispatch_at
                ),
                first_provider_confirmed_at=(
                    self._first_provider_confirmed_at
                ),
            )
            if not result.persisted:
                self.state.status = "delivery_failed"
                return
            if (
                result.first_provider_confirmation_recorded
                and result.inbound_message is not None
            ):
                log_im_timing(
                    result.inbound_message,
                    stage="first_provider_confirmed",
                )

    async def _ownership_current(self) -> bool:
        if not self._durable_projection:
            return True
        async with AsyncSessionLocal() as db:
            return await ImInboundQueue(db).owns_processing(
                inbound_message_id=self.identity.inbound_message_id,
                owner_instance_id=self.identity.owner_instance_id,
                lease_epoch=self.identity.lease_epoch,
            )


def _utf8_prefix(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _semantic_chunks(value: str, limit: int) -> list[str]:
    """Split exactly on readable boundaries without losing UTF-8 text."""

    if not value:
        return []
    if limit <= 0:
        raise ValueError("chunk byte limit must be positive")

    chunks: list[str] = []
    remaining = value
    while len(remaining.encode("utf-8")) > limit:
        prefix = _utf8_prefix(remaining, limit)
        if not prefix:
            prefix = remaining[0]
        threshold = max(1, len(prefix) // 2)
        candidates: list[int] = []
        for marker in (
            "\n\n",
            "\n",
            "。",
            "！",
            "？",
            "；",
            ". ",
            "! ",
            "? ",
        ):
            index = prefix.rfind(marker)
            if index >= 0:
                cut = index + len(marker)
                if cut >= threshold:
                    candidates.append(cut)
        cut = max(candidates) if candidates else len(prefix)
        chunk = remaining[:cut]
        chunks.append(chunk)
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _utf8_chunks(value: str, limit: int) -> list[str]:
    """Backward-compatible alias for callers testing UTF-8 preservation."""

    return _semantic_chunks(value, limit)
