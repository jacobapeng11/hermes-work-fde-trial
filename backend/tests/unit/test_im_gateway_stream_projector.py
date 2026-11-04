from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.hermes_runtime.events import PlatformStreamEvent
from app.im_gateway import stream_projector as stream_projector_module
from app.im_gateway.node_bridge import WeComBridgeError
from app.im_gateway.stream_projector import (
    _ACTIVE_MARKDOWN_LIMIT_BYTES,
    _PASSIVE_STREAM_LIMIT_BYTES,
    ProjectorIdentity,
    WeComStreamProjector,
    _semantic_chunks,
    _utf8_chunks,
)
from app.im_gateway.wecom_projection import WeComProjection


class _Bridge:
    def __init__(
        self,
        *,
        passive_error: bool = False,
        active_error: bool = False,
        passive_delivered: bool = True,
    ) -> None:
        self.passive_error = passive_error
        self.active_error = active_error
        self.passive_delivered = passive_delivered
        self.passive: list[tuple[str, bool]] = []
        self.active: list[str] = []

    async def reply_stream(self, **values) -> bool:
        if self.passive_error:
            raise WeComBridgeError(
                "CALLBACK_EXPIRED", "callback frame expired"
            )
        self.passive.append(
            (str(values["content"]), bool(values["finish"]))
        )
        return self.passive_delivered

    async def send_markdown(self, **values) -> None:
        if self.active_error:
            raise WeComBridgeError(
                "WECOM_UNAVAILABLE", "active delivery unavailable"
            )
        self.active.append(str(values["content"]))


class _LoadRows:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def scalars(self) -> _LoadRows:
        return self

    def all(self) -> list[object]:
        return self.values


class _LoadSession:
    def __init__(
        self,
        checkpoint: object,
        events: list[object] | None = None,
    ) -> None:
        self.checkpoint = checkpoint
        self.events = events or []
        self.execute_calls = 0

    async def __aenter__(self) -> _LoadSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalar(self, _query: object) -> object:
        return self.checkpoint

    async def execute(self, _query: object) -> _LoadRows:
        self.execute_calls += 1
        return _LoadRows(self.events)


def _projector(bridge: _Bridge) -> WeComStreamProjector:
    projector = WeComStreamProjector(
        bridge=bridge,  # type: ignore[arg-type]
        identity=ProjectorIdentity(
            inbound_message_id=1,
            channel_id=2,
            generation=3,
            owner_instance_id="backend-1",
            lease_epoch=4,
            provider_request_id="req",
            provider_stream_id="stream",
            chat_id="zhangsan",
        ),
        flush_seconds=0,
    )
    projector._persist = AsyncMock()  # type: ignore[method-assign]
    projector._ownership_current = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    return projector


