from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json

import httpx
import pytest

from app.schemas.runtime_internal import (
    ChannelFastLaneV2DispositionFrame,
    InternalTurnCommandPayload,
    RuntimeCommandAck,
    RuntimeTurnIdentity,
)
from app.services.runtime_channel_fast_lane import (
    CHANNEL_LEASE_TTL_SECONDS,
    RuntimeChannelFastLaneClient,
    RuntimeChannelFastLaneV2Connection,
    RuntimeChannelFastLaneV2Client,
    RuntimeChannelFastLaneV2HttpConnection,
    RuntimeChannelFastLaneV2TransportError,
    _RecoveringFastLaneV2Connection,
    _websocket_url,
    channel_lease_signature,
    issue_channel_execution_lease,
)
from app.services.runtime_commands import (
    RuntimeCommandTarget,
    RuntimeCommandError,
    build_runtime_command,
)


TARGET = RuntimeCommandTarget(
    runtime_id="runtime-1",
    binding_id="binding-1",
    endpoint_host_header="runtime-1.runtime.example",
    api_server_key="runtime-secret-" + "x" * 32,
    admission_generation=11,
)


def _lease():
    return issue_channel_execution_lease(
        target=TARGET,
        org_id=3,
        channel_id=31,
        target_type="personal_agent",
        target_id=115,
        subject_user_id=7,
        runtime_session_id="session-wecom-0001",
        model_entry_id="19",
        profile_id="default",
        gateway_instance_id="backend-1:im-gateway",
        channel_lease_epoch=7,
        channel_generation=4,
        config_generation=3,
        issued_at=datetime(2026, 8, 14, 8, tzinfo=timezone.utc),
    )


def _envelope(*, with_lease: bool = True):
    payload = InternalTurnCommandPayload(
        source="personal_wecom",
        run_id="run-fast-lane-0001",
        client_message_id="message-fast-lane-0001",
        identity=RuntimeTurnIdentity(
            user_id=7,
            org_id=3,
            session_id="session-wecom-0001",
            profile_id="default",
            runtime_agent_profile_id=51,
            profile_desired_revision=1,
            profile_applied_revision=1,
            profile_digest="sha256:" + "1" * 64,
            skill_digest="sha256:" + "2" * 64,
            mcp_digest="sha256:" + "3" * 64,
            knowledge_digest="sha256:" + "4" * 64,
            model_id="19",
            upstream_model_id="deepseek-chat",
            config_revision="generation-3",
            admission_generation=11,
            max_prompt_bytes=1024,
            max_output_tokens=4096,
        ),
        prompt="继续",
        channel_execution_lease=_lease() if with_lease else None,
    )
    return build_runtime_command(
        target=TARGET,
        command_id="command-fast-lane-0001",
        payload=payload,
        issued_at=datetime(2026, 8, 14, 8, tzinfo=timezone.utc),
        deadline_seconds=600,
    )


def test_channel_execution_lease_is_short_lived_and_signed() -> None:
    lease = _lease()

    assert (
        lease.expires_at - lease.issued_at
    ).total_seconds() == CHANNEL_LEASE_TTL_SECONDS
    assert lease.signature == channel_lease_signature(
        TARGET.api_server_key,
        lease,
    )


def test_fast_lane_websocket_uses_runtime_authority_without_duplicate_host() -> None:
    assert _websocket_url(
        "http://127.0.0.1:8080",
        endpoint_host_header="runtime-1.runtime.example",
    ) == (
        "ws://runtime-1.runtime.example/internal/v1/channel-fast-lane/ws"
    )


class _FastConnection:
    def __init__(self) -> None:
        self.submitted = []

    async def submit(self, envelope, lease):
        self.submitted.append((envelope, lease))
        return RuntimeCommandAck(
            command_id=envelope.command_id,
            state="accepted",
            run_id=envelope.payload.run_id,
        )

    async def wait_run(self, *, run_id, event_sink, timeout_seconds):
        del timeout_seconds
        await event_sink(
            "chat.message.completed",
            {"runtime_run_id": run_id, "seq": 1, "text": "完成"},
        )
        await event_sink(
            "chat.done",
            {"runtime_run_id": run_id, "seq": 2},
        )


