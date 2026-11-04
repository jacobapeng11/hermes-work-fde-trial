from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
import httpx
import pytest

from platform_supervisor.channel_fast_lane import (
    CHANNEL_FAST_LANE_V1_PROTOCOL,
    CHANNEL_FAST_LANE_V2_PROTOCOL,
    DurableChannelFastLaneV2Router,
    attach_channel_fast_lane,
    channel_handshake_signature,
    channel_lease_signature,
)
from platform_supervisor.contracts import (
    ChannelExecutionLease,
    ChannelFastLaneV2SubmitFrame,
    ChannelFastLaneV2DeliveryAck,
    ChannelFastLaneV2DispositionFrame,
    ChannelFastLaneV2ResumeFrame,
    canonical_payload_digest,
)
from platform_supervisor.edge import ChannelFastLaneV2ResumeBatch
from platform_supervisor.execution.manager import RunSubmission
from platform_supervisor.storage import (
    RunEnqueueIntent,
    SQLiteStore,
    StoredRunEvent,
)


SECRET = "fast-lane-secret-" + "x" * 32
GATEWAY_INSTANCE_ID = "backend-1:8011:im-gateway"
CHANNEL_LEASE_EPOCH = 7
ADMISSION_GENERATION = 11


class FixedConfig:
    def require_revision(self, revision: str) -> None:
        if revision != "revision-18":
            raise RuntimeError("CONFIG_REVISION_STALE")


class FakeV2Router:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.resumes: list[Any] = []
        self.disposition_acks: list[Any] = []
        self.delivery_acks: list[Any] = []
        self.submit_disposition: Any = _disposition(
            state="pending",
            disposition_seq=1,
        )
        self.resume_batch = ChannelFastLaneV2ResumeBatch(
            disposition=_disposition(
                state="redirect_consumed",
                disposition_seq=3,
                start_after_seq=32,
            ),
            events=(
                {
                    "schema_version": 1,
                    "channel_fast_lane_schema_version": 2,
                    "type": "run.event",
                    "delivery_id": "delivery-wecom-message-00000001",
                    "canonical_run_id": "run-parent-00000001",
                    "run_id": "run-parent-00000001",
                    "seq": 33,
                    "event_type": "chat.steer.consumed",
                    "emitted_at": "2026-08-14T08:00:08Z",
                    "data": {},
                },
            ),
        )

    async def submit_turn(self, **kwargs: Any) -> Any:
        self.submissions.append(kwargs)
        return self.submit_disposition

    async def resume_delivery(self, *, frame: Any) -> ChannelFastLaneV2ResumeBatch:
        self.resumes.append(frame)
        return self.resume_batch

    async def acknowledge_disposition(self, *, frame: Any) -> None:
        self.disposition_acks.append(frame)

    async def acknowledge_provider_delivery(self, *, frame: Any) -> int:
        self.delivery_acks.append(frame)
        return frame.highest_contiguous_seq


class FakeRuns:
    def __init__(self) -> None:
        self.subscriptions: list[tuple[str, int]] = []
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.events: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def subscribe(self, run_id: str, *, after_seq: int = 0):
        self.subscriptions.append((run_id, after_seq))
        self.started.set()
        try:
            while True:
                event = await self.events.get()
                if event is None:
                    return
                yield event
        finally:
            self.stopped.set()

    async def emit(self, payload: dict[str, Any]) -> None:
        await self.events.put(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )


