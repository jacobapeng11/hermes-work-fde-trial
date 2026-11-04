from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from platform_supervisor.hermes.dashboard_hot_channel import (
    ActiveRunControlPeerRegistry,
    DashboardChannelBusy,
    DashboardHotCapacityUnavailable,
    DashboardHotChannelIdentity,
    DashboardHotChannelManager,
    DashboardPreparedPeer,
    DashboardRedirectDisposition,
    DashboardRedirectUncertain,
    DashboardRpcPeer,
    HotChannelState,
)
from platform_supervisor.hermes.protocol import HermesProtocolError


class FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False
        self.recv_active = 0
        self.recv_concurrency_max = 0

    async def send(self, value: str) -> None:
        self.sent.append(json.loads(value))

    async def recv(self) -> str:
        self.recv_active += 1
        self.recv_concurrency_max = max(self.recv_concurrency_max, self.recv_active)
        try:
            value = await self.incoming.get()
        finally:
            self.recv_active -= 1
        if isinstance(value, BaseException):
            raise value
        return value

    async def push(self, frame: dict) -> None:
        await self.incoming.put(json.dumps(frame))

    async def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self) -> None:
        self.connect_count = 0
        self.close_count = 0
        self.websockets: list[FakeWebSocket] = []

    @asynccontextmanager
    async def __call__(self, _endpoint):
        self.connect_count += 1
        websocket = FakeWebSocket()
        self.websockets.append(websocket)
        try:
            yield websocket
        finally:
            self.close_count += 1
            await websocket.close()


class FakeBoundaryHandle:
    def __init__(
        self,
        *,
        run_id: str,
        stored_session_id: str,
        connection_id: str,
        live_session_id: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.stored_session_id = stored_session_id
        self.live_session_id = live_session_id or stored_session_id
        self.connection_id = connection_id
        self.claim_count = 0

    def claim_dashboard_redirect(
        self,
        *,
        runtime_run_id: str,
        stored_session_id: str,
        live_session_id: str,
        dashboard_connection_id: str,
    ) -> bool:
        if self.claim_count:
            return False
        if (
            runtime_run_id != self.run_id
            or stored_session_id != self.stored_session_id
            or live_session_id != self.live_session_id
            or dashboard_connection_id != self.connection_id
        ):
            return False
        self.claim_count += 1
        return True


def identity(
    *,
    config_generation: int = 3,
    model_entry_id: int = 7,
    runtime_session_db_id: int = 4,
    runtime_session_id: str = "rs-3-1",
    independent_session: bool = False,
    reuse_across_turns: bool = False,
    max_coordination_slots: int = 1,
) -> DashboardHotChannelIdentity:
    return DashboardHotChannelIdentity(
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        runtime_session_db_id=runtime_session_db_id,
        runtime_session_id=runtime_session_id,
        runtime_id="rt-3",
        gateway_model_id=f"model-{model_entry_id}",
        config_generation=config_generation,
        endpoint_digest="endpoint",
        credential_digest="credential",
        workspace_path_digest="workspace",
        effective_model_entry_id=model_entry_id,
        model_policy_digest="model-policy",
        coordination_scope_key=(
            "application:rt-3:3"
            if independent_session
            else None
        ),
        independent_session=independent_session,
        reuse_across_turns=reuse_across_turns,
        max_coordination_slots=max_coordination_slots,
    )


@pytest.mark.asyncio
async def test_reusable_terminal_keeps_exact_hot_channel_for_next_turn() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    current = identity(reuse_across_turns=True)
    prepare_count = 0

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        nonlocal prepare_count
        prepare_count += 1
        return "rs-3-1", "/data/workspace/rs-3-1"

    await manager.prewarm(current, object(), connector, prepare)
    first = await manager.acquire_run(
        current,
        object(),
        connector,
        prepare,
        runtime_run_id="rr-1",
    )
    first_peer = first.peer
    await first.release(terminal=True)

    second = await manager.acquire_run(
        current,
        object(),
        connector,
        prepare,
        runtime_run_id="rr-2",
    )

    assert second.peer is first_peer
    assert connector.connect_count == 1
    assert connector.close_count == 0
    assert prepare_count == 1
    assert manager.result_snapshot()["run_reused"] == 1

    await manager.invalidate_session(1, 2, 3, "rs-3-1", reason="test_cleanup")
    await second.release(terminal=True)
    await manager.close()


@pytest.mark.asyncio
async def test_invalidate_session_matches_explicit_coordination_scope() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    current = identity(independent_session=True)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "rs-3-1", "/data/workspace/rs-3-1"

    assert await manager.prewarm(current, object(), connector, prepare) is True
    assert manager.slot_count == 1

    await manager.invalidate_session(
        1,
        2,
        3,
        current.runtime_session_id,
        reason="session_evicted",
    )

    assert connector.close_count == 1
    assert manager.slot_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_invalidate_workspace_matches_explicit_coordination_scope() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    current = identity(independent_session=True)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "rs-3-1", "/data/workspace/rs-3-1"

    assert await manager.prewarm(current, object(), connector, prepare) is True

    await manager.invalidate_workspace(1, 2, 3, reason="workspace_reloaded")

    assert connector.close_count == 1
    assert manager.slot_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_rpc_peer_has_one_reader_and_routes_rpc_and_notifications() -> None:
    websocket = FakeWebSocket()
    peer = DashboardRpcPeer(websocket)
    await peer.start()

    request = asyncio.create_task(peer.request("session.resume", {"session_id": "rs-3-1"}))
    await asyncio.sleep(0)
    request_id = websocket.sent[0]["id"]
    await websocket.push(
        {"jsonrpc": "2.0", "method": "event", "params": {"type": "message.delta"}}
    )
    await websocket.push(
        {"jsonrpc": "2.0", "id": request_id, "result": {"session_id": "rs-3-1"}}
    )

    assert await request == {"session_id": "rs-3-1"}
    assert (await peer.next_notification())["method"] == "event"
    assert websocket.recv_concurrency_max == 1
    await peer.close()


@pytest.mark.asyncio
async def test_rpc_peer_preserves_structured_rpc_error() -> None:
    websocket = FakeWebSocket()
    peer = DashboardRpcPeer(websocket)
    request = asyncio.create_task(
        peer.request("session.resume", {"session_id": "rs-3-1"})
    )
    await asyncio.sleep(0)
    await websocket.push(
        {
            "jsonrpc": "2.0",
            "id": websocket.sent[0]["id"],
            "error": {
                "code": 5070,
                "message": "session capacity reached",
                "data": {"limit": 1},
            },
        }
    )

    with pytest.raises(HermesProtocolError) as captured:
        await request

    assert captured.value.rpc_code == 5070
    assert captured.value.rpc_method == "session.resume"
    assert captured.value.rpc_data == {"limit": 1}
    await peer.close()


@pytest.mark.asyncio
async def test_rpc_peer_notification_waiter_does_not_consume_stream_event() -> None:
    websocket = FakeWebSocket()
    peer = DashboardRpcPeer(websocket)
    await peer.start()
    frame = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "session.info",
            "session_id": "live",
            "payload": {"model": "7"},
        },
    }

    await websocket.push(frame)
    await wait_until(lambda: bool(peer._recent_notifications))

    observed = await peer.wait_for_notification(
        lambda candidate: candidate.get("params", {}).get("type") == "session.info",
        timeout_seconds=1,
    )

    assert observed == frame
    assert await peer.next_notification(timeout_seconds=1) == frame
    await peer.close()


