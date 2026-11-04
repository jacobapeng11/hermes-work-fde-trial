import json

import pytest

from app.services.model_catalog_cache import (
    ModelCatalogCacheCircuitOpenError,
    ModelCatalogCacheFillThrottledError,
    ModelCacheInvalidationEvent,
    ModelCacheMetricsRecorder,
    ModelRouteCacheCircuitOpenError,
    ModelRouteCacheFillThrottledError,
    RedisModelCatalogCache,
    RedisModelRouteCache,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}
        self.loads = 0

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    async def delete(self, key: str):
        self.values.pop(key, None)

    async def incr(self, key: str):
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    async def eval(self, _script: str, _numkeys: int, key: str, token: str):
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0


@pytest.mark.asyncio
async def test_catalog_cache_builds_once_and_reuses_safe_payload() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelCatalogCache(redis, catalog_ttl_seconds=300, metrics=metrics)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return [{"modelEntryId": 7, "displayName": "Model A"}]

    assert await cache.get_or_build(org_id=1, user_id=9, loader=loader) == [{"modelEntryId": 7, "displayName": "Model A"}]
    assert await cache.get_or_build(org_id=1, user_id=9, loader=loader) == [{"modelEntryId": 7, "displayName": "Model A"}]
    assert calls == 1
    assert redis.expirations[cache.catalog_key(1, 9, "0")] >= 270
    assert metrics.count(cache_name="model_catalog", event="miss") == 1
    assert metrics.count(cache_name="model_catalog", event="hit") == 1
    assert metrics.count(cache_name="model_catalog", event="refill_success") == 1
    assert metrics.durations_ms[("model_catalog", "refill_success")]
    assert metrics.cache_hit_rate(cache_name="model_catalog") == 0.5
    snapshot = metrics.snapshot()
    assert snapshot["hit_rates"] == {"model_catalog": 0.5}
    assert {
        (counter["cache_name"], counter["event"], counter["count"])
        for counter in snapshot["counters"]
    } >= {
        ("model_catalog", "hit", 1),
        ("model_catalog", "miss", 1),
        ("model_catalog", "refill_success", 1),
    }


@pytest.mark.asyncio
async def test_catalog_revision_invalidates_without_scanning_old_keys() -> None:
    redis = FakeRedis()
    cache = RedisModelCatalogCache(redis)
    await redis.set(cache.catalog_key(1, 9, "0"), json.dumps([{ "modelEntryId": 1 }]), ex=300)

    await cache.invalidate_org(1)

    assert await redis.get(cache.revision_key(1)) == "1"
    assert await redis.get(cache.catalog_key(1, 9, "0")) is not None


@pytest.mark.asyncio
async def test_catalog_revision_is_shared_across_backend_cache_instances() -> None:
    redis = FakeRedis()
    first_instance = RedisModelCatalogCache(redis)
    second_instance = RedisModelCatalogCache(redis)
    first_calls = 0
    second_calls = 0

    async def first_loader():
        nonlocal first_calls
        first_calls += 1
        return [{"modelEntryId": 1, "displayName": "Old Model"}]

    async def second_loader():
        nonlocal second_calls
        second_calls += 1
        return [{"modelEntryId": 2, "displayName": "New Model"}]

    assert await first_instance.get_or_build(org_id=1, user_id=9, loader=first_loader) == [
        {"modelEntryId": 1, "displayName": "Old Model"}
    ]
    assert await first_instance.get_or_build(org_id=1, user_id=9, loader=first_loader) == [
        {"modelEntryId": 1, "displayName": "Old Model"}
    ]

    await second_instance.invalidate(
        ModelCacheInvalidationEvent.catalog_published(
            org_id=1,
            catalog_version=2,
            model_entry_ids=[2],
            provider_ids=[1],
        )
    )

    assert await first_instance.get_or_build(org_id=1, user_id=9, loader=second_loader) == [
        {"modelEntryId": 2, "displayName": "New Model"}
    ]
    assert first_calls == 1
    assert second_calls == 1
    assert await redis.get(first_instance.catalog_key(1, 9, "0")) is not None
    assert await redis.get(first_instance.catalog_key(1, 9, "1")) is not None


