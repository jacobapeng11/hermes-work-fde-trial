from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.im_gateway import runtime as runtime_module
from app.im_gateway.leases import OwnedImChannel
from app.im_gateway.node_bridge import WeComBridgeError
from app.im_gateway.queue import (
    InboundReceipt,
    QueuedFastLaneInbound,
    QueuedInbound,
)
from app.im_gateway.runtime import ImGatewayRuntime
from app.im_gateway.runtime_bridge import (
    PreparedWeComArtifact,
    PreparedWeComFastLaneResult,
    WeComFastLaneRouteRejected,
)
from app.services.runtime_workspace import Principal
from app.services.application_agent_invocation import (
    ApplicationAgentInvocationError,
)


def _actor() -> OwnedImChannel:
    return OwnedImChannel(
        channel_id=1,
        org_id=2,
        user_id=3,
        runtime_workspace_id=4,
        runtime_session_row_id=5,
        runtime_session_id="session-1",
        bot_id="bot-1",
        secret="secret-1",
        config_generation=6,
        lease_epoch=7,
        status="registered",
    )


def _application_actor() -> OwnedImChannel:
    return OwnedImChannel(
        channel_id=11,
        org_id=2,
        user_id=None,
        runtime_workspace_id=None,
        runtime_session_row_id=None,
        runtime_session_id=None,
        bot_id="app-bot-1",
        secret="secret-1",
        config_generation=6,
        lease_epoch=7,
        status="connected",
        target_type="application_agent",
        created_by_user_id=3,
        application_agent_id=21,
        application_client_id=31,
        direct_enabled=True,
        group_enabled=True,
        max_inflight_runs=8,
    )


def _message_frame(
    *,
    message_id: str = "message-1",
    message_type: str = "text",
) -> dict[str, object]:
    payload = (
        {"content": "你好"}
        if message_type == "text"
        else {
            "url": "https://example.test/image",
            "aeskey": "image-key",
        }
    )
    return {
        "headers": {"req_id": f"request-{message_id}"},
        "body": {
            "msgid": message_id,
            "chattype": "single",
            "from": {"userid": "zhangsan"},
            "msgtype": message_type,
            message_type: payload,
        },
    }


def _enter_chat_frame() -> dict[str, object]:
    return {
        "headers": {"req_id": "request-enter-chat"},
        "body": {
            "msgid": "event-enter-chat",
            "chattype": "single",
            "from": {"userid": "zhangsan"},
            "msgtype": "event",
            "event": {"eventtype": "enter_chat"},
        },
    }


def _queued(
    *,
    runtime_run_row_id: int | None = None,
) -> QueuedInbound:
    return QueuedInbound(
        inbound_message_id=10,
        channel_id=1,
        org_id=2,
        user_id=3,
        runtime_workspace_id=4,
        runtime_session_row_id=5,
        external_conversation_id="zhangsan",
        provider_request_id="request-10",
        message_type="text",
        normalized_content={"text": "你好"},
        quote_payload=None,
        attempt_count=1,
        stream_id="stream-10",
        generation=6,
        owner_instance_id="backend-1",
        lease_epoch=7,
        runtime_run_row_id=runtime_run_row_id,
    )


