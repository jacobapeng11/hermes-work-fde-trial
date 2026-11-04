from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


from app.models import RuntimeClarifyRequest
from app.schemas.runtime_clarify import RuntimeClarifyCreateRequest
from app.services.runtime_clarify import RuntimeClarifyService
from app.services.runtime_workspace import RuntimeApiKeyCodec


NOW = datetime(2026, 10, 28, 10, 0, 0)


def _binding() -> SimpleNamespace:
    return SimpleNamespace(
        id=17,
        org_id=1,
        user_id=2,
        runtime_workspace_id=11,
        runtime_id="runtime-11",
        endpoint_host_header="runtime-11.local",
        api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-secret"),
    )


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        id=51,
        display_name="报告专家",
    )


def _chat_session() -> SimpleNamespace:
    return SimpleNamespace(
        id=21,
        runtime_session_id="rs-web",
        source_type="web",
    )


def _worker_run(status: str = "running") -> SimpleNamespace:
    return SimpleNamespace(
        id=81,
        status=status,
        request_payload={"expert_profile_name": "expert-51"},
    )


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        id=66,
        board="default",
        task_id="task-1",
    )


def _collaboration() -> SimpleNamespace:
    return SimpleNamespace(
        id=61,
        session_id=21,
        state="waiting_tasks",
    )


class _Rows:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, ...]]:
        return self._rows


class _Update:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class WorkerFakeDb:
    """Fake async session covering the expert-worker clarify branches."""

    def __init__(
        self,
        *,
        worker_status: str = "running",
        active_clarify: object = None,
        existing_by_event: object = None,
    ) -> None:
        self.binding = _binding()
        self.profile = _profile()
        self.chat_session = _chat_session()
        self.worker_run = _worker_run(worker_status)
        self.task = _task()
        self.collaboration = _collaboration()
        self.active_clarify = active_clarify
        self.existing_by_event = existing_by_event
        self.added: RuntimeClarifyRequest | None = None
        self.committed = 0
        self.refreshed: list[object] = []

    async def scalar(self, statement) -> object:
        sql = str(statement)
        if "FROM hermes_runtime_binding" in sql:
            return self.binding
        if "FROM runtime_session" in sql:
            # The worker passes its private Hermes session, which is not a
            # Web chat session.
            return None
        if "FROM runtime_agent_profile" in sql:
            return self.profile
        if "FROM runtime_run" in sql:
            return self.worker_run
        if "FROM runtime_clarify_request" in sql:
            if "event_id =" in sql:
                return self.existing_by_event
            return self.active_clarify
        raise AssertionError(sql)

    async def execute(self, statement):
        sql = str(statement)
        if sql.upper().startswith("UPDATE"):
            return _Update(1)
        if "expert_kanban_collaboration_task" in sql:
            return _Rows([(self.task, self.collaboration)])
        raise AssertionError(sql)

    async def get(self, model, identity: int) -> object:
        assert model.__name__ == "RuntimeSession"
        assert identity == 21
        return self.chat_session

    def add(self, value) -> None:
        self.added = value

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        return None

    async def refresh(self, value) -> None:
        # The finalize update was accepted; a real database reload would
        # return the persisted answered row.
        if getattr(value, "status", None) == "claimed":
            value.status = "answered"
        self.refreshed.append(value)


def _payload(**overrides: object) -> RuntimeClarifyCreateRequest:
    values: dict[str, object] = {
        "event_id": "clarify:call-worker",
        "runtime_session_id": "worker-hermes-session",
        "tool_call_id": "call-worker",
        "turn_id": "turn-worker",
        "question": "需要哪种报告格式？",
        "choices": ["PDF", "Word"],
        "multi_select": False,
        "runtime_agent_profile_id": 51,
    }
    values.update(overrides)
    return RuntimeClarifyCreateRequest(**values)  # type: ignore[arg-type]


async def test_expert_worker_request_attaches_to_origin_web_session() -> None:
    db = WorkerFakeDb()
    service = RuntimeClarifyService(db, auto_default_seconds=60)  # type: ignore[arg-type]

    receipt = await service.accept_runtime_request(
        runtime_workspace_id=11,
        runtime_token="runtime-secret",
        payload=_payload(),
        now=NOW,
    )

    assert receipt.enabled is True
    assert receipt.mode == "expert_worker"
    assert receipt.duplicate is False
    assert db.added is not None
    assert db.added.mode == "expert_worker"
    assert db.added.session_id == 21
    assert db.added.runtime_session_id == "rs-web"
    assert db.added.parent_run_id == 81
    assert db.added.expert_profile_id == 51
    assert db.added.collaboration_task_id == 66
    assert db.added.requested_by == "报告专家"
    assert db.added.auto_default_at == NOW + timedelta(seconds=60)
    assert db.committed == 1


async def test_expert_worker_disabled_without_active_worker_run() -> None:
    db = WorkerFakeDb(worker_status="completed")
    service = RuntimeClarifyService(db, auto_default_seconds=60)  # type: ignore[arg-type]

    receipt = await service.accept_runtime_request(
        runtime_workspace_id=11,
        runtime_token="runtime-secret",
        payload=_payload(),
        now=NOW,
    )

    assert receipt.enabled is False
    assert db.added is None


async def test_second_active_expert_worker_request_fails_closed() -> None:
    db = WorkerFakeDb(active_clarify=SimpleNamespace(status="waiting"))
    service = RuntimeClarifyService(db, auto_default_seconds=60)  # type: ignore[arg-type]

    receipt = await service.accept_runtime_request(
        runtime_workspace_id=11,
        runtime_token="runtime-secret",
        payload=_payload(event_id="clarify:call-worker-2"),
        now=NOW,
    )

    assert receipt.enabled is False
    assert db.added is None


async def test_finalize_expert_worker_clears_resume_identity() -> None:
    db = WorkerFakeDb()
    service = RuntimeClarifyService(db)  # type: ignore[arg-type]
    request = SimpleNamespace(
        id=91,
        status="claimed",
        version=3,
    )

    finalized = await service.finalize_expert_worker_request(request)  # type: ignore[arg-type]

    assert finalized.status == "answered"
    assert db.committed == 1


async def test_finalize_expert_worker_ignores_non_claimed_rows() -> None:
    db = WorkerFakeDb()
    service = RuntimeClarifyService(db)  # type: ignore[arg-type]
    request = SimpleNamespace(id=91, status="answered", version=3)

    finalized = await service.finalize_expert_worker_request(request)  # type: ignore[arg-type]

    assert finalized is request
    assert db.committed == 0


def test_runtime_clarify_model_declares_expert_worker_contract() -> None:
    columns = set(RuntimeClarifyRequest.__table__.columns.keys())
    assert {"mode", "collaboration_task_id", "expert_profile_id", "requested_by"}.issubset(
        columns
    )


def test_current_schema_upgrades_expert_worker_columns() -> None:
    content = (
        Path(__file__).resolve().parents[4]
        / "backend"
        / "scripts"
        / "current_schema.sql"
    ).read_text(encoding="utf-8")
    for column in (
        "mode",
        "collaboration_task_id",
        "expert_profile_id",
        "requested_by",
    ):
        assert column in content
