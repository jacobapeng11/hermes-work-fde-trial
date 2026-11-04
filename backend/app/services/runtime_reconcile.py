from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any, Awaitable, Callable, Protocol

from app.hermes_runtime.providers.knative import KnativeRuntimeSpec
from app.services.runtime_activity import RuntimeActivitySnapshot
from app.services.runtime_admission import (
    RuntimeAdmissionError,
    RuntimeAdmissionService,
)
from app.services.runtime_config_bootstrap import RuntimeConfigPreflightError
from app.services.runtime_platform_control import (
    RuntimePlatformControlKeyMaterial,
)
from app.services.runtime_workspace import (
    RuntimeBindingEnsureResult,
    RuntimeBindingReadyService,
    RuntimeBindingStatusPatch,
    RuntimeWorkspaceRecord,
)


class ReconcileAdmissionService(Protocol):
    async def begin_reconcile(
        self,
        runtime_workspace_id: int,
        target_status: str,
        **kwargs: object,
    ) -> bool: ...

    async def mark_failed(self, runtime_workspace_id: int, message: str) -> bool | None: ...


class ReconcileActivityStore(Protocol):
    async def get_snapshot(
        self,
        runtime_workspace_id: int,
    ) -> RuntimeActivitySnapshot: ...


class ReconcileKubernetesClient(Protocol):
    async def kservice_exists(
        self,
        *,
        namespace: str,
        name: str,
    ) -> bool: ...

    async def delete_kservice(
        self,
        *,
        namespace: str,
        name: str,
    ) -> None: ...

    async def list_kservice_pods(
        self,
        *,
        namespace: str,
        service_name: str,
    ) -> list[dict[str, Any]]: ...


class ReconcileReadyService(Protocol):
    async def ensure_kservice_ready(
        self,
        *,
        spec: KnativeRuntimeSpec,
        api_server_key: str,
        platform_control_key: RuntimePlatformControlKeyMaterial,
        ingress_base_url: str,
    ) -> RuntimeBindingStatusPatch: ...

    async def activate_matching_kservice(
        self,
        *,
        spec: KnativeRuntimeSpec,
        api_server_key: str,
        ingress_base_url: str,
    ) -> RuntimeBindingStatusPatch | None: ...

    async def has_matching_zero_scale_kservice(
        self,
        *,
        spec: KnativeRuntimeSpec,
    ) -> bool: ...


class ReconcileDirectoryProvisioner(Protocol):
    async def ensure_directory(self, spec: KnativeRuntimeSpec) -> None: ...


class ReconcileConfigPreparer(Protocol):
    async def prepare(
        self,
        spec: KnativeRuntimeSpec,
    ) -> KnativeRuntimeSpec: ...


class RuntimeDrainTimeout(RuntimeError):
    pass


