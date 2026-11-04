from __future__ import annotations

from pathlib import Path

from app.models import RuntimeClarifyRequest


def test_runtime_clarify_model_declares_claim_and_retry_contract() -> None:
    columns = set(RuntimeClarifyRequest.__table__.columns.keys())
    assert {
        "auto_default_at",
        "answer_source",
        "claim_expires_at",
        "resume_attempt_count",
        "next_resume_at",
    }.issubset(columns)


def test_current_schema_upgrades_existing_runtime_clarify_rows() -> None:
    content = (
        Path(__file__).resolve().parents[4]
        / "backend"
        / "scripts"
        / "current_schema.sql"
    ).read_text(encoding="utf-8")
    for column in (
        "auto_default_at",
        "answer_source",
        "claim_expires_at",
        "resume_attempt_count",
        "next_resume_at",
    ):
        assert column in content
