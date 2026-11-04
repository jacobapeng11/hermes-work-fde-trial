from __future__ import annotations

from datetime import datetime

import pytest

from app.models.base import Base
from app.models import (
    HermesRuntimeBinding,
    ProfileConvergenceJob,
    RuntimeAgentProfile,
    RuntimeMaintenanceControl,
    RuntimeMcpRegistryJob,
    RuntimeMcpRegistryState,
)
from app.services.runtime_activity import RuntimeActivitySnapshot
from app.services import runtime_reload
from app.services.runtime_image_config import RuntimeImageConfig
from app.services.runtime_reload import RuntimeReloadBinding, RuntimeReloadService
from app.services.runtime_workspace import (
    RuntimeBindingEnsureResult,
    RuntimeWorkspaceRecord,
)


TARGET = RuntimeImageConfig(
    image="kind.local/hermes-agent:zero-scale",
    manifest_digest="sha256:" + "2" * 64,
    config_id="sha256:" + "1" * 64,
)


@pytest.mark.asyncio
async def test_precreate_keeps_login_admission_ready_and_pending_reload(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/zero-scale.db")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                RuntimeMaintenanceControl.__table__,
                HermesRuntimeBinding.__table__,
                RuntimeAgentProfile.__table__,
                ProfileConvergenceJob.__table__,
                RuntimeMcpRegistryState.__table__,
                RuntimeMcpRegistryJob.__table__,
            ],
        )

    class WorkspaceStore:
        async def prepare_reload(self, *, runtime_id, target, maintenance_epoch):
            assert runtime_id == "rt-191"
            assert target is TARGET
            assert maintenance_epoch == "epoch-zero"
            record = RuntimeWorkspaceRecord(
                runtime_workspace_id=191,
                cloud_profile_id=191,
                org_id=1,
                user_id=2,
                user_name="测试用户",
                user_code="191",
                name="默认数字人",
                status="active",
                runtime_status="updating",
                pending_reload=True,
            )
            owned = RuntimeBindingEnsureResult(
                plan=None,  # type: ignore[arg-type]
                api_server_key="runtime-key",
                reconcile_owned=True,
                reconcile_required=True,
                binding_status="updating",
            )
            return record, owned

    class Topology:
        async def precreate_zero_scale_runtime(self, record, owned):
            assert owned.binding_status == "updating"
            return record

    class Infrastructure:
        workspace_store = WorkspaceStore()
        topology_reconciler = Topology()

    class ActivityStore:
        async def get_snapshot(self, runtime_workspace_id):
            assert runtime_workspace_id == 191
            return RuntimeActivitySnapshot(run_count=0, self_evolution_count=0)

    def fake_infrastructure(_session):
        return Infrastructure()

    monkeypatch.setattr(runtime_reload, "build_runtime_infrastructure", fake_infrastructure)
    monkeypatch.setattr(
        runtime_reload,
        "SqlAlchemyRuntimeActivityStore",
        lambda _session: ActivityStore(),
    )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            RuntimeMaintenanceControl(
                maintenance_state="frozen",
                maintenance_epoch="epoch-zero",
                maintenance_reason="unit test zero scale",
                started_at=datetime(2026, 10, 31, 12, 0, 0),
                grant_issuance_enabled=False,
            )
        )
        session.add(
            RuntimeAgentProfile(
                id=3,
                org_id=1,
                user_id=2,
                runtime_workspace_id=191,
                hermes_profile_name="default",
                profile_kind="default",
                display_name="默认数字人",
                desired_revision=23,
                status="active",
            )
        )
        session.add(
            ProfileConvergenceJob(
                id=3,
                org_id=1,
                runtime_workspace_id=191,
                runtime_agent_profile_id=3,
                target_revision=23,
                status="applied",
                stage="complete",
                available_at=datetime(2026, 10, 30, 12, 0, 0),
            )
        )
        session.add(
            HermesRuntimeBinding(
                id=191,
                org_id=1,
                user_id=2,
                runtime_workspace_id=191,
                runtime_id="rt-191",
                image="old-image",
                image_digest="sha256:old",
                k8s_namespace="hermes-runtimes",
                knative_service="runtime-191",
                runtime_root_pvc_uid="uid-191",
                pvc_storage_identity_hash="sha256:hash-191",
                hermes_home_sub_path="home",
                workspace_sub_path="workspace",
                hermes_home_mount_path="/opt/data",
                workspace_mount_path="/data/workspace",
                api_key_encrypted="encrypted",
                status="updating",
                pending_reload=True,
            )
        )
        await session.commit()

        completed = await RuntimeReloadService(session).precreate_deferred_runtime(
            RuntimeReloadBinding(
                binding_type="personal_runtime",
                binding_id=191,
                runtime_id="rt-191",
                namespace="hermes-runtimes",
                service_name="runtime-191",
                pvc_name="runtime-191-root",
                pvc_uid="uid-191",
                pvc_storage_identity_hash="sha256:hash-191",
                image="old-image",
                image_digest="sha256:old",
                status="updating",
            ),
            target=TARGET,
            maintenance_epoch="epoch-zero",
        )

        assert completed.status == "ready"
        assert completed.image == TARGET.image
        assert completed.image_digest == TARGET.config_id
        row = await session.get(HermesRuntimeBinding, 191)
        assert row is not None
        assert row.pending_reload is True
        assert row.last_error is None
        job = await session.get(ProfileConvergenceJob, 3)
        assert job is not None
        assert job.status == "applied"
        assert job.stage == "complete"
        assert job.available_at == datetime(2026, 10, 30, 12, 0, 0)

    await engine.dispose()
