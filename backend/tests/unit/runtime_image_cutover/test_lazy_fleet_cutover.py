from __future__ import annotations

import importlib.util
import json
import base64
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

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
from app.services.runtime_image_config import RuntimeImageConfig
from app.services.runtime_platform_control import runtime_pvc_storage_identity_hash
from app.services.runtime_reload import (
    RuntimeReloadBinding,
    RuntimeReloadError,
    RuntimeReloadService,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
TARGET = RuntimeImageConfig(
    image="kind.local/hermes-agent:v3-test-target",
    manifest_digest="sha256:" + "2" * 64,
    config_id="sha256:" + "1" * 64,
)


def _binding_dict(binding_id: int, binding_type: str) -> dict:
    runtime_id = f"rt-{binding_id}" if binding_type == "personal_runtime" else f"app-{binding_id}"
    return {
        "binding_type": binding_type,
        "binding_id": binding_id,
        "runtime_id": runtime_id,
        "namespace": "hermes-runtimes",
        "service_name": f"runtime-{binding_id}",
        "pvc_name": f"runtime-{binding_id}-root",
        "pvc_uid": f"uid-{binding_id}",
        "pvc_storage_identity_hash": f"sha256:hash-{binding_id}",
        "image": "old-image",
        "image_digest": "sha256:old",
        "status": "ready",
    }


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_defer_runtime_reload_keeps_login_admission_ready(tmp_path) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/lazy.db")
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
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            RuntimeMaintenanceControl(
                maintenance_state="frozen",
                maintenance_epoch="epoch-lazy",
                maintenance_reason="unit test lazy cutover",
                started_at=datetime(2026, 10, 30, 12, 0, 0),
                grant_issuance_enabled=False,
            )
        )
        session.add(
            HermesRuntimeBinding(
                id=191,
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                runtime_id="rt-191",
                image="old-image",
                image_digest="sha256:old",
                k8s_namespace="hermes-runtimes",
                knative_service="runtime-10001",
                runtime_root_pvc_uid="uid-191",
                pvc_storage_identity_hash="sha256:hash-191",
                hermes_home_sub_path="home",
                workspace_sub_path="workspace",
                hermes_home_mount_path="/opt/data",
                workspace_mount_path="/opt/workspace",
                api_key_encrypted="encrypted",
                status="updating",
                pending_reload=True,
            )
        )
        await session.commit()

        service = RuntimeReloadService(session)
        binding = RuntimeReloadBinding(**_binding_dict(191, "personal_runtime"))
        completed = await service.defer_runtime_reload(
            binding,
            target=TARGET,
            maintenance_epoch="epoch-lazy",
        )

        assert completed.status == "ready"
        assert completed.image == TARGET.image
        assert completed.image_digest == TARGET.config_id
        row = await session.get(HermesRuntimeBinding, 191)
        assert row is not None
        assert row.pending_reload is True
        assert row.last_error is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_defer_runtime_reload_rejects_wrong_maintenance_epoch(tmp_path) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/lazy-epoch.db")
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
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            RuntimeMaintenanceControl(
                maintenance_state="frozen",
                maintenance_epoch="epoch-owner",
                maintenance_reason="unit test lazy cutover",
                started_at=datetime(2026, 10, 30, 12, 0, 0),
                grant_issuance_enabled=False,
            )
        )
        await session.commit()
        service = RuntimeReloadService(session)
        binding = RuntimeReloadBinding(**_binding_dict(192, "personal_runtime"))
        with pytest.raises(RuntimeReloadError):
            await service.defer_runtime_reload(
                binding,
                target=TARGET,
                maintenance_epoch="epoch-other",
            )
    await engine.dispose()