@pytest.mark.asyncio
async def test_rpc_peer_marks_remote_reader_failure_closed() -> None:
    websocket = FakeWebSocket()
    peer = DashboardRpcPeer(websocket)
    await peer.start()

    await websocket.incoming.put(ConnectionError("remote closed"))
    await wait_until(lambda: peer.closed)

    assert peer.closed is True
    await peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        DashboardRedirectDisposition.REDIRECTED,
        DashboardRedirectDisposition.REJECTED,
        DashboardRedirectDisposition.QUEUED,
    ],
)
async def test_active_run_control_peer_calls_session_redirect_on_single_reader(
    status: DashboardRedirectDisposition,
) -> None:
    websocket = FakeWebSocket()
    peer = DashboardRpcPeer(websocket)
    registry = ActiveRunControlPeerRegistry()
    lease = await registry.register(
        runtime_run_id="run-1",
        stored_session_id="stored-1",
        peer=peer,
    )
    await lease.bind_live_session("stored-1")
    await lease.observe_message_start()
    boundary = FakeBoundaryHandle(
        run_id="run-1",
        stored_session_id="stored-1",
        connection_id=lease.dashboard_connection_id,
    )

    redirect = asyncio.create_task(
        registry.redirect(
            runtime_run_id="run-1",
            wire_text="apply this correction",
            boundary_handle=boundary,
            timeout_seconds=1,
        )
    )
    await asyncio.sleep(0)
    request = websocket.sent[0]
    await websocket.push(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"status": status.value},
        }
    )

    result = await redirect
    assert result.disposition is status
    assert result.accepted is (status is DashboardRedirectDisposition.REDIRECTED)
    assert result.uncertain is (status is DashboardRedirectDisposition.QUEUED)
    assert request["method"] == "session.redirect"
    assert request["params"] == {
        "session_id": "stored-1",
        "text": "apply this correction",
    }
    assert websocket.recv_concurrency_max == 1
    await lease.close()
    await peer.close()


@pytest.mark.asyncio
async def test_active_run_control_peer_steers_only_while_tool_is_active() -> None:
    websocket = FakeWebSocket()
    peer = DashboardRpcPeer(websocket)
    registry = ActiveRunControlPeerRegistry()
    lease = await registry.register(
        runtime_run_id="run-steer",
        stored_session_id="stored-steer",
        peer=peer,
    )
    await lease.bind_live_session("live-steer")
    await lease.observe_message_start()
    await lease.observe_tool_event("tool.start", {"tool_call_id": "call-1"})
    assert await registry.is_tool_active("run-steer") is True

    steering = asyncio.create_task(
        registry.steer(
            runtime_run_id="run-steer",
            wire_text="also check the logs",
            timeout_seconds=1,
        )
    )
    await wait_until(lambda: len(websocket.sent) == 1)
    request = websocket.sent[0]
    await websocket.push(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"status": "queued"},
        }
    )

    assert await steering == {"status": "queued"}
    assert request["method"] == "session.steer"
    assert request["params"] == {
        "session_id": "live-steer",
        "text": "also check the logs",
    }
    await lease.observe_tool_event("tool.complete", {"tool_call_id": "call-1"})
    assert await registry.is_tool_active("run-steer") is False
    await lease.close()
    await peer.close()


@pytest.mark.asyncio
async def test_active_run_control_peer_interrupts_once_then_proves_settle() -> None:
    websocket = FakeWebSocket()
    peer = DashboardRpcPeer(websocket)
    registry = ActiveRunControlPeerRegistry()
    lease = await registry.register(
        runtime_run_id="run-settle",
        stored_session_id="stored-settle",
        peer=peer,
    )
    await lease.bind_live_session("live-settle")

    settling = asyncio.create_task(
        registry.interrupt_and_settle_redirect(
            runtime_run_id="run-settle",
            stored_session_id="stored-settle",
            timeout_seconds=1,
        )
    )
    await wait_until(lambda: len(websocket.sent) == 1)
    await websocket.push(
        {
            "jsonrpc": "2.0",
            "id": websocket.sent[0]["id"],
            "result": {"status": "interrupted"},
        }
    )
    await wait_until(lambda: len(websocket.sent) == 2)
    await websocket.push(
        {
            "jsonrpc": "2.0",
            "id": websocket.sent[1]["id"],
            "result": {
                "running": True,
                "status": "streaming",
                "inflight": {"streaming": True},
            },
        }
    )
    await asyncio.sleep(0.11)
    await wait_until(lambda: len(websocket.sent) == 3)
    await websocket.push(
        {
            "jsonrpc": "2.0",
            "id": websocket.sent[2]["id"],
            "result": {
                "running": False,
                "status": "idle",
                "inflight": None,
                "queued": [],
            },
        }
    )

    result = await settling
    assert result.interrupt_sent is True
    assert result.checkpoint["status"] == "idle"
    assert [request["method"] for request in websocket.sent] == [
        "session.interrupt",
        "session.resume",
        "session.resume",
    ]
    assert websocket.sent[0]["params"] == {"session_id": "live-settle"}
    assert websocket.recv_concurrency_max == 1
    await lease.close()
    await peer.close()


