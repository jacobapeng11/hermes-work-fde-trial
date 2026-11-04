from __future__ import annotations
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


RuntimeKubernetesStatus = Literal[
    "scaled_to_zero",
    "starting",
    "ready",
    "stopping",
    "degraded",
    "failed",
    "unknown",
]
RuntimeWakeBehavior = Literal["allowed", "blocked", "unknown"]


class AdminClusterSummary(BaseModel):
    node_count: int
    ready_node_count: int
    namespace_count: int
    running_pod_count: int
    ready_runtime_count: int
    scaled_to_zero_runtime_count: int
    starting_runtime_count: int
    warning_event_count: int
    knative_control_plane_ready: bool
    collected_at: datetime
    capacity: AdminClusterCapacity | None = None
    stale: bool = False
    error: str | None = None


class AdminResourceAmount(BaseModel):
    cpu_millicores: int = 0
    memory_bytes: int = 0


class AdminResourceUsage(BaseModel):
    cpu_millicores: int | None = None
    memory_bytes: int | None = None


class AdminResourceRatios(BaseModel):
    cpu: float | None = None
    memory: float | None = None


class AdminClusterGroupCapacity(BaseModel):
    key: str
    pod_count: int = 0
    running_pod_count: int = 0
    pending_pod_count: int = 0
    requests: AdminResourceAmount = Field(default_factory=AdminResourceAmount)
    limits: AdminResourceAmount = Field(default_factory=AdminResourceAmount)
    actual_usage: AdminResourceUsage = Field(default_factory=AdminResourceUsage)
    missing_request_pod_count: int = 0
    missing_metrics_pod_count: int = 0


class AdminNodeCapacity(BaseModel):
    name: str
    capacity: AdminResourceAmount
    allocatable: AdminResourceAmount
    requests: AdminResourceAmount = Field(default_factory=AdminResourceAmount)
    limits: AdminResourceAmount = Field(default_factory=AdminResourceAmount)
    actual_usage: AdminResourceUsage = Field(default_factory=AdminResourceUsage)
    request_ratio: AdminResourceRatios = Field(default_factory=AdminResourceRatios)
    usage_ratio: AdminResourceRatios = Field(default_factory=AdminResourceRatios)
    headroom: AdminResourceAmount = Field(default_factory=AdminResourceAmount)
    ready: bool = False
    pressure_conditions: list[str] = Field(default_factory=list)


class AdminClusterCapacity(BaseModel):
    capacity: AdminResourceAmount
    allocatable: AdminResourceAmount
    requests: AdminResourceAmount
    limits: AdminResourceAmount
    actual_usage: AdminResourceUsage
    request_ratio: AdminResourceRatios
    usage_ratio: AdminResourceRatios
    headroom: AdminResourceAmount
    pending_pod_count: int = 0
    pending_reasons: list[str] = Field(default_factory=list)
    pressure_node_count: int = 0
    nodes: list[AdminNodeCapacity] = Field(default_factory=list)
    groups: list[AdminClusterGroupCapacity] = Field(default_factory=list)
    observed_at: datetime
    metrics_coverage: float = 0.0
    missing_metrics_pod_count: int = 0
    metrics_stale: bool = False


class AdminKubernetesEventSummary(BaseModel):
    event_type: str
    reason: str
    message: str
    involved_kind: str
    involved_name: str
    occurred_at: datetime | None = None
    count: int = 1


class AdminContainerResourceSummary(BaseModel):
    name: str
    cpu_usage_millicores: int | None = None
    memory_usage_bytes: int | None = None


class AdminPodSummary(BaseModel):
    name: str
    phase: str
    ready: bool
    containers: list[str] = Field(default_factory=list)
    restart_count: int = 0
    node_name: str | None = None
    created_at: datetime | None = None
    deletion_timestamp: datetime | None = None
    waiting_reason: str | None = None
    terminated_reason: str | None = None
    image_names: list[str] = Field(default_factory=list)
    revision_name: str | None = None
    cpu_usage_millicores: int | None = None
    memory_usage_bytes: int | None = None
    resource_metrics_collected_at: datetime | None = None
    container_metrics: list[AdminContainerResourceSummary] = Field(
        default_factory=list
    )


class AdminRuntimeServiceSnapshot(BaseModel):
    role: Literal["runtime"]
    kservice_name: str
    namespace: str
    status: RuntimeKubernetesStatus
    ready: bool
    latest_created_revision: str | None = None
    latest_ready_revision: str | None = None
    traffic_revision: str | None = None
    desired_scale: int | None = None
    actual_scale: int | None = None
    active: bool | None = None
    scale_down_delay_seconds: int | None = None
    pods: list[AdminPodSummary] = Field(default_factory=list)


class AdminRuntimeKubernetesSnapshot(BaseModel):
    runtime_workspace_id: int
    resource_key: str
    runtime_kind: Literal["personal", "application"]
    application_agent_id: int | None = None
    collected_at: datetime
    aggregate_status: RuntimeKubernetesStatus
    wake_behavior: RuntimeWakeBehavior = "unknown"
    runtime_service: AdminRuntimeServiceSnapshot
    pods: list[AdminPodSummary] = Field(default_factory=list)
    recent_events: list[AdminKubernetesEventSummary] = Field(default_factory=list)
    state_mismatch: bool = False
    error: str | None = None


class AdminRuntimeHealthSnapshot(BaseModel):
    runtime_workspace_id: int
    runtime_kind: Literal["personal", "application"]
    runtime_id: str
    observed_at: datetime
    received_at: datetime
    stale: bool
    summary: dict[str, Any]


class AdminSafeKubernetesSummary(BaseModel):
    runtime_workspace_id: int
    resource_key: str
    runtime_kind: Literal["personal", "application"]
    application_agent_id: int | None = None
    collected_at: datetime
    objects: list[dict[str, Any]] = Field(default_factory=list)
