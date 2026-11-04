from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import asyncio
import json

import pytest

from platform_supervisor.execution.redirect_gate import (
    HermesV0201RedirectHistoryVerifier,
    RedirectDurableGate,
)
from platform_supervisor.hermes.dashboard_hot_channel import (
    ActiveRunControlPeerLease,
    ActiveRunControlPeerRegistry,
)
from platform_supervisor.model_relay.boundary import (
    MainBoundaryAction,
    MainBoundaryBoot,
    MainBoundaryCoordinator,
    MainBoundaryDispatchResult,
    MainBoundaryRouteBinding,
)
from platform_supervisor.storage import (
    DispatchIntent,
    RedirectAdmissionIntent,
    RunEnqueueIntent,
    SQLiteStore,
)


NOW = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
ROUTE_TOKEN = "a" * 64
BOUNDARY_EPOCH = "EBESExQVFhcYGRobHB0eHw"
REQUEST_NONCE = "AAECAwQFBgcICQoLDA0ODw"


class FakePeer:
    def __init__(self, status: str) -> None:
        self.status = status
        self.closed = False
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def request(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        del timeout_seconds
        self.calls.append((method, params))
        return {"status": self.status}


class FakeHistory:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = messages

    async def get_session_messages(
        self,
        *,
        session_id: str,
        profile_name: str = "default",
    ) -> dict[str, object]:
        del session_id, profile_name
        return {"session_id": "hermes-live-1", "messages": self.messages}


class FakeEventFences:
    def __init__(self, store: SQLiteStore | None = None) -> None:
        self.store = store
        self.active: set[str] = set()
        self.released: list[str] = []

    async def publish_durable_followup_events(
        self,
        parent_run_id: str,
    ) -> None:
        del parent_run_id

    async def begin_redirect_event_fence(
        self,
        *,
        parent_run_id: str,
        dispatch_group_id: str,
        recorder,
    ) -> None:
        pre_fence_event_seq = 1
        if self.store is not None:
            snapshot = await self.store.load_run_snapshot(parent_run_id)
            if snapshot is not None and snapshot.events:
                pre_fence_event_seq = max(event.seq for event in snapshot.events)
        await recorder(pre_fence_event_seq, f"sha256:{'4' * 64}")
        self.active.add(dispatch_group_id)

    async def release_redirect_event_fence(
        self,
        *,
        parent_run_id: str,
        dispatch_group_id: str,
    ) -> None:
        del parent_run_id
        self.active.discard(dispatch_group_id)
        self.released.append(dispatch_group_id)
        if self.store is not None:
            await self.store.complete_redirect_event_fence_release(
                dispatch_group_id
            )


class BlockingSettleController:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def interrupt_and_settle_redirect(self, **_kwargs) -> object:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return object()


class RecordingSettleController:
    def __init__(self, callback=None) -> None:
        self.callback = callback
        self.calls = 0

    async def interrupt_and_settle_redirect(self, **_kwargs) -> object:
        self.calls += 1
        if self.callback is not None:
            result = self.callback()
            if result is not None:
                await result
        return object()


class RaisingPreRpcEventFences(FakeEventFences):
    async def begin_redirect_event_fence(
        self,
        *,
        parent_run_id: str,
        dispatch_group_id: str,
        recorder,
    ) -> None:
        del parent_run_id
        await recorder(1, f"sha256:{'4' * 64}")
        self.active.add(dispatch_group_id)
        raise RuntimeError("pre-RPC event fence failure")


class BlockingPreRpcEventFences(FakeEventFences):
    def __init__(self, store: SQLiteStore) -> None:
        super().__init__(store)
        self.started = asyncio.Event()

    async def begin_redirect_event_fence(
        self,
        *,
        parent_run_id: str,
        dispatch_group_id: str,
        recorder,
    ) -> None:
        snapshot = await self.store.load_run_snapshot(parent_run_id)
        assert snapshot is not None
        pre_fence_event_seq = max(event.seq for event in snapshot.events)
        await recorder(pre_fence_event_seq, f"sha256:{'4' * 64}")
        self.active.add(dispatch_group_id)
        self.started.set()
        await asyncio.Event().wait()


@dataclass(slots=True)
class GateFixture:
    store: SQLiteStore
    gate: RedirectDurableGate
    boundary: MainBoundaryCoordinator
    peers: ActiveRunControlPeerRegistry
    peer: FakePeer
    lease: ActiveRunControlPeerLease
    fences: FakeEventFences
    parent_run_id: str
    session_id: str
    marker: str
    settle: object
    history: FakeHistory

    async def close(self) -> None:
        await self.gate.close()
        await self.store.close()


async def _build_gate(
    tmp_path: Path,
    *,
    status: str,
    settle: object | None = None,
    carrier_proof_seconds: float = 30.0,
) -> GateFixture:
    store = SQLiteStore(tmp_path / f"{status}.db")
    await store.start()
    session_id = f"session-{status}"
    queued = await store.enqueue_run(
        RunEnqueueIntent(
            run_id=f"run-{status}",
            session_id=session_id,
            client_message_id=f"parent-{status}",
            payload_digest="a" * 64,
            config_revision="revision-1",
            source="web",
            enqueued_at=NOW,
            prompt="original",
            audit_prompt="original",
            identity={
                "runtime_id": "runtime-1",
                "session_id": session_id,
                "profile_id": "default",
                "model_id": "7",
                "config_revision": "revision-1",
                "admission_generation": 1,
            },
        )
    )
    claimed = await store.claim_next_queued(session_id, accepted_at=NOW)
    assert claimed is not None
    parent = await store.persist_dispatch_intent(
        DispatchIntent(
            run_id=queued.run_id,
            session_id=session_id,
            client_message_id=queued.client_message_id,
            payload_digest=queued.payload_digest,
            config_revision=queued.config_revision,
            source="web",
            accepted_at=NOW,
            profile_id="default",
            model_id="7",
        )
    )
    await store.admit_redirect(
        RedirectAdmissionIntent(
            redirect_id=f"redirect-{status}",
            session_id=session_id,
            client_message_id=f"correction-{status}",
            request_digest=f"sha256:{'b' * 64}",
            parent_run_id=parent.run_id,
            interaction_origin="user_web",
            content="use the corrected approach",
            initial_state="WAITING_AGENT_READY",
            promotion_reserved_run_id=f"promoted-{status}",
            promotion_run_intent={
                "prepare_identity": {
                    "runtime_id": "runtime-1",
                    "session_id": session_id,
                    "profile_id": "default",
                    "model_id": "7",
                    "config_revision": "revision-1",
                    "admission_generation": 1,
                },
                "source": "web",
                "prompt": "use the corrected approach",
                "audit_prompt": "use the corrected approach",
                "payload_digest": f"sha256:{'c' * 64}",
            },
            promotion_run_payload_digest=f"sha256:{'c' * 64}",
            admission_generation=1,
            created_at=NOW,
        )
    )

    peers = ActiveRunControlPeerRegistry()
    peer = FakePeer(status)
    lease = await peers.register(
        runtime_run_id=parent.run_id,
        stored_session_id=session_id,
        peer=peer,  # type: ignore[arg-type]
    )
    history = FakeHistory(
        [
            {
                "id": 1,
                "session_id": "hermes-live-1",
                "role": "user",
                "content": "original",
                "active": 1,
                "compacted": 0,
            }
        ]
    )
    fences = FakeEventFences(store)
    settle = settle or BlockingSettleController()
    gate = RedirectDurableGate(
        store=store,
        control_peers=peers,
        history_source=history,
        event_fences=fences,
        settle_controller=settle,  # type: ignore[arg-type]
        carrier_proof_seconds=carrier_proof_seconds,
    )
    boundary = MainBoundaryCoordinator(gate=gate, clock=lambda: NOW)
    gate.bind_boundary(boundary)
    peers.set_boundary_observer(gate)
    await boundary.register_boot(
        MainBoundaryBoot(
            dashboard_boot_id="dashboard-boot-1",
            marker_key=bytes(range(32)),
            boundary_epoch_nonce=BOUNDARY_EPOCH,
        )
    )
    await boundary.register_route(
        MainBoundaryRouteBinding(
            route_token=ROUTE_TOKEN,
            run_id=parent.run_id,
            dashboard_boot_id="dashboard-boot-1",
            dashboard_connection_id=lease.dashboard_connection_id,
            stored_session_id=session_id,
            live_session_id="hermes-live-1",
            task_id="task-1",
            profile_key="default",
            boundary_epoch_nonce=BOUNDARY_EPOCH,
        )
    )
    await lease.bind_live_session("hermes-live-1")
    await lease.observe_message_start()

    marker = MainBoundaryCoordinator.sign_test_marker(
        bytes(range(32)),
        {
            "contract_version": 1,
            "route_token": ROUTE_TOKEN,
            "session_id": session_id,
            "task_id": "task-1",
            "turn_id": "turn-1",
            "api_request_id": f"request-{status}",
            "api_call_count": 1,
            "boundary_epoch_nonce": BOUNDARY_EPOCH,
            "request_nonce": REQUEST_NONCE,
            "issued_at": "2026-08-14T12:00:00.000Z",
            "expires_at": "2026-08-14T12:00:20.000Z",
        },
    )
    return GateFixture(
        store=store,
        gate=gate,
        boundary=boundary,
        peers=peers,
        peer=peer,
        lease=lease,
        fences=fences,
        parent_run_id=parent.run_id,
        session_id=session_id,
        marker=marker,
        settle=settle,
        history=history,
    )


async def _decision(fixture: GateFixture):
    decision = await fixture.boundary.before_forward(
        route=SimpleNamespace(
            route_token=ROUTE_TOKEN,
            run_id=fixture.parent_run_id,
            session_id=fixture.session_id,
        ),
        marker_header=fixture.marker,
    )
    return await fixture.boundary.mark_upstream_active(decision)


@pytest.mark.asyncio
async def test_redirect_capability_rejects_live_session_drift_before_rpc(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="redirected")
    try:
        await fixture.boundary.disarm_redirect(fixture.parent_run_id)
        decision = await fixture.boundary.before_forward(
            route=SimpleNamespace(
                route_token=ROUTE_TOKEN,
                run_id=fixture.parent_run_id,
                session_id=fixture.session_id,
            ),
            marker_header=fixture.marker,
        )
        active = await fixture.boundary.mark_upstream_active(decision)
        assert active.action is MainBoundaryAction.FORWARD

        handle = await fixture.boundary.take_active_upstream_handle(
            fixture.parent_run_id
        )
        assert handle is not None
        assert handle.live_session_id == "hermes-live-1"

        await fixture.lease.bind_live_session("hermes-live-2")
        result = await fixture.gate.dispatch(handle)

        assert result is MainBoundaryDispatchResult.RELEASE
        assert fixture.peer.calls == []
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_active_main_request_redirects_without_waiting_for_next_boundary(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="redirected")
    await fixture.boundary.disarm_redirect(fixture.parent_run_id)
    decision = await _decision(fixture)
    assert decision.action is MainBoundaryAction.FORWARD
    await fixture.boundary.mark_upstream_active(decision)

    await fixture.gate.redirect_admitted(fixture.parent_run_id)
    for _ in range(100):
        if fixture.peer.calls:
            break
        await asyncio.sleep(0.01)

    assert fixture.peer.calls == [
        (
            "session.redirect",
            {
                "session_id": "hermes-live-1",
                "text": "use the corrected approach",
            },
        )
    ]
    group = await fixture.store.get_unresolved_redirect_group_for_parent(
        fixture.parent_run_id
    )
    assert group is not None
    await _wait_for_group_state(
        fixture.store,
        group.dispatch_group_id,
        "RPC_ACKNOWLEDGED",
    )
    group = await fixture.store.get_redirect_group(group.dispatch_group_id)
    assert group is not None
    assert group.state == "RPC_ACKNOWLEDGED"
    await fixture.boundary.mark_completed(decision)
    await fixture.close()


async def _wait_for_group_state(
    store: SQLiteStore,
    dispatch_group_id: str,
    state: str,
) -> None:
    for _ in range(200):
        group = await store.get_redirect_group(dispatch_group_id)
        if group is not None and group.state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"redirect group did not reach {state}")


