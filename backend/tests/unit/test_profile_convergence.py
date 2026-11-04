from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.hermes_runtime.schemas import RuntimeEndpoint
from app.hermes_runtime.protocol import HermesProtocolError
from app.models import (
    AssetRolloutBatch,
    AssetRolloutTarget,
    Base,
    InstalledExpertBinding,
    ProfileConvergenceAttempt,
    ProfileConvergenceJob,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    RuntimeMcpRegistryEntry,
    RuntimeMcpRegistryState,
)
from app.schemas.asset_projection import (
    ProfileCapabilityEvidence,
    ProfileProjectionApplyResponse,
)
from app.services.profile_convergence import (
    ProfileConvergenceError,
    ProfileConvergenceWorker,
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


def _snapshot(label: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "profile": {
            "display_name": label,
            "description": label,
            "avatar_url": None,
            "soul_content": f"{label} soul",
            "system_prompt": None,
            "plugin_ids": ["personal-knowledge"],
        },
        "skills": [
            {
                "skill_id": 101,
                "skill_version_id": 201,
                "version_policy": "follow_latest",
                "asset_scope": "public",
                "artifact_type": "source",
                "package_uri": "/data/hermes/public-assets/101/201",
                "package_sha256": "e" * 64,
            }
        ],
        "mcps": [
            {
                "mcp_id": 301,
                "mcp_version_id": 401,
                "server_slug": "crm",
                "toolset_key": "mcp-crm",
                "transport": "streamable_http",
                "connection_config": {"url": "https://crm.invalid/mcp"},
                "credential_refs": {"Authorization": "Bearer test-token"},
                "config_digest": f"sha256:{'f' * 64}",
            }
        ],
        "knowledge": {
            "enterprise_knowledge_enabled": True,
            "personal_knowledge_base_ids": [501],
        },
        "expert_package_version_id": None,
    }


def _revision(profile_id: int, revision: int) -> RuntimeAgentProfileRevision:
    suffix = format(revision, "x")[-1]
    return RuntimeAgentProfileRevision(
        id=1000 + profile_id * 10 + revision,
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        runtime_agent_profile_id=profile_id,
        revision=revision,
        profile_digest=f"sha256:{suffix * 64}",
        skill_digest=f"sha256:{'b' * 64}",
        mcp_digest=f"sha256:{'c' * 64}",
        knowledge_digest=f"sha256:{'d' * 64}",
        snapshot=_snapshot(f"revision-{revision}"),
        source_type="private_expert",
    )


async def _seed(factory, *, profile_id: int = 10, target_revision: int = 3) -> None:
    async with factory() as db:
        registry = await db.scalar(select(RuntimeMcpRegistryState))
        if registry is None:
            registry = RuntimeMcpRegistryState(
                id=500,
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                runtime_binding_id=119,
                runtime_id="rt-119",
                desired_revision=1,
                applied_revision=1,
                desired_digest=f"sha256:{'9' * 64}",
                applied_digest=f"sha256:{'9' * 64}",
                status="ready",
            )
            db.add(registry)
            db.add(
                RuntimeMcpRegistryEntry(
                    id=501,
                    runtime_mcp_registry_state_id=500,
                    mcp_id=301,
                    mcp_version_id=401,
                    server_slug="crm",
                    config_digest=f"sha256:{'f' * 64}",
                    status="ready",
                )
            )
        db.add(
            RuntimeAgentProfile(
                id=profile_id,
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                hermes_profile_name=f"expert-{profile_id}",
                profile_kind="expert",
                display_name="Alpha",
                ownership_type="self_created",
                desired_revision=target_revision,
                applied_revision=0,
                desired_digest=f"sha256:{format(target_revision, 'x')[-1] * 64}",
                status="active",
            )
        )
        db.add(_revision(profile_id, target_revision))
        db.add(
            ProfileConvergenceJob(
                id=profile_id,
                org_id=1,
                runtime_workspace_id=3,
                runtime_agent_profile_id=profile_id,
                target_revision=target_revision,
                status="waiting",
                stage="resolve_desired",
                available_at=datetime.utcnow(),
            )
        )
        await db.commit()


class FakeTargetResolver:
    async def resolve(self, _db, *, profile):
        return 119, RuntimeEndpoint(
            runtime_workspace_id=int(profile.runtime_workspace_id),
            runtime_id="rt-119",
            endpoint_base_url="http://runtime.test",
            endpoint_host_header="runtime.test",
            api_key="secret",
        )


class UnreadyTargetResolver:
    async def resolve(self, _db, *, profile):
        del profile
        raise ProfileConvergenceError("RUNTIME_NOT_READY", retryable=True)


class ApplyingClient:
    def __init__(self) -> None:
        self.requests = []
        self.active = 0
        self.max_active = 0

    async def apply_profile_projection(self, _endpoint, request):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.requests.append(request)
            await asyncio.sleep(0.01)
            return ProfileProjectionApplyResponse(
                operation_id=request.operation_id,
                runtime_agent_profile_id=request.runtime_agent_profile_id,
                desired_revision=request.desired_revision,
                applied_revision=request.desired_revision,
                effective_generation=request.expected_effective_generation + 1,
                status="applied",
                digests=request.digests,
                capabilities=[
                    ProfileCapabilityEvidence(
                        capability_type=item.capability_type,
                        capability_id=item.capability_id,
                        target_version_id=item.target_version_id,
                        status="applied",
                        applied_digest=item.desired_digest,
                    )
                    for item in request.capabilities
                ],
            )
        finally:
            self.active -= 1


class AlreadyAdvancedGenerationClient(ApplyingClient):
    async def apply_profile_projection(self, endpoint, request):
        response = await super().apply_profile_projection(endpoint, request)
        return response.model_copy(
            update={"effective_generation": request.expected_effective_generation + 4}
        )


class AlreadyAppliedGenerationClient(ApplyingClient):
    async def apply_profile_projection(self, endpoint, request):
        response = await super().apply_profile_projection(endpoint, request)
        return response.model_copy(
            update={"effective_generation": request.expected_effective_generation}
        )


class StaleGenerationClient(ApplyingClient):
    def __init__(self, *, observed_digest: str | None = None) -> None:
        super().__init__()
        self.observed_digest = observed_digest
        self.status_requests: list[int] = []
        self.stale_once = True
        self.stale_error = "PROFILE_EFFECTIVE_GENERATION_STALE"

    async def apply_profile_projection(self, endpoint, request):
        if self.stale_once:
            self.stale_once = False
            raise HermesProtocolError(
                self.stale_error,
                status_code=409,
                path="/api/control",
            )
        return await super().apply_profile_projection(endpoint, request)

    async def get_profile_projection_status(
        self,
        _endpoint,
        *,
        runtime_agent_profile_id: int,
    ) -> ProfileProjectionApplyResponse:
        self.status_requests.append(runtime_agent_profile_id)
        return ProfileProjectionApplyResponse(
            operation_id="profile-status-observation",
            runtime_agent_profile_id=runtime_agent_profile_id,
            desired_revision=1,
            applied_revision=1,
            effective_generation=1,
            status="applied",
            digests={
                "profile": self.observed_digest or f"sha256:{'1' * 64}",
                "skill": f"sha256:{'b' * 64}",
                "mcp": f"sha256:{'c' * 64}",
                "knowledge": f"sha256:{'d' * 64}",
            },
        )


class StaleAppliedRevisionClient(StaleGenerationClient):
    def __init__(self) -> None:
        super().__init__()
        self.stale_error = "PROFILE_APPLIED_REVISION_STALE"

    async def get_profile_projection_status(
        self,
        _endpoint,
        *,
        runtime_agent_profile_id: int,
    ) -> ProfileProjectionApplyResponse:
        self.status_requests.append(runtime_agent_profile_id)
        return ProfileProjectionApplyResponse(
            operation_id="profile-status-observation",
            runtime_agent_profile_id=runtime_agent_profile_id,
            desired_revision=2,
            applied_revision=2,
            effective_generation=2,
            status="applied",
            digests={
                "profile": f"sha256:{'2' * 64}",
                "skill": f"sha256:{'b' * 64}",
                "mcp": f"sha256:{'c' * 64}",
                "knowledge": f"sha256:{'d' * 64}",
            },
        )


async def _seed_applied_revision(factory) -> None:
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        assert profile is not None
        profile.applied_revision = 1
        profile.applied_digest = f"sha256:{'1' * 64}"
        profile.effective_generation = 0
        db.add(_revision(10, 1))
        await db.commit()


@pytest.mark.asyncio
async def test_worker_applies_latest_complete_revision_and_records_attempt() -> None:
    engine, factory = await _database()
    await _seed(factory, target_revision=3)
    async with factory() as db:
        job = await db.get(ProfileConvergenceJob, 10)
        assert job is not None
        job.target_revision = 1
        batch = AssetRolloutBatch(
            rollout_id="rollout-profile-complete",
            org_id=1,
            asset_type="skill",
            asset_id=101,
            action="publish",
            target_version_id=201,
            created_by_user_id=9,
            affected_user_count=1,
            affected_profile_count=1,
            updating_count=1,
            status="updating",
            snapshot_at=datetime.utcnow(),
        )
        db.add(batch)
        await db.flush()
        db.add(
            AssetRolloutTarget(
                asset_rollout_batch_id=int(batch.id),
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                runtime_agent_profile_id=10,
                target_revision=3,
                convergence_job_id=10,
                status="updating",
            )
        )
        await db.commit()
    client = ApplyingClient()
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=client,
        target_resolver=FakeTargetResolver(),
        worker_id="worker-a",
    )

    assert await worker.reconcile_once() == 1
    assert [item.desired_revision for item in client.requests] == [3]
    request = client.requests[0]
    assert request.profile_config["platform_toolsets"] == {
        "cli": ["hermes-cli", "mcp-crm"]
    }
    assert request.profile_config["mcp_servers"] == {
        "crm": {
            "url": "https://crm.invalid/mcp",
            "headers": {"Authorization": "Bearer test-token"},
            "transport": "streamable_http",
            "enabled": True,
        }
    }
    assert request.artifacts[0].target_relative_path == "skills/releases/101/current"
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        job = await db.get(ProfileConvergenceJob, 10)
        attempt = await db.scalar(select(ProfileConvergenceAttempt))
        batch = await db.scalar(select(AssetRolloutBatch))
        target = await db.scalar(select(AssetRolloutTarget))
        assert profile is not None and profile.applied_revision == 3
        assert profile.status == "active"
        assert job is not None and job.status == "applied"
        assert attempt is not None and attempt.status == "applied"
        assert attempt.evidence["operation_id"] == request.operation_id
        assert target is not None and target.status == "applied"
        assert batch is not None and batch.status == "completed"
        assert batch.applied_count == 1
    await engine.dispose()


