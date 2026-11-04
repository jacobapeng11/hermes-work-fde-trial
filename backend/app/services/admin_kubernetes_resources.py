from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import AsyncIterator
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApplicationAgent,
    ApplicationAgentRuntimeBinding,
    HermesRuntimeBinding,
)
from app.schemas.admin_kubernetes import (
    AdminClusterSummary,
    AdminContainerResourceSummary,
    AdminKubernetesEventSummary,
    AdminPodSummary,
    AdminRuntimeKubernetesSnapshot,
    AdminRuntimeServiceSnapshot,
    AdminSafeKubernetesSummary,
    RuntimeKubernetesStatus,
)
from app.services.admin_auth import AdminPrincipal
from app.services.admin_kubernetes_capacity import (
    aggregate_cluster_capacity,
    cpu_quantity_millicores as _cpu_quantity_millicores,
    memory_quantity_bytes as _memory_quantity_bytes,
)
from app.services.admin_kubernetes_client import AdminKubernetesError, redact_kubernetes_text


KPA_RESOURCE = "podautoscalers.autoscaling.internal.knative.dev"
KSERVICE_RESOURCE = "services.serving.knative.dev"
POD_METRICS_RESOURCE = "pods.metrics.k8s.io"
RUNTIME_WORKSPACE_LABEL = "hermes.platform/runtime-workspace-id"
RUNTIME_ID_LABEL = "hermes.platform/runtime-id"
APPLICATION_AGENT_LABEL = "hermes.platform/application-agent-id"
REVISION_LABEL = "serving.knative.dev/revision"


class AdminKubernetesResourceError(RuntimeError):
    pass


class AdminKubernetesNotFoundError(AdminKubernetesResourceError):
    pass


class AdminKubernetesConflictError(AdminKubernetesResourceError):
    pass


@dataclass(frozen=True)
class PodResourceMetrics:
    cpu_usage_millicores: int | None
    memory_usage_bytes: int | None
    collected_at: datetime | None
    containers: tuple[AdminContainerResourceSummary, ...] = ()


@dataclass(frozen=True)
class AdminRuntimeKubernetesScope:
    org_id: int
    user_id: int
    runtime_workspace_id: int
    namespace: str
    kservice_name: str
    binding_status: str
    active_run_count: int
    runtime_kind: str = "personal"
    runtime_id: str = ""
    application_agent_id: int | None = None

    @property
    def resource_key(self) -> str:
        return f"{self.runtime_kind}:{self.runtime_workspace_id}"

    @property
    def selector_labels(self) -> dict[str, str]:
        if self.runtime_id:
            return {RUNTIME_ID_LABEL: self.runtime_id}
        labels = {
            RUNTIME_WORKSPACE_LABEL: str(self.runtime_workspace_id)
        }
        if self.application_agent_id is not None:
            labels[APPLICATION_AGENT_LABEL] = str(self.application_agent_id)
        return labels

class AdminKubernetesScopeStore(Protocol):
    async def get_runtime_scope(
        self,
        *,
        org_id: int,
        runtime_workspace_id: int,
        runtime_kind: str = "personal",
    ) -> AdminRuntimeKubernetesScope | None:
        ...

    async def list_runtime_scopes(
        self,
        *,
        org_id: int,
    ) -> list[AdminRuntimeKubernetesScope]:
        ...