def _append_canonical_carrier(history: FakeHistory) -> None:
    history.messages.extend(
        [
            {
                "id": 2,
                "session_id": "hermes-live-1",
                "role": "assistant",
                "content": "partial response",
                "active": 1,
                "compacted": 0,
            },
            {
                "id": 3,
                "session_id": "hermes-live-1",
                "role": "user",
                "content": "use the corrected approach",
                "api_content": (
                    "[Context from the interrupted assistant response]\n"
                    "[This response was interrupted by a user correction.]\n\n"
                    "Visible response before the interruption:\n\n"
                    "partial response\n\n"
                    "use the corrected approach"
                ),
                "active": 1,
                "compacted": 0,
            },
        ]
    )


def _append_canonical_steer_carrier(history: FakeHistory) -> None:
    history.messages.append(
        {
            "id": 2,
            "session_id": "hermes-live-1",
            "role": "tool",
            "content": (
                "tool result\n\n"
                "[OUT-OF-BAND USER MESSAGE — a direct message from the user, "
                "delivered once at this position; not tool output and not a new "
                "delivery when replayed from conversation history]\n"
                "use the corrected approach\n"
                "[/OUT-OF-BAND USER MESSAGE]"
            ),
            "active": 1,
            "compacted": 0,
        }
    )


