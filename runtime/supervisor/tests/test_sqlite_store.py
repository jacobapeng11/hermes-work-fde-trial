from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

import pytest

from platform_supervisor.storage import (
    DispatchIntent,
    IdempotencyConflict,
    RedirectAdmissionIntent,
    RunEnqueueIntent,
    SQLiteStore,
    SQLiteStoreError,
    StoredRunEvent,
)


def intent(index: int, *, digest: str | None = None) -> DispatchIntent:
    return DispatchIntent(
        run_id=f"run-{index:08d}",
        session_id=f"session-{index:08d}",
        client_message_id=f"message-{index:08d}",
        payload_digest=digest or f"{index:064x}",
        config_revision="revision-18",
        source="web",
        accepted_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
    )


def queued_intent(
    index: int,
    *,
    session_id: str = "session-queued",
    source: str = "web",
    scheduled_task_id: int | None = None,
    admission_generation: int = 11,
) -> RunEnqueueIntent:
    return RunEnqueueIntent(
        run_id=f"queued-run-{index:08d}",
        session_id=session_id,
        client_message_id=f"queued-message-{index:08d}",
        payload_digest=f"{index:064x}",
        config_revision="revision-18",
        source=source,
        enqueued_at=datetime(2026, 8, 14, 9, index, tzinfo=timezone.utc),
        prompt=f"prompt-{index}",
        audit_prompt=f"prompt-{index}",
        identity={
            "runtime_id": "application-runtime-1",
            "session_id": session_id,
            "profile_id": "default",
            "model_id": "7",
            "config_revision": "revision-18",
            "admission_generation": admission_generation,
        },
        source_detail=(
            {
                "type": "scheduled",
                "scheduled_task_id": scheduled_task_id,
                "scheduled_task_name": f"task-{scheduled_task_id}",
                "trigger_kind": "scheduled",
                "task_available": True,
            }
            if scheduled_task_id is not None
            else None
        ),
        scheduled_task_id=scheduled_task_id,
        scheduled_occurrence_key=(
            f"scheduled:{scheduled_task_id}:2026-08-14T09:{index:02d}:00Z"
            if scheduled_task_id is not None
            else None
        ),
    )


