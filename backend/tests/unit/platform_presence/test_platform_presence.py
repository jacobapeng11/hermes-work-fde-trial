from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from redis.exceptions import RedisError

from app.models.identity import User
from app.services.platform_presence import (
    PlatformPresenceDirectoryService,
    PlatformPresenceMonitor,
    PlatformPresenceUser,
    SqlAlchemyCrossDayPresenceRecorder,
    _business_day_start,
    session_started_before_business_day,
)
from app.services.platform_session import (
    InMemoryPlatformSessionStore,
    PlatformPresenceSummary,
    PlatformSessionUnavailable,
    RedisPlatformSessionStore,
)
from app.services.runtime_workspace import Principal


async def test_in_memory_presence_splits_clients_and_honors_window() -> None:
    now = 1_000.0
    store = InMemoryPlatformSessionStore(idle_ttl_seconds=180, now=lambda: now)
    first = await store.create(Principal(org_id=1, user_id=7))
    second = await store.create(Principal(org_id=1, user_id=7))
    third = await store.create(Principal(org_id=1, user_id=8))
    other_org = await store.create(Principal(org_id=2, user_id=9))

    await store.record_client_presence(first.session_id, first.principal, client_type="web")
    await store.record_client_presence(second.session_id, second.principal, client_type="web")
    await store.record_client_presence(third.session_id, third.principal, client_type="desktop")
    await store.record_client_presence(other_org.session_id, other_org.principal, client_type="desktop")

    summary = await store.presence_summary(1, window_seconds=120)

    assert summary.web_user_ids == frozenset({7})
    assert summary.desktop_user_ids == frozenset({8})
    assert summary.window_seconds == 120

    now = 1_130.0
    await store.record_client_presence(
        first.session_id,
        first.principal,
        client_type="desktop",
    )

    stale_summary = await store.presence_summary(1, window_seconds=120)

    assert stale_summary.web_user_ids == frozenset()
    assert stale_summary.desktop_user_ids == frozenset({7})


class FakePresenceRedis:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.values: dict[str, tuple[str, int]] = {}

    async def set(self, key: str, value: str, *, ex: int) -> None:
        if self.unavailable:
            raise RedisError("unavailable")
        self.values[key] = (value, int(ex))

    async def delete(self, *keys: str) -> None:
        if self.unavailable:
            raise RedisError("unavailable")
        for key in keys:
            self.values.pop(key, None)

    async def scan_iter(self, *, match: str, count: int):
        assert match == "hermes:platform-client-presence:*"
        assert count == 100
        if self.unavailable:
            raise RedisError("unavailable")
        for key in sorted(self.values):
            if key.startswith(match[:-1]):
                yield key

    def pipeline(self, *, transaction: bool):
        assert transaction is False
        return FakePresencePipeline(self)


class FakePresencePipeline:
    def __init__(self, redis: FakePresenceRedis) -> None:
        self.redis = redis
        self.operations: list[str] = []

    def get(self, key: str) -> None:
        self.operations.append(key)

    def ttl(self, key: str) -> None:
        self.operations.append(key)

    async def execute(self) -> list[object]:
        result: list[object] = []
        for key in self.operations:
            value, ttl = self.redis.values[key]
            result.append(value if len(result) % 2 == 0 else ttl)
        return result


def _redis_presence(
    redis: FakePresenceRedis,
    *,
    session_id: str,
    org_id: int,
    user_id: int,
    client_type: str,
    ttl: int,
) -> None:
    redis.values[f"hermes:platform-client-presence:{session_id}"] = (
        json.dumps({
            "session_id": session_id,
            "org_id": org_id,
            "user_id": user_id,
            "client_type": client_type,
        }),
        ttl,
    )


async def test_redis_presence_splits_clients_and_uses_ttl() -> None:
    redis = FakePresenceRedis()
    store = RedisPlatformSessionStore(redis, idle_ttl_seconds=180)  # type: ignore[arg-type]
    _redis_presence(redis, session_id="a", org_id=1, user_id=7, client_type="web", ttl=175)
    _redis_presence(redis, session_id="b", org_id=1, user_id=7, client_type="web", ttl=120)
    _redis_presence(redis, session_id="c", org_id=1, user_id=8, client_type="desktop", ttl=175)
    _redis_presence(redis, session_id="d", org_id=1, user_id=10, client_type="web", ttl=50)
    _redis_presence(redis, session_id="e", org_id=2, user_id=9, client_type="desktop", ttl=175)

    summary = await store.presence_summary(1, window_seconds=60)

    assert summary.web_user_ids == frozenset({7})
    assert summary.desktop_user_ids == frozenset({8})
    assert summary.observed_at.tzinfo is not None


async def test_redis_records_and_revokes_client_presence() -> None:
    redis = FakePresenceRedis()
    store = RedisPlatformSessionStore(redis, idle_ttl_seconds=180)  # type: ignore[arg-type]
    session = await store.create(Principal(org_id=1, user_id=7))

    await store.record_client_presence(
        session.session_id,
        session.principal,
        client_type="desktop",
    )
    assert store._client_presence_key(session.session_id) in redis.values

    await store.revoke(session.session_id)
    assert store._key(session.session_id) not in redis.values
    assert store._client_presence_key(session.session_id) not in redis.values


async def test_redis_presence_fails_closed_when_store_is_unavailable() -> None:
    store = RedisPlatformSessionStore(
        FakePresenceRedis(unavailable=True),  # type: ignore[arg-type]
        idle_ttl_seconds=180,
    )

    with pytest.raises(PlatformSessionUnavailable):
        await store.presence_summary(1, window_seconds=120)


