from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Iterable, Protocol
from uuid import uuid4

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.hermes_runtime.events import PlatformStreamEvent
from app.models import (
    ApplicationAgent,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
    ChatFileArtifact,
    CloudHermesProfile,
    HermesRuntimeBinding,
    ImChannel,
    ImChannelLease,
    ImDeliveryCheckpoint,
    ImExecutionBatch,
    ImInboundMessage,
    ImMessageAttachment,
    ModelEntry,
    RuntimeAgentProfile,
    RuntimeRun,
    RuntimeSession,
)
from app.schemas.model_runtime_capabilities import (
    normalize_runtime_capabilities,
    supports_native_vision,
)
from app.schemas.application_agent_invocation import (
    ApplicationAgentResponseRequest,
)
from app.schemas.runtime_internal import (
    CancelCommandPayload,
    ChannelFastLaneV2DispositionFrame,
    InternalTurnCommandPayload,
    RuntimeTurnAttachment,
    RuntimeTurnIdentity,
)
from app.services.application_agent_invocation import (
    ApplicationAgentInvocationError,
    ApplicationChannelSessionPlan,
    PreparedApplicationChannelInput,
    SqlAlchemyApplicationAgentInvocationService,
)
from app.services.chat_files import (
    ChatFileIndexService,
    RegisterChatFileCommand,
    SqlAlchemyChatFileStore,
)
from app.services.model_catalog_cache import (
    RedisModelRouteCache,
    shared_model_cache_metrics,
    shared_model_catalog_cache,
)
from app.services.runtime_admission import (
    RuntimeAdmissionError,
    RuntimeAdmissionService,
)
from app.services.runtime_chat import (
    RuntimeChatRequestIntent,
    RuntimeChatService,
    SqlAlchemyRuntimeChatStore,
)
from app.services.runtime_chat_timing import RuntimeChatTiming
from app.services.runtime_infrastructure import (
    build_runtime_infrastructure,
    build_runtime_protocol_adapters,
)
from app.services.runtime_prepared_knowledge import (
    shared_runtime_prepared_knowledge_service,
)
from app.services.runtime_models import (
    RuntimeTurnModelNotVisibleError,
    RuntimeTurnModelUnavailableError,
    SqlAlchemyRuntimeModelStore,
    resolve_runtime_turn_model_entry_id,
)
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec
from app.services.runtime_commands import (
    RuntimeCommandClient,
    RuntimeCommandError,
    RuntimeCommandTarget,
    build_runtime_command,
    deterministic_command_id,
    load_runtime_command_admission_generation,
)
from app.services.profile_execution_identity import (
    ProfileExecutionIdentityError,
    resolve_profile_execution_identity,
)
from app.services.application_agent_execution_identity import application_asset_digests
from app.services.runtime_channel_fast_lane import (
    RuntimeChannelFastLaneClient,
    RuntimeChannelFastLaneV2Client,
    acknowledge_channel_delivery,
    acknowledge_channel_delivery_v2,
    issue_channel_execution_lease,
)
from app.services.runtime_provider_routes import (
    ProviderRouteBindingIdentity,
    RuntimeProviderRouteError,
    RuntimeProviderRouteResolver,
)

from .node_bridge import WeComBridgeError, WeComNodeBridge
from .messages import NormalizedWeComMessage
from .queue import ImInboundQueue, QueuedBatchMessage, QueuedInbound
from .stream_projector import ProjectorIdentity, WeComStreamProjector


_SAFE_FILENAME_RE = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+")
_SAFE_CORRELATION_ID_RE = re.compile(
    r"[A-Za-z0-9._:-]{1,128}\Z"
)
_APPLICATION_IM_TIMING_PAIRS = (
    ("projector_loaded", "application_authorized"),
    ("application_authorized", "conversation_resolved"),
    ("conversation_resolved", "session_materialization_started"),
    ("session_materialization_started", "session_materialized"),
    ("session_materialized", "session_prewarm_started"),
    ("session_prewarm_started", "session_prewarm_completed"),
    ("conversation_resolved", "session_import_done"),
    ("session_import_done", "attachment_staging_done"),
    ("attachment_staging_done", "run_committed"),
    ("run_committed", "im_run_bound"),
    ("im_run_bound", "capacity_acquired"),
    ("capacity_acquired", "running_committed"),
    ("capacity_acquired", "runtime_submitted"),
    ("running_committed", "runtime_submitted"),
    ("runtime_submitted", "first_runtime_public_event"),
    ("runtime_submitted", "first_visible_event_received"),
    ("projector_loaded", "first_visible_event_received"),
)


@dataclass(frozen=True)
class PreparedWeComTurn:
    prompt: str
    attachments: tuple[RuntimeTurnAttachment, ...]
_WECOM_EVENT_PUMP_QUEUE_LIMIT = 256
_PERSONAL_IM_RUN_TIMING_PAIRS = (
    ("runtime_activate_started", "runtime_activate_done"),
    ("prompt_build_started", "prompt_build_done"),
    ("attachments_done", "run_prepare_done"),
    ("run_prepare_done", "run_profile_gate_done"),
    (
        "run_profile_gate_done",
        "run_reserved_id_precheck_done",
    ),
    ("run_reserved_id_precheck_done", "run_binding_lock_done"),
    ("run_binding_lock_done", "run_session_lock_done"),
    ("run_session_lock_done", "run_reserved_id_check_done"),
    ("run_reserved_id_check_done", "run_active_check_done"),
    ("run_active_check_done", "run_insert_begin"),
    ("run_insert_begin", "run_insert_done"),
    ("run_insert_done", "run_commit_begin"),
    ("run_commit_begin", "run_commit_done"),
    ("run_commit_done", "run_created"),
    ("attachments_done", "run_created"),
    ("run_created", "im_run_bound"),
    ("im_run_bound", "model_route_prepare_started"),
    (
        "model_route_prepare_started",
        "model_route_prepare_done",
    ),
    ("im_run_bound", "projector_loaded"),
    ("run_created", "model_route_prepare_done"),
)


@dataclass(frozen=True)
class PreparedWeComFastLaneRoute:
    """Short-lived control-plane snapshot used only by the text hot path."""

    target: RuntimeCommandTarget
    identity: RuntimeTurnIdentity
    channel_id: int
    target_type: str
    target_id: int
    gateway_instance_id: str
    channel_lease_epoch: int
    channel_generation: int
    config_generation: int
    prepared_at: float

    def is_current(
        self,
        *,
        channel_id: int,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        channel_generation: int,
        max_age_seconds: float = 120.0,
    ) -> bool:
        return (
            self.channel_id == channel_id
            and self.gateway_instance_id == gateway_instance_id
            and self.channel_lease_epoch == channel_lease_epoch
            and self.channel_generation == channel_generation
            and time.monotonic() - self.prepared_at < max_age_seconds
        )


class WeComFastLaneColdPath(RuntimeError):
    """The turn was not accepted and must enter the durable Backend path."""


