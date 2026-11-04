from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "scripts" / "lib" / "public_dev_runtime_control.py"
SPEC = importlib.util.spec_from_file_location("public_dev_runtime_control", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
runtime_control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime_control
SPEC.loader.exec_module(runtime_control)


def _labels(name: str, *, project: bool = True) -> dict[str, str]:
    labels = {
        "serving.knative.dev/service": name.split("-000", 1)[0],
        "serving.knative.dev/revision": name,
    }
    if project:
        labels["hermes.platform/runtime-id"] = f"rt-{name}"
    return labels


def _revision(
    name: str,
    annotations: dict[str, str],
    *,
    project: bool = True,
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "labels": _labels(name, project=project),
            "annotations": dict(annotations),
        }
    }


def _kpa(
    name: str,
    *,
    desired: int,
    actual: int,
    annotations: dict[str, str],
    project: bool = True,
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "uid": f"uid-{name}-1",
            "labels": _labels(name, project=project),
            "annotations": dict(annotations),
        },
        "spec": {
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": f"{name}-deployment",
            }
        },
        "status": {"desiredScale": desired, "actualScale": actual},
    }


def _pod(name: str, revision: str, phase: str, *, project: bool = True) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "labels": _labels(revision, project=project),
        },
        "status": {"phase": phase},
    }


class FakeKubernetesClient:
    def __init__(
        self,
        *,
        revisions: list[dict[str, Any]],
        kpas: list[dict[str, Any]],
        pods: list[dict[str, Any]],
    ) -> None:
        self.objects = {
            runtime_control.REVISION_RESOURCE: revisions,
            runtime_control.KPA_RESOURCE: kpas,
            runtime_control.POD_RESOURCE: pods,
        }
        self.namespace_verified = False
        self.patches: list[tuple[str, str, dict[str, str | None]]] = []
        self.deletes: list[tuple[str, str]] = []
        self.scales: list[str] = []
        self.deleted_pods: list[list[str]] = []
        self.events: list[str] = []
        self.fail_patch = None
        self.recreate_kpa = True
        self.replacement_desired = 0
        self.replacement_actual = 1
        self.replacement_annotations: dict[str, dict[str, str]] = {}

    def verify_namespace(self) -> None:
        self.namespace_verified = True

    def list_objects(self, resource: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.objects[resource])

    def get_object(self, resource: str, name: str) -> dict[str, Any]:
        return copy.deepcopy(self._find(resource, name))

    def patch_annotations(
        self,
        resource: str,
        name: str,
        annotations: dict[str, str | None],
    ) -> None:
        self.patches.append((resource, name, dict(annotations)))
        self.events.append(f"patch:{resource}:{name}")
        if self.fail_patch is not None and self.fail_patch(resource, name, annotations):
            raise runtime_control.RuntimeControlError("injected patch failure")
        item = self._find(resource, name)
        current = item.setdefault("metadata", {}).setdefault("annotations", {})
        for key, value in annotations.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value

    def delete_object(self, resource: str, name: str) -> None:
        self.deletes.append((resource, name))
        self.events.append(f"delete:{resource}:{name}")
        item = self._find(resource, name)
        self.objects[resource].remove(item)
        if resource != runtime_control.KPA_RESOURCE or not self.recreate_kpa:
            return
        revision = self._find(runtime_control.REVISION_RESOURCE, name)
        annotations = dict(revision["metadata"].get("annotations", {}))
        replacement = _kpa(
            name,
            desired=self.replacement_desired,
            actual=self.replacement_actual,
            annotations=annotations,
        )
        replacement["metadata"]["uid"] = f"uid-{name}-replacement"
        self.objects[resource].append(replacement)
        self.replacement_annotations[name] = dict(annotations)

    def scale_target_zero(self, kpa: dict[str, Any]) -> None:
        name = str(kpa["metadata"]["name"])
        assert kpa["status"]["desiredScale"] == 0
        self.scales.append(name)
        self.events.append(f"scale:{name}")
        current = self._find(runtime_control.KPA_RESOURCE, name)
        current["status"]["actualScale"] = 0

    def delete_pods(self, names: list[str]) -> None:
        self.deleted_pods.append(list(names))
        self.events.append("delete-pods:" + ",".join(names))
        name_set = set(names)
        self.objects[runtime_control.POD_RESOURCE] = [
            pod
            for pod in self.objects[runtime_control.POD_RESOURCE]
            if pod["metadata"]["name"] not in name_set
        ]

    def _find(self, resource: str, name: str) -> dict[str, Any]:
        for item in self.objects[resource]:
            if item.get("metadata", {}).get("name") == name:
                return item
        raise runtime_control.RuntimeControlError(f"missing fake object {resource}/{name}")


