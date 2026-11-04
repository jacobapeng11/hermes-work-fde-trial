from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.runtime_activity import RuntimeActivitySnapshot
from app.services.runtime_platform_control import runtime_pvc_storage_identity_hash
from app.services.runtime_reconcile import RuntimeTopologyReconciler
from app.services.runtime_reload import RuntimeReloadBinding
from app.services.runtime_workspace import (
    RuntimeBindingEnsureResult,
    RuntimeBindingPlan,
    RuntimeBindingStatusPatch,
    RuntimeWorkspaceRecord,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _binding() -> RuntimeReloadBinding:
    return RuntimeReloadBinding(
        binding_type="personal_runtime",
        binding_id=191,
        runtime_id="rt-191",
        namespace="hermes-runtimes",
        service_name="runtime-191",
        pvc_name="runtime-191-root",
        pvc_uid="uid-191",
        pvc_storage_identity_hash=runtime_pvc_storage_identity_hash(
            namespace="hermes-runtimes",
            pvc_uid="uid-191",
        ),
        image="kind.local/hermes-agent:target",
        image_digest="sha256:" + "1" * 64,
        status="ready",
    )


@pytest.mark.asyncio
async def test_verify_precreated_zero_scale_requires_ready_target_and_zero_pods() -> None:
    control = _load_module(
        "runtime_code_reload_zero_scale",
        REPOSITORY_ROOT / "backend/scripts/runtime_code_reload.py",
    )
    binding = _binding()
    target = SimpleNamespace(
        image=binding.image,
        config_id=binding.image_digest,
        manifest_digest="sha256:" + "2" * 64,
    )
    secret = {
        "data": {
            "control-keys.json": base64.b64encode(
                json.dumps(
                    {
                        "runtime_id": binding.runtime_id,
                        "binding_type": binding.binding_type,
                        "binding_id": binding.binding_id,
                        "pvc_storage_identity_hash": binding.pvc_storage_identity_hash,
                        "current_key_version": 3,
                    }
                ).encode()
            ).decode()
        }
    }
    kservice = {
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "latestCreatedRevisionName": "runtime-191-00001",
            "latestReadyRevisionName": "runtime-191-00001",
        },
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "autoscaling.knative.dev/min-scale": "0",
                        "autoscaling.knative.dev/initial-scale": "0",
                        "hermes.platform/pvc-storage-identity-hash": (
                            binding.pvc_storage_identity_hash
                        ),
                        "hermes.platform/control-key-version": "3",
                    }
                },
                "spec": {
                    "containers": [
                        {
                            "image": binding.image,
                            "env": [
                                {"name": "HERMES_STARTUP_MODE", "value": "steady_state"},
                                {
                                    "name": "RUNTIME_INITIAL_CONFIG_DOWNLOAD_URL",
                                    "value": "/internal/config",
                                },
                                {
                                    "name": "RUNTIME_INITIAL_CONFIG_DIGEST",
                                    "value": "sha256:config",
                                },
                            ],
                        }
                    ]
                },
            }
        },
    }

    class Kubernetes:
        def __init__(self, pods):
            self.pods = pods

        async def get_persistent_volume_claim(self, *, namespace, name):
            return {"metadata": {"uid": binding.pvc_uid}}

        async def get_kservice(self, *, namespace, name):
            return kservice

        async def get_secret(self, *, namespace, name):
            return secret

        async def list_kservice_pods(self, *, namespace, service_name):
            return self.pods

    evidence = await control._verify(
        Kubernetes(pods=[]),
        binding,
        target=target,
        timeout=0.1,
        precreated_zero_scale=True,
    )
    assert evidence["ready"] is True
    assert evidence["activation"] == "deferred_zero_scale_to_next_login"
    assert evidence["live_image_ids"] == []

    with pytest.raises(RuntimeError, match="must not have a Pod"):
        await control._verify(
            Kubernetes(pods=[{"metadata": {"name": "runtime-191"}}]),
            binding,
            target=target,
            timeout=0.1,
            precreated_zero_scale=True,
        )