class AdminKubernetesReader(Protocol):
    async def get_json(
        self,
        resource: str,
        *,
        namespace: str | None = None,
        name: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> dict:
        ...

    async def logs(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        tail_lines: int = 500,
        previous: bool = False,
    ) -> str:
        ...

    def stream_logs(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        tail_lines: int = 100,
        duration_seconds: int = 1800,
    ) -> AsyncIterator[str]:
        ...


class InMemoryAdminKubernetesScopeStore:
    def __init__(self, scopes: list[AdminRuntimeKubernetesScope] | None = None) -> None:
        self.scopes = {
            (item.org_id, item.runtime_kind, item.runtime_workspace_id): item
            for item in (scopes or [
                AdminRuntimeKubernetesScope(
                    org_id=1,
                    user_id=1,
                    runtime_workspace_id=1,
                    namespace="hermes-runtimes",
                    kservice_name="runtime-employee-7",
                    binding_status="ready",
                    active_run_count=0,
                )
            ])
        }

    async def get_runtime_scope(
        self,
        *,
        org_id: int,
        runtime_workspace_id: int,
        runtime_kind: str = "personal",
    ) -> AdminRuntimeKubernetesScope | None:
        return self.scopes.get((org_id, runtime_kind, runtime_workspace_id))

    async def list_runtime_scopes(
        self,
        *,
        org_id: int,
    ) -> list[AdminRuntimeKubernetesScope]:
        return [
            scope
            for (scope_org_id, _, _), scope in self.scopes.items()
            if scope_org_id == org_id
        ]


class SqlAlchemyAdminKubernetesScopeStore:
    def __init__(self, db: AsyncSession, *, runtime_namespace: str) -> None:
        self.db = db
        self.runtime_namespace = runtime_namespace

    async def get_runtime_scope(
        self,
        *,
        org_id: int,
        runtime_workspace_id: int,
        runtime_kind: str = "personal",
    ) -> AdminRuntimeKubernetesScope | None:
        if runtime_kind == "application":
            row = (
                await self.db.execute(
                    select(ApplicationAgentRuntimeBinding, ApplicationAgent)
                    .join(
                        ApplicationAgent,
                        ApplicationAgent.id
                        == ApplicationAgentRuntimeBinding.application_agent_id,
                    )
                    .where(
                        ApplicationAgentRuntimeBinding.org_id == org_id,
                        ApplicationAgentRuntimeBinding.application_agent_id
                        == runtime_workspace_id,
                        ApplicationAgentRuntimeBinding.k8s_namespace
                        == self.runtime_namespace,
                    )
                )
            ).first()
            if row is None:
                return None
            binding, agent = row
            return AdminRuntimeKubernetesScope(
                org_id=int(binding.org_id),
                user_id=int(agent.created_by_user_id),
                runtime_workspace_id=int(binding.application_agent_id),
                namespace=str(binding.k8s_namespace),
                kservice_name=str(binding.knative_service),
                binding_status=str(binding.status),
                active_run_count=int(binding.active_run_count),
                runtime_kind="application",
                runtime_id=str(binding.runtime_id),
                application_agent_id=int(binding.application_agent_id),
            )
        binding = (
            await self.db.execute(
                select(HermesRuntimeBinding).where(
                    HermesRuntimeBinding.org_id == org_id,
                    HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id,
                    HermesRuntimeBinding.k8s_namespace == self.runtime_namespace,
                )
            )
        ).scalar_one_or_none()
        if binding is None:
            return None
        return AdminRuntimeKubernetesScope(
            org_id=int(binding.org_id),
            user_id=int(binding.user_id),
            runtime_workspace_id=int(binding.runtime_workspace_id),
            namespace=str(binding.k8s_namespace),
            kservice_name=str(binding.knative_service),
            binding_status=str(binding.status),
            active_run_count=int(binding.active_run_count),
            runtime_kind="personal",
            runtime_id=str(binding.runtime_id),
        )

    async def list_runtime_scopes(
        self,
        *,
        org_id: int,
    ) -> list[AdminRuntimeKubernetesScope]:
        personal_bindings = (
            await self.db.execute(
                select(HermesRuntimeBinding)
                .where(
                    HermesRuntimeBinding.org_id == org_id,
                    HermesRuntimeBinding.k8s_namespace == self.runtime_namespace,
                )
                .order_by(HermesRuntimeBinding.runtime_workspace_id.asc())
            )
        ).scalars().all()
        scopes = [
            AdminRuntimeKubernetesScope(
                org_id=int(binding.org_id),
                user_id=int(binding.user_id),
                runtime_workspace_id=int(binding.runtime_workspace_id),
                namespace=str(binding.k8s_namespace),
                kservice_name=str(binding.knative_service),
                binding_status=str(binding.status),
                active_run_count=int(binding.active_run_count),
                runtime_kind="personal",
                runtime_id=str(binding.runtime_id),
            )
            for binding in personal_bindings
        ]
        application_bindings = (
            await self.db.execute(
                select(ApplicationAgentRuntimeBinding, ApplicationAgent)
                .join(
                    ApplicationAgent,
                    ApplicationAgent.id
                    == ApplicationAgentRuntimeBinding.application_agent_id,
                )
                .where(
                    ApplicationAgentRuntimeBinding.org_id == org_id,
                    ApplicationAgentRuntimeBinding.k8s_namespace
                    == self.runtime_namespace,
                )
                .order_by(
                    ApplicationAgentRuntimeBinding.application_agent_id.asc()
                )
            )
        ).all()
        scopes.extend(
            AdminRuntimeKubernetesScope(
                org_id=int(binding.org_id),
                user_id=int(agent.created_by_user_id),
                runtime_workspace_id=int(binding.application_agent_id),
                namespace=str(binding.k8s_namespace),
                kservice_name=str(binding.knative_service),
                binding_status=str(binding.status),
                active_run_count=int(binding.active_run_count),
                runtime_kind="application",
                runtime_id=str(binding.runtime_id),
                application_agent_id=int(binding.application_agent_id),
            )
            for binding, agent in application_bindings
        )
        return scopes


class InMemoryAdminKubernetesReader:
    @staticmethod
    def _kservice(name: str) -> dict:
        revision = f"{name}-00001"
        return {
            "metadata": {"name": name, "namespace": "hermes-runtimes"},
            "status": {
                "latestCreatedRevisionName": revision,
                "latestReadyRevisionName": revision,
                "traffic": [{"revisionName": revision, "percent": 100}],
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }

    async def get_json(
        self,
        resource: str,
        *,
        namespace: str | None = None,
        name: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> dict:
        del namespace
        if resource == KSERVICE_RESOURCE:
            if name:
                return self._kservice(name)
            return {"items": [self._kservice("runtime-employee-7")]}
        if resource == KPA_RESOURCE:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "runtime-employee-7-00001",
                            "namespace": "hermes-runtimes",
                            "annotations": {
                                "autoscaling.knative.dev/scale-down-delay": "900s"
                            },
                        },
                        "status": {
                            "desiredScale": 0,
                            "actualScale": 0,
                            "conditions": [{"type": "Active", "status": "False"}],
                        },
                    },
                ]
            }
        if resource == "nodes":
            return {
                "items": [{
                    "metadata": {"name": "contract-node"},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }]
            }
        if resource == "namespaces":
            return {"items": [{"metadata": {"name": "hermes-runtimes"}}]}
        if resource == "deployments":
            return {"items": [{"status": {"replicas": 1, "readyReplicas": 1}}]}
        return {"items": []}

    async def logs(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        tail_lines: int = 500,
        previous: bool = False,
    ) -> str:
        del namespace, tail_lines, previous
        return f"contract log from {pod}/{container}\n"

    async def stream_logs(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        tail_lines: int = 100,
        duration_seconds: int = 1800,
    ) -> AsyncIterator[str]:
        del namespace, tail_lines, duration_seconds
        yield f"contract log from {pod}/{container}"

    async def delete_pod(self, *, namespace: str, pod: str) -> None:
        del namespace, pod

    async def patch_annotation(
        self,
        *,
        resource: str,
        namespace: str,
        name: str,
        key: str,
        value: str,
    ) -> None:
        del resource, namespace, name, key, value