def redirect_intent(
    index: int,
    *,
    parent_run_id: str,
    session_id: str,
    content: str,
    initial_state: str = "WAITING_REDIRECT_BOUNDARY",
    source_detail: dict[str, object] | None = None,
) -> RedirectAdmissionIntent:
    return RedirectAdmissionIntent(
        redirect_id=f"00000000-0000-4000-8000-{index:012d}",
        session_id=session_id,
        client_message_id=f"redirect-message-{index}",
        request_digest=f"sha256:{index:064x}",
        parent_run_id=parent_run_id,
        interaction_origin="user_web",
        content=content,
        initial_state=initial_state,
        promotion_reserved_run_id=f"promoted-run-{index}",
        promotion_run_intent={
            "prepare_identity": {
                "runtime_id": "application-runtime-1",
                "session_id": session_id,
                "profile_id": "default",
                "model_id": "7",
                "config_revision": "revision-18",
            },
            "prompt": content,
            "source_detail": source_detail,
        },
        promotion_run_payload_digest=f"sha256:{index + 100:064x}",
        admission_generation=11,
        created_at=datetime(2026, 8, 14, 10, index, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_queued_runs_are_durable_and_claimed_in_fifo_order(
    tmp_path: Path,
) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()

    first = await store.enqueue_run(queued_intent(1))
    second = await store.enqueue_run(queued_intent(2))

    assert first.state == "QUEUED"
    assert second.state == "QUEUED"
    assert first.enqueue_seq < second.enqueue_seq
    assert (await store.queue_summary("session-queued")) == {
        "queued_count": 2,
        "scheduled_queued_count": 0,
        "queued_runs": [
            {
                "run_id": first.run_id,
                "client_message_id": first.client_message_id,
                "enqueue_seq": first.enqueue_seq,
                "prompt": "prompt-1",
                "attachment_file_ids": [],
            },
            {
                "run_id": second.run_id,
                "client_message_id": second.client_message_id,
                "enqueue_seq": second.enqueue_seq,
                "prompt": "prompt-2",
                "attachment_file_ids": [],
            },
        ],
        "active_run": None,
    }

    claimed = await store.claim_next_queued(
        "session-queued",
        accepted_at=datetime(2026, 8, 14, 10, tzinfo=timezone.utc),
    )
    assert claimed is not None
    assert claimed.run_id == first.run_id
    assert claimed.state == "ADMITTING"

    await store.close()
    reopened = SQLiteStore(database)
    await reopened.start()
    restored = await reopened.get_run(second.run_id)
    assert restored is not None
    assert restored.state == "QUEUED"
    assert restored.enqueue_seq == second.enqueue_seq
    await reopened.close()


@pytest.mark.asyncio
async def test_scheduled_queue_coalesces_one_unstarted_candidate_per_task(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()

    first = await store.enqueue_run(
        queued_intent(1, source="scheduled_task", scheduled_task_id=41)
    )
    repeated = await store.enqueue_run(
        queued_intent(2, source="scheduled_task", scheduled_task_id=41)
    )

    assert repeated.run_id == first.run_id
    assert repeated.created is False
    assert repeated.coalesced_count == 1
    assert (await store.queue_summary("session-queued")) == {
        "queued_count": 1,
        "scheduled_queued_count": 1,
        "queued_runs": [],
        "active_run": None,
    }
    await store.close()


@pytest.mark.asyncio
async def test_queue_summary_projects_web_attachments_and_active_cancel_state(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    active = await store.enqueue_run(queued_intent(1))
    queued = await store.enqueue_run(
        replace(
            queued_intent(2),
            source_detail={"attachment_file_ids": [7, "8", None]},
        )
    )
    claimed = await store.claim_next_queued(
        active.session_id,
        accepted_at=datetime(2026, 8, 14, 10, tzinfo=timezone.utc),
    )
    assert claimed is not None
    marked = await store.request_run_cancellation(
        active.run_id,
        requested_at=datetime(2026, 8, 14, 10, 1, tzinfo=timezone.utc),
    )
    assert marked.cancellation_requested_at is not None

    summary = await store.queue_summary(active.session_id)

    assert summary["queued_runs"] == [{
        "run_id": queued.run_id,
        "client_message_id": queued.client_message_id,
        "enqueue_seq": queued.enqueue_seq,
        "prompt": "prompt-2",
        "attachment_file_ids": ["7", "8"],
    }]
    assert summary["active_run"] == {
        "run_id": active.run_id,
        "state": "ADMITTING",
        "cancellation_requested_at": "2026-08-14T10:01:00+00:00",
    }
    await store.close()


@pytest.mark.asyncio
async def test_scheduled_history_checkpoint_and_queued_cancellation_are_durable(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    queued = await store.enqueue_run(
        queued_intent(1, source="scheduled_task", scheduled_task_id=41)
    )
    claimed = await store.claim_next_queued(
        queued.session_id,
        accepted_at=datetime(2026, 8, 14, 10, tzinfo=timezone.utc),
    )
    assert claimed is not None
    checkpointed = await store.record_run_history_checkpoint(
        claimed.run_id,
        history_message_count_before_run=2,
    )
    assert checkpointed.history_message_count_before_run == 2
    assert (await store.session_history_sources(queued.session_id))[2][
        "scheduled_task_name"
    ] == "task-41"
    assert await store.cancel_queued_scheduled_task(
        session_id=queued.session_id,
        scheduled_task_id=41,
    ) == "already_running"

    await store.transition_run(
        claimed.run_id,
        "COMPLETED",
        terminal_at=datetime(2026, 8, 14, 10, 1, tzinfo=timezone.utc),
    )
    await store.enqueue_run(
        queued_intent(2, source="scheduled_task", scheduled_task_id=41)
    )
    assert await store.cancel_queued_scheduled_task(
        session_id=queued.session_id,
        scheduled_task_id=41,
    ) == "canceled"
    assert (await store.queue_summary(queued.session_id))["queued_count"] == 0
    terminal = [
        item
        for item in await store.load_projection_batch(max_items=20)
        if item.projection_id.endswith(":terminal")
    ]
    assert terminal[-1].payload["state"] == "CANCELED"
    await store.close()


@pytest.mark.asyncio
async def test_store_uses_wal_full_and_omits_retired_upgrade_ledgers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()

    assert await store.quick_check()

    await store.close()
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "runtime_meta",
            "session_envelopes",
            "runs",
            "run_events",
            "internal_commands",
            "projection_outbox",
            "projection_checkpoints",
            "revocations",
            "channel_delivery_checkpoints",
            "input_admission_counter",
            "input_admission_reservations",
            "run_redirects",
            "run_redirect_dispatch_groups",
            "redirect_event_fences",
            "redirect_event_fence_snapshots",
            "channel_fast_lane_v2_deliveries",
            "channel_fast_lane_v2_checkpoints",
        }.issubset(tables)
        assert {
            "admission_barrier",
            "admission_generation",
            "admission_control_operations",
            "legacy_source_gate_progress",
            "legacy_source_gate_transitions",
        }.isdisjoint(tables)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_store_removes_temporary_overlong_metric_trigger(
    tmp_path: Path,
) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()
    await store.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER platform_ack_overlong_run_metric_v1
            AFTER INSERT ON projection_outbox
            BEGIN
                SELECT 1;
            END
            """
        )
        connection.commit()

    restarted = SQLiteStore(database)
    await restarted.start()
    await restarted.close()
    with sqlite3.connect(database) as connection:
        trigger = connection.execute(
            """
            SELECT name
            FROM sqlite_schema
            WHERE type = 'trigger'
              AND name = 'platform_ack_overlong_run_metric_v1'
            """
        ).fetchone()
    assert trigger is None


@pytest.mark.asyncio
async def test_redirect_admission_is_durable_idempotent_and_ordered(
    tmp_path: Path,
) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()
    parent = await store.persist_dispatch_intent(intent(1))
    created_at = datetime(2026, 8, 14, 10, tzinfo=timezone.utc)
    redirect_intent = RedirectAdmissionIntent(
        redirect_id="11111111-1111-4111-8111-111111111111",
        session_id=parent.session_id,
        client_message_id="redirect-message-1",
        request_digest=f"sha256:{'a' * 64}",
        parent_run_id=parent.run_id,
        interaction_origin="user_web",
        content="请改用更简洁的实现",
        initial_state="WAITING_REDIRECT_BOUNDARY",
        promotion_reserved_run_id="promoted-run-1",
        promotion_run_intent={
            "prepare_identity": {
                "runtime_id": "application-runtime-1",
                "session_id": parent.session_id,
                "profile_id": "default",
                "model_id": "7",
                "config_revision": "revision-18",
            },
            "prompt": "请改用更简洁的实现",
        },
        promotion_run_payload_digest=f"sha256:{'b' * 64}",
        admission_generation=11,
        created_at=created_at,
    )

    redirect = await store.admit_redirect(redirect_intent)
    repeated = await store.admit_redirect(redirect_intent)
    queued = await store.enqueue_run(
        queued_intent(2, session_id=parent.session_id)
    )

    assert redirect.created is True
    assert repeated.created is False
    assert repeated.redirect_id == redirect.redirect_id
    assert parent.enqueue_seq < redirect.input_admission_seq < queued.enqueue_seq
    reservations = await store.list_input_admission_reservations(
        parent.session_id,
        include_completed=True,
    )
    assert [item.kind for item in reservations] == [
        "ordinary_run",
        "redirect",
        "ordinary_run",
    ]
    assert [item.state for item in reservations] == [
        "completed",
        "bound",
        "bound",
    ]
    snapshot = await store.load_run_snapshot(parent.run_id)
    assert snapshot is not None
    assert snapshot.events[-1].event_type == "followup.received"
    assert snapshot.events[-1].seq == redirect.pending_event_seq
    assert snapshot.events[-1].payload["client_message_id"] == (
        redirect.client_message_id
    )
    await store.transition_run(
        parent.run_id,
        "COMPLETED",
        terminal_at=datetime(2026, 8, 14, 10, 1, tzinfo=timezone.utc),
    )
    assert (
        await store.claim_next_queued(
            parent.session_id,
            accepted_at=datetime(2026, 8, 14, 10, 2, tzinfo=timezone.utc),
        )
        is None
    )
    still_queued = await store.get_run(queued.run_id)
    assert still_queued is not None and still_queued.state == "QUEUED"
    await store.close()

    reopened = SQLiteStore(database)
    await reopened.start()
    restored = await reopened.get_redirect_by_message(
        session_id=parent.session_id,
        client_message_id=redirect.client_message_id,
    )
    assert restored is not None
    assert restored.input_admission_seq == redirect.input_admission_seq
    assert restored.promotion_run_intent == redirect.promotion_run_intent
    history_entries = await reopened.list_session_followup_history(
        parent.session_id
    )
    assert [entry.client_message_id for entry in history_entries] == [
        redirect.client_message_id
    ]
    assert history_entries[0].content == "请改用更简洁的实现"
    assert history_entries[0].state == "WAITING_REDIRECT_BOUNDARY"
    await reopened.close()


@pytest.mark.asyncio
async def test_redirect_admission_rejects_cross_table_idempotency_conflict(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    parent = await store.persist_dispatch_intent(intent(1))
    ordinary = await store.enqueue_run(
        queued_intent(2, session_id=parent.session_id)
    )
    with pytest.raises(IdempotencyConflict, match="ordinary Run"):
        await store.admit_redirect(
            RedirectAdmissionIntent(
                redirect_id="22222222-2222-4222-8222-222222222222",
                session_id=parent.session_id,
                client_message_id=ordinary.client_message_id,
                request_digest=f"sha256:{'c' * 64}",
                parent_run_id=parent.run_id,
                interaction_origin="user_web",
                content="same id",
                initial_state="WAITING_AGENT_READY",
                promotion_reserved_run_id="promoted-run-2",
                promotion_run_intent={"prompt": "same id"},
                promotion_run_payload_digest=f"sha256:{'d' * 64}",
                admission_generation=11,
                created_at=datetime(2026, 8, 14, 10, tzinfo=timezone.utc),
            )
        )
    await store.close()


@pytest.mark.asyncio
async def test_redirect_group_dispatch_ack_and_canonical_proof_are_ordered(
    tmp_path: Path,
) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()
    parent = await store.persist_dispatch_intent(intent(1))
    first = await store.admit_redirect(
        redirect_intent(
            2,
            parent_run_id=parent.run_id,
            session_id=parent.session_id,
            content="  第一条修正",
        )
    )
    second = await store.admit_redirect(
        redirect_intent(
            1,
            parent_run_id=parent.run_id,
            session_id=parent.session_id,
            content="第二条修正  ",
        )
    )
    assert first.input_admission_seq < second.input_admission_seq

    group = await store.prepare_redirect_group(
        dispatch_group_id="33333333-3333-4333-8333-333333333333",
        parent_run_id=parent.run_id,
        created_at=datetime(2026, 8, 14, 11, tzinfo=timezone.utc),
    )
    assert group is not None
    assert group.member_count == 2
    assert group.wire_text == (
        "第一条修正\n\n[Additional user correction]\n第二条修正"
    )

    snapshot = await store.load_run_snapshot(parent.run_id)
    assert snapshot is not None
    await store.record_redirect_dispatch_intent(
        dispatch_group_id=group.dispatch_group_id,
        hermes_profile_key="default",
        hermes_stored_session_id="hermes-session-1",
        pre_dispatch_message_id=41,
        pre_dispatch_history_digest=f"sha256:{'1' * 64}",
        pre_dispatch_model_request_seq=7,
        pre_dispatch_model_request_digest=f"sha256:{'2' * 64}",
        dispatch_turn_id="turn-1",
        dispatch_turn_task_id="hermes-session-1",
        dispatch_origin_evidence_digest=f"sha256:{'3' * 64}",
        dispatch_boundary_epoch_nonce="boundary-1",
        dispatch_marker_request_nonce="marker-1",
        dispatch_peer_boot_id="44444444-4444-4444-8444-444444444444",
        dispatch_peer_connection_id="connection-1",
        pre_fence_event_seq=snapshot.events[-1].seq,
        pre_fence_token_snapshot_digest=f"sha256:{'4' * 64}",
        recorded_at=datetime(2026, 8, 14, 11, 1, tzinfo=timezone.utc),
    )
    acknowledged = await store.acknowledge_redirect_group(
        group.dispatch_group_id,
        hermes_status="redirected",
        acknowledged_at=datetime(2026, 8, 14, 11, 2, tzinfo=timezone.utc),
    )
    assert acknowledged.state == "RPC_ACKNOWLEDGED"

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT state FROM redirect_event_fences WHERE dispatch_group_id = ?",
            (group.dispatch_group_id,),
        ).fetchone()[0] == "ACTIVE"
    finally:
        connection.close()

    consumed = await store.consume_redirect_group(
        group.dispatch_group_id,
        proof_message_id=42,
        proof_post_digest=f"sha256:{'5' * 64}",
        consumed_at=datetime(2026, 8, 14, 11, 3, tzinfo=timezone.utc),
    )
    assert consumed.state == "CANONICAL_CONSUMED"
    assert consumed.proof_message_id == 42
    assert await store.complete_redirect_event_fence_release(
        group.dispatch_group_id
    ) is True
    assert await store.complete_redirect_event_fence_release(
        group.dispatch_group_id
    ) is False
    members = [
        await store.get_redirect_by_message(
            session_id=parent.session_id,
            client_message_id=item.client_message_id,
        )
        for item in (first, second)
    ]
    assert all(item is not None for item in members)
    assert [item.dispatch_group_position for item in members if item] == [0, 1]
    assert all(item.accepted_event_seq is not None for item in members if item)
    assert all(item.consumed_event_seq is not None for item in members if item)
    history_entries = await store.list_session_followup_history(
        parent.session_id
    )
    assert [entry.client_message_id for entry in history_entries] == [
        first.client_message_id,
        second.client_message_id,
    ]
    assert all(
        entry.group_proof_message_id == 42 for entry in history_entries
    )
    assert all(
        entry.group_proof_kind == "correction_user_row"
        for entry in history_entries
    )
    assert all(entry.group_wire_text == group.wire_text for entry in history_entries)
    assert await store.list_input_admission_reservations(parent.session_id) == []
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM redirect_event_fences WHERE dispatch_group_id = ?",
            (group.dispatch_group_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM redirect_event_fence_snapshots WHERE dispatch_group_id = ?",
            (group.dispatch_group_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()
    final_snapshot = await store.load_run_snapshot(parent.run_id)
    assert final_snapshot is not None
    assert [event.event_type for event in final_snapshot.events] == [
        "followup.received",
        "followup.received",
        "followup.applying",
        "followup.applying",
        "followup.applied",
        "followup.applied",
    ]
    await store.close()


@pytest.mark.asyncio
async def test_late_followup_proof_links_the_existing_continuation_run(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    continuation = await store.persist_dispatch_intent(intent(1))
    late = await store.admit_redirect(
        redirect_intent(
            9,
            parent_run_id=continuation.run_id,
            session_id=continuation.session_id,
            content="late but still applicable",
            source_detail={
                "followup_original_parent_run_id": "run-original-parent",
                "followup_continuation_run_id": continuation.run_id,
            },
        )
    )
    group = await store.prepare_redirect_group(
        dispatch_group_id="group-late-existing-continuation",
        parent_run_id=continuation.run_id,
        created_at=datetime(2026, 8, 14, 11, tzinfo=timezone.utc),
    )
    assert group is not None
    snapshot = await store.load_run_snapshot(continuation.run_id)
    assert snapshot is not None
    await store.record_redirect_dispatch_intent(
        dispatch_group_id=group.dispatch_group_id,
        hermes_profile_key="default",
        hermes_stored_session_id="hermes-session-1",
        pre_dispatch_message_id=41,
        pre_dispatch_history_digest=f"sha256:{'1' * 64}",
        pre_dispatch_model_request_seq=7,
        pre_dispatch_model_request_digest=f"sha256:{'2' * 64}",
        dispatch_turn_id="turn-late",
        dispatch_turn_task_id="task-late",
        dispatch_origin_evidence_digest=f"sha256:{'3' * 64}",
        dispatch_boundary_epoch_nonce="boundary-late",
        dispatch_marker_request_nonce="marker-late",
        dispatch_peer_boot_id="44444444-4444-4444-8444-444444444444",
        dispatch_peer_connection_id="connection-late",
        pre_fence_event_seq=snapshot.events[-1].seq,
        pre_fence_token_snapshot_digest=f"sha256:{'4' * 64}",
        recorded_at=datetime(2026, 8, 14, 11, 1, tzinfo=timezone.utc),
    )
    await store.acknowledge_redirect_group(
        group.dispatch_group_id,
        hermes_status="redirected",
        acknowledged_at=datetime(2026, 8, 14, 11, 2, tzinfo=timezone.utc),
    )
    await store.consume_redirect_group(
        group.dispatch_group_id,
        proof_message_id=42,
        proof_post_digest=f"sha256:{'5' * 64}",
        consumed_at=datetime(2026, 8, 14, 11, 3, tzinfo=timezone.utc),
    )

    resolved = await store.get_redirect_by_message(
        session_id=continuation.session_id,
        client_message_id=late.client_message_id,
    )
    assert resolved is not None
    assert resolved.state == "PROMOTED_TO_RUN"
    assert resolved.promoted_run_id == continuation.run_id
    assert await store.get_run(late.promotion_reserved_run_id) is None
    snapshot = await store.load_run_snapshot(continuation.run_id)
    assert snapshot is not None
    assert [event.event_type for event in snapshot.events] == [
        "followup.received",
        "followup.applying",
        "followup.continued",
    ]
    assert snapshot.events[-1].payload["parent_run_id"] == "run-original-parent"
    assert snapshot.events[-1].payload["continuation_run_id"] == continuation.run_id
    history = await store.list_session_followup_history(continuation.session_id)
    assert history[0].parent_run_id == "run-original-parent"
    assert history[0].promoted_run_id == continuation.run_id
    await store.close()


@pytest.mark.asyncio
async def test_redirect_uncertain_is_one_atomic_post_settle_terminal(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    parent = await store.persist_dispatch_intent(intent(1))
    redirect = await store.admit_redirect(
        redirect_intent(
            1,
            parent_run_id=parent.run_id,
            session_id=parent.session_id,
            content="uncertain correction",
        )
    )
    group = await store.prepare_redirect_group(
        dispatch_group_id="44444444-4444-4444-8444-444444444444",
        parent_run_id=parent.run_id,
        created_at=datetime(2026, 8, 14, 11, tzinfo=timezone.utc),
    )
    assert group is not None
    snapshot = await store.load_run_snapshot(parent.run_id)
    assert snapshot is not None
    dispatched = await store.record_redirect_dispatch_intent(
        dispatch_group_id=group.dispatch_group_id,
        hermes_profile_key="default",
        hermes_stored_session_id="hermes-session-1",
        pre_dispatch_message_id=41,
        pre_dispatch_history_digest=f"sha256:{'1' * 64}",
        pre_dispatch_model_request_seq=7,
        pre_dispatch_model_request_digest=f"sha256:{'2' * 64}",
        dispatch_turn_id="turn-1",
        dispatch_turn_task_id="hermes-session-1",
        dispatch_origin_evidence_digest=f"sha256:{'3' * 64}",
        dispatch_boundary_epoch_nonce="boundary-1",
        dispatch_marker_request_nonce="marker-1",
        dispatch_peer_boot_id="dashboard-boot-1",
        dispatch_peer_connection_id="connection-1",
        pre_fence_event_seq=snapshot.events[-1].seq,
        pre_fence_token_snapshot_digest=f"sha256:{'4' * 64}",
        recorded_at=datetime(2026, 8, 14, 11, 1, tzinfo=timezone.utc),
    )
    isolated = await store.isolate_redirect_group(
        dispatched.dispatch_group_id,
        isolated_at=datetime(2026, 8, 14, 11, 2, tzinfo=timezone.utc),
    )
    assert isolated.state == "ISOLATING_UNCERTAIN"

    finalized = await store.finalize_redirect_group_uncertain(
        group.dispatch_group_id,
        finalized_at=datetime(2026, 8, 14, 11, 3, tzinfo=timezone.utc),
    )
    repeated = await store.finalize_redirect_group_uncertain(
        group.dispatch_group_id,
        finalized_at=datetime(2026, 8, 14, 11, 4, tzinfo=timezone.utc),
    )

    assert finalized.state == "SUBMISSION_UNCERTAIN"
    assert repeated == finalized
    disposition = await store.get_redirect_by_message(
        session_id=parent.session_id,
        client_message_id=redirect.client_message_id,
    )
    assert disposition is not None
    assert disposition.state == "SUBMISSION_UNCERTAIN"
    assert disposition.terminal_disposition_kind == "uncertain"
    assert disposition.start_after_seq == disposition.pending_event_seq
    assert await store.list_input_admission_reservations(parent.session_id) == []
    events = await store.load_run_snapshot(parent.run_id)
    assert events is not None
    assert [event.event_type for event in events.events] == [
        "followup.received",
        "followup.failed",
    ]
    release_ready = await store.list_redirect_groups_with_release_ready_fences()
    assert [item.dispatch_group_id for item in release_ready] == [
        group.dispatch_group_id
    ]
    assert await store.complete_redirect_event_fence_release(
        group.dispatch_group_id
    ) is True
    await store.close()


@pytest.mark.asyncio
async def test_pre_rpc_redirect_group_fails_all_members_without_creating_run(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    parent = await store.persist_dispatch_intent(intent(1))
    first = await store.admit_redirect(
        redirect_intent(
            1,
            parent_run_id=parent.run_id,
            session_id=parent.session_id,
            content="first correction",
        )
    )
    second = await store.admit_redirect(
        redirect_intent(
            2,
            parent_run_id=parent.run_id,
            session_id=parent.session_id,
            content="second correction",
        )
    )
    group = await store.prepare_redirect_group(
        dispatch_group_id="45454545-4545-4545-8545-454545454545",
        parent_run_id=parent.run_id,
        created_at=datetime(2026, 8, 14, 11, tzinfo=timezone.utc),
    )
    assert group is not None
    assert group.member_count == 2

    failed = await store.finalize_redirect_group_failed(
        group.dispatch_group_id,
        finalized_at=datetime(2026, 8, 14, 11, 1, tzinfo=timezone.utc),
        error_code="FOLLOWUP_PARENT_TERMINATED_BEFORE_DELIVERY",
        include_all_parent_followups=True,
    )
    repeated = await store.finalize_redirect_group_failed(
        group.dispatch_group_id,
        finalized_at=datetime(2026, 8, 14, 11, 2, tzinfo=timezone.utc),
        error_code="FOLLOWUP_PARENT_TERMINATED_BEFORE_DELIVERY",
        include_all_parent_followups=True,
    )

    assert failed.state == "SUBMISSION_UNCERTAIN"
    assert repeated == failed
    for redirect in (first, second):
        disposition = await store.get_redirect_by_message(
            session_id=parent.session_id,
            client_message_id=redirect.client_message_id,
        )
        assert disposition is not None
        assert disposition.state == "SUBMISSION_UNCERTAIN"
        assert disposition.terminal_disposition_kind == "uncertain"
        assert disposition.promoted_run_id is None
        assert await store.get_run(disposition.promotion_reserved_run_id) is None
    assert await store.list_input_admission_reservations(parent.session_id) == []
    snapshot = await store.load_run_snapshot(parent.run_id)
    assert snapshot is not None
    failed_events = [
        event for event in snapshot.events if event.event_type == "followup.failed"
    ]
    assert len(failed_events) == 2
    assert {
        event.payload["error_code"] for event in failed_events
    } == {"FOLLOWUP_PARENT_TERMINATED_BEFORE_DELIVERY"}
    assert await store.list_redirect_groups_with_release_ready_fences() == []
    await store.close()


@pytest.mark.asyncio
async def test_redirect_group_does_not_jump_an_earlier_ordinary_run(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    parent = await store.persist_dispatch_intent(intent(1))
    first = await store.admit_redirect(
        redirect_intent(
            1,
            parent_run_id=parent.run_id,
            session_id=parent.session_id,
            content="first",
        )
    )
    await store.enqueue_run(queued_intent(2, session_id=parent.session_id))
    await store.admit_redirect(
        redirect_intent(
            3,
            parent_run_id=parent.run_id,
            session_id=parent.session_id,
            content="third",
        )
    )

    group = await store.prepare_redirect_group(
        dispatch_group_id="55555555-5555-4555-8555-555555555555",
        parent_run_id=parent.run_id,
        created_at=datetime(2026, 8, 14, 11, tzinfo=timezone.utc),
    )
    assert group is not None
    assert group.member_count == 1
    assert group.wire_text == first.content
    await store.close()


@pytest.mark.asyncio
async def test_promoted_followup_runs_before_earlier_scheduled_work(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    parent = await store.persist_dispatch_intent(intent(1))
    scheduled = await store.enqueue_run(
        queued_intent(
            2,
            session_id=parent.session_id,
            source="scheduled_task",
            scheduled_task_id=41,
        )
    )
    redirect = await store.admit_redirect(
        redirect_intent(
            3,
            parent_run_id=parent.run_id,
            session_id=parent.session_id,
            content="continue the active conversation first",
        )
    )
    group = await store.prepare_redirect_group(
        dispatch_group_id="56565656-5656-4656-8656-565656565656",
        parent_run_id=parent.run_id,
        created_at=datetime(2026, 8, 14, 11, tzinfo=timezone.utc),
    )
    assert group is not None
    await store.promote_redirect_group(
        group.dispatch_group_id,
        promoted_at=datetime(2026, 8, 14, 11, 1, tzinfo=timezone.utc),
    )
    await store.transition_run(
        parent.run_id,
        "COMPLETED",
        terminal_at=datetime(2026, 8, 14, 11, 2, tzinfo=timezone.utc),
    )

    claimed = await store.claim_next_queued(
        parent.session_id,
        accepted_at=datetime(2026, 8, 14, 11, 3, tzinfo=timezone.utc),
    )

    assert claimed is not None
    assert claimed.run_id == redirect.promotion_reserved_run_id
    remaining = await store.list_input_admission_reservations(parent.session_id)
    assert [item.run_id for item in remaining] == [scheduled.run_id]
    await store.close()


@pytest.mark.asyncio
async def test_rejected_redirect_promotes_from_immutable_run_snapshot(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    parent = await store.persist_dispatch_intent(intent(1))
    redirect = await store.admit_redirect(
        redirect_intent(
            1,
            parent_run_id=parent.run_id,
            session_id=parent.session_id,
            content="promote me",
        )
    )
    group = await store.prepare_redirect_group(
        dispatch_group_id="66666666-6666-4666-8666-666666666666",
        parent_run_id=parent.run_id,
        created_at=datetime(2026, 8, 14, 11, tzinfo=timezone.utc),
    )
    assert group is not None
    promoted_group = await store.promote_redirect_group(
        group.dispatch_group_id,
        promoted_at=datetime(2026, 8, 14, 11, 1, tzinfo=timezone.utc),
    )
    assert promoted_group.state == "PROMOTED_TO_RUN"
    promoted = await store.get_run(redirect.promotion_reserved_run_id)
    assert promoted is not None
    assert promoted.client_message_id == redirect.client_message_id
    assert promoted.enqueue_seq == redirect.input_admission_seq
    disposition = await store.get_redirect_by_message(
        session_id=parent.session_id,
        client_message_id=redirect.client_message_id,
    )
    assert disposition is not None
    assert disposition.promoted_run_id == promoted.run_id
    assert disposition.terminal_disposition_kind == "promoted"
    assert (
        await store.list_input_admission_reservations(parent.session_id)
    )[0].state == "promoted"

    await store.transition_run(
        parent.run_id,
        "COMPLETED",
        terminal_at=datetime(2026, 8, 14, 11, 2, tzinfo=timezone.utc),
    )
    claimed = await store.claim_next_queued(
        parent.session_id,
        accepted_at=datetime(2026, 8, 14, 11, 3, tzinfo=timezone.utc),
    )
    assert claimed is not None
    assert claimed.run_id == promoted.run_id
    dispatched = await store.persist_dispatch_intent(
        DispatchIntent(
            run_id=claimed.run_id,
            session_id=claimed.session_id,
            client_message_id=claimed.client_message_id,
            payload_digest=claimed.payload_digest,
            config_revision=claimed.config_revision,
            source=claimed.source,
            accepted_at=claimed.accepted_at
            or datetime(2026, 8, 14, 11, 3, tzinfo=timezone.utc),
        )
    )
    assert dispatched.run_id == promoted.run_id
    assert dispatched.state == "DISPATCH_INTENT_DURABLE"
    assert await store.list_input_admission_reservations(parent.session_id) == []
    await store.close()


@pytest.mark.asyncio
async def test_redirect_group_promotes_all_members_into_one_ordered_run(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    parent = await store.persist_dispatch_intent(intent(1))
    redirects = [
        await store.admit_redirect(
            redirect_intent(
                index,
                parent_run_id=parent.run_id,
                session_id=parent.session_id,
                content=content,
            )
        )
        for index, content in enumerate(("first", "second", "third"), start=1)
    ]
    group = await store.prepare_redirect_group(
        dispatch_group_id="67676767-6767-4767-8767-676767676767",
        parent_run_id=parent.run_id,
        created_at=datetime(2026, 8, 14, 11, tzinfo=timezone.utc),
    )
    assert group is not None
    assert group.member_count == 3

    promoted_group = await store.promote_redirect_group(
        group.dispatch_group_id,
        promoted_at=datetime(2026, 8, 14, 11, 1, tzinfo=timezone.utc),
    )

    assert promoted_group.state == "PROMOTED_TO_RUN"
    dispositions = [
        await store.get_redirect_by_message(
            session_id=parent.session_id,
            client_message_id=redirect.client_message_id,
        )
        for redirect in redirects
    ]
    promoted_run_ids = {
        disposition.promoted_run_id
        for disposition in dispositions
        if disposition is not None
    }
    assert promoted_run_ids == {redirects[0].promotion_reserved_run_id}
    promoted = await store.get_run(redirects[0].promotion_reserved_run_id)
    assert promoted is not None
    assert promoted.prompt == (
        "first\n\n[Additional user correction]\nsecond"
        "\n\n[Additional user correction]\nthird"
    )
    assert await store.get_run(redirects[1].promotion_reserved_run_id) is None
    assert await store.get_run(redirects[2].promotion_reserved_run_id) is None

    await store.transition_run(
        parent.run_id,
        "COMPLETED",
        terminal_at=datetime(2026, 8, 14, 11, 2, tzinfo=timezone.utc),
    )
    claimed = await store.claim_next_queued(
        parent.session_id,
        accepted_at=datetime(2026, 8, 14, 11, 3, tzinfo=timezone.utc),
    )
    assert claimed is not None
    assert claimed.run_id == promoted.run_id
    assert await store.list_input_admission_reservations(parent.session_id) == []
    await store.close()


@pytest.mark.asyncio
async def test_fast_lane_v2_delivery_binding_follows_redirect_promotion(
    tmp_path: Path,
) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()
    parent = await store.persist_dispatch_intent(intent(1))
    payload_digest = "f" * 64
    base = redirect_intent(
        1,
        parent_run_id=parent.run_id,
        session_id=parent.session_id,
        content="promote the channel correction",
    )
    redirect = await store.admit_redirect(
        replace(
            base,
            delivery_id="delivery-redirect-00000001",
            promotion_run_intent={
                **base.promotion_run_intent,
                "surface_payload_digest": payload_digest,
            },
        )
    )
    pending = await store.get_channel_fast_lane_v2_delivery(
        "delivery-redirect-00000001"
    )
    assert pending is not None
    assert pending.redirect_id == redirect.redirect_id
    assert pending.disposition_state == "pending"
    assert pending.disposition_seq == 1
    assert pending.canonical_run_id == parent.run_id

    group = await store.prepare_redirect_group(
        dispatch_group_id="77777777-7777-4777-8777-777777777777",
        parent_run_id=parent.run_id,
        created_at=datetime(2026, 8, 14, 11, tzinfo=timezone.utc),
    )
    assert group is not None
    await store.promote_redirect_group(
        group.dispatch_group_id,
        promoted_at=datetime(2026, 8, 14, 11, 1, tzinfo=timezone.utc),
    )
    promoted = await store.get_channel_fast_lane_v2_delivery(
        "delivery-redirect-00000001"
    )
    assert promoted is not None
    assert promoted.disposition_state == "promoted"
    assert promoted.disposition_seq == 2
    assert promoted.start_after_seq == 0
    assert promoted.canonical_run_id == redirect.promotion_reserved_run_id
    assert promoted.promoted_run_id == redirect.promotion_reserved_run_id

    await store.close()
    reopened = SQLiteStore(database)
    await reopened.start()
    restored = await reopened.get_channel_fast_lane_v2_delivery(
        "delivery-redirect-00000001"
    )
    assert restored == promoted
    await reopened.close()


@pytest.mark.asyncio
async def test_fast_lane_v2_acks_are_monotonic_durable_and_fan_out(
    tmp_path: Path,
) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()
    queued = await store.enqueue_run(
        replace(
            queued_intent(1),
            delivery_id="delivery-ordinary-00000001",
        )
    )
    delivery = await store.get_channel_fast_lane_v2_delivery(
        "delivery-ordinary-00000001"
    )
    assert delivery is not None
    assert delivery.run_id == queued.run_id
    assert delivery.disposition_state == "ordinary_run_accepted"
    assert delivery.start_after_seq == 0

    acknowledged = await store.acknowledge_channel_fast_lane_v2_disposition(
        delivery_id=delivery.delivery_id,
        disposition_seq=1,
        gateway_instance_id="gateway-instance-0001",
        channel_lease_epoch=7,
        acknowledged_at=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
    )
    repeated = await store.acknowledge_channel_fast_lane_v2_disposition(
        delivery_id=delivery.delivery_id,
        disposition_seq=1,
        gateway_instance_id="gateway-instance-0001",
        channel_lease_epoch=7,
        acknowledged_at=datetime(2026, 8, 14, 12, 1, tzinfo=timezone.utc),
    )
    assert acknowledged.disposition_ack_seq == 1
    assert repeated.disposition_ack_seq == 1

    checkpoint = await store.acknowledge_channel_fast_lane_v2_provider_delivery(
        owner_delivery_id=delivery.delivery_id,
        canonical_run_id=queued.run_id,
        highest_contiguous_seq=4,
        gateway_instance_id="gateway-instance-0001",
        channel_lease_epoch=7,
        provider_request_id="provider-request-0001",
        provider_message_id="provider-message-0001",
        projection_version=1,
        confirmed_at=datetime(2026, 8, 14, 12, 2, tzinfo=timezone.utc),
    )
    older_retry = await store.acknowledge_channel_fast_lane_v2_provider_delivery(
        owner_delivery_id=delivery.delivery_id,
        canonical_run_id=queued.run_id,
        highest_contiguous_seq=3,
        gateway_instance_id="gateway-instance-0001",
        channel_lease_epoch=7,
        provider_request_id="provider-request-older",
        provider_message_id=None,
        projection_version=1,
        confirmed_at=datetime(2026, 8, 14, 12, 3, tzinfo=timezone.utc),
    )
    assert checkpoint.highest_contiguous_seq == 4
    assert older_retry.highest_contiguous_seq == 4
    advanced = await store.get_channel_fast_lane_v2_delivery(delivery.delivery_id)
    assert advanced is not None and advanced.provider_confirmed_seq == 4

    await store.close()
    reopened = SQLiteStore(database)
    await reopened.start()
    restored = await reopened.get_channel_fast_lane_v2_delivery(
        delivery.delivery_id
    )
    assert restored is not None
    assert restored.disposition_ack_seq == 1
    assert restored.provider_confirmed_seq == 4
    await reopened.close()






@pytest.mark.asyncio
async def test_v6_runs_table_is_rebuilt_with_nullable_accepted_at(
    tmp_path: Path,
) -> None:
    database = tmp_path / "supervisor.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            client_message_id TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            config_revision TEXT NOT NULL,
            source TEXT NOT NULL,
            state TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            acknowledged_at TEXT,
            terminal_at TEXT,
            answer_snapshot TEXT NOT NULL DEFAULT '',
            answer_through_seq INTEGER NOT NULL DEFAULT 0,
            UNIQUE(session_id, client_message_id)
        );
        INSERT INTO runs(
            run_id, session_id, client_message_id, payload_digest,
            config_revision, source, state, accepted_at
        ) VALUES (
            'legacy-run', 'legacy-session', 'legacy-message',
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'revision-18', 'web', 'COMPLETED',
            '2026-08-13T00:00:00+00:00'
        );
        PRAGMA user_version=6;
        """
    )
    connection.close()

    store = SQLiteStore(database)
    await store.start()
    assert (await store.get_run("legacy-run")) is not None
    queued = await store.enqueue_run(queued_intent(1))
    assert queued.accepted_at is None
    await store.close()

    connection = sqlite3.connect(database)
    try:
        columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(runs)")
        }
        assert columns["accepted_at"][3] == 0
        assert connection.execute(
            "SELECT accepted_at FROM runs WHERE run_id = ?",
            (queued.run_id,),
        ).fetchone()[0] is None
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_dispatch_intent_is_durable_before_return(tmp_path: Path) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()

    stored = await store.persist_dispatch_intent(intent(1))
    assert stored.created is True
    assert stored.state == "DISPATCH_INTENT_DURABLE"
    await store.close()

    reopened = SQLiteStore(database)
    await reopened.start()
    restored = await reopened.get_run("run-00000001")
    assert restored is not None
    assert restored.state == "DISPATCH_INTENT_DURABLE"
    assert [run.run_id for run in await reopened.load_unfinished_runs()] == [
        "run-00000001"
    ]
    await reopened.close()


@pytest.mark.asyncio
async def test_run_provider_identity_and_cancel_intent_are_durable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()
    await store.persist_dispatch_intent(intent(1))
    requested_at = datetime(2026, 8, 13, 8, 30, tzinfo=timezone.utc)

    mapped = await store.set_runtime_provider_run_id(
        "run-00000001",
        "live-dashboard-session-1",
    )
    requested = await store.request_run_cancellation(
        "run-00000001",
        requested_at=requested_at,
    )
    repeated = await store.request_run_cancellation(
        "run-00000001",
        requested_at=datetime(2026, 8, 13, 8, 31, tzinfo=timezone.utc),
    )

    assert mapped.runtime_provider_run_id == "live-dashboard-session-1"
    assert requested.cancellation_requested_at == requested_at
    assert repeated.cancellation_requested_at == requested_at
    await store.close()

    reopened = SQLiteStore(database)
    await reopened.start()
    restored = await reopened.get_run("run-00000001")
    assert restored is not None
    assert restored.runtime_provider_run_id == "live-dashboard-session-1"
    assert restored.cancellation_requested_at == requested_at
    await reopened.close()


@pytest.mark.asyncio
async def test_channel_delivery_checkpoint_is_monotonic_and_durable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()
    await store.persist_dispatch_intent(intent(1))
    confirmed_at = datetime(2026, 8, 14, 8, tzinfo=timezone.utc)

    first = await store.acknowledge_channel_delivery(
        run_id="run-00000001",
        highest_contiguous_seq=8,
        gateway_instance_id="backend-1:im-gateway",
        channel_lease_epoch=7,
        provider_request_id="provider-request-1",
        provider_message_id="provider-message-1",
        projection_version=2,
        confirmed_at=confirmed_at,
    )
    repeated = await store.acknowledge_channel_delivery(
        run_id="run-00000001",
        highest_contiguous_seq=3,
        gateway_instance_id="backend-1:im-gateway",
        channel_lease_epoch=7,
        provider_request_id="provider-request-1",
        provider_message_id="provider-message-1",
        projection_version=2,
        confirmed_at=confirmed_at,
    )

    assert first.highest_contiguous_seq == 8
    assert repeated.highest_contiguous_seq == 8
    await store.close()

    reopened = SQLiteStore(database)
    await reopened.start()
    restored = await reopened.get_channel_delivery_checkpoint(
        "run-00000001"
    )
    assert restored is not None
    assert restored.highest_contiguous_seq == 8
    assert restored.gateway_instance_id == "backend-1:im-gateway"
    await reopened.close()


@pytest.mark.asyncio
async def test_same_message_and_digest_returns_original_run(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()

    original = await store.persist_dispatch_intent(intent(1))
    retry = replace(
        intent(2),
        session_id=original.session_id,
        client_message_id=original.client_message_id,
        payload_digest=original.payload_digest,
    )
    repeated = await store.persist_dispatch_intent(retry)

    assert repeated.created is False
    assert repeated.run_id == original.run_id
    await store.close()


@pytest.mark.asyncio
async def test_same_message_with_different_digest_is_rejected(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    await store.persist_dispatch_intent(intent(1))
    conflicting = DispatchIntent(
        run_id="run-99999999",
        session_id="session-00000001",
        client_message_id="message-00000001",
        payload_digest="f" * 64,
        config_revision="revision-18",
        source="web",
        accepted_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
    )

    with pytest.raises(IdempotencyConflict):
        await store.persist_dispatch_intent(conflicting)
    await store.close()


@pytest.mark.asyncio
async def test_concurrent_dispatches_are_committed_by_single_writer(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()

    stored = await asyncio.gather(
        *(store.persist_dispatch_intent(intent(index)) for index in range(50))
    )

    assert len(stored) == 50
    assert all(run.created for run in stored)
    assert len(await store.load_unfinished_runs()) == 50
    metrics = store.writer_metrics_snapshot()
    assert metrics["commit_sample_count"] >= 1
    assert metrics["commit_p95_ms"] >= 0
    assert metrics["writer_queue_depth"] == 0
    await store.close()


@pytest.mark.asyncio
async def test_run_events_snapshot_and_terminal_state_are_durable(tmp_path: Path) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()
    await store.persist_dispatch_intent(intent(1))
    now = datetime(2026, 8, 7, 1, tzinfo=timezone.utc)

    await store.transition_run("run-00000001", "ACKNOWLEDGED", acknowledged_at=now)
    assert await store.append_run_event(
        StoredRunEvent(
            run_id="run-00000001",
            seq=1,
            event_type="chat.message.completed",
            payload={"text": "完成"},
            occurred_at=now,
        ),
        durable=True,
    )
    assert await store.save_answer_snapshot(
        "run-00000001",
        answer="完成",
        through_seq=1,
    )
    await store.transition_run("run-00000001", "COMPLETED", terminal_at=now)
    with pytest.raises(SQLiteStoreError, match="terminal"):
        await store.transition_run("run-00000001", "FAILED", terminal_at=now)
    await store.close()

    reopened = SQLiteStore(database)
    await reopened.start()
    snapshot = await reopened.load_run_snapshot("run-00000001")
    assert snapshot is not None
    assert snapshot.run.state == "COMPLETED"
    assert snapshot.answer == "完成"
    assert snapshot.events[0].payload == {"text": "完成"}
    assert await reopened.load_unfinished_runs() == []
    await reopened.close()


@pytest.mark.asyncio
async def test_session_command_outbox_and_revocation_share_writer(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "supervisor.db")
    await store.start()
    now = datetime(2026, 8, 7, 2, tzinfo=timezone.utc)

    session = await store.upsert_session_envelope(
        session_id="session-00000001",
        prepare_identity_digest="a" * 64,
        state="READY",
        last_activity_at=now,
    )
    assert session.state == "READY"
    assert (await store.load_recent_sessions(active_since=now, limit=64))[0] == session

    command = await store.persist_command(
        command_id="command-00000001",
        payload_digest="b" * 64,
        command_type="prepare",
    )
    assert command.created
    repeated = await store.persist_command(
        command_id=command.command_id,
        payload_digest=command.payload_digest,
        command_type="prepare",
    )
    assert repeated.created is False
    with pytest.raises(IdempotencyConflict):
        await store.persist_command(
            command_id=command.command_id,
            payload_digest="c" * 64,
            command_type="prepare",
        )
    assert await store.abandon_command(
        command_id=command.command_id,
        payload_digest=command.payload_digest,
    )
    retried = await store.persist_command(
        command_id=command.command_id,
        payload_digest=command.payload_digest,
        command_type="prepare",
    )
    assert retried.created is True

    assert await store.enqueue_projection(
        projection_id="projection-00000001",
        projection_type="run.terminal",
        payload={"run_id": "run-00000001"},
        created_at=now,
    )
    batch = await store.load_projection_batch()
    assert [item.projection_id for item in batch] == ["projection-00000001"]
    assert await store.acknowledge_projections(
        ["projection-00000001"],
        acknowledged_at=now,
    ) == 1
    assert await store.load_projection_batch() == []

    assert await store.record_revocation(
        command_id="command-revoke-1",
        scope="model",
        subject_id="model-1",
        revoked_at=now,
        cancel_active_runs=False,
    )
    assert await store.is_revoked(
        user_id=1,
        model_id="model-1",
        application_id="application-1",
    )
    await store.close()
