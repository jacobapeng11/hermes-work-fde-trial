#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.services.runtime_cutover_checkpoint import RuntimeCutoverCheckpointStore  # noqa: E402
from app.services.runtime_image_config import RuntimeImageConfigStore  # noqa: E402


BACKEND_PYTHON = REPOSITORY_ROOT / "backend/.venv/bin/python"
CONTROL = REPOSITORY_ROOT / "backend/scripts/runtime_code_reload.py"
PREFLIGHT = REPOSITORY_ROOT / "backend/scripts/runtime_image_preflight.py"
CHECKPOINT_PATH = REPOSITORY_ROOT / ".runtime/runtime-code-cutover/fleet.json"
COMPOSE_EXECUTION = None
STAGE_ORDER = {
    "planned": 0,
    "frozen": 1,
    "runs_canceled": 2,
    "pods_zero": 3,
    "permissions_repaired": 4,
    "backend_restarted": 5,
    "targets_switched": 6,
    "reloaded": 7,
    "session_recovered": 8,
    "verified": 9,
    "completed": 10,
}


class RuntimeFleetCommandError(RuntimeError):
    """A non-secret per-Binding failure suitable for a fleet checkpoint."""

    def __init__(self, *, binding: dict, command: str, cause: Exception) -> None:
        super().__init__(
            "fleet Runtime command failed for "
            f"{binding['binding_type']} Binding {binding['binding_id']}"
        )
        self.binding = binding
        self.cause = cause
        self.command = command
        self.cause_type = type(cause).__name__


def main() -> int:
    global COMPOSE_EXECUTION, CHECKPOINT_PATH
    parser = argparse.ArgumentParser(
        description="Hard-reload the closed Runtime fleet."
    )
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--compose-deployment", type=Path,
        help="UAT73 deployment metadata; use Compose Backend execution instead of host lifecycle.",
    )
    parser.add_argument(
        "--stop-concurrency",
        type=int,
        default=None,
        help="独立的 Pod stop 并发数；默认取 concurrency 的两倍，上限 16。",
    )
    parser.add_argument(
        "--activation-concurrency",
        type=int,
        default=2,
        help="并发重建并激活的 Runtime 上限；缩零个人 Runtime 默认延后到下次登录。",
    )
    parser.add_argument(
        "--no-lazy",
        action="store_true",
        help="禁用缩零个人 Runtime 的懒切换，恢复全量重建与 live 验收。",
    )
    parser.add_argument(
        "--precreate-zero-scale",
        action="store_true",
        help=(
            "缩零个人 Runtime 在 reset 后预创建 Ready zero-scale KService；"
            "登录时再激活。默认保持 KService 缺席懒切换。"
        ),
    )
    parser.add_argument(
        "--backend-lifecycle",
        choices=("auto", "dev", "prod"),
        default="auto",
        help="Backend restart lifecycle; auto selects a registered public-prod Backend first.",
    )
    parser.add_argument("--supersede-failed-target", action="store_true")
    parser.add_argument(
        "--skip-unavailable",
        action="store_true",
        help="Skip non-ready Bindings whose Runtime resource is unavailable (audited separately).",
    )
    parser.add_argument(
        "--reexecute-completed",
        action="store_true",
        help="Explicitly rerun a completed cutover for the same image target.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Fast path: close exact task/PID identity, then API-force-delete exact Runtime Pods.",
    )
    parser.add_argument(
        "--repair-session-permissions",
        action="store_true",
        help="After strict fleet Pod zero, repair every managed historical Session tree.",
    )
    args = parser.parse_args()
    if args.compose_deployment:
        if args.force or args.repair_session_permissions:
            parser.error("Compose/Kind mode does not permit host task killing or permission repair")
        sys.path.insert(0, str(REPOSITORY_ROOT / "scripts/uat73"))
        from runtime_transport import ComposeFleetExecution
        COMPOSE_EXECUTION = ComposeFleetExecution(args.compose_deployment)
        CHECKPOINT_PATH = COMPOSE_EXECUTION.checkpoint
    if args.concurrency < 1 or args.concurrency > 16:
        raise ValueError("--concurrency must be between 1 and 16")
    if args.activation_concurrency < 1 or args.activation_concurrency > 8:
        raise ValueError("--activation-concurrency must be between 1 and 8")
    if getattr(args, "precreate_zero_scale", False) and getattr(args, "no_lazy", False):
        raise ValueError("--precreate-zero-scale cannot be combined with --no-lazy")
    stop_concurrency = args.stop_concurrency or min(args.concurrency * 2, 16)
    if stop_concurrency < 1 or stop_concurrency > 16:
        raise ValueError("--stop-concurrency must be between 1 and 16")
    lock_path = REPOSITORY_ROOT / ".runtime/runtime-image-lifecycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lifecycle_lock:
        try:
            fcntl.flock(lifecycle_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "another Runtime image lifecycle operation is active"
            ) from exc
        return _run(args)


