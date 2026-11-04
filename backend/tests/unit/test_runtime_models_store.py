from types import SimpleNamespace

import pytest

from app.services.model_catalog_cache import RedisModelCatalogCache
from app.services.model_defaults import DEFAULT_MODEL_CONTEXT_LENGTH
from app.services.runtime_models import SqlAlchemyRuntimeModelStore


class Result:
    def __init__(self, *, rows=None, scalar=None, count=None) -> None:
        self.rows = rows or []
        self.scalar = scalar
        self.count = count

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.scalar

    def scalar_one(self):
        return self.count


class FakeDb:
    def __init__(self, result: Result) -> None:
        self.result = result
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return self.result


class FakeCache:
    def __init__(self) -> None:
        self.prewarm_calls: list[dict] = []

    async def get_or_build(self, *, org_id, user_id, loader):
        return await loader()

    async def prewarm_catalog(self, *, org_id, user_id, loader):
        self.prewarm_calls.append({"org_id": org_id, "user_id": user_id})
        await loader()
        return True


class StaleNullContextCache:
    async def get_or_build(self, *, org_id, user_id, loader):
        return [
            {
                "model_entry_id": 19,
                "model_key": "deepseek-v4-flash",
                "display_name": "deepseek-v4-flash",
                "provider_id": 3,
                "provider_display_name": "DeepSeek",
                "context_length": None,
                "tags": [],
                "is_default": False,
            }
        ]


@pytest.mark.asyncio
async def test_sqlalchemy_runtime_model_store_maps_only_active_catalog_fields() -> None:
    entry = SimpleNamespace(
        id=7,
        model_key="model-a",
        display_name="Model A",
        context_length=8192,
        tags="chat, fast",
        is_default=True,
    )
    provider = SimpleNamespace(id=3, display_name="Provider A")
    db = FakeDb(Result(rows=[(entry, provider)]))

    records = await SqlAlchemyRuntimeModelStore(db, cache=FakeCache()).list_visible_models(org_id=1, user_id=9)

    assert records[0].model_entry_id == 7
    assert records[0].provider_display_name == "Provider A"
    assert records[0].tags == ["chat", "fast"]
    assert records[0].is_default is True
    assert "model_entry.status" in str(db.statement)
    assert "model_provider.status" in str(db.statement)
    assert "model_upgrade_log" in str(db.statement)
    assert "catalog_version" in str(db.statement)


@pytest.mark.asyncio
async def test_sqlalchemy_runtime_model_store_defaults_missing_context_length() -> None:
    entry = SimpleNamespace(
        id=19,
        model_key="deepseek-v4-flash",
        display_name="deepseek-v4-flash",
        context_length=None,
        tags="chat, fast",
    )
    provider = SimpleNamespace(id=3, display_name="DeepSeek")
    db = FakeDb(Result(rows=[(entry, provider)]))

    records = await SqlAlchemyRuntimeModelStore(db, cache=FakeCache()).list_visible_models(org_id=1, user_id=9)

    assert records[0].model_key == "deepseek-v4-flash"
    assert records[0].context_length == DEFAULT_MODEL_CONTEXT_LENGTH


@pytest.mark.asyncio
async def test_sqlalchemy_runtime_model_store_defaults_stale_cached_null_context_length() -> None:
    db = FakeDb(Result(rows=[]))

    records = await SqlAlchemyRuntimeModelStore(db, cache=StaleNullContextCache()).list_visible_models(
        org_id=1,
        user_id=9,
    )

    assert records[0].model_key == "deepseek-v4-flash"
    assert records[0].context_length == DEFAULT_MODEL_CONTEXT_LENGTH


@pytest.mark.asyncio
async def test_sqlalchemy_runtime_model_store_prewarms_visible_model_cache() -> None:
    entry = SimpleNamespace(
        id=7,
        model_key="model-a",
        display_name="Model A",
        context_length=8192,
        tags="chat",
    )
    provider = SimpleNamespace(id=3, display_name="Provider A")
    db = FakeDb(Result(rows=[(entry, provider)]))
    cache = FakeCache()

    assert await SqlAlchemyRuntimeModelStore(db, cache=cache).prewarm_visible_models(org_id=1, user_id=9) is True

    assert cache.prewarm_calls == [{"org_id": 1, "user_id": 9}]
    assert "model_entry.status" in str(db.statement)
    assert "model_provider.status" in str(db.statement)


@pytest.mark.asyncio
async def test_sqlalchemy_runtime_model_store_returns_session_model_and_active_run_count() -> None:
    session = SimpleNamespace(id=11, session_model_entry_id=7)
    db = FakeDb(Result(scalar=session, count=1))

    state = await SqlAlchemyRuntimeModelStore(db).get_session_model_state(
        org_id=1,
        user_id=9,
        runtime_workspace_id=4,
        runtime_session_id="rs-4-1",
    )

    assert state is not None
    assert state.current_model_entry_id == 7
    assert state.next_model_entry_id == 7
    assert state.active_run_count == 1