class _FailedFastConnection(_FastConnection):
    async def submit(self, envelope, lease):
        del envelope, lease
        raise RuntimeCommandError("connection unavailable")


class _Pool:
    def __init__(self, connection) -> None:
        self.fast_connection = connection

    async def connection(self, *, target, lease):
        del target, lease
        return self.fast_connection


def _v2_disposition(
    *,
    state: str = "ordinary_run_accepted",
    disposition_seq: int = 1,
) -> ChannelFastLaneV2DispositionFrame:
    parent_run_id = (
        "run-parent-00000001"
        if state != "ordinary_run_accepted"
        else None
    )
    canonical_run_id = parent_run_id or "run-fast-lane-0001"
    return ChannelFastLaneV2DispositionFrame(
        delivery_id="delivery-wecom-message-00000001",
        client_message_id="message-fast-lane-0001",
        disposition_seq=disposition_seq,
        state=state,
        canonical_run_id=canonical_run_id,
        parent_run_id=parent_run_id,
        start_after_seq=(
            12 if state in {"redirect_accepted", "redirect_consumed"} else 0
        ),
        promoted_run_id=None,
    )


class _FastV2Connection:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, str]] = []
        self.resumes: list[tuple[str, int, int]] = []
        self.acked: list[int] = []
        self.events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.event_seq = 0

    async def submit(
        self,
        *,
        delivery_id,
        client_message_id,
        envelope,
        lease,
    ):
        del envelope, lease
        self.submissions.append((delivery_id, client_message_id))
        return _v2_disposition()

    async def resume(
        self,
        *,
        delivery_id,
        last_disposition_seq,
        last_event_seq,
    ):
        self.resumes.append(
            (delivery_id, last_disposition_seq, last_event_seq)
        )
        return _v2_disposition()

    async def acknowledge_disposition(self, disposition):
        self.acked.append(disposition.disposition_seq)

    async def next_event(self, *, delivery_id, timeout_seconds):
        del delivery_id, timeout_seconds
        if self.events.empty():
            return None
        event = await self.events.get()
        self.event_seq = int(event["seq"])
        return event

    def last_event_seq(self, delivery_id):
        del delivery_id
        return self.event_seq


class _AmbiguousFastV2Connection(_FastV2Connection):
    async def submit(self, **kwargs):
        del kwargs
        raise RuntimeChannelFastLaneV2TransportError(
            "ambiguous send",
            may_have_been_sent=True,
        )


class _KnownZeroByteFastV2Connection(_FastV2Connection):
    async def submit(self, **kwargs):
        del kwargs
        raise RuntimeChannelFastLaneV2TransportError(
            "connect failed before send",
            may_have_been_sent=False,
        )


class _V2Pool:
    def __init__(self, connection) -> None:
        self.fast_connection = connection

    async def connection(self, *, target, lease):
        del target, lease
        return self.fast_connection


@pytest.mark.asyncio
async def test_fast_lane_v2_recovery_uses_active_canonical_event_boundary() -> None:
    primary = _FastV2Connection()
    primary.event_seq = 33
    recovery = _FastV2Connection()
    recovery.event_seq = 1
    connection = _RecoveringFastLaneV2Connection(
        primary=primary,
        recovery=recovery,
    )

    connection.use_recovery()

    assert connection.last_event_seq("delivery-wecom-message-00000001") == 33
    await connection.resume(
        delivery_id="delivery-wecom-message-00000001",
        last_disposition_seq=3,
        last_event_seq=33,
    )
    assert connection.last_event_seq("delivery-wecom-message-00000001") == 1


