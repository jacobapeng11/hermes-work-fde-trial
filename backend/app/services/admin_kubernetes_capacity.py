from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

from app.schemas.admin_kubernetes import (
    AdminClusterCapacity,
    AdminClusterGroupCapacity,
    AdminNodeCapacity,
    AdminResourceAmount,
    AdminResourceRatios,
    AdminResourceUsage,
)

TERMINAL_PHASES = {"Succeeded", "Failed"}
GROUP_ORDER = (
    "application_runtime",
    "personal_runtime",
    "knative",
    "kubernetes_system",
    "other",
)


def _dig(value: Any, *path: str) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _split_quantity(value: Any, suffixes: tuple[str, ...]) -> tuple[Decimal | None, str]:
    if value is None:
        return None, ""
    text = str(value).strip()
    if not text:
        return None, ""
    for suffix in sorted(suffixes, key=len, reverse=True):
        if text.endswith(suffix):
            try:
                return Decimal(text[: -len(suffix)]), suffix
            except InvalidOperation:
                return None, ""
    try:
        return Decimal(text), ""
    except InvalidOperation:
        return None, ""


def _ceil_decimal_to_int(value: Decimal) -> int:
    if value <= 0:
        return 0
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def cpu_quantity_millicores(value: Any) -> int | None:
    amount, suffix = _split_quantity(value, ("n", "u", "m"))
    if amount is None:
        return None
    return _ceil_decimal_to_int(amount * {"n": Decimal("0.000001"), "u": Decimal("0.001"), "m": Decimal(1), "": Decimal(1000)}[suffix])


def memory_quantity_bytes(value: Any) -> int | None:
    amount, suffix = _split_quantity(value, ("Ki", "Mi", "Gi", "Ti", "K", "M", "G", "T"))
    if amount is None:
        return None
    return _ceil_decimal_to_int(amount * {"Ki": Decimal(1024), "Mi": Decimal(1024) ** 2, "Gi": Decimal(1024) ** 3, "Ti": Decimal(1024) ** 4, "K": Decimal(1000), "M": Decimal(1000) ** 2, "G": Decimal(1000) ** 3, "T": Decimal(1000) ** 4, "": Decimal(1)}[suffix])


def _amount(cpu: int = 0, memory: int = 0) -> AdminResourceAmount:
    return AdminResourceAmount(cpu_millicores=max(0, cpu), memory_bytes=max(0, memory))


def _add(left: AdminResourceAmount, right: AdminResourceAmount) -> AdminResourceAmount:
    return _amount(left.cpu_millicores + right.cpu_millicores, left.memory_bytes + right.memory_bytes)


def _usage_add(left: AdminResourceUsage, right: AdminResourceUsage) -> AdminResourceUsage:
    cpu = None if left.cpu_millicores is None or right.cpu_millicores is None else left.cpu_millicores + right.cpu_millicores
    memory = None if left.memory_bytes is None or right.memory_bytes is None else left.memory_bytes + right.memory_bytes
    return AdminResourceUsage(cpu_millicores=cpu, memory_bytes=memory)


def _ratio(value: int | None, denominator: int) -> float | None:
    if value is None or denominator <= 0:
        return None
    return round(value / denominator, 4)


def _ratios(value: AdminResourceAmount | AdminResourceUsage, denominator: AdminResourceAmount) -> AdminResourceRatios:
    return AdminResourceRatios(
        cpu=_ratio(value.cpu_millicores, denominator.cpu_millicores),
        memory=_ratio(value.memory_bytes, denominator.memory_bytes),
    )


def _resources(container: dict[str, Any]) -> tuple[AdminResourceAmount, AdminResourceAmount]:
    resources = container.get("resources") if isinstance(container, dict) else None
    if not isinstance(resources, dict):
        return _amount(), _amount()
    requests = resources.get("requests") if isinstance(resources.get("requests"), dict) else {}
    limits = resources.get("limits") if isinstance(resources.get("limits"), dict) else {}
    return (
        _amount(cpu_quantity_millicores(requests.get("cpu")) or 0, memory_quantity_bytes(requests.get("memory")) or 0),
        _amount(cpu_quantity_millicores(limits.get("cpu")) or 0, memory_quantity_bytes(limits.get("memory")) or 0),
    )