def test_redirect_settle_checkpoint_requires_explicit_running_false() -> None:
    assert (
        ActiveRunControlPeerRegistry.redirect_checkpoint_is_settled(
            {"status": "idle", "inflight": None, "queued": []}
        )
        is False
    )


@pytest.mark.asyncio
async def test_active_run_control_peer_registration_wakes_boundary_joiner() -> None:
    registry = ActiveRunControlPeerRegistry()
    waiting = asyncio.create_task(registry.wait_for_connection("run-1"))
    await asyncio.sleep(0)
    assert waiting.done() is False

    websocket = FakeWebSocket()
    peer = DashboardRpcPeer(websocket)
    lease = await registry.register(
        runtime_run_id="run-1",
        stored_session_id="stored-1",
        peer=peer,
    )

    assert await waiting == lease.dashboard_connection_id
    await lease.close()
    await peer.close()


@pytest.mark.asyncio
async def test_active_run_control_peer_requires_ready_valid_one_shot_boundary() -> None:
    websocket = FakeWebSocket()
    peer = DashboardRpcPeer(websocket)
    registry = ActiveRunControlPeerRegistry()
    lease = await registry.register(
        runtime_run_id="run-1",
        stored_session_id="stored-1",
        peer=peer,
    )
    boundary = FakeBoundaryHandle(
        run_id="run-1",
        stored_session_id="stored-1",
        connection_id=lease.dashboard_connection_id,
    )

    with pytest.raises(HermesProtocolError, match="not agent-ready"):
        await registry.redirect(
            runtime_run_id="run-1",
            wire_text="correction",
            boundary_handle=boundary,
        )
    assert websocket.sent == []
    assert boundary.claim_count == 0

    await lease.bind_live_session("stored-1")
    await lease.observe_message_start()
    wrong = FakeBoundaryHandle(
        run_id="run-other",
        stored_session_id="stored-1",
        connection_id=lease.dashboard_connection_id,
    )
    with pytest.raises(HermesProtocolError, match="boundary handle"):
        await registry.redirect(
            runtime_run_id="run-1",
            wire_text="correction",
            boundary_handle=wrong,
        )
    assert websocket.sent == []
    await lease.close()
    await peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["steered", "submitted", ""])
async def test_active_run_control_peer_maps_unknown_redirect_status_uncertain(
    status: str,
) -> None:
    websocket = FakeWebSocket()
    peer = DashboardRpcPeer(websocket)
    registry = ActiveRunControlPeerRegistry()
    lease = await registry.register(
        runtime_run_id="run-1",
        stored_session_id="stored-1",
        peer=peer,
    )
    await lease.bind_live_session("stored-1")
    await lease.observe_message_start()
    boundary = FakeBoundaryHandle(
        run_id="run-1",
        stored_session_id="stored-1",
        connection_id=lease.dashboard_connection_id,
    )
    redirect = asyncio.create_task(
        registry.redirect(
            runtime_run_id="run-1",
            wire_text="correction",
            boundary_handle=boundary,
            timeout_seconds=1,
        )
    )
    await asyncio.sleep(0)
    await websocket.push(
        {
            "jsonrpc": "2.0",
            "id": websocket.sent[0]["id"],
            "result": {"status": status},
        }
    )

    with pytest.raises(DashboardRedirectUncertain, match="unsupported"):
        await redirect
    assert len(websocket.sent) == 1
    with pytest.raises(HermesProtocolError, match="boundary handle"):
        await registry.redirect(
            runtime_run_id="run-1",
            wire_text="correction",
            boundary_handle=boundary,
        )
    assert len(websocket.sent) == 1
    await lease.close()
    await peer.close()


@pytest.mark.asyncio
async def test_active_run_control_peer_preserves_redirect_rpc_error_as_uncertain() -> None:
    websocket = FakeWebSocket()
    peer = DashboardRpcPeer(websocket)
    registry = ActiveRunControlPeerRegistry()
    lease = await registry.register(
        runtime_run_id="run-1",
        stored_session_id="stored-1",
        peer=peer,
    )
    await lease.bind_live_session("stored-1")
    await lease.observe_message_start()
    boundary = FakeBoundaryHandle(
        run_id="run-1",
        stored_session_id="stored-1",
        connection_id=lease.dashboard_connection_id,
    )
    redirect = asyncio.create_task(
        registry.redirect(
            runtime_run_id="run-1",
            wire_text="correction",
            boundary_handle=boundary,
            timeout_seconds=1,
        )
    )
    await asyncio.sleep(0)
    await websocket.push(
        {
            "jsonrpc": "2.0",
            "id": websocket.sent[0]["id"],
            "error": {
                "code": 5091,
                "message": "redirect unavailable",
                "data": {"state": "building"},
            },
        }
    )

    with pytest.raises(DashboardRedirectUncertain) as captured:
        await redirect
    assert captured.value.rpc_code == 5091
    assert captured.value.rpc_method == "session.redirect"
    assert captured.value.rpc_data == {"state": "building"}
    await lease.close()
    await peer.close()


@pytest.mark.asyncio
async def test_active_run_control_peer_exact_unregister_cannot_remove_replacement() -> None:
    first_peer = DashboardRpcPeer(FakeWebSocket())
    second_peer = DashboardRpcPeer(FakeWebSocket())
    registry = ActiveRunControlPeerRegistry()
    first = await registry.register(
        runtime_run_id="run-1",
        stored_session_id="stored-1",
        peer=first_peer,
    )
    await first.close()
    second = await registry.register(
        runtime_run_id="run-1",
        stored_session_id="stored-1",
        peer=second_peer,
    )

    await first.close()
    assert await registry.current_connection_id("run-1") == (
        second.dashboard_connection_id
    )
    await second.close()
    assert await registry.current_connection_id("run-1") is None
    await first_peer.close()
    await second_peer.close()


