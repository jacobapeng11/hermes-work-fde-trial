from __future__ import annotations

from copy import deepcopy

import pytest

from app.services.admin_auth import AdminPrincipal
from app.services.admin_kubernetes_resources import (
    AdminKubernetesNotFoundError,
    AdminKubernetesResourceService,
    AdminRuntimeKubernetesScope,
    InMemoryAdminKubernetesScopeStore,
    _cpu_quantity_millicores,
    _memory_quantity_bytes,
    redact_runtime_text,
)


def admin(*, org_id: int = 1) -> AdminPrincipal:
    return AdminPrincipal(org_id=org_id, user_id=9, roles=("admin",))


def scope(*, org_id: int = 1, active_run_count: int = 0) -> AdminRuntimeKubernetesScope:
    return AdminRuntimeKubernetesScope(
        org_id=org_id,
        user_id=11,
        runtime_workspace_id=7,
        namespace="hermes-runtimes",
        kservice_name="runtime-employee-7",
        binding_status="ready",
        active_run_count=active_run_count,
    )


def kservice(name: str, revision: str, *, ready: bool = True) -> dict:
    return {
        "metadata": {"name": name, "namespace": "hermes-runtimes"},
        "status": {
            "latestCreatedRevisionName": revision,
            "latestReadyRevisionName": revision if ready else None,
            "traffic": [{"revisionName": revision, "percent": 100}],
            "conditions": [{"type": "Ready", "status": "True" if ready else "Unknown"}],
        },
    }


def kpa(revision: str, desired: int, actual: int, *, active: str = "True") -> dict:
    return {
        "metadata": {
            "name": revision,
            "namespace": "hermes-runtimes",
            "annotations": {"autoscaling.knative.dev/scale-down-delay": "900s"},
        },
        "status": {
            "desiredScale": desired,
            "actualScale": actual,
            "conditions": [{"type": "Active", "status": active}],
        },
    }


def pod(
    name: str,
    revision: str,
    *,
    ready: bool = True,
    phase: str = "Running",
    runtime_id: str | None = None,
) -> dict:
    labels = {
        "serving.knative.dev/revision": revision,
        "hermes.platform/runtime-workspace-id": "7",
    }
    if runtime_id:
        labels["hermes.platform/runtime-id"] = runtime_id
    return {
        "metadata": {
            "name": name,
            "namespace": "hermes-runtimes",
            "labels": labels,
            "creationTimestamp": "2026-07-18T00:00:00Z",
        },
        "spec": {
            "nodeName": "node-a",
            "containers": [
                {"name": "user-container", "image": "registry/runtime:v1"},
                {"name": "queue-proxy", "image": "registry/queue-proxy:v1"},
            ],
        },
        "status": {
            "phase": phase,
            "containerStatuses": [
                {
                    "name": "user-container",
                    "ready": ready,
                    "restartCount": 2,
                    "image": "registry/runtime:v1",
                    "state": {},
                },
                {
                    "name": "queue-proxy",
                    "ready": ready,
                    "restartCount": 0,
                    "image": "registry/queue-proxy:v1",
                    "state": {},
                },
            ],
        },
    }