@pytest.mark.asyncio
async def test_catalog_invalidation_event_carries_release_scope_and_bumps_revision() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelCatalogCache(redis, metrics=metrics)
    event = ModelCacheInvalidationEvent.catalog_published(
        org_id=1,
        catalog_version=3,
        model_entry_ids=[7, 8],
        provider_ids=[2],
    )

    await cache.invalidate(event)

    assert event.event_type == "catalog_published"
    assert event.catalog_version == 3
    assert event.model_entry_ids == (7, 8)
    assert event.provider_ids == (2,)
    assert await redis.get(cache.revision_key(1)) == "1"
    assert metrics.count(cache_name="model_catalog", event="invalidation_success") == 1
    assert metrics.durations_ms[("model_catalog", "invalidation_success")]


@pytest.mark.asyncio
async def test_catalog_cache_waits_for_lock_then_reports_throttling() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelCatalogCache(redis, wait_timeout_seconds=0.01, metrics=metrics)
    lock = cache.lock_key(1, 9, "0")
    await redis.set(lock, "another-worker", nx=True, ex=4)

    async def loader():
        raise AssertionError("loader must not run while another worker owns the lock")

    with pytest.raises(ModelCatalogCacheFillThrottledError):
        await cache.get_or_build(org_id=1, user_id=9, loader=loader)
    assert metrics.count(cache_name="model_catalog", event="miss") == 1
    assert metrics.count(cache_name="model_catalog", event="refill_throttled") == 1


@pytest.mark.asyncio
async def test_catalog_cache_records_refill_failure_metric() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelCatalogCache(redis, metrics=metrics)

    async def loader():
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError):
        await cache.get_or_build(org_id=1, user_id=9, loader=loader)

    assert metrics.count(cache_name="model_catalog", event="miss") == 1
    assert metrics.count(cache_name="model_catalog", event="refill_failure", error_type="RuntimeError") == 1
    assert metrics.durations_ms[("model_catalog", "refill_failure")]


@pytest.mark.asyncio
async def test_catalog_cache_opens_short_circuit_after_repeated_refill_failures() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelCatalogCache(
        redis,
        circuit_breaker_failure_threshold=2,
        circuit_breaker_cooldown_seconds=60,
        metrics=metrics,
    )
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError):
        await cache.get_or_build(org_id=1, user_id=9, loader=loader)
    with pytest.raises(RuntimeError):
        await cache.get_or_build(org_id=1, user_id=9, loader=loader)
    with pytest.raises(ModelCatalogCacheCircuitOpenError):
        await cache.get_or_build(org_id=1, user_id=9, loader=loader)

    assert calls == 2
    assert metrics.count(cache_name="model_catalog", event="refill_failure", error_type="RuntimeError") == 2
    assert metrics.count(cache_name="model_catalog", event="circuit_open") == 1


@pytest.mark.asyncio
async def test_catalog_cache_prewarm_populates_current_revision() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelCatalogCache(redis, catalog_ttl_seconds=300, metrics=metrics)
    await redis.set(cache.revision_key(1), "4")
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return [{"model_entry_id": 7, "display_name": "Model A"}]

    assert await cache.prewarm_catalog(org_id=1, user_id=9, loader=loader) is True

    assert calls == 1
    assert json.loads(await redis.get(cache.catalog_key(1, 9, "4"))) == [
        {"model_entry_id": 7, "display_name": "Model A"}
    ]
    assert metrics.count(cache_name="model_catalog", event="prewarm_success") == 1
    assert metrics.count(cache_name="model_catalog", event="refill_success") == 1


@pytest.mark.asyncio
async def test_catalog_cache_prewarm_failure_is_bounded() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelCatalogCache(redis, metrics=metrics)

    async def loader():
        raise RuntimeError("database unavailable")

    assert await cache.prewarm_catalog(org_id=1, user_id=9, loader=loader) is False

    assert metrics.count(cache_name="model_catalog", event="prewarm_failure", error_type="RuntimeError") == 1
    assert metrics.count(cache_name="model_catalog", event="refill_failure", error_type="RuntimeError") == 1