def _controller(
    client: FakeKubernetesClient,
    *,
    timeout: float = 5,
    on_sleep=None,
    include_application_agents: bool = False,
    restore_annotations_on_success: bool = True,
):
    now = [0.0]

    def monotonic() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if on_sleep is not None:
            on_sleep()

    return runtime_control.RuntimeZeroController(
        client,
        timeout_seconds=timeout,
        poll_interval_seconds=1,
        include_application_agents=include_application_agents,
        restore_annotations_on_success=restore_annotations_on_success,
        monotonic=monotonic,
        sleep=sleep,
    )


def test_force_zero_targets_only_nonzero_project_revision_and_restores_annotations() -> None:
    running = "runtime-a-00002"
    zero = "runtime-b-00002"
    foreign = "foreign-00001"
    revision_annotations = {
        "autoscaling.knative.dev/scale-down-delay": "600s",
        "autoscaling.knative.dev/window": "12s",
        "unrelated": "preserved",
    }
    kpa_annotations = {
        "autoscaling.knative.dev/scale-down-delay": "600s",
        "unrelated": "preserved",
    }
    client = FakeKubernetesClient(
        revisions=[
            _revision(running, revision_annotations),
            _revision(zero, revision_annotations),
            _revision(foreign, revision_annotations, project=False),
        ],
        kpas=[
            _kpa(running, desired=1, actual=1, annotations=kpa_annotations),
            _kpa(zero, desired=0, actual=0, annotations=kpa_annotations),
            _kpa(
                foreign,
                desired=1,
                actual=1,
                annotations=kpa_annotations,
                project=False,
            ),
        ],
        pods=[
            _pod("runtime-a-pod", running, "Running"),
            _pod("foreign-pod", foreign, "Running", project=False),
        ],
    )
    targets = _controller(client).force_zero()

    assert client.namespace_verified is True
    assert targets == [running]
    assert {name for _resource, name, _annotations in client.patches} == {running}
    assert client.patches[0][2] == runtime_control.FAST_ZERO_ANNOTATIONS
    assert runtime_control.FAST_ZERO_ANNOTATIONS[
        "autoscaling.knative.dev/scale-down-delay"
    ] == "0s"
    assert client.replacement_annotations[running][
        "autoscaling.knative.dev/scale-down-delay"
    ] == "0s"
    assert client.deletes == [(runtime_control.KPA_RESOURCE, running)]
    assert client.scales == [running]
    assert client.deleted_pods == [["runtime-a-pod"]]
    assert client.events.index(f"scale:{running}") > client.events.index(
        f"delete:{runtime_control.KPA_RESOURCE}:{running}"
    )
    assert client.get_object(runtime_control.REVISION_RESOURCE, running)["metadata"][
        "annotations"
    ] == revision_annotations
    assert client.get_object(runtime_control.KPA_RESOURCE, running)["metadata"][
        "annotations"
    ] == kpa_annotations
    assert not any(name == zero for _resource, name, _annotations in client.patches)
    assert not any(name == foreign for _resource, name, _annotations in client.patches)