def pod_metric(name: str, *, cpu: str = "125m", memory: str = "256Mi") -> dict:
    return {
        "metadata": {
            "name": name,
            "namespace": "hermes-runtimes",
            "creationTimestamp": "2026-07-18T00:03:00Z",
        },
        "timestamp": "2026-07-18T00:03:00Z",
        "containers": [
            {"name": "user-container", "usage": {"cpu": cpu, "memory": memory}},
            {"name": "queue-proxy", "usage": {"cpu": "25m", "memory": "64Mi"}},
        ],
    }


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.label_calls: list[tuple[str, dict[str, str] | None]] = []
        self.objects = {
            ("services.serving.knative.dev", "runtime-employee-7"): kservice(
                "runtime-employee-7", "runtime-employee-7-00002"
            ),
        }
        self.lists = {
            "podautoscalers.autoscaling.internal.knative.dev": [
                kpa("runtime-employee-7-00002", 1, 1),
            ],
            "pods": [
                pod("runtime-pod", "runtime-employee-7-00002"),
            ],
            "pods.metrics.k8s.io": [
                pod_metric("runtime-pod", cpu="125m", memory="256Mi"),
            ],
            "events": [{
                "metadata": {"name": "warning.1", "creationTimestamp": "2026-07-18T00:01:00Z"},
                "type": "Warning",
                "reason": "BackOff",
                "message": "Authorization: Bearer super-secret /root/private/data",
                "involvedObject": {"kind": "Pod", "name": "runtime-pod"},
                "lastTimestamp": "2026-07-18T00:02:00Z",
            }],
        }

    async def get_json(self, resource, *, namespace=None, name=None, labels=None):
        del namespace
        self.calls.append((resource, name))
        self.label_calls.append((resource, labels))
        if name is not None:
            return deepcopy(self.objects[(resource, name)])
        if resource == "services.serving.knative.dev":
            return {
                "items": deepcopy(
                    [
                        value
                        for (kind, _name), value in self.objects.items()
                        if kind == resource
                    ]
                )
            }
        return {"items": deepcopy(self.lists.get(resource, []))}


class MetricsUnavailableClient(FakeClient):
    async def get_json(self, resource, *, namespace=None, name=None, labels=None):
        if resource == "pods.metrics.k8s.io":
            raise RuntimeError("metrics unavailable")
        return await super().get_json(resource, namespace=namespace, name=name, labels=labels)


class OptionalClusterDataUnavailableClient(FakeClient):
    async def get_json(self, resource, *, namespace=None, name=None, labels=None):
        if resource in {
            "events",
            "services.serving.knative.dev",
            "podautoscalers.autoscaling.internal.knative.dev",
        } and name is None:
            raise RuntimeError(f"{resource} forbidden")
        return await super().get_json(resource, namespace=namespace, name=name, labels=labels)


def test_redact_runtime_text_removes_token_and_host_path() -> None:
    result = redact_runtime_text("Authorization: Bearer secret /root/private/data")
    assert "secret" not in result
    assert "/root/private/data" not in result


def test_kubernetes_resource_quantity_parsers() -> None:
    assert _cpu_quantity_millicores("250m") == 250
    assert _cpu_quantity_millicores("1") == 1000
    assert _cpu_quantity_millicores("1500000n") == 2
    assert _cpu_quantity_millicores("1500u") == 2
    assert _memory_quantity_bytes("256Mi") == 256 * 1024 * 1024
    assert _memory_quantity_bytes("1Gi") == 1024 * 1024 * 1024
    assert _memory_quantity_bytes("1G") == 1000 * 1000 * 1000


@pytest.mark.asyncio
async def test_snapshot_rejects_workspace_from_other_org() -> None:
    service = AdminKubernetesResourceService(
        scope_store=InMemoryAdminKubernetesScopeStore([scope(org_id=2)]),
        client=FakeClient(),
    )

    with pytest.raises(AdminKubernetesNotFoundError):
        await service.runtime_snapshot(admin(org_id=1), 7)