class FastLaneHarness:
    def __init__(
        self,
        app: FastAPI,
        *,
        protocol: str,
        nonce: str,
    ) -> None:
        self.app = app
        self.protocol = protocol
        self.nonce = nonce
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def connect(self) -> dict[str, Any]:
        timestamp = int(datetime.now(timezone.utc).timestamp())
        signature = channel_handshake_signature(
            SECRET,
            runtime_id="runtime-1",
            binding_id="binding-1",
            gateway_instance_id=GATEWAY_INSTANCE_ID,
            channel_lease_epoch=CHANNEL_LEASE_EPOCH,
            timestamp=timestamp,
            nonce=self.nonce,
        )
        headers = {
            "authorization": f"Bearer {SECRET}",
            "host": "runtime.example.com",
            "sec-websocket-protocol": self.protocol,
            "x-hermes-runtime-id": "runtime-1",
            "x-hermes-binding-id": "binding-1",
            "x-hermes-gateway-instance-id": GATEWAY_INSTANCE_ID,
            "x-hermes-channel-lease-epoch": str(CHANNEL_LEASE_EPOCH),
            "x-hermes-timestamp": str(timestamp),
            "x-hermes-connection-nonce": self.nonce,
            "x-hermes-signature": signature,
        }
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "wss",
            "path": "/internal/v1/channel-fast-lane/ws",
            "raw_path": b"/internal/v1/channel-fast-lane/ws",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (key.encode(), value.encode()) for key, value in headers.items()
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("runtime.example.com", 443),
            "subprotocols": [self.protocol],
            "state": {},
        }
        self.task = asyncio.create_task(
            self.app(scope, self.incoming.get, self.outgoing.put)
        )
        await self.incoming.put({"type": "websocket.connect"})
        return await self.outgoing.get()

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self.incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps(payload),
            }
        )

    async def receive_json(self) -> dict[str, Any]:
        message = await asyncio.wait_for(self.outgoing.get(), timeout=5)
        assert message["type"] == "websocket.send"
        return json.loads(message["text"])

    async def close(self) -> None:
        await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        if self.task is not None:
            await self.task


def _disposition(
    *,
    state: str,
    disposition_seq: int,
    start_after_seq: int | None = None,
) -> ChannelFastLaneV2DispositionFrame:
    return ChannelFastLaneV2DispositionFrame.model_validate(
        {
            "schema_version": 1,
            "channel_fast_lane_schema_version": 2,
            "type": "channel.turn.disposition",
            "delivery_id": "delivery-wecom-message-00000001",
            "client_message_id": "im-wecom-message-00000001",
            "disposition_seq": disposition_seq,
            "state": state,
            "canonical_run_id": "run-parent-00000001",
            "parent_run_id": "run-parent-00000001",
            "start_after_seq": start_after_seq,
            "promoted_run_id": None,
        }
    )


def _build_app(
    router: FakeV2Router,
    *,
    runs: FakeRuns | None = None,
) -> FastAPI:
    app = FastAPI()
    dependencies = SimpleNamespace(
        runtime_id="runtime-1",
        binding_id="binding-1",
        api_server_key=SECRET,
        config=FixedConfig(),
        channel_fast_lane_v2_router=router,
        runs=runs or FakeRuns(),
    )
    attach_channel_fast_lane(app, dependencies)
    return app


def _v2_submit_frame() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    unsigned_lease = ChannelExecutionLease(
        lease_id="channel-lease-00000001",
        org_id=3,
        channel_id=31,
        target_type="personal_agent",
        target_id=115,
        subject_user_id=7,
        runtime_id="runtime-1",
        binding_id="binding-1",
        runtime_session_id="session-wecom-00000001",
        model_entry_id="7",
        profile_id="default",
        gateway_instance_id=GATEWAY_INSTANCE_ID,
        channel_lease_epoch=CHANNEL_LEASE_EPOCH,
        channel_generation=4,
        config_generation=18,
        admission_generation=ADMISSION_GENERATION,
        authorization_epoch=1,
        issued_at=now,
        expires_at=now + timedelta(seconds=120),
        signature="0" * 64,
    )
    lease = unsigned_lease.model_copy(
        update={"signature": channel_lease_signature(SECRET, unsigned_lease)}
    )
    turn = {
        "type": "internal_turn",
        "source": "personal_wecom",
        "run_id": "rr-fastlane-00000001",
        "client_message_id": "im-wecom-message-00000001",
        "identity": {
            "user_id": 7,
            "org_id": 3,
            "session_id": "session-wecom-00000001",
            "profile_id": "default",
            "runtime_agent_profile_id": 51,
            "profile_desired_revision": 27,
            "profile_applied_revision": 27,
            "profile_digest": f"sha256:{'2' * 64}",
            "skill_digest": f"sha256:{'3' * 64}",
            "mcp_digest": f"sha256:{'4' * 64}",
            "knowledge_digest": f"sha256:{'5' * 64}",
            "model_id": "7",
            "context_length": 128000,
            "config_revision": "revision-18",
            "admission_generation": ADMISSION_GENERATION,
            "max_prompt_bytes": 1024,
            "max_output_tokens": 4096,
        },
        "prompt": "继续分析",
        "attachment_file_ids": [],
        "attachments": [],
        "channel_execution_lease": lease.model_dump(mode="json"),
    }
    envelope = {
        "schema_version": 1,
        "command_id": "command-fastlane-v2-00000001",
        "runtime_id": "runtime-1",
        "binding_id": "binding-1",
        "admission_generation": ADMISSION_GENERATION,
        "payload_digest": canonical_payload_digest(turn),
        "issued_at": now.isoformat(),
        "deadline_at": (now + timedelta(seconds=120)).isoformat(),
        "payload": turn,
    }
    return {
        "schema_version": 1,
        "channel_fast_lane_schema_version": 2,
        "type": "channel.turn.submit",
        "request_id": "fastlane-v2-request-0001",
        "gateway_instance_id": GATEWAY_INSTANCE_ID,
        "channel_lease_epoch": CHANNEL_LEASE_EPOCH,
        "delivery_id": "delivery-wecom-message-00000001",
        "client_message_id": "im-wecom-message-00000001",
        "envelope": envelope,
    }


