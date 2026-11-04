from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.identity import SysDept
from app.schemas.experts import ExpertShareScope
from app.services.expert_errors import ExpertProfileError
from app.services.expert_review_commands import ExpertReviewCommandService
from app.services.runtime_workspace import Principal


async def _database() -> async_sessionmaker:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SysDept.__table__.create(sync_connection)
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(
            [
                SysDept(
                    dept_id="D100",
                    parent_id=None,
                    code="active-department",
                    name="有效部门",
                    status=1,
                ),
                SysDept(
                    dept_id="D200",
                    parent_id=None,
                    code="disabled-department",
                    name="停用部门",
                    status=0,
                ),
            ]
        )
        await session.commit()
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_company_roots_and_active_departments_pass_review_validation() -> None:
    sessions = await _database()
    engine = sessions.kw["bind"]
    try:
        async with sessions() as db:
            await ExpertReviewCommandService(db)._validate_share_scope(
                principal=Principal(org_id=1, user_id=1),
                scope=ExpertShareScope(
                    organization=False,
                    user_ids=[],
                    department_ids=["1", "2", "D100"],
                ),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "department_ids",
    [
        ["virtual:unassigned"],
        ["1", "D999"],
        ["1", "D200"],
    ],
    ids=["virtual-node", "missing-department", "disabled-department"],
)
async def test_invalid_review_departments_are_rejected(department_ids: list[str]) -> None:
    sessions = await _database()
    engine = sessions.kw["bind"]
    try:
        async with sessions() as db:
            with pytest.raises(ExpertProfileError) as excinfo:
                await ExpertReviewCommandService(db)._validate_share_scope(
                    principal=Principal(org_id=1, user_id=1),
                    scope=ExpertShareScope(
                        organization=False,
                        user_ids=[],
                        department_ids=department_ids,
                    ),
                )
            assert excinfo.value.code == "invalid_expert_share_department"
    finally:
        await engine.dispose()
