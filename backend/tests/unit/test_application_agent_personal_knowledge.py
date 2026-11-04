from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import BigInteger, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import (
    ApplicationAgent,
    ApplicationAgentConfigRevision,
    ApplicationAgentPersonalKnowledgeBinding,
    Base,
    KnowledgeBase,
    KnowledgeBaseFile,
    Organization,
    Permission,
    User,
)
from app.services.application_agent_personal_knowledge import (
    ApplicationAgentPersonalKnowledgeService,
)
from app.services.runtime_workspace import Principal


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_integer(
    _type: BigInteger,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "INTEGER"


async def _database() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                Organization.__table__,
                User.__table__,
                ApplicationAgent.__table__,
                KnowledgeBase.__table__,
                KnowledgeBaseFile.__table__,
                Permission.__table__,
                ApplicationAgentPersonalKnowledgeBinding.__table__,
                ApplicationAgentConfigRevision.__table__,
            ],
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(db: AsyncSession) -> None:
    db.add_all([
        Organization(id=1, code="org-1", name="测试企业"),
        User(id=7, org_id=1, name="创建者", status="active"),
        User(id=8, org_id=1, name="共享者", status="active"),
        ApplicationAgent(
            id=31,
            org_id=1,
            created_by_user_id=7,
            updated_by_user_id=7,
            slug="app-a",
            name="应用 A",
            config={},
        ),
        ApplicationAgent(
            id=32,
            org_id=1,
            created_by_user_id=7,
            updated_by_user_id=7,
            slug="app-b",
            name="应用 B",
            config={},
        ),
        KnowledgeBase(
            id=41,
            org_id=1,
            owner_user_id=7,
            slug="owned",
            name="我创建的知识库",
            kind="personal",
            status="active",
            dataset_id="dataset-41",
            visibility="private",
        ),
        KnowledgeBase(
            id=42,
            org_id=1,
            owner_user_id=8,
            slug="shared",
            name="新共享知识库",
            kind="personal",
            status="active",
            dataset_id="dataset-42",
            visibility="shared",
        ),
        Permission(
            org_id=1,
            asset_type="knowledge_base",
            asset_id=42,
            grantee_type="user",
            grantee_id="7",
            kb_role="retrieve",
        ),
    ])
    await db.commit()


@pytest.mark.asyncio
async def test_new_shared_library_is_visible_but_unselected_per_application() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db)
            service = ApplicationAgentPersonalKnowledgeService(db)
            principal = Principal(org_id=1, user_id=7)

            first = await service.catalog(principal=principal, agent_id=31)
            second = await service.catalog(principal=principal, agent_id=32)

            assert {item.knowledge_base_id for item in first.items} == {41, 42}
            assert all(item.selected is False for item in first.items)
            assert all(item.selected is False for item in second.items)

            selected = await service.select(
                principal=principal,
                agent_id=31,
                knowledge_base_id=42,
            )
            untouched = await service.catalog(
                principal=principal,
                agent_id=32,
            )

            assert next(
                item for item in selected.items
                if item.knowledge_base_id == 42
            ).selected is True
            assert next(
                item for item in untouched.items
                if item.knowledge_base_id == 42
            ).selected is False
            assert selected.enabled_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_revoked_library_disappears_and_reshare_requires_manual_selection() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as db:
            await _seed(db)
            service = ApplicationAgentPersonalKnowledgeService(db)
            principal = Principal(org_id=1, user_id=7)
            await service.select(
                principal=principal,
                agent_id=31,
                knowledge_base_id=42,
            )

            await db.execute(delete(Permission).where(Permission.asset_id == 42))
            await db.commit()
            revoked = await service.catalog(principal=principal, agent_id=31)

            assert {item.knowledge_base_id for item in revoked.items} == {41}
            assert revoked.enabled_count == 0
            assert revoked.revoked_count == 1

            db.add(Permission(
                org_id=1,
                asset_type="knowledge_base",
                asset_id=42,
                grantee_type="user",
                grantee_id="7",
                kb_role="retrieve",
            ))
            await db.commit()
            reshared = await service.catalog(principal=principal, agent_id=31)

            candidate = next(
                item for item in reshared.items
                if item.knowledge_base_id == 42
            )
            assert candidate.selected is False
            assert reshared.enabled_count == 0
            assert reshared.revoked_count == 1
    finally:
        await engine.dispose()
