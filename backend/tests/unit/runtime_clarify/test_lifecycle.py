from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.runtime_clarify as runtime_clarify_module
from app.models import RuntimeClarifyRequest, RuntimeRun
from app.schemas.runtime_clarify import RuntimeClarifyAnswer, RuntimeClarifyCreateRequest
from app.services.runtime_clarify import RuntimeClarifyError, RuntimeClarifyService
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec


PRINCIPAL = Principal(org_id=1, user_id=2)
NOW = datetime(2026, 9, 23, 10, 0, 0)


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(RuntimeRun.__table__.create)
        await connection.run_sync(RuntimeClarifyRequest.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _request(db: AsyncSession, **overrides: object) -> RuntimeClarifyRequest:
    values: dict[str, object] = {
        "id": 41,
        "org_id": 1,
        "user_id": 2,
        "runtime_workspace_id": 11,
        "session_id": 21,
        "parent_run_id": 31,
        "request_id": "clr_lifecycle",
        "event_id": "clarify:call-lifecycle",
        "runtime_session_id": "rs-lifecycle",
        "question": "选择格式",
        "choices": ["PDF", "Word"],
        "multi_select": False,
        "status": "waiting",
        "auto_default_at": NOW + timedelta(seconds=60),
        "version": 1,
    }
    values.update(overrides)
    request = RuntimeClarifyRequest(**values)
    db.add(request)
    await db.flush()
    return request


async def _parent(db: AsyncSession) -> RuntimeRun:
    run = RuntimeRun(
        id=31,
        org_id=1,
        user_id=2,
        runtime_workspace_id=11,
        session_id=21,
        runtime_session_id="rs-lifecycle",
        runtime_run_id="rr-parent-lifecycle",
        model_entry_id=7,
        status="parked_for_clarify",
        source_type="web",
    )
    db.add(run)
    await db.flush()
    return run


async def test_new_request_uses_independent_auto_default_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_clarify_module,
        "load_runtime_command_admission_generation",
        AsyncMock(return_value=11),
    )
    binding = SimpleNamespace(
        id=17,
        org_id=1,
        user_id=2,
        runtime_id="runtime-11",
        endpoint_host_header="runtime-11.local",
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-secret"),
    )
    session = SimpleNamespace(id=21, source_type="web")
    run = SimpleNamespace(id=31, runtime_run_id="rr-parent")

    class Db:
        added = None

        async def scalar(self, statement):
            sql = str(statement)
            if "FROM hermes_runtime_binding" in sql:
                return binding
            if "FROM runtime_session" in sql:
                return session
            if "FROM runtime_run" in sql:
                return run
            if "FROM runtime_clarify_request" in sql:
                return None
            raise AssertionError(sql)

        def add(self, value) -> None:
            self.added = value

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            raise AssertionError("unexpected rollback")

    class Client:
        async def send(self, *, target, envelope):
            return SimpleNamespace(
                command_id=envelope.command_id,
                state="completed",
                run_id="rr-parent",
            )

        async def close(self) -> None:
            return None

    db = Db()
    service = RuntimeClarifyService(
        db,  # type: ignore[arg-type]
        timeout_seconds=3600,
        auto_default_seconds=60,
        command_client_factory=Client,
    )

    receipt = await service.accept_runtime_request(
        runtime_workspace_id=11,
        runtime_token="runtime-secret",
        payload=RuntimeClarifyCreateRequest(
            event_id="clarify:call-new",
            runtime_session_id="rs-lifecycle",
            question="选择格式",
            choices=["PDF", "Word"],
        ),
        now=NOW,
    )

    assert receipt.enabled is True
    assert db.added.auto_default_at == NOW + timedelta(seconds=60)
    assert db.added.expires_at is None