def _checkpoint(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "last_runtime_event_seq": 0,
        "reasoning_text": None,
        "activity_text": None,
        "message_text": None,
        "final_text": None,
        "provider_delivered_text": None,
        "delivery_mode": "passive_stream",
        "status": "pending",
        "runtime_run_row_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_projects_every_public_reasoning_delta_in_order() -> None:
    bridge = _Bridge()
    projector = _projector(bridge)
    events = [
        ("chat.reasoning.delta", {"seq": 1, "delta": "思考一"}),
        (
            "chat.tool.event",
            {
                "seq": 2,
                "name": "search",
                "status": "completed",
                "preview": "找到结果",
            },
        ),
        ("chat.reasoning.delta", {"seq": 3, "delta": "思考二"}),
        (
            "chat.message.completed",
            {"seq": 4, "text": "最终结论"},
        ),
        ("chat.done", {"seq": 5, "status": "completed"}),
    ]
    for name, data in events:
        await projector.on_event(
            PlatformStreamEvent(event=name, data=data)
        )

    final = bridge.passive[-1]
    assert final[1] is True
    assert final[0] == projector.state.logical_text
    assert final[0].count("<think>") == 1
    assert final[0].count("</think>") == 1
    assert "思考一" in final[0]
    assert "思考二" in final[0]
    assert final[0].index("思考一") < final[0].index("思考二")
    assert projector.state.reasoning_text == "思考一思考二"
    assert "✓ 资料搜索完成" in final[0]
    assert final[0].endswith("最终结论")
    assert projector.state.last_seq == 5


@pytest.mark.asyncio
async def test_provider_confirmation_advances_supervisor_delivery_checkpoint() -> None:
    bridge = _Bridge()
    acknowledgements: list[int] = []

    async def acknowledge(seq: int, _confirmed_at: object) -> None:
        acknowledgements.append(seq)

    projector = WeComStreamProjector(
        bridge=bridge,  # type: ignore[arg-type]
        identity=ProjectorIdentity(
            inbound_message_id=1,
            channel_id=2,
            generation=3,
            owner_instance_id="backend-1",
            lease_epoch=4,
            provider_request_id="req",
            provider_stream_id="stream",
            chat_id="zhangsan",
        ),
        flush_seconds=0,
        delivery_ack_sink=acknowledge,
    )
    projector._persist = AsyncMock()  # type: ignore[method-assign]
    projector._ownership_current = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )

    await projector.on_event(
        PlatformStreamEvent(
            "chat.message.delta",
            {"seq": 9, "delta": "答案"},
        )
    )

    assert acknowledgements == [9]


@pytest.mark.asyncio
async def test_provider_dispatch_and_confirmation_bound_actual_await() -> None:
    dispatched = asyncio.Event()
    release = asyncio.Event()

    class BlockingBridge(_Bridge):
        async def reply_stream(self, **values) -> bool:
            dispatched.set()
            await release.wait()
            return await super().reply_stream(**values)

    projector = _projector(BlockingBridge())
    task = asyncio.create_task(
        projector.on_event(
            PlatformStreamEvent(
                event="chat.message.delta",
                data={"seq": 1, "delta": "首个输出"},
            )
        )
    )

    await asyncio.wait_for(dispatched.wait(), timeout=1)
    assert projector._first_provider_dispatch_at is not None
    assert projector._first_provider_confirmed_at is None

    release.set()
    await task

    assert projector._first_provider_confirmed_at is not None
    assert (
        projector._first_provider_dispatch_at
        <= projector._first_provider_confirmed_at
    )


@pytest.mark.asyncio
async def test_streamed_reasoning_is_preserved_when_sealed_final_is_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def begin_delivery(self, **_values: object) -> str:
            return "dispatching"

    monkeypatch.setattr(
        stream_projector_module,
        "AsyncSessionLocal",
        SessionContext,
    )
    monkeypatch.setattr(
        stream_projector_module,
        "ImInboundQueue",
        Queue,
    )
    bridge = _Bridge()
    projector = _projector(bridge)

    await projector.on_event(
        PlatformStreamEvent(
            event="chat.reasoning.delta",
            data={"seq": 1, "delta": "真实公开推理"},
        )
    )
    status = await projector.deliver_final("最终回答")

    assert status == "delivered"
    assert bridge.passive[0] == ("<think>\n真实公开推理", False)
    final_text, finish = bridge.passive[-1]
    assert finish is True
    assert "<think>\n真实公开推理\n</think>" in final_text
    assert final_text.endswith("最终回答")


@pytest.mark.asyncio
async def test_load_restores_v2_projection_without_runtime_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = WeComProjection()
    expected.apply(
        "chat.reasoning.delta", {"delta": "已保存的推理"}
    )
    expected.apply(
        "chat.message.completed", {"text": "已保存的回答"}
    )
    session = _LoadSession(
        _checkpoint(
            last_runtime_event_seq=8,
            reasoning_text=expected.reasoning_text,
            activity_text=expected.serialize_activity(),
            message_text=expected.message_text,
            final_text=expected.render_passive(),
            provider_delivered_text=expected.render_passive(),
            status="streaming",
            runtime_run_row_id=88,
        )
    )
    monkeypatch.setattr(
        stream_projector_module,
        "AsyncSessionLocal",
        lambda: session,
    )
    projector = _projector(_Bridge())

    await projector.load()

    assert session.execute_calls == 0
    assert projector.state.last_seq == 8
    assert (
        projector.state.logical_text == expected.render_passive()
    )
    assert projector.state.reasoning_text == "已保存的推理"
    assert projector.state.message_text == "已保存的回答"