@pytest.mark.asyncio
async def test_snapshot_maps_runtime_pod_autoscaler_and_events() -> None:
    client = FakeClient()
    service = AdminKubernetesResourceService(
        scope_store=InMemoryAdminKubernetesScopeStore([scope()]),
        client=client,
    )

    result = await service.runtime_snapshot(admin(), 7)

    assert result.aggregate_status == "ready"
    assert result.runtime_service.role == "runtime"
    assert result.runtime_service.kservice_name == "runtime-employee-7"
    assert result.runtime_service.desired_scale == 1
    assert result.runtime_service.actual_scale == 1
    assert [item.name for item in result.pods] == ["runtime-pod"]
    assert result.pods[0].containers == ["user-container", "queue-proxy"]
    assert result.pods[0].cpu_usage_millicores == 150
    assert result.pods[0].memory_usage_bytes == 320 * 1024 * 1024
    assert result.pods[0].resource_metrics_collected_at is not None
    assert [
        item.name for item in result.pods[0].container_metrics
    ] == ["user-container", "queue-proxy"]
    assert result.pods[0].container_metrics[0].cpu_usage_millicores == 125
    assert (
        result.pods[0].container_metrics[1].memory_usage_bytes
        == 64 * 1024 * 1024
    )
    assert result.runtime_service.pods[0].cpu_usage_millicores == 150
    assert (
        "pods.metrics.k8s.io",
        {"hermes.platform/runtime-workspace-id": "7"},
    ) in client.label_calls
    assert result.recent_events[0].reason == "BackOff"
    assert "super-secret" not in result.recent_events[0].message
    assert "/root/private/data" not in result.recent_events[0].message


@pytest.mark.asyncio
async def test_snapshot_keeps_status_when_metrics_are_unavailable() -> None:
    service = AdminKubernetesResourceService(
        scope_store=InMemoryAdminKubernetesScopeStore([scope()]),
        client=MetricsUnavailableClient(),
    )

    result = await service.runtime_snapshot(admin(), 7)

    assert result.aggregate_status == "ready"
    assert result.pods[0].name == "runtime-pod"
    assert result.pods[0].cpu_usage_millicores is None
    assert result.pods[0].memory_usage_bytes is None
    assert result.pods[0].resource_metrics_collected_at is None


@pytest.mark.asyncio
async def test_ready_snapshot_ignores_terminal_failed_pod_from_current_revision() -> None:
    client = FakeClient()
    client.lists["pods"].append(
        pod(
            "evicted-runtime-pod",
            "runtime-employee-7-00002",
            ready=False,
            phase="Failed",
        )
    )
    service = AdminKubernetesResourceService(
        scope_store=InMemoryAdminKubernetesScopeStore([scope()]),
        client=client,
    )

    result = await service.runtime_snapshot(admin(), 7)

    assert result.aggregate_status == "ready"
    assert {item.phase for item in result.pods} == {"Running", "Failed"}


@pytest.mark.asyncio
async def test_snapshot_is_failed_when_only_current_revision_pod_failed() -> None:
    client = FakeClient()
    client.lists["pods"] = [
        pod(
            "failed-runtime-pod",
            "runtime-employee-7-00002",
            ready=False,
            phase="Failed",
        )
    ]
    service = AdminKubernetesResourceService(
        scope_store=InMemoryAdminKubernetesScopeStore([scope()]),
        client=client,
    )

    result = await service.runtime_snapshot(admin(), 7)

    assert result.aggregate_status == "failed"


@pytest.mark.asyncio
async def test_scaled_to_zero_runtime_is_healthy_and_wakeable() -> None:
    client = FakeClient()
    client.lists["podautoscalers.autoscaling.internal.knative.dev"] = [
        kpa("runtime-employee-7-00002", 0, 0, active="False"),
    ]
    client.lists["pods"] = []
    service = AdminKubernetesResourceService(
        scope_store=InMemoryAdminKubernetesScopeStore([scope()]),
        client=client,
    )

    result = await service.runtime_snapshot(admin(), 7)

    assert result.aggregate_status == "scaled_to_zero"
    assert result.wake_behavior == "allowed"
    assert result.state_mismatch is False


