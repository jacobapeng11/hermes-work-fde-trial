import pytest
from sqlalchemy.exc import OperationalError

from app.models import RuntimeRun, RuntimeRunEvent
from app.services.runtime_chat import SqlAlchemyRuntimeChatStore


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value


class ListResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class EventStoreDb:
    def __init__(
        self,
        *,
        run: RuntimeRun | None,
        next_seq: int = 0,
        events: list[RuntimeRunEvent] | None = None,
        commit_errors: list[Exception] | None = None,
    ) -> None:
        self.run = run
        self.next_seq = next_seq
        self.events = events or []
        self.commit_errors = list(commit_errors or [])
        self.statements = []
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        self.statements.append(statement)
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        if "runtime_run_event.event_name = 'chat.done'" in compiled:
            existing = next((event for event in self.events if event.event_name == "chat.done"), None)
            return ScalarResult(existing)
        if "ORDER BY runtime_run_event.seq DESC" in compiled:
            return ScalarResult(self.next_seq)
        if "FROM runtime_run_event" in compiled:
            return ListResult(self.events)
        return ScalarResult(self.run)

    def add(self, record) -> None:
        self.added.append(record)

    async def commit(self) -> None:
        self.commits += 1
        if self.commit_errors:
            raise self.commit_errors.pop(0)

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.added.clear()


async def test_event_store_appends_next_sequence_for_owned_run() -> None:
    run = RuntimeRun(
        id=31,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_run_id="rr-123-1",
        status="running",
    )
    db = EventStoreDb(run=run, next_seq=4)
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    event = await store.append_runtime_run_event(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-123-1",
        event_name="chat.message.delta",
        payload={"delta": "hello"},
    )

    assert event.seq == 5
    assert event.event_name == "chat.message.delta"
    assert event.payload == {"delta": "hello"}
    assert db.commits == 1
    assert len(db.added) == 1
    persisted = db.added[0]
    assert persisted.run_id == 31
    assert persisted.runtime_run_public_id == "rr-123-1"
    statement = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "runtime_run.org_id = 7" in statement
    assert "runtime_run.user_id = 9" in statement
    assert "runtime_run.runtime_run_id = 'rr-123-1'" in statement
    sequence_statement = str(db.statements[1].compile(compile_kwargs={"literal_binds": True}))
    assert "ORDER BY runtime_run_event.seq DESC" in sequence_statement
    assert "FOR UPDATE" not in sequence_statement


async def test_event_store_binds_provider_and_appends_started_in_one_commit() -> None:
    run = RuntimeRun(
        id=31,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_run_id="rr-123-1",
        status="accepted",
    )
    db = EventStoreDb(run=run, next_seq=0)
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    event = await store.bind_runtime_provider_run_and_append_event(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-123-1",
        runtime_provider_run_id="hermes-42",
        event_name="chat.run.started",
        payload={"run_id": "hermes-42"},
    )

    assert db.commits == 1
    assert run.status == "running"
    assert run.response_summary == {"hermes_run_id": "hermes-42"}
    assert event.seq == 1
    assert event.event_name == "chat.run.started"
    assert event.payload == {"run_id": "hermes-42"}


async def test_event_store_reuses_existing_terminal_done() -> None:
    run = RuntimeRun(
        id=31,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_run_id="rr-123-1",
        status="canceled",
    )
    existing = RuntimeRunEvent(
        run_id=31,
        runtime_run_public_id="rr-123-1",
        seq=8,
        event_name="chat.done",
        payload={"status": "canceled"},
    )
    db = EventStoreDb(run=run, next_seq=8, events=[existing])

    event = await SqlAlchemyRuntimeChatStore(db).append_runtime_run_event(  # type: ignore[arg-type]
        org_id=7,
        user_id=9,
        runtime_run_id="rr-123-1",
        event_name="chat.done",
        payload={"status": "canceled"},
    )

    assert event is existing
    assert db.added == []
    assert db.commits == 0


def mysql_operational_error(code: int, message: str) -> OperationalError:
    return OperationalError("INSERT INTO runtime_run_event", {}, Exception(code, message))