def _assert_profile_execution_identity(identity: Any) -> None:
    assert identity.runtime_agent_profile_id == 51
    assert identity.profile_desired_revision == 27
    assert identity.profile_applied_revision == 27
    assert identity.profile_digest == f"sha256:{'2' * 64}"
    assert identity.skill_digest == f"sha256:{'3' * 64}"
    assert identity.mcp_digest == f"sha256:{'4' * 64}"
    assert identity.knowledge_digest == f"sha256:{'5' * 64}"


def _v2_resume_frame() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "channel_fast_lane_schema_version": 2,
        "type": "channel.turn.resume",
        "request_id": "fastlane-v2-resume-0001",
        "gateway_instance_id": GATEWAY_INSTANCE_ID,
        "channel_lease_epoch": CHANNEL_LEASE_EPOCH,
        "delivery_id": "delivery-wecom-message-00000001",
        "last_disposition_seq": 2,
        "last_event_seq": 12,
    }


def _v2_http_headers(*, secret: str = SECRET) -> dict[str, str]:
    return {
        "authorization": f"Bearer {secret}",
        "x-hermes-runtime-id": "runtime-1",
        "x-hermes-binding-id": "binding-1",
        "x-hermes-gateway-instance-id": GATEWAY_INSTANCE_ID,
        "x-hermes-channel-lease-epoch": str(CHANNEL_LEASE_EPOCH),
    }


