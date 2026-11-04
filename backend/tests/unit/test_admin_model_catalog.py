from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.v1.admin.model_config import publish_model_config
from app.config import settings
from app.schemas.admin import AdminModelCatalogPublishRequest
from app.services.admin_auth import AdminPrincipal
from app.services.admin_model_catalog import (
    AdminModelCatalogError,
    AdminModelCatalogService,
    AdminModelConfigItem,
    ASYNC_MODEL_CATALOG_RELEASE_STATUSES,
    InMemoryAdminModelCatalogStore,
    ModelCatalogPublishIntent,
    SYNC_MODEL_CATALOG_RELEASE_STATUSES,
    SqlAlchemyAdminModelCatalogStore,
)
from app.services.model_defaults import DEFAULT_MODEL_CONTEXT_LENGTH
from app.services.model_catalog_cache import shared_model_cache_metrics


def entry(**overrides) -> AdminModelConfigItem:
    values = {
        "provider_key": "provider-a",
        "provider_display_name": "Provider A",
        "provider_type": "openai_compatible",
        "api_endpoint": "https://provider.example/v1",
        "api_key": "secret-value",
        "model_key": "model-a",
        "model_display_name": "Model A",
        "context_length": 8192,
        "tags": ["chat", "fast"],
        "is_restricted": False,
    }
    values.update(overrides)
    return AdminModelConfigItem(**values)


def test_sqlalchemy_catalog_publish_disables_providers_missing_from_publish_payload() -> None:
    source = (Path(__file__).resolve().parents[2] / "app/services/admin_model_catalog.py").read_text(encoding="utf-8")

    assert "included_provider_ids.add(int(provider.id))" in source
    assert "for provider in providers:" in source
    assert 'provider.status = "deleted"' in source
    assert 'model.status = "deleted"' in source


def test_sqlalchemy_catalog_new_provider_does_not_receive_model_runtime_capabilities() -> None:
    source = (Path(__file__).resolve().parents[2] / "app/services/admin_model_catalog.py").read_text(encoding="utf-8")
    new_provider_block = source[source.index("provider = ModelProvider("):source.index("self.db.add(provider)")]

    assert "runtime_capabilities" not in new_provider_block


def test_sync_catalog_publish_statuses_reserve_pending_for_future_async_queue() -> None:
    assert SYNC_MODEL_CATALOG_RELEASE_STATUSES == frozenset({"publishing", "published", "failed"})
    assert ASYNC_MODEL_CATALOG_RELEASE_STATUSES == frozenset({"pending"})


def test_admin_publish_schema_accepts_runtime_capability_wire_values() -> None:
    payload = AdminModelCatalogPublishRequest(
        entries=[
            {
                "provider_name": "provider-a",
                "provider_display_name": "Provider A",
                "api_endpoint": "https://provider.example/v1",
                "model_key": "model-a",
                "model_display_name": "Model A",
                "runtime_capabilities": {
                    "thinking_mode": "provider_default",
                    "reasoning_replay": "not_supported",
                },
            }
        ]
    )

    assert payload.entries[0].runtime_capabilities.model_dump(
        exclude_none=True
    ) == {
        "thinking_mode": "provider_default",
        "reasoning_replay": "not_supported",
    }


@pytest.mark.asyncio
async def test_catalog_publish_returns_monotonic_version_and_hash_without_secret() -> None:
    service = AdminModelCatalogService(InMemoryAdminModelCatalogStore())
    principal = AdminPrincipal(org_id=1, user_id=7, roles=("admin",))

    first = await service.publish_catalog(principal=principal, entries=[entry()])
    duplicate = await service.publish_catalog(principal=principal, entries=[entry()])
    second = await service.publish_catalog(principal=principal, entries=[entry(model_display_name="Model A2")])

    assert first.catalog_version == 1
    assert first.model_entry_ids == (1,)
    assert first.provider_ids == (1,)
    assert duplicate.release_id == first.release_id
    assert duplicate.catalog_version == first.catalog_version
    assert duplicate.model_entry_ids == first.model_entry_ids
    assert duplicate.provider_ids == first.provider_ids
    assert second.catalog_version == 2
    assert second.previous_catalog_version == 1
    assert len(first.config_hash) == 64
    assert "secret-value" not in first.config_hash


