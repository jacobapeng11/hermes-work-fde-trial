from __future__ import annotations

import pytest

from app.services.asset_visibility import (
    AssetNotVisibleError,
    AssetPermissionRecord,
    AssetVisibilityRecord,
    AssetVisibilityService,
)
from app.services.runtime_workspace import Principal


class FakeAssetVisibilityStore:
    def __init__(self) -> None:
        self.assets = [
            AssetVisibilityRecord(
                asset_type="skill",
                asset_id=1,
                version_id=10,
                org_id=7,
                owner_user_id=9,
                status="active",
                visibility="private",
            ),
            AssetVisibilityRecord(
                asset_type="skill",
                asset_id=2,
                version_id=20,
                org_id=7,
                owner_user_id=10,
                status="active",
                visibility="organization",
            ),
            AssetVisibilityRecord(
                asset_type="skill",
                asset_id=3,
                version_id=30,
                org_id=7,
                owner_user_id=10,
                status="draft",
                visibility="organization",
            ),
            AssetVisibilityRecord(
                asset_type="skill",
                asset_id=4,
                version_id=40,
                org_id=8,
                owner_user_id=9,
                status="active",
                visibility="public",
            ),
            AssetVisibilityRecord(
                asset_type="mcp",
                asset_id=5,
                org_id=7,
                owner_user_id=10,
                status="approved",
                visibility="private",
            ),
            AssetVisibilityRecord(
                asset_type="knowledge_base",
                asset_id=6,
                org_id=7,
                owner_user_id=10,
                status="published",
                visibility="private",
            ),
        ]
        self.permissions = [
            AssetPermissionRecord(
                asset_type="mcp",
                asset_id=5,
                org_id=7,
                grantee_type="user",
                grantee_id="9",
            ),
            AssetPermissionRecord(
                asset_type="knowledge_base",
                asset_id=6,
                org_id=7,
                grantee_type="organization",
                grantee_id="7",
            ),
        ]

    async def list_assets(self, *, org_id: int, asset_type: str, asset_ids: set[int] | None = None):
        return [
            asset for asset in self.assets
            if asset.org_id == org_id
            and asset.asset_type == asset_type
            and (asset_ids is None or asset.asset_id in asset_ids)
        ]

    async def list_permissions(self, *, org_id: int, asset_type: str, asset_ids: set[int]):
        return [
            permission for permission in self.permissions
            if permission.org_id == org_id
            and permission.asset_type == asset_type
            and permission.asset_id in asset_ids
        ]


async def test_list_visible_assets_includes_owner_org_and_permission_grants() -> None:
    service = AssetVisibilityService(FakeAssetVisibilityStore())

    skills = await service.list_visible_assets(
        principal=Principal(org_id=7, user_id=9),
        asset_type="skill",
    )
    mcps = await service.list_visible_assets(
        principal=Principal(org_id=7, user_id=9),
        asset_type="mcp",
    )
    knowledge_bases = await service.list_visible_assets(
        principal=Principal(org_id=7, user_id=9),
        asset_type="knowledge_base",
    )

    assert [skill.asset_id for skill in skills] == [1, 2]
    assert [mcp.asset_id for mcp in mcps] == [5]
    assert [knowledge_base.asset_id for knowledge_base in knowledge_bases] == [6]


async def test_require_visible_assets_rejects_missing_or_private_assets() -> None:
    service = AssetVisibilityService(FakeAssetVisibilityStore())

    with pytest.raises(AssetNotVisibleError, match="skill is not visible: 3"):
        await service.require_visible_assets(
            principal=Principal(org_id=7, user_id=9),
            asset_type="skill",
            asset_ids={1, 3},
        )


async def test_require_visible_assets_returns_visible_ids() -> None:
    service = AssetVisibilityService(FakeAssetVisibilityStore())

    result = await service.require_visible_assets(
        principal=Principal(org_id=7, user_id=9),
        asset_type="skill",
        asset_ids={1, 2},
    )

    assert result == {1, 2}
