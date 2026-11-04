from __future__ import annotations

import pytest

from app.models import SkillCategory
from app.services.personal_skill_categories import (
    PersonalSkillCategoryError,
    PersonalSkillCategoryService,
)
from app.services.runtime_workspace import Principal
from tests.unit.test_profile_desired import _database, _seed


@pytest.mark.asyncio
async def test_personal_category_is_private_and_upload_validation_is_owner_scoped() -> None:
    engine, sessions = await _database()
    async with sessions() as db:
        await _seed(db)
        db.add(
            SkillCategory(
                org_id=1,
                category_key="productivity",
                display_name="效率工具",
                is_builtin=False,
                is_market_visible=True,
                is_personal_visible=True,
            )
        )
        await db.commit()
        service = PersonalSkillCategoryService(db)
        owner = Principal(org_id=1, user_id=2)
        other = Principal(org_id=1, user_id=3)

        created = await service.create(principal=owner, display_name="  我的汇报  ")
        await db.commit()

        assert created.scope == "personal"
        assert created.category_key.startswith("personal-")
        owner_keys = {
            item.category_key
            for item in await service.list_available(principal=owner)
        }
        other_keys = {
            item.category_key
            for item in await service.list_available(principal=other)
        }
        assert created.category_key in owner_keys
        assert created.category_key not in other_keys
        assert "productivity" in owner_keys and "productivity" in other_keys
        assert await service.validate_upload_category(
            principal=owner,
            category_key=created.category_key,
        ) == (created.category_key, "personal")
        with pytest.raises(
            PersonalSkillCategoryError,
            match="personal_skill_category_unavailable",
        ):
            await service.validate_upload_category(
                principal=other,
                category_key=created.category_key,
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_personal_category_name_is_normalized_and_unique_per_owner() -> None:
    engine, sessions = await _database()
    async with sessions() as db:
        await _seed(db)
        service = PersonalSkillCategoryService(db)
        principal = Principal(org_id=1, user_id=2)
        await service.create(principal=principal, display_name="研发 工具")
        await db.commit()

        with pytest.raises(
            PersonalSkillCategoryError,
            match="personal_skill_category_exists",
        ):
            await service.create(principal=principal, display_name="  研发   工具 ")
    await engine.dispose()