class WeComFastLaneRouteRejected(RuntimeError):
    """The durable inbound cannot pass a permanent business validation."""

    def __init__(self, *, error_code: str, user_message: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.user_message = user_message


@dataclass(frozen=True, slots=True)
class PreparedWeComArtifact:
    """One final artifact revision emitted by the current Runtime Run."""

    relative_path: str
    mime_type: str | None
    size_bytes: int
    mtime_ns: int


@dataclass(frozen=True)
class PreparedWeComFastLaneResult:
    runtime_run_id: str
    provider_stream_id: str
    highest_contiguous_seq: int
    final_text: str
    delivery_status: str
    artifacts: tuple[PreparedWeComArtifact, ...]


class WeComFastLaneArtifactCheckpointStore(Protocol):
    async def delivered_keys(self, inbound_message_id: int) -> set[str]: ...

    async def mark_delivered(
        self,
        inbound_message_id: int,
        artifact_key: str,
    ) -> None: ...

    async def mark_failed(
        self,
        inbound_message_id: int,
        error_code: str,
    ) -> None: ...

    async def clear_failure(self, inbound_message_id: int) -> None: ...


class SqlAlchemyWeComFastLaneArtifactCheckpointStore:
    """Artifact-only idempotency on the current Provider message checkpoint."""

    async def delivered_keys(self, inbound_message_id: int) -> set[str]:
        async with AsyncSessionLocal() as db:
            values = await db.scalar(
                select(ImDeliveryCheckpoint.delivered_artifact_ids).where(
                    ImDeliveryCheckpoint.inbound_message_id
                    == inbound_message_id
                )
            )
        return {
            str(value)
            for value in values or []
            if isinstance(value, str)
        }

    async def mark_delivered(
        self,
        inbound_message_id: int,
        artifact_key: str,
    ) -> None:
        async with AsyncSessionLocal() as db:
            checkpoint = (
                await db.execute(
                    select(ImDeliveryCheckpoint)
                    .where(
                        ImDeliveryCheckpoint.inbound_message_id
                        == inbound_message_id
                    )
                    .with_for_update()
                )
            ).scalar_one()
            delivered = list(checkpoint.delivered_artifact_ids or [])
            if artifact_key not in delivered:
                delivered.append(artifact_key)
                checkpoint.delivered_artifact_ids = delivered
            if str(checkpoint.error_code or "").startswith(
                "fast_lane_artifact:"
            ):
                checkpoint.error_code = None
                checkpoint.error_message = None
            await db.commit()

    async def mark_failed(
        self,
        inbound_message_id: int,
        error_code: str,
    ) -> None:
        async with AsyncSessionLocal() as db:
            checkpoint = await db.scalar(
                select(ImDeliveryCheckpoint).where(
                    ImDeliveryCheckpoint.inbound_message_id
                    == inbound_message_id
                )
            )
            if checkpoint is None:
                return
            checkpoint.error_code = (
                f"fast_lane_artifact:{error_code}"[:100]
            )
            checkpoint.error_message = (
                "Agent 生成附件暂未投递到企微，系统将在本轮内重试"
            )
            await db.commit()

    async def clear_failure(self, inbound_message_id: int) -> None:
        async with AsyncSessionLocal() as db:
            checkpoint = await db.scalar(
                select(ImDeliveryCheckpoint).where(
                    ImDeliveryCheckpoint.inbound_message_id
                    == inbound_message_id
                )
            )
            if checkpoint is None or not str(
                checkpoint.error_code or ""
            ).startswith("fast_lane_artifact:"):
                return
            checkpoint.error_code = None
            checkpoint.error_message = None
            await db.commit()


class WeComFastLaneArtifactDelivery:
    """Deliver only artifacts emitted by one completed Fast Lane Run."""

    def __init__(
        self,
        *,
        node_bridge: WeComNodeBridge,
        dashboard: Any,
        checkpoint_store: WeComFastLaneArtifactCheckpointStore | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.node_bridge = node_bridge
        self.dashboard = dashboard
        self.checkpoint_store = (
            checkpoint_store
            or SqlAlchemyWeComFastLaneArtifactCheckpointStore()
        )
        self.sleep = sleep

    async def deliver(
        self,
        *,
        route: PreparedWeComFastLaneRoute,
        message: NormalizedWeComMessage,
        inbound_message_id: int,
        artifacts: tuple[PreparedWeComArtifact, ...],
    ) -> None:
        if not artifacts:
            return
        for attempt in range(3):
            try:
                await self._deliver_pending(
                    route=route,
                    message=message,
                    inbound_message_id=inbound_message_id,
                    artifacts=artifacts,
                )
                await self.checkpoint_store.clear_failure(
                    inbound_message_id
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.checkpoint_store.mark_failed(
                    inbound_message_id,
                    type(exc).__name__,
                )
                if attempt == 2:
                    raise
                await self.sleep(float(2**attempt))

    async def _deliver_pending(
        self,
        *,
        route: PreparedWeComFastLaneRoute,
        message: NormalizedWeComMessage,
        inbound_message_id: int,
        artifacts: tuple[PreparedWeComArtifact, ...],
    ) -> None:
        delivered = await self.checkpoint_store.delivered_keys(
            inbound_message_id
        )
        endpoint = RuntimeEndpoint(
            runtime_workspace_id=route.target_id,
            runtime_id=route.target.runtime_id,
            endpoint_base_url=settings.HERMES_RUNTIME_INGRESS_BASE_URL,
            endpoint_host_header=route.target.endpoint_host_header,
            api_key=route.target.api_server_key,
            coordination_scope_key=(
                f"{route.target_type}:{route.target_id}"
            ),
        )
        for artifact in artifacts:
            artifact_key = _fast_lane_artifact_key(
                artifact.relative_path
            )
            if artifact_key in delivered:
                continue
            filename = _safe_filename(
                PurePosixPath(artifact.relative_path).name
            )
            if self.node_bridge.temp_root is None:
                raise ImRuntimeBridgeError(
                    "WeCom bridge temp root is unavailable"
                )
            target = self.node_bridge.require_scoped_path(
                str(
                    self.node_bridge.temp_root
                    / f"hot-out-{uuid4().hex}-{filename}"
                )
            )
            try:
                with target.open("wb") as destination:
                    async for chunk in (
                        self.dashboard.stream_managed_artifact(
                            endpoint,
                            relative_path=artifact.relative_path,
                        )
                    ):
                        destination.write(chunk)
                try:
                    await self.node_bridge.upload_media(
                        passive=False,
                        channel_id=route.channel_id,
                        generation=route.channel_generation,
                        lease_epoch=route.channel_lease_epoch,
                        media_type=_outbound_media_type(
                            artifact.mime_type,
                            filename,
                        ),
                        source_path=target,
                        filename=filename,
                        chat_id=message.external_conversation_id,
                    )
                except WeComBridgeError as exc:
                    if exc.code != "MEDIA_TOO_LARGE":
                        raise
                    await self.node_bridge.send_markdown(
                        channel_id=route.channel_id,
                        generation=route.channel_generation,
                        lease_epoch=route.channel_lease_epoch,
                        chat_id=message.external_conversation_id,
                        content=(
                            f"附件 **{filename}** 超出企微发送上限，"
                            "请在 WebUI 中下载。"
                        ),
                    )
                await self.checkpoint_store.mark_delivered(
                    inbound_message_id,
                    artifact_key,
                )
                delivered.add(artifact_key)
            finally:
                target.unlink(missing_ok=True)


def _fast_lane_artifact_key(relative_path: str) -> str:
    return (
        "path:"
        + hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
    )


def _prepared_artifact(
    data: dict[str, object],
) -> PreparedWeComArtifact | None:
    relative_path = _prepared_artifact_path(data.get("relative_path"))
    if relative_path is None:
        return None
    try:
        size_bytes = max(0, int(data.get("size_bytes") or 0))
        mtime_ns = max(0, int(data.get("mtime_ns") or 0))
    except (TypeError, ValueError, OverflowError):
        return None
    return PreparedWeComArtifact(
        relative_path=relative_path,
        mime_type=(
            str(data["mime_type"])
            if data.get("mime_type")
            else None
        ),
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
    )


def _prepared_artifact_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    path = PurePosixPath(value.strip())
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or path.parts[0] != "workspace"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return None
    return path.as_posix()


def _hot_turn_identity(
    *,
    target_type: str,
    channel_id: int,
    external_message_id: str,
    external_conversation_id: str,
) -> tuple[int, str, str, str, str]:
    digest = hashlib.sha256(
        (
            f"{channel_id}\0{external_message_id}\0"
            f"{external_conversation_id}"
        ).encode("utf-8")
    ).hexdigest()
    synthetic_inbound_id = int(digest[:15], 16)
    invocation_request_id = f"wecom_{digest[:48]}"
    client_message_id = (
        deterministic_command_id(
            "application-message",
            invocation_request_id,
        )
        if target_type == "application_agent"
        else f"msg_{digest[:48]}"
    )
    return (
        synthetic_inbound_id,
        f"wecom_run_{digest[:40]}",
        client_message_id,
        f"im-{digest[:48]}",
        invocation_request_id,
    )


def _hot_text_prompt(message: NormalizedWeComMessage) -> str:
    text = message.text or "[企微消息没有可读取的正文]"
    if message.quote_payload:
        text += (
            "\n\n<wecom_quote>\n"
            + json.dumps(
                message.quote_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n</wecom_quote>"
        )
    return text


def _durable_hot_runtime_run_id(
    *,
    target_type: str,
    channel_id: int,
    inbound_message_id: int,
) -> str:
    if target_type == "application_agent":
        digest = hashlib.sha256(
            f"wecom:{channel_id}:{inbound_message_id}".encode("utf-8")
        ).hexdigest()
        return f"app_run_wecom_{digest[:40]}"
    return f"rr-im-wecom-{inbound_message_id}"


async def execute_prepared_wecom_fast_lane(
    *,
    node_bridge: WeComNodeBridge,
    route: PreparedWeComFastLaneRoute,
    message: NormalizedWeComMessage,
) -> PreparedWeComFastLaneResult:
    """Submit and project one DB-free hot text turn through Supervisor."""

    (
        synthetic_inbound_id,
        runtime_run_id,
        client_message_id,
        stream_id,
        _invocation_request_id,
    ) = _hot_turn_identity(
        target_type=route.target_type,
        channel_id=route.channel_id,
        external_message_id=message.external_message_id,
        external_conversation_id=message.external_conversation_id,
    )
    lease = issue_channel_execution_lease(
        target=route.target,
        org_id=route.identity.org_id,
        channel_id=route.channel_id,
        target_type=route.target_type,
        target_id=route.target_id,
        subject_user_id=route.identity.user_id,
        runtime_session_id=route.identity.session_id,
        model_entry_id=route.identity.model_id,
        profile_id=route.identity.profile_id,
        application_agent_id=getattr(route.identity, "application_agent_id", None),
        gateway_instance_id=route.gateway_instance_id,
        channel_lease_epoch=route.channel_lease_epoch,
        channel_generation=route.channel_generation,
        config_generation=route.config_generation,
    )
    payload = InternalTurnCommandPayload(
        source=(
            "application_wecom"
            if route.target_type == "application_agent"
            else "personal_wecom"
        ),
        run_id=runtime_run_id,
        client_message_id=client_message_id,
        identity=route.identity,
        prompt=_hot_text_prompt(message),
        channel_execution_lease=lease,
    )
    envelope = build_runtime_command(
        target=route.target,
        command_id=deterministic_command_id(
            "wecom-fast-lane",
            route.target.binding_id,
            client_message_id,
        ),
        payload=payload,
        deadline_seconds=600,
    )

    async def delivery_ack_sink(
        highest_contiguous_seq: int,
        confirmed_at: datetime,
    ) -> None:
        await acknowledge_channel_delivery(
            target=route.target,
            lease=lease,
            run_id=runtime_run_id,
            highest_contiguous_seq=highest_contiguous_seq,
            provider_request_id=message.provider_request_id,
            confirmed_at=confirmed_at,
        )

    projector = WeComStreamProjector(
        bridge=node_bridge,
        identity=ProjectorIdentity(
            inbound_message_id=synthetic_inbound_id,
            channel_id=route.channel_id,
            generation=route.channel_generation,
            owner_instance_id=route.gateway_instance_id,
            lease_epoch=route.channel_lease_epoch,
            provider_request_id=message.provider_request_id,
            provider_stream_id=stream_id,
            chat_id=message.external_conversation_id,
            target_type=route.target_type,
        ),
        delivery_ack_sink=delivery_ack_sink,
        durable_projection=False,
    )
    event_pump = _WeComRuntimeEventPump(
        projector=projector,
        task_name=f"im-hot-project-{runtime_run_id}",
    )
    await event_pump.start()
    client = RuntimeChannelFastLaneClient()
    terminal_event: PlatformStreamEvent | None = None
    artifacts_by_path: dict[str, PreparedWeComArtifact] = {}
    drained = False

    async def on_runtime_event(
        event_name: str,
        event_data: dict[str, object],
    ) -> None:
        nonlocal terminal_event
        if event_name in {
            "chat.artifact.created",
            "chat.artifact.updated",
        }:
            artifact = _prepared_artifact(event_data)
            if artifact is not None:
                artifacts_by_path[artifact.relative_path] = artifact
        elif event_name == "chat.artifact.deleted":
            relative_path = _prepared_artifact_path(
                event_data.get("relative_path")
            )
            if relative_path is not None:
                artifacts_by_path.pop(relative_path, None)
        if event_name == "chat.done":
            terminal_event = PlatformStreamEvent(
                event=event_name,
                data=event_data,
            )
        await event_pump.emit(
            PlatformStreamEvent(event=event_name, data=event_data)
        )

    accepted = False
    try:
        ack = await client.send(target=route.target, envelope=envelope)
        if ack.state != "accepted" or ack.run_id != runtime_run_id:
            raise WeComFastLaneColdPath(
                ack.error_code or "fast_lane_turn_rejected"
            )
        accepted = True
        snapshot = await client.wait_run(
            target=route.target,
            run_id=runtime_run_id,
            event_sink=on_runtime_event,
            timeout_seconds=600,
        )
        await event_pump.drain()
        drained = True
        if terminal_event is not None:
            await projector.on_event(terminal_event)
        elif str(snapshot.get("status") or "").upper() in {
            "COMPLETED",
            "FAILED",
            "CANCELED",
        }:
            # A duplicate whose delivery checkpoint already covers the
            # terminal seq intentionally replays no Provider frames.
            projector.state.logical_text = str(
                snapshot.get("answer")
                or snapshot.get("error_message")
                or ""
            )
            projector.state.status = "completed"
    except WeComFastLaneColdPath:
        raise
    except Exception as exc:
        if not accepted:
            raise WeComFastLaneColdPath(
                "fast_lane_turn_not_accepted"
            ) from exc
        raise
    finally:
        try:
            if not drained:
                await event_pump.drain()
        finally:
            await client.close()
    return PreparedWeComFastLaneResult(
        runtime_run_id=runtime_run_id,
        provider_stream_id=stream_id,
        highest_contiguous_seq=projector.state.last_seq,
        final_text=projector.state.logical_text,
        delivery_status=projector.state.status,
        artifacts=tuple(
            artifacts_by_path[path]
            for path in sorted(artifacts_by_path)
        ),
    )


async def execute_durable_wecom_fast_lane_v2(
    *,
    node_bridge: WeComNodeBridge,
    route: PreparedWeComFastLaneRoute,
    message: NormalizedWeComMessage,
    inbound_message_id: int,
    delivery_id: str,
    client_message_id: str,
    provider_stream_id: str,
    runtime_durable_sink: Callable[
        [ChannelFastLaneV2DispositionFrame], Awaitable[None]
    ],
    runtime_disposition_seq: int | None = None,
    runtime_canonical_run_id: str | None = None,
    runtime_start_after_seq: int | None = None,
) -> PreparedWeComFastLaneResult:
    """Resume or submit one already-durable channel delivery through v2."""

    recovery_facts_present = any(
        value is not None
        for value in (
            runtime_disposition_seq,
            runtime_canonical_run_id,
            runtime_start_after_seq,
        )
    )
    if recovery_facts_present and (
        runtime_disposition_seq is None
        or runtime_disposition_seq <= 0
        or not runtime_canonical_run_id
        or runtime_start_after_seq is None
    ):
        raise RuntimeCommandError(
            "Fast Lane v2 durable recovery facts are incomplete"
        )

    runtime_run_id = _durable_hot_runtime_run_id(
        target_type=route.target_type,
        channel_id=route.channel_id,
        inbound_message_id=inbound_message_id,
    )
    lease = issue_channel_execution_lease(
        target=route.target,
        org_id=route.identity.org_id,
        channel_id=route.channel_id,
        target_type=route.target_type,
        target_id=route.target_id,
        subject_user_id=route.identity.user_id,
        runtime_session_id=route.identity.session_id,
        model_entry_id=route.identity.model_id,
        profile_id=route.identity.profile_id,
        application_agent_id=getattr(route.identity, "application_agent_id", None),
        gateway_instance_id=route.gateway_instance_id,
        channel_lease_epoch=route.channel_lease_epoch,
        channel_generation=route.channel_generation,
        config_generation=route.config_generation,
    )
    payload = InternalTurnCommandPayload(
        source=(
            "application_wecom"
            if route.target_type == "application_agent"
            else "personal_wecom"
        ),
        run_id=runtime_run_id,
        client_message_id=client_message_id,
        identity=route.identity,
        prompt=_hot_text_prompt(message),
        channel_execution_lease=lease,
    )
    envelope = build_runtime_command(
        target=route.target,
        command_id=deterministic_command_id(
            "wecom-fast-lane-v2",
            route.target.binding_id,
            delivery_id,
        ),
        payload=payload,
        deadline_seconds=600,
    )
    owner = {
        "canonical_run_id": (
            runtime_canonical_run_id or runtime_run_id
        ),
    }

    async def delivery_ack_sink(
        highest_contiguous_seq: int,
        confirmed_at: datetime,
    ) -> None:
        await acknowledge_channel_delivery_v2(
            target=route.target,
            lease=lease,
            owner_delivery_id=delivery_id,
            canonical_run_id=owner["canonical_run_id"],
            highest_contiguous_seq=highest_contiguous_seq,
            provider_request_id=message.provider_request_id,
            confirmed_at=confirmed_at,
        )

    projector = WeComStreamProjector(
        bridge=node_bridge,
        identity=ProjectorIdentity(
            inbound_message_id=inbound_message_id,
            channel_id=route.channel_id,
            generation=route.channel_generation,
            owner_instance_id=route.gateway_instance_id,
            lease_epoch=route.channel_lease_epoch,
            provider_request_id=message.provider_request_id,
            provider_stream_id=provider_stream_id,
            chat_id=message.external_conversation_id,
            target_type=route.target_type,
        ),
        delivery_ack_sink=delivery_ack_sink,
        durable_projection=True,
    )
    event_pump = _WeComRuntimeEventPump(
        projector=projector,
        task_name=f"im-fast-lane-v2-project-{delivery_id}",
    )
    await event_pump.start()
    client = RuntimeChannelFastLaneV2Client()
    terminal_event: PlatformStreamEvent | None = None
    artifacts_by_path: dict[str, PreparedWeComArtifact] = {}
    drained = False

    async def on_runtime_event(
        event_name: str,
        event_data: dict[str, object],
    ) -> None:
        nonlocal terminal_event
        event_run_id = str(event_data.get("runtime_run_id") or "")
        if event_run_id:
            owner["canonical_run_id"] = event_run_id
        async with AsyncSessionLocal() as db:
            is_owner = await ImInboundQueue(
                db
            ).is_fast_lane_delivery_owner(
                inbound_message_id=inbound_message_id,
                owner_instance_id=route.gateway_instance_id,
                lease_epoch=route.channel_lease_epoch,
            )
        if not is_owner:
            return
        if event_name in {
            "chat.artifact.created",
            "chat.artifact.updated",
        }:
            artifact = _prepared_artifact(event_data)
            if artifact is not None:
                artifacts_by_path[artifact.relative_path] = artifact
        elif event_name == "chat.artifact.deleted":
            relative_path = _prepared_artifact_path(
                event_data.get("relative_path")
            )
            if relative_path is not None:
                artifacts_by_path.pop(relative_path, None)
        if event_name == "chat.done":
            terminal_event = PlatformStreamEvent(
                event=event_name,
                data=event_data,
            )
        await event_pump.emit(
            PlatformStreamEvent(event=event_name, data=event_data)
        )

    try:
        if recovery_facts_present:
            connection, disposition = await client.resume(
                target=route.target,
                lease=lease,
                delivery_id=delivery_id,
                last_disposition_seq=int(runtime_disposition_seq or 0),
                last_event_seq=projector.state.last_seq,
            )
        else:
            connection, disposition = await client.submit_or_resume(
                target=route.target,
                lease=lease,
                delivery_id=delivery_id,
                client_message_id=client_message_id,
                envelope=envelope,
                last_event_seq=projector.state.last_seq,
            )
        if (
            disposition.delivery_id != delivery_id
            or disposition.client_message_id != client_message_id
        ):
            raise RuntimeCommandError(
                "Fast Lane v2 disposition identity does not match delivery"
            )
        if recovery_facts_present and (
            disposition.disposition_seq
            < int(runtime_disposition_seq or 0)
        ):
            raise RuntimeCommandError(
                "Fast Lane v2 disposition regressed during recovery"
            )
        owner["canonical_run_id"] = disposition.canonical_run_id
        await runtime_durable_sink(disposition)
        disposition = await client.wait_delivery(
            connection=connection,
            initial_disposition=disposition,
            event_sink=on_runtime_event,
            disposition_sink=runtime_durable_sink,
            timeout_seconds=600,
        )
        owner["canonical_run_id"] = disposition.canonical_run_id
        async with AsyncSessionLocal() as db:
            is_owner = await ImInboundQueue(
                db
            ).is_fast_lane_delivery_owner(
                inbound_message_id=inbound_message_id,
                owner_instance_id=route.gateway_instance_id,
                lease_epoch=route.channel_lease_epoch,
            )
        if disposition.state == "redirect_consumed" and not is_owner:
            await projector.deliver_final("已合并到当前任务。")
        elif disposition.state == "uncertain":
            await projector.deliver_final(
                "这条消息的送达状态暂时无法确认，请稍后在 WebUI 中核对。"
            )
        elif disposition.state == "canceled":
            await projector.deliver_final("上一任务已停止，这条修正未被应用。")
        await event_pump.drain()
        drained = True
        if terminal_event is not None:
            await projector.on_event(terminal_event)
    except RuntimeCommandError as exc:
        raise WeComFastLaneColdPath(
            "fast_lane_v2_delivery_not_durable"
        ) from exc
    finally:
        if not drained:
            await event_pump.drain()
    return PreparedWeComFastLaneResult(
        runtime_run_id=owner["canonical_run_id"],
        provider_stream_id=provider_stream_id,
        highest_contiguous_seq=projector.state.last_seq,
        final_text=projector.state.logical_text,
        delivery_status=projector.state.status,
        artifacts=tuple(
            artifacts_by_path[path]
            for path in sorted(artifacts_by_path)
        ),
    )


def _format_wecom_batch_prompt(
    messages: tuple[QueuedBatchMessage, ...],
    *,
    attachments: dict[int, list[dict[str, str]]],
) -> str:
    """Serialize a sealed batch without summarizing or replacing messages."""

    payload_messages: list[dict[str, object]] = []
    for message in sorted(messages, key=lambda item: item.batch_ordinal):
        payload_messages.append(
            {
                "inbound_message_id": message.inbound_message_id,
                "ordinal": message.batch_ordinal,
                "text": str(message.normalized_content.get("text") or ""),
                "quote": message.quote_payload,
                "attachments": attachments.get(
                    message.inbound_message_id, []
                ),
            }
        )
    return (
        "<wecom_messages>\n"
        + json.dumps(
            {"messages": payload_messages},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n</wecom_messages>"
    )


def _wecom_batch_attachment_entry(
    *,
    display_name: str,
    relative_path: str,
    mime_type: str | None,
) -> dict[str, str]:
    if mime_type and mime_type.startswith("image/"):
        return {"name": display_name, "delivery": "vision_channel"}
    return {"name": display_name, "path": relative_path}


def _wecom_platform_attachments_contract(
    attachments: Iterable[RuntimeTurnAttachment],
) -> str:
    image_lines: list[str] = []
    other_lines: list[str] = []
    for attachment in attachments:
        display_name = str(attachment.display_name)
        if attachment.mime_type and attachment.mime_type.startswith("image/"):
            image_lines.append(f"- {display_name}")
        else:
            other_lines.append(
                f"- {display_name}: {attachment.relative_path}"
            )
    if not image_lines and not other_lines:
        return ""
    sections = []
    if image_lines:
        sections.append(
            "Image attachments are already delivered to your vision channel "
            "by image.attach. Answer visual questions directly from them. "
            "Do not call vision_analyze, terminal, file/workspace tools, or "
            "session_search for these images unless the user explicitly "
            "requests file-level inspection or cross-session comparison:\n"
            + "\n".join(image_lines)
        )
    if other_lines:
        sections.append(
            "Non-image files (read from the current workspace when needed):\n"
            + "\n".join(other_lines)
        )
    return (
        "\n\n<platform_attachments>\n"
        "The user attached these files from WeCom. Read them from "
        "the current workspace when needed.\n"
        "Attachment-specific delivery instructions below override the "
        "default workspace instruction.\n"
        + "\n".join(sections)
        + "\n</platform_attachments>"
    )


_logger = logging.getLogger(__name__)
_timing_logger = logging.getLogger("uvicorn.error.runtime_timing")
_timing_tasks: set[asyncio.Task[Any]] = set()


def _timing_mark(
    timing: RuntimeChatTiming,
    stage: str,
) -> None:
    try:
        timing.mark(stage)
    except Exception:
        pass


def _log_application_im_timing(
    timing: RuntimeChatTiming,
    *,
    stage: str,
    status: str,
    inbound_message_id: int,
    runtime_session_id: str | None = None,
    error_type: str | None = None,
) -> None:
    try:
        snapshot = timing.safe_snapshot(
            duration_pairs=_APPLICATION_IM_TIMING_PAIRS,
        )
        runtime_run_id = snapshot.get("runtime_run_id")
        payload: dict[str, object] = {
            "component": "application_agent_im",
            "channel": "wecom",
            "stage": stage,
            "status": status,
            "request_id": f"im-wecom-{inbound_message_id}",
            "inbound_message_id": inbound_message_id,
            "durations_ms": snapshot.get("durations_ms", {}),
        }
        if (
            isinstance(runtime_run_id, str)
            and _SAFE_CORRELATION_ID_RE.fullmatch(runtime_run_id)
        ):
            payload["runtime_run_id"] = runtime_run_id
        if (
            runtime_session_id
            and _SAFE_CORRELATION_ID_RE.fullmatch(
                runtime_session_id
            )
        ):
            payload["runtime_session_id"] = runtime_session_id
        if error_type:
            payload["error_type"] = error_type
        _timing_logger.info("agent ttft timing=%s", payload)
    except Exception:
        pass


def _log_personal_im_run_timing(
    timing: RuntimeChatTiming,
    *,
    inbound_message_id: int,
    runtime_session_id: str,
    stage: str = "run_created",
) -> None:
    try:
        snapshot = timing.safe_snapshot(
            duration_pairs=_PERSONAL_IM_RUN_TIMING_PAIRS,
        )
        payload: dict[str, object] = {
            "component": "personal_agent_im",
            "channel": "wecom",
            "stage": stage,
            "status": "success",
            "request_id": f"im-wecom-{inbound_message_id}",
            "inbound_message_id": inbound_message_id,
            "durations_ms": snapshot.get("durations_ms", {}),
        }
        runtime_run_id = snapshot.get("runtime_run_id")
        if (
            isinstance(runtime_run_id, str)
            and _SAFE_CORRELATION_ID_RE.fullmatch(runtime_run_id)
        ):
            payload["runtime_run_id"] = runtime_run_id
        if _SAFE_CORRELATION_ID_RE.fullmatch(runtime_session_id):
            payload["runtime_session_id"] = runtime_session_id
        _timing_logger.info("agent ttft timing=%s", payload)
    except Exception:
        pass


def _consume_timing_task(task: asyncio.Task[Any]) -> None:
    _timing_tasks.discard(task)
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


def _schedule_timing_update(
    coroutine: Any,
    *,
    name: str,
) -> None:
    try:
        task = asyncio.create_task(coroutine, name=name)
    except Exception:
        close = getattr(coroutine, "close", None)
        if callable(close):
            close()
        return
    _timing_tasks.add(task)
    task.add_done_callback(_consume_timing_task)


def _is_public_runtime_event(event: PlatformStreamEvent) -> bool:
    return event.event in {
        "chat.reasoning.delta",
        "chat.activity.delta",
        "chat.progress.delta",
        "chat.tool.event",
        "chat.subagent.event",
        "chat.message.delta",
        "chat.message.completed",
        "chat.error",
    }


class _WeComRuntimeEventPump:
    """Project non-terminal Runtime events without blocking their producer."""

    def __init__(
        self,
        *,
        projector: WeComStreamProjector,
        task_name: str,
        first_public_sink: Callable[[PlatformStreamEvent], None]
        | None = None,
    ) -> None:
        self.projector = projector
        self.task_name = task_name
        self.first_public_sink = first_public_sink
        self._events: asyncio.Queue[PlatformStreamEvent | None] = (
            asyncio.Queue(maxsize=_WECOM_EVENT_PUMP_QUEUE_LIMIT)
        )
        self._task: asyncio.Task[None] | None = None
        self._error: BaseException | None = None
        self._stop_sent = False
        self._first_public_recorded = False

    async def start(self) -> None:
        await self.projector.load()
        self._task = asyncio.create_task(
            self._project_events(),
            name=self.task_name,
        )

    async def emit(self, event: PlatformStreamEvent) -> None:
        if self._error is not None:
            raise self._error
        if event.event == "chat.done":
            # The terminal frame must be emitted only after the final answer
            # and delivery digest have been durably sealed.
            return
        await self._events.put(event)

    async def drain(self) -> None:
        if self._task is None:
            return
        if not self._stop_sent:
            self._stop_sent = True
            await self._events.put(None)
        await self._task
        if self._error is not None:
            raise self._error

    async def _project_events(self) -> None:
        try:
            while True:
                event = await self._events.get()
                if event is None:
                    return
                data = dict(event.data)
                projected = PlatformStreamEvent(
                    event=event.event,
                    data=data,
                )
                if (
                    not self._first_public_recorded
                    and _is_public_runtime_event(projected)
                ):
                    self._first_public_recorded = True
                    if self.first_public_sink is not None:
                        self.first_public_sink(projected)
                await self.projector.on_event(projected)
        except BaseException as exc:
            self._error = exc


async def _persist_runtime_submitted(
    inbound_message_id: int,
    occurred_at: datetime,
) -> None:
    async with AsyncSessionLocal() as db:
        await ImInboundQueue(db).mark_runtime_submitted(
            inbound_message_id=inbound_message_id,
            occurred_at=occurred_at,
        )


async def _persist_batch_accepted(
    queued: QueuedInbound,
) -> None:
    async with AsyncSessionLocal() as db:
        await ImInboundQueue(db).mark_batch_accepted(
            inbound_message_id=queued.inbound_message_id,
            owner_instance_id=queued.owner_instance_id,
            lease_epoch=queued.lease_epoch,
        )


async def _persist_first_runtime_public_event(
    inbound_message_id: int,
    occurred_at: datetime,
) -> None:
    async with AsyncSessionLocal() as db:
        await ImInboundQueue(db).mark_first_runtime_public_event(
            inbound_message_id=inbound_message_id,
            occurred_at=occurred_at,
        )


class ImRuntimeBridgeError(RuntimeError):
    pass


class ImPreemptionCoordinator:
    """Cancel the predecessor of one durable interrupting WeCom batch."""

    def __init__(self) -> None:
        self.codec = RuntimeApiKeyCodec()
        self.command_client_factory = RuntimeCommandClient

    async def _cancel_and_wait(
        self,
        *,
        target: RuntimeCommandTarget,
        command_id: str,
        runtime_run_id: str,
    ) -> dict[str, Any] | None:
        client = self.command_client_factory()
        try:
            envelope = build_runtime_command(
                target=target,
                command_id=command_id,
                payload=CancelCommandPayload(run_id=runtime_run_id),
                deadline_seconds=30,
            )
            ack = await client.send(target=target, envelope=envelope)
            if ack.run_id != runtime_run_id:
                return None
            return await client.wait_run(
                target=target,
                run_id=runtime_run_id,
                timeout_seconds=30,
            )
        finally:
            await client.close()

    async def preempt_next(
        self,
        *,
        channel_id: int,
        owner_instance_id: str,
        lease_epoch: int,
    ) -> bool:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            lease = await db.scalar(
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
            if lease is None:
                await db.rollback()
                return False
            batch = await db.scalar(
                select(ImExecutionBatch)
                .where(
                    ImExecutionBatch.channel_id == channel_id,
                    ImExecutionBatch.state == "interrupting",
                )
                .order_by(
                    ImExecutionBatch.collected_at,
                    ImExecutionBatch.id,
                )
                .limit(1)
                .with_for_update()
            )
            if batch is None or batch.predecessor_batch_id is None:
                await db.rollback()
                return False
            predecessor = await db.scalar(
                select(ImExecutionBatch)
                .where(
                    ImExecutionBatch.id == batch.predecessor_batch_id,
                    ImExecutionBatch.state.in_(("submitting", "running")),
                )
                .with_for_update()
            )
            channel = await db.get(ImChannel, channel_id)
            if predecessor is None or channel is None:
                await db.rollback()
                return False
            runtime_run_id = str(
                predecessor.execution_runtime_run_id or ""
            ).strip()
            if not runtime_run_id:
                await db.rollback()
                return False
            if str(channel.target_type) == "application_agent":
                binding = await db.scalar(
                    select(ApplicationAgentRuntimeBinding).where(
                        ApplicationAgentRuntimeBinding.application_agent_id
                        == channel.application_agent_id,
                        ApplicationAgentRuntimeBinding.status == "ready",
                    )
                )
            else:
                binding = await db.scalar(
                    select(HermesRuntimeBinding).where(
                        HermesRuntimeBinding.org_id == channel.org_id,
                        HermesRuntimeBinding.user_id == channel.user_id,
                        HermesRuntimeBinding.runtime_workspace_id
                        == channel.runtime_workspace_id,
                        HermesRuntimeBinding.status == "ready",
                    )
                )
            if (
                binding is None
                or not binding.endpoint_host_header
                or not binding.api_key_encrypted
            ):
                await db.rollback()
                return False
            command_id = batch.cancel_command_id or deterministic_command_id(
                "wecom-preempt",
                channel_id,
                batch.ordering_key,
                runtime_run_id,
                batch.id,
            )
            batch.cancel_command_id = command_id
            batch.processing_owner = owner_instance_id
            batch.processing_lease_epoch = lease_epoch
            batch.predecessor_runtime_run_id = runtime_run_id
            target = RuntimeCommandTarget(
                runtime_id=str(binding.runtime_id),
                binding_id=str(binding.id),
                endpoint_host_header=str(binding.endpoint_host_header),
                api_server_key=self.codec.decrypt(
                    str(binding.api_key_encrypted)
                ),
                admission_generation=(
                    await load_runtime_command_admission_generation(db)
                ),
            )
            batch_id = int(batch.id)
            await db.commit()

        try:
            snapshot = await self._cancel_and_wait(
                target=target,
                command_id=command_id,
                runtime_run_id=runtime_run_id,
            )
        except RuntimeCommandError:
            return False
        if snapshot is None:
            return False
        state = str(snapshot.get("status") or "").upper()
        if state not in {
            "CANCELED",
            "COMPLETED",
            "FAILED",
            "FAILED_SUBMISSION_UNCERTAIN",
            "RUN_INTERRUPTED_BY_RUNTIME_RESTART",
            "RUN_INTERRUPTED_BY_RUNTIME_TERMINATION",
            "RUN_INTERRUPTED_BY_ADMIN_RESTART",
        }:
            return False
        terminal_status = (
            "canceled"
            if state == "CANCELED"
            else "completed"
            if state == "COMPLETED"
            else "failed"
        )
        async with AsyncSessionLocal() as db:
            return await ImInboundQueue(db).release_predecessor(
                execution_batch_id=batch_id,
                predecessor_terminal_status=terminal_status,
                owner_instance_id=owner_instance_id,
                lease_epoch=lease_epoch,
            )


@asynccontextmanager
async def _artifact_index_scope():
    async with AsyncSessionLocal() as db:
        yield ChatFileIndexService(SqlAlchemyChatFileStore(db))


def _build_runtime_chat_service(db) -> RuntimeChatService:
    route_cache = _build_model_route_cache()
    runtime_store = SqlAlchemyRuntimeChatStore(
        db, route_cache=route_cache
    )
    return RuntimeChatService(runtime_store)


def _build_model_route_cache() -> RedisModelRouteCache:
    return RedisModelRouteCache(
        shared_model_catalog_cache().client,
        metrics=shared_model_cache_metrics(),
    )


class PersonalImRuntimeBridge:
    def __init__(
        self,
        *,
        node_bridge: WeComNodeBridge,
    ) -> None:
        self.node_bridge = node_bridge
        self.dashboard = build_runtime_protocol_adapters().dashboard_adapter
        self._hot_routes: dict[int, PreparedWeComFastLaneRoute] = {}

    def hot_route(
        self,
        *,
        channel_id: int,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        channel_generation: int,
    ) -> PreparedWeComFastLaneRoute | None:
        route = getattr(self, "_hot_routes", {}).get(channel_id)
        if route is None or not route.is_current(
            channel_id=channel_id,
            gateway_instance_id=gateway_instance_id,
            channel_lease_epoch=channel_lease_epoch,
            channel_generation=channel_generation,
        ):
            return None
        return route

    def invalidate_channel(self, channel_id: int) -> None:
        getattr(self, "_hot_routes", {}).pop(channel_id, None)

    async def prepare_fast_lane_route(
        self,
        *,
        channel_id: int,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        channel_generation: int,
    ) -> PreparedWeComFastLaneRoute | None:
        """Rebuild a short-lived v2 route without creating a second Run."""

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with AsyncSessionLocal() as db:
            current = (
                await db.execute(
                    select(
                        ImChannel,
                        ImChannelLease,
                        RuntimeSession,
                        RuntimeAgentProfile,
                        CloudHermesProfile,
                        HermesRuntimeBinding,
                    )
                    .join(
                        ImChannelLease,
                        ImChannelLease.channel_id == ImChannel.id,
                    )
                    .join(
                        RuntimeSession,
                        RuntimeSession.id
                        == ImChannel.runtime_session_row_id,
                    )
                    .join(
                        RuntimeAgentProfile,
                        RuntimeAgentProfile.id
                        == RuntimeSession.runtime_agent_profile_id,
                    )
                    .join(
                        CloudHermesProfile,
                        CloudHermesProfile.id
                        == RuntimeSession.runtime_workspace_id,
                    )
                    .join(
                        HermesRuntimeBinding,
                        HermesRuntimeBinding.runtime_workspace_id
                        == RuntimeSession.runtime_workspace_id,
                    )
                    .where(ImChannel.id == channel_id)
                )
            ).first()
            if current is None:
                return None
            channel, lease, session, profile, workspace, binding = current
            admission_generation = (
                await load_runtime_command_admission_generation(db)
            )
            valid = all(
                (
                    channel.enabled,
                    channel.target_type == "personal_agent",
                    channel.provider == "wecom",
                    channel.user_id is not None,
                    channel.runtime_workspace_id is not None,
                    channel.runtime_session_row_id == session.id,
                    channel.config_generation == channel_generation,
                    lease.owner_instance_id == gateway_instance_id,
                    int(lease.lease_epoch) == channel_lease_epoch,
                    lease.expires_at > now,
                    session.status == "active",
                    session.source_type == "wecom",
                    session.org_id == channel.org_id,
                    session.user_id == channel.user_id,
                    session.runtime_workspace_id
                    == channel.runtime_workspace_id,
                    profile.status == "active",
                    profile.org_id == channel.org_id,
                    profile.user_id == channel.user_id,
                    profile.runtime_workspace_id
                    == channel.runtime_workspace_id,
                    workspace.status == "active",
                    binding.status == "ready",
                    binding.org_id == channel.org_id,
                    binding.user_id == channel.user_id,
                    binding.runtime_workspace_id
                    == channel.runtime_workspace_id,
                    bool(binding.endpoint_host_header),
                    admission_generation > 0,
                )
            )
            if not valid:
                return None
            visible_models = await SqlAlchemyRuntimeModelStore(
                db
            ).list_visible_models(
                org_id=int(channel.org_id),
                user_id=int(channel.user_id),
            )
            try:
                model_entry_id = resolve_runtime_turn_model_entry_id(
                    requested_model_entry_id=None,
                    session_model_entry_id=(
                        int(session.session_model_entry_id)
                        if session.session_model_entry_id
                        else None
                    ),
                    workspace_default_model_entry_id=(
                        int(workspace.default_model_id)
                        if workspace.default_model_id
                        else None
                    ),
                    visible_models=visible_models,
                )
            except RuntimeTurnModelNotVisibleError as exc:
                raise WeComFastLaneRouteRejected(
                    error_code="MODEL_NOT_VISIBLE",
                    user_message=(
                        "当前选择的模型已不可用，请先在平台"
                        "重新选择模型后再发送。"
                    ),
                ) from exc
            except RuntimeTurnModelUnavailableError as exc:
                raise WeComFastLaneRouteRejected(
                    error_code="MODEL_NOT_AVAILABLE",
                    user_message=(
                        "当前没有可用模型，请先在平台选择模型"
                        "后重新发送。"
                    ),
                ) from exc
            model = (
                await db.execute(
                    select(ModelEntry).where(
                        ModelEntry.id == model_entry_id,
                        ModelEntry.org_id == channel.org_id,
                        ModelEntry.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if model is None:
                return None
            try:
                profile_identity = await resolve_profile_execution_identity(
                    db,
                    org_id=int(channel.org_id),
                    user_id=int(channel.user_id),
                    runtime_workspace_id=int(channel.runtime_workspace_id),
                    runtime_agent_profile_id=int(profile.id),
                    hermes_profile_name=str(profile.hermes_profile_name),
                )
            except ProfileExecutionIdentityError:
                return None
            provider_route = None
            model_context_length = getattr(model, "context_length", None)
            context_length = (
                int(model_context_length)
                if int(model_context_length or 0) > 0
                else 128_000
            )
            if settings.provider_route_issuance_enabled(str(binding.runtime_id)):
                try:
                    resolved_route = await RuntimeProviderRouteResolver(db).resolve(
                        org_id=int(channel.org_id),
                        model_entry_id=model_entry_id,
                        binding=ProviderRouteBindingIdentity(
                            runtime_id=str(binding.runtime_id),
                            binding_type="personal_runtime",
                            binding_id=int(binding.id),
                            pvc_storage_identity_hash=str(
                                binding.pvc_storage_identity_hash or ""
                            ),
                        ),
                    )
                except (RuntimeProviderRouteError, ValueError):
                    return None
                upstream_model_id = resolved_route.upstream_model_id
                context_length = resolved_route.context_length
                runtime_capabilities = resolved_route.runtime_capabilities
                provider_route = resolved_route.provider_route
            else:
                upstream_model_id = str(model.model_key)
                runtime_capabilities = normalize_runtime_capabilities(
                    model.runtime_capabilities
                )
            route_facts = (
                int(channel.org_id),
                int(channel.user_id),
                int(channel.runtime_workspace_id),
                str(session.runtime_session_id),
                str(profile.hermes_profile_name),
                profile_identity,
                model_entry_id,
                upstream_model_id,
                context_length,
                runtime_capabilities,
                provider_route,
                str(binding.runtime_id),
                str(binding.id),
                str(binding.endpoint_host_header),
                int(binding.config_generation or 0),
                str(
                    binding.config_hash
                    or f"generation-{int(binding.config_generation or 0)}"
                ),
                admission_generation,
            )

        (
            org_id,
            user_id,
            runtime_workspace_id,
            runtime_session_id,
            profile_id,
            profile_identity,
            model_entry_id,
            upstream_model_id,
            context_length,
            runtime_capabilities,
            provider_route,
            runtime_id,
            binding_id,
            endpoint_host_header,
            config_generation,
            config_revision,
            admission_generation,
        ) = route_facts
        endpoint = await self.activate_runtime(
            principal=Principal(org_id=org_id, user_id=user_id),
            runtime_workspace_id=runtime_workspace_id,
        )
        if (
            str(endpoint.runtime_id) != runtime_id
            or str(endpoint.endpoint_host_header) != endpoint_host_header
        ):
            return None
        route = PreparedWeComFastLaneRoute(
            target=RuntimeCommandTarget(
                runtime_id=runtime_id,
                binding_id=binding_id,
                endpoint_host_header=endpoint_host_header,
                api_server_key=str(endpoint.api_key),
                admission_generation=admission_generation,
            ),
            identity=RuntimeTurnIdentity(
                user_id=user_id,
                org_id=org_id,
                session_id=runtime_session_id,
                profile_id=profile_id,
                runtime_agent_profile_id=(
                    profile_identity.runtime_agent_profile_id
                ),
                profile_desired_revision=profile_identity.desired_revision,
                profile_applied_revision=profile_identity.applied_revision,
                profile_digest=profile_identity.profile_digest,
                skill_digest=profile_identity.skill_digest,
                mcp_digest=profile_identity.mcp_digest,
                knowledge_digest=profile_identity.knowledge_digest,
                model_id=str(model_entry_id),
                upstream_model_id=upstream_model_id,
                context_length=context_length,
                runtime_capabilities=runtime_capabilities,
                provider_route=provider_route,
                config_revision=config_revision,
                max_prompt_bytes=settings.RUNTIME_EDGE_MAX_PROMPT_BYTES,
                max_output_tokens=settings.RUNTIME_EDGE_MAX_OUTPUT_TOKENS,
                admission_generation=admission_generation,
            ),
            channel_id=channel_id,
            target_type="personal_agent",
            target_id=runtime_workspace_id,
            gateway_instance_id=gateway_instance_id,
            channel_lease_epoch=channel_lease_epoch,
            channel_generation=channel_generation,
            config_generation=config_generation,
            prepared_at=time.monotonic(),
        )
        self._hot_routes[channel_id] = route
        return route

    async def activate_runtime(
        self, *, principal: Principal, runtime_workspace_id: int
    ):
        async with AsyncSessionLocal() as db:
            infrastructure = build_runtime_infrastructure(db)
            profile = (
                await infrastructure.workspace_service.ensure_default_for_user(
                    principal
                )
            )
            if int(profile.runtime_workspace_id) != runtime_workspace_id:
                raise ImRuntimeBridgeError(
                    "runtime workspace ownership mismatch"
                )
            if profile.runtime_status != "ready":
                try:
                    profile = await infrastructure.warmup_runner.warmup(
                        profile
                    )
                except RuntimeAdmissionError as exc:
                    raise ImRuntimeBridgeError(
                        exc.decision.code
                    ) from exc
            if profile.runtime_status != "ready":
                decision = RuntimeAdmissionService.decide(
                    str(profile.runtime_status or "")
                )
                raise ImRuntimeBridgeError(decision.code)
            await infrastructure.default_mcp_bootstrapper.ensure_for_runtime(profile)
            return await infrastructure.workspace_store.get_runtime_endpoint(
                profile
            )

    async def stage_attachments(
        self,
        *,
        queued_channel: Any,
        inbound_message_id: int,
        owner_instance_id: str,
    ) -> str:
        """Download provider URLs first, then wake Runtime and register files."""

        async with AsyncSessionLocal() as db:
            runtime_session_id = (
                await self._current_personal_channel_session_id(
                    db,
                    queued_channel=queued_channel,
                )
            )
            pending = await ImInboundQueue(db).pending_attachments(
                inbound_message_id,
                owner_instance_id=owner_instance_id,
                lease_epoch=int(queued_channel.lease_epoch),
            )
        if not pending:
            async with AsyncSessionLocal() as db:
                finished = await ImInboundQueue(db).finish_staging(
                    inbound_message_id,
                    owner_instance_id=owner_instance_id,
                    lease_epoch=int(queued_channel.lease_epoch),
                )
            return "queued" if finished else "stale"
        downloaded: list[
            tuple[Any, dict[str, Any], Path, str, int, str, str]
        ] = []
        try:
            for attachment, metadata in pending:
                result = await self.node_bridge.download_media(
                    channel_id=int(queued_channel.channel_id),
                    generation=int(queued_channel.config_generation),
                    lease_epoch=int(queued_channel.lease_epoch),
                    url=str(metadata["url"]),
                    aes_key=metadata.get("aes_key"),
                    filename=attachment.display_name,
                )
                local_path = self.node_bridge.require_scoped_path(
                    str(result["path"])
                )
                size = int(result.get("size_bytes") or 0)
                limit = settings.IM_GATEWAY_MEDIA_MAX_MB * 1024 * 1024
                if size < 0 or size > limit:
                    raise ImRuntimeBridgeError(
                        "wecom media exceeds the configured limit"
                    )
                filename = _safe_filename(
                    str(result.get("filename") or local_path.name)
                )
                digest = await asyncio.to_thread(
                    _sha256_file, local_path
                )
                mime_type = await asyncio.to_thread(
                    _detect_mime_type, local_path, filename
                )
                _validate_inbound_media_type(
                    str(attachment.media_type), mime_type
                )
                downloaded.append(
                    (
                        attachment,
                        metadata,
                        local_path,
                        filename,
                        size,
                        digest,
                        mime_type,
                    )
                )
            principal = Principal(
                org_id=int(queued_channel.org_id),
                user_id=int(queued_channel.user_id),
            )
            endpoint = await self.activate_runtime(
                principal=principal,
                runtime_workspace_id=int(
                    queued_channel.runtime_workspace_id
                ),
            )
            for (
                attachment,
                _metadata,
                local_path,
                filename,
                size,
                digest,
                mime_type,
            ) in downloaded:
                relative_path = (
                    "workspace/sessions/"
                    f"{runtime_session_id}/"
                    f"im-inbox/{uuid4().hex[:12]}-{filename}"
                )
                with local_path.open("rb") as source:
                    uploaded = await self.dashboard.upload_managed_artifact(
                        endpoint,
                        relative_path=relative_path,
                        content=source,
                        filename=filename,
                        mime_type=mime_type,
                    )
                async with AsyncSessionLocal() as db:
                    file_record = await ChatFileIndexService(
                        SqlAlchemyChatFileStore(db)
                    ).register_file(
                        principal=principal,
                        command=RegisterChatFileCommand(
                            runtime_workspace_id=int(
                                queued_channel.runtime_workspace_id
                            ),
                            runtime_session_id=runtime_session_id,
                            runtime_run_id=None,
                            relative_path=uploaded.relative_path,
                            mime_type=uploaded.mime_type or mime_type,
                            size_bytes=uploaded.size_bytes or size,
                            source_type="upload",
                        ),
                    )
                    marked = await ImInboundQueue(
                        db
                    ).mark_attachment_ready(
                        attachment_id=int(attachment.id),
                        display_name=filename,
                        mime_type=uploaded.mime_type or mime_type,
                        size_bytes=uploaded.size_bytes or size,
                        sha256=digest,
                        chat_file_artifact_id=file_record.file_id,
                        inbound_message_id=inbound_message_id,
                        owner_instance_id=owner_instance_id,
                        lease_epoch=int(
                            queued_channel.lease_epoch
                        ),
                    )
                    if not marked:
                        raise ImRuntimeBridgeError(
                            "IM queue ownership was lost while staging media"
                        )
            async with AsyncSessionLocal() as db:
                finished = await ImInboundQueue(db).finish_staging(
                    inbound_message_id,
                    owner_instance_id=owner_instance_id,
                    lease_epoch=int(queued_channel.lease_epoch),
                )
            return "queued" if finished else "stale"
        except Exception as exc:
            async with AsyncSessionLocal() as db:
                status = await ImInboundQueue(
                    db
                ).mark_staging_failed(
                    inbound_message_id,
                    owner_instance_id=owner_instance_id,
                    lease_epoch=int(queued_channel.lease_epoch),
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                    max_attempts=(
                        settings.IM_GATEWAY_MAX_MESSAGE_ATTEMPTS
                    ),
                )
            return status
        finally:
            for _attachment, _metadata, local_path, *_rest in downloaded:
                try:
                    self.node_bridge.require_scoped_path(
                        str(local_path)
                    ).unlink(missing_ok=True)
                except Exception:
                    pass

    async def execute(self, queued: QueuedInbound) -> tuple[int, str]:
        principal = Principal(org_id=queued.org_id, user_id=queued.user_id)
        if queued.runtime_run_row_id is not None:
            async with AsyncSessionLocal() as db:
                existing_status = await db.scalar(
                    select(RuntimeRun.status).where(
                        RuntimeRun.id == queued.runtime_run_row_id
                    )
                )
            if str(existing_status or "").lower() == "pending":
                # A prior attempt reserved the deterministic MySQL row but
                # did not cross Supervisor admission. Re-enter submission
                # with the same sealed batch instead of polling a fake Run.
                queued = replace(queued, runtime_run_row_id=None)
            else:
                return await self._recover_existing_run(
                    queued=queued,
                    principal=principal,
                )
        timing = RuntimeChatTiming()

        async def activate() -> Any:
            _timing_mark(timing, "runtime_activate_started")
            try:
                return await self.activate_runtime(
                    principal=principal,
                    runtime_workspace_id=queued.runtime_workspace_id,
                )
            finally:
                _timing_mark(timing, "runtime_activate_done")

        async def build_prompt() -> PreparedWeComTurn:
            _timing_mark(timing, "prompt_build_started")
            try:
                return await self._build_prompt(queued)
            finally:
                _timing_mark(timing, "prompt_build_done")

        activate_task = asyncio.create_task(
            activate(),
            name=(
                "im-personal-runtime-activate-"
                f"{queued.inbound_message_id}"
            ),
        )
        prompt_task = asyncio.create_task(
            build_prompt(),
            name=(
                "im-personal-prompt-build-"
                f"{queued.inbound_message_id}"
            ),
        )
        try:
            endpoint, prepared_turn = await asyncio.gather(
                activate_task,
                prompt_task,
            )
        except BaseException:
            for task in (activate_task, prompt_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                activate_task,
                prompt_task,
                return_exceptions=True,
            )
            raise
        _timing_mark(timing, "attachments_done")
        (
            _synthetic_inbound_id,
            stable_runtime_run_id,
            stable_client_message_id,
            _stable_stream_id,
            _stable_request_id,
        ) = _queued_wecom_turn_identity(queued)
        async with AsyncSessionLocal() as db:
            service = _build_runtime_chat_service(db)
            created = await service.create_run(
                principal=principal,
                intent=RuntimeChatRequestIntent(
                    runtime_workspace_id=queued.runtime_workspace_id,
                    runtime_session_id=await self._runtime_session_id(
                        queued.runtime_session_row_id
                    ),
                    message=prepared_turn.prompt,
                    source_type="wecom",
                    attachment_file_ids=[
                        int(attachment.file_id)
                        for attachment in prepared_turn.attachments
                    ],
                    audit_message=prepared_turn.prompt,
                    channel="wecom",
                    runtime_run_id=stable_runtime_run_id,
                ),
                timing=timing,
            )
            _timing_mark(timing, "run_created")
            runtime_binding = await db.scalar(
                select(HermesRuntimeBinding).where(
                    HermesRuntimeBinding.org_id == principal.org_id,
                    HermesRuntimeBinding.user_id == principal.user_id,
                    HermesRuntimeBinding.runtime_workspace_id
                    == queued.runtime_workspace_id,
                    HermesRuntimeBinding.status == "ready",
                )
            )
            if (
                runtime_binding is None
                or not runtime_binding.endpoint_host_header
            ):
                raise ImRuntimeBridgeError(
                    "personal Runtime command target is unavailable"
                )
            run_row_id = await db.scalar(
                select(RuntimeRun.id).where(
                    RuntimeRun.runtime_run_id
                    == created.runtime_run_id
                )
            )
            if run_row_id is None:
                raise ImRuntimeBridgeError(
                    "created RuntimeRun is unavailable"
                )
        try:
            timing.bind_run(created.runtime_run_id)
            timing.set_metadata(
                "inbound_message_id",
                str(queued.inbound_message_id),
            )
            timing.set_metadata(
                "runtime_session_id",
                created.runtime_session_id,
            )
        except Exception:
            pass
        _log_personal_im_run_timing(
            timing,
            inbound_message_id=queued.inbound_message_id,
            runtime_session_id=created.runtime_session_id,
        )
        async with AsyncSessionLocal() as db:
            bound = await ImInboundQueue(db).bind_runtime_run(
                inbound_message_id=queued.inbound_message_id,
                runtime_run_row_id=int(run_row_id),
                runtime_run_id=created.runtime_run_id,
                owner_instance_id=queued.owner_instance_id,
                lease_epoch=queued.lease_epoch,
            )
        if not bound:
            raise ImRuntimeBridgeError(
                "IM queue ownership was lost before Runtime execution"
            )
        _timing_mark(timing, "im_run_bound")
        if created.reused and str(created.status).lower() in {
            "completed",
            "failed",
            "canceled",
            "cancel_failed",
        }:
            return await self._recover_existing_run(
                queued=replace(
                    queued, runtime_run_row_id=int(run_row_id)
                ),
                principal=principal,
            )

        _log_personal_im_run_timing(
            timing,
            inbound_message_id=queued.inbound_message_id,
            runtime_session_id=created.runtime_session_id,
            stage="runtime_submit_ready",
        )

        def on_runtime_submitted() -> None:
            _schedule_timing_update(
                _persist_runtime_submitted(
                    queued.inbound_message_id,
                    datetime.now(timezone.utc),
                ),
                name=(
                    "im-personal-runtime-submitted-timing-"
                    f"{queued.inbound_message_id}"
                ),
            )

        async with AsyncSessionLocal() as db:
            admission_generation = (
                await load_runtime_command_admission_generation(db)
            )
            provider_route = None
            context_length = 128_000
            if settings.provider_route_issuance_enabled(
                str(runtime_binding.runtime_id)
            ):
                try:
                    resolved_route = await RuntimeProviderRouteResolver(db).resolve(
                        org_id=principal.org_id,
                        model_entry_id=int(created.model_entry_id or 0),
                        binding=ProviderRouteBindingIdentity(
                            runtime_id=str(runtime_binding.runtime_id),
                            binding_type="personal_runtime",
                            binding_id=int(runtime_binding.id),
                            pvc_storage_identity_hash=str(
                                runtime_binding.pvc_storage_identity_hash or ""
                            ),
                        ),
                    )
                except (RuntimeProviderRouteError, ValueError) as exc:
                    raise RuntimeError(
                        "runtime_model_provider_route_unavailable"
                    ) from exc
                upstream_model_id = resolved_route.upstream_model_id
                context_length = resolved_route.context_length
                runtime_capabilities = resolved_route.runtime_capabilities
                provider_route = resolved_route.provider_route
            else:
                model_route = (
                    await db.execute(
                        select(
                            ModelEntry.model_key,
                            ModelEntry.context_length,
                            ModelEntry.runtime_capabilities,
                        ).where(
                            ModelEntry.id == int(created.model_entry_id or 0),
                            ModelEntry.org_id == principal.org_id,
                            ModelEntry.status == "active",
                        )
                    )
                ).one_or_none()
                if model_route is None or not model_route[0]:
                    raise RuntimeError("runtime_model_route_unavailable")
                upstream_model_id = str(model_route[0])
                context_length = (
                    int(model_route[1])
                    if int(model_route[1] or 0) > 0
                    else 128_000
                )
                runtime_capabilities = normalize_runtime_capabilities(
                    model_route[2]
                )
            if (
                any(
                    attachment.mime_type and attachment.mime_type.startswith("image/")
                    for attachment in prepared_turn.attachments
                )
                and not supports_native_vision(runtime_capabilities)
            ):
                raise ImRuntimeBridgeError("MODEL_VISION_UNSUPPORTED")
            try:
                profile_identity = await resolve_profile_execution_identity(
                    db,
                    org_id=principal.org_id,
                    user_id=principal.user_id,
                    runtime_workspace_id=queued.runtime_workspace_id,
                    runtime_agent_profile_id=created.runtime_agent_profile_id,
                    hermes_profile_name=str(
                        created.hermes_profile_name or "default"
                    ),
                )
            except ProfileExecutionIdentityError as exc:
                raise RuntimeError("runtime_profile_unavailable") from exc
        command_target = RuntimeCommandTarget(
            runtime_id=str(runtime_binding.runtime_id),
            binding_id=str(runtime_binding.id),
            endpoint_host_header=str(runtime_binding.endpoint_host_header),
            api_server_key=str(endpoint.api_key),
            admission_generation=admission_generation,
        )
        channel_lease = issue_channel_execution_lease(
            target=command_target,
            org_id=principal.org_id,
            channel_id=queued.channel_id,
            target_type="personal_agent",
            target_id=queued.runtime_workspace_id,
            subject_user_id=principal.user_id,
            runtime_session_id=created.runtime_session_id,
            model_entry_id=str(created.model_entry_id or "default"),
            profile_id=(
                created.hermes_profile_name
                if created.hermes_profile_name != "default"
                else "default"
            ),
            gateway_instance_id=queued.owner_instance_id,
            channel_lease_epoch=queued.lease_epoch,
            channel_generation=queued.generation,
            config_generation=int(runtime_binding.config_generation or 0),
        )
        command_payload = InternalTurnCommandPayload(
            source="personal_wecom",
            run_id=created.runtime_run_id,
            client_message_id=(
                stable_client_message_id
            ),
            identity=RuntimeTurnIdentity(
                user_id=principal.user_id,
                org_id=principal.org_id,
                session_id=created.runtime_session_id,
                profile_id=(
                    created.hermes_profile_name
                    if created.hermes_profile_name != "default"
                    else "default"
                ),
                runtime_agent_profile_id=(
                    profile_identity.runtime_agent_profile_id
                ),
                profile_desired_revision=profile_identity.desired_revision,
                profile_applied_revision=profile_identity.applied_revision,
                profile_digest=profile_identity.profile_digest,
                skill_digest=profile_identity.skill_digest,
                mcp_digest=profile_identity.mcp_digest,
                knowledge_digest=profile_identity.knowledge_digest,
                model_id=str(created.model_entry_id or "default"),
                upstream_model_id=upstream_model_id,
                context_length=context_length,
                runtime_capabilities=runtime_capabilities,
                provider_route=provider_route,
                config_revision=str(
                    runtime_binding.config_hash
                    or (
                        "generation-"
                        f"{int(runtime_binding.config_generation or 0)}"
                    )
                ),
                max_prompt_bytes=settings.RUNTIME_EDGE_MAX_PROMPT_BYTES,
                max_output_tokens=settings.RUNTIME_EDGE_MAX_OUTPUT_TOKENS,
                admission_generation=admission_generation,
            ),
            prompt=prepared_turn.prompt,
            attachments=list(prepared_turn.attachments),
            channel_execution_lease=channel_lease,
        )
        hot_routes = getattr(self, "_hot_routes", None)
        if hot_routes is None:
            hot_routes = {}
            self._hot_routes = hot_routes
        hot_routes[queued.channel_id] = PreparedWeComFastLaneRoute(
            target=command_target,
            identity=command_payload.identity,
            channel_id=queued.channel_id,
            target_type="personal_agent",
            target_id=queued.runtime_workspace_id,
            gateway_instance_id=queued.owner_instance_id,
            channel_lease_epoch=queued.lease_epoch,
            channel_generation=queued.generation,
            config_generation=int(runtime_binding.config_generation or 0),
            prepared_at=time.monotonic(),
        )
        envelope = build_runtime_command(
            target=command_target,
            command_id=deterministic_command_id(
                "personal-wecom",
                runtime_binding.id,
                queued.inbound_message_id,
            ),
            payload=command_payload,
            deadline_seconds=600,
        )
        async def delivery_ack_sink(
            highest_contiguous_seq: int,
            confirmed_at: datetime,
        ) -> None:
            await acknowledge_channel_delivery(
                target=command_target,
                lease=channel_lease,
                run_id=created.runtime_run_id,
                highest_contiguous_seq=highest_contiguous_seq,
                provider_request_id=queued.provider_request_id,
                confirmed_at=confirmed_at,
            )

        projector = self._projector(
            queued,
            delivery_ack_sink=delivery_ack_sink,
        )

        def on_first_public_event(_event: PlatformStreamEvent) -> None:
            _schedule_timing_update(
                _persist_first_runtime_public_event(
                    queued.inbound_message_id,
                    datetime.now(timezone.utc),
                ),
                name=(
                    "im-personal-first-event-timing-"
                    f"{queued.inbound_message_id}"
                ),
            )

        event_pump = _WeComRuntimeEventPump(
            projector=projector,
            task_name=(
                "im-personal-project-"
                f"{queued.inbound_message_id}"
            ),
            first_public_sink=on_first_public_event,
        )
        await event_pump.start()
        _timing_mark(timing, "projector_loaded")
        command_client = RuntimeChannelFastLaneClient()

        async def on_runtime_event(
            event_name: str,
            event_data: dict[str, object],
        ) -> None:
            await event_pump.emit(
                PlatformStreamEvent(
                    event=event_name,
                    data=event_data,
                )
            )

        try:
            ack = await command_client.send(
                target=command_target,
                envelope=envelope,
            )
            if (
                ack.state != "accepted"
                or ack.run_id != created.runtime_run_id
            ):
                raise ImRuntimeBridgeError(
                    ack.error_code or "personal Runtime rejected the message"
                )
            async with AsyncSessionLocal() as db:
                accepted = await ImInboundQueue(db).mark_batch_accepted(
                    inbound_message_id=queued.inbound_message_id,
                    owner_instance_id=queued.owner_instance_id,
                    lease_epoch=queued.lease_epoch,
                )
            if not accepted:
                raise ImRuntimeBridgeError(
                    "IM batch ownership was lost after Runtime acceptance"
                )
            on_runtime_submitted()
            snapshot = await command_client.wait_run(
                target=command_target,
                run_id=created.runtime_run_id,
                event_sink=on_runtime_event,
                timeout_seconds=600,
            )
        finally:
            try:
                await event_pump.drain()
            finally:
                await command_client.close()
        state = str(snapshot.get("status") or "").upper()
        status = (
            "completed"
            if state == "COMPLETED"
            else "canceled"
            if state == "CANCELED"
            else "failed"
        )
        snapshot_answer = str(snapshot.get("answer") or "")
        if status == "completed":
            final_text = snapshot_answer
        elif status == "canceled":
            async with AsyncSessionLocal() as db:
                partial_text = await db.scalar(
                    select(ImDeliveryCheckpoint.message_text).where(
                        ImDeliveryCheckpoint.inbound_message_id
                        == queued.inbound_message_id
                    )
                )
            preserved = snapshot_answer or str(partial_text or "")
            final_text = (
                f"{preserved}\n\n本轮已被新消息打断。"
                if preserved
                else "本轮已被新消息打断。"
            )
        else:
            final_text = str(
                snapshot.get("error_message")
                or "Agent 处理失败，请稍后重试。"
            )
        async with AsyncSessionLocal() as db:
            queue = ImInboundQueue(db)
            await queue.mark_batch_runtime_terminal(
                inbound_message_id=queued.inbound_message_id,
                terminal_status=status,
                owner_instance_id=queued.owner_instance_id,
                lease_epoch=queued.lease_epoch,
            )
            stream_id = await queue.mark_ready_to_deliver(
                inbound_message_id=queued.inbound_message_id,
                final_text=final_text,
                runtime_run_id=created.runtime_run_id,
                runtime_run_row_id=int(run_row_id),
                owner_instance_id=queued.owner_instance_id,
                lease_epoch=queued.lease_epoch,
            )
        if stream_id is None:
            raise ImRuntimeBridgeError(
                "IM queue ownership was lost before terminal delivery"
            )
        if stream_id != queued.stream_id:
            raise ImRuntimeBridgeError(
                "WeCom stream identity changed before terminal delivery"
            )
        delivery_queued = replace(queued, stream_id=stream_id)
        delivery_status = await projector.deliver_final(final_text)
        if delivery_status != "delivered":
            return int(run_row_id), delivery_status
        if status == "completed":
            await self._deliver_run_artifacts(
                principal=principal,
                endpoint=endpoint,
                runtime_run_row_id=int(run_row_id),
                queued=delivery_queued,
            )
        return int(run_row_id), status

    async def _recover_existing_run(
        self,
        *,
        queued: QueuedInbound,
        principal: Principal,
    ) -> tuple[int, str]:
        run_row_id = int(queued.runtime_run_row_id or 0)
        while True:
            async with AsyncSessionLocal() as db:
                run = await db.scalar(
                    select(RuntimeRun).where(
                        RuntimeRun.id == run_row_id,
                        RuntimeRun.org_id == principal.org_id,
                        RuntimeRun.user_id == principal.user_id,
                        RuntimeRun.runtime_workspace_id
                        == queued.runtime_workspace_id,
                        RuntimeRun.session_id
                        == queued.runtime_session_row_id,
                        RuntimeRun.source_type == "wecom",
                    )
                )
                if run is None:
                    raise ImRuntimeBridgeError(
                        "claimed WeCom Runtime Run is unavailable"
                    )
                status = str(run.status or "failed").lower()
                error_message = run.error_message
                answer = str(
                    (run.response_summary or {}).get("answer") or ""
                )
                runtime_run_id = str(run.runtime_run_id)
            if status in {
                "completed",
                "failed",
                "canceled",
                "cancel_failed",
            }:
                if status == "completed":
                    final_text = answer
                elif status == "canceled":
                    final_text = (
                        f"{answer}\n\n本轮已被新消息打断。"
                        if answer
                        else "本轮已被新消息打断。"
                    )
                else:
                    final_text = str(
                        error_message
                        or "Agent 处理失败，请稍后重试。"
                    )
                async with AsyncSessionLocal() as db:
                    queue = ImInboundQueue(db)
                    await queue.mark_batch_runtime_terminal(
                        inbound_message_id=queued.inbound_message_id,
                        terminal_status=status,
                        owner_instance_id=queued.owner_instance_id,
                        lease_epoch=queued.lease_epoch,
                    )
                    stream_id = await queue.mark_ready_to_deliver(
                        inbound_message_id=(
                            queued.inbound_message_id
                        ),
                        final_text=final_text,
                        runtime_run_id=runtime_run_id,
                        runtime_run_row_id=run_row_id,
                        owner_instance_id=(
                            queued.owner_instance_id
                        ),
                        lease_epoch=queued.lease_epoch,
                    )
                if stream_id is None:
                    raise ImRuntimeBridgeError(
                        "IM queue ownership was lost during recovery"
                    )
                delivery_queued = replace(
                    queued, stream_id=stream_id
                )
                projector = self._projector(delivery_queued)
                await projector.load()
                delivery_status = await projector.deliver_final(
                    final_text
                )
                if (
                    delivery_status == "delivered"
                    and status == "completed"
                ):
                    endpoint = await self.activate_runtime(
                        principal=principal,
                        runtime_workspace_id=(
                            queued.runtime_workspace_id
                        ),
                    )
                    await self._deliver_run_artifacts(
                        principal=principal,
                        endpoint=endpoint,
                        runtime_run_row_id=run_row_id,
                        queued=delivery_queued,
                    )
                if delivery_status != "delivered":
                    return run_row_id, delivery_status
                return (
                    run_row_id,
                    "completed"
                    if status == "completed"
                    else "failed",
                )
            await asyncio.sleep(
                max(1.0, settings.IM_GATEWAY_QUEUE_POLL_SECONDS)
            )

    def _projector(
        self,
        queued: QueuedInbound,
        *,
        delivery_ack_sink: Callable[[int, datetime], Any] | None = None,
    ) -> WeComStreamProjector:
        return WeComStreamProjector(
            bridge=self.node_bridge,
            identity=ProjectorIdentity(
                inbound_message_id=queued.inbound_message_id,
                channel_id=queued.channel_id,
                generation=queued.generation,
                owner_instance_id=queued.owner_instance_id,
                lease_epoch=queued.lease_epoch,
                provider_request_id=queued.provider_request_id,
                provider_stream_id=queued.stream_id,
                chat_id=queued.external_conversation_id,
                target_type="personal_agent",
            ),
            delivery_ack_sink=delivery_ack_sink,
        )

    async def _current_personal_channel_session_id(
        self,
        db: Any,
        *,
        queued_channel: Any,
    ) -> str:
        current = (
            await db.execute(
                select(ImChannel, RuntimeSession)
                .join(
                    RuntimeSession,
                    RuntimeSession.id
                    == ImChannel.runtime_session_row_id,
                )
                .where(ImChannel.id == int(queued_channel.channel_id))
            )
        ).first()
        if current is None:
            raise ImRuntimeBridgeError(
                "fixed WeCom Runtime Session is unavailable"
            )
        channel, session = current
        if (
            channel.target_type != "personal_agent"
            or channel.provider != "wecom"
            or channel.runtime_session_row_id != session.id
            or session.source_type != "wecom"
            or session.status != "active"
            or channel.org_id != session.org_id
            or channel.org_id != int(queued_channel.org_id)
            or channel.user_id is None
            or queued_channel.user_id is None
            or channel.user_id != session.user_id
            or channel.user_id != int(queued_channel.user_id)
            or channel.runtime_workspace_id is None
            or queued_channel.runtime_workspace_id is None
            or channel.runtime_workspace_id
            != session.runtime_workspace_id
            or channel.runtime_workspace_id
            != int(queued_channel.runtime_workspace_id)
        ):
            raise ImRuntimeBridgeError(
                "fixed WeCom Runtime Session invariant is broken"
            )
        return str(session.runtime_session_id)

    async def _runtime_session_id(self, session_row_id: int) -> str:
        async with AsyncSessionLocal() as db:
            value = await db.scalar(
                select(RuntimeSession.runtime_session_id).where(
                    RuntimeSession.id == session_row_id,
                    RuntimeSession.source_type == "wecom",
                    RuntimeSession.status == "active",
                )
            )
        if not value:
            raise ImRuntimeBridgeError(
                "fixed WeCom Runtime Session is unavailable"
            )
        return str(value)

    async def _build_prompt(self, queued: QueuedInbound) -> PreparedWeComTurn:
        text = str(queued.normalized_content.get("text") or "")
        batch_messages = queued.batch_messages
        inbound_ids = [
            message.inbound_message_id for message in batch_messages
        ] or [queued.inbound_message_id]
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(
                        ImMessageAttachment,
                        ChatFileArtifact,
                    )
                    .join(
                        ChatFileArtifact,
                        ChatFileArtifact.id
                        == ImMessageAttachment.chat_file_artifact_id,
                    )
                    .where(
                        ImMessageAttachment.inbound_message_id.in_(
                            inbound_ids
                        ),
                        ImMessageAttachment.staging_status == "ready",
                        ChatFileArtifact.status == "active",
                    )
                    .order_by(ImMessageAttachment.ordinal)
                )
            ).all()
        runtime_attachments = tuple(
            RuntimeTurnAttachment(
                file_id=str(artifact.id),
                display_name=str(
                    attachment.display_name
                    or Path(artifact.relative_path).name
                ),
                relative_path=str(artifact.relative_path),
                mime_type=attachment.mime_type,
            )
            for attachment, artifact in rows
        )
        original_text = text.strip()
        if len(batch_messages) > 1:
            attachments: dict[int, list[dict[str, str]]] = {}
            for attachment, artifact in rows:
                attachments.setdefault(
                    int(attachment.inbound_message_id), []
                ).append(
                    _wecom_batch_attachment_entry(
                        display_name=str(
                            attachment.display_name
                            or Path(artifact.relative_path).name
                        ),
                        relative_path=str(artifact.relative_path),
                        mime_type=attachment.mime_type,
                    ),
                )
            prompt = _format_wecom_batch_prompt(
                batch_messages,
                attachments=attachments,
            ) + _wecom_platform_attachments_contract(runtime_attachments)
            return PreparedWeComTurn(
                prompt=prompt,
                attachments=runtime_attachments,
            )
        if queued.quote_payload:
            import json

            text += (
                "\n\n<wecom_quote>\n"
                + json.dumps(
                    queued.quote_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n</wecom_quote>"
            )
        has_image = any(
            attachment.mime_type and attachment.mime_type.startswith("image/")
            for attachment, _artifact in rows
        )
        if has_image and original_text == "[企微图片附件]":
            text = "请识别这些图片" if len(runtime_attachments) > 1 else "请识别这张图片"
        text += _wecom_platform_attachments_contract(runtime_attachments)
        return PreparedWeComTurn(
            prompt=text,
            attachments=runtime_attachments,
        )

    async def _deliver_run_artifacts(
        self,
        *,
        principal: Principal,
        endpoint: Any,
        runtime_run_row_id: int,
        queued: QueuedInbound,
    ) -> None:
        async with AsyncSessionLocal() as db:
            checkpoint = await db.scalar(
                select(ImDeliveryCheckpoint).where(
                    ImDeliveryCheckpoint.inbound_message_id
                    == queued.inbound_message_id
                )
            )
            if checkpoint is None:
                raise ImRuntimeBridgeError(
                    "WeCom delivery checkpoint is unavailable"
                )
            delivered_ids = {
                int(value)
                for value in (
                    checkpoint.delivered_artifact_ids or []
                )
                if isinstance(value, int)
                or (isinstance(value, str) and value.isdigit())
            }
            artifacts = (
                await db.execute(
                    select(ChatFileArtifact).where(
                        ChatFileArtifact.run_id
                        == runtime_run_row_id,
                        ChatFileArtifact.org_id == principal.org_id,
                        ChatFileArtifact.user_id == principal.user_id,
                        ChatFileArtifact.status == "active",
                    )
                )
            ).scalars().all()
        failures: list[str] = []
        for artifact in artifacts:
            if int(artifact.id) in delivered_ids:
                continue
            filename = _safe_filename(
                Path(artifact.relative_path).name
            )
            if self.node_bridge.temp_root is None:
                raise ImRuntimeBridgeError(
                    "WeCom bridge temp root is unavailable"
                )
            target = self.node_bridge.require_scoped_path(
                str(
                    self.node_bridge.temp_root
                    / f"out-{uuid4().hex}-{filename}"
                )
            )
            try:
                with target.open("wb") as destination:
                    async for chunk in self.dashboard.stream_managed_artifact(
                        endpoint,
                        relative_path=artifact.relative_path,
                    ):
                        destination.write(chunk)
                await self.node_bridge.upload_media(
                    passive=False,
                    channel_id=queued.channel_id,
                    generation=queued.generation,
                    lease_epoch=queued.lease_epoch,
                    media_type=_outbound_media_type(
                        artifact.mime_type, filename
                    ),
                    source_path=target,
                    filename=filename,
                    chat_id=queued.external_conversation_id,
                )
                async with AsyncSessionLocal() as db:
                    marked = await ImInboundQueue(
                        db
                    ).mark_artifact_delivered(
                        inbound_message_id=queued.inbound_message_id,
                        artifact_id=int(artifact.id),
                        owner_instance_id=queued.owner_instance_id,
                        lease_epoch=queued.lease_epoch,
                    )
                if not marked:
                    raise ImRuntimeBridgeError(
                        "IM queue ownership was lost after media delivery"
                    )
            except Exception as exc:
                # The official SDK has a hard ~50 MB upload ceiling. For this
                # permanent case, send a clear text fallback and consider the
                # artifact accounted for; it remains downloadable in WebUI.
                if (
                    isinstance(exc, WeComBridgeError)
                    and exc.code == "MEDIA_TOO_LARGE"
                ):
                    try:
                        await self.node_bridge.send_markdown(
                            channel_id=queued.channel_id,
                            generation=queued.generation,
                            lease_epoch=queued.lease_epoch,
                            chat_id=queued.external_conversation_id,
                            content=(
                                f"附件 **{filename}** 超出企微 50 MB "
                                "发送上限，请在 WebUI 的企微固定会话中下载。"
                            ),
                        )
                        async with AsyncSessionLocal() as db:
                            marked = await ImInboundQueue(
                                db
                            ).mark_artifact_delivered(
                                inbound_message_id=(
                                    queued.inbound_message_id
                                ),
                                artifact_id=int(artifact.id),
                                owner_instance_id=(
                                    queued.owner_instance_id
                                ),
                                lease_epoch=queued.lease_epoch,
                            )
                        if marked:
                            continue
                    except Exception:
                        pass
                failures.append(type(exc).__name__)
                async with AsyncSessionLocal() as db:
                    await ImInboundQueue(
                        db
                    ).mark_artifact_delivery_failed(
                        inbound_message_id=queued.inbound_message_id,
                        owner_instance_id=queued.owner_instance_id,
                        lease_epoch=queued.lease_epoch,
                        error_code=type(exc).__name__,
                        error_message=(
                            "Agent 生成附件暂未投递到企微，系统将自动重试"
                        ),
                    )
            finally:
                target.unlink(missing_ok=True)
        if failures:
            raise ImRuntimeBridgeError(
                "WeCom artifact delivery is pending retry"
            )
        async with AsyncSessionLocal() as db:
            cleared = await ImInboundQueue(
                db
            ).clear_artifact_delivery_failure(
                inbound_message_id=queued.inbound_message_id,
                owner_instance_id=queued.owner_instance_id,
                lease_epoch=queued.lease_epoch,
            )
        if not cleared:
            raise ImRuntimeBridgeError(
                "IM queue ownership was lost after artifact delivery"
            )


class ApplicationImRuntimeBridge:
    """Trusted multi-user adapter from IM work items to ApplicationAgentRun."""

    def __init__(self, *, node_bridge: WeComNodeBridge) -> None:
        self.node_bridge = node_bridge
        self.dashboard = build_runtime_protocol_adapters().dashboard_adapter
        self._hot_routes: dict[
            tuple[int, str, int], PreparedWeComFastLaneRoute
        ] = {}

    def hot_route(
        self,
        *,
        channel_id: int,
        external_conversation_id: str,
        platform_user_id: int,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        channel_generation: int,
    ) -> PreparedWeComFastLaneRoute | None:
        route = getattr(self, "_hot_routes", {}).get(
            (channel_id, external_conversation_id, platform_user_id)
        )
        if route is None or not route.is_current(
            channel_id=channel_id,
            gateway_instance_id=gateway_instance_id,
            channel_lease_epoch=channel_lease_epoch,
            channel_generation=channel_generation,
        ):
            return None
        return route

    def invalidate_channel(self, channel_id: int) -> None:
        hot_routes = getattr(self, "_hot_routes", {})
        for key in tuple(hot_routes):
            if key[0] == channel_id:
                hot_routes.pop(key, None)

    async def prepare_fast_lane_route(
        self,
        *,
        inbound_message_id: int,
        channel_id: int,
        external_conversation_id: str,
        platform_user_id: int,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        channel_generation: int,
    ) -> PreparedWeComFastLaneRoute | None:
        """Prepare the current application route without creating a Run."""

        runtime_run_id = _durable_hot_runtime_run_id(
            target_type="application_agent",
            channel_id=channel_id,
            inbound_message_id=inbound_message_id,
        )
        try:
            async with AsyncSessionLocal() as db:
                message = await db.get(
                    ImInboundMessage,
                    inbound_message_id,
                )
                channel = await db.get(ImChannel, channel_id)
                if (
                    message is None
                    or channel is None
                    or int(message.channel_id) != channel_id
                    or message.target_type != "application_agent"
                    or channel.target_type != "application_agent"
                    or int(channel.application_agent_id or 0)
                    != int(message.application_agent_id or 0)
                    or not channel.enabled
                    or channel.status != "connected"
                    or int(channel.config_generation)
                    != channel_generation
                    or message.dispatch_lane != "fast_lane_v2"
                    or message.ingress_ticket_state
                    not in {"pending", "retry_wait"}
                    or message.runtime_disposition_seq is not None
                    or message.runtime_canonical_run_id is not None
                    or int(message.sender_platform_user_id or 0)
                    != platform_user_id
                    or str(message.external_conversation_id or "")
                    != external_conversation_id
                    or message.application_agent_id is None
                    or channel.application_client_id is None
                ):
                    return None
                conversation_key = _application_conversation_key_values(
                    application_agent_id=int(
                        message.application_agent_id
                    ),
                    channel_id=channel_id,
                    chat_type=str(message.chat_type or "direct"),
                    external_conversation_id=external_conversation_id,
                    platform_user_id=platform_user_id,
                )
                plan = await SqlAlchemyApplicationAgentInvocationService(
                    db,
                    prepared_knowledge_service=(
                        shared_runtime_prepared_knowledge_service()
                    ),
                ).prepare_channel_fast_lane(
                    application_agent_id=int(
                        message.application_agent_id
                    ),
                    application_client_id=int(
                        channel.application_client_id
                    ),
                    delegated_principal=Principal(
                        org_id=int(channel.org_id),
                        user_id=platform_user_id,
                    ),
                    conversation_key=conversation_key,
                )
            key = (
                channel_id,
                external_conversation_id,
                platform_user_id,
            )
            cached = getattr(self, "_hot_routes", {}).get(key)
            if (
                cached is not None
                and cached.is_current(
                    channel_id=channel_id,
                    gateway_instance_id=gateway_instance_id,
                    channel_lease_epoch=channel_lease_epoch,
                    channel_generation=channel_generation,
                )
                and cached.config_generation == plan.config_generation
                and cached.target == plan.target
                and cached.identity == plan.identity
            ):
                return cached
            await shared_runtime_prepared_knowledge_service().prepare_application_agent_channel(
                application_agent_id=plan.application_agent_id,
                application_client_id=plan.application_client_id,
                subject_user_id=plan.subject_user_id,
                runtime_session_id=plan.runtime_session_id,
                runtime_run_id=runtime_run_id,
                runtime_token=str(plan.target.api_server_key),
            )
            route = PreparedWeComFastLaneRoute(
                target=plan.target,
                identity=plan.identity,
                channel_id=channel_id,
                target_type="application_agent",
                target_id=plan.application_agent_id,
                gateway_instance_id=gateway_instance_id,
                channel_lease_epoch=channel_lease_epoch,
                channel_generation=channel_generation,
                config_generation=plan.config_generation,
                prepared_at=time.monotonic(),
            )
        except ApplicationAgentInvocationError:
            return None
        except Exception:
            _logger.exception(
                "Application WeCom Fast Lane route preparation failed",
                extra={"inbound_message_id": inbound_message_id},
            )
            return None
        self._hot_routes[key] = route
        return route

    async def prepare_fast_lane_recovery_route(
        self,
        *,
        inbound_message_id: int,
        channel_id: int,
        external_conversation_id: str,
        platform_user_id: int,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        channel_generation: int,
        runtime_canonical_run_id: str,
    ) -> PreparedWeComFastLaneRoute | None:
        """Rebuild only the transport identity for a durable delivery."""

        async with AsyncSessionLocal() as db:
            message = await db.get(ImInboundMessage, inbound_message_id)
            if (
                message is None
                or int(message.channel_id) != channel_id
                or message.target_type != "application_agent"
                or message.dispatch_lane != "fast_lane_v2"
                or message.ingress_ticket_state != "runtime_durable"
                or int(message.runtime_disposition_seq or 0) <= 0
                or str(message.runtime_canonical_run_id or "")
                != runtime_canonical_run_id
                or int(message.sender_platform_user_id or 0)
                != platform_user_id
                or str(message.external_conversation_id or "")
                != external_conversation_id
                or message.application_agent_id is None
            ):
                return None
            current = (
                await db.execute(
                    select(
                        ApplicationAgent,
                        ApplicationAgentRuntimeBinding,
                        ApplicationAgentRun,
                    )
                    .join(
                        ApplicationAgentRuntimeBinding,
                        ApplicationAgentRuntimeBinding.application_agent_id
                        == ApplicationAgent.id,
                    )
                    .join(
                        ApplicationAgentRun,
                        ApplicationAgentRun.application_agent_id
                        == ApplicationAgent.id,
                    )
                    .where(
                        ApplicationAgent.id
                        == int(message.application_agent_id),
                        ApplicationAgent.status == "published",
                        ApplicationAgent.apply_status == "applied",
                        ApplicationAgentRuntimeBinding.status == "ready",
                        ApplicationAgentRun.runtime_run_id
                        == runtime_canonical_run_id,
                        ApplicationAgentRun.runtime_binding_id
                        == ApplicationAgentRuntimeBinding.id,
                        ApplicationAgentRun.subject_type == "user",
                        ApplicationAgentRun.subject_user_id
                        == platform_user_id,
                        ApplicationAgentRun.channel == "wecom",
                    )
                )
            ).first()
            if current is None:
                return None
            agent, binding, run = current
            model_entry_id = int(
                (agent.config or {}).get("model_entry_id") or 0
            )
            if model_entry_id <= 0:
                return None
            model = (
                await db.execute(
                    select(
                        ModelEntry.model_key,
                        ModelEntry.context_length,
                        ModelEntry.runtime_capabilities,
                    ).where(
                        ModelEntry.id == model_entry_id,
                        ModelEntry.org_id == agent.org_id,
                        ModelEntry.status == "active",
                    )
                )
            ).one_or_none()
            if model is None or not model[0]:
                return None
            admission_generation = (
                await load_runtime_command_admission_generation(db)
            )
            try:
                api_server_key = RuntimeApiKeyCodec().decrypt(
                    str(binding.api_key_encrypted)
                )
            except ValueError:
                return None
            config_generation = max(
                int(agent.config_generation or 0),
                int(binding.config_generation or 0),
            )
            profile_digest, skill_digest, mcp_digest, knowledge_digest = (
                application_asset_digests(dict(agent.config or {}))
            )
            route = PreparedWeComFastLaneRoute(
                target=RuntimeCommandTarget(
                    runtime_id=str(binding.runtime_id),
                    binding_id=str(binding.id),
                    endpoint_host_header=str(
                        binding.endpoint_host_header
                    ),
                    api_server_key=api_server_key,
                    admission_generation=admission_generation,
                ),
                identity=RuntimeTurnIdentity(
                    user_id=platform_user_id,
                    org_id=int(agent.org_id),
                    session_id=str(run.runtime_session_id),
                    profile_id=f"application-{int(agent.id)}",
                    runtime_agent_profile_id=0,
                    application_agent_id=int(agent.id),
                    profile_desired_revision=int(agent.config_generation or 0),
                    profile_applied_revision=int(binding.config_generation or 0),
                    profile_digest=profile_digest,
                    skill_digest=skill_digest,
                    mcp_digest=mcp_digest,
                    knowledge_digest=knowledge_digest,
                    model_id=str(model_entry_id),
                    upstream_model_id=str(model[0]),
                    context_length=(
                        int(model[1]) if int(model[1] or 0) > 0 else 128_000
                    ),
                    runtime_capabilities=(
                        normalize_runtime_capabilities(model[1])
                    ),
                    config_revision=f"generation-{config_generation}",
                    max_prompt_bytes=(
                        settings.RUNTIME_EDGE_MAX_PROMPT_BYTES
                    ),
                    max_output_tokens=(
                        settings.RUNTIME_EDGE_MAX_OUTPUT_TOKENS
                    ),
                    admission_generation=admission_generation,
                ),
                channel_id=channel_id,
                target_type="application_agent",
                target_id=int(agent.id),
                gateway_instance_id=gateway_instance_id,
                channel_lease_epoch=channel_lease_epoch,
                channel_generation=channel_generation,
                config_generation=config_generation,
                prepared_at=time.monotonic(),
            )
        self._hot_routes[
            (
                channel_id,
                external_conversation_id,
                platform_user_id,
            )
        ] = route
        return route

    async def execute(self, queued: QueuedInbound) -> tuple[int, str]:
        if (
            queued.application_agent_id is None
            or queued.application_client_id is None
            or queued.sender_platform_user_id is None
        ):
            raise ImRuntimeBridgeError(
                "application IM identity or grant is unavailable"
            )
        if queued.application_agent_run_id is not None:
            async with AsyncSessionLocal() as db:
                existing = await db.get(
                    ApplicationAgentRun,
                    int(queued.application_agent_run_id),
                )
            if (
                existing is None
                or str(existing.submission_state or "") != "reserved"
                or existing.submit_intent_at is not None
            ):
                return await self._recover_existing_run(queued)

        timing = RuntimeChatTiming()
        run_row_id: int | None = None
        endpoint: Any | None = None
        runtime_session_id: str | None = None
        input_paths: set[str] = set()
        fast_lane_target: RuntimeCommandTarget | None = None
        fast_lane_lease: Any | None = None
        fast_lane_run_id: str | None = None
        started_wall_time = time.time()

        async def delivery_ack_sink(
            highest_contiguous_seq: int,
            confirmed_at: datetime,
        ) -> None:
            if (
                fast_lane_target is None
                or fast_lane_lease is None
                or fast_lane_run_id is None
            ):
                return
            await acknowledge_channel_delivery(
                target=fast_lane_target,
                lease=fast_lane_lease,
                run_id=fast_lane_run_id,
                highest_contiguous_seq=highest_contiguous_seq,
                provider_request_id=queued.provider_request_id,
                confirmed_at=confirmed_at,
            )

        projector = WeComStreamProjector(
            bridge=self.node_bridge,
            identity=ProjectorIdentity(
                inbound_message_id=queued.inbound_message_id,
                channel_id=queued.channel_id,
                generation=queued.generation,
                owner_instance_id=queued.owner_instance_id,
                lease_epoch=queued.lease_epoch,
                provider_request_id=queued.provider_request_id,
                provider_stream_id=queued.stream_id,
                chat_id=queued.external_conversation_id,
                target_type="application_agent",
            ),
            delivery_ack_sink=delivery_ack_sink,
        )

        def on_first_public_event(_event: PlatformStreamEvent) -> None:
            _schedule_timing_update(
                _persist_first_runtime_public_event(
                    queued.inbound_message_id,
                    datetime.now(timezone.utc),
                ),
                name=(
                    "im-app-first-event-timing-"
                    f"{queued.inbound_message_id}"
                ),
            )
            _timing_mark(timing, "first_runtime_public_event")
            _log_application_im_timing(
                timing,
                stage="first_runtime_public_event",
                status="success",
                inbound_message_id=queued.inbound_message_id,
                runtime_session_id=runtime_session_id,
            )

        event_pump = _WeComRuntimeEventPump(
            projector=projector,
            task_name=(
                "im-app-project-"
                f"{queued.inbound_message_id}"
            ),
            first_public_sink=on_first_public_event,
        )
        await event_pump.start()
        _timing_mark(timing, "projector_loaded")

        async def on_event(event: PlatformStreamEvent) -> None:
            await event_pump.emit(event)

        async def on_run_created(run: ApplicationAgentRun) -> None:
            nonlocal run_row_id
            run_row_id = int(run.id)
            async with AsyncSessionLocal() as db:
                bound = await ImInboundQueue(
                    db
                ).bind_application_run(
                    inbound_message_id=queued.inbound_message_id,
                    application_agent_run_id=run_row_id,
                    runtime_run_id=str(run.runtime_run_id),
                    owner_instance_id=queued.owner_instance_id,
                    lease_epoch=queued.lease_epoch,
                )
            if not bound:
                raise ImRuntimeBridgeError(
                    "IM queue ownership was lost before application execution"
                )
            _timing_mark(timing, "im_run_bound")
            _log_application_im_timing(
                timing,
                stage="im_run_bound",
                status="success",
                inbound_message_id=queued.inbound_message_id,
                runtime_session_id=runtime_session_id,
            )

        async def prepare_session(
            plan: ApplicationChannelSessionPlan,
        ) -> PreparedApplicationChannelInput:
            nonlocal endpoint, runtime_session_id, input_paths
            endpoint = plan.endpoint
            runtime_session_id = plan.runtime_session_id
            _timing_mark(timing, "session_materialization_started")
            prepared_input, input_paths = await self._stage_and_build_prompt(
                queued=queued,
                endpoint=plan.endpoint,
                runtime_session_id=plan.runtime_session_id,
            )
            _timing_mark(timing, "session_materialized")
            try:
                timing.set_metadata(
                    "session_materialization",
                    "supervisor_internal_turn",
                )
            except Exception:
                pass
            _timing_mark(timing, "session_import_done")
            _timing_mark(timing, "session_prewarm_started")
            _timing_mark(timing, "session_prewarm_completed")
            _timing_mark(timing, "attachment_staging_done")
            return prepared_input

        def on_runtime_submitted() -> None:
            _schedule_timing_update(
                _persist_runtime_submitted(
                    queued.inbound_message_id,
                    datetime.now(timezone.utc),
                ),
                name=(
                    "im-app-runtime-submitted-timing-"
                    f"{queued.inbound_message_id}"
                ),
            )

        def on_channel_lease(
            target: RuntimeCommandTarget,
            lease: Any,
            runtime_run_id: str,
            turn_identity: RuntimeTurnIdentity,
        ) -> None:
            nonlocal fast_lane_target, fast_lane_lease, fast_lane_run_id
            fast_lane_target = target
            fast_lane_lease = lease
            fast_lane_run_id = runtime_run_id
            hot_routes = getattr(self, "_hot_routes", None)
            if hot_routes is None:
                hot_routes = {}
                self._hot_routes = hot_routes
            hot_routes[
                (
                    queued.channel_id,
                    queued.external_conversation_id,
                    int(queued.sender_platform_user_id or 0),
                )
            ] = PreparedWeComFastLaneRoute(
                target=target,
                identity=turn_identity,
                channel_id=queued.channel_id,
                target_type="application_agent",
                target_id=int(queued.application_agent_id or 0),
                gateway_instance_id=queued.owner_instance_id,
                channel_lease_epoch=queued.lease_epoch,
                channel_generation=queued.generation,
                config_generation=max(
                    int(lease.config_generation),
                    0,
                ),
                prepared_at=time.monotonic(),
            )
            _schedule_timing_update(
                _persist_batch_accepted(queued),
                name=(
                    "im-app-batch-accepted-"
                    f"{queued.inbound_message_id}"
                ),
            )

        principal = Principal(
            org_id=queued.org_id,
            user_id=queued.sender_platform_user_id,
        )
        conversation_key = _application_conversation_key(queued)
        (
            idempotency_key,
            deterministic_runtime_run_id,
            invocation_request_id,
        ) = (
            _application_wecom_run_identity(queued)
        )
        try:
            async with AsyncSessionLocal() as db:
                result = await SqlAlchemyApplicationAgentInvocationService(
                    db,
                    prepared_knowledge_service=(
                        shared_runtime_prepared_knowledge_service()
                    ),
                ).invoke_channel(
                    application_agent_id=queued.application_agent_id,
                    application_client_id=queued.application_client_id,
                    delegated_principal=principal,
                    payload=ApplicationAgentResponseRequest(
                        input=str(
                            queued.normalized_content.get("text") or ""
                        )
                        or "[企微消息没有可读取的正文]",
                        conversation_key=conversation_key,
                        request_id=invocation_request_id,
                    ),
                    channel="wecom",
                    trusted_context={
                        "application_agent_id": (
                            queued.application_agent_id
                        ),
                        "im_channel_id": queued.channel_id,
                        "im_inbound_message_id": (
                            queued.inbound_message_id
                        ),
                        "chat_type": queued.chat_type,
                        "conversation_source": "wecom",
                        "im_gateway_instance_id": (
                            queued.owner_instance_id
                        ),
                        "im_channel_lease_epoch": queued.lease_epoch,
                        "im_channel_generation": queued.generation,
                    },
                    event_sink=on_event,
                    run_created_sink=on_run_created,
                    before_execute_sink=prepare_session,
                    runtime_submitted_sink=on_runtime_submitted,
                    channel_lease_sink=on_channel_lease,
                    idempotency_key=idempotency_key,
                    deterministic_runtime_run_id=(
                        deterministic_runtime_run_id
                    ),
                    timing=timing,
                )
            await event_pump.drain()
            if run_row_id is None:
                raise ImRuntimeBridgeError(
                    "ApplicationAgentRun was not bound"
                )
            async with AsyncSessionLocal() as db:
                queue = ImInboundQueue(db)
                await queue.mark_batch_runtime_terminal(
                    inbound_message_id=queued.inbound_message_id,
                    terminal_status="completed",
                    owner_instance_id=queued.owner_instance_id,
                    lease_epoch=queued.lease_epoch,
                )
                stream_id = await queue.mark_ready_to_deliver(
                    inbound_message_id=queued.inbound_message_id,
                    final_text=result.output_text,
                    runtime_run_id=deterministic_runtime_run_id,
                    application_agent_run_id=run_row_id,
                    owner_instance_id=queued.owner_instance_id,
                    lease_epoch=queued.lease_epoch,
                )
            if stream_id is None:
                raise ImRuntimeBridgeError(
                    "IM queue ownership was lost before terminal delivery"
                )
            if stream_id != queued.stream_id:
                raise ImRuntimeBridgeError(
                    "WeCom stream identity changed before terminal delivery"
                )
            delivery_queued = replace(queued, stream_id=stream_id)
            delivery_status = await projector.deliver_final(
                result.output_text
            )
            if delivery_status != "delivered":
                return run_row_id, delivery_status
            if (
                endpoint is not None
                and runtime_session_id is not None
            ):
                await self._deliver_generated_artifacts_best_effort(
                    queued=delivery_queued,
                    endpoint=endpoint,
                    runtime_session_id=runtime_session_id,
                    input_paths=input_paths,
                    started_wall_time=started_wall_time,
                )
            return run_row_id, "completed"
        except BaseException as exc:
            _log_application_im_timing(
                timing,
                stage="failed",
                status="error",
                inbound_message_id=queued.inbound_message_id,
                runtime_session_id=runtime_session_id,
                error_type=type(exc).__name__,
            )
            try:
                await event_pump.drain()
            except BaseException as drain_exc:
                if drain_exc is not exc:
                    raise drain_exc from exc
            raise

    async def _deliver_generated_artifacts_best_effort(
        self,
        *,
        queued: QueuedInbound,
        endpoint: Any,
        runtime_session_id: str,
        input_paths: set[str],
        started_wall_time: float,
    ) -> None:
        """Keep a delivered answer terminal when artifact follow-up fails."""

        try:
            await self._deliver_generated_artifacts(
                queued=queued,
                endpoint=endpoint,
                runtime_session_id=runtime_session_id,
                input_paths=input_paths,
                started_wall_time=started_wall_time,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.warning(
                "Application WeCom artifact follow-up failed after answer delivery",
                extra={"inbound_message_id": queued.inbound_message_id},
                exc_info=True,
            )
            try:
                async with AsyncSessionLocal() as db:
                    await ImInboundQueue(db).mark_artifact_delivery_failed(
                        inbound_message_id=queued.inbound_message_id,
                        owner_instance_id=queued.owner_instance_id,
                        lease_epoch=queued.lease_epoch,
                        error_code=type(exc).__name__,
                        error_message=str(exc),
                    )
            except Exception:
                _logger.exception(
                    "Application WeCom artifact failure checkpoint failed",
                    extra={"inbound_message_id": queued.inbound_message_id},
                )

    async def _stage_and_build_prompt(
        self,
        *,
        queued: QueuedInbound,
        endpoint: Any,
        runtime_session_id: str,
    ) -> tuple[PreparedApplicationChannelInput, set[str]]:
        text = str(queued.normalized_content.get("text") or "")
        batch_messages = queued.batch_messages
        inbound_ids = [
            message.inbound_message_id for message in batch_messages
        ] or [queued.inbound_message_id]
        if queued.quote_payload:
            text += (
                "\n\n<wecom_quote>\n"
                + json.dumps(
                    queued.quote_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n</wecom_quote>"
            )
        async with AsyncSessionLocal() as db:
            attachments = list(
                (
                    await db.execute(
                        select(ImMessageAttachment)
                        .where(
                            ImMessageAttachment.inbound_message_id.in_(
                                inbound_ids
                            ),
                        )
                        .order_by(
                            ImMessageAttachment.inbound_message_id,
                            ImMessageAttachment.ordinal,
                        )
                    )
                ).scalars()
            )
        paths: set[str] = set()
        paths_by_message: dict[int, list[dict[str, str]]] = {}
        runtime_attachments: list[RuntimeTurnAttachment] = []
        for attachment in attachments:
            if (
                attachment.staging_status == "ready"
                and attachment.runtime_relative_path
            ):
                ready_path = str(attachment.runtime_relative_path)
                paths.add(ready_path)
                runtime_attachments.append(
                    RuntimeTurnAttachment(
                        file_id=str(attachment.id),
                        display_name=str(
                            attachment.display_name or Path(ready_path).name
                        ),
                        relative_path=ready_path,
                        mime_type=attachment.mime_type,
                    )
                )
                paths_by_message.setdefault(
                    int(attachment.inbound_message_id), []
                ).append(
                    {
                        "name": str(
                            attachment.display_name
                            or Path(ready_path).name
                        ),
                        "path": ready_path,
                    }
                )
                continue
            encrypted = attachment.provider_media_metadata_encrypted
            if not encrypted:
                raise ImRuntimeBridgeError(
                    "WeCom attachment metadata is unavailable"
                )
            metadata = json.loads(
                self._secret_codec().decrypt(str(encrypted))
            )
            local_path: Path | None = None
            try:
                result = await self.node_bridge.download_media(
                    channel_id=queued.channel_id,
                    generation=queued.generation,
                    lease_epoch=queued.lease_epoch,
                    url=str(metadata["url"]),
                    aes_key=metadata.get("aes_key"),
                    filename=attachment.display_name,
                )
                local_path = self.node_bridge.require_scoped_path(
                    str(result["path"])
                )
                size = int(result.get("size_bytes") or 0)
                limit = settings.IM_GATEWAY_MEDIA_MAX_MB * 1024 * 1024
                if size < 0 or size > limit:
                    raise ImRuntimeBridgeError(
                        "wecom media exceeds the configured limit"
                    )
                filename = _safe_filename(
                    str(result.get("filename") or local_path.name)
                )
                digest = await asyncio.to_thread(
                    _sha256_file, local_path
                )
                mime_type = await asyncio.to_thread(
                    _detect_mime_type, local_path, filename
                )
                _validate_inbound_media_type(
                    str(attachment.media_type),
                    mime_type,
                )
                relative_path = (
                    "workspace/sessions/"
                    f"{runtime_session_id}/im-inbox/"
                    f"{uuid4().hex[:12]}-{filename}"
                )
                with local_path.open("rb") as source:
                    uploaded = await self.dashboard.upload_managed_artifact(
                        endpoint,
                        relative_path=relative_path,
                        content=source,
                        filename=filename,
                        mime_type=mime_type,
                    )
                async with AsyncSessionLocal() as db:
                    current = await db.get(
                        ImMessageAttachment,
                        int(attachment.id),
                    )
                    if current is None:
                        raise ImRuntimeBridgeError(
                            "WeCom attachment row disappeared"
                        )
                    current.display_name = filename
                    current.mime_type = (
                        uploaded.mime_type or mime_type
                    )
                    current.size_bytes = (
                        uploaded.size_bytes or size
                    )
                    current.sha256 = digest
                    current.runtime_relative_path = (
                        uploaded.relative_path
                    )
                    current.staging_status = "ready"
                    current.provider_media_metadata_encrypted = None
                    await db.commit()
                paths.add(str(uploaded.relative_path))
                runtime_attachments.append(
                    RuntimeTurnAttachment(
                        file_id=str(current.id),
                        display_name=str(
                            current.display_name
                            or Path(str(uploaded.relative_path)).name
                        ),
                        relative_path=str(uploaded.relative_path),
                        mime_type=current.mime_type,
                    )
                )
                paths_by_message.setdefault(
                    int(attachment.inbound_message_id), []
                ).append(
                    _wecom_batch_attachment_entry(
                        display_name=filename,
                        relative_path=str(uploaded.relative_path),
                        mime_type=mime_type,
                    )
                )
            finally:
                if local_path is not None:
                    try:
                        local_path.unlink(missing_ok=True)
                    except OSError:
                        pass
        original_text = text.strip()
        attachment_contract = _wecom_platform_attachments_contract(
            runtime_attachments
        )
        if len(batch_messages) > 1:
            text = _format_wecom_batch_prompt(
                batch_messages,
                attachments=paths_by_message,
            ) + attachment_contract
        has_image = any(
            attachment.mime_type and attachment.mime_type.startswith("image/")
            for attachment in runtime_attachments
        )
        if has_image and original_text == "[企微图片附件]":
            text = "请识别这些图片" if len(runtime_attachments) > 1 else "请识别这张图片"
        else:
            text += attachment_contract
        return (
            PreparedApplicationChannelInput(
                prompt=text or "[企微消息没有可读取的正文]",
                attachments=tuple(runtime_attachments),
            ),
            paths,
        )

    async def _deliver_generated_artifacts(
        self,
        *,
        queued: QueuedInbound,
        endpoint: Any,
        runtime_session_id: str,
        input_paths: set[str],
        started_wall_time: float,
    ) -> None:
        artifacts = await self.dashboard.list_session_artifacts(
            endpoint,
            runtime_session_id=runtime_session_id,
        )
        async with AsyncSessionLocal() as db:
            checkpoint = await db.scalar(
                select(ImDeliveryCheckpoint).where(
                    ImDeliveryCheckpoint.inbound_message_id
                    == queued.inbound_message_id
                )
            )
            delivered = {
                str(value)
                for value in (
                    checkpoint.delivered_artifact_ids or []
                    if checkpoint is not None
                    else []
                )
                if isinstance(value, str)
            }
        for artifact in artifacts:
            relative_path = str(artifact.relative_path)
            path_key = f"path:{hashlib.sha256(relative_path.encode()).hexdigest()}"
            if (
                relative_path in input_paths
                or path_key in delivered
                or artifact.mtime is None
                or (
                    artifact.mtime < started_wall_time - 1.0
                )
            ):
                continue
            filename = _safe_filename(Path(relative_path).name)
            if self.node_bridge.temp_root is None:
                return
            target = self.node_bridge.require_scoped_path(
                str(
                    self.node_bridge.temp_root
                    / f"app-out-{uuid4().hex}-{filename}"
                )
            )
            try:
                with target.open("wb") as destination:
                    async for chunk in (
                        self.dashboard.stream_managed_artifact(
                            endpoint,
                            relative_path=relative_path,
                        )
                    ):
                        destination.write(chunk)
                await self.node_bridge.upload_media(
                    passive=False,
                    channel_id=queued.channel_id,
                    generation=queued.generation,
                    lease_epoch=queued.lease_epoch,
                    media_type=_outbound_media_type(
                        artifact.mime_type,
                        filename,
                    ),
                    source_path=target,
                    filename=filename,
                    chat_id=queued.external_conversation_id,
                )
                await self._mark_artifact_path_delivered(
                    queued=queued,
                    path_key=path_key,
                )
            except WeComBridgeError as exc:
                if exc.code != "MEDIA_TOO_LARGE":
                    raise
                await self.node_bridge.send_markdown(
                    channel_id=queued.channel_id,
                    generation=queued.generation,
                    lease_epoch=queued.lease_epoch,
                    chat_id=queued.external_conversation_id,
                    content=(
                        f"附件 **{filename}** 超出企微发送上限，"
                        "请在 WebUI 中下载。"
                    ),
                )
                await self._mark_artifact_path_delivered(
                    queued=queued,
                    path_key=path_key,
                )
            finally:
                target.unlink(missing_ok=True)

    @staticmethod
    async def _mark_artifact_path_delivered(
        *,
        queued: QueuedInbound,
        path_key: str,
    ) -> None:
        async with AsyncSessionLocal() as db:
            checkpoint = (
                await db.execute(
                    select(ImDeliveryCheckpoint)
                    .where(
                        ImDeliveryCheckpoint.inbound_message_id
                        == queued.inbound_message_id
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if checkpoint is None:
                return
            delivered = list(
                checkpoint.delivered_artifact_ids or []
            )
            if path_key not in delivered:
                delivered.append(path_key)
                checkpoint.delivered_artifact_ids = delivered
                await db.commit()

    @staticmethod
    async def _mark_first_runtime_event(
        inbound_message_id: int,
    ) -> None:
        await _persist_first_runtime_public_event(
            inbound_message_id,
            datetime.now(timezone.utc),
        )

    async def _recover_existing_run(
        self,
        queued: QueuedInbound,
    ) -> tuple[int, str]:
        run_id = int(queued.application_agent_run_id or 0)
        async with AsyncSessionLocal() as db:
            run = await db.get(ApplicationAgentRun, run_id)
        if run is None:
            raise ImRuntimeBridgeError(
                "claimed ApplicationAgentRun is unavailable"
            )
        status = str(run.status or "failed")
        if status not in {"completed", "failed"}:
            raise ImRuntimeBridgeError(
                "application run recovery requires reconciliation"
            )
        final_text = (
            str((run.response_snapshot or {}).get("output_text") or "")
            if status == "completed"
            else "Agent 处理失败，请稍后重试。"
        )
        async with AsyncSessionLocal() as db:
            stream_id = await ImInboundQueue(
                db
            ).mark_ready_to_deliver(
                inbound_message_id=queued.inbound_message_id,
                final_text=final_text,
                runtime_run_id=str(run.runtime_run_id),
                application_agent_run_id=run_id,
                owner_instance_id=queued.owner_instance_id,
                lease_epoch=queued.lease_epoch,
            )
        if stream_id is None:
            raise ImRuntimeBridgeError(
                "IM queue ownership was lost during application recovery"
            )
        projector = WeComStreamProjector(
            bridge=self.node_bridge,
            identity=ProjectorIdentity(
                inbound_message_id=queued.inbound_message_id,
                channel_id=queued.channel_id,
                generation=queued.generation,
                owner_instance_id=queued.owner_instance_id,
                lease_epoch=queued.lease_epoch,
                provider_request_id=queued.provider_request_id,
                provider_stream_id=stream_id,
                chat_id=queued.external_conversation_id,
                target_type="application_agent",
            ),
        )
        await projector.load()
        delivery_status = await projector.deliver_final(final_text)
        if delivery_status != "delivered":
            return run_id, delivery_status
        return run_id, status

    @staticmethod
    def _secret_codec():
        from app.services.managed_secret import ManagedSecretCodec

        return ManagedSecretCodec()


def _application_conversation_key(queued: QueuedInbound) -> str:
    return _application_conversation_key_values(
        application_agent_id=int(queued.application_agent_id or 0),
        channel_id=queued.channel_id,
        chat_type=queued.chat_type,
        external_conversation_id=queued.external_conversation_id,
        platform_user_id=int(queued.sender_platform_user_id or 0),
    )


def _application_conversation_key_values(
    *,
    application_agent_id: int,
    channel_id: int,
    chat_type: str,
    external_conversation_id: str,
    platform_user_id: int,
) -> str:
    prefix = f"wecom:app:{application_agent_id}:{channel_id}"
    if chat_type == "group":
        return (
            f"{prefix}:group:{external_conversation_id}:"
            f"{platform_user_id}"
        )[:255]
    return f"{prefix}:direct:{platform_user_id}"[:255]


def _application_wecom_run_identity(
    queued: QueuedInbound,
) -> tuple[str, str, str]:
    if queued.external_message_id:
        (
            _synthetic_inbound_id,
            runtime_run_id,
            _client_message_id,
            _stream_id,
            invocation_request_id,
        ) = _queued_wecom_turn_identity(queued)
        return (
            invocation_request_id,
            runtime_run_id,
            invocation_request_id,
        )
    idempotency_key = (
        f"wecom:{queued.channel_id}:{queued.inbound_message_id}"
    )
    digest = hashlib.sha256(
        idempotency_key.encode("utf-8")
    ).hexdigest()
    return (
        idempotency_key,
        f"app_run_wecom_{digest[:40]}",
        f"im-wecom-{queued.inbound_message_id}",
    )


def _queued_wecom_turn_identity(
    queued: QueuedInbound,
) -> tuple[int, str, str, str, str]:
    if queued.external_message_id:
        return _hot_turn_identity(
            target_type=queued.target_type,
            channel_id=queued.channel_id,
            external_message_id=queued.external_message_id,
            external_conversation_id=queued.external_conversation_id,
        )
    legacy_digest = hashlib.sha256(
        (
            f"wecom:{queued.channel_id}:{queued.inbound_message_id}"
            if queued.target_type == "application_agent"
            else f"{queued.channel_id}\0{queued.inbound_message_id}"
        ).encode("utf-8")
    ).hexdigest()
    runtime_run_id = (
        f"app_run_wecom_{legacy_digest[:40]}"
        if queued.target_type == "application_agent"
        else f"rr-im-wecom-{queued.inbound_message_id}"
    )
    request_id = f"im-wecom-{queued.inbound_message_id}"
    return (
        queued.inbound_message_id,
        runtime_run_id,
        (
            deterministic_command_id("application-message", request_id)
            if queued.target_type == "application_agent"
            else request_id
        ),
        queued.stream_id,
        request_id,
    )


def _safe_filename(value: str) -> str:
    candidate = _SAFE_FILENAME_RE.sub("_", Path(value).name).strip("._")
    return (candidate or "wecom-file.bin")[:180]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _detect_mime_type(path: Path, filename: str) -> str:
    """Identify common IM payloads from their bytes, never the suffix alone."""

    with path.open("rb") as stream:
        header = stream.read(16 * 1024)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if (
        len(header) >= 12
        and header.startswith(b"RIFF")
        and header[8:12] == b"WEBP"
    ):
        return "image/webp"
    if header.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"{\\rtf"):
        return "application/rtf"
    if header.startswith(b"%!PS-Adobe-"):
        return "application/postscript"
    if header.startswith(b"\x7fELF") or (
        header.startswith(b"MZ") and len(header) >= 64
    ):
        return "application/x-executable"
    if header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/x-ole-storage"
    if header.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if header.startswith(b"7z\xbc\xaf'\x1c"):
        return "application/x-7z-compressed"
    if header.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return "application/vnd.rar"
    if len(header) >= 262 and header[257:262] == b"ustar":
        return "application/x-tar"
    if header.startswith(b"OggS"):
        return "application/ogg"
    if header.startswith(b"fLaC"):
        return "audio/flac"
    if header.startswith(b"ID3") or _looks_like_mp3_frame(header):
        return "audio/mpeg"
    if (
        len(header) >= 12
        and header.startswith(b"RIFF")
        and header[8:12] == b"WAVE"
    ):
        return "audio/wav"
    if (
        len(header) >= 12
        and header.startswith(b"RIFF")
        and header[8:12] == b"AVI "
    ):
        return "video/x-msvideo"
    if header.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return "video/mpeg"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return (
            "video/webm"
            if b"webm" in header[:4096].lower()
            else "video/x-matroska"
        )
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12].lower()
        if brand in {b"avif", b"avis"}:
            return "image/avif"
        if brand in {
            b"heic",
            b"heix",
            b"hevc",
            b"hevx",
            b"heim",
            b"heis",
            b"mif1",
            b"msf1",
        }:
            return "image/heic"
        if brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    if zipfile.is_zipfile(path):
        return _zip_mime_type(path)
    text_mime = _text_mime_type(header, filename)
    return text_mime or "application/octet-stream"


def _zip_mime_type(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            names = {name.lower() for name in archive.namelist()}
            if any(name.startswith("word/") for name in names):
                return (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                )
            if any(name.startswith("xl/") for name in names):
                return (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                )
            if any(name.startswith("ppt/") for name in names):
                return (
                    "application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation"
                )
            if "mimetype" in names:
                mime = archive.read("mimetype")[:255].decode(
                    "ascii", errors="ignore"
                )
                if mime.startswith("application/vnd.oasis.opendocument."):
                    return mime
    except (OSError, KeyError, zipfile.BadZipFile):
        return "application/octet-stream"
    return "application/zip"


def _looks_like_mp3_frame(header: bytes) -> bool:
    return (
        len(header) >= 2
        and header[0] == 0xFF
        and header[1] & 0xE0 == 0xE0
    )


def _text_mime_type(header: bytes, filename: str) -> str | None:
    if not header or b"\x00" in header:
        return None
    try:
        text = header.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    stripped = text.lstrip().lower()
    if stripped.startswith("<svg") or (
        stripped.startswith("<?xml") and "<svg" in stripped[:2048]
    ):
        return "image/svg+xml"
    if stripped.startswith(("<!doctype html", "<html")):
        return "text/html"
    suffix = Path(filename).suffix.lower()
    if suffix == ".json" and stripped.startswith(("{", "[")):
        return "application/json"
    if suffix == ".xml" and stripped.startswith("<"):
        return "application/xml"
    if suffix in {".yaml", ".yml"}:
        return "application/yaml"
    if suffix == ".csv":
        return "text/csv"
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    return "text/plain"


def _validate_inbound_media_type(
    provider_media_type: str, detected_mime_type: str
) -> None:
    required_prefix = {
        "image": "image/",
        "video": "video/",
    }.get(provider_media_type)
    if required_prefix and not detected_mime_type.startswith(required_prefix):
        raise ImRuntimeBridgeError(
            "WeCom media content does not match its declared type"
        )


def _outbound_media_type(
    mime_type: str | None, filename: str
) -> str:
    mime = str(mime_type or mimetypes.guess_type(filename)[0] or "")
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "voice"
    return "file"