@pytest.mark.asyncio
async def test_cold_run_blocks_workspace_reload_until_run_finishes() -> None:
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async with manager.cold_run(3):
        with pytest.raises(DashboardChannelBusy, match="active run"):
            await manager.require_workspace_idle(3)

    await manager.require_workspace_idle(3)
    await manager.close()


@pytest.mark.asyncio
async def test_workspace_operation_blocks_new_cold_run_registration() -> None:
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    entered = asyncio.Event()

    async def run() -> None:
        async with manager.cold_run(3):
            entered.set()

    lock = manager.workspace_operation(3)
    await lock.acquire()
    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    assert entered.is_set() is False

    lock.release()
    await task
    assert entered.is_set() is True
    await manager.close()


@pytest.mark.asyncio
async def test_prewarm_and_acquire_share_one_connection_and_one_prepare() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()
    prepare_count = 0

    async def prepare(peer: DashboardRpcPeer) -> tuple[str, str | None]:
        nonlocal prepare_count
        prepare_count += 1
        prepare_started.set()
        await release_prepare.wait()
        return "rs-3-1", "/data/workspace/rs-3-1"

    warming = asyncio.create_task(manager.prewarm(identity(), object(), connector, prepare))
    await prepare_started.wait()
    acquiring = asyncio.create_task(
        manager.acquire_run(identity(), object(), connector, prepare, runtime_run_id="rr-1")
    )
    release_prepare.set()

    await warming
    lease = await acquiring
    assert connector.connect_count == 1
    assert prepare_count == 1
    assert lease.state is HotChannelState.IN_USE
    await manager.invalidate_session(1, 2, 3, "rs-3-1", reason="test_cleanup")
    await lease.release(terminal=True)
    assert manager.ready_count == 0
    assert manager.slot_count == 0
    assert connector.close_count == 1
    await manager.close()


@pytest.mark.asyncio
async def test_matching_session_reclaimed_invalidates_hot_channel_identity() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return f"live-{connector.connect_count}", "/data/workspace/session"

    current = identity()
    await manager.prewarm(current, object(), connector, prepare)
    assert manager.ready_count == 1
    await connector.websockets[0].push(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "session.reclaimed",
                "session_id": "",
                "payload": {
                    "session_id": "live-1",
                    "stored_session_id": current.runtime_session_id,
                    "reason": "idle_timeout",
                },
            },
        }
    )
    await wait_until(lambda: manager.slot_count == 0)

    lease = await manager.acquire_run(
        current,
        object(),
        connector,
        prepare,
        runtime_run_id="rr-after-reclaim",
    )

    assert connector.connect_count == 2
    assert connector.close_count == 1
    assert lease.live_session_id == "live-2"
    await manager.invalidate_session(1, 2, 3, "rs-3-1", reason="test_cleanup")
    await lease.release(terminal=True)
    await manager.close()


@pytest.mark.asyncio
async def test_nonmatching_session_reclaimed_keeps_hot_channel() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "live-current", "/data/workspace/session"

    current = identity()
    await manager.prewarm(current, object(), connector, prepare)
    await connector.websockets[0].push(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "session.reclaimed",
                "session_id": "",
                "payload": {
                    "session_id": "live-other",
                    "stored_session_id": current.runtime_session_id,
                    "reason": "idle_timeout",
                },
            },
        }
    )
    await wait_until(lambda: bool(connector.websockets[0].incoming.empty()))
    await asyncio.sleep(0)

    assert manager.ready_count == 1
    assert manager.slot_count == 1
    assert connector.close_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_reclaimed_in_use_slot_is_not_replaced_on_terminal_release() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "live-in-use", "/data/workspace/session"

    current = identity()
    lease = await manager.acquire_run(
        current,
        object(),
        connector,
        prepare,
        runtime_run_id="rr-reclaimed",
    )
    await connector.websockets[0].push(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "session.reclaimed",
                "session_id": "",
                "payload": {
                    "session_id": "live-in-use",
                    "stored_session_id": current.runtime_session_id,
                    "reason": "lru_evict",
                },
            },
        }
    )
    await wait_until(lambda: manager.slot_count == 0)
    await lease.release(terminal=True)
    await asyncio.sleep(0)

    assert connector.connect_count == 1
    assert manager.inflight_count == 0
    assert manager.ready_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_warming_channel_is_not_acquired_until_prewarm_is_ready() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    agent_ready = asyncio.Event()
    outcomes: list[str] = []

    async def prepare(_peer: DashboardRpcPeer) -> DashboardPreparedPeer:
        return DashboardPreparedPeer(
            live_session_id="live",
            workspace="/data/workspace/session",
            ready=False,
        )

    async def wait_ready(_peer: DashboardRpcPeer, _live_session_id: str) -> None:
        await agent_ready.wait()

    warming = asyncio.create_task(
        manager.prewarm(
            identity(),
            object(),
            connector,
            prepare,
            wait_ready=wait_ready,
        )
    )
    await wait_until(lambda: manager.warming_count == 1)

    acquiring = asyncio.create_task(
        manager.acquire_run(
            identity(),
            object(),
            connector,
            prepare,
            wait_ready=wait_ready,
            runtime_run_id="rr-warming",
            on_result=outcomes.append,
        )
    )

    await wait_until(lambda: outcomes == ["hit"])
    assert acquiring.done() is False
    assert manager.warming_count == 1
    assert warming.done() is False
    assert connector.connect_count == 1
    assert outcomes == ["hit"]

    agent_ready.set()
    await warming
    lease = await acquiring
    assert lease.state is HotChannelState.IN_USE
    await manager.invalidate_session(1, 2, 3, "rs-3-1", reason="test_cleanup")
    await lease.release(terminal=True)
    await manager.close()


@pytest.mark.asyncio
async def test_warming_failure_never_publishes_ready_slot() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> DashboardPreparedPeer:
        return DashboardPreparedPeer("live", None, ready=False)

    async def wait_ready(_peer: DashboardRpcPeer, _live_session_id: str) -> None:
        raise HermesProtocolError("agent init failed")

    with pytest.raises(HermesProtocolError, match="agent init failed"):
        await manager.prewarm(
            identity(),
            object(),
            connector,
            prepare,
            wait_ready=wait_ready,
        )

    assert manager.ready_count == 0
    assert manager.warming_count == 0
    assert manager.slot_count == 0
    assert connector.close_count == 1
    await manager.close()