def test_live_activation_plan_defers_only_idle_personal_runtimes(monkeypatch) -> None:
    fleet = _load_module(
        "reload_all_runtimes_lazy",
        REPOSITORY_ROOT / "scripts/reload-all-runtimes-from-env-dev.py",
    )
    personal_idle = _binding_dict(191, "personal_runtime")
    personal_active = _binding_dict(193, "personal_runtime")
    application_idle = _binding_dict(17, "application_runtime")
    personal_active["runtime_id"] = "rt-115"
    application_idle["runtime_id"] = "application-agent-runtime-25"

    def fake_control(arguments: list[str]) -> str:
        assert arguments == ["classify"]
        return json.dumps(
            [
                {
                    "binding_type": "personal_runtime",
                    "binding_id": 191,
                    "runtime_id": "rt-191",
                    "service_name": "runtime-191",
                    "running_pod_count": 0,
                },
                {
                    "binding_type": "personal_runtime",
                    "binding_id": 193,
                    "runtime_id": "rt-115",
                    "service_name": "runtime-193",
                    "running_pod_count": 1,
                },
                {
                    "binding_type": "application_runtime",
                    "binding_id": 17,
                    "runtime_id": "application-agent-runtime-25",
                    "service_name": "application-agent-25",
                    "running_pod_count": 0,
                },
            ]
        )

    monkeypatch.setattr(fleet, "_control", fake_control)
    plan = fleet._live_activation_plan(
        [personal_idle, personal_active, application_idle]
    )
    assert plan[fleet._binding_key(personal_idle)] == "deferred"
    assert plan[fleet._binding_key(personal_active)] == "eager"
    assert plan[fleet._binding_key(application_idle)] == "eager"


def test_legacy_checkpoint_after_pod_zero_never_guesses_deferred() -> None:
    fleet = _load_module(
        "reload_all_runtimes_legacy",
        REPOSITORY_ROOT / "scripts/reload-all-runtimes-from-env-dev.py",
    )
    bindings = [
        _binding_dict(191, "personal_runtime"),
        _binding_dict(17, "application_runtime"),
    ]
    plan = fleet._resolve_activation_plan(
        bindings,
        checks={},
        preserved_plan=None,
        stage="pods_zero",
        lazy_enabled=True,
    )
    assert all(value == "eager" for value in plan.values())


def test_no_lazy_mode_keeps_all_eager() -> None:
    fleet = _load_module(
        "reload_all_runtimes_nolazy",
        REPOSITORY_ROOT / "scripts/reload-all-runtimes-from-env-dev.py",
    )
    bindings = [_binding_dict(191, "personal_runtime")]
    plan = fleet._resolve_activation_plan(
        bindings,
        checks={"activation_plan": {fleet._binding_key(bindings[0]): "deferred"}},
        preserved_plan=None,
        stage="planned",
        lazy_enabled=False,
    )
    assert plan == {fleet._binding_key(bindings[0]): "eager"}


def test_cancel_active_skips_endpoint_call_for_idle_runtime() -> None:
    source = (
        REPOSITORY_ROOT / "backend/scripts/runtime_code_reload.py"
    ).read_text(encoding="utf-8")

    cancel_branch = source.index('if args.command == "cancel-active":')
    idle_short_circuit = source.index("backend_idle_no_endpoint_call")
    endpoint_client = source.index("client = RuntimeCommandClient()")
    assert cancel_branch < idle_short_circuit < endpoint_client


