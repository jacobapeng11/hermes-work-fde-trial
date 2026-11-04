from app.models import Base, RuntimeRun, RuntimeSession


def test_runtime_session_and_run_tables_are_registered() -> None:
    table_names = set(Base.metadata.tables)

    assert {"runtime_session", "runtime_run"}.issubset(table_names)


def test_runtime_session_tracks_hermes_session_and_model() -> None:
    table = RuntimeSession.__table__

    assert "runtime_session_id" in table.c
    assert "session_model_entry_id" in table.c
    assert {fk.target_fullname for fk in table.c.session_model_entry_id.foreign_keys} == {
        "model_entry.id"
    }


def test_runtime_run_tracks_active_run_and_allowed_model() -> None:
    table = RuntimeRun.__table__

    assert "runtime_run_id" in table.c
    assert "model_entry_id" in table.c
    assert "status" in table.c
    assert {fk.target_fullname for fk in table.c.model_entry_id.foreign_keys} == {
        "model_entry.id"
    }


def test_runtime_run_contains_stop_approval_and_error_fields() -> None:
    columns = set(RuntimeRun.__table__.c.keys())

    assert {
        "approval_status",
        "stop_requested_at",
        "stopped_by_user_id",
        "started_at",
        "finished_at",
        "error_code",
        "error_message",
    }.issubset(columns)


def test_runtime_session_run_exclude_copaw_fields() -> None:
    for table in (RuntimeSession.__table__, RuntimeRun.__table__):
        columns = set(table.c.keys())
        assert "copaw_session_id" not in columns
        assert "copaw_chat_id" not in columns
        assert "workspace_id" not in columns
