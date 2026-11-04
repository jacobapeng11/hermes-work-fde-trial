from __future__ import annotations

import pytest

from app.services.runtime_image_config import RuntimeImageConfig
from app.services.runtime_reload import RuntimeReloadBinding
from app.services.runtime_session_permission_maintenance import (
    RuntimeSessionPermissionMaintenance,
    build_session_permission_repair_job,
)


def test_session_permission_job_is_bound_to_exact_pvc_and_minimal_capabilities() -> None:
    binding = RuntimeReloadBinding(
        binding_type="personal_runtime",
        binding_id=193,
        runtime_id="rt-115",
        namespace="hermes-runtimes",
        service_name="runtime-10002",
        pvc_name="runtime-10002-root-g2",
        pvc_uid="425a5c19-9aac-49c2-bb40-456cd12e8ea5",
        pvc_storage_identity_hash="sha256:" + "a" * 64,
        image="kind.local/hermes-agent:old",
        image_digest="sha256:" + "b" * 64,
        status="ready",
    )
    target = RuntimeImageConfig(
        image="kind.local/hermes-agent:code-new",
        manifest_digest="sha256:" + "c" * 64,
        config_id="sha256:" + "d" * 64,
    )

    manifest = build_session_permission_repair_job(
        binding=binding,
        target=target,
        maintenance_epoch="runtime-code-test-20260901",
        apply=True,
    )

    assert manifest["kind"] == "Job"
    metadata = manifest["metadata"]
    assert metadata["namespace"] == binding.namespace
    assert metadata["labels"]["hermes.platform/runtime-id"] == binding.runtime_id
    assert metadata["labels"]["hermes.platform/pvc-uid"] == binding.pvc_uid
    pod_spec = manifest["spec"]["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    assert pod_spec["automountServiceAccountToken"] is False
    container = pod_spec["containers"][0]
    assert container["image"] == target.image
    assert container["volumeMounts"] == [{"name": "runtime-root", "mountPath": "/repair"}]
    assert container["securityContext"]["capabilities"] == {
        "drop": ["ALL"],
        "add": [
            "CHOWN",
            "DAC_OVERRIDE",
            "FOWNER",
            "FSETID",
            "SETGID",
            "SETUID",
        ],
    }
    assert container["args"][-2:] == ["--confirm", "REPAIR_ALL_MANAGED_SESSIONS"]
    assert pod_spec["volumes"][0]["persistentVolumeClaim"]["claimName"] == (
        binding.pvc_name
    )


async def test_session_permission_maintenance_runs_dry_then_apply_and_unmounts() -> None:
    binding = RuntimeReloadBinding(
        binding_type="application_runtime",
        binding_id=25,
        runtime_id="application-agent-runtime-25",
        namespace="hermes-runtimes",
        service_name="application-agent-25",
        pvc_name="application-agent-25-root",
        pvc_uid="pvc-25",
        pvc_storage_identity_hash="sha256:" + "a" * 64,
        image="kind.local/hermes-agent:old",
        image_digest="sha256:" + "b" * 64,
        status="ready",
    )
    target = RuntimeImageConfig(
        image="kind.local/hermes-agent:code-new",
        manifest_digest="sha256:" + "c" * 64,
        config_id="sha256:" + "d" * 64,
    )

    class Kubernetes:
        manifests: list[dict] = []
        deleted: list[str] = []

        async def kservice_exists(self, **_kwargs):
            return False

        async def get_persistent_volume_claim(self, **_kwargs):
            return {"metadata": {"uid": binding.pvc_uid}}

        async def delete_job(self, *, namespace: str, name: str):
            assert namespace == binding.namespace
            self.deleted.append(name)

        async def list_persistent_volume_claim_pods(self, **_kwargs):
            return []

        async def apply(self, manifest: dict):
            self.manifests.append(manifest)

        async def get_job(self, **_kwargs):
            return {"status": {"succeeded": 1}}

        async def get_job_logs(self, **_kwargs):
            applied = "--apply" in self.manifests[-1]["spec"]["template"]["spec"][
                "containers"
            ][0]["args"]
            return '{"ok":true,"applied":' + str(applied).lower() + "}\n"

    kubernetes = Kubernetes()
    receipt = await RuntimeSessionPermissionMaintenance(kubernetes).repair(  # type: ignore[arg-type]
        binding=binding,
        target=target,
        maintenance_epoch="runtime-code-test-20260901",
    )

    assert [
        "--apply" in manifest["spec"]["template"]["spec"]["containers"][0]["args"]
        for manifest in kubernetes.manifests
    ] == [False, True]
    assert receipt.dry_run["applied"] is False
    assert receipt.applied["applied"] is True
    assert len(kubernetes.deleted) == 4


async def test_session_permission_maintenance_preserves_safe_failure_diagnostics() -> None:
    binding = RuntimeReloadBinding(
        binding_type="personal_runtime",
        binding_id=193,
        runtime_id="rt-115",
        namespace="hermes-runtimes",
        service_name="runtime-10002",
        pvc_name="runtime-10002-root-g2",
        pvc_uid="pvc-193",
        pvc_storage_identity_hash="sha256:" + "a" * 64,
        image="kind.local/hermes-agent:old",
        image_digest="sha256:" + "b" * 64,
        status="ready",
    )
    target = RuntimeImageConfig(
        image="kind.local/hermes-agent:code-new",
        manifest_digest="sha256:" + "c" * 64,
        config_id="sha256:" + "d" * 64,
    )

    class Kubernetes:
        deleted: list[str] = []

        async def kservice_exists(self, **_kwargs):
            return False

        async def get_persistent_volume_claim(self, **_kwargs):
            return {"metadata": {"uid": binding.pvc_uid}}

        async def delete_job(self, *, name: str, **_kwargs):
            self.deleted.append(name)

        async def list_persistent_volume_claim_pods(self, **_kwargs):
            return []

        async def apply(self, _manifest: dict):
            return None

        async def get_job(self, **_kwargs):
            return {"status": {"failed": 1}}

        async def get_job_logs(self, **_kwargs):
            return (
                '{"ok":false,"error_type":"SessionPermissionRepairError",'
                '"error":"unknown POSIX identity: sha256:safe"}\n'
            )

    kubernetes = Kubernetes()
    with pytest.raises(
        RuntimeError,
        match=r"error_type=SessionPermissionRepairError error_digest=[0-9a-f]{12}",
    ):
        await RuntimeSessionPermissionMaintenance(kubernetes).repair(  # type: ignore[arg-type]
            binding=binding,
            target=target,
            maintenance_epoch="runtime-code-test-20260901",
        )

    assert len(kubernetes.deleted) == 3