def _run(args: argparse.Namespace) -> int:
    if COMPOSE_EXECUTION is not None:
        COMPOSE_EXECUTION.preflight()
    else:
        _checked([str(BACKEND_PYTHON), str(PREFLIGHT)])
    stop_concurrency = getattr(args, "stop_concurrency", None) or min(
        args.concurrency * 2, 16
    )
    target = RuntimeImageConfigStore(
        COMPOSE_EXECUTION.env_file if COMPOSE_EXECUTION is not None else REPOSITORY_ROOT / "backend/.env"
    ).read()
    if not target.manifest_digest or not target.config_id:
        raise RuntimeError("backend/.env Runtime image identity is incomplete")
    store = RuntimeCutoverCheckpointStore(CHECKPOINT_PATH)
    existing = store.load()
    target_document = {
        "image": target.image,
        "manifest_digest": target.manifest_digest,
        "config_id": target.config_id,
    }
    if (
        existing
        and existing.get("target") == target_document
        and existing.get("status") == "completed"
        and not args.repair_session_permissions
        and not getattr(args, "reexecute_completed", False)
    ):
        completed_bindings = json.loads(_control(["list"]))
        if not completed_bindings:
            raise RuntimeError("current Backend has no initialized Runtime Binding")
        _control(["audit-fleet"])
        _verify_by_plan(
            completed_bindings,
            _live_activation_plan(completed_bindings),
            args.concurrency,
        )
        print(
            json.dumps(
                {
                    "status": "completed",
                    "target_image": target.image,
                    "runtime_count": len(completed_bindings),
                    "maintenance_state": "open",
                    "checkpoint": str(CHECKPOINT_PATH if COMPOSE_EXECUTION is not None else CHECKPOINT_PATH.relative_to(REPOSITORY_ROOT)),
                },
                sort_keys=True,
            )
        )
        return 0
    if (
        getattr(args, "reexecute_completed", False)
        and existing
        and existing.get("target") == target_document
        and existing.get("status") == "completed"
        and not args.repair_session_permissions
    ):
        # Keep the old receipt as evidence, but start a fresh cutover so an
        # explicit project restart never degrades into verify-only.
        _control(["audit-fleet"])
        _archive_checkpoint(CHECKPOINT_PATH, existing, "completed")
        existing = None
    if (
        existing
        and existing.get("target") == target_document
        and existing.get("status") == "failed"
    ):
        failed_epoch = str(
            (existing.get("checks") or {}).get("maintenance_epoch") or ""
        )
        maintenance = json.loads(_control(["maintenance-status"]))
        state = str(maintenance.get("state") or "")
        active_epoch = str(maintenance.get("epoch") or "")
        if state == "open":
            print(
                "reload-all-runtimes: archiving stale failed checkpoint and starting a new maintenance epoch",
                file=sys.stderr,
            )
            _archive_checkpoint(CHECKPOINT_PATH, existing, "failed-stale")
            existing = None
        elif state == "frozen":
            if not failed_epoch:
                raise RuntimeError("failed fleet checkpoint has no maintenance ownership")
            if active_epoch != failed_epoch:
                raise RuntimeError(
                    "another Runtime maintenance epoch is active; resolve it before resuming"
                )
        elif state != "frozen":
            raise RuntimeError(
                "Runtime maintenance state is invalid; cannot resume failed fleet target"
            )
    if (
        args.repair_session_permissions
        and existing
        and existing.get("target") == target_document
        and existing.get("status") == "completed"
    ):
        _control(["audit-fleet"])
        _archive_checkpoint(CHECKPOINT_PATH, existing, "completed")
        existing = None
    preserved_epoch: str | None = None
    preserved_bindings: list[dict] | None = None
    preserved_activation_plan: dict | None = None
    if existing and existing.get("target") != target_document:
        if existing.get("status") == "completed":
            _control(["audit-fleet"])
            _archive_checkpoint(CHECKPOINT_PATH, existing, "completed")
        else:
            if not args.supersede_failed_target or existing.get("status") != "failed":
                raise RuntimeError("fleet checkpoint target conflicts with backend/.env")
            old_epoch = str(
                (existing.get("checks") or {}).get("maintenance_epoch") or ""
            )
            maintenance = json.loads(_control(["maintenance-status"]))
            state = str(maintenance.get("state") or "")
            active_epoch = str(maintenance.get("epoch") or "")
            if state == "frozen":
                if not old_epoch:
                    raise RuntimeError("failed fleet checkpoint has no maintenance ownership")
                if active_epoch != old_epoch:
                    raise RuntimeError(
                        "another Runtime maintenance epoch is active; resolve it before superseding"
                    )
                # Keep the existing maintenance window and closed Binding set.
                # This is the only case where a new image may supersede a failed
                # target while the old checkpoint is still authoritative.
                preserved_epoch = old_epoch
                preserved_bindings = list(existing.get("bindings") or [])
                old_plan = (existing.get("checks") or {}).get("activation_plan")
                if isinstance(old_plan, dict):
                    preserved_activation_plan = old_plan
            elif state != "open":
                raise RuntimeError(
                    "Runtime maintenance state is invalid; cannot supersede failed fleet target"
                )
            else:
                # The old checkpoint is stale: its epoch is no longer active.
                # Start a fresh maintenance window and take a new Binding snapshot.
                print(
                    "reload-all-runtimes: archiving stale failed checkpoint and starting a new maintenance epoch",
                    file=sys.stderr,
                )
            _archive_checkpoint(CHECKPOINT_PATH, existing, "failed")
        existing = None
    bindings = preserved_bindings or json.loads(_control(["list"]))
    if getattr(args, "skip_unavailable", False):
        unavailable = [
            binding for binding in bindings if str(binding.get("status") or "") != "ready"
        ]
        if unavailable:
            print(
                "reload-all-runtimes: skipping unavailable Bindings "
                + ", ".join(str(binding.get("binding_id")) for binding in unavailable),
                file=sys.stderr,
            )
            bindings = [
                binding for binding in bindings if str(binding.get("status") or "") == "ready"
            ]
    if not bindings:
        raise RuntimeError("no initialized Runtime Binding exists for fleet reload")
    epoch = preserved_epoch or (
        "runtime-code-"
        + target.config_id[7:19]
        + "-"
        + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    )
    checkpoint = store.initialize(flow="fleet", target=target, bindings=bindings)
    checks = dict(checkpoint.get("checks") or {})
    activation_plan = _resolve_activation_plan(
        bindings,
        checks=checks,
        preserved_plan=preserved_activation_plan,
        stage=str(checkpoint.get("stage") or "planned"),
        lazy_enabled=not getattr(args, "no_lazy", False),
    )
    if not checks.get("maintenance_epoch"):
        checkpoint = store.advance(
            stage="planned",
            checks={
                "maintenance_epoch": epoch,
                "activation_plan": activation_plan,
                "last_completed_stage": "planned",
            },
        )
    else:
        epoch = str(checks["maintenance_epoch"])
    if checkpoint.get("status") == "failed":
        _control(["assert-frozen", "--maintenance-epoch", epoch])
    stop_extra = (
        [
            "--delete-pods",
            "--fast-test-mode",
            "--timeout",
            "30",
        ]
        if getattr(args, "force", False)
        else None
    )
    last_stage = str(
        (checkpoint.get("checks") or {}).get("last_completed_stage") or "planned"
    )
    try:
        if STAGE_ORDER[last_stage] <= STAGE_ORDER["planned"]:
            _control(_fleet_audit_command(args, checkpoint=True))
            _control(
                [
                    "freeze",
                    "--maintenance-epoch",
                    epoch,
                    "--reason",
                    "runtime code fleet reload",
                ]
            )
        else:
            _control(["assert-frozen", "--maintenance-epoch", epoch])
        if STAGE_ORDER[last_stage] < STAGE_ORDER["frozen"]:
            checkpoint = _advance(store, "frozen")
            last_stage = "frozen"
        if STAGE_ORDER[last_stage] < STAGE_ORDER["runs_canceled"]:
            cancellations = {
                _binding_key(binding): json.loads(output)
                for binding, output in _parallel(
                    bindings,
                    "cancel-active",
                    args.concurrency,
                )
            }
            checkpoint = _advance(
                store,
                "runs_canceled",
                {"active_runs": cancellations},
            )
            last_stage = "runs_canceled"
        if STAGE_ORDER[last_stage] < STAGE_ORDER["pods_zero"]:
            # The historical equivalent was:
            # _parallel(bindings, "stop", args.concurrency, extra=stop_extra)
            stop_completed = set(
                (checkpoint.get("checks") or {}).get("stop_completed") or []
            )

            def record_stop(binding: dict, _output: str) -> None:
                stop_completed.add(_binding_key(binding))
                store.advance(
                    stage="runs_canceled",
                    checks={"stop_completed": sorted(stop_completed)},
                )

            pending_stop = [
                binding
                for binding in bindings
                if _binding_key(binding) not in stop_completed
            ]
            _parallel(
                pending_stop,
                "stop",
                stop_concurrency,
                extra=stop_extra,
                on_success=record_stop,
            )
            checkpoint = _advance(store, "pods_zero", {"old_pods_zero": True})
            last_stage = "pods_zero"
        if (
            args.repair_session_permissions
            and STAGE_ORDER[last_stage] < STAGE_ORDER["permissions_repaired"]
        ):
            receipts = {
                _binding_key(binding): json.loads(output)
                for binding, output in _parallel(
                    bindings,
                    "repair-session-permissions",
                    args.concurrency,
                    extra=["--maintenance-epoch", epoch],
                )
            }
            checkpoint = _advance(
                store,
                "permissions_repaired",
                {"session_permission_repairs": receipts},
            )
            last_stage = "permissions_repaired"
        if STAGE_ORDER[last_stage] < STAGE_ORDER["backend_restarted"]:
            if COMPOSE_EXECUTION is not None:
                COMPOSE_EXECUTION.restart_backend()
            else:
                _checked(_backend_restart_command(args.backend_lifecycle))
            _control(
                [
                    "freeze",
                    "--maintenance-epoch",
                    epoch,
                    "--reason",
                    "runtime code fleet reload",
                ]
            )
            checkpoint = _advance(store, "backend_restarted")
            last_stage = "backend_restarted"
        if STAGE_ORDER[last_stage] < STAGE_ORDER["targets_switched"]:
            _control(
                [
                    "switch-fleet",
                    "--maintenance-epoch",
                    epoch,
                    "--checkpoint",
                    str(CHECKPOINT_PATH),
                ]
            )
            checkpoint = _advance(store, "targets_switched")
            last_stage = "targets_switched"

        completed = set((checkpoint.get("checks") or {}).get("reload_completed") or [])
        pending: list[dict] = []
        for binding in bindings:
            key = _binding_key(binding)
            if key in completed:
                try:
                    _control([
                        "verify",
                        *_selector(binding),
                        *(
                            (
                                ["--precreated-zero-scale"]
                                if getattr(args, "precreate_zero_scale", False)
                                else ["--allow-absent"]
                            )
                            if activation_plan.get(key) == "deferred"
                            else []
                        ),
                    ])
                    continue
                except Exception:
                    completed.discard(key)
            pending.append(binding)

        def record_reload(binding: dict, _output: str) -> None:
            nonlocal checkpoint
            completed.add(_binding_key(binding))
            checkpoint = store.advance(
                stage="targets_switched",
                checks={"reload_completed": sorted(completed)},
            )

        # A failed reload may have left a replacement Pod/task behind. Re-run
        # the exact stop gate for pending bindings before retrying; never assume
        # a previous Pod-zero checkpoint still describes the live cluster.
        if checkpoint.get("status") == "failed" and STAGE_ORDER[last_stage] >= STAGE_ORDER["targets_switched"]:
            _parallel(
                pending,
                "stop",
                stop_concurrency,
                extra=stop_extra,
            )
        deferred_pending = [
            binding
            for binding in pending
            if activation_plan.get(_binding_key(binding)) == "deferred"
        ]
        eager_pending = [
            binding
            for binding in pending
            if activation_plan.get(_binding_key(binding)) != "deferred"
        ]
        _parallel(
            deferred_pending,
            "reload",
            args.concurrency,
            extra=[
                "--maintenance-epoch",
                epoch,
                (
                    "--deferred-precreate"
                    if getattr(args, "precreate_zero_scale", False)
                    else "--deferred"
                ),
            ],
            on_success=record_reload,
        )
        _parallel(
            eager_pending,
            "reload",
            args.activation_concurrency,
            extra=["--maintenance-epoch", epoch],
            on_success=record_reload,
        )
        checkpoint = _advance(
            store,
            "reloaded",
            {"reload_completed": sorted(completed)},
        )
        # A legacy Supervisor can acknowledge per-Run cancellation without
        # durably terminalizing queued/recovered rows.  Once the new image is
        # live, run the authoritative batch cancellation again while the
        # maintenance gate is still frozen, and retain both receipt sets for
        # the Session/history probe.
        post_reload_cancellations = {
            _binding_key(binding): json.loads(output)
            for binding, output in _parallel(
                [
                    binding
                    for binding in bindings
                    if activation_plan.get(_binding_key(binding)) != "deferred"
                ],
                "cancel-active",
                args.concurrency,
            )
        }
        existing_active = (checkpoint.get("checks") or {}).get("active_runs") or {}
        if isinstance(existing_active, dict):
            merged_active = dict(existing_active)
            for key, receipt in post_reload_cancellations.items():
                previous = merged_active.get(key) or {}
                previous_runs = previous.get("runs", []) if isinstance(previous, dict) else []
                current_runs = receipt.get("runs", []) if isinstance(receipt, dict) else []
                seen = {(str(item.get("run_id")), str(item.get("session_id"))) for item in previous_runs if isinstance(item, dict)}
                combined = list(previous_runs)
                for item in current_runs:
                    identity = (str(item.get("run_id")), str(item.get("session_id")))
                    if identity not in seen:
                        combined.append(item)
                        seen.add(identity)
                merged_active[key] = {**(previous if isinstance(previous, dict) else {}), **receipt, "runs": combined}
            checkpoint = _advance(store, "reloaded", {"active_runs": merged_active})
        session_evidence = {
            _binding_key(binding): json.loads(output)
            for binding, output in _parallel(
                [
                    binding
                    for binding in bindings
                    if activation_plan.get(_binding_key(binding)) != "deferred"
                ],
                "verify-sessions",
                args.concurrency,
                extra=["--checkpoint", str(CHECKPOINT_PATH)],
            )
        }
        checkpoint = _advance(
            store,
            "session_recovered",
            {"session_history": session_evidence},
        )
        evidence = {
            key: json.loads(output)
            for key, output in _verify_by_plan(
                bindings,
                activation_plan,
                args.concurrency,
                precreate_zero_scale=getattr(
                    args,
                    "precreate_zero_scale",
                    False,
                ),
            ).items()
        }
        closure = json.loads(
            _control(_fleet_audit_command(args, checkpoint=True))
        )
        checkpoint = _advance(
            store,
            "verified",
            {"technical_acceptance": evidence, "fleet_closure": closure},
        )
        _control(["unfreeze", "--maintenance-epoch", epoch])
        store.advance(
            stage="completed",
            status="completed",
            checks={"last_completed_stage": "completed", "maintenance_open": True},
        )
    except Exception as exc:
        failure_checks: dict[str, object] | None = None
        if isinstance(exc, RuntimeFleetCommandError):
            failure_checks = {
                "failed_binding": _binding_key(exc.binding),
                "failed_command": exc.command,
                "failed_error_type": exc.cause_type,
            }
        store.advance(
            stage="failed",
            status="failed",
            checks=failure_checks,
            error_summary=f"fleet Runtime reload failed: {type(exc).__name__}",
        )
        raise
    print(
        json.dumps(
            {
                "status": "completed",
                "target_image": target.image,
                "runtime_count": len(bindings),
                "maintenance_state": "open",
                "checkpoint": str(CHECKPOINT_PATH if COMPOSE_EXECUTION is not None else CHECKPOINT_PATH.relative_to(REPOSITORY_ROOT)),
            },
            sort_keys=True,
        )
    )
    return 0