def redact_runtime_text(value: str | None, *, limit: int = 1000) -> str:
    return redact_kubernetes_text(value or "", limit=limit)


def runtime_wake_behavior(
    *,
    binding_status: str,
    aggregate_status: RuntimeKubernetesStatus,
) -> str:
    if binding_status in {
        "force_stopped",
        "force_stop_requested",
        "force_stop_failed",
        "restart_requested",
    }:
        return "blocked"
    if binding_status in {"scale_to_zero_failed", "starting", "failed"}:
        return "unknown"
    if aggregate_status in {"ready", "scaled_to_zero"}:
        return "allowed"
    return "unknown"


class AdminKubernetesResourceService:
    def __init__(
        self,
        *,
        scope_store: AdminKubernetesScopeStore,
        client: AdminKubernetesReader,
        cache_ttl_seconds: float = 2,
        max_concurrency: int = 8,
        runtime_namespace: str = "hermes-runtimes",
    ) -> None:
        self.scope_store = scope_store
        self.client = client
        self.cache_ttl_seconds = cache_ttl_seconds
        self.runtime_namespace = runtime_namespace
        self._cache: dict[
            tuple[int, str],
            tuple[float, AdminRuntimeKubernetesSnapshot],
        ] = {}
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def resolve_scope(
        self,
        principal: AdminPrincipal,
        runtime_workspace_id: int,
        *,
        runtime_kind: str = "personal",
    ) -> AdminRuntimeKubernetesScope:
        scope = await self.scope_store.get_runtime_scope(
            org_id=principal.org_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_kind=runtime_kind,
        )
        if scope is None or scope.org_id != principal.org_id:
            raise AdminKubernetesNotFoundError("runtime workspace not found")
        return scope

    async def cluster_summary(self, principal: AdminPrincipal) -> AdminClusterSummary:
        scopes = await self.scope_store.list_runtime_scopes(org_id=principal.org_id)
        async with self._semaphore:
            cluster_results = await asyncio.gather(
                self.client.get_json("nodes"),
                self.client.get_json("namespaces"),
                self.client.get_json("pods"),
                self.client.get_json(KPA_RESOURCE, namespace=self.runtime_namespace),
                self.client.get_json("events"),
                self.client.get_json(KSERVICE_RESOURCE, namespace=self.runtime_namespace),
                return_exceptions=True,
            )
            metric_results = await asyncio.gather(
                self.client.get_json("nodes.metrics.k8s.io"),
                self.client.get_json(POD_METRICS_RESOURCE),
                return_exceptions=True,
            )
            control_results = await asyncio.gather(
                self.client.get_json("deployments", namespace="knative-serving"),
                self.client.get_json("deployments", namespace="kourier-system"),
                return_exceptions=True,
            )
        nodes_result, namespaces_result, pods_result, kpas_result, events_result, services_result = cluster_results
        # Node and Pod lists are the minimum inputs for a truthful capacity
        # summary.  Keep the existing error boundary for these two rather than
        # returning an apparently empty cluster when RBAC/network access is
        # broken.  Other sources are diagnostic enrichments and may degrade.
        for resource, result in (("nodes", nodes_result), ("pods", pods_result)):
            if isinstance(result, Exception):
                raise AdminKubernetesError(
                    f"Kubernetes {resource} data is unavailable"
                ) from result
        degraded_sources = [
            resource
            for resource, result in (
                ("namespaces", namespaces_result),
                ("runtime autoscalers", kpas_result),
                ("events", events_result),
                ("runtime services", services_result),
            )
            if isinstance(result, Exception)
        ]
        nodes = nodes_result
        pods = pods_result
        namespaces = {} if isinstance(namespaces_result, Exception) else namespaces_result
        kpas = {} if isinstance(kpas_result, Exception) else kpas_result
        events = {} if isinstance(events_result, Exception) else events_result
        services = {} if isinstance(services_result, Exception) else services_result
        node_items = _items(nodes)
        pod_items = _items(pods)
        service_by_name = {
            str(_dig(item, "metadata", "name")): item
            for item in _items(services)
            if _dig(item, "metadata", "name")
        }
        runtime_revisions = {
            str(
                _dig(
                    service_by_name.get(scope.kservice_name, {}),
                    "status",
                    "latestCreatedRevisionName",
                )
            )
            for scope in scopes
            if _dig(
                service_by_name.get(scope.kservice_name, {}),
                "status",
                "latestCreatedRevisionName",
            )
        }
        kpa_items = [
            item
            for item in _items(kpas)
            if str(_dig(item, "metadata", "name") or "") in runtime_revisions
        ]
        ready_runtime_count = 0
        scaled_count = 0
        starting_count = 0
        for item in kpa_items:
            desired = _as_int(_dig(item, "status", "desiredScale"))
            actual = _as_int(_dig(item, "status", "actualScale"))
            if desired == 0 and actual == 0:
                scaled_count += 1
            elif desired is not None and desired > 0 and actual == desired:
                ready_runtime_count += 1
            elif desired is not None and desired > 0:
                starting_count += 1
        deployments_ready = all(
            not isinstance(result, Exception)
            and all(
                _as_int(_dig(item, "status", "readyReplicas"), default=0)
                >= _as_int(_dig(item, "status", "replicas"), default=0)
                for item in _items(result)
            )
            for result in control_results
        )
        node_metrics = metric_results[0] if isinstance(metric_results[0], dict) else None
        pod_metrics = metric_results[1] if isinstance(metric_results[1], dict) else None
        pending_reasons = [
            str(item.get("reason"))
            for item in _items(events)
            if item.get("reason")
            and str(_dig(item, "involvedObject", "kind") or "") == "Pod"
            and str(item.get("reason")) in {"FailedScheduling", "FailedMount", "FailedAttachVolume"}
        ]
        collected_at = datetime.now(timezone.utc)
        return AdminClusterSummary(
            node_count=len(node_items),
            ready_node_count=sum(_condition_true(item, "Ready") for item in node_items),
            namespace_count=len(_items(namespaces)),
            running_pod_count=sum(_dig(item, "status", "phase") == "Running" for item in pod_items),
            ready_runtime_count=ready_runtime_count,
            scaled_to_zero_runtime_count=scaled_count,
            starting_runtime_count=starting_count,
            warning_event_count=sum(item.get("type") == "Warning" for item in _items(events)),
            knative_control_plane_ready=deployments_ready,
            collected_at=collected_at,
            capacity=aggregate_cluster_capacity(
                nodes=nodes,
                pods=pods,
                pod_metrics=pod_metrics,
                node_metrics=node_metrics,
                observed_at=collected_at,
                pending_reasons=pending_reasons,
            ),
            stale=bool(degraded_sources)
            or any(isinstance(result, Exception) for result in metric_results),
            error=(
                "部分 Kubernetes 观测数据不可用：" + "、".join(degraded_sources)
                if degraded_sources
                else None
            ),
        )

    async def runtime_snapshot(
        self,
        principal: AdminPrincipal,
        runtime_workspace_id: int,
        *,
        runtime_kind: str = "personal",
        use_cache: bool = True,
    ) -> AdminRuntimeKubernetesSnapshot:
        scope = await self.resolve_scope(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
        )
        cache_key = (principal.org_id, scope.resource_key)
        cached = self._cache.get(cache_key)
        if use_cache and cached is not None and cached[0] > time.monotonic():
            return cached[1]
        labels = scope.selector_labels
        async with self._semaphore:
            (
                runtime_object,
                kpas,
                pods,
                events,
                metrics,
            ) = await asyncio.gather(
                self.client.get_json(
                    KSERVICE_RESOURCE,
                    namespace=scope.namespace,
                    name=scope.kservice_name,
                ),
                self.client.get_json(
                    KPA_RESOURCE,
                    namespace=scope.namespace,
                    labels=labels,
                ),
                self.client.get_json(
                    "pods",
                    namespace=scope.namespace,
                    labels=labels,
                ),
                self.client.get_json("events", namespace=scope.namespace),
                self._read_pod_metrics(
                    namespace=scope.namespace,
                    labels=labels,
                ),
            )
        snapshot = _build_runtime_snapshot(
            scope=scope,
            runtime_object=runtime_object,
            kpas=kpas,
            pods=pods,
            events=events,
            metrics_by_pod=_pod_metrics_by_name(metrics),
        )
        self._cache[cache_key] = (time.monotonic() + self.cache_ttl_seconds, snapshot)
        return snapshot

    async def _read_pod_metrics(
        self,
        *,
        namespace: str,
        labels: dict[str, str] | None = None,
    ) -> dict:
        try:
            return await self.client.get_json(
                POD_METRICS_RESOURCE,
                namespace=namespace,
                labels=labels,
            )
        except Exception:
            return {"items": []}

    async def runtime_snapshots(
        self,
        principal: AdminPrincipal,
    ) -> list[AdminRuntimeKubernetesSnapshot]:
        scopes = await self.scope_store.list_runtime_scopes(org_id=principal.org_id)
        if not scopes:
            return []
        async with self._semaphore:
            (
                services,
                kpas,
                pods,
                events,
                metrics,
            ) = await asyncio.gather(
                self.client.get_json(KSERVICE_RESOURCE, namespace=self.runtime_namespace),
                self.client.get_json(KPA_RESOURCE, namespace=self.runtime_namespace),
                self.client.get_json("pods", namespace=self.runtime_namespace),
                self.client.get_json("events", namespace=self.runtime_namespace),
                self._read_pod_metrics(namespace=self.runtime_namespace),
            )
        service_by_name = {
            str(_dig(item, "metadata", "name")): item
            for item in _items(services)
            if _dig(item, "metadata", "name")
        }
        all_kpas = _items(kpas)
        all_pods = _items(pods)
        all_events = _items(events)
        metrics_by_pod = _pod_metrics_by_name(metrics)
        snapshots: list[AdminRuntimeKubernetesSnapshot] = []
        for scope in scopes:
            runtime_object = service_by_name.get(scope.kservice_name, {})
            latest_created_revision = _string_or_none(
                _dig(runtime_object, "status", "latestCreatedRevisionName")
            )
            selector_labels = scope.selector_labels
            workspace_pods_payload = {
                "items": [
                    item
                    for item in all_pods
                    if _matches_labels(item, selector_labels)
                ]
            }
            workspace_kpas_payload = {
                "items": [
                    item
                    for item in all_kpas
                    if str(_dig(item, "metadata", "name") or "")
                    == str(latest_created_revision or "")
                ]
            }
            snapshot = _build_runtime_snapshot(
                scope=scope,
                runtime_object=runtime_object,
                kpas=workspace_kpas_payload,
                pods=workspace_pods_payload,
                events={"items": all_events},
                metrics_by_pod=metrics_by_pod,
            )
            self._cache[(principal.org_id, scope.resource_key)] = (
                time.monotonic() + self.cache_ttl_seconds,
                snapshot,
            )
            snapshots.append(snapshot)
        return snapshots

    async def personal_runtime_ready_user_ids(
        self,
        principal: AdminPrincipal,
    ) -> frozenset[int]:
        """Return users whose own personal Runtime is currently ready."""
        scopes = await self.scope_store.list_runtime_scopes(
            org_id=principal.org_id,
        )
        personal_users_by_resource_key = {
            scope.resource_key: scope.user_id
            for scope in scopes
            if scope.runtime_kind == "personal"
        }
        snapshots = await self.runtime_snapshots(principal)
        return frozenset(
            user_id
            for snapshot in snapshots
            if (
                snapshot.runtime_kind == "personal"
                and snapshot.aggregate_status == "ready"
                and (user_id := personal_users_by_resource_key.get(snapshot.resource_key))
                is not None
            )
        )

    async def runtime_events(
        self,
        principal: AdminPrincipal,
        runtime_workspace_id: int,
        *,
        runtime_kind: str = "personal",
    ) -> list[AdminKubernetesEventSummary]:
        snapshot = await self.runtime_snapshot(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
            use_cache=False,
        )
        return snapshot.recent_events

    async def safe_yaml_summary(
        self,
        principal: AdminPrincipal,
        runtime_workspace_id: int,
        *,
        runtime_kind: str = "personal",
    ) -> AdminSafeKubernetesSummary:
        scope = await self.resolve_scope(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
        )
        snapshot = await self.runtime_snapshot(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
        )
        objects: list[dict[str, Any]] = []
        service = snapshot.runtime_service
        objects.append({
            "apiVersion": "serving.knative.dev/v1",
            "kind": "Service",
            "metadata": {
                "name": service.kservice_name,
                "namespace": service.namespace,
            },
            "status": {
                "ready": service.ready,
                "latestCreatedRevision": service.latest_created_revision,
                "latestReadyRevision": service.latest_ready_revision,
                "trafficRevision": service.traffic_revision,
            },
        })
        if service.latest_created_revision:
            objects.append({
                "apiVersion": "serving.knative.dev/v1",
                "kind": "Revision",
                "metadata": {
                    "name": service.latest_created_revision,
                    "namespace": service.namespace,
                },
                "status": {
                    "ready": (
                        service.latest_ready_revision
                        == service.latest_created_revision
                    ),
                },
            })
            objects.append({
                "apiVersion": "autoscaling.internal.knative.dev/v1alpha1",
                "kind": "PodAutoscaler",
                "metadata": {
                    "name": service.latest_created_revision,
                    "namespace": service.namespace,
                },
                "status": {
                    "desiredScale": service.desired_scale,
                    "actualScale": service.actual_scale,
                    "active": service.active,
                    "scaleDownDelaySeconds": service.scale_down_delay_seconds,
                },
            })
        for pod in snapshot.pods:
            objects.append({
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": pod.name, "namespace": scope.namespace},
                "status": {
                    "phase": pod.phase,
                    "ready": pod.ready,
                    "restartCount": pod.restart_count,
                    "containers": list(pod.containers),
                },
            })
        return AdminSafeKubernetesSummary(
            runtime_workspace_id=runtime_workspace_id,
            resource_key=scope.resource_key,
            runtime_kind=scope.runtime_kind,
            application_agent_id=scope.application_agent_id,
            collected_at=datetime.now(timezone.utc),
            objects=objects,
        )

    async def pod_logs(
        self,
        principal: AdminPrincipal,
        runtime_workspace_id: int,
        *,
        runtime_kind: str = "personal",
        pod_name: str,
        container: str,
        tail_lines: int,
        previous: bool,
    ) -> str:
        scope, _ = await self.validate_pod_container(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
            pod_name=pod_name,
            container=container,
        )
        content = await self.client.logs(
            namespace=scope.namespace,
            pod=pod_name,
            container=container,
            tail_lines=tail_lines,
            previous=previous,
        )
        return redact_runtime_text(content, limit=1024 * 1024)

    async def stream_pod_logs(
        self,
        principal: AdminPrincipal,
        runtime_workspace_id: int,
        *,
        runtime_kind: str = "personal",
        pod_name: str,
        container: str,
        tail_lines: int,
        duration_seconds: int = 1800,
    ) -> AsyncIterator[str]:
        scope, _ = await self.validate_pod_container(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
            pod_name=pod_name,
            container=container,
        )
        async for line in self.client.stream_logs(
            namespace=scope.namespace,
            pod=pod_name,
            container=container,
            tail_lines=tail_lines,
            duration_seconds=duration_seconds,
        ):
            yield redact_runtime_text(line, limit=16 * 1024)

    async def validate_pod_container(
        self,
        principal: AdminPrincipal,
        runtime_workspace_id: int,
        *,
        runtime_kind: str = "personal",
        pod_name: str,
        container: str,
    ) -> tuple[AdminRuntimeKubernetesScope, AdminPodSummary]:
        scope = await self.resolve_scope(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
        )
        snapshot = await self.runtime_snapshot(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
        )
        pod = next((item for item in snapshot.pods if item.name == pod_name), None)
        if pod is None:
            raise AdminKubernetesNotFoundError("runtime pod not found")
        if container not in pod.containers:
            raise AdminKubernetesNotFoundError("runtime pod container not found")
        return scope, pod


