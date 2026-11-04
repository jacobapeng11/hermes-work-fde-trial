from __future__ import annotations

from dataclasses import replace

import pytest

from app.hermes_runtime.providers.knative import (
    KubectlCommandError,
    KnativeRuntimeProvider,
    KnativeRuntimeSpec,
)
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.services.runtime_platform_control import (
    RuntimePlatformControlKeyMaterial,
    runtime_pvc_storage_identity_hash,
)
from app.services.runtime_workspace import (
    RuntimeBindingPlan,
    RuntimeBindingReadyService,
)


PVC_UID = "pvc-uid-191"
PVC_IDENTITY = runtime_pvc_storage_identity_hash(
    namespace="hermes-runtimes",
    pvc_uid=PVC_UID,
)


def _plan() -> RuntimeBindingPlan:
    return RuntimeBindingPlan(
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
        runtime_root_pvc_uid=PVC_UID,
        pvc_storage_identity_hash=PVC_IDENTITY,
        platform_control_secret_name="runtime-191-platform-control",
        platform_control_key_version=3,
    )


def _spec(**overrides) -> KnativeRuntimeSpec:
    values = {
        "runtime_config_revision": "generation-7",
        "initial_config_download_url": "/internal/v1/config-bundles/personal/191/7",
        "initial_config_digest": "sha256:" + "4" * 64,
        **overrides,
    }
    return replace(
        RuntimeBindingReadyService.build_knative_spec(
            plan=_plan(),
            namespace="hermes-runtimes",
            image="kind.local/hermes-agent:target",
            service_account="hermes-runtime-workload",
            startup_mode="steady_state",
            runtime_node_name="runtime-node",
            enterprise_knowledge_host_path="",
        ),
        **values,
    )


def test_kservice_manifest_controls_zero_initial_scale_explicitly() -> None:
    provider = KnativeRuntimeProvider()

    assert "autoscaling.knative.dev/initial-scale" not in (
        provider.build_kservice_manifest(_spec())["spec"]["template"]["metadata"][
            "annotations"
        ]
    )
    manifest = provider.build_kservice_manifest(
        replace(_spec(), zero_initial_scale=True)
    )
    annotations = manifest["spec"]["template"]["metadata"]["annotations"]
    assert annotations["autoscaling.knative.dev/min-scale"] == "0"
    assert annotations["autoscaling.knative.dev/initial-scale"] == "0"


def test_precreated_match_requires_exact_zero_scale_contract() -> None:
    provider = KnativeRuntimeProvider()
    spec = replace(_spec(), zero_initial_scale=True)
    manifest = provider.build_kservice_manifest(spec)
    kservice = {
        **manifest,
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "latestCreatedRevisionName": "runtime-191-00001",
            "latestReadyRevisionName": "runtime-191-00001",
        },
    }

    assert provider.precreated_kservice_matches(kservice, spec) is True
    assert (
        provider.precreated_kservice_matches(
            kservice,
            replace(spec, image="kind.local/hermes-agent:other"),
        )
        is False
    )
    stale_revision = {
        **kservice,
        "status": {
            **kservice["status"],
            "latestReadyRevisionName": "runtime-191-00000",
        },
    }
    assert provider.precreated_kservice_matches(stale_revision, spec) is False


@pytest.mark.asyncio
async def test_zero_scale_ready_path_does_not_call_runtime_health() -> None:
    class Kubernetes:
        def __init__(self):
            self.applied = []

        async def apply(self, manifest):
            self.applied.append(manifest)

        async def zero_initial_scale_allowed(self):
            return True

        async def get_persistent_volume_claim(self, *, namespace, name):
            return {"metadata": {"uid": PVC_UID}}

        async def get_kservice(self, *, namespace, name):
            return {
                "status": {
                    "url": "http://runtime-191.hermes.local",
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "latestCreatedRevisionName": "runtime-191-00001",
                    "latestReadyRevisionName": "runtime-191-00001",
                }
            }

        async def delete_service(self, *, namespace, name):
            return None

    class BindingStore:
        def __init__(self):
            self.patches = []

        async def bind_pvc_identity(self, **kwargs):
            return None

        async def update_binding_status(self, patch):
            self.patches.append(patch)

    class Protocol:
        async def health(self, endpoint: RuntimeEndpoint):
            raise AssertionError("zero-scale precreation must not activate Runtime")

    kubernetes = Kubernetes()
    store = BindingStore()

    result = await RuntimeBindingReadyService(
        k8s_client=kubernetes,
        binding_store=store,
        protocol_adapter=Protocol(),
    ).ensure_kservice_ready(
        spec=_spec(),
        api_server_key="runtime-key",
        platform_control_key=RuntimePlatformControlKeyMaterial(
            binding_type="personal_runtime",
            binding_id=191,
            runtime_id="rt-191",
            key_version=3,
            root_key=b"k" * 32,
        ),
        ingress_base_url="http://backend",
        zero_initial_scale=True,
    )

    service_manifest = next(item for item in kubernetes.applied if item["kind"] == "Service")
    annotations = service_manifest["spec"]["template"]["metadata"]["annotations"]
    assert annotations["autoscaling.knative.dev/initial-scale"] == "0"
    assert result.status == "ready"
    assert [patch.status for patch in store.patches] == ["starting", "ready"]


@pytest.mark.asyncio
async def test_zero_scale_probe_treats_missing_kservice_as_no_match() -> None:
    class Kubernetes:
        async def get_kservice(self, *, namespace, name):
            raise KubectlCommandError(
                f'kubectl get ksvc {name} -n {namespace} failed: '
                f'Error from server (NotFound): services.serving.knative.dev '
                f'"{name}" not found'
            )

    result = await RuntimeBindingReadyService(
        k8s_client=Kubernetes(),
        binding_store=object(),
        protocol_adapter=object(),
    ).has_matching_zero_scale_kservice(spec=_spec())

    assert result is False
