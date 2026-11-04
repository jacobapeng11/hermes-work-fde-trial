from sqlalchemy.dialects.mysql import LONGTEXT

from app.models import Base, ChatLog, RuntimeRun, RuntimeSession


def test_chat_log_table_is_registered() -> None:
    assert "chat_log" in Base.metadata.tables


def test_chat_log_model_has_audit_and_channel_fields() -> None:
    columns = ChatLog.__table__.columns

    assert {
        "org_id",
        "user_id",
        "runtime_workspace_id",
        "session_id",
        "runtime_session_id",
        "runtime_run_id",
        "message_seq",
        "role",
        "content",
        "content_format",
        "message_status",
        "channel",
        "channel_account_id",
        "external_conversation_id",
        "external_message_id",
        "source_type",
        "source_detail",
        "model_entry_id",
        "ingest_source",
        "occurred_at",
        "completed_at",
        "created_at",
        "updated_at",
    }.issubset(columns.keys())
    assert isinstance(columns["content"].type, LONGTEXT)


def test_chat_log_has_ordering_and_run_idempotency_constraints() -> None:
    names = {constraint.name for constraint in ChatLog.__table__.constraints}

    assert "uq_chat_log_run_role" in names
    assert "uq_chat_log_session_seq" in names
    assert "ck_chat_log_role" in names
    assert "ck_chat_log_content_format" in names
    assert "ck_chat_log_message_status" in names
    assert "ck_chat_log_ingest_source" in names


def test_runtime_models_expose_chat_log_projection_markers() -> None:
    assert "chat_log_backfilled_at" in RuntimeSession.__table__.columns
    assert "chat_log_projected_at" in RuntimeRun.__table__.columns
