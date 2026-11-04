from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager

import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles, deregister

from app.api.v1.admin.model_config import (
    _catalog_snapshot_response,
    publish_model_config,
)
from app.models import ModelEntry, ModelProvider, ModelUpgradeLog, Organization, Permission, SysDept, SysUser, User
from app.models.base import Base
from app.schemas.admin import AdminModelCatalogPublishRequest
from app.services.admin_auth import AdminPrincipal
from app.services.admin_model_catalog import (
    AdminModelCatalogService,
    AdminModelCatalogSnapshot,
    AdminModelConfigItem,
    AdminModelConfigRecord,
    AdminModelVisibilityUserRecord,
    ModelCatalogPublishIntent,
    ModelVisibilityScope,
    SqlAlchemyAdminModelCatalogStore,
)


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


@pytest.fixture
def sqlite_big_integer():
    with sqlite_big_integer_compiler():
        yield


def catalog_entry(**overrides) -> AdminModelConfigItem:
    values = {
        "provider_key": "provider-a",
        "provider_display_name": "Provider A",
        "provider_type": "openai_compatible",
        "api_endpoint": "https://provider.example/v1",
        "api_key": "secret-value",
        "model_key": "model-a",
        "model_display_name": "Model A",
        "context_length": 8192,
        "tags": ["chat"],
        "is_restricted": False,
        "visibility_scope": ModelVisibilityScope(organization=True),
    }
    values.update(overrides)
    return AdminModelConfigItem(**values)


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