def pod_resource_requirements(pod: dict[str, Any]) -> tuple[AdminResourceAmount, AdminResourceAmount, bool]:
    """Return effective Pod requests/limits and whether requests are complete.

    Kubernetes uses the greater of summed app containers and each init container
    for each resource dimension. Pod overhead is added to the effective request.
    """
    containers = _dig(pod, "spec", "containers")
    containers = containers if isinstance(containers, list) else []
    init_containers = _dig(pod, "spec", "initContainers")
    init_containers = init_containers if isinstance(init_containers, list) else []
    app_requests = _amount()
    app_limits = _amount()
    request_complete = bool(containers)
    for container in containers:
        request, limit = _resources(container)
        app_requests = _add(app_requests, request)
        app_limits = _add(app_limits, limit)
        resource_block = container.get("resources") if isinstance(container, dict) else None
        request_complete = request_complete and isinstance(resource_block, dict) and isinstance(resource_block.get("requests"), dict)
    init_requests = _amount()
    init_limits = _amount()
    for container in init_containers:
        request, limit = _resources(container)
        init_requests = _amount(max(init_requests.cpu_millicores, request.cpu_millicores), max(init_requests.memory_bytes, request.memory_bytes))
        init_limits = _amount(max(init_limits.cpu_millicores, limit.cpu_millicores), max(init_limits.memory_bytes, limit.memory_bytes))
    overhead_request, _ = _resources({"resources": {"requests": _dig(pod, "spec", "overhead") or {}}})
    requests = _add(_amount(max(app_requests.cpu_millicores, init_requests.cpu_millicores), max(app_requests.memory_bytes, init_requests.memory_bytes)), overhead_request)
    limits = _amount(max(app_limits.cpu_millicores, init_limits.cpu_millicores), max(app_limits.memory_bytes, init_limits.memory_bytes))
    return requests, limits, request_complete


def classify_pod(pod: dict[str, Any]) -> str:
    namespace = str(_dig(pod, "metadata", "namespace") or "")
    labels = _dig(pod, "metadata", "labels")
    labels = labels if isinstance(labels, dict) else {}
    binding_type = str(labels.get("hermes.platform/runtime-binding-type") or "")
    if binding_type == "application_runtime" or labels.get("hermes.platform/application-agent-id"):
        return "application_runtime"
    if binding_type == "personal_runtime" or labels.get("hermes.platform/runtime-workspace-id"):
        return "personal_runtime"
    if namespace in {"knative-serving", "kourier-system"}:
        return "knative"
    if namespace == "kube-system" or namespace.startswith("kube-"):
        return "kubernetes_system"
    return "other"