@pytest.mark.asyncio
async def test_load_rebuilds_legacy_inflight_checkpoint_from_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        SimpleNamespace(
            event_name="chat.reasoning.delta",
            payload={"delta": "恢复后的精确推理"},
        ),
        SimpleNamespace(
            event_name="chat.tool.event",
            payload={
                "name": "terminal",
                "status": "completed",
                "callId": "terminal-1",
                "hasStableCallId": True,
                "result": '{"exit_code":0,"output":"private"}',
            },
        ),
        SimpleNamespace(
            event_name="chat.message.completed",
            payload={"text": "恢复后的最终回答"},
        ),
    ]
    session = _LoadSession(
        _checkpoint(
            last_runtime_event_seq=3,
            reasoning_text="旧推理",
            activity_text="工具 · terminal：completed\n{raw json}",
            message_text="旧草稿",
            final_text="旧的不美观输出",
            provider_delivered_text="旧的不美观输出",
            delivery_mode="active_markdown",
            status="delivery_failed",
            runtime_run_row_id=99,
        ),
        events,
    )
    monkeypatch.setattr(
        stream_projector_module,
        "AsyncSessionLocal",
        lambda: session,
    )
    projector = _projector(_Bridge())

    await projector.load()

    assert session.execute_calls == 1
    assert projector.state.delivery_mode == "active_buffered"
    assert projector.state.reasoning_text == "恢复后的精确推理"
    assert projector.state.message_text == "恢复后的最终回答"
    assert "✓ 检查完成" in projector.state.logical_text
    assert "raw json" not in projector.state.logical_text
    assert "private" not in projector.state.logical_text


@pytest.mark.asyncio
async def test_reasoning_replaces_native_placeholder_and_closes_card() -> None:
    bridge = _Bridge()
    projector = _projector(bridge)

    await projector.on_event(
        PlatformStreamEvent(
            event="chat.reasoning.delta",
            data={"seq": 1, "delta": "真实公开推理"},
        )
    )

    assert bridge.passive[-1] == (
        "<think>\n真实公开推理",
        False,
    )
    assert "<think></think>" not in bridge.passive[-1][0]

    await projector.on_event(
        PlatformStreamEvent(
            event="chat.message.delta",
            data={"seq": 2, "delta": "最终回答"},
        )
    )

    assert bridge.passive[-1] == (
        "<think>\n真实公开推理\n</think>\n\n最终回答",
        False,
    )

    await projector.on_event(
        PlatformStreamEvent(
            event="chat.done",
            data={"seq": 3, "status": "completed"},
        )
    )

    assert bridge.passive[-1] == (
        "<think>\n真实公开推理\n</think>\n\n最终回答",
        True,
    )