def publish_payload(**overrides) -> dict:
    values = {
        "provider_name": "provider-a",
        "provider_display_name": "Provider A",
        "api_endpoint": "https://provider.example/v1",
        "model_key": "model-a",
        "model_display_name": "Model A",
        "context_length": 8192,
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_catalog_scope_schema_and_service_validate_canonical_shape() -> None:
    organization = AdminModelCatalogPublishRequest(
        entries=[publish_payload(visibility_scope={"organization": True})]
    )
    mixed = AdminModelCatalogPublishRequest(
        entries=[
            publish_payload(
                visibility_scope={
                    "organization": False,
                    "user_ids": [2, 1, 1],
                    "department_ids": ["dept-b", "dept-a ", "dept-a"],
                }
            )
        ]
    )
    with pytest.raises(ValueError, match="at least one user or department"):
        AdminModelCatalogPublishRequest(
            entries=[publish_payload(visibility_scope={"organization": False})]
        )
    with pytest.raises(ValueError, match="must not include"):
        AdminModelCatalogPublishRequest(
            entries=[
                publish_payload(
                    visibility_scope={
                        "organization": True,
                        "user_ids": [1],
                        "department_ids": ["dept-a"],
                    }
                )
            ]
        )

    assert organization.entries[0].visibility_scope is not None
    assert organization.entries[0].visibility_scope.organization is True
    assert mixed.entries[0].visibility_scope is not None
    assert mixed.entries[0].visibility_scope.user_ids == [1, 2]
    assert mixed.entries[0].visibility_scope.department_ids == ["dept-a", "dept-b"]


@pytest.mark.asyncio
async def test_catalog_publish_normalizes_and_replaces_model_permissions(
    sqlite_big_integer,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all,
                tables=MODEL_VISIBILITY_TABLES,
            )
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            organization = Organization(id=1, name="Org", code="default", status="active")
            db.add(organization)
            parent = SysDept(dept_id="dept-parent", parent_id=None, name="研发中心", status=1)
            child = SysDept(dept_id="dept-child", parent_id="dept-parent", name="平台组", status=1)
            other_dept = SysDept(dept_id="dept-other", parent_id=None, name="产品部", status=1)
            db.add_all([parent, child, other_dept])
            covered = User(org_id=1, user_code="u-covered", name="被部门覆盖", status="active")
            explicit = User(org_id=1, user_code="u-explicit", name="精确授权", status="active")
            db.add_all([covered, explicit])
            await db.flush()
            db.add_all(
                [
                    SysUser(
                        user_id="hrm-covered",
                        username="u-covered",
                        code="u-covered",
                        dept_id="dept-child",
                        status=1,
                    ),
                    SysUser(
                        user_id="hrm-explicit",
                        username="u-explicit",
                        code="u-explicit",
                        dept_id="dept-other",
                        status=1,
                    ),
                ]
            )
            await db.commit()

        principal = AdminPrincipal(org_id=1, user_id=9, roles=("admin",))
        scope = ModelVisibilityScope(
            organization=False,
            user_ids=[int(covered.id), int(explicit.id)],
            department_ids=["dept-parent", "dept-child"],
        )

        async with Session() as db:
            store = SqlAlchemyAdminModelCatalogStore(db)
            service = AdminModelCatalogService(store)
            first = await service.publish_catalog(
                principal=principal,
                entries=[catalog_entry(is_restricted=True, visibility_scope=scope)],
            )

            permissions = (
                await db.execute(
                    select(Permission).where(
                        Permission.org_id == 1,
                        Permission.asset_type == "model_entry",
                    )
                )
            ).scalars().all()
            assert first.catalog_version == 1
            assert sorted((row.grantee_type, row.grantee_id) for row in permissions) == [
                ("department", "dept-parent"),
                ("user", str(explicit.id)),
            ]

            snapshot = await store.list_catalog(org_id=1)
            assert snapshot.entries[0].is_restricted is True
            assert snapshot.entries[0].visibility_scope == ModelVisibilityScope(
                organization=False,
                user_ids=[int(explicit.id)],
                department_ids=["dept-parent"],
            )
            assert [item.id for item in snapshot.entries[0].visibility_users] == [int(explicit.id)]
            assert snapshot.entries[0].visibility_users[0].name == "精确授权"

            await db.execute(
                Permission.__table__.delete().where(
                    Permission.org_id == 1,
                    Permission.asset_type == "model_entry",
                )
            )
            repaired = await service.publish_catalog(
                principal=principal,
                entries=[catalog_entry(is_restricted=True, visibility_scope=scope)],
            )
            permissions = (
                await db.execute(
                    select(Permission).where(
                        Permission.org_id == 1,
                        Permission.asset_type == "model_entry",
                    )
                )
            ).scalars().all()
            releases = (
                await db.execute(
                    select(ModelUpgradeLog).where(
                        ModelUpgradeLog.org_id == 1,
                        ModelUpgradeLog.action == "model_catalog_publish",
                    )
                )
            ).scalars().all()
            assert repaired.catalog_version == first.catalog_version
            assert repaired.release_id == first.release_id
            assert len(releases) == 1
            assert sorted((row.grantee_type, row.grantee_id) for row in permissions) == [
                ("department", "dept-parent"),
                ("user", str(explicit.id)),
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_publish_keeps_visibility_scopes_isolated_per_model(
    sqlite_big_integer,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all,
                tables=MODEL_VISIBILITY_TABLES,
            )
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            db.add(Organization(id=1, name="Org", code="default", status="active"))
            db.add_all(
                [
                    SysDept(dept_id="dept-a", parent_id=None, name="研发中心", status=1),
                    SysDept(dept_id="dept-b", parent_id=None, name="产品部", status=1),
                ]
            )
            user_a = User(org_id=1, user_code="u-a", name="用户A", status="active")
            user_b = User(org_id=1, user_code="u-b", name="用户B", status="active")
            db.add_all([user_a, user_b])
            await db.flush()
            db.add_all(
                [
                    SysUser(
                        user_id="hrm-a",
                        username="u-a",
                        code="u-a",
                        dept_id="dept-a",
                        status=1,
                    ),
                    SysUser(
                        user_id="hrm-b",
                        username="u-b",
                        code="u-b",
                        dept_id="dept-b",
                        status=1,
                    ),
                ]
            )
            await db.commit()

        principal = AdminPrincipal(org_id=1, user_id=9, roles=("admin",))
        async with Session() as db:
            store = SqlAlchemyAdminModelCatalogStore(db)
            service = AdminModelCatalogService(store)
            await service.publish_catalog(
                principal=principal,
                entries=[
                    catalog_entry(
                        model_key="model-a",
                        model_display_name="Model A",
                        is_restricted=True,
                        visibility_scope=ModelVisibilityScope(
                            organization=False,
                            user_ids=[int(user_b.id)],
                            department_ids=["dept-a"],
                        ),
                    ),
                    catalog_entry(
                        model_key="model-b",
                        model_display_name="Model B",
                        is_restricted=True,
                        visibility_scope=ModelVisibilityScope(
                            organization=False,
                            user_ids=[int(user_a.id)],
                            department_ids=["dept-b"],
                        ),
                    ),
                ],
            )

            rows = (
                await db.execute(
                    select(ModelEntry.model_key, Permission.grantee_type, Permission.grantee_id)
                    .join(ModelEntry, Permission.asset_id == ModelEntry.id)
                    .where(
                        Permission.org_id == 1,
                        Permission.asset_type == "model_entry",
                    )
                )
            ).all()
            permissions_by_model: dict[str, set[tuple[str, str]]] = {}
            for model_key, grantee_type, grantee_id in rows:
                permissions_by_model.setdefault(str(model_key), set()).add(
                    (str(grantee_type), str(grantee_id))
                )

            assert permissions_by_model == {
                "model-a": {
                    ("department", "dept-a"),
                    ("user", str(user_b.id)),
                },
                "model-b": {
                    ("department", "dept-b"),
                    ("user", str(user_a.id)),
                },
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_publish_keeps_parent_child_normalization_isolated_per_model(
    sqlite_big_integer,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all,
                tables=MODEL_VISIBILITY_TABLES,
            )
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            db.add(Organization(id=1, name="Org", code="default", status="active"))
            db.add_all(
                [
                    SysDept(dept_id="dept-parent", parent_id=None, name="研发中心", status=1),
                    SysDept(
                        dept_id="dept-child",
                        parent_id="dept-parent",
                        name="平台组",
                        status=1,
                    ),
                ]
            )
            await db.commit()

        principal = AdminPrincipal(org_id=1, user_id=9, roles=("admin",))
        async with Session() as db:
            store = SqlAlchemyAdminModelCatalogStore(db)
            service = AdminModelCatalogService(store)
            await service.publish_catalog(
                principal=principal,
                entries=[
                    catalog_entry(
                        model_key="model-a",
                        model_display_name="Model A",
                        is_restricted=True,
                        visibility_scope=ModelVisibilityScope(
                            organization=False,
                            department_ids=["dept-parent", "dept-child"],
                        ),
                    ),
                    catalog_entry(
                        model_key="model-b",
                        model_display_name="Model B",
                        is_restricted=True,
                        visibility_scope=ModelVisibilityScope(
                            organization=False,
                            department_ids=["dept-child"],
                        ),
                    ),
                ],
            )

            rows = (
                await db.execute(
                    select(ModelEntry.model_key, Permission.grantee_type, Permission.grantee_id)
                    .join(ModelEntry, Permission.asset_id == ModelEntry.id)
                    .where(
                        Permission.org_id == 1,
                        Permission.asset_type == "model_entry",
                    )
                )
            ).all()
            permissions_by_model: dict[str, set[tuple[str, str]]] = {}
            for model_key, grantee_type, grantee_id in rows:
                permissions_by_model.setdefault(str(model_key), set()).add(
                    (str(grantee_type), str(grantee_id))
                )

            assert permissions_by_model == {
                "model-a": {("department", "dept-parent")},
                "model-b": {("department", "dept-child")},
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_publish_rejects_invalid_target_and_organization_scope_clears_grants(
    sqlite_big_integer,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all,
                tables=MODEL_VISIBILITY_TABLES,
            )
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            db.add(Organization(id=1, name="Org", code="default", status="active"))
            db.add(SysDept(dept_id="dept-a", parent_id=None, name="研发中心", status=1))
            user = User(org_id=1, user_code="u-a", name="用户A", status="active")
            db.add(user)
            await db.commit()
            user_id = int(user.id)

        principal = AdminPrincipal(org_id=1, user_id=9, roles=("admin",))
        invalid_scope = ModelVisibilityScope(
            organization=False,
            user_ids=[user_id, 999999],
            department_ids=["dept-a"],
        )
        async with Session() as db:
            store = SqlAlchemyAdminModelCatalogStore(db)
            service = AdminModelCatalogService(store)
            with pytest.raises(Exception, match="invalid model visibility user"):
                await service.publish_catalog(
                    principal=principal,
                    entries=[
                        catalog_entry(is_restricted=True, visibility_scope=invalid_scope)
                    ],
                )
            assert (
                await db.scalar(select(ModelEntry.id).where(ModelEntry.org_id == 1))
            ) is None

        valid_scope = ModelVisibilityScope(
            organization=False,
            user_ids=[user_id],
            department_ids=["dept-a"],
        )
        async with Session() as db:
            store = SqlAlchemyAdminModelCatalogStore(db)
            service = AdminModelCatalogService(store)
            await service.publish_catalog(
                principal=principal,
                entries=[catalog_entry(is_restricted=True, visibility_scope=valid_scope)],
            )

        async with Session() as db:
            store = SqlAlchemyAdminModelCatalogStore(db)
            service = AdminModelCatalogService(store)
            await service.publish_catalog(
                principal=principal,
                entries=[
                    catalog_entry(
                        is_restricted=False,
                        visibility_scope=ModelVisibilityScope(organization=True),
                    )
                ],
            )
            permissions = (
                await db.execute(
                    select(Permission).where(
                        Permission.org_id == 1,
                        Permission.asset_type == "model_entry",
                    )
                )
            ).scalars().all()
            model = (
                await db.execute(select(ModelEntry).where(ModelEntry.org_id == 1))
            ).scalar_one()
            assert permissions == []
            assert model.is_restricted is False
    finally:
        await engine.dispose()


def test_catalog_models_keep_existing_default_without_legacy_restriction_scope() -> None:
    item = catalog_entry(visibility_scope=None)

    assert item.is_restricted is False
    assert item.visibility_scope is None


@pytest.mark.asyncio
async def test_model_config_route_uses_scope_as_authoritative_wire_contract() -> None:
    class CapturingService:
        def __init__(self) -> None:
            self.entries = None

        async def publish_catalog(self, *, principal, entries):
            self.entries = entries
            return ModelCatalogPublishIntent(
                release_id=1,
                catalog_version=1,
                previous_catalog_version=None,
                config_hash="a" * 64,
                status="published",
                published_at=datetime.now(timezone.utc),
            )

    class CapturingAudit:
        async def record(self, **kwargs):
            return None

    service = CapturingService()
    payload = AdminModelCatalogPublishRequest(
        entries=[
            publish_payload(
                is_restricted=False,
                visibility_scope={
                    "organization": False,
                    "user_ids": [9],
                    "department_ids": ["dept-a"],
                },
            )
        ]
    )

    await publish_model_config(
        payload=payload,
        db=None,
        principal=AdminPrincipal(org_id=1, user_id=7, roles=("admin",)),
        service=service,
        audit_service=CapturingAudit(),
        model_cache=None,
    )

    assert service.entries[0].is_restricted is True
    assert service.entries[0].visibility_scope == ModelVisibilityScope(
        organization=False,
        user_ids=[9],
        department_ids=["dept-a"],
    )


def test_catalog_snapshot_response_maps_visibility_scope_and_users() -> None:
    response = _catalog_snapshot_response(
        AdminModelCatalogSnapshot(
            entries=[
                AdminModelConfigRecord(
                    provider_id=3,
                    provider_key="provider-a",
                    provider_display_name="Provider A",
                    provider_type="openai_compatible",
                    api_endpoint="https://provider.example/v1",
                    api_key_configured=True,
                    provider_status="active",
                    provider_catalog_version=1,
                    model_entry_id=5,
                    model_key="model-a",
                    model_display_name="Model A",
                    context_length=8192,
                    tags=["chat"],
                    is_restricted=True,
                    visibility_scope=ModelVisibilityScope(
                        organization=False,
                        user_ids=[9],
                        department_ids=["dept-a"],
                    ),
                    visibility_users=[
                        AdminModelVisibilityUserRecord(
                            id=9,
                            name="用户A",
                            user_code="u-a",
                            status="active",
                        )
                    ],
                    is_default=False,
                    model_status="active",
                    model_catalog_version=1,
                    runtime_capabilities={},
                )
            ],
            latest_release=None,
        )
    )

    assert response.entries[0].visibility_scope.organization is False
    assert response.entries[0].visibility_scope.user_ids == [9]
    assert response.entries[0].visibility_scope.department_ids == ["dept-a"]
    assert response.entries[0].visibility_users[0].name == "用户A"
