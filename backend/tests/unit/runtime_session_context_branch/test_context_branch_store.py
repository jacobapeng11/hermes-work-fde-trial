from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, ChatLog, CloudHermesProfile, Organization, RuntimeAgentProfile, RuntimeRun, RuntimeSession, User
from app.api.v1.user import runtime_sessions as api
from app.config import settings
from app.hermes_runtime.protocol import HermesProtocolError
from app.services.runtime_sessions import (
    CreateRuntimeSessionCommand,
    RuntimeSessionActiveRunError,
    RuntimeSessionContextBranchError,
    SqlAlchemyRuntimeSessionStore,
)


ROOT = Path(__file__).resolve().parents[4]


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_sqlite_text(_type, _compiler, **_kwargs) -> str:
    return "TEXT"


class OwnedWorkspaceSessionStore(SqlAlchemyRuntimeSessionStore):
    async def _assert_workspace_owner(self, **_kwargs) -> None:
        return None


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add_all(
                [
                    Organization(id=1, name="Org", code="org-1", status="active"),
                    User(id=9, org_id=1, name="User", status="active"),
                    CloudHermesProfile(
                        id=10,
                        org_id=1,
                        user_id=9,
                        profile_type="default_chat_agent",
                        name="数字人",
                        status="active",
                    ),
                    RuntimeAgentProfile(
                        id=1,
                        org_id=1,
                        user_id=9,
                        runtime_workspace_id=10,
                        hermes_profile_name="default",
                        profile_kind="default",
                        display_name="个人",
                        default_workspace_key=10,
                        desired_revision=1,
                        applied_revision=1,
                        status="active",
                    ),
                    RuntimeAgentProfile(
                        id=2,
                        org_id=1,
                        user_id=9,
                        runtime_workspace_id=10,
                        hermes_profile_name="expert-2",
                        profile_kind="expert",
                        display_name="市场专家",
                        desired_revision=1,
                        applied_revision=1,
                        status="active",
                    ),
                ]
            )
            await db.commit()
        return engine, factory
    except BaseException:
        await engine.dispose()
        raise


