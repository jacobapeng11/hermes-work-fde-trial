from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.v1.admin.assets import delete_mcp, delete_skill, enable_mcp
from app.models import Mcp, McpSlugLease, McpVersion, Skill, SkillVersion
from app.schemas.asset_catalog import (
    AssetExecutionSecurityRequest,
    CreateMcpAssetCommand,
)
from app.services.admin_auth import AdminPrincipal
from app.services.asset_catalog import AssetCatalogService
from tests.unit.test_asset_catalog import (
    _create_skill,
    _database,
    _mcp_material,
)


@pytest.mark.asyncio
async def test_admin_public_skill_permanent_delete_is_disabled_while_mcp_remains_deletable() -> None:
    engine, sessions = await _database()
    principal = AdminPrincipal(org_id=1, user_id=9, roles=("admin",))
    async with sessions() as session:
        catalog = AssetCatalogService(session)
        skill = await catalog.create_skill(_create_skill(owner_user_id=9))
        mcp = await catalog.create_mcp(
            CreateMcpAssetCommand(
                org_id=1,
                owner_user_id=9,
                slug="crm",
                name="CRM",
                visibility="organization",
                material=_mcp_material(),
            )
        )
        await session.commit()

        with pytest.raises(HTTPException) as skill_delete:
            await delete_skill(
                skill_id=skill.asset_id,
                principal=principal,
                db=session,
            )
        assert skill_delete.value.status_code == 410
        assert skill_delete.value.detail == "SKILL_PERMANENT_DELETE_DISABLED"
        first_mcp_delete = await delete_mcp(
            mcp_id=mcp.asset_id,
            principal=principal,
            db=session,
        )
        repeated_mcp_delete = await delete_mcp(
            mcp_id=mcp.asset_id,
            principal=principal,
            db=session,
        )

        deleted_skill = await session.get(Skill, skill.asset_id)
        deleted_mcp = await session.get(Mcp, mcp.asset_id)
        assert deleted_skill is not None and deleted_skill.status == "published"
        assert deleted_mcp is not None and deleted_mcp.status == "deleted"
        assert first_mcp_delete.created is True
        assert repeated_mcp_delete.created is False
        assert repeated_mcp_delete.rollout_id == first_mcp_delete.rollout_id
        assert await session.scalar(select(func.count(SkillVersion.id))) == 1
        assert await session.scalar(select(func.count(McpVersion.id))) == 1
        assert await session.scalar(
            select(func.count(McpSlugLease.id)).where(
                McpSlugLease.mcp_id == mcp.asset_id
            )
        ) == 0

        replacement = await catalog.create_mcp(
            CreateMcpAssetCommand(
                org_id=1,
                owner_user_id=9,
                slug="crm",
                name="CRM replacement",
                visibility="organization",
                material=_mcp_material(tool_name="crm.replacement"),
            )
        )
        assert replacement.asset_id != mcp.asset_id

        with pytest.raises(HTTPException) as deleted_enable:
            await enable_mcp(
                mcp_id=mcp.asset_id,
                payload=AssetExecutionSecurityRequest(reason="must stay deleted"),
                idempotency_key="deleted-mcp-enable",
                principal=principal,
                db=session,
            )
        assert deleted_enable.value.status_code == 404
        assert deleted_enable.value.detail == "MCP_UNAVAILABLE"
    await engine.dispose()


@pytest.mark.asyncio
async def test_admin_public_skill_permanent_delete_is_disabled_without_asset_disclosure() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        skill = await AssetCatalogService(session).create_skill(
            _create_skill(owner_user_id=9)
        )
        await session.commit()

        with pytest.raises(HTTPException) as error:
            await delete_skill(
                skill_id=skill.asset_id,
                principal=AdminPrincipal(org_id=2, user_id=9, roles=("admin",)),
                db=session,
            )
        assert error.value.status_code == 410
        assert error.value.detail == "SKILL_PERMANENT_DELETE_DISABLED"
    await engine.dispose()
