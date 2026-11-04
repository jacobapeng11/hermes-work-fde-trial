import pytest

from app.models import RuntimeRun, RuntimeSession
from app.services.runtime_sessions import (
    CreateRuntimeSessionCommand,
    RuntimeSessionActiveRunError,
    RuntimeSessionImmutableError,
    RuntimeSessionRecord,
    RuntimeSessionService,
    SqlAlchemyRuntimeSessionStore,
    default_runtime_session_id,
)
from app.services.runtime_workspace import Principal


class FakeRuntimeSessionStore:
    def __init__(self) -> None:
        self.records = [
            RuntimeSessionRecord(
                session_id=1,
                runtime_workspace_id=10,
                org_id=7,
                user_id=9,
                runtime_session_id="rs-10-1",
                title="Mine",
                source_type="web",
                message_count=2,
                session_model_entry_id=3,
                status="active",
            ),
            RuntimeSessionRecord(
                session_id=2,
                runtime_workspace_id=10,
                org_id=7,
                user_id=10,
                runtime_session_id="rs-10-2",
                title="Other user",
                source_type="web",
                message_count=0,
                session_model_entry_id=None,
                status="active",
            ),
        ]
        self.created: list[dict] = []

    async def list_sessions(self, *, org_id: int, user_id: int, runtime_workspace_id: int):
        return [
            record for record in self.records
            if record.org_id == org_id
            and record.user_id == user_id
            and record.runtime_workspace_id == runtime_workspace_id
        ]

    async def create_session(self, *, org_id: int, user_id: int, command: CreateRuntimeSessionCommand):
        self.created.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "runtime_workspace_id": command.runtime_workspace_id,
                "model_entry_id": command.model_entry_id,
            }
        )
        return RuntimeSessionRecord(
            session_id=3,
            runtime_workspace_id=command.runtime_workspace_id,
            org_id=org_id,
            user_id=user_id,
            runtime_session_id=default_runtime_session_id(
                runtime_workspace_id=command.runtime_workspace_id,
                session_id=3,
            ),
            title=command.title,
            source_type=command.source_type,
            message_count=0,
            session_model_entry_id=command.model_entry_id,
            status="active",
        )


class FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeDeleteSessionDb:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []
        self.deleted = []
        self.added = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return FakeScalarResult(self.results.pop(0))

    async def delete(self, record):
        self.deleted.append(record)

    def add(self, record):
        self.added.append(record)

    async def commit(self):
        self.commits += 1


class FakeCreateSessionDb:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0

    def add(self, record):
        self.added.append(record)

    async def commit(self):
        self.commits += 1

    async def refresh(self, record):
        record.id = 12


class FakeRowsResult:
    def __init__(self, rows) -> None:
        self.rows = rows

    def all(self):
        return self.rows


class FakeListSessionDb:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return FakeRowsResult(self.rows)


class CreateSessionStore(SqlAlchemyRuntimeSessionStore):
    async def _assert_workspace_owner(self, **_kwargs) -> None:
        return None


async def test_list_sessions_filters_by_platform_owner_and_runtime_workspace() -> None:
    store = FakeRuntimeSessionStore()

    result = await RuntimeSessionService(store).list_sessions(
        principal=Principal(org_id=7, user_id=9),
        runtime_workspace_id=10,
    )

    assert [record.runtime_session_id for record in result] == ["rs-10-1"]


async def test_create_session_uses_platform_owner_and_runtime_session_id() -> None:
    store = FakeRuntimeSessionStore()

    result = await RuntimeSessionService(store).create_session(
        principal=Principal(org_id=7, user_id=9),
        command=CreateRuntimeSessionCommand(
            runtime_workspace_id=10,
            title="New",
            model_entry_id=3,
        ),
    )

    assert result.runtime_session_id == "rs-10-3"
    assert result.session_model_entry_id == 3
    assert store.created == [
        {
            "org_id": 7,
            "user_id": 9,
            "runtime_workspace_id": 10,
            "model_entry_id": 3,
        }
    ]


async def test_sqlalchemy_create_session_marks_chat_log_history_complete_from_birth() -> None:
    db = FakeCreateSessionDb()

    result = await CreateSessionStore(db).create_session(
        org_id=7,
        user_id=9,
        command=CreateRuntimeSessionCommand(
            runtime_workspace_id=10,
            title="New",
            model_entry_id=3,
        ),
    )

    assert result.session_id == 12
    assert db.commits == 1
    assert len(db.added) == 1
    assert db.added[0].chat_log_backfilled_at is not None


async def test_sqlalchemy_list_sessions_projects_latest_run_in_one_query() -> None:
    session = RuntimeSession(
        id=11,
        org_id=7,
        user_id=9,
        runtime_workspace_id=10,
        runtime_agent_profile_id=3,
        runtime_session_id="rs-10-1",
        title_cache="Latest run",
        source_type="web",
        message_count_cache=2,
        status="active",
    )
    latest_run = RuntimeRun(
        id=21,
        org_id=7,
        user_id=9,
        runtime_workspace_id=10,
        session_id=11,
        runtime_session_id="rs-10-1",
        runtime_run_id="run-latest",
        status="completed",
        started_at=None,
        finished_at=None,
    )
    db = FakeListSessionDb([(session, None, None, latest_run)])

    records = await SqlAlchemyRuntimeSessionStore(db).list_sessions(
        org_id=7,
        user_id=9,
        runtime_workspace_id=10,
    )

    assert len(db.statements) == 1
    assert records[0].latest_run is not None
    assert records[0].latest_run.runtime_run_id == "run-latest"
    assert records[0].latest_run.status == "completed"
    statement = str(db.statements[0])
    assert "max(runtime_run.id)" in statement.lower()
    assert "runtime_run.org_id" in statement
    assert "runtime_run.user_id" in statement
    assert "runtime_run.runtime_workspace_id" in statement


