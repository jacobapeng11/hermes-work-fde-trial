from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.v1.admin.assets import create_skill_category
from app.schemas.assets import CreateSkillCategoryRequest
from app.services.admin_auth import AdminPrincipal
from tests.unit.test_asset_catalog import _database


@pytest.mark.asyncio
async def test_admin_creates_category_from_display_name_and_rejects_duplicate() -> None:
    engine, sessions = await _database()
    principal = AdminPrincipal(org_id=1, user_id=9, roles=("admin",))
    async with sessions() as db:
        created = await create_skill_category(
            payload=CreateSkillCategoryRequest(display_name="写作"),
            principal=principal,
            db=db,
        )

        assert created.category_key.startswith("category-")
        assert created.display_name == "写作"
        assert created.is_market_visible is True
        assert created.is_personal_visible is True

        with pytest.raises(HTTPException) as duplicate:
            await create_skill_category(
                payload=CreateSkillCategoryRequest(display_name=" 写作 "),
                principal=principal,
                db=db,
            )
        assert duplicate.value.status_code == 409
        assert duplicate.value.detail == "category_exists"
    await engine.dispose()