@pytest.mark.asyncio
async def test_route_cache_reuses_exact_run_without_storing_plaintext_token() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelRouteCache(redis, metrics=metrics)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return {"runtimeTokenDigest": "digest", "providerApiKeyEncrypted": "local-v1:encrypted"}

    first = await cache.get_or_build(runtime_workspace_id=4, runtime_run_id="rr-1", loader=loader)
    second = await cache.get_or_build(runtime_workspace_id=4, runtime_run_id="rr-1", loader=loader)

    assert first == second
    assert calls == 1
    assert "plaintext-token" not in json.dumps(redis.values)
    assert metrics.count(cache_name="model_route", event="miss") == 1
    assert metrics.count(cache_name="model_route", event="hit") == 1
    assert metrics.count(cache_name="model_route", event="refill_success") == 1


@pytest.mark.asyncio
async def test_route_cache_invalidation_event_deletes_exact_run_key() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelRouteCache(redis, metrics=metrics)
    key = cache.route_key(4, "rr-1")
    await redis.set(key, json.dumps({"runtimeTokenDigest": "digest"}), ex=30)

    await cache.invalidate(ModelCacheInvalidationEvent.run_finished(org_id=1, runtime_workspace_id=4, runtime_run_id="rr-1"))

    assert await redis.get(key) is None
    assert metrics.count(cache_name="model_route", event="invalidation_success") == 1
    assert metrics.durations_ms[("model_route", "invalidation_success")]


@pytest.mark.asyncio
async def test_route_cache_invalidation_is_safe_to_consume_more_than_once() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelRouteCache(redis, metrics=metrics)
    event = ModelCacheInvalidationEvent.run_finished(
        org_id=1,
        runtime_workspace_id=4,
        runtime_run_id="rr-1",
    )
    await redis.set(cache.route_key(4, "rr-1"), json.dumps({"runtimeTokenDigest": "digest"}), ex=30)

    await cache.invalidate(event)
    await cache.invalidate(event)

    assert await redis.get(cache.route_key(4, "rr-1")) is None
    assert metrics.count(cache_name="model_route", event="invalidation_success") == 2


@pytest.mark.asyncio
async def test_application_active_run_pointer_is_short_lived_and_run_scoped() -> None:
    redis = FakeRedis()
    cache = RedisModelRouteCache(redis, route_ttl_seconds=30)

    await cache.publish_application_active_run(
        runtime_workspace_id=41,
        runtime_run_id="app_run_old",
        model_entry_id=7,
    )

    key = cache.application_active_run_key(41, 7)
    assert await cache.get_application_active_run_id(
        runtime_workspace_id=41,
        model_entry_id=7,
    ) == "app_run_old"
    assert 27 <= redis.expirations[key] <= 33


@pytest.mark.asyncio
async def test_workspace_route_retirement_keeps_newer_concurrent_run() -> None:
    redis = FakeRedis()
    cache = RedisModelRouteCache(redis)
    await redis.set(
        cache.route_key(51, "rr-old"),
        json.dumps({
            "runtimeTokenDigest": "old",
            "allowed_model_entry_id": 7,
        }),
        ex=30,
    )
    await redis.set(
        cache.route_key(51, "rr-new"),
        json.dumps({
            "runtimeTokenDigest": "new",
            "allowed_model_entry_id": 7,
        }),
        ex=30,
    )
    await cache.publish_workspace_active_run(
        runtime_workspace_id=51,
        runtime_run_id="rr-new",
        model_entry_id=7,
    )

    await cache.invalidate_workspace_active_run(
        runtime_workspace_id=51,
        runtime_run_id="rr-old",
    )

    assert await cache.get_workspace_active_run_id(
        runtime_workspace_id=51,
        model_entry_id=7,
    ) == "rr-new"
    assert await redis.get(cache.route_key(51, "rr-old")) is None
    assert await redis.get(cache.route_key(51, "rr-new")) is not None