def test_profile_projection_keeps_hermes_cli_without_mcp() -> None:
    profile = RuntimeAgentProfile(
        id=10,
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        hermes_profile_name="default",
        profile_kind="default",
        display_name="Default",
        ownership_type="self_created",
        desired_revision=1,
        applied_revision=0,
        desired_digest=f"sha256:{'a' * 64}",
        status="active",
    )
    revision = _revision(10, 1)
    revision.snapshot = {**dict(revision.snapshot or {}), "mcps": []}
    request = ProfileConvergenceWorker._request(
        profile=profile,
        revision=revision,
        binding_id=119,
        endpoint=RuntimeEndpoint(
            runtime_workspace_id=3,
            runtime_id="rt-119",
            endpoint_base_url="http://runtime.invalid",
            endpoint_host_header="runtime.invalid",
            api_key="test-key",
        ),
    )

    assert request.profile_config["platform_toolsets"] == {
        "cli": ["hermes-cli", "no_mcp"]
    }


@pytest.mark.asyncio
async def test_stale_generation_repairs_only_matching_applied_projection_then_retries() -> None:
    engine, factory = await _database()
    await _seed(factory, target_revision=3)
    await _seed_applied_revision(factory)
    client = StaleGenerationClient()
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=client,
        target_resolver=FakeTargetResolver(),
        worker_id="worker-a",
    )

    assert await worker.reconcile_once() == 1
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        job = await db.get(ProfileConvergenceJob, 10)
        attempt = await db.scalar(
            select(ProfileConvergenceAttempt).order_by(
                ProfileConvergenceAttempt.attempt_number.desc()
            )
        )
        assert profile is not None and profile.effective_generation == 1
        assert profile.applied_revision == 1
        assert job is not None and job.status == "waiting"
        assert job.available_at <= datetime.utcnow()
        assert attempt is not None and attempt.status == "waiting"
        assert attempt.stage == "supervisor_reconcile"

    assert await worker.reconcile_once() == 1
    assert client.status_requests == [10]
    assert client.requests[0].expected_effective_generation == 1
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        job = await db.get(ProfileConvergenceJob, 10)
        assert profile is not None and profile.applied_revision == 3
        assert profile.effective_generation == 2
        assert job is not None and job.status == "applied"
    await engine.dispose()