def _service_snapshot(
    service: dict,
    kpa_by_name: dict[str, dict],
    pods: list[AdminPodSummary],
) -> AdminRuntimeServiceSnapshot:
    name = str(_dig(service, "metadata", "name") or "")
    namespace = str(_dig(service, "metadata", "namespace") or "")
    latest_created = _string_or_none(_dig(service, "status", "latestCreatedRevisionName"))
    latest_ready = _string_or_none(_dig(service, "status", "latestReadyRevisionName"))
    traffic = _dig(service, "status", "traffic")
    traffic_revision = None
    if isinstance(traffic, list):
        for item in traffic:
            if isinstance(item, dict) and item.get("revisionName"):
                traffic_revision = str(item["revisionName"])
                break
    service_pods = [pod for pod in pods if pod.revision_name == latest_created]
    kpa = kpa_by_name.get(latest_created or "", {})
    desired = _as_int(_dig(kpa, "status", "desiredScale"))
    actual = _as_int(_dig(kpa, "status", "actualScale"))
    active_condition = _condition_status(kpa, "Active")
    active = None if active_condition is None else active_condition == "True"
    delay = _duration_seconds(
        _dig(kpa, "metadata", "annotations", "autoscaling.knative.dev/scale-down-delay")
    )
    ready = _condition_true(service, "Ready")
    status = _service_status(
        ready=ready,
        desired=desired,
        actual=actual,
        pods=service_pods,
    )
    return AdminRuntimeServiceSnapshot(
        role="runtime",
        kservice_name=name,
        namespace=namespace,
        status=status,
        ready=ready,
        latest_created_revision=latest_created,
        latest_ready_revision=latest_ready,
        traffic_revision=traffic_revision,
        desired_scale=desired,
        actual_scale=actual,
        active=active,
        scale_down_delay_seconds=delay,
        pods=service_pods,
    )