@pytest.mark.asyncio
async def test_workspace_active_runs_are_partitioned_by_model() -> None:
    redis = FakeRedis()
    cache = RedisModelRouteCache(redis)

    await cache.publish_workspace_active_run(
        runtime_workspace_id=51,
        runtime_run_id="rr-model-7",
        model_entry_id=7,
    )
    await cache.publish_workspace_active_run(
        runtime_workspace_id=51,
        runtime_run_id="rr-model-8",
        model_entry_id=8,
    )

    assert await cache.get_workspace_active_run_id(
        runtime_workspace_id=51,
        model_entry_id=7,
    ) == "rr-model-7"
    assert await cache.get_workspace_active_run_id(
        runtime_workspace_id=51,
        model_entry_id=8,
    ) == "rr-model-8"
    assert await cache.get_workspace_active_run_id(
        runtime_workspace_id=51,
        model_entry_id=None,
    ) == "rr-model-8"


@pytest.mark.asyncio
async def test_application_route_retirement_keeps_newer_concurrent_run() -> None:
    redis = FakeRedis()
    cache = RedisModelRouteCache(redis)
    await redis.set(
        cache.route_key(41, "app_run_old"),
        json.dumps({
            "runtimeTokenDigest": "old",
            "allowed_model_entry_id": 7,
        }),
        ex=30,
    )
    await redis.set(
        cache.route_key(41, "app_run_new"),
        json.dumps({
            "runtimeTokenDigest": "new",
            "allowed_model_entry_id": 7,
        }),
        ex=30,
    )
    await cache.publish_application_active_run(
        runtime_workspace_id=41,
        runtime_run_id="app_run_new",
        model_entry_id=7,
    )

    await cache.invalidate_application_active_run(
        runtime_workspace_id=41,
        runtime_run_id="app_run_old",
    )

    assert await cache.get_application_active_run_id(
        runtime_workspace_id=41,
        model_entry_id=7,
    ) == "app_run_new"
    assert await redis.get(cache.route_key(41, "app_run_old")) is None
    assert await redis.get(cache.route_key(41, "app_run_new")) is not None


@pytest.mark.asyncio
async def test_route_cache_lock_timeout_is_explicit() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelRouteCache(redis, wait_timeout_seconds=0.01, metrics=metrics)
    await redis.set(cache.lock_key(4, "rr-1"), "another-worker", nx=True, ex=4)

    with pytest.raises(ModelRouteCacheFillThrottledError):
        await cache.get_or_build(runtime_workspace_id=4, runtime_run_id="rr-1", loader=lambda: None)
    assert metrics.count(cache_name="model_route", event="miss") == 1
    assert metrics.count(cache_name="model_route", event="refill_throttled") == 1


@pytest.mark.asyncio
async def test_route_cache_records_refill_failure_metric() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelRouteCache(redis, metrics=metrics)

    async def loader():
        raise RuntimeError("route query failed")

    with pytest.raises(RuntimeError):
        await cache.get_or_build(runtime_workspace_id=4, runtime_run_id="rr-1", loader=loader)

    assert metrics.count(cache_name="model_route", event="miss") == 1
    assert metrics.count(cache_name="model_route", event="refill_failure", error_type="RuntimeError") == 1
    assert metrics.durations_ms[("model_route", "refill_failure")]


@pytest.mark.asyncio
async def test_route_cache_opens_short_circuit_after_repeated_refill_failures() -> None:
    redis = FakeRedis()
    metrics = ModelCacheMetricsRecorder()
    cache = RedisModelRouteCache(
        redis,
        circuit_breaker_failure_threshold=2,
        circuit_breaker_cooldown_seconds=60,
        metrics=metrics,
    )
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        raise RuntimeError("route query failed")

    with pytest.raises(RuntimeError):
        await cache.get_or_build(runtime_workspace_id=4, runtime_run_id="rr-1", loader=loader)
    with pytest.raises(RuntimeError):
        await cache.get_or_build(runtime_workspace_id=4, runtime_run_id="rr-1", loader=loader)
    with pytest.raises(ModelRouteCacheCircuitOpenError):
        await cache.get_or_build(runtime_workspace_id=4, runtime_run_id="rr-1", loader=loader)

    assert calls == 2
    assert metrics.count(cache_name="model_route", event="refill_failure", error_type="RuntimeError") == 2
    assert metrics.count(cache_name="model_route", event="circuit_open") == 1