@pytest.mark.asyncio
async def test_matching_applied_receipt_can_advance_stale_control_plane_generation() -> None:
    engine, factory = await _database()
    await _seed(factory, target_revision=1)
    client = AlreadyAdvancedGenerationClient()
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=client,
        target_resolver=FakeTargetResolver(),
        worker_id="worker-a",
    )

    assert await worker.reconcile_once() == 1
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        job = await db.get(ProfileConvergenceJob, 10)
        assert profile is not None
        assert profile.applied_revision == 1
        assert profile.effective_generation == 4
        assert job is not None and job.status == "applied"
    await engine.dispose()


@pytest.mark.asyncio
async def test_matching_applied_no_op_receipt_keeps_authoritative_generation() -> None:
    engine, factory = await _database()
    await _seed(factory, target_revision=3)
    await _seed_applied_revision(factory)
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        assert profile is not None
        profile.effective_generation = 1
        await db.commit()
    client = AlreadyAppliedGenerationClient()
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=client,
        target_resolver=FakeTargetResolver(),
        worker_id="worker-a",
    )

    assert await worker.reconcile_once() == 1
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        job = await db.get(ProfileConvergenceJob, 10)
        assert profile is not None
        assert profile.applied_revision == 3
        assert profile.effective_generation == 1
        assert job is not None and job.status == "applied"
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_applied_revision_recovers_matching_supervisor_state() -> None:
    engine, factory = await _database()
    await _seed(factory, target_revision=3)
    await _seed_applied_revision(factory)
    async with factory() as db:
        db.add(_revision(10, 2))
        await db.commit()
    client = StaleAppliedRevisionClient()
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=client,
        target_resolver=FakeTargetResolver(),
        worker_id="worker-a",
    )

    assert await worker.reconcile_once() == 1
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        job = await db.get(ProfileConvergenceJob, 10)
        assert profile is not None
        assert profile.applied_revision == 2
        assert profile.applied_digest == f"sha256:{'2' * 64}"
        assert profile.effective_generation == 2
        assert job is not None and job.status == "waiting"

    assert await worker.reconcile_once() == 1
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        job = await db.get(ProfileConvergenceJob, 10)
        assert profile is not None
        assert profile.applied_revision == 3
        assert profile.effective_generation == 3
        assert job is not None and job.status == "applied"
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_generation_with_digest_mismatch_fails_closed() -> None:
    engine, factory = await _database()
    await _seed(factory, target_revision=3)
    await _seed_applied_revision(factory)
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=StaleGenerationClient(observed_digest=f"sha256:{'9' * 64}"),
        target_resolver=FakeTargetResolver(),
        worker_id="worker-a",
    )

    assert await worker.reconcile_once() == 1
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        job = await db.get(ProfileConvergenceJob, 10)
        assert profile is not None and profile.effective_generation == 0
        assert job is not None and job.status == "failed"
        assert job.last_error_code == "PROFILE_PROJECTION_STATE_DIVERGED"
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_status", ["disabled", "revoked"])
async def test_convergence_preserves_inactive_expert_status(binding_status: str) -> None:
    engine, factory = await _database()
    await _seed(factory, target_revision=1)
    async with factory() as db:
        db.add(
            InstalledExpertBinding(
                id=801,
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                expert_package_id=701,
                expert_package_version_id=711,
                runtime_agent_profile_id=10,
                source_type="market",
                status=binding_status,
            )
        )
        await db.commit()
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=ApplyingClient(),
        target_resolver=FakeTargetResolver(),
        worker_id="worker-a",
    )

    assert await worker.reconcile_once() == 1
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        assert profile is not None
        assert profile.status == "active"
        assert profile.applied_revision == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_convergence_preserves_disabled_private_expert_without_binding() -> None:
    engine, factory = await _database()
    await _seed(factory, target_revision=1)
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        assert profile is not None
        profile.status = "disabled"
        await db.commit()
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=ApplyingClient(),
        target_resolver=FakeTargetResolver(),
        worker_id="worker-a",
    )

    assert await worker.reconcile_once() == 0
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        assert profile is not None
        assert profile.status == "disabled"
        assert profile.applied_revision == 0
    await engine.dispose()


