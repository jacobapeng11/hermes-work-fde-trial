#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.services.runtime_cutover_checkpoint import (  # noqa: E402
    RuntimeCutoverCheckpointStore,
)
from app.services.runtime_image_config import RuntimeImageConfigStore  # noqa: E402


BACKEND_PYTHON = REPOSITORY_ROOT / "backend/.venv/bin/python"
CONTROL = REPOSITORY_ROOT / "backend/scripts/runtime_code_reload.py"
PREFLIGHT = REPOSITORY_ROOT / "backend/scripts/runtime_image_preflight.py"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reload exactly one Runtime from backend/.env."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPOSITORY_ROOT / "backend/.env",
        help="Runtime image identity file; all public lifecycles use backend/.env.",
    )
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument(
        "--binding-type",
        choices=("personal_runtime", "application_runtime"),
    )
    parser.add_argument("--skip-backend-restart", action="store_true")
    parser.add_argument(
        "--reexecute-completed",
        action="store_true",
        help="显式重执行同一镜像目标的已完成单 Runtime 重载。",
    )
    parser.add_argument(
        "--backend-lifecycle",
        choices=("auto", "dev", "prod"),
        default="auto",
        help="Backend restart lifecycle; auto selects a registered public-prod Backend first.",
    )
    parser.add_argument(
        "--maintenance-epoch",
        help="Existing Runtime maintenance epoch when the control plane is frozen.",
    )
    parser.add_argument("--supersede-failed-target", action="store_true")
    parser.add_argument(
        "--precreate-zero-scale",
        action="store_true",
        help="个人 Runtime 在 reset 后预创建 Ready zero-scale KService，不激活 Pod。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="快速路径：身份闭环后精确收敛 task/PID，再强删 Pod。",
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
        return _run(args)


def _run(args: argparse.Namespace) -> int:
    env_file = args.env_file
    if not env_file.is_absolute():
        env_file = (REPOSITORY_ROOT / env_file).resolve()
    _checked([str(BACKEND_PYTHON), str(PREFLIGHT), "--env-file", str(env_file)])
    target = RuntimeImageConfigStore(env_file).read()
    if not target.manifest_digest or not target.config_id:
        raise RuntimeError("backend/.env Runtime image identity is incomplete")
    command = ["list", "--runtime-id", args.runtime_id]
    if args.binding_type:
        command.extend(["--binding-type", args.binding_type])
    matches = json.loads(_control(command, env_file=env_file))
    if len(matches) != 1:
        raise RuntimeError(
            "runtime_id must resolve to exactly one Binding; specify --binding-type on conflict"
        )
    binding = matches[0]
    if getattr(args, "precreate_zero_scale", False) and binding[
        "binding_type"
    ] != "personal_runtime":
        raise ValueError("--precreate-zero-scale only supports personal_runtime")
    selector = [
        "--binding-type",
        binding["binding_type"],
        "--binding-id",
        str(binding["binding_id"]),
    ]
    checkpoint_path = (
        REPOSITORY_ROOT
        / ".runtime/runtime-code-cutover"
        / f"{binding['binding_type']}-{binding['runtime_id']}.json"
    )
    checkpoint = RuntimeCutoverCheckpointStore(checkpoint_path)
    existing = checkpoint.load()
    expected_target = {
        "image": target.image,
        "manifest_digest": target.manifest_digest,
        "config_id": target.config_id,
    }
    if (
        existing
        and existing.get("target") == expected_target
        and existing.get("status") == "failed"
        and binding.get("status") == "ready"
        and binding.get("image") == target.image
        and binding.get("image_digest") == target.config_id
    ):
        evidence = json.loads(
            _control(["verify", *selector], env_file=env_file)
        )
        checkpoint.advance(
            stage="verified",
            status="completed",
            checks={"technical_acceptance": evidence},
        )
        print(
            json.dumps(
                {
                    "status": "completed",
                    "binding_type": binding["binding_type"],
                    "binding_id": binding["binding_id"],
                    "runtime_id": binding["runtime_id"],
                    "target_image": target.image,
                    "checkpoint": str(checkpoint_path.relative_to(REPOSITORY_ROOT)),
                },
                sort_keys=True,
            )
        )
        return 0
    if (
        existing
        and existing.get("target") == expected_target
        and existing.get("status") == "failed"
        and binding.get("status") not in {"updating", "failed"}
    ):
        raise RuntimeError("failed single Runtime flow is no longer isolated")
    if existing and existing.get("target") != expected_target:
        if existing.get("status") == "completed":
            if binding.get("status") != "ready":
                raise RuntimeError("completed single Runtime flow is not currently ready")
            _archive_checkpoint(checkpoint_path, existing, "completed")
        else:
            previous_target = existing.get("target") or {}
            if (
                existing.get("status") == "failed"
                and binding.get("status") == "ready"
                and binding.get("image") == target.image
                and binding.get("image_digest") == target.config_id
            ):
                _archive_checkpoint(checkpoint_path, existing, "failed-stale")
            elif (
                existing.get("status") == "failed"
                and binding.get("status") == "ready"
                and binding.get("image") == previous_target.get("image")
                and binding.get("image_digest") == previous_target.get("config_id")
            ):
                _archive_checkpoint(checkpoint_path, existing, "failed-recovered")
            else:
                if not args.supersede_failed_target or existing.get("status") != "failed":
                    raise RuntimeError("checkpoint target conflicts with backend/.env")
                if binding.get("status") not in {"updating", "failed"}:
                    raise RuntimeError("failed single Runtime flow is no longer isolated")
                _archive_checkpoint(checkpoint_path, existing, "failed")
        existing = None
    if (
        getattr(args, "reexecute_completed", False)
        and existing
        and existing.get("target") == expected_target
        and existing.get("status") == "completed"
    ):
        _archive_checkpoint(checkpoint_path, existing, "completed")
        existing = None
    checkpoint.initialize(flow="single", target=target, bindings=[binding])
    if (
        existing
        and existing.get("target") == expected_target
        and existing.get("status") == "completed"
    ):
        try:
            _control(["verify", *selector], env_file=env_file)
        except Exception:
            pass
        else:
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "binding_type": binding["binding_type"],
                        "binding_id": binding["binding_id"],
                        "runtime_id": binding["runtime_id"],
                        "target_image": target.image,
                        "checkpoint": str(checkpoint_path.relative_to(REPOSITORY_ROOT)),
                    },
                    sort_keys=True,
                )
            )
            return 0
    try:
        isolate_command = ["isolate", *selector]
        if args.maintenance_epoch:
            isolate_command.extend(["--maintenance-epoch", args.maintenance_epoch])
        _control(isolate_command, env_file=env_file)
        checkpoint.advance(stage="isolated")
        cancellation = json.loads(
            _control(["cancel-active", *selector], env_file=env_file)
        )
        checkpoint.advance(
            stage="runs_canceled",
            checks={"active_runs": cancellation.get("runs", [])},
        )
        if not args.skip_backend_restart:
            _checked(_backend_restart_command(args.backend_lifecycle))
        checkpoint.advance(stage="backend_restarted")
        stop_command = ["stop", *selector]
        if getattr(args, "force", False):
            stop_command.extend(
                [
                    "--delete-pods",
                    "--fast-test-mode",
                    "--timeout",
                    "30",
                ]
            )
        _control(stop_command, env_file=env_file)
        checkpoint.advance(stage="pods_zero", checks={"old_pods_zero": True})
        reload_command = ["reload", *selector]
        if args.maintenance_epoch:
            reload_command.extend(["--maintenance-epoch", args.maintenance_epoch])
        if getattr(args, "precreate_zero_scale", False):
            reload_command.append("--deferred-precreate")
        _control(reload_command, env_file=env_file)
        checkpoint.advance(stage="reprojected")
        session_evidence = (
            {"skipped_reason": "zero_scale_precreated"}
            if getattr(args, "precreate_zero_scale", False)
            else json.loads(
                _control(
                    [
                        "verify-sessions",
                        *selector,
                        "--checkpoint",
                        str(checkpoint_path),
                    ],
                    env_file=env_file,
                )
            )
        )
        checkpoint.advance(
            stage="session_recovered",
            checks={"session_history": session_evidence},
        )
        verify_command = ["verify", *selector]
        if getattr(args, "precreate_zero_scale", False):
            verify_command.append("--precreated-zero-scale")
        evidence = json.loads(_control(verify_command, env_file=env_file))
        checkpoint.advance(
            stage="verified",
            status="completed",
            checks={"technical_acceptance": evidence},
        )
    except Exception as exc:
        try:
            _control(["mark-failed", *selector], env_file=env_file)
        except Exception:
            # The original failure remains authoritative; checkpoint preserves resume state.
            pass
        checkpoint.advance(
            stage="failed",
            status="failed",
            error_summary=f"single Runtime reload failed: {type(exc).__name__}",
        )
        raise
    print(
        json.dumps(
            {
                "status": "completed",
                "binding_type": binding["binding_type"],
                "binding_id": binding["binding_id"],
                "runtime_id": binding["runtime_id"],
                "target_image": target.image,
                "checkpoint": str(checkpoint_path.relative_to(REPOSITORY_ROOT)),
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


def _control(arguments: list[str], *, env_file: Path) -> str:
    return _checked(
        [
            str(BACKEND_PYTHON),
            str(CONTROL),
            "--env-file",
            str(env_file),
            *arguments,
        ]
    )


def _backend_restart_command(lifecycle: str) -> list[str]:
    if lifecycle == "prod":
        return [str(REPOSITORY_ROOT / "scripts/restart-public-prod.sh")]
    if lifecycle == "dev":
        return [str(REPOSITORY_ROOT / "scripts/restart-public-backend-dev.sh")]
    formal_status = subprocess.run(
        [str(REPOSITORY_ROOT / "scripts/status-public-prod.sh")],
        text=True,
        capture_output=True,
        check=False,
        cwd=REPOSITORY_ROOT,
    )
    if formal_status.returncode == 0:
        return [str(REPOSITORY_ROOT / "scripts/restart-public-prod.sh")]
    return [str(REPOSITORY_ROOT / "scripts/restart-public-backend-dev.sh")]


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
