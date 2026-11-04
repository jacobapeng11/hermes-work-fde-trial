from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import (
    Base,
    CloudHermesProfile,
    ExpertKanbanCollaboration,
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
from app.services.expert_collaborations import ExpertCollaborationService
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
                ),
            ]
        )
        await db.flush()
        db.add(
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
            )
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
                RuntimeSession(
                    id=40,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=10,
                    runtime_agent_profile_id=30,
                    runtime_session_id="rs-main",
                    source_type="web",
                    status="active",
                    session_model_entry_id=61,
                ),
                RuntimeRun(
                    id=50,
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=10,
                    session_id=40,
                    runtime_session_id="rs-main",
                    runtime_run_id="rr-origin",
                    model_entry_id=61,
                    status="running",
                    source_type="web",
                    request_payload={"message": "请分析"},
                ),
            ]
        )
        await db.commit()
    return engine, sessions


def _create_event(task_id: str) -> RuntimeKanbanLeaseEventRequest:
    return RuntimeKanbanLeaseEventRequest(
        event_id=f"post_tool_call:{task_id}",
        event_name="post_tool_call",
        runtime_agent_profile_id=30,
        tool_name="kanban_create",
        task_id=task_id,
        board="default",
        outcome="ok",
        runtime_session_id="rs-main",
        tool_call_id=f"call-{task_id}",
        assignee="expert-30",
    )


class _FakeProfileAdapter:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
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
        return self.payloads.pop(0)


class _FakeRunner:
    def __init__(self) -> None:
        self.launched: list[int] = []

    def launch(self, collaboration_id: int) -> bool:
        self.launched.append(collaboration_id)
        return True


def _task_payload(status: str) -> dict[str, Any]:
    return {
        "task": {
            "id": "task-1",
            "session_id": "rs-main",
            "title": "任务",
            "status": status,
        },
        "child_results": [],
    }


def _make_reconciler(
    sessions,
    adapter: _FakeProfileAdapter,
    *,
    wait_max_attempts: int = 100,
) -> ExpertCollaborationReconciler:
    return ExpertCollaborationReconciler(
        session_factory=sessions,
        profile_adapter=adapter,  # type: ignore[arg-type]
        runner=_FakeRunner(),  # type: ignore[arg-type]
        interval_seconds=2,
        wait_backoff_max_seconds=60,
        wait_max_attempts=wait_max_attempts,
    )


async def _seed_collaboration(sessions, now: datetime) -> None:
    async with sessions() as db:
        await ExpertCollaborationService(db).observe_runtime_event(
            runtime_workspace_id=10,
            event=_create_event("task-1"),
            now=now,
        )


@pytest.mark.asyncio
async def test_waiting_without_progress_backs_off_exponentially() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 10, 7, 12, 0, 0)
    adapter = _FakeProfileAdapter(
        [_task_payload("in_progress") for _ in range(3)]
    )
    try:
        await _seed_collaboration(sessions, now)
        reconciler = _make_reconciler(sessions, adapter)

        await reconciler.reconcile_once(now=now)
        await reconciler.reconcile_once(now=now + timedelta(seconds=4))
        await reconciler.reconcile_once(now=now + timedelta(seconds=12))

        async with sessions() as db:
            collaboration = await db.scalar(
                select(ExpertKanbanCollaboration)
            )
            assert collaboration is not None
            assert collaboration.state == "waiting_tasks"
            assert collaboration.wait_attempt_count == 3
            assert collaboration.next_reconcile_at == now + timedelta(
                seconds=28
            )
        assert len(adapter.calls) == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_state_change_resets_wait_backoff() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 10, 7, 12, 0, 0)
    adapter = _FakeProfileAdapter([_task_payload("blocked")])
    try:
        await _seed_collaboration(sessions, now)
        reconciler = _make_reconciler(sessions, adapter)

        await reconciler.reconcile_once(now=now)

        async with sessions() as db:
            collaboration = await db.scalar(
                select(ExpertKanbanCollaboration)
            )
            assert collaboration is not None
            assert collaboration.state == "attention_required"
            assert collaboration.wait_attempt_count == 0
            assert collaboration.next_reconcile_at == now + timedelta(
                seconds=2
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_exhausted_wait_becomes_delivery_failed() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 10, 7, 12, 0, 0)
    adapter = _FakeProfileAdapter(
        [_task_payload("in_progress") for _ in range(3)]
    )
    try:
        await _seed_collaboration(sessions, now)
        reconciler = _make_reconciler(
            sessions,
            adapter,
            wait_max_attempts=3,
        )

        await reconciler.reconcile_once(now=now)
        await reconciler.reconcile_once(now=now + timedelta(seconds=4))
        await reconciler.reconcile_once(now=now + timedelta(seconds=12))
        await reconciler.reconcile_once(now=now + timedelta(seconds=60))

        async with sessions() as db:
            collaboration = await db.scalar(
                select(ExpertKanbanCollaboration)
            )
            assert collaboration is not None
            assert collaboration.state == "delivery_failed"
            assert collaboration.last_error_code == "wait_reconcile_exhausted"
        assert len(adapter.calls) == 3
    finally:
        await engine.dispose()


def test_current_schema_registers_wait_attempt_count() -> None:
    content = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "current_schema.sql"
    ).read_text(encoding="utf-8")

    assert "wait_attempt_count" in content
    assert "expert_kanban_collaboration" in content
