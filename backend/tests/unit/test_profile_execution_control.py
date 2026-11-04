from __future__ import annotations

import pytest
from sqlalchemy import select

from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models import (
    AssetRolloutBatch,
    AssetRolloutTarget,
    ExpertPackage,
    InstalledExpertBinding,
    ProfileExecutionControlJob,
    RuntimeAgentProfile,
)
from app.schemas.asset_projection import ProfileExecutionControlResponse
from app.services.asset_rollout import AssetRolloutWorker
from app.services.expert_security import ExpertSecurityService
from app.services.profile_execution_control import ProfileExecutionControlWorker
from tests.unit.test_asset_rollout import _seed_market_experts
from tests.unit.test_profile_desired import _database


class _TargetResolver:
    async def resolve(self, db, *, profile: RuntimeAgentProfile):
        del db
        return 900 + int(profile.id), RuntimeEndpoint(
            runtime_workspace_id=int(profile.runtime_workspace_id),
            runtime_id=f"rt-{int(profile.runtime_workspace_id)}",
            endpoint_base_url="http://runtime.invalid",
            endpoint_host_header="runtime.invalid",
            api_key="test-key",
            timeout_seconds=1,
        )


class _ControlClient:
    def __init__(self) -> None:
        self.requests = []

    async def control_profile_execution(self, endpoint, request):
        del endpoint
        self.requests.append(request)
        return ProfileExecutionControlResponse(
            operation_id=request.operation_id,
            runtime_agent_profile_id=request.runtime_agent_profile_id,
            action=request.action,
            admission_generation=request.admission_generation,
            status="applied",
        )


@pytest.mark.asyncio
async def test_force_disable_and_enable_preserve_binding_and_converge_monotonically() -> None:
    engine, sessions = await _database()
    client = _ControlClient()
    async with sessions() as session:
        await _seed_market_experts(session)
        result = await ExpertSecurityService(session).set_execution_enabled(
            org_id=1,
            expert_package_id=701,
            enabled=False,
            reason="security incident",
            rollout_id="expert-security-disable-701",
            actor_user_id=9,
        )
        await session.commit()
        assert result.affected_profile_count == 2

    rollout_worker = AssetRolloutWorker(session_factory=sessions, concurrency=2)
    assert await rollout_worker.reconcile_once() == 2
    control_worker = ProfileExecutionControlWorker(
        session_factory=sessions,
        worker_id="control-test",
        client=client,
        target_resolver=_TargetResolver(),
        concurrency=2,
    )
    assert await control_worker.reconcile_once() == 2

    async with sessions() as session:
        package = await session.get(ExpertPackage, 701)
        assert package is not None
        assert package.status == "published"
        assert package.execution_status == "force_disabled"
        assert package.execution_status_reason == "security incident"
        assert set(await session.scalars(select(InstalledExpertBinding.status))) == {
            "enabled"
        }
        jobs = list(await session.scalars(select(ProfileExecutionControlJob)))
        assert {item.applied_action for item in jobs} == {"revoke"}
        assert {item.applied_generation for item in jobs} == {1}
        targets = list(await session.scalars(select(AssetRolloutTarget)))
        assert {item.status for item in targets} == {"revoked"}

        enabled = await ExpertSecurityService(session).set_execution_enabled(
            org_id=1,
            expert_package_id=701,
            enabled=True,
            reason="incident cleared",
            rollout_id="expert-security-enable-701",
            actor_user_id=9,
        )
        await session.commit()
        assert enabled.affected_profile_count == 2

    assert await rollout_worker.reconcile_once() == 2
    assert await control_worker.reconcile_once() == 2

    async with sessions() as session:
        package = await session.get(ExpertPackage, 701)
        assert package is not None
        assert package.status == "published"
        assert package.execution_status == "enabled"
        assert package.execution_status_reason is None
        jobs = list(await session.scalars(select(ProfileExecutionControlJob)))
        assert {item.applied_action for item in jobs} == {"enable"}
        assert {item.applied_generation for item in jobs} == {2}
        batches = list(
            await session.scalars(
                select(AssetRolloutBatch).order_by(AssetRolloutBatch.id)
            )
        )
        assert [item.status for item in batches] == ["completed", "completed"]
        targets = list(
            await session.scalars(
                select(AssetRolloutTarget).where(
                    AssetRolloutTarget.asset_rollout_batch_id == batches[-1].id
                )
            )
        )
        assert {item.status for item in targets} == {"applied"}

    assert [item.action for item in client.requests] == [
        "revoke",
        "revoke",
        "enable",
        "enable",
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_security_rollout_id_is_idempotent_and_payload_conflicts() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed_market_experts(session)
        service = ExpertSecurityService(session)
        first = await service.set_execution_enabled(
            org_id=1,
            expert_package_id=701,
            enabled=False,
            reason="security incident",
            rollout_id="expert-security-same-key",
            actor_user_id=9,
        )
        second = await service.set_execution_enabled(
            org_id=1,
            expert_package_id=701,
            enabled=False,
            reason="security incident",
            rollout_id="expert-security-same-key",
            actor_user_id=9,
        )
        assert first.created is True
        assert second.created is False
        await session.rollback()
    await engine.dispose()
