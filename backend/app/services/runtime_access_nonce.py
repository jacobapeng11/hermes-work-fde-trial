from __future__ import annotations

import asyncio
from functools import lru_cache
import time
from typing import Protocol

from redis.asyncio import Redis, from_url
from redis.exceptions import RedisError

from app.config import settings


class RuntimeAccessNonceUnavailable(RuntimeError):
    pass


class RuntimeAccessNonceStore(Protocol):
    async def consume(self, nonce: str, *, ttl_seconds: int) -> bool: ...


class RedisRuntimeAccessNonceStore:
    def __init__(
        self,
        client: Redis,
        *,
        key_prefix: str = "hermes:runtime-access-nonce:v1:",
    ) -> None:
        self.client = client
        self.key_prefix = key_prefix

    async def consume(self, nonce: str, *, ttl_seconds: int) -> bool:
        try:
            return bool(
                await self.client.set(
                    f"{self.key_prefix}{nonce}",
                    "used",
                    nx=True,
                    ex=max(1, ttl_seconds),
                )
            )
        except RedisError as exc:
            raise RuntimeAccessNonceUnavailable(
                "RuntimeAccess nonce store is unavailable"
            ) from exc


class InMemoryRuntimeAccessNonceStore:
    def __init__(self) -> None:
        self._expires: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def consume(self, nonce: str, *, ttl_seconds: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            self._expires = {
                key: expiry
                for key, expiry in self._expires.items()
                if expiry > now
            }
            if nonce in self._expires:
                return False
            self._expires[nonce] = now + max(1, ttl_seconds)
            return True


@lru_cache(maxsize=1)
def shared_runtime_access_nonce_store() -> RuntimeAccessNonceStore:
    if settings.RUNTIME_CHAT_MODE != "real":
        return InMemoryRuntimeAccessNonceStore()
    return RedisRuntimeAccessNonceStore(
        from_url(settings.REDIS_URL, decode_responses=True)
    )


async def close_runtime_access_nonce_store() -> None:
    if shared_runtime_access_nonce_store.cache_info().currsize == 0:
        return
    store = shared_runtime_access_nonce_store()
    shared_runtime_access_nonce_store.cache_clear()
    if isinstance(store, RedisRuntimeAccessNonceStore):
        await store.client.aclose()
