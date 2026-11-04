from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    ModelEntry,
    ModelProvider,
    ModelUpgradeLog,
    Organization,
    Permission,
    SysDept,
    SysUser,
    User,
)
from app.models.base import Base
from app.services.model_catalog_cache import RedisModelCatalogCache
from app.services.runtime_models import (
    SqlAlchemyRuntimeModelStore,
    resolve_default_model_entry_id,
    resolve_model_visibility_grantee,
)


RUNTIME_VISIBILITY_TABLES = [
    Organization.__table__,
    User.__table__,
    SysDept.__table__,
    SysUser.__table__,
    Permission.__table__,
    ModelProvider.__table__,
    ModelEntry.__table__,
    ModelUpgradeLog.__table__,
]


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=RUNTIME_VISIBILITY_TABLES,
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add(Organization(id=1, name="Org", code="default", status="active"))
        db.add_all(
            [
                SysDept(
                    dept_id="dept-root",
                    parent_id=None,
                    name="研发中心",
                    status=1,
                ),
                SysDept(
                    dept_id="dept-child",
                    parent_id="dept-root",
                    name="平台组",
                    status=1,
                ),
                SysDept(
                    dept_id="dept-other",
                    parent_id=None,
                    name="产品部",
                    status=1,
                ),
            ]
        )
        target = User(
            id=101,
            org_id=1,
            user_code="u-target",
            name="目标用户",
            status="active",
        )
        other = User(
            id=102,
            org_id=1,
            user_code="u-other",
            name="其他用户",
            status="active",
        )
        db.add_all([target, other])
        await db.flush()
        db.add_all(
            [
                SysUser(
                    user_id="hrm-target",
                    username="u-target",
                    code="u-target",
                    dept_id="dept-child",
                    status=1,
                ),
                SysUser(
                    user_id="hrm-other",
                    username="u-other",
                    code="u-other",
                    dept_id="dept-other",
                    status=1,
                ),
            ]
        )
        provider = ModelProvider(
            id=1,
            org_id=1,
            provider_key="provider-a",
            display_name="Provider A",
            status="active",
            catalog_version=1,
        )
        db.add(provider)
        await db.flush()
        db.add_all(
            [
                ModelEntry(
                    id=1,
                    org_id=1,
                    provider_id=1,
                    model_key="model-public",
                    display_name="Public Model",
                    status="active",
                    is_default=False,
                    is_restricted=False,
                    catalog_version=1,
                ),
                ModelEntry(
                    id=2,
                    org_id=1,
                    provider_id=1,
                    model_key="model-default",
                    display_name="Default Model",
                    status="active",
                    is_default=True,
                    is_restricted=True,
                    catalog_version=1,
                ),
                ModelEntry(
                    id=3,
                    org_id=1,
                    provider_id=1,
                    model_key="model-user",
                    display_name="User Model",
                    status="active",
                    is_default=False,
                    is_restricted=True,
                    catalog_version=1,
                ),
                ModelEntry(
                    id=4,
                    org_id=1,
                    provider_id=1,
                    model_key="model-other",
                    display_name="Other Model",
                    status="active",
                    is_default=False,
                    is_restricted=True,
                    catalog_version=1,
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Permission(
                    id=201,
                    org_id=1,
                    asset_type="model_entry",
                    asset_id=2,
                    grantee_type="department",
                    grantee_id="dept-root",
                ),
                Permission(
                    id=202,
                    org_id=1,
                    asset_type="model_entry",
                    asset_id=3,
                    grantee_type="user",
                    grantee_id=str(target.id),
                ),
                Permission(
                    id=203,
                    org_id=1,
                    asset_type="model_entry",
                    asset_id=4,
                    grantee_type="user",
                    grantee_id=str(other.id),
                ),
                Permission(
                    id=204,
                    org_id=1,
                    asset_type="model_entry",
                    asset_id=0,
                    grantee_type="all",
                    grantee_id="*",
                ),
            ]
        )
        await db.commit()
        target_user_id = int(target.id)
        other_user_id = int(other.id)
    return engine, sessions, target_user_id, other_user_id


@pytest.mark.asyncio
async def test_runtime_visibility_inherits_departments_and_rejects_legacy_global_grant() -> None:
    engine, sessions, target_user_id, other_user_id = await _database()
    try:
        async with sessions() as db:
            grantee = await resolve_model_visibility_grantee(
                db,
                org_id=1,
                user_id=target_user_id,
            )
            models = await SqlAlchemyRuntimeModelStore(db).list_visible_models(
                org_id=1,
                user_id=target_user_id,
            )

            assert grantee.department_ids == frozenset({"dept-child", "dept-root"})
            assert [model.model_entry_id for model in models] == [1, 2, 3]
            assert resolve_default_model_entry_id(models) == 2

        async with sessions() as db:
            models = await SqlAlchemyRuntimeModelStore(db).list_visible_models(
                org_id=1,
                user_id=other_user_id,
            )

            assert [model.model_entry_id for model in models] == [1, 4]
            assert resolve_default_model_entry_id(models) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_visibility_uses_bounded_query_count() -> None:
    engine, sessions, target_user_id, _other_user_id = await _database()
    try:
        statements: list[str] = []

        def before_cursor_execute(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
        async with sessions() as db:
            models = await SqlAlchemyRuntimeModelStore(db).list_visible_models(
                org_id=1,
                user_id=target_user_id,
            )

        assert [model.model_entry_id for model in models] == [1, 2, 3]
        assert len(statements) == 2
        assert all("JOIN" in statement.upper() or "WITH RECURSIVE" in statement.upper() for statement in statements)
    finally:
        await engine.dispose()


def test_catalog_cache_keys_include_visibility_fingerprint() -> None:
    base_key = RedisModelCatalogCache.catalog_key(1, 2, "3")
    changed_key = RedisModelCatalogCache.catalog_key(
        1,
        2,
        "3",
        visibility_fingerprint="dept-chain-a",
    )
    changed_again_key = RedisModelCatalogCache.catalog_key(
        1,
        2,
        "3",
        visibility_fingerprint="dept-chain-b",
    )

    assert base_key != changed_key
    assert changed_key != changed_again_key
    assert RedisModelCatalogCache.lock_key(
        1,
        2,
        "3",
        visibility_fingerprint="dept-chain-a",
    ) != RedisModelCatalogCache.lock_key(
        1,
        2,
        "3",
        visibility_fingerprint="dept-chain-b",
    )


def test_runtime_chat_delegates_model_visibility_to_single_store_boundary() -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "app/services/runtime_chat.py"
    ).read_text(encoding="utf-8")

    assert 'Permission.asset_type == "model_entry"' not in source
    assert "list_visible_models(" in source