def test_fleet_verify_uses_explicit_precreated_zero_scale_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet = _load_module(
        "reload_all_zero_scale",
        REPOSITORY_ROOT / "scripts/reload-all-runtimes-from-env-dev.py",
    )
    binding = {
        "binding_type": "personal_runtime",
        "binding_id": 191,
        "runtime_id": "rt-191",
    }
    captured = {}

    def fake_parallel(bindings, command, concurrency, *, extra=None, on_success=None):
        if extra == ["--precreated-zero-scale"]:
            captured["deferred"] = (bindings, command, concurrency, extra)
        return [(binding, json.dumps({"ready": True}))]

    monkeypatch.setattr(fleet, "_parallel", fake_parallel)
    output = fleet._verify_by_plan(
        [binding],
        {fleet._binding_key(binding): "deferred"},
        3,
        precreate_zero_scale=True,
    )

    assert output[fleet._binding_key(binding)] == '{"ready": true}'
    assert captured["deferred"] == ([binding], "verify", 3, ["--precreated-zero-scale"])


@pytest.mark.asyncio
async def test_reconcile_activates_matching_precreated_kservice_without_reapply() -> None:
    events: list[str] = []
    record = RuntimeWorkspaceRecord(
        runtime_workspace_id=191,
        cloud_profile_id=191,
        org_id=1,
        user_id=2,
        user_name="测试用户",
        user_code="191",
        name="默认数字人",
        status="active",
        runtime_status="ready",
        pending_reload=True,
    )
    from app.services.runtime_platform_control import (
        RuntimePlatformControlKeyMaterial,
    )

    binding = RuntimeBindingEnsureResult(
        plan=RuntimeBindingPlan(
            org_id=1,
            user_id=2,
            user_name="测试用户",
            user_code="191",
            runtime_workspace_id=191,
            runtime_id="rt-191",
            service_name="runtime-191",
            secret_name="runtime-191-api-key",
            hermes_home_sub_path="home",
            workspace_sub_path="workspace",
            runtime_root_pvc_name="runtime-191-root",
            runtime_root_host_path="/tmp/runtime-191",
            binding_id="191",
            config_revision="generation-7",
            image="kind.local/hermes-agent:target",
            runtime_root_pvc_uid="uid-191",
            pvc_storage_identity_hash=runtime_pvc_storage_identity_hash(
                namespace="hermes-runtimes",
                pvc_uid="uid-191",
            ),
            platform_control_secret_name="runtime-191-platform-control",
            platform_control_key_version=3,
        ),
        api_server_key="runtime-key",
        binding_status="ready",
        reconcile_required=True,
        platform_control_key=RuntimePlatformControlKeyMaterial(
            binding_type="personal_runtime",
            binding_id=191,
            runtime_id="rt-191",
            key_version=3,
            root_key=b"k" * 32,
        ),
    )

    class Admission:
        async def begin_reconcile(self, workspace_id, target_status, **kwargs):
            events.append("admission:starting")
            return True

        async def mark_failed(self, workspace_id, message):
            raise AssertionError(message)

    class Activity:
        async def get_snapshot(self, workspace_id):
            return RuntimeActivitySnapshot(run_count=0, self_evolution_count=0)

    class Kubernetes:
        async def kservice_exists(self, *, namespace, name):
            return True

        async def delete_kservice(self, *, namespace, name):
            events.append("kservice:delete")

        async def list_kservice_pods(self, *, namespace, service_name):
            events.append("pods:list")
            return []

    class Directory:
        async def ensure_directory(self, spec):
            events.append("directory:ensure")

    class ConfigPreparer:
        async def prepare(self, spec):
            events.append("config:bundle")
            return spec

    class ReadyService:
        async def has_matching_zero_scale_kservice(self, **kwargs):
            return True

        async def activate_matching_kservice(self, **kwargs):
            events.append("kservice:activate")
            return RuntimeBindingStatusPatch(
                runtime_workspace_id=191,
                knative_status_url="http://runtime-191.hermes.local",
                route_host="runtime-191.hermes.local",
                endpoint_base_url="http://backend",
                endpoint_host_header="runtime-191.hermes.local",
                status="ready",
                last_error=None,
            )

        async def ensure_kservice_ready(self, **kwargs):
            raise AssertionError("matching precreated KService must not be reapplied")

    service = RuntimeTopologyReconciler(
        admission_service=Admission(),
        activity_store=Activity(),
        k8s_client=Kubernetes(),
        ready_service=ReadyService(),
        directory_provisioner=Directory(),
        config_preparer=ConfigPreparer(),
        namespace="hermes-runtimes",
        service_account="hermes-runtime-workload",
        ingress_base_url="http://backend",
        runtime_node_name="runtime-node",
        drain_timeout_seconds=10,
        enterprise_knowledge_host_path="",
    )

    result = await service.reconcile(record, binding)

    assert result.runtime_status == "ready"
    assert result.pending_reload is False
    assert events == ["admission:starting", "config:bundle", "kservice:activate"]
