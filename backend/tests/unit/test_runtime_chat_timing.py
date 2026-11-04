from app.services.runtime_chat_timing import RuntimeChatTiming


def test_runtime_chat_timing_uses_monotonic_marks_and_bounded_snapshot() -> None:
    values = iter([10.0, 10.015, 10.025])
    timing = RuntimeChatTiming(clock=lambda: next(values))

    timing.mark("request_received")
    timing.bind_run("rr-1-safe")
    timing.set_metadata("prepared_state", "hit")
    timing.mark("admission_done")
    timing.mark("stream_ready")

    assert timing.duration_ms("request_received", "admission_done") == 15.0
    assert timing.safe_snapshot() == {
        "runtime_run_id": "rr-1-safe",
        "durations_ms": {
            "request_received_to_admission_done": 15.0,
            "admission_done_to_stream_ready": 10.0,
        },
        "metadata": {"prepared_state": "hit"},
    }


def test_runtime_chat_timing_splits_transport_ack_stages() -> None:
    values = iter([1.0, 1.010, 1.025, 1.040, 1.055])
    timing = RuntimeChatTiming(clock=lambda: next(values))

    timing.mark("prompt_submitted")
    timing.mark("hot_channel_acquired")
    timing.mark("prompt_submit_begin")
    timing.mark("prompt_ack")
    timing.mark("first_agent_event")

    assert timing.safe_snapshot()["durations_ms"] == {
        "prompt_submitted_to_hot_channel_acquired": 10.0,
        "hot_channel_acquired_to_prompt_submit_begin": 15.0,
        "prompt_submit_begin_to_prompt_ack": 15.0,
        "prompt_ack_to_first_agent_event": 15.0,
    }


def test_runtime_chat_timing_records_first_sse_yield_after_background_launch() -> None:
    values = iter([1.0, 1.010, 1.025])
    timing = RuntimeChatTiming(clock=lambda: next(values))

    timing.mark("stream_ready")
    timing.mark("background_launched")
    timing.mark("first_sse_yield")

    assert timing.safe_snapshot()["durations_ms"] == {
        "stream_ready_to_background_launched": 10.0,
        "background_launched_to_first_sse_yield": 15.0,
    }


def test_runtime_chat_timing_ignores_unknown_and_duplicate_stage_names() -> None:
    values = iter([1.0, 2.0])
    timing = RuntimeChatTiming(clock=lambda: next(values))

    timing.mark("request_received")
    timing.mark("request_received")
    timing.mark("not_a_stage")

    assert timing.duration_ms("missing", "request_received") is None
    assert timing.safe_snapshot()["durations_ms"] == {}


def test_runtime_chat_timing_explicit_pairs_skip_missing_and_reversed_marks() -> None:
    values = iter([2.0, 1.0, 3.0])
    timing = RuntimeChatTiming(clock=lambda: next(values))

    timing.mark("background_launched")
    timing.mark("stream_ready")
    timing.mark("first_sse_yield")

    assert timing.safe_snapshot(
        duration_pairs=(
            ("background_launched", "stream_ready"),
            ("stream_ready", "first_sse_yield"),
            ("request_received", "first_sse_yield"),
        )
    )["durations_ms"] == {
        "stream_ready_to_first_sse_yield": 2000.0,
    }


def test_runtime_chat_timing_accepts_ttft_breakdown_stages() -> None:
    values = iter([1.0, 1.01, 1.025, 1.04])
    timing = RuntimeChatTiming(clock=lambda: next(values))

    assert timing.mark("executor_started") is True
    assert timing.mark("artifact_baseline_done") is True
    assert timing.mark("dashboard_stream_begin") is True
    assert timing.mark("first_visible_event_received") is True

    assert timing.safe_snapshot(
        duration_pairs=(
            ("executor_started", "artifact_baseline_done"),
            ("artifact_baseline_done", "dashboard_stream_begin"),
            (
                "dashboard_stream_begin",
                "first_visible_event_received",
            ),
        )
    )["durations_ms"] == {
        "executor_started_to_artifact_baseline_done": 10.0,
        "artifact_baseline_done_to_dashboard_stream_begin": 15.0,
        "dashboard_stream_begin_to_first_visible_event_received": 15.0,
    }


def test_runtime_chat_timing_accepts_run_creation_stages() -> None:
    values = iter(
        [1.0, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07, 1.08]
    )
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
    ):
        assert timing.mark(stage) is True

    assert timing.safe_snapshot(
        duration_pairs=(
            ("attachments_done", "run_prepare_done"),
            ("run_prepare_done", "run_profile_gate_done"),
            ("run_profile_gate_done", "run_binding_lock_done"),
            ("run_binding_lock_done", "run_session_lock_done"),
            (
                "run_session_lock_done",
                "run_reserved_id_check_done",
            ),
            (
                "run_reserved_id_check_done",
                "run_active_check_done",
            ),
            ("run_active_check_done", "run_commit_begin"),
            ("run_commit_begin", "run_commit_done"),
        )
    )["durations_ms"] == {
        "attachments_done_to_run_prepare_done": 10.0,
        "run_prepare_done_to_run_profile_gate_done": 10.0,
        "run_profile_gate_done_to_run_binding_lock_done": 10.0,
        "run_binding_lock_done_to_run_session_lock_done": 10.0,
        "run_session_lock_done_to_run_reserved_id_check_done": 10.0,
        "run_reserved_id_check_done_to_run_active_check_done": 10.0,
        "run_active_check_done_to_run_commit_begin": 10.0,
        "run_commit_begin_to_run_commit_done": 10.0,
    }


def test_runtime_chat_timing_accepts_application_prewarm_stages() -> None:
    values = iter([1.0, 1.01, 1.02, 1.03, 1.04])
    timing = RuntimeChatTiming(clock=lambda: next(values))

    for stage in (
        "session_materialization_started",
        "session_materialized",
        "session_prewarm_started",
        "session_prewarm_completed",
        "prepared_context_ready",
    ):
        assert timing.mark(stage) is True

    assert timing.safe_snapshot(
        duration_pairs=(
            (
                "session_materialization_started",
                "session_materialized",
            ),
            (
                "session_prewarm_started",
                "session_prewarm_completed",
            ),
        )
    )["durations_ms"] == {
        "session_materialization_started_to_session_materialized": 10.0,
        "session_prewarm_started_to_session_prewarm_completed": 10.0,
    }


def test_runtime_chat_timing_accepts_ready_receipt_run_stages() -> None:
    timing = RuntimeChatTiming()

    for stage in (
        "ready_receipt_reserve_done",
        "ready_receipt_prepare_done",
        "run_reserved_id_precheck_done",
        "run_ready_identity_done",
        "run_profile_lock_done",
        "run_ready_target_done",
        "run_insert_begin",
        "run_insert_done",
    ):
        assert timing.mark(stage) is True


def test_runtime_chat_timing_drops_unsafe_metadata_from_logs() -> None:
    timing = RuntimeChatTiming()

    timing.set_metadata("request_id", "safe-request:1")
    timing.set_metadata("unsafe value", "ignored")
    timing.set_metadata("runtime_session_id", "message text with spaces")

    assert timing.safe_snapshot()["metadata"] == {
        "request_id": "safe-request:1",
    }
