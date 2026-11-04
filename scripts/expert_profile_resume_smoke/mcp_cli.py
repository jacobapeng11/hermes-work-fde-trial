"""CLI lifecycle and exact cleanup for the dual-MCP isolation smoke."""

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
from app.services.auth import create_access_token

from .contracts import Checkpoint, CheckpointStore, SmokeError, Target
from .marker_server import MarkerMcp
from .mcp_experiments import McpExperiments
from .mcp_fixture import McpProfileFixture
from .runtime_edge import RuntimeEdge
from .target import load_target


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
    return result


def reserve_port() -> int:
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        return int(sock.getsockname()[1])


async def async_main(args: argparse.Namespace) -> dict[str, object]:
    try:
        target, baseline = await load_target(args.user_code, args.runtime_id)
        plan: dict[str, object] = {
            "apply": bool(args.apply),
            "target": {
                "name": target.user_name,
                "user_code": target.user_code,
                "user_id": target.principal.user_id,
                "workspace_id": target.workspace_id,
                "runtime_id": target.runtime_id,
            },
            "baseline": baseline,
            "experiments": [f"M{index}" for index in range(4)],
            "side_effects": (
                "creates two temporary business experts, one Registry keeper, two MCP "
                "Profile bindings, Sessions, and real Runs; all exact IDs are cleaned "
                "in finally"
            ),
            "global_reload": False,
            "registry_incremental_registration": "out_of_scope",
        }
        if not args.apply:
            return plan
        return await _run_apply(args, target, baseline)
    finally:
        await engine.dispose()


async def _run_apply(
    args: argparse.Namespace,
    target: Target,
    baseline: dict[str, object],
) -> dict[str, object]:
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:6]
    checkpoint = Checkpoint(
        suffix=suffix,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    store = CheckpointStore(
        checkpoint,
        ROOT / ".runtime" / "expert-profile-mcp-isolation-smoke" / f"{suffix}.json",
    )
    marker = MarkerMcp(
        suffix=suffix,
        host=args.marker_host,
        port=reserve_port(),
    )
    token = create_access_token(target.principal)
    client = httpx.AsyncClient(
        base_url=args.backend_base.rstrip("/"),
        headers={
            "Authorization": f"Bearer {token}",
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
    profiles = McpProfileFixture(
        client=client,
        target=target,
        store=store,
        marker=marker,
    )
    experiments = McpExperiments(
        target=target,
        baseline=baseline,
        store=store,
        marker=marker,
        profiles=profiles,
        edge=edge,
    )
    result: dict[str, object] = {}
    error: str | None = None
    cleanup_errors: list[str] = []
    await marker.start()
    try:
        result = await experiments.execute()
        acceptance = result.get("acceptance")
        if isinstance(acceptance, dict) and not acceptance.get("passed"):
            raise SmokeError(
                "business acceptance failed: "
                + json.dumps(acceptance.get("failures"), ensure_ascii=False)
            )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        marker.release_all_holds()
        await edge.cleanup(cleanup_errors)
        await profiles.cleanup(cleanup_errors)
        await profiles.verify_cleanup(cleanup_errors)
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
