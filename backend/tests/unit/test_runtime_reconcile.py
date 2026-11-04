from __future__ import annotations

from dataclasses import replace

import pytest

from app.services.runtime_activity import RuntimeActivitySnapshot
from app.services.runtime_admission import RuntimeAdmissionError
from app.services.runtime_reconcile import RuntimeTopologyReconciler
from app.services.runtime_platform_control import (
    RuntimePlatformControlKeyMaterial,
)
from app.services.runtime_workspace import (
    RuntimeBindingEnsureResult,
    RuntimeBindingStatusPatch,
    RuntimeWorkspaceRecord,
    RuntimeWorkspaceService,
)


def workspace() -> RuntimeWorkspaceRecord:
    return RuntimeWorkspaceRecord(
        runtime_workspace_id=7,
        cloud_profile_id=7,
        org_id=1,
        user_id=2,
        user_name="User",
        user_code="10002",
        name="Workspace",
        status="active",
        runtime_status="ready",
    )


def binding(
    *,
    created: bool = False,
    status: str | None = None,
    reconcile_owned: bool = False,
) -> RuntimeBindingEnsureResult:
    return RuntimeBindingEnsureResult(
        plan=replace(
            RuntimeWorkspaceService.build_default_binding_plan(
                workspace(),
                runtime_root_path="/tmp/hermes-runtimes",
            ),
            image=("kind.local/hermes-agent:v0.19.0-f4df260f2-expert-kanban-guard"),
            binding_id="7",
            platform_control_secret_name=("runtime-10002-platform-control"),
            platform_control_key_version=1,
        ),
        api_server_key="runtime-key",
        created=created,
        reconcile_owned=reconcile_owned,
        reconcile_required=True,
        binding_status=status or ("starting" if created else "ready"),
        platform_control_key=RuntimePlatformControlKeyMaterial(
            binding_type="personal_runtime",
            binding_id=7,
            runtime_id="rt-7",
            key_version=1,
            root_key=b"k" * 32,
        ),
    )


class FakeAdmission:
    def __init__(self, events: list[str], *, acquired: bool = True) -> None:
        self.events = events
        self.acquired = acquired
        self.failures: list[str] = []

    async def begin_reconcile(self, workspace_id, target_status, **kwargs):
        assert workspace_id == 7
        assert kwargs == {
            "target_image": "kind.local/hermes-agent:v0.19.0-f4df260f2-expert-kanban-guard",
        }
        self.events.append(f"admission:{target_status}")
        return self.acquired

    async def mark_failed(self, workspace_id: int, message: str) -> None:
        assert workspace_id == 7
        self.failures.append(message)
        self.events.append("binding:failed")


class FakeActivity:
    def __init__(self, events: list[str], idle_values: list[bool]) -> None:
        self.events = events
        self.idle_values = list(idle_values)

    async def get_snapshot(self, runtime_workspace_id: int) -> RuntimeActivitySnapshot:
        assert runtime_workspace_id == 7
        idle = self.idle_values.pop(0) if self.idle_values else False
        self.events.append("activity:idle" if idle else "activity:busy")
        return RuntimeActivitySnapshot(
            run_count=0 if idle else 1,
            self_evolution_count=0,
        )


class FakeKubernetes:
    def __init__(self, events: list[str], pod_counts: list[int]) -> None:
        self.events = events
        self.pod_counts = list(pod_counts)

    async def kservice_exists(self, *, namespace: str, name: str) -> bool:
        assert namespace == "hermes-runtimes"
        assert name == "runtime-10002"
        return True

    async def delete_kservice(self, *, namespace: str, name: str) -> None:
        assert namespace == "hermes-runtimes"
        assert name == "runtime-10002"
        self.events.append("kservice:delete")

    async def list_kservice_pods(self, *, namespace: str, service_name: str):
        assert namespace == "hermes-runtimes"
        assert service_name == "runtime-10002"
        count = self.pod_counts.pop(0) if self.pod_counts else 1
        self.events.append(f"old-pods:{count}")
        return [{"metadata": {"name": f"old-{index}"}} for index in range(count)]


class FakeProvisioner:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def ensure_directory(self, _spec) -> None:
        self.events.append("directory:ensure")


class FakeConfigPreparer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def prepare(self, spec):
        self.events.append("config:bundle")
        return spec


class FakeReadyService:
    def __init__(self, events: list[str], *, forbidden: bool = False) -> None:
        self.events = events
        self.forbidden = forbidden

    async def has_matching_zero_scale_kservice(self, *, spec):
        return False

    async def ensure_kservice_ready(
        self,
        *,
        spec,
        api_server_key,
        platform_control_key,
        ingress_base_url,
    ):
        if self.forbidden:
            raise AssertionError("new Revision must not be applied before old Pods are zero")
        assert api_server_key == "runtime-key"
        assert ingress_base_url == "http://127.0.0.1:30364"
        assert platform_control_key.key_version == 1
        self.events.append("kservice:apply")
        return RuntimeBindingStatusPatch(
            runtime_workspace_id=7,
            knative_status_url="http://runtime-10002.hermes.local",
            route_host="runtime-10002.hermes.local",
            endpoint_base_url="http://127.0.0.1:30364",
            endpoint_host_header="runtime-10002.hermes.local",
            status="ready",
        )


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += seconds