def _build_runtime_snapshot(
    *,
    scope: AdminRuntimeKubernetesScope,
    runtime_object: dict,
    kpas: dict,
    pods: dict,
    events: dict,
    metrics_by_pod: dict[str, PodResourceMetrics] | None = None,
) -> AdminRuntimeKubernetesSnapshot:
    pod_summaries = [
        _pod_summary(item, metrics_by_pod=metrics_by_pod or {})
        for item in _items(pods)
    ]
    kpa_by_name = {
        str(_dig(item, "metadata", "name")): item
        for item in _items(kpas)
        if _dig(item, "metadata", "name")
    }
    runtime_service = _service_snapshot(runtime_object, kpa_by_name, pod_summaries)
    aggregate_status = runtime_service.status
    allowed_names = {
        scope.kservice_name,
        *(pod.name for pod in pod_summaries),
        *(
            [runtime_service.latest_created_revision]
            if runtime_service.latest_created_revision
            else []
        ),
    }
    event_summaries = [
        _event_summary(item)
        for item in _items(events)
        if str(_dig(item, "involvedObject", "name") or "") in allowed_names
    ]
    event_summaries.sort(
        key=lambda item: item.occurred_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    database_is_stopped = scope.binding_status in {
        "force_stopped",
        "force_stop_requested",
        "force_stop_failed",
        "restart_requested",
    }
    state_mismatch = (
        database_is_stopped
        and runtime_service.status in {"ready", "starting", "stopping"}
    )
    return AdminRuntimeKubernetesSnapshot(
        runtime_workspace_id=scope.runtime_workspace_id,
        resource_key=scope.resource_key,
        runtime_kind=scope.runtime_kind,
        application_agent_id=scope.application_agent_id,
        collected_at=datetime.now(timezone.utc),
        aggregate_status=aggregate_status,
        wake_behavior=runtime_wake_behavior(
            binding_status=scope.binding_status,
            aggregate_status=aggregate_status,
        ),
        runtime_service=runtime_service,
        pods=pod_summaries,
        recent_events=event_summaries[:100],
        state_mismatch=state_mismatch,
    )
def _service_status(
    *,
    ready: bool,
    desired: int | None,
    actual: int | None,
    pods: list[AdminPodSummary],
) -> RuntimeKubernetesStatus:
    running_pods = [pod for pod in pods if pod.phase == "Running"]
    ready_pods = [pod for pod in running_pods if pod.ready]
    if any(
        pod.waiting_reason
        in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "Error"}
        for pod in pods
    ):
        return "failed"
    if desired == 0 and actual == 0 and not running_pods:
        return "scaled_to_zero"
    if desired == 0 and pods:
        return "stopping"
    if (
        ready
        and desired is not None
        and desired > 0
        and desired == actual
        and ready_pods
    ):
        return "ready"
    if any(pod.phase == "Failed" for pod in pods):
        return "failed"
    if desired is not None and desired > 0 and not ready_pods:
        return "starting"
    if pods or not ready:
        return "degraded"
    return "unknown"