@pytest.mark.parametrize("error_code", [1205, 1213])
async def test_event_store_retries_transient_mysql_write_errors(error_code: int) -> None:
    run = RuntimeRun(
        id=31,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_run_id="rr-123-1",
        status="running",
    )
    db = EventStoreDb(
        run=run,
        next_seq=4,
        commit_errors=[mysql_operational_error(error_code, "transient lock failure")],
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    store = SqlAlchemyRuntimeChatStore(
        db,  # type: ignore[arg-type]
        event_write_retry_base_delay_seconds=0,
        sleep=sleep,
    )

    event = await store.append_runtime_run_event(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-123-1",
        event_name="chat.message.delta",
        payload={"delta": "hello"},
    )

    assert event.seq == 5
    assert db.commits == 2
    assert db.rollbacks == 1
    assert sleeps == [0]
    assert len(db.added) == 1


async def test_event_store_does_not_retry_non_transient_mysql_write_error() -> None:
    run = RuntimeRun(
        id=31,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_run_id="rr-123-1",
        status="running",
    )
    error = mysql_operational_error(1062, "duplicate key")
    db = EventStoreDb(run=run, next_seq=4, commit_errors=[error])
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    store = SqlAlchemyRuntimeChatStore(db, sleep=sleep)  # type: ignore[arg-type]

    with pytest.raises(OperationalError) as raised:
        await store.append_runtime_run_event(
            org_id=7,
            user_id=9,
            runtime_run_id="rr-123-1",
            event_name="chat.message.delta",
            payload={"delta": "hello"},
        )

    assert raised.value is error
    assert db.commits == 1
    assert db.rollbacks == 1
    assert sleeps == []


async def test_event_store_propagates_transient_error_after_retry_budget() -> None:
    run = RuntimeRun(
        id=31,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_run_id="rr-123-1",
        status="running",
    )
    errors = [mysql_operational_error(1213, f"deadlock {attempt}") for attempt in range(3)]
    db = EventStoreDb(run=run, next_seq=4, commit_errors=errors)

    async def sleep(_delay: float) -> None:
        return None

    store = SqlAlchemyRuntimeChatStore(
        db,  # type: ignore[arg-type]
        event_write_retry_base_delay_seconds=0,
        sleep=sleep,
    )

    with pytest.raises(OperationalError):
        await store.append_runtime_run_event(
            org_id=7,
            user_id=9,
            runtime_run_id="rr-123-1",
            event_name="chat.message.delta",
            payload={"delta": "hello"},
        )

    assert db.commits == 3
    assert db.rollbacks == 3


async def test_event_store_replays_only_owned_events_after_sequence() -> None:
    events = [
        RuntimeRunEvent(
            id=101,
            run_id=31,
            runtime_run_public_id="rr-123-1",
            seq=3,
            event_name="chat.message.delta",
            payload={"delta": "later"},
        )
    ]
    db = EventStoreDb(run=None, events=events)
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    replay = await store.list_runtime_run_events(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-123-1",
        after_seq=2,
    )

    assert [(event.seq, event.event_name, event.payload) for event in replay] == [
        (3, "chat.message.delta", {"delta": "later"})
    ]
    statement = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "runtime_run.org_id = 7" in statement
    assert "runtime_run.user_id = 9" in statement
    assert "runtime_run.runtime_run_id = 'rr-123-1'" in statement
    assert "runtime_run_event.seq > 2" in statement
    assert "ORDER BY runtime_run_event.seq ASC" in statement


async def test_event_store_preserves_reasoning_delta_for_replay() -> None:
    run = RuntimeRun(
        id=31,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_run_id="rr-123-1",
        status="running",
    )
    db = EventStoreDb(run=run, next_seq=7)
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    event = await store.append_runtime_run_event(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-123-1",
        event_name="chat.reasoning.delta",
        payload={"delta": "thought"},
    )

    assert event.seq == 8
    assert event.event_name == "chat.reasoning.delta"
    assert event.payload == {"delta": "thought"}


async def test_event_store_recovers_failed_run_with_terminal_events_atomically() -> None:
    run = RuntimeRun(
        id=31,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        runtime_run_id="rr-123-1",
        status="running",
    )
    db = EventStoreDb(run=run, next_seq=4)
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    events = await store.recover_runtime_run_failure(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-123-1",
        error_message="runtime event persistence failed",
    )

    assert run.status == "failed"
    assert run.error_message == "runtime event persistence failed"
    assert [(event.seq, event.event_name) for event in events] == [
        (5, "chat.error"),
        (6, "chat.done"),
    ]
    assert events[0].payload["message"] == "runtime event persistence failed"
    assert events[1].payload["status"] == "failed"
    assert db.commits == 1
    parent_statement = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    sequence_statement = str(db.statements[1].compile(compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" in parent_statement
    assert "FOR UPDATE" not in sequence_statement


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "canceled", "cancel_failed"])
async def test_event_store_recovery_preserves_existing_terminal_status(terminal_status: str) -> None:
    run = RuntimeRun(
        id=31,
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_session_id="rs-123-1",
        runtime_run_id="rr-123-1",
        status=terminal_status,
    )
    db = EventStoreDb(run=run, next_seq=4)
    store = SqlAlchemyRuntimeChatStore(db)  # type: ignore[arg-type]

    events = await store.recover_runtime_run_failure(
        org_id=7,
        user_id=9,
        runtime_run_id="rr-123-1",
        error_message="runtime event persistence failed",
    )

    assert events == []
    assert run.status == terminal_status
    assert db.commits == 1
    assert db.added == []