def test_profile_request_projects_stdio_credentials_to_env() -> None:
    profile = RuntimeAgentProfile(
        id=10,
        org_id=1,
        user_id=2,
        runtime_workspace_id=3,
        hermes_profile_name="expert-10",
        profile_kind="expert",
        display_name="Alpha",
        ownership_type="self_created",
        desired_revision=1,
        applied_revision=0,
        desired_digest=f"sha256:{'1' * 64}",
        status="updating",
    )
    revision = _revision(10, 1)
    snapshot = dict(revision.snapshot or {})
    mcps = list(snapshot["mcps"])
    mcps[0] = {
        **mcps[0],
        "transport": "stdio",
        "connection_config": {
            "command": "crm-mcp",
            "env": {"CRM_REGION": "cn"},
        },
        "credential_refs": {"CRM_API_KEY": "test-token"},
    }
    snapshot["mcps"] = mcps
    revision.snapshot = snapshot

    request = ProfileConvergenceWorker._request(
        profile=profile,
        revision=revision,
        binding_id=119,
        endpoint=RuntimeEndpoint(
            runtime_workspace_id=3,
            runtime_id="rt-119",
            endpoint_base_url="http://runtime.test",
            endpoint_host_header="runtime.test",
            api_key="secret",
        ),
    )

    assert request.profile_config["mcp_servers"]["crm"] == {
        "command": "crm-mcp",
        "env": {"CRM_REGION": "cn", "CRM_API_KEY": "test-token"},
        "transport": "stdio",
        "enabled": True,
    }