def test_fast_lane_v2_resets_event_high_water_on_canonical_run_change() -> None:
    connection = RuntimeChannelFastLaneV2Connection(
        target=TARGET,
        gateway_instance_id=_lease().gateway_instance_id,
        channel_lease_epoch=_lease().channel_lease_epoch,
    )
    delivery_id = "delivery-wecom-message-00000001"
    parent = _v2_disposition(
        state="redirect_consumed",
        disposition_seq=3,
    )
    connection._record_disposition(parent)
    connection._record_event(
        {
            "schema_version": 1,
            "channel_fast_lane_schema_version": 2,
            "type": "run.event",
            "delivery_id": delivery_id,
            "canonical_run_id": parent.canonical_run_id,
            "run_id": parent.canonical_run_id,
            "seq": 33,
            "event_type": "chat.done",
            "data": {},
        }
    )
    assert connection.last_event_seq(delivery_id) == 33

    promoted = ChannelFastLaneV2DispositionFrame(
        delivery_id=delivery_id,
        client_message_id="message-fast-lane-0001",
        disposition_seq=4,
        state="promoted",
        canonical_run_id="run-promoted-00000001",
        parent_run_id=parent.canonical_run_id,
        start_after_seq=0,
        promoted_run_id="run-promoted-00000001",
    )
    connection._record_disposition(promoted)
    connection._record_event(
        {
            "schema_version": 1,
            "channel_fast_lane_schema_version": 2,
            "type": "run.event",
            "delivery_id": delivery_id,
            "canonical_run_id": promoted.canonical_run_id,
            "run_id": promoted.canonical_run_id,
            "seq": 1,
            "event_type": "chat.message.delta",
            "data": {"delta": "新"},
        }
    )
    connection._record_event(
        {
            "schema_version": 1,
            "channel_fast_lane_schema_version": 2,
            "type": "run.event",
            "delivery_id": delivery_id,
            "canonical_run_id": parent.canonical_run_id,
            "run_id": parent.canonical_run_id,
            "seq": 34,
            "event_type": "chat.done",
            "data": {},
        }
    )

    assert connection.last_event_seq(delivery_id) == 1


def test_fast_lane_v2_http_resets_event_high_water_on_canonical_change() -> None:
    connection = RuntimeChannelFastLaneV2HttpConnection(
        target=TARGET,
        gateway_instance_id=_lease().gateway_instance_id,
        channel_lease_epoch=_lease().channel_lease_epoch,
    )
    delivery_id = "delivery-wecom-message-00000001"
    parent = _v2_disposition(
        state="redirect_consumed",
        disposition_seq=3,
    )
    connection._parse_disposition(parent.model_dump(mode="json"))
    connection._record_event(
        {
            "schema_version": 1,
            "channel_fast_lane_schema_version": 2,
            "type": "run.event",
            "delivery_id": delivery_id,
            "canonical_run_id": parent.canonical_run_id,
            "run_id": parent.canonical_run_id,
            "seq": 33,
            "event_type": "chat.done",
            "data": {},
        },
        delivery_id=delivery_id,
        canonical_run_id=parent.canonical_run_id,
    )
    promoted = ChannelFastLaneV2DispositionFrame(
        delivery_id=delivery_id,
        client_message_id="message-fast-lane-0001",
        disposition_seq=4,
        state="promoted",
        canonical_run_id="run-promoted-00000001",
        parent_run_id=parent.canonical_run_id,
        start_after_seq=0,
        promoted_run_id="run-promoted-00000001",
    )
    connection._parse_disposition(promoted.model_dump(mode="json"))
    connection._record_event(
        {
            "schema_version": 1,
            "channel_fast_lane_schema_version": 2,
            "type": "run.event",
            "delivery_id": delivery_id,
            "canonical_run_id": promoted.canonical_run_id,
            "run_id": promoted.canonical_run_id,
            "seq": 1,
            "event_type": "chat.message.delta",
            "data": {"delta": "新"},
        },
        delivery_id=delivery_id,
        canonical_run_id=promoted.canonical_run_id,
    )

    assert connection.last_event_seq(delivery_id) == 1