def _fast_lane_queued() -> QueuedFastLaneInbound:
    return QueuedFastLaneInbound(
        inbound_message_id=44,
        channel_id=1,
        delivery_id="im_delivery_00000000000000000000000000000001",
        client_message_id="im_message_00000000000000000000000000000001",
        ingress_ordering_key="personal-session:5",
        ingress_ticket=2,
        provider_request_id="request-44",
        provider_stream_id="stream-44",
        external_message_id="message-44",
        external_conversation_id="member-44",
        message_type="text",
        normalized_content={"text": "继续", "chat_type": "direct"},
        quote_payload=None,
        target_type="personal_agent",
        sender_platform_user_id=3,
        owner_instance_id="backend-a",
        lease_epoch=7,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "expected_dashboard"),
    [
        (_actor(), "personal-dashboard"),
        (_application_actor(), "application-dashboard"),
    ],
)
async def test_existing_hot_projection_delivers_only_current_run_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    actor: OwnedImChannel,
    expected_dashboard: str,
) -> None:
    runtime = ImGatewayRuntime()
    runtime.runtime_bridge.dashboard = "personal-dashboard"
    runtime.application_runtime_bridge.dashboard = "application-dashboard"
    deliveries: list[dict[str, object]] = []

    class Session:
        calls = 0

        async def scalar(self, _statement: object) -> object:
            type(self).calls += 1
            if type(self).calls % 2 == 1:
                return SimpleNamespace(id=55)
            return SimpleNamespace(id=66)

        async def get(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    class SessionContext:
        async def __aenter__(self) -> Session:
            return Session()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Delivery:
        def __init__(self, **values: object) -> None:
            self.values = values

        async def deliver(self, **values: object) -> None:
            deliveries.append({**self.values, **values})

    monkeypatch.setattr(runtime_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        runtime_module,
        "WeComFastLaneArtifactDelivery",
        Delivery,
    )
    artifact = PreparedWeComArtifact(
        relative_path="workspace/sessions/session-1/report.pptx",
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        size_bytes=100,
        mtime_ns=200,
    )
    result = PreparedWeComFastLaneResult(
        runtime_run_id="run-1",
        provider_stream_id="stream-1",
        highest_contiguous_seq=9,
        final_text="PPT 已生成",
        delivery_status="completed",
        artifacts=(artifact,),
    )
    message = SimpleNamespace(
        external_message_id="message-artifact-1",
        external_conversation_id="conversation-1",
        provider_request_id="request-1",
        chat_type="direct",
        message_type="text",
        normalized_content={"text": "生成 PPT"},
        quote_payload=None,
    )

    await runtime._project_hot_message(
        actor=actor,
        message=message,
        route=SimpleNamespace(target_id=21),
        result=result,
    )

    assert len(deliveries) == 1
    assert deliveries[0]["dashboard"] == expected_dashboard
    assert deliveries[0]["inbound_message_id"] == 55
    assert deliveries[0]["artifacts"] == (artifact,)


@pytest.mark.asyncio
async def test_hot_projection_without_current_run_artifacts_never_uploads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()

    class Session:
        calls = 0

        async def scalar(self, _statement: object) -> object:
            type(self).calls += 1
            if type(self).calls % 2 == 1:
                return SimpleNamespace(id=55)
            return SimpleNamespace(id=66)

        async def get(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    class SessionContext:
        async def __aenter__(self) -> Session:
            return Session()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class ForbiddenDelivery:
        def __init__(self, **_values: object) -> None:
            raise AssertionError("ordinary follow-up must not initialize upload")

    monkeypatch.setattr(runtime_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        runtime_module,
        "WeComFastLaneArtifactDelivery",
        ForbiddenDelivery,
    )

    await runtime._project_hot_message(
        actor=_actor(),
        message=SimpleNamespace(
            external_message_id="message-text-2",
            external_conversation_id="conversation-1",
            provider_request_id="request-2",
            chat_type="direct",
            message_type="text",
            normalized_content={"text": "继续解释"},
            quote_payload=None,
        ),
        route=SimpleNamespace(target_id=4),
        result=PreparedWeComFastLaneResult(
            runtime_run_id="run-2",
            provider_stream_id="stream-2",
            highest_contiguous_seq=3,
            final_text="普通回复",
            delivery_status="completed",
            artifacts=(),
        ),
    )


@pytest.mark.asyncio
async def test_personal_followup_starts_preemption_while_run_task_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()
    runtime.instance_id = "backend-a"
    runtime.preemption_coordinator.preempt_next = AsyncMock(
        return_value=True
    )

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def claim_staging(self, **_values: object) -> None:
            return None

    monkeypatch.setattr(runtime_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)

    blocker = asyncio.Event()
    active = asyncio.create_task(blocker.wait())
    runtime._run_tasks[(1, "__personal__")] = active
    try:
        await runtime._schedule_personal_channel(_actor())
        preemption = runtime._preemption_tasks[1]
        await asyncio.wait_for(preemption, timeout=1)
        runtime.preemption_coordinator.preempt_next.assert_awaited_once_with(
            channel_id=1,
            owner_instance_id="backend-a",
            lease_epoch=7,
        )
    finally:
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active


@pytest.mark.asyncio
async def test_active_personal_run_does_not_block_durable_fast_lane_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()
    runtime.instance_id = "backend-a"
    claimed = QueuedFastLaneInbound(
        inbound_message_id=44,
        channel_id=1,
        delivery_id="im_delivery_00000000000000000000000000000001",
        client_message_id="im_message_00000000000000000000000000000001",
        ingress_ordering_key="personal-session:5",
        ingress_ticket=2,
        provider_request_id="request-44",
        provider_stream_id="stream-44",
        external_message_id="message-44",
        external_conversation_id="member-44",
        message_type="text",
        normalized_content={"text": "继续", "chat_type": "direct"},
        quote_payload=None,
        target_type="personal_agent",
        sender_platform_user_id=3,
        owner_instance_id="backend-a",
        lease_epoch=7,
    )
    claims = [claimed, None]
    claim_values: list[dict[str, object]] = []
    started = asyncio.Event()
    release = asyncio.Event()

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def claim_next_fast_lane(self, **_values: object):
            claim_values.append(_values)
            return claims.pop(0)

    async def execute_fast_lane_v2(**_values: object) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(runtime_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime,
        "_execute_fast_lane_v2",
        execute_fast_lane_v2,
    )
    ordinary = asyncio.create_task(asyncio.Event().wait())
    runtime._run_tasks[(1, "__personal__")] = ordinary
    try:
        count = await runtime._schedule_fast_lane_v2(
            _actor(),
            max_inflight=1,
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert count == 1
        assert (1, claimed.delivery_id) in runtime._hot_turn_tasks
        assert claim_values[0]["active_delivery_ids"] == set()
    finally:
        release.set()
        await asyncio.gather(
            *runtime._hot_turn_tasks.values(),
            return_exceptions=True,
        )
        ordinary.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ordinary


@pytest.mark.asyncio
async def test_finished_local_fast_lane_task_does_not_block_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()
    runtime.instance_id = "backend-a"
    claimed = _fast_lane_queued()
    stale = asyncio.create_task(asyncio.sleep(0))
    await stale
    key = (claimed.channel_id, claimed.delivery_id)
    runtime._hot_turn_tasks[key] = stale
    claims = [claimed, None]
    executed = asyncio.Event()

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def claim_next_fast_lane(self, **_values: object):
            return claims.pop(0)

    async def execute_fast_lane_v2(**_values: object) -> None:
        executed.set()

    monkeypatch.setattr(runtime_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime,
        "_execute_fast_lane_v2",
        execute_fast_lane_v2,
    )

    count = await runtime._schedule_fast_lane_v2(
        _actor(),
        max_inflight=1,
    )
    await asyncio.wait_for(executed.wait(), timeout=1)

    assert count == 1
    await asyncio.gather(
        *runtime._hot_turn_tasks.values(),
        return_exceptions=True,
    )


@pytest.mark.asyncio
async def test_cold_personal_fast_lane_route_is_prepared_before_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()
    runtime.instance_id = "backend-a"
    actor = _actor()
    queued = QueuedFastLaneInbound(
        inbound_message_id=44,
        channel_id=1,
        delivery_id="im_delivery_00000000000000000000000000000001",
        client_message_id="im_message_00000000000000000000000000000001",
        ingress_ordering_key="personal-session:5",
        ingress_ticket=2,
        provider_request_id="request-44",
        provider_stream_id="stream-44",
        external_message_id="message-44",
        external_conversation_id="member-44",
        message_type="text",
        normalized_content={"text": "继续", "chat_type": "direct"},
        quote_payload=None,
        target_type="personal_agent",
        sender_platform_user_id=3,
        owner_instance_id="backend-a",
        lease_epoch=7,
    )
    prepared_route = object()
    runtime.runtime_bridge.hot_route = lambda **_values: None
    runtime.runtime_bridge.prepare_fast_lane_route = AsyncMock(
        return_value=prepared_route
    )

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def mark_fast_lane_runtime_durable(
            self, **_values: object
        ) -> bool:
            return True

        async def mark_completed(self, **_values: object) -> bool:
            return True

    async def execute(**values: object) -> PreparedWeComFastLaneResult:
        assert values["route"] is prepared_route
        sink = values["runtime_durable_sink"]
        await sink(
            SimpleNamespace(
                disposition_seq=1,
                state="run_queued",
                canonical_run_id="run-44",
                parent_run_id=None,
                start_after_seq=0,
            )
        )
        return PreparedWeComFastLaneResult(
            runtime_run_id="run-44",
            provider_stream_id="stream-44",
            highest_contiguous_seq=1,
            final_text="完成",
            delivery_status="completed",
            artifacts=(),
        )

    monkeypatch.setattr(runtime_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime_module,
        "execute_durable_wecom_fast_lane_v2",
        execute,
    )

    await runtime._execute_fast_lane_v2(actor=actor, queued=queued)

    runtime.runtime_bridge.prepare_fast_lane_route.assert_awaited_once_with(
        channel_id=1,
        gateway_instance_id="backend-a",
        channel_lease_epoch=7,
        channel_generation=6,
    )


@pytest.mark.asyncio
async def test_personal_fast_lane_model_rejection_settles_and_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()
    runtime.instance_id = "backend-a"
    actor = _actor()
    queued = _fast_lane_queued()
    rejection = WeComFastLaneRouteRejected(
        error_code="MODEL_NOT_AVAILABLE",
        user_message="当前没有可用模型，请先在平台选择模型后重新发送。",
    )
    runtime.runtime_bridge.hot_route = lambda **_values: None
    runtime.runtime_bridge.prepare_fast_lane_route = AsyncMock(
        side_effect=rejection
    )
    runtime.bridge.reply_stream = AsyncMock()
    persisted: list[dict[str, object]] = []

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def mark_fast_lane_validation_rejected(
            self,
            **values: object,
        ) -> bool:
            persisted.append(values)
            return True

    monkeypatch.setattr(runtime_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)

    await runtime._execute_fast_lane_v2(actor=actor, queued=queued)

    assert persisted == [
        {
            "inbound_message_id": queued.inbound_message_id,
            "owner_instance_id": queued.owner_instance_id,
            "lease_epoch": queued.lease_epoch,
            "error_code": rejection.error_code,
            "error_message": rejection.user_message,
        }
    ]
    runtime.bridge.reply_stream.assert_awaited_once_with(
        channel_id=actor.channel_id,
        generation=actor.config_generation,
        lease_epoch=actor.lease_epoch,
        provider_request_id=queued.provider_request_id,
        stream_id=queued.provider_stream_id,
        content=rejection.user_message,
        finish=True,
        non_blocking=True,
    )


@pytest.mark.asyncio
async def test_disconnect_cancels_fast_lane_and_returns_it_to_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()
    actor = _actor()
    queued = _fast_lane_queued()
    started = asyncio.Event()
    retry_calls: list[dict[str, object]] = []
    runtime.runtime_bridge.hot_route = lambda **_values: object()
    runtime.bridge.disconnect = AsyncMock()

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def mark_fast_lane_retry_wait(
            self,
            **values: object,
        ) -> bool:
            retry_calls.append(values)
            return True

    async def execute(**_values: object) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime_module,
        "execute_durable_wecom_fast_lane_v2",
        execute,
    )
    task = asyncio.create_task(
        runtime._execute_fast_lane_v2(actor=actor, queued=queued)
    )
    key = (actor.channel_id, queued.delivery_id)
    runtime._hot_turn_tasks[key] = task
    await asyncio.wait_for(started.wait(), timeout=1)

    await runtime._disconnect_actor(actor)

    assert task.cancelled() is True
    assert key not in runtime._hot_turn_tasks
    assert retry_calls == [
        {
            "inbound_message_id": queued.inbound_message_id,
            "owner_instance_id": queued.owner_instance_id,
            "lease_epoch": queued.lease_epoch,
            "error_code": "FAST_LANE_TASK_CANCELLED",
            "error_message": (
                "Fast Lane task canceled before delivery completed"
            ),
        }
    ]


@pytest.mark.asyncio
async def test_cold_application_fast_lane_prepares_current_route_and_keeps_delivered_answer_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()
    runtime.instance_id = "backend-a"
    actor = _application_actor()
    queued = QueuedFastLaneInbound(
        inbound_message_id=45,
        channel_id=11,
        delivery_id="im_delivery_00000000000000000000000000000002",
        client_message_id="im_message_00000000000000000000000000000002",
        ingress_ordering_key="direct:11:member:abc",
        ingress_ticket=1,
        provider_request_id="request-45",
        provider_stream_id="stream-45",
        external_message_id="message-45",
        external_conversation_id="member-45",
        message_type="text",
        normalized_content={"text": "你好", "chat_type": "direct"},
        quote_payload=None,
        target_type="application_agent",
        sender_platform_user_id=3,
        owner_instance_id="backend-a",
        lease_epoch=7,
    )
    prepared_route = object()
    runtime.application_runtime_bridge.prepare_fast_lane_route = AsyncMock(
        return_value=prepared_route
    )
    completed_calls: list[dict[str, object]] = []

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def mark_fast_lane_runtime_durable(
            self, **_values: object
        ) -> bool:
            return True

        async def mark_completed(self, **values: object) -> bool:
            completed_calls.append(values)
            return True

        async def demote_application_fast_lane_to_ordinary(
            self, **_values: object
        ) -> bool:
            raise AssertionError("application text must not be demoted")

    async def execute(**values: object) -> PreparedWeComFastLaneResult:
        assert values["route"] is prepared_route
        sink = values["runtime_durable_sink"]
        await sink(
            SimpleNamespace(
                disposition_seq=1,
                state="ordinary_run_accepted",
                canonical_run_id="run-45",
                parent_run_id=None,
                start_after_seq=0,
            )
        )
        return PreparedWeComFastLaneResult(
            runtime_run_id="run-45",
            provider_stream_id="stream-45",
            highest_contiguous_seq=1,
            final_text="完成",
            delivery_status="completed",
            artifacts=(
                PreparedWeComArtifact(
                    relative_path="workspace/sessions/session/report.txt",
                    mime_type="text/plain",
                    size_bytes=10,
                    mtime_ns=20,
                ),
            ),
        )

    class FailedArtifactDelivery:
        def __init__(self, **_values: object) -> None:
            pass

        async def deliver(self, **_values: object) -> None:
            raise RuntimeError("artifact follow-up failed")

    monkeypatch.setattr(runtime_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime_module,
        "execute_durable_wecom_fast_lane_v2",
        execute,
    )
    monkeypatch.setattr(
        runtime_module,
        "WeComFastLaneArtifactDelivery",
        FailedArtifactDelivery,
    )

    await runtime._execute_fast_lane_v2(actor=actor, queued=queued)

    runtime.application_runtime_bridge.prepare_fast_lane_route.assert_awaited_once_with(
        inbound_message_id=45,
        channel_id=11,
        external_conversation_id="member-45",
        platform_user_id=3,
        gateway_instance_id="backend-a",
        channel_lease_epoch=7,
        channel_generation=6,
    )
    assert completed_calls == [
        {
            "inbound_message_id": 45,
            "runtime_run_row_id": None,
            "owner_instance_id": "backend-a",
            "lease_epoch": 7,
        }
    ]


@pytest.mark.asyncio
async def test_application_fast_lane_waits_when_current_route_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()
    runtime.instance_id = "backend-a"
    actor = _application_actor()
    queued = QueuedFastLaneInbound(
        inbound_message_id=46,
        channel_id=11,
        delivery_id="im_delivery_00000000000000000000000000000003",
        client_message_id="im_message_00000000000000000000000000000003",
        ingress_ordering_key="direct:11:member:abc",
        ingress_ticket=2,
        provider_request_id="request-46",
        provider_stream_id="stream-46",
        external_message_id="message-46",
        external_conversation_id="member-45",
        message_type="text",
        normalized_content={"text": "切换模型后继续", "chat_type": "direct"},
        quote_payload=None,
        target_type="application_agent",
        sender_platform_user_id=3,
        owner_instance_id="backend-a",
        lease_epoch=7,
    )
    runtime.application_runtime_bridge.prepare_fast_lane_route = AsyncMock(
        return_value=None
    )
    retry_calls: list[dict[str, object]] = []

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def mark_fast_lane_retry_wait(
            self, **values: object
        ) -> bool:
            retry_calls.append(values)
            return True

        async def demote_application_fast_lane_to_ordinary(
            self, **_values: object
        ) -> bool:
            raise AssertionError("application text must not be demoted")

    monkeypatch.setattr(runtime_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)
    execute = AsyncMock()
    monkeypatch.setattr(
        runtime_module,
        "execute_durable_wecom_fast_lane_v2",
        execute,
    )

    await runtime._execute_fast_lane_v2(actor=actor, queued=queued)

    execute.assert_not_awaited()
    assert retry_calls == [
        {
            "inbound_message_id": 46,
            "owner_instance_id": "backend-a",
            "lease_epoch": 7,
            "error_code": "FAST_LANE_ROUTE_UNAVAILABLE",
            "error_message": (
                "current application Runtime route is not ready"
            ),
        }
    ]


@pytest.mark.asyncio
async def test_ordinary_task_failure_is_consumed_and_wakes_scheduler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = ImGatewayRuntime()
    queued = _queued()
    runtime._execute_queued = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("projection cleanup failed")
    )
    key = (queued.channel_id, "session-a")

    runtime._start_run_task(key=key, queued=queued)
    task = runtime._run_tasks[key]
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert key not in runtime._run_tasks
    assert runtime._queue_wakeup.is_set()
    assert "WeCom durable ordinary task failed" in caplog.text


@pytest.mark.asyncio
async def test_durable_application_fast_lane_rebuilds_route_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()
    runtime.instance_id = "backend-b"
    actor = replace(_application_actor(), lease_epoch=8)
    queued = QueuedFastLaneInbound(
        inbound_message_id=699,
        channel_id=11,
        delivery_id="im_delivery_00000000000000000000000000000699",
        client_message_id="im_message_00000000000000000000000000000699",
        ingress_ordering_key="direct:11:member:abc",
        ingress_ticket=32,
        provider_request_id="request-699",
        provider_stream_id="stream-699",
        external_message_id="message-699",
        external_conversation_id="member-45",
        message_type="text",
        normalized_content={"text": "切换前的问题", "chat_type": "direct"},
        quote_payload=None,
        target_type="application_agent",
        sender_platform_user_id=3,
        owner_instance_id="backend-b",
        lease_epoch=8,
        runtime_disposition_seq=1,
        runtime_canonical_run_id="run-before-model-switch",
        runtime_start_after_seq=0,
    )
    prepared_route = object()
    runtime.application_runtime_bridge.hot_route = lambda **_values: None
    runtime.application_runtime_bridge.prepare_fast_lane_recovery_route = (
        AsyncMock(return_value=prepared_route)
    )

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def demote_application_fast_lane_to_ordinary(
            self, **_values: object
        ) -> bool:
            raise AssertionError(
                "a Runtime-durable delivery must not be demoted"
            )

        async def mark_fast_lane_runtime_durable(
            self, **_values: object
        ) -> bool:
            return True

        async def mark_completed(self, **_values: object) -> bool:
            return True

    async def execute(**values: object) -> PreparedWeComFastLaneResult:
        assert values["route"] is prepared_route
        assert values["runtime_disposition_seq"] == 1
        assert (
            values["runtime_canonical_run_id"]
            == "run-before-model-switch"
        )
        return PreparedWeComFastLaneResult(
            runtime_run_id="run-before-model-switch",
            provider_stream_id="stream-699",
            highest_contiguous_seq=24,
            final_text="原 Run 已恢复",
            delivery_status="completed",
            artifacts=(),
        )

    monkeypatch.setattr(runtime_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime_module,
        "execute_durable_wecom_fast_lane_v2",
        execute,
    )

    await runtime._execute_fast_lane_v2(actor=actor, queued=queued)

    prepare = (
        runtime.application_runtime_bridge.prepare_fast_lane_recovery_route
    )
    prepare.assert_awaited_once_with(
        inbound_message_id=699,
        channel_id=11,
        external_conversation_id="member-45",
        platform_user_id=3,
        gateway_instance_id="backend-b",
        channel_lease_epoch=8,
        channel_generation=6,
        runtime_canonical_run_id="run-before-model-switch",
    )


def _ingest_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    receipt: InboundReceipt,
    submit_stream_state,
) -> ImGatewayRuntime:
    runtime = ImGatewayRuntime()

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def ingest(self, **_values: object) -> InboundReceipt:
            return receipt

    monkeypatch.setattr(
        runtime_module, "AsyncSessionLocal", SessionContext
    )
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime.bridge,
        "submit_stream_state",
        submit_stream_state,
    )
    return runtime


