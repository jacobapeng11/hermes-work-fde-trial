from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateTable

from app.api.v1.admin.task_prompt_config import (
    get_task_prompt_config_service,
    save_task_prompt_config,
)
from app.api.v1.user.runtime_workspace import get_principal
from app.main import app
from app.models import Organization, TaskPromptConfig, User
from app.schemas.task_prompt_config import (
    TaskPromptConfigDocument,
    TaskPromptConfigSaveRequest,
)
from app.services.admin_auth import AdminPrincipal
from app.services.runtime_workspace import Principal
from app.services.task_prompt_config import (
    TaskPromptConfigConflictError,
    TaskPromptConfigService,
)
from scripts.schema_bootstrap import expected_schema


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(_type, _compiler, **_kwargs) -> str:
    return "INTEGER"


def document() -> TaskPromptConfigDocument:
    return TaskPromptConfigDocument.model_validate({
        "tabs": [{
            "label": "看板",
            "description": "生成经营数据看板。",
            "is_default": True,
            "show_board_gallery": True,
            "topics": [{
                "title": "销售分析",
                "tasks": [{
                    "summary": "生成销售趋势",
                    "detail": "请生成近 30 天销售趋势看板。"
                }]
            }]
        }]
    })


def test_task_prompt_config_schema_contract() -> None:
    schema = expected_schema()["task_prompt_config"]

    assert {
        "id",
        "org_id",
        "config",
        "revision",
        "updated_by_user_id",
        "created_at",
        "updated_at",
    }.issubset(schema)


def test_document_requires_unique_topics_and_single_default_tab() -> None:
    base = document().model_dump()
    base["tabs"].append({**base["tabs"][0], "label": "办公", "is_default": True})

    with pytest.raises(ValueError, match="exactly one default tab"):
        TaskPromptConfigDocument.model_validate(base)

    duplicate_topic = document().model_dump()
    duplicate_topic["tabs"][0]["topics"].append(
        duplicate_topic["tabs"][0]["topics"][0]
    )
    with pytest.raises(ValueError, match="topic titles must be unique"):
        TaskPromptConfigDocument.model_validate(duplicate_topic)


@pytest.mark.asyncio
async def test_service_saves_updates_and_rejects_stale_revision() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.execute(CreateTable(Organization.__table__))
        await connection.execute(CreateTable(User.__table__))
        await connection.execute(CreateTable(TaskPromptConfig.__table__))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            organization = Organization(id=1, name="org", code="org", status="active")
            administrator = User(
                id=1,
                name="管理员",
                org_id=1,
                status="active",
                user_code="admin",
            )
            session.add_all([organization, administrator])
            await session.flush()
            org_id = int(organization.id)
            user_id = int(administrator.id)
            await session.commit()

            service = TaskPromptConfigService(session)
            assert (await service.get_config(org_id=org_id)).revision == 0

            saved = await service.save_config(
                org_id=org_id,
                updated_by_user_id=user_id,
                expected_revision=0,
                config=document(),
            )
            assert saved.revision == 1

            updated_document = document()
            updated_document.tabs[0].label = "经营看板"
            updated = await service.save_config(
                org_id=org_id,
                updated_by_user_id=user_id,
                expected_revision=1,
                config=updated_document,
            )
            assert updated.revision == 2
            assert updated.config.tabs[0].label == "经营看板"

            with pytest.raises(TaskPromptConfigConflictError):
                await service.save_config(
                    org_id=org_id,
                    updated_by_user_id=user_id,
                    expected_revision=1,
                    config=document(),
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admin_save_maps_conflict_and_records_audit() -> None:
    class Service:
        async def save_config(self, **_kwargs):
            raise TaskPromptConfigConflictError("stale")

    class Audit:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def record(self, **kwargs):
            self.calls.append(kwargs)

    audit = Audit()
    payload = TaskPromptConfigSaveRequest(revision=0, config=document())

    with pytest.raises(HTTPException) as exc_info:
        await save_task_prompt_config(
            payload=payload,
            principal=AdminPrincipal(org_id=1, user_id=2, roles=("admin",)),
            service=Service(),
            audit_service=audit,
        )

    assert exc_info.value.status_code == 409
    assert audit.calls == []


def test_admin_and_user_routes_use_the_same_organization_scope(admin_api_access) -> None:
    class Service:
        def __init__(self) -> None:
            self.org_ids: list[int] = []

        async def get_config(self, *, org_id: int):
            self.org_ids.append(org_id)
            from app.schemas.task_prompt_config import TaskPromptConfigState
            return TaskPromptConfigState(revision=8, config=document())

    admin_service = Service()
    user_service = Service()

    async def ordinary_user() -> Principal:
        return Principal(org_id=9, user_id=10002)

    app.dependency_overrides[get_task_prompt_config_service] = lambda: admin_service
    app.dependency_overrides[get_principal] = ordinary_user
    import app.api.v1.user.task_prompt_config as user_api
    app.dependency_overrides[user_api.get_task_prompt_config_service] = lambda: user_service
    try:
        client = TestClient(app)
        admin_response = client.get("/api/v1/admin/task-prompt-config")
        user_response = client.get("/api/v1/user/task-prompt-config")
    finally:
        app.dependency_overrides.pop(get_task_prompt_config_service, None)
        app.dependency_overrides.pop(get_principal, None)
        app.dependency_overrides.pop(user_api.get_task_prompt_config_service, None)

    assert admin_response.status_code == 200
    assert admin_response.json()["revision"] == 8
    assert admin_service.org_ids == [1]
    assert user_response.status_code == 200
    assert user_response.json()["revision"] == 8
    assert user_service.org_ids == [9]