async def _add_source_session(db: AsyncSession, *, active_run: bool = False) -> None:
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    source = RuntimeSession(
        id=100,
        org_id=1,
        user_id=9,
        runtime_workspace_id=10,
        runtime_agent_profile_id=1,
        runtime_session_id="rs-10-source",
        source_type="web",
        title_cache="市场调研",
        message_count_cache=2,
        chat_log_backfilled_at=now,
        status="active",
    )
    db.add(source)
    db.add_all(
        [
            ChatLog(
                org_id=1,
                user_id=9,
                runtime_workspace_id=10,
                session_id=100,
                runtime_session_id="rs-10-source",
                runtime_run_id="run-source",
                message_seq=0,
                role="user",
                content="帮我分析市场",
                content_format="text",
                message_status="accepted",
                channel="web",
                source_type="web",
                ingest_source="runtime_run",
                occurred_at=now,
            ),
            ChatLog(
                org_id=1,
                user_id=9,
                runtime_workspace_id=10,
                session_id=100,
                runtime_session_id="rs-10-source",
                runtime_run_id="run-source",
                message_seq=1,
                role="assistant",
                content="已完成初步分析",
                content_format="markdown",
                message_status="completed",
                channel="web",
                source_type="web",
                ingest_source="runtime_run",
                occurred_at=now,
                completed_at=now,
            ),
        ]
    )
    if active_run:
        db.add(
            RuntimeRun(
                org_id=1,
                user_id=9,
                runtime_workspace_id=10,
                session_id=100,
                runtime_session_id="rs-10-source",
                runtime_run_id="run-active",
                status="running",
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_create_context_branch_copies_visible_chat_log() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _add_source_session(db)
            record = await OwnedWorkspaceSessionStore(db).create_session(
                org_id=1,
                user_id=9,
                command=CreateRuntimeSessionCommand(
                    runtime_workspace_id=10,
                    model_entry_id=3,
                    runtime_agent_profile_id=2,
                    context_source_runtime_session_id="rs-10-source",
                ),
            )
            assert record.context_source_runtime_session_id == "rs-10-source"
            assert record.title == "市场调研"
            assert record.message_count == 2
            branch = await db.get(RuntimeSession, record.session_id)
            copied = (
                await db.execute(
                    ChatLog.__table__.select().where(
                        ChatLog.session_id == branch.id
                    )
                )
            ).all()
            assert [(row.role, row.content, row.runtime_run_id) for row in copied] == [
                ("user", "帮我分析市场", None),
                ("assistant", "已完成初步分析", None),
            ]
            assert {row.ingest_source for row in copied} == {"hermes_backfill"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_context_branch_rejects_active_source_run() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _add_source_session(db, active_run=True)
            with pytest.raises(RuntimeSessionActiveRunError):
                await OwnedWorkspaceSessionStore(db).create_session(
                    org_id=1,
                    user_id=9,
                    command=CreateRuntimeSessionCommand(
                        runtime_workspace_id=10,
                        runtime_agent_profile_id=2,
                        context_source_runtime_session_id="rs-10-source",
                    ),
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_context_branch_rejects_unprojected_source_run() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _add_source_session(db)
            db.add(
                RuntimeRun(
                    org_id=1,
                    user_id=9,
                    runtime_workspace_id=10,
                    session_id=100,
                    runtime_session_id="rs-10-source",
                    runtime_run_id="run-completed",
                    status="completed",
                    chat_log_projected_at=None,
                )
            )
            await db.commit()
            with pytest.raises(RuntimeSessionContextBranchError):
                await OwnedWorkspaceSessionStore(db).create_session(
                    org_id=1,
                    user_id=9,
                    command=CreateRuntimeSessionCommand(
                        runtime_workspace_id=10,
                        runtime_agent_profile_id=2,
                        context_source_runtime_session_id="rs-10-source",
                    ),
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_context_chain_carries_history_and_updates_head() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _add_source_session(db)
            store = OwnedWorkspaceSessionStore(db)
            expert_branch = await store.create_session(
                org_id=1,
                user_id=9,
                command=CreateRuntimeSessionCommand(
                    runtime_workspace_id=10,
                    model_entry_id=3,
                    runtime_agent_profile_id=2,
                    context_source_runtime_session_id="rs-10-source",
                ),
            )
            personal_branch = await store.create_session(
                org_id=1,
                user_id=9,
                command=CreateRuntimeSessionCommand(
                    runtime_workspace_id=10,
                    model_entry_id=3,
                    runtime_agent_profile_id=1,
                    context_source_runtime_session_id=expert_branch.runtime_session_id,
                ),
            )
            source = await db.get(RuntimeSession, 100)
            expert_logs = (
                await db.execute(ChatLog.__table__.select().where(
                    ChatLog.session_id == expert_branch.session_id
                ))
            ).all()
            personal_logs = (
                await db.execute(ChatLog.__table__.select().where(
                    ChatLog.session_id == personal_branch.session_id
                ))
            ).all()

            assert source.context_head_runtime_session_id == personal_branch.runtime_session_id
            assert personal_branch.context_source_runtime_session_id == expert_branch.runtime_session_id
            assert [row.context_runtime_agent_profile_id for row in expert_logs] == [1, 1]
            assert [row.context_runtime_agent_profile_id for row in personal_logs] == [1, 1]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_context_chain_rejects_switching_to_the_same_profile() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _add_source_session(db)
            store = OwnedWorkspaceSessionStore(db)
            expert_branch = await store.create_session(
                org_id=1,
                user_id=9,
                command=CreateRuntimeSessionCommand(
                    runtime_workspace_id=10,
                    model_entry_id=3,
                    runtime_agent_profile_id=2,
                    context_source_runtime_session_id="rs-10-source",
                ),
            )
            with pytest.raises(RuntimeSessionContextBranchError):
                await store.create_session(
                    org_id=1,
                    user_id=9,
                    command=CreateRuntimeSessionCommand(
                        runtime_workspace_id=10,
                        model_entry_id=3,
                        runtime_agent_profile_id=2,
                        context_source_runtime_session_id=expert_branch.runtime_session_id,
                    ),
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_sessions_keeps_logical_roots_with_active_heads() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            started_at = datetime(2026, 11, 3, tzinfo=timezone.utc)
            root_ids = {f"rs-10-root-{index}" for index in range(50)}
            head_ids = {f"rs-10-head-{index}" for index in range(50)}
            sessions: list[RuntimeSession] = []
            for index in range(50):
                root_id = f"rs-10-root-{index}"
                head_id = f"rs-10-head-{index}"
                sessions.append(
                    RuntimeSession(
                        id=1000 + index,
                        org_id=1,
                        user_id=9,
                        runtime_workspace_id=10,
                        runtime_agent_profile_id=1,
                        runtime_session_id=root_id,
                        source_type="web",
                        title_cache=f"任务 {index}",
                        message_count_cache=0,
                        status="active",
                        last_active_at=started_at + timedelta(minutes=index),
                        context_head_runtime_session_id=head_id,
                    )
                )
                source_id = (
                    "rs-10-intermediate"
                    if index == 0
                    else f"rs-10-root-{index}"
                )
                sessions.append(
                    RuntimeSession(
                        id=2000 + index,
                        org_id=1,
                        user_id=9,
                        runtime_workspace_id=10,
                        runtime_agent_profile_id=2,
                        runtime_session_id=head_id,
                        source_type="web",
                        title_cache=f"任务 {index}",
                        message_count_cache=0,
                        status="active",
                        last_active_at=started_at
                        + timedelta(hours=2, minutes=index),
                        context_source_runtime_session_id=source_id,
                    )
                )
            sessions.append(
                RuntimeSession(
                    id=3000,
                    org_id=1,
                    user_id=9,
                    runtime_workspace_id=10,
                    runtime_agent_profile_id=1,
                    runtime_session_id="rs-10-intermediate",
                    source_type="web",
                    title_cache="任务 0",
                    message_count_cache=0,
                    status="active",
                    last_active_at=started_at + timedelta(hours=1),
                    context_source_runtime_session_id="rs-10-root-0",
                )
            )
            db.add_all(sessions)
            await db.commit()

            records = await OwnedWorkspaceSessionStore(db).list_sessions(
                org_id=1,
                user_id=9,
                runtime_workspace_id=10,
            )
            record_ids = {record.runtime_session_id for record in records}

            assert len(records) == 100
            assert root_ids <= record_ids
            assert head_ids <= record_ids
            assert "rs-10-intermediate" not in record_ids
            assert records[0].runtime_session_id == "rs-10-root-49"
            assert records[49].runtime_session_id == "rs-10-root-0"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_marking_head_deleting_restores_previous_context_head() -> None:
    engine, factory = await _database()
    try:
        async with factory() as db:
            await _add_source_session(db)
            store = OwnedWorkspaceSessionStore(db)
            expert_branch = await store.create_session(
                org_id=1,
                user_id=9,
                command=CreateRuntimeSessionCommand(
                    runtime_workspace_id=10,
                    model_entry_id=3,
                    runtime_agent_profile_id=2,
                    context_source_runtime_session_id="rs-10-source",
                ),
            )
            personal_branch = await store.create_session(
                org_id=1,
                user_id=9,
                command=CreateRuntimeSessionCommand(
                    runtime_workspace_id=10,
                    model_entry_id=3,
                    runtime_agent_profile_id=1,
                    context_source_runtime_session_id=expert_branch.runtime_session_id,
                ),
            )
            await store.mark_session_deleting(
                org_id=1,
                user_id=9,
                runtime_workspace_id=10,
                runtime_session_id=personal_branch.runtime_session_id,
            )
            source = await db.get(RuntimeSession, 100)

            assert source.context_head_runtime_session_id == expert_branch.runtime_session_id
    finally:
        await engine.dispose()


def test_current_schema_registers_context_source_column() -> None:
    content = (ROOT / "backend/scripts/current_schema.sql").read_text(encoding="utf-8")
    assert "TABLE_NAME = 'runtime_session'" in content
    assert "COLUMN_NAME = 'context_source_runtime_session_id'" in content
    assert "ADD COLUMN context_source_runtime_session_id VARCHAR(128) NULL" in content
    assert "ix_runtime_session_context_source_runtime_session_id" in content
    assert "ADD COLUMN context_head_runtime_session_id VARCHAR(128) NULL" in content
    assert "ADD COLUMN context_runtime_agent_profile_id BIGINT NULL" in content


def test_context_payload_shape_is_public_messages_only(monkeypatch) -> None:
    from app.services.runtime_workspace import Principal

    captured: dict[str, object] = {}

    class Adapter:
        async def dashboard_json_request(
            self,
            _endpoint,
            method,
            path,
            *,
            json_body,
        ):
            captured.update({"method": method, "path": path, "body": json_body})
            return {
                "ok": True,
                "imported": 1,
                "skipped": 0,
                "imported_ids": ["rs-10-branch"],
                "skipped_ids": [],
            }

    class Projector:
        async def project_owned_session(self, **_kwargs):
            return 0

        async def history_for_owner(self, **_kwargs):
            return SimpleNamespace(
                messages=[
                    {"role": "user", "content": "帮我分析市场"},
                    {"role": "assistant", "content": "已完成初步分析"},
                    {"role": "assistant", "content": ""},
                ]
            )

    class ChatService:
        async def get_session_history_control(self, **_kwargs):
            return SimpleNamespace(endpoint=object(), hermes_profile_name="expert-2")

    monkeypatch.setattr(api, "HermesDashboardProtocolAdapter", Adapter)
    record = SimpleNamespace(
        runtime_session_id="rs-10-branch",
        message_count=2,
    )
    import asyncio

    asyncio.run(
        api._import_context_branch_history(
            principal=Principal(org_id=1, user_id=9),
            runtime_workspace_id=10,
            created=record,
            chat_service=ChatService(),
            chat_log_projector=Projector(),
        )
    )
    body = captured["body"]
    assert captured["path"] == "/api/sessions/import"
    assert body["profile"] == "expert-2"
    assert body["sessions"][0]["messages"] == [
        {"role": "user", "content": "帮我分析市场"},
        {"role": "assistant", "content": "已完成初步分析"},
    ]


@pytest.mark.asyncio
async def test_runtime_import_failure_hides_partial_context_branch(monkeypatch) -> None:
    from app.schemas.runtime_chat import CreateRuntimeSessionRequest
    from app.services.runtime_sessions import RuntimeSessionRecord
    from app.services.runtime_workspace import Principal

    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    calls: list[str] = []
    source = RuntimeSessionRecord(
        session_id=100,
        runtime_workspace_id=10,
        org_id=1,
        user_id=9,
        runtime_session_id="rs-10-source",
        title="市场调研",
        source_type="web",
        message_count=2,
        session_model_entry_id=3,
        status="active",
    )
    branch = RuntimeSessionRecord(
        session_id=101,
        runtime_workspace_id=10,
        org_id=1,
        user_id=9,
        runtime_session_id="rs-10-branch",
        title="市场调研",
        source_type="web",
        message_count=2,
        session_model_entry_id=3,
        status="active",
        runtime_agent_profile_id=2,
        context_source_runtime_session_id="rs-10-source",
    )

    class SessionService:
        def __init__(self) -> None:
            self.deleting: list[str] = []

        async def get_session(self, **_kwargs):
            calls.append("service-get-source")
            return source

        async def create_session(self, **_kwargs):
            return branch

        async def mark_session_deleting(self, *, runtime_session_id, **_kwargs):
            self.deleting.append(runtime_session_id)

    class ModelService:
        async def list_models(self, _principal):
            return [SimpleNamespace(model_entry_id=3)]

    class Projector:
        async def project_owned_session(self, **_kwargs):
            calls.append("project-source-or-branch")
            return 0

        async def history_for_owner(self, **_kwargs):
            return SimpleNamespace(
                messages=[
                    {"role": "user", "content": "帮我分析市场"},
                    {"role": "assistant", "content": "已完成初步分析"},
                ]
            )

    class ChatService:
        async def get_session_history_control(self, **_kwargs):
            return SimpleNamespace(endpoint=object(), hermes_profile_name="expert-2")

    class FailingAdapter:
        async def dashboard_json_request(self, *_args, **_kwargs):
            raise HermesProtocolError("import failed")

    monkeypatch.setattr(api, "HermesDashboardProtocolAdapter", FailingAdapter)
    service = SessionService()
    with pytest.raises(HTTPException) as exc_info:
        await api.create_runtime_session(
            runtime_workspace_id=10,
            payload=CreateRuntimeSessionRequest(
                model_entry_id=3,
                runtime_agent_profile_id=2,
                context_source_runtime_session_id="rs-10-source",
            ),
            principal=Principal(org_id=1, user_id=9),
            service=service,
            model_service=ModelService(),
            chat_service=ChatService(),
            chat_log_projector=Projector(),
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "expert context import failed"
    assert service.deleting == ["rs-10-branch"]
    assert calls == ["project-source-or-branch", "service-get-source", "project-source-or-branch"]