async def _capture_ingest_reply(
    monkeypatch: pytest.MonkeyPatch,
    *,
    receipt: InboundReceipt,
    frame: dict[str, object],
    bridge_error: bool = False,
) -> tuple[ImGatewayRuntime, list[dict[str, object]]]:
    observed: list[dict[str, object]] = []

    async def submit_stream_state(**values: object) -> None:
        observed.append(values)
        if bridge_error:
            raise WeComBridgeError(
                "BRIDGE_SUBMIT_FAILED",
                "local state submission failed",
            )

    runtime = _ingest_runtime(
        monkeypatch,
        receipt=receipt,
        submit_stream_state=submit_stream_state,
    )

    await runtime._ingest_message(_actor(), frame)
    return runtime, observed


@pytest.mark.asyncio
async def test_connecting_status_precedes_inline_authenticated_event(
    monkeypatch,
) -> None:
    runtime = ImGatewayRuntime()
    actor = _actor()
    observed: list[str] = []

    async def set_status(
        _actor: OwnedImChannel,
        status: str,
        *,
        error_code: str | None,
        error_message: str | None = None,
    ) -> None:
        del error_code, error_message
        observed.append(status)

    async def connect(**_values) -> None:
        observed.append("sdk.connect")
        await runtime._on_bridge_event(
            {
                "type": "connection.authenticated",
                "channel_id": actor.channel_id,
                "generation": actor.config_generation,
                "lease_epoch": actor.lease_epoch,
            }
        )

    monkeypatch.setattr(runtime, "_set_status", set_status)
    monkeypatch.setattr(runtime.bridge, "connect", connect)

    await runtime._connect_actor(actor)

    assert observed == ["connecting", "sdk.connect", "connected"]


