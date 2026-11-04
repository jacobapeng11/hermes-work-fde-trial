from app.schemas.runtime_chat import RuntimeRunActionRequest


def test_runtime_run_action_request_defaults_replay_cursor_to_zero() -> None:
    request = RuntimeRunActionRequest(runtime_run_id="rr-123")

    assert request.after_seq == 0


def test_runtime_run_action_request_accepts_replay_cursor() -> None:
    request = RuntimeRunActionRequest(runtime_run_id="rr-123", after_seq=12)

    assert request.after_seq == 12
