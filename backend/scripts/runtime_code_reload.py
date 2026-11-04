#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import time


from sqlalchemy import func, select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.database import AsyncSessionLocal  # noqa: E402
from app.hermes_runtime.providers.knative import KubectlKnativeClient  # noqa: E402
from app.models import (  # noqa: E402
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
    HermesRuntimeBinding,
)
from app.services.runtime_activity import SqlAlchemyRuntimeActivityStore  # noqa: E402
from app.services.runtime_image_config import RuntimeImageConfigStore  # noqa: E402
from app.services.runtime_image_verification import (  # noqa: E402
    require_live_image_matches_target,
)
from app.services.runtime_maintenance import RuntimeMaintenanceService  # noqa: E402
from app.services.runtime_platform_control import (  # noqa: E402
    PLATFORM_CONTROL_KEY_FILE_NAME,
    platform_control_secret_name,
    runtime_pvc_storage_identity_hash,
)
from app.services.runtime_reload import (  # noqa: E402
    RuntimeReloadBinding,
    RuntimeReloadService,
)
from app.services.runtime_commands import (  # noqa: E402
    RuntimeCommandError,
    RuntimeCommandClient,
    load_runtime_reload_command_target,
)
from app.services.runtime_session_permission_maintenance import (  # noqa: E402
    RuntimeSessionPermissionMaintenance,
)


USER_CONTAINER_NAME = "user-container"
CONTAINERD_NAMESPACE = "k8s.io"
PROC_CGROUP_RETRIES = 2
PROC_CGROUP_RETRY_DELAY_SECONDS = 0.05


def _unavailable_runtime_cancel_receipt(
    *,
    error: Exception,
    backend_idle: bool,
) -> dict[str, object] | None:
    """Defer cancellation only when Backend proves there is no active work."""

    if not backend_idle:
        return None
    message = str(error)
    if (
        "delivery failed" not in message
        and "HTTP 408" not in message
        and "HTTP 503" not in message
    ):
        return None
    return {
        "runs": [],
        "deferred_cancel": "runtime_endpoint_unavailable_backend_idle",
    }


async def _personal_runtime_backend_idle(
    db: object,
    binding: RuntimeReloadBinding,
) -> bool:
    if binding.binding_type != "personal_runtime":
        return False
    row = await db.scalar(  # type: ignore[attr-defined]
        select(HermesRuntimeBinding).where(
            HermesRuntimeBinding.id == binding.binding_id,
            HermesRuntimeBinding.runtime_id == binding.runtime_id,
        )
    )
    if row is None:
        return False
    snapshot = await SqlAlchemyRuntimeActivityStore(db).get_snapshot(
        int(row.runtime_workspace_id),
    )
    return snapshot.idle


async def _application_runtime_backend_idle(
    db: object,
    binding: RuntimeReloadBinding,
) -> bool:
    row = await db.scalar(  # type: ignore[attr-defined]
        select(ApplicationAgentRuntimeBinding).where(
            ApplicationAgentRuntimeBinding.id == binding.binding_id,
            ApplicationAgentRuntimeBinding.runtime_id == binding.runtime_id,
        )
    )
    if row is None or int(row.active_run_count or 0) != 0:
        return False
    active_runs = await db.scalar(  # type: ignore[attr-defined]
        select(func.count(ApplicationAgentRun.id)).where(
            ApplicationAgentRun.runtime_binding_id == binding.binding_id,
            ApplicationAgentRun.status.in_(("queued", "running")),
        )
    )
    return int(active_runs or 0) == 0


