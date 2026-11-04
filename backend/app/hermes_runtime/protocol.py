from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx


class HermesProtocolError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path


@dataclass(frozen=True)
class RuntimeDashboardUpstreamError:
    status_code: int
    path: str
    message: str = "runtime Dashboard request failed"

    @classmethod
    def parse(cls, value: Any) -> "RuntimeDashboardUpstreamError | None":
        if not isinstance(value, dict):
            return None
        if value.get("code") != "RUNTIME_DASHBOARD_UPSTREAM_ERROR":
            return None
        status_code = value.get("upstream_status_code")
        path = value.get("upstream_path")
        if (
            not isinstance(status_code, int)
            or isinstance(status_code, bool)
            or not 400 <= status_code <= 599
            or not isinstance(path, str)
            or not _is_bounded_dashboard_path(path)
        ):
            return None
        return cls(status_code=status_code, path=path)


_DASHBOARD_CONTROL_PREFIXES = (
    "/api/config",
    "/api/files",
    "/api/mcp/servers",
    "/api/model/info",
    "/api/plugins/kanban",
    "/api/profiles",
)


def _is_bounded_dashboard_path(path: str) -> bool:
    if not path or len(path) > 2048:
        return False
    parsed = urlsplit(path)
    return bool(
        not parsed.scheme
        and not parsed.netloc
        and not parsed.fragment
        and ".." not in PurePosixPath(parsed.path).parts
        and any(
            parsed.path == prefix or parsed.path.startswith(f"{prefix}/")
            for prefix in _DASHBOARD_CONTROL_PREFIXES
        )
    )


_runtime_http_client: httpx.AsyncClient | None = None
_runtime_http_client_lock = asyncio.Lock()


def _new_runtime_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        trust_env=False,
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=120.0,
        ),
    )


async def shared_runtime_http_client() -> httpx.AsyncClient:
    global _runtime_http_client
    async with _runtime_http_client_lock:
        if _runtime_http_client is None or _runtime_http_client.is_closed:
            _runtime_http_client = _new_runtime_http_client()
        return _runtime_http_client


async def close_runtime_http_client() -> None:
    global _runtime_http_client
    async with _runtime_http_client_lock:
        client = _runtime_http_client
        _runtime_http_client = None
    if client is not None:
        await client.aclose()