@pytest.mark.asyncio
async def test_fast_lane_v2_pushes_live_delta_without_resume() -> None:
    router = FakeV2Router()
    router.submit_disposition = ChannelFastLaneV2DispositionFrame(
        delivery_id="delivery-wecom-message-00000001",
        client_message_id="im-wecom-message-00000001",
        disposition_seq=1,
        state="ordinary_run_accepted",
        canonical_run_id="rr-fastlane-00000001",
        parent_run_id=None,
        start_after_seq=0,
        promoted_run_id=None,
    )
    runs = FakeRuns()
    websocket = FastLaneHarness(
        _build_app(router, runs=runs),
        protocol=CHANNEL_FAST_LANE_V2_PROTOCOL,
        nonce="fast-lane-v2-live-delta-00000001",
    )
    try:
        await websocket.connect()
        await websocket.send_json(_v2_submit_frame())
        disposition = await websocket.receive_json()
        assert disposition["state"] == "ordinary_run_accepted"

        await asyncio.wait_for(runs.started.wait(), timeout=0.2)
        assert runs.subscriptions == [("rr-fastlane-00000001", 0)]
        await runs.emit(
            {
                "schema_version": 1,
                "type": "run.event",
                "run_id": "rr-fastlane-00000001",
                "seq": 1,
                "event_type": "chat.message.delta",
                "emitted_at": "2026-08-20T08:00:00Z",
                "data": {"delta": "你"},
            }
        )

        event = await websocket.receive_json()
        assert event["type"] == "run.event"
        assert event["delivery_id"] == "delivery-wecom-message-00000001"
        assert event["event_type"] == "chat.message.delta"
        assert event["data"] == {"delta": "你"}
    finally:
        await websocket.close()
    await asyncio.wait_for(runs.stopped.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_fast_lane_v2_pending_disposition_does_not_start_live_stream() -> None:
    router = FakeV2Router()
    runs = FakeRuns()
    websocket = FastLaneHarness(
        _build_app(router, runs=runs),
        protocol=CHANNEL_FAST_LANE_V2_PROTOCOL,
        nonce="fast-lane-v2-pending-00000001",
    )
    try:
        await websocket.connect()
        await websocket.send_json(_v2_submit_frame())
        assert (await websocket.receive_json())["state"] == "pending"
        await asyncio.sleep(0)
        assert runs.subscriptions == []
    finally:
        await websocket.close()


@pytest.mark.asyncio
async def test_fast_lane_v2_reuses_one_live_stream_for_canonical_aliases() -> None:
    class AliasRouter(FakeV2Router):
        async def resume_delivery(
            self,
            *,
            frame: Any,
        ) -> ChannelFastLaneV2ResumeBatch:
            self.resumes.append(frame)
            is_owner = frame.delivery_id.endswith("00000001")
            return ChannelFastLaneV2ResumeBatch(
                disposition=ChannelFastLaneV2DispositionFrame(
                    delivery_id=frame.delivery_id,
                    client_message_id=(
                        "im-wecom-message-00000001"
                        if is_owner
                        else "im-wecom-message-00000002"
                    ),
                    disposition_seq=1,
                    state=(
                        "ordinary_run_accepted"
                        if is_owner
                        else "redirect_consumed"
                    ),
                    canonical_run_id="run-shared-00000001",
                    parent_run_id=(
                        None if is_owner else "run-shared-00000001"
                    ),
                    start_after_seq=0 if is_owner else 12,
                    promoted_run_id=None,
                )
            )

    router = AliasRouter()
    runs = FakeRuns()
    websocket = FastLaneHarness(
        _build_app(router, runs=runs),
        protocol=CHANNEL_FAST_LANE_V2_PROTOCOL,
        nonce="fast-lane-v2-aliases-00000001",
    )
    try:
        await websocket.connect()
        owner_resume = {
            **_v2_resume_frame(),
            "request_id": "fastlane-v2-owner-resume-0001",
            "delivery_id": "delivery-wecom-message-00000001",
            "last_disposition_seq": 0,
            "last_event_seq": 0,
        }
        await websocket.send_json(owner_resume)
        assert (await websocket.receive_json())["state"] == (
            "ordinary_run_accepted"
        )
        await asyncio.wait_for(runs.started.wait(), timeout=0.2)

        alias_resume = {
            **_v2_resume_frame(),
            "request_id": "fastlane-v2-alias-resume-0001",
            "delivery_id": "delivery-wecom-message-00000002",
            "last_disposition_seq": 0,
            "last_event_seq": 12,
        }
        await websocket.send_json(alias_resume)
        assert (await websocket.receive_json())["state"] == (
            "redirect_consumed"
        )
        await asyncio.sleep(0)

        assert runs.subscriptions == [("run-shared-00000001", 0)]
        await runs.emit(
            {
                "schema_version": 1,
                "type": "run.event",
                "run_id": "run-shared-00000001",
                "seq": 13,
                "event_type": "chat.message.delta",
                "emitted_at": "2026-08-20T08:00:01Z",
                "data": {"delta": "继续"},
            }
        )
        event = await websocket.receive_json()
        assert event["delivery_id"] == "delivery-wecom-message-00000001"
        await asyncio.sleep(0)
        assert websocket.outgoing.empty()
    finally:
        await websocket.close()


@pytest.mark.asyncio
async def test_fast_lane_v2_promoted_run_resets_resume_event_boundary() -> None:
    router = FakeV2Router()
    promoted = ChannelFastLaneV2DispositionFrame(
        delivery_id="delivery-wecom-message-00000001",
        client_message_id="im-wecom-message-00000001",
        disposition_seq=3,
        state="promoted",
        canonical_run_id="run-promoted-00000001",
        parent_run_id="run-parent-00000001",
        start_after_seq=0,
        promoted_run_id="run-promoted-00000001",
    )
    router.resume_batch = ChannelFastLaneV2ResumeBatch(
        disposition=promoted,
        events=(
            {
                "schema_version": 1,
                "channel_fast_lane_schema_version": 2,
                "type": "run.event",
                "delivery_id": promoted.delivery_id,
                "canonical_run_id": promoted.canonical_run_id,
                "run_id": promoted.canonical_run_id,
                "seq": 1,
                "event_type": "chat.run.started",
                "emitted_at": "2026-08-20T08:00:00Z",
                "data": {},
            },
        ),
    )
    runs = FakeRuns()
    websocket = FastLaneHarness(
        _build_app(router, runs=runs),
        protocol=CHANNEL_FAST_LANE_V2_PROTOCOL,
        nonce="fast-lane-v2-promoted-00000001",
    )
    try:
        await websocket.connect()
        await websocket.send_json(_v2_resume_frame())
        assert (await websocket.receive_json())["state"] == "promoted"
        assert (await websocket.receive_json())["seq"] == 1

        await asyncio.wait_for(runs.started.wait(), timeout=0.2)
        assert runs.subscriptions == [("run-promoted-00000001", 1)]
    finally:
        await websocket.close()


@pytest.mark.asyncio
async def test_fast_lane_v2_routes_by_delivery_identity_and_acknowledges() -> None:
    router = FakeV2Router()
    websocket = FastLaneHarness(
        _build_app(router),
        protocol=CHANNEL_FAST_LANE_V2_PROTOCOL,
        nonce="fast-lane-v2-nonce-00000001",
    )
    try:
        accepted = await websocket.connect()
        assert accepted["subprotocol"] == CHANNEL_FAST_LANE_V2_PROTOCOL

        await websocket.send_json(_v2_submit_frame())
        disposition = await websocket.receive_json()
        assert disposition["type"] == "channel.turn.disposition"
        assert disposition["state"] == "pending"
        assert disposition["delivery_id"] == "delivery-wecom-message-00000001"
        assert router.submissions[0]["identity"].admission_generation == 11
        _assert_profile_execution_identity(router.submissions[0]["identity"])

        await websocket.send_json(_v2_resume_frame())
        resumed_disposition = await websocket.receive_json()
        resumed_event = await websocket.receive_json()
        assert resumed_disposition["state"] == "redirect_consumed"
        assert resumed_event["delivery_id"] == "delivery-wecom-message-00000001"
        assert router.resumes[0].delivery_id == "delivery-wecom-message-00000001"

        await websocket.send_json(
            {
                "schema_version": 1,
                "channel_fast_lane_schema_version": 2,
                "type": "channel.turn.disposition.ack",
                "request_id": "fastlane-v2-disposition-ack-0001",
                "gateway_instance_id": GATEWAY_INSTANCE_ID,
                "channel_lease_epoch": CHANNEL_LEASE_EPOCH,
                "delivery_id": "delivery-wecom-message-00000001",
                "disposition_seq": 3,
            }
        )
        disposition_ack = await websocket.receive_json()
        assert disposition_ack["type"] == "channel.turn.disposition.ack_ok"
        assert router.disposition_acks[0].disposition_seq == 3

        await websocket.send_json(
            {
                "schema_version": 1,
                "channel_fast_lane_schema_version": 2,
                "type": "channel.delivery.ack",
                "request_id": "delivery-v2-request-0001",
                "gateway_instance_id": GATEWAY_INSTANCE_ID,
                "channel_lease_epoch": CHANNEL_LEASE_EPOCH,
                "owner_delivery_id": "delivery-wecom-message-00000001",
                "canonical_run_id": "run-parent-00000001",
                "highest_contiguous_seq": 20,
                "provider_request_id": "wecom-request-0002",
                "provider_message_id": "wecom-message-0002",
                "projection_version": 4,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        provider_ack = await websocket.receive_json()
        assert provider_ack["type"] == "channel.delivery.ack_ok"
        assert provider_ack["owner_delivery_id"] == (
            "delivery-wecom-message-00000001"
        )
        assert router.delivery_acks[0].canonical_run_id == "run-parent-00000001"
    finally:
        await websocket.close()


@pytest.mark.asyncio
async def test_fast_lane_v2_http_fallback_returns_durable_frames() -> None:
    router = FakeV2Router()
    transport = httpx.ASGITransport(app=_build_app(router))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runtime",
    ) as client:
        submitted = await client.post(
            "/internal/v1/channel-fast-lane/v2",
            headers=_v2_http_headers(),
            json=_v2_submit_frame(),
        )
        assert submitted.status_code == 200
        assert submitted.json()["type"] == "channel.turn.disposition"
        assert submitted.json()["delivery_id"] == (
            "delivery-wecom-message-00000001"
        )

        resumed = await client.post(
            "/internal/v1/channel-fast-lane/v2",
            headers=_v2_http_headers(),
            json=_v2_resume_frame(),
        )
        assert resumed.status_code == 200
        payload = resumed.json()
        assert payload["type"] == "channel.turn.resume.result"
        assert payload["disposition"]["state"] == "redirect_consumed"
        assert payload["events"][0]["seq"] == 33
        assert router.submissions[0]["frame"].delivery_id == (
            "delivery-wecom-message-00000001"
        )
        _assert_profile_execution_identity(router.submissions[0]["identity"])


@pytest.mark.asyncio
async def test_fast_lane_v2_http_rejects_v1_and_bad_credentials() -> None:
    router = FakeV2Router()
    transport = httpx.ASGITransport(app=_build_app(router))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runtime",
    ) as client:
        legacy = await client.post(
            "/internal/v1/channel-fast-lane/v2",
            headers=_v2_http_headers(),
            json={
                "schema_version": 1,
                "type": "channel.turn.resume",
                "request_id": "legacy-http-resume-0001",
                "run_id": "run-legacy-00000001",
                "after_seq": 0,
            },
        )
        assert legacy.status_code == 400
        assert legacy.json() == {
            "schema_version": 1,
            "channel_fast_lane_schema_version": 2,
            "type": "channel.error",
            "request_id": "legacy-http-resume-0001",
            "error_code": "INVALID_FRAME",
            "retryable": False,
        }

        unauthorized = await client.post(
            "/internal/v1/channel-fast-lane/v2",
            headers=_v2_http_headers(secret="wrong-secret"),
            json=_v2_resume_frame(),
        )
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error_code"] == (
            "INVALID_FAST_LANE_CREDENTIAL"
        )


