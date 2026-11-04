from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.im_gateway import runtime_bridge as runtime_bridge_module
from app.im_gateway.queue import QueuedBatchMessage, QueuedInbound
from app.im_gateway.messages import NormalizedWeComMessage
from app.im_gateway.node_bridge import WeComBridgeError
from app.models import ImChannel, ImInboundMessage
from app.im_gateway.runtime_bridge import (
    ApplicationImRuntimeBridge,
    ImPreemptionCoordinator,
    PersonalImRuntimeBridge,
    PreparedWeComArtifact,
    PreparedWeComFastLaneRoute,
    WeComFastLaneArtifactDelivery,
    _application_wecom_run_identity,
    _format_wecom_batch_prompt,
    _log_personal_im_run_timing,
    execute_durable_wecom_fast_lane_v2,
    execute_prepared_wecom_fast_lane,
)
from app.schemas.runtime_internal import RuntimeTurnIdentity
from app.services.runtime_chat_timing import RuntimeChatTiming
from app.services.runtime_commands import RuntimeCommandTarget
from app.services.runtime_models import RuntimeModelRecord


_UNMANAGED_PROFILE_IDENTITY = {
    "runtime_agent_profile_id": 0,
    "profile_desired_revision": 0,
    "profile_applied_revision": 0,
    "profile_digest": "sha256:" + "0" * 64,
    "skill_digest": "sha256:" + "0" * 64,
    "mcp_digest": "sha256:" + "0" * 64,
    "knowledge_digest": "sha256:" + "0" * 64,
}


def _personal_queued() -> QueuedInbound:
    return QueuedInbound(
        inbound_message_id=101,
        channel_id=7,
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        runtime_session_row_id=4,
        external_conversation_id="chat",
        provider_request_id="request",
        message_type="text",
        normalized_content={"text": "hello"},
        quote_payload=None,
        attempt_count=1,
        stream_id="stream",
        generation=1,
        owner_instance_id="backend-a",
        lease_epoch=1,
    )


def _application_queued() -> QueuedInbound:
    return QueuedInbound(
        inbound_message_id=202,
        channel_id=17,
        org_id=1,
        user_id=None,
        runtime_workspace_id=None,
        runtime_session_row_id=None,
        external_conversation_id="application-chat",
        provider_request_id="application-request",
        message_type="text",
        normalized_content={"text": "hello application"},
        quote_payload=None,
        attempt_count=1,
        stream_id="application-stream",
        generation=2,
        owner_instance_id="backend-a",
        lease_epoch=3,
        target_type="application_agent",
        application_agent_id=21,
        application_client_id=31,
        sender_platform_user_id=41,
    )


def test_application_wecom_run_identity_is_deterministic_per_inbound() -> None:
    queued = QueuedInbound(
        inbound_message_id=101,
        channel_id=7,
        org_id=1,
        user_id=None,
        runtime_workspace_id=None,
        runtime_session_row_id=None,
        external_conversation_id="chat",
        provider_request_id="request",
        message_type="text",
        normalized_content={"text": "hello"},
        quote_payload=None,
        attempt_count=1,
        stream_id="stream",
        generation=1,
        owner_instance_id="backend-a",
        lease_epoch=1,
    )

    first = _application_wecom_run_identity(queued)
    second = _application_wecom_run_identity(queued)

    assert first == second
    assert first[0] == "wecom:7:101"
    assert first[1].startswith("app_run_wecom_")
    assert len(first[1]) == len("app_run_wecom_") + 40


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_status", "route_expected"),
    [("active", True), ("disabled", False)],
)
async def test_personal_fast_lane_route_honors_active_profile_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    profile_status: str,
    route_expected: bool,
) -> None:
    channel = SimpleNamespace(
        id=7,
        org_id=1,
        enabled=True,
        target_type="personal_agent",
        provider="wecom",
        user_id=2,
        runtime_workspace_id=3,
        runtime_session_row_id=4,
        config_generation=5,
    )
    lease = SimpleNamespace(
        owner_instance_id="backend-a",
        lease_epoch=6,
        expires_at=(
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(minutes=5)
        ),
    )
    session = SimpleNamespace(
        id=4,
        status="active",
        source_type="wecom",
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        runtime_session_id="session-wecom-4",
        session_model_entry_id=None,
    )
    profile = SimpleNamespace(
        id=8,
        status=profile_status,
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        hermes_profile_name="default",
    )
    workspace = SimpleNamespace(
        status="active",
        default_model_id=None,
    )
    binding = SimpleNamespace(
        id=9,
        status="ready",
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        endpoint_host_header="runtime.example",
        runtime_id="runtime-3",
        pvc_storage_identity_hash="pvc-hash",
        config_generation=11,
        config_hash="config-hash",
    )
    model = SimpleNamespace(
        id=21,
        model_key="visible-default",
        runtime_capabilities=None,
    )

    class Result:
        def __init__(self, *, row=None, scalar=None) -> None:
            self._row = row
            self._scalar = scalar

        def first(self):
            return self._row

        def scalar_one_or_none(self):
            return self._scalar

    class Db:
        def __init__(self) -> None:
            self.executions = 0

        async def execute(self, _statement):
            self.executions += 1
            if self.executions == 1:
                return Result(
                    row=(
                        channel,
                        lease,
                        session,
                        profile,
                        workspace,
                        binding,
                    )
                )
            return Result(scalar=model)

    db = Db()

    class SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args: object) -> None:
            return None

    class ModelStore:
        async def list_visible_models(self, **_values: object):
            return [
                RuntimeModelRecord(
                    model_entry_id=21,
                    model_key="visible-default",
                    display_name="Visible Default",
                    provider_id=1,
                    provider_display_name="Provider",
                    is_default=True,
                )
            ]

    monkeypatch.setattr(
        runtime_bridge_module,
        "AsyncSessionLocal",
        SessionContext,
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "SqlAlchemyRuntimeModelStore",
        lambda _db: ModelStore(),
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "load_runtime_command_admission_generation",
        AsyncMock(return_value=12),
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "resolve_profile_execution_identity",
        AsyncMock(
            return_value=SimpleNamespace(
                runtime_agent_profile_id=8,
                desired_revision=3,
                applied_revision=3,
                profile_digest="sha256:" + "0" * 64,
                skill_digest="sha256:" + "1" * 64,
                mcp_digest="sha256:" + "2" * 64,
                knowledge_digest="sha256:" + "3" * 64,
            )
        ),
    )
    bridge = object.__new__(PersonalImRuntimeBridge)
    bridge._hot_routes = {}
    bridge.activate_runtime = AsyncMock(
        return_value=SimpleNamespace(
            runtime_id="runtime-3",
            endpoint_host_header="runtime.example",
            api_key="runtime-key",
        )
    )

    route = await bridge.prepare_fast_lane_route(
        channel_id=7,
        gateway_instance_id="backend-a",
        channel_lease_epoch=6,
        channel_generation=5,
    )

    if not route_expected:
        assert route is None
        bridge.activate_runtime.assert_not_awaited()
        return
    assert route is not None
    assert route.identity.model_id == "21"
    assert route.identity.upstream_model_id == "visible-default"