def reconciler(
    events: list[str],
    *,
    activity: list[bool],
    pods: list[int],
    acquired: bool = True,
    ready_forbidden: bool = False,
    timeout: float = 10.0,
) -> tuple[RuntimeTopologyReconciler, FakeAdmission]:
    clock = Clock()
    admission = FakeAdmission(events, acquired=acquired)
    return (
        RuntimeTopologyReconciler(
            admission_service=admission,
            activity_store=FakeActivity(events, activity),
            k8s_client=FakeKubernetes(events, pods),
            ready_service=FakeReadyService(events, forbidden=ready_forbidden),
            directory_provisioner=FakeProvisioner(events),
            config_preparer=FakeConfigPreparer(events),
            namespace="hermes-runtimes",
            service_account="hermes-runtime-workload",
            ingress_base_url="http://127.0.0.1:30364",
            runtime_node_name="demo",
            drain_timeout_seconds=timeout,
            poll_interval_seconds=1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            enterprise_knowledge_host_path="",
        ),
        admission,
    )


async def test_reconcile_drains_activity_and_old_revision_before_apply() -> None:
    events: list[str] = []
    service, admission = reconciler(
        events,
        activity=[False, True],
        pods=[1, 0],
    )

    result = await service.reconcile(workspace(), binding())

    assert result.runtime_status == "ready"
    assert result.pending_reload is False
    assert admission.failures == []
    assert events == [
        "admission:starting",
        "activity:busy",
        "activity:idle",
        "kservice:delete",
        "old-pods:1",
        "old-pods:0",
        "directory:ensure",
        "config:bundle",
        "kservice:apply",
    ]


async def test_reload_hard_stops_without_activity_drain_and_reprojects_steady_state() -> None:
    events: list[str] = []
    service, admission = reconciler(events, activity=[], pods=[1, 0])
    owned = binding(reconcile_owned=True, status="updating")
    owned = replace(
        owned,
        plan=replace(
            owned.plan,
            runtime_root_pvc_uid="pvc-uid-7",
            pvc_storage_identity_hash="sha256:" + "a" * 64,
        ),
    )

    result = await service.reload_runtime(workspace(), owned)

    assert result.runtime_status == "ready"
    assert admission.failures == []
    assert events == [
        "kservice:delete",
        "old-pods:1",
        "old-pods:0",
        "directory:ensure",
        "config:bundle",
        "kservice:apply",
    ]


async def test_new_binding_is_already_owned_by_insert_transaction() -> None:
    events: list[str] = []
    service, _admission = reconciler(events, activity=[True], pods=[])

    result = await service.reconcile(
        replace(workspace(), runtime_status="starting"),
        binding(created=True),
    )

    assert result.runtime_status == "ready"
    assert all(not event.startswith("admission:") for event in events)


async def test_fresh_bootstrap_prepares_current_config_bundle_reference() -> None:
    events: list[str] = []
    service, _admission = reconciler(events, activity=[True], pods=[])
    target = binding(created=True)
    target = replace(
        target,
        plan=replace(target.plan, state_schema=25, config_schema=34),
    )

    result = await service.reconcile(
        replace(workspace(), runtime_status="starting"),
        target,
    )

    assert result.runtime_status == "ready"
    assert "config:bundle" in events


async def test_resumed_initial_binding_preserves_partial_topology() -> None:
    events: list[str] = []
    service, admission = reconciler(events, activity=[True], pods=[])

    result = await service.reconcile(
        replace(workspace(), runtime_status="starting"),
        binding(status="starting", reconcile_owned=True),
    )

    assert result.runtime_status == "ready"
    assert admission.failures == []
    assert events == [
        "activity:idle",
        "directory:ensure",
        "config:bundle",
        "kservice:apply",
    ]


async def test_unowned_starting_binding_remains_fail_closed() -> None:
    events: list[str] = []
    service, admission = reconciler(events, activity=[True], pods=[])

    with pytest.raises(RuntimeAdmissionError) as error:
        await service.reconcile(
            replace(workspace(), runtime_status="starting"),
            binding(status="starting"),
        )

    assert error.value.decision.code == "RUNTIME_STARTING"
    assert events == []
    assert admission.failures == []


async def test_failed_binding_can_reacquire_reconcile_ownership() -> None:
    events: list[str] = []
    service, admission = reconciler(events, activity=[True], pods=[0])

    result = await service.reconcile(
        replace(workspace(), runtime_status="failed"),
        binding(status="failed"),
    )

    assert result.runtime_status == "ready"
    assert admission.failures == []
    assert events[0] == "admission:starting"


async def test_nonzero_old_pods_timeout_without_applying_new_revision() -> None:
    events: list[str] = []
    service, admission = reconciler(
        events,
        activity=[True],
        pods=[1],
        ready_forbidden=True,
        timeout=2,
    )

    result = await service.reconcile(workspace(), binding())

    assert result.runtime_status == "failed"
    assert admission.failures == ["previous Runtime Pods did not terminate before deadline"]
    assert "kservice:apply" not in events


async def test_activity_timeout_does_not_invalidate_or_apply() -> None:
    events: list[str] = []
    service, admission = reconciler(
        events,
        activity=[False],
        pods=[],
        ready_forbidden=True,
        timeout=2,
    )

    result = await service.reconcile(workspace(), binding())

    assert result.runtime_status == "failed"
    assert admission.failures == ["runtime activity did not drain before deadline"]
    assert "kservice:delete" not in events


async def test_lost_reconcile_ownership_stops_before_drain() -> None:
    events: list[str] = []
    service, admission = reconciler(
        events,
        activity=[True],
        pods=[0],
        acquired=False,
    )

    with pytest.raises(RuntimeAdmissionError) as error:
        await service.reconcile(workspace(), binding())

    assert error.value.decision.code == "RUNTIME_RECONCILE_IN_PROGRESS"
    assert events == ["admission:starting"]
    assert admission.failures == []
