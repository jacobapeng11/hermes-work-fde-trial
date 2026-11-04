from __future__ import annotations

import asyncio

import httpx


class HermesProtocolError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        rpc_code: int | None = None,
        rpc_method: str | None = None,
        rpc_data: object | None = None,
        status_code: int | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.rpc_code = rpc_code
        self.rpc_method = rpc_method
        self.rpc_data = rpc_data
        self.status_code = status_code
        self.path = path


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
