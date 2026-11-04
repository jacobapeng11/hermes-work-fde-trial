from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.runtime_clarify as runtime_clarify_module
from app.models import RuntimeClarifyRequest, RuntimeRun
from app.services.runtime_chat import CreatedRuntimeRun
from app.services.runtime_clarify import RuntimeClarifyScanner


NOW = datetime(2026, 9, 23, 10, 0, 0)


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(RuntimeRun.__table__.create)
        await connection.run_sync(RuntimeClarifyRequest.__table__.create)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(db, **overrides: object) -> None:
    db.add(RuntimeRun(
        id=31,
        org_id=1,
        user_id=2,
        runtime_workspace_id=11,
        session_id=21,
        runtime_session_id="rs-scanner",
        runtime_run_id="rr-parent-scanner",
        model_entry_id=7,
        status="parked_for_clarify",
        source_type="web",
    ))
    values: dict[str, object] = {
        "id": 41,
        "org_id": 1,
        "user_id": 2,
        "runtime_workspace_id": 11,
        "session_id": 21,
        "parent_run_id": 31,
        "request_id": "clr_scanner",
        "event_id": "clarify:call-scanner",
        "runtime_session_id": "rs-scanner",
        "question": "选择格式",
        "choices": ["PDF", "Word"],
        "status": "waiting",
        "auto_default_at": NOW - timedelta(seconds=1),
        "version": 1,
    }
    values.update(overrides)
    db.add(RuntimeClarifyRequest(**values))
    await db.commit()


class FailingChatService:
    async def create_run(self, **kwargs):
        raise PermissionError("runtime is not ready")

    async def get_runtime_endpoint(self, **kwargs):
        raise AssertionError("resume should not reach endpoint resolution")


async def test_scanner_auto_defaults_to_skip_and_failure_is_retryable() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _seed(db)
        scanner = RuntimeClarifyScanner(
            session_factory=factory,
            chat_service_factory=lambda db: FailingChatService(),
            auto_default_seconds=60,
            claim_lease_seconds=120,
            max_auto_resume_attempts=12,
        )
        attempted = await scanner.scan_once(now=NOW)
        assert attempted == 1

        async with factory() as db:
            row = await db.scalar(
                select(RuntimeClarifyRequest).where(
                    RuntimeClarifyRequest.request_id == "clr_scanner"
                )
            )
            assert row.status == "resume_failed"
            assert row.answer == {"kind": "skip", "text": None, "values": []}
            assert row.answer_source == "auto"
            assert row.resume_attempt_count == 1
            assert row.resume_run_id is not None
            assert row.next_resume_at == NOW + timedelta(seconds=5)

        assert await scanner.scan_once(now=NOW + timedelta(seconds=1)) == 0
    finally:
        await engine.dispose()


async def test_scanner_does_not_override_user_claim() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _seed(
                db,
                status="claimed",
                answer={"kind": "single", "text": "PDF", "values": []},
                answer_source="user",
                answered_at=NOW - timedelta(seconds=1),
                claim_expires_at=NOW + timedelta(seconds=120),
                resume_attempt_count=1,
                resume_run_id="rr-clarify-stable",
                version=2,
            )
        scanner = RuntimeClarifyScanner(
            session_factory=factory,
            chat_service_factory=lambda db: FailingChatService(),
        )
        assert await scanner.scan_once(now=NOW) == 0

        async with factory() as db:
            row = await db.scalar(
                select(RuntimeClarifyRequest).where(
                    RuntimeClarifyRequest.request_id == "clr_scanner"
                )
            )
            assert row.answer == {"kind": "single", "text": "PDF", "values": []}
            assert row.answer_source == "user"
    finally:
        await engine.dispose()


async def test_auto_retry_is_bounded_and_user_retry_remains_possible() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _seed(
                db,
                status="resume_failed",
                answer={"kind": "skip", "text": None, "values": []},
                answer_source="auto",
                answered_at=NOW - timedelta(seconds=300),
                resume_attempt_count=12,
                resume_run_id="rr-clarify-stable",
                next_resume_at=NOW - timedelta(seconds=1),
                version=13,
            )
        scanner = RuntimeClarifyScanner(
            session_factory=factory,
            chat_service_factory=lambda db: FailingChatService(),
            max_auto_resume_attempts=12,
        )
        assert await scanner.scan_once(now=NOW) == 0
    finally:
        await engine.dispose()


async def test_successful_auto_default_records_answered_with_stable_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _seed(db)

        class ChatService:
            async def create_run(self, **kwargs):
                return CreatedRuntimeRun(
                    runtime_workspace_id=11,
                    runtime_session_id="rs-scanner",
                    runtime_run_id="rr-clarify-stable",
                    model_entry_id=7,
                    status="running",
                )

            async def get_runtime_endpoint(self, **kwargs):
                return SimpleNamespace()

        async def resolve(*args, **kwargs):
            return SimpleNamespace()

        async def submit(*args, **kwargs):
            return {"admission_state": "queued"}

        monkeypatch.setattr(runtime_clarify_module, "resolve_personal_internal_turn", resolve)
        monkeypatch.setattr(runtime_clarify_module, "submit_personal_internal_turn", submit)
        scanner = RuntimeClarifyScanner(
            session_factory=factory,
            chat_service_factory=lambda db: ChatService(),
        )
        assert await scanner.scan_once(now=NOW) == 1

        async with factory() as db:
            row = await db.scalar(
                select(RuntimeClarifyRequest).where(
                    RuntimeClarifyRequest.request_id == "clr_scanner"
                )
            )
            assert row.status == "answered"
            assert row.answer_source == "auto"
            assert row.resume_run_id == "rr-clarify-stable"
    finally:
        await engine.dispose()
