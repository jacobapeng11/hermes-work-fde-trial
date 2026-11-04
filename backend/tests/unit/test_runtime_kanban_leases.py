from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import BigInteger, func, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import (
    Base,
    CloudHermesProfile,
    HermesRuntimeBinding,
    Organization,
    RuntimeAgentProfile,
    RuntimeKanbanLease,
    RuntimeKanbanLeaseEvent,
    User,
)
from app.main import create_app
from app.schemas.runtime_kanban import RuntimeKanbanLeaseEventRequest
from app.services.runtime_kanban_leases import (
    RuntimeKanbanLeaseError,
    RuntimeKanbanLeaseReconciler,
    RuntimeKanbanLeaseService,
)
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
    def __init__(self, observations: list[bool | Exception]) -> None:
        self.observations = observations
        self.calls = []

    async def has_any_executable_kanban_task(self, endpoint) -> bool:
        self.calls.append(endpoint)
        value = self.observations.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


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
            ]
        )
        await db.commit()
    return engine, sessions


def _event(**changes) -> RuntimeKanbanLeaseEventRequest:
    values = {
        "event_id": "pre_tool_call:call-1",
        "event_name": "pre_tool_call",
        "runtime_agent_profile_id": 30,
        "tool_name": "kanban_create",
    }
    values.update(changes)
    return RuntimeKanbanLeaseEventRequest(**values)


def test_runtime_kanban_plugin_event_route_is_internal_runtime_authenticated() -> None:
    operation = create_app().openapi()["paths"][
        "/api/v1/runtime-kanban/{runtime_workspace_id}/events"
    ]["post"]

    assert "runtime-kanban" in operation["tags"]
    assert any(
        parameter["name"] == "authorization"
        and parameter["in"] == "header"
        for parameter in operation["parameters"]
    )


@pytest.mark.asyncio
async def test_pre_create_event_is_authenticated_durable_and_idempotent() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 7, 25, 12, 0, 0)
    try:
        async with sessions() as db:
            service = RuntimeKanbanLeaseService(db, temporary_seconds=120)
            first = await service.accept_event(
                runtime_workspace_id=10,
                runtime_token="secret",
                event=_event(),
                now=now,
            )
            second = await service.accept_event(
                runtime_workspace_id=10,
                runtime_token="secret",
                event=_event(),
                now=now + timedelta(seconds=1),
            )
            lease = await db.scalar(select(RuntimeKanbanLease))
            event_count = await db.scalar(select(func.count(RuntimeKanbanLeaseEvent.id)))

            assert first.duplicate is False
            assert first.lease_state == "temporary"
            assert second.duplicate is True
            assert lease is not None
            assert lease.temporary_expires_at == now + timedelta(seconds=120)
            assert event_count == 1

            with pytest.raises(RuntimeKanbanLeaseError, match="unauthorized"):
                await service.accept_event(
                    runtime_workspace_id=10,
                    runtime_token="wrong",
                    event=_event(event_id="pre_tool_call:call-2"),
                    now=now,
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_successful_post_event_requests_authoritative_reconcile() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 7, 25, 12, 0, 0)
    try:
        async with sessions() as db:
            service = RuntimeKanbanLeaseService(db)
            await service.accept_event(
                runtime_workspace_id=10,
                runtime_token="secret",
                event=_event(),
                now=now,
            )
            receipt = await service.accept_event(
                runtime_workspace_id=10,
                runtime_token="secret",
                event=_event(
                    event_id="post_tool_call:call-1",
                    event_name="post_tool_call",
                    outcome="ok",
                ),
                now=now + timedelta(seconds=1),
            )
            lease = await db.scalar(select(RuntimeKanbanLease))

            assert receipt.lease_state == "reconciling"
            assert lease is not None
            assert lease.temporary_expires_at is None
            assert lease.next_reconcile_at == now + timedelta(seconds=1)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconciler_keeps_native_nonterminal_board_active() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 7, 25, 12, 0, 0)
    adapter = FakeProfileAdapter([True])
    try:
        async with sessions() as db:
            await RuntimeKanbanLeaseService(db).accept_event(
                runtime_workspace_id=10,
                runtime_token="secret",
                event=_event(),
                now=now,
            )
        count = await RuntimeKanbanLeaseReconciler(
            session_factory=sessions,
            profile_adapter=adapter,  # type: ignore[arg-type]
            interval_seconds=30,
            release_grace_seconds=60,
        ).reconcile_once(now=now)
        async with sessions() as db:
            lease = await db.scalar(select(RuntimeKanbanLease))

            assert count == 1
            assert len(adapter.calls) == 1
            assert adapter.calls[0].api_key == "secret"
            assert lease is not None
            assert lease.state == "active"
            assert lease.release_after is None
            assert lease.next_reconcile_at == now + timedelta(seconds=30)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconciler_releases_only_after_two_empty_board_observations() -> None:
    engine, sessions = await _database()
    now = datetime(2026, 7, 25, 12, 0, 0)
    adapter = FakeProfileAdapter([False, False])
    reconciler = RuntimeKanbanLeaseReconciler(
        session_factory=sessions,
        profile_adapter=adapter,  # type: ignore[arg-type]
        interval_seconds=30,
        release_grace_seconds=60,
    )
    try:
        async with sessions() as db:
            await RuntimeKanbanLeaseService(db).accept_event(
                runtime_workspace_id=10,
                runtime_token="secret",
                event=_event(),
                now=now,
            )
        await reconciler.reconcile_once(now=now)
        async with sessions() as db:
            lease = await db.scalar(select(RuntimeKanbanLease))
            assert lease is not None
            assert lease.state == "release_pending"
            assert lease.release_after == now + timedelta(seconds=60)

        await reconciler.reconcile_once(now=now + timedelta(seconds=60))
        async with sessions() as db:
            assert await db.scalar(select(RuntimeKanbanLease.id)) is None
    finally:
        await engine.dispose()
