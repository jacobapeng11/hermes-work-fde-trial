from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.hermes_runtime.providers.knative import KnativeRuntimeProvider, KnativeRuntimeSpec
from app.models import ApplicationAgentRuntimeBinding, HermesRuntimeBinding
from app.services.runtime_bootstrap_intent import (
    RuntimeBootstrapAuthorization,
    RuntimeBootstrapIntentError,
    RuntimeBootstrapIntentService,
)
from app.services.runtime_platform_control import RuntimeBindingType


class RuntimeFreshInitError(RuntimeError):
    code = "RUNTIME_FRESH_INIT_FAILED"


class RuntimeFreshImageIdentityUnavailable(RuntimeFreshInitError):
    code = "FRESH_IMAGE_IDENTITY_UNAVAILABLE"


class RuntimeFreshInitAuthoringStore:
    """Authorize one fresh bootstrap from current Binding facts only."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def authorize(
        self,
        *,
        binding_type: RuntimeBindingType,
        binding_id: int,
        runtime_id: str,
        pvc_storage_identity_hash: str,
        target_image: str,
    ) -> RuntimeBootstrapAuthorization:
        model = (
            HermesRuntimeBinding
            if binding_type == "personal_runtime"
            else ApplicationAgentRuntimeBinding
        )
        try:
            async with self.session_factory() as db:
                binding = await db.scalar(
                    select(model).where(model.id == binding_id).with_for_update()
                )
                if binding is None:
                    raise RuntimeFreshInitError("fresh bootstrap Binding does not exist")
                if (
                    str(binding.runtime_id) != runtime_id
                    or str(binding.pvc_storage_identity_hash or "") != pvc_storage_identity_hash
                    or str(binding.image or "") != target_image
                ):
                    raise RuntimeFreshInitError("fresh bootstrap Binding identity changed")
                image_digest = str(binding.image_digest or "").strip()
                if not image_digest:
                    raise RuntimeFreshImageIdentityUnavailable(
                        "fresh bootstrap requires the published image digest"
                    )
                return await RuntimeBootstrapIntentService(db).authorize(
                    binding_type=binding_type,
                    binding_id=binding_id,
                    runtime_id=runtime_id,
                    pvc_storage_identity_hash=pvc_storage_identity_hash,
                    target_image=target_image,
                    target_image_digest=image_digest,
                )
        except RuntimeFreshInitError:
            raise
        except RuntimeBootstrapIntentError as exc:
            raise RuntimeFreshInitError(str(exc)) from exc

    async def mark_completed(self, *, intent_id: str) -> None:
        try:
            async with self.session_factory() as db:
                await RuntimeBootstrapIntentService(db).mark_completed(intent_id=intent_id)
        except RuntimeBootstrapIntentError as exc:
            raise RuntimeFreshInitError(str(exc)) from exc


class RuntimeFreshInitKubernetesClient(Protocol):
    async def apply(self, manifest: dict[str, Any]) -> None: ...

    async def get_kservice(self, *, namespace: str, name: str) -> dict[str, Any]: ...

    async def list_kservice_pods(
        self, *, namespace: str, service_name: str
    ) -> list[dict[str, Any]]: ...

    async def delete_kservice(self, *, namespace: str, name: str) -> None: ...


class RuntimeFreshInitProvisioner:
    """Converge write-free probe -> Backend authorization -> first bootstrap."""

    def __init__(
        self,
        *,
        kubernetes: RuntimeFreshInitKubernetesClient,
        provider: KnativeRuntimeProvider,
        authoring_store: RuntimeFreshInitAuthoringStore,
        timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 0.25,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.kubernetes = kubernetes
        self.provider = provider
        self.authoring_store = authoring_store
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.sleep = sleep

    async def prepare(
        self,
        spec: KnativeRuntimeSpec,
        ingress_base_url: str,
    ) -> KnativeRuntimeSpec:
        del ingress_base_url
        if spec.startup_mode in {"steady_state", "fresh_probe"}:
            return spec
        if not spec.pvc_storage_identity_hash or not spec.runtime_root_pvc_uid:
            raise RuntimeFreshInitError("fresh bootstrap PVC identity is unavailable")
        if not spec.initial_config_download_url or not spec.initial_config_digest:
            raise RuntimeFreshInitError("fresh bootstrap ConfigBundle is unavailable")
        binding_type: RuntimeBindingType = (
            "application_runtime" if spec.application_agent_id is not None else "personal_runtime"
        )
        try:
            binding_id = int(spec.runtime_binding_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeFreshInitError("fresh bootstrap Binding identity is invalid") from exc

        probe_spec = replace(
            spec,
            startup_mode="fresh_probe",
            initial_config_download_url=None,
            initial_config_digest=None,
            bootstrap_intent_id=None,
            bootstrap_authorization_digest=None,
            min_scale=1,
        )
        await self.kubernetes.apply(self.provider.build_kservice_manifest(probe_spec))
        await self._wait_ready(spec.namespace, spec.service_name)
        await self.kubernetes.delete_kservice(namespace=spec.namespace, name=spec.service_name)
        await self._wait_pods_zero(spec.namespace, spec.service_name)

        authorization = await self.authoring_store.authorize(
            binding_type=binding_type,
            binding_id=binding_id,
            runtime_id=spec.runtime_id,
            pvc_storage_identity_hash=spec.pvc_storage_identity_hash,
            target_image=spec.image,
        )
        return replace(
            spec,
            startup_mode="fresh_bootstrap",
            bootstrap_intent_id=authorization.intent_id,
            bootstrap_authorization_digest=authorization.authorization_digest,
            min_scale=1,
        )

    async def _wait_ready(self, namespace: str, name: str) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        while True:
            kservice = await self.kubernetes.get_kservice(namespace=namespace, name=name)
            conditions = (kservice.get("status") or {}).get("conditions") or []
            if any(
                item.get("type") == "Ready" and item.get("status") == "True"
                for item in conditions
                if isinstance(item, dict)
            ):
                return kservice
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeFreshInitError("fresh probe KService readiness timed out")
            await self.sleep(self.poll_interval_seconds)

    async def _wait_pods_zero(self, namespace: str, service_name: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        while True:
            pods = await self.kubernetes.list_kservice_pods(
                namespace=namespace,
                service_name=service_name,
            )
            live = [
                pod
                for pod in pods
                if not (pod.get("metadata") or {}).get("deletionTimestamp")
                and str((pod.get("status") or {}).get("phase") or "") not in {"Succeeded", "Failed"}
            ]
            if not live:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeFreshInitError("fresh probe Pods did not terminate")
            await self.sleep(self.poll_interval_seconds)


__all__ = [
    "RuntimeFreshImageIdentityUnavailable",
    "RuntimeFreshInitAuthoringStore",
    "RuntimeFreshInitError",
    "RuntimeFreshInitProvisioner",
]