class AdvancingClient(ApplyingClient):
    def __init__(self, factory) -> None:
        super().__init__()
        self.factory = factory
        self.advanced = False

    async def apply_profile_projection(self, endpoint, request):
        if not self.advanced:
            self.advanced = True
            async with self.factory() as db:
                profile = await db.get(RuntimeAgentProfile, 10)
                job = await db.get(ProfileConvergenceJob, 10)
                assert profile is not None and job is not None
                profile.desired_revision = 4
                profile.desired_digest = f"sha256:{'4' * 64}"
                job.target_revision = 4
                db.add(_revision(10, 4))
                await db.commit()
        return await super().apply_profile_projection(endpoint, request)


@pytest.mark.asyncio
async def test_revision_arriving_during_apply_converges_next_without_replaying_assets() -> None:
    engine, factory = await _database()
    await _seed(factory, target_revision=3)
    client = AdvancingClient(factory)
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=client,
        target_resolver=FakeTargetResolver(),
        worker_id="worker-a",
    )

    assert await worker.reconcile_once() == 1
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        job = await db.get(ProfileConvergenceJob, 10)
        assert profile is not None and profile.applied_revision == 3
        assert profile.status == "active"
        assert job is not None and job.status == "waiting"
        assert job.target_revision == 4
    assert await worker.reconcile_once() == 1
    assert [item.desired_revision for item in client.requests] == [3, 4]
    async with factory() as db:
        profile = await db.get(RuntimeAgentProfile, 10)
        assert profile is not None and profile.applied_revision == 4
        assert profile.status == "active"
    await engine.dispose()


