from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import secrets
import socket
import os
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import httpx
import websockets

from app.config import settings
from app.schemas.runtime_internal import (
    ChannelFastLaneV2DeliveryAck,
    ChannelFastLaneV2DispositionAck,
    ChannelFastLaneV2DispositionFrame,
    ChannelFastLaneV2ResumeFrame,
    ChannelFastLaneV2SubmitFrame,
    ChannelExecutionLease,
    RuntimeCommandAck,
    RuntimeCommandEnvelope,
)
from app.services.runtime_commands import (
    RuntimeCommandClient,
    RuntimeCommandError,
    RuntimeCommandTarget,
)


logger = logging.getLogger(__name__)
CHANNEL_FAST_LANE_PROTOCOL = "hermes-channel-fast-lane-v1"
CHANNEL_FAST_LANE_V2_PROTOCOL = "hermes-channel-fast-lane-v2"
CHANNEL_LEASE_TTL_SECONDS = 120
FAST_LANE_ACK_TIMEOUT_SECONDS = 10
FAST_LANE_EVENT_QUEUE_SIZE = 256


class RuntimeChannelFastLaneV2TransportError(RuntimeCommandError):
    """Transport failure with a closed zero-byte/ambiguous-send boundary."""

    def __init__(self, message: str, *, may_have_been_sent: bool) -> None:
        super().__init__(message)
        self.may_have_been_sent = may_have_been_sent


def channel_gateway_instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:im-gateway"


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


def issue_channel_execution_lease(
    *,
    target: RuntimeCommandTarget,
    org_id: int,
    channel_id: int,
    target_type: str,
    target_id: int,
    subject_user_id: int,
    runtime_session_id: str,
    model_entry_id: str,
    profile_id: str,
    application_agent_id: int | None = None,
    gateway_instance_id: str,
    channel_lease_epoch: int,
    channel_generation: int,
    config_generation: int,
    authorization_epoch: int = 1,
    issued_at: datetime | None = None,
) -> ChannelExecutionLease:
    now = issued_at or datetime.now(timezone.utc)
    unsigned = ChannelExecutionLease(
        lease_id=f"chl_{uuid4().hex}",
        org_id=org_id,
        channel_id=channel_id,
        target_type=target_type,
        target_id=target_id,
        subject_user_id=subject_user_id,
        runtime_id=target.runtime_id,
        binding_id=target.binding_id,
        runtime_session_id=runtime_session_id,
        model_entry_id=model_entry_id,
        profile_id=profile_id,
        application_agent_id=application_agent_id,
        gateway_instance_id=gateway_instance_id,
        channel_lease_epoch=channel_lease_epoch,
        channel_generation=channel_generation,
        config_generation=config_generation,
        admission_generation=target.admission_generation,
        authorization_epoch=authorization_epoch,
        issued_at=now,
        expires_at=now + timedelta(seconds=CHANNEL_LEASE_TTL_SECONDS),
        signature="0" * 64,
    )
    return unsigned.model_copy(
        update={
            "signature": channel_lease_signature(
                target.api_server_key,
                unsigned,
            )
        }
    )


class _FastLaneConnectionProtocol(Protocol):
    async def submit(
        self,
        envelope: RuntimeCommandEnvelope,
        lease: ChannelExecutionLease,
    ) -> RuntimeCommandAck: ...

    async def wait_run(
        self,
        *,
        run_id: str,
        event_sink: Callable[[str, dict[str, object]], Awaitable[None]] | None,
        timeout_seconds: float,
    ) -> None: ...

    async def delivery_ack(self, payload: dict[str, Any]) -> int: ...


class _FastLaneV2ConnectionProtocol(Protocol):
    async def submit(
        self,
        *,
        delivery_id: str,
        client_message_id: str,
        envelope: RuntimeCommandEnvelope,
        lease: ChannelExecutionLease,
    ) -> ChannelFastLaneV2DispositionFrame: ...

    async def resume(
        self,
        *,
        delivery_id: str,
        last_disposition_seq: int,
        last_event_seq: int,
    ) -> ChannelFastLaneV2DispositionFrame: ...

    async def acknowledge_disposition(
        self,
        disposition: ChannelFastLaneV2DispositionFrame,
    ) -> None: ...

    async def acknowledge_provider_delivery(
        self,
        *,
        owner_delivery_id: str,
        canonical_run_id: str,
        highest_contiguous_seq: int,
        provider_request_id: str,
        provider_message_id: str | None,
        projection_version: int,
        confirmed_at: datetime,
    ) -> int: ...

    async def next_event(
        self,
        *,
        delivery_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any] | None: ...

    def last_event_seq(self, delivery_id: str) -> int: ...


