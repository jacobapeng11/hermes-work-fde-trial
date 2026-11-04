"""Locate one exact Runtime Pod and verify its active-MCP-reload test image."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from typing import Any

from expert_profile_resume_smoke.contracts import SmokeError, Target


TEST_MARKER = "/opt/hermes-platform/allow-active-mcp-reload-test"


@dataclass(frozen=True)
class RuntimePod:
    namespace: str
    name: str
    image: str
    image_id: str
    container: str = "user-container"


class RuntimePodInspector:
    def __init__(self, *, namespace: str = "hermes-runtimes") -> None:
        self.namespace = namespace

    async def locate(self, target: Target) -> RuntimePod:
        selector = ",".join(
            (
                f"hermes.platform/runtime-workspace-id={target.workspace_id}",
                f"hermes.platform/runtime-id={target.runtime_id}",
                f"hermes.platform/owner-user-id={target.principal.user_id}",
            )
        )
        payload = await self._kubectl_json(
            "-n",
            self.namespace,
            "get",
            "pods",
            "-l",
            selector,
            "-o",
            "json",
        )
        items = [
            item
            for item in payload.get("items", [])
            if isinstance(item, dict) and _pod_is_ready(item)
        ]
        if len(items) != 1:
            raise SmokeError(
                f"target Runtime must have exactly one Ready Pod, observed {len(items)}"
            )
        item = items[0]
        spec_containers = (item.get("spec") or {}).get("containers") or []
        statuses = (item.get("status") or {}).get("containerStatuses") or []
        container = next(
            (
                value
                for value in spec_containers
                if isinstance(value, dict) and value.get("name") == "user-container"
            ),
            None,
        )
        status = next(
            (
                value
                for value in statuses
                if isinstance(value, dict) and value.get("name") == "user-container"
            ),
            None,
        )
        if not isinstance(container, dict) or not isinstance(status, dict):
            raise SmokeError("target Runtime user-container identity is unavailable")
        return RuntimePod(
            namespace=self.namespace,
            name=str((item.get("metadata") or {}).get("name") or ""),
            image=str(container.get("image") or ""),
            image_id=str(status.get("imageID") or ""),
        )

    async def require_test_marker(self, pod: RuntimePod) -> None:
        _, stderr, code = await self._run(
            "-n",
            pod.namespace,
            "exec",
            pod.name,
            "-c",
            pod.container,
            "--",
            "test",
            "-f",
            TEST_MARKER,
            timeout_seconds=20,
        )
        if code != 0:
            raise SmokeError(
                "target Runtime is not running the active-MCP-reload test image: "
                f"{stderr.strip()[:200]}"
            )

    async def _kubectl_json(self, *arguments: str) -> dict[str, Any]:
        stdout, stderr, code = await self._run(*arguments, timeout_seconds=20)
        if code != 0:
            raise SmokeError(f"kubectl failed: {stderr.strip()[:300]}")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SmokeError("kubectl returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SmokeError("kubectl returned a non-object JSON payload")
        return payload

    @staticmethod
    async def _run(
        *arguments: str,
        timeout_seconds: float,
    ) -> tuple[str, str, int]:
        environment = dict(os.environ)
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            environment.pop(key, None)
        process = await asyncio.create_subprocess_exec(
            "kubectl",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise SmokeError("kubectl command timed out")
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            int(process.returncode or 0),
        )


def _pod_is_ready(item: dict[str, Any]) -> bool:
    metadata = item.get("metadata") or {}
    status = item.get("status") or {}
    if metadata.get("deletionTimestamp") or status.get("phase") != "Running":
        return False
    conditions = status.get("conditions") or []
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in conditions
        if isinstance(condition, dict)
    )
