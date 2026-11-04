from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from platform_supervisor.configuration import RuntimeConfigError, RuntimeConfigUnavailable
from platform_supervisor.contracts import (
    ChannelExecutionLease,
    ChannelFastLaneDeliveryAck,
    ChannelFastLaneSubmitFrame,
    ChannelFastLaneV2DeliveryAck,
    ChannelFastLaneV2DispositionAck,
    ChannelFastLaneV2DispositionFrame,
    ChannelFastLaneV2ResumeFrame,
    ChannelFastLaneV2SubmitFrame,
    canonical_payload_digest,
)
from platform_supervisor.edge import (
    ChannelFastLaneV2ResumeBatch,
    RuntimeEdgeDependencies,
)
from platform_supervisor.execution import CapacityBusy
from platform_supervisor.execution.manager import (
    RunManager,
    RunNotFound,
    RunSubmission,
    SessionBusy,
)
from platform_supervisor.execution.sessions import PrepareIdentity, SessionCapacityBusy
from platform_supervisor.storage import (
    IdempotencyConflict,
    SQLiteStore,
    StoredChannelFastLaneV2Delivery,
    StoredRedirect,
    TERMINAL_STATES,
)


CHANNEL_FAST_LANE_V1_PROTOCOL = "hermes-channel-fast-lane-v1"
CHANNEL_FAST_LANE_V2_PROTOCOL = "hermes-channel-fast-lane-v2"
# Compatibility alias retained for callers draining the deployed v1 lane.
CHANNEL_FAST_LANE_PROTOCOL = CHANNEL_FAST_LANE_V1_PROTOCOL
HANDSHAKE_MAX_SKEW_SECONDS = 30
HANDSHAKE_NONCE_TTL_SECONDS = 60
MAX_ACTIVE_RUN_STREAMS = 512


logger = logging.getLogger(__name__)


class DurableChannelFastLaneV2Router:
    """Delivery-keyed Fast Lane v2 adapter backed by Supervisor SQLite."""

    def __init__(self, store: SQLiteStore, runs: RunManager) -> None:
        self.store = store
        self.runs = runs

    async def submit_turn(
        self,
        *,
        frame: ChannelFastLaneV2SubmitFrame,
        identity: PrepareIdentity,
        payload_digest: str,
    ) -> ChannelFastLaneV2DispositionFrame:
        turn = frame.envelope.payload
        if turn.attachment_file_ids or turn.attachments:
            raise PermissionError("FAST_LANE_ATTACHMENTS_FORBIDDEN")
        interaction_origin = (
            "wecom_personal"
            if turn.source == "personal_wecom"
            else "wecom_application"
        )
        submission = await self.runs.submit_interactive(
            identity=identity,
            client_message_id=frame.client_message_id,
            payload_digest=payload_digest,
            prompt=turn.prompt,
            audit_prompt=turn.prompt,
            attachment_file_ids=(),
            redirect_eligible=True,
            interrupt_parent=True,
            admission_generation=frame.envelope.admission_generation,
            interaction_origin=interaction_origin,
            source=turn.source,
            source_detail={
                "kind": "channel_fast_lane_v2",
                "delivery_id": frame.delivery_id,
            },
            run_id=turn.run_id,
            delivery_id=frame.delivery_id,
        )
        delivery = await self.store.refresh_channel_fast_lane_v2_delivery(
            frame.delivery_id,
            refreshed_at=datetime.now(timezone.utc),
        )
        if isinstance(submission, RunSubmission):
            valid_target = (
                delivery.target_kind == "ordinary_run"
                and delivery.run_id == submission.run.run_id
            )
        elif isinstance(submission, StoredRedirect):
            valid_target = (
                delivery.target_kind == "redirect"
                and delivery.redirect_id == submission.redirect_id
            )
        else:
            valid_target = False
        if not valid_target:
            raise IdempotencyConflict(
                "Fast Lane v2 durable disposition target conflicts"
            )
        return _channel_fast_lane_v2_disposition(delivery)

    async def resume_delivery(
        self,
        *,
        frame: ChannelFastLaneV2ResumeFrame,
    ) -> ChannelFastLaneV2ResumeBatch:
        if (
            await self.store.get_channel_fast_lane_v2_delivery(
                frame.delivery_id
            )
            is None
        ):
            raise RunNotFound(frame.delivery_id)
        delivery = await self.store.refresh_channel_fast_lane_v2_delivery(
            frame.delivery_id,
            refreshed_at=datetime.now(timezone.utc),
        )
        if frame.last_disposition_seq > delivery.disposition_seq:
            raise IdempotencyConflict(
                "Fast Lane v2 resume disposition is ahead of durable state"
            )
        disposition = _channel_fast_lane_v2_disposition(delivery)
        if delivery.start_after_seq is None:
            return ChannelFastLaneV2ResumeBatch(disposition=disposition)
        after_seq = max(
            _v2_resume_event_after_seq(frame, disposition),
            delivery.start_after_seq,
            delivery.provider_confirmed_seq,
        )
        snapshot = await self.store.load_run_snapshot(
            delivery.canonical_run_id,
            after_seq=after_seq,
        )
        if snapshot is None:
            raise RunNotFound(delivery.canonical_run_id)
        events = tuple(
            {
                "schema_version": 1,
                "channel_fast_lane_schema_version": 2,
                "type": "run.event",
                "delivery_id": delivery.delivery_id,
                "canonical_run_id": delivery.canonical_run_id,
                "run_id": delivery.canonical_run_id,
                "seq": event.seq,
                "event_type": event.event_type,
                "emitted_at": event.occurred_at.isoformat(),
                "data": event.payload,
            }
            for event in snapshot.events
        )
        return ChannelFastLaneV2ResumeBatch(
            disposition=disposition,
            events=events,
        )

    async def acknowledge_disposition(
        self,
        *,
        frame: ChannelFastLaneV2DispositionAck,
    ) -> None:
        if (
            await self.store.get_channel_fast_lane_v2_delivery(
                frame.delivery_id
            )
            is None
        ):
            raise RunNotFound(frame.delivery_id)
        await self.store.acknowledge_channel_fast_lane_v2_disposition(
            delivery_id=frame.delivery_id,
            disposition_seq=frame.disposition_seq,
            gateway_instance_id=frame.gateway_instance_id,
            channel_lease_epoch=frame.channel_lease_epoch,
            acknowledged_at=datetime.now(timezone.utc),
        )

    async def acknowledge_provider_delivery(
        self,
        *,
        frame: ChannelFastLaneV2DeliveryAck,
    ) -> int:
        if (
            await self.store.get_channel_fast_lane_v2_delivery(
                frame.owner_delivery_id
            )
            is None
        ):
            raise RunNotFound(frame.owner_delivery_id)
        checkpoint = (
            await self.store.acknowledge_channel_fast_lane_v2_provider_delivery(
                owner_delivery_id=frame.owner_delivery_id,
                canonical_run_id=frame.canonical_run_id,
                highest_contiguous_seq=frame.highest_contiguous_seq,
                gateway_instance_id=frame.gateway_instance_id,
                channel_lease_epoch=frame.channel_lease_epoch,
                provider_request_id=frame.provider_request_id,
                provider_message_id=frame.provider_message_id,
                projection_version=frame.projection_version,
                confirmed_at=frame.confirmed_at,
            )
        )
        return checkpoint.highest_contiguous_seq


