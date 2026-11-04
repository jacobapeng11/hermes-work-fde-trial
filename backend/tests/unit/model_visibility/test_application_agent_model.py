from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    ApplicationAgent,
    ApplicationAgentPersonalKnowledgeBinding,
    ModelEntry,
    ModelUpgradeLog,
    ModelProvider,
    Organization,
    Permission,
    SysDept,
    SysUser,
    User,
)
from app.models.base import Base
from app.schemas.application_agent import (
    ApplicationAgentConfig,
    ApplicationAgentUpdateRequest,
)
from app.services.application_agents import (
    ApplicationAgentError,
    SqlAlchemyApplicationAgentService,
)
from app.services.runtime_workspace import Principal


APPLICATION_MODEL_TABLES = [
    Organization.__table__,
    User.__table__,
    SysDept.__table__,
    SysUser.__table__,
    Permission.__table__,
    ModelProvider.__table__,
    ModelEntry.__table__,
    ModelUpgradeLog.__table__,
    ApplicationAgent.__table__,
    ApplicationAgentPersonalKnowledgeBinding.__table__,
]


@pytest.mark.asyncio
async def test_application_agent_update_rejects_model_invisible_to_creator() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all,
                tables=APPLICATION_MODEL_TABLES,
            )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            db.add(Organization(id=1, name="Org", code="default", status="active"))
            db.add(
                User(
                    id=101,
                    org_id=1,
                    user_code="u-owner",
                    name="创建者",
                    status="active",
                )
            )
            db.add(SysDept(dept_id="dept-a", parent_id=None, name="研发", status=1))
            db.add(
                SysUser(
                    user_id="hrm-owner",
                    username="u-owner",
                    code="u-owner",
                    dept_id="dept-a",
                    status=1,
                )
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
            db.add(
                ModelEntry(
                    id=1,
                    org_id=1,
                    provider_id=1,
                    model_key="model-a",
                    display_name="Model A",
                    status="active",
                    is_restricted=True,
                    catalog_version=1,
                )
            )
            db.add(
                ApplicationAgent(
                    id=31,
                    org_id=1,
                    slug="ticket-agent",
                    name="工单助手",
                    status="unpublished",
                    created_by_user_id=101,
                    updated_by_user_id=101,
                    config={"model_entry_id": 99},
                    config_generation=2,
                    apply_status="not_applied",
                    lifecycle_status="active",
                )
            )
            await db.commit()

            service = SqlAlchemyApplicationAgentService(db)
            request = ApplicationAgentUpdateRequest(
                name="工单助手",
                avatar_url=None,
                config=ApplicationAgentConfig(model_entry_id=99),
                expected_config_generation=2,
            )

            with pytest.raises(ApplicationAgentError) as exc_info:
                await service.update_agent(
                    principal=Principal(org_id=1, user_id=101),
                    agent_id=31,
                    payload=request,
                )

            assert exc_info.value.code == "application_agent_model_unavailable"
            assert exc_info.value.status_code == 409
    finally:
        await engine.dispose()