@pytest.mark.asyncio
async def test_catalog_publish_rejects_duplicate_or_unsafe_entries() -> None:
    service = AdminModelCatalogService(InMemoryAdminModelCatalogStore())
    principal = AdminPrincipal(org_id=1, user_id=7, roles=("admin",))

    with pytest.raises(AdminModelCatalogError, match="duplicate"):
        await service.publish_catalog(principal=principal, entries=[entry(), entry()])
    with pytest.raises(AdminModelCatalogError, match="http or https"):
        await service.publish_catalog(principal=principal, entries=[entry(api_endpoint="file:///tmp/model")])
    with pytest.raises(AdminModelCatalogError, match="active or disabled"):
        await service.publish_catalog(principal=principal, entries=[entry(model_status="deleted")])
    with pytest.raises(AdminModelCatalogError, match="default model must be active"):
        await service.publish_catalog(principal=principal, entries=[entry(model_status="disabled", is_default=True)])
    with pytest.raises(AdminModelCatalogError, match="only one default"):
        await service.publish_catalog(
            principal=principal,
            entries=[
                entry(is_default=True),
                entry(provider_key="provider-b", model_key="model-b", model_display_name="Model B", is_default=True),
            ],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_capabilities",
    [
        {"thinking_mode": "enabled", "reasoning_replay": "not_supported"},
        {"thinking_mode": "enabled", "reasoning_replay": "optional"},
        {"thinking_mode": "provider_default", "reasoning_replay": "required"},
        {"thinking_mode": "disabled", "reasoning_replay": "required"},
    ],
)
async def test_catalog_publish_rejects_ambiguous_runtime_capability_combinations(runtime_capabilities) -> None:
    service = AdminModelCatalogService(InMemoryAdminModelCatalogStore())
    principal = AdminPrincipal(org_id=1, user_id=7, roles=("admin",))

    with pytest.raises(AdminModelCatalogError, match="runtime capabilities"):
        await service.publish_catalog(
            principal=principal,
            entries=[entry(runtime_capabilities=runtime_capabilities)],
        )


@pytest.mark.asyncio
async def test_catalog_list_returns_configured_state_without_secret() -> None:
    store = InMemoryAdminModelCatalogStore()
    service = AdminModelCatalogService(store)
    principal = AdminPrincipal(org_id=1, user_id=7, roles=("admin",))

    await service.publish_catalog(principal=principal, entries=[entry(api_key="secret-value")])
    snapshot = await service.list_catalog(principal=principal)

    assert snapshot.latest_release is not None
    assert snapshot.latest_release.status == "published"
    assert snapshot.entries[0].provider_key == "provider-a"
    assert snapshot.entries[0].api_key_configured is True
    assert snapshot.entries[0].model_status == "active"
    assert snapshot.entries[0].is_default is False
    assert not hasattr(snapshot.entries[0], "api_key")
    assert not hasattr(snapshot.entries[0], "api_key_encrypted")


@pytest.mark.asyncio
async def test_catalog_list_retains_disabled_config_for_admin_governance() -> None:
    store = InMemoryAdminModelCatalogStore()
    service = AdminModelCatalogService(store)
    principal = AdminPrincipal(org_id=1, user_id=7, roles=("admin",))

    await service.publish_catalog(principal=principal, entries=[entry(model_status="disabled", api_key="secret-value")])
    snapshot = await service.list_catalog(principal=principal)

    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].model_key == "model-a"
    assert snapshot.entries[0].model_status == "disabled"
    assert snapshot.entries[0].api_key_configured is True


