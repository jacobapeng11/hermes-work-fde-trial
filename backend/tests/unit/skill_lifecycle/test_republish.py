from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.schemas.asset_catalog import AssetCatalogResult
from app.services.asset_publication import PublicAssetPublicationService


@pytest.mark.asyncio
async def test_republish_creates_rollout_even_when_current_version_is_unchanged() -> None:
    service = object.__new__(PublicAssetPublicationService)
    service.db = AsyncMock()
    service.rollouts = AsyncMock()
    service.application_rollouts = AsyncMock()
    service.catalog = AsyncMock()
    service.catalog.republish_skill = AsyncMock(
        return_value=AssetCatalogResult(
            asset_type="skill",
            asset_id=7,
            version_id=70,
            version=2,
            created_version=False,
            content_digest="sha256:" + "a" * 64,
        )
    )
    service.rollouts.create = AsyncMock(return_value="rollout")

    result = await service.republish_skill(
        org_id=1,
        skill_id=7,
        rollout_id="skill-publish-7",
        created_by_user_id=9,
        change_summary="republish",
    )

    assert result.catalog is not None
    service.rollouts.create.assert_awaited_once()
    service.application_rollouts.refresh_public_asset.assert_awaited_once_with(
        org_id=1,
        asset_type="skill",
        asset_id=7,
        created_by_user_id=9,
    )
