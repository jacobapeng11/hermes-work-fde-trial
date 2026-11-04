"""CLI lifecycle for the focused active-Run Supervisor MCP reload smoke."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.config import settings
from app.database import engine
from app.hermes_runtime.dashboard_protocol import (
    HermesDashboardProtocolAdapter,
    RuntimeControlBusy,
)
from app.services.auth import create_access_token

from expert_profile_resume_smoke.contracts import (
    Checkpoint,
    CheckpointStore,
    SmokeError,
    Target,
)
from expert_profile_resume_smoke.marker_server import MarkerMcp
from expert_profile_resume_smoke.runtime_edge import RuntimeEdge
from expert_profile_resume_smoke.target import load_target

from .business_mcp_fixture import BusinessMcpProfileFixture
from .experiment import ActiveReloadExperiment
from .raw_dashboard import RuntimePod, RuntimePodInspector


ROOT = Path(__file__).resolve().parents[2]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--user-code", required=True)
    result.add_argument("--runtime-id", required=True, help="Expected rw-<workspace-id>")
    result.add_argument("--apply", action="store_true")
    result.add_argument(
        "--backend-base",
        default=f"http://127.0.0.1:{settings.PORT_BACKEND}/api/v1",
    )
    result.add_argument("--runtime-edge-base", default=settings.PUBLIC_WEB_BASE)
    result.add_argument(
        "--marker-host",
        default=urlsplit(settings.PUBLIC_API_BASE).hostname or "127.0.0.1",
    )
    result.add_argument("--timeout-seconds", type=float, default=180)
    result.add_argument("--namespace", default="hermes-runtimes")
    return result


def reserve_port() -> int:
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        return int(sock.getsockname()[1])


async def async_main(args: argparse.Namespace) -> dict[str, object]:
    try:
        target, baseline = await load_target(args.user_code, args.runtime_id)
        pods = RuntimePodInspector(namespace=args.namespace)
        pod = await pods.locate(target)
        plan: dict[str, object] = {
            "apply": bool(args.apply),
            "target": {
                "name": target.user_name,
                "user_code": target.user_code,
                "user_id": target.principal.user_id,
                "workspace_id": target.workspace_id,
                "runtime_id": target.runtime_id,
            },
            "pod": {
                "namespace": pod.namespace,
                "name": pod.name,
                "image": pod.image,
                "image_id": pod.image_id,
            },
            "baseline": baseline,
            "experiment": "one active MCP hold Run plus one Supervisor reload_mcp",
            "side_effects": (
                "creates one temporary expert/Profile/Session, calls a real Provider, "
                "and uses the target test image to globally reload MCP during the Run"
            ),
        }
        if not args.apply:
            return plan
        await pods.require_test_marker(pod)
        return await _run_apply(args, target, baseline, pod)
    finally:
        await engine.dispose()


async def _run_apply(
    args: argparse.Namespace,
    target: Target,
    baseline: dict[str, object],
    pod: RuntimePod,
) -> dict[str, object]:
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:6]
    checkpoint = Checkpoint(
        suffix=suffix,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    store = CheckpointStore(
        checkpoint,
        ROOT / ".runtime" / "hermes-active-mcp-reload-smoke" / f"{suffix}.json",
    )
    marker = MarkerMcp(suffix=suffix, host=args.marker_host, port=reserve_port())
    client = httpx.AsyncClient(
        base_url=args.backend_base.rstrip("/"),
        headers={
            "Authorization": f"Bearer {create_access_token(target.principal)}",
            "Origin": settings.PUBLIC_WEB_BASE.rstrip("/"),
        },
        timeout=180,
        trust_env=False,
    )
    edge = RuntimeEdge(
        client=client,
        target=target,
        store=store,
        runtime_edge_base=args.runtime_edge_base,
        timeout_seconds=args.timeout_seconds,
    )
    profiles = BusinessMcpProfileFixture(
        client=client,
        target=target,
        store=store,
        marker=marker,
    )
    experiment = ActiveReloadExperiment(
        target=target,
        baseline=baseline,
        store=store,
        marker=marker,
        profiles=profiles,
        edge=edge,
        control=HermesDashboardProtocolAdapter(),
        pod=pod,
    )
    result: dict[str, object] = {}
    error: str | None = None
    cleanup_errors: list[str] = []
    await marker.start()
    try:
        result = await experiment.execute()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        marker.release_all_holds()
        await edge.cleanup(cleanup_errors)
        await profiles.cleanup(cleanup_errors)
        await profiles.verify_cleanup(cleanup_errors)
        if any(
            item.get("method") == "supervisor.reload_mcp"
            for item in checkpoint.control_methods
        ):
            checkpoint.evidence["post_experiment_registry_restore"] = (
                await _restore_global_registry(target, cleanup_errors)
            )
        await edge.close()
        await client.aclose()
        await marker.stop()
        checkpoint.evidence["marker_history"] = marker.history
        checkpoint.evidence["error"] = error
        checkpoint.evidence["cleanup_errors"] = cleanup_errors
        store.save()
    if error or cleanup_errors:
        raise SmokeError(
            json.dumps(
                {
                    "error": error,
                    "cleanup_errors": cleanup_errors,
                    "checkpoint": str(store.path),
                },
                ensure_ascii=False,
            )
        )
    return {**result, "cleanup": {"success": True}}


async def _restore_global_registry(
    target: Target, errors: list[str]
) -> dict[str, object]:
    dashboard = HermesDashboardProtocolAdapter()
    for attempt in range(31):
        try:
            await dashboard.reload_mcp(target.endpoint)
            return {"status": "completed", "attempts": attempt + 1}
        except RuntimeControlBusy:
            if attempt < 30:
                await asyncio.sleep(1)
                continue
            errors.append("registry_restore:RuntimeControlBusy")
        except Exception as exc:
            errors.append(f"registry_restore:{type(exc).__name__}")
        break
    return {"status": "failed", "attempts": 31}


def main() -> int:
    args = parser().parse_args()
    try:
        result = asyncio.run(async_main(args))
    except SmokeError as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "success": False,
                    "error": f"unexpected {type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"success": True, **result}, ensure_ascii=False, indent=2))
    return 0