@pytest.mark.asyncio
async def test_different_profiles_are_processed_in_one_parallel_batch() -> None:
    engine, factory = await _database()
    await _seed(factory, profile_id=10, target_revision=1)
    await _seed(factory, profile_id=11, target_revision=1)
    client = ApplyingClient()
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=client,
        target_resolver=FakeTargetResolver(),
        worker_id="worker-a",
        concurrency=2,
    )
    assert await worker.reconcile_once() == 2
    assert {item.runtime_agent_profile_id for item in client.requests} == {10, 11}
    assert client.max_active == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_not_ready_records_attempt_and_releases_lease() -> None:
    engine, factory = await _database()
    await _seed(factory, target_revision=1)
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=ApplyingClient(),
        target_resolver=UnreadyTargetResolver(),
        worker_id="worker-a",
    )
    assert await worker.reconcile_once() == 0
    async with factory() as db:
        job = await db.get(ProfileConvergenceJob, 10)
        attempt = await db.scalar(select(ProfileConvergenceAttempt))
        assert job is not None and job.status == "waiting"
        assert job.lease_owner is None
        assert job.last_error_code == "RUNTIME_NOT_READY"
        assert attempt is not None and attempt.stage == "resolve_runtime"
        assert attempt.status == "waiting"
    await engine.dispose()


@pytest.mark.asyncio
async def test_profile_waits_until_required_mcp_registry_version_is_ready() -> None:
    engine, factory = await _database()
    await _seed(factory, target_revision=1)
    async with factory() as db:
        registry = await db.get(RuntimeMcpRegistryState, 500)
        assert registry is not None
        registry.status = "installing"
        registry.applied_revision = 0
        await db.commit()
    client = ApplyingClient()
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=client,
        target_resolver=FakeTargetResolver(),
        worker_id="worker-a",
    )

    assert await worker.reconcile_once() == 0
    assert client.requests == []
    async with factory() as db:
        job = await db.get(ProfileConvergenceJob, 10)
        assert job is not None and job.status == "waiting"
        assert job.stage == "mcp_registry"
        assert job.last_error_code == "MCP_REGISTRY_PENDING"
    await engine.dispose()


@pytest.mark.asyncio
async def test_profile_stops_waiting_when_required_mcp_registry_failed() -> None:
    engine, factory = await _database()
    await _seed(factory, target_revision=1)
    async with factory() as db:
        registry = await db.get(RuntimeMcpRegistryState, 500)
        assert registry is not None
        registry.status = "failed"
        registry.last_error_code = "MCP_REGISTRY_CONFIG_INVALID"
        await db.commit()
    client = ApplyingClient()
    worker = ProfileConvergenceWorker(
        session_factory=factory,
        client=client,
        target_resolver=FakeTargetResolver(),
        worker_id="worker-a",
    )

    assert await worker.reconcile_once() == 0
    assert client.requests == []
    async with factory() as db:
        job = await db.get(ProfileConvergenceJob, 10)
        assert job is not None and job.status == "failed"
        assert job.stage == "mcp_registry"
        assert job.last_error_code == "MCP_REGISTRY_CONFIG_INVALID"
    await engine.dispose()