class CountingPresenceStore:
    def __init__(self) -> None:
        self.calls = 0

    async def presence_summary(
        self,
        org_id: int,
        *,
        window_seconds: int,
    ) -> PlatformPresenceSummary:
        self.calls += 1
        return PlatformPresenceSummary(
            web_user_ids=frozenset({org_id}),
            desktop_user_ids=frozenset({org_id + 1}),
            window_seconds=window_seconds,
            observed_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
        )


async def test_presence_monitor_caches_org_scoped_results() -> None:
    store = CountingPresenceStore()
    current_time = 0.0
    monitor = PlatformPresenceMonitor(
        store,  # type: ignore[arg-type]
        window_seconds=120,
        cache_ttl_seconds=5,
        now=lambda: current_time,
    )

    first = await monitor.summary(org_id=1)
    current_time = 4
    cached = await monitor.summary(org_id=1)
    other_org = await monitor.summary(org_id=2)
    current_time = 6
    refreshed = await monitor.summary(org_id=1)

    assert first is cached
    assert first.web_user_ids == frozenset({1})
    assert other_org.web_user_ids == frozenset({2})
    assert refreshed.web_user_ids == frozenset({1})
    assert store.calls == 3


async def test_directory_service_formats_names_and_daily_users() -> None:
    service = PlatformPresenceDirectoryService(object())  # type: ignore[arg-type]

    async def daily_login_users(_org_id: int, *, since_utc):
        assert since_utc.tzinfo is None
        return (
            PlatformPresenceUser(
                user_id=3,
                name="王五",
                user_code="10003",
                last_login_at=since_utc,
            ),
            PlatformPresenceUser(
                user_id=1,
                name="张三",
                user_code="10001",
                last_login_at=since_utc,
            ),
        )

    async def users_by_id(_org_id: int, user_ids):
        return {
            int(user_id): PlatformPresenceUser(
                user_id=int(user_id),
                name="张三" if user_id == 1 else "李四",
                user_code="10001" if user_id == 1 else None,
            )
            for user_id in user_ids
        }

    service._daily_login_users = daily_login_users  # type: ignore[method-assign]
    service._users_by_id = users_by_id  # type: ignore[method-assign]
    presence = PlatformPresenceSummary(
        web_user_ids=frozenset({1, 2}),
        desktop_user_ids=frozenset({1}),
        window_seconds=120,
        observed_at=datetime(2026, 10, 13, 8, 30, tzinfo=timezone.utc),
    )

    dashboard = await service.dashboard(presence, org_id=1)

    assert [user.display_name for user in dashboard.web_users] == [
        "张三（10001）",
        "李四（工号缺失）",
    ]
    assert [user.display_name for user in dashboard.desktop_users] == ["张三（10001）"]
    assert [user.display_name for user in dashboard.daily_login_users] == [
        "王五（10003）",
        "张三（10001）",
    ]
    assert dashboard.daily_login_since.tzinfo is not None


def test_business_day_start_converts_shanghai_midnight_to_aware_datetime() -> None:
    since = _business_day_start(datetime(2026, 10, 12, 16, 30, tzinfo=timezone.utc))

    assert since.isoformat() == "2026-10-13T00:00:00+08:00"


def test_session_before_business_day_is_cross_day_presence() -> None:
    now = datetime(2026, 10, 12, 16, 30, tzinfo=timezone.utc)
    previous_day_session = int(
        datetime(2026, 10, 12, 15, 30, tzinfo=timezone.utc).timestamp()
    )
    current_day_session = int(
        datetime(2026, 10, 12, 17, 30, tzinfo=timezone.utc).timestamp()
    )

    assert session_started_before_business_day(
        previous_day_session,
        now=now,
    )
    assert not session_started_before_business_day(
        current_day_session,
        now=now,
    )


async def test_daily_login_query_includes_cross_day_presence_contract() -> None:
    class FakeScalars:
        def __init__(self, users):
            self.users = users

        def all(self):
            return self.users

    class FakeResult:
        def __init__(self, users):
            self.users = users

        def scalars(self):
            return FakeScalars(self.users)

    class FakeDb:
        async def execute(self, statement):
            sql = str(statement)
            assert '"user".last_login_at >=' in sql
            assert '"user".last_daily_presence_at >=' in sql
            return FakeResult([
                User(
                    id=2,
                    org_id=1,
                    name="跨天在线",
                    user_code="10002",
                    status="active",
                    last_login_at=None,
                    last_daily_presence_at=datetime.now(timezone.utc),
                ),
            ])

    users = await PlatformPresenceDirectoryService(FakeDb())._daily_login_users(
        1,
        since_utc=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    assert [user.user_id for user in users] == [2]


async def test_cross_day_presence_recorder_uses_daily_idempotence_contract() -> None:
    class FakeResult:
        def __init__(self, rowcount):
            self.rowcount = rowcount

    class FakeDb:
        def __init__(self):
            self.rowcounts = iter((1, 0))
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, statement):
            sql = str(statement)
            assert '"user".last_daily_presence_at IS NULL' in sql
            assert '"user".last_daily_presence_at <' in sql
            return FakeResult(next(self.rowcounts))

        async def commit(self):
            self.commits += 1

    factory = FakeDb()
    recorder = SqlAlchemyCrossDayPresenceRecorder(lambda: factory)

    assert await recorder.record(Principal(org_id=1, user_id=7))
    assert not await recorder.record(Principal(org_id=1, user_id=7))
    assert factory.commits == 2