def test_force_zero_skips_application_agent_resources() -> None:
    runtime = "runtime-a-00002"
    application_agent = "application-agent-1-00001"
    annotations = {"autoscaling.knative.dev/scale-down-delay": "600s"}
    agent_revision = _revision(application_agent, annotations)
    agent_kpa = _kpa(
        application_agent,
        desired=1,
        actual=1,
        annotations=annotations,
    )
    agent_pod = _pod("application-agent-pod", application_agent, "Running")
    for item in (agent_revision, agent_kpa, agent_pod):
        item["metadata"]["labels"]["hermes.platform/component"] = (
            "application-agent"
        )

    client = FakeKubernetesClient(
        revisions=[_revision(runtime, annotations), agent_revision],
        kpas=[
            _kpa(runtime, desired=1, actual=1, annotations=annotations),
            agent_kpa,
        ],
        pods=[_pod("runtime-pod", runtime, "Running"), agent_pod],
    )

    assert _controller(client).force_zero() == [runtime]
    assert {name for _resource, name, _annotations in client.patches} == {runtime}
    assert client.deletes == [(runtime_control.KPA_RESOURCE, runtime)]
    assert client.scales == [runtime]
    assert client.deleted_pods == [["runtime-pod"]]
    assert client.get_object(runtime_control.KPA_RESOURCE, application_agent)[
        "status"
    ] == {"desiredScale": 1, "actualScale": 1}


def test_force_zero_can_include_application_agents_and_keep_annotations() -> None:
    runtime = "runtime-a-00002"
    application_agent = "application-agent-1-00001"
    original = {"autoscaling.knative.dev/scale-down-delay": "600s"}
    agent_revision = _revision(application_agent, original)
    agent_kpa = _kpa(
        application_agent,
        desired=1,
        actual=1,
        annotations=original,
    )
    agent_pod = _pod("application-agent-pod", application_agent, "Running")
    for item in (agent_revision, agent_kpa, agent_pod):
        item["metadata"]["labels"]["hermes.platform/component"] = (
            "application-agent"
        )
    client = FakeKubernetesClient(
        revisions=[_revision(runtime, original), agent_revision],
        kpas=[
            _kpa(runtime, desired=1, actual=1, annotations=original),
            agent_kpa,
        ],
        pods=[_pod("runtime-pod", runtime, "Running"), agent_pod],
    )

    assert _controller(
        client,
        include_application_agents=True,
        restore_annotations_on_success=False,
    ).force_zero() == [application_agent, runtime]
    assert client.scales == [application_agent, runtime]
    assert client.get_object(runtime_control.REVISION_RESOURCE, runtime)["metadata"][
        "annotations"
    ]["autoscaling.knative.dev/scale-down-delay"] == "0s"
    assert client.get_object(
        runtime_control.REVISION_RESOURCE, application_agent
    )["metadata"]["annotations"]["autoscaling.knative.dev/scale-down-delay"] == "0s"


def test_force_zero_noops_when_all_project_runtimes_are_already_zero() -> None:
    revision = "runtime-a-00002"
    client = FakeKubernetesClient(
        revisions=[_revision(revision, {})],
        kpas=[_kpa(revision, desired=0, actual=0, annotations={})],
        pods=[],
    )

    assert _controller(client).force_zero() == []
    assert client.patches == []
    assert client.deletes == []
    assert client.scales == []


def test_force_zero_timeout_restores_original_annotations() -> None:
    revision = "runtime-a-00002"
    original = {"autoscaling.knative.dev/scale-down-delay": "600s"}
    client = FakeKubernetesClient(
        revisions=[_revision(revision, original)],
        kpas=[_kpa(revision, desired=1, actual=1, annotations=original)],
        pods=[_pod("runtime-a-pod", revision, "Running")],
    )
    client.replacement_desired = 1

    with pytest.raises(runtime_control.RuntimeControlError, match="timed out"):
        _controller(client, timeout=2).force_zero()

    assert client.get_object(runtime_control.REVISION_RESOURCE, revision)["metadata"][
        "annotations"
    ] == original
    assert client.get_object(runtime_control.KPA_RESOURCE, revision)["metadata"][
        "annotations"
    ] == original
    assert client.scales == []
    assert client.deleted_pods == []