class _Fallback:
    def __init__(self) -> None:
        self.sent = 0
        self.waited = 0
        self.session_mutations = []

    async def send(self, *, target, envelope):
        del target
        self.sent += 1
        return RuntimeCommandAck(
            command_id=envelope.command_id,
            state="accepted",
            run_id=envelope.payload.run_id,
        )

    async def wait_run(self, **kwargs):
        del kwargs
        self.waited += 1
        return {
            "run_id": "run-fast-lane-0001",
            "status": "COMPLETED",
            "answer": "完成",
            "events": [],
        }

    async def get_run(self, **kwargs):
        del kwargs
        return None

    async def mutate_session(self, **kwargs):
        self.session_mutations.append(kwargs)
        return RuntimeCommandAck(
            command_id="command-session-mutation-0001",
            state="completed",
        )

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_fast_lane_facade_streams_events_then_reads_one_terminal_snapshot() -> None:
    fast = _FastConnection()
    fallback = _Fallback()
    client = RuntimeChannelFastLaneClient(
        pool=_Pool(fast),
        fallback=fallback,
    )
    events: list[str] = []

    ack = await client.send(target=TARGET, envelope=_envelope())
    snapshot = await client.wait_run(
        target=TARGET,
        run_id="run-fast-lane-0001",
        event_sink=lambda name, _data: _append(events, name),
    )

    assert ack.state == "accepted"
    assert len(fast.submitted) == 1
    assert fallback.sent == 0
    assert fallback.waited == 1
    assert events == ["chat.message.completed", "chat.done"]
    assert snapshot["answer"] == "完成"


@pytest.mark.asyncio
async def test_non_channel_turn_uses_existing_runtime_command_path() -> None:
    fast = _FastConnection()
    fallback = _Fallback()
    client = RuntimeChannelFastLaneClient(
        pool=_Pool(fast),
        fallback=fallback,
    )

    ack = await client.send(
        target=TARGET,
        envelope=_envelope(with_lease=False),
    )

    assert ack.state == "accepted"
    assert fallback.sent == 1
    assert fast.submitted == []


@pytest.mark.asyncio
async def test_fast_lane_connection_failure_reuses_same_envelope_on_fallback() -> None:
    fallback = _Fallback()
    client = RuntimeChannelFastLaneClient(
        pool=_Pool(_FailedFastConnection()),
        fallback=fallback,
    )
    envelope = _envelope()

    ack = await client.send(target=TARGET, envelope=envelope)

    assert ack.run_id == envelope.payload.run_id
    assert fallback.sent == 1


@pytest.mark.asyncio
async def test_fast_lane_facade_delegates_session_mutation_to_http_client() -> None:
    fallback = _Fallback()
    client = RuntimeChannelFastLaneClient(fallback=fallback)

    ack = await client.mutate_session(
        target=TARGET,
        operation="delete",
        session_id="session-studio-0001",
        profile_id="default",
    )

    assert ack.state == "completed"
    assert fallback.session_mutations == [
        {
            "target": TARGET,
            "operation": "delete",
            "session_id": "session-studio-0001",
            "profile_id": "default",
            "title": None,
        }
    ]


@pytest.mark.asyncio
async def test_fast_lane_v2_uses_delivery_disposition_without_legacy_wait() -> None:
    connection = _FastV2Connection()
    await connection.events.put(
        {
            "type": "run.event",
            "delivery_id": "delivery-wecom-message-00000001",
            "canonical_run_id": "run-fast-lane-0001",
            "run_id": "run-fast-lane-0001",
            "seq": 1,
            "event_type": "chat.message.completed",
            "data": {"text": "完成"},
        }
    )
    await connection.events.put(
        {
            "type": "run.event",
            "delivery_id": "delivery-wecom-message-00000001",
            "canonical_run_id": "run-fast-lane-0001",
            "run_id": "run-fast-lane-0001",
            "seq": 2,
            "event_type": "chat.done",
            "data": {},
        }
    )
    client = RuntimeChannelFastLaneV2Client(pool=_V2Pool(connection))
    events: list[str] = []

    active, disposition = await client.submit_or_resume(
        target=TARGET,
        lease=_lease(),
        delivery_id="delivery-wecom-message-00000001",
        client_message_id="message-fast-lane-0001",
        envelope=_envelope(),
    )
    settled = await client.wait_delivery(
        connection=active,
        initial_disposition=disposition,
        event_sink=lambda name, _data: _append(events, name),
    )

    assert settled.state == "ordinary_run_accepted"
    assert connection.submissions == [
        (
            "delivery-wecom-message-00000001",
            "message-fast-lane-0001",
        )
    ]
    assert connection.acked == [1]
    assert events == ["chat.message.completed", "chat.done"]


