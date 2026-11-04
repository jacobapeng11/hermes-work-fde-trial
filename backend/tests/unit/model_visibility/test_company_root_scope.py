from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles, deregister

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
from app.services.admin_auth import AdminPrincipal
from app.services.admin_model_catalog import (
    AdminModelCatalogService,
    ModelVisibilityScope,
    SqlAlchemyAdminModelCatalogStore,
)
from app.services.runtime_models import resolve_model_visibility_grantee


@contextmanager
def sqlite_big_integer_compiler():
    previous_dispatch = BigInteger.__dict__.get("_compiler_dispatch")
    previous_dispatcher = BigInteger.__dict__.get("_compiler_dispatcher")

    @compiles(BigInteger, "sqlite")
    def compile_big_integer_as_integer(
        _type: BigInteger,
        _compiler: object,
        **_kwargs: object,
    ) -> str:
        return "INTEGER"

    try:
        yield
    finally:
        deregister(BigInteger)
        if previous_dispatcher is not None and previous_dispatch is not None:
            BigInteger._compiler_dispatch = previous_dispatch
            BigInteger._compiler_dispatcher = previous_dispatcher


MODEL_VISIBILITY_TABLES = [
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
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=MODEL_VISIBILITY_TABLES,
            )
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        covered = User(org_id=1, user_code="u-covered", name="被部门覆盖", status="active")
        direct = User(org_id=1, user_code="u-direct", name="公司直挂人员", status="active")
        explicit = User(org_id=1, user_code="u-explicit", name="精确授权", status="active")
        db.add(Organization(id=1, name="Org", code="org", status="active"))
        db.add_all(
            [
                covered,
                direct,
                explicit,
                SysDept(
                    dept_id="D100",
                    parent_id=None,
                    name="研发中心",
                    status=1,
                    com_id="1",
                ),
                SysDept(
                    dept_id="D800",
                    parent_id=None,
                    name="产品部",
                    status=1,
                    com_id="2",
                ),
                SysDept(
                    dept_id="D200",
                    parent_id=None,
                    name="停用部门",
                    status=0,
                    com_id="1",
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                SysUser(
                    user_id="hrm-covered",
                    username="u-covered",
                    code="u-covered",
                    dept_id="D100",
                    com_id="1",
                    status=1,
                ),
                SysUser(
                    user_id="hrm-direct",
                    username="u-direct",
                    code="u-direct",
                    dept_id=None,
                    com_id="1",
                    status=1,
                ),
                SysUser(
                    user_id="hrm-explicit",
                    username="u-explicit",
                    code="u-explicit",
                    dept_id="D800",
                    com_id="2",
                    status=1,
                ),
            ]
        )
        await db.commit()
        return engine, sessions, int(covered.id), int(direct.id), int(explicit.id)


def _catalog_entry(**overrides):
    from tests.unit.model_visibility.test_model_catalog_scope import catalog_entry

    return catalog_entry(**overrides)


@pytest.mark.asyncio
async def test_catalog_publish_accepts_company_root_and_prunes_covered_targets() -> None:
    with sqlite_big_integer_compiler():
        engine, sessions, covered_id, direct_id, explicit_id = await _database()
        try:
            scope = ModelVisibilityScope(
                organization=False,
                user_ids=[covered_id, direct_id, explicit_id],
                department_ids=["1", "D100"],
            )
            async with sessions() as db:
                service = AdminModelCatalogService(SqlAlchemyAdminModelCatalogStore(db))
                await service.publish_catalog(
                    principal=AdminPrincipal(org_id=1, user_id=9, roles=("admin",)),
                    entries=[
                        _catalog_entry(
                            is_restricted=True,
                            visibility_scope=scope,
                        )
                    ],
                )

            async with sessions() as db:
                store = SqlAlchemyAdminModelCatalogStore(db)
                snapshot = await store.list_catalog(org_id=1)
                permissions = (
                    await db.execute(
                        select(Permission.grantee_type, Permission.grantee_id).where(
                            Permission.org_id == 1,
                            Permission.asset_type == "model_entry",
                        )
                    )
                ).all()
                covered_grantee = await resolve_model_visibility_grantee(
                    db,
                    org_id=1,
                    user_id=covered_id,
                )

            assert snapshot.entries[0].visibility_scope == ModelVisibilityScope(
                organization=False,
                user_ids=[explicit_id],
                department_ids=["1"],
            )
            assert sorted(permissions) == [
                ("department", "1"),
                ("user", str(explicit_id)),
            ]
            assert set(covered_grantee.department_ids) >= {"1", "D100"}
        finally:
            await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "department_id",
    ["virtual:unassigned", "D999", "D200"],
)
async def test_catalog_publish_rejects_invalid_company_scope_targets(department_id: str) -> None:
    with sqlite_big_integer_compiler():
        engine, sessions, _covered_id, _direct_id, _explicit_id = await _database()
        try:
            async with sessions() as db:
                service = AdminModelCatalogService(SqlAlchemyAdminModelCatalogStore(db))
                with pytest.raises(Exception, match="invalid model visibility department"):
                    await service.publish_catalog(
                        principal=AdminPrincipal(org_id=1, user_id=9, roles=("admin",)),
                        entries=[
                            _catalog_entry(
                                is_restricted=True,
                                visibility_scope=ModelVisibilityScope(
                                    organization=False,
                                    user_ids=[],
                                    department_ids=[department_id],
                                ),
                            )
                        ],
                    )
        finally:
            await engine.dispose()