def _pod_summary(
    item: dict,
    *,
    metrics_by_pod: dict[str, PodResourceMetrics] | None = None,
) -> AdminPodSummary:
    name = str(_dig(item, "metadata", "name") or "")
    metrics = (metrics_by_pod or {}).get(name)
    statuses = _dig(item, "status", "containerStatuses")
    statuses = statuses if isinstance(statuses, list) else []
    spec_containers = _dig(item, "spec", "containers")
    spec_containers = spec_containers if isinstance(spec_containers, list) else []
    waiting_reason = None
    terminated_reason = None
    for status in statuses:
        state = status.get("state") if isinstance(status, dict) else None
        if not isinstance(state, dict):
            continue
        waiting = state.get("waiting")
        terminated = state.get("terminated")
        if isinstance(waiting, dict) and waiting.get("reason"):
            waiting_reason = str(waiting["reason"])
        if isinstance(terminated, dict) and terminated.get("reason"):
            terminated_reason = str(terminated["reason"])
    return AdminPodSummary(
        name=name,
        phase=str(_dig(item, "status", "phase") or "Unknown"),
        ready=bool(statuses) and all(bool(status.get("ready")) for status in statuses),
        containers=[
            str(container.get("name"))
            for container in spec_containers
            if isinstance(container, dict) and container.get("name")
        ],
        restart_count=sum(_as_int(status.get("restartCount"), default=0) for status in statuses),
        node_name=_string_or_none(_dig(item, "spec", "nodeName")),
        created_at=_datetime_or_none(_dig(item, "metadata", "creationTimestamp")),
        deletion_timestamp=_datetime_or_none(_dig(item, "metadata", "deletionTimestamp")),
        waiting_reason=waiting_reason,
        terminated_reason=terminated_reason,
        image_names=[
            str(container.get("image"))
            for container in spec_containers
            if isinstance(container, dict) and container.get("image")
        ],
        revision_name=_string_or_none(_dig(item, "metadata", "labels", REVISION_LABEL)),
        cpu_usage_millicores=metrics.cpu_usage_millicores if metrics else None,
        memory_usage_bytes=metrics.memory_usage_bytes if metrics else None,
        resource_metrics_collected_at=metrics.collected_at if metrics else None,
        container_metrics=list(metrics.containers) if metrics else [],
    )