@pytest.mark.asyncio
async def test_fast_lane_v2_http_transport_uses_v2_frames_and_identity_headers() -> None:
    frames: list[dict[str, object]] = []
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        frame = json.loads(request.content)
        frames.append(frame)
        if frame["type"] == "channel.turn.submit":
            return httpx.Response(
                200,
                json=_v2_disposition().model_dump(mode="json"),
            )
        if frame["type"] == "channel.turn.resume":
            disposition = _v2_disposition(
                state="redirect_consumed",
                disposition_seq=3,
            )
            events = [
                {
                    "schema_version": 1,
                    "channel_fast_lane_schema_version": 2,
                    "type": "run.event",
                    "delivery_id": disposition.delivery_id,
                    "canonical_run_id": disposition.canonical_run_id,
                    "run_id": disposition.canonical_run_id,
                    "seq": 10,
                    "event_type": "chat.message.completed",
                    "data": {"text": "完成"},
                }
            ]
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "channel_fast_lane_schema_version": 2,
                    "type": "channel.turn.resume.result",
                    "disposition": disposition.model_dump(mode="json"),
                    "events": events,
                },
            )
        if frame["type"] == "channel.turn.disposition.ack":
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "channel_fast_lane_schema_version": 2,
                    "type": "channel.turn.disposition.ack_ok",
                    "request_id": frame["request_id"],
                    "delivery_id": frame["delivery_id"],
                    "disposition_seq": frame["disposition_seq"],
                },
            )
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "channel_fast_lane_schema_version": 2,
                "type": "channel.delivery.ack_ok",
                "request_id": frame["request_id"],
                "owner_delivery_id": frame["owner_delivery_id"],
                "canonical_run_id": frame["canonical_run_id"],
                "highest_contiguous_seq": frame["highest_contiguous_seq"],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle)
    ) as http_client:
        connection = RuntimeChannelFastLaneV2HttpConnection(
            target=TARGET,
            gateway_instance_id=_lease().gateway_instance_id,
            channel_lease_epoch=_lease().channel_lease_epoch,
            ingress_base_url="http://127.0.0.1:31080",
            client=http_client,
        )
        submitted = await connection.submit(
            delivery_id="delivery-wecom-message-00000001",
            client_message_id="message-fast-lane-0001",
            envelope=_envelope(),
            lease=_lease(),
        )
        resumed = await connection.resume(
            delivery_id="delivery-wecom-message-00000001",
            last_disposition_seq=1,
            last_event_seq=9,
        )
        await connection.acknowledge_disposition(resumed)
        acknowledged_seq = await connection.acknowledge_provider_delivery(
            owner_delivery_id=resumed.delivery_id,
            canonical_run_id=resumed.canonical_run_id,
            highest_contiguous_seq=10,
            provider_request_id="wecom-request-0001",
            provider_message_id="wecom-message-0001",
            projection_version=1,
            confirmed_at=datetime(2026, 8, 14, 8, tzinfo=timezone.utc),
        )
        event = await connection.next_event(
            delivery_id=resumed.delivery_id,
            timeout_seconds=0.01,
        )

    assert submitted.state == "ordinary_run_accepted"
    assert resumed.state == "redirect_consumed"
    assert event is not None and event["seq"] == 10
    assert connection.last_event_seq(resumed.delivery_id) == 10
    assert acknowledged_seq == 10
    assert [frame["type"] for frame in frames] == [
        "channel.turn.submit",
        "channel.turn.resume",
        "channel.turn.disposition.ack",
        "channel.delivery.ack",
    ]
    submitted_lease = frames[0]["envelope"]["payload"][
        "channel_execution_lease"
    ]
    assert submitted_lease["signature"] == channel_lease_signature(
        TARGET.api_server_key,
        submitted_lease,
    )
    assert requests[0].url.path == "/internal/v1/channel-fast-lane/v2"
    assert requests[0].headers["host"] == TARGET.endpoint_host_header
    assert requests[0].headers["authorization"] == (
        f"Bearer {TARGET.api_server_key}"
    )
    assert requests[0].headers["x-hermes-runtime-id"] == TARGET.runtime_id
    assert requests[0].headers["x-hermes-binding-id"] == TARGET.binding_id
    assert requests[0].headers["x-hermes-gateway-instance-id"] == (
        _lease().gateway_instance_id
    )
    assert requests[0].headers["x-hermes-channel-lease-epoch"] == "7"


