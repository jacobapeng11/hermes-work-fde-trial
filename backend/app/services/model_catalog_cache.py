from __future__ import annotations

import asyncio
import json
import random
import secrets
import time
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

from redis.asyncio import Redis, from_url

from app.config import settings


class ModelCatalogCacheFillThrottledError(RuntimeError):
    """The cache is cold and another request is already rebuilding this key."""


class ModelRouteCacheFillThrottledError(RuntimeError):
    """The exact Run route is cold and another request is rebuilding it."""


class ModelCatalogCacheCircuitOpenError(RuntimeError):
    """Catalog cache refill is temporarily blocked after repeated database failures."""


class ModelRouteCacheCircuitOpenError(RuntimeError):
    """Route cache refill is temporarily blocked after repeated database failures."""


@dataclass(frozen=True)
class ModelCacheMetricEvent:
    cache_name: str
    event: str
    duration_ms: float | None = None
    error_type: str | None = None


@dataclass
class ModelCacheMetricsRecorder:
    counters: dict[tuple[str, str, str | None], int] = field(default_factory=dict)
    durations_ms: dict[tuple[str, str], list[float]] = field(default_factory=dict)

    def record(self, event: ModelCacheMetricEvent) -> None:
        key = (event.cache_name, event.event, event.error_type)
        self.counters[key] = self.counters.get(key, 0) + 1
        if event.duration_ms is not None:
            duration_key = (event.cache_name, event.event)
            self.durations_ms.setdefault(duration_key, []).append(event.duration_ms)

    def count(self, *, cache_name: str, event: str, error_type: str | None = None) -> int:
        return self.counters.get((cache_name, event, error_type), 0)

    def cache_hit_rate(self, *, cache_name: str) -> float | None:
        hits = self.count(cache_name=cache_name, event="hit")
        misses = self.count(cache_name=cache_name, event="miss")
        total = hits + misses
        if total == 0:
            return None
        return hits / total

    def snapshot(self) -> dict[str, Any]:
        cache_names = {cache_name for cache_name, _event, _error_type in self.counters}
        return {
            "counters": [
                {
                    "cache_name": cache_name,
                    "event": event,
                    "error_type": error_type,
                    "count": count,
                }
                for (cache_name, event, error_type), count in sorted(self.counters.items())
            ],
            "durations": [
                {
                    "cache_name": cache_name,
                    "event": event,
                    "count": len(values),
                    "avg_ms": sum(values) / len(values),
                    "max_ms": max(values),
                }
                for (cache_name, event), values in sorted(self.durations_ms.items())
                if values
            ],
            "hit_rates": {
                cache_name: hit_rate
                for cache_name in sorted(cache_names)
                if (hit_rate := self.cache_hit_rate(cache_name=cache_name)) is not None
            },
        }


@dataclass(frozen=True)
class ModelCacheInvalidationEvent:
    event_type: str
    org_id: int
    catalog_version: int | None = None
    model_entry_ids: tuple[int, ...] = ()
    provider_ids: tuple[int, ...] = ()
    runtime_workspace_id: int | None = None
    runtime_run_id: str | None = None

    @classmethod
    def catalog_published(
        cls,
        *,
        org_id: int,
        catalog_version: int,
        model_entry_ids: list[int] | tuple[int, ...],
        provider_ids: list[int] | tuple[int, ...],
    ) -> "ModelCacheInvalidationEvent":
        return cls(
            event_type="catalog_published",
            org_id=org_id,
            catalog_version=catalog_version,
            model_entry_ids=tuple(int(value) for value in model_entry_ids),
            provider_ids=tuple(int(value) for value in provider_ids),
        )

    @classmethod
    def model_status_changed(cls, *, org_id: int, model_entry_id: int, provider_id: int | None = None) -> "ModelCacheInvalidationEvent":
        return cls(
            event_type="model_status_changed",
            org_id=org_id,
            model_entry_ids=(int(model_entry_id),),
            provider_ids=(int(provider_id),) if provider_id is not None else (),
        )

    @classmethod
    def session_model_changed(cls, *, org_id: int, runtime_workspace_id: int) -> "ModelCacheInvalidationEvent":
        return cls(event_type="session_model_changed", org_id=org_id, runtime_workspace_id=runtime_workspace_id)

    @classmethod
    def run_created(cls, *, org_id: int, runtime_workspace_id: int, runtime_run_id: str) -> "ModelCacheInvalidationEvent":
        return cls(
            event_type="run_created",
            org_id=org_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_run_id=runtime_run_id,
        )

    @classmethod
    def run_finished(cls, *, org_id: int, runtime_workspace_id: int, runtime_run_id: str) -> "ModelCacheInvalidationEvent":
        return cls(
            event_type="run_finished",
            org_id=org_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_run_id=runtime_run_id,
        )


