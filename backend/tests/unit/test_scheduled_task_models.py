from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from app.models import Base, RuntimeSession, ScheduledTask
from app.repositories.scheduled_task import ScheduledTaskRepository
from app.schemas.scheduled_task import ScheduledTaskCommand, ScheduledTaskSummary


def test_scheduled_task_table_is_registered_with_required_fields() -> None:
    assert "scheduled_task" in Base.metadata.tables
    columns = set(ScheduledTask.__table__.c.keys())
    assert {
        "id",
        "org_id",
        "user_id",
        "runtime_workspace_id",
        "target_session_id",
        "task_name",
        "prompt",
        "schedule_type",
        "schedule_config",
        "cron_expr",
        "timezone",
        "status",
        "next_run_at",
        "last_run_at",
        "last_run_status",
        "last_error_code",
        "last_error",
        "last_runtime_run_id",
        "run_count",
        "created_at",
        "updated_at",
    }.issubset(columns)
    assert "profile_id" not in columns


def test_scheduled_task_has_session_foreign_key_and_required_indexes() -> None:
    table = ScheduledTask.__table__
    assert {fk.target_fullname for fk in table.c.target_session_id.foreign_keys} == {
        "runtime_session.id"
    }
    indexes = {index.name: tuple(column.name for column in index.columns) for index in table.indexes}
    assert indexes["ix_scheduled_task_status_next_run"] == ("status", "next_run_at")
    assert indexes["ix_scheduled_task_owner_status_updated"] == (
        "org_id",
        "user_id",
        "status",
        "updated_at",
    )
    assert indexes["ix_scheduled_task_target_session"] == ("target_session_id",)


def test_scheduled_task_defaults_are_safe_and_last_fields_nullable() -> None:
    table = ScheduledTask.__table__
    assert table.c.timezone.default is not None
    assert table.c.timezone.default.arg == "Asia/Shanghai"
    assert table.c.run_count.default is not None
    assert table.c.run_count.default.arg == 0
    for column in (table.c.next_run_at, table.c.last_run_at, table.c.last_run_status):
        assert column.nullable is True


def test_scheduled_task_command_only_exposes_wire_fields() -> None:
    fields = set(ScheduledTaskCommand.model_fields)
    assert fields == {"task_name", "prompt", "target_runtime_session_id", "schedule_config"}
    assert "target_session_id" not in fields


def test_scheduled_task_summary_does_not_leak_numeric_session_id() -> None:
    summary = ScheduledTaskSummary(
        task_id=1,
        task_name="Daily",
        prompt="  hello\nworld  ",
        status="active",
        schedule_config={"type": "once", "run_at": "2026-07-18T10:00:00+08:00"},
        schedule_summary="执行一次",
        target_runtime_session_id="session-1",
        target_session_title="Sales",
        target_session_status="active",
        next_run_at=datetime(2026, 7, 18, 2, tzinfo=timezone.utc),
        is_running=False,
        run_count=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    payload = summary.model_dump()
    assert payload["target_runtime_session_id"] == "session-1"
    assert "target_session_id" not in payload


def test_scheduled_task_record_treats_mysql_naive_datetimes_as_utc() -> None:
    """MySQL DATETIME loses tzinfo, but scheduled instants are persisted in UTC."""

    task = SimpleNamespace(
        id=1,
        org_id=2,
        user_id=3,
        runtime_workspace_id=4,
        target_session_id=5,
        task_name="Daily",
        prompt="hello",
        schedule_type="once",
        schedule_config={"type": "once", "run_at": "2026-07-18T00:50:00Z"},
        cron_expr=None,
        timezone="Asia/Shanghai",
        status="completed",
        next_run_at=datetime(2026, 7, 18, 1, 0),
        last_run_at=datetime(2026, 7, 18, 0, 50),
        last_run_status="success",
        last_error_code=None,
        last_error=None,
        last_runtime_run_id="run-1",
        run_count=1,
        created_at=datetime(2026, 7, 17, 16, 50),
        updated_at=datetime(2026, 7, 18, 8, 50, tzinfo=timezone(timedelta(hours=8))),
    )
    session = SimpleNamespace(runtime_session_id="session-1", title_cache="Chat", status="active")

    record = ScheduledTaskRepository._record(task, session)

    for value in (record.next_run_at, record.last_run_at, record.created_at, record.updated_at):
        assert value is not None
        assert value.tzinfo == timezone.utc
    assert record.last_run_at == datetime(2026, 7, 18, 0, 50, tzinfo=timezone.utc)
    assert record.updated_at == datetime(2026, 7, 18, 0, 50, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_scheduled_task_list_uses_mysql_safe_null_last_ordering() -> None:
    """MySQL has no ``ASC NULLS LAST`` syntax, so spell out the null bucket."""

    class Result:
        def scalar_one_or_none(self):
            return 1

        def all(self):
            return []

    class Database:
        def __init__(self) -> None:
            self.statements: list[sa.Select] = []

        async def execute(self, statement):
            self.statements.append(statement)
            return Result()

    database = Database()
    repository = ScheduledTaskRepository(database)  # type: ignore[arg-type]
    assert await repository.list(org_id=1, user_id=2, runtime_workspace_id=3) == []

    statement = database.statements[-1]
    compiled = str(statement.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
    normalized = " ".join(compiled.lower().split())

    assert "nulls last" not in normalized
    assert "scheduled_task.next_run_at is null asc" in normalized
    assert "scheduled_task.next_run_at asc" in normalized
    # Equal non-null run times retain a deterministic most-recently-updated order.
    assert normalized.index("scheduled_task.next_run_at asc") < normalized.index("scheduled_task.updated_at desc")