@pytest.mark.asyncio
async def test_personal_enter_chat_prewarms_runtime_once_while_inflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()
    actor = _actor()
    started = asyncio.Event()
    release = asyncio.Event()
    activations: list[dict[str, object]] = []
    welcomes: list[dict[str, object]] = []

    async def activate_runtime(**values: object) -> object:
        activations.append(values)
        started.set()
        await release.wait()
        return object()

    async def reply_welcome(**values: object) -> None:
        welcomes.append(values)

    monkeypatch.setattr(
        runtime.runtime_bridge,
        "activate_runtime",
        activate_runtime,
    )
    monkeypatch.setattr(
        runtime.bridge,
        "reply_welcome",
        reply_welcome,
    )

    await runtime._handle_provider_event(actor, _enter_chat_frame())
    await asyncio.wait_for(started.wait(), timeout=1)
    first_task = runtime._personal_runtime_prewarm_tasks[
        actor.channel_id
    ]

    await runtime._handle_provider_event(actor, _enter_chat_frame())

    assert len(activations) == 1
    assert len(welcomes) == 2
    assert first_task.done() is False
    assert activations[0]["principal"] == Principal(
        org_id=actor.org_id,
        user_id=int(actor.user_id or 0),
    )
    assert (
        activations[0]["runtime_workspace_id"]
        == actor.runtime_workspace_id
    )

    release.set()
    await asyncio.wait_for(first_task, timeout=1)
    await asyncio.sleep(0)

    assert runtime._personal_runtime_prewarm_tasks == {}


