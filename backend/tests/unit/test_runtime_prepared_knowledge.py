from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import app.services.runtime_prepared_knowledge as prepared_module
from app.services.runtime_prepared_knowledge import (
    PreparedKnowledgeStoreUnavailable,
    RedisRuntimePreparedKnowledgeStore,
    RuntimePreparedKnowledgeKey,
    RuntimePreparedKnowledgeService,
)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.operations: list[tuple[str, tuple, dict]] = []

    def hset(self, *args, **kwargs):
        self.operations.append(("hset", args, kwargs))
        return self

    def expire(self, *args, **kwargs):
        self.operations.append(("expire", args, kwargs))
        return self

    async def execute(self):
        for name, args, kwargs in self.operations:
            await getattr(self.redis, name)(*args, **kwargs)
        return [True] * len(self.operations)


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.expirations: dict[str, int] = {}

    def pipeline(self, *, transaction: bool):
        assert transaction is True
        return FakePipeline(self)

    async def hget(self, key: str, field: str):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key: str, *, mapping: dict[str, str]):
        self.hashes.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def expire(self, key: str, ttl_seconds: int):
        self.expirations[key] = ttl_seconds
        return True


def _key(token: str = "runtime-secret") -> RuntimePreparedKnowledgeKey:
    return RuntimePreparedKnowledgeKey.workspace(
        runtime_workspace_id=43,
        runtime_session_id="rs-43-a",
        runtime_agent_profile_id=7,
        runtime_token=token,
    )


@pytest.mark.asyncio
async def test_prepared_knowledge_bundle_is_session_scoped_and_excludes_token() -> None:
    redis = FakeRedis()
    store = RedisRuntimePreparedKnowledgeStore(redis, ttl_seconds=180)
    key = _key()

    await store.put_bundle(
        key,
        {
            "enterprise": {"enabled": True, "mode": "full"},
            "personal": {
                "enabled": False,
                "knowledge_bases": [],
                "revision": "pkb:v2:none",
            },
        },
    )

    redis_key = next(iter(redis.hashes))
    serialized = json.dumps(redis.hashes, ensure_ascii=False)
    assert "runtime-secret" not in redis_key
    assert "runtime-secret" not in serialized
    assert redis.expirations[redis_key] == 180
    assert await store.get(key, "enterprise") == {
        "enabled": True,
        "mode": "full",
    }
    assert await store.get(key, "personal") == {
        "enabled": False,
        "knowledge_bases": [],
        "revision": "pkb:v2:none",
    }


@pytest.mark.asyncio
async def test_prepared_knowledge_does_not_cross_session_profile_or_token() -> None:
    redis = FakeRedis()
    store = RedisRuntimePreparedKnowledgeStore(redis)
    await store.put(
        _key(),
        "enterprise",
        {"enabled": True, "mode": "full"},
    )

    assert (
        await store.get(
            RuntimePreparedKnowledgeKey.workspace(
                runtime_workspace_id=43,
                runtime_session_id="rs-43-b",
                runtime_agent_profile_id=7,
                runtime_token="runtime-secret",
            ),
            "enterprise",
        )
        is None
    )
    assert (
        await store.get(
            RuntimePreparedKnowledgeKey.workspace(
                runtime_workspace_id=43,
                runtime_session_id="rs-43-a",
                runtime_agent_profile_id=8,
                runtime_token="runtime-secret",
            ),
            "enterprise",
        )
        is None
    )
    assert await store.get(_key("rotated-secret"), "enterprise") is None


@pytest.mark.asyncio
async def test_load_or_prepare_uses_hot_state_without_calling_loader() -> None:
    redis = FakeRedis()
    store = RedisRuntimePreparedKnowledgeStore(redis)
    service = RuntimePreparedKnowledgeService(store)
    key = _key()
    await store.put(
        key,
        "enterprise",
        {"enabled": True, "mode": "full"},
    )

    async def unexpected_loader():
        raise AssertionError("hot lookup must not access the database")

    state = await service.load_or_prepare(
        key=key,
        kind="enterprise",
        loader=unexpected_loader,
        serializer=lambda value: value,
    )

    assert state == {"enabled": True, "mode": "full"}