class RuntimeChannelFastLaneConnection:
    def __init__(
        self,
        *,
        target: RuntimeCommandTarget,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        ingress_base_url: str | None = None,
    ) -> None:
        self.target = target
        self.gateway_instance_id = gateway_instance_id
        self.channel_lease_epoch = channel_lease_epoch
        self.ingress_base_url = (
            ingress_base_url or settings.HERMES_RUNTIME_INGRESS_BASE_URL
        ).rstrip("/")
        self._websocket: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._events: dict[str, asyncio.Queue[dict[str, Any] | BaseException]] = {}
        self._last_seq: dict[str, int] = {}
        self._delivery_complete: set[str] = set()

    async def submit(
        self,
        envelope: RuntimeCommandEnvelope,
        lease: ChannelExecutionLease,
    ) -> RuntimeCommandAck:
        if (
            lease.gateway_instance_id != self.gateway_instance_id
            or lease.channel_lease_epoch != self.channel_lease_epoch
        ):
            raise RuntimeCommandError("Fast Lane lease does not match connection")
        self._events.setdefault(
            str(envelope.payload.run_id),
            asyncio.Queue(maxsize=FAST_LANE_EVENT_QUEUE_SIZE),
        )
        request_id = f"fl_{uuid4().hex}"
        response = await self._request(
            {
                "schema_version": 1,
                "type": "channel.turn.submit",
                "request_id": request_id,
                "gateway_instance_id": self.gateway_instance_id,
                "channel_lease_epoch": self.channel_lease_epoch,
                "envelope": envelope.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
            },
            request_id=request_id,
        )
        if response.get("type") == "channel.error":
            return RuntimeCommandAck(
                command_id=envelope.command_id,
                state="rejected",
                error_code=str(response.get("error_code") or "FAST_LANE_REJECTED"),
            )
        if response.get("type") != "channel.turn.ack":
            raise RuntimeCommandError("Fast Lane returned an invalid turn ACK")
        run_id = str(response.get("run_id") or "")
        if bool(response.get("delivery_complete")):
            self._delivery_complete.add(run_id)
        return RuntimeCommandAck(
            command_id=envelope.command_id,
            state="accepted",
            run_id=run_id,
        )

    async def wait_run(
        self,
        *,
        run_id: str,
        event_sink: Callable[[str, dict[str, object]], Awaitable[None]] | None,
        timeout_seconds: float,
    ) -> None:
        queue = self._events.setdefault(
            run_id,
            asyncio.Queue(maxsize=FAST_LANE_EVENT_QUEUE_SIZE),
        )
        if run_id in self._delivery_complete:
            self._delivery_complete.discard(run_id)
            self._events.pop(run_id, None)
            self._last_seq.pop(run_id, None)
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        terminal = False
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RuntimeCommandError("Fast Lane Run timed out")
                item = await asyncio.wait_for(queue.get(), timeout=remaining)
                if isinstance(item, BaseException):
                    raise RuntimeCommandError(
                        "Fast Lane connection was lost"
                    ) from item
                event_name = str(item.get("event_type") or "")
                data = dict(item.get("data") or {})
                seq = int(item.get("seq") or 0)
                if seq > 0:
                    data.setdefault("seq", seq)
                data.setdefault("runtime_run_id", run_id)
                if event_sink is not None:
                    await event_sink(event_name, data)
                if event_name == "chat.done":
                    terminal = True
                    return
        finally:
            if terminal:
                self._events.pop(run_id, None)
                self._last_seq.pop(run_id, None)

    async def delivery_ack(self, payload: dict[str, Any]) -> int:
        request_id = str(payload.get("request_id") or f"delivery_{uuid4().hex}")
        response = await self._request(
            {
                **payload,
                "schema_version": 1,
                "type": "channel.delivery.ack",
                "request_id": request_id,
                "gateway_instance_id": self.gateway_instance_id,
                "channel_lease_epoch": self.channel_lease_epoch,
            },
            request_id=request_id,
        )
        if response.get("type") != "channel.delivery.ack_ok":
            raise RuntimeCommandError("Fast Lane delivery ACK was rejected")
        return int(response.get("highest_contiguous_seq") or 0)

    async def close(self) -> None:
        websocket, self._websocket = self._websocket, None
        reader, self._reader_task = self._reader_task, None
        if websocket is not None:
            await websocket.close()
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        self._fail(RuntimeCommandError("Fast Lane connection closed"))
        self._delivery_complete.clear()

    async def _request(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        await self._ensure_connected()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            async with self._send_lock:
                assert self._websocket is not None
                await self._websocket.send(encoded)
            return await asyncio.wait_for(
                future,
                timeout=FAST_LANE_ACK_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            await self.close()
            raise RuntimeCommandError("Fast Lane request failed") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _ensure_connected(self) -> None:
        if self._websocket is not None and self._reader_task is not None:
            if not self._reader_task.done():
                return
        async with self._connect_lock:
            if self._websocket is not None and self._reader_task is not None:
                if not self._reader_task.done():
                    return
            timestamp = int(datetime.now(timezone.utc).timestamp())
            nonce = secrets.token_urlsafe(24)
            signature = channel_handshake_signature(
                self.target.api_server_key,
                runtime_id=self.target.runtime_id,
                binding_id=self.target.binding_id,
                gateway_instance_id=self.gateway_instance_id,
                channel_lease_epoch=self.channel_lease_epoch,
                timestamp=timestamp,
                nonce=nonce,
            )
            headers = {
                "Authorization": f"Bearer {self.target.api_server_key}",
                "X-Hermes-Runtime-Id": self.target.runtime_id,
                "X-Hermes-Binding-Id": self.target.binding_id,
                "X-Hermes-Gateway-Instance-Id": self.gateway_instance_id,
                "X-Hermes-Channel-Lease-Epoch": str(
                    self.channel_lease_epoch
                ),
                "X-Hermes-Timestamp": str(timestamp),
                "X-Hermes-Connection-Nonce": nonce,
                "X-Hermes-Signature": signature,
            }
            ingress = urlparse(self.ingress_base_url)
            if not ingress.hostname:
                raise RuntimeCommandError("Fast Lane ingress host is invalid")
            self._websocket = await websockets.connect(
                _websocket_url(
                    self.ingress_base_url,
                    endpoint_host_header=(
                        self.target.endpoint_host_header
                    ),
                ),
                subprotocols=[CHANNEL_FAST_LANE_PROTOCOL],
                additional_headers=headers,
                host=ingress.hostname,
                port=(
                    ingress.port
                    or (443 if ingress.scheme == "https" else 80)
                ),
                proxy=None,
                compression=None,
                open_timeout=5,
                ping_interval=20,
                ping_timeout=20,
                max_queue=(32, 8),
                write_limit=(64 * 1024, 16 * 1024),
            )
            self._reader_task = asyncio.create_task(
                self._read_loop(),
                name=(
                    "channel-fast-lane-reader:"
                    f"{self.target.runtime_id}:{self.channel_lease_epoch}"
                ),
            )

    async def _read_loop(self) -> None:
        assert self._websocket is not None
        try:
            async for raw in self._websocket:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise RuntimeCommandError("Fast Lane frame is invalid")
                if payload.get("type") == "run.event":
                    run_id = str(payload.get("run_id") or "")
                    seq = int(payload.get("seq") or 0)
                    if not run_id or seq <= self._last_seq.get(run_id, 0):
                        continue
                    self._last_seq[run_id] = seq
                    queue = self._events.setdefault(
                        run_id,
                        asyncio.Queue(maxsize=FAST_LANE_EVENT_QUEUE_SIZE),
                    )
                    try:
                        queue.put_nowait(payload)
                    except asyncio.QueueFull as exc:
                        raise RuntimeCommandError(
                            "Fast Lane event queue reached its bound"
                        ) from exc
                    continue
                request_id = str(payload.get("request_id") or "")
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(payload)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fail(exc)
        finally:
            self._websocket = None

    def _fail(self, error: BaseException) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        for queue in tuple(self._events.values()):
            try:
                queue.put_nowait(error)
            except asyncio.QueueFull:
                pass


class RuntimeChannelFastLaneV2Connection:
    """One v2 connection keyed by durable delivery identities.

    Successful submit/resume responses intentionally carry no request id, so
    request errors and durable dispositions are tracked independently.  Once a
    WebSocket send has been attempted, any transport failure is treated as
    ambiguous and callers may only resume/retry the same ``delivery_id``.
    """

    def __init__(
        self,
        *,
        target: RuntimeCommandTarget,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        ingress_base_url: str | None = None,
    ) -> None:
        self.target = target
        self.gateway_instance_id = gateway_instance_id
        self.channel_lease_epoch = channel_lease_epoch
        self.ingress_base_url = (
            ingress_base_url or settings.HERMES_RUNTIME_INGRESS_BASE_URL
        ).rstrip("/")
        self._websocket: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._pending_requests: dict[
            str, asyncio.Future[dict[str, Any]]
        ] = {}
        self._pending_dispositions: dict[
            str, asyncio.Future[ChannelFastLaneV2DispositionFrame]
        ] = {}
        self._events: dict[
            str, asyncio.Queue[dict[str, Any] | BaseException]
        ] = {}
        self._last_dispositions: dict[
            str, ChannelFastLaneV2DispositionFrame
        ] = {}
        self._current_canonical_run_ids: dict[str, str] = {}
        self._last_event_seq: dict[str, int] = {}
        self._delivery_locks: dict[str, asyncio.Lock] = {}

    async def submit(
        self,
        *,
        delivery_id: str,
        client_message_id: str,
        envelope: RuntimeCommandEnvelope,
        lease: ChannelExecutionLease,
    ) -> ChannelFastLaneV2DispositionFrame:
        self._validate_lease(lease)
        if envelope.payload.client_message_id != client_message_id:
            raise RuntimeCommandError(
                "Fast Lane v2 client message identity does not match envelope"
            )
        request_id = f"flv2_{uuid4().hex}"
        frame = ChannelFastLaneV2SubmitFrame(
            request_id=request_id,
            gateway_instance_id=self.gateway_instance_id,
            channel_lease_epoch=self.channel_lease_epoch,
            delivery_id=delivery_id,
            client_message_id=client_message_id,
            envelope=envelope,
        )
        async with self._delivery_lock(delivery_id):
            return await self._request_disposition(
                frame.model_dump(mode="json", exclude_none=True),
                request_id=request_id,
                delivery_id=delivery_id,
            )

    async def resume(
        self,
        *,
        delivery_id: str,
        last_disposition_seq: int,
        last_event_seq: int,
    ) -> ChannelFastLaneV2DispositionFrame:
        request_id = f"resumev2_{uuid4().hex}"
        frame = ChannelFastLaneV2ResumeFrame(
            request_id=request_id,
            gateway_instance_id=self.gateway_instance_id,
            channel_lease_epoch=self.channel_lease_epoch,
            delivery_id=delivery_id,
            last_disposition_seq=last_disposition_seq,
            last_event_seq=last_event_seq,
        )
        async with self._delivery_lock(delivery_id):
            return await self._request_disposition(
                frame.model_dump(mode="json"),
                request_id=request_id,
                delivery_id=delivery_id,
            )

    async def acknowledge_disposition(
        self,
        disposition: ChannelFastLaneV2DispositionFrame,
    ) -> None:
        request_id = f"dispositionv2_{uuid4().hex}"
        frame = ChannelFastLaneV2DispositionAck(
            request_id=request_id,
            gateway_instance_id=self.gateway_instance_id,
            channel_lease_epoch=self.channel_lease_epoch,
            delivery_id=disposition.delivery_id,
            disposition_seq=disposition.disposition_seq,
        )
        response = await self._request(
            frame.model_dump(mode="json"),
            request_id=request_id,
        )
        if (
            response.get("type") != "channel.turn.disposition.ack_ok"
            or response.get("request_id") != frame.request_id
            or response.get("delivery_id") != disposition.delivery_id
            or int(response.get("disposition_seq") or 0)
            != disposition.disposition_seq
        ):
            raise RuntimeCommandError(
                "Fast Lane v2 disposition ACK was rejected"
            )

    async def acknowledge_provider_delivery(
        self,
        *,
        owner_delivery_id: str,
        canonical_run_id: str,
        highest_contiguous_seq: int,
        provider_request_id: str,
        provider_message_id: str | None,
        projection_version: int,
        confirmed_at: datetime,
    ) -> int:
        request_id = f"deliveryv2_{uuid4().hex}"
        frame = ChannelFastLaneV2DeliveryAck(
            request_id=request_id,
            gateway_instance_id=self.gateway_instance_id,
            channel_lease_epoch=self.channel_lease_epoch,
            owner_delivery_id=owner_delivery_id,
            canonical_run_id=canonical_run_id,
            highest_contiguous_seq=highest_contiguous_seq,
            provider_request_id=provider_request_id,
            provider_message_id=provider_message_id,
            projection_version=projection_version,
            confirmed_at=confirmed_at,
        )
        response = await self._request(
            frame.model_dump(mode="json", exclude_none=True),
            request_id=request_id,
        )
        if (
            response.get("type") != "channel.delivery.ack_ok"
            or response.get("request_id") != frame.request_id
            or response.get("owner_delivery_id") != owner_delivery_id
            or response.get("canonical_run_id") != canonical_run_id
        ):
            raise RuntimeCommandError(
                "Fast Lane v2 Provider delivery ACK was rejected"
            )
        return int(response.get("highest_contiguous_seq") or 0)

    async def next_event(
        self,
        *,
        delivery_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        queue = self._events.setdefault(
            delivery_id,
            asyncio.Queue(maxsize=FAST_LANE_EVENT_QUEUE_SIZE),
        )
        try:
            item = await asyncio.wait_for(queue.get(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return None
        if isinstance(item, BaseException):
            raise RuntimeChannelFastLaneV2TransportError(
                "Fast Lane v2 connection was lost",
                may_have_been_sent=True,
            ) from item
        return item

    def last_disposition(
        self,
        delivery_id: str,
    ) -> ChannelFastLaneV2DispositionFrame | None:
        return self._last_dispositions.get(delivery_id)

    def last_event_seq(self, delivery_id: str) -> int:
        return self._last_event_seq.get(delivery_id, 0)

    async def close(self) -> None:
        websocket, self._websocket = self._websocket, None
        reader, self._reader_task = self._reader_task, None
        if websocket is not None:
            await websocket.close()
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        self._fail(RuntimeCommandError("Fast Lane v2 connection closed"))

    def _validate_lease(self, lease: ChannelExecutionLease) -> None:
        if (
            lease.gateway_instance_id != self.gateway_instance_id
            or lease.channel_lease_epoch != self.channel_lease_epoch
            or lease.runtime_id != self.target.runtime_id
            or lease.binding_id != self.target.binding_id
            or lease.admission_generation != self.target.admission_generation
        ):
            raise RuntimeCommandError(
                "Fast Lane v2 lease does not match connection"
            )

    def _delivery_lock(self, delivery_id: str) -> asyncio.Lock:
        return self._delivery_locks.setdefault(delivery_id, asyncio.Lock())

    async def _request_disposition(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
        delivery_id: str,
    ) -> ChannelFastLaneV2DispositionFrame:
        await self._ensure_connected_for_request()
        loop = asyncio.get_running_loop()
        request_future: asyncio.Future[dict[str, Any]] = loop.create_future()
        disposition_future: asyncio.Future[
            ChannelFastLaneV2DispositionFrame
        ] = loop.create_future()
        self._pending_requests[request_id] = request_future
        self._pending_dispositions[delivery_id] = disposition_future
        try:
            await self._send_after_connect(payload)
            done, _pending = await asyncio.wait(
                {request_future, disposition_future},
                timeout=FAST_LANE_ACK_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise RuntimeChannelFastLaneV2TransportError(
                    "Fast Lane v2 disposition timed out",
                    may_have_been_sent=True,
                )
            completed = done.pop()
            if completed is request_future:
                response = request_future.result()
                if response.get("type") == "channel.error":
                    raise RuntimeCommandError(
                        str(response.get("error_code") or "FAST_LANE_V2_REJECTED")
                    )
                raise RuntimeCommandError(
                    "Fast Lane v2 returned an invalid disposition response"
                )
            return disposition_future.result()
        except asyncio.CancelledError:
            raise
        except (RuntimeChannelFastLaneV2TransportError, RuntimeCommandError):
            raise
        except Exception as exc:
            raise RuntimeChannelFastLaneV2TransportError(
                "Fast Lane v2 disposition outcome is ambiguous",
                may_have_been_sent=True,
            ) from exc
        finally:
            self._pending_requests.pop(request_id, None)
            self._pending_dispositions.pop(delivery_id, None)
            if not request_future.done():
                request_future.cancel()
            if not disposition_future.done():
                disposition_future.cancel()

    async def _request(
        self,
        payload: dict[str, Any],
        *,
        request_id: str,
    ) -> dict[str, Any]:
        await self._ensure_connected_for_request()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_requests[request_id] = future
        try:
            await self._send_after_connect(payload)
            response = await asyncio.wait_for(
                future,
                timeout=FAST_LANE_ACK_TIMEOUT_SECONDS,
            )
            if response.get("type") == "channel.error":
                raise RuntimeCommandError(
                    str(response.get("error_code") or "FAST_LANE_V2_REJECTED")
                )
            return response
        except asyncio.CancelledError:
            raise
        except (RuntimeChannelFastLaneV2TransportError, RuntimeCommandError):
            raise
        except Exception as exc:
            raise RuntimeChannelFastLaneV2TransportError(
                "Fast Lane v2 request outcome is ambiguous",
                may_have_been_sent=True,
            ) from exc
        finally:
            self._pending_requests.pop(request_id, None)

    async def _ensure_connected_for_request(self) -> None:
        try:
            await self._ensure_connected()
        except RuntimeChannelFastLaneV2TransportError:
            raise
        except Exception as exc:
            raise RuntimeChannelFastLaneV2TransportError(
                "Fast Lane v2 connection failed before send",
                may_have_been_sent=False,
            ) from exc

    async def _send_after_connect(self, payload: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            async with self._send_lock:
                if self._websocket is None:
                    raise ConnectionError("Fast Lane v2 connection is absent")
                await self._websocket.send(encoded)
        except Exception as exc:
            raise RuntimeChannelFastLaneV2TransportError(
                "Fast Lane v2 send outcome is ambiguous",
                may_have_been_sent=True,
            ) from exc

    async def _ensure_connected(self) -> None:
        if self._websocket is not None and self._reader_task is not None:
            if not self._reader_task.done():
                return
        async with self._connect_lock:
            if self._websocket is not None and self._reader_task is not None:
                if not self._reader_task.done():
                    return
            timestamp = int(datetime.now(timezone.utc).timestamp())
            nonce = secrets.token_urlsafe(24)
            signature = channel_handshake_signature(
                self.target.api_server_key,
                runtime_id=self.target.runtime_id,
                binding_id=self.target.binding_id,
                gateway_instance_id=self.gateway_instance_id,
                channel_lease_epoch=self.channel_lease_epoch,
                timestamp=timestamp,
                nonce=nonce,
            )
            headers = {
                "Authorization": f"Bearer {self.target.api_server_key}",
                "X-Hermes-Runtime-Id": self.target.runtime_id,
                "X-Hermes-Binding-Id": self.target.binding_id,
                "X-Hermes-Gateway-Instance-Id": self.gateway_instance_id,
                "X-Hermes-Channel-Lease-Epoch": str(
                    self.channel_lease_epoch
                ),
                "X-Hermes-Timestamp": str(timestamp),
                "X-Hermes-Connection-Nonce": nonce,
                "X-Hermes-Signature": signature,
            }
            ingress = urlparse(self.ingress_base_url)
            if not ingress.hostname:
                raise RuntimeCommandError(
                    "Fast Lane v2 ingress host is invalid"
                )
            self._websocket = await websockets.connect(
                _websocket_url(
                    self.ingress_base_url,
                    endpoint_host_header=self.target.endpoint_host_header,
                ),
                subprotocols=[CHANNEL_FAST_LANE_V2_PROTOCOL],
                additional_headers=headers,
                host=ingress.hostname,
                port=(
                    ingress.port
                    or (443 if ingress.scheme == "https" else 80)
                ),
                proxy=None,
                compression=None,
                open_timeout=5,
                ping_interval=20,
                ping_timeout=20,
                max_queue=(32, 8),
                write_limit=(64 * 1024, 16 * 1024),
            )
            if self._websocket.subprotocol != CHANNEL_FAST_LANE_V2_PROTOCOL:
                websocket, self._websocket = self._websocket, None
                await websocket.close()
                raise RuntimeCommandError(
                    "Runtime did not negotiate Fast Lane v2"
                )
            self._reader_task = asyncio.create_task(
                self._read_loop(),
                name=(
                    "channel-fast-lane-v2-reader:"
                    f"{self.target.runtime_id}:{self.channel_lease_epoch}"
                ),
            )

    async def _read_loop(self) -> None:
        assert self._websocket is not None
        try:
            async for raw in self._websocket:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise RuntimeCommandError(
                        "Fast Lane v2 frame is invalid"
                    )
                if payload.get("channel_fast_lane_schema_version") != 2:
                    raise RuntimeCommandError(
                        "Fast Lane v1 frame appeared on a v2 connection"
                    )
                frame_type = str(payload.get("type") or "")
                if frame_type == "channel.turn.disposition":
                    disposition = ChannelFastLaneV2DispositionFrame.model_validate(
                        payload
                    )
                    self._record_disposition(disposition)
                    future = self._pending_dispositions.get(
                        disposition.delivery_id
                    )
                    if future is not None and not future.done():
                        future.set_result(disposition)
                    continue
                if frame_type == "run.event":
                    self._record_event(payload)
                    continue
                request_id = str(payload.get("request_id") or "")
                future = self._pending_requests.get(request_id)
                if future is not None and not future.done():
                    future.set_result(payload)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fail(exc)
        finally:
            self._websocket = None

    def _record_event(self, payload: dict[str, Any]) -> None:
        delivery_id = str(payload.get("delivery_id") or "")
        canonical_run_id = str(payload.get("canonical_run_id") or "")
        run_id = str(payload.get("run_id") or "")
        seq = int(payload.get("seq") or 0)
        if (
            not delivery_id
            or not canonical_run_id
            or run_id != canonical_run_id
            or self._current_canonical_run_ids.get(delivery_id)
            != canonical_run_id
            or seq <= self._last_event_seq.get(delivery_id, 0)
        ):
            return
        self._last_event_seq[delivery_id] = seq
        queue = self._events.setdefault(
            delivery_id,
            asyncio.Queue(maxsize=FAST_LANE_EVENT_QUEUE_SIZE),
        )
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull as exc:
            raise RuntimeCommandError(
                "Fast Lane v2 event queue reached its bound"
            ) from exc

    def _record_disposition(
        self,
        disposition: ChannelFastLaneV2DispositionFrame,
    ) -> None:
        delivery_id = disposition.delivery_id
        previous_run_id = self._current_canonical_run_ids.get(delivery_id)
        if (
            previous_run_id is not None
            and previous_run_id != disposition.canonical_run_id
        ):
            self._last_event_seq[delivery_id] = 0
            queue = self._events.get(delivery_id)
            if queue is not None:
                while not queue.empty():
                    queue.get_nowait()
        self._current_canonical_run_ids[
            delivery_id
        ] = disposition.canonical_run_id
        self._last_dispositions[delivery_id] = disposition

    def _fail(self, error: BaseException) -> None:
        for future in tuple(self._pending_requests.values()):
            if not future.done():
                future.set_exception(error)
        for future in tuple(self._pending_dispositions.values()):
            if not future.done():
                future.set_exception(error)
        for queue in tuple(self._events.values()):
            try:
                queue.put_nowait(error)
            except asyncio.QueueFull:
                pass


class RuntimeChannelFastLaneV2HttpConnection:
    """Stateless v2 HTTP recovery transport for one Runtime lease epoch."""

    def __init__(
        self,
        *,
        target: RuntimeCommandTarget,
        gateway_instance_id: str,
        channel_lease_epoch: int,
        ingress_base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.target = target
        self.gateway_instance_id = gateway_instance_id
        self.channel_lease_epoch = channel_lease_epoch
        self.ingress_base_url = (
            ingress_base_url or settings.HERMES_RUNTIME_INGRESS_BASE_URL
        ).rstrip("/")
        self._client = client
        self._events: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._current_canonical_run_ids: dict[str, str] = {}
        self._last_event_seq: dict[str, int] = {}
        self._delivery_locks: dict[str, asyncio.Lock] = {}

    async def submit(
        self,
        *,
        delivery_id: str,
        client_message_id: str,
        envelope: RuntimeCommandEnvelope,
        lease: ChannelExecutionLease,
    ) -> ChannelFastLaneV2DispositionFrame:
        self._validate_lease(lease)
        if envelope.payload.client_message_id != client_message_id:
            raise RuntimeCommandError(
                "Fast Lane v2 client message identity does not match envelope"
            )
        frame = ChannelFastLaneV2SubmitFrame(
            request_id=f"flv2http_{uuid4().hex}",
            gateway_instance_id=self.gateway_instance_id,
            channel_lease_epoch=self.channel_lease_epoch,
            delivery_id=delivery_id,
            client_message_id=client_message_id,
            envelope=envelope,
        )
        async with self._delivery_lock(delivery_id):
            response = await self._post(
                frame.model_dump(mode="json", exclude_none=True)
            )
        disposition = self._parse_disposition(response)
        if (
            disposition.delivery_id != delivery_id
            or disposition.client_message_id != client_message_id
        ):
            raise RuntimeCommandError(
                "Fast Lane v2 HTTP disposition identity does not match submit"
            )
        return disposition

    async def resume(
        self,
        *,
        delivery_id: str,
        last_disposition_seq: int,
        last_event_seq: int,
    ) -> ChannelFastLaneV2DispositionFrame:
        frame = ChannelFastLaneV2ResumeFrame(
            request_id=f"resumev2http_{uuid4().hex}",
            gateway_instance_id=self.gateway_instance_id,
            channel_lease_epoch=self.channel_lease_epoch,
            delivery_id=delivery_id,
            last_disposition_seq=last_disposition_seq,
            last_event_seq=last_event_seq,
        )
        async with self._delivery_lock(delivery_id):
            response = await self._post(frame.model_dump(mode="json"))
            if response.get("type") != "channel.turn.resume.result":
                raise RuntimeCommandError(
                    "Fast Lane v2 HTTP returned an invalid resume response"
                )
            disposition_payload = response.get("disposition")
            if not isinstance(disposition_payload, dict):
                raise RuntimeCommandError(
                    "Fast Lane v2 HTTP resume omitted its disposition"
                )
            disposition = self._parse_disposition(disposition_payload)
            if disposition.delivery_id != delivery_id:
                raise RuntimeCommandError(
                    "Fast Lane v2 HTTP resume delivery identity does not match"
                )
            events = response.get("events")
            if not isinstance(events, list):
                raise RuntimeCommandError(
                    "Fast Lane v2 HTTP resume returned invalid events"
                )
            effective_last_event_seq = (
                0
                if disposition.state == "promoted"
                and last_disposition_seq < disposition.disposition_seq
                else last_event_seq
            )
            self._last_event_seq[delivery_id] = max(
                self._last_event_seq.get(delivery_id, 0),
                effective_last_event_seq,
            )
            for event in events:
                self._record_event(
                    event,
                    delivery_id=delivery_id,
                    canonical_run_id=disposition.canonical_run_id,
                )
        return disposition

    async def acknowledge_disposition(
        self,
        disposition: ChannelFastLaneV2DispositionFrame,
    ) -> None:
        frame = ChannelFastLaneV2DispositionAck(
            request_id=f"dispositionv2http_{uuid4().hex}",
            gateway_instance_id=self.gateway_instance_id,
            channel_lease_epoch=self.channel_lease_epoch,
            delivery_id=disposition.delivery_id,
            disposition_seq=disposition.disposition_seq,
        )
        response = await self._post(frame.model_dump(mode="json"))
        if (
            response.get("type") != "channel.turn.disposition.ack_ok"
            or response.get("request_id") != frame.request_id
            or response.get("delivery_id") != disposition.delivery_id
            or int(response.get("disposition_seq") or 0)
            != disposition.disposition_seq
        ):
            raise RuntimeCommandError(
                "Fast Lane v2 HTTP disposition ACK was rejected"
            )

    async def acknowledge_provider_delivery(
        self,
        *,
        owner_delivery_id: str,
        canonical_run_id: str,
        highest_contiguous_seq: int,
        provider_request_id: str,
        provider_message_id: str | None,
        projection_version: int,
        confirmed_at: datetime,
    ) -> int:
        frame = ChannelFastLaneV2DeliveryAck(
            request_id=f"deliveryv2http_{uuid4().hex}",
            gateway_instance_id=self.gateway_instance_id,
            channel_lease_epoch=self.channel_lease_epoch,
            owner_delivery_id=owner_delivery_id,
            canonical_run_id=canonical_run_id,
            highest_contiguous_seq=highest_contiguous_seq,
            provider_request_id=provider_request_id,
            provider_message_id=provider_message_id,
            projection_version=projection_version,
            confirmed_at=confirmed_at,
        )
        response = await self._post(
            frame.model_dump(mode="json", exclude_none=True)
        )
        acknowledged_seq = int(response.get("highest_contiguous_seq") or 0)
        if (
            response.get("type") != "channel.delivery.ack_ok"
            or response.get("request_id") != frame.request_id
            or response.get("owner_delivery_id") != owner_delivery_id
            or response.get("canonical_run_id") != canonical_run_id
            or acknowledged_seq < highest_contiguous_seq
        ):
            raise RuntimeCommandError(
                "Fast Lane v2 HTTP Provider delivery ACK was rejected"
            )
        return acknowledged_seq

    async def next_event(
        self,
        *,
        delivery_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        queue = self._events.setdefault(
            delivery_id,
            asyncio.Queue(maxsize=FAST_LANE_EVENT_QUEUE_SIZE),
        )
        try:
            return await asyncio.wait_for(
                queue.get(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            return None

    def last_event_seq(self, delivery_id: str) -> int:
        return self._last_event_seq.get(delivery_id, 0)

    def _validate_lease(self, lease: ChannelExecutionLease) -> None:
        if (
            lease.gateway_instance_id != self.gateway_instance_id
            or lease.channel_lease_epoch != self.channel_lease_epoch
            or lease.runtime_id != self.target.runtime_id
            or lease.binding_id != self.target.binding_id
            or lease.admission_generation != self.target.admission_generation
        ):
            raise RuntimeCommandError(
                "Fast Lane v2 HTTP lease does not match connection"
            )

    def _delivery_lock(self, delivery_id: str) -> asyncio.Lock:
        return self._delivery_locks.setdefault(delivery_id, asyncio.Lock())

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            if self._client is None:
                async with httpx.AsyncClient(
                    trust_env=False,
                    timeout=self._timeout(),
                ) as client:
                    response = await client.post(
                        self._endpoint_url(),
                        content=json.dumps(
                            payload,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ),
                        headers=self._headers(),
                    )
            else:
                response = await self._client.post(
                    self._endpoint_url(),
                    content=json.dumps(
                        payload,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ),
                    headers=self._headers(),
                    timeout=self._timeout(),
                )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise RuntimeChannelFastLaneV2TransportError(
                "Fast Lane v2 HTTP delivery outcome is ambiguous",
                may_have_been_sent=True,
            ) from exc
        try:
            document = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeCommandError(
                "Fast Lane v2 HTTP returned invalid JSON"
            ) from exc
        if not isinstance(document, dict):
            raise RuntimeCommandError(
                "Fast Lane v2 HTTP returned an invalid frame"
            )
        if (
            document.get("schema_version") != 1
            or document.get("channel_fast_lane_schema_version") != 2
        ):
            raise RuntimeCommandError(
                "Fast Lane v2 HTTP returned an incompatible frame"
            )
        if document.get("type") == "channel.error":
            raise RuntimeCommandError(
                str(document.get("error_code") or "FAST_LANE_V2_REJECTED")
            )
        if response.status_code >= 400:
            raise RuntimeCommandError(
                "Fast Lane v2 HTTP rejected with HTTP "
                f"{response.status_code}"
            )
        return document

    def _record_event(
        self,
        payload: object,
        *,
        delivery_id: str,
        canonical_run_id: str,
    ) -> None:
        if not isinstance(payload, dict):
            raise RuntimeCommandError(
                "Fast Lane v2 HTTP resume returned an invalid event"
            )
        seq = int(payload.get("seq") or 0)
        if (
            payload.get("schema_version") != 1
            or payload.get("channel_fast_lane_schema_version") != 2
            or payload.get("type") != "run.event"
            or payload.get("delivery_id") != delivery_id
            or payload.get("canonical_run_id") != canonical_run_id
            or payload.get("run_id") != canonical_run_id
            or seq <= 0
        ):
            raise RuntimeCommandError(
                "Fast Lane v2 HTTP resume returned an invalid event"
            )
        if (
            self._current_canonical_run_ids.get(delivery_id)
            != canonical_run_id
            or seq <= self._last_event_seq.get(delivery_id, 0)
        ):
            return
        queue = self._events.setdefault(
            delivery_id,
            asyncio.Queue(maxsize=FAST_LANE_EVENT_QUEUE_SIZE),
        )
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull as exc:
            raise RuntimeCommandError(
                "Fast Lane v2 HTTP event queue reached its bound"
            ) from exc
        self._last_event_seq[delivery_id] = seq

    def _parse_disposition(
        self,
        payload: dict[str, Any],
    ) -> ChannelFastLaneV2DispositionFrame:
        try:
            disposition = ChannelFastLaneV2DispositionFrame.model_validate(
                payload
            )
        except ValueError as exc:
            raise RuntimeCommandError(
                "Fast Lane v2 HTTP returned an invalid disposition"
            ) from exc
        delivery_id = disposition.delivery_id
        previous_run_id = self._current_canonical_run_ids.get(delivery_id)
        if (
            previous_run_id is not None
            and previous_run_id != disposition.canonical_run_id
        ):
            self._last_event_seq[delivery_id] = 0
            queue = self._events.get(delivery_id)
            if queue is not None:
                while not queue.empty():
                    queue.get_nowait()
        self._current_canonical_run_ids[
            delivery_id
        ] = disposition.canonical_run_id
        return disposition

    def _endpoint_url(self) -> str:
        return f"{self.ingress_base_url}/internal/v1/channel-fast-lane/v2"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.target.api_server_key}",
            "Host": self.target.endpoint_host_header,
            "X-Hermes-Runtime-Id": self.target.runtime_id,
            "X-Hermes-Binding-Id": self.target.binding_id,
            "X-Hermes-Gateway-Instance-Id": self.gateway_instance_id,
            "X-Hermes-Channel-Lease-Epoch": str(
                self.channel_lease_epoch
            ),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _timeout() -> httpx.Timeout:
        return httpx.Timeout(
            connect=5,
            pool=1,
            write=10,
            read=FAST_LANE_ACK_TIMEOUT_SECONDS,
        )


class _RecoveringFastLaneV2Connection:
    """Switches a live delivery from WebSocket to HTTP without changing ids."""

    def __init__(
        self,
        *,
        primary: _FastLaneV2ConnectionProtocol,
        recovery: _FastLaneV2ConnectionProtocol,
    ) -> None:
        self.primary = primary
        self.recovery = recovery
        self.active = primary
        self._recovery_resumed_deliveries: set[str] = set()

    def use_recovery(self) -> None:
        self.active = self.recovery

    async def submit(
        self,
        *,
        delivery_id: str,
        client_message_id: str,
        envelope: RuntimeCommandEnvelope,
        lease: ChannelExecutionLease,
    ) -> ChannelFastLaneV2DispositionFrame:
        return await self.active.submit(
            delivery_id=delivery_id,
            client_message_id=client_message_id,
            envelope=envelope,
            lease=lease,
        )

    async def resume(
        self,
        *,
        delivery_id: str,
        last_disposition_seq: int,
        last_event_seq: int,
    ) -> ChannelFastLaneV2DispositionFrame:
        try:
            disposition = await self.active.resume(
                delivery_id=delivery_id,
                last_disposition_seq=last_disposition_seq,
                last_event_seq=last_event_seq,
            )
            if self.active is self.recovery:
                self._recovery_resumed_deliveries.add(delivery_id)
            return disposition
        except RuntimeChannelFastLaneV2TransportError:
            if self.active is self.recovery:
                raise
            self.use_recovery()
            disposition = await self.recovery.resume(
                delivery_id=delivery_id,
                last_disposition_seq=last_disposition_seq,
                last_event_seq=last_event_seq,
            )
            self._recovery_resumed_deliveries.add(delivery_id)
            return disposition

    async def acknowledge_disposition(
        self,
        disposition: ChannelFastLaneV2DispositionFrame,
    ) -> None:
        try:
            await self.active.acknowledge_disposition(disposition)
        except RuntimeChannelFastLaneV2TransportError:
            if self.active is self.recovery:
                raise
            self.use_recovery()
            await self.recovery.acknowledge_disposition(disposition)

    async def acknowledge_provider_delivery(
        self,
        *,
        owner_delivery_id: str,
        canonical_run_id: str,
        highest_contiguous_seq: int,
        provider_request_id: str,
        provider_message_id: str | None,
        projection_version: int,
        confirmed_at: datetime,
    ) -> int:
        parameters = {
            "owner_delivery_id": owner_delivery_id,
            "canonical_run_id": canonical_run_id,
            "highest_contiguous_seq": highest_contiguous_seq,
            "provider_request_id": provider_request_id,
            "provider_message_id": provider_message_id,
            "projection_version": projection_version,
            "confirmed_at": confirmed_at,
        }
        try:
            return await self.active.acknowledge_provider_delivery(
                **parameters
            )
        except RuntimeChannelFastLaneV2TransportError:
            if self.active is self.recovery:
                raise
            self.use_recovery()
            return await self.recovery.acknowledge_provider_delivery(
                **parameters
            )

    async def next_event(
        self,
        *,
        delivery_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        try:
            return await self.active.next_event(
                delivery_id=delivery_id,
                timeout_seconds=timeout_seconds,
            )
        except RuntimeChannelFastLaneV2TransportError:
            if self.active is self.recovery:
                raise
            self.use_recovery()
            return None

    def last_event_seq(self, delivery_id: str) -> int:
        if (
            self.active is self.recovery
            and delivery_id in self._recovery_resumed_deliveries
        ):
            return self.recovery.last_event_seq(delivery_id)
        return max(
            self.primary.last_event_seq(delivery_id),
            self.recovery.last_event_seq(delivery_id),
        )


class RuntimeChannelFastLanePool:
    def __init__(self) -> None:
        self._connections: dict[
            tuple[str, str, str, int],
            RuntimeChannelFastLaneConnection,
        ] = {}
        self._lock = asyncio.Lock()

    async def connection(
        self,
        *,
        target: RuntimeCommandTarget,
        lease: ChannelExecutionLease,
    ) -> RuntimeChannelFastLaneConnection:
        key = (
            target.runtime_id,
            target.binding_id,
            lease.gateway_instance_id,
            lease.channel_lease_epoch,
        )
        stale: tuple[RuntimeChannelFastLaneConnection, ...] = ()
        async with self._lock:
            connection = self._connections.get(key)
            if connection is None:
                stale_keys = tuple(
                    existing_key
                    for existing_key in self._connections
                    if existing_key[:3] == key[:3] and existing_key != key
                )
                stale = tuple(
                    self._connections.pop(existing_key)
                    for existing_key in stale_keys
                )
                connection = RuntimeChannelFastLaneConnection(
                    target=target,
                    gateway_instance_id=lease.gateway_instance_id,
                    channel_lease_epoch=lease.channel_lease_epoch,
                )
                self._connections[key] = connection
        if stale:
            await asyncio.gather(
                *(item.close() for item in stale),
                return_exceptions=True,
            )
        return connection

    async def close(self) -> None:
        async with self._lock:
            connections = tuple(self._connections.values())
            self._connections.clear()
        await asyncio.gather(
            *(connection.close() for connection in connections),
            return_exceptions=True,
        )


class RuntimeChannelFastLaneV2Pool:
    def __init__(self) -> None:
        self._connections: dict[
            tuple[str, str, str, int],
            RuntimeChannelFastLaneV2Connection,
        ] = {}
        self._lock = asyncio.Lock()

    async def connection(
        self,
        *,
        target: RuntimeCommandTarget,
        lease: ChannelExecutionLease,
    ) -> RuntimeChannelFastLaneV2Connection:
        key = (
            target.runtime_id,
            target.binding_id,
            lease.gateway_instance_id,
            lease.channel_lease_epoch,
        )
        stale: tuple[RuntimeChannelFastLaneV2Connection, ...] = ()
        async with self._lock:
            connection = self._connections.get(key)
            if connection is None:
                stale_keys = tuple(
                    existing_key
                    for existing_key in self._connections
                    if existing_key[:3] == key[:3] and existing_key != key
                )
                stale = tuple(
                    self._connections.pop(existing_key)
                    for existing_key in stale_keys
                )
                connection = RuntimeChannelFastLaneV2Connection(
                    target=target,
                    gateway_instance_id=lease.gateway_instance_id,
                    channel_lease_epoch=lease.channel_lease_epoch,
                )
                self._connections[key] = connection
        if stale:
            await asyncio.gather(
                *(item.close() for item in stale),
                return_exceptions=True,
            )
        return connection

    async def close(self) -> None:
        async with self._lock:
            connections = tuple(self._connections.values())
            self._connections.clear()
        await asyncio.gather(
            *(connection.close() for connection in connections),
            return_exceptions=True,
        )


_shared_pool = RuntimeChannelFastLanePool()
_shared_v2_pool = RuntimeChannelFastLaneV2Pool()


async def close_runtime_channel_fast_lane_pool() -> None:
    await asyncio.gather(
        _shared_pool.close(),
        _shared_v2_pool.close(),
    )


class RuntimeChannelFastLaneV2Client:
    """Delivery-oriented v2 facade; it never falls back to a v1 Run wait."""

    def __init__(
        self,
        *,
        pool: RuntimeChannelFastLaneV2Pool | None = None,
        ingress_base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.pool = pool or _shared_v2_pool
        self.ingress_base_url = ingress_base_url
        self.http_client = http_client

    async def submit_or_resume(
        self,
        *,
        target: RuntimeCommandTarget,
        lease: ChannelExecutionLease,
        delivery_id: str,
        client_message_id: str,
        envelope: RuntimeCommandEnvelope,
        last_disposition_seq: int = 0,
        last_event_seq: int = 0,
    ) -> tuple[
        _FastLaneV2ConnectionProtocol,
        ChannelFastLaneV2DispositionFrame,
    ]:
        primary = await self.pool.connection(target=target, lease=lease)
        recovery = self._http_connection(target=target, lease=lease)
        connection = _RecoveringFastLaneV2Connection(
            primary=primary,
            recovery=recovery,
        )
        try:
            disposition = await primary.submit(
                delivery_id=delivery_id,
                client_message_id=client_message_id,
                envelope=envelope,
                lease=lease,
            )
        except RuntimeChannelFastLaneV2TransportError as exc:
            connection.use_recovery()
            if not exc.may_have_been_sent:
                disposition = await recovery.submit(
                    delivery_id=delivery_id,
                    client_message_id=client_message_id,
                    envelope=envelope,
                    lease=lease,
                )
            else:
                try:
                    disposition = await connection.resume(
                        delivery_id=delivery_id,
                        last_disposition_seq=last_disposition_seq,
                        last_event_seq=last_event_seq,
                    )
                except RuntimeCommandError as resume_error:
                    if str(resume_error) != "DELIVERY_NOT_FOUND":
                        raise
                    disposition = await recovery.submit(
                        delivery_id=delivery_id,
                        client_message_id=client_message_id,
                        envelope=envelope,
                        lease=lease,
                    )
        if (
            disposition.delivery_id != delivery_id
            or disposition.client_message_id != client_message_id
        ):
            raise RuntimeCommandError(
                "Fast Lane v2 disposition identity does not match submit"
            )
        return connection, disposition

    async def resume(
        self,
        *,
        target: RuntimeCommandTarget,
        lease: ChannelExecutionLease,
        delivery_id: str,
        last_disposition_seq: int,
        last_event_seq: int,
    ) -> tuple[
        _FastLaneV2ConnectionProtocol,
        ChannelFastLaneV2DispositionFrame,
    ]:
        primary = await self.pool.connection(target=target, lease=lease)
        connection = _RecoveringFastLaneV2Connection(
            primary=primary,
            recovery=self._http_connection(target=target, lease=lease),
        )
        disposition = await connection.resume(
            delivery_id=delivery_id,
            last_disposition_seq=last_disposition_seq,
            last_event_seq=last_event_seq,
        )
        return connection, disposition

    async def wait_delivery(
        self,
        *,
        connection: _FastLaneV2ConnectionProtocol,
        initial_disposition: ChannelFastLaneV2DispositionFrame,
        event_sink: Callable[
            [str, dict[str, object]], Awaitable[None]
        ] | None = None,
        disposition_sink: Callable[
            [ChannelFastLaneV2DispositionFrame], Awaitable[None]
        ] | None = None,
        timeout_seconds: float = 600,
        poll_interval_seconds: float = 0.2,
    ) -> ChannelFastLaneV2DispositionFrame:
        delivery_id = initial_disposition.delivery_id
        disposition = initial_disposition
        acknowledged_disposition_seq = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            if disposition.disposition_seq > acknowledged_disposition_seq:
                await connection.acknowledge_disposition(disposition)
                acknowledged_disposition_seq = disposition.disposition_seq
            if disposition.state in {"uncertain", "canceled"}:
                return disposition

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeCommandError("Fast Lane v2 delivery timed out")
            event = await connection.next_event(
                delivery_id=delivery_id,
                timeout_seconds=min(poll_interval_seconds, remaining),
            )
            if event is not None:
                event_name = str(event.get("event_type") or "")
                data = dict(event.get("data") or {})
                seq = int(event.get("seq") or 0)
                if seq > 0:
                    data.setdefault("seq", seq)
                data.setdefault(
                    "runtime_run_id",
                    str(event.get("canonical_run_id") or ""),
                )
                data.setdefault("delivery_id", delivery_id)
                if event_sink is not None:
                    await event_sink(event_name, data)
                if event_name == "chat.done":
                    return disposition
                continue

            disposition = await connection.resume(
                delivery_id=delivery_id,
                last_disposition_seq=acknowledged_disposition_seq,
                last_event_seq=connection.last_event_seq(delivery_id),
            )
            if disposition_sink is not None:
                await disposition_sink(disposition)

    def _http_connection(
        self,
        *,
        target: RuntimeCommandTarget,
        lease: ChannelExecutionLease,
    ) -> RuntimeChannelFastLaneV2HttpConnection:
        return RuntimeChannelFastLaneV2HttpConnection(
            target=target,
            gateway_instance_id=lease.gateway_instance_id,
            channel_lease_epoch=lease.channel_lease_epoch,
            ingress_base_url=self.ingress_base_url,
            client=self.http_client,
        )


async def acknowledge_channel_delivery(
    *,
    target: RuntimeCommandTarget,
    lease: ChannelExecutionLease,
    run_id: str,
    highest_contiguous_seq: int,
    provider_request_id: str,
    provider_message_id: str | None = None,
    projection_version: int = 1,
    confirmed_at: datetime | None = None,
) -> int:
    connection = await _shared_pool.connection(
        target=target,
        lease=lease,
    )
    return await connection.delivery_ack(
        {
            "request_id": f"delivery_{uuid4().hex}",
            "run_id": run_id,
            "highest_contiguous_seq": highest_contiguous_seq,
            "provider_request_id": provider_request_id,
            "provider_message_id": provider_message_id,
            "projection_version": projection_version,
            "confirmed_at": (
                confirmed_at or datetime.now(timezone.utc)
            ).isoformat(),
        }
    )


async def acknowledge_channel_delivery_v2(
    *,
    target: RuntimeCommandTarget,
    lease: ChannelExecutionLease,
    owner_delivery_id: str,
    canonical_run_id: str,
    highest_contiguous_seq: int,
    provider_request_id: str,
    provider_message_id: str | None = None,
    projection_version: int = 1,
    confirmed_at: datetime | None = None,
) -> int:
    primary = await _shared_v2_pool.connection(
        target=target,
        lease=lease,
    )
    connection = _RecoveringFastLaneV2Connection(
        primary=primary,
        recovery=RuntimeChannelFastLaneV2HttpConnection(
            target=target,
            gateway_instance_id=lease.gateway_instance_id,
            channel_lease_epoch=lease.channel_lease_epoch,
        ),
    )
    return await connection.acknowledge_provider_delivery(
        owner_delivery_id=owner_delivery_id,
        canonical_run_id=canonical_run_id,
        highest_contiguous_seq=highest_contiguous_seq,
        provider_request_id=provider_request_id,
        provider_message_id=provider_message_id,
        projection_version=projection_version,
        confirmed_at=confirmed_at or datetime.now(timezone.utc),
    )


class RuntimeChannelFastLaneClient:
    """RuntimeCommandClient-compatible facade with safe HTTP fallback."""

    def __init__(
        self,
        *,
        pool: RuntimeChannelFastLanePool | None = None,
        fallback: RuntimeCommandClient | None = None,
    ) -> None:
        self.pool = pool or _shared_pool
        self.fallback = fallback or RuntimeCommandClient()
        self._connection: _FastLaneConnectionProtocol | None = None
        self._run_id: str | None = None

    async def send(
        self,
        *,
        target: RuntimeCommandTarget,
        envelope: RuntimeCommandEnvelope,
    ) -> RuntimeCommandAck:
        payload = envelope.payload
        lease = (
            payload.channel_execution_lease
            if payload.type == "internal_turn"
            else None
        )
        if lease is None:
            return await self.fallback.send(target=target, envelope=envelope)
        if (
            getattr(payload, "attachment_file_ids", ())
            or getattr(payload, "attachments", ())
        ):
            # Fast Lane v1 intentionally carries only channel text to the
            # Supervisor. Attachment turns must retain the Runtime command
            # payload so Runtime can persist and dispatch image.attach.
            return await self.fallback.send(target=target, envelope=envelope)
        try:
            connection = await self.pool.connection(
                target=target,
                lease=lease,
            )
            ack = await connection.submit(envelope, lease)
        except RuntimeCommandError:
            logger.warning(
                "Runtime Channel Fast Lane unavailable; using idempotent fallback",
                extra={
                    "runtime_id": target.runtime_id,
                    "binding_id": target.binding_id,
                },
            )
            return await self.fallback.send(target=target, envelope=envelope)
        self._connection = connection
        self._run_id = ack.run_id
        return ack

    async def wait_run(
        self,
        *,
        target: RuntimeCommandTarget,
        run_id: str,
        event_sink: Callable[[str, dict[str, object]], Awaitable[None]] | None = None,
        timeout_seconds: float = 600,
        poll_interval_seconds: float = 0.2,
    ) -> dict[str, Any]:
        if self._connection is None or self._run_id != run_id:
            return await self.fallback.wait_run(
                target=target,
                run_id=run_id,
                event_sink=event_sink,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        seen: set[int] = set()

        async def deduplicated_sink(
            event_name: str,
            event_data: dict[str, object],
        ) -> None:
            seq = int(event_data.get("seq") or 0)
            if seq > 0 and seq in seen:
                return
            if seq > 0:
                seen.add(seq)
            if event_sink is not None:
                await event_sink(event_name, event_data)

        try:
            await self._connection.wait_run(
                run_id=run_id,
                event_sink=deduplicated_sink,
                timeout_seconds=timeout_seconds,
            )
        except RuntimeCommandError:
            return await self.fallback.wait_run(
                target=target,
                run_id=run_id,
                event_sink=deduplicated_sink,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        return await self.fallback.wait_run(
            target=target,
            run_id=run_id,
            event_sink=None,
            timeout_seconds=min(30, timeout_seconds),
            poll_interval_seconds=poll_interval_seconds,
        )

    async def get_run(
        self,
        *,
        target: RuntimeCommandTarget,
        run_id: str,
    ) -> dict[str, Any] | None:
        return await self.fallback.get_run(target=target, run_id=run_id)

    async def mutate_session(
        self,
        *,
        target: RuntimeCommandTarget,
        operation: str,
        session_id: str,
        profile_id: str,
        title: str | None = None,
    ) -> RuntimeCommandAck:
        """Keep non-turn commands on the canonical HTTP command client."""
        return await self.fallback.mutate_session(
            target=target,
            operation=operation,
            session_id=session_id,
            profile_id=profile_id,
            title=title,
        )

    async def close(self) -> None:
        await self.fallback.close()


def _websocket_url(
    ingress_base_url: str,
    *,
    endpoint_host_header: str | None = None,
) -> str:
    parsed = urlparse(ingress_base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    authority = endpoint_host_header or parsed.netloc
    return urlunparse(
        parsed._replace(
            scheme=scheme,
            netloc=authority,
            path="/internal/v1/channel-fast-lane/ws",
            params="",
            query="",
            fragment="",
        )
    )