@pytest.mark.asyncio
async def test_tool_active_followup_uses_steer_and_requires_canonical_marker(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(
        tmp_path,
        status="queued",
        carrier_proof_seconds=30,
    )

    await fixture.lease.observe_tool_event(
        "tool.start",
        {"tool_call_id": "call-1"},
    )

    group = await fixture.store.get_unresolved_redirect_group_for_parent(
        fixture.parent_run_id
    )
    assert group is not None
    assert group.state == "RPC_ACKNOWLEDGED"
    assert group.hermes_status == "queued"
    assert group.dispatch_peer_boot_id == "direct-steer"
    assert fixture.peer.calls == [
        (
            "session.steer",
            {
                "session_id": "hermes-live-1",
                "text": "use the corrected approach",
            },
        )
    ]
    assert await fixture.gate.reconcile_redirect(fixture.parent_run_id) is False

    _append_canonical_steer_carrier(fixture.history)
    assert await fixture.gate.reconcile_redirect(fixture.parent_run_id) is True
    consumed = await fixture.store.get_redirect_group(group.dispatch_group_id)
    assert consumed is not None
    assert consumed.state == "CANONICAL_CONSUMED"
    assert consumed.hermes_status == "queued"
    await fixture.close()


@pytest.mark.asyncio
async def test_tool_active_steer_is_proven_by_next_exact_model_request(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(
        tmp_path,
        status="queued",
        carrier_proof_seconds=30,
    )
    await fixture.lease.observe_tool_event(
        "tool.start",
        {"tool_call_id": "call-model-proof"},
    )
    group = await fixture.store.get_unresolved_redirect_group_for_parent(
        fixture.parent_run_id
    )
    assert group is not None
    fixture.history.messages.append(
        {
            "id": 2,
            "session_id": "hermes-live-1",
            "role": "tool",
            "content": "tool result",
            "active": 1,
            "compacted": 0,
        }
    )
    marker = (
        "\n\n[OUT-OF-BAND USER MESSAGE — a direct message from the user, "
        "delivered once at this position; not tool output and not a new "
        "delivery when replayed from conversation history]\n"
        "use the corrected approach\n"
        "[/OUT-OF-BAND USER MESSAGE]"
    )
    request_body = json.dumps(
        {
            "model": "7",
            "messages": [
                {"role": "tool", "content": f"tool result{marker}"},
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    binding = MainBoundaryRouteBinding(
        route_token=ROUTE_TOKEN,
        run_id=fixture.parent_run_id,
        dashboard_boot_id="dashboard-boot-1",
        dashboard_connection_id=fixture.lease.dashboard_connection_id,
        stored_session_id=fixture.session_id,
        live_session_id="hermes-live-1",
        task_id="task-1",
        profile_key="default",
        boundary_epoch_nonce=BOUNDARY_EPOCH,
    )

    assert await fixture.gate.observe_model_request(
        binding=binding,
        request_body=request_body,
    ) is True
    consumed = await fixture.store.get_redirect_group(group.dispatch_group_id)
    assert consumed is not None
    assert consumed.state == "CANONICAL_CONSUMED"
    snapshot = await fixture.store.load_run_snapshot(fixture.parent_run_id)
    assert snapshot is not None
    applied = [
        event
        for event in snapshot.events
        if event.event_type == "followup.applied"
    ]
    assert len(applied) == 1
    assert applied[0].payload["proof_kind"] == "steer_oob_marker"
    assert group.dispatch_group_id not in fixture.fences.active
    await fixture.close()


@pytest.mark.asyncio
async def test_tool_active_rapid_followups_merge_before_one_steer_rpc(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(
        tmp_path,
        status="queued",
        carrier_proof_seconds=30,
    )
    fixture.peers.set_boundary_observer(None)
    await fixture.lease.observe_tool_event(
        "tool.start",
        {"tool_call_id": "call-merge"},
    )
    fixture.peers.set_boundary_observer(fixture.gate)

    for index, content in ((2, "second follow-up"), (3, "third follow-up")):
        await fixture.store.admit_redirect(
            RedirectAdmissionIntent(
                redirect_id=f"redirect-steer-{index}",
                session_id=fixture.session_id,
                client_message_id=f"correction-steer-{index}",
                request_digest=f"sha256:{str(index) * 64}",
                parent_run_id=fixture.parent_run_id,
                interaction_origin="user_web",
                content=content,
                initial_state="WAITING_AGENT_READY",
                promotion_reserved_run_id=f"promoted-steer-{index}",
                promotion_run_intent={
                    "prepare_identity": {
                        "runtime_id": "runtime-1",
                        "session_id": fixture.session_id,
                        "profile_id": "default",
                        "model_id": "7",
                        "config_revision": "revision-1",
                        "admission_generation": 1,
                    },
                    "source": "web",
                    "prompt": content,
                    "audit_prompt": content,
                    "payload_digest": f"sha256:{str(index + 3) * 64}",
                },
                promotion_run_payload_digest=f"sha256:{str(index + 3) * 64}",
                admission_generation=1,
                created_at=NOW,
            )
        )
        await fixture.gate.redirect_admitted(fixture.parent_run_id)

    await asyncio.sleep(0.1)

    assert fixture.peer.calls == [
        (
            "session.steer",
            {
                "session_id": "hermes-live-1",
                "text": (
                    "use the corrected approach\n\n"
                    "[Additional user correction]\nsecond follow-up\n\n"
                    "[Additional user correction]\nthird follow-up"
                ),
            },
        )
    ]
    group = await fixture.store.get_unresolved_redirect_group_for_parent(
        fixture.parent_run_id
    )
    assert group is not None
    assert group.member_count == 3
    await fixture.close()


@pytest.mark.asyncio
async def test_redirected_ack_is_durable_and_keeps_event_fence(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="redirected")
    decision = await _decision(fixture)

    assert decision.action is MainBoundaryAction.ABORT
    group = await fixture.store.get_unresolved_redirect_group_for_parent(
        fixture.parent_run_id
    )
    assert group is not None
    assert group.state == "RPC_ACKNOWLEDGED"
    redirect = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-redirected",
    )
    assert redirect is not None
    assert redirect.state == "RPC_ACKNOWLEDGED"
    assert redirect.accepted_event_seq is not None
    assert group.dispatch_group_id in fixture.fences.active
    assert fixture.peer.calls == [
        (
            "session.redirect",
            {
                "session_id": "hermes-live-1",
                "text": "use the corrected approach",
            },
        )
    ]
    await fixture.close()


@pytest.mark.asyncio
async def test_rejected_redirect_atomically_continues_and_releases_fence(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    fixture = await _build_gate(tmp_path, status="rejected")
    decision = await _decision(fixture)

    assert decision.action is MainBoundaryAction.FORWARD
    redirect = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-rejected",
    )
    assert redirect is not None
    assert redirect.state == "PROMOTED_TO_RUN"
    assert redirect.terminal_disposition_kind == "promoted"
    assert redirect.promoted_run_id == "promoted-rejected"
    promoted = await fixture.store.get_run("promoted-rejected")
    assert promoted is not None
    assert promoted.enqueue_seq == redirect.input_admission_seq
    snapshot = await fixture.store.load_run_snapshot(fixture.parent_run_id)
    assert snapshot is not None
    assert snapshot.events[-1].event_type == "followup.continued"
    assert fixture.fences.active == set()
    assert len(fixture.fences.released) == 1
    receipts = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "followup_boundary_receipt"
    ]
    assert [record.followup_stage for record in receipts] == [
        "redirect_dispatch_anchored",
        "redirect_rpc_returned",
        "canonical_proof_absent",
        "redirect_rejected_continued",
    ]
    assert receipts[0].followup_live_session_id == "hermes-live-1"
    assert receipts[0].followup_member_count == 1
    assert all("use the corrected approach" not in record.getMessage() for record in receipts)
    await fixture.close()


@pytest.mark.asyncio
async def test_queued_redirect_enters_isolation_without_success_event(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="queued")
    decision = await _decision(fixture)

    assert decision.action is MainBoundaryAction.ISOLATE
    group = await fixture.store.get_unresolved_redirect_group_for_parent(
        fixture.parent_run_id
    )
    assert group is not None
    assert group.state == "ISOLATING_UNCERTAIN"
    assert group.hermes_status == "queued"
    redirect = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-queued",
    )
    assert redirect is not None
    assert redirect.accepted_event_seq is not None
    snapshot = await fixture.store.load_run_snapshot(fixture.parent_run_id)
    assert snapshot is not None
    assert "followup.applied" not in {
        event.event_type for event in snapshot.events
    }
    assert fixture.fences.active == {group.dispatch_group_id}
    await fixture.close()


@pytest.mark.asyncio
async def test_redirected_carrier_deadline_keeps_confirming_without_recovery_run(
    tmp_path: Path,
) -> None:
    settle = RecordingSettleController()
    fixture = await _build_gate(
        tmp_path,
        status="redirected",
        settle=settle,
        carrier_proof_seconds=0,
    )
    decision = await _decision(fixture)
    assert decision.action is MainBoundaryAction.ABORT
    pending = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-redirected",
    )
    assert pending is not None
    assert pending.dispatch_group_id is not None
    group = await fixture.store.get_redirect_group(pending.dispatch_group_id)
    assert group is not None

    await asyncio.sleep(0.05)

    redirect = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-redirected",
    )
    assert redirect is not None
    assert redirect.state == "RPC_ACKNOWLEDGED"
    assert redirect.terminal_disposition_kind is None
    assert settle.calls == 0
    assert fixture.fences.active == {group.dispatch_group_id}
    promoted = await fixture.store.get_run(redirect.promotion_reserved_run_id)
    assert promoted is None
    await fixture.close()


@pytest.mark.asyncio
async def test_parent_terminal_fails_unproven_delivery_without_continuation_run(
    tmp_path: Path,
) -> None:
    settle = RecordingSettleController()
    fixture = await _build_gate(
        tmp_path,
        status="redirected",
        settle=settle,
        carrier_proof_seconds=0,
    )
    await _decision(fixture)

    await fixture.gate.prepare_parent_terminal(fixture.parent_run_id)

    redirect = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-redirected",
    )
    assert redirect is not None
    assert redirect.state == "SUBMISSION_UNCERTAIN"
    assert redirect.terminal_disposition_kind == "uncertain"
    assert settle.calls == 0
    promoted = await fixture.store.get_run(redirect.promotion_reserved_run_id)
    assert promoted is None
    snapshot = await fixture.store.load_run_snapshot(fixture.parent_run_id)
    assert snapshot is not None
    assert snapshot.events[-1].event_type == "followup.failed"
    assert snapshot.events[-1].payload["error_code"] == (
        "FOLLOWUP_PARENT_TERMINATED_BEFORE_DELIVERY"
    )
    await fixture.close()


