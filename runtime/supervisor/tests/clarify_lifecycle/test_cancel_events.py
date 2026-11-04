from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from platform_supervisor.execution import (
    CapacityGate,
    PrepareIdentity,
    RunManager,
    SessionManager,
)
from platform_supervisor.hermes.events import PlatformStreamEvent
from platform_supervisor.storage import SQLiteStore, StoredRunEvent


def _identity() -> PrepareIdentity:
    return PrepareIdentity(
        runtime_id="runtime-clarify-1",
        session_id="session-clarify-0001",
        profile_id="default",
        model_id="7",
        config_revision="revision-19",
        admission_generation=11,
    )


class CancelRaisesAdapter:
    def __init__(
        self,
        *,
        emit_marker_first: bool = False,
        terminate_by_cancel: bool = False,
    ) -> None:
        self.entered = asyncio.Event()
        self.prompt_started = asyncio.Event()
        self.release = asyncio.Event()
        self.emit_marker_first = emit_marker_first
        self.terminate_by_cancel = terminate_by_cancel

    async def stream(
        self,
        *,
        identity: PrepareIdentity,
        run_id: str,
        prompt: str,
        trusted_context=None,
        attachments=(),
        prompt_submit_started_sink=None,
        prompt_ack_sink=None,
        runtime_provider_run_id_sink=None,
    ) -> AsyncIterator[PlatformStreamEvent]:
        del prompt, trusted_context, attachments, prompt_ack_sink
        self.entered.set()
        if runtime_provider_run_id_sink is not None:
            await runtime_provider_run_id_sink(f"live-{run_id}")
        if prompt_submit_started_sink is not None:
            started = prompt_submit_started_sink()
            if started is not None:
                await started
        self.prompt_started.set()
        yield PlatformStreamEvent(
            "chat.run.started",
            {"run_id": f"live-{run_id}"},
        )
        if self.emit_marker_first:
            yield PlatformStreamEvent(
                "chat.clarify.requested",
                {"request_id": "clr_preexisting"},
            )
        await self.release.wait()
        if not self.terminate_by_cancel:
            yield PlatformStreamEvent(
                "chat.done",
                {"run_id": f"live-{run_id}", "status": "canceled"},
            )
            return
        raise asyncio.CancelledError

    async def cancel(
        self,
        identity: PrepareIdentity,
        *,
        runtime_provider_run_id: str | None = None,
    ) -> None:
        del identity, runtime_provider_run_id
        self.release.set()


async def _build_manager(
    tmp_path: Path,
    *,
    emit_marker_first: bool,
    terminate_by_cancel: bool = False,
) -> tuple[SQLiteStore, SessionManager, RunManager, CancelRaisesAdapter]:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()

    async def prepare(identity: PrepareIdentity) -> None:
        del identity

    async def close(identity: PrepareIdentity) -> None:
        del identity

    sessions = SessionManager(
        store,
        runtime_id="runtime-clarify-1",
        max_inflight_runs=1,
        max_resident_sessions=2,
        post_run_warm_grace_seconds=600,
        hot_session_limit=0,
        hot_session_ttl_seconds=900,
        prepare_callback=prepare,
        close_callback=close,
    )
    adapter = CancelRaisesAdapter(
        emit_marker_first=emit_marker_first,
        terminate_by_cancel=terminate_by_cancel,
    )
    manager = RunManager(store, sessions, CapacityGate(1), adapter)
    return store, sessions, manager, adapter


async def _submit_and_cancel(
    manager: RunManager,
    adapter: CancelRaisesAdapter,
    *,
    clarify_request_id: str | None,
) -> str:
    selected = _identity()
    submission = await manager.submit(
        identity=selected,
        client_message_id="message-clarify-cancel",
        payload_digest="a" * 64,
        prompt="CLARIFY_PROBE",
        source="web",
    )
    await asyncio.wait_for(adapter.prompt_started.wait(), timeout=5)
    assert (
        await manager.cancel(
            submission.run.run_id,
            clarify_request_id=clarify_request_id,
        )
        == "accepted"
    )
    context = manager._contexts[submission.run.run_id]
    assert context.task is not None
    await context.task
    return submission.run.run_id


