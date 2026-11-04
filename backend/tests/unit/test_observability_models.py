from app.models import Base, ChatFileArtifact, ModelUsageLog, RuntimeAuditLog, RuntimeRunEvent


def test_observability_tables_are_registered() -> None:
    table_names = set(Base.metadata.tables)

    assert {"chat_file_artifact", "runtime_audit_log", "model_usage_log", "runtime_run_event"}.issubset(
        table_names
    )


def test_chat_file_artifact_tracks_runtime_context() -> None:
    columns = set(ChatFileArtifact.__table__.c.keys())

    assert {
        "org_id",
        "user_id",
        "runtime_workspace_id",
        "session_id",
        "run_id",
        "relative_path",
        "mime_type",
        "size_bytes",
        "visibility",
    }.issubset(columns)


def test_runtime_audit_log_tracks_admin_operations() -> None:
    columns = set(RuntimeAuditLog.__table__.c.keys())

    assert {"actor_user_id", "runtime_workspace_id", "action", "target_type", "target_id"}.issubset(
        columns
    )


def test_model_usage_log_tracks_model_relay_dimensions() -> None:
    columns = set(ModelUsageLog.__table__.c.keys())

    assert {
        "org_id",
        "user_id",
        "runtime_workspace_id",
        "session_id",
        "run_id",
        "runtime_run_id",
        "request_id",
        "model_entry_id",
        "provider_id",
        "input_tokens",
        "output_tokens",
        "latency_ms",
    }.issubset(columns)


def test_runtime_run_event_records_replayable_stream_events() -> None:
    table = RuntimeRunEvent.__table__

    assert {
        "run_id",
        "runtime_run_public_id",
        "seq",
        "event_name",
        "payload",
    }.issubset(table.c.keys())
    assert {fk.target_fullname for fk in table.c.run_id.foreign_keys} == {"runtime_run.id"}
    assert any(
        constraint.columns.keys() == ["run_id", "seq"]
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    )
    assert any(index.columns.keys() == ["runtime_run_public_id", "seq"] for index in table.indexes)