@pytest.mark.asyncio
async def test_parent_terminal_continues_prepared_group_without_crossing_rpc(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="redirected")
    prepared = await fixture.store.prepare_redirect_group(
        dispatch_group_id="redirect-group-parent-terminal",
        parent_run_id=fixture.parent_run_id,
        created_at=NOW,
    )
    assert prepared is not None
    assert prepared.state == "PREPARED"

    await fixture.gate.prepare_parent_terminal(fixture.parent_run_id)

    group = await fixture.store.get_redirect_group(
        "redirect-group-parent-terminal"
    )
    assert group is not None
    assert group.state == "PROMOTED_TO_RUN"
    redirect = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-redirected",
    )
    assert redirect is not None
    assert redirect.state == "PROMOTED_TO_RUN"
    assert redirect.terminal_disposition_kind == "promoted"
    assert redirect.promoted_run_id == redirect.promotion_reserved_run_id
    promoted = await fixture.store.get_run(redirect.promoted_run_id)
    assert promoted is not None
    snapshot = await fixture.store.load_run_snapshot(fixture.parent_run_id)
    assert snapshot is not None
    assert snapshot.events[-1].event_type == "followup.continued"
    assert fixture.peer.calls == []
    await fixture.close()


@pytest.mark.asyncio
async def test_parent_terminal_rolls_back_unclaimed_intent_then_continues(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="redirected")
    fences = BlockingPreRpcEventFences(fixture.store)
    fixture.gate._event_fences = fences
    decision_task = asyncio.create_task(_decision(fixture))
    await asyncio.wait_for(fences.started.wait(), timeout=1)
    group = await fixture.store.get_unresolved_redirect_group_for_parent(
        fixture.parent_run_id
    )
    assert group is not None
    assert group.state == "DISPATCH_INTENT"

    await fixture.gate.prepare_parent_terminal(fixture.parent_run_id)
    result = await asyncio.gather(decision_task, return_exceptions=True)
    assert len(result) == 1
    assert isinstance(result[0], asyncio.CancelledError)

    finalized = await fixture.store.get_redirect_group(group.dispatch_group_id)
    assert finalized is not None
    assert finalized.state == "PROMOTED_TO_RUN"
    redirect = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-redirected",
    )
    assert redirect is not None
    assert redirect.state == "PROMOTED_TO_RUN"
    assert redirect.promoted_run_id == redirect.promotion_reserved_run_id
    assert fixture.peer.calls == []
    await fixture.close()