async def _runtime_backend_idle(
    db: object,
    binding: RuntimeReloadBinding,
) -> bool:
    if binding.binding_type == "personal_runtime":
        return await _personal_runtime_backend_idle(db, binding)
    if binding.binding_type == "application_runtime":
        return await _application_runtime_backend_idle(db, binding)
    return False


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backend-owned Runtime code reload control.")
    parser.add_argument("--env-file", type=Path, default=BACKEND_ROOT / ".env")
    parser.add_argument("--kubeconfig", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    binding_parser = argparse.ArgumentParser(add_help=False)
    binding_parser.add_argument(
        "--binding-type",
        required=True,
        choices=("personal_runtime", "application_runtime"),
    )
    binding_parser.add_argument("--binding-id", required=True, type=int)

    subparsers.add_parser("resolve", parents=[binding_parser])
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--runtime-id")
    list_parser.add_argument(
        "--binding-type",
        choices=("personal_runtime", "application_runtime"),
    )
    isolate = subparsers.add_parser("isolate", parents=[binding_parser])
    isolate.add_argument("--maintenance-epoch")
    subparsers.add_parser("mark-failed", parents=[binding_parser])
    subparsers.add_parser("cancel-active", parents=[binding_parser])
    session_check = subparsers.add_parser("verify-sessions", parents=[binding_parser])
    session_check.add_argument("--checkpoint", required=True, type=Path)
    stop = subparsers.add_parser("stop", parents=[binding_parser])
    stop.add_argument("--timeout", type=float, default=180.0)
    stop.add_argument(
        "--delete-pods",
        action="store_true",
        help="After deleting the KService, explicitly delete its exact Pods (dev only).",
    )
    stop.add_argument(
        "--fast-test-mode",
        action="store_true",
        help="15 测试环境：身份闭环后精确终止 task/PID，再强制删除精确 Pod。",
    )
    stop.add_argument(
        "--task-kill-timeout",
        type=float,
        default=3.0,
        help="Fast stop task TERM 后等待秒数（0.5-10）。",
    )
    stop.add_argument(
        "--delete-grace",
        type=float,
        default=1.5,
        help="兼容旧参数；fast stop 保持 API 强删参数兼容。",
    )
    permission_repair = subparsers.add_parser(
        "repair-session-permissions",
        parents=[binding_parser],
    )
    permission_repair.add_argument("--maintenance-epoch", required=True)
    reload_parser = subparsers.add_parser("reload", parents=[binding_parser])
    reload_parser.add_argument("--maintenance-epoch")
    reload_parser.add_argument(
        "--deferred",
        action="store_true",
        help=(
            "Idle personal Runtime path: switch the Binding target without "
            "recreating the KService; the next login warmup recreates it."
        ),
    )
    reload_parser.add_argument(
        "--deferred-precreate",
        action="store_true",
        help=(
            "Idle personal Runtime path: recreate a Ready zero-scale KService "
            "without activating a Pod; the next login warmup activates it."
        ),
    )
    verify = subparsers.add_parser("verify", parents=[binding_parser])
    verify.add_argument("--timeout", type=float, default=180.0)
    verify.add_argument(
        "--allow-absent",
        action="store_true",
        help=(
            "Accept a deliberately absent KService when the Binding already "
            "owns the target image and the PVC identity is unchanged."
        ),
    )
    verify.add_argument(
        "--precreated-zero-scale",
        action="store_true",
        help=(
            "Accept a Ready target KService with zero Pods as deferred login "
            "activation evidence."
        ),
    )
    subparsers.add_parser(
        "classify",
        help="Read-only fleet classification of live Pods per Runtime Binding.",
    )
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--maintenance-epoch", required=True)
    freeze.add_argument("--reason", required=True)
    unfreeze = subparsers.add_parser("unfreeze")
    unfreeze.add_argument("--maintenance-epoch", required=True)
    frozen = subparsers.add_parser("assert-frozen")
    frozen.add_argument("--maintenance-epoch", required=True)
    fleet_switch = subparsers.add_parser("switch-fleet")
    fleet_switch.add_argument("--maintenance-epoch", required=True)
    fleet_switch.add_argument("--checkpoint", required=True, type=Path)
    fleet_audit = subparsers.add_parser("audit-fleet")
    fleet_audit.add_argument("--checkpoint", type=Path)
    fleet_audit.add_argument(
        "--allow-unavailable",
        action="store_true",
        help="Allow non-ready Backend Bindings omitted by the closed test fleet.",
    )
    subparsers.add_parser(
        "maintenance-status",
        help="Read the Backend-owned Runtime maintenance gate without changing it.",
    )
    args = parser.parse_args()

    async with AsyncSessionLocal() as db:
        if args.command == "freeze":
            snapshot = await RuntimeMaintenanceService(db).freeze(
                maintenance_epoch=args.maintenance_epoch,
                reason=args.reason,
            )
            print(
                json.dumps(
                    {"state": snapshot.maintenance_state, "epoch": snapshot.maintenance_epoch}
                )
            )
            return 0
        if args.command == "unfreeze":
            snapshot = await RuntimeMaintenanceService(db).open(
                maintenance_epoch=args.maintenance_epoch
            )
            print(
                json.dumps(
                    {"state": snapshot.maintenance_state, "epoch": snapshot.maintenance_epoch}
                )
            )
            return 0
        if args.command == "assert-frozen":
            snapshot = await RuntimeMaintenanceService(db).snapshot()
            if (
                not snapshot.maintenance_active
                or snapshot.maintenance_epoch != args.maintenance_epoch
            ):
                raise RuntimeError("global Runtime maintenance ownership does not match")
            print(json.dumps({"state": "frozen", "epoch": snapshot.maintenance_epoch}))
            return 0
        if args.command == "maintenance-status":
            snapshot = await RuntimeMaintenanceService(db).snapshot()
            print(
                json.dumps(
                    {
                        "state": snapshot.maintenance_state,
                        "epoch": snapshot.maintenance_epoch,
                        "grant_issuance_enabled": snapshot.grant_issuance_enabled,
                    }
                )
            )
            return 0

        service = RuntimeReloadService(db)
        if args.command == "audit-fleet":
            if args.checkpoint is None:
                bindings = await service.list_bindings()
            else:
                checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
                bindings = [
                    RuntimeReloadBinding(**item)
                    for item in checkpoint.get("bindings") or []
                ]
            evidence = await _audit_fleet(
                KubectlKnativeClient(kubeconfig=args.kubeconfig),
                service,
                bindings,
                allow_unavailable=args.allow_unavailable,
            )
            print(json.dumps(evidence, sort_keys=True))
            return 0
        if args.command == "switch-fleet":
            checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
            bindings = [RuntimeReloadBinding(**item) for item in checkpoint.get("bindings") or []]
            target = RuntimeImageConfigStore(args.env_file).read()
            await service.switch_fleet_target(
                bindings,
                target=target,
                maintenance_epoch=args.maintenance_epoch,
            )
            print(json.dumps({"switched": len(bindings)}, sort_keys=True))
            return 0
        if args.command == "list":
            bindings = await service.list_bindings(
                runtime_id=args.runtime_id,
                binding_type=args.binding_type,
            )
            print(json.dumps([asdict(item) for item in bindings], sort_keys=True))
            return 0
        if args.command == "classify":
            kubernetes = KubectlKnativeClient(kubeconfig=args.kubeconfig)
            classifications = []
            for binding in await service.list_bindings():
                pods = await kubernetes.list_kservice_pods(
                    namespace=binding.namespace,
                    service_name=binding.service_name,
                )
                running = [
                    pod
                    for pod in pods
                    if not (pod.get("metadata") or {}).get("deletionTimestamp")
                    and str((pod.get("status") or {}).get("phase") or "") == "Running"
                ]
                classifications.append(
                    {
                        "binding_type": binding.binding_type,
                        "binding_id": binding.binding_id,
                        "runtime_id": binding.runtime_id,
                        "service_name": binding.service_name,
                        "running_pod_count": len(running),
                    }
                )
            print(json.dumps(classifications, sort_keys=True))
            return 0
        binding = await service.resolve_binding(
            binding_type=args.binding_type,
            binding_id=args.binding_id,
        )
        if args.command == "resolve":
            print(json.dumps(asdict(binding), sort_keys=True))
            return 0
        if args.command == "isolate":
            isolated = await service.isolate_binding(
                binding,
                maintenance_epoch=args.maintenance_epoch,
            )
            print(json.dumps(asdict(isolated), sort_keys=True))
            return 0
        if args.command == "mark-failed":
            failed = await service.mark_failed(binding)
            print(json.dumps(asdict(failed), sort_keys=True))
            return 0
        if args.command == "cancel-active":
            target = await load_runtime_reload_command_target(
                db,
                binding_type=binding.binding_type,
                binding_id=binding.binding_id,
            )
            if await _runtime_backend_idle(db, binding):
                print(
                    json.dumps(
                        {
                            "runs": [],
                            "deferred_cancel": "backend_idle_no_endpoint_call",
                        },
                        sort_keys=True,
                    )
                )
                return 0
            client = RuntimeCommandClient()
            try:
                try:
                    result = await client.cancel_active_runs(target=target)
                except RuntimeCommandError as exc:
                    # A pre-batch-cancel Supervisor rejects the new command
                    # at its union validator.  Use its existing per-Run
                    # cancel command as a one-time compatibility bridge;
                    # this is only reachable during an explicitly frozen
                    # reload and records orphaned historical rows.
                    if "HTTP 422" not in str(exc):
                        if "HTTP 404" in str(exc):
                            # An application Runtime may have no KService yet
                            # (for example after an interrupted first
                            # convergence).  There cannot be an active Run
                            # to cancel in that state; let the reload flow
                            # recreate the exact service from its Binding.
                            result = {
                                "runs": [],
                                "deferred_cancel": "runtime_endpoint_unavailable",
                            }
                        elif "HTTP 500: Internal Server Error" in str(exc):
                            # A previously deployed Supervisor may have a
                            # corrupt redirect journal that rejects the batch
                            # cancel.  The new image contains the terminal
                            # interruption fix; defer cancellation until it
                            # is live, while keeping maintenance frozen.
                            result = {
                                "runs": [],
                                "deferred_cancel": "legacy_redirect_conflict",
                            }
                        else:
                            deferred = _unavailable_runtime_cancel_receipt(
                                error=exc,
                                backend_idle=await _runtime_backend_idle(
                                    db,
                                    binding,
                                ),
                            )
                            if deferred is None:
                                raise
                            result = deferred
                    else:
                        result = await _legacy_cancel_active_runs(
                            client,
                            target=target,
                            binding=binding,
                        )
            finally:
                await client.close()
            print(json.dumps(result, sort_keys=True))
            return 0

        kubernetes = KubectlKnativeClient(kubeconfig=args.kubeconfig)
        if args.command == "verify-sessions":
            checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
            raw_receipts = (checkpoint.get("checks") or {}).get("active_runs") or []
            if isinstance(raw_receipts, dict):
                binding_key = (
                    f"{binding.binding_type}:{binding.binding_id}:{binding.runtime_id}"
                )
                binding_receipt = raw_receipts.get(binding_key) or {}
                receipts = binding_receipt.get("runs", [])
                legacy_orphans = binding_receipt.get("legacy_orphans", [])
            else:
                receipts = raw_receipts
                legacy_orphans = []
            target_command = await load_runtime_reload_command_target(
                db,
                binding_type=binding.binding_type,
                binding_id=binding.binding_id,
            )
            client = RuntimeCommandClient()
            try:
                evidence = await _verify_session_receipts(
                    client,
                    target=target_command,
                    receipts=receipts,
                    legacy_orphans=legacy_orphans,
                )
            finally:
                await client.close()
            print(json.dumps(evidence, sort_keys=True))
            return 0
        if args.command == "stop":
            if args.fast_test_mode:
                _require_fast_mode(args.delete_pods)
            force_receipts = await _stop(
                kubernetes,
                binding,
                timeout=args.timeout,
                delete_pods=args.delete_pods,
                fast_test_mode=args.fast_test_mode,
                task_kill_timeout=args.task_kill_timeout,
                delete_grace=args.delete_grace,
            )
            result = {"runtime_id": binding.runtime_id, "pods": 0}
            if force_receipts:
                result["force_termination"] = force_receipts
            print(json.dumps(result, sort_keys=True))
            return 0
        target = RuntimeImageConfigStore(args.env_file).read()
        if args.command == "repair-session-permissions":
            snapshot = await RuntimeMaintenanceService(db).snapshot()
            if (
                not snapshot.maintenance_active
                or snapshot.maintenance_epoch != args.maintenance_epoch
            ):
                raise RuntimeError(
                    "global Runtime maintenance ownership does not match"
                )
            receipt = await RuntimeSessionPermissionMaintenance(kubernetes).repair(
                binding=binding,
                target=target,
                maintenance_epoch=args.maintenance_epoch,
            )
            print(json.dumps(asdict(receipt), sort_keys=True))
            return 0
        if args.command == "reload":
            if getattr(args, "deferred", False) and getattr(
                args, "deferred_precreate", False
            ):
                raise RuntimeError("choose either --deferred or --deferred-precreate")
            if getattr(args, "deferred", False):
                deferred = await service.defer_runtime_reload(
                    binding,
                    target=target,
                    maintenance_epoch=args.maintenance_epoch,
                )
                print(json.dumps(asdict(deferred), sort_keys=True))
                return 0
            if getattr(args, "deferred_precreate", False):
                precreated = await service.precreate_deferred_runtime(
                    binding,
                    target=target,
                    maintenance_epoch=args.maintenance_epoch,
                )
                print(json.dumps(asdict(precreated), sort_keys=True))
                return 0
            completed = await service.reload_runtime(
                binding,
                target=target,
                maintenance_epoch=args.maintenance_epoch,
            )
            print(json.dumps(asdict(completed), sort_keys=True))
            return 0
        evidence = await _verify(
            kubernetes,
            binding,
            target=target,
            timeout=args.timeout,
            allow_absent=getattr(args, "allow_absent", False),
            precreated_zero_scale=getattr(args, "precreated_zero_scale", False),
        )
        print(json.dumps(evidence, sort_keys=True))
        return 0


async def _stop(
    kubernetes: KubectlKnativeClient,
    binding: RuntimeReloadBinding,
    *,
    timeout: float,
    delete_pods: bool = False,
    fast_test_mode: bool = False,
    task_kill_timeout: float = 3.0,
    delete_grace: float = 1.5,
) -> list[dict[str, object]]:
    if fast_test_mode:
        _require_fast_mode(delete_pods)
        # Fast mode closes task/PID identity while the exact Pod is still in
        # the API, then force-deletes the exact Pod object.  A Pod that has
        # already disappeared is handled only by the orphan sweep below.
        timeout = min(timeout, 30.0)
    if await kubernetes.kservice_exists(
        namespace=binding.namespace,
        name=binding.service_name,
    ):
        kservice_delete_kwargs = {
            "namespace": binding.namespace,
            "name": binding.service_name,
        }
        if fast_test_mode:
            kservice_delete_kwargs["wait"] = False
        await kubernetes.delete_kservice(**kservice_delete_kwargs)
    deadline = time.monotonic() + timeout
    deleted_pods: set[str] = set()
    force_receipts: list[dict[str, object]] = []
    while True:
        pods = await kubernetes.list_kservice_pods(
            namespace=binding.namespace,
            service_name=binding.service_name,
        )
        live = [
            pod
            for pod in pods
            if str((pod.get("status") or {}).get("phase") or "")
            not in {"Succeeded", "Failed"}
        ]
        if not live:
            if fast_test_mode:
                # A previous interrupted cutover may have deleted the Pod
                # object while its Supervisor task remained alive and held
                # the PVC lock.  Reap only a process whose binding, Runtime,
                # PVC identity, command and cgroup Pod marker all agree.
                force_receipts.extend(
                    await _reap_orphan_supervisor_tasks(binding, timeout=task_kill_timeout)
                )
            return force_receipts
        if delete_pods:
            for pod in live:
                metadata = pod.get("metadata") or {}
                pod_name = str(metadata.get("name") or "")
                if not pod_name:
                    raise RuntimeError("Runtime Pod identity is incomplete")
                if pod_name in deleted_pods:
                    continue
                if fast_test_mode:
                    # Close the container/task identity while the exact Pod
                    # still exists in the API.  Once the API object is gone,
                    # this path must never inspect host ctr/proc again.
                    # A freshly-created replacement Pod can briefly have no
                    # user-container status yet.  It has no task identity to
                    # terminate; API force deletion is sufficient and keeps
                    # the fleet moving without unsafe host guessing.
                    if _has_user_container_identity(pod):
                        force_receipts.append(
                            await _force_terminate_pod_task(
                                pod,
                                binding,
                                timeout=task_kill_timeout,
                                identity_revalidated=True,
                            )
                        )
                    else:
                        force_receipts.append(
                            _identity_incomplete_receipt(pod)
                        )
                delete_kwargs = {
                    "namespace": binding.namespace,
                    "name": pod_name,
                    "grace_period_seconds": 0 if fast_test_mode else 1,
                }
                if fast_test_mode:
                    delete_kwargs["wait"] = False
                    delete_kwargs["force"] = True
                await kubernetes.delete_pod(**delete_kwargs)
                deleted_pods.add(pod_name)
        if time.monotonic() >= deadline:
            raise RuntimeError("Runtime Pods did not reach strict zero")
        await asyncio.sleep(0.5 if fast_test_mode else 1)


def _require_fast_mode(delete_pods: bool) -> None:
    if not delete_pods:
        raise RuntimeError("fast mode requires --delete-pods")


def _has_user_container_identity(pod: dict[str, object]) -> bool:
    """Return whether the API object has a concrete user-container ID."""
    status = pod.get("status") or {}
    container_statuses = status.get("containerStatuses") or []
    if not isinstance(container_statuses, list):
        return False
    for item in container_statuses:
        if not isinstance(item, dict) or item.get("name") != USER_CONTAINER_NAME:
            continue
        container_id = str(item.get("containerID") or "").removeprefix("containerd://")
        return bool(re.fullmatch(r"[0-9a-f]{12,128}", container_id))
    return False


def _identity_incomplete_receipt(pod: dict[str, object]) -> dict[str, object]:
    metadata = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    return {
        "pod_uid": str(metadata.get("uid") or ""),
        "node": str(spec.get("nodeName") or ""),
        "container_id": None,
        "pid": None,
        "signal_method": "api-force-no-user-container",
        "escalated": False,
    }


async def _reap_orphan_supervisor_tasks(
    binding: RuntimeReloadBinding,
    *,
    timeout: float,
) -> list[dict[str, object]]:
    """Terminate exact Supervisor tasks left after a Pod API object vanished."""
    receipts: list[dict[str, object]] = []
    expected = {
        "RUNTIME_BINDING_ID": str(binding.binding_id),
        "RUNTIME_ID": binding.runtime_id,
        "RUNTIME_PVC_STORAGE_IDENTITY_HASH": binding.pvc_storage_identity_hash,
    }
    for proc_dir in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(proc_dir.name)
            command = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            ).strip()
            if "platform-supervisor" not in command:
                continue
            raw_env = (proc_dir / "environ").read_bytes().split(b"\0")
            env = {}
            for item in raw_env:
                key, separator, value = item.partition(b"=")
                if separator:
                    env[key.decode("utf-8", errors="ignore")] = value.decode(
                        "utf-8", errors="ignore"
                    )
            if any(env.get(key) != value for key, value in expected.items()):
                continue
            pod_uid = env.get("HERMES_SUPERVISOR_POD_UID", "")
            if not pod_uid:
                continue
            cgroup_result, cgroup_text = await _read_cgroup_or_confirm_task_exit(
                pid,
                None,
            )
            if cgroup_result != "verified":
                # This is a discovery sweep.  A candidate that exits or has
                # unreadable identity is skipped rather than blocking other
                # exact candidates; the single-writer gate still catches a
                # residual task or lock before a new Pod can write the PVC.
                continue
            assert cgroup_text is not None
            if pod_uid.replace("-", "_") not in cgroup_text:
                continue
            match = re.search(r"cri-containerd-([0-9a-f]{12,128})\.scope", cgroup_text)
            if not match:
                continue
            container_id = match.group(1)
            lock_evidence = _find_runtime_lock_fds(pid)
            await _host_command(
                [
                    "ctr",
                    "-n",
                    CONTAINERD_NAMESPACE,
                    "task",
                    "kill",
                    "--signal",
                    "TERM",
                    container_id,
                ],
                timeout=3.0,
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
                await asyncio.sleep(0.2)
            escalated = Path(f"/proc/{pid}").exists()
            if escalated:
                await _host_command(
                    [
                        "ctr",
                        "-n",
                        CONTAINERD_NAMESPACE,
                        "task",
                        "kill",
                        "--signal",
                        "KILL",
                        container_id,
                    ],
                    timeout=3.0,
                )
            receipts.append(
                {
                    "pod_uid": pod_uid,
                    "node": socket.gethostname(),
                    "container_id": container_id,
                    "pid": pid,
                    "signal_method": "orphan-ctr-task",
                    "lock_evidence": lock_evidence,
                    "inspect_method": "proc-cgroup",
                    "escalated": escalated,
                    "identity_revalidated": True,
                }
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return receipts


async def _force_terminate_pod_task(
    pod: dict[str, object],
    binding: RuntimeReloadBinding,
    *,
    timeout: float,
    identity_revalidated: bool = False,
) -> dict[str, object]:
    """Terminate one stuck task only after Pod/container/cgroup identity closes."""
    if not identity_revalidated:
        raise RuntimeError("Pod deletion identity was not revalidated")
    metadata = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}
    pod_uid = str(metadata.get("uid") or "")
    node_name = str(spec.get("nodeName") or "")
    container_id = ""
    container_statuses = status.get("containerStatuses") or []
    for item in container_statuses:
        if not isinstance(item, dict):
            continue
        if item.get("name") == USER_CONTAINER_NAME or not container_id:
            raw_id = str(item.get("containerID") or "")
            if raw_id:
                container_id = raw_id.removeprefix("containerd://")
        if item.get("name") == USER_CONTAINER_NAME:
            break
    if not pod_uid or not node_name or not container_id:
        raise RuntimeError("stuck Runtime Pod identity is incomplete; refusing host termination")
    if not _local_node_matches(node_name):
        raise RuntimeError(
            f"stuck Runtime Pod node {node_name!r} is not this host; refusing remote termination"
        )
    if not re.fullmatch(r"[0-9a-f]{12,128}", container_id):
        raise RuntimeError("container ID is malformed; refusing host termination")

    cgroup_path = ""
    if shutil.which("crictl"):
        inspect = await _host_json(
            ["crictl", "inspect", "-o", "json", container_id], timeout=3.0
        )
        info = inspect.get("info") if isinstance(inspect, dict) else None
        if not isinstance(info, dict):
            raise RuntimeError("crictl inspect returned no container info")
        pid = int(info.get("pid") or 0)
        runtime_spec = info.get("runtimeSpec")
        if isinstance(runtime_spec, dict):
            linux_spec = runtime_spec.get("linux")
            if isinstance(linux_spec, dict):
                cgroup_path = str(linux_spec.get("cgroupsPath") or "")
        inspect_method = "crictl"
    else:
        task_list = await _host_command(
            ["ctr", "-n", CONTAINERD_NAMESPACE, "tasks", "ls"], timeout=3.0
        )
        try:
            pid = _ctr_task_pid(task_list, container_id)
        except RuntimeError as exc:
            if "not present" not in str(exc):
                raise
            # The exact Pod UID and container ID were revalidated immediately
            # before this lookup; a task disappearing in this small race is
            # already terminated and must not turn into a fleet failure.
            return {
                "pod_uid": pod_uid,
                "node": node_name,
                "container_id": container_id,
                "pid": None,
                "signal_method": "ctr-task-absent",
                "lock_evidence": [],
                "inspect_method": "ctr",
                "escalated": False,
                "identity_revalidated": identity_revalidated,
            }
        inspect_method = "ctr"
    if pid <= 1:
        raise RuntimeError("container task has no safe PID")
    cgroup_result, cgroup_text = await _read_cgroup_or_confirm_task_exit(
        pid,
        container_id,
    )
    if cgroup_result != "verified":
        return {
            "pod_uid": pod_uid,
            "node": node_name,
            "container_id": container_id,
            "pid": None,
            "signal_method": cgroup_result,
            "lock_evidence": [],
            "inspect_method": inspect_method,
            "escalated": False,
            "identity_revalidated": identity_revalidated,
        }
    assert cgroup_text is not None
    identity_markers = (pod_uid, container_id, cgroup_path)
    if not any(marker and marker in cgroup_text for marker in identity_markers):
        raise RuntimeError("PID cgroup does not prove the requested Pod/container identity")
    lock_evidence = _find_runtime_lock_fds(pid)

    try:
        task_list = await _host_command(
            ["ctr", "-n", CONTAINERD_NAMESPACE, "tasks", "ls", "-q"], timeout=3.0
        )
        task_present = container_id in {line.strip() for line in task_list.splitlines()}
    except RuntimeError:
        # containerd can transiently stall while several Pods are deleted in
        # parallel.  The PID/cgroup identity above is already exact, so use
        # the same verified process path instead of aborting the whole fleet.
        task_present = False
    signal_method = "ctr-task" if task_present else "verified-pid"
    if task_present:
        await _host_command(
            [
                "ctr",
                "-n",
                CONTAINERD_NAMESPACE,
                "task",
                "kill",
                "--signal",
                "TERM",
                container_id,
            ],
            timeout=3.0,
        )
    else:
        _signal_verified_pid(pid, cgroup_text, pod_uid, container_id, signal.SIGTERM)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not await _host_target_present(container_id, pid, cgroup_text):
            return {
                "pod_uid": pod_uid,
                "node": node_name,
                "container_id": container_id,
                "pid": pid,
                "signal_method": signal_method,
                "lock_evidence": lock_evidence,
                "inspect_method": inspect_method,
                "escalated": False,
                "identity_revalidated": identity_revalidated,
            }
        await asyncio.sleep(0.2)

    if not await _host_target_present(container_id, pid, cgroup_text):
        task_still_present = False
    else:
        try:
            task_still_present = container_id in {
                line.strip()
                for line in (
                    await _host_command(
                        ["ctr", "-n", CONTAINERD_NAMESPACE, "tasks", "ls", "-q"],
                        timeout=5.0,
                    )
                ).splitlines()
            }
        except RuntimeError:
            task_still_present = False
    if task_still_present:
        try:
            await _host_command(
                [
                    "ctr",
                    "-n",
                    CONTAINERD_NAMESPACE,
                    "task",
                    "kill",
                    "--signal",
                    "KILL",
                    container_id,
                ],
                timeout=3.0,
            )
        except RuntimeError:
            # A fast-exiting Runtime Supervisor can terminate its container
            # task between ``tasks ls`` and ``task kill``. Confirm the exact
            # task is gone before treating this as a cutover failure.
            task_list = await _host_command(
                ["ctr", "-n", CONTAINERD_NAMESPACE, "tasks", "ls", "-q"],
                timeout=3.0,
            )
            if container_id in {line.strip() for line in task_list.splitlines()}:
                raise
    else:
        try:
            _signal_verified_pid(
                pid, cgroup_text, pod_uid, container_id, signal.SIGKILL
            )
        except RuntimeError:
            # Same race as above: the verified PID may be reaped right before
            # its cgroup is re-read. Only accept the exit when both /proc and
            # the containerd task list prove the exact task is gone.
            status, _ = await _read_cgroup_or_confirm_task_exit(
                pid,
                container_id,
            )
            if status not in {"proc-absent", "proc-race-task-gone"}:
                raise
    return {
        "pod_uid": pod_uid,
        "node": node_name,
        "container_id": container_id,
        "pid": pid,
        "signal_method": signal_method,
        "lock_evidence": lock_evidence,
        "inspect_method": inspect_method,
        "escalated": True,
        "identity_revalidated": identity_revalidated,
    }


def _ctr_task_pid(task_list: str, container_id: str) -> int:
    """Resolve one exact container task PID from ``ctr tasks ls`` output."""
    for line in task_list.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0] != container_id:
            continue
        try:
            return int(fields[1])
        except ValueError as exc:
            raise RuntimeError("ctr task PID is malformed") from exc
    raise RuntimeError("container task is not present in ctr task list")