async def test_user_answer_claims_before_continuation_and_is_idempotent() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _parent(db)
            row = await _request(db)
            await db.commit()

            service = RuntimeClarifyService(db, auto_default_seconds=60)
            claimed = await service.answer(
                principal=PRINCIPAL,
                runtime_workspace_id=11,
                request_id="clr_lifecycle",
                answer=RuntimeClarifyAnswer(kind="single", text="PDF"),
                version=1,
                now=NOW,
            )

            assert claimed.status == "claimed"
            assert claimed.answer == {"kind": "single", "text": "PDF", "values": []}
            assert claimed.answer_source == "user"
            assert claimed.answered_at == NOW
            assert claimed.resume_attempt_count == 1
            assert claimed.resume_run_id is not None
            assert claimed.version == 2

            repeated = await service.answer(
                principal=PRINCIPAL,
                runtime_workspace_id=11,
                request_id="clr_lifecycle",
                answer=RuntimeClarifyAnswer(kind="single", text="PDF"),
                version=1,
                now=NOW + timedelta(seconds=1),
            )
            assert repeated.status == "claimed"
            assert repeated.version == 2
            assert repeated.resume_attempt_count == 1

            with pytest.raises(RuntimeClarifyError) as conflict:
                await service.answer(
                    principal=PRINCIPAL,
                    runtime_workspace_id=11,
                    request_id="clr_lifecycle",
                    answer=RuntimeClarifyAnswer(kind="single", text="Word"),
                    version=2,
                    now=NOW + timedelta(seconds=2),
                )
            assert conflict.value.code == "clarify_already_answered"
            assert row.answer == {"kind": "single", "text": "PDF", "values": []}
    finally:
        await engine.dispose()


async def test_known_resume_failure_preserves_answer_and_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _parent(db)
            await _request(db)
            await db.commit()
            service = RuntimeClarifyService(
                db,
                auto_default_seconds=60,
                claim_lease_seconds=120,
            )
            claimed = await service.answer(
                principal=PRINCIPAL,
                runtime_workspace_id=11,
                request_id="clr_lifecycle",
                answer=RuntimeClarifyAnswer(kind="single", text="PDF"),
                version=1,
                now=NOW,
            )
            stable_resume_id = claimed.resume_run_id

            async def fail(*args, **kwargs):
                raise PermissionError("runtime is not ready")

            monkeypatch.setattr(service, "_submit_continuation", fail)
            failed = await service.resume_claimed_request(
                principal=PRINCIPAL,
                runtime_workspace_id=11,
                request=claimed,
                chat_service=SimpleNamespace(),
                now=NOW + timedelta(seconds=1),
            )

            assert failed.status == "resume_failed"
            assert failed.answer == {"kind": "single", "text": "PDF", "values": []}
            assert failed.answer_source == "user"
            assert failed.resume_run_id == stable_resume_id
            assert failed.resume_attempt_count == 1
            assert failed.next_resume_at == NOW + timedelta(seconds=6)

            retried = await service.answer(
                principal=PRINCIPAL,
                runtime_workspace_id=11,
                request_id="clr_lifecycle",
                answer=RuntimeClarifyAnswer(kind="single", text="PDF"),
                version=failed.version,
                now=NOW + timedelta(seconds=2),
            )
            assert retried.status == "claimed"
            assert retried.resume_attempt_count == 2
            assert retried.resume_run_id == stable_resume_id
    finally:
        await engine.dispose()


async def test_immediate_answer_waits_for_parent_run_projection() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            parent = await _parent(db)
            parent.status = "running"
            await _request(db)
            await db.commit()
            service = RuntimeClarifyService(db, auto_default_seconds=60)
            claimed = await service.answer(
                principal=PRINCIPAL,
                runtime_workspace_id=11,
                request_id="clr_lifecycle",
                answer=RuntimeClarifyAnswer(kind="single", text="PDF"),
                version=1,
                now=NOW,
            )

            async def project_parent_terminal() -> None:
                await asyncio.sleep(0.01)
                async with factory() as other:
                    run = await other.get(RuntimeRun, 31)
                    assert run is not None
                    run.status = "parked_for_clarify"
                    await other.commit()

            async def submit(*args, **kwargs):
                asserted_parent = kwargs["parent"]
                assert asserted_parent.status == "parked_for_clarify"
                return "rr-clarify-resume"

            projection = asyncio.create_task(project_parent_terminal())
            monkeypatch = pytest.MonkeyPatch()
            monkeypatch.setattr(service, "_submit_continuation", submit)
            try:
                answered = await service.resume_claimed_request(
                    principal=PRINCIPAL,
                    runtime_workspace_id=11,
                    request=claimed,
                    chat_service=SimpleNamespace(),
                    now=NOW + timedelta(seconds=1),
                )
            finally:
                await projection
                monkeypatch.undo()

            assert answered.status == "answered"
            assert answered.resume_run_id == "rr-clarify-resume"
    finally:
        await engine.dispose()