@pytest.mark.asyncio
async def test_safe_yaml_summary_is_allowlist_only() -> None:
    client = FakeClient()
    client.objects[("services.serving.knative.dev", "runtime-employee-7")]["spec"] = {
        "template": {
            "spec": {
                "containers": [{
                    "name": "user-container",
                    "env": [{"name": "SECRET", "value": "bad"}],
                }],
                "volumes": [{"name": "runtime", "hostPath": {"path": "/root/runtime"}}],
            }
        }
    }
    service = AdminKubernetesResourceService(
        scope_store=InMemoryAdminKubernetesScopeStore([scope()]),
        client=client,
    )

    summary = await service.safe_yaml_summary(admin(), 7)
    serialized = str(summary).lower()

    assert "runtime-employee-7" in serialized
    assert "secret" not in serialized
    assert "hostpath" not in serialized
    assert "/root/runtime" not in serialized
    assert "endpointslice" not in serialized


@pytest.mark.asyncio
async def test_runtime_list_uses_one_namespace_batch_per_resource_type() -> None:
    client = FakeClient()
    service = AdminKubernetesResourceService(
        scope_store=InMemoryAdminKubernetesScopeStore([scope()]),
        client=client,
    )

    snapshots = await service.runtime_snapshots(admin())

    assert [item.runtime_workspace_id for item in snapshots] == [7]
    assert snapshots[0].aggregate_status == "ready"
    assert client.calls == [
        ("services.serving.knative.dev", None),
        ("podautoscalers.autoscaling.internal.knative.dev", None),
        ("pods", None),
        ("events", None),
        ("pods.metrics.k8s.io", None),
    ]


@pytest.mark.asyncio
async def test_personal_and_application_runtime_ids_do_not_collide() -> None:
    personal_scope = scope()
    personal_scope = AdminRuntimeKubernetesScope(
        **{
            **personal_scope.__dict__,
            "runtime_id": "personal-runtime-7",
        }
    )
    application_scope = AdminRuntimeKubernetesScope(
        org_id=1,
        user_id=12,
        runtime_workspace_id=7,
        namespace="hermes-runtimes",
        kservice_name="application-agent-7",
        binding_status="ready",
        active_run_count=1,
        runtime_kind="application",
        runtime_id="application-agent-runtime-7",
        application_agent_id=7,
    )
    client = FakeClient()
    client.objects[
        ("services.serving.knative.dev", "application-agent-7")
    ] = kservice("application-agent-7", "application-agent-7-00001")
    client.lists["podautoscalers.autoscaling.internal.knative.dev"].append(
        kpa("application-agent-7-00001", 1, 1)
    )
    client.lists["pods"] = [
        pod(
            "personal-pod",
            "runtime-employee-7-00002",
            runtime_id="personal-runtime-7",
        ),
        pod(
            "application-pod",
            "application-agent-7-00001",
            runtime_id="application-agent-runtime-7",
        ),
    ]
    service = AdminKubernetesResourceService(
        scope_store=InMemoryAdminKubernetesScopeStore(
            [personal_scope, application_scope]
        ),
        client=client,
    )

    snapshots = await service.runtime_snapshots(admin())

    assert [item.resource_key for item in snapshots] == [
        "personal:7",
        "application:7",
    ]
    assert [item.name for item in snapshots[0].pods] == ["personal-pod"]
    assert [item.name for item in snapshots[1].pods] == ["application-pod"]
    assert snapshots[1].application_agent_id == 7


@pytest.mark.asyncio
async def test_cluster_summary_counts_each_runtime_kpa_once() -> None:
    service = AdminKubernetesResourceService(
        scope_store=InMemoryAdminKubernetesScopeStore([scope()]),
        client=FakeClient(),
    )

    summary = await service.cluster_summary(admin())

    assert summary.ready_runtime_count == 1


@pytest.mark.asyncio
async def test_cluster_summary_degrades_optional_sources_without_returning_502_shape() -> None:
    service = AdminKubernetesResourceService(
        scope_store=InMemoryAdminKubernetesScopeStore([scope()]),
        client=OptionalClusterDataUnavailableClient(),
    )

    summary = await service.cluster_summary(admin())

    assert summary.node_count == 0
    assert summary.capacity is not None
    assert summary.stale is True
    assert "events" in (summary.error or "")