def _local_node_matches(node_name: str) -> bool:
    configured = os.environ.get("HERMES_RUNTIME_TEST_NODE_NAME")
    candidates = {socket.gethostname(), socket.gethostname().split(".", 1)[0]}
    try:
        candidates.add(Path("/etc/hostname").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    if configured:
        candidates.add(configured)
    return node_name in {candidate for candidate in candidates if candidate}


def _read_proc_cgroup(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read cgroup for verified PID {pid}") from exc


async def _read_cgroup_or_confirm_task_exit(
    pid: int,
    container_id: str | None,
) -> tuple[str, str | None]:
    """Read a verified PID cgroup or classify a concurrent task exit.

    ``ctr tasks ls`` and ``/proc`` are independent snapshots.  A task can be
    reaped after its PID was listed but before the cgroup file is read.  Retry
    the proc read only for a few milliseconds, then perform one exact task
    lookup when ``container_id`` is provided.  If the task is gone, no PID
    signal is needed; if it remains, the original identity error is preserved
    and the caller fails closed.  Discovery callers pass ``None`` and skip a
    candidate whose identity remains unreadable.
    """
    last_error: RuntimeError | None = None
    for attempt in range(PROC_CGROUP_RETRIES + 1):
        try:
            return "verified", _read_proc_cgroup(pid)
        except RuntimeError as exc:
            last_error = exc
            if not Path(f"/proc/{pid}").exists():
                return "proc-absent", None
            if attempt < PROC_CGROUP_RETRIES:
                await asyncio.sleep(PROC_CGROUP_RETRY_DELAY_SECONDS)

    if container_id is None:
        return "cgroup-unreadable", None
    try:
        task_list = await _host_command(
            ["ctr", "-n", CONTAINERD_NAMESPACE, "tasks", "ls", "-q"],
            timeout=3.0,
        )
    except RuntimeError:
        # A live PID whose cgroup cannot be read remains unsafe.  Do not turn
        # an unavailable final check into permission to signal a host PID.
        assert last_error is not None
        raise last_error
    if container_id not in {line.strip() for line in task_list.splitlines()}:
        return "proc-race-task-gone", None
    assert last_error is not None
    raise last_error


def _find_runtime_lock_fds(pid: int) -> list[str]:
    evidence: list[str] = []
    for fd in Path(f"/proc/{pid}/fd").glob("*"):
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.endswith("/runtime.lock") or target.endswith("/runtime.lock (deleted)"):
            evidence.append(target)
    return evidence


def _signal_verified_pid(
    pid: int,
    cgroup_text: str,
    pod_uid: str,
    container_id: str,
    sig: signal.Signals,
) -> None:
    current_cgroup = _read_proc_cgroup(pid)
    if current_cgroup != cgroup_text or not any(
        marker in current_cgroup for marker in (pod_uid, container_id)
    ):
        raise RuntimeError("PID identity changed before signal; refusing to kill")
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return


async def _host_json(command: list[str], *, timeout: float) -> dict[str, object]:
    output = await _host_command(command, timeout=timeout)
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError("host inspection returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("host inspection returned a non-object")
    return payload


async def _host_command(command: list[str], *, timeout: float) -> str:
    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    try:
        result = await asyncio.to_thread(run)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"host inspection command failed: {command[0]}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"host inspection command failed: {command[0]}")
    return result.stdout


async def _host_target_present(
    container_id: str,
    pid: int,
    expected_cgroup: str,
) -> bool:
    """Use the already-verified PID/cgroup for cheap termination polling.

    ``ctr tasks ls`` can block while containerd is reconciling many Pods.  The
    PID is accepted only while its cgroup remains byte-for-byte identical and
    still contains the exact container ID, preventing PID reuse from being
    mistaken for the target task.  A final ctr lookup is retained only when
    the verified PID is still alive after the TERM/KILL window.
    """
    proc_path = Path(f"/proc/{pid}")
    if not proc_path.exists():
        return False
    try:
        current_cgroup = _read_proc_cgroup(pid)
    except RuntimeError:
        return False
    if current_cgroup != expected_cgroup or container_id not in current_cgroup:
        return False
    try:
        stat_fields = (proc_path / "stat").read_text(encoding="utf-8")
        process_state = stat_fields.rsplit(")", 1)[1].strip().split(" ", 1)[0]
    except (OSError, IndexError):
        return False
    return process_state != "Z"


async def _audit_fleet(
    kubernetes: KubectlKnativeClient,
    service: RuntimeReloadService,
    bindings: list[RuntimeReloadBinding],
    *,
    allow_unavailable: bool = False,
) -> dict[str, object]:
    """Reject DB drift and Runtime-labelled resources outside the closed fleet."""
    current = await service.list_bindings()
    expected_by_key = {
        (item.binding_type, item.binding_id, item.runtime_id): item for item in bindings
    }

    current_by_key = {
        (item.binding_type, item.binding_id, item.runtime_id): item for item in current
    }
    expected_keys = set(expected_by_key)
    current_keys = set(current_by_key)
    missing_keys = expected_keys - current_keys
    unavailable_keys = current_keys - expected_keys
    if missing_keys or (
        unavailable_keys
        and (
            not allow_unavailable
            or any(current_by_key[key].status == "ready" for key in unavailable_keys)
        )
    ):
        raise RuntimeError("closed Runtime Binding list no longer matches Backend DB")
    for key, expected in expected_by_key.items():
        observed = current_by_key[key]
        if (
            observed.namespace != expected.namespace
            or observed.service_name != expected.service_name
            or observed.pvc_name != expected.pvc_name
            or observed.pvc_uid != expected.pvc_uid
            or observed.pvc_storage_identity_hash
            != expected.pvc_storage_identity_hash
        ):
            raise RuntimeError("closed Runtime Binding identity changed in Backend DB")

    audited_bindings = current if allow_unavailable else bindings
    expected_services = {
        (item.namespace, item.service_name): item for item in audited_bindings
    }
    observed_services: set[tuple[str, str]] = set()
    observed_pods = 0
    for namespace in sorted({item.namespace for item in audited_bindings}):
        for kservice in await kubernetes.list_runtime_kservices(namespace=namespace):
            metadata = kservice.get("metadata") or {}
            labels = metadata.get("labels") or {}
            name = str(metadata.get("name") or "")
            key = (namespace, name)
            expected = expected_services.get(key)
            if expected is None:
                raise RuntimeError("unknown Runtime KService exists outside closed fleet")
            if (
                labels.get("hermes.platform/runtime-id") != expected.runtime_id
                or labels.get("hermes.platform/runtime-binding-type")
                != expected.binding_type
                or labels.get("hermes.platform/runtime-binding-id")
                != str(expected.binding_id)
            ):
                raise RuntimeError("Runtime KService labels do not match Backend Binding")
            observed_services.add(key)
        expected_by_runtime = {
            item.runtime_id: item
            for item in audited_bindings
            if item.namespace == namespace
        }
        for pod in await kubernetes.list_runtime_pods(namespace=namespace):
            metadata = pod.get("metadata") or {}
            labels = metadata.get("labels") or {}
            expected = expected_by_runtime.get(
                str(labels.get("hermes.platform/runtime-id") or "")
            )
            if expected is None:
                raise RuntimeError("unknown Runtime Pod exists outside closed fleet")
            if (
                labels.get("hermes.platform/runtime-binding-type")
                != expected.binding_type
                or labels.get("hermes.platform/runtime-binding-id")
                != str(expected.binding_id)
                or labels.get("serving.knative.dev/service")
                != expected.service_name
            ):
                raise RuntimeError("Runtime Pod labels do not match Backend Binding")
            observed_pods += 1
    return {
        "binding_count": len(bindings),
        "unavailable_binding_count": len(unavailable_keys),
        "live_kservice_count": len(observed_services),
        "runtime_pod_count": observed_pods,
        "closed": True,
    }


async def _verify_session_receipts(
    client: RuntimeCommandClient,
    *,
    target,
    receipts: object,
    legacy_orphans: object = (),
) -> dict[str, object]:
    """Confirm canceled Runs still expose their original Session after reload."""

    if not isinstance(receipts, list):
        raise RuntimeError("active Run cancellation receipt is unavailable")
    checked: list[dict[str, object]] = []
    for item in receipts:
        if not isinstance(item, dict):
            raise RuntimeError("active Run cancellation receipt is invalid")
        run_id = str(item.get("run_id") or "")
        session_id = str(item.get("session_id") or "")
        if not run_id or not session_id:
            raise RuntimeError("active Run cancellation receipt lacks identity")
        metadata_reader = getattr(client, "get_run_metadata", None)
        snapshot = await (
            metadata_reader(target=target, run_id=run_id)
            if callable(metadata_reader)
            else client.get_run(target=target, run_id=run_id)
        )
        observed_session_id = (
            str(snapshot.get("session_id") or "") if snapshot else ""
        ) or _snapshot_session_id(snapshot)
        if snapshot is None or (
            observed_session_id
            and observed_session_id != session_id
        ):
            raise RuntimeError("original Session is not recoverable after Runtime reload")
        status = str(snapshot.get("status") or "").upper()
        if status not in {
            "CANCELED",
            "FAILED",
            "RUN_INTERRUPTED_BY_RUNTIME_RESTART",
            "RUN_INTERRUPTED_BY_RUNTIME_TERMINATION",
            "RUN_INTERRUPTED_BY_ADMIN_RESTART",
        }:
            raise RuntimeError("canceled Run is not terminal after Runtime reload")
        checked.append(
            {
                "run_id": run_id,
                "session_id": session_id,
                "status": status,
                "history_event_count": int(
                    snapshot.get("event_count")
                    if "event_count" in snapshot
                    else len(snapshot.get("events") or [])
                ),
            }
        )
    orphan_checked: list[dict[str, object]] = []
    if not isinstance(legacy_orphans, list):
        raise RuntimeError("legacy Run cancellation receipt is invalid")
    for item in legacy_orphans:
        if not isinstance(item, dict):
            raise RuntimeError("legacy Run cancellation receipt is invalid")
        run_id = str(item.get("run_id") or "")
        session_id = str(item.get("session_id") or "")
        if not run_id or not session_id:
            raise RuntimeError("legacy Run cancellation receipt lacks identity")
        metadata_reader = getattr(client, "get_run_metadata", None)
        snapshot = await (
            metadata_reader(target=target, run_id=run_id)
            if callable(metadata_reader)
            else client.get_run(target=target, run_id=run_id)
        )
        observed_session_id = (
            str(snapshot.get("session_id") or "") if snapshot else ""
        ) or _snapshot_session_id(snapshot)
        if snapshot is None or (
            observed_session_id
            and observed_session_id != session_id
        ):
            raise RuntimeError("legacy orphan Run Session is not recoverable")
        status = str(snapshot.get("status") or "").upper()
        if status not in {
            "CANCELED",
            "FAILED",
            "FAILED_SUBMISSION_UNCERTAIN",
            "RUN_INTERRUPTED_BY_RUNTIME_RESTART",
            "RUN_INTERRUPTED_BY_RUNTIME_TERMINATION",
            "RUN_INTERRUPTED_BY_ADMIN_RESTART",
            "COMPLETED",
        }:
            raise RuntimeError("legacy orphan Run is still non-terminal after reload")
        orphan_checked.append(
            {"run_id": run_id, "session_id": session_id, "status": status}
        )
    return {
        "checked": checked,
        "legacy_orphans": orphan_checked,
        "session_history_verified": True,
    }


def _snapshot_session_id(snapshot: object) -> str:
    """Read Session identity from either current or legacy snapshot shape."""

    if not isinstance(snapshot, dict):
        return ""
    direct = str(snapshot.get("session_id") or "")
    if direct:
        return direct
    events = snapshot.get("events")
    if not isinstance(events, list):
        return ""
    for event in events:
        if not isinstance(event, dict):
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        session_id = str(data.get("session_id") or "")
        if session_id:
            return session_id
    return ""


async def _legacy_cancel_active_runs(
    client: RuntimeCommandClient,
    *,
    target,
    binding: RuntimeReloadBinding,
) -> dict[str, object]:
    """Best-effort cancellation for Supervisors predating batch cancel.

    The query is read-only and returns only Run/Session identities.  Every
    returned Run is sent through the old ``cancel`` command.  A 500 from the
    legacy endpoint is treated as an orphan only when the endpoint reports
    ``RunNotFound``; the receipt is retained for post-reload verification.
    """

    pods = await asyncio.to_thread(
        subprocess.run,
        [
            "kubectl",
            "get",
            "pods",
            "-n",
            binding.namespace,
            "-l",
            f"serving.knative.dev/service={binding.service_name}",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    pod_name = pods.stdout.strip()
    if pods.returncode != 0 or not pod_name:
        raise RuntimeError("legacy cancel could not resolve the Runtime Pod")
    query = (
        "import json,sqlite3; c=sqlite3.connect('/data/platform/supervisor.db'); "
        "q=\"select run_id,session_id,state from runs where state not in "
        "('COMPLETED','FAILED','CANCELED','FAILED_SUBMISSION_UNCERTAIN',"
        "'RUN_INTERRUPTED_BY_RUNTIME_RESTART','RUN_INTERRUPTED_BY_RUNTIME_TERMINATION',"
        "'RUN_INTERRUPTED_BY_ADMIN_RESTART')\"; "
        "print(json.dumps([{'run_id':r[0],'session_id':r[1],'state':r[2]} "
        "for r in c.execute(q)]))"
    )
    queried = await asyncio.to_thread(
        subprocess.run,
        [
            "kubectl",
            "exec",
            "-n",
            binding.namespace,
            pod_name,
            "-c",
            USER_CONTAINER_NAME,
            "--",
            "su",
            "hermes",
            "-s",
            "/bin/sh",
            "-c",
            "/opt/hermes/.venv/bin/python -c " + shlex.quote(query),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if queried.returncode != 0:
        raise RuntimeError("legacy cancel could not inspect the Runtime journal")
    try:
        rows = json.loads(queried.stdout.strip() or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("legacy Runtime journal returned invalid identity data") from exc
    if not isinstance(rows, list):
        raise RuntimeError("legacy Runtime journal identity data is invalid")
    canceled: list[dict[str, str]] = []
    orphans: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        run_id = str(row.get("run_id") or "")
        session_id = str(row.get("session_id") or "")
        if not run_id or not session_id:
            continue
        try:
            ack = await client.cancel_run(target=target, run_id=run_id)
            if ack.state != "completed":
                raise RuntimeError("legacy Run cancellation was not completed")
            canceled.append({"run_id": run_id, "session_id": session_id})
        except RuntimeCommandError as exc:
            if "HTTP 500" not in str(exc) or "RunNotFound" not in str(exc):
                raise
            orphans.append(
                {
                    "run_id": run_id,
                    "session_id": session_id,
                    "state": str(row.get("state") or ""),
                }
            )
    return {
        "runs": canceled,
        "legacy_protocol": True,
        "legacy_checked_count": len(rows),
        "legacy_orphans": orphans,
    }


async def _verify(
    kubernetes: KubectlKnativeClient,
    binding: RuntimeReloadBinding,
    *,
    target,
    timeout: float,
    allow_absent: bool = False,
    precreated_zero_scale: bool = False,
) -> dict[str, object]:
    pvc = await kubernetes.get_persistent_volume_claim(
        namespace=binding.namespace,
        name=binding.pvc_name,
    )
    pvc_uid = str((pvc.get("metadata") or {}).get("uid") or "")
    if (
        pvc_uid != binding.pvc_uid
        or runtime_pvc_storage_identity_hash(
            namespace=binding.namespace,
            pvc_uid=pvc_uid,
        )
        != binding.pvc_storage_identity_hash
    ):
        raise RuntimeError("live PVC identity does not match Binding")
    if allow_absent and not await kubernetes.kservice_exists(
        namespace=binding.namespace,
        name=binding.service_name,
    ):
        if (
            binding.image != target.image
            or binding.image_digest != target.config_id
            or binding.status != "ready"
        ):
            raise RuntimeError("deferred Runtime Binding does not own the target identity")
        return {
            "runtime_id": binding.runtime_id,
            "binding_type": binding.binding_type,
            "binding_id": binding.binding_id,
            "pvc_uid": pvc_uid,
            "activation": "deferred_to_next_login",
            "live_image_ids": [],
            "ready": True,
        }
    kservice = await kubernetes.get_kservice(
        namespace=binding.namespace,
        name=binding.service_name,
    )
    conditions = (kservice.get("status") or {}).get("conditions") or []
    if not any(
        item.get("type") == "Ready" and item.get("status") == "True"
        for item in conditions
        if isinstance(item, dict)
    ):
        raise RuntimeError("KService is not Ready")
    template = (kservice.get("spec") or {}).get("template") or {}
    pod_spec = template.get("spec") or {}
    containers = pod_spec.get("containers") or []
    if len(containers) != 1 or containers[0].get("image") != target.image:
        raise RuntimeError("KService image does not match target")
    env = {item.get("name"): item.get("value") for item in containers[0].get("env") or []}
    if env.get("HERMES_STARTUP_MODE") != "steady_state":
        raise RuntimeError("KService startup mode is not steady_state")
    if not env.get("RUNTIME_INITIAL_CONFIG_DOWNLOAD_URL") or not env.get(
        "RUNTIME_INITIAL_CONFIG_DIGEST"
    ):
        raise RuntimeError("KService current ConfigBundle identity is incomplete")
    annotations = (template.get("metadata") or {}).get("annotations") or {}
    if annotations.get("hermes.platform/pvc-storage-identity-hash") != (
        binding.pvc_storage_identity_hash
    ):
        raise RuntimeError("KService PVC annotation does not match Binding")
    control_key_version = annotations.get("hermes.platform/control-key-version")
    if precreated_zero_scale:
        status = kservice.get("status") or {}
        if status.get("latestCreatedRevisionName") != status.get(
            "latestReadyRevisionName"
        ):
            raise RuntimeError("precreated KService Revision is not Ready")
        if annotations.get("autoscaling.knative.dev/min-scale") != "0":
            raise RuntimeError("precreated KService min-scale is not zero")
        if annotations.get("autoscaling.knative.dev/initial-scale") != "0":
            raise RuntimeError("precreated KService initial-scale is not zero")
        if binding.image != target.image or binding.image_digest != target.config_id:
            raise RuntimeError("precreated Runtime Binding does not own target identity")
        pods = await kubernetes.list_kservice_pods(
            namespace=binding.namespace,
            service_name=binding.service_name,
        )
        if pods:
            raise RuntimeError("precreated KService must not have a Pod")
        return {
            "runtime_id": binding.runtime_id,
            "binding_type": binding.binding_type,
            "binding_id": binding.binding_id,
            "pvc_uid": pvc_uid,
            "activation": "deferred_zero_scale_to_next_login",
            "live_image_ids": [],
            "ready": True,
        }
    secret = await kubernetes.get_secret(
        namespace=binding.namespace,
        name=platform_control_secret_name(binding.service_name),
    )
    encoded = (secret.get("data") or {}).get(PLATFORM_CONTROL_KEY_FILE_NAME)
    if not isinstance(encoded, str):
        raise RuntimeError("platform-control Secret document is unavailable")
    document = json.loads(base64.b64decode(encoded, validate=True))
    if (
        document.get("runtime_id") != binding.runtime_id
        or document.get("binding_type") != binding.binding_type
        or int(document.get("binding_id") or 0) != binding.binding_id
        or document.get("pvc_storage_identity_hash") != binding.pvc_storage_identity_hash
    ):
        raise RuntimeError("platform-control Secret identity does not match Binding")
    if precreated_zero_scale and str(
        document.get("current_key_version") or ""
    ) != str(control_key_version or ""):
        raise RuntimeError("platform-control key version does not match KService")

    deadline = time.monotonic() + timeout
    while True:
        pods = await kubernetes.list_kservice_pods(
            namespace=binding.namespace,
            service_name=binding.service_name,
        )
        live = [
            pod
            for pod in pods
            if not (pod.get("metadata") or {}).get("deletionTimestamp")
            and str((pod.get("status") or {}).get("phase") or "") == "Running"
        ]
        image_ids: list[str] = []
        for pod in live:
            statuses = (pod.get("status") or {}).get("containerStatuses") or []
            image_ids.extend(
                str(status.get("imageID") or "")
                for status in statuses
                if status.get("name") == "user-container"
            )
        if live and image_ids:
            observed = [require_live_image_matches_target(item, target) for item in image_ids]
            return {
                "runtime_id": binding.runtime_id,
                "binding_type": binding.binding_type,
                "binding_id": binding.binding_id,
                "pvc_uid": pvc_uid,
                "startup_mode": "steady_state",
                "live_image_ids": observed,
                "ready": True,
            }
        if allow_absent:
            return {
                "runtime_id": binding.runtime_id,
                "binding_type": binding.binding_type,
                "binding_id": binding.binding_id,
                "pvc_uid": pvc_uid,
                "activation": "scaled_to_zero",
                "live_image_ids": [],
                "ready": True,
            }
        if time.monotonic() >= deadline:
            raise RuntimeError("no Running Pod with a verified live imageID")
        await asyncio.sleep(1)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