@pytest.mark.asyncio
async def test_personal_enter_chat_prewarm_is_cancelled_on_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()
    actor = _actor()
    started = asyncio.Event()

    async def activate_runtime(**_values: object) -> object:
        started.set()
        await asyncio.Event().wait()
        return object()

    async def reply_welcome(**_values: object) -> None:
        return None

    async def disconnect(_channel_id: int) -> None:
        return None

    monkeypatch.setattr(
        runtime.runtime_bridge,
        "activate_runtime",
        activate_runtime,
    )
    monkeypatch.setattr(
        runtime.bridge,
        "reply_welcome",
        reply_welcome,
    )
    monkeypatch.setattr(runtime.bridge, "disconnect", disconnect)

    await runtime._handle_provider_event(actor, _enter_chat_frame())
    await asyncio.wait_for(started.wait(), timeout=1)
    task = runtime._personal_runtime_prewarm_tasks[actor.channel_id]

    await runtime._disconnect_actor(actor)

    assert task.cancelled() is True
    assert runtime._personal_runtime_prewarm_tasks == {}


@pytest.mark.parametrize(
    ("has_unfinished_predecessor", "expected_content"),
        [
            (False, "<think></think>"),
            (True, "正在结束上一轮，消息已保存。"),
        ],
)
@pytest.mark.asyncio
async def test_text_receipt_reflects_real_queue_state(
    monkeypatch,
    has_unfinished_predecessor: bool,
    expected_content: str,
) -> None:
    _runtime, observed = await _capture_ingest_reply(
        monkeypatch,
        receipt=InboundReceipt(
            inbound_message_id=10,
            duplicate=False,
            status="queued",
            stream_id="stream-10",
            has_unfinished_predecessor=(
                has_unfinished_predecessor
            ),
        ),
        frame=_message_frame(),
    )

    assert len(observed) == 1
    assert observed[0]["content"] == expected_content
    assert observed[0]["provider_request_id"] == "request-message-1"
    assert observed[0]["stream_id"] == "stream-10"


