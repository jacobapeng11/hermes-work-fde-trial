from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import BigInteger, func, select
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.main import create_app
from app.models import (
    Base,
    CloudHermesProfile,
    ExpertKanbanCollaboration,
    ExpertKanbanCollaborationTask,
    HermesRuntimeBinding,
    ModelEntry,
    ModelProvider,
    Organization,
    RuntimeAgentProfile,
    RuntimeMaintenanceControl,
    RuntimeRun,
    RuntimeSession,
    User,
)
from app.schemas.runtime_kanban import RuntimeKanbanLeaseEventRequest
from app.services.expert_collaboration_reconciler import (
    ExpertCollaborationReconciler,
)
from app.services.expert_collaboration_runner import _claim_context_statement
from app.services.expert_collaborations import ExpertCollaborationService
from app.services.expert_kanban_worker_runs import (
    ExpertKanbanWorkerRunService,
)
from app.services.expert_kanban_worker_routes import (
    ExpertKanbanWorkerRouteService,
)
from app.services.provider_credentials import ProviderCredentialCodec
from app.services.runtime_workspace import RuntimeApiKeyCodec


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(
    _type: BigInteger,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "INTEGER"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_sqlite_text(
    _type: LONGTEXT,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "TEXT"


class FakeProfileAdapter:
    def __init__(self, payloads: list[dict[str, Any] | Exception]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, str]] = []

    async def get_kanban_task_detail(
        self,
        _endpoint,
        *,
        task_id: str,
        board: str,
    ) -> dict[str, Any]:
        self.calls.append((board, task_id))
        value = self.payloads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeRunner:
    def __init__(self) -> None:
        self.launched: list[int] = []

    def launch(self, collaboration_id: int) -> bool:
        self.launched.append(collaboration_id)
        return True


class FakeRuntimeCommandClient:
    def __init__(self) -> None:
        self.sent: list[tuple[Any, Any]] = []

    async def send(self, *, target, envelope):
        from app.schemas.runtime_internal import RuntimeCommandAck

        self.sent.append((target, envelope))
        return RuntimeCommandAck(
            command_id=envelope.command_id,
            state="completed",
            result={"run_id": getattr(envelope.payload, "run_id", "")},
        )

    async def close(self) -> None:
        return None


async def _database() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add_all(
            [
                Organization(id=1, name="Org", code="org-1", status="active"),
                User(id=1, org_id=1, name="User", status="active"),
                CloudHermesProfile(
                    id=10,
                    org_id=1,
                    user_id=1,
                    profile_type="default_chat_agent",
                    name="默认数字人",
                    status="active",
                ),
                RuntimeMaintenanceControl(
                    id=1,
                    maintenance_state="open",
                    admission_generation=1,
                    grant_issuance_enabled=True,
                ),
            ]
        )
        await db.flush()
        db.add(
            ModelProvider(
                id=60,
                org_id=1,
                provider_key="platform-test",
                display_name="平台模型",
                provider_type="openai_compatible",
                api_endpoint="https://model.invalid/v1",
                api_key_encrypted=ProviderCredentialCodec().encrypt(
                    "provider-secret"
                ),
                status="active",
            )
        )
        await db.flush()
        db.add_all(
            [
                ModelEntry(
                    id=61,
                    org_id=1,
                    provider_id=60,
                    model_key="platform-default",
                    display_name="平台默认模型",
                    context_length=128000,
                    is_default=True,
                    is_restricted=False,
                    status="active",
                ),
                ModelEntry(
                    id=62,
                    org_id=1,
                    provider_id=60,
                    model_key="session-selected",
                    display_name="会话所选模型",
                    context_length=64000,
                    is_default=False,
                    is_restricted=False,
                    status="active",
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                HermesRuntimeBinding(
                    id=20,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=10,
                    runtime_id="rt-10",
                    image="runtime:test",
                    k8s_namespace="runtimes",
                    knative_service="runtime-user",
                    endpoint_base_url="http://runtime.invalid",
                    endpoint_host_header="runtime.invalid",
                    hermes_home_sub_path="home",
                    workspace_sub_path="workspace",
                    hermes_home_mount_path="/opt/data",
                    workspace_mount_path="/data/workspace",
                    api_key_encrypted=RuntimeApiKeyCodec().encrypt("secret"),
                    status="ready",
                ),
                RuntimeAgentProfile(
                    id=30,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=10,
                    hermes_profile_name="default",
                    profile_kind="default",
                    display_name="自己",
                    default_workspace_key=10,
                    config_generation=1,
                    applied_config_generation=1,
                    status="active",
                ),
                RuntimeAgentProfile(
                    id=31,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=10,
                    hermes_profile_name="expert-31",
                    profile_kind="expert",
                    display_name="市场洞察专家",
                    config_generation=1,
                    applied_config_generation=1,
                    status="active",
                ),
            ]
        )
        await db.flush()
        db.add(
            RuntimeSession(
                id=40,
                org_id=1,
                user_id=1,
                runtime_workspace_id=10,
                runtime_agent_profile_id=30,
                runtime_session_id="rs-main",
                source_type="web",
                status="active",
                session_model_entry_id=62,
            )
        )
        await db.flush()
        db.add(
            RuntimeRun(
                id=50,
                org_id=1,
                user_id=1,
                runtime_workspace_id=10,
                session_id=40,
                runtime_session_id="rs-main",
                runtime_run_id="rr-origin",
                model_entry_id=62,
                status="running",
                source_type="web",
                request_payload={"message": "请让多个专家分析市场"},
            )
        )
        await db.commit()
    return engine, sessions


def _create_event(
    task_id: str,
    *,
    runtime_agent_profile_id: int = 30,
    runtime_session_id: str = "rs-main",
) -> RuntimeKanbanLeaseEventRequest:
    return RuntimeKanbanLeaseEventRequest(
        event_id=f"post_tool_call:{task_id}",
        event_name="post_tool_call",
        runtime_agent_profile_id=runtime_agent_profile_id,
        tool_name="kanban_create",
        task_id=task_id,
        board="default",
        outcome="ok",
        runtime_session_id=runtime_session_id,
        tool_call_id=f"call-{task_id}",
        assignee="expert-31",
    )


def test_background_collaboration_routes_are_user_authenticated_gets() -> None:
    paths = create_app().openapi()["paths"]

    assert "get" in paths["/api/v1/user/chat/background-collaborations"]
    assert "get" in paths[
        "/api/v1/user/chat/background-collaborations/stream"
    ]


def test_synthesis_claim_query_is_valid_for_mysql_join_order() -> None:
    sql = str(
        _claim_context_statement(origin_run_id=50, session_id=40).compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert (
        "JOIN runtime_run ON runtime_run.session_id = runtime_session.id"
        in sql
    )
    assert "expert_kanban_collaboration" not in sql


@pytest.mark.asyncio
async def test_default_profile_create_events_group_native_tasks_by_origin_run() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 7, 26, 12, 0, 0)
    try:
        async with sessions() as db:
            service = ExpertCollaborationService(db)
            first = await service.observe_runtime_event(
                runtime_workspace_id=10,
                event=_create_event("task-1"),
                now=now,
            )
            second = await service.observe_runtime_event(
                runtime_workspace_id=10,
                event=_create_event("task-2"),
                now=now + timedelta(seconds=1),
            )
            response = await service.list_owned(
                org_id=1,
                user_id=1,
                runtime_workspace_id=10,
                runtime_session_id="rs-main",
            )
            collaboration_count = await db.scalar(
                select(func.count(ExpertKanbanCollaboration.id))
            )
            task_count = await db.scalar(
                select(func.count(ExpertKanbanCollaborationTask.id))
            )

            assert first == second
            assert collaboration_count == 1
            assert task_count == 2
            assert response.pending_count == 1
            assert response.collaborations[0].origin_runtime_run_id == "rr-origin"
            assert response.collaborations[0].task_count == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expert_profile_and_unrelated_session_events_do_not_bind() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            service = ExpertCollaborationService(db)

            assert await service.observe_runtime_event(
                runtime_workspace_id=10,
                event=_create_event(
                    "expert-task",
                    runtime_agent_profile_id=31,
                ),
            ) is None
            assert await service.observe_runtime_event(
                runtime_workspace_id=10,
                event=_create_event(
                    "nested-task",
                    runtime_session_id="kanban-worker-session",
                ),
            ) is None
            assert await db.scalar(
                select(func.count(ExpertKanbanCollaboration.id))
            ) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bound_expert_task_gets_isolated_platform_default_worker_run() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 7, 26, 12, 0, 0)
    try:
        async with sessions() as db:
            event = _create_event("task-1")
            collaboration_id = await ExpertCollaborationService(
                db
            ).observe_runtime_event(
                runtime_workspace_id=10,
                event=event,
                now=now,
            )
            assert collaboration_id is not None
            run_id = await ExpertKanbanWorkerRunService(
                db
            ).observe_runtime_event(
                runtime_workspace_id=10,
                event=event,
                now=now,
            )
            run = await db.scalar(
                select(RuntimeRun).where(
                    RuntimeRun.runtime_run_id == run_id
                )
            )

            assert run is not None
            assert run.source_type == "expert_kanban_worker"
            assert run.status == "accepted"
            assert run.session_id is None
            assert run.runtime_session_id is None
            # The Web session selected model 62. Expert profiles use the
            # administrator's platform default instead of inheriting it.
            assert run.model_entry_id == 61
            assert run.request_payload == {
                "kind": "expert_kanban_worker",
                "collaboration_public_id": collaboration_id,
                "board": "default",
                "task_id": "task-1",
                "expert_profile_id": 31,
                "expert_profile_name": "expert-31",
                "expert_display_name": "市场洞察专家",
            }
            claimed = _create_event("task-1")
            claimed.event_name = "kanban_task_claimed"
            await ExpertKanbanWorkerRunService(db).observe_runtime_event(
                runtime_workspace_id=10,
                event=claimed,
                now=now + timedelta(seconds=1),
            )
            await db.refresh(run)
            assert run.status == "running"

            completed = _create_event("task-1")
            completed.event_name = "kanban_task_completed"
            await ExpertKanbanWorkerRunService(db).observe_runtime_event(
                runtime_workspace_id=10,
                event=completed,
                now=now + timedelta(seconds=2),
            )
            await db.refresh(run)
            assert run.status == "completed"
            assert run.finished_at == now + timedelta(seconds=2)
            assert run.response_summary == {"task_status": "done"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_run_is_not_created_for_unbound_or_nonexpert_task() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            service = ExpertKanbanWorkerRunService(db)
            assert await service.observe_runtime_event(
                runtime_workspace_id=10,
                event=_create_event("unbound"),
            ) is None

            event = _create_event("task-1")
            await ExpertCollaborationService(db).observe_runtime_event(
                runtime_workspace_id=10,
                event=event,
            )
            event.assignee = "default"
            assert await service.observe_runtime_event(
                runtime_workspace_id=10,
                event=event,
            ) is None
            assert await db.scalar(
                select(func.count(RuntimeRun.id)).where(
                    RuntimeRun.source_type == "expert_kanban_worker"
                )
            ) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_route_service_activates_and_revokes_exact_task_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "PROVIDER_ROUTE_ISSUANCE_MODE", "off")
    engine, sessions = await _database()
    client = FakeRuntimeCommandClient()
    try:
        async with sessions() as db:
            event = _create_event("task-route")
            await ExpertCollaborationService(db).observe_runtime_event(
                runtime_workspace_id=10,
                event=event,
            )
            await ExpertKanbanWorkerRunService(db).observe_runtime_event(
                runtime_workspace_id=10,
                event=event,
            )
            service = ExpertKanbanWorkerRouteService(
                db,
                client=client,  # type: ignore[arg-type]
            )

            activated = await service.activate_for_task(
                runtime_workspace_id=10,
                board="default",
                task_id="task-route",
            )
            revoked = await service.revoke_for_task(
                runtime_workspace_id=10,
                board="default",
                task_id="task-route",
            )

            assert activated is True
            assert revoked is True
            assert len(client.sent) == 2
            target, activate = client.sent[0]
            assert target.runtime_id == "rt-10"
            assert target.binding_id == "20"
            assert activate.payload.type == "kanban_worker_route_activate"
            assert activate.payload.profile_id == "expert-31"
            assert activate.payload.task_id == "task-route"
            assert activate.payload.model_id == "61"
            assert activate.payload.upstream_model_id == "platform-default"
            assert activate.payload.run_id.startswith("kw-10-")
            assert client.sent[1][1].payload.type == "kanban_worker_route_revoke"
            assert client.sent[1][1].payload.run_id == activate.payload.run_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_route_service_fails_closed_without_bound_ready_identity() -> None:
    engine, sessions = await _database()
    client = FakeRuntimeCommandClient()
    try:
        async with sessions() as db:
            service = ExpertKanbanWorkerRouteService(
                db,
                client=client,  # type: ignore[arg-type]
            )
            assert await service.activate_for_task(
                runtime_workspace_id=10,
                board="default",
                task_id="missing",
            ) is False
            assert client.sent == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconciler_uses_native_board_result_and_queues_one_summary() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 7, 26, 12, 0, 0)
    runner = FakeRunner()
    adapter = FakeProfileAdapter(
        [
            {
                "task": {
                    "id": "task-1",
                    "session_id": "rs-main",
                    "title": "市场分析",
                    "assignee": "expert-31",
                    "status": "done",
                    "latest_summary": "华东市场优先级最高",
                },
                "child_results": [],
            }
        ]
    )
    try:
        async with sessions() as db:
            await ExpertCollaborationService(db).observe_runtime_event(
                runtime_workspace_id=10,
                event=_create_event("task-1"),
                now=now,
            )
        reconciler = ExpertCollaborationReconciler(
            session_factory=sessions,
            profile_adapter=adapter,  # type: ignore[arg-type]
            runner=runner,  # type: ignore[arg-type]
            interval_seconds=2,
        )

        assert await reconciler.reconcile_once(now=now) == 2

        async with sessions() as db:
            collaboration = await db.scalar(
                select(ExpertKanbanCollaboration)
            )
            assert collaboration is not None
            assert collaboration.state == "synthesis_pending"
            assert collaboration.result_snapshot == {
                "tasks": [
                    {
                        "task_id": "task-1",
                        "board": "default",
                        "title": "市场分析",
                        "assignee": "expert-31",
                        "status": "done",
                        "summary": "华东市场优先级最高",
                        "children": [],
                    }
                ]
            }
            assert runner.launched == [collaboration.id]
            assert adapter.calls == [("default", "task-1")]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconciler_does_not_summarize_blocked_or_identity_mismatched_tasks() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 7, 26, 12, 0, 0)
    runner = FakeRunner()
    adapter = FakeProfileAdapter(
        [
            {
                "task": {
                    "id": "task-1",
                    "session_id": "rs-main",
                    "status": "blocked",
                }
            },
            {
                "task": {
                    "id": "task-1",
                    "session_id": "other-session",
                    "status": "done",
                }
            },
        ]
    )
    try:
        async with sessions() as db:
            await ExpertCollaborationService(db).observe_runtime_event(
                runtime_workspace_id=10,
                event=_create_event("task-1"),
                now=now,
            )
        reconciler = ExpertCollaborationReconciler(
            session_factory=sessions,
            profile_adapter=adapter,  # type: ignore[arg-type]
            runner=runner,  # type: ignore[arg-type]
            interval_seconds=2,
        )
        await reconciler.reconcile_once(now=now)
        async with sessions() as db:
            collaboration = await db.scalar(
                select(ExpertKanbanCollaboration)
            )
            assert collaboration is not None
            assert collaboration.state == "attention_required"
            assert collaboration.result_snapshot is None
        await reconciler.reconcile_once(now=now + timedelta(seconds=2))
        async with sessions() as db:
            collaboration = await db.scalar(
                select(ExpertKanbanCollaboration)
            )
            assert collaboration is not None
            assert collaboration.state == "attention_required"
            assert collaboration.last_error_code == "runtimeerror"
            assert runner.launched == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_naive_synthesis_lease_is_recovered_after_restart() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 7, 26, 12, 0, 0)
    runner = FakeRunner()
    try:
        async with sessions() as db:
            await ExpertCollaborationService(db).observe_runtime_event(
                runtime_workspace_id=10,
                event=_create_event("task-1"),
                now=now,
            )
            collaboration = await db.scalar(
                select(ExpertKanbanCollaboration)
            )
            assert collaboration is not None
            collaboration.state = "synthesizing"
            collaboration.attempt_count = 1
            collaboration.lease_expires_at = now - timedelta(seconds=1)
            collaboration.next_reconcile_at = now
            await db.commit()
        reconciler = ExpertCollaborationReconciler(
            session_factory=sessions,
            profile_adapter=FakeProfileAdapter([]),  # type: ignore[arg-type]
            runner=runner,  # type: ignore[arg-type]
            interval_seconds=2,
        )

        await reconciler.reconcile_once(now=now)

        async with sessions() as db:
            collaboration = await db.scalar(
                select(ExpertKanbanCollaboration)
            )
            assert collaboration is not None
            assert collaboration.state == "synthesis_pending"
            assert collaboration.final_run_id is None
            assert collaboration.last_error_code == "summary_run_interrupted"
            assert runner.launched == [collaboration.id]
    finally:
        await engine.dispose()