@pytest.mark.asyncio
async def test_durable_router_asks_runs_to_interrupt_active_parent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    raw_frame = _v2_submit_frame()
    frame = ChannelFastLaneV2SubmitFrame.model_validate(raw_frame)
    calls: list[dict[str, Any]] = []

    async def submit_interactive(**kwargs: Any) -> RunSubmission:
        calls.append(kwargs)
        run = await store.enqueue_run(
            RunEnqueueIntent(
                run_id=frame.envelope.payload.run_id,
                session_id=frame.envelope.payload.identity.session_id,
                client_message_id=frame.client_message_id,
                payload_digest=frame.envelope.payload_digest,
                config_revision="revision-18",
                source="personal_wecom",
                enqueued_at=datetime.now(timezone.utc),
                prompt=frame.envelope.payload.prompt,
                audit_prompt=frame.envelope.payload.prompt,
                identity=frame.envelope.payload.identity.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
                delivery_id=frame.delivery_id,
            )
        )
        return RunSubmission(run=run, created=True)

    router = DurableChannelFastLaneV2Router(
        store,
        SimpleNamespace(submit_interactive=submit_interactive),
    )
    disposition = await router.submit_turn(
        frame=frame,
        identity=SimpleNamespace(),
        payload_digest=frame.envelope.payload_digest,
    )

    assert calls[0]["interrupt_parent"] is True
    assert calls[0]["redirect_eligible"] is True
    assert calls[0]["interaction_origin"] == "wecom_personal"
    assert disposition.state == "ordinary_run_accepted"
    await store.close()