class RuntimeTopologyReconciler:
    """Own the slow, no-overlap replacement of one Runtime KService."""

    def __init__(
        self,
        *,
        admission_service: ReconcileAdmissionService,
        activity_store: ReconcileActivityStore,
        k8s_client: ReconcileKubernetesClient,
        ready_service: ReconcileReadyService,
        directory_provisioner: ReconcileDirectoryProvisioner,
        config_preparer: ReconcileConfigPreparer,
        namespace: str,
        service_account: str,
        ingress_base_url: str,
        runtime_node_name: str,
        drain_timeout_seconds: float,
        poll_interval_seconds: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        platform_api_base_url: str | None = None,
        enterprise_knowledge_host_path: str | None = None,
    ) -> None:
        if drain_timeout_seconds <= 0:
            raise ValueError("drain_timeout_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.admission_service = admission_service
        self.activity_store = activity_store
        self.k8s_client = k8s_client
        self.ready_service = ready_service
        self.directory_provisioner = directory_provisioner
        self.config_preparer = config_preparer
        self.namespace = namespace
        self.service_account = service_account
        self.ingress_base_url = ingress_base_url
        self.runtime_node_name = runtime_node_name
        self.drain_timeout_seconds = float(drain_timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.monotonic = monotonic
        self.sleep = sleep
        self.platform_api_base_url = platform_api_base_url
        self.enterprise_knowledge_host_path = enterprise_knowledge_host_path

    async def kservice_exists(self, service_name: str) -> bool:
        return await self.k8s_client.kservice_exists(
            namespace=self.namespace,
            name=service_name,
        )

    async def reconcile(
        self,
        record: RuntimeWorkspaceRecord,
        binding: RuntimeBindingEnsureResult,
    ) -> RuntimeWorkspaceRecord:
        if not binding.reconcile_required:
            return replace(record, runtime_status="ready", pending_reload=False)

        target_image = getattr(binding.plan, "image", "")
        if not target_image:
            raise RuntimeConfigPreflightError(
                "Runtime reconcile plan is missing the authoritative image"
            )
        if binding.platform_control_key is None:
            raise RuntimeConfigPreflightError(
                "Runtime reconcile plan is missing its binding-scoped control key"
            )
        owns_transition = binding.created or binding.reconcile_owned
        if not owns_transition:
            if binding.binding_status != "failed":
                RuntimeAdmissionService.require_ready(binding.binding_status)
            owns_transition = await self.admission_service.begin_reconcile(
                record.runtime_workspace_id,
                "starting",
                target_image=target_image,
            )
            if not owns_transition:
                raise RuntimeAdmissionError.reconcile_in_progress()

        deadline = self.monotonic() + self.drain_timeout_seconds
        try:
            await self._wait_for_activity_drain(
                record.runtime_workspace_id,
                deadline=deadline,
            )
            spec = RuntimeBindingReadyService.build_knative_spec(
                plan=binding.plan,
                namespace=self.namespace,
                image=target_image,
                service_account=self.service_account,
                startup_mode=(
                    "fresh_bootstrap"
                    if binding.created
                    or binding.initial_bootstrap_resume_eligible
                    or not binding.plan.runtime_root_pvc_uid
                    else "steady_state"
                ),
                runtime_node_name=self.runtime_node_name,
                default_model_entry_id=record.default_model_id,
                platform_api_base_url=self.platform_api_base_url,
                enterprise_knowledge_host_path=self.enterprise_knowledge_host_path,
            )
            precreated_candidate = (
                not binding.created
                and not binding.reconcile_owned
                and binding.binding_status == "ready"
            )
            zero_scale_candidate = precreated_candidate and (
                await self.ready_service.has_matching_zero_scale_kservice(spec=spec)
            )
            if zero_scale_candidate:
                spec = await self.config_preparer.prepare(spec)
                patch = await self.ready_service.activate_matching_kservice(
                    spec=spec,
                    api_server_key=binding.api_server_key,
                    ingress_base_url=self.ingress_base_url,
                )
                if patch is not None:
                    return replace(
                        record,
                        runtime_status=patch.status,
                        pending_reload=patch.status != "ready",
                    )
            if not binding.created and not binding.reconcile_owned:
                await self.k8s_client.delete_kservice(
                    namespace=self.namespace,
                    name=binding.plan.service_name,
                )
                await self._wait_for_old_pods(
                    binding.plan.service_name,
                    deadline=deadline,
                )
            await self.directory_provisioner.ensure_directory(spec)
            if not zero_scale_candidate:
                spec = await self.config_preparer.prepare(spec)
            patch = await self.ready_service.ensure_kservice_ready(
                spec=spec,
                api_server_key=binding.api_server_key,
                platform_control_key=binding.platform_control_key,
                ingress_base_url=self.ingress_base_url,
            )
        except RuntimeDrainTimeout as exc:
            await self.admission_service.mark_failed(
                record.runtime_workspace_id,
                str(exc),
            )
            return replace(record, runtime_status="failed", pending_reload=True)
        except Exception:
            await self.admission_service.mark_failed(
                record.runtime_workspace_id,
                "runtime topology reconcile failed",
            )
            return replace(record, runtime_status="failed", pending_reload=True)

        return replace(
            record,
            runtime_status=patch.status,
            pending_reload=patch.status != "ready",
        )

    async def reload_runtime(
        self,
        record: RuntimeWorkspaceRecord,
        binding: RuntimeBindingEnsureResult,
    ) -> RuntimeWorkspaceRecord:
        """Hard replace one isolated Binding from current Backend facts."""

        if not binding.reconcile_owned or binding.binding_status != "updating":
            raise RuntimeConfigPreflightError("Runtime reload ownership is unavailable")
        if binding.platform_control_key is None:
            raise RuntimeConfigPreflightError("Runtime reload control key is unavailable")
        if not binding.plan.runtime_root_pvc_uid or not binding.plan.pvc_storage_identity_hash:
            raise RuntimeConfigPreflightError("Runtime reload PVC identity is incomplete")
        deadline = self.monotonic() + self.drain_timeout_seconds
        try:
            if await self.k8s_client.kservice_exists(
                namespace=self.namespace,
                name=binding.plan.service_name,
            ):
                await self.k8s_client.delete_kservice(
                    namespace=self.namespace,
                    name=binding.plan.service_name,
                )
                await self._wait_for_old_pods(
                    binding.plan.service_name,
                    deadline=deadline,
                )
            spec = RuntimeBindingReadyService.build_knative_spec(
                plan=binding.plan,
                namespace=self.namespace,
                image=binding.plan.image,
                service_account=self.service_account,
                startup_mode="steady_state",
                runtime_node_name=self.runtime_node_name,
                default_model_entry_id=record.default_model_id,
                platform_api_base_url=self.platform_api_base_url,
                enterprise_knowledge_host_path=self.enterprise_knowledge_host_path,
            )
            await self.directory_provisioner.ensure_directory(spec)
            spec = await self.config_preparer.prepare(spec)
            patch = await self.ready_service.ensure_kservice_ready(
                spec=spec,
                api_server_key=binding.api_server_key,
                platform_control_key=binding.platform_control_key,
                ingress_base_url=self.ingress_base_url,
            )
        except Exception:
            await self.admission_service.mark_failed(
                record.runtime_workspace_id,
                "runtime code reload failed",
            )
            raise
        if patch.status != "ready":
            raise RuntimeConfigPreflightError(
                patch.last_error or "Runtime reload did not become ready"
            )
        return replace(record, runtime_status="ready", pending_reload=False)

    async def precreate_zero_scale_runtime(
        self,
        record: RuntimeWorkspaceRecord,
        binding: RuntimeBindingEnsureResult,
    ) -> RuntimeWorkspaceRecord:
        """Replace topology to a Ready zero-scale Revision without activating it."""

        if not binding.reconcile_owned or binding.binding_status != "updating":
            raise RuntimeConfigPreflightError("Runtime precreation ownership is unavailable")
        if binding.platform_control_key is None:
            raise RuntimeConfigPreflightError("Runtime precreation control key is unavailable")
        if not binding.plan.runtime_root_pvc_uid or not binding.plan.pvc_storage_identity_hash:
            raise RuntimeConfigPreflightError("Runtime precreation PVC identity is incomplete")
        deadline = self.monotonic() + self.drain_timeout_seconds
        try:
            if await self.k8s_client.kservice_exists(
                namespace=self.namespace,
                name=binding.plan.service_name,
            ):
                if await self.k8s_client.list_kservice_pods(
                    namespace=self.namespace,
                    service_name=binding.plan.service_name,
                ):
                    raise RuntimeConfigPreflightError(
                        "zero-scale precreation requires an empty Pod set"
                    )
                await self.k8s_client.delete_kservice(
                    namespace=self.namespace,
                    name=binding.plan.service_name,
                )
                await self._wait_for_old_pods(
                    binding.plan.service_name,
                    deadline=deadline,
                )
            spec = RuntimeBindingReadyService.build_knative_spec(
                plan=binding.plan,
                namespace=self.namespace,
                image=binding.plan.image,
                service_account=self.service_account,
                startup_mode="steady_state",
                runtime_node_name=self.runtime_node_name,
                default_model_entry_id=record.default_model_id,
                platform_api_base_url=self.platform_api_base_url,
                enterprise_knowledge_host_path=self.enterprise_knowledge_host_path,
            )
            await self.directory_provisioner.ensure_directory(spec)
            spec = await self.config_preparer.prepare(spec)
            patch = await self.ready_service.ensure_kservice_ready(
                spec=spec,
                api_server_key=binding.api_server_key,
                platform_control_key=binding.platform_control_key,
                ingress_base_url=self.ingress_base_url,
                zero_initial_scale=True,
            )
        except Exception:
            await self.admission_service.mark_failed(
                record.runtime_workspace_id,
                "runtime zero-scale precreation failed",
            )
            raise
        if patch.status != "ready":
            raise RuntimeConfigPreflightError(
                patch.last_error or "Runtime zero-scale precreation did not become ready"
            )
        return replace(record, runtime_status="ready", pending_reload=True)

    async def _wait_for_activity_drain(
        self,
        runtime_workspace_id: int,
        *,
        deadline: float,
    ) -> None:
        while True:
            snapshot = await self.activity_store.get_snapshot(runtime_workspace_id)
            if snapshot.idle:
                return
            await self._sleep_or_timeout(
                deadline,
                "runtime activity did not drain before deadline",
            )

    async def _wait_for_old_pods(
        self,
        service_name: str,
        *,
        deadline: float,
    ) -> None:
        while True:
            pods = await self.k8s_client.list_kservice_pods(
                namespace=self.namespace,
                service_name=service_name,
            )
            if not pods:
                return
            await self._sleep_or_timeout(
                deadline,
                "previous Runtime Pods did not terminate before deadline",
            )

    async def _sleep_or_timeout(self, deadline: float, message: str) -> None:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise RuntimeDrainTimeout(message)
        await self.sleep(min(self.poll_interval_seconds, remaining))