async def test_parent_terminal_wait_timeout_keeps_request_retryable() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            parent = await _parent(db)
            parent.status = "running"
            await _request(db)
            await db.commit()
            service = RuntimeClarifyService(db, auto_default_seconds=60)
            claimed = await service.answer(
                principal=PRINCIPAL,
                runtime_workspace_id=11,
                request_id="clr_lifecycle",
                answer=RuntimeClarifyAnswer(kind="single", text="PDF"),
                version=1,
                now=NOW,
            )
            service.parent_terminal_wait_seconds = 0.01
            submit = AsyncMock(return_value="rr-should-not-run")
            monkeypatch = pytest.MonkeyPatch()
            monkeypatch.setattr(service, "_submit_continuation", submit)
            try:
                failed = await service.resume_claimed_request(
                    principal=PRINCIPAL,
                    runtime_workspace_id=11,
                    request=claimed,
                    chat_service=SimpleNamespace(),
                    now=NOW + timedelta(seconds=1),
                )
            finally:
                monkeypatch.undo()

            assert failed.status == "resume_failed"
            assert failed.answer == {"kind": "single", "text": "PDF", "values": []}
            assert failed.resume_run_id == claimed.resume_run_id
            submit.assert_not_awaited()
    finally:
        await engine.dispose()


async def test_uncertain_resume_keeps_claim_and_same_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _parent(db)
            await _request(db)
            await db.commit()
            service = RuntimeClarifyService(db, auto_default_seconds=60)
            claimed = await service.answer(
                principal=PRINCIPAL,
                runtime_workspace_id=11,
                request_id="clr_lifecycle",
                answer=RuntimeClarifyAnswer(kind="skip"),
                version=1,
                now=NOW,
            )

            async def uncertain(*args, **kwargs):
                raise runtime_clarify_module.RuntimeInternalTurnError(
                    "RUNTIME_TURN_SUBMISSION_UNCERTAIN"
                )

            monkeypatch.setattr(service, "_submit_continuation", uncertain)
            result = await service.resume_claimed_request(
                principal=PRINCIPAL,
                runtime_workspace_id=11,
                request=claimed,
                chat_service=SimpleNamespace(),
                now=NOW + timedelta(seconds=1),
            )

            assert result.status == "claimed"
            assert result.resume_run_id == claimed.resume_run_id
            assert result.resume_attempt_count == 1
            assert result.next_resume_at is None
    finally:
        await engine.dispose()


async def test_success_closes_request_only_with_resume_run() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _parent(db)
            await _request(db)
            await db.commit()
            service = RuntimeClarifyService(db, auto_default_seconds=60)
            claimed = await service.answer(
                principal=PRINCIPAL,
                runtime_workspace_id=11,
                request_id="clr_lifecycle",
                answer=RuntimeClarifyAnswer(kind="skip"),
                version=1,
                now=NOW,
            )

            async def submit(*args, **kwargs):
                return None

            monkeypatch = pytest.MonkeyPatch()
            monkeypatch.setattr(service, "_submit_continuation", submit)
            answered = await service.resume_claimed_request(
                principal=PRINCIPAL,
                runtime_workspace_id=11,
                request=claimed,
                chat_service=SimpleNamespace(),
                now=NOW + timedelta(seconds=1),
            )
            monkeypatch.undo()

            assert answered.status == "answered"
            assert answered.resume_run_id is not None
            assert answered.resume_run_id == claimed.resume_run_id
            assert answered.claim_expires_at is None
            assert answered.next_resume_at is None
    finally:
        await engine.dispose()


async def test_expired_legacy_terminal_row_is_readable_and_not_resumed() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _parent(db)
            await _request(
                db,
                status="timed_out",
                expires_at=NOW - timedelta(seconds=1),
                auto_default_at=None,
            )
            await db.commit()
            service = RuntimeClarifyService(db, auto_default_seconds=60)
            rows = await service.list_for_session(
                principal=PRINCIPAL,
                runtime_workspace_id=11,
                runtime_session_id="rs-lifecycle",
                now=NOW,
            )
            assert [row.status for row in rows] == ["timed_out"]
    finally:
        await engine.dispose()