async def wait_until(predicate, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


@pytest.mark.asyncio
async def test_selecting_another_session_closes_old_ready_channel() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "live", "/data/workspace/session"

    first = identity()
    second = identity(runtime_session_db_id=5, runtime_session_id="rs-3-2")
    await manager.prewarm(first, object(), connector, prepare)
    await manager.select_session(1, 2, 3, second.runtime_session_id)

    assert manager.slot_count == 0
    assert connector.close_count == 1

    await manager.prewarm(second, object(), connector, prepare)
    assert manager.ready_count == 1
    assert connector.connect_count == 2
    await manager.close()


@pytest.mark.asyncio
async def test_selected_only_prewarm_never_reselects_superseded_session() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "live", "/data/workspace/session"

    first = identity()
    second = identity(runtime_session_db_id=5, runtime_session_id="rs-3-2")
    first_epoch = await manager.select_session(
        1, 2, 3, first.runtime_session_id
    )
    second_epoch = await manager.select_session(
        1, 2, 3, second.runtime_session_id
    )

    assert (
        await manager.prewarm(
            second,
            object(),
            connector,
            prepare,
            selected_only=True,
            selection_epoch=second_epoch,
        )
        is True
    )
    assert (
        await manager.prewarm(
            first,
            object(),
            connector,
            prepare,
            selected_only=True,
            selection_epoch=first_epoch,
        )
        is False
    )
    assert connector.connect_count == 1
    assert connector.close_count == 0
    assert manager.ready_count == 1
    lease = await manager.acquire_run(
        second,
        object(),
        connector,
        prepare,
        runtime_run_id="rr-selected",
    )
    assert connector.connect_count == 1
    await manager.invalidate_session(1, 2, 3, "rs-3-2", reason="test_cleanup")
    await lease.release(terminal=True)
    await manager.close()


@pytest.mark.asyncio
async def test_selected_only_connecting_prewarm_finishes_when_superseded() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    prepare_started = asyncio.Event()

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        prepare_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    first = identity()
    second = identity(runtime_session_db_id=5, runtime_session_id="rs-3-2")
    first_epoch = await manager.select_session(
        1, 2, 3, first.runtime_session_id
    )
    prewarm = asyncio.create_task(
        manager.prewarm(
            first,
            object(),
            connector,
            prepare,
            selected_only=True,
            selection_epoch=first_epoch,
        )
    )
    await prepare_started.wait()

    await manager.select_session(1, 2, 3, second.runtime_session_id)

    assert await prewarm is False
    assert connector.connect_count == 1
    assert connector.close_count == 1
    assert manager.inflight_count == 0
    assert manager.slot_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_selected_only_connect_cancelled_before_start_is_superseded() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        raise AssertionError("superseded connect must not prepare")

    first = identity()
    second = identity(runtime_session_db_id=5, runtime_session_id="rs-3-2")
    first_epoch = await manager.select_session(
        1, 2, 3, first.runtime_session_id
    )

    prewarm = asyncio.create_task(
        manager.prewarm(
            first,
            object(),
            connector,
            prepare,
            selected_only=True,
            selection_epoch=first_epoch,
        )
    )
    switch = asyncio.create_task(
        manager.select_session(1, 2, 3, second.runtime_session_id)
    )

    assert await prewarm is False
    await switch
    assert connector.connect_count == 0
    assert manager.inflight_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_selected_only_warming_prewarm_waiter_is_woken_when_superseded() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    readiness_cancelled = asyncio.Event()

    async def prepare(_peer: DashboardRpcPeer) -> DashboardPreparedPeer:
        return DashboardPreparedPeer("live", None, ready=False)

    async def wait_ready(_peer: DashboardRpcPeer, _live_session_id: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            readiness_cancelled.set()
            raise

    first = identity()
    second = identity(runtime_session_db_id=5, runtime_session_id="rs-3-2")
    first_epoch = await manager.select_session(
        1, 2, 3, first.runtime_session_id
    )
    prewarm = asyncio.create_task(
        manager.prewarm(
            first,
            object(),
            connector,
            prepare,
            wait_ready=wait_ready,
            selected_only=True,
            selection_epoch=first_epoch,
        )
    )
    await wait_until(lambda: manager.warming_count == 1)

    await manager.select_session(1, 2, 3, second.runtime_session_id)

    assert await prewarm is False
    assert readiness_cancelled.is_set()
    assert connector.close_count == 1
    assert manager.warming_count == 0
    assert manager.slot_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_selected_only_warming_prewarm_stops_after_invalidation() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> DashboardPreparedPeer:
        return DashboardPreparedPeer("live", None, ready=False)

    async def wait_ready(_peer: DashboardRpcPeer, _live_session_id: str) -> None:
        await asyncio.Event().wait()

    current = identity()
    selection_epoch = await manager.select_session(
        1, 2, 3, current.runtime_session_id
    )
    prewarm = asyncio.create_task(
        manager.prewarm(
            current,
            object(),
            connector,
            prepare,
            wait_ready=wait_ready,
            selected_only=True,
            selection_epoch=selection_epoch,
        )
    )
    await wait_until(lambda: manager.warming_count == 1)

    await manager.invalidate_session(
        1,
        2,
        3,
        current.runtime_session_id,
        reason="model_changed",
    )

    assert await prewarm is False
    assert connector.close_count == 1
    assert manager.slot_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_selected_only_prewarm_does_not_swallow_caller_cancellation() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    prepare_started = asyncio.Event()

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        prepare_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    current = identity()
    selection_epoch = await manager.select_session(
        1, 2, 3, current.runtime_session_id
    )
    prewarm = asyncio.create_task(
        manager.prewarm(
            current,
            object(),
            connector,
            prepare,
            selected_only=True,
            selection_epoch=selection_epoch,
        )
    )
    await prepare_started.wait()
    prewarm.cancel()

    with pytest.raises(asyncio.CancelledError):
        await prewarm

    await manager.close()


@pytest.mark.asyncio
async def test_selected_only_connecting_prewarm_rejects_aba_selection() -> None:
    class DelayedExitConnector(FakeConnector):
        def __init__(self) -> None:
            super().__init__()
            self.exit_started = asyncio.Event()
            self.release_exit = asyncio.Event()

        @asynccontextmanager
        async def __call__(self, _endpoint):
            self.connect_count += 1
            websocket = FakeWebSocket()
            self.websockets.append(websocket)
            try:
                yield websocket
            finally:
                self.exit_started.set()
                await self.release_exit.wait()
                self.close_count += 1
                await websocket.close()

    connector = DelayedExitConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    prepare_started = asyncio.Event()
    prepare_calls = 0

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        nonlocal prepare_calls
        prepare_calls += 1
        if prepare_calls == 1:
            prepare_started.set()
            await asyncio.Event().wait()
        return "live", "/data/workspace/session"

    first = identity()
    second = identity(runtime_session_db_id=5, runtime_session_id="rs-3-2")
    first_epoch = await manager.select_session(
        1, 2, 3, first.runtime_session_id
    )
    old_prewarm = asyncio.create_task(
        manager.prewarm(
            first,
            object(),
            connector,
            prepare,
            selected_only=True,
            selection_epoch=first_epoch,
        )
    )
    await prepare_started.wait()

    switch_to_second = asyncio.create_task(
        manager.select_session(1, 2, 3, second.runtime_session_id)
    )
    await connector.exit_started.wait()
    new_first_epoch = await manager.select_session(
        1, 2, 3, first.runtime_session_id
    )
    connector.release_exit.set()

    await switch_to_second
    assert await old_prewarm is False
    assert new_first_epoch > first_epoch
    assert (
        await manager.prewarm(
            first,
            object(),
            connector,
            prepare,
            selected_only=True,
            selection_epoch=new_first_epoch,
        )
        is True
    )
    assert connector.connect_count == 2
    assert manager.ready_count == 1
    await manager.close()


@pytest.mark.asyncio
async def test_selected_only_same_selection_builds_one_channel() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    prepare_started = asyncio.Event()
    release_prepare = asyncio.Event()
    prepare_calls = 0

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        nonlocal prepare_calls
        prepare_calls += 1
        prepare_started.set()
        await release_prepare.wait()
        return "live", "/data/workspace/session"

    current = identity()
    selection_epoch = await manager.select_session(
        1, 2, 3, current.runtime_session_id
    )
    first = asyncio.create_task(
        manager.prewarm(
            current,
            object(),
            connector,
            prepare,
            selected_only=True,
            selection_epoch=selection_epoch,
        )
    )
    await prepare_started.wait()
    second = asyncio.create_task(
        manager.prewarm(
            current,
            object(),
            connector,
            prepare,
            selected_only=True,
            selection_epoch=selection_epoch,
        )
    )
    release_prepare.set()

    assert await asyncio.gather(first, second) == [True, True]
    assert connector.connect_count == 1
    assert prepare_calls == 1
    await manager.close()


@pytest.mark.asyncio
async def test_selected_only_ready_channel_closed_before_final_check_is_error() -> None:
    class ClosingAfterReadyManager(DashboardHotChannelManager):
        async def _wait_slot_ready(self, slot) -> None:
            await super()._wait_slot_ready(slot)
            await slot.peer.close()

    connector = FakeConnector()
    manager = ClosingAfterReadyManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "live", "/data/workspace/session"

    current = identity()
    selection_epoch = await manager.select_session(
        1, 2, 3, current.runtime_session_id
    )

    with pytest.raises(
        HermesProtocolError,
        match="HotChannel is not reusable after prewarm",
    ):
        await manager.prewarm(
            current,
            object(),
            connector,
            prepare,
            selected_only=True,
            selection_epoch=selection_epoch,
        )

    await manager.close()


@pytest.mark.asyncio
async def test_session_switch_drains_without_interrupting_old_run() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "live", "/data/workspace/session"

    first = identity()
    second = identity(runtime_session_db_id=5, runtime_session_id="rs-3-2")
    await manager.prewarm(first, object(), connector, prepare)
    lease = await manager.acquire_run(
        first, object(), connector, prepare, runtime_run_id="rr-old"
    )

    await manager.select_session(1, 2, 3, second.runtime_session_id)
    assert lease.state is HotChannelState.DRAINING
    assert connector.close_count == 0
    assert await manager.has_blocking_owner_channel(second) is True

    second_prewarm = asyncio.create_task(
        manager.prewarm(second, object(), connector, prepare)
    )
    await asyncio.sleep(0)
    assert second_prewarm.done() is False
    assert manager.slot_count == 1
    await lease.release(terminal=True)
    assert await second_prewarm is True
    assert await manager.has_blocking_owner_channel(second) is False
    assert connector.connect_count == 2
    assert connector.close_count == 1
    assert manager.ready_count == 1
    await manager.close()


@pytest.mark.asyncio
async def test_workspace_touch_updates_only_selected_session() -> None:
    now = 10.0
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90, now=lambda: now)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "live", "/data/workspace/session"

    first = identity()
    second = identity(runtime_session_db_id=5, runtime_session_id="rs-3-2")
    await manager.prewarm(first, object(), connector, prepare)
    old_lease = await manager.acquire_run(
        first, object(), connector, prepare, runtime_run_id="rr-old"
    )
    await manager.select_session(1, 2, 3, second.runtime_session_id)
    second_prewarm = asyncio.create_task(
        manager.prewarm(second, object(), connector, prepare)
    )
    await asyncio.sleep(0)

    now = 50.0
    await manager.touch_workspace(1, 2, 3)

    assert manager._slots[first].last_business_touch == 10.0
    assert second not in manager._slots
    await old_lease.release(terminal=True)
    assert await second_prewarm is True
    await manager.touch_workspace(1, 2, 3)
    assert manager._slots[second].last_business_touch == 50.0
    await manager.close()


@pytest.mark.asyncio
async def test_prewarm_replaces_dead_ready_channel() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "live", "/data/workspace/session"

    await manager.prewarm(identity(), object(), connector, prepare)
    await connector.websockets[0].incoming.put(ConnectionError("remote closed"))
    await wait_until(lambda: manager._slots[identity()].peer.closed)

    await manager.prewarm(identity(), object(), connector, prepare)

    assert connector.connect_count == 2
    assert connector.close_count == 1
    assert manager.ready_count == 1
    assert manager.result_snapshot()["dead_ready"] == 1
    await manager.close()


@pytest.mark.asyncio
async def test_normal_terminal_builds_one_fresh_selected_channel() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    prepare_count = 0

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        nonlocal prepare_count
        prepare_count += 1
        return "rs-3-1", "/data/workspace/rs-3-1"

    await manager.prewarm(identity(), object(), connector, prepare)
    lease = await manager.acquire_run(
        identity(), object(), connector, prepare, runtime_run_id="rr-1"
    )
    await lease.release(terminal=True)
    await wait_until(lambda: manager.ready_count == 1)

    assert connector.connect_count == 2
    assert connector.close_count == 1
    assert prepare_count == 2
    assert manager.inflight_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_fast_acquire_shares_registered_replacement_task() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    replacement_started = asyncio.Event()
    release_replacement = asyncio.Event()
    prepare_count = 0

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        nonlocal prepare_count
        prepare_count += 1
        if prepare_count == 2:
            replacement_started.set()
            await release_replacement.wait()
        return "rs-3-1", "/data/workspace/rs-3-1"

    await manager.prewarm(identity(), object(), connector, prepare)
    first = await manager.acquire_run(
        identity(), object(), connector, prepare, runtime_run_id="rr-1"
    )
    await first.release(terminal=True)
    await replacement_started.wait()

    second_task = asyncio.create_task(
        manager.acquire_run(
            identity(), object(), connector, prepare, runtime_run_id="rr-2"
        )
    )
    duplicate_prewarm = asyncio.create_task(
        manager.prewarm(identity(), object(), connector, prepare)
    )
    await asyncio.sleep(0)
    assert manager.inflight_count == 1
    assert connector.connect_count == 2

    release_replacement.set()
    second = await second_task
    await duplicate_prewarm
    assert connector.connect_count == 2
    await manager.invalidate_session(1, 2, 3, "rs-3-1", reason="test_cleanup")
    await second.release(terminal=True)
    await manager.close()


@pytest.mark.asyncio
async def test_invalidation_prevents_old_identity_replacement() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "rs-3-1", None

    await manager.prewarm(identity(), object(), connector, prepare)
    lease = await manager.acquire_run(
        identity(), object(), connector, prepare, runtime_run_id="rr-1"
    )
    await manager.invalidate_session(1, 2, 3, "rs-3-1", reason="model_changed")
    await lease.release(terminal=True)
    await asyncio.sleep(0)

    assert connector.connect_count == 1
    assert manager.slot_count == 0
    assert manager.inflight_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_invalidation_waits_for_terminal_replacement_to_close() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    replacement_started = asyncio.Event()
    prepare_count = 0

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        nonlocal prepare_count
        prepare_count += 1
        if prepare_count == 2:
            replacement_started.set()
            await asyncio.Event().wait()
        return "rs-3-1", None

    await manager.prewarm(identity(), object(), connector, prepare)
    lease = await manager.acquire_run(
        identity(), object(), connector, prepare, runtime_run_id="rr-1"
    )
    await lease.release(terminal=True)
    await replacement_started.wait()

    await manager.invalidate_session(
        1,
        2,
        3,
        "rs-3-1",
        reason="session_mutation",
    )

    assert connector.connect_count == 2
    assert connector.close_count == 2
    assert manager.slot_count == 0
    assert manager.inflight_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_nonterminal_release_does_not_build_replacement() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "rs-3-1", None

    await manager.prewarm(identity(), object(), connector, prepare)
    lease = await manager.acquire_run(
        identity(), object(), connector, prepare, runtime_run_id="rr-1"
    )
    await lease.release(terminal=False)
    await asyncio.sleep(0)

    assert connector.connect_count == 1
    assert manager.slot_count == 0
    assert manager.inflight_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_failed_replacement_cleans_up_and_later_prewarm_retries() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    prepare_count = 0

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        nonlocal prepare_count
        prepare_count += 1
        if prepare_count == 2:
            raise HermesProtocolError("replacement failed")
        return "rs-3-1", None

    await manager.prewarm(identity(), object(), connector, prepare)
    lease = await manager.acquire_run(
        identity(), object(), connector, prepare, runtime_run_id="rr-1"
    )
    await lease.release(terminal=True)
    await wait_until(lambda: manager.inflight_count == 0)

    assert manager.slot_count == 0
    await manager.prewarm(identity(), object(), connector, prepare)
    assert connector.connect_count == 3
    assert manager.ready_count == 1
    await manager.close()


@pytest.mark.asyncio
async def test_close_cancels_replacement_without_resurrecting_slot() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    replacement_started = asyncio.Event()
    prepare_count = 0

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        nonlocal prepare_count
        prepare_count += 1
        if prepare_count == 2:
            replacement_started.set()
            await asyncio.Event().wait()
        return "rs-3-1", None

    await manager.prewarm(identity(), object(), connector, prepare)
    lease = await manager.acquire_run(
        identity(), object(), connector, prepare, runtime_run_id="rr-1"
    )
    await lease.release(terminal=True)
    await replacement_started.wait()
    await manager.close()

    assert manager.slot_count == 0
    assert manager.inflight_count == 0
    assert connector.connect_count == 2
    assert connector.close_count == 2


@pytest.mark.asyncio
async def test_identity_change_closes_ready_channel_and_never_reuses_it() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "rs-3-1", "/data/workspace/rs-3-1"

    await manager.prewarm(identity(config_generation=3), object(), connector, prepare)
    await manager.prewarm(identity(config_generation=4), object(), connector, prepare)

    assert connector.connect_count == 2
    assert connector.close_count == 1
    assert manager.ready_count == 1
    await manager.close()


@pytest.mark.asyncio
async def test_in_use_channel_drains_after_invalidation_and_rejects_second_run() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "rs-3-1", "/data/workspace/rs-3-1"

    lease = await manager.acquire_run(
        identity(), object(), connector, prepare, runtime_run_id="rr-1"
    )
    with pytest.raises(DashboardChannelBusy):
        await manager.acquire_run(
            identity(), object(), connector, prepare, runtime_run_id="rr-2"
        )

    await manager.invalidate_session(1, 2, 3, "rs-3-1", reason="model_changed")
    assert lease.state is HotChannelState.DRAINING
    assert connector.close_count == 0
    await lease.release(terminal=True)
    assert connector.close_count == 1
    assert manager.slot_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_ready_channel_expires_from_business_time_not_rpc_activity() -> None:
    now = 10.0
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90, now=lambda: now)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "rs-3-1", "/data/workspace/rs-3-1"

    await manager.prewarm(identity(), object(), connector, prepare)
    now = 101.0
    await manager.reap_idle()

    assert connector.close_count == 1
    assert manager.slot_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_and_leaves_no_reader_tasks() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "rs-3-1", None

    await manager.prewarm(identity(), object(), connector, prepare)
    await manager.close()
    await manager.close()

    assert manager.slot_count == 0
    assert manager.inflight_count == 0
    assert connector.close_count == 1