@pytest.mark.asyncio
async def test_agent_activity_deltas_form_one_message_without_blank_lines_or_duplication() -> None:
    bridge = _Bridge()
    projector = _projector(bridge)
    final_text = (
        '这个问题有点难倒我了——作为一个 AI 助理，我没有"出生日期"。'
        "\n\n有什么工作需要我帮忙吗？"
    )
    events = [
        (
            "chat.reasoning.delta",
            {"seq": 1, "delta": "公开推理"},
        ),
        (
            "chat.activity.delta",
            {"seq": 2, "delta": "这个问题", "source": "agent"},
        ),
        (
            "chat.activity.delta",
            {"seq": 3, "delta": "有点难倒我了——", "source": "agent"},
        ),
        (
            "chat.activity.delta",
            {
                "seq": 4,
                "delta": '作为一个 AI 助理，我没有"出生日期"。',
                "source": "agent",
            },
        ),
        (
            "chat.activity.delta",
            {"seq": 5, "delta": "\n\n", "source": "agent"},
        ),
        (
            "chat.activity.delta",
            {
                "seq": 6,
                "delta": "有什么工作需要我帮忙吗？",
                "source": "agent",
            },
        ),
        (
            "chat.message.completed",
            {"seq": 7, "text": final_text},
        ),
        ("chat.done", {"seq": 8, "status": "completed"}),
    ]

    for name, data in events:
        await projector.on_event(
            PlatformStreamEvent(event=name, data=data)
        )

    delivered = bridge.passive[-1][0]
    assert delivered == (
        "<think>\n公开推理\n</think>\n\n" + final_text
    )
    assert "> 这个问题" not in delivered
    assert delivered.count(final_text) == 1
    assert projector.state.activity_text.startswith(
        "wecom_projection_v2:"
    )
    assert projector.state.message_text == final_text


@pytest.mark.asyncio
async def test_completed_answer_replaces_a_divergent_agent_draft() -> None:
    bridge = _Bridge()
    projector = _projector(bridge)

    await projector.on_event(
        PlatformStreamEvent(
            event="chat.activity.delta",
            data={
                "seq": 1,
                "delta": "尚未校准的草稿",
                "source": "agent",
            },
        )
    )
    await projector.on_event(
        PlatformStreamEvent(
            event="chat.message.completed",
            data={"seq": 2, "text": "权威最终回答"},
        )
    )
    await projector.on_event(
        PlatformStreamEvent(
            event="chat.done",
            data={"seq": 3, "status": "completed"},
        )
    )

    delivered = bridge.passive[-1][0]
    assert delivered == "权威最终回答"
    assert "尚未校准的草稿" not in delivered
    assert "---" not in delivered


@pytest.mark.asyncio
async def test_completed_event_recovers_reasoning_when_realtime_was_missed() -> None:
    bridge = _Bridge()
    projector = _projector(bridge)

    await projector.on_event(
        PlatformStreamEvent(
            event="chat.message.completed",
            data={
                "seq": 26,
                "text": "最终回答",
                "reasoning": "真实思考",
            },
        )
    )
    await projector.on_event(
        PlatformStreamEvent(
            event="chat.done",
            data={"seq": 27, "status": "completed"},
        )
    )

    assert bridge.passive[-1] == (
        "<think>\n真实思考\n</think>\n\n最终回答",
        True,
    )
    assert projector.state.reasoning_text == "真实思考"


@pytest.mark.asyncio
async def test_expired_callback_buffers_then_sends_one_clean_active_snapshot() -> None:
    bridge = _Bridge(passive_error=True)
    projector = _projector(bridge)
    await projector.on_event(
        PlatformStreamEvent(
            event="chat.reasoning.delta",
            data={"seq": 1, "delta": "公开推理"},
        )
    )
    assert bridge.active == []
    await projector.on_event(
        PlatformStreamEvent(
            event="chat.message.completed",
            data={"seq": 2, "text": "回答"},
        )
    )
    assert bridge.active == []
    await projector.on_event(
        PlatformStreamEvent(
            event="chat.done",
            data={"seq": 3, "status": "completed"},
        )
    )

    assert projector.state.delivery_mode == "active_markdown"
    assert "".join(bridge.active) == (
        "**思考过程**\n公开推理\n\n回答"
    )
    assert "<think>" not in "".join(bridge.active)
    assert "</think>" not in "".join(bridge.active)
    assert "".join(bridge.active).count("回答") == 1
    assert projector.state.delivered_text == projector.state.logical_text
    assert projector.state.status == "completed"


def test_utf8_chunking_preserves_every_multibyte_character() -> None:
    value = "推理🙂" * 3_000
    chunks = _utf8_chunks(value, 3_500)
    assert "".join(chunks) == value
    assert all(len(chunk.encode("utf-8")) <= 3_500 for chunk in chunks)


