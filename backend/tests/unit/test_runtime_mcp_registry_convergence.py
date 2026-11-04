from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import (
    Base,
    HermesRuntimeBinding,
    RuntimeMcpRegistryEntry,
    RuntimeMcpRegistryJob,
    RuntimeMcpRegistryRevision,
    RuntimeMcpRegistryState,
)
from app.schemas.asset_projection import RuntimeMcpRegistryApplyResponse
from app.services.runtime_mcp_registry_convergence import (
    RuntimeMcpRegistryConvergenceWorker,
)


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


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(factory) -> None:
    digest = "sha256:" + "1" * 64
    async with factory() as db:
        db.add(
            HermesRuntimeBinding(
                id=119,
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                runtime_id="rt-119",
                image="hermes:test",
                k8s_namespace="test",
                knative_service="rt-119",
                endpoint_base_url="http://runtime.invalid",
                endpoint_host_header="runtime.invalid",
                hermes_home_sub_path="users/2",
                workspace_sub_path="workspaces/3",
                hermes_home_mount_path="/home/hermes",
                workspace_mount_path="/workspace",
                api_key_encrypted="secret",
                status="ready",
            )
        )
        db.add(
            RuntimeMcpRegistryState(
                id=500,
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                runtime_binding_id=119,
                runtime_id="rt-119",
                desired_revision=1,
                applied_revision=0,
                desired_digest=digest,
                status="installing",
            )
        )
        db.add(
            RuntimeMcpRegistryRevision(
                id=501,
                runtime_mcp_registry_state_id=500,
                revision=1,
                registry_digest=digest,
                normalized_servers={
                    "servers": [
                        {
                            "mcp_id": 301,
                            "mcp_version_id": 401,
                            "server_slug": "crm",
                            "transport": "streamable_http",
                            "connection_config": {
                                "url": "https://crm.invalid/mcp"
                            },
                            "credential_refs": {},
                            "config_digest": "sha256:" + "2" * 64,
                        }
                    ]
                },
                source_type="default_user",
                source_id="operation-1",
            )
        )
        db.add(
            RuntimeMcpRegistryEntry(
                id=502,
                runtime_mcp_registry_state_id=500,
                mcp_id=301,
                mcp_version_id=401,
                server_slug="crm",
                config_digest="sha256:" + "2" * 64,
                status="installing",
            )
        )
        db.add(
            RuntimeMcpRegistryJob(
                id=503,
                runtime_mcp_registry_state_id=500,
                target_revision=1,
                status="waiting",
                stage="resolve_registry",
                available_at=datetime.utcnow(),
            )
        )
        await db.commit()


class RegistryClient:
    def __init__(self, *, include_evidence: bool = True) -> None:
        self.include_evidence = include_evidence
        self.requests = []

    async def apply_runtime_mcp_registry(self, _endpoint, request):
        self.requests.append(request)
        servers = (
            [
                {
                    "server_slug": "crm",
                    "status": "ready",
                    "tools_schema_digest": "sha256:" + "3" * 64,
                    "tool_names": ["mcp_crm_search"],
                }
            ]
            if self.include_evidence
            else []
        )
        return RuntimeMcpRegistryApplyResponse(
            operation_id=request.operation_id,
            desired_revision=request.desired_revision,
            applied_revision=request.desired_revision,
            status="ready",
            registry_digest=request.registry_digest,
            servers=servers,
            affected_active_run_ids=["run-working-1"],
        )


@pytest.mark.asyncio
async def test_worker_records_tools_list_and_active_run_reload_evidence() -> None:
    engine, factory = await _database()
    await _seed(factory)
    client = RegistryClient()
    worker = RuntimeMcpRegistryConvergenceWorker(
        session_factory=factory,
        worker_id="registry-worker",
        client=client,
    )

    assert await worker.reconcile_once() == 1
    assert len(client.requests) == 1
    async with factory() as db:
        state = await db.get(RuntimeMcpRegistryState, 500)
        entry = await db.get(RuntimeMcpRegistryEntry, 502)
        job = await db.get(RuntimeMcpRegistryJob, 503)
        assert state is not None and state.status == "ready"
        assert state.applied_revision == 1
        assert state.last_reload_affected_run_ids == ["run-working-1"]
        assert state.last_reload_at is not None
        assert entry is not None and entry.status == "ready"
        assert entry.tools_schema == [{"name": "mcp_crm_search"}]
        assert entry.tools_schema_digest == "sha256:" + "3" * 64
        assert job is not None and job.status == "completed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_retries_when_ready_receipt_has_no_tools_list_evidence() -> None:
    engine, factory = await _database()
    await _seed(factory)
    worker = RuntimeMcpRegistryConvergenceWorker(
        session_factory=factory,
        worker_id="registry-worker",
        client=RegistryClient(include_evidence=False),
    )

    assert await worker.reconcile_once() == 1
    async with factory() as db:
        state = await db.get(RuntimeMcpRegistryState, 500)
        job = await db.get(RuntimeMcpRegistryJob, 503)
        assert state is not None and state.status == "failed"
        assert state.applied_revision == 0
        assert state.last_error_code == "MCP_REGISTRY_EVIDENCE_MISSING"
        assert job is not None and job.status == "waiting"
        assert job.lease_owner is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_quarantines_invalid_registry_without_calling_runtime() -> None:
    engine, factory = await _database()
    await _seed(factory)
    async with factory() as db:
        revision = await db.get(RuntimeMcpRegistryRevision, 501)
        assert revision is not None
        server = dict(dict(revision.normalized_servers or {})["servers"][0])
        server["connection_config"] = {}
        revision.normalized_servers = {"servers": [server]}
        await db.commit()
    client = RegistryClient()
    worker = RuntimeMcpRegistryConvergenceWorker(
        session_factory=factory,
        worker_id="registry-worker",
        client=client,
    )

    assert await worker.reconcile_once() == 0
    assert client.requests == []
    async with factory() as db:
        state = await db.get(RuntimeMcpRegistryState, 500)
        job = await db.get(RuntimeMcpRegistryJob, 503)
        assert state is not None and state.status == "failed"
        assert state.applied_revision == 0
        assert state.last_error_code == "MCP_REGISTRY_CONFIG_INVALID"
        assert job is not None and job.status == "failed"
        assert job.stage == "resolve_registry"
        assert job.lease_owner is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_does_not_reload_when_registry_is_already_applied() -> None:
    engine, factory = await _database()
    await _seed(factory)
    async with factory() as db:
        state = await db.get(RuntimeMcpRegistryState, 500)
        assert state is not None
        state.applied_revision = state.desired_revision
        state.applied_digest = state.desired_digest
        state.status = "ready"
        await db.commit()
    client = RegistryClient()
    worker = RuntimeMcpRegistryConvergenceWorker(
        session_factory=factory,
        worker_id="registry-worker",
        client=client,
    )

    assert await worker.reconcile_once() == 0
    assert client.requests == []
    async with factory() as db:
        job = await db.scalar(select(RuntimeMcpRegistryJob))
        assert job is not None and job.status == "completed"
    await engine.dispose()