async def _events(store: SQLiteStore, run_id: str) -> list[StoredRunEvent]:
    snapshot = await store.load_run_snapshot(run_id)
    assert snapshot is not None
    return list(snapshot.events)


@pytest.mark.asyncio
async def test_clarify_cancel_records_authority_event_before_canceled_done(
    tmp_path: Path,
) -> None:
    store, sessions, manager, adapter = await _build_manager(
        tmp_path,
        emit_marker_first=False,
    )
    try:
        run_id = await _submit_and_cancel(
            manager,
            adapter,
            clarify_request_id="clr_12345678",
        )

        events = await _events(store, run_id)
        assert [(item.seq, item.event_type) for item in events] == [
            (1, "chat.run.started"),
            (2, "chat.clarify.requested"),
            (3, "chat.done"),
        ]
        assert events[1].payload == {"request_id": "clr_12345678"}
        assert events[-1].payload["status"] == "canceled"
        projections = await store.load_projection_batch(max_items=100)
        clarify_projection = next(
            item
            for item in projections
            if item.projection_id == f"{run_id}:event:2"
        )
        assert clarify_projection.projection_type == "run.event"
        assert clarify_projection.payload["event_type"] == (
            "chat.clarify.requested"
        )
        assert clarify_projection.payload["event_data"] == {
            "request_id": "clr_12345678"
        }
        stored = await store.get_run(run_id)
        assert stored is not None and stored.state == "CANCELED"
    finally:
        await manager.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_normal_cancel_does_not_record_clarify_event(
    tmp_path: Path,
) -> None:
    store, sessions, manager, adapter = await _build_manager(
        tmp_path,
        emit_marker_first=False,
    )
    try:
        run_id = await _submit_and_cancel(
            manager,
            adapter,
            clarify_request_id=None,
        )

        events = await _events(store, run_id)
        assert [item.event_type for item in events] == [
            "chat.run.started",
            "chat.done",
        ]
        assert events[-1].payload["status"] == "canceled"
    finally:
        await manager.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_dashboard_marker_wins_without_duplicate_event(
    tmp_path: Path,
) -> None:
    store, sessions, manager, adapter = await _build_manager(
        tmp_path,
        emit_marker_first=True,
    )
    try:
        run_id = await _submit_and_cancel(
            manager,
            adapter,
            clarify_request_id="clr_from_cancel",
        )

        events = await _events(store, run_id)
        clarify_events = [
            item for item in events if item.event_type == "chat.clarify.requested"
        ]
        assert len(clarify_events) == 1
        assert clarify_events[0].payload == {"request_id": "clr_preexisting"}
        assert events[-1].event_type == "chat.done"
        assert events[-1].payload["status"] == "canceled"
    finally:
        await manager.close()
        await sessions.close()
        await store.close()


@pytest.mark.asyncio
async def test_clarify_cancelled_stream_records_authority_event(
    tmp_path: Path,
) -> None:
    store, sessions, manager, adapter = await _build_manager(
        tmp_path,
        emit_marker_first=False,
        terminate_by_cancel=True,
    )
    try:
        run_id = await _submit_and_cancel(
            manager,
            adapter,
            clarify_request_id="clr_stream_cancelled",
        )

        events = await _events(store, run_id)
        assert [(item.seq, item.event_type) for item in events] == [
            (1, "chat.run.started"),
            (2, "chat.clarify.requested"),
            (3, "chat.done"),
        ]
        assert events[1].payload == {"request_id": "clr_stream_cancelled"}
        assert events[-1].payload["status"] == "canceled"
    finally:
        await manager.close()
        await sessions.close()
        await store.close()