@pytest.mark.asyncio
async def test_attachment_receipt_stays_in_media_staging(
    monkeypatch,
) -> None:
    _runtime, observed = await _capture_ingest_reply(
        monkeypatch,
        receipt=InboundReceipt(
            inbound_message_id=11,
            duplicate=False,
            status="staging_media",
            stream_id="stream-11",
            has_unfinished_predecessor=False,
        ),
        frame=_message_frame(message_type="image"),
    )

    assert len(observed) == 1
    assert observed[0]["content"] == "正在接收附件…"
    assert observed[0]["provider_request_id"] == "request-message-1"
    assert observed[0]["stream_id"] == "stream-11"


@pytest.mark.asyncio
async def test_personal_ingest_waits_for_local_state_submission_before_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_submit_started = asyncio.Event()
    local_submit_release = asyncio.Event()

    async def submit_stream_state(**_values: object) -> None:
        local_submit_started.set()
        await local_submit_release.wait()

    runtime = _ingest_runtime(
        monkeypatch,
        receipt=InboundReceipt(
            inbound_message_id=10,
            duplicate=False,
            status="queued",
            stream_id="stream-10",
            has_unfinished_predecessor=False,
        ),
        submit_stream_state=submit_stream_state,
    )
    ingest_task = asyncio.create_task(
        runtime._ingest_message(_actor(), _message_frame())
    )

    await asyncio.wait_for(local_submit_started.wait(), timeout=1)
    assert runtime._queue_wakeup.is_set() is False

    local_submit_release.set()
    await asyncio.wait_for(ingest_task, timeout=1)

    assert runtime._queue_wakeup.is_set() is True


