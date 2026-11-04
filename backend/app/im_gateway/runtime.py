from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from redis.asyncio import from_url
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import (
    ImChannel,
    ImChannelLease,
    ImDeliveryCheckpoint,
    ImInboundMessage,
    ImOnboardingSession,
)
from app.services.application_agent_invocation import (
    ApplicationAgentInvocationError,
)
from app.services.runtime_workspace import Principal
from app.services.runtime_channel_fast_lane import (
    close_runtime_channel_fast_lane_pool,
)

from .leases import OwnedImChannel, SqlAlchemyImChannelLeaseStore
from .identity import OrganizationWeComIdentityService
from .messages import (
    NormalizedWeComMessage,
    WeComGroupMessageIgnored,
    WeComMessageRejected,
    normalize_wecom_message,
)
from .node_bridge import WeComBridgeError, WeComNodeBridge
from .queue import ImInboundQueue, QueuedFastLaneInbound, QueuedInbound
from .outbound import ImOutboundDeliveryStore
from .stream_projector import (
    _ACTIVE_MARKDOWN_LIMIT_BYTES,
    _semantic_chunks,
)
from .runtime_bridge import (
    ApplicationImRuntimeBridge,
    ImPreemptionCoordinator,
    PersonalImRuntimeBridge,
    PreparedWeComFastLaneRoute,
    PreparedWeComFastLaneResult,
    WeComFastLaneArtifactDelivery,
    WeComFastLaneColdPath,
    WeComFastLaneRouteRejected,
    _outbound_media_type,
    _safe_filename,
    execute_durable_wecom_fast_lane_v2,
)


logger = logging.getLogger(__name__)
timing_logger = logging.getLogger("uvicorn.error.runtime_timing")
_CONFIG_CHANNEL = "hermes:im:channel-config"
_WECOM_THINKING_PLACEHOLDER = "<think></think>"


def _log_im_gateway_timing(
    *,
    target_type: str,
    stage: str,
    inbound_message_id: int,
    status: str,
    duration_ms: float | None = None,
    error_type: str | None = None,
) -> None:
    try:
        payload: dict[str, object] = {
            "component": (
                "application_agent_im"
                if target_type == "application_agent"
                else "personal_agent_im"
            ),
            "channel": "wecom",
            "stage": stage,
            "status": status,
            "inbound_message_id": inbound_message_id,
        }
        if target_type == "application_agent":
            payload["request_id"] = (
                f"im-wecom-{inbound_message_id}"
            )
        if duration_ms is not None:
            payload["duration_ms"] = round(duration_ms, 3)
        if error_type:
            payload["error_type"] = error_type
        timing_logger.info("agent ttft timing=%s", payload)
    except Exception:
        pass