def _archive_checkpoint(path: Path, document: dict, status: str) -> Path:
    config_id = str((document.get("target") or {}).get("config_id") or "unknown")
    identity = config_id.removeprefix("sha256:")[:12] or "unknown"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    archive = path.with_name(f"{path.name}.{status}-{identity}-{timestamp}")
    path.replace(archive)
    return archive


def _advance(
    store: RuntimeCutoverCheckpointStore,
    stage: str,
    checks: dict[str, object] | None = None,
) -> dict:
    return store.advance(
        stage=stage,
        checks={"last_completed_stage": stage, **(checks or {})},
    )


def _binding_key(binding: dict) -> str:
    return f"{binding['binding_type']}:{binding['binding_id']}:{binding['runtime_id']}"


def _live_activation_plan(bindings: list[dict]) -> dict[str, str]:
    classifications = json.loads(_control(["classify"]))
    running = {
        f"{item['binding_type']}:{item['binding_id']}:{item['runtime_id']}": int(
            item.get("running_pod_count") or 0
        )
        for item in classifications
        if isinstance(item, dict)
    }
    plan: dict[str, str] = {}
    for binding in bindings:
        key = _binding_key(binding)
        plan[key] = (
            "deferred"
            if binding["binding_type"] == "personal_runtime"
            and key in running
            and running[key] <= 0
            else "eager"
        )
    return plan