class RedisModelCatalogCache:
    """Shared L2 cache for safe, user-visible model catalog records.

    The center database remains authoritative.  Redis stores only serialized
    display records; revision-based keys make publication invalidation O(1)
    without scanning every user's catalog key.
    """

    _RELEASE_LOCK_SCRIPT = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    end
    return 0
    """

    def __init__(
        self,
        client: Redis,
        *,
        catalog_ttl_seconds: int = 300,
        lock_ttl_seconds: int = 4,
        wait_timeout_seconds: float = 0.5,
        max_refill_concurrency: int = 16,
        circuit_breaker_failure_threshold: int = 3,
        circuit_breaker_cooldown_seconds: float = 1.0,
        metrics: ModelCacheMetricsRecorder | None = None,
    ) -> None:
        self.client = client
        self.catalog_ttl_seconds = catalog_ttl_seconds
        self.lock_ttl_seconds = lock_ttl_seconds
        self.wait_timeout_seconds = wait_timeout_seconds
        self.refill_semaphore = asyncio.Semaphore(max_refill_concurrency)
        self.circuit_breaker_failure_threshold = max(1, circuit_breaker_failure_threshold)
        self.circuit_breaker_cooldown_seconds = max(0.0, circuit_breaker_cooldown_seconds)
        self._consecutive_refill_failures = 0
        self._circuit_open_until = 0.0
        self.metrics = metrics

    @staticmethod
    def revision_key(org_id: int) -> str:
        return f"hermes:model:revision:org:{org_id}"

    @staticmethod
    def catalog_key(
        org_id: int,
        user_id: int,
        revision: str,
        *,
        visibility_fingerprint: str | None = None,
    ) -> str:
        key = f"hermes:model:catalog:org:{org_id}:user:{user_id}:revision:{revision}"
        if visibility_fingerprint is not None:
            key = f"{key}:visibility:{visibility_fingerprint}"
        return key

    @staticmethod
    def lock_key(
        org_id: int,
        user_id: int,
        revision: str,
        *,
        visibility_fingerprint: str | None = None,
    ) -> str:
        key = f"hermes:model:lock:catalog:org:{org_id}:user:{user_id}:revision:{revision}"
        if visibility_fingerprint is not None:
            key = f"{key}:visibility:{visibility_fingerprint}"
        return key

    async def get_or_build(
        self,
        *,
        org_id: int,
        user_id: int,
        loader: Callable[[], Awaitable[list[dict[str, Any]]]],
        visibility_fingerprint: str | None = None,
    ) -> list[dict[str, Any]]:
        revision = str(await self.client.get(self.revision_key(org_id)) or "0")
        key = self.catalog_key(
            org_id,
            user_id,
            revision,
            visibility_fingerprint=visibility_fingerprint,
        )
        cached = await self._read(key)
        if cached is not None:
            self._record("hit")
            return cached
        self._record("miss")
        if self._circuit_is_open():
            self._record("circuit_open")
            raise ModelCatalogCacheCircuitOpenError("model catalog cache refill is temporarily unavailable")

        lock_key = self.lock_key(
            org_id,
            user_id,
            revision,
            visibility_fingerprint=visibility_fingerprint,
        )
        token = secrets.token_urlsafe(16)
        acquired = bool(await self.client.set(lock_key, token, nx=True, ex=self.lock_ttl_seconds))
        if acquired:
            try:
                cached = await self._read(key)
                if cached is not None:
                    self._record("hit")
                    return cached
                started = time.perf_counter()
                try:
                    async with self.refill_semaphore:
                        records = await loader()
                except Exception as exc:
                    self._record("refill_failure", duration_ms=self._elapsed_ms(started), error_type=exc.__class__.__name__)
                    self._note_refill_failure()
                    raise
                self._note_refill_success()
                self._record("refill_success", duration_ms=self._elapsed_ms(started))
                await self.client.set(
                    key,
                    json.dumps(records, separators=(",", ":"), ensure_ascii=False),
                    ex=self._ttl_with_jitter(),
                )
                return records
            finally:
                await self.client.eval(self._RELEASE_LOCK_SCRIPT, 1, lock_key, token)

        deadline = asyncio.get_running_loop().time() + self.wait_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
            cached = await self._read(key)
            if cached is not None:
                self._record("hit")
                return cached
        self._record("refill_throttled")
        raise ModelCatalogCacheFillThrottledError("model catalog cache refill is in progress")

    async def prewarm_catalog(
        self,
        *,
        org_id: int,
        user_id: int,
        loader: Callable[[], Awaitable[list[dict[str, Any]]]],
        visibility_fingerprint: str | None = None,
    ) -> bool:
        started = time.perf_counter()
        try:
            await self.get_or_build(
                org_id=org_id,
                user_id=user_id,
                loader=loader,
                visibility_fingerprint=visibility_fingerprint,
            )
        except Exception as exc:
            self._record(
                "prewarm_failure",
                duration_ms=self._elapsed_ms(started),
                error_type=exc.__class__.__name__,
            )
            return False
        self._record("prewarm_success", duration_ms=self._elapsed_ms(started))
        return True

    async def invalidate_org(self, org_id: int) -> None:
        # Incrementing the revision creates a new namespace.  Existing keys
        # expire naturally, so publication never needs an O(number of users)
        # delete scan.
        started = time.perf_counter()
        try:
            await self.client.incr(self.revision_key(org_id))
        except Exception as exc:
            self._record(
                "invalidation_failure",
                duration_ms=self._elapsed_ms(started),
                error_type=exc.__class__.__name__,
            )
            raise
        self._record("invalidation_success", duration_ms=self._elapsed_ms(started))

    async def invalidate(self, event: ModelCacheInvalidationEvent) -> None:
        if event.event_type in {"catalog_published", "model_status_changed"}:
            await self.invalidate_org(event.org_id)

    async def _read(self, key: str) -> list[dict[str, Any]] | None:
        raw = await self.client.get(key)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            await self.client.delete(key)
            return None
        return value if isinstance(value, list) else None

    def _ttl_with_jitter(self) -> int:
        jitter = random.uniform(0.9, 1.1)
        return max(1, int(self.catalog_ttl_seconds * jitter))

    def _record(self, event: str, *, duration_ms: float | None = None, error_type: str | None = None) -> None:
        if self.metrics is not None:
            self.metrics.record(
                ModelCacheMetricEvent(
                    cache_name="model_catalog",
                    event=event,
                    duration_ms=duration_ms,
                    error_type=error_type,
                )
            )

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return (time.perf_counter() - started) * 1000

    def _circuit_is_open(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    def _note_refill_failure(self) -> None:
        self._consecutive_refill_failures += 1
        if self._consecutive_refill_failures >= self.circuit_breaker_failure_threshold:
            self._circuit_open_until = time.monotonic() + self.circuit_breaker_cooldown_seconds

    def _note_refill_success(self) -> None:
        self._consecutive_refill_failures = 0
        self._circuit_open_until = 0.0


class RedisModelRouteCache:
    """Short-lived exact-Run route cache.

    The payload deliberately carries a credential digest and the encrypted
    provider credential, never a plaintext runtime token or Provider API key.
    """

    _RELEASE_LOCK_SCRIPT = RedisModelCatalogCache._RELEASE_LOCK_SCRIPT

    def __init__(
        self,
        client: Redis,
        *,
        route_ttl_seconds: int = 30,
        lock_ttl_seconds: int = 4,
        wait_timeout_seconds: float = 0.25,
        circuit_breaker_failure_threshold: int = 3,
        circuit_breaker_cooldown_seconds: float = 1.0,
        metrics: ModelCacheMetricsRecorder | None = None,
    ) -> None:
        self.client = client
        self.route_ttl_seconds = route_ttl_seconds
        self.lock_ttl_seconds = lock_ttl_seconds
        self.wait_timeout_seconds = wait_timeout_seconds
        self.circuit_breaker_failure_threshold = max(1, circuit_breaker_failure_threshold)
        self.circuit_breaker_cooldown_seconds = max(0.0, circuit_breaker_cooldown_seconds)
        self._consecutive_refill_failures = 0
        self._circuit_open_until = 0.0
        self.metrics = metrics

    @staticmethod
    def route_key(runtime_workspace_id: int, runtime_run_id: str) -> str:
        return f"hermes:model:route:workspace:{runtime_workspace_id}:run:{runtime_run_id}"

    @staticmethod
    def lock_key(runtime_workspace_id: int, runtime_run_id: str) -> str:
        return f"hermes:model:lock:route:workspace:{runtime_workspace_id}:run:{runtime_run_id}"

    @staticmethod
    def application_active_run_key(
        runtime_workspace_id: int,
        model_entry_id: int | None = None,
    ) -> str:
        model_scope = (
            str(int(model_entry_id))
            if model_entry_id is not None
            else "default"
        )
        return (
            "hermes:model:route:application-active:"
            f"workspace:{runtime_workspace_id}:model:{model_scope}"
        )

    @staticmethod
    def workspace_active_run_key(
        runtime_workspace_id: int,
        model_entry_id: int | None = None,
    ) -> str:
        model_scope = (
            str(int(model_entry_id))
            if model_entry_id is not None
            else "default"
        )
        return (
            "hermes:model:route:workspace-active:"
            f"workspace:{runtime_workspace_id}:model:{model_scope}"
        )

    async def get_or_build(
        self,
        *,
        runtime_workspace_id: int,
        runtime_run_id: str,
        loader: Callable[[], Awaitable[dict[str, Any] | None]],
    ) -> dict[str, Any] | None:
        key = self.route_key(runtime_workspace_id, runtime_run_id)
        cached = await self._read(key)
        if cached is not None:
            self._record("hit")
            return cached
        self._record("miss")
        if self._circuit_is_open():
            self._record("circuit_open")
            raise ModelRouteCacheCircuitOpenError("model route cache refill is temporarily unavailable")
        lock_key = self.lock_key(runtime_workspace_id, runtime_run_id)
        token = secrets.token_urlsafe(16)
        acquired = bool(await self.client.set(lock_key, token, nx=True, ex=self.lock_ttl_seconds))
        if acquired:
            try:
                cached = await self._read(key)
                if cached is not None:
                    self._record("hit")
                    return cached
                started = time.perf_counter()
                try:
                    route = await loader()
                except Exception as exc:
                    self._record("refill_failure", duration_ms=self._elapsed_ms(started), error_type=exc.__class__.__name__)
                    self._note_refill_failure()
                    raise
                self._note_refill_success()
                self._record("refill_success", duration_ms=self._elapsed_ms(started))
                if route is not None and route.get("_cacheable", True):
                    await self.client.set(
                        key,
                        json.dumps(route, separators=(",", ":"), ensure_ascii=False),
                        ex=self._ttl_with_jitter(),
                    )
                return route
            finally:
                await self.client.eval(self._RELEASE_LOCK_SCRIPT, 1, lock_key, token)
        deadline = asyncio.get_running_loop().time() + self.wait_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
            cached = await self._read(key)
            if cached is not None:
                self._record("hit")
                return cached
        self._record("refill_throttled")
        raise ModelRouteCacheFillThrottledError("model route cache refill is in progress")

    async def _read(self, key: str) -> dict[str, Any] | None:
        raw = await self.client.get(key)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            await self.client.delete(key)
            return None
        return value if isinstance(value, dict) else None

    async def publish_application_active_run(
        self,
        *,
        runtime_workspace_id: int,
        runtime_run_id: str,
        model_entry_id: int,
    ) -> None:
        """Publish the Run whose exact route was prepared before Prompt.

        Hermes' generic OpenAI-compatible client does not attach the platform
        Run header.  Application Agent traffic therefore resolves the prepared
        exact-Run route through this short-lived pointer.  The pointer contains
        no credential or user data and is published only after the exact route
        cache entry exists.
        """

        await self.client.set(
            self.application_active_run_key(runtime_workspace_id),
            runtime_run_id,
            ex=self._ttl_with_jitter(),
        )
        await self.client.set(
            self.application_active_run_key(
                runtime_workspace_id,
                model_entry_id,
            ),
            runtime_run_id,
            ex=self._ttl_with_jitter(),
        )

    async def publish_workspace_active_run(
        self,
        *,
        runtime_workspace_id: int,
        runtime_run_id: str,
        model_entry_id: int,
    ) -> None:
        await self.client.set(
            self.workspace_active_run_key(runtime_workspace_id),
            runtime_run_id,
            ex=self._ttl_with_jitter(),
        )
        await self.client.set(
            self.workspace_active_run_key(
                runtime_workspace_id,
                model_entry_id,
            ),
            runtime_run_id,
            ex=self._ttl_with_jitter(),
        )

    async def get_application_active_run_id(
        self,
        *,
        runtime_workspace_id: int,
        model_entry_id: int | None,
    ) -> str | None:
        value = await self.client.get(
            self.application_active_run_key(
                runtime_workspace_id,
                model_entry_id,
            )
        )
        if value is None:
            return None
        runtime_run_id = str(value).strip()
        return runtime_run_id or None

    async def get_workspace_active_run_id(
        self,
        *,
        runtime_workspace_id: int,
        model_entry_id: int | None,
    ) -> str | None:
        value = await self.client.get(
            self.workspace_active_run_key(
                runtime_workspace_id,
                model_entry_id,
            )
        )
        if value is None:
            return None
        runtime_run_id = str(value).strip()
        return runtime_run_id or None

    async def invalidate_application_active_run(
        self,
        *,
        runtime_workspace_id: int,
        runtime_run_id: str,
    ) -> None:
        """Retire only this Run without deleting a newer concurrent pointer."""

        started = time.perf_counter()
        try:
            route = await self._read(
                self.route_key(runtime_workspace_id, runtime_run_id)
            )
            await self.client.eval(
                self._RELEASE_LOCK_SCRIPT,
                1,
                self.application_active_run_key(runtime_workspace_id),
                runtime_run_id,
            )
            model_entry_id = (
                route.get("allowed_model_entry_id")
                if route is not None
                else None
            )
            if isinstance(model_entry_id, int):
                await self.client.eval(
                    self._RELEASE_LOCK_SCRIPT,
                    1,
                    self.application_active_run_key(
                        runtime_workspace_id,
                        model_entry_id,
                    ),
                    runtime_run_id,
                )
            await self.client.delete(
                self.route_key(runtime_workspace_id, runtime_run_id)
            )
        except Exception as exc:
            self._record(
                "invalidation_failure",
                duration_ms=self._elapsed_ms(started),
                error_type=exc.__class__.__name__,
            )
            raise
        self._record(
            "invalidation_success",
            duration_ms=self._elapsed_ms(started),
        )

    async def invalidate_workspace_active_run(
        self,
        *,
        runtime_workspace_id: int,
        runtime_run_id: str,
    ) -> None:
        started = time.perf_counter()
        try:
            route = await self._read(
                self.route_key(runtime_workspace_id, runtime_run_id)
            )
            await self.client.eval(
                self._RELEASE_LOCK_SCRIPT,
                1,
                self.workspace_active_run_key(runtime_workspace_id),
                runtime_run_id,
            )
            model_entry_id = (
                route.get("allowed_model_entry_id")
                if route is not None
                else None
            )
            if isinstance(model_entry_id, int):
                await self.client.eval(
                    self._RELEASE_LOCK_SCRIPT,
                    1,
                    self.workspace_active_run_key(
                        runtime_workspace_id,
                        model_entry_id,
                    ),
                    runtime_run_id,
                )
            await self.client.delete(
                self.route_key(runtime_workspace_id, runtime_run_id)
            )
        except Exception as exc:
            self._record(
                "invalidation_failure",
                duration_ms=self._elapsed_ms(started),
                error_type=exc.__class__.__name__,
            )
            raise
        self._record(
            "invalidation_success",
            duration_ms=self._elapsed_ms(started),
        )

    async def invalidate_route(self, *, runtime_workspace_id: int, runtime_run_id: str) -> None:
        started = time.perf_counter()
        try:
            await self.client.delete(self.route_key(runtime_workspace_id, runtime_run_id))
        except Exception as exc:
            self._record(
                "invalidation_failure",
                duration_ms=self._elapsed_ms(started),
                error_type=exc.__class__.__name__,
            )
            raise
        self._record("invalidation_success", duration_ms=self._elapsed_ms(started))

    async def invalidate(self, event: ModelCacheInvalidationEvent) -> None:
        if event.runtime_workspace_id is None or event.runtime_run_id is None:
            return
        if event.event_type == "run_finished":
            await self.invalidate_workspace_active_run(
                runtime_workspace_id=event.runtime_workspace_id,
                runtime_run_id=event.runtime_run_id,
            )
        elif event.event_type == "run_created":
            await self.invalidate_route(
                runtime_workspace_id=event.runtime_workspace_id,
                runtime_run_id=event.runtime_run_id,
            )

    def _ttl_with_jitter(self) -> int:
        return max(1, int(self.route_ttl_seconds * random.uniform(0.9, 1.1)))

    def _record(self, event: str, *, duration_ms: float | None = None, error_type: str | None = None) -> None:
        if self.metrics is not None:
            self.metrics.record(
                ModelCacheMetricEvent(
                    cache_name="model_route",
                    event=event,
                    duration_ms=duration_ms,
                    error_type=error_type,
                )
            )

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return (time.perf_counter() - started) * 1000

    def _circuit_is_open(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    def _note_refill_failure(self) -> None:
        self._consecutive_refill_failures += 1
        if self._consecutive_refill_failures >= self.circuit_breaker_failure_threshold:
            self._circuit_open_until = time.monotonic() + self.circuit_breaker_cooldown_seconds

    def _note_refill_success(self) -> None:
        self._consecutive_refill_failures = 0
        self._circuit_open_until = 0.0


@lru_cache(maxsize=1)
def shared_model_cache_metrics() -> ModelCacheMetricsRecorder:
    return ModelCacheMetricsRecorder()


@lru_cache(maxsize=1)
def shared_model_catalog_cache() -> RedisModelCatalogCache:
    return RedisModelCatalogCache(
        from_url(settings.REDIS_URL, decode_responses=True),
        metrics=shared_model_cache_metrics(),
    )