@pytest.mark.asyncio
async def test_carrier_before_deadline_consumes_without_interrupt(
    tmp_path: Path,
) -> None:
    settle = RecordingSettleController()
    fixture = await _build_gate(
        tmp_path,
        status="redirected",
        settle=settle,
        carrier_proof_seconds=30,
    )
    await _decision(fixture)
    pending = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-redirected",
    )
    assert pending is not None
    assert pending.dispatch_group_id is not None
    group = await fixture.store.get_redirect_group(pending.dispatch_group_id)
    assert group is not None
    _append_canonical_carrier(fixture.history)

    assert await fixture.gate.reconcile_redirect(fixture.parent_run_id) is True
    await fixture.gate.prepare_parent_stop(fixture.parent_run_id)

    assert settle.calls == 0
    assert fixture.fences.active == set()
    await fixture.close()


@pytest.mark.asyncio
async def test_final_reconcile_after_interrupt_prefers_late_canonical_proof(
    tmp_path: Path,
) -> None:
    fixture: GateFixture | None = None

    def persist_during_settle() -> None:
        assert fixture is not None
        _append_canonical_carrier(fixture.history)

    settle = RecordingSettleController(persist_during_settle)
    fixture = await _build_gate(
        tmp_path,
        status="queued",
        settle=settle,
    )
    await _decision(fixture)
    pending = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-queued",
    )
    assert pending is not None
    assert pending.dispatch_group_id is not None
    group = await fixture.store.get_redirect_group(pending.dispatch_group_id)
    assert group is not None
    await fixture.gate.prepare_parent_stop(fixture.parent_run_id)

    redirect = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-queued",
    )
    assert redirect is not None
    assert redirect.state == "CANONICAL_CONSUMED"
    assert redirect.terminal_disposition_event_seq is None
    assert settle.calls == 1
    await fixture.close()


