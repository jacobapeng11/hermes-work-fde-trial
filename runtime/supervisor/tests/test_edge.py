from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from platform_supervisor.edge import (
    ExternalConnectionGate,
    RuntimeEdgeDependencies,
    _edge_interactive_disposition,
    attach_runtime_routes,
)
from platform_supervisor.channel_fast_lane import (
    CHANNEL_FAST_LANE_PROTOCOL,
    attach_channel_fast_lane,
    channel_handshake_signature,
    channel_lease_signature,
)
from platform_supervisor.contracts import (
    ChannelExecutionLease,
    RuntimeEdgeFollowupReceived,
    canonical_payload_digest,
)
from platform_supervisor.execution import CapacityGate, RunManager, SessionManager
from platform_supervisor.health import (
    RuntimeHealth,
    StartupCheckResult,
    StartupResourceSnapshot,
)
from platform_supervisor.hermes.protocol import HermesProtocolError
from platform_supervisor.hermes.execution_adapter import HermesSessionPrepareError
from platform_supervisor.hermes.events import PlatformStreamEvent
from platform_supervisor.security import RuntimeTokenVerifier
from platform_supervisor.security.tokens import encode_runtime_token
from platform_supervisor.storage import (
    DispatchIntent,
    RedirectAdmissionIntent,
    RunEnqueueIntent,
    SQLiteStore,
    StoredRedirect,
)
from platform_supervisor.model_relay import (
    KanbanWorkerModelRouter,
    ModelRouteRegistry,
)
from platform_supervisor.settings import SupervisorSettings


SECRET = "runtime-secret-" + "x" * 32
NOW = 1_800_000_000
EMPTY_DIGEST = "sha256:" + "0" * 64


class ImmediateAdapter:
    async def stream(
        self,
        *,
        identity,
        run_id,
        prompt,
        trusted_context=None,
        attachments=(),
        prompt_submit_started_sink=None,
        prompt_ack_sink=None,
        runtime_provider_run_id_sink=None,
    ) -> AsyncIterator[PlatformStreamEvent]:
        del identity, prompt, trusted_context, attachments
        if runtime_provider_run_id_sink:
            await runtime_provider_run_id_sink(run_id)
        if prompt_submit_started_sink:
            started = prompt_submit_started_sink()
            if started is not None:
                await started
        if prompt_ack_sink:
            await prompt_ack_sink()
        yield PlatformStreamEvent("chat.message.delta", {"delta": "你"})
        yield PlatformStreamEvent("chat.message.delta", {"delta": "好"})
        yield PlatformStreamEvent("chat.message.delta", {"delta": "！"})
        yield PlatformStreamEvent("chat.message.completed", {"text": "你好！"})
        yield PlatformStreamEvent("chat.done", {"run_id": run_id})

    async def cancel(self, identity, *, runtime_provider_run_id=None) -> None:
        del identity, runtime_provider_run_id


class GatedReasoningAdapter:
    def __init__(self) -> None:
        self.reasoning_emitted = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        identity,
        run_id,
        prompt,
        trusted_context=None,
        attachments=(),
        prompt_submit_started_sink=None,
        prompt_ack_sink=None,
        runtime_provider_run_id_sink=None,
    ) -> AsyncIterator[PlatformStreamEvent]:
        del identity, prompt, trusted_context, attachments
        if runtime_provider_run_id_sink:
            await runtime_provider_run_id_sink(run_id)
        if prompt_submit_started_sink:
            started = prompt_submit_started_sink()
            if started is not None:
                await started
        if prompt_ack_sink:
            await prompt_ack_sink()
        yield PlatformStreamEvent("chat.run.started", {"run_id": run_id})
        yield PlatformStreamEvent("chat.reasoning.delta", {"delta": "真实思考"})
        self.reasoning_emitted.set()
        await self.release.wait()
        yield PlatformStreamEvent(
            "chat.message.completed",
            {"text": "最终回答", "reasoning": "真实思考"},
        )
        yield PlatformStreamEvent("chat.done", {"run_id": run_id})

    async def cancel(self, identity, *, runtime_provider_run_id=None) -> None:
        del identity, runtime_provider_run_id


class FixedConfig:
    current_revision = "revision-18"

    def require_revision(self, config_revision: str) -> None:
        if config_revision != self.current_revision:
            raise RuntimeError("CONFIG_REVISION_STALE")

    async def apply(self, bundle):
        self.current_revision = bundle.config_revision
        return type(
            "Applied",
            (),
            {
                "config_revision": bundle.config_revision,
                "config_digest": bundle.config_digest,
            },
        )()


class FakeSessionControl:
    def __init__(self) -> None:
        self.renamed: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.listed_mcp_profiles: list[str] = []

    async def capabilities(self) -> dict[str, object]:
        return {"runtime": {"mode": "dashboard_live_session"}}

    async def dashboard_request(self, *, method, path, json_body=None):
        return {"method": method, "path": path, "json_body": json_body}

    async def list_skills(self):
        return [{"name": "search"}]

    async def reload_skills(self):
        return {"status": "reloaded"}

    async def reload_skills_and_list(self):
        return await self.list_skills()

    async def reload_mcp(self) -> None:
        return None

    async def reload_mcp_and_list(self):
        return [{"name": "docs"}]

    async def list_profile_mcp_servers(self, *, profile_name: str):
        self.listed_mcp_profiles.append(profile_name)
        return [{"name": "scoped-docs"}]

    async def apply_projected_config(self, **kwargs):
        return {
            "loaded_generation": kwargs["generation"],
            "projection_hash": kwargs["projection_hash"],
        }

    async def list_sessions(self, *, limit: int) -> dict[str, object]:
        del limit
        return {"sessions": []}

    async def get_session_messages(
        self,
        *,
        session_id: str,
        profile_name: str = "default",
    ) -> dict[str, object]:
        assert profile_name == "default"
        return {
            "session_id": session_id,
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "您好"},
            ],
        }

    async def rename_session(
        self,
        *,
        session_id: str,
        title: str,
        profile_name: str = "default",
    ) -> dict[str, object]:
        self.renamed.append((session_id, title, profile_name))
        return {"renamed": True}

    async def delete_session(
        self,
        *,
        session_id: str,
        profile_name: str = "default",
    ) -> dict[str, object]:
        self.deleted.append((session_id, profile_name))
        return {"deleted": True}


def access_token(*, admission_generation: int = 11) -> str:
    return encode_runtime_token(
        {
            "token_type": "runtime_access",
            "schema_version": 1,
            "sub": "user-7",
            "user_id": 7,
            "org_id": 3,
            "runtime_id": "runtime-1",
            "admission_generation": admission_generation,
            "allowed_origin": "https://agent.example.com",
            "connection_nonce": "nonce-0000000000000001",
            "issued_at": NOW,
            "expires_at": NOW + 300,
        },
        secret=SECRET,
    )