@pytest.mark.asyncio
async def test_personal_ingest_local_submit_failure_still_wakes_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, observed = await _capture_ingest_reply(
        monkeypatch,
        receipt=InboundReceipt(
            inbound_message_id=10,
            duplicate=False,
            status="queued",
            stream_id="stream-10",
            has_unfinished_predecessor=False,
        ),
        frame=_message_frame(),
        bridge_error=True,
    )

    assert len(observed) == 1
    assert runtime._queue_wakeup.is_set() is True


@pytest.mark.asyncio
async def test_application_runtime_does_not_wait_for_thinking_provider_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ImGatewayRuntime()
    actor = _application_actor()
    thinking_release = asyncio.Event()
    identity_resolved = asyncio.Event()

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Identities:
        def __init__(self, _db: object) -> None:
            pass

        async def resolve(self, **_values: object):
            identity_resolved.set()
            return SimpleNamespace(
                identity_id=71,
                principal=Principal(org_id=2, user_id=101),
            )

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def mark_identity_resolved(
            self, **_values: object
        ) -> str:
            return "queued"

    async def thinking() -> None:
        await thinking_release.wait()

    async def record_access(**_values: object) -> None:
        return None

    monkeypatch.setattr(
        runtime_module, "AsyncSessionLocal", SessionContext
    )
    monkeypatch.setattr(
        runtime_module,
        "OrganizationWeComIdentityService",
        Identities,
    )
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime,
        "_record_application_access",
        record_access,
    )
    thinking_task = asyncio.create_task(thinking())
    resolve_task = asyncio.create_task(
        runtime._resolve_application_identity(
            actor=actor,
            message=SimpleNamespace(
                sender_member_id="zhangsan",
                chat_type="group",
                external_message_id="message-1",
                external_conversation_id="group-a",
                provider_request_id="request-1",
            ),
            inbound_message_id=91,
            stream_id="stream-91",
            thinking_task=thinking_task,
        )
    )

    await asyncio.wait_for(identity_resolved.wait(), timeout=1)
    await resolve_task

    assert runtime._queue_wakeup.is_set() is True
    assert thinking_task.done() is False

    thinking_release.set()
    await thinking_task

    if runtime._maintenance_tasks:
        await asyncio.gather(
            *tuple(runtime._maintenance_tasks),
            return_exceptions=True,
        )