def _resolve_activation_plan(
    bindings: list[dict],
    *,
    checks: dict,
    preserved_plan: dict | None,
    stage: str,
    lazy_enabled: bool,
) -> dict[str, str]:
    if not lazy_enabled:
        return {_binding_key(binding): "eager" for binding in bindings}
    stored_plan = checks.get("activation_plan")
    if isinstance(stored_plan, dict):
        plan = dict(stored_plan)
        if all(plan.get(_binding_key(binding)) in {"eager", "deferred"} for binding in bindings):
            return plan
    if preserved_plan is not None and all(
        preserved_plan.get(_binding_key(binding)) in {"eager", "deferred"}
        for binding in bindings
    ):
        return {
            _binding_key(binding): preserved_plan[_binding_key(binding)]
            for binding in bindings
        }
    # Classification is only meaningful while live Pods still exist. Once the
    # fleet has passed pods_zero, an old checkpoint without a plan must keep
    # the conservative all-eager contract instead of guessing from emptiness.
    if STAGE_ORDER.get(stage, 0) >= STAGE_ORDER["pods_zero"]:
        return {_binding_key(binding): "eager" for binding in bindings}
    return _live_activation_plan(bindings)


def _verify_by_plan(
    bindings: list[dict],
    plan: dict[str, str],
    concurrency: int,
    *,
    precreate_zero_scale: bool = False,
) -> dict[str, str]:
    deferred = [
        binding
        for binding in bindings
        if plan.get(_binding_key(binding)) == "deferred"
    ]
    eager = [
        binding
        for binding in bindings
        if plan.get(_binding_key(binding)) != "deferred"
    ]
    outputs = _parallel(
        deferred,
        "verify",
        concurrency,
        extra=(
            ["--precreated-zero-scale"]
            if precreate_zero_scale
            else ["--allow-absent"]
        ),
    )
    outputs += _parallel(eager, "verify", concurrency)
    return {_binding_key(binding): output for binding, output in outputs}