def session_grant(
    *,
    admission_generation: int = 11,
    attachments: list[dict[str, str]] | None = None,
) -> str:
    return encode_runtime_token(
        {
            "token_type": "session_grant",
            "schema_version": 1,
            "user_id": 7,
            "org_id": 3,
            "runtime_id": "runtime-1",
            "admission_generation": admission_generation,
            "session_id": "session-00000001",
            "profile_id": "default",
            "runtime_agent_profile_id": 0,
            "model_id": "7",
            "upstream_model_id": "qwen3-235b-a22b",
            "context_length": 128000,
            "config_revision": "revision-18",
            "profile_desired_revision": 0,
            "profile_applied_revision": 0,
            "profile_digest": EMPTY_DIGEST,
            "skill_digest": EMPTY_DIGEST,
            "mcp_digest": EMPTY_DIGEST,
            "knowledge_digest": EMPTY_DIGEST,
            "max_prompt_bytes": 1024,
            "max_output_tokens": 4096,
            "attachments": attachments or [],
            "issued_at": NOW,
            "expires_at": NOW + 1800,
        },
        secret=SECRET,
    )


def unmanaged_turn_profile_identity() -> dict[str, object]:
    return {
        "runtime_agent_profile_id": 0,
        "profile_desired_revision": 0,
        "profile_applied_revision": 0,
        "profile_digest": EMPTY_DIGEST,
        "skill_digest": EMPTY_DIGEST,
        "mcp_digest": EMPTY_DIGEST,
        "knowledge_digest": EMPTY_DIGEST,
    }


def build_app(
    tmp_path: Path,
    *,
    profile_projection=None,
) -> tuple[
    FastAPI,
    SQLiteStore,
    SessionManager,
    RunManager,
    FakeSessionControl,
]:
    store = SQLiteStore(tmp_path / "supervisor.db")
    adapter = ImmediateAdapter()
    health = RuntimeHealth()
    health.mark_supervisor_ready(
        StartupCheckResult(
            ready=True,
            errors=(),
            warnings=(),
            snapshot=StartupResourceSnapshot(
                nofile_soft=65536,
                pids_limit=4096,
                disk_used_percent=1,
                disk_free_bytes=10 * 1024**3,
                sqlite_ok=True,
            ),
        )
    )
    health.mark_hermes_ready()

    async def prepare(_identity) -> None:
        return None

    async def close(_identity) -> None:
        return None

    sessions = SessionManager(
        store,
        runtime_id="runtime-1",
        max_inflight_runs=2,
        post_run_warm_grace_seconds=600,
        hot_session_limit=1,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
    )
    capacity = CapacityGate(2)
    runs = RunManager(store, sessions, capacity, adapter)
    session_control = FakeSessionControl()
    hermes_home = tmp_path / "hermes"
    expert_home = hermes_home / "profiles" / "expert-75"
    expert_home.mkdir(parents=True)
    (expert_home / "config.yaml").write_text(
        "model: {}\nproviders: {}\n",
        encoding="utf-8",
    )
    kanban_model_router = KanbanWorkerModelRouter(
        SupervisorSettings(
            _env_file=None,
            RUNTIME_ID="runtime-1",
            RUNTIME_BINDING_ID="binding-1",
            RUNTIME_MODE="personal",
            HERMES_STARTUP_MODE="steady_state",
            API_SERVER_KEY=SECRET,
            NEWAPI_API_KEY="model-gateway-only-secret-0123456789",
            NEWAPI_BASE_URL="https://newapi.example/v1",
            HERMES_HOME=hermes_home,
        ),
        ModelRouteRegistry(),
    )

    app = FastAPI()
    app.state.kanban_model_router = kanban_model_router
    dependencies = RuntimeEdgeDependencies(
        runtime_id="runtime-1",
        binding_id="binding-1",
        api_server_key=SECRET,
        token_verifier=RuntimeTokenVerifier(
            runtime_id="runtime-1",
            secret=SECRET,
            clock=lambda: NOW + 10,
        ),
        store=store,
        sessions=sessions,
        runs=runs,
        health=health,
        connections=ExternalConnectionGate(8),
        config=FixedConfig(),
        session_control=session_control,
        workspace_dir=tmp_path / "workspace",
        kanban_model_router=kanban_model_router,
        profile_projection=profile_projection,
    )
    attach_runtime_routes(app, dependencies)
    attach_channel_fast_lane(app, dependencies)
    return app, store, sessions, runs, session_control


class FakeProfileProjection:
    def __init__(self) -> None:
        self.requests = []

    async def apply(self, request):
        from platform_supervisor.asset_contracts import (
            ProfileProjectionApplyResponse,
        )

        self.requests.append(request)
        return ProfileProjectionApplyResponse(
            operation_id=request.operation_id,
            runtime_agent_profile_id=request.runtime_agent_profile_id,
            desired_revision=request.desired_revision,
            applied_revision=request.desired_revision,
            status="applied",
            digests=request.digests,
        )

    async def status(self, runtime_agent_profile_id: int):
        return (await self.apply(self.requests[-1])).model_copy(
            update={"runtime_agent_profile_id": runtime_agent_profile_id}
        )

    async def resolve_identity(self, identity, *, allow_pending: bool = False):
        return identity


class WebSocketHarness:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def connect(self) -> dict:
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "wss",
            "path": "/runtime-edge",
            "raw_path": b"/runtime-edge",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"runtime.example.com"),
                (b"origin", b"https://agent.example.com"),
                (
                    b"sec-websocket-protocol",
                    (
                        "hermes-runtime-v1, runtime-access."
                        + access_token()
                    ).encode(),
                ),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("runtime.example.com", 443),
            "subprotocols": [
                "hermes-runtime-v1",
                f"runtime-access.{access_token()}",
            ],
            "state": {},
        }
        self.task = asyncio.create_task(
            self.app(scope, self.incoming.get, self.outgoing.put)
        )
        await self.incoming.put({"type": "websocket.connect"})
        return await self.outgoing.get()

    async def send_json(self, payload: dict) -> None:
        await self.incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps(payload),
            }
        )

    async def receive_json(self) -> dict:
        message = await asyncio.wait_for(self.outgoing.get(), timeout=5)
        assert message["type"] == "websocket.send"
        return json.loads(message["text"])

    async def close(self) -> None:
        await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        if self.task is not None:
            await self.task


class FastLaneWebSocketHarness:
    def __init__(
        self,
        app: FastAPI,
        *,
        gateway_instance_id: str,
        channel_lease_epoch: int,
    ) -> None:
        self.app = app
        self.gateway_instance_id = gateway_instance_id
        self.channel_lease_epoch = channel_lease_epoch
        self.incoming: asyncio.Queue[dict] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

    async def connect(self) -> dict:
        timestamp = int(datetime.now(timezone.utc).timestamp())
        nonce = "fast-lane-nonce-00000001"
        signature = channel_handshake_signature(
            SECRET,
            runtime_id="runtime-1",
            binding_id="binding-1",
            gateway_instance_id=self.gateway_instance_id,
            channel_lease_epoch=self.channel_lease_epoch,
            timestamp=timestamp,
            nonce=nonce,
        )
        headers = {
            "authorization": f"Bearer {SECRET}",
            "host": "runtime.example.com",
            "sec-websocket-protocol": CHANNEL_FAST_LANE_PROTOCOL,
            "x-hermes-runtime-id": "runtime-1",
            "x-hermes-binding-id": "binding-1",
            "x-hermes-gateway-instance-id": self.gateway_instance_id,
            "x-hermes-channel-lease-epoch": str(
                self.channel_lease_epoch
            ),
            "x-hermes-timestamp": str(timestamp),
            "x-hermes-connection-nonce": nonce,
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
                (key.encode(), value.encode())
                for key, value in headers.items()
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("runtime.example.com", 443),
            "subprotocols": [CHANNEL_FAST_LANE_PROTOCOL],
            "state": {},
        }
        self.task = asyncio.create_task(
            self.app(scope, self.incoming.get, self.outgoing.put)
        )
        await self.incoming.put({"type": "websocket.connect"})
        return await self.outgoing.get()

    async def send_json(self, payload: dict) -> None:
        await self.incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps(payload),
            }
        )

    async def receive_json(self) -> dict:
        message = await asyncio.wait_for(self.outgoing.get(), timeout=5)
        assert message["type"] == "websocket.send"
        return json.loads(message["text"])

    async def close(self) -> None:
        await self.incoming.put(
            {"type": "websocket.disconnect", "code": 1000}
        )
        if self.task is not None:
            await self.task