@pytest.mark.asyncio
async def test_acquire_discards_notifications_buffered_after_previous_turn() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "rs-3-1", "/data/workspace/rs-3-1"

    await manager.prewarm(identity(), object(), connector, prepare)
    await connector.websockets[0].push(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"type": "message.complete", "text": "old answer"},
        }
    )
    await asyncio.sleep(0)

    lease = await manager.acquire_run(
        identity(), object(), connector, prepare, runtime_run_id="rr-next"
    )

    assert manager.result_snapshot()["stale_notifications_discarded"] == 1
    await lease.release(terminal=True)
    await manager.close()


@pytest.mark.asyncio
async def test_independent_sessions_keep_multiple_ready_channels_and_rearm() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    first = identity(
        runtime_session_db_id=41,
        runtime_session_id="app-session-a",
        independent_session=True,
        max_coordination_slots=2,
    )
    second = identity(
        runtime_session_db_id=42,
        runtime_session_id="app-session-b",
        independent_session=True,
        max_coordination_slots=2,
    )

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "live", "/data/workspace/sessions/live"

    await manager.prewarm(first, object(), connector, prepare)
    await manager.prewarm(second, object(), connector, prepare)

    assert manager.ready_count == 2
    lease = await manager.acquire_run(
        first,
        object(),
        connector,
        prepare,
        runtime_run_id="app-run-a",
    )
    await lease.release(terminal=True)
    await wait_until(lambda: manager.inflight_count == 0)

    assert manager.ready_count == 2
    assert connector.connect_count == 3
    await manager.close()