@pytest.mark.asyncio
async def test_boot_loss_resumes_confirmation_without_resending_redirect(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="redirected")
    await _decision(fixture)
    group = await fixture.store.get_unresolved_redirect_group_for_parent(
        fixture.parent_run_id
    )
    assert group is not None
    database = fixture.store.path
    history = fixture.history
    peer_calls = list(fixture.peer.calls)
    await fixture.close()

    reopened = SQLiteStore(database)
    await reopened.start()
    settle = RecordingSettleController()
    fences = FakeEventFences(reopened)
    restarted = RedirectDurableGate(
        store=reopened,
        control_peers=ActiveRunControlPeerRegistry(),
        history_source=history,
        event_fences=fences,
        settle_controller=settle,
    )
    await restarted.start()
    await asyncio.sleep(0.05)

    recovered = await reopened.get_redirect_group(group.dispatch_group_id)
    assert recovered is not None
    assert recovered.dispatch_group_id == group.dispatch_group_id
    assert recovered.state == "ISOLATING_UNCERTAIN"
    assert settle.calls == 0
    assert peer_calls == [
        (
            "session.redirect",
            {
                "session_id": "hermes-live-1",
                "text": "use the corrected approach",
            },
        )
    ]
    assert len(await reopened.list_redirect_groups_with_release_ready_fences()) == 0
    await restarted.close()
    await reopened.close()


@pytest.mark.asyncio
async def test_cross_rpc_stop_joins_one_recovery_settle(
    tmp_path: Path,
) -> None:
    settle = RecordingSettleController()
    fixture = await _build_gate(
        tmp_path,
        status="redirected",
        settle=settle,
        carrier_proof_seconds=30,
    )
    await _decision(fixture)

    first, second = await asyncio.gather(
        fixture.gate.prepare_parent_stop(fixture.parent_run_id),
        fixture.gate.prepare_parent_stop(fixture.parent_run_id),
    )

    assert first is True
    assert second is True
    assert settle.calls == 1
    redirect = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-redirected",
    )
    assert redirect is not None
    assert redirect.state == "PROMOTED_TO_RUN"
    assert redirect.promoted_run_id == redirect.promotion_reserved_run_id
    await fixture.close()


@pytest.mark.asyncio
async def test_generic_pre_rpc_failure_rolls_back_and_forwards(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="redirected")
    fences = RaisingPreRpcEventFences()
    fixture.gate._event_fences = fences

    decision = await _decision(fixture)

    assert decision.action is MainBoundaryAction.FORWARD
    assert decision.disposition == "released"
    group = await fixture.store.get_unresolved_redirect_group_for_parent(
        fixture.parent_run_id
    )
    assert group is not None
    assert group.state == "PREPARED"
    assert fixture.peer.calls == []
    assert fences.active == set()
    assert len(fences.released) == 1
    await fixture.close()


@pytest.mark.asyncio
async def test_stop_promotes_prepared_group_without_crossing_rpc(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="redirected")
    prepared = await fixture.store.prepare_redirect_group(
        dispatch_group_id="redirect-group-stop",
        parent_run_id=fixture.parent_run_id,
        created_at=NOW,
    )
    assert prepared is not None
    assert prepared.state == "PREPARED"

    assert await fixture.gate.prepare_parent_stop(
        fixture.parent_run_id
    ) is False

    group = await fixture.store.get_redirect_group("redirect-group-stop")
    assert group is not None
    assert group.state == "PROMOTED_TO_RUN"
    redirect = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-redirected",
    )
    assert redirect is not None
    assert redirect.state == "PROMOTED_TO_RUN"
    assert redirect.promoted_run_id == redirect.promotion_reserved_run_id
    assert fixture.peer.calls == []
    assert await fixture.boundary.has_waiting_redirect(
        fixture.parent_run_id
    ) is False
    await fixture.close()