def _pod_metrics_by_name(payload: dict) -> dict[str, PodResourceMetrics]:
    metrics: dict[str, PodResourceMetrics] = {}
    for item in _items(payload):
        name = _string_or_none(_dig(item, "metadata", "name"))
        if name is None:
            continue
        cpu_values: list[int] = []
        memory_values: list[int] = []
        container_values: list[AdminContainerResourceSummary] = []
        containers = item.get("containers")
        if isinstance(containers, list):
            for container in containers:
                if not isinstance(container, dict):
                    continue
                usage = container.get("usage")
                if not isinstance(usage, dict):
                    continue
                cpu = _cpu_quantity_millicores(usage.get("cpu"))
                memory = _memory_quantity_bytes(usage.get("memory"))
                if cpu is not None:
                    cpu_values.append(cpu)
                if memory is not None:
                    memory_values.append(memory)
                container_name = _string_or_none(container.get("name"))
                if container_name is not None:
                    container_values.append(
                        AdminContainerResourceSummary(
                            name=container_name,
                            cpu_usage_millicores=cpu,
                            memory_usage_bytes=memory,
                        )
                    )
        metrics[name] = PodResourceMetrics(
            cpu_usage_millicores=sum(cpu_values) if cpu_values else None,
            memory_usage_bytes=sum(memory_values) if memory_values else None,
            collected_at=_datetime_or_none(item.get("timestamp")),
            containers=tuple(container_values),
        )
    return metrics


