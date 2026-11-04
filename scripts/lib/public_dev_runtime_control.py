#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

LIB_DIR = Path(__file__).resolve().parent
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from proxy_environment import proxy_free_environment


REVISION_RESOURCE = "revision.serving.knative.dev"
KPA_RESOURCE = "podautoscaler.autoscaling.internal.knative.dev"
POD_RESOURCE = "pod"
PROJECT_SELECTOR = "serving.knative.dev/service,hermes.platform/runtime-id"
REVISION_LABEL = "serving.knative.dev/revision"
PROJECT_LABELS = ("serving.knative.dev/service", "hermes.platform/runtime-id")
COMPONENT_LABEL = "hermes.platform/component"
APPLICATION_AGENT_COMPONENT = "application-agent"
FAST_ZERO_ANNOTATIONS = {
    "autoscaling.knative.dev/scale-down-delay": "0s",
    "autoscaling.knative.dev/window": "6s",
    "autoscaling.knative.dev/scale-to-zero-pod-retention-period": "0s",
}
SNAPSHOT_ANNOTATIONS = (
    *FAST_ZERO_ANNOTATIONS,
)


class RuntimeControlError(RuntimeError):
    pass


class RuntimeControlInterrupted(RuntimeControlError):
    pass


class KubernetesClient(Protocol):
    def verify_namespace(self) -> None: ...

    def list_objects(self, resource: str) -> list[dict[str, Any]]: ...

    def get_object(self, resource: str, name: str) -> dict[str, Any]: ...

    def patch_annotations(
        self,
        resource: str,
        name: str,
        annotations: dict[str, str | None],
    ) -> None: ...

    def delete_object(self, resource: str, name: str) -> None: ...

    def scale_target_zero(self, kpa: dict[str, Any]) -> None: ...

    def delete_pods(self, names: list[str]) -> None: ...


@dataclass(frozen=True)
class AnnotationSnapshot:
    resource: str
    name: str
    annotations: dict[str, str | None]