@pytest.mark.asyncio
async def test_recovered_parent_stop_reuses_already_promoted_followups(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="redirected")

    assert await fixture.gate.prepare_parent_stop(
        fixture.parent_run_id
    ) is False
    promoted = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-redirected",
    )
    assert promoted is not None
    assert promoted.state == "PROMOTED_TO_RUN"
    await fixture.store.transition_run(
        fixture.parent_run_id,
        "RECOVERING",
    )

    assert await fixture.gate.prepare_parent_stop(
        fixture.parent_run_id
    ) is False
    unchanged = await fixture.store.get_redirect_by_message(
        session_id=fixture.session_id,
        client_message_id="correction-redirected",
    )
    assert unchanged is not None
    assert unchanged.state == "PROMOTED_TO_RUN"
    assert unchanged.promoted_run_id == promoted.promoted_run_id
    await fixture.close()


@pytest.mark.asyncio
async def test_stop_preserves_three_followups_in_one_ordered_continuation(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="redirected")
    for index, content in ((2, "second follow-up"), (3, "third follow-up")):
        await fixture.store.admit_redirect(
            RedirectAdmissionIntent(
                redirect_id=f"redirect-stop-{index}",
                session_id=fixture.session_id,
                client_message_id=f"correction-stop-{index}",
                request_digest=f"sha256:{str(index) * 64}",
                parent_run_id=fixture.parent_run_id,
                interaction_origin="user_web",
                content=content,
                initial_state="WAITING_AGENT_READY",
                promotion_reserved_run_id=f"promoted-stop-{index}",
                promotion_run_intent={
                    "prepare_identity": {
                        "runtime_id": "runtime-1",
                        "session_id": fixture.session_id,
                        "profile_id": "default",
                        "model_id": "7",
                        "config_revision": "revision-1",
                        "admission_generation": 1,
                    },
                    "source": "web",
                    "prompt": content,
                    "audit_prompt": content,
                    "payload_digest": f"sha256:{str(index + 3) * 64}",
                },
                promotion_run_payload_digest=f"sha256:{str(index + 3) * 64}",
                admission_generation=1,
                created_at=NOW,
            )
        )

    assert await fixture.gate.prepare_parent_stop(fixture.parent_run_id) is False

    redirects = [
        await fixture.store.get_redirect_by_message(
            session_id=fixture.session_id,
            client_message_id=client_message_id,
        )
        for client_message_id in (
            "correction-redirected",
            "correction-stop-2",
            "correction-stop-3",
        )
    ]
    promoted_ids = {
        redirect.promoted_run_id
        for redirect in redirects
        if redirect is not None
    }
    assert promoted_ids == {"promoted-redirected"}
    promoted = await fixture.store.get_run("promoted-redirected")
    assert promoted is not None
    assert promoted.prompt == (
        "use the corrected approach\n\n[Additional user correction]\n"
        "second follow-up\n\n[Additional user correction]\nthird follow-up"
    )
    await fixture.close()


@pytest.mark.asyncio
async def test_stop_preserves_more_than_one_dispatch_group_in_one_continuation(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="redirected")
    contents = ["use the corrected approach"]
    for index in range(2, 36):
        content = f"follow-up {index:02d}"
        contents.append(content)
        digest = f"sha256:{index:064x}"
        await fixture.store.admit_redirect(
            RedirectAdmissionIntent(
                redirect_id=f"redirect-many-{index}",
                session_id=fixture.session_id,
                client_message_id=f"correction-many-{index}",
                request_digest=digest,
                parent_run_id=fixture.parent_run_id,
                interaction_origin="user_web",
                content=content,
                initial_state="WAITING_AGENT_READY",
                promotion_reserved_run_id=f"promoted-many-{index}",
                promotion_run_intent={
                    "prepare_identity": {
                        "runtime_id": "runtime-1",
                        "session_id": fixture.session_id,
                        "profile_id": "default",
                        "model_id": "7",
                        "config_revision": "revision-1",
                        "admission_generation": 1,
                    },
                    "source": "web",
                    "prompt": content,
                    "audit_prompt": content,
                    "payload_digest": digest,
                },
                promotion_run_payload_digest=digest,
                admission_generation=1,
                created_at=NOW,
            )
        )

    assert await fixture.gate.prepare_parent_stop(fixture.parent_run_id) is False

    history = await fixture.store.list_session_followup_history(
        fixture.session_id
    )
    assert len(history) == 35
    assert {entry.promoted_run_id for entry in history} == {
        "promoted-redirected"
    }
    promoted = await fixture.store.get_run("promoted-redirected")
    assert promoted is not None
    assert promoted.prompt == "\n\n[Additional user correction]\n".join(contents)
    assert promoted.source_detail is not None
    assert promoted.source_detail["followup_member_count"] == 35
    await fixture.close()