class ImGatewayRuntime:
    """Connection actors and FIFO workers owned by one Backend replica."""

    def __init__(self) -> None:
        self.instance_id = (
            f"{socket.gethostname()}:{os.getpid()}:im-gateway"
        )
        self.bridge = WeComNodeBridge(event_handler=self._on_bridge_event)
        self.runtime_bridge = PersonalImRuntimeBridge(
            node_bridge=self.bridge
        )
        self.application_runtime_bridge = ApplicationImRuntimeBridge(
            node_bridge=self.bridge
        )
        self.preemption_coordinator = ImPreemptionCoordinator()
        self.actors: dict[int, OwnedImChannel] = {}
        self._reconcile_lock = asyncio.Lock()
        self._queue_task: asyncio.Task[None] | None = None
        self._redis_task: asyncio.Task[None] | None = None
        self._run_tasks: dict[
            tuple[int, str], asyncio.Task[None]
        ] = {}
        self._preemption_tasks: dict[int, asyncio.Task[None]] = {}
        self._staging_tasks: set[asyncio.Task[None]] = set()
        self._staging_by_channel: dict[
            tuple[int, str], asyncio.Task[None]
        ] = {}
        self._maintenance_tasks: set[asyncio.Task[None]] = set()
        self._hot_turn_tasks: dict[
            tuple[int, str], asyncio.Task[None]
        ] = {}
        self._application_identity_cache: dict[
            tuple[int, str, str], tuple[int, int, int, float]
        ] = {}
        self._personal_runtime_prewarm_tasks: dict[
            int, asyncio.Task[None]
        ] = {}
        self._outbound_tasks: dict[int, asyncio.Task[None]] = {}
        self._queue_wakeup = asyncio.Event()
        self._started = False
        self._last_heartbeat = 0.0
        self._last_onboarding_cleanup = 0.0
        self.group_rejected_count = 0
        self.feedback_event_count = 0
        self.template_card_event_count = 0

    async def start(self) -> None:
        if self._started:
            return
        await self.bridge.start()
        self._started = True
        self._queue_task = asyncio.create_task(
            self._queue_loop(), name="im-gateway-queue"
        )
        self._redis_task = asyncio.create_task(
            self._redis_config_loop(), name="im-gateway-config-events"
        )

    async def close(self) -> None:
        self._started = False
        for task in (
            self._queue_task,
            self._redis_task,
            *self._run_tasks.values(),
            *self._preemption_tasks.values(),
            *self._staging_tasks,
            *self._maintenance_tasks,
            *self._hot_turn_tasks.values(),
            *self._personal_runtime_prewarm_tasks.values(),
            *self._outbound_tasks.values(),
        ):
            if task is not None:
                task.cancel()
        for task in (
            self._queue_task,
            self._redis_task,
            *self._run_tasks.values(),
            *self._preemption_tasks.values(),
            *self._staging_tasks,
            *self._maintenance_tasks,
            *self._hot_turn_tasks.values(),
            *self._personal_runtime_prewarm_tasks.values(),
            *self._outbound_tasks.values(),
        ):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        self._queue_task = None
        self._redis_task = None
        self._run_tasks.clear()
        self._preemption_tasks.clear()
        self._staging_tasks.clear()
        self._maintenance_tasks.clear()
        self._hot_turn_tasks.clear()
        self._application_identity_cache.clear()
        self._personal_runtime_prewarm_tasks.clear()
        self._staging_by_channel.clear()
        owned_at_shutdown = tuple(self.actors.values())
        for actor in owned_at_shutdown:
            try:
                await self.bridge.disconnect(actor.channel_id)
            except Exception:
                pass
        self.actors.clear()
        self._outbound_tasks.clear()
        if owned_at_shutdown:
            try:
                async with AsyncSessionLocal() as db:
                    store = SqlAlchemyImChannelLeaseStore(
                        db,
                        owner_instance_id=self.instance_id,
                        lease_ttl_seconds=(
                            settings.IM_GATEWAY_LEASE_TTL_SECONDS
                        ),
                    )
                    for actor in owned_at_shutdown:
                        await store.release(
                            channel_id=actor.channel_id,
                            lease_epoch=actor.lease_epoch,
                        )
            except Exception:
                # TTL takeover remains the correctness fallback.
                pass
        await self.bridge.stop()
        await close_runtime_channel_fast_lane_pool()

    async def notify_config_changed(
        self, channel_id: int, config_generation: int
    ) -> None:
        client = from_url(settings.REDIS_URL, decode_responses=True)
        try:
            await client.publish(
                _CONFIG_CHANNEL,
                json.dumps(
                    {
                        "channel_id": channel_id,
                        "config_generation": config_generation,
                    },
                    separators=(",", ":"),
                ),
            )
        except RedisError:
            # The five-second MySQL reconciliation loop is authoritative.
            pass
        finally:
            await client.aclose()

    async def reconcile(self) -> None:
        async with self._reconcile_lock:
            if not self._started:
                await self.start()
            elif not self.bridge.is_running:
                await self._restart_bridge()
            loop_time = asyncio.get_running_loop().time()
            if loop_time - self._last_onboarding_cleanup >= 30:
                await self._cleanup_expired_onboarding()
                self._last_onboarding_cleanup = loop_time
            if (
                loop_time - self._last_heartbeat
                >= settings.IM_GATEWAY_HEARTBEAT_INTERVAL_SECONDS
            ):
                await self._heartbeat()
                self._last_heartbeat = loop_time
            capacity = max(
                0,
                settings.IM_GATEWAY_CONNECTION_CAPACITY
                - len(self.actors),
            )
            if capacity:
                async with AsyncSessionLocal() as db:
                    await SqlAlchemyImChannelLeaseStore(
                        db,
                        owner_instance_id=self.instance_id,
                        lease_ttl_seconds=(
                            settings.IM_GATEWAY_LEASE_TTL_SECONDS
                        ),
                    ).claim_available(limit=capacity)
            async with AsyncSessionLocal() as db:
                owned = await SqlAlchemyImChannelLeaseStore(
                    db,
                    owner_instance_id=self.instance_id,
                    lease_ttl_seconds=(
                        settings.IM_GATEWAY_LEASE_TTL_SECONDS
                    ),
                ).owned_channels()
            desired = {item.channel_id: item for item in owned}
            for channel_id, current in tuple(self.actors.items()):
                target = desired.get(channel_id)
                if (
                    target is not None
                    and target.config_generation
                    == current.config_generation
                    and target.lease_epoch == current.lease_epoch
                ):
                    continue
                await self._disconnect_actor(current)
            for channel_id, target in desired.items():
                if channel_id in self.actors:
                    continue
                await self._connect_actor(target)

    async def _cleanup_expired_onboarding(self) -> None:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(ImOnboardingSession)
                    .where(
                        ImOnboardingSession.status.in_(
                            ("pending", "scanned", "credentials_ready")
                        ),
                        ImOnboardingSession.expires_at <= now,
                    )
                    .order_by(ImOnboardingSession.expires_at)
                    .limit(100)
                    .with_for_update()
                )
            ).scalars()
            changed = False
            for row in rows:
                row.status = "expired"
                row.qr_scode_encrypted = None
                row.qr_url = None
                row.credentials_encrypted = None
                row.bot_id = None
                row.poll_owner = None
                row.poll_lease_expires_at = None
                changed = True
            if changed:
                await db.commit()
            else:
                await db.rollback()

    async def _restart_bridge(self) -> None:
        """Recover all actors after an unexpected official SDK exit."""

        previous = tuple(self.actors.values())
        for actor in previous:
            try:
                await self._set_status(
                    actor,
                    "reconnecting",
                    error_code="WECOM_BRIDGE_RESTARTING",
                    error_message="企微连接进程正在自动恢复",
                )
            except Exception:
                pass
        for channel_id in tuple(
            self._personal_runtime_prewarm_tasks
        ):
            await self._cancel_personal_runtime_prewarm(channel_id)
        # The child no longer owns any of these SDK connections. Clearing the
        # local view makes the normal desired-state pass reconnect every lease
        # with the same generation and fencing epoch.
        self.actors.clear()
        await self.bridge.stop()
        await self.bridge.start()

    async def _heartbeat(self) -> None:
        epochs = {
            channel_id: actor.lease_epoch
            for channel_id, actor in self.actors.items()
        }
        if not epochs:
            return
        async with AsyncSessionLocal() as db:
            retained = await SqlAlchemyImChannelLeaseStore(
                db,
                owner_instance_id=self.instance_id,
                lease_ttl_seconds=settings.IM_GATEWAY_LEASE_TTL_SECONDS,
            ).heartbeat(channel_epochs=epochs)
        for channel_id in set(epochs) - retained:
            actor = self.actors.get(channel_id)
            if actor is not None:
                await self._disconnect_actor(actor)

    async def _connect_actor(self, actor: OwnedImChannel) -> None:
        self.actors[actor.channel_id] = actor
        try:
            # The SDK may emit connection.authenticated before connect()
            # returns. Persist the transitional state first so a late
            # "connecting" write cannot overwrite the authenticated state.
            await self._set_status(
                actor, "connecting", error_code=None
            )
            await self.bridge.connect(
                channel_id=actor.channel_id,
                generation=actor.config_generation,
                lease_epoch=actor.lease_epoch,
                bot_id=actor.bot_id,
                secret=actor.secret,
            )
        except Exception as exc:
            self.actors.pop(actor.channel_id, None)
            await self._set_status(
                actor,
                "error",
                error_code=type(exc).__name__,
                error_message="企微连接启动失败",
            )

    async def _disconnect_actor(self, actor: OwnedImChannel) -> None:
        self.actors.pop(actor.channel_id, None)
        await self._cancel_fast_lane_tasks(actor.channel_id)
        self.runtime_bridge.invalidate_channel(actor.channel_id)
        self.application_runtime_bridge.invalidate_channel(
            actor.channel_id
        )
        for key in tuple(self._application_identity_cache):
            if key[0] == actor.channel_id:
                self._application_identity_cache.pop(key, None)
        await self._cancel_personal_runtime_prewarm(actor.channel_id)
        try:
            await self.bridge.disconnect(actor.channel_id)
        except Exception:
            pass

    async def _cancel_fast_lane_tasks(self, channel_id: int) -> None:
        tasks = [
            (key, task)
            for key, task in tuple(self._hot_turn_tasks.items())
            if key[0] == channel_id
        ]
        for _key, task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(
                *(task for _key, task in tasks),
                return_exceptions=True,
            )
        for key, task in tasks:
            if self._hot_turn_tasks.get(key) is task:
                self._hot_turn_tasks.pop(key, None)

    async def _cancel_personal_runtime_prewarm(
        self,
        channel_id: int,
    ) -> None:
        prewarm_task = self._personal_runtime_prewarm_tasks.pop(
            channel_id,
            None,
        )
        if prewarm_task is not None and not prewarm_task.done():
            prewarm_task.cancel()
            await asyncio.gather(
                prewarm_task,
                return_exceptions=True,
            )

    async def _schedule_personal_runtime_prewarm(
        self,
        actor: OwnedImChannel,
    ) -> None:
        if (
            actor.target_type != "personal_agent"
            or actor.user_id is None
            or actor.runtime_workspace_id is None
        ):
            return
        current = self._personal_runtime_prewarm_tasks.get(
            actor.channel_id
        )
        if current is not None and not current.done():
            return

        async def prewarm() -> None:
            await self.runtime_bridge.activate_runtime(
                principal=Principal(
                    org_id=actor.org_id,
                    user_id=int(actor.user_id),
                ),
                runtime_workspace_id=int(
                    actor.runtime_workspace_id
                ),
            )
            logger.info(
                "Personal IM enter-chat Runtime prewarm completed "
                "channel_id=%s workspace_id=%s",
                actor.channel_id,
                actor.runtime_workspace_id,
            )

        task = asyncio.create_task(
            prewarm(),
            name=(
                "im-personal-runtime-prewarm-"
                f"{actor.channel_id}"
            ),
        )
        self._personal_runtime_prewarm_tasks[
            actor.channel_id
        ] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if (
                self._personal_runtime_prewarm_tasks.get(
                    actor.channel_id
                )
                is completed
            ):
                self._personal_runtime_prewarm_tasks.pop(
                    actor.channel_id,
                    None,
                )
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "Personal IM enter-chat Runtime prewarm failed"
                )

        task.add_done_callback(discard)

    async def _on_bridge_event(self, event: dict[str, Any]) -> None:
        channel_id = int(event.get("channel_id") or 0)
        actor = self.actors.get(channel_id)
        if actor is None or not self._event_is_current(actor, event):
            return
        event_type = str(event.get("type") or "")
        if event_type == "message":
            await self._ingest_message(actor, event.get("frame"))
            return
        if event_type == "event":
            await self._handle_provider_event(actor, event.get("frame"))
            return
        if event_type == "connection.authenticated":
            await self._set_status(actor, "connected", error_code=None)
        elif event_type == "connection.reconnecting":
            await self._set_status(
                actor, "reconnecting", error_code=None
            )
        elif event_type == "connection.disconnected":
            await self._set_status(
                actor, "reconnecting", error_code=None
            )
        elif event_type == "connection.error":
            error = event.get("error")
            code = (
                str(error.get("code"))
                if isinstance(error, dict)
                else "WECOM_SDK_ERROR"
            )
            status = (
                "error"
                if code == "WS_AUTH_FAILURE_EXHAUSTED"
                else "reconnecting"
            )
            await self._set_status(
                actor,
                status,
                error_code=code,
                error_message=(
                    "企微凭据无效"
                    if status == "error"
                    else "企微连接正在恢复"
                ),
            )

    async def _ingest_message(
        self,
        actor: OwnedImChannel,
        frame: object,
    ) -> None:
        if not isinstance(frame, dict):
            return
        provider_received_at = time.perf_counter()
        try:
            message = normalize_wecom_message(
                frame,
                allow_group=(
                    actor.target_type == "application_agent"
                    and actor.group_enabled
                ),
            )
        except WeComGroupMessageIgnored:
            self.group_rejected_count += 1
            return
        except WeComMessageRejected as exc:
            request_id = str(
                (frame.get("headers") or {}).get("req_id") or ""
            )
            if request_id:
                try:
                    await self.bridge.reply_stream(
                        channel_id=actor.channel_id,
                        generation=actor.config_generation,
                        lease_epoch=actor.lease_epoch,
                        provider_request_id=request_id,
                        stream_id=f"im-rejected-{os.urandom(6).hex()}",
                        content=f"无法处理这条企微消息：{exc}",
                        finish=True,
                        non_blocking=False,
                    )
                except Exception:
                    pass
            return
        if (
            message.chat_type == "direct"
            and not actor.direct_enabled
        ):
            return
        if (
            message.chat_type == "group"
            and (
                actor.target_type != "application_agent"
                or not actor.group_enabled
                or not message.bot_mentioned
            )
        ):
            self.group_rejected_count += 1
            return
        async with AsyncSessionLocal() as db:
            receipt = await ImInboundQueue(db).ingest(
                channel_id=actor.channel_id,
                message=message,
            )
        if receipt.dispatch_lane == "fast_lane_v2":
            # The lane worker starts only after the transaction above has
            # committed the inbound row, stable delivery identity and ticket.
            self._queue_wakeup.set()
        if not receipt.duplicate:
            _log_im_gateway_timing(
                target_type=actor.target_type,
                stage="db_committed",
                inbound_message_id=receipt.inbound_message_id,
                status="success",
                duration_ms=(
                    time.perf_counter() - provider_received_at
                )
                * 1000,
            )
            if actor.target_type == "application_agent":
                if receipt.status == "dead_letter":
                    try:
                        await self.bridge.reply_stream(
                            channel_id=actor.channel_id,
                            generation=actor.config_generation,
                            lease_epoch=actor.lease_epoch,
                            provider_request_id=message.provider_request_id,
                            stream_id=receipt.stream_id,
                            content="当前使用人数较多，请稍后再试。",
                            finish=True,
                            non_blocking=False,
                        )
                    except WeComBridgeError:
                        pass
                    return
                thinking_task = asyncio.create_task(
                    self._send_thinking(
                        actor=actor,
                        provider_request_id=message.provider_request_id,
                        stream_id=receipt.stream_id,
                        content=(
                            "正在结束上一轮，消息已保存。"
                            if receipt.has_unfinished_predecessor
                            else _WECOM_THINKING_PLACEHOLDER
                        ),
                    ),
                    name=(
                        "im-thinking-"
                        f"{receipt.inbound_message_id}"
                    ),
                )
                identity_task = asyncio.create_task(
                    self._resolve_application_identity(
                        actor=actor,
                        message=message,
                        inbound_message_id=(
                            receipt.inbound_message_id
                        ),
                        stream_id=receipt.stream_id,
                        thinking_task=thinking_task,
                    ),
                    name=(
                        "im-identity-"
                        f"{receipt.inbound_message_id}"
                    ),
                )
                self._maintenance_tasks.update(
                    (thinking_task, identity_task)
                )
                thinking_task.add_done_callback(
                    self._maintenance_tasks.discard
                )
                identity_task.add_done_callback(
                    self._maintenance_tasks.discard
                )
                return
            try:
                await self.bridge.submit_stream_state(
                    channel_id=actor.channel_id,
                    generation=actor.config_generation,
                    lease_epoch=actor.lease_epoch,
                    provider_request_id=message.provider_request_id,
                    stream_id=receipt.stream_id,
                    content=(
                        "正在接收附件…"
                        if message.media
                        else (
                            "正在结束上一轮，消息已保存。"
                            if receipt.has_unfinished_predecessor
                            else _WECOM_THINKING_PLACEHOLDER
                        )
                    ),
                )
            except WeComBridgeError:
                pass
            self._queue_wakeup.set()
        # Durable queue polling claims attachment staging. This is slightly
        # later than spawning here, but it allows another Backend replica to
        # resume a crash without losing strict per-channel FIFO.

    async def _send_thinking(
        self,
        *,
        actor: OwnedImChannel,
        provider_request_id: str,
        stream_id: str,
        content: str = _WECOM_THINKING_PLACEHOLDER,
    ) -> None:
        try:
            await self.bridge.reply_stream(
                channel_id=actor.channel_id,
                generation=actor.config_generation,
                lease_epoch=actor.lease_epoch,
                provider_request_id=provider_request_id,
                stream_id=stream_id,
                content=content,
                finish=False,
                non_blocking=True,
            )
        except WeComBridgeError:
            pass

    async def _resolve_application_identity(
        self,
        *,
        actor: OwnedImChannel,
        message,
        inbound_message_id: int,
        stream_id: str,
        thinking_task: asyncio.Task[None],
    ) -> None:
        try:
            async with AsyncSessionLocal() as db:
                identities = OrganizationWeComIdentityService(db)
                resolved = await identities.resolve(
                    org_id=actor.org_id,
                    member_id=message.sender_member_id,
                )
                if resolved is None:
                    binding_url = identities.shared_binding_url(
                        member_id=message.sender_member_id,
                    )
                    await ImInboundQueue(
                        db
                    ).mark_identity_required(
                        inbound_message_id=inbound_message_id,
                    )
                    # Preserve one stream's provider ordering: the immediate
                    # thinking frame must be accepted before a terminal
                    # identity link is sent.
                    await asyncio.gather(
                        thinking_task,
                        return_exceptions=True,
                    )
                    await self.bridge.reply_stream(
                        channel_id=actor.channel_id,
                        generation=actor.config_generation,
                        lease_epoch=actor.lease_epoch,
                        provider_request_id=(
                            message.provider_request_id
                        ),
                        stream_id=stream_id,
                        content=(
                            "请先完成企业身份绑定，再重新发送问题："
                            f"\n{binding_url}"
                        ),
                        finish=True,
                        non_blocking=True,
                    )
                    return
                self._application_identity_cache[
                    (
                        actor.channel_id,
                        message.external_conversation_id,
                        message.sender_member_id,
                    )
                ] = (
                    int(resolved.principal.user_id),
                    actor.config_generation,
                    actor.lease_epoch,
                    time.monotonic() + 120.0,
                )
                status = await ImInboundQueue(
                    db
                ).mark_identity_resolved(
                    inbound_message_id=inbound_message_id,
                    platform_user_id=(
                        resolved.principal.user_id
                    ),
                )
            if status not in {"queued", "staging_media"}:
                return
            # The thinking task was scheduled before identity resolution and
            # therefore submits first to the local bridge.  Never wait for
            # its provider ACK here: WeCom network latency must not sit in
            # front of Runtime startup.  The SDK serializes frames by req_id
            # and skips stale non-terminal intermediate frames.
            self._queue_wakeup.set()
            if resolved.identity_id is not None:
                task = asyncio.create_task(
                    self._record_application_access(
                        actor=actor,
                        identity_id=resolved.identity_id,
                        chat_type=message.chat_type,
                        external_conversation_id=(
                            message.external_conversation_id
                        ),
                    ),
                    name=f"im-access-{inbound_message_id}",
                )
                self._maintenance_tasks.add(task)
                task.add_done_callback(
                    self._maintenance_tasks.discard
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            async with AsyncSessionLocal() as db:
                await ImInboundQueue(db).mark_identity_required(
                    inbound_message_id=inbound_message_id,
                )
            try:
                await asyncio.gather(
                    thinking_task,
                    return_exceptions=True,
                )
                await self.bridge.reply_stream(
                    channel_id=actor.channel_id,
                    generation=actor.config_generation,
                    lease_epoch=actor.lease_epoch,
                    provider_request_id=message.provider_request_id,
                    stream_id=stream_id,
                    content="企业身份校验暂时失败，请稍后重新发送。",
                    finish=True,
                    non_blocking=True,
                )
            except Exception:
                pass

    def _prepared_hot_route(
        self,
        actor: OwnedImChannel,
        message: Any,
    ) -> PreparedWeComFastLaneRoute | None:
        # Active execution is intentionally not a blocker: Runtime decides
        # whether the already-durable text becomes a redirect or an ordinary
        # FIFO Run.  Ticket ordering is enforced before this route is used.
        if actor.target_type == "personal_agent":
            return self.runtime_bridge.hot_route(
                channel_id=actor.channel_id,
                gateway_instance_id=self.instance_id,
                channel_lease_epoch=actor.lease_epoch,
                channel_generation=actor.config_generation,
            )
        cached_identity = self._application_identity_cache.get(
            (
                actor.channel_id,
                message.external_conversation_id,
                message.sender_member_id,
            )
        )
        if cached_identity is None:
            return None
        user_id, generation, epoch, expires_at = cached_identity
        if (
            generation != actor.config_generation
            or epoch != actor.lease_epoch
            or expires_at <= time.monotonic()
        ):
            return None
        return self.application_runtime_bridge.hot_route(
            channel_id=actor.channel_id,
            external_conversation_id=message.external_conversation_id,
            platform_user_id=user_id,
            gateway_instance_id=self.instance_id,
            channel_lease_epoch=actor.lease_epoch,
            channel_generation=actor.config_generation,
        )

    async def _execute_fast_lane_v2(
        self,
        *,
        actor: OwnedImChannel,
        queued: QueuedFastLaneInbound,
    ) -> None:
        started = time.perf_counter()
        route: PreparedWeComFastLaneRoute | None
        if queued.target_type == "personal_agent":
            route = self.runtime_bridge.hot_route(
                channel_id=actor.channel_id,
                gateway_instance_id=self.instance_id,
                channel_lease_epoch=actor.lease_epoch,
                channel_generation=actor.config_generation,
            )
            if route is None:
                try:
                    route = (
                        await self.runtime_bridge.prepare_fast_lane_route(
                            channel_id=actor.channel_id,
                            gateway_instance_id=self.instance_id,
                            channel_lease_epoch=actor.lease_epoch,
                            channel_generation=actor.config_generation,
                        )
                    )
                except WeComFastLaneRouteRejected as exc:
                    await self._reject_fast_lane_route(
                        actor=actor,
                        queued=queued,
                        rejection=exc,
                    )
                    return
        elif queued.sender_platform_user_id is not None:
            if (
                queued.runtime_disposition_seq is not None
                and queued.runtime_canonical_run_id
                and queued.runtime_start_after_seq is not None
            ):
                route = await self.application_runtime_bridge.prepare_fast_lane_recovery_route(
                    inbound_message_id=queued.inbound_message_id,
                    channel_id=actor.channel_id,
                    external_conversation_id=(
                        queued.external_conversation_id
                    ),
                    platform_user_id=queued.sender_platform_user_id,
                    gateway_instance_id=self.instance_id,
                    channel_lease_epoch=actor.lease_epoch,
                    channel_generation=actor.config_generation,
                    runtime_canonical_run_id=(
                        queued.runtime_canonical_run_id
                    ),
                )
            else:
                route = await self.application_runtime_bridge.prepare_fast_lane_route(
                    inbound_message_id=queued.inbound_message_id,
                    channel_id=actor.channel_id,
                    external_conversation_id=(
                        queued.external_conversation_id
                    ),
                    platform_user_id=queued.sender_platform_user_id,
                    gateway_instance_id=self.instance_id,
                    channel_lease_epoch=actor.lease_epoch,
                    channel_generation=actor.config_generation,
                )
        else:
            route = None
        if route is None:
            async with AsyncSessionLocal() as db:
                queue = ImInboundQueue(db)
                if queued.target_type == "application_agent":
                    retry_status = await queue.mark_fast_lane_retry_wait(
                        inbound_message_id=queued.inbound_message_id,
                        owner_instance_id=queued.owner_instance_id,
                        lease_epoch=queued.lease_epoch,
                        error_code="FAST_LANE_ROUTE_UNAVAILABLE",
                        error_message=(
                            "current application Runtime route is not ready"
                        ),
                    )
                    if retry_status == "failed":
                        try:
                            await self.bridge.reply_stream(
                                channel_id=queued.channel_id,
                                generation=actor.config_generation,
                                lease_epoch=actor.lease_epoch,
                                provider_request_id=queued.provider_request_id,
                                stream_id=queued.provider_stream_id,
                                content=(
                                    "应用 Agent 当前不可用，请管理员重新发布后重试。"
                                ),
                                finish=True,
                                non_blocking=False,
                            )
                        except WeComBridgeError:
                            pass
                else:
                    await queue.mark_fast_lane_retry_wait(
                        inbound_message_id=queued.inbound_message_id,
                        owner_instance_id=queued.owner_instance_id,
                        lease_epoch=queued.lease_epoch,
                        error_code="FAST_LANE_ROUTE_UNAVAILABLE",
                        error_message=(
                            "durable Fast Lane route is not warm yet"
                        ),
                    )
            return

        normalized = dict(queued.normalized_content or {})
        message = NormalizedWeComMessage(
            external_message_id=queued.external_message_id,
            provider_request_id=queued.provider_request_id,
            external_conversation_id=queued.external_conversation_id,
            message_type=queued.message_type,
            text=str(normalized.get("text") or ""),
            quote_payload=queued.quote_payload,
            chat_type=str(normalized.get("chat_type") or "direct"),
            provider_created_at=(
                int(normalized["provider_created_at"])
                if normalized.get("provider_created_at") is not None
                else None
            ),
        )

        async def runtime_durable(disposition: Any) -> None:
            async with AsyncSessionLocal() as db:
                persisted = await ImInboundQueue(
                    db
                ).mark_fast_lane_runtime_durable(
                    inbound_message_id=queued.inbound_message_id,
                    owner_instance_id=queued.owner_instance_id,
                    lease_epoch=queued.lease_epoch,
                    disposition_seq=disposition.disposition_seq,
                    disposition_state=disposition.state,
                    canonical_run_id=disposition.canonical_run_id,
                    parent_run_id=disposition.parent_run_id,
                    start_after_seq=disposition.start_after_seq,
                )
            if not persisted:
                raise RuntimeError(
                    "Fast Lane ticket ownership was lost before disposition"
                )

        try:
            result = await execute_durable_wecom_fast_lane_v2(
                node_bridge=self.bridge,
                route=route,
                message=message,
                inbound_message_id=queued.inbound_message_id,
                delivery_id=queued.delivery_id,
                client_message_id=queued.client_message_id,
                provider_stream_id=queued.provider_stream_id,
                runtime_durable_sink=runtime_durable,
                runtime_disposition_seq=queued.runtime_disposition_seq,
                runtime_canonical_run_id=queued.runtime_canonical_run_id,
                runtime_start_after_seq=queued.runtime_start_after_seq,
            )
            if result.artifacts:
                try:
                    await WeComFastLaneArtifactDelivery(
                        node_bridge=self.bridge,
                        dashboard=(
                            self.application_runtime_bridge.dashboard
                            if queued.target_type == "application_agent"
                            else self.runtime_bridge.dashboard
                        ),
                    ).deliver(
                        inbound_message_id=queued.inbound_message_id,
                        route=route,
                        message=message,
                        artifacts=result.artifacts,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "WeCom Fast Lane artifact follow-up failed after "
                        "answer delivery",
                        extra={
                            "inbound_message_id": queued.inbound_message_id
                        },
                        exc_info=True,
                    )
            async with AsyncSessionLocal() as db:
                await ImInboundQueue(db).mark_completed(
                    inbound_message_id=queued.inbound_message_id,
                    runtime_run_row_id=None,
                    owner_instance_id=queued.owner_instance_id,
                    lease_epoch=queued.lease_epoch,
                )
            _log_im_gateway_timing(
                target_type=actor.target_type,
                stage="fast_lane_completed",
                inbound_message_id=queued.inbound_message_id,
                status="success",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except asyncio.CancelledError:
            async with AsyncSessionLocal() as db:
                await ImInboundQueue(db).mark_fast_lane_retry_wait(
                    inbound_message_id=queued.inbound_message_id,
                    owner_instance_id=queued.owner_instance_id,
                    lease_epoch=queued.lease_epoch,
                    error_code="FAST_LANE_TASK_CANCELLED",
                    error_message=(
                        "Fast Lane task canceled before delivery completed"
                    ),
                )
            raise
        except (WeComFastLaneColdPath, Exception) as exc:
            async with AsyncSessionLocal() as db:
                await ImInboundQueue(db).mark_fast_lane_retry_wait(
                    inbound_message_id=queued.inbound_message_id,
                    owner_instance_id=queued.owner_instance_id,
                    lease_epoch=queued.lease_epoch,
                    error_code=type(exc).__name__,
                    error_message=str(exc) or "Fast Lane v2 retry required",
                )
            if not isinstance(exc, WeComFastLaneColdPath):
                logger.exception(
                    "WeCom Fast Lane v2 delivery failed",
                    extra={"delivery_id": queued.delivery_id},
                )

    async def _reject_fast_lane_route(
        self,
        *,
        actor: OwnedImChannel,
        queued: QueuedFastLaneInbound,
        rejection: WeComFastLaneRouteRejected,
    ) -> None:
        async with AsyncSessionLocal() as db:
            persisted = await ImInboundQueue(
                db
            ).mark_fast_lane_validation_rejected(
                inbound_message_id=queued.inbound_message_id,
                owner_instance_id=queued.owner_instance_id,
                lease_epoch=queued.lease_epoch,
                error_code=rejection.error_code,
                error_message=rejection.user_message,
            )
        if not persisted:
            return
        try:
            await self.bridge.reply_stream(
                channel_id=actor.channel_id,
                generation=actor.config_generation,
                lease_epoch=actor.lease_epoch,
                provider_request_id=queued.provider_request_id,
                stream_id=queued.provider_stream_id,
                content=rejection.user_message,
                finish=True,
                non_blocking=True,
            )
        except Exception:
            logger.warning(
                "WeCom Fast Lane validation rejection reply failed",
                extra={
                    "inbound_message_id": queued.inbound_message_id,
                    "error_code": rejection.error_code,
                },
                exc_info=True,
            )

    async def _project_hot_message(
        self,
        *,
        actor: OwnedImChannel,
        message: Any,
        route: PreparedWeComFastLaneRoute,
        result: PreparedWeComFastLaneResult,
    ) -> None:
        """Asynchronously backfill the Backend management/audit projection."""

        now = datetime.now(timezone.utc)
        inbound_message_id: int | None = None
        try:
            async with AsyncSessionLocal() as db:
                existing = await db.scalar(
                    select(ImInboundMessage).where(
                        ImInboundMessage.provider == "wecom",
                        ImInboundMessage.channel_id == actor.channel_id,
                        ImInboundMessage.external_message_id
                        == message.external_message_id,
                    )
                )
                if existing is not None:
                    inbound_message_id = int(existing.id)
                    checkpoint = await db.scalar(
                        select(ImDeliveryCheckpoint).where(
                            ImDeliveryCheckpoint.inbound_message_id
                            == inbound_message_id
                        )
                    )
                    if checkpoint is None:
                        db.add(
                            ImDeliveryCheckpoint(
                                inbound_message_id=inbound_message_id,
                                provider_request_id=(
                                    message.provider_request_id
                                ),
                                provider_stream_id=(
                                    result.provider_stream_id
                                ),
                                last_runtime_event_seq=(
                                    result.highest_contiguous_seq
                                ),
                                delivery_mode="passive_stream",
                                status="completed",
                                final_text=result.final_text,
                                provider_delivered_text=(
                                    result.final_text
                                ),
                                last_delivered_at=now,
                            )
                        )
                        await db.commit()
                else:
                    row = ImInboundMessage(
                        channel_id=actor.channel_id,
                        provider="wecom",
                        target_type=actor.target_type,
                        application_agent_id=(
                            route.target_id
                            if actor.target_type == "application_agent"
                            else None
                        ),
                        external_message_id=message.external_message_id,
                        external_conversation_id=(
                            message.external_conversation_id
                        ),
                        chat_type=message.chat_type,
                        sender_platform_user_id=route.identity.user_id,
                        identity_status=(
                            "resolved"
                            if actor.target_type == "application_agent"
                            else "not_required"
                        ),
                        provider_request_id=message.provider_request_id,
                        message_type=message.message_type,
                        normalized_content=message.normalized_content,
                        quote_payload=message.quote_payload,
                        status="completed",
                        attempt_count=0,
                        capacity_wait_attempt_count=0,
                        received_at=now,
                        db_committed_at=now,
                        scheduler_claimed_at=now,
                        identity_resolved_at=now,
                        runtime_submitted_at=now,
                        first_runtime_public_event_at=now,
                        first_provider_dispatch_at=now,
                        first_provider_confirmed_at=now,
                        started_at=now,
                        finished_at=now,
                    )
                    db.add(row)
                    await db.flush()
                    inbound_message_id = int(row.id)
                    db.add(
                        ImDeliveryCheckpoint(
                            inbound_message_id=inbound_message_id,
                            provider_request_id=message.provider_request_id,
                            provider_stream_id=result.provider_stream_id,
                            last_runtime_event_seq=(
                                result.highest_contiguous_seq
                            ),
                            delivery_mode="passive_stream",
                            status="completed",
                            final_text=result.final_text,
                            provider_delivered_text=result.final_text,
                            last_delivered_at=now,
                        )
                    )
                channel = await db.get(ImChannel, actor.channel_id)
                if channel is not None:
                    channel.last_message_at = now
                await db.commit()
        except IntegrityError:
            # A duplicate Provider delivery or concurrent projection won the
            # unique provider-message key. Re-read it so its current-message
            # artifact checkpoint can still recover without model replay.
            async with AsyncSessionLocal() as db:
                existing = await db.scalar(
                    select(ImInboundMessage).where(
                        ImInboundMessage.provider == "wecom",
                        ImInboundMessage.channel_id == actor.channel_id,
                        ImInboundMessage.external_message_id
                        == message.external_message_id,
                    )
                )
                if existing is not None:
                    inbound_message_id = int(existing.id)
                    checkpoint = await db.scalar(
                        select(ImDeliveryCheckpoint).where(
                            ImDeliveryCheckpoint.inbound_message_id
                            == inbound_message_id
                        )
                    )
                    if checkpoint is None:
                        db.add(
                            ImDeliveryCheckpoint(
                                inbound_message_id=inbound_message_id,
                                provider_request_id=(
                                    message.provider_request_id
                                ),
                                provider_stream_id=(
                                    result.provider_stream_id
                                ),
                                last_runtime_event_seq=(
                                    result.highest_contiguous_seq
                                ),
                                delivery_mode="passive_stream",
                                status="completed",
                                final_text=result.final_text,
                                provider_delivered_text=(
                                    result.final_text
                                ),
                                last_delivered_at=now,
                            )
                        )
                        await db.commit()
        except Exception:
            logger.exception(
                "WeCom Fast Lane management projection failed"
            )
            return
        if inbound_message_id is None or not result.artifacts:
            return
        try:
            await WeComFastLaneArtifactDelivery(
                node_bridge=self.bridge,
                dashboard=(
                    self.application_runtime_bridge.dashboard
                    if actor.target_type == "application_agent"
                    else self.runtime_bridge.dashboard
                ),
            ).deliver(
                route=route,
                message=message,
                inbound_message_id=inbound_message_id,
                artifacts=result.artifacts,
            )
        except Exception:
            logger.exception(
                "WeCom Fast Lane artifact delivery failed",
                extra={
                    "channel_id": actor.channel_id,
                    "target_type": actor.target_type,
                    "artifact_count": len(result.artifacts),
                },
            )

    @staticmethod
    async def _record_application_access(
        *,
        actor: OwnedImChannel,
        identity_id: int,
        chat_type: str,
        external_conversation_id: str,
    ) -> None:
        try:
            async with AsyncSessionLocal() as db:
                channel = await db.get(ImChannel, actor.channel_id)
                if channel is None:
                    return
                await OrganizationWeComIdentityService(
                    db
                ).record_access(
                    channel=channel,
                    identity_id=identity_id,
                    chat_type=chat_type,
                    external_conversation_id=(
                        external_conversation_id
                    ),
                )
        except Exception:
            # This table is a UI projection and never gates a user message.
            return

    async def _stage_message(
        self, actor: OwnedImChannel, inbound_message_id: int
    ) -> None:
        success = await self.runtime_bridge.stage_attachments(
            queued_channel=actor,
            inbound_message_id=inbound_message_id,
            owner_instance_id=self.instance_id,
        )
        if success in {"queued", "stale"}:
            return
        async with AsyncSessionLocal() as db:
            from app.models import ImDeliveryCheckpoint

            checkpoint = await db.scalar(
                select(ImDeliveryCheckpoint).where(
                    ImDeliveryCheckpoint.inbound_message_id
                    == inbound_message_id
                )
            )
        if checkpoint is None:
            return
        try:
            await self.bridge.reply_stream(
                channel_id=actor.channel_id,
                generation=actor.config_generation,
                lease_epoch=actor.lease_epoch,
                provider_request_id=str(
                    checkpoint.provider_request_id or ""
                ),
                stream_id=str(checkpoint.provider_stream_id or ""),
                content=(
                    "企微附件接收暂时失败，系统正在自动重试。"
                    if success == "staging_media"
                    else "企微附件接收多次失败，本轮未提交给 Agent。"
                ),
                finish=success == "dead_letter",
                non_blocking=False,
            )
        except Exception:
            pass

    async def _handle_provider_event(
        self, actor: OwnedImChannel, frame: object
    ) -> None:
        if not isinstance(frame, dict):
            return
        body = frame.get("body")
        headers = frame.get("headers")
        if not isinstance(body, dict) or not isinstance(headers, dict):
            return
        if str(body.get("chattype") or "single") == "group":
            self.group_rejected_count += 1
            return
        provider_event = body.get("event")
        event_type = (
            str(provider_event.get("eventtype") or "")
            if isinstance(provider_event, dict)
            else ""
        )
        if event_type != "enter_chat":
            if event_type == "template_card_event":
                self.template_card_event_count += 1
                event_key = str(
                    provider_event.get("event_key") or ""
                ).strip()
                task_id = str(
                    provider_event.get("task_id") or ""
                ).strip()
                details = "；".join(
                    item
                    for item in (
                        f"操作：{event_key}" if event_key else "",
                        f"任务：{task_id}" if task_id else "",
                    )
                    if item
                )
                await self._ingest_message(
                    actor,
                    {
                        "headers": headers,
                        "body": {
                            "msgid": body.get("msgid"),
                            "chattype": body.get(
                                "chattype", "single"
                            ),
                            "from": body.get("from"),
                            "create_time": body.get("create_time"),
                            "msgtype": "text",
                            "text": {
                                "content": (
                                    "[企微模板卡片操作]"
                                    + (f" {details}" if details else "")
                                )
                            },
                        },
                    },
                )
            elif event_type == "feedback_event":
                # The SDK currently exposes no feedback value or comment.
                # Recognize and count it without inventing an Agent prompt.
                self.feedback_event_count += 1
            elif event_type == "disconnected_event":
                await self._set_status(
                    actor,
                    "reconnecting",
                    error_code="WECOM_CONNECTION_REPLACED",
                    error_message="企微连接正在恢复",
                )
            return
        if actor.target_type == "personal_agent":
            await self._schedule_personal_runtime_prewarm(actor)
        try:
            await self.bridge.reply_welcome(
                channel_id=actor.channel_id,
                generation=actor.config_generation,
                lease_epoch=actor.lease_epoch,
                provider_request_id=str(
                    headers.get("req_id") or ""
                ),
                body={
                    "msgtype": "text",
                    "text": {
                        "content": (
                            "您好，我是企业应用 Agent。直接发送消息即可开始。"
                            if actor.target_type == "application_agent"
                            else "您好，我是您的个人 Agent。直接发送消息即可开始。"
                        )
                    },
                },
            )
        except Exception:
            pass

    async def _queue_loop(self) -> None:
        while self._started:
            self._queue_wakeup.clear()
            for channel_id, actor in tuple(self.actors.items()):
                self._discard_finished_channel_tasks(channel_id)
                if actor.target_type == "application_agent":
                    await self._schedule_application_channel(actor)
                else:
                    await self._schedule_personal_channel(actor)
            try:
                await asyncio.wait_for(
                    self._queue_wakeup.wait(),
                    timeout=settings.IM_GATEWAY_QUEUE_POLL_SECONDS,
                )
            except asyncio.TimeoutError:
                # Recovery scan for restarts, lost in-process signals and
                # another replica taking over a lease.
                pass

    def _discard_finished_channel_tasks(
        self, channel_id: int
    ) -> None:
        for mapping in (
            self._run_tasks,
            self._staging_by_channel,
        ):
            for key, task in tuple(mapping.items()):
                if key[0] == channel_id and task.done():
                    mapping.pop(key, None)
        preemption = self._preemption_tasks.get(channel_id)
        if preemption is not None and preemption.done():
            self._preemption_tasks.pop(channel_id, None)
        outbound = self._outbound_tasks.get(channel_id)
        if outbound is not None and outbound.done():
            self._outbound_tasks.pop(channel_id, None)

    def _ensure_preemption_task(self, actor: OwnedImChannel) -> bool:
        current = self._preemption_tasks.get(actor.channel_id)
        if current is not None and not current.done():
            return True

        async def run() -> None:
            try:
                await self.preemption_coordinator.preempt_next(
                    channel_id=actor.channel_id,
                    owner_instance_id=self.instance_id,
                    lease_epoch=actor.lease_epoch,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "WeCom preemption coordinator failed channel_id=%s",
                    actor.channel_id,
                )
            finally:
                self._queue_wakeup.set()

        task = asyncio.create_task(
            run(), name=f"im-preempt-{actor.channel_id}"
        )
        self._preemption_tasks[actor.channel_id] = task
        return True

    async def _schedule_fast_lane_v2(
        self,
        actor: OwnedImChannel,
        *,
        max_inflight: int,
    ) -> int:
        active = sum(
            1
            for (channel_id, _delivery_id), task in (
                self._hot_turn_tasks.items()
            )
            if channel_id == actor.channel_id and not task.done()
        )
        while active < max(1, max_inflight):
            active_delivery_ids = {
                delivery_id
                for (channel_id, delivery_id), task in (
                    self._hot_turn_tasks.items()
                )
                if channel_id == actor.channel_id and not task.done()
            }
            try:
                async with AsyncSessionLocal() as db:
                    queued = await ImInboundQueue(
                        db
                    ).claim_next_fast_lane(
                        channel_id=actor.channel_id,
                        owner_instance_id=self.instance_id,
                        lease_epoch=actor.lease_epoch,
                        active_delivery_ids=active_delivery_ids,
                    )
            except Exception:
                queued = None
            if queued is None:
                break
            key = (actor.channel_id, queued.delivery_id)
            existing = self._hot_turn_tasks.get(key)
            if existing is not None and not existing.done():
                break
            if existing is not None:
                self._hot_turn_tasks.pop(key, None)
            task = asyncio.create_task(
                self._execute_fast_lane_v2(
                    actor=actor,
                    queued=queued,
                ),
                name=f"im-fast-lane-v2-{queued.delivery_id[:32]}",
            )
            self._hot_turn_tasks[key] = task

            def discard(
                completed: asyncio.Task[None],
                *,
                task_key: tuple[int, str] = key,
            ) -> None:
                if self._hot_turn_tasks.get(task_key) is completed:
                    self._hot_turn_tasks.pop(task_key, None)
                try:
                    completed.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception(
                        "WeCom durable Fast Lane task failed"
                    )
                self._queue_wakeup.set()

            task.add_done_callback(discard)
            active += 1
        return active

    async def _schedule_personal_channel(
        self, actor: OwnedImChannel
    ) -> None:
        self._schedule_outbound_delivery(actor)
        await self._schedule_fast_lane_v2(actor, max_inflight=1)
        key = (actor.channel_id, "__personal__")
        staging_task = self._staging_by_channel.get(key)
        if staging_task is not None and not staging_task.done():
            return
        try:
            async with AsyncSessionLocal() as db:
                staging_message_id = await ImInboundQueue(
                    db
                ).claim_staging(
                    channel_id=actor.channel_id,
                    owner_instance_id=self.instance_id,
                    lease_epoch=actor.lease_epoch,
                )
        except Exception:
            staging_message_id = None
        if staging_message_id is not None:
            self._start_staging_task(
                key=key,
                actor=actor,
                inbound_message_id=staging_message_id,
            )
            return
        current = self._run_tasks.get(key)
        if current is not None and not current.done():
            self._ensure_preemption_task(actor)
            return
        try:
            async with AsyncSessionLocal() as db:
                queued = await ImInboundQueue(db).claim_next(
                    channel_id=actor.channel_id,
                    owner_instance_id=self.instance_id,
                    lease_epoch=actor.lease_epoch,
                )
        except Exception:
            queued = None
        if queued is not None:
            self._start_run_task(key=key, queued=queued)

    def _schedule_outbound_delivery(self, actor: OwnedImChannel) -> None:
        current = self._outbound_tasks.get(actor.channel_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._deliver_outbound(actor),
            name=f"im-outbound-{actor.channel_id}",
        )
        self._outbound_tasks[actor.channel_id] = task

        def discard(completed: asyncio.Task[None]) -> None:
            if self._outbound_tasks.get(actor.channel_id) is completed:
                self._outbound_tasks.pop(actor.channel_id, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "WeCom proactive delivery worker failed channel_id=%s",
                    actor.channel_id,
                )

        task.add_done_callback(discard)

    async def _deliver_outbound(self, actor: OwnedImChannel) -> None:
        async with AsyncSessionLocal() as db:
            store = ImOutboundDeliveryStore(db)
            claimed = await store.claim_next(
                channel_id=actor.channel_id,
                owner_instance_id=self.instance_id,
                lease_epoch=actor.lease_epoch,
            )
        if claimed is None:
            return
        try:
            delivery_text = claimed.content or "定时任务已执行完成。"
            delivered_text = claimed.provider_delivered_text or ""
            if not delivery_text.startswith(delivered_text):
                delivered_text = ""
            for chunk in _semantic_chunks(
                delivery_text[len(delivered_text) :],
                _ACTIVE_MARKDOWN_LIMIT_BYTES,
            ):
                await self.bridge.send_markdown(
                    channel_id=actor.channel_id,
                    generation=actor.config_generation,
                    lease_epoch=actor.lease_epoch,
                    chat_id=claimed.target,
                    content=chunk,
                )
                delivered_text += chunk
                async with AsyncSessionLocal() as db:
                    await ImOutboundDeliveryStore(
                        db
                    ).mark_text_delivered(
                        claimed,
                        delivered_text=delivered_text,
                    )
            async with AsyncSessionLocal() as db:
                artifacts = await ImOutboundDeliveryStore(db).artifacts(
                    claimed
                )
            if artifacts:
                endpoint = await self.runtime_bridge.activate_runtime(
                    principal=Principal(
                        org_id=claimed.org_id,
                        user_id=claimed.user_id,
                    ),
                    runtime_workspace_id=claimed.runtime_workspace_id,
                )
                for artifact in artifacts:
                    if self.bridge.temp_root is None:
                        raise RuntimeError(
                            "WeCom bridge temp root is unavailable"
                        )
                    filename = _safe_filename(
                        Path(artifact.relative_path).name
                    )
                    target = self.bridge.require_scoped_path(
                        str(
                            self.bridge.temp_root
                            / f"scheduled-out-{uuid4().hex}-{filename}"
                        )
                    )
                    try:
                        with target.open("wb") as destination:
                            async for chunk in (
                                self.runtime_bridge.dashboard.stream_managed_artifact(
                                    endpoint,
                                    relative_path=artifact.relative_path,
                                )
                            ):
                                destination.write(chunk)
                        try:
                            await self.bridge.upload_media(
                                passive=False,
                                channel_id=actor.channel_id,
                                generation=actor.config_generation,
                                lease_epoch=actor.lease_epoch,
                                media_type=_outbound_media_type(
                                    artifact.mime_type, filename
                                ),
                                source_path=target,
                                filename=filename,
                                chat_id=claimed.target,
                            )
                        except WeComBridgeError as exc:
                            if exc.code != "MEDIA_TOO_LARGE":
                                raise
                            await self.bridge.send_markdown(
                                channel_id=actor.channel_id,
                                generation=actor.config_generation,
                                lease_epoch=actor.lease_epoch,
                                chat_id=claimed.target,
                                content=(
                                    f"附件 **{filename}** 超出企微发送上限，"
                                    "请在 WebUI 中下载。"
                                ),
                            )
                        async with AsyncSessionLocal() as db:
                            await ImOutboundDeliveryStore(
                                db
                            ).mark_artifact_delivered(
                                claimed,
                                artifact_id=artifact.artifact_id,
                            )
                    finally:
                        target.unlink(missing_ok=True)
            async with AsyncSessionLocal() as db:
                await ImOutboundDeliveryStore(db).mark_delivered(claimed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with AsyncSessionLocal() as db:
                await ImOutboundDeliveryStore(db).mark_retry(
                    claimed,
                    error_code=(
                        str(getattr(exc, "code", ""))
                        or type(exc).__name__
                    ),
                    error_message=str(exc) or type(exc).__name__,
                )

    async def _schedule_application_channel(
        self, actor: OwnedImChannel
    ) -> None:
        max_inflight = max(1, actor.max_inflight_runs)
        await self._schedule_fast_lane_v2(
            actor,
            max_inflight=max_inflight,
        )
        active_staging = sum(
            1
            for (channel_id, _key), task in (
                self._staging_by_channel.items()
            )
            if channel_id == actor.channel_id and not task.done()
        )
        while active_staging < max_inflight:
            try:
                async with AsyncSessionLocal() as db:
                    inbound_id = await ImInboundQueue(
                        db
                    ).claim_staging_application(
                        channel_id=actor.channel_id,
                        owner_instance_id=self.instance_id,
                        lease_epoch=actor.lease_epoch,
                    )
            except Exception:
                inbound_id = None
            if inbound_id is None:
                break
            key = (actor.channel_id, f"media:{inbound_id}")
            self._start_staging_task(
                key=key,
                actor=actor,
                inbound_message_id=inbound_id,
            )
            active_staging += 1

        active_runs = sum(
            1
            for (channel_id, _key), task in self._run_tasks.items()
            if channel_id == actor.channel_id and not task.done()
        )
        if active_runs > 0:
            self._ensure_preemption_task(actor)
        while active_runs < max_inflight:
            try:
                async with AsyncSessionLocal() as db:
                    queued = await ImInboundQueue(
                        db
                    ).claim_next_application(
                        channel_id=actor.channel_id,
                        owner_instance_id=self.instance_id,
                        lease_epoch=actor.lease_epoch,
                    )
            except Exception:
                queued = None
            if queued is None:
                break
            ordering = queued.ordering_key or (
                f"message:{queued.inbound_message_id}"
            )
            key = (actor.channel_id, ordering)
            if key in self._run_tasks:
                break
            self._start_run_task(key=key, queued=queued)
            active_runs += 1

    def _start_staging_task(
        self,
        *,
        key: tuple[int, str],
        actor: OwnedImChannel,
        inbound_message_id: int,
    ) -> None:
        task = asyncio.create_task(
            self._stage_message(actor, inbound_message_id),
            name=f"im-stage-{inbound_message_id}",
        )
        self._staging_by_channel[key] = task
        self._staging_tasks.add(task)
        task.add_done_callback(self._staging_tasks.discard)
        task.add_done_callback(
            lambda _task: self._queue_wakeup.set()
        )

    def _start_run_task(
        self,
        *,
        key: tuple[int, str],
        queued: QueuedInbound,
    ) -> None:
        task = asyncio.create_task(
            self._execute_queued(queued),
            name=f"im-run-{queued.inbound_message_id}",
        )
        self._run_tasks[key] = task

        def finish(completed: asyncio.Task[None]) -> None:
            if self._run_tasks.get(key) is completed:
                self._run_tasks.pop(key, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "WeCom durable ordinary task failed",
                    extra={
                        "channel_id": queued.channel_id,
                        "inbound_message_id": queued.inbound_message_id,
                    },
                )
            self._queue_wakeup.set()

        task.add_done_callback(finish)

    async def _execute_queued(self, queued: QueuedInbound) -> None:
        try:
            for member in queued.batch_messages[:-1]:
                try:
                    delivered = await self.bridge.reply_stream(
                        channel_id=queued.channel_id,
                        generation=queued.generation,
                        lease_epoch=queued.lease_epoch,
                        provider_request_id=member.provider_request_id,
                        stream_id=member.stream_id,
                        content="已接入后续消息，将统一处理。",
                        finish=True,
                        non_blocking=False,
                    )
                    if delivered:
                        async with AsyncSessionLocal() as db:
                            await ImInboundQueue(
                                db
                            ).mark_batched_delivered(
                                inbound_message_id=(
                                    member.inbound_message_id
                                ),
                                owner_instance_id=(
                                    queued.owner_instance_id
                                ),
                                lease_epoch=queued.lease_epoch,
                            )
                except Exception:
                    # Provider delivery is recoverable independently and must
                    # never hold the execution slot.
                    pass
            if (
                queued.target_type == "personal_agent"
                and queued.runtime_run_row_id is None
            ):
                try:
                    # Full stream payloads replace the prior provider state.
                    # This is idempotent for an immediately accepted message
                    # and changes queued/media status into WeCom's native
                    # dynamic thinking card before Runtime startup. Existing
                    # Runtime runs keep their recovered projected content.
                    await self.bridge.submit_stream_state(
                        channel_id=queued.channel_id,
                        generation=queued.generation,
                        lease_epoch=queued.lease_epoch,
                        provider_request_id=(
                            queued.provider_request_id
                        ),
                        stream_id=queued.stream_id,
                        content=_WECOM_THINKING_PLACEHOLDER,
                    )
                except WeComBridgeError:
                    # A local state submission failure must not prevent the
                    # durable Runtime run from starting.
                    pass
            if queued.target_type == "application_agent":
                run_row_id, run_status = (
                    await self.application_runtime_bridge.execute(
                        queued
                    )
                )
            else:
                run_row_id, run_status = (
                    await self.runtime_bridge.execute(queued)
                )
            async with AsyncSessionLocal() as db:
                queue = ImInboundQueue(db)
                if run_status in {
                    "delivery_failed",
                    "delivery_uncertain",
                    "stale",
                    "not_ready",
                }:
                    delivery_status = (
                        await queue.retry_or_finish_delivery(
                            inbound_message_id=(
                                queued.inbound_message_id
                            ),
                            owner_instance_id=(
                                queued.owner_instance_id
                            ),
                            lease_epoch=queued.lease_epoch,
                            uncertain=(
                                run_status
                                == "delivery_uncertain"
                            ),
                        )
                    )
                    if delivery_status == "ready_to_deliver":
                        self._queue_wakeup.set()
                elif run_status == "completed":
                    if queued.target_type == "application_agent":
                        await queue.mark_application_completed(
                            inbound_message_id=(
                                queued.inbound_message_id
                            ),
                            application_agent_run_id=run_row_id,
                            owner_instance_id=(
                                queued.owner_instance_id
                            ),
                            lease_epoch=queued.lease_epoch,
                        )
                    else:
                        await queue.mark_completed(
                            inbound_message_id=(
                                queued.inbound_message_id
                            ),
                            runtime_run_row_id=run_row_id,
                            owner_instance_id=(
                                queued.owner_instance_id
                            ),
                            lease_epoch=queued.lease_epoch,
                        )
                elif run_status == "canceled":
                    await queue.mark_preempted(
                        inbound_message_id=queued.inbound_message_id,
                        owner_instance_id=queued.owner_instance_id,
                        lease_epoch=queued.lease_epoch,
                    )
                else:
                    await queue.mark_runtime_terminal_failed(
                        inbound_message_id=queued.inbound_message_id,
                        error_code="RUNTIME_RUN_FAILED",
                        error_message="Agent 处理失败",
                        owner_instance_id=queued.owner_instance_id,
                        lease_epoch=queued.lease_epoch,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with AsyncSessionLocal() as db:
                interrupted_text = await ImInboundQueue(
                    db
                ).preempted_final_text(
                    inbound_message_id=queued.inbound_message_id,
                )
            if interrupted_text is not None:
                try:
                    await self.bridge.reply_stream(
                        channel_id=queued.channel_id,
                        generation=queued.generation,
                        lease_epoch=queued.lease_epoch,
                        provider_request_id=queued.provider_request_id,
                        stream_id=queued.stream_id,
                        content=interrupted_text,
                        finish=True,
                        non_blocking=False,
                    )
                except Exception:
                    pass
                async with AsyncSessionLocal() as db:
                    await ImInboundQueue(db).mark_preempted(
                        inbound_message_id=queued.inbound_message_id,
                        owner_instance_id=queued.owner_instance_id,
                        lease_epoch=queued.lease_epoch,
                    )
                return
            error_code = str(getattr(exc, "code", None) or exc)
            if error_code in {
                "SESSION_BUSY",
                "SESSION_CAPACITY_BUSY",
                "application_agent_session_busy",
                "conversation_busy",
            }:
                async with AsyncSessionLocal() as db:
                    waiting = await ImInboundQueue(
                        db
                    ).mark_session_busy_wait(
                        inbound_message_id=queued.inbound_message_id,
                        owner_instance_id=queued.owner_instance_id,
                        lease_epoch=queued.lease_epoch,
                    )
                if waiting:
                    try:
                        await self.bridge.reply_stream(
                            channel_id=queued.channel_id,
                            generation=queued.generation,
                            lease_epoch=queued.lease_epoch,
                            provider_request_id=queued.provider_request_id,
                            stream_id=queued.stream_id,
                            content=(
                                "消息已保存，Agent 恢复后将自动继续。"
                            ),
                            finish=False,
                            non_blocking=True,
                        )
                    except Exception:
                        pass
                    self._queue_wakeup.set()
                return
            if (
                queued.target_type == "application_agent"
                and isinstance(exc, ApplicationAgentInvocationError)
                and exc.code == "application_agent_capacity_busy"
            ):
                async with AsyncSessionLocal() as db:
                    capacity_status = await ImInboundQueue(
                        db
                    ).mark_capacity_wait(
                        inbound_message_id=queued.inbound_message_id,
                        owner_instance_id=queued.owner_instance_id,
                        lease_epoch=queued.lease_epoch,
                    )
                if capacity_status == "stale":
                    return
                if capacity_status == "capacity_timeout":
                    try:
                        await self.bridge.reply_stream(
                            channel_id=queued.channel_id,
                            generation=queued.generation,
                            lease_epoch=queued.lease_epoch,
                            provider_request_id=queued.provider_request_id,
                            stream_id=queued.stream_id,
                            content="当前使用人数较多，请稍后再试。",
                            finish=True,
                            non_blocking=False,
                        )
                    except Exception:
                        pass
                else:
                    self._queue_wakeup.set()
                return
            _log_im_gateway_timing(
                target_type=queued.target_type,
                stage="failed",
                inbound_message_id=queued.inbound_message_id,
                status="error",
                error_type=type(exc).__name__,
            )
            error_code = str(
                getattr(exc, "code", None)
                or type(exc).__name__
            )
            async with AsyncSessionLocal() as db:
                status = await ImInboundQueue(db).mark_failed(
                    inbound_message_id=queued.inbound_message_id,
                    error_code=error_code,
                    error_message=str(exc),
                    max_attempts=(
                        settings.IM_GATEWAY_MAX_MESSAGE_ATTEMPTS
                    ),
                    owner_instance_id=queued.owner_instance_id,
                    lease_epoch=queued.lease_epoch,
                )
            if status == "stale":
                return
            try:
                await self.bridge.reply_stream(
                    channel_id=queued.channel_id,
                    generation=queued.generation,
                    lease_epoch=queued.lease_epoch,
                    provider_request_id=queued.provider_request_id,
                    stream_id=queued.stream_id,
                    content=(
                        (
                            "当前会话模型不支持图片识别。请在网页端切换支持图片的模型后重试。"
                            if queued.target_type == "personal_agent"
                            else "当前应用模型暂不支持图片识别，请联系应用管理员调整模型配置。"
                        )
                        if error_code == "MODEL_VISION_UNSUPPORTED"
                        else "消息已保存，Agent 恢复后将自动继续。"
                        if status == "queued"
                        else "Agent 多次唤醒失败，本轮已停止；后续消息仍可继续。"
                    ),
                    finish=status != "queued",
                    non_blocking=False,
                )
            except Exception:
                pass

    async def _set_status(
        self,
        actor: OwnedImChannel,
        status: str,
        *,
        error_code: str | None,
        error_message: str | None = None,
    ) -> None:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(ImChannel, ImChannelLease)
                    .join(
                        ImChannelLease,
                        ImChannelLease.channel_id == ImChannel.id,
                    )
                    .where(
                        ImChannel.id == actor.channel_id,
                        ImChannel.config_generation
                        == actor.config_generation,
                        ImChannelLease.owner_instance_id
                        == self.instance_id,
                        ImChannelLease.lease_epoch
                        == actor.lease_epoch,
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                await db.rollback()
                return
            channel, _lease = row
            channel.status = status
            channel.last_error_code = error_code
            channel.last_error_message = error_message
            if status == "connected":
                channel.last_connected_at = datetime.now(timezone.utc)
            await db.commit()

    async def _redis_config_loop(self) -> None:
        client = from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(_CONFIG_CHANNEL)
            async for message in pubsub.listen():
                if not self._started:
                    return
                if message.get("type") != "message":
                    continue
                try:
                    await self.reconcile()
                except Exception:
                    pass
        except (RedisError, asyncio.CancelledError):
            if not self._started:
                return
        finally:
            await pubsub.aclose()
            await client.aclose()

    @staticmethod
    def _event_is_current(
        actor: OwnedImChannel, event: dict[str, Any]
    ) -> bool:
        return (
            int(event.get("generation") or -1)
            == actor.config_generation
            and int(event.get("lease_epoch") or -1)
            == actor.lease_epoch
        )


gateway_runtime = ImGatewayRuntime()