class KubectlClient:
    def __init__(self, *, binary: str, namespace: str) -> None:
        self.binary = binary
        self.namespace = namespace

    def verify_namespace(self) -> None:
        self._run(["get", "namespace", self.namespace, "-o", "json"])

    def list_objects(self, resource: str) -> list[dict[str, Any]]:
        payload = self._json(
            [
                "-n",
                self.namespace,
                "get",
                resource,
                "-l",
                PROJECT_SELECTOR,
                "-o",
                "json",
            ]
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise RuntimeControlError(f"kubectl returned invalid {resource} list")
        return [item for item in items if isinstance(item, dict)]

    def get_object(self, resource: str, name: str) -> dict[str, Any]:
        return self._json(
            ["-n", self.namespace, "get", resource, name, "-o", "json"]
        )

    def patch_annotations(
        self,
        resource: str,
        name: str,
        annotations: dict[str, str | None],
    ) -> None:
        patch = json.dumps(
            {"metadata": {"annotations": annotations}},
            separators=(",", ":"),
            sort_keys=True,
        )
        self._run(
            [
                "-n",
                self.namespace,
                "patch",
                resource,
                name,
                "--type=merge",
                "-p",
                patch,
            ]
        )

    def delete_object(self, resource: str, name: str) -> None:
        self._run(
            [
                "-n",
                self.namespace,
                "delete",
                resource,
                name,
                "--wait=false",
            ]
        )

    def scale_target_zero(self, kpa: dict[str, Any]) -> None:
        spec = kpa.get("spec")
        target = spec.get("scaleTargetRef") if isinstance(spec, dict) else None
        if not isinstance(target, dict):
            raise RuntimeControlError("KPA has no scaleTargetRef")
        api_version = str(target.get("apiVersion") or "")
        kind = str(target.get("kind") or "")
        name = str(target.get("name") or "")
        if api_version != "apps/v1" or kind != "Deployment" or not name:
            raise RuntimeControlError(
                "KPA scaleTargetRef is not an apps/v1 Deployment"
            )
        self._run(
            [
                "-n",
                self.namespace,
                "scale",
                f"deployment/{name}",
                "--replicas=0",
            ]
        )

    def delete_pods(self, names: list[str]) -> None:
        if not names:
            return
        self._run(
            [
                "-n",
                self.namespace,
                "delete",
                "pod",
                *names,
                "--grace-period=0",
                "--force",
                "--wait=false",
            ]
        )

    def _json(self, arguments: list[str]) -> dict[str, Any]:
        output = self._run(arguments)
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeControlError("kubectl returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeControlError("kubectl returned a non-object JSON response")
        return payload

    def _run(self, arguments: list[str]) -> str:
        command = [self.binary, *arguments]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
                env=proxy_free_environment(),
            )
        except FileNotFoundError as exc:
            raise RuntimeControlError(f"kubectl binary not found: {self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeControlError("kubectl command timed out") from exc
        if completed.returncode != 0:
            detail = " ".join((completed.stderr or completed.stdout).split())
            detail = detail[:500] or f"exit {completed.returncode}"
            raise RuntimeControlError(f"kubectl command failed: {detail}")
        return completed.stdout


class RuntimeZeroController:
    def __init__(
        self,
        client: KubernetesClient,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float = 1.0,
        include_application_agents: bool = False,
        restore_annotations_on_success: bool = True,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.include_application_agents = include_application_agents
        self.restore_annotations_on_success = restore_annotations_on_success
        self.monotonic = monotonic
        self.sleep = sleep

    def force_zero(self) -> list[str]:
        self.client.verify_namespace()
        targets, snapshots, original_kpa_uids = self._discover_targets()
        if not targets:
            print("public-dev runtime stop: no non-zero project Runtime found")
            return []

        print(
            "public-dev runtime stop: forcing zero for revisions="
            + ",".join(targets),
            flush=True,
        )
        modified: list[AnnotationSnapshot] = []
        primary_error: BaseException | None = None
        deadline = self.monotonic() + self.timeout_seconds
        try:
            for name in targets:
                revision_snapshot = snapshots[(REVISION_RESOURCE, name)]
                kpa_snapshot = snapshots[(KPA_RESOURCE, name)]
                modified.extend((revision_snapshot, kpa_snapshot))
                self.client.patch_annotations(
                    REVISION_RESOURCE,
                    name,
                    dict(FAST_ZERO_ANNOTATIONS),
                )
                self.client.patch_annotations(
                    KPA_RESOURCE,
                    name,
                    dict(FAST_ZERO_ANNOTATIONS),
                )
            for name in targets:
                self.client.delete_object(KPA_RESOURCE, name)
            self._wait_for_replacement_kpas(
                targets,
                original_kpa_uids,
                deadline=deadline,
            )
            self._wait_for_desired_zero(targets, deadline=deadline)
            self._force_remove_runtime_pods(targets)
            self._wait_for_zero(targets, deadline=deadline)
        except BaseException as exc:
            primary_error = exc

        restore_errors = (
            self._restore_annotations(modified)
            if primary_error is not None or self.restore_annotations_on_success
            else []
        )
        if primary_error is not None:
            if restore_errors:
                raise RuntimeControlError(
                    f"{primary_error}; annotation restore failed: "
                    + "; ".join(restore_errors)
                ) from primary_error
            raise primary_error
        if restore_errors:
            raise RuntimeControlError(
                "annotation restore failed: " + "; ".join(restore_errors)
            )

        if not self.restore_annotations_on_success:
            print(
                "public-dev runtime stop: scaled to zero and retained maintenance "
                f"annotations revisions={','.join(targets)}",
                flush=True,
            )
            return targets

        self._verify_annotations(modified)
        unfinished = self._unfinished(targets)
        if unfinished:
            raise RuntimeControlError(
                "Runtime became non-zero while restoring annotations: "
                + ", ".join(unfinished)
            )
        print(
            "public-dev runtime stop: scaled to zero and restored annotations "
            f"revisions={','.join(targets)}",
            flush=True,
        )
        return targets

    def _discover_targets(
        self,
    ) -> tuple[
        list[str],
        dict[tuple[str, str], AnnotationSnapshot],
        dict[str, str],
    ]:
        revisions = self._by_name(self.client.list_objects(REVISION_RESOURCE))
        kpas = self._by_name(self.client.list_objects(KPA_RESOURCE))
        pods = [
            item
            for item in self.client.list_objects(POD_RESOURCE)
            if self._is_target_object(item)
        ]

        pod_revisions = {
            str(_labels(pod).get(REVISION_LABEL) or "")
            for pod in pods
        }
        pod_revisions.discard("")

        targets = sorted(
            name
            for name, kpa in kpas.items()
            if _positive_scale(kpa, "desiredScale")
            or _positive_scale(kpa, "actualScale")
            or name in pod_revisions
        )
        missing_revisions = [name for name in targets if name not in revisions]
        if missing_revisions:
            raise RuntimeControlError(
                "project KPA has no matching Revision: "
                + ", ".join(missing_revisions)
            )
        missing_kpas = sorted(pod_revisions.difference(kpas))
        if missing_kpas:
            raise RuntimeControlError(
                "project Pod has no matching KPA: " + ", ".join(missing_kpas)
            )

        snapshots: dict[tuple[str, str], AnnotationSnapshot] = {}
        for name in targets:
            snapshots[(REVISION_RESOURCE, name)] = _snapshot(
                REVISION_RESOURCE,
                revisions[name],
            )
            snapshots[(KPA_RESOURCE, name)] = _snapshot(
                KPA_RESOURCE,
                kpas[name],
            )
        original_kpa_uids = {name: _uid(kpas[name]) for name in targets}
        return targets, snapshots, original_kpa_uids

    def _by_name(
        self,
        items: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for item in items:
            if not self._is_target_object(item):
                continue
            name = str((item.get("metadata") or {}).get("name") or "")
            if name:
                result[name] = item
        return result

    def _wait_for_replacement_kpas(
        self,
        targets: list[str],
        original_uids: dict[str, str],
        *,
        deadline: float,
    ) -> None:
        while True:
            kpas = self._by_name(self.client.list_objects(KPA_RESOURCE))
            unfinished: list[str] = []
            for name in targets:
                kpa = kpas.get(name)
                if kpa is None:
                    unfinished.append(f"{name}(missing)")
                    continue
                if _uid(kpa) == original_uids[name]:
                    unfinished.append(f"{name}(old-uid)")
                    continue
                annotations = _annotations(kpa)
                missing = [
                    key
                    for key, expected in FAST_ZERO_ANNOTATIONS.items()
                    if annotations.get(key) != expected
                ]
                if missing:
                    unfinished.append(f"{name}(annotations-not-ready)")
            if not unfinished:
                return
            if self.monotonic() >= deadline:
                raise RuntimeControlError(
                    "timed out waiting for replacement KPA: "
                    + ", ".join(unfinished)
                )
            self.sleep(self.poll_interval_seconds)

    def _wait_for_desired_zero(
        self,
        targets: list[str],
        *,
        deadline: float,
    ) -> None:
        while True:
            kpas = self._by_name(self.client.list_objects(KPA_RESOURCE))
            unfinished: list[str] = []
            for name in targets:
                kpa = kpas.get(name)
                desired = _scale_value(kpa, "desiredScale") if kpa else None
                if desired != 0:
                    unfinished.append(f"{name}(desired={desired})")
            if not unfinished:
                return
            if self.monotonic() >= deadline:
                raise RuntimeControlError(
                    "timed out waiting for replacement KPA desiredScale=0: "
                    + ", ".join(unfinished)
                )
            self.sleep(self.poll_interval_seconds)

    def _force_remove_runtime_pods(self, targets: list[str]) -> None:
        target_set = set(targets)
        kpas = self._by_name(self.client.list_objects(KPA_RESOURCE))
        for name in targets:
            kpa = kpas.get(name)
            if kpa is None or _scale_value(kpa, "desiredScale") != 0:
                raise RuntimeControlError(
                    f"refusing direct scale before KPA desiredScale=0: {name}"
                )
            self.client.scale_target_zero(kpa)

        pod_names = sorted(
            str((pod.get("metadata") or {}).get("name") or "")
            for pod in self.client.list_objects(POD_RESOURCE)
            if self._is_target_object(pod)
            and str(_labels(pod).get(REVISION_LABEL) or "") in target_set
        )
        self.client.delete_pods([name for name in pod_names if name])

    def _wait_for_zero(self, targets: list[str], *, deadline: float) -> None:
        while True:
            unfinished = self._unfinished(targets)
            if not unfinished:
                return
            if self.monotonic() >= deadline:
                raise RuntimeControlError(
                    "timed out waiting for Runtime scale-to-zero: "
                    + ", ".join(unfinished)
                )
            self.sleep(self.poll_interval_seconds)

    def _unfinished(self, targets: list[str]) -> list[str]:
        target_set = set(targets)
        kpas = self._by_name(self.client.list_objects(KPA_RESOURCE))
        pods = [
            item
            for item in self.client.list_objects(POD_RESOURCE)
            if self._is_target_object(item)
        ]
        active_pods: dict[str, int] = {}
        for pod in pods:
            revision = str(_labels(pod).get(REVISION_LABEL) or "")
            if revision in target_set:
                active_pods[revision] = active_pods.get(revision, 0) + 1

        unfinished: list[str] = []
        for name in targets:
            kpa = kpas.get(name)
            desired = _scale_value(kpa, "desiredScale") if kpa else None
            actual = _scale_value(kpa, "actualScale") if kpa else None
            pod_count = active_pods.get(name, 0)
            if desired != 0 or actual != 0 or pod_count:
                unfinished.append(
                    f"{name}(desired={desired},actual={actual},pods={pod_count})"
                )
        return unfinished

    def _is_target_object(self, item: dict[str, Any]) -> bool:
        return _is_project_object(
            item,
            include_application_agents=self.include_application_agents,
        )

    def _restore_annotations(
        self,
        snapshots: list[AnnotationSnapshot],
    ) -> list[str]:
        errors: list[str] = []
        for snapshot in snapshots:
            try:
                self.client.patch_annotations(
                    snapshot.resource,
                    snapshot.name,
                    dict(snapshot.annotations),
                )
            except BaseException as exc:
                errors.append(
                    f"{snapshot.resource}/{snapshot.name}: "
                    + " ".join(str(exc).split())[:300]
                )
        return errors

    def _verify_annotations(self, snapshots: list[AnnotationSnapshot]) -> None:
        mismatches: list[str] = []
        for snapshot in snapshots:
            current = self.client.get_object(snapshot.resource, snapshot.name)
            annotations = _annotations(current)
            for key, expected in snapshot.annotations.items():
                actual = annotations.get(key)
                if actual != expected:
                    mismatches.append(
                        f"{snapshot.resource}/{snapshot.name}:{key}"
                    )
        if mismatches:
            raise RuntimeControlError(
                "annotation restore verification failed: " + ", ".join(mismatches)
            )


def _labels(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    return labels if isinstance(labels, dict) else {}


def _annotations(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata")
    annotations = (
        metadata.get("annotations") if isinstance(metadata, dict) else None
    )
    return annotations if isinstance(annotations, dict) else {}


def _is_project_object(
    item: dict[str, Any],
    *,
    include_application_agents: bool = False,
) -> bool:
    labels = _labels(item)
    return (
        all(bool(labels.get(key)) for key in PROJECT_LABELS)
        and (
            include_application_agents
            or labels.get(COMPONENT_LABEL) != APPLICATION_AGENT_COMPONENT
        )
    )


def _scale_value(item: dict[str, Any] | None, key: str) -> int | None:
    if not isinstance(item, dict):
        return None
    status = item.get("status")
    value = status.get(key) if isinstance(status, dict) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _positive_scale(item: dict[str, Any], key: str) -> bool:
    value = _scale_value(item, key)
    return value is not None and value > 0


def _uid(item: dict[str, Any]) -> str:
    uid = str((item.get("metadata") or {}).get("uid") or "")
    if not uid:
        name = str((item.get("metadata") or {}).get("name") or "<unknown>")
        raise RuntimeControlError(f"Kubernetes object has no UID: {name}")
    return uid


def _snapshot(resource: str, item: dict[str, Any]) -> AnnotationSnapshot:
    name = str((item.get("metadata") or {}).get("name") or "")
    annotations = _annotations(item)
    return AnnotationSnapshot(
        resource=resource,
        name=name,
        annotations={
            key: str(annotations[key]) if key in annotations else None
            for key in SNAPSHOT_ANNOTATIONS
        },
    )


def _signal_handler(signum: int, _frame: Any) -> None:
    raise RuntimeControlInterrupted(f"interrupted by signal {signum}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Force project Knative Runtime revisions to zero replicas."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    force_zero = subparsers.add_parser("force-zero")
    force_zero.add_argument("--namespace", required=True)
    force_zero.add_argument("--kubectl", default="kubectl")
    force_zero.add_argument("--timeout-seconds", type=float, default=120.0)
    force_zero.add_argument("--poll-interval-seconds", type=float, default=1.0)
    force_zero.add_argument("--include-application-agents", action="store_true")
    force_zero.add_argument(
        "--keep-annotations",
        action="store_true",
        help="retain temporary autoscaling annotations after successful zero",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    try:
        controller = RuntimeZeroController(
            KubectlClient(binary=args.kubectl, namespace=args.namespace),
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            include_application_agents=args.include_application_agents,
            restore_annotations_on_success=not args.keep_annotations,
        )
        controller.force_zero()
    except (RuntimeControlError, ValueError) as exc:
        print(
            "public-dev runtime stop: failed: "
            + " ".join(str(exc).split())[:700],
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