async def test_sqlalchemy_marks_empty_session_materialized_without_incrementing_messages() -> None:
    session = RuntimeSession(
        id=11,
        org_id=7,
        user_id=9,
        runtime_workspace_id=10,
        runtime_session_id="rs-10-empty",
        message_count_cache=0,
        status="active",
    )
    db = FakeDeleteSessionDb([session])

    await SqlAlchemyRuntimeSessionStore(db).mark_hermes_materialized(
        org_id=7,
        user_id=9,
        runtime_workspace_id=10,
        runtime_session_id="rs-10-empty",
    )

    assert session.hermes_materialized_at is not None
    assert session.message_count_cache == 0
    assert db.commits == 1


async def test_sqlalchemy_delete_session_rejects_active_run_before_mutating_records() -> None:
    session = RuntimeSession(id=11, org_id=7, user_id=9, runtime_workspace_id=10, runtime_session_id="rs-10-1", message_count_cache=0, status="active")
    db = FakeDeleteSessionDb([session, 21])

    with pytest.raises(RuntimeSessionActiveRunError):
        await SqlAlchemyRuntimeSessionStore(db).delete_session(
            org_id=7,
            user_id=9,
            runtime_workspace_id=10,
            runtime_session_id="rs-10-1",
        )

    assert db.deleted == []
    assert db.added == []
    assert db.commits == 0


@pytest.mark.parametrize(
    "operation", ["rename", "mark_deleting", "delete"]
)
async def test_sqlalchemy_keeps_wecom_session_immutable(
    operation: str,
) -> None:
    session = RuntimeSession(
        id=19,
        org_id=7,
        user_id=9,
        runtime_workspace_id=10,
        runtime_session_id="rs-10-wecom",
        source_type="wecom",
        message_count_cache=3,
        status="active",
    )
    db = FakeDeleteSessionDb([session])
    store = SqlAlchemyRuntimeSessionStore(db)

    with pytest.raises(RuntimeSessionImmutableError):
        if operation == "rename":
            await store.rename_session(
                org_id=7,
                user_id=9,
                runtime_workspace_id=10,
                runtime_session_id="rs-10-wecom",
                title="不能改名",
            )
        elif operation == "mark_deleting":
            await store.mark_session_deleting(
                org_id=7,
                user_id=9,
                runtime_workspace_id=10,
                runtime_session_id="rs-10-wecom",
            )
        else:
            await store.delete_session(
                org_id=7,
                user_id=9,
                runtime_workspace_id=10,
                runtime_session_id="rs-10-wecom",
            )

    assert db.commits == 0
    assert session.status == "active"


async def test_sqlalchemy_mark_session_deleting_preserves_retriable_tombstone_on_remote_failure() -> None:
    session = RuntimeSession(id=11, org_id=7, user_id=9, runtime_workspace_id=10, runtime_session_id="rs-10-1", message_count_cache=0, status="active")
    db = FakeDeleteSessionDb([session, None, None])

    await SqlAlchemyRuntimeSessionStore(db).mark_session_deleting(
        org_id=7,
        user_id=9,
        runtime_workspace_id=10,
        runtime_session_id="rs-10-1",
    )

    assert session.status == "deleting"
    assert db.commits == 1


async def test_mark_session_deleting_pauses_referencing_scheduled_tasks_in_same_transaction(monkeypatch) -> None:
    session = RuntimeSession(id=11, org_id=7, user_id=9, runtime_workspace_id=10, runtime_session_id="rs-10-1", message_count_cache=0, status="active")
    db = FakeDeleteSessionDb([session, None])
    calls = []

    class FakeScheduledTaskRepository:
        def __init__(self, _db):
            pass

        async def pause_for_session(self, **kwargs):
            calls.append(kwargs)
            return 2

    monkeypatch.setattr("app.services.runtime_sessions.ScheduledTaskRepository", FakeScheduledTaskRepository)
    await SqlAlchemyRuntimeSessionStore(db).mark_session_deleting(
        org_id=7,
        user_id=9,
        runtime_workspace_id=10,
        runtime_session_id="rs-10-1",
    )

    assert calls == [{"org_id": 7, "user_id": 9, "runtime_workspace_id": 10, "session_id": 11}]
    assert session.status == "deleting"
    assert db.commits == 1


async def test_sqlalchemy_delete_session_tombstones_id_after_removing_related_indexes() -> None:
    session = RuntimeSession(id=11, org_id=7, user_id=9, runtime_workspace_id=10, runtime_session_id="rs-10-1", message_count_cache=0, status="active")
    db = FakeDeleteSessionDb([session, None, None, None, None])

    await SqlAlchemyRuntimeSessionStore(db).delete_session(
        org_id=7,
        user_id=9,
        runtime_workspace_id=10,
        runtime_session_id="rs-10-1",
    )

    assert db.deleted == []
    assert db.commits == 1
    assert session.status == "deleted"
    assert len(db.added) == 1
    audit = db.added[0]
    assert audit.action == "runtime_session.deleted"
    assert audit.target_type == "runtime_session"
    assert audit.target_id == "rs-10-1"
    assert audit.detail == {"message_count": 0}
    statements = [str(statement.compile(compile_kwargs={"literal_binds": True})) for statement in db.statements]
    assert any("DELETE FROM chat_file_artifact" in statement for statement in statements)
    assert any("DELETE FROM runtime_run" in statement for statement in statements)
