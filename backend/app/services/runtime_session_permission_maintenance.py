from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any

from app.hermes_runtime.providers.knative import KubectlKnativeClient
from app.services.runtime_image_config import RuntimeImageConfig
from app.services.runtime_reload import RuntimeReloadBinding


_APPLY_CONFIRMATION = "REPAIR_ALL_MANAGED_SESSIONS"
_WORKSPACE_GID = 1000


@dataclass(frozen=True, slots=True)
class RuntimeSessionPermissionRepairReceipt:
    runtime_id: str
    binding_type: str
    binding_id: int
    pvc_uid: str
    dry_run: dict[str, Any]
    applied: dict[str, Any]


def build_session_permission_repair_job(
    *,
    binding: RuntimeReloadBinding,
    target: RuntimeImageConfig,
    maintenance_epoch: str,
    apply: bool,
) -> dict[str, Any]:
    mode = "apply" if apply else "scan"
    identity = hashlib.sha256(
        (
            f"{maintenance_epoch}\0{binding.binding_type}\0"
            f"{binding.binding_id}\0{binding.pvc_uid}\0{mode}"
        ).encode("utf-8")
    ).hexdigest()[:12]
    job_name = f"session-permission-{binding.binding_id}-{mode}-{identity}"
    labels = {
        "app.kubernetes.io/name": "hermes-session-permission-repair",
        "hermes.platform/runtime-id": binding.runtime_id,
        "hermes.platform/runtime-binding-type": binding.binding_type,
        "hermes.platform/runtime-binding-id": str(binding.binding_id),
        "hermes.platform/pvc-uid": binding.pvc_uid,
    }
    arguments = [
        "-m",
        "platform_supervisor.session_permission_repair",
        "--pvc-root",
        "/repair",
    ]
    if apply:
        arguments.extend(["--apply", "--confirm", _APPLY_CONFIRMATION])
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": binding.namespace,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 900,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsUser": 0,
                        "runAsGroup": 0,
                        "supplementalGroups": [_WORKSPACE_GID],
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "repair",
                            "image": target.image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/opt/hermes/.venv/bin/python"],
                            "args": arguments,
                            "env": [
                                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                                {"name": "PYTHONNOUSERSITE", "value": "1"},
                            ],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {
                                    "drop": ["ALL"],
                                    "add": [
                                        "CHOWN",
                                        "DAC_OVERRIDE",
                                        "FOWNER",
                                        "FSETID",
                                        "SETGID",
                                        "SETUID",
                                    ],
                                },
                            },
                            "volumeMounts": [
                                {"name": "runtime-root", "mountPath": "/repair"}
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "runtime-root",
                            "persistentVolumeClaim": {
                                "claimName": binding.pvc_name,
                            },
                        }
                    ],
                },
            },
        },
    }


class RuntimeSessionPermissionMaintenance:
    """Run exact-PVC Session repairs after the business Runtime is at zero."""

    def __init__(
        self,
        kubernetes: KubectlKnativeClient,
        *,
        timeout_seconds: float = 900.0,
    ) -> None:
        self.kubernetes = kubernetes
        self.timeout_seconds = timeout_seconds

    async def repair(
        self,
        *,
        binding: RuntimeReloadBinding,
        target: RuntimeImageConfig,
        maintenance_epoch: str,
    ) -> RuntimeSessionPermissionRepairReceipt:
        if await self.kubernetes.kservice_exists(
            namespace=binding.namespace,
            name=binding.service_name,
        ):
            raise RuntimeError("Runtime KService must be absent before permission repair")
        pvc = await self.kubernetes.get_persistent_volume_claim(
            namespace=binding.namespace,
            name=binding.pvc_name,
        )
        observed_uid = str((pvc.get("metadata") or {}).get("uid") or "")
        if observed_uid != binding.pvc_uid:
            raise RuntimeError("Runtime permission repair PVC identity changed")

        manifests = {
            apply: build_session_permission_repair_job(
                binding=binding,
                target=target,
                maintenance_epoch=maintenance_epoch,
                apply=apply,
            )
            for apply in (False, True)
        }
        for manifest in manifests.values():
            await self.kubernetes.delete_job(
                namespace=binding.namespace,
                name=str(manifest["metadata"]["name"]),
            )
        await self._wait_pvc_zero(binding)

        dry_run = await self._run_job(binding=binding, manifest=manifests[False])
        applied = await self._run_job(binding=binding, manifest=manifests[True])
        return RuntimeSessionPermissionRepairReceipt(
            runtime_id=binding.runtime_id,
            binding_type=binding.binding_type,
            binding_id=binding.binding_id,
            pvc_uid=binding.pvc_uid,
            dry_run=dry_run,
            applied=applied,
        )

    async def _run_job(
        self,
        *,
        binding: RuntimeReloadBinding,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        name = str(manifest["metadata"]["name"])
        await self.kubernetes.apply(manifest)
        try:
            try:
                await self._wait_job(binding.namespace, name)
            except Exception as exc:
                failure = await self._failure_receipt(binding.namespace, name)
                error_type = str(failure.get("error_type") or type(exc).__name__)
                error_digest = hashlib.sha256(
                    str(failure.get("error") or "").encode("utf-8")
                ).hexdigest()[:12]
                raise RuntimeError(
                    "Runtime Session permission Job failed: "
                    f"error_type={error_type} error_digest={error_digest}"
                ) from exc
            logs = await self.kubernetes.get_job_logs(
                namespace=binding.namespace,
                name=name,
            )
            payload = self._parse_receipt(logs)
            if payload.get("ok") is not True:
                raise RuntimeError("Runtime Session permission Job returned a failure")
            return payload
        finally:
            await self.kubernetes.delete_job(
                namespace=binding.namespace,
                name=name,
            )
            await self._wait_pvc_zero(binding)

    async def _failure_receipt(self, namespace: str, name: str) -> dict[str, Any]:
        try:
            logs = await self.kubernetes.get_job_logs(
                namespace=namespace,
                name=name,
            )
            return self._parse_receipt(logs)
        except Exception as exc:
            return {"error_type": type(exc).__name__}

    async def _wait_job(self, namespace: str, name: str) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            job = await self.kubernetes.get_job(namespace=namespace, name=name)
            status = job.get("status") or {}
            if int(status.get("succeeded") or 0) == 1:
                return
            if int(status.get("failed") or 0) > 0:
                raise RuntimeError("Runtime Session permission Job failed")
            if time.monotonic() >= deadline:
                raise RuntimeError("Runtime Session permission Job timed out")
            await asyncio.sleep(1)

    async def _wait_pvc_zero(self, binding: RuntimeReloadBinding) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            pods = await self.kubernetes.list_persistent_volume_claim_pods(
                namespace=binding.namespace,
                pvc_name=binding.pvc_name,
            )
            if not pods:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Runtime PVC did not reach strict Pod zero")
            await asyncio.sleep(1)

    @staticmethod
    def _parse_receipt(logs: str) -> dict[str, Any]:
        for line in reversed(logs.splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        raise RuntimeError("Runtime Session permission Job receipt is unavailable")


__all__ = [
    "RuntimeSessionPermissionMaintenance",
    "RuntimeSessionPermissionRepairReceipt",
    "build_session_permission_repair_job",
]