def _fleet_audit_command(
    args: argparse.Namespace,
    *,
    checkpoint: bool,
) -> list[str]:
    command = ["audit-fleet"]
    if checkpoint:
        command.extend(["--checkpoint", str(CHECKPOINT_PATH)])
    if getattr(args, "skip_unavailable", False):
        command.append("--allow-unavailable")
    return command


def _selector(binding: dict) -> list[str]:
    return [
        "--binding-type",
        str(binding["binding_type"]),
        "--binding-id",
        str(binding["binding_id"]),
    ]


def _parallel(
    bindings: list[dict],
    command: str,
    concurrency: int,
    *,
    extra: list[str] | None = None,
    on_success=None,
) -> list[tuple[dict, str]]:
    if not bindings:
        return []
    results: list[tuple[dict, str]] = []
    iterator = iter(bindings)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures: dict[object, dict] = {}

        def fill_slots() -> None:
            while len(futures) < concurrency:
                try:
                    binding = next(iterator)
                except StopIteration:
                    return
                future = executor.submit(
                    _control, [command, *_selector(binding), *(extra or [])]
                )
                futures[future] = binding

        fill_slots()
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            failures: list[RuntimeFleetCommandError] = []
            for future in done:
                binding = futures.pop(future)
                try:
                    output = future.result()
                except Exception as exc:
                    failures.append(
                        RuntimeFleetCommandError(
                            binding=binding,
                            command=command,
                            cause=exc,
                        )
                    )
                    continue
                results.append((binding, output))
                if on_success is not None:
                    on_success(binding, output)
            if failures:
                for future in futures:
                    future.cancel()
                raise failures[0] from failures[0].cause
            fill_slots()
    return results


def _control(arguments: list[str]) -> str:
    if COMPOSE_EXECUTION is not None:
        return COMPOSE_EXECUTION.control(arguments)
    return _checked([str(BACKEND_PYTHON), str(CONTROL), *arguments])


def _backend_restart_command(lifecycle: str) -> list[str]:
    if lifecycle == "prod":
        return [str(REPOSITORY_ROOT / "scripts/restart-public-prod.sh")]
    if lifecycle == "dev":
        return [
            str(REPOSITORY_ROOT / "scripts/restart-public-backend-dev.sh"),
            "--from-prod",
        ]
    formal_status = subprocess.run(
        [str(REPOSITORY_ROOT / "scripts/status-public-prod.sh")],
        text=True,
        capture_output=True,
        check=False,
        cwd=REPOSITORY_ROOT,
    )
    if formal_status.returncode == 0:
        return [str(REPOSITORY_ROOT / "scripts/restart-public-prod.sh")]
    return [
        str(REPOSITORY_ROOT / "scripts/restart-public-backend-dev.sh"),
        "--from-prod",
    ]


def _checked(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        cwd=BACKEND_ROOT,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise RuntimeError(f"command failed: {message}")
    return completed.stdout


if __name__ == "__main__":
    raise SystemExit(main())