@pytest.mark.asyncio
async def test_application_fast_lane_route_refreshes_current_model_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(
        id=719,
        channel_id=3,
        target_type="application_agent",
        dispatch_lane="fast_lane_v2",
        ingress_ticket_state="pending",
        runtime_disposition_seq=None,
        runtime_canonical_run_id=None,
        sender_platform_user_id=101,
        external_conversation_id="conversation-a",
        application_agent_id=25,
        chat_type="direct",
    )
    channel = SimpleNamespace(
        id=3,
        org_id=1,
        target_type="application_agent",
        application_agent_id=25,
        application_client_id=35,
        enabled=True,
        status="connected",
        config_generation=1,
    )

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, model: object, row_id: int) -> object:
            if model is ImInboundMessage and row_id == 719:
                return message
            if model is ImChannel and row_id == 3:
                return channel
            raise AssertionError("unexpected route preparation lookup")

    def route_plan(generation: int) -> SimpleNamespace:
        target = RuntimeCommandTarget(
            runtime_id="application-agent-25",
            binding_id="125",
            endpoint_host_header="application-agent-25.test",
            api_server_key="test-only",
            admission_generation=9,
        )
        return SimpleNamespace(
            target=target,
            identity=RuntimeTurnIdentity(
                user_id=101,
                org_id=1,
                session_id="app-25-session-a",
                profile_id="default",
                **_UNMANAGED_PROFILE_IDENTITY,
                model_id="19" if generation == 17 else "21",
                upstream_model_id=(
                    "deepseek-chat" if generation == 17 else "qwen-max"
                ),
                runtime_capabilities={},
                config_revision=f"generation-{generation}",
                max_prompt_bytes=1_000_000,
                max_output_tokens=32_000,
                admission_generation=9,
            ),
            application_agent_id=25,
            application_client_id=35,
            subject_user_id=101,
            runtime_session_id="app-25-session-a",
            config_generation=generation,
        )

    current_plan = [route_plan(17)]
    prepared_calls: list[dict[str, object]] = []
    knowledge_calls: list[dict[str, object]] = []

    class InvocationService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def prepare_channel_fast_lane(
            self, **values: object
        ) -> object:
            prepared_calls.append(values)
            return current_plan[0]

    class PreparedKnowledge:
        async def prepare_application_agent_channel(
            self, **values: object
        ) -> None:
            knowledge_calls.append(values)

        async def prepare_application_agent(
            self, **_values: object
        ) -> None:
            raise AssertionError(
                "cold Fast Lane route must not require an active Backend Run"
            )

    monkeypatch.setattr(runtime_bridge_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(
        runtime_bridge_module,
        "SqlAlchemyApplicationAgentInvocationService",
        InvocationService,
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "shared_runtime_prepared_knowledge_service",
        lambda: PreparedKnowledge(),
    )
    bridge = object.__new__(ApplicationImRuntimeBridge)
    bridge._hot_routes = {}

    first = await bridge.prepare_fast_lane_route(
        inbound_message_id=719,
        channel_id=3,
        external_conversation_id="conversation-a",
        platform_user_id=101,
        gateway_instance_id="backend-instance-a",
        channel_lease_epoch=23,
        channel_generation=1,
    )
    current_plan[0] = route_plan(18)
    second = await bridge.prepare_fast_lane_route(
        inbound_message_id=719,
        channel_id=3,
        external_conversation_id="conversation-a",
        platform_user_id=101,
        gateway_instance_id="backend-instance-a",
        channel_lease_epoch=23,
        channel_generation=1,
    )
    repeated = await bridge.prepare_fast_lane_route(
        inbound_message_id=719,
        channel_id=3,
        external_conversation_id="conversation-a",
        platform_user_id=101,
        gateway_instance_id="backend-instance-a",
        channel_lease_epoch=23,
        channel_generation=1,
    )

    assert first is not None and first.config_generation == 17
    assert second is not None and second.config_generation == 18
    assert second is not first
    assert repeated is second
    assert len(prepared_calls) == 3
    assert len(knowledge_calls) == 2
    assert all(
        call["application_client_id"] == 35
        and call["subject_user_id"] == 101
        for call in knowledge_calls
    )
    assert all(
        call["application_client_id"] == 35
        for call in prepared_calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_type", "target_id"),
    [
        ("personal_agent", 3),
        ("application_agent", 21),
    ],
)
async def test_prepared_text_turn_uses_no_mysql_before_supervisor_and_finishes(
    monkeypatch: pytest.MonkeyPatch,
    target_type: str,
    target_id: int,
) -> None:
    envelopes: list[object] = []
    replies: list[dict[str, object]] = []

    class NoDatabase:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("hot Fast Lane must not open MySQL")

    class CommandClient:
        async def send(self, *, target: object, envelope: object) -> object:
            del target
            envelopes.append(envelope)
            return SimpleNamespace(
                state="accepted",
                run_id=envelope.payload.run_id,
                error_code=None,
            )

        async def wait_run(self, **values: object) -> dict[str, object]:
            sink = values["event_sink"]
            await sink(
                "chat.message.completed",
                {"seq": 1, "text": "热路径回复"},
            )
            await sink("chat.done", {"seq": 2, "status": "completed"})
            return {"status": "COMPLETED", "answer": "热路径回复"}

        async def close(self) -> None:
            return None

    class Bridge:
        async def reply_stream(self, **values: object) -> bool:
            replies.append(values)
            return True

    async def acknowledge(**_values: object) -> int:
        return 2

    monkeypatch.setattr(runtime_bridge_module, "AsyncSessionLocal", NoDatabase)
    monkeypatch.setattr(
        runtime_bridge_module,
        "RuntimeChannelFastLaneClient",
        CommandClient,
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "acknowledge_channel_delivery",
        acknowledge,
    )
    route = PreparedWeComFastLaneRoute(
        target=RuntimeCommandTarget(
            runtime_id="runtime-1",
            binding_id="binding-1",
            endpoint_host_header="runtime.internal",
            api_server_key="secret-" + "x" * 32,
            admission_generation=11,
        ),
        identity=RuntimeTurnIdentity(
            user_id=2,
            org_id=1,
            session_id="session-hot-1",
            profile_id="default",
            **_UNMANAGED_PROFILE_IDENTITY,
            model_id="7",
            upstream_model_id="upstream-model",
            config_revision="generation-1",
            admission_generation=11,
            max_prompt_bytes=4096,
            max_output_tokens=4096,
        ),
        channel_id=7,
        target_type=target_type,
        target_id=target_id,
        gateway_instance_id="backend-a:im-gateway",
        channel_lease_epoch=1,
        channel_generation=1,
        config_generation=1,
        prepared_at=runtime_bridge_module.time.monotonic(),
    )
    message = NormalizedWeComMessage(
        external_message_id="provider-message-1",
        provider_request_id="provider-request-1",
        external_conversation_id="conversation-1",
        message_type="text",
        text="继续",
        quote_payload=None,
        sender_member_id="member-1",
    )

    result = await execute_prepared_wecom_fast_lane(
        node_bridge=Bridge(),
        route=route,
        message=message,
    )

    assert result.delivery_status == "completed"
    assert result.highest_contiguous_seq == 2
    assert result.artifacts == ()
    assert replies[-1]["finish"] is True
    assert replies[-1]["content"] == "热路径回复"
    assert envelopes[0].payload.source == (
        "application_wecom"
        if target_type == "application_agent"
        else "personal_wecom"
    )


@pytest.mark.asyncio
async def test_durable_fast_lane_recovery_resumes_without_resubmit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resumed: list[dict[str, object]] = []
    durable_facts: list[object] = []
    disposition = SimpleNamespace(
        delivery_id="delivery-model-switch-1",
        client_message_id="message-model-switch-1",
        disposition_seq=1,
        state="ordinary_run_accepted",
        canonical_run_id="run-qwen-before-switch",
        parent_run_id=None,
        start_after_seq=0,
    )

    class Projector:
        def __init__(self, **_values: object) -> None:
            self.state = SimpleNamespace(
                last_seq=7,
                logical_text="",
                status="pending",
            )

        async def load(self) -> None:
            return None

        async def on_event(self, _event: object) -> None:
            return None

    class Client:
        async def submit_or_resume(self, **_values: object) -> object:
            raise AssertionError(
                "a Runtime-durable delivery must never be resubmitted"
            )

        async def resume(self, **values: object) -> tuple[object, object]:
            resumed.append(values)
            return object(), disposition

        async def wait_delivery(self, **_values: object) -> object:
            return disposition

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def is_fast_lane_delivery_owner(
            self, **_values: object
        ) -> bool:
            return True

    monkeypatch.setattr(
        runtime_bridge_module,
        "WeComStreamProjector",
        Projector,
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "RuntimeChannelFastLaneV2Client",
        Client,
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "AsyncSessionLocal",
        SessionContext,
    )
    monkeypatch.setattr(runtime_bridge_module, "ImInboundQueue", Queue)
    route = PreparedWeComFastLaneRoute(
        target=RuntimeCommandTarget(
            runtime_id="application-agent-runtime-25",
            binding_id="17",
            endpoint_host_header="application-agent-25.internal",
            api_server_key="secret-" + "x" * 32,
            admission_generation=15,
        ),
        identity=RuntimeTurnIdentity(
            user_id=2,
            org_id=1,
            session_id="application-session-1",
            profile_id="default",
            **_UNMANAGED_PROFILE_IDENTITY,
            model_id="19",
            upstream_model_id="deepseek-current",
            config_revision="generation-15",
            admission_generation=15,
            max_prompt_bytes=4096,
            max_output_tokens=4096,
        ),
        channel_id=7,
        target_type="application_agent",
        target_id=25,
        gateway_instance_id="backend-a:im-gateway",
        channel_lease_epoch=21,
        channel_generation=6,
        config_generation=15,
        prepared_at=runtime_bridge_module.time.monotonic(),
    )
    message = NormalizedWeComMessage(
        external_message_id="provider-message-model-switch-1",
        provider_request_id="provider-request-model-switch-1",
        external_conversation_id="conversation-1",
        message_type="text",
        text="旧 Qwen delivery 的问题",
        quote_payload=None,
        sender_member_id="member-1",
    )

    async def record_durable_fact(value: object) -> None:
        durable_facts.append(value)

    await execute_durable_wecom_fast_lane_v2(
        node_bridge=object(),
        route=route,
        message=message,
        inbound_message_id=699,
        delivery_id="delivery-model-switch-1",
        client_message_id="message-model-switch-1",
        provider_stream_id="provider-stream-model-switch-1",
        runtime_durable_sink=record_durable_fact,
        runtime_disposition_seq=1,
        runtime_canonical_run_id="run-qwen-before-switch",
        runtime_start_after_seq=0,
    )

    assert len(resumed) == 1
    assert resumed[0]["delivery_id"] == "delivery-model-switch-1"
    assert resumed[0]["last_disposition_seq"] == 1
    assert resumed[0]["last_event_seq"] == 7
    assert durable_facts == [disposition]


@pytest.mark.asyncio
async def test_prepared_text_turn_keeps_only_current_run_terminal_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CommandClient:
        async def send(self, *, target: object, envelope: object) -> object:
            del target
            return SimpleNamespace(
                state="accepted",
                run_id=envelope.payload.run_id,
                error_code=None,
            )

        async def wait_run(self, **values: object) -> dict[str, object]:
            sink = values["event_sink"]
            await sink(
                "chat.artifact.created",
                {
                    "seq": 1,
                    "relative_path": "workspace/sessions/session-hot-1/report.pptx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "size_bytes": 10,
                    "mtime_ns": 100,
                },
            )
            await sink(
                "chat.artifact.updated",
                {
                    "seq": 2,
                    "relative_path": "workspace/sessions/session-hot-1/report.pptx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "size_bytes": 20,
                    "mtime_ns": 200,
                },
            )
            await sink(
                "chat.artifact.created",
                {
                    "seq": 3,
                    "relative_path": "workspace/sessions/session-hot-1/transient.txt",
                    "mime_type": "text/plain",
                    "size_bytes": 5,
                    "mtime_ns": 150,
                },
            )
            await sink(
                "chat.artifact.deleted",
                {
                    "seq": 4,
                    "relative_path": "workspace/sessions/session-hot-1/transient.txt",
                },
            )
            await sink(
                "chat.message.completed",
                {"seq": 5, "text": "PPT 已生成"},
            )
            await sink("chat.done", {"seq": 6, "status": "completed"})
            return {"status": "COMPLETED", "answer": "PPT 已生成"}

        async def close(self) -> None:
            return None

    class Bridge:
        async def reply_stream(self, **_values: object) -> bool:
            return True

    monkeypatch.setattr(
        runtime_bridge_module,
        "RuntimeChannelFastLaneClient",
        CommandClient,
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "acknowledge_channel_delivery",
        AsyncMock(return_value=6),
    )
    route = PreparedWeComFastLaneRoute(
        target=RuntimeCommandTarget(
            runtime_id="runtime-1",
            binding_id="binding-1",
            endpoint_host_header="runtime.internal",
            api_server_key="secret-" + "x" * 32,
            admission_generation=11,
        ),
        identity=RuntimeTurnIdentity(
            user_id=2,
            org_id=1,
            session_id="session-hot-1",
            profile_id="default",
            **_UNMANAGED_PROFILE_IDENTITY,
            model_id="7",
            upstream_model_id="upstream-model",
            config_revision="generation-1",
            admission_generation=11,
            max_prompt_bytes=4096,
            max_output_tokens=4096,
        ),
        channel_id=7,
        target_type="personal_agent",
        target_id=3,
        gateway_instance_id="backend-a:im-gateway",
        channel_lease_epoch=1,
        channel_generation=1,
        config_generation=1,
        prepared_at=runtime_bridge_module.time.monotonic(),
    )
    message = NormalizedWeComMessage(
        external_message_id="provider-message-artifact",
        provider_request_id="provider-request-artifact",
        external_conversation_id="conversation-1",
        message_type="text",
        text="生成 PPT",
        quote_payload=None,
        sender_member_id="member-1",
    )

    result = await execute_prepared_wecom_fast_lane(
        node_bridge=Bridge(),
        route=route,
        message=message,
    )

    assert result.artifacts == (
        PreparedWeComArtifact(
            relative_path="workspace/sessions/session-hot-1/report.pptx",
            mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            size_bytes=20,
            mtime_ns=200,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target_type", ["personal_agent", "application_agent"])
async def test_fast_lane_artifact_delivery_is_current_message_scoped_and_idempotent(
    tmp_path,
    target_type: str,
) -> None:
    uploaded: list[dict[str, object]] = []
    streamed: list[str] = []

    class Store:
        def __init__(self) -> None:
            self.values: dict[int, set[str]] = {}

        async def delivered_keys(self, inbound_message_id: int) -> set[str]:
            return set(self.values.get(inbound_message_id, set()))

        async def mark_delivered(
            self, inbound_message_id: int, artifact_key: str
        ) -> None:
            self.values.setdefault(inbound_message_id, set()).add(artifact_key)

        async def mark_failed(
            self, inbound_message_id: int, error_code: str
        ) -> None:
            raise AssertionError((inbound_message_id, error_code))

        async def clear_failure(self, inbound_message_id: int) -> None:
            return None

    class Dashboard:
        async def stream_managed_artifact(
            self, _endpoint: object, *, relative_path: str
        ):
            streamed.append(relative_path)
            yield b"pptx-bytes"

    class Bridge:
        temp_root = tmp_path

        @staticmethod
        def require_scoped_path(value: str):
            return runtime_bridge_module.Path(value)

        async def upload_media(self, **values: object) -> None:
            uploaded.append(values)

        async def send_markdown(self, **_values: object) -> None:
            raise AssertionError("small PPT must not use markdown fallback")

    route = PreparedWeComFastLaneRoute(
        target=RuntimeCommandTarget(
            runtime_id="runtime-1",
            binding_id="binding-1",
            endpoint_host_header="runtime.internal",
            api_server_key="secret-" + "x" * 32,
            admission_generation=11,
        ),
        identity=RuntimeTurnIdentity(
            user_id=2,
            org_id=1,
            session_id="session-hot-1",
            profile_id="default",
            **_UNMANAGED_PROFILE_IDENTITY,
            model_id="7",
            upstream_model_id="upstream-model",
            config_revision="generation-1",
            admission_generation=11,
            max_prompt_bytes=4096,
            max_output_tokens=4096,
        ),
        channel_id=7,
        target_type=target_type,
        target_id=3,
        gateway_instance_id="backend-a:im-gateway",
        channel_lease_epoch=1,
        channel_generation=1,
        config_generation=1,
        prepared_at=runtime_bridge_module.time.monotonic(),
    )
    message = NormalizedWeComMessage(
        external_message_id="provider-message-artifact",
        provider_request_id="provider-request-artifact",
        external_conversation_id="conversation-1",
        message_type="text",
        text="生成 PPT",
        quote_payload=None,
        sender_member_id="member-1",
    )
    artifact = PreparedWeComArtifact(
        relative_path="workspace/sessions/session-hot-1/report.pptx",
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        size_bytes=10,
        mtime_ns=100,
    )
    store = Store()
    delivery = WeComFastLaneArtifactDelivery(
        node_bridge=Bridge(),
        dashboard=Dashboard(),
        checkpoint_store=store,
    )

    await delivery.deliver(
        route=route,
        message=message,
        inbound_message_id=101,
        artifacts=(artifact,),
    )
    await delivery.deliver(
        route=route,
        message=message,
        inbound_message_id=101,
        artifacts=(artifact,),
    )
    await delivery.deliver(
        route=route,
        message=message,
        inbound_message_id=102,
        artifacts=(),
    )
    await delivery.deliver(
        route=route,
        message=message,
        inbound_message_id=103,
        artifacts=(artifact,),
    )

    assert streamed == [artifact.relative_path, artifact.relative_path]
    assert len(uploaded) == 2
    assert all(item["media_type"] == "file" for item in uploaded)
    assert all(item["filename"] == "report.pptx" for item in uploaded)
    assert all(item["chat_id"] == "conversation-1" for item in uploaded)


@pytest.mark.asyncio
async def test_fast_lane_artifact_delivery_retries_file_only(
    tmp_path,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    class Store:
        def __init__(self) -> None:
            self.delivered: set[str] = set()
            self.failures: list[str] = []

        async def delivered_keys(self, _inbound_message_id: int) -> set[str]:
            return set(self.delivered)

        async def mark_delivered(
            self, _inbound_message_id: int, artifact_key: str
        ) -> None:
            self.delivered.add(artifact_key)

        async def mark_failed(
            self, _inbound_message_id: int, error_code: str
        ) -> None:
            self.failures.append(error_code)

        async def clear_failure(self, _inbound_message_id: int) -> None:
            return None

    class Dashboard:
        async def stream_managed_artifact(
            self, _endpoint: object, *, relative_path: str
        ):
            del relative_path
            yield b"pptx-bytes"

    class Bridge:
        temp_root = tmp_path

        @staticmethod
        def require_scoped_path(value: str):
            return runtime_bridge_module.Path(value)

        async def upload_media(self, **_values: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise WeComBridgeError(
                    "TEMPORARY_UPLOAD_FAILURE",
                    "temporary failure",
                )

        async def send_markdown(self, **_values: object) -> None:
            raise AssertionError("temporary failure must retry the file")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    store = Store()
    delivery = WeComFastLaneArtifactDelivery(
        node_bridge=Bridge(),
        dashboard=Dashboard(),
        checkpoint_store=store,
        sleep=sleep,
    )
    route = PreparedWeComFastLaneRoute(
        target=RuntimeCommandTarget(
            runtime_id="runtime-1",
            binding_id="binding-1",
            endpoint_host_header="runtime.internal",
            api_server_key="secret-" + "x" * 32,
            admission_generation=11,
        ),
        identity=RuntimeTurnIdentity(
            user_id=2,
            org_id=1,
            session_id="session-hot-1",
            profile_id="default",
            **_UNMANAGED_PROFILE_IDENTITY,
            model_id="7",
            upstream_model_id="upstream-model",
            config_revision="generation-1",
            admission_generation=11,
            max_prompt_bytes=4096,
            max_output_tokens=4096,
        ),
        channel_id=7,
        target_type="personal_agent",
        target_id=3,
        gateway_instance_id="backend-a:im-gateway",
        channel_lease_epoch=1,
        channel_generation=1,
        config_generation=1,
        prepared_at=runtime_bridge_module.time.monotonic(),
    )
    message = NormalizedWeComMessage(
        external_message_id="provider-message-artifact",
        provider_request_id="provider-request-artifact",
        external_conversation_id="conversation-1",
        message_type="text",
        text="生成 PPT",
        quote_payload=None,
        sender_member_id="member-1",
    )
    artifact = PreparedWeComArtifact(
        relative_path="workspace/sessions/session-hot-1/report.pptx",
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        size_bytes=10,
        mtime_ns=100,
    )

    await delivery.deliver(
        route=route,
        message=message,
        inbound_message_id=101,
        artifacts=(artifact,),
    )

    assert attempts == 3
    assert sleeps == [1.0, 2.0]
    assert store.failures == ["WeComBridgeError", "WeComBridgeError"]
    assert len(store.delivered) == 1


def test_batch_prompt_keeps_every_user_message_verbatim_and_ordered() -> None:
    messages = (
        QueuedBatchMessage(201, 1, {"text": "B 原文"}, None),
        QueuedBatchMessage(
            202,
            2,
            {"text": "C 原文\n第二行"},
            {"text": "被引用内容"},
        ),
    )

    prompt = _format_wecom_batch_prompt(messages, attachments={})
    payload = json.loads(
        prompt.removeprefix("<wecom_messages>\n").removesuffix(
            "\n</wecom_messages>"
        )
    )

    assert [item["text"] for item in payload["messages"]] == [
        "B 原文",
        "C 原文\n第二行",
    ]
    assert [item["inbound_message_id"] for item in payload["messages"]] == [
        201,
        202,
    ]
    assert payload["messages"][1]["quote"] == {"text": "被引用内容"}


@pytest.mark.asyncio
async def test_preemption_waits_for_terminal_snapshot_after_cancel_ack() -> None:
    class Client:
        def __init__(self) -> None:
            self.envelope = None
            self.closed = False

        async def send(self, *, target, envelope):
            self.envelope = envelope
            return type("Ack", (), {"run_id": "run-000a"})()

        async def wait_run(self, *, target, run_id, timeout_seconds):
            assert run_id == "run-000a"
            assert timeout_seconds == 30
            return {"status": "CANCELED", "run_id": run_id}

        async def close(self) -> None:
            self.closed = True

    client = Client()
    coordinator = ImPreemptionCoordinator()
    coordinator.command_client_factory = lambda: client
    target = RuntimeCommandTarget(
        runtime_id="runtime-a",
        binding_id="binding-a",
        endpoint_host_header="runtime-a.internal",
        api_server_key="secret",
        admission_generation=11,
    )

    snapshot = await coordinator._cancel_and_wait(
        target=target,
        command_id="cancel-a",
        runtime_run_id="run-000a",
    )

    assert snapshot == {"status": "CANCELED", "run_id": "run-000a"}
    assert client.envelope.command_id == "cancel-a"
    assert client.envelope.payload.run_id == "run-000a"
    assert client.closed is True


def test_personal_im_run_timing_logs_safe_stage_breakdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    values = iter(1.0 + index * 0.01 for index in range(14))
    timing = RuntimeChatTiming(clock=lambda: next(values))
    for stage in (
        "attachments_done",
        "run_prepare_done",
        "run_profile_gate_done",
        "run_binding_lock_done",
        "run_session_lock_done",
        "run_reserved_id_check_done",
        "run_active_check_done",
        "run_commit_begin",
        "run_commit_done",
        "run_created",
        "im_run_bound",
        "model_route_prepare_started",
        "projector_loaded",
        "model_route_prepare_done",
    ):
        timing.mark(stage)
    timing.bind_run("rr-im-wecom-101")

    caplog.set_level(
        logging.INFO,
        logger="uvicorn.error.runtime_timing",
    )
    _log_personal_im_run_timing(
        timing,
        inbound_message_id=101,
        runtime_session_id="rs-123-1",
    )

    assert "personal_agent_im" in caplog.text
    assert "rr-im-wecom-101" in caplog.text
    assert "rs-123-1" in caplog.text
    assert "attachments_done_to_run_prepare_done" in caplog.text
    assert "run_prepare_done_to_run_profile_gate_done" in caplog.text
    assert "run_commit_done_to_run_created" in caplog.text
    assert "attachments_done_to_run_created" in caplog.text
    assert "run_created_to_im_run_bound" in caplog.text
    assert (
        "model_route_prepare_started_to_model_route_prepare_done"
        in caplog.text
    )
    assert "im_run_bound_to_projector_loaded" in caplog.text


@pytest.mark.asyncio
async def test_personal_runtime_activation_and_prompt_build_are_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExecutionReachedDatabase(RuntimeError):
        pass

    activation_started = asyncio.Event()
    prompt_started = asyncio.Event()
    bridge = object.__new__(PersonalImRuntimeBridge)

    async def activate_runtime(**_values: object) -> object:
        activation_started.set()
        await asyncio.wait_for(prompt_started.wait(), timeout=1)
        return object()

    async def build_prompt(_queued: QueuedInbound) -> str:
        prompt_started.set()
        await asyncio.wait_for(activation_started.wait(), timeout=1)
        return "hello"

    class FailingSessionContext:
        async def __aenter__(self) -> object:
            raise ExecutionReachedDatabase

        async def __aexit__(self, *_args: object) -> None:
            return None

    bridge.activate_runtime = activate_runtime  # type: ignore[method-assign]
    bridge._build_prompt = build_prompt  # type: ignore[method-assign]
    monkeypatch.setattr(
        runtime_bridge_module,
        "AsyncSessionLocal",
        FailingSessionContext,
    )

    with pytest.raises(ExecutionReachedDatabase):
        await bridge.execute(_personal_queued())

    assert activation_started.is_set() is True
    assert prompt_started.is_set() is True


@pytest.mark.asyncio
async def test_pending_personal_run_reenters_submission_not_db_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SubmissionReachedDatabase(RuntimeError):
        pass

    class StatusSession:
        async def scalar(self, _statement: object) -> str:
            return "pending"

    class SessionContext:
        enters = 0

        async def __aenter__(self) -> object:
            type(self).enters += 1
            if type(self).enters == 1:
                return StatusSession()
            raise SubmissionReachedDatabase

        async def __aexit__(self, *_args: object) -> None:
            return None

    queued = _personal_queued()
    queued = QueuedInbound(
        **{
            **queued.__dict__,
            "runtime_run_row_id": 77,
        }
    )
    bridge = object.__new__(PersonalImRuntimeBridge)
    bridge._recover_existing_run = AsyncMock()  # type: ignore[method-assign]
    bridge.activate_runtime = AsyncMock(return_value=object())  # type: ignore[method-assign]
    bridge._build_prompt = AsyncMock(return_value="hello")  # type: ignore[method-assign]
    monkeypatch.setattr(
        runtime_bridge_module,
        "AsyncSessionLocal",
        SessionContext,
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "load_runtime_command_admission_generation",
        AsyncMock(return_value=11),
    )

    with pytest.raises(SubmissionReachedDatabase):
        await bridge.execute(queued)

    bridge._recover_existing_run.assert_not_awaited()


def test_personal_wecom_has_no_backend_model_route_preparation() -> None:
    assert not hasattr(
        PersonalImRuntimeBridge,
        "_prepare_model_route",
    )


@pytest.mark.asyncio
async def test_application_wecom_projects_runtime_events_before_sealed_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    projected: list[str] = []

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def bind_application_run(self, **_values: object) -> bool:
            return True

        async def mark_batch_runtime_terminal(
            self, **_values: object
        ) -> bool:
            order.append("runtime_terminal")
            return True

        async def mark_ready_to_deliver(
            self, **_values: object
        ) -> str:
            order.append("sealed_final")
            return "application-stream"

    class Projector:
        def __init__(self, **_values: object) -> None:
            self.state = SimpleNamespace(last_seq=0, status="pending")

        async def load(self) -> None:
            order.append("projector_loaded")

        async def on_event(self, event: object) -> None:
            event_name = str(getattr(event, "event"))
            projected.append(event_name)
            order.append(event_name)

        async def deliver_final(self, final_text: str) -> str:
            assert final_text == "application answer"
            order.append("deliver_final")
            return "delivered"

    class InvocationService:
        def __init__(self, *_args: object, **_values: object) -> None:
            pass

        async def invoke_channel(self, **values: object) -> object:
            event_sink = values.get("event_sink")
            assert callable(event_sink)
            await values["run_created_sink"](
                SimpleNamespace(
                    id=51,
                    runtime_run_id="app_run_wecom_test",
                )
            )
            await event_sink(
                runtime_bridge_module.PlatformStreamEvent(
                    event="chat.reasoning.delta",
                    data={"seq": 1, "delta": "reasoning"},
                )
            )
            await event_sink(
                runtime_bridge_module.PlatformStreamEvent(
                    event="chat.message.delta",
                    data={"seq": 2, "delta": "answer"},
                )
            )
            await event_sink(
                runtime_bridge_module.PlatformStreamEvent(
                    event="chat.done",
                    data={"seq": 3, "status": "completed"},
                )
            )
            return SimpleNamespace(output_text="application answer")

    monkeypatch.setattr(
        runtime_bridge_module,
        "AsyncSessionLocal",
        SessionContext,
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "load_runtime_command_admission_generation",
        AsyncMock(return_value=11),
    )
    monkeypatch.setattr(runtime_bridge_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime_bridge_module,
        "WeComStreamProjector",
        Projector,
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "SqlAlchemyApplicationAgentInvocationService",
        InvocationService,
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "shared_runtime_prepared_knowledge_service",
        lambda: object(),
    )

    bridge = ApplicationImRuntimeBridge(node_bridge=object())
    run_id, status = await bridge.execute(_application_queued())

    assert (run_id, status) == (51, "completed")
    assert projected == [
        "chat.reasoning.delta",
        "chat.message.delta",
    ]
    assert order.index("chat.reasoning.delta") < order.index(
        "sealed_final"
    )
    assert order.index("sealed_final") < order.index("deliver_final")


@pytest.mark.asyncio
async def test_personal_wecom_projects_runtime_events_before_sealed_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    projected: list[str] = []
    scalar_values = iter(
        (
                SimpleNamespace(
                    id=12,
                runtime_id="runtime-12",
                endpoint_host_header="runtime-12.internal",
                config_hash="config-12",
                config_generation=1,
                ),
                61,
                SimpleNamespace(id=51, desired_revision=3, applied_revision=3),
                SimpleNamespace(
                    profile_digest="sha256:" + "1" * 64,
                    skill_digest="sha256:" + "2" * 64,
                    mcp_digest="sha256:" + "3" * 64,
                    knowledge_digest="sha256:" + "4" * 64,
                ),
        )
    )

    class QueryResult:
        def one_or_none(self) -> tuple[str, int, dict[str, str]]:
            return (
                "upstream-model",
                128000,
                {"thinking_mode": "enabled"},
            )

    class Database:
        async def scalar(self, _statement: object) -> object:
            return next(scalar_values)

        async def execute(self, _statement: object) -> QueryResult:
            return QueryResult()

    class SessionContext:
        async def __aenter__(self) -> Database:
            return Database()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def bind_runtime_run(self, **_values: object) -> bool:
            return True

        async def mark_batch_accepted(self, **_values: object) -> bool:
            return True

        async def mark_batch_runtime_terminal(
            self, **_values: object
        ) -> bool:
            order.append("runtime_terminal")
            return True

        async def mark_ready_to_deliver(
            self, **_values: object
        ) -> str:
            order.append("sealed_final")
            return "stream"

    class ChatService:
        async def create_run(self, **_values: object) -> object:
            return SimpleNamespace(
                runtime_run_id="rr-im-wecom-101",
                runtime_session_id="runtime-session-101",
                runtime_workspace_id=3,
                model_entry_id=7,
                    hermes_profile_name="default",
                    runtime_agent_profile_id=51,
                reused=False,
                status="pending",
            )

    class Projector:
        state = SimpleNamespace(last_seq=0, status="pending")

        async def load(self) -> None:
            order.append("projector_loaded")

        async def on_event(self, event: object) -> None:
            event_name = str(getattr(event, "event"))
            projected.append(event_name)
            order.append(event_name)

        async def deliver_final(self, final_text: str) -> str:
            assert final_text == "personal answer"
            order.append("deliver_final")
            return "delivered"

    class CommandClient:
        async def send(self, **_values: object) -> object:
            return SimpleNamespace(
                state="accepted",
                run_id="rr-im-wecom-101",
                error_code=None,
            )

        async def wait_run(self, **values: object) -> dict[str, object]:
            event_sink = values.get("event_sink")
            assert callable(event_sink)
            await event_sink(
                "chat.reasoning.delta",
                {"seq": 1, "delta": "reasoning"},
            )
            await event_sink(
                "chat.message.delta",
                {"seq": 2, "delta": "answer"},
            )
            await event_sink(
                "chat.done",
                {"seq": 3, "status": "completed"},
            )
            return {
                "status": "COMPLETED",
                "answer": "personal answer",
            }

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        runtime_bridge_module,
        "AsyncSessionLocal",
        SessionContext,
    )
    monkeypatch.setattr(runtime_bridge_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime_bridge_module,
        "_build_runtime_chat_service",
        lambda _db: ChatService(),
    )
    monkeypatch.setattr(
        runtime_bridge_module,
        "load_runtime_command_admission_generation",
        AsyncMock(return_value=11),
    )
    monkeypatch.setattr(
        runtime_bridge_module,
            "RuntimeChannelFastLaneClient",
        CommandClient,
    )

    bridge = object.__new__(PersonalImRuntimeBridge)
    bridge.node_bridge = object()
    bridge.activate_runtime = AsyncMock(
        return_value=SimpleNamespace(api_key="runtime-key")
    )
    bridge._build_prompt = AsyncMock(return_value="hello")
    bridge._runtime_session_id = AsyncMock(
        return_value="runtime-session-101"
    )
    projector = Projector()
    bridge._projector = lambda _queued, **_kwargs: projector
    bridge._deliver_run_artifacts = AsyncMock()

    run_id, status = await bridge.execute(_personal_queued())

    assert (run_id, status) == (61, "completed")
    assert projected == [
        "chat.reasoning.delta",
        "chat.message.delta",
    ]
    assert order.index("chat.reasoning.delta") < order.index(
        "sealed_final"
    )
    assert order.index("sealed_final") < order.index("deliver_final")
