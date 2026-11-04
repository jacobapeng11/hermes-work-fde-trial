from dataclasses import replace

import pytest

from app.hermes_runtime.providers.knative import KnativeRuntimeProvider, KnativeRuntimeSpec
from app.services.runtime_bootstrap_intent import RuntimeBootstrapAuthorization
from app.services.runtime_fresh_init import RuntimeFreshInitError, RuntimeFreshInitProvisioner


PVC_IDENTITY = "sha256:" + "a" * 64


def _spec() -> KnativeRuntimeSpec:
    return KnativeRuntimeSpec(
        org_id=1,
        user_id=2,
        user_name="fresh",
        user_code="fresh-2",
        runtime_workspace_id=101,
        runtime_id="rt-101",
        namespace="hermes-runtimes",
        service_name="runtime-fresh-101",
        image="kind.local/hermes-agent:build-101",
        service_account="runtime",
        api_server_key_secret_name="runtime-fresh-101-api-key",
        runtime_root_pvc_name="runtime-fresh-101-root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="runtime-fresh-101-root-pv",
        runtime_root_host_path="/runtime/1/101",
        runtime_node_name="node-1",
        upstream_commit="current",
        state_schema=25,
        config_schema=34,
        startup_mode="fresh_bootstrap",
        runtime_binding_id="205",
        runtime_root_pvc_uid="pvc-uid-101",
        pvc_storage_identity_hash=PVC_IDENTITY,
        platform_control_secret_name="runtime-fresh-101-platform-control",
        platform_control_key_version=1,
        initial_config_download_url="http://backend/internal/runtime-config/205",
        initial_config_digest="b" * 64,
    )


@pytest.mark.asyncio
async def test_provisioner_orders_write_free_probe_stop_authorize_and_bootstrap() -> None:
    events: list[str] = []

    class Kubernetes:
        async def apply(self, manifest: dict) -> None:
            mode = next(
                item["value"]
                for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
                if item["name"] == "HERMES_STARTUP_MODE"
            )
            events.append(f"apply:{mode}")

        async def get_kservice(self, *, namespace: str, name: str) -> dict:
            del namespace, name
            return {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}

        async def delete_kservice(self, *, namespace: str, name: str) -> None:
            del namespace, name
            events.append("delete:probe")

        async def list_kservice_pods(self, *, namespace: str, service_name: str) -> list:
            del namespace, service_name
            events.append("pods:zero")
            return []

    class AuthoringStore:
        async def authorize(self, **kwargs: object) -> RuntimeBootstrapAuthorization:
            assert kwargs == {
                "binding_type": "personal_runtime",
                "binding_id": 205,
                "runtime_id": "rt-101",
                "pvc_storage_identity_hash": PVC_IDENTITY,
                "target_image": "kind.local/hermes-agent:build-101",
            }
            events.append("authorize")
            return RuntimeBootstrapAuthorization(
                intent_id="bootstrap-intent-205",
                authorization_digest="sha256:" + "c" * 64,
                status="authorized",
                document={},
            )

    provisioner = RuntimeFreshInitProvisioner(
        kubernetes=Kubernetes(),
        provider=KnativeRuntimeProvider(),
        authoring_store=AuthoringStore(),  # type: ignore[arg-type]
    )
    final_spec = await provisioner.prepare(_spec(), "http://runtime.example")

    assert events == ["apply:fresh_probe", "delete:probe", "pods:zero", "authorize"]
    assert final_spec.startup_mode == "fresh_bootstrap"
    assert final_spec.bootstrap_intent_id == "bootstrap-intent-205"
    assert final_spec.bootstrap_authorization_digest == "sha256:" + "c" * 64
    assert final_spec.initial_config_digest == "b" * 64


@pytest.mark.asyncio
async def test_provisioner_does_not_probe_steady_state() -> None:
    class NeverCalled:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(name)

    spec = replace(_spec(), startup_mode="steady_state")
    provisioner = RuntimeFreshInitProvisioner(
        kubernetes=NeverCalled(),  # type: ignore[arg-type]
        provider=KnativeRuntimeProvider(),
        authoring_store=NeverCalled(),  # type: ignore[arg-type]
    )

    assert await provisioner.prepare(spec, "http://runtime.example") is spec


@pytest.mark.asyncio
async def test_fresh_bootstrap_requires_current_config_bundle() -> None:
    class NeverCalled:
        pass

    provisioner = RuntimeFreshInitProvisioner(
        kubernetes=NeverCalled(),  # type: ignore[arg-type]
        provider=KnativeRuntimeProvider(),
        authoring_store=NeverCalled(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeFreshInitError, match="ConfigBundle"):
        await provisioner.prepare(
            replace(
                _spec(),
                initial_config_download_url=None,
                initial_config_digest=None,
            ),
            "http://runtime.example",
        )