@pytest.mark.asyncio
async def test_application_hot_context_returns_without_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    store = RedisRuntimePreparedKnowledgeStore(redis)
    service = RuntimePreparedKnowledgeService(store)
    key = RuntimePreparedKnowledgeKey.application_agent(
        application_agent_id=31,
        runtime_session_id="studio-31-7-session",
        runtime_token="runtime-secret",
    )
    await store.put_bundle(
        key,
        {
            "enterprise": {"enabled": True, "mode": "full"},
            "personal": {
                "enabled": False,
                "knowledge_bases": [],
                "revision": "prepared",
            },
        },
    )
    def unexpected_database_access():
        raise AssertionError("hot application context must not access the database")

    monkeypatch.setattr(prepared_module, "AsyncSessionLocal", unexpected_database_access)

    await service.prepare_application_agent(
        application_agent_id=31,
        runtime_session_id="studio-31-7-session",
        runtime_run_id="app_run_31",
        runtime_token="runtime-secret",
    )


@pytest.mark.asyncio
async def test_application_channel_context_prepares_without_active_backend_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    service = RuntimePreparedKnowledgeService(
        RedisRuntimePreparedKnowledgeStore(redis)
    )
    statements: list[str] = []
    row = (
        SimpleNamespace(
            id=31,
            org_id=1,
            config={
                "enterprise_knowledge_enabled": True,
                "knowledge_bindings": [],
            },
        ),
        SimpleNamespace(api_key_encrypted="encrypted"),
        SimpleNamespace(id=41),
        SimpleNamespace(scopes=["responses:create"]),
        SimpleNamespace(id=51),
        SimpleNamespace(id=7, org_id=1, status="active", user_code="u7"),
    )

    class Result:
        def first(self) -> tuple[object, ...]:
            return row

        def scalars(self):
            return iter(())

        def scalar_one_or_none(self):
            return None

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, statement: object) -> Result:
            statements.append(str(statement))
            return Result()

    class Codec:
        def decrypt(self, _value: str) -> str:
            return "runtime-secret"

    monkeypatch.setattr(prepared_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(prepared_module, "RuntimeApiKeyCodec", Codec)

    await service.prepare_application_agent_channel(
        application_agent_id=31,
        application_client_id=41,
        subject_user_id=7,
        runtime_session_id="app-31-session",
        runtime_run_id="wecom-run-31",
        runtime_token="runtime-secret",
    )

    assert len(statements) == 2
    assert "JOIN application_agent_run " not in statements[0]
    assert "application_agent.status =" in statements[0]
    key = RuntimePreparedKnowledgeKey.application_agent(
        application_agent_id=31,
        runtime_session_id="app-31-session",
        runtime_token="runtime-secret",
    )
    assert await service.store.get(key, "enterprise") == {
        "enabled": True,
        "mode": "full",
    }
    personal = await service.store.get(key, "personal")
    assert personal is not None
    assert personal["enabled"] is False


@pytest.mark.asyncio
async def test_application_channel_context_rejects_wrong_runtime_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    service = RuntimePreparedKnowledgeService(
        RedisRuntimePreparedKnowledgeStore(redis)
    )
    row = (
        SimpleNamespace(id=31, org_id=1, config={}),
        SimpleNamespace(api_key_encrypted="encrypted"),
        SimpleNamespace(id=41),
        SimpleNamespace(scopes=["responses:create"]),
        SimpleNamespace(id=51),
        SimpleNamespace(id=7, org_id=1, status="active", user_code="u7"),
    )

    class Result:
        def first(self) -> tuple[object, ...]:
            return row

        def scalars(self):
            return iter(())

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _statement: object) -> Result:
            return Result()

    class Codec:
        def decrypt(self, _value: str) -> str:
            return "current-runtime-secret"

    monkeypatch.setattr(prepared_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(prepared_module, "RuntimeApiKeyCodec", Codec)

    with pytest.raises(
        PreparedKnowledgeStoreUnavailable,
        match="credential is unauthorized",
    ):
        await service.prepare_application_agent_channel(
            application_agent_id=31,
            application_client_id=41,
            subject_user_id=7,
            runtime_session_id="app-31-session",
            runtime_run_id="wecom-run-31",
            runtime_token="stale-runtime-secret",
        )

    assert redis.hashes == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_status",
    ["unpublished", "published", "disabled"],
)
async def test_application_studio_context_allows_creator_session(
    monkeypatch: pytest.MonkeyPatch,
    agent_status: str,
) -> None:
    redis = FakeRedis()
    service = RuntimePreparedKnowledgeService(
        RedisRuntimePreparedKnowledgeStore(redis)
    )
    key = RuntimePreparedKnowledgeKey.application_agent(
        application_agent_id=31,
        runtime_session_id="studio-31-7-session",
        runtime_token="runtime-secret",
    )
    await service.store.put_bundle(
        key,
        {
            "enterprise": {"enabled": False, "mode": "full"},
            "personal": {
                "enabled": True,
                "knowledge_bases": [{"id": 999}],
                "revision": "stale",
            },
        },
    )
    statements: list[str] = []
    row = (
        SimpleNamespace(
            id=31,
            org_id=1,
            created_by_user_id=7,
            status=agent_status,
            config={
                "enterprise_knowledge_enabled": True,
                "knowledge_bindings": [],
            },
        ),
        SimpleNamespace(api_key_encrypted="encrypted"),
        SimpleNamespace(id=51),
        SimpleNamespace(id=7, org_id=1, status="active", user_code="u7"),
    )

    class Result:
        def first(self) -> tuple[object, ...]:
            return row

        def scalars(self):
            return iter(())

        def scalar_one_or_none(self):
            return None

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, statement: object) -> Result:
            statements.append(str(statement))
            return Result()

    class Codec:
        def decrypt(self, _value: str) -> str:
            return "runtime-secret"

    monkeypatch.setattr(prepared_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(prepared_module, "RuntimeApiKeyCodec", Codec)

    await service.prepare_application_agent_studio(
        application_agent_id=31,
        subject_user_id=7,
        runtime_session_id="studio-31-7-session",
        runtime_token="runtime-secret",
    )

    assert len(statements) == 2
    assert "JOIN application_agent_run " not in statements[0]
    assert "application_agent.status =" not in statements[0]
    assert statements[0].count("application_agent.created_by_user_id") == 2
    assert await service.store.get(key, "enterprise") == {
        "enabled": True,
        "mode": "full",
    }
    personal = await service.store.get(key, "personal")
    assert personal is not None
    assert personal["enabled"] is False
    assert personal["knowledge_bases"] == []
    assert personal["revision"] != "stale"


@pytest.mark.asyncio
async def test_application_studio_context_rejects_wrong_runtime_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    service = RuntimePreparedKnowledgeService(
        RedisRuntimePreparedKnowledgeStore(redis)
    )
    row = (
        SimpleNamespace(
            id=31,
            org_id=1,
            created_by_user_id=7,
            status="unpublished",
            config={},
        ),
        SimpleNamespace(api_key_encrypted="encrypted"),
        SimpleNamespace(id=51),
        SimpleNamespace(id=7, org_id=1, status="active", user_code="u7"),
    )

    class Result:
        def first(self) -> tuple[object, ...]:
            return row

        def scalars(self):
            return iter(())

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _statement: object) -> Result:
            return Result()

    class Codec:
        def decrypt(self, _value: str) -> str:
            return "current-runtime-secret"

    monkeypatch.setattr(prepared_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(prepared_module, "RuntimeApiKeyCodec", Codec)

    with pytest.raises(
        PreparedKnowledgeStoreUnavailable,
        match="credential is unauthorized",
    ):
        await service.prepare_application_agent_studio(
            application_agent_id=31,
            subject_user_id=7,
            runtime_session_id="studio-31-7-session",
            runtime_token="stale-runtime-secret",
        )

    assert redis.hashes == {}