def _channel_fast_lane_v2_disposition(
    delivery: StoredChannelFastLaneV2Delivery,
) -> ChannelFastLaneV2DispositionFrame:
    return ChannelFastLaneV2DispositionFrame(
        delivery_id=delivery.delivery_id,
        client_message_id=delivery.client_message_id,
        disposition_seq=delivery.disposition_seq,
        state=delivery.disposition_state,
        canonical_run_id=delivery.canonical_run_id,
        parent_run_id=delivery.parent_run_id,
        start_after_seq=delivery.start_after_seq,
        promoted_run_id=delivery.promoted_run_id,
    )


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def channel_lease_signature(
    secret: str,
    lease: ChannelExecutionLease | dict[str, Any],
) -> str:
    payload = (
        lease.model_dump(mode="json", exclude={"signature"}, exclude_none=True)
        if isinstance(lease, ChannelExecutionLease)
        else {key: value for key, value in lease.items() if key != "signature"}
    )
    return hmac.new(
        secret.encode("utf-8"),
        _canonical_json(payload),
        hashlib.sha256,
    ).hexdigest()


def channel_handshake_signature(
    secret: str,
    *,
    runtime_id: str,
    binding_id: str,
    gateway_instance_id: str,
    channel_lease_epoch: int,
    timestamp: int,
    nonce: str,
) -> str:
    payload = {
        "binding_id": binding_id,
        "channel_lease_epoch": channel_lease_epoch,
        "gateway_instance_id": gateway_instance_id,
        "nonce": nonce,
        "runtime_id": runtime_id,
        "timestamp": timestamp,
    }
    return hmac.new(
        secret.encode("utf-8"),
        _canonical_json(payload),
        hashlib.sha256,
    ).hexdigest()