def test_semantic_chunking_prefers_boundaries_without_losing_text() -> None:
    value = (
        ("第一段内容。" * 300)
        + "\n\n"
        + ("第二段内容🙂" * 300)
    )
    chunks = _semantic_chunks(value, 800)

    assert "".join(chunks) == value
    assert all(len(chunk.encode("utf-8")) <= 800 for chunk in chunks)
    assert any(chunk.endswith(("。", "\n")) for chunk in chunks[:-1])


@pytest.mark.asyncio
async def test_oversized_reasoning_finishes_valid_passive_fold_and_continues_active() -> None:
    bridge = _Bridge()
    projector = _projector(bridge)
    reasoning = "完整公开推理🙂。" * 3_000

    await projector.on_event(
        PlatformStreamEvent(
            event="chat.reasoning.delta",
            data={"seq": 1, "delta": reasoning},
        )
    )
    await projector.on_event(
        PlatformStreamEvent(
            event="chat.message.completed",
            data={"seq": 2, "text": "最终回答"},
        )
    )

    # Nothing oversized is sent through the non-terminal callback.
    assert bridge.passive == []
    assert bridge.active == []

    await projector.on_event(
        PlatformStreamEvent(
            event="chat.done",
            data={"seq": 3, "status": "completed"},
        )
    )

    passive, finish = bridge.passive[-1]
    active = "".join(bridge.active)
    assert finish is True
    assert len(passive.encode("utf-8")) <= (
        _PASSIVE_STREAM_LIMIT_BYTES
    )
    assert passive.count("<think>") == 1
    assert passive.count("</think>") == 1
    assert passive.endswith("回答较长，完整内容将继续发送。")
    assert active.startswith("**思考过程（续）**\n")
    assert "<think>" not in active
    assert "</think>" not in active
    assert all(
        len(chunk.encode("utf-8"))
        <= _ACTIVE_MARKDOWN_LIMIT_BYTES
        for chunk in bridge.active
    )

    passive_reasoning = passive.removeprefix("<think>\n").split(
        "\n</think>", 1
    )[0]
    active_reasoning = active.removeprefix(
        "**思考过程（续）**\n"
    ).rsplit("\n\n最终回答", 1)[0]
    assert passive_reasoning + active_reasoning == reasoning
    assert active.count("最终回答") == 1
    assert projector.state.status == "completed"


@pytest.mark.asyncio
async def test_throttled_projection_persists_text_and_matching_cursor() -> None:
    projector = _projector(_Bridge())
    projector.flush_seconds = 60
    projector._last_flush = time.monotonic()

    await projector.on_event(
        PlatformStreamEvent(
            event="chat.reasoning.delta",
            data={"seq": 11, "delta": "只追加一次"},
        )
    )

    assert projector.state.last_seq == 11
    assert projector.state.reasoning_text == "只追加一次"
    projector._persist.assert_awaited_once_with(last_seq=11)


@pytest.mark.asyncio
async def test_terminal_delivery_failure_remains_retryable() -> None:
    projector = _projector(
        _Bridge(passive_error=True, active_error=True)
    )

    await projector.on_event(
        PlatformStreamEvent(
            event="chat.done",
            data={"seq": 9, "status": "completed"},
        )
    )

    assert projector.state.last_seq == 9
    assert projector.state.status == "delivery_failed"
    assert projector.state.delivery_mode == "active_buffered"


@pytest.mark.asyncio
async def test_old_owner_cannot_send_after_lease_takeover() -> None:
    bridge = _Bridge()
    projector = _projector(bridge)
    projector._ownership_current = AsyncMock(  # type: ignore[method-assign]
        return_value=False
    )

    await projector.on_event(
        PlatformStreamEvent(
            event="chat.done",
            data={"seq": 10, "status": "completed"},
        )
    )

    assert bridge.passive == []
    assert bridge.active == []
    assert projector.state.status == "delivery_failed"