@pytest.mark.asyncio
async def test_pre_rpc_peer_loss_rolls_back_intent_and_forwards_normally(
    tmp_path: Path,
) -> None:
    fixture = await _build_gate(tmp_path, status="redirected")
    fixture.peer.closed = True
    decision = await _decision(fixture)

    assert decision.action is MainBoundaryAction.FORWARD
    group = await fixture.store.get_unresolved_redirect_group_for_parent(
        fixture.parent_run_id
    )
    assert group is not None
    assert group.state == "PREPARED"
    assert fixture.peer.calls == []
    assert fixture.fences.active == set()
    await fixture.close()


def test_v0201_history_proof_requires_exact_renderer_and_stable_anchor() -> None:
    before = {
        "session_id": "live-1",
        "messages": [
            {
                "id": 1,
                "session_id": "live-1",
                "role": "user",
                "content": "original",
                "active": 1,
                "compacted": 0,
            }
        ],
    }
    anchor = HermesV0201RedirectHistoryVerifier.anchor(before)
    wire_text = "use postgres"
    after = {
        "session_id": "live-1",
        "messages": [
            *before["messages"],
            {
                "id": 2,
                "session_id": "live-1",
                "role": "assistant",
                "content": "I would use SQLite",
                "active": 1,
                "compacted": 0,
            },
            {
                "id": 3,
                "session_id": "live-1",
                "role": "user",
                "content": wire_text,
                "api_content": (
                    "[Context from the interrupted assistant response]\n"
                    "[This response was interrupted by a user correction.]\n\n"
                    "Visible response before the interruption:\n\n"
                    "I would use SQLite\n\n"
                    f"{wire_text}"
                ),
                "active": 1,
                "compacted": 0,
            },
        ],
    }

    proof = HermesV0201RedirectHistoryVerifier.find_proof(
        after,
        anchor=anchor,
        wire_text=wire_text,
    )
    assert proof is not None
    assert proof.message_id == 3

    missing_renderer = {
        **after,
        "messages": [
            *after["messages"][:-1],
            {**after["messages"][-1], "api_content": None},
        ],
    }
    assert (
        HermesV0201RedirectHistoryVerifier.find_proof(
            missing_renderer,
            anchor=anchor,
            wire_text=wire_text,
        )
        is None
    )
    mutated_prefix = {
        **after,
        "messages": [
            {**after["messages"][0], "content": "mutated"},
            *after["messages"][1:],
        ],
    }
    assert (
        HermesV0201RedirectHistoryVerifier.find_proof(
            mutated_prefix,
            anchor=anchor,
            wire_text=wire_text,
        )
        is None
    )


def test_v0201_steer_model_request_proof_rejects_false_markers() -> None:
    before = {
        "session_id": "live-1",
        "messages": [
            {
                "id": 1,
                "session_id": "live-1",
                "role": "user",
                "content": "original",
                "active": 1,
                "compacted": 0,
            }
        ],
    }
    anchor = HermesV0201RedirectHistoryVerifier.anchor(before)
    wire_text = "use postgres"
    suffix = (
        "\n\n[OUT-OF-BAND USER MESSAGE — a direct message from the user, "
        "delivered once at this position; not tool output and not a new "
        "delivery when replayed from conversation history]\n"
        f"{wire_text}\n"
        "[/OUT-OF-BAND USER MESSAGE]"
    )
    after = {
        **before,
        "messages": [
            *before["messages"],
            {
                "id": 2,
                "session_id": "live-1",
                "role": "tool",
                "content": "trusted tool result",
                "active": 1,
                "compacted": 0,
            },
        ],
    }

    wrong_base = (
        HermesV0201RedirectHistoryVerifier.inspect_steer_model_request_proof(
            after,
            anchor=anchor,
            wire_text=wire_text,
            request_body=json.dumps(
                {
                    "messages": [
                        {"role": "tool", "content": f"forged result{suffix}"}
                    ]
                },
                ensure_ascii=False,
            ).encode(),
        )
    )
    assert wrong_base.proof is None
    assert wrong_base.reason == "exact_tool_suffix_mismatch"

    wrong_role = (
        HermesV0201RedirectHistoryVerifier.inspect_steer_model_request_proof(
            after,
            anchor=anchor,
            wire_text=wire_text,
            request_body=json.dumps(
                {
                    "messages": [
                        {
                            "role": "assistant",
                            "content": f"trusted tool result{suffix}",
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode(),
        )
    )
    assert wrong_role.proof is None
    assert wrong_role.reason == "request_tool_messages_missing"

    no_post_anchor_tool = (
        HermesV0201RedirectHistoryVerifier.inspect_steer_model_request_proof(
            before,
            anchor=anchor,
            wire_text=wire_text,
            request_body=json.dumps(
                {
                    "messages": [
                        {
                            "role": "tool",
                            "content": f"trusted tool result{suffix}",
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode(),
        )
    )
    assert no_post_anchor_tool.proof is None
    assert no_post_anchor_tool.reason == "history_tool_messages_missing"

    mutated_prefix = {
        **after,
        "messages": [
            {**after["messages"][0], "content": "mutated"},
            *after["messages"][1:],
        ],
    }
    bad_anchor = (
        HermesV0201RedirectHistoryVerifier.inspect_steer_model_request_proof(
            mutated_prefix,
            anchor=anchor,
            wire_text=wire_text,
            request_body=json.dumps(
                {
                    "messages": [
                        {
                            "role": "tool",
                            "content": f"trusted tool result{suffix}",
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode(),
        )
    )
    assert bad_anchor.proof is None
    assert bad_anchor.reason == "history_prefix_mismatch"