@pytest.mark.asyncio
async def test_durable_router_resume_honors_provider_checkpoint_after_restart(
    tmp_path,
) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()
    delivery_id = "delivery-durable-router-0001"
    run = await store.enqueue_run(
        RunEnqueueIntent(
            run_id="run-durable-router-0001",
            session_id="session-durable-router-0001",
            client_message_id="message-durable-router-0001",
            payload_digest="a" * 64,
            config_revision="revision-18",
            source="personal_wecom",
            enqueued_at=datetime(2026, 8, 14, 8, tzinfo=timezone.utc),
            prompt="hello",
            audit_prompt="hello",
            identity={"admission_generation": ADMISSION_GENERATION},
            delivery_id=delivery_id,
        )
    )
    for seq in (1, 2):
        assert await store.append_run_event(
            StoredRunEvent(
                run_id=run.run_id,
                seq=seq,
                event_type="chat.message.completed",
                payload={"text": f"event-{seq}"},
                occurred_at=datetime(
                    2026,
                    8,
                    14,
                    8,
                    seq,
                    tzinfo=timezone.utc,
                ),
            ),
            durable=True,
        )
    router = DurableChannelFastLaneV2Router(store, SimpleNamespace())
    resume = ChannelFastLaneV2ResumeFrame.model_validate(
        {
            **_v2_resume_frame(),
            "delivery_id": delivery_id,
            "last_disposition_seq": 0,
            "last_event_seq": 0,
        }
    )
    initial = await router.resume_delivery(frame=resume)
    assert initial.disposition.state == "ordinary_run_accepted"
    assert [event["seq"] for event in initial.events] == [1, 2]

    await router.acknowledge_provider_delivery(
        frame=ChannelFastLaneV2DeliveryAck(
            request_id="provider-ack-durable-0001",
            gateway_instance_id=GATEWAY_INSTANCE_ID,
            channel_lease_epoch=CHANNEL_LEASE_EPOCH,
            owner_delivery_id=delivery_id,
            canonical_run_id=run.run_id,
            highest_contiguous_seq=1,
            provider_request_id="provider-request-durable-0001",
            projection_version=1,
            confirmed_at=datetime(2026, 8, 14, 8, 3, tzinfo=timezone.utc),
        )
    )
    await store.close()

    reopened = SQLiteStore(database)
    await reopened.start()
    restored_router = DurableChannelFastLaneV2Router(
        reopened,
        SimpleNamespace(),
    )
    restored = await restored_router.resume_delivery(frame=resume)
    assert restored.disposition.delivery_id == delivery_id
    assert [event["seq"] for event in restored.events] == [2]
    await reopened.close()


