from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile
from typing import Any

import pytest
from sqlalchemy import BigInteger, event, select
from sqlalchemy.schema import CreateTable
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.mysql import LONGTEXT

from app.models import AppCenterApp, Base, Organization, Permission, User
from app.schemas.app_center import AppShareScope
from app.services.app_center import AppCenterService
from app.services.app_center_contracts import AppCenterError
from app.services.runtime_workspace import Principal


HTML = "<!doctype html><html><head><title>看板</title></head><body>hello</body></html>"
UPDATED_HTML = "<!doctype html><html><body>updated</body></html>"


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(
    _type: BigInteger,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "INTEGER"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_sqlite_text(
    _type: LONGTEXT,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "TEXT"


def _package(html: str = HTML) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("index.html", html)
    return output.getvalue()


async def _database() -> tuple[object, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            await connection.execute(CreateTable(table))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(session: AsyncSession) -> tuple[Principal, Principal]:
    organization = Organization(name="org", code="org", status="active")
    owner = User(name="创建者", org_id=1, status="active", user_code="u1")
    viewer = User(name="使用者", org_id=1, status="active", user_code="u2")
    session.add_all([organization, owner, viewer])
    await session.flush()
    organization_id = int(organization.id)
    owner_id = int(owner.id)
    viewer_id = int(viewer.id)
    await session.commit()
    return (
        Principal(user_id=owner_id, org_id=organization_id),
        Principal(user_id=viewer_id, org_id=organization_id),
    )


@pytest.mark.asyncio
async def test_create_share_idempotency_and_visibility() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            owner, viewer = await _seed(session)
            service = AppCenterService(session)
            scope = AppShareScope(user_ids=[viewer.user_id])

            created = await service.create_from_html(
                principal=owner,
                name="销售看板",
                description="销售数据一屏看",
                html_content=HTML,
                share_mode="scope",
                scope=scope,
                idempotency_key="request-1",
                source_session_id="session-1",
                source_run_id="run-1",
            )
            retried = await service.create_from_html(
                principal=owner,
                name="销售看板",
                description="销售数据一屏看",
                html_content=HTML,
                share_mode="scope",
                scope=scope,
                idempotency_key="request-1",
                source_session_id="session-1",
                source_run_id="run-1",
            )

            assert retried.id == created.id
            assert created.description == "销售数据一屏看"
            assert created.access_url.endswith(
                f"/api/v1/user/data-apps/{created.id}/index.html"
            )
            assert created.mine is True
            assert created.scope_summary == "1 人"

            owner_items = await service.list_for_user(principal=owner)
            viewer_items = await service.list_for_user(principal=viewer)
            assert [item.id for item in owner_items] == [created.id]
            assert [item.id for item in viewer_items] == [created.id]
            assert viewer_items[0].mine is False
            assert viewer_items[0].creator_name == "创建者"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_company_root_department_is_a_valid_share_target() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            owner, _ = await _seed(session)
            service = AppCenterService(session)
            created = await service.create_from_html(
                principal=owner,
                name="公司根应用",
                description="公司根目录应用",
                html_content=HTML,
                share_mode="scope",
                scope=AppShareScope(department_ids=["1"]),
                idempotency_key=None,
                source_session_id=None,
                source_run_id=None,
            )

            assert created.scope_summary == "1 个部门"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_package_preserves_access_url_and_clears_html() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            owner, _ = await _seed(session)
            service = AppCenterService(session)
            created = await service.create_from_html(
                principal=owner,
                name="应用",
                description="普通应用",
                html_content=HTML,
                share_mode="link",
                scope=None,
                idempotency_key=None,
                source_session_id=None,
                source_run_id=None,
            )
            updated = await service.update_from_package(
                principal=owner,
                app_id=created.id,
                package=_package(UPDATED_HTML),
            )
            record = await session.get(AppCenterApp, created.id)

            assert updated.publish_version_id != created.publish_version_id
            assert updated.access_url == created.access_url
            assert updated.description == created.description
            assert record is not None
            assert record.access_url == created.access_url
            assert record.html_content is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_consumer_package_preserves_link_and_scope_boundaries() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            owner, viewer = await _seed(session)
            service = AppCenterService(session)
            link_app = await service.create_from_html(
                principal=owner,
                name="链接看板",
                description="链接看板",
                html_content=HTML,
                share_mode="link",
                scope=None,
                idempotency_key=None,
                source_session_id=None,
                source_run_id=None,
            )
            scope_app = await service.create_from_html(
                principal=owner,
                name="授权看板",
                description="授权看板",
                html_content=HTML,
                share_mode="scope",
                scope=AppShareScope(),
                idempotency_key=None,
                source_session_id=None,
                source_run_id=None,
            )

            await service.require_consumer_access(
                principal=viewer,
                app_id=link_app.id,
            )
            assert await service.consumer_package(
                principal=viewer,
                app_id=link_app.id,
            ) == _package(HTML)
            with pytest.raises(AppCenterError):
                await service.package(principal=viewer, app_id=link_app.id)

            with pytest.raises(AppCenterError):
                await service.require_consumer_access(
                    principal=viewer,
                    app_id=scope_app.id,
                )
            with pytest.raises(AppCenterError):
                await service.consumer_package(
                    principal=viewer,
                    app_id=scope_app.id,
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_rewrites_legacy_external_access_url() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            owner, _ = await _seed(session)
            service = AppCenterService(session)
            created = await service.create_from_html(
                principal=owner,
                name="历史应用",
                description="历史外部应用",
                html_content=HTML,
                share_mode="scope",
                scope=AppShareScope(),
                idempotency_key=None,
                source_session_id=None,
                source_run_id=None,
            )
            record = await session.get(AppCenterApp, created.id)
            assert record is not None
            record.access_url = "https://demo.example/legacy"
            await session.commit()

            items = await service.list_for_user(principal=owner)

            assert items[0].access_url.endswith(
                f"/api/v1/user/data-apps/{created.id}/index.html"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_same_package_is_naturally_idempotent() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            owner, _ = await _seed(session)
            service = AppCenterService(session)
            created = await service.create_from_html(
                principal=owner,
                name="幂等应用",
                description="幂等测试应用",
                html_content=HTML,
                share_mode="link",
                scope=None,
                idempotency_key=None,
                source_session_id=None,
                source_run_id=None,
            )
            updated = await service.update_from_html(
                principal=owner,
                app_id=created.id,
                html_content=HTML,
            )

            assert updated.publish_version_id == created.publish_version_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_from_html_renames_and_exposes_source_session() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            owner, _ = await _seed(session)
            service = AppCenterService(session)
            created = await service.create_from_html(
                principal=owner,
                name="旧名字",
                description="旧描述",
                html_content=HTML,
                share_mode="scope",
                scope=AppShareScope(),
                idempotency_key=None,
                source_session_id="session-9",
                source_run_id="run-9",
            )

            refreshed = await service.update_from_html(
                principal=owner,
                app_id=created.id,
                html_content=UPDATED_HTML,
                name="新名字",
                description="新描述",
            )
            assert refreshed.name == "新名字"
            assert refreshed.description == "新描述"
            assert refreshed.publish_version_id != created.publish_version_id

            items = await service.list_for_user(principal=owner)
            assert items[0].source_session_id == "session-9"
            assert items[0].name == "新名字"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_copy_creates_independent_private_application() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            owner, viewer = await _seed(session)
            service = AppCenterService(session)
            source = await service.create_from_html(
                principal=owner,
                name="原应用",
                description="被复制的应用",
                html_content=HTML,
                share_mode="scope",
                scope=AppShareScope(user_ids=[viewer.user_id]),
                idempotency_key=None,
                source_session_id="source-session",
                source_run_id="source-run",
            )
            copied = await service.copy(principal=viewer, app_id=source.id)
            permissions = len(
                (await session.scalars(
                    select(Permission).where(
                        Permission.asset_type == "app_center_app",
                        Permission.asset_id == copied.id,
                    )
                )).all()
            )

            assert copied.mine is True
            assert copied.description == source.description
            assert copied.copied_from_app_id == source.id
            assert copied.copied_from_creator_name == "创建者"
            assert copied.scope_summary == "仅自己"
            assert permissions == 0
            record = await session.get(AppCenterApp, copied.id)
            assert record is not None
            assert record.source_session_id is None
            assert record.source_run_id is None
            assert copied.access_url.endswith(
                f"/api/v1/user/data-apps/{copied.id}/index.html"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_live_data_manifest_rejects_publication() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            owner, _ = await _seed(session)
            service = AppCenterService(session)
            html = (
                "<!doctype html><html><body>"
                '<script type="application/json" id="hermes-report-manifest">'
                '{"engine":"sql_manifest_v1","data_mode":"live","queries":['
                '{"id":"delete","sql":"DELETE FROM crm_project_info_t",'
                '"page":1,"page_size":100}]}</script>'
                "</body></html>"
            )

            with pytest.raises(AppCenterError) as error:
                await service.create_from_html(
                    principal=owner,
                    name="非法活数据",
                    description="非法 profile",
                    html_content=html,
                    share_mode="link",
                    scope=None,
                    idempotency_key=None,
                    source_session_id=None,
                    source_run_id=None,
                )

            assert error.value.code == "APP_CENTER_PACKAGE_INVALID"
            assert error.value.error_code == -14001
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_non_creator_cannot_manage_application() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            owner, viewer = await _seed(session)
            service = AppCenterService(session)
            created = await service.create_from_html(
                principal=owner,
                name="受控应用",
                description="权限受控应用",
                html_content=HTML,
                share_mode="scope",
                scope=AppShareScope(user_ids=[viewer.user_id]),
                idempotency_key=None,
                source_session_id=None,
                source_run_id=None,
            )

            with pytest.raises(AppCenterError) as error:
                await service.update_from_html(
                    principal=viewer,
                    app_id=created.id,
                    html_content=UPDATED_HTML,
                )
            assert error.value.code == "APP_CENTER_PERMISSION_DENIED"

            filename, package = await service.package(
                principal=viewer,
                app_id=created.id,
            )
            assert filename == "受控应用.zip"
            assert package == _package(HTML)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_query_does_not_load_large_columns() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            owner, _ = await _seed(session)
            service = AppCenterService(session)
            await service.create_from_html(
                principal=owner,
                name="大字段应用",
                description="列表字段测试",
                html_content=HTML,
                share_mode="link",
                scope=None,
                idempotency_key=None,
                source_session_id=None,
                source_run_id=None,
            )
            statements: list[str] = []

            def before_cursor_execute(
                _session,
                _cursor,
                statement: str,
                _parameters,
                _context,
                _executemany,
            ) -> None:
                statements.append(statement)

            event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
            items = await service.list_for_user(principal=owner)
            event.remove(engine.sync_engine, "before_cursor_execute", before_cursor_execute)

            assert len(items) == 1
            assert statements
            app_queries = [
                item
                for item in statements
                if "from app_center_app" in item.lower()
            ]
            assert app_queries, statements
            assert all("package_blob" not in item.lower() for item in app_queries)
            assert all("html_content" not in item.lower() for item in app_queries)
    finally:
        await engine.dispose()
