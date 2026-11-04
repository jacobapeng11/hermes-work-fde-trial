from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.network_environment import proxy_free_environment


DATABASE_TIMEOUT_SECONDS = 5
RUNTIME_PROBE_TIMEOUT_SECONDS = 30
RUNTIME_PROBE_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "lib"
    / "runtime_infra_probe.py"
)


async def check_database_readiness() -> dict[str, str]:
    try:
        async with AsyncSessionLocal() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")),
                timeout=DATABASE_TIMEOUT_SECONDS,
            )
    except Exception as exc:
        return {
            "status": "not_ready",
            "detail": f"database check failed: {type(exc).__name__}",
        }
    return {"status": "ready", "detail": "SELECT 1 succeeded"}


async def check_runtime_infrastructure_readiness() -> dict[str, Any]:
    if not RUNTIME_PROBE_PATH.is_file():
        return {
            "status": "not_ready",
            "detail": "runtime infrastructure probe is missing",
        }
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(RUNTIME_PROBE_PATH),
            "--format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proxy_free_environment(),
        )
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=RUNTIME_PROBE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        if "process" in locals():
            process.kill()
            await process.wait()
        return {
            "status": "not_ready",
            "detail": "runtime infrastructure probe timed out",
        }
    except OSError as exc:
        return {
            "status": "not_ready",
            "detail": f"runtime infrastructure probe failed: {type(exc).__name__}",
        }
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "status": "not_ready",
            "detail": "runtime infrastructure probe returned invalid JSON",
        }
    if not isinstance(payload, dict) or payload.get("status") not in {
        "ready",
        "not_ready",
    }:
        return {
            "status": "not_ready",
            "detail": "runtime infrastructure probe returned invalid status",
        }
    if process.returncode not in {0, 1}:
        return {
            "status": "not_ready",
            "detail": "runtime infrastructure probe could not run",
        }
    return payload


async def get_platform_readiness() -> dict[str, Any]:
    database, runtime_infrastructure = await asyncio.gather(
        check_database_readiness(),
        check_runtime_infrastructure_readiness(),
    )
    components = {
        "database": database,
        "runtime_infrastructure": runtime_infrastructure,
    }
    ready = all(
        component.get("status") == "ready"
        for component in components.values()
    )
    return {
        "status": "ready" if ready else "not_ready",
        "components": components,
    }
