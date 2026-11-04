#!/usr/bin/env python3
"""One-time, fail-closed cleanup of a Runtime's obsolete ConfigBundle residue.

This tool intentionally is not part of normal reconcile or image reload.  It
requires an already isolated Binding and derives every removable path from the
interrupted, signed ConfigBundle transaction stored on that exact PVC.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.database import AsyncSessionLocal  # noqa: E402
from app.hermes_runtime.providers.knative import KubectlKnativeClient  # noqa: E402
from app.services.runtime_platform_control import (  # noqa: E402
    runtime_pvc_storage_identity_hash,
)
from app.services.runtime_reload import (  # noqa: E402
    RuntimeReloadBinding,
    RuntimeReloadService,
)


CHECKPOINT_ROOT = REPOSITORY_ROOT / ".runtime/runtime-config-residue-cleanup"
_CONFIG_STATE_NAME = "current-config.json"
_CONFIG_JOURNAL_NAME = "config-apply-journal.json"


@dataclass(frozen=True)
class CleanupPlan:
    binding: RuntimeReloadBinding
    pvc_uid: str
    local_pv_root: Path
    home: Path
    state_root: Path
    old_files: tuple[str, ...]
    current_files: tuple[str, ...]
    delete_files: tuple[str, ...]
    config_digest: str

    @property
    def path_digest(self) -> str:
        encoded = json.dumps(
            self.delete_files,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan or apply one-time obsolete Runtime ConfigBundle cleanup."
    )
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument(
        "--binding-type",
        required=True,
        choices=("personal_runtime", "application_runtime"),
    )
    parser.add_argument("--binding-id", required=True, type=int)
    parser.add_argument("--kubeconfig", default=None)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the exact legacy file difference after KService/Pod isolation.",
    )
    args = parser.parse_args()

    lock_path = REPOSITORY_ROOT / ".runtime/runtime-image-lifecycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lifecycle_lock:
        try:
            fcntl.flock(lifecycle_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "another Runtime image lifecycle operation is active"
            ) from exc
        return await _run(args)


async def _run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        binding = await RuntimeReloadService(db).resolve_binding(
            binding_type=args.binding_type,
            binding_id=args.binding_id,
        )
    if binding.runtime_id != args.runtime_id:
        raise RuntimeError("Runtime Binding does not match --runtime-id")
    if binding.status not in {"updating", "failed"}:
        raise RuntimeError("Runtime Binding is not isolated for legacy cleanup")

    kubernetes = KubectlKnativeClient(kubeconfig=args.kubeconfig)
    pvc = await kubernetes.get_persistent_volume_claim(
        namespace=binding.namespace,
        name=binding.pvc_name,
    )
    kservice = await kubernetes.get_kservice(
        namespace=binding.namespace,
        name=binding.service_name,
    )
    plan = _build_plan(
        binding=binding,
        pvc=pvc,
        kservice=kservice,
        kubeconfig=args.kubeconfig,
    )
    checkpoint_path = CHECKPOINT_ROOT / (
        f"{binding.binding_type}-{binding.runtime_id}.json"
    )
    if not args.apply:
        print(json.dumps(_evidence(plan, status="planned"), sort_keys=True))
        return 0

    _assert_not_completed(checkpoint_path, plan)
    _write_checkpoint(checkpoint_path, _evidence(plan, status="isolating"))
    await _delete_kservice_and_wait_for_claim_zero(
        kubernetes=kubernetes,
        binding=binding,
        kubeconfig=args.kubeconfig,
        timeout=args.timeout,
    )
    _write_checkpoint(checkpoint_path, _evidence(plan, status="cleaning"))
    deleted = _apply_cleanup(plan)
    evidence = _evidence(plan, status="completed")
    evidence["deleted_files"] = deleted
    _write_checkpoint(checkpoint_path, evidence)
    print(json.dumps(evidence, sort_keys=True))
    return 0


def _build_plan(
    *,
    binding: RuntimeReloadBinding,
    pvc: dict[str, object],
    kservice: dict[str, object],
    kubeconfig: str | None,
) -> CleanupPlan:
    pvc_metadata = _mapping(pvc.get("metadata"), "PVC metadata")
    pvc_uid = _string(pvc_metadata.get("uid"), "PVC UID")
    if (
        pvc_uid != binding.pvc_uid
        or runtime_pvc_storage_identity_hash(
            namespace=binding.namespace,
            pvc_uid=pvc_uid,
        )
        != binding.pvc_storage_identity_hash
    ):
        raise RuntimeError("live PVC identity does not match Binding")

    template = _mapping(
        _mapping(kservice.get("spec"), "KService spec").get("template"),
        "KService template",
    )
    metadata = _mapping(kservice.get("metadata"), "KService metadata")
    labels = _mapping(metadata.get("labels"), "KService labels")
    if (
        _string(labels.get("hermes.platform/runtime-id"), "KService runtime ID")
        != binding.runtime_id
        or _string(
            labels.get("hermes.platform/runtime-binding-type"),
            "KService binding type",
        )
        != binding.binding_type
        or _integer(
            labels.get("hermes.platform/runtime-binding-id"),
            "KService binding ID",
        )
        != binding.binding_id
    ):
        raise RuntimeError("KService identity does not match Binding")
    template_metadata = _mapping(template.get("metadata"), "KService template metadata")
    annotations = _mapping(template_metadata.get("annotations"), "KService annotations")
    if annotations.get("hermes.platform/pvc-storage-identity-hash") != (
        binding.pvc_storage_identity_hash
    ):
        raise RuntimeError("KService PVC annotation does not match Binding")

    pod_spec = _mapping(template.get("spec"), "KService Pod spec")
    container = _runtime_container(pod_spec)
    current_digest = _environment_value(
        container,
        "RUNTIME_INITIAL_CONFIG_DIGEST",
    )
    runtime_volume, home_subpath, state_subpath = _runtime_volume_layout(
        pod_spec=pod_spec,
        container=container,
        pvc_name=binding.pvc_name,
    )
    if not runtime_volume:
        raise RuntimeError("KService Runtime PVC volume is missing")
    volume_name = _string(
        _mapping(pvc.get("spec"), "PVC spec").get("volumeName"),
        "PVC volume name",
    )
    pv = _kubectl_json(["get", "pv", volume_name], kubeconfig=kubeconfig)
    local_path = _mapping(_mapping(pv.get("spec"), "PV spec").get("local"), "PV local")
    local_pv_root = Path(_string(local_path.get("path"), "PV local path"))
    if not local_pv_root.is_dir():
        raise RuntimeError("PV local path is not available on this host")
    home = _safe_child(local_pv_root, home_subpath)
    state_root = _safe_child(local_pv_root, state_subpath)
    if not home.is_dir() or not state_root.is_dir():
        raise RuntimeError("Runtime PVC home/state paths are incomplete")

    journal_path = state_root / _CONFIG_JOURNAL_NAME
    state_path = state_root / _CONFIG_STATE_NAME
    journal = _read_json_file(journal_path, "ConfigBundle apply journal")
    if journal.get("runtime_id") != binding.runtime_id or _integer(
        journal.get("binding_id"), "journal binding ID"
    ) != binding.binding_id:
        raise RuntimeError("ConfigBundle journal identity does not match Binding")
    if journal.get("config_digest") != current_digest:
        raise RuntimeError("ConfigBundle journal digest does not match KService")
    old_files = _safe_relative_paths(journal.get("old_files"), "journal old_files")
    current_files = _safe_relative_paths(journal.get("new_files"), "journal new_files")
    if not current_files:
        raise RuntimeError("current ConfigBundle file set is empty")
    current_state = _read_json_file(state_path, "Supervisor current config state")
    state_files = _safe_relative_paths(
        current_state.get("managed_files"),
        "Supervisor current managed_files",
    )
    if set(state_files) != set(old_files):
        raise RuntimeError("Supervisor current state does not close journal old_files")
    delete_files = tuple(sorted(set(old_files) - set(current_files)))
    if not delete_files:
        raise RuntimeError("legacy ConfigBundle residue is already empty")
    return CleanupPlan(
        binding=binding,
        pvc_uid=pvc_uid,
        local_pv_root=local_pv_root,
        home=home,
        state_root=state_root,
        old_files=old_files,
        current_files=current_files,
        delete_files=delete_files,
        config_digest=current_digest,
    )


def _runtime_container(pod_spec: dict[str, object]) -> dict[str, object]:
    containers = pod_spec.get("containers")
    if not isinstance(containers, list):
        raise RuntimeError("KService containers are invalid")
    matches = [
        item
        for item in containers
        if isinstance(item, dict) and item.get("name") == "user-container"
    ]
    if len(matches) != 1:
        raise RuntimeError("KService must contain exactly one user-container")
    return matches[0]


def _runtime_volume_layout(
    *,
    pod_spec: dict[str, object],
    container: dict[str, object],
    pvc_name: str,
) -> tuple[str, str, str]:
    volumes = pod_spec.get("volumes")
    mounts = container.get("volumeMounts")
    if not isinstance(volumes, list) or not isinstance(mounts, list):
        raise RuntimeError("KService Runtime volume layout is invalid")
    runtime_volumes: set[str] = set()
    for item in volumes:
        if not isinstance(item, dict):
            raise RuntimeError("KService volume is invalid")
        claim = item.get("persistentVolumeClaim")
        if isinstance(claim, dict) and claim.get("claimName") == pvc_name:
            runtime_volumes.add(_string(item.get("name"), "volume name"))
    if len(runtime_volumes) != 1:
        raise RuntimeError("KService Runtime PVC volume is ambiguous")
    runtime_volume = next(iter(runtime_volumes))
    by_path = {
        _string(item.get("mountPath"), "volume mount path"): item
        for item in mounts
        if isinstance(item, dict) and item.get("name") == runtime_volume
    }
    home_mount = _mapping(by_path.get("/opt/data"), "Runtime home mount")
    state_mount = _mapping(by_path.get("/data/platform"), "Runtime state mount")
    return (
        runtime_volume,
        _safe_subpath(home_mount.get("subPath"), "Runtime home subPath"),
        _safe_subpath(state_mount.get("subPath"), "Runtime state subPath"),
    )


async def _delete_kservice_and_wait_for_claim_zero(
    *,
    kubernetes: KubectlKnativeClient,
    binding: RuntimeReloadBinding,
    kubeconfig: str | None,
    timeout: float,
) -> None:
    if await kubernetes.kservice_exists(
        namespace=binding.namespace,
        name=binding.service_name,
    ):
        await kubernetes.delete_kservice(
            namespace=binding.namespace,
            name=binding.service_name,
        )
    deadline = time.monotonic() + timeout
    while True:
        if not _claim_pods(
            namespace=binding.namespace,
            pvc_name=binding.pvc_name,
            kubeconfig=kubeconfig,
        ) and not await kubernetes.kservice_exists(
            namespace=binding.namespace,
            name=binding.service_name,
        ):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Runtime PVC did not reach strict Pod/KService zero")
        await asyncio.sleep(1)


def _claim_pods(
    *,
    namespace: str,
    pvc_name: str,
    kubeconfig: str | None,
) -> list[str]:
    document = _kubectl_json(["get", "pods", "-A"], kubeconfig=kubeconfig)
    pods: list[str] = []
    for item in document.get("items", []):
        if not isinstance(item, dict):
            continue
        metadata = _mapping(item.get("metadata"), "Pod metadata")
        if metadata.get("namespace") != namespace:
            continue
        spec = _mapping(item.get("spec"), "Pod spec")
        volumes = spec.get("volumes")
        if not isinstance(volumes, list):
            continue
        for volume in volumes:
            if not isinstance(volume, dict):
                continue
            claim = volume.get("persistentVolumeClaim")
            if isinstance(claim, dict) and claim.get("claimName") == pvc_name:
                pods.append(_string(metadata.get("name"), "Pod name"))
                break
    return pods


def _apply_cleanup(plan: CleanupPlan) -> int:
    deleted = 0
    for relative in plan.delete_files:
        target = _safe_child(plan.home, relative)
        if target.is_symlink():
            raise RuntimeError("legacy cleanup refuses symbolic links")
        if target.exists():
            if not target.is_file():
                raise RuntimeError("legacy cleanup target is not a regular file")
            target.unlink()
            deleted += 1
    _remove_empty_ancestors(plan.home, plan.delete_files)
    _remove_regular_file(plan.state_root / _CONFIG_STATE_NAME)
    _remove_regular_file(plan.state_root / _CONFIG_JOURNAL_NAME)
    backup = plan.home / ".platform-config-backup"
    if backup.exists() or backup.is_symlink():
        _remove_safe_tree(backup, root=plan.home)
    return deleted


def _remove_empty_ancestors(home: Path, paths: tuple[str, ...]) -> None:
    candidates = {
        _safe_child(home, relative).parent
        for relative in paths
    }
    for directory in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
        current = directory
        while current != home:
            if current.is_symlink():
                raise RuntimeError("legacy cleanup refuses symbolic link directories")
            try:
                current.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                break
            current = current.parent


def _remove_regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("legacy cleanup metadata is not a regular file")
    path.unlink()


def _remove_safe_tree(path: Path, *, root: Path) -> None:
    _require_child(root, path)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("legacy cleanup backup is not a directory")
    for child in path.rglob("*"):
        if child.is_symlink():
            raise RuntimeError("legacy cleanup backup contains a symbolic link")
    shutil.rmtree(path)


def _evidence(plan: CleanupPlan, *, status: str) -> dict[str, object]:
    root_counts = Counter(item.split("/", 1)[0] for item in plan.delete_files)
    return {
        "binding_id": plan.binding.binding_id,
        "binding_type": plan.binding.binding_type,
        "config_digest": plan.config_digest,
        "contract_version": 1,
        "current_file_count": len(plan.current_files),
        "delete_file_count": len(plan.delete_files),
        "delete_path_digest": f"sha256:{plan.path_digest}",
        "delete_root_counts": dict(sorted(root_counts.items())),
        "old_file_count": len(plan.old_files),
        "pvc_name": plan.binding.pvc_name,
        "pvc_storage_identity_hash": plan.binding.pvc_storage_identity_hash,
        "pvc_uid": plan.pvc_uid,
        "runtime_id": plan.binding.runtime_id,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _assert_not_completed(path: Path, plan: CleanupPlan) -> None:
    if not path.exists():
        return
    previous = _read_json_file(path, "legacy cleanup checkpoint")
    if previous.get("status") == "completed":
        raise RuntimeError("legacy ConfigBundle cleanup already completed")
    if (
        previous.get("runtime_id") != plan.binding.runtime_id
        or previous.get("pvc_uid") != plan.pvc_uid
        or previous.get("delete_path_digest") != f"sha256:{plan.path_digest}"
    ):
        raise RuntimeError("legacy cleanup checkpoint does not match current plan")


def _write_checkpoint(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _kubectl_json(arguments: list[str], *, kubeconfig: str | None) -> dict[str, Any]:
    command = ["kubectl"]
    if kubeconfig:
        command.extend(["--kubeconfig", kubeconfig])
    command.extend([*arguments, "-o", "json"])
    completed = subprocess.run(
        command,
        cwd=BACKEND_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("kubectl query failed during legacy cleanup")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("kubectl returned invalid JSON during legacy cleanup") from exc
    if not isinstance(result, dict):
        raise RuntimeError("kubectl returned an invalid document during legacy cleanup")
    return result


def _environment_value(container: dict[str, object], name: str) -> str:
    env = container.get("env")
    if not isinstance(env, list):
        raise RuntimeError("KService Runtime environment is invalid")
    values = [
        item.get("value")
        for item in env
        if isinstance(item, dict) and item.get("name") == name
    ]
    if len(values) != 1 or not isinstance(values[0], str):
        raise RuntimeError("KService current ConfigBundle identity is incomplete")
    return values[0]


def _safe_relative_paths(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RuntimeError(f"{label} is invalid")
    normalized = tuple(sorted({_safe_subpath(item, label) for item in value}))
    if len(normalized) != len(value):
        raise RuntimeError(f"{label} contains duplicate paths")
    return normalized


def _safe_subpath(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"{label} is unsafe")
    return path.as_posix()


def _safe_child(root: Path, relative: str) -> Path:
    return _require_child(root, root.joinpath(*PurePosixPath(relative).parts))


def _require_child(root: Path, candidate: Path) -> Path:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("legacy cleanup path escapes its Runtime PVC") from exc
    return candidate


def _read_json_file(path: Path, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} is not a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is invalid") from exc
    return _mapping(payload, label)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} is invalid")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} is invalid")
    return value


def _integer(value: object, label: str) -> int:
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} is invalid") from exc
    if result <= 0:
        raise RuntimeError(f"{label} is invalid")
    return result


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