def attach_channel_fast_lane(
    app: FastAPI,
    dependencies: RuntimeEdgeDependencies,
) -> None:
    seen_nonces: dict[str, float] = {}
    nonce_lock = asyncio.Lock()

    async def authorize(websocket: WebSocket) -> tuple[str, int] | None:
        authorization = websocket.headers.get("authorization")
        expected_authorization = f"Bearer {dependencies.api_server_key}"
        if authorization is None or not hmac.compare_digest(
            authorization,
            expected_authorization,
        ):
            return None
        runtime_id = str(websocket.headers.get("x-hermes-runtime-id") or "")
        binding_id = str(websocket.headers.get("x-hermes-binding-id") or "")
        gateway_instance_id = str(
            websocket.headers.get("x-hermes-gateway-instance-id") or ""
        )
        nonce = str(websocket.headers.get("x-hermes-connection-nonce") or "")
        raw_epoch = str(websocket.headers.get("x-hermes-channel-lease-epoch") or "")
        raw_timestamp = str(websocket.headers.get("x-hermes-timestamp") or "")
        supplied_signature = str(
            websocket.headers.get("x-hermes-signature") or ""
        )
        try:
            epoch = int(raw_epoch)
            timestamp = int(raw_timestamp)
        except ValueError:
            return None
        if (
            runtime_id != dependencies.runtime_id
            or binding_id != dependencies.binding_id
            or len(gateway_instance_id) < 8
            or epoch <= 0
            or len(nonce) < 16
            or abs(int(time.time()) - timestamp) > HANDSHAKE_MAX_SKEW_SECONDS
        ):
            return None
        expected_signature = channel_handshake_signature(
            dependencies.api_server_key,
            runtime_id=runtime_id,
            binding_id=binding_id,
            gateway_instance_id=gateway_instance_id,
            channel_lease_epoch=epoch,
            timestamp=timestamp,
            nonce=nonce,
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        now = time.monotonic()
        async with nonce_lock:
            expired = [
                item
                for item, expires_at in seen_nonces.items()
                if expires_at <= now
            ]
            for item in expired:
                seen_nonces.pop(item, None)
            if nonce in seen_nonces:
                return None
            seen_nonces[nonce] = now + HANDSHAKE_NONCE_TTL_SECONDS
        return gateway_instance_id, epoch

    def authorize_http(request: Request) -> tuple[str, int] | None:
        authorization = request.headers.get("authorization")
        expected_authorization = f"Bearer {dependencies.api_server_key}"
        if authorization is None or not hmac.compare_digest(
            authorization,
            expected_authorization,
        ):
            return None
        runtime_id = str(request.headers.get("x-hermes-runtime-id") or "")
        binding_id = str(request.headers.get("x-hermes-binding-id") or "")
        gateway_instance_id = str(
            request.headers.get("x-hermes-gateway-instance-id") or ""
        )
        raw_epoch = str(request.headers.get("x-hermes-channel-lease-epoch") or "")
        try:
            epoch = int(raw_epoch)
        except ValueError:
            return None
        if (
            runtime_id != dependencies.runtime_id
            or binding_id != dependencies.binding_id
            or len(gateway_instance_id) < 8
            or epoch <= 0
        ):
            return None
        return gateway_instance_id, epoch

    def http_v2_error(
        request_id: str,
        code: str,
        *,
        retryable: bool,
        status_code: int,
    ) -> JSONResponse:
        return JSONResponse(
            {
                "schema_version": 1,
                "channel_fast_lane_schema_version": 2,
                "type": "channel.error",
                "request_id": request_id,
                "error_code": code,
                "retryable": retryable,
            },
            status_code=status_code,
        )

    @app.post("/internal/v1/channel-fast-lane/v2")
    async def channel_fast_lane_v2_http(request: Request) -> JSONResponse:
        authorized = authorize_http(request)
        if authorized is None:
            return http_v2_error(
                "invalid-frame",
                "INVALID_FAST_LANE_CREDENTIAL",
                retryable=False,
                status_code=401,
            )
        gateway_instance_id, connection_epoch = authorized
        router = dependencies.channel_fast_lane_v2_router
        if router is None:
            return http_v2_error(
                "invalid-frame",
                "FAST_LANE_V2_UNAVAILABLE",
                retryable=True,
                status_code=503,
            )
        try:
            payload = await request.json()
        except (UnicodeDecodeError, ValueError):
            return http_v2_error(
                "invalid-frame",
                "INVALID_JSON",
                retryable=False,
                status_code=400,
            )
        if not isinstance(payload, dict):
            return http_v2_error(
                "invalid-frame",
                "INVALID_FRAME",
                retryable=False,
                status_code=400,
            )
        request_id = str(payload.get("request_id") or "invalid-frame")
        frame_type = str(payload.get("type") or "")
        try:
            if payload.get("channel_fast_lane_schema_version") != 2:
                raise ValueError("v1 frame is not valid on the v2 endpoint")
            if frame_type == "channel.turn.submit":
                frame = ChannelFastLaneV2SubmitFrame.model_validate(payload)
                _validate_v2_connection_identity(
                    frame,
                    gateway_instance_id=gateway_instance_id,
                    connection_epoch=connection_epoch,
                )
                envelope = frame.envelope
                turn = envelope.payload
                if turn.type != "internal_turn" or turn.source not in {
                    "personal_wecom",
                    "application_wecom",
                }:
                    raise PermissionError("CHANNEL_SOURCE_FORBIDDEN")
                lease = turn.channel_execution_lease
                if lease is None:
                    raise PermissionError("CHANNEL_LEASE_REQUIRED")
                _validate_lease(
                    lease,
                    envelope=envelope,
                    gateway_instance_id=gateway_instance_id,
                    connection_epoch=connection_epoch,
                    secret=dependencies.api_server_key,
                )
                dependencies.config.require_revision(
                    turn.identity.config_revision
                )
                if (
                    len(turn.prompt.encode("utf-8"))
                    > turn.identity.max_prompt_bytes
                ):
                    raise PermissionError("PROMPT_TOO_LARGE")
                disposition = await router.submit_turn(
                    frame=frame,
                    identity=_prepare_identity(
                        turn.identity,
                        runtime_id=dependencies.runtime_id,
                    ),
                    payload_digest=canonical_payload_digest(
                        turn.model_dump(
                            mode="json",
                            exclude_none=True,
                            exclude={"channel_execution_lease"},
                        )
                    ),
                )
                if not _valid_v2_disposition(
                    disposition,
                    delivery_id=frame.delivery_id,
                    client_message_id=frame.client_message_id,
                ):
                    raise ValueError("invalid durable disposition")
                return JSONResponse(
                    disposition.model_dump(mode="json", exclude_none=False)
                )
            if frame_type == "channel.turn.resume":
                frame = ChannelFastLaneV2ResumeFrame.model_validate(payload)
                _validate_v2_connection_identity(
                    frame,
                    gateway_instance_id=gateway_instance_id,
                    connection_epoch=connection_epoch,
                )
                batch = await router.resume_delivery(frame=frame)
                if not isinstance(batch, ChannelFastLaneV2ResumeBatch) or not (
                    _valid_v2_disposition(
                        batch.disposition,
                        delivery_id=frame.delivery_id,
                    )
                ):
                    raise ValueError("invalid durable disposition")
                if any(
                    not _valid_v2_delivery_event(
                        event,
                        delivery_id=frame.delivery_id,
                        canonical_run_id=batch.disposition.canonical_run_id,
                    )
                    for event in batch.events
                ):
                    raise ValueError("invalid durable event")
                return JSONResponse(
                    {
                        "schema_version": 1,
                        "channel_fast_lane_schema_version": 2,
                        "type": "channel.turn.resume.result",
                        "disposition": batch.disposition.model_dump(
                            mode="json",
                            exclude_none=False,
                        ),
                        "events": list(batch.events),
                    }
                )
            if frame_type == "channel.turn.disposition.ack":
                frame = ChannelFastLaneV2DispositionAck.model_validate(payload)
                _validate_v2_connection_identity(
                    frame,
                    gateway_instance_id=gateway_instance_id,
                    connection_epoch=connection_epoch,
                )
                await router.acknowledge_disposition(frame=frame)
                return JSONResponse(
                    {
                        "schema_version": 1,
                        "channel_fast_lane_schema_version": 2,
                        "type": "channel.turn.disposition.ack_ok",
                        "request_id": frame.request_id,
                        "delivery_id": frame.delivery_id,
                        "disposition_seq": frame.disposition_seq,
                    }
                )
            if frame_type == "channel.delivery.ack":
                frame = ChannelFastLaneV2DeliveryAck.model_validate(payload)
                _validate_v2_connection_identity(
                    frame,
                    gateway_instance_id=gateway_instance_id,
                    connection_epoch=connection_epoch,
                )
                highest_contiguous_seq = (
                    await router.acknowledge_provider_delivery(frame=frame)
                )
                if (
                    not isinstance(highest_contiguous_seq, int)
                    or highest_contiguous_seq < frame.highest_contiguous_seq
                ):
                    raise ValueError("invalid provider delivery checkpoint")
                return JSONResponse(
                    {
                        "schema_version": 1,
                        "channel_fast_lane_schema_version": 2,
                        "type": "channel.delivery.ack_ok",
                        "request_id": frame.request_id,
                        "owner_delivery_id": frame.owner_delivery_id,
                        "canonical_run_id": frame.canonical_run_id,
                        "highest_contiguous_seq": highest_contiguous_seq,
                    }
                )
            return http_v2_error(
                request_id,
                "FRAME_NOT_IMPLEMENTED",
                retryable=False,
                status_code=400,
            )
        except (TypeError, ValidationError, ValueError):
            return http_v2_error(
                request_id,
                "INVALID_FRAME",
                retryable=False,
                status_code=400,
            )
        except IdempotencyConflict:
            return http_v2_error(
                request_id,
                "IDEMPOTENCY_CONFLICT",
                retryable=False,
                status_code=409,
            )
        except (CapacityBusy, SessionBusy, SessionCapacityBusy) as exc:
            return http_v2_error(
                request_id,
                str(exc),
                retryable=True,
                status_code=409,
            )
        except (RuntimeConfigUnavailable, RuntimeConfigError) as exc:
            return http_v2_error(
                request_id,
                str(exc),
                retryable=True,
                status_code=503,
            )
        except PermissionError as exc:
            return http_v2_error(
                request_id,
                str(exc),
                retryable=False,
                status_code=403,
            )
        except RunNotFound:
            return http_v2_error(
                request_id,
                "DELIVERY_NOT_FOUND",
                retryable=False,
                status_code=404,
            )
        except Exception:
            logger.exception(
                "Runtime Channel Fast Lane v2 HTTP frame failed",
                extra={
                    "runtime_id": dependencies.runtime_id,
                    "binding_id": dependencies.binding_id,
                    "request_id": request_id,
                    "frame_type": frame_type,
                },
            )
            return http_v2_error(
                request_id,
                "INTERNAL_ERROR",
                retryable=True,
                status_code=500,
            )

    @app.websocket("/internal/v1/channel-fast-lane/ws")
    async def channel_fast_lane(websocket: WebSocket) -> None:
        authorized = await authorize(websocket)
        if authorized is None:
            await websocket.close(code=4401, reason="invalid fast lane credential")
            return
        gateway_instance_id, connection_epoch = authorized
        protocols = {
            item.strip()
            for item in str(
                websocket.headers.get("sec-websocket-protocol") or ""
            ).split(",")
            if item.strip()
        }
        if CHANNEL_FAST_LANE_V2_PROTOCOL in protocols:
            if dependencies.channel_fast_lane_v2_router is None:
                await websocket.close(
                    code=4406,
                    reason="fast lane v2 unavailable",
                )
                return
            selected_protocol = CHANNEL_FAST_LANE_V2_PROTOCOL
        elif CHANNEL_FAST_LANE_V1_PROTOCOL in protocols:
            selected_protocol = CHANNEL_FAST_LANE_V1_PROTOCOL
        else:
            await websocket.close(code=4406, reason="fast lane protocol required")
            return
        await websocket.accept(subprotocol=selected_protocol)
        send_lock = asyncio.Lock()
        stream_tasks: dict[str, asyncio.Task[None]] = {}

        async def send(payload: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_text(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                )

        async def send_error(
            request_id: str,
            code: str,
            *,
            retryable: bool,
        ) -> None:
            await send(
                {
                    "schema_version": 1,
                    "type": "channel.error",
                    "request_id": request_id,
                    "error_code": code,
                    "retryable": retryable,
                }
            )

        if selected_protocol == CHANNEL_FAST_LANE_V2_PROTOCOL:
            await _serve_channel_fast_lane_v2(
                websocket,
                dependencies=dependencies,
                gateway_instance_id=gateway_instance_id,
                connection_epoch=connection_epoch,
                send=send,
            )
            return

        async def stream_run(run_id: str, after_seq: int) -> None:
            try:
                async for chunk in dependencies.runs.subscribe(
                    run_id,
                    after_seq=max(0, after_seq),
                ):
                    await send(json.loads(chunk))
            except RunNotFound:
                snapshot = await dependencies.store.load_run_snapshot(
                    run_id,
                    after_seq=max(0, after_seq),
                )
                if snapshot is None:
                    return
                for event in snapshot.events:
                    await send(
                        {
                            "schema_version": 1,
                            "type": "run.event",
                            "run_id": run_id,
                            "seq": event.seq,
                            "event_type": event.event_type,
                            "emitted_at": event.occurred_at.isoformat(),
                            "data": event.payload,
                        }
                    )
            finally:
                stream_tasks.pop(run_id, None)

        def start_stream(run_id: str, after_seq: int) -> bool:
            current = stream_tasks.get(run_id)
            if current is not None and not current.done():
                return True
            if len(stream_tasks) >= MAX_ACTIVE_RUN_STREAMS:
                return False
            stream_tasks[run_id] = asyncio.create_task(
                stream_run(run_id, after_seq),
                name=f"channel-fast-lane:{run_id}",
            )
            return True

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await send_error("invalid-frame", "INVALID_JSON", retryable=False)
                    continue
                if not isinstance(payload, dict):
                    await send_error(
                        "invalid-frame",
                        "INVALID_FRAME",
                        retryable=False,
                    )
                    continue
                request_id = str(payload.get("request_id") or "invalid-frame")
                frame_type = str(payload.get("type") or "")
                try:
                    if any(
                        key in payload
                        for key in (
                            "channel_fast_lane_schema_version",
                            "delivery_id",
                            "owner_delivery_id",
                            "client_message_id",
                            "last_disposition_seq",
                            "last_event_seq",
                            "disposition_seq",
                        )
                    ):
                        raise ValueError("v2 frame is not valid on a v1 connection")
                    if frame_type == "channel.turn.submit":
                        frame = ChannelFastLaneSubmitFrame.model_validate(payload)
                        if (
                            frame.gateway_instance_id != gateway_instance_id
                            or frame.channel_lease_epoch != connection_epoch
                        ):
                            raise PermissionError("CHANNEL_EPOCH_STALE")
                        envelope = frame.envelope
                        turn = envelope.payload
                        if turn.type != "internal_turn" or turn.source not in {
                            "personal_wecom",
                            "application_wecom",
                        }:
                            raise PermissionError("CHANNEL_SOURCE_FORBIDDEN")
                        lease = turn.channel_execution_lease
                        if lease is None:
                            raise PermissionError("CHANNEL_LEASE_REQUIRED")
                        _validate_lease(
                            lease,
                            envelope=envelope,
                            gateway_instance_id=gateway_instance_id,
                            connection_epoch=connection_epoch,
                            secret=dependencies.api_server_key,
                        )
                        dependencies.config.require_revision(
                            turn.identity.config_revision
                        )
                        if (
                            len(turn.prompt.encode("utf-8"))
                            > turn.identity.max_prompt_bytes
                        ):
                            raise PermissionError("PROMPT_TOO_LARGE")
                        current_stream = stream_tasks.get(turn.run_id)
                        if (
                            len(stream_tasks) >= MAX_ACTIVE_RUN_STREAMS
                            and (
                                current_stream is None
                                or current_stream.done()
                            )
                        ):
                            raise CapacityBusy("FAST_LANE_BACKPRESSURE")
                        submission = await dependencies.runs.submit(
                            identity=_prepare_identity(
                                turn.identity,
                                runtime_id=dependencies.runtime_id,
                            ),
                            client_message_id=turn.client_message_id,
                            payload_digest=canonical_payload_digest(
                                turn.model_dump(
                                    mode="json",
                                    exclude_none=True,
                                    exclude={"channel_execution_lease"},
                                )
                            ),
                            prompt=turn.prompt,
                            source=turn.source,
                            run_id=turn.run_id,
                        )
                        delivery_checkpoint = (
                            await dependencies.store.get_channel_delivery_checkpoint(
                                submission.run.run_id
                            )
                        )
                        after_seq = (
                            delivery_checkpoint.highest_contiguous_seq
                            if delivery_checkpoint is not None
                            else 0
                        )
                        delivery_complete = False
                        if (
                            not submission.created
                            and delivery_checkpoint is not None
                            and submission.run.state in TERMINAL_STATES
                        ):
                            remaining = await dependencies.store.load_run_snapshot(
                                submission.run.run_id,
                                after_seq=after_seq,
                            )
                            delivery_complete = (
                                remaining is not None
                                and not remaining.events
                            )
                        await send(
                            {
                                "schema_version": 1,
                                "type": "channel.turn.ack",
                                "request_id": frame.request_id,
                                "state": "accepted",
                                "run_id": submission.run.run_id,
                                "created": submission.created,
                                "delivery_checkpoint_seq": after_seq,
                                "delivery_complete": delivery_complete,
                            }
                        )
                        # Capacity was reserved before durable Run submission, so
                        # an accepted ACK always has a corresponding event stream.
                        if not delivery_complete:
                            assert start_stream(
                                submission.run.run_id,
                                after_seq,
                            )
                    elif frame_type == "channel.turn.resume":
                        run_id = str(payload.get("run_id") or "")
                        after_seq = int(payload.get("after_seq") or 0)
                        if len(run_id) < 8 or after_seq < 0:
                            raise ValueError("INVALID_RESUME")
                        if await dependencies.store.get_run(run_id) is None:
                            raise RunNotFound(run_id)
                        if not start_stream(run_id, after_seq):
                            raise CapacityBusy("FAST_LANE_BACKPRESSURE")
                        await send(
                            {
                                "schema_version": 1,
                                "type": "channel.turn.resume_ack",
                                "request_id": request_id,
                                "run_id": run_id,
                                "after_seq": after_seq,
                            }
                        )
                    elif frame_type == "channel.delivery.ack":
                        frame = ChannelFastLaneDeliveryAck.model_validate(payload)
                        if (
                            frame.gateway_instance_id != gateway_instance_id
                            or frame.channel_lease_epoch != connection_epoch
                        ):
                            raise PermissionError("CHANNEL_EPOCH_STALE")
                        checkpoint = (
                            await dependencies.store.acknowledge_channel_delivery(
                                run_id=frame.run_id,
                                highest_contiguous_seq=(
                                    frame.highest_contiguous_seq
                                ),
                                gateway_instance_id=frame.gateway_instance_id,
                                channel_lease_epoch=frame.channel_lease_epoch,
                                provider_request_id=frame.provider_request_id,
                                provider_message_id=frame.provider_message_id,
                                projection_version=frame.projection_version,
                                confirmed_at=frame.confirmed_at,
                            )
                        )
                        await send(
                            {
                                "schema_version": 1,
                                "type": "channel.delivery.ack_ok",
                                "request_id": frame.request_id,
                                "run_id": frame.run_id,
                                "highest_contiguous_seq": (
                                    checkpoint.highest_contiguous_seq
                                ),
                            }
                        )
                    elif frame_type == "channel.ping":
                        await send(
                            {
                                "schema_version": 1,
                                "type": "channel.pong",
                                "request_id": request_id,
                            }
                        )
                    else:
                        await send_error(
                            request_id,
                            "FRAME_NOT_IMPLEMENTED",
                            retryable=False,
                        )
                except (TypeError, ValidationError, ValueError):
                    await send_error(request_id, "INVALID_FRAME", retryable=False)
                except IdempotencyConflict:
                    await send_error(
                        request_id,
                        "IDEMPOTENCY_CONFLICT",
                        retryable=False,
                    )
                except (CapacityBusy, SessionBusy, SessionCapacityBusy) as exc:
                    await send_error(request_id, str(exc), retryable=True)
                except (RuntimeConfigUnavailable, RuntimeConfigError) as exc:
                    await send_error(request_id, str(exc), retryable=True)
                except PermissionError as exc:
                    await send_error(request_id, str(exc), retryable=False)
                except RunNotFound:
                    await send_error(request_id, "RUN_NOT_FOUND", retryable=False)
                except Exception:
                    logger.exception(
                        "Runtime Channel Fast Lane frame failed",
                        extra={
                            "runtime_id": dependencies.runtime_id,
                            "binding_id": dependencies.binding_id,
                            "request_id": request_id,
                            "frame_type": frame_type,
                        },
                    )
                    await send_error(request_id, "INTERNAL_ERROR", retryable=True)
        except WebSocketDisconnect:
            pass
        finally:
            for task in tuple(stream_tasks.values()):
                task.cancel()
            await asyncio.gather(*stream_tasks.values(), return_exceptions=True)


async def _serve_channel_fast_lane_v2(
    websocket: WebSocket,
    *,
    dependencies: RuntimeEdgeDependencies,
    gateway_instance_id: str,
    connection_epoch: int,
    send: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    router = dependencies.channel_fast_lane_v2_router
    if router is None:
        await websocket.close(code=1012, reason="fast lane v2 unavailable")
        return

    live_deliveries: dict[str, dict[str, Any]] = {}
    live_stream_tasks: dict[str, asyncio.Task[None]] = {}

    def remove_live_delivery(delivery_id: str) -> None:
        current = live_deliveries.pop(delivery_id, None)
        if current is None:
            return
        canonical_run_id = str(current["canonical_run_id"])
        if any(
            item["canonical_run_id"] == canonical_run_id
            for item in live_deliveries.values()
        ):
            return
        task = live_stream_tasks.get(canonical_run_id)
        if task is not None and not task.done():
            task.cancel()

    async def stream_live_run(
        canonical_run_id: str,
        after_seq: int,
    ) -> None:
        completed_normally = False
        try:
            async for chunk in dependencies.runs.subscribe(
                canonical_run_id,
                after_seq=max(0, after_seq),
            ):
                event = json.loads(chunk)
                if not isinstance(event, dict):
                    continue
                seq = int(event.get("seq") or 0)
                if (
                    event.get("schema_version") != 1
                    or event.get("type") != "run.event"
                    or event.get("run_id") != canonical_run_id
                    or seq <= 0
                ):
                    logger.warning(
                        "Runtime Channel Fast Lane v2 ignored invalid live event",
                        extra={
                            "runtime_id": dependencies.runtime_id,
                            "binding_id": dependencies.binding_id,
                            "canonical_run_id": canonical_run_id,
                        },
                    )
                    continue
                candidates = tuple(
                    (delivery_id, registration)
                    for delivery_id, registration in live_deliveries.items()
                    if registration["canonical_run_id"] == canonical_run_id
                )
                if not candidates:
                    continue
                delivery_id, registration = min(
                    candidates,
                    key=lambda item: (
                        int(item[1]["start_after_seq"]),
                        item[0],
                    ),
                )
                if seq <= int(registration["last_sent_seq"]):
                    continue
                await send(
                    {
                        "schema_version": 1,
                        "channel_fast_lane_schema_version": 2,
                        "type": "run.event",
                        "delivery_id": delivery_id,
                        "canonical_run_id": canonical_run_id,
                        "run_id": canonical_run_id,
                        "seq": seq,
                        "event_type": event.get("event_type"),
                        "emitted_at": event.get("emitted_at"),
                        "data": event.get("data") or {},
                    }
                )
                current = live_deliveries.get(delivery_id)
                if (
                    current is registration
                    and current["canonical_run_id"] == canonical_run_id
                ):
                    current["last_sent_seq"] = seq
            completed_normally = True
        except asyncio.CancelledError:
            raise
        except RunNotFound:
            return
        except Exception:
            logger.exception(
                "Runtime Channel Fast Lane v2 live stream failed",
                extra={
                    "runtime_id": dependencies.runtime_id,
                    "binding_id": dependencies.binding_id,
                    "canonical_run_id": canonical_run_id,
                },
            )
        finally:
            if completed_normally:
                for delivery_id, registration in tuple(
                    live_deliveries.items()
                ):
                    if registration["canonical_run_id"] == canonical_run_id:
                        live_deliveries.pop(delivery_id, None)
            current = live_stream_tasks.get(canonical_run_id)
            if current is asyncio.current_task():
                live_stream_tasks.pop(canonical_run_id, None)

    def activate_live_delivery(
        disposition: ChannelFastLaneV2DispositionFrame,
        *,
        observed_after_seq: int,
    ) -> None:
        delivery_id = disposition.delivery_id
        if disposition.start_after_seq is None or disposition.state not in {
            "ordinary_run_accepted",
            "redirect_accepted",
            "redirect_consumed",
            "promoted",
        }:
            remove_live_delivery(delivery_id)
            return
        canonical_run_id = disposition.canonical_run_id
        previous = live_deliveries.get(delivery_id)
        if (
            previous is not None
            and previous["canonical_run_id"] != canonical_run_id
        ):
            remove_live_delivery(delivery_id)
            previous = None
        last_sent_seq = max(
            disposition.start_after_seq,
            observed_after_seq,
            (
                int(previous["last_sent_seq"])
                if previous is not None
                else 0
            ),
        )
        live_deliveries[delivery_id] = {
            "canonical_run_id": canonical_run_id,
            "start_after_seq": disposition.start_after_seq,
            "last_sent_seq": last_sent_seq,
        }
        current = live_stream_tasks.get(canonical_run_id)
        if current is not None and not current.done():
            return
        if len(live_stream_tasks) >= MAX_ACTIVE_RUN_STREAMS:
            logger.warning(
                "Runtime Channel Fast Lane v2 live stream capacity reached",
                extra={
                    "runtime_id": dependencies.runtime_id,
                    "binding_id": dependencies.binding_id,
                    "canonical_run_id": canonical_run_id,
                },
            )
            return
        run_after_seq = min(
            int(item["last_sent_seq"])
            for item in live_deliveries.values()
            if item["canonical_run_id"] == canonical_run_id
        )
        live_stream_tasks[canonical_run_id] = asyncio.create_task(
            stream_live_run(canonical_run_id, run_after_seq),
            name=f"channel-fast-lane-v2:{canonical_run_id}",
        )

    async def send_error(
        request_id: str,
        code: str,
        *,
        retryable: bool,
    ) -> None:
        await send(
            {
                "schema_version": 1,
                "channel_fast_lane_schema_version": 2,
                "type": "channel.error",
                "request_id": request_id,
                "error_code": code,
                "retryable": retryable,
            }
        )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await send_error(
                    "invalid-frame",
                    "INVALID_JSON",
                    retryable=False,
                )
                continue
            if not isinstance(payload, dict):
                await send_error(
                    "invalid-frame",
                    "INVALID_FRAME",
                    retryable=False,
                )
                continue
            request_id = str(payload.get("request_id") or "invalid-frame")
            frame_type = str(payload.get("type") or "")
            try:
                if payload.get("channel_fast_lane_schema_version") != 2:
                    raise ValueError("v1 frame is not valid on a v2 connection")
                if frame_type == "channel.turn.submit":
                    frame = ChannelFastLaneV2SubmitFrame.model_validate(payload)
                    _validate_v2_connection_identity(
                        frame,
                        gateway_instance_id=gateway_instance_id,
                        connection_epoch=connection_epoch,
                    )
                    envelope = frame.envelope
                    turn = envelope.payload
                    if turn.type != "internal_turn" or turn.source not in {
                        "personal_wecom",
                        "application_wecom",
                    }:
                        raise PermissionError("CHANNEL_SOURCE_FORBIDDEN")
                    lease = turn.channel_execution_lease
                    if lease is None:
                        raise PermissionError("CHANNEL_LEASE_REQUIRED")
                    _validate_lease(
                        lease,
                        envelope=envelope,
                        gateway_instance_id=gateway_instance_id,
                        connection_epoch=connection_epoch,
                        secret=dependencies.api_server_key,
                    )
                    dependencies.config.require_revision(
                        turn.identity.config_revision
                    )
                    if (
                        len(turn.prompt.encode("utf-8"))
                        > turn.identity.max_prompt_bytes
                    ):
                        raise PermissionError("PROMPT_TOO_LARGE")
                    disposition = await router.submit_turn(
                        frame=frame,
                        identity=_prepare_identity(
                            turn.identity,
                            runtime_id=dependencies.runtime_id,
                        ),
                        payload_digest=canonical_payload_digest(
                            turn.model_dump(
                                mode="json",
                                exclude_none=True,
                                exclude={"channel_execution_lease"},
                            )
                        ),
                    )
                    if not _valid_v2_disposition(
                        disposition,
                        delivery_id=frame.delivery_id,
                        client_message_id=frame.client_message_id,
                    ):
                        await send_error(
                            frame.request_id,
                            "INVALID_DURABLE_DISPOSITION",
                            retryable=False,
                        )
                        continue
                    await send(
                        disposition.model_dump(
                            mode="json",
                            exclude_none=False,
                        )
                    )
                    activate_live_delivery(
                        disposition,
                        observed_after_seq=0,
                    )
                elif frame_type == "channel.turn.resume":
                    frame = ChannelFastLaneV2ResumeFrame.model_validate(payload)
                    _validate_v2_connection_identity(
                        frame,
                        gateway_instance_id=gateway_instance_id,
                        connection_epoch=connection_epoch,
                    )
                    batch = await router.resume_delivery(frame=frame)
                    if not isinstance(batch, ChannelFastLaneV2ResumeBatch) or not (
                        _valid_v2_disposition(
                            batch.disposition,
                            delivery_id=frame.delivery_id,
                        )
                    ):
                        await send_error(
                            frame.request_id,
                            "INVALID_DURABLE_DISPOSITION",
                            retryable=False,
                        )
                        continue
                    await send(
                        batch.disposition.model_dump(
                            mode="json",
                            exclude_none=False,
                        )
                    )
                    if any(
                        not _valid_v2_delivery_event(
                            event,
                            delivery_id=frame.delivery_id,
                            canonical_run_id=(
                                batch.disposition.canonical_run_id
                            ),
                        )
                        for event in batch.events
                    ):
                        await send_error(
                            frame.request_id,
                            "INVALID_DURABLE_EVENT",
                            retryable=False,
                        )
                        continue
                    for event in batch.events:
                        await send(event)
                    activate_live_delivery(
                        batch.disposition,
                        observed_after_seq=max(
                            (
                                _v2_resume_event_after_seq(
                                    frame,
                                    batch.disposition,
                                ),
                                *(
                                    int(event.get("seq") or 0)
                                    for event in batch.events
                                ),
                            ),
                        ),
                    )
                elif frame_type == "channel.turn.disposition.ack":
                    frame = ChannelFastLaneV2DispositionAck.model_validate(
                        payload
                    )
                    _validate_v2_connection_identity(
                        frame,
                        gateway_instance_id=gateway_instance_id,
                        connection_epoch=connection_epoch,
                    )
                    await router.acknowledge_disposition(frame=frame)
                    await send(
                        {
                            "schema_version": 1,
                            "channel_fast_lane_schema_version": 2,
                            "type": "channel.turn.disposition.ack_ok",
                            "request_id": frame.request_id,
                            "delivery_id": frame.delivery_id,
                            "disposition_seq": frame.disposition_seq,
                        }
                    )
                elif frame_type == "channel.delivery.ack":
                    frame = ChannelFastLaneV2DeliveryAck.model_validate(payload)
                    _validate_v2_connection_identity(
                        frame,
                        gateway_instance_id=gateway_instance_id,
                        connection_epoch=connection_epoch,
                    )
                    highest_contiguous_seq = (
                        await router.acknowledge_provider_delivery(frame=frame)
                    )
                    if (
                        not isinstance(highest_contiguous_seq, int)
                        or highest_contiguous_seq
                        < frame.highest_contiguous_seq
                    ):
                        raise ValueError("invalid provider delivery checkpoint")
                    await send(
                        {
                            "schema_version": 1,
                            "channel_fast_lane_schema_version": 2,
                            "type": "channel.delivery.ack_ok",
                            "request_id": frame.request_id,
                            "owner_delivery_id": frame.owner_delivery_id,
                            "canonical_run_id": frame.canonical_run_id,
                            "highest_contiguous_seq": highest_contiguous_seq,
                        }
                    )
                elif frame_type == "channel.ping":
                    await send(
                        {
                            "schema_version": 1,
                            "channel_fast_lane_schema_version": 2,
                            "type": "channel.pong",
                            "request_id": request_id,
                        }
                    )
                else:
                    await send_error(
                        request_id,
                        "FRAME_NOT_IMPLEMENTED",
                        retryable=False,
                    )
            except (ValidationError, ValueError):
                await send_error(request_id, "INVALID_FRAME", retryable=False)
            except IdempotencyConflict:
                await send_error(
                    request_id,
                    "IDEMPOTENCY_CONFLICT",
                    retryable=False,
                )
            except (CapacityBusy, SessionBusy, SessionCapacityBusy) as exc:
                await send_error(request_id, str(exc), retryable=True)
            except (RuntimeConfigUnavailable, RuntimeConfigError) as exc:
                await send_error(request_id, str(exc), retryable=True)
            except PermissionError as exc:
                await send_error(request_id, str(exc), retryable=False)
            except RunNotFound:
                await send_error(request_id, "DELIVERY_NOT_FOUND", retryable=False)
            except Exception:
                logger.exception(
                    "Runtime Channel Fast Lane v2 frame failed",
                    extra={
                        "runtime_id": dependencies.runtime_id,
                        "binding_id": dependencies.binding_id,
                        "request_id": request_id,
                        "frame_type": frame_type,
                    },
                )
                await send_error(request_id, "INTERNAL_ERROR", retryable=True)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tuple(live_stream_tasks.values()):
            task.cancel()
        await asyncio.gather(
            *live_stream_tasks.values(),
            return_exceptions=True,
        )


def _validate_v2_connection_identity(
    frame: Any,
    *,
    gateway_instance_id: str,
    connection_epoch: int,
) -> None:
    if (
        frame.gateway_instance_id != gateway_instance_id
        or frame.channel_lease_epoch != connection_epoch
    ):
        raise PermissionError("CHANNEL_EPOCH_STALE")


def _v2_resume_event_after_seq(
    frame: ChannelFastLaneV2ResumeFrame,
    disposition: ChannelFastLaneV2DispositionFrame,
) -> int:
    if (
        disposition.state == "promoted"
        and frame.last_disposition_seq < disposition.disposition_seq
    ):
        return 0
    return frame.last_event_seq


def _valid_v2_disposition(
    disposition: Any,
    *,
    delivery_id: str,
    client_message_id: str | None = None,
) -> bool:
    return (
        isinstance(disposition, ChannelFastLaneV2DispositionFrame)
        and disposition.delivery_id == delivery_id
        and (
            client_message_id is None
            or disposition.client_message_id == client_message_id
        )
    )


def _valid_v2_delivery_event(
    event: Any,
    *,
    delivery_id: str,
    canonical_run_id: str,
) -> bool:
    if not isinstance(event, dict):
        return False
    if (
        event.get("schema_version") != 1
        or event.get("channel_fast_lane_schema_version") != 2
        or event.get("delivery_id") != delivery_id
        or event.get("canonical_run_id") != canonical_run_id
    ):
        return False
    raw_state = event.get("state")
    raw_status = event.get("status")
    raw_type = event.get("type")
    raw_values = {"queued", "steered", "redirected"}
    return (
        raw_state not in raw_values
        and raw_status not in raw_values
        and raw_type not in raw_values
    )


def _validate_lease(
    lease: ChannelExecutionLease,
    *,
    envelope: Any,
    gateway_instance_id: str,
    connection_epoch: int,
    secret: str,
) -> None:
    turn = envelope.payload
    identity = turn.identity
    now = datetime.now(timezone.utc)
    expected_target_type = (
        "personal_agent"
        if turn.source == "personal_wecom"
        else "application_agent"
    )
    if (
        lease.expires_at <= now
        or lease.issued_at > now
        or lease.runtime_id != envelope.runtime_id
        or lease.binding_id != envelope.binding_id
        or lease.runtime_session_id != identity.session_id
        or lease.org_id != identity.org_id
        or lease.subject_user_id != identity.user_id
        or lease.model_entry_id != identity.model_id
        or lease.profile_id != identity.profile_id
        or lease.application_agent_id != identity.application_agent_id
        or lease.gateway_instance_id != gateway_instance_id
        or lease.channel_lease_epoch != connection_epoch
        or lease.target_type != expected_target_type
        or lease.admission_generation != envelope.admission_generation
        or lease.admission_generation != identity.admission_generation
    ):
        raise PermissionError("CHANNEL_LEASE_INVALID")
    expected_signature = channel_lease_signature(secret, lease)
    if not hmac.compare_digest(lease.signature, expected_signature):
        raise PermissionError("CHANNEL_LEASE_SIGNATURE_INVALID")


def _prepare_identity(identity: Any, *, runtime_id: str) -> PrepareIdentity:
    capabilities = identity.runtime_capabilities
    return PrepareIdentity(
        runtime_id=runtime_id,
        session_id=identity.session_id,
        profile_id=identity.profile_id,
        model_id=identity.model_id,
        upstream_model_id=identity.upstream_model_id,
        context_length=getattr(identity, "context_length", 128_000),
        runtime_capabilities=(
            capabilities.model_dump(mode="json")
            if capabilities is not None
            else {}
        ),
        config_revision=identity.config_revision,
        admission_generation=identity.admission_generation,
        max_output_tokens=identity.max_output_tokens,
        user_id=identity.user_id,
        org_id=identity.org_id,
        runtime_agent_profile_id=identity.runtime_agent_profile_id,
        application_agent_id=getattr(identity, "application_agent_id", None),
        profile_desired_revision=identity.profile_desired_revision,
        profile_applied_revision=identity.profile_applied_revision,
        profile_digest=identity.profile_digest,
        skill_digest=identity.skill_digest,
        mcp_digest=identity.mcp_digest,
        knowledge_digest=identity.knowledge_digest,
        provider_route=(
            identity.provider_route.model_dump(mode="json")
            if getattr(identity, "provider_route", None) is not None
            else None
        ),
        provider_route_digest=(
            identity.provider_route.digest
            if getattr(identity, "provider_route", None) is not None
            else None
        ),
    )