def _fast_lane_submit_frame(
    *,
    source: str,
    target_type: str,
    target_id: int,
    run_id: str,
    gateway_instance_id: str,
    channel_lease_epoch: int,
) -> dict:
    now = datetime.now(timezone.utc)
    unsigned_lease = ChannelExecutionLease(
        lease_id=f"lease-{run_id}",
        org_id=3,
        channel_id=31,
        target_type=target_type,
        target_id=target_id,
        subject_user_id=7,
        runtime_id="runtime-1",
        binding_id="binding-1",
        runtime_session_id=f"session-{run_id}",
        model_entry_id="7",
        profile_id="default",
        gateway_instance_id=gateway_instance_id,
        channel_lease_epoch=channel_lease_epoch,
        channel_generation=4,
        config_generation=18,
        admission_generation=11,
        authorization_epoch=1,
        issued_at=now,
        expires_at=now + timedelta(seconds=120),
        signature="0" * 64,
    )
    lease = unsigned_lease.model_copy(
        update={"signature": channel_lease_signature(SECRET, unsigned_lease)}
    )
    payload = {
        "type": "internal_turn",
        "source": source,
        "run_id": run_id,
        "client_message_id": f"message-{run_id}",
        "identity": {
            "user_id": 7,
            "org_id": 3,
            "session_id": f"session-{run_id}",
            "profile_id": "default",
            **unmanaged_turn_profile_identity(),
            "model_id": "7",
            "context_length": 128000,
            "config_revision": "revision-18",
            "admission_generation": 11,
            "max_prompt_bytes": 1024,
            "max_output_tokens": 4096,
        },
        "prompt": "测试企微 Fast Lane",
        "attachment_file_ids": [],
        "attachments": [],
        "channel_execution_lease": lease.model_dump(mode="json"),
    }
    envelope = {
        "schema_version": 1,
        "command_id": f"command-{run_id}",
        "runtime_id": "runtime-1",
        "binding_id": "binding-1",
        "admission_generation": 11,
        "payload_digest": canonical_payload_digest(payload),
        "issued_at": now.isoformat(),
        "deadline_at": (now + timedelta(seconds=120)).isoformat(),
        "payload": payload,
    }
    return {
        "schema_version": 1,
        "type": "channel.turn.submit",
        "request_id": f"request-{run_id}",
        "gateway_instance_id": gateway_instance_id,
        "channel_lease_epoch": channel_lease_epoch,
        "envelope": envelope,
    }


def _stored_redirect(**updates) -> StoredRedirect:
    values = {
        "redirect_id": "redirect-00000001",
        "session_id": "session-00000001",
        "client_message_id": "message-correction-00000001",
        "request_digest": "redirect-request-digest",
        "parent_run_id": "run-parent-00000001",
        "input_admission_seq": 201,
        "interaction_origin": "user_web",
        "content": "先不要继续，改成另一个方向",
        "state": "WAITING_AGENT_READY",
        "hermes_status": None,
        "dispatch_group_id": None,
        "dispatch_group_position": None,
        "pending_event_seq": 31,
        "accepted_event_seq": None,
        "consumed_event_seq": None,
        "terminal_disposition_kind": None,
        "terminal_disposition_event_seq": None,
        "start_after_seq": None,
        "delivery_id": None,
        "promoted_run_id": None,
        "promotion_reserved_run_id": "run-promoted-00000001",
        "promotion_run_intent": {},
        "promotion_run_payload_digest": "promotion-digest",
        "promoted_pre_prompt_message_id": None,
        "promoted_pre_prompt_history_digest": None,
        "promoted_prompt_wire_digest": None,
        "promoted_proof_message_id": None,
        "promoted_proof_post_digest": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "created": True,
    }
    values.update(updates)
    return StoredRedirect(**values)


@pytest.mark.parametrize(
    ("updates", "expected_type"),
    [
        ({"state": "WAITING_AGENT_READY"}, "followup.received"),
        (
            {
                "state": "RPC_ACKNOWLEDGED",
                "dispatch_group_id": "dispatch-group-00000001",
                "dispatch_group_position": 0,
                "accepted_event_seq": 32,
                "start_after_seq": 32,
            },
            "followup.applying",
        ),
        (
            {
                "state": "CANONICAL_CONSUMED",
                "dispatch_group_id": "dispatch-group-00000001",
                "dispatch_group_position": 0,
                "consumed_event_seq": 33,
                "start_after_seq": 32,
            },
            "followup.applied",
        ),
        (
            {
                "state": "PROMOTED_TO_RUN",
                "dispatch_group_id": "dispatch-group-00000002",
                "dispatch_group_position": 0,
                "terminal_disposition_kind": "promoted",
                "terminal_disposition_event_seq": 34,
                "promoted_run_id": "run-promoted-00000001",
            },
            "followup.continued",
        ),
        (
            {
                "state": "CANCELED_WITH_PARENT",
                "terminal_disposition_kind": "canceled",
                "terminal_disposition_event_seq": 35,
            },
            "followup.failed",
        ),
        (
            {
                "state": "SUBMISSION_UNCERTAIN",
                "dispatch_group_id": "dispatch-group-00000003",
                "dispatch_group_position": 0,
                "terminal_disposition_kind": "uncertain",
                "terminal_disposition_event_seq": 36,
                "start_after_seq": 35,
            },
            "followup.failed",
        ),
    ],
)
def test_stored_redirect_maps_internal_states_to_public_followup_dispositions(
    updates: dict,
    expected_type: str,
) -> None:
    disposition = _edge_interactive_disposition(_stored_redirect(**updates))

    assert disposition.type == expected_type