@pytest.mark.asyncio
async def test_catalog_publish_defaults_context_length_and_allows_same_provider_multiple_models() -> None:
    store = InMemoryAdminModelCatalogStore()
    service = AdminModelCatalogService(store)
    principal = AdminPrincipal(org_id=1, user_id=7, roles=("admin",))

    await service.publish_catalog(
        principal=principal,
        entries=[
            entry(
                model_key="deepseek-v4-pro",
                model_display_name="deepseek-v4-pro",
                context_length=None,
                is_default=True,
            ),
            entry(
                model_key="deepseek-v4-flash",
                model_display_name="deepseek-v4-flash",
                context_length=None,
            ),
        ],
    )
    snapshot = await service.list_catalog(principal=principal)

    assert {item.model_key for item in snapshot.entries} == {"deepseek-v4-pro", "deepseek-v4-flash"}
    assert {item.context_length for item in snapshot.entries} == {DEFAULT_MODEL_CONTEXT_LENGTH}
    assert [item.model_key for item in snapshot.entries if item.is_default] == ["deepseek-v4-pro"]


@pytest.mark.asyncio
async def test_catalog_publish_rejects_non_positive_context_length() -> None:
    service = AdminModelCatalogService(InMemoryAdminModelCatalogStore())
    principal = AdminPrincipal(org_id=1, user_id=7, roles=("admin",))

    with pytest.raises(AdminModelCatalogError, match="context length"):
        await service.publish_catalog(principal=principal, entries=[entry(context_length=0)])


@pytest.mark.asyncio
async def test_catalog_publish_hashes_and_returns_runtime_capabilities() -> None:
    store = InMemoryAdminModelCatalogStore()
    service = AdminModelCatalogService(store)
    principal = AdminPrincipal(org_id=1, user_id=7, roles=("admin",))
    await service.publish_catalog(
        principal=principal,
        entries=[entry(runtime_capabilities={
            "reasoning_replay": "required", "thinking_mode": "enabled",
        })],
    )
    snapshot = await service.list_catalog(principal=principal)
    assert snapshot.entries[0].runtime_capabilities == {
        "reasoning_replay": "required", "thinking_mode": "enabled",
    }


@pytest.mark.asyncio
async def test_catalog_publish_accepts_each_explicit_runtime_capability_mode() -> None:
    service = AdminModelCatalogService(InMemoryAdminModelCatalogStore())
    principal = AdminPrincipal(org_id=1, user_id=7, roles=("admin",))

    for capabilities in (
        {"thinking_mode": "enabled", "reasoning_replay": "required"},
        {"thinking_mode": "provider_default", "reasoning_replay": "not_supported"},
        {"thinking_mode": "disabled", "reasoning_replay": "not_supported"},
    ):
        await service.publish_catalog(principal=principal, entries=[entry(runtime_capabilities=capabilities)])