def _metric_index(metrics: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in (metrics.get("items") if isinstance(metrics, dict) else []) or []:
        if not isinstance(item, dict):
            continue
        namespace = str(_dig(item, "metadata", "namespace") or "")
        name = str(_dig(item, "metadata", "name") or "")
        if name:
            result[(namespace, name)] = item
    return result


def _metric_usage(item: dict[str, Any] | None) -> AdminResourceUsage:
    if not item:
        return AdminResourceUsage()
    cpu = 0
    memory = 0
    cpu_seen = memory_seen = False
    for container in item.get("containers") or []:
        usage = container.get("usage") if isinstance(container, dict) else None
        if not isinstance(usage, dict):
            continue
        parsed_cpu = cpu_quantity_millicores(usage.get("cpu"))
        parsed_memory = memory_quantity_bytes(usage.get("memory"))
        if parsed_cpu is not None:
            cpu += parsed_cpu
            cpu_seen = True
        if parsed_memory is not None:
            memory += parsed_memory
            memory_seen = True
    return AdminResourceUsage(cpu_millicores=cpu if cpu_seen else None, memory_bytes=memory if memory_seen else None)


def _node_usage_index(metrics: dict[str, Any]) -> dict[str, AdminResourceUsage]:
    result: dict[str, AdminResourceUsage] = {}
    for item in (metrics.get("items") if isinstance(metrics, dict) else []) or []:
        if not isinstance(item, dict):
            continue
        name = str(_dig(item, "metadata", "name") or "")
        if name:
            usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
            result[name] = AdminResourceUsage(
                cpu_millicores=cpu_quantity_millicores(usage.get("cpu")),
                memory_bytes=memory_quantity_bytes(usage.get("memory")),
            )
    return result


def aggregate_cluster_capacity(
    *,
    nodes: dict[str, Any],
    pods: dict[str, Any],
    pod_metrics: dict[str, Any] | None = None,
    node_metrics: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
    pending_reasons: list[str] | None = None,
) -> AdminClusterCapacity:
    node_items = [item for item in (nodes.get("items") if isinstance(nodes, dict) else []) or [] if isinstance(item, dict)]
    pod_items = [item for item in (pods.get("items") if isinstance(pods, dict) else []) or [] if isinstance(item, dict)]
    active_pods = [item for item in pod_items if str(_dig(item, "status", "phase") or "Unknown") not in TERMINAL_PHASES]
    pod_index = _metric_index(pod_metrics or {})
    node_usage = _node_usage_index(node_metrics or {})
    pod_metric_items = [item for item in (pod_metrics.get("items") if isinstance(pod_metrics, dict) else []) or [] if isinstance(item, dict)]
    node_metric_items = [item for item in (node_metrics.get("items") if isinstance(node_metrics, dict) else []) or [] if isinstance(item, dict)]
    cluster_capacity = _amount()
    cluster_allocatable = _amount()
    cluster_requests = _amount()
    cluster_limits = _amount()
    cluster_usage = AdminResourceUsage(cpu_millicores=0, memory_bytes=0)
    group_state: dict[str, dict[str, Any]] = defaultdict(lambda: {"pod_count": 0, "running": 0, "pending": 0, "requests": _amount(), "limits": _amount(), "usage": AdminResourceUsage(cpu_millicores=0, memory_bytes=0), "missing_requests": 0, "missing_metrics": 0})
    node_requests: dict[str, AdminResourceAmount] = defaultdict(_amount)
    node_limits: dict[str, AdminResourceAmount] = defaultdict(_amount)
    for pod in active_pods:
        request, limit, complete = pod_resource_requirements(pod)
        cluster_requests = _add(cluster_requests, request)
        cluster_limits = _add(cluster_limits, limit)
        node_name = str(_dig(pod, "spec", "nodeName") or "")
        if node_name:
            node_requests[node_name] = _add(node_requests[node_name], request)
            node_limits[node_name] = _add(node_limits[node_name], limit)
        key = classify_pod(pod)
        state = group_state[key]
        state["pod_count"] += 1
        phase = str(_dig(pod, "status", "phase") or "Unknown")
        state["running"] += phase == "Running"
        state["pending"] += phase == "Pending"
        state["requests"] = _add(state["requests"], request)
        state["limits"] = _add(state["limits"], limit)
        if not complete:
            state["missing_requests"] += 1
        namespace = str(_dig(pod, "metadata", "namespace") or "")
        name = str(_dig(pod, "metadata", "name") or "")
        usage = _metric_usage(pod_index.get((namespace, name)))
        if usage.cpu_millicores is None or usage.memory_bytes is None:
            state["missing_metrics"] += 1
        state["usage"] = _usage_add(state["usage"], usage)
        cluster_usage = _usage_add(cluster_usage, usage)
    node_models: list[AdminNodeCapacity] = []
    for node in node_items:
        name = str(_dig(node, "metadata", "name") or "")
        capacity = _amount(cpu_quantity_millicores(_dig(node, "status", "capacity", "cpu")) or 0, memory_quantity_bytes(_dig(node, "status", "capacity", "memory")) or 0)
        allocatable = _amount(cpu_quantity_millicores(_dig(node, "status", "allocatable", "cpu")) or 0, memory_quantity_bytes(_dig(node, "status", "allocatable", "memory")) or 0)
        cluster_capacity = _add(cluster_capacity, capacity)
        cluster_allocatable = _add(cluster_allocatable, allocatable)
        usage = node_usage.get(name, AdminResourceUsage())
        conditions = _dig(node, "status", "conditions") or []
        pressure = [str(item.get("type")) for item in conditions if isinstance(item, dict) and item.get("type", "").endswith("Pressure") and item.get("status") == "True"]
        node_models.append(AdminNodeCapacity(name=name, capacity=capacity, allocatable=allocatable, requests=node_requests[name], limits=node_limits[name], actual_usage=usage, request_ratio=_ratios(node_requests[name], allocatable), usage_ratio=_ratios(usage, allocatable), headroom=_amount(max(0, allocatable.cpu_millicores - node_requests[name].cpu_millicores), max(0, allocatable.memory_bytes - node_requests[name].memory_bytes)), ready=any(isinstance(item, dict) and item.get("type") == "Ready" and item.get("status") == "True" for item in conditions), pressure_conditions=pressure))
    if node_usage:
        cluster_usage = AdminResourceUsage(cpu_millicores=sum(item.cpu_millicores or 0 for item in node_usage.values()) if any(item.cpu_millicores is not None for item in node_usage.values()) else None, memory_bytes=sum(item.memory_bytes or 0 for item in node_usage.values()) if any(item.memory_bytes is not None for item in node_usage.values()) else None)
    groups = [AdminClusterGroupCapacity(key=key, pod_count=group_state[key]["pod_count"], running_pod_count=group_state[key]["running"], pending_pod_count=group_state[key]["pending"], requests=group_state[key]["requests"], limits=group_state[key]["limits"], actual_usage=group_state[key]["usage"], missing_request_pod_count=group_state[key]["missing_requests"], missing_metrics_pod_count=group_state[key]["missing_metrics"]) for key in GROUP_ORDER if group_state[key]["pod_count"] or key != "other"]
    observed = observed_at or datetime.now(timezone.utc)
    pending_count = sum(str(_dig(item, "status", "phase") or "Unknown") == "Pending" for item in active_pods)
    return AdminClusterCapacity(capacity=cluster_capacity, allocatable=cluster_allocatable, requests=cluster_requests, limits=cluster_limits, actual_usage=cluster_usage, request_ratio=_ratios(cluster_requests, cluster_allocatable), usage_ratio=_ratios(cluster_usage, cluster_allocatable), headroom=_amount(max(0, cluster_allocatable.cpu_millicores - cluster_requests.cpu_millicores), max(0, cluster_allocatable.memory_bytes - cluster_requests.memory_bytes)), pending_pod_count=pending_count, pending_reasons=sorted(set(pending_reasons or [])), pressure_node_count=sum(bool(item.pressure_conditions) for item in node_models), nodes=node_models, groups=groups, observed_at=observed, metrics_coverage=(sum(1 for pod in active_pods if _metric_usage(pod_index.get((str(_dig(pod, "metadata", "namespace") or ""), str(_dig(pod, "metadata", "name") or "")))).cpu_millicores is not None and _metric_usage(pod_index.get((str(_dig(pod, "metadata", "namespace") or ""), str(_dig(pod, "metadata", "name") or "")))).memory_bytes is not None) / len(active_pods) if active_pods else 0.0), missing_metrics_pod_count=sum(state["missing_metrics"] for state in group_state.values()), metrics_stale=(bool(active_pods) and not pod_metric_items) or (bool(node_models) and not node_metric_items))