@pytest.mark.asyncio
async def test_fast_lane_versions_are_isolated() -> None:
    router = FakeV2Router()
    app = _build_app(router)
    v1 = FastLaneHarness(
        app,
        protocol=CHANNEL_FAST_LANE_V1_PROTOCOL,
        nonce="fast-lane-v1-nonce-00000001",
    )
    v2 = FastLaneHarness(
        app,
        protocol=CHANNEL_FAST_LANE_V2_PROTOCOL,
        nonce="fast-lane-v2-nonce-00000002",
    )
    try:
        await v1.connect()
        await v1.send_json(_v2_resume_frame())
        assert (await v1.receive_json())["error_code"] == "INVALID_FRAME"
        await v1.send_json(
            {
                "schema_version": 1,
                "type": "channel.turn.resume",
                "request_id": "legacy-hybrid-resume-0001",
                "run_id": "run-legacy-00000001",
                "after_seq": 0,
                "delivery_id": "delivery-must-not-downgrade-0001",
            }
        )
        assert (await v1.receive_json())["error_code"] == "INVALID_FRAME"

        await v2.connect()
        await v2.send_json(
            {
                "schema_version": 1,
                "type": "channel.turn.resume",
                "request_id": "legacy-resume-0001",
                "run_id": "run-legacy-00000001",
                "after_seq": 0,
            }
        )
        rejected = await v2.receive_json()
        assert rejected["error_code"] == "INVALID_FRAME"
        assert rejected["channel_fast_lane_schema_version"] == 2
    finally:
        await v1.close()
        await v2.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_state", ["queued", "steered", "redirected"])
async def test_fast_lane_v2_never_projects_raw_upstream_status_as_success(
    raw_state: str,
) -> None:
    router = FakeV2Router()
    router.submit_disposition = {
        "type": raw_state,
        "state": raw_state,
        "delivery_id": "delivery-wecom-message-00000001",
    }
    websocket = FastLaneHarness(
        _build_app(router),
        protocol=CHANNEL_FAST_LANE_V2_PROTOCOL,
        nonce=f"fast-lane-raw-{raw_state}-00000001",
    )
    try:
        await websocket.connect()
        await websocket.send_json(_v2_submit_frame())
        response = await websocket.receive_json()
        assert response["type"] == "channel.error"
        assert response["error_code"] == "INVALID_DURABLE_DISPOSITION"
        assert response.get("state") not in {"queued", "steered", "redirected"}
    finally:
        await websocket.close()
