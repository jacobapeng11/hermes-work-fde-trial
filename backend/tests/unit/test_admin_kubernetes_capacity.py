from app.services.admin_kubernetes_capacity import aggregate_cluster_capacity, pod_resource_requirements


def node(name="node-a"):
    return {
        "metadata": {"name": name},
        "status": {
            "capacity": {"cpu": "4", "memory": "16Gi"},
            "allocatable": {"cpu": "3800m", "memory": "15Gi"},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def pod(name="app", namespace="hermes-prod", phase="Running"):
    labels = {"hermes.platform/application-agent-id": "1"} if namespace == "hermes-prod" else {"hermes.platform/runtime-workspace-id": "7"}
    return {
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "nodeName": "node-a",
            "containers": [{"name": "user-container", "resources": {"requests": {"cpu": "200m", "memory": "2Gi"}, "limits": {"cpu": "1", "memory": "4Gi"}}}],
            "initContainers": [{"name": "init", "resources": {"requests": {"cpu": "300m", "memory": "1Gi"}}}],
        },
        "status": {"phase": phase},
    }


def test_effective_requests_use_max_init_and_add_overhead():
    item = pod()
    item["spec"]["overhead"] = {"cpu": "10m", "memory": "16Mi"}
    requests, limits, complete = pod_resource_requirements(item)
    assert requests.cpu_millicores == 310
    assert requests.memory_bytes == 2 * 1024**3 + 16 * 1024**2
    assert limits.cpu_millicores == 1000
    assert complete is True


def test_cluster_capacity_groups_requests_actual_and_pending():
    payload = aggregate_cluster_capacity(
        nodes={"items": [node()]},
        pods={"items": [pod(), pod("pending", phase="Pending", namespace="hermes-runtimes")]},
        node_metrics={"items": [{"metadata": {"name": "node-a"}, "usage": {"cpu": "500m", "memory": "1Gi"}}]},
        pod_metrics={"items": [{"metadata": {"name": "app", "namespace": "hermes-prod"}, "containers": [{"usage": {"cpu": "100m", "memory": "256Mi"}}]}]},
    )
    assert payload.allocatable.cpu_millicores == 3800
    assert payload.requests.cpu_millicores == 600
    assert payload.pending_pod_count == 1
    assert payload.actual_usage.cpu_millicores == 500
    assert payload.metrics_coverage == 0.5
    assert payload.groups[0].key == "application_runtime"
    assert payload.groups[0].requests.cpu_millicores == 300
    assert payload.groups[0].missing_metrics_pod_count == 0
    assert payload.groups[1].key == "personal_runtime"