def _event_summary(item: dict) -> AdminKubernetesEventSummary:
    return AdminKubernetesEventSummary(
        event_type=str(item.get("type") or "Normal"),
        reason=str(item.get("reason") or "Unknown"),
        message=redact_runtime_text(str(item.get("message") or ""), limit=1000),
        involved_kind=str(_dig(item, "involvedObject", "kind") or "Unknown"),
        involved_name=str(_dig(item, "involvedObject", "name") or "Unknown"),
        occurred_at=_datetime_or_none(
            item.get("eventTime")
            or item.get("lastTimestamp")
            or _dig(item, "metadata", "creationTimestamp")
        ),
        count=_as_int(item.get("count"), default=1),
    )


def _items(payload: Any) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return []
    return [item for item in payload["items"] if isinstance(item, dict)]


def _matches_labels(item: dict, expected: dict[str, str]) -> bool:
    labels = _dig(item, "metadata", "labels")
    if not isinstance(labels, dict):
        return False
    return all(
        str(labels.get(key) or "") == value
        for key, value in expected.items()
    )


def _dig(value: Any, *path: str) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _condition_status(value: dict, condition_type: str) -> str | None:
    conditions = _dig(value, "status", "conditions")
    if not isinstance(conditions, list):
        return None
    for condition in conditions:
        if isinstance(condition, dict) and condition.get("type") == condition_type:
            return _string_or_none(condition.get("status"))
    return None


def _condition_true(value: dict, condition_type: str) -> bool:
    return _condition_status(value, condition_type) == "True"


def _as_int(value: Any, *, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _string_or_none(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _datetime_or_none(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = __import__("re").fullmatch(r"(\d+)([smh])", value)
    if not match:
        return None
    amount = int(match.group(1))
    return amount * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