@pytest.mark.asyncio
async def test_selecting_another_independent_session_does_not_drain_active_run() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    first = identity(
        runtime_session_db_id=41,
        runtime_session_id="personal-session-a",
        independent_session=True,
        max_coordination_slots=14,
    )
    second = identity(
        runtime_session_db_id=42,
        runtime_session_id="personal-session-b",
        independent_session=True,
        max_coordination_slots=14,
    )

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "live", "/data/workspace/sessions/live"

    await manager.prewarm(first, object(), connector, prepare)
    lease = await manager.acquire_run(
        first,
        object(),
        connector,
        prepare,
        runtime_run_id="personal-run-a",
    )

    await manager.select_session(1, 2, 3, second.runtime_session_id)
    assert lease.state is HotChannelState.IN_USE
    assert await manager.has_blocking_owner_channel(second) is False

    await manager.prewarm(second, object(), connector, prepare)
    assert manager.ready_count == 1
    assert manager.slot_count == 2
    await lease.release(terminal=False)
    await manager.close()


@pytest.mark.asyncio
async def test_independent_capacity_evicts_oldest_ready_slot() -> None:
    now = 1.0
    connector = FakeConnector()
    manager = DashboardHotChannelManager(
        idle_grace_seconds=90,
        now=lambda: now,
    )

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "live", None

    first = identity(
        runtime_session_db_id=41,
        runtime_session_id="app-session-a",
        independent_session=True,
        max_coordination_slots=2,
    )
    second = identity(
        runtime_session_db_id=42,
        runtime_session_id="app-session-b",
        independent_session=True,
        max_coordination_slots=2,
    )
    third = identity(
        runtime_session_db_id=43,
        runtime_session_id="app-session-c",
        independent_session=True,
        max_coordination_slots=2,
    )
    await manager.prewarm(first, object(), connector, prepare)
    now = 2.0
    await manager.prewarm(second, object(), connector, prepare)
    now = 3.0
    await manager.prewarm(third, object(), connector, prepare)

    assert manager.ready_count == 2
    assert connector.close_count == 1
    assert manager.result_snapshot()["capacity_lru_evicted"] == 1
    await manager.close()


@pytest.mark.asyncio
async def test_independent_capacity_never_evicts_in_use_slot() -> None:
    connector = FakeConnector()
    manager = DashboardHotChannelManager(idle_grace_seconds=90)
    first = identity(
        runtime_session_db_id=41,
        runtime_session_id="app-session-a",
        independent_session=True,
        max_coordination_slots=1,
    )
    second = identity(
        runtime_session_db_id=42,
        runtime_session_id="app-session-b",
        independent_session=True,
        max_coordination_slots=1,
    )

    async def prepare(_peer: DashboardRpcPeer) -> tuple[str, str | None]:
        return "live", None

    await manager.prewarm(first, object(), connector, prepare)
    lease = await manager.acquire_run(
        first,
        object(),
        connector,
        prepare,
        runtime_run_id="app-run-a",
    )
    with pytest.raises(DashboardHotCapacityUnavailable):
        await manager.prewarm(second, object(), connector, prepare)

    assert manager.slot_count == 1
    assert connector.close_count == 0
    assert (
        manager.result_snapshot()["capacity_cold_fallback"]
        == 1
    )
    await lease.release(terminal=False)
    await manager.close()
