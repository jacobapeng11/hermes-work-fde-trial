from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.v1.admin.assets import (
    create_mcp as create_mcp_api,
    delete_mcp as delete_mcp_api,
    enable_mcp as enable_mcp_api,
    force_disable_mcp as force_disable_mcp_api,
    publish_mcp_version as publish_mcp_version_api,
)
from app.models import Mcp
from app.schemas.asset_catalog import (
    AssetExecutionSecurityRequest,
    CreateStableMcpRequest,
    McpVersionMaterial,
    PublishStableMcpVersionRequest,
)
from app.services.admin_auth import AdminPrincipal
from tests.unit.test_asset_catalog import _database


def _material(url: str) -> McpVersionMaterial:
    return McpVersionMaterial(
        transport="streamable_http",
        connection_config={"url": url},
    )


@pytest.mark.asyncio
async def test_admin_mcp_update_and_reversible_execution_security_contract() -> None:
    engine, sessions = await _database()
    principal = AdminPrincipal(org_id=1, user_id=9, roles=("admin",))
    async with sessions() as session:
        created = await create_mcp_api(
            payload=CreateStableMcpRequest(
                slug="crm",
                name="CRM",
                material=_material("https://old.example.com/mcp"),
            ),
            principal=principal,
            db=session,
        )
        assert created.version == 1

        publication = await publish_mcp_version_api(
            mcp_id=created.asset_id,
            payload=PublishStableMcpVersionRequest(
                expected_current_version_id=int(created.current_version_id),
                name="CRM v2",
                description="new endpoint",
                material=_material("https://new.example.com/mcp"),
            ),
            principal=principal,
            db=session,
        )
        assert publication.asset.asset_id == created.asset_id
        assert publication.asset.version == 2
        assert publication.rollout is not None
        assert publication.rollout.affected_profile_count == 0

        with pytest.raises(HTTPException) as stale_error:
            await publish_mcp_version_api(
                mcp_id=created.asset_id,
                payload=PublishStableMcpVersionRequest(
                    expected_current_version_id=int(created.current_version_id),
                    name="stale",
                    material=_material("https://stale.example.com/mcp"),
                ),
                principal=principal,
                db=session,
            )
        assert stale_error.value.status_code == 409
        assert stale_error.value.detail == "MCP_VERSION_CONFLICT"

        disabled = await force_disable_mcp_api(
            mcp_id=created.asset_id,
            payload=AssetExecutionSecurityRequest(reason="security incident"),
            idempotency_key="mcp-security-1",
            principal=principal,
            db=session,
        )
        assert disabled.rollout_id.startswith("mcp-revoke-")
        assert disabled.affected_profile_count == 0
        row = await session.get(Mcp, created.asset_id)
        assert row is not None and row.status == "published"
        assert row.archived_at is None and row.revoked_at is not None

        enabled = await enable_mcp_api(
            mcp_id=created.asset_id,
            payload=AssetExecutionSecurityRequest(reason="incident resolved"),
            idempotency_key="mcp-security-2",
            principal=principal,
            db=session,
        )
        assert enabled.rollout_id.startswith("mcp-enable-")
        assert enabled.affected_profile_count == 0
        await session.refresh(row)
        assert row.status == "published"
        assert row.archived_at is None and row.revoked_at is None

        with pytest.raises(HTTPException) as duplicate_slug:
            await create_mcp_api(
                payload=CreateStableMcpRequest(
                    slug="crm",
                    name="CRM replacement",
                    material=_material("https://replacement.example.com/mcp"),
                ),
                principal=principal,
                db=session,
            )
        assert duplicate_slug.value.status_code == 409
        assert duplicate_slug.value.detail == "MCP_SLUG_EXISTS"

        deleted = await delete_mcp_api(
            mcp_id=created.asset_id,
            principal=principal,
            db=session,
        )
        assert deleted.rollout_id.startswith("mcp-delete-")
        assert deleted.affected_profile_count == 0
        await session.refresh(row)
        assert row.status == "deleted"
    await engine.dispose()