@pytest.mark.asyncio
async def test_runtime_edge_marks_browser_plain_text_ineligible_for_redirect(
    tmp_path: Path,
) -> None:
    pending = RuntimeEdgeFollowupReceived(
        followup_id="redirect-00000001",
        client_message_id="message-correction-00000001",
        parent_run_id="run-parent-00000001",
        input_admission_seq=201,
        dispatch_group_id=None,
        dispatch_group_position=None,
        event_seq=31,
    )
    app, store, sessions, runs, _control = build_app(tmp_path)
    calls: list[dict] = []

    async def submit_interactive(**kwargs):
        calls.append(kwargs)
        return _stored_redirect()

    runs.submit_interactive = submit_interactive  # type: ignore[method-assign]
    await store.start()
    websocket = WebSocketHarness(app)
    try:
        await websocket.connect()
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "run.submit",
                "client_message_id": "message-correction-00000001",
                "session_grant": session_grant(),
                "prompt": "先不要继续，改成另一个方向",
                "attachment_file_ids": [],
            }
        )

        disposition = await websocket.receive_json()

        assert disposition == pending.model_dump(
            mode="json",
            exclude_none=False,
        )
        assert calls[0]["redirect_eligible"] is False
        assert calls[0]["admission_generation"] == 11
        assert calls[0]["source_detail"] is None
        assert await store.get_run("run-parent-00000001") is None
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_runtime_edge_preserves_nonempty_attachment_source_detail(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    calls: list[dict] = []

    async def submit_interactive(**kwargs):
        calls.append(kwargs)
        return _stored_redirect()

    runs.submit_interactive = submit_interactive  # type: ignore[method-assign]
    await store.start()
    websocket = WebSocketHarness(app)
    try:
        await websocket.connect()
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "run.submit",
                "client_message_id": "message-attachment-00000001",
                "session_grant": session_grant(
                    attachments=[
                        {
                            "file_id": "91",
                            "display_name": "report.txt",
                            "relative_path": "workspace/91/report.txt",
                        }
                    ]
                ),
                "prompt": "分析附件",
                "attachment_file_ids": ["91"],
            }
        )

        await websocket.receive_json()

        assert calls[0]["source_detail"] == {
            "attachment_file_ids": ["91"]
        }
        assert "<platform_attachments>" in calls[0]["prompt"]
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_runtime_edge_followup_submit_targets_active_parent(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    calls: list[dict] = []

    async def submit_interactive(**kwargs):
        calls.append(kwargs)
        return _stored_redirect()

    runs.submit_interactive = submit_interactive  # type: ignore[method-assign]
    await store.start()
    websocket = WebSocketHarness(app)
    try:
        await websocket.connect()
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "followup.submit",
                "client_message_id": "message-correction-00000001",
                "parent_run_id": "run-parent-00000001",
                "session_grant": session_grant(),
                "prompt": "再检查一下日志",
                "attachment_file_ids": [],
            }
        )

        response = await websocket.receive_json()

        assert response["type"] == "followup.received"
        assert calls[0]["redirect_eligible"] is True
        assert calls[0]["interrupt_parent"] is True
        assert calls[0]["expected_parent_run_id"] == "run-parent-00000001"
        assert calls[0]["audit_prompt"] == "再检查一下日志"
        assert calls[0]["source_detail"] is None
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_state", ["queued", "steered", "redirected"])
async def test_runtime_edge_does_not_project_raw_upstream_disposition(
    tmp_path: Path,
    raw_state: str,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)

    async def raw_submission(**_kwargs):
        return {"type": raw_state, "state": raw_state}

    runs.submit_interactive = raw_submission  # type: ignore[method-assign]
    await store.start()
    websocket = WebSocketHarness(app)
    try:
        await websocket.connect()
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "run.submit",
                "client_message_id": "message-correction-00000001",
                "session_grant": session_grant(),
                "prompt": "修正当前任务",
                "attachment_file_ids": [],
            }
        )

        response = await websocket.receive_json()

        assert response["type"] == "error"
        assert response["code"] == "RUNTIME_SESSION_UNAVAILABLE"
        assert response.get("state") not in {"queued", "steered", "redirected"}
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_session_grant_must_match_connection_admission_generation(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    websocket = WebSocketHarness(app)
    try:
        await websocket.connect()
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "session.prepare",
                "request_id": "prepare-generation-0001",
                "session_grant": session_grant(admission_generation=12),
            }
        )

        response = await websocket.receive_json()

        assert response["type"] == "error"
        assert response["code"] == "SESSION_GRANT_INVALID"
        assert response["retryable"] is True
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_websocket_selects_fixed_protocol_and_streams_without_backend(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    websocket = WebSocketHarness(app)
    try:
        accepted_frame = await websocket.connect()
        assert accepted_frame == {
            "type": "websocket.accept",
            "subprotocol": "hermes-runtime-v1",
            "headers": [],
        }
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "session.prepare",
                "request_id": "request-00000001",
                "session_grant": session_grant(),
            }
        )
        ready = await websocket.receive_json()
        assert ready["type"] == "session.ready"

        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "run.submit",
                "client_message_id": "message-00000001",
                "session_grant": session_grant(),
                "prompt": "你好",
                "attachment_file_ids": [],
            }
        )
        accepted = await websocket.receive_json()
        assert accepted["type"] == "run.accepted"
        assert accepted["client_message_id"] == "message-00000001"
        first_event = await websocket.receive_json()
        if first_event["type"] == "run.snapshot":
            assert first_event["answer"] == "你好"
            assert first_event["status"] == "COMPLETED"
        else:
            received = [
                first_event,
                await websocket.receive_json(),
                await websocket.receive_json(),
                await websocket.receive_json(),
                await websocket.receive_json(),
            ]
            assert [item["event_type"] for item in received[:3]] == [
                "chat.message.delta",
                "chat.message.delta",
                "chat.message.delta",
            ]
            assert [item["data"]["delta"] for item in received[:3]] == [
                "你",
                "好",
                "！",
            ]
            assert [item["event_type"] for item in received[3:]] == [
                "chat.message.completed",
                "chat.done",
            ]
        stored = await store.get_run_by_message(
            session_id="session-00000001",
            client_message_id="message-00000001",
        )
        assert stored is not None
        assert stored.source_detail is None
        assert stored.history_message_count_before_run is None
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_resume_queued_run_reports_queue_state_before_admission(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    queued = await store.enqueue_run(
        RunEnqueueIntent(
            run_id="run-queued-resume",
            session_id="session-00000001",
            client_message_id="message-queued-resume",
            payload_digest="a" * 64,
            config_revision="revision-18",
            source="scheduled_task",
            enqueued_at=datetime.now(timezone.utc),
            prompt="scheduled prompt",
            audit_prompt="scheduled prompt",
            identity={
                "runtime_id": "runtime-1",
                "session_id": "session-00000001",
                "profile_id": "default",
                "model_id": "7",
                "config_revision": "revision-18",
                "admission_generation": 11,
                "user_id": 7,
                "org_id": 3,
            },
            scheduled_task_id=7,
            scheduled_occurrence_key="scheduled:7:queued-resume",
        )
    )
    websocket = WebSocketHarness(app)
    try:
        await websocket.connect()
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "session.prepare",
                "request_id": "request-queued-resume",
                "session_grant": session_grant(),
            }
        )
        assert (await websocket.receive_json())["type"] == "session.ready"
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "run.resume",
                "run_id": queued.run_id,
                "after_seq": 0,
            }
        )

        response = await websocket.receive_json()
        assert response["type"] == "run.queued"
        assert response["run_id"] == queued.run_id
        assert response["enqueue_seq"] == queued.enqueue_seq
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_status", "expected_status"),
    [
        ("accepted", "accepted"),
        ("already_terminal", "already_terminal"),
        ("failed", "failed"),
    ],
)
async def test_websocket_returns_correlated_run_cancel_ack(
    tmp_path: Path,
    cancel_status: str,
    expected_status: str,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    run_id = "run-cancel-edge-00000001"
    await store.persist_dispatch_intent(
        DispatchIntent(
            run_id=run_id,
            session_id="session-00000001",
            client_message_id="message-cancel-edge",
            payload_digest="a" * 64,
            config_revision="revision-18",
            source="web",
            accepted_at=datetime.now(timezone.utc),
            user_id=7,
            org_id=3,
        )
    )

    async def cancel(_run_id: str) -> str:
        assert _run_id == run_id
        return cancel_status

    runs.cancel = cancel
    websocket = WebSocketHarness(app)
    try:
        await websocket.connect()
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "run.cancel",
                "run_id": run_id,
            }
        )

        assert await websocket.receive_json() == {
            "schema_version": 1,
            "type": "run.cancel_ack",
            "run_id": run_id,
            "status": expected_status,
        }
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        "live_execution_release",
        "model_route",
        "session_import",
        "dashboard_prewarm",
        "profile_prompt_refresh",
        "unknown",
    ],
)
async def test_websocket_reports_safe_dashboard_prepare_failure_and_stays_connected(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    phase: str,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()

    async def fail_prepare(_identity) -> None:
        cause = HermesProtocolError(
            "sensitive upstream response body",
            status_code=502,
            path="/api/sessions?session_grant=secret",
        )
        if phase == "unknown":
            raise cause
        raise HermesSessionPrepareError(phase, cause)

    sessions.prepare_callback = fail_prepare
    websocket = WebSocketHarness(app)
    try:
        caplog.set_level(logging.WARNING)
        await websocket.connect()
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "session.prepare",
                "request_id": "request-dashboard-missing",
                "session_grant": session_grant(),
            }
        )
        error = await websocket.receive_json()
        assert error == {
            "schema_version": 1,
            "type": "error",
            "code": "RUNTIME_SESSION_PREPARE_FAILED",
            "message": "会话暂时不可用，请稍后重试",
            "retryable": True,
            "request_id": "request-dashboard-missing",
            "phase": phase,
        }
        assert "sensitive upstream response body" not in json.dumps(error)
        record = next(
            item
            for item in caplog.records
            if item.message == "runtime_session_prepare_failed"
        )
        assert record.event == "runtime_session_prepare_failed"
        assert record.phase == phase
        assert record.request_id == "request-dashboard-missing"
        assert record.session_id == "session-00000001"
        assert record.profile_id == "default"
        assert record.upstream_status_code == 502
        assert record.upstream_path == "/api/sessions"
        assert "sensitive upstream response body" not in caplog.text

        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "unsupported",
                "request_id": "request-still-connected",
            }
        )
        still_connected = await websocket.receive_json()
        assert still_connected["code"] == "UNSUPPORTED_MESSAGE"
        assert still_connected["request_id"] == "request-still-connected"
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_dashboard_control_allows_profile_put_and_rejects_other_verbs(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runtime.local",
        ) as client:
            headers = {"Authorization": f"Bearer {SECRET}"}
            allowed = await client.post(
                "/internal/v1/control",
                json={
                    "action": "dashboard_request",
                    "params": {
                        "method": "PUT",
                        "path": "/api/profiles/expert-79/description",
                    },
                },
                headers=headers,
            )
            assert allowed.status_code == 200
            assert allowed.json()["result"]["method"] == "PUT"

            for method in ("OPTIONS", "HEAD"):
                rejected = await client.post(
                    "/internal/v1/control",
                    json={
                        "action": "dashboard_request",
                        "params": {
                            "method": method,
                            "path": "/api/profiles/expert-79/description",
                        },
                    },
                    headers=headers,
                )
                assert rejected.status_code == 403

            escaped = await client.post(
                "/internal/v1/control",
                json={
                    "action": "dashboard_request",
                    "params": {
                        "method": "PUT",
                        "path": "/api/profiles/../config",
                    },
                },
                headers=headers,
            )
            assert escaped.status_code == 403
    finally:
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_internal_control_exposes_typed_profile_apply_and_status(
    tmp_path: Path,
) -> None:
    projection = FakeProfileProjection()
    app, store, sessions, runs, _control = build_app(
        tmp_path,
        profile_projection=projection,
    )
    await store.start()
    request = {
        "schema_version": 1,
        "operation_id": "profile-operation-0001",
        "runtime_id": "runtime-1",
        "binding_id": 119,
        "runtime_workspace_id": 7,
        "runtime_agent_profile_id": 173,
        "hermes_profile_name": "expert-173",
        "desired_revision": 7,
        "expected_applied_revision": 6,
        "digests": {
            "profile": f"sha256:{'a' * 64}",
            "skill": f"sha256:{'b' * 64}",
            "mcp": f"sha256:{'c' * 64}",
            "knowledge": f"sha256:{'d' * 64}",
        },
        "profile_config": {},
        "artifacts": [],
    }
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runtime.local",
        ) as client:
            headers = {"Authorization": f"Bearer {SECRET}"}
            applied = await client.post(
                "/internal/v1/control",
                json={"action": "profile_projection.apply", "params": request},
                headers=headers,
            )
            assert applied.status_code == 200
            assert applied.json()["result"]["status"] == "applied"
            status_response = await client.post(
                "/internal/v1/control",
                json={
                    "action": "profile_projection.status",
                    "params": {"runtime_agent_profile_id": 173},
                },
                headers=headers,
            )
            assert status_response.status_code == 200
            assert status_response.json()["result"]["applied_revision"] == 7

            invalid = await client.post(
                "/internal/v1/control",
                json={
                    "action": "profile_projection.apply",
                    "params": {**request, "unexpected": True},
                },
                headers=headers,
            )
            assert invalid.status_code == 422
    finally:
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_websocket_correlates_submission_rejection_to_client_message(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    websocket = WebSocketHarness(app)
    try:
        await websocket.connect()
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "session.prepare",
                "request_id": "request-00000002",
                "session_grant": session_grant(),
            }
        )
        assert (await websocket.receive_json())["type"] == "session.ready"
        await runs.capacity.begin_drain("test")
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "run.submit",
                "client_message_id": "message-00000002",
                "session_grant": session_grant(),
                "prompt": "你好",
                "attachment_file_ids": [],
            }
        )

        rejected = await websocket.receive_json()
        assert rejected["type"] == "error"
        assert rejected["code"] == "RUNTIME_DRAINING"
        assert rejected["client_message_id"] == "message-00000002"
        assert "request_id" not in rejected
        assert "run_id" not in rejected
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_websocket_returns_session_history_directly_from_runtime(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    selected_session_id = "session-00000001"
    selected_config_revision = "revision-18"
    queued = await store.enqueue_run(
        RunEnqueueIntent(
            run_id="scheduled-history-run",
            session_id=selected_session_id,
            client_message_id="scheduled-history-message",
            payload_digest="a" * 64,
            config_revision=selected_config_revision,
            source="scheduled_task",
            enqueued_at=datetime.now(timezone.utc),
            prompt="scheduled prompt",
            audit_prompt="scheduled prompt",
            identity={
                "runtime_id": "runtime-1",
                "session_id": selected_session_id,
                "profile_id": "default",
                "model_id": "7",
                "config_revision": selected_config_revision,
                "admission_generation": 11,
                "user_id": 7,
                "org_id": 3,
            },
            source_detail={
                "type": "scheduled",
                "scheduled_task_id": 7,
                "scheduled_task_name": "daily",
                "trigger_kind": "scheduled",
                "task_available": True,
            },
            scheduled_task_id=7,
            scheduled_occurrence_key="scheduled:7:history",
        )
    )
    claimed = await store.claim_next_queued(
        selected_session_id,
        accepted_at=datetime.now(timezone.utc),
    )
    assert claimed is not None
    await store.record_run_history_checkpoint(
        queued.run_id,
        history_message_count_before_run=0,
    )
    websocket = WebSocketHarness(app)
    try:
        await websocket.connect()
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "session.history",
                "request_id": "history-00000001",
                "session_grant": session_grant(),
            }
        )
        history = await websocket.receive_json()
        assert history == {
            "schema_version": 1,
            "type": "session.history",
            "request_id": "history-00000001",
            "session_id": "session-00000001",
            "messages": [
                {
                    "role": "user",
                    "content": "你好",
                    "source": {
                        "type": "scheduled",
                        "scheduled_task_id": 7,
                        "scheduled_task_name": "daily",
                        "trigger_kind": "scheduled",
                        "task_available": True,
                    },
                },
                {"role": "assistant", "content": "您好"},
            ],
        }
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_websocket_history_includes_durable_followup_ledger(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    parent = await store.persist_dispatch_intent(
        DispatchIntent(
            run_id="run-parent-00000001",
            session_id="session-00000001",
            client_message_id="message-parent-00000001",
            payload_digest="a" * 64,
            config_revision="revision-18",
            source="web",
            accepted_at=datetime.now(timezone.utc),
        )
    )
    await store.admit_redirect(
        RedirectAdmissionIntent(
            redirect_id="redirect-00000001",
            session_id=parent.session_id,
            client_message_id="correction-00000001",
            request_digest=f"sha256:{'b' * 64}",
            parent_run_id=parent.run_id,
            interaction_origin="user_web",
            content="请继续检查数据库",
            initial_state="WAITING_AGENT_READY",
            promotion_reserved_run_id="run-promoted-00000001",
            promotion_run_intent={
                "prepare_identity": {
                    "runtime_id": "runtime-1",
                    "session_id": parent.session_id,
                    "profile_id": "default",
                    "model_id": "7",
                    "config_revision": "revision-18",
                },
                "prompt": "请继续检查数据库",
            },
            promotion_run_payload_digest=f"sha256:{'c' * 64}",
            admission_generation=11,
            created_at=datetime.now(timezone.utc),
        )
    )
    websocket = WebSocketHarness(app)
    try:
        await websocket.connect()
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "session.history",
                "request_id": "history-followup-0001",
                "session_grant": session_grant(),
            }
        )

        history = await websocket.receive_json()

        assert history["messages"][-1] == {
            "role": "user",
            "content": "请继续检查数据库",
            "client_message_id": "correction-00000001",
            "followup_id": "redirect-00000001",
            "parent_run_id": "run-parent-00000001",
            "input_admission_seq": 2,
            "followup_status": "received",
        }
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_internal_command_requires_runtime_key_and_is_idempotent(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    payload = {
        "type": "prepare",
        "identity": {
            "user_id": 7,
            "org_id": 3,
            "session_id": "session-00000001",
            "profile_id": "default",
            **unmanaged_turn_profile_identity(),
            "model_id": "7",
            "context_length": 128000,
            "config_revision": "revision-18",
            "admission_generation": 11,
            "max_prompt_bytes": 1024,
            "max_output_tokens": 4096,
        },
        "priority": "business",
    }
    envelope = {
        "schema_version": 1,
        "command_id": "command-00000001",
        "runtime_id": "runtime-1",
        "binding_id": "binding-1",
        "admission_generation": 11,
        "payload_digest": canonical_payload_digest(payload),
        "issued_at": datetime(2026, 8, 7, tzinfo=timezone.utc).isoformat(),
        "payload": payload,
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runtime.local",
    ) as client:
        assert (
            await client.post("/internal/v1/commands", json=envelope)
        ).status_code == 401
        response = await client.post(
            "/internal/v1/commands",
            json=envelope,
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        assert response.status_code == 200
        assert response.json()["state"] == "completed"
    await runs.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_internal_run_event_stream_replays_realtime_reasoning_with_auth(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    adapter = GatedReasoningAdapter()
    runs.adapter = adapter
    await store.start()
    payload = {
        "type": "internal_turn",
        "source": "personal_wecom",
        "run_id": "run-realtime-0001",
        "client_message_id": "message-realtime-0001",
        "identity": {
            "user_id": 7,
            "org_id": 3,
            "session_id": "session-realtime-0001",
            "profile_id": "default",
            **unmanaged_turn_profile_identity(),
            "model_id": "7",
            "context_length": 128000,
            "config_revision": "revision-18",
            "admission_generation": 11,
            "max_prompt_bytes": 1024,
            "max_output_tokens": 4096,
        },
        "prompt": "测试实时思考",
        "attachment_file_ids": [],
        "attachments": [],
    }
    envelope = {
        "schema_version": 1,
        "command_id": "command-realtime-0001",
        "runtime_id": "runtime-1",
        "binding_id": "binding-1",
        "admission_generation": 11,
        "payload_digest": canonical_payload_digest(payload),
        "issued_at": datetime(2026, 8, 7, tzinfo=timezone.utc).isoformat(),
        "payload": payload,
    }
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runtime.local",
        ) as client:
            accepted = await client.post(
                "/internal/v1/commands",
                json=envelope,
                headers={"Authorization": f"Bearer {SECRET}"},
            )
            assert accepted.status_code == 200
            await asyncio.wait_for(adapter.reasoning_emitted.wait(), timeout=5)

            unauthorized = await client.get(
                "/internal/v1/runs/run-realtime-0001/events"
            )
            assert unauthorized.status_code == 401

            stream_task = asyncio.create_task(
                client.get(
                    "/internal/v1/runs/run-realtime-0001/events",
                    params={"after_seq": 1},
                    headers={"Authorization": f"Bearer {SECRET}"},
                )
            )
            for _ in range(100):
                context = runs._contexts.get("run-realtime-0001")
                if context is not None and context.subscribers:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("internal event stream did not subscribe")
            adapter.release.set()
            streamed = await asyncio.wait_for(stream_task, timeout=5)

        assert streamed.status_code == 200
        events = [json.loads(line) for line in streamed.text.splitlines()]
        assert [event["event_type"] for event in events] == [
            "chat.reasoning.delta",
            "chat.message.completed",
            "chat.done",
        ]
        assert events[0]["seq"] == 2
        assert events[0]["data"]["delta"] == "真实思考"
        assert events[1]["data"]["reasoning"] == "真实思考"
    finally:
        adapter.release.set()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_kanban_worker_route_command_replays_after_durable_ack(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    payload = {
        "type": "kanban_worker_route_activate",
        "run_id": "kw-115-task-00000001",
        "task_id": "t_task0001",
        "board": "default",
            "profile_id": "expert-75",
            "org_id": 1,
            "model_id": "1",
        "upstream_model_id": "claude-sonnet-4-5",
        "context_length": 128000,
        "runtime_capabilities": {
            "reasoning_replay": "not_supported",
            "thinking_mode": "enabled",
        },
        "max_output_tokens": 8192,
    }
    envelope = {
        "schema_version": 1,
        "command_id": "command-kanban-route-activate-1",
        "runtime_id": "runtime-1",
        "binding_id": "binding-1",
        "admission_generation": 11,
        "payload_digest": canonical_payload_digest(payload),
        "issued_at": datetime(2026, 8, 7, tzinfo=timezone.utc).isoformat(),
        "payload": payload,
    }
    calls = 0
    model_router = app.state.kanban_model_router
    original = model_router.activate

    async def counting_activate(identity):
        nonlocal calls
        calls += 1
        return await original(identity)

    model_router.activate = counting_activate
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runtime.local",
    ) as client:
        first = await client.post(
            "/internal/v1/commands",
            json=envelope,
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        second = await client.post(
            "/internal/v1/commands",
            json=envelope,
            headers={"Authorization": f"Bearer {SECRET}"},
        )
    assert first.status_code == second.status_code == 200
    assert calls == 2
    await runs.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_internal_control_is_authenticated_and_path_bounded(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runtime.local",
    ) as client:
        assert (
            await client.post(
                "/internal/v1/control",
                json={"action": "capabilities"},
            )
        ).status_code == 401
        response = await client.post(
            "/internal/v1/control",
            json={"action": "reload_skills_and_list"},
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        assert response.json() == {"result": [{"name": "search"}]}
        forbidden = await client.post(
            "/internal/v1/control",
            json={
                "action": "dashboard_request",
                "params": {"method": "GET", "path": "/auth/password-login"},
            },
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        assert forbidden.status_code == 403
    await runs.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_internal_control_lists_only_bounded_profile_mcp_registry(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, control = build_app(tmp_path)
    await store.start()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runtime.local",
        ) as client:
            accepted = await client.post(
                "/internal/v1/control",
                json={
                    "action": "list_profile_mcp_servers",
                    "params": {"profile_name": "expert-7"},
                },
                headers={"Authorization": f"Bearer {SECRET}"},
            )
            rejected = await client.post(
                "/internal/v1/control",
                json={
                    "action": "list_profile_mcp_servers",
                    "params": {"profile_name": "../../default"},
                },
                headers={"Authorization": f"Bearer {SECRET}"},
            )

        assert accepted.json() == {"result": [{"name": "scoped-docs"}]}
        assert control.listed_mcp_profiles == ["expert-7"]
        assert rejected.status_code == 422
    finally:
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "target_type", "target_id"),
    [
        ("personal_wecom", "personal_agent", 115),
        ("application_wecom", "application_agent", 25),
    ],
)
async def test_channel_fast_lane_uses_shared_protocol_for_both_wecom_targets(
    tmp_path: Path,
    source: str,
    target_type: str,
    target_id: int,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    gateway_instance_id = "backend-1:im-gateway"
    epoch = 7
    websocket = FastLaneWebSocketHarness(
        app,
        gateway_instance_id=gateway_instance_id,
        channel_lease_epoch=epoch,
    )
    run_id = f"run-{target_type}-0001"
    try:
        accepted_frame = await websocket.connect()
        assert accepted_frame == {
            "type": "websocket.accept",
            "subprotocol": CHANNEL_FAST_LANE_PROTOCOL,
            "headers": [],
        }
        await websocket.send_json(
            _fast_lane_submit_frame(
                source=source,
                target_type=target_type,
                target_id=target_id,
                run_id=run_id,
                gateway_instance_id=gateway_instance_id,
                channel_lease_epoch=epoch,
            )
        )
        accepted = await websocket.receive_json()
        assert accepted["type"] == "channel.turn.ack"
        assert accepted["run_id"] == run_id

        events: list[dict] = []
        while not events or events[-1].get("event_type") != "chat.done":
            events.append(await websocket.receive_json())
        assert any(
            event.get("event_type") == "chat.message.completed"
            for event in events
        )
        highest_seq = max(int(event.get("seq") or 0) for event in events)
        await websocket.send_json(
            {
                "schema_version": 1,
                "type": "channel.delivery.ack",
                "request_id": f"delivery-{run_id}",
                "gateway_instance_id": gateway_instance_id,
                "channel_lease_epoch": epoch,
                "run_id": run_id,
                "highest_contiguous_seq": highest_seq,
                "provider_request_id": f"provider-{run_id}",
                "provider_message_id": f"message-{run_id}",
                "projection_version": 1,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        delivery = await websocket.receive_json()
        assert delivery["type"] == "channel.delivery.ack_ok"
        checkpoint = await store.get_channel_delivery_checkpoint(run_id)
        assert checkpoint is not None
        assert checkpoint.highest_contiguous_seq == highest_seq

        refreshed = _fast_lane_submit_frame(
            source=source,
            target_type=target_type,
            target_id=target_id,
            run_id=run_id,
            gateway_instance_id=gateway_instance_id,
            channel_lease_epoch=epoch,
        )
        await websocket.send_json(refreshed)
        duplicate = await websocket.receive_json()
        assert duplicate["type"] == "channel.turn.ack"
        assert duplicate["run_id"] == run_id
        assert duplicate["created"] is False
    finally:
        await websocket.close()
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_internal_control_preserves_bounded_dashboard_upstream_error(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, control = build_app(tmp_path)

    async def missing_files(*, method, path, json_body=None):
        del method, json_body
        raise HermesProtocolError(
            "sensitive upstream response body",
            status_code=404,
            path=path,
        )

    control.dashboard_request = missing_files  # type: ignore[method-assign]
    await store.start()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runtime.local",
        ) as client:
            response = await client.post(
                "/internal/v1/control",
                json={
                    "action": "dashboard_request",
                    "params": {"method": "GET", "path": "/api/files"},
                },
                headers={"Authorization": f"Bearer {SECRET}"},
            )

        assert response.status_code == 502
        assert response.json() == {
            "detail": {
                "code": "RUNTIME_DASHBOARD_UPSTREAM_ERROR",
                "message": "runtime Dashboard request failed",
                "upstream_status_code": 404,
                "upstream_path": "/api/files",
            }
        }
        assert "sensitive upstream response body" not in response.text
    finally:
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_internal_control_omits_untrusted_dashboard_error_fields(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, control = build_app(tmp_path)

    async def unsafe_error(*, method, path, json_body=None):
        del method, path, json_body
        raise HermesProtocolError(
            "token=secret-value",
            status_code=200,
            path="https://secret.invalid/api/files",
        )

    control.dashboard_request = unsafe_error  # type: ignore[method-assign]
    await store.start()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runtime.local",
        ) as client:
            response = await client.post(
                "/internal/v1/control",
                json={
                    "action": "dashboard_request",
                    "params": {"method": "GET", "path": "/api/files"},
                },
                headers={"Authorization": f"Bearer {SECRET}"},
            )

        assert response.status_code == 502
        assert response.json() == {
            "detail": {
                "code": "RUNTIME_DASHBOARD_UPSTREAM_ERROR",
                "message": "runtime Dashboard request failed",
            }
        }
        assert "secret-value" not in response.text
        assert "secret.invalid" not in response.text
    finally:
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_internal_file_control_stays_under_workspace(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    headers = {"Authorization": f"Bearer {SECRET}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runtime.local",
    ) as client:
        path = "workspace/sessions/session-1/report.txt"
        uploaded = await client.put(
            "/internal/v1/files/content",
            params={"path": path},
            content=b"report",
            headers=headers,
        )
        assert uploaded.status_code == 200
        listed = await client.get(
            "/internal/v1/files",
            params={"session_id": "session-1"},
            headers=headers,
        )
        assert listed.json()["entries"][0]["relative_path"] == path
        downloaded = await client.get(
            "/internal/v1/files/content",
            params={"path": path},
            headers=headers,
        )
        assert downloaded.content == b"report"
        escaped = await client.get(
            "/internal/v1/files/content",
            params={"path": "workspace/../config.yaml"},
            headers=headers,
        )
        assert escaped.status_code == 400
    await runs.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_session_mutation_command_is_supervisor_owned(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, control = build_app(tmp_path)
    await store.start()
    payload = {
        "type": "session_mutation",
        "operation": "rename",
        "session_id": "session-00000001",
        "profile_id": "default",
        "title": "新标题",
    }
    envelope = {
        "schema_version": 1,
        "command_id": "command-session-rename-1",
        "runtime_id": "runtime-1",
        "binding_id": "binding-1",
        "admission_generation": 11,
        "payload_digest": canonical_payload_digest(payload),
        "issued_at": datetime(2026, 8, 7, tzinfo=timezone.utc).isoformat(),
        "payload": payload,
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runtime.local",
    ) as client:
        response = await client.post(
            "/internal/v1/commands",
            json=envelope,
            headers={"Authorization": f"Bearer {SECRET}"},
        )
    assert response.status_code == 200
    assert response.json()["state"] == "completed"
    assert control.renamed == [
        ("session-00000001", "新标题", "default")
    ]
    await runs.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_capacity_rejected_command_can_retry_same_id(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    payload = {
        "type": "internal_turn",
        "source": "openapi",
        "run_id": "run-00000009",
        "client_message_id": "message-00000009",
        "identity": {
            "user_id": 7,
            "org_id": 3,
            "session_id": "session-00000009",
            "profile_id": "default",
            **unmanaged_turn_profile_identity(),
            "model_id": "7",
            "context_length": 128000,
            "config_revision": "revision-18",
            "admission_generation": 11,
            "max_prompt_bytes": 1024,
            "max_output_tokens": 4096,
        },
        "prompt": "重试",
        "attachment_file_ids": [],
        "attachments": [],
    }
    envelope = {
        "schema_version": 1,
        "command_id": "command-00000009",
        "runtime_id": "runtime-1",
        "binding_id": "binding-1",
        "admission_generation": 11,
        "payload_digest": canonical_payload_digest(payload),
        "issued_at": datetime(2026, 8, 7, tzinfo=timezone.utc).isoformat(),
        "payload": payload,
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runtime.local",
    ) as client:
        await runs.capacity.begin_drain("test")
        rejected = await client.post(
            "/internal/v1/commands",
            json=envelope,
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        assert rejected.status_code == 429
        assert rejected.headers["Retry-After"] == "1"
        assert rejected.json()["error_code"] == "RUNTIME_DRAINING"

        await runs.capacity.cancel_drain("test")
        accepted = await client.post(
            "/internal/v1/commands",
            json=envelope,
            headers={"Authorization": f"Bearer {SECRET}"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["state"] == "accepted"
        assert accepted.json()["run_id"] == "run-00000009"

    await runs.close()
    await sessions.close()
    await store.close()


@pytest.mark.asyncio
async def test_internal_turn_coalescing_returns_canonical_run_receipt(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()

    async def coalesced_submit(**_kwargs):
        return SimpleNamespace(
            run=SimpleNamespace(
                run_id="run-existing-candidate",
                state="QUEUED",
            ),
            created=False,
        )

    runs.submit = coalesced_submit  # type: ignore[method-assign]
    payload = {
        "type": "internal_turn",
        "source": "scheduled_task",
        "run_id": "run-new-occurrence",
        "client_message_id": "message-new-occurrence",
        "identity": {
            "user_id": 7,
            "org_id": 3,
            "session_id": "session-00000001",
            "profile_id": "default",
            **unmanaged_turn_profile_identity(),
            "model_id": "7",
            "context_length": 128000,
            "config_revision": "revision-18",
            "admission_generation": 11,
            "max_prompt_bytes": 1024,
            "max_output_tokens": 4096,
        },
        "prompt": "定时汇总",
        "attachment_file_ids": [],
        "attachments": [],
        "scheduled_task_id": 7,
        "scheduled_occurrence_key": "scheduled:7:new-occurrence",
    }
    envelope = {
        "schema_version": 1,
        "command_id": "command-new-occurrence",
        "runtime_id": "runtime-1",
        "binding_id": "binding-1",
        "admission_generation": 11,
        "payload_digest": canonical_payload_digest(payload),
        "issued_at": datetime(2026, 8, 7, tzinfo=timezone.utc).isoformat(),
        "payload": payload,
    }
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runtime.local",
        ) as client:
            response = await client.post(
                "/internal/v1/commands",
                json=envelope,
                headers={"Authorization": f"Bearer {SECRET}"},
            )

        assert response.status_code == 200, response.text
        assert response.json()["run_id"] == "run-existing-candidate"
        assert response.json()["result"] == {
            "admission_state": "queued",
            "coalesced": True,
            "canonical_run_id": "run-existing-candidate",
        }
    finally:
        await runs.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_file_ticket_streams_upload_and_download_without_backend_body(
    tmp_path: Path,
) -> None:
    app, store, sessions, runs, _control = build_app(tmp_path)
    await store.start()
    upload_claims = {
        "token_type": "file_transfer",
        "schema_version": 1,
        "ticket_id": "upload-ticket-0000000019",
        "operation": "upload",
        "user_id": 7,
        "org_id": 3,
        "runtime_id": "runtime-1",
        "session_id": "session-00000001",
        "file_id": 19,
        "relative_path": "workspace/sessions/session-00000001/19/report.txt",
        "display_name": "report.txt",
        "mime_type": "text/plain",
        "max_bytes": 17,
        "issued_at": NOW,
        "expires_at": NOW + 300,
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runtime.local",
    ) as client:
        uploaded = await asyncio.wait_for(
            client.put(
                "/runtime-files/upload",
                content=b"runtime file body",
                headers={
                    "Authorization": (
                        "Bearer "
                        + encode_runtime_token(upload_claims, secret=SECRET)
                    )
                },
            ),
            timeout=5,
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["size_bytes"] == 17
        target = (
            tmp_path
            / "workspace"
            / "sessions"
            / "session-00000001"
            / "19"
            / "report.txt"
        )
        assert target.read_bytes() == b"runtime file body"
        projection = (await store.load_projection_batch())[0]
        assert projection.projection_type == "file.ready"
        assert projection.payload["file_id"] == 19

        replayed_upload = await client.put(
            "/runtime-files/upload",
            content=b"runtime file body",
            headers={
                "Authorization": (
                    "Bearer "
                    + encode_runtime_token(upload_claims, secret=SECRET)
                )
            },
        )
        assert replayed_upload.status_code == 401

        download_claims = {
            **upload_claims,
            "ticket_id": "download-ticket-00000019",
            "operation": "download",
            "max_bytes": 17,
        }
        downloaded = await asyncio.wait_for(
            client.get(
                "/runtime-files/download",
                headers={
                    "Authorization": (
                        "Bearer "
                        + encode_runtime_token(
                            download_claims,
                            secret=SECRET,
                        )
                    )
                },
            ),
            timeout=5,
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"runtime file body"
        assert downloaded.headers["content-length"] == "17"

        replayed_download = await client.get(
            "/runtime-files/download",
            headers={
                "Authorization": (
                    "Bearer "
                    + encode_runtime_token(
                        download_claims,
                        secret=SECRET,
                    )
                )
            },
        )
        assert replayed_download.status_code == 401

    await runs.close()
    await sessions.close()
    await store.close()