def test_force_zero_partial_patch_failure_restores_both_objects() -> None:
    revision = "runtime-a-00002"
    original = {"autoscaling.knative.dev/scale-down-delay": "600s"}
    client = FakeKubernetesClient(
        revisions=[_revision(revision, original)],
        kpas=[_kpa(revision, desired=1, actual=1, annotations=original)],
        pods=[_pod("runtime-a-pod", revision, "Running")],
    )
    failed = [False]

    def fail_patch(resource, _name, annotations) -> bool:
        if (
            not failed[0]
            and resource == runtime_control.KPA_RESOURCE
            and annotations == runtime_control.FAST_ZERO_ANNOTATIONS
        ):
            failed[0] = True
            return True
        return False

    client.fail_patch = fail_patch

    with pytest.raises(runtime_control.RuntimeControlError, match="injected patch failure"):
        _controller(client).force_zero()

    assert client.get_object(runtime_control.REVISION_RESOURCE, revision)["metadata"][
        "annotations"
    ] == original
    assert client.get_object(runtime_control.KPA_RESOURCE, revision)["metadata"][
        "annotations"
    ] == original


def test_force_zero_reports_restore_failure_after_attempting_other_restores() -> None:
    revision = "runtime-a-00002"
    original = {"autoscaling.knative.dev/scale-down-delay": "600s"}
    client = FakeKubernetesClient(
        revisions=[_revision(revision, original)],
        kpas=[_kpa(revision, desired=1, actual=1, annotations=original)],
        pods=[_pod("runtime-a-pod", revision, "Running")],
    )
    restore_failed = [False]

    def fail_patch(resource, _name, annotations) -> bool:
        if (
            not restore_failed[0]
            and resource == runtime_control.REVISION_RESOURCE
            and annotations == {
                "autoscaling.knative.dev/scale-down-delay": "600s",
                "autoscaling.knative.dev/window": None,
                "autoscaling.knative.dev/scale-to-zero-pod-retention-period": None,
            }
        ):
            restore_failed[0] = True
            return True
        return False

    client.fail_patch = fail_patch

    with pytest.raises(runtime_control.RuntimeControlError, match="annotation restore failed"):
        _controller(client).force_zero()

    assert any(
        resource == runtime_control.KPA_RESOURCE and annotations.get(
            "autoscaling.knative.dev/scale-down-delay"
        ) == "600s"
        for resource, _name, annotations in client.patches
    )


def test_force_zero_times_out_when_deleted_kpa_is_not_recreated() -> None:
    revision = "runtime-a-00002"
    original = {"autoscaling.knative.dev/scale-down-delay": "600s"}
    client = FakeKubernetesClient(
        revisions=[_revision(revision, original)],
        kpas=[_kpa(revision, desired=1, actual=1, annotations=original)],
        pods=[_pod("runtime-a-pod", revision, "Running")],
    )
    client.recreate_kpa = False

    with pytest.raises(
        runtime_control.RuntimeControlError,
        match="timed out waiting for replacement KPA",
    ):
        _controller(client, timeout=2).force_zero()

    assert client.scales == []
    assert client.deleted_pods == []


def test_force_zero_deletes_completed_runtime_pod_objects_too() -> None:
    revision = "runtime-a-00002"
    original = {"autoscaling.knative.dev/scale-down-delay": "600s"}
    client = FakeKubernetesClient(
        revisions=[_revision(revision, original)],
        kpas=[_kpa(revision, desired=0, actual=0, annotations=original)],
        pods=[_pod("runtime-a-completed", revision, "Succeeded")],
    )

    assert _controller(client).force_zero() == [revision]
    assert client.deleted_pods == [["runtime-a-completed"]]