@pytest.mark.asyncio
async def test_verify_allow_absent_accepts_only_target_owned_binding() -> None:
    control = _load_module(
        "runtime_code_reload_lazy",
        REPOSITORY_ROOT / "backend/scripts/runtime_code_reload.py",
    )

    class FakeKubernetes:
        async def get_persistent_volume_claim(self, *, namespace: str, name: str):
            return {"metadata": {"uid": "uid-191"}}

        async def kservice_exists(self, *, namespace: str, name: str) -> bool:
            return False

    binding = RuntimeReloadBinding(
        **{
            **_binding_dict(191, "personal_runtime"),
            "image": TARGET.image,
            "image_digest": TARGET.config_id,
            "status": "ready",
            "pvc_storage_identity_hash": runtime_pvc_storage_identity_hash(
                namespace="hermes-runtimes",
                pvc_uid="uid-191",
            ),
        }
    )
    evidence = await control._verify(
        FakeKubernetes(),
        binding,
        target=SimpleNamespace(
            image=TARGET.image,
            config_id=TARGET.config_id,
            manifest_digest=TARGET.manifest_digest,
        ),
        timeout=0.1,
        allow_absent=True,
    )
    assert evidence["ready"] is True
    assert evidence["activation"] == "deferred_to_next_login"
    assert evidence["live_image_ids"] == []

    stale = RuntimeReloadBinding(
        **{
            **_binding_dict(191, "personal_runtime"),
            "image": "old-image",
            "image_digest": "sha256:old",
            "status": "ready",
            "pvc_storage_identity_hash": runtime_pvc_storage_identity_hash(
                namespace="hermes-runtimes",
                pvc_uid="uid-191",
            ),
        }
    )
    with pytest.raises(RuntimeError):
        await control._verify(
            FakeKubernetes(),
            stale,
            target=SimpleNamespace(
                image=TARGET.image,
                config_id=TARGET.config_id,
                manifest_digest=TARGET.manifest_digest,
            ),
            timeout=0.1,
            allow_absent=True,
        )


@pytest.mark.asyncio
async def test_verify_allow_absent_accepts_recreated_scaled_zero_runtime() -> None:
    control = _load_module(
        "runtime_code_reload_scaled_zero",
        REPOSITORY_ROOT / "backend/scripts/runtime_code_reload.py",
    )
    pvc_hash = runtime_pvc_storage_identity_hash(
        namespace="hermes-runtimes",
        pvc_uid="uid-191",
    )
    secret_document = {
        "runtime_id": "rt-191",
        "binding_type": "personal_runtime",
        "binding_id": 191,
        "pvc_storage_identity_hash": pvc_hash,
    }

    class FakeScaledZeroKubernetes:
        async def get_persistent_volume_claim(self, *, namespace: str, name: str):
            return {"metadata": {"uid": "uid-191"}}

        async def kservice_exists(self, *, namespace: str, name: str) -> bool:
            return True

        async def get_kservice(self, *, namespace: str, name: str):
            return {
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                    ],
                },
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "hermes.platform/pvc-storage-identity-hash": pvc_hash,
                            },
                        },
                        "spec": {
                            "containers": [
                                {
                                    "name": "user-container",
                                    "image": TARGET.image,
                                    "env": [
                                        {
                                            "name": "HERMES_STARTUP_MODE",
                                            "value": "steady_state",
                                        },
                                        {
                                            "name": "RUNTIME_INITIAL_CONFIG_DOWNLOAD_URL",
                                            "value": "https://config.test/bundle.tar.gz",
                                        },
                                        {
                                            "name": "RUNTIME_INITIAL_CONFIG_DIGEST",
                                            "value": "sha256:" + "3" * 64,
                                        },
                                    ],
                                },
                            ],
                        },
                    },
                },
            }

        async def get_secret(self, *, namespace: str, name: str):
            return {
                "data": {
                    "control-keys.json": base64.b64encode(
                        json.dumps(secret_document).encode()
                    ).decode(),
                },
            }

        async def list_kservice_pods(self, *, namespace: str, service_name: str):
            return []

    binding = RuntimeReloadBinding(
        **{
            **_binding_dict(191, "personal_runtime"),
            "image": TARGET.image,
            "image_digest": TARGET.config_id,
            "status": "ready",
            "pvc_storage_identity_hash": pvc_hash,
        }
    )
    evidence = await control._verify(
        FakeScaledZeroKubernetes(),
        binding,
        target=SimpleNamespace(
            image=TARGET.image,
            config_id=TARGET.config_id,
            manifest_digest=TARGET.manifest_digest,
        ),
        timeout=0.1,
        allow_absent=True,
    )
    assert evidence["ready"] is True
    assert evidence["activation"] == "scaled_to_zero"