def _queued_execution_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    observed: list[tuple[str, object]],
    submit_stream_state,
    execute_started: asyncio.Event | None = None,
) -> ImGatewayRuntime:
    runtime = ImGatewayRuntime()

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def mark_completed(self, **values: object) -> None:
            observed.append(("completed", values))

    async def execute(value: QueuedInbound) -> tuple[int, str]:
        observed.append(("execute", value))
        if execute_started is not None:
            execute_started.set()
        return 99, "completed"

    monkeypatch.setattr(
        runtime_module, "AsyncSessionLocal", SessionContext
    )
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime.bridge,
        "submit_stream_state",
        submit_stream_state,
    )
    monkeypatch.setattr(runtime.runtime_bridge, "execute", execute)
    return runtime


async def _capture_queued_execution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    queued: QueuedInbound,
    bridge_error: bool = False,
) -> list[tuple[str, object]]:
    observed: list[tuple[str, object]] = []

    async def submit_stream_state(**values: object) -> None:
        observed.append(("submit", values))
        if bridge_error:
            raise WeComBridgeError(
                "BRIDGE_SUBMIT_FAILED",
                "local state submission failed",
            )

    runtime = _queued_execution_runtime(
        monkeypatch,
        observed=observed,
        submit_stream_state=submit_stream_state,
    )

    await runtime._execute_queued(queued)
    return observed


@pytest.mark.asyncio
async def test_new_queued_run_enters_native_thinking_before_runtime(
    monkeypatch,
) -> None:
    observed = await _capture_queued_execution(
        monkeypatch,
        queued=_queued(),
    )

    assert [kind for kind, _value in observed] == [
        "submit",
        "execute",
        "completed",
    ]
    submit = observed[0][1]
    assert isinstance(submit, dict)
    assert submit["provider_request_id"] == "request-10"
    assert submit["stream_id"] == "stream-10"
    assert submit["content"] == "<think></think>"
    assert "finish" not in submit
    assert "non_blocking" not in submit


@pytest.mark.asyncio
async def test_runtime_starts_after_local_state_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, object]] = []
    local_submit_started = asyncio.Event()
    local_submit_release = asyncio.Event()
    runtime_execute_started = asyncio.Event()

    async def submit_stream_state(**values: object) -> None:
        observed.append(("submit", values))
        local_submit_started.set()
        await local_submit_release.wait()

    runtime = _queued_execution_runtime(
        monkeypatch,
        observed=observed,
        submit_stream_state=submit_stream_state,
        execute_started=runtime_execute_started,
    )
    execution_task = asyncio.create_task(
        runtime._execute_queued(_queued())
    )

    await asyncio.wait_for(local_submit_started.wait(), timeout=1)
    assert runtime_execute_started.is_set() is False

    local_submit_release.set()
    await asyncio.wait_for(runtime_execute_started.wait(), timeout=1)
    await asyncio.wait_for(execution_task, timeout=1)

    assert [kind for kind, _value in observed] == [
        "submit",
        "execute",
        "completed",
    ]


@pytest.mark.asyncio
async def test_recovered_runtime_run_keeps_existing_stream_content(
    monkeypatch,
) -> None:
    observed = await _capture_queued_execution(
        monkeypatch,
        queued=_queued(runtime_run_row_id=42),
    )

    assert [kind for kind, _value in observed] == [
        "execute",
        "completed",
    ]


@pytest.mark.asyncio
async def test_application_conversation_busy_waits_without_spending_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, object]] = []
    runtime = ImGatewayRuntime()

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def preempted_final_text(self, **_values: object) -> None:
            return None

        async def mark_session_busy_wait(self, **values: object) -> bool:
            observed.append(("session_busy_wait", values))
            return True

        async def mark_failed(self, **_values: object) -> str:
            raise AssertionError("conversation busy must not spend a retry")

    async def execute(_queued: QueuedInbound) -> tuple[int, str]:
        raise ApplicationAgentInvocationError(
            "conversation_busy",
            status_code=409,
        )

    async def reply_stream(**values: object) -> bool:
        observed.append(("reply", values))
        return True

    monkeypatch.setattr(
        runtime_module, "AsyncSessionLocal", SessionContext
    )
    monkeypatch.setattr(runtime_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime.application_runtime_bridge,
        "execute",
        execute,
    )
    monkeypatch.setattr(runtime.bridge, "reply_stream", reply_stream)

    await runtime._execute_queued(
        replace(
            _queued(),
            target_type="application_agent",
            application_agent_id=25,
            application_client_id=31,
            sender_platform_user_id=3,
        )
    )

    assert [kind for kind, _value in observed] == [
        "session_busy_wait",
        "reply",
    ]
    assert observed[1][1]["finish"] is False
    assert runtime._queue_wakeup.is_set() is True


@pytest.mark.asyncio
async def test_thinking_local_submit_failure_does_not_block_runtime(
    monkeypatch,
) -> None:
    observed = await _capture_queued_execution(
        monkeypatch,
        queued=_queued(),
        bridge_error=True,
    )

    assert [kind for kind, _value in observed] == [
        "submit",
        "execute",
        "completed",
    ]