class FlakyPublishStore:
    def __init__(self, *, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls: list[dict] = []

    async def list_catalog(self, *, org_id: int):
        raise NotImplementedError

    async def publish_catalog(self, *, org_id: int, operator_id: int, entries: list[AdminModelConfigItem], config_hash: str):
        self.calls.append(
            {
                "org_id": org_id,
                "operator_id": operator_id,
                "entry_count": len(entries),
                "config_hash": config_hash,
            }
        )
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("transient publish failure")
        return ModelCatalogPublishIntent(
            release_id=17,
            catalog_version=3,
            previous_catalog_version=2,
            config_hash=config_hash,
            status="published",
            published_at=datetime.now(timezone.utc),
            model_entry_ids=(11,),
            provider_ids=(5,),
        )


@pytest.mark.asyncio
async def test_catalog_publish_retries_transient_transaction_failure_as_single_publish_unit() -> None:
    sleep_calls: list[float] = []

    async def sleep(delay: float) -> None:
        sleep_calls.append(delay)

    store = FlakyPublishStore(fail_times=2)
    service = AdminModelCatalogService(
        store,
        retry_base_delay_seconds=0.01,
        sleep=sleep,
    )

    intent = await service.publish_catalog(
        principal=AdminPrincipal(org_id=1, user_id=7, roles=("admin",)),
        entries=[entry()],
    )

    assert intent.catalog_version == 3
    assert len(store.calls) == 3
    assert {call["config_hash"] for call in store.calls} == {intent.config_hash}
    assert [call["entry_count"] for call in store.calls] == [1, 1, 1]
    assert sleep_calls == [0.01, 0.02]


@pytest.mark.asyncio
async def test_catalog_publish_stops_after_five_failed_attempts() -> None:
    sleep_calls: list[float] = []

    async def sleep(delay: float) -> None:
        sleep_calls.append(delay)

    store = FlakyPublishStore(fail_times=10)
    service = AdminModelCatalogService(
        store,
        max_publish_attempts=5,
        retry_base_delay_seconds=0.01,
        sleep=sleep,
    )

    with pytest.raises(RuntimeError, match="transient publish failure"):
        await service.publish_catalog(
            principal=AdminPrincipal(org_id=1, user_id=7, roles=("admin",)),
            entries=[entry()],
        )

    assert len(store.calls) == 5
    assert sleep_calls == [0.01, 0.02, 0.04, 0.08]


class FailingDb:
    def __init__(self) -> None:
        self.rollback_count = 0
        self.committed = False
        self.added = []

    async def execute(self, statement):
        raise RuntimeError("primary transaction failed")

    async def rollback(self):
        self.rollback_count += 1

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_sqlalchemy_catalog_publish_records_failed_log_after_rollback() -> None:
    db = FailingDb()
    store = SqlAlchemyAdminModelCatalogStore(db)

    with pytest.raises(RuntimeError, match="primary transaction failed"):
        await store.publish_catalog(
            org_id=1,
            operator_id=7,
            entries=[entry()],
            config_hash="a" * 64,
        )

    assert db.rollback_count == 1
    assert db.committed is True
    assert len(db.added) == 1
    assert db.added[0].status == "failed"
    assert db.added[0].error_message == "primary transaction failed"


class Result:
    def __init__(self, *, rows=None, scalar=None) -> None:
        self.rows = rows or []
        self.scalar = scalar

    def all(self):
        return self.rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self.scalar


class ListDb:
    def __init__(self) -> None:
        provider = SimpleNamespace(
            id=3,
            provider_key="provider-a",
            display_name="Provider A",
            provider_type="openai_compatible",
            api_endpoint="https://provider.example/v1",
            api_key_encrypted="provider-v1:encrypted",
            status="active",
            catalog_version=2,
        )
        model = SimpleNamespace(
            id=5,
            model_key="model-a",
            display_name="Model A",
            context_length=8192,
            tags="fast, chat",
            is_restricted=False,
            is_default=False,
            status="active",
            catalog_version=2,
        )
        latest = SimpleNamespace(
            id=9,
            catalog_version=2,
            previous_catalog_version=1,
            config_hash="b" * 64,
            status="published",
            error_message=None,
            started_at=None,
            finished_at=None,
        )
        self.results = [
            Result(rows=[(provider, model)]),
            Result(scalar=latest),
            Result(rows=[]),
        ]
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_sqlalchemy_catalog_list_maps_safe_config_snapshot() -> None:
    db = ListDb()
    snapshot = await SqlAlchemyAdminModelCatalogStore(db).list_catalog(org_id=1)

    assert snapshot.latest_release is not None
    assert snapshot.latest_release.catalog_version == 2
    assert snapshot.entries[0].provider_id == 3
    assert snapshot.entries[0].api_key_configured is True
    assert snapshot.entries[0].tags == ["fast", "chat"]
    catalog_sql = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "model_provider.status IN ('active', 'disabled')" in catalog_sql
    assert "model_entry.status IN ('active', 'disabled')" in catalog_sql


class FailingPublishService:
    async def publish_catalog(self, *, principal, entries):
        raise RuntimeError("secret should not be exposed")


class CapturingAuditService:
    def __init__(self) -> None:
        self.records = []

    async def record(self, **kwargs):
        self.records.append(kwargs)


class SuccessfulPublishService:
    async def publish_catalog(self, *, principal, entries):
        return ModelCatalogPublishIntent(
            release_id=21,
            catalog_version=4,
            previous_catalog_version=3,
            config_hash="c" * 64,
            status="published",
            published_at=datetime.now(timezone.utc),
            model_entry_ids=(5,),
            provider_ids=(3,),
        )


class PrewarmDb:
    def __init__(self) -> None:
        self.results = [
            Result(rows=[]),
        ]

    async def execute(self, statement):
        if self.results:
            return self.results.pop(0)
        provider = SimpleNamespace(id=3, display_name="Provider A")
        model = SimpleNamespace(
            id=5,
            model_key="model-a",
            display_name="Model A",
            context_length=8192,
            tags="chat",
        )
        return Result(rows=[(model, provider)])


class PrewarmCache:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def invalidate(self, event) -> None:
        self.events.append(
            {
                "action": "invalidate",
                "event_type": event.event_type,
                "catalog_version": event.catalog_version,
                "model_entry_ids": event.model_entry_ids,
                "provider_ids": event.provider_ids,
            }
        )

    async def prewarm_catalog(
        self,
        *,
        org_id,
        user_id,
        loader,
        visibility_fingerprint=None,
    ):
        payload = await loader()
        self.events.append(
            {
                "action": "prewarm",
                "org_id": org_id,
                "user_id": user_id,
                "model_entry_ids": [item["model_entry_id"] for item in payload],
            }
        )
        return True


@pytest.mark.asyncio
async def test_publish_route_invalidates_then_prewarm_catalog_for_publishing_actor(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_CHAT_MODE", "real")
    audit_service = CapturingAuditService()
    cache = PrewarmCache()
    payload = AdminModelCatalogPublishRequest(
        entries=[
            {
                "provider_name": "provider-a",
                "provider_display_name": "Provider A",
                "api_endpoint": "https://provider.example/v1",
                "provider_credential": "secret-value",
                "model_key": "model-a",
                "model_display_name": "Model A",
            }
        ]
    )

    response = await publish_model_config(
        payload=payload,
        db=PrewarmDb(),
        principal=AdminPrincipal(org_id=1, user_id=7, roles=("admin",)),
        service=SuccessfulPublishService(),
        audit_service=audit_service,
        model_cache=cache,
    )

    assert response.catalog_version == 4
    assert cache.events == [
        {
            "action": "invalidate",
            "event_type": "catalog_published",
            "catalog_version": 4,
            "model_entry_ids": (5,),
            "provider_ids": (3,),
        },
        {
            "action": "prewarm",
            "org_id": 1,
            "user_id": 7,
            "model_entry_ids": [5],
        },
    ]
    assert audit_service.records == [
        {
            "principal": AdminPrincipal(org_id=1, user_id=7, roles=("admin",)),
            "action": "admin.model.catalog_published",
            "target_type": "model_catalog",
            "target_id": 21,
            "detail": {
                "catalog_version": 4,
                "previous_catalog_version": 3,
                "config_hash": "c" * 64,
                "status": "published",
                "entry_count": 1,
                "reasoning_protocol_enabled_model_ids": [],
                "reasoning_protocol_disabled_model_ids": [],
                "reasoning_protocol_enabled_count": 0,
                "reasoning_protocol_disabled_count": 0,
                "restricted_model_count": 0,
                "user_grant_count": 0,
                "department_grant_count": 0,
            },
        }
    ]


@pytest.mark.asyncio
async def test_publish_route_records_sanitized_failure_audit() -> None:
    metrics = shared_model_cache_metrics()
    metrics.counters.clear()
    metrics.durations_ms.clear()
    audit_service = CapturingAuditService()
    payload = AdminModelCatalogPublishRequest(
        entries=[
            {
                "provider_name": "provider-a",
                "provider_display_name": "Provider A",
                "api_endpoint": "https://provider.example/v1",
                "provider_credential": "secret-value",
                "model_key": "model-a",
                "model_display_name": "Model A",
            }
        ]
    )

    with pytest.raises(HTTPException) as exc_info:
        await publish_model_config(
            payload=payload,
            principal=AdminPrincipal(org_id=1, user_id=7, roles=("admin",)),
            service=FailingPublishService(),
            audit_service=audit_service,
            model_cache=None,
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "model catalog publish failed"
    assert audit_service.records[0]["action"] == "admin.model.catalog_publish_failed"
    assert audit_service.records[0]["target_id"] == "pending"
    assert audit_service.records[0]["detail"] == {
        "status": "failed",
        "entry_count": 1,
        "error_type": "RuntimeError",
    }
    assert metrics.count(cache_name="model_publish", event="failure", error_type="RuntimeError") == 1