@pytest.mark.asyncio
async def test_fast_lane_v2_known_zero_byte_failure_submits_over_http() -> None:
    frames: list[dict[str, object]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        frame = json.loads(request.content)
        frames.append(frame)
        return httpx.Response(
            200,
            json=_v2_disposition().model_dump(mode="json"),
        )

    primary = _KnownZeroByteFastV2Connection()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle)
    ) as http_client:
        client = RuntimeChannelFastLaneV2Client(
            pool=_V2Pool(primary),
            ingress_base_url="http://127.0.0.1:31080",
            http_client=http_client,
        )
        _active, disposition = await client.submit_or_resume(
            target=TARGET,
            lease=_lease(),
            delivery_id="delivery-wecom-message-00000001",
            client_message_id="message-fast-lane-0001",
            envelope=_envelope(),
        )

    assert disposition.delivery_id == "delivery-wecom-message-00000001"
    assert [frame["type"] for frame in frames] == ["channel.turn.submit"]
    assert frames[0]["delivery_id"] == "delivery-wecom-message-00000001"
    assert primary.resumes == []


@pytest.mark.asyncio
async def test_fast_lane_v2_ambiguous_submit_resumes_same_delivery_over_http() -> None:
    connection = _AmbiguousFastV2Connection()
    frames: list[dict[str, object]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        frame = json.loads(request.content)
        frames.append(frame)
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "channel_fast_lane_schema_version": 2,
                "type": "channel.turn.resume.result",
                "disposition": _v2_disposition().model_dump(mode="json"),
                "events": [],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle)
    ) as http_client:
        client = RuntimeChannelFastLaneV2Client(
            pool=_V2Pool(connection),
            ingress_base_url="http://127.0.0.1:31080",
            http_client=http_client,
        )
        _active, disposition = await client.submit_or_resume(
            target=TARGET,
            lease=_lease(),
            delivery_id="delivery-wecom-message-00000001",
            client_message_id="message-fast-lane-0001",
            envelope=_envelope(),
            last_disposition_seq=2,
            last_event_seq=9,
        )

    assert disposition.delivery_id == "delivery-wecom-message-00000001"
    assert connection.resumes == []
    assert [frame["type"] for frame in frames] == ["channel.turn.resume"]
    assert frames[0]["delivery_id"] == "delivery-wecom-message-00000001"
    assert frames[0]["last_disposition_seq"] == 2
    assert frames[0]["last_event_seq"] == 9


@pytest.mark.asyncio
async def test_fast_lane_v2_ambiguous_missing_resume_retries_same_http_id() -> None:
    connection = _AmbiguousFastV2Connection()
    frames: list[dict[str, object]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        frame = json.loads(request.content)
        frames.append(frame)
        if frame["type"] == "channel.turn.resume":
            return httpx.Response(
                404,
                json={
                    "schema_version": 1,
                    "channel_fast_lane_schema_version": 2,
                    "type": "channel.error",
                    "request_id": frame["request_id"],
                    "error_code": "DELIVERY_NOT_FOUND",
                    "retryable": False,
                },
            )
        return httpx.Response(
            200,
            json=_v2_disposition().model_dump(mode="json"),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle)
    ) as http_client:
        client = RuntimeChannelFastLaneV2Client(
            pool=_V2Pool(connection),
            ingress_base_url="http://127.0.0.1:31080",
            http_client=http_client,
        )
        _active, disposition = await client.submit_or_resume(
            target=TARGET,
            lease=_lease(),
            delivery_id="delivery-wecom-message-00000001",
            client_message_id="message-fast-lane-0001",
            envelope=_envelope(),
        )

    assert disposition.delivery_id == "delivery-wecom-message-00000001"
    assert [frame["type"] for frame in frames] == [
        "channel.turn.resume",
        "channel.turn.submit",
    ]
    assert {frame["delivery_id"] for frame in frames} == {
        "delivery-wecom-message-00000001"
    }


async def _append(values: list[str], value: str) -> None:
    values.append(value)
