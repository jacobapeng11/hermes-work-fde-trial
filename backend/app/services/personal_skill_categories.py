from __future__ import annotations

from hashlib import sha256
import unicodedata

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PersonalSkillCategory, SkillCategory
from app.schemas.personal_skills import PersonalSkillCategorySummary
from app.services.runtime_workspace import Principal


class PersonalSkillCategoryError(ValueError):
    pass


class PersonalSkillCategoryService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_available(
        self,
        *,
        principal: Principal,
    ) -> list[PersonalSkillCategorySummary]:
        organization = (
            await self.db.scalars(
                select(SkillCategory)
                .where(
                    SkillCategory.org_id == principal.org_id,
                    SkillCategory.is_personal_visible.is_(True),
                )
                .order_by(SkillCategory.display_name, SkillCategory.category_key)
            )
        ).all()
        personal = (
            await self.db.scalars(
                select(PersonalSkillCategory)
                .where(
                    PersonalSkillCategory.org_id == principal.org_id,
                    PersonalSkillCategory.owner_user_id == principal.user_id,
                )
                .order_by(
                    PersonalSkillCategory.display_name,
                    PersonalSkillCategory.category_key,
                )
            )
        ).all()
        return [
            PersonalSkillCategorySummary(
                category_key="system",
                display_name="系统技能",
                scope="system",
                is_builtin=True,
            ),
            PersonalSkillCategorySummary(
                category_key="uncategorized",
                display_name="未分类",
                scope="uncategorized",
                is_builtin=True,
            ),
            *[
                PersonalSkillCategorySummary(
                    category_key=str(row.category_key),
                    display_name=str(row.display_name),
                    scope="organization",
                    is_builtin=bool(row.is_builtin),
                )
                for row in organization
                if row.category_key not in {"system", "uncategorized", "personal"}
            ],
            *[
                PersonalSkillCategorySummary(
                    category_key=str(row.category_key),
                    display_name=str(row.display_name),
                    scope="personal",
                    is_builtin=False,
                )
                for row in personal
            ],
        ]

    async def create(
        self,
        *,
        principal: Principal,
        display_name: str,
    ) -> PersonalSkillCategorySummary:
        display = " ".join(display_name.split()).strip()
        normalized = unicodedata.normalize("NFKC", display).casefold()
        if not normalized or len(display) > 100 or len(normalized) > 100:
            raise PersonalSkillCategoryError("personal_skill_category_name_invalid")
        existing = await self.db.scalar(
            select(PersonalSkillCategory).where(
                PersonalSkillCategory.org_id == principal.org_id,
                PersonalSkillCategory.owner_user_id == principal.user_id,
                PersonalSkillCategory.normalized_name == normalized,
            )
        )
        if existing is not None:
            raise PersonalSkillCategoryError("personal_skill_category_exists")
        digest = sha256(
            f"{principal.org_id}:{principal.user_id}:{normalized}".encode("utf-8")
        ).hexdigest()[:24]
        row = PersonalSkillCategory(
            org_id=principal.org_id,
            owner_user_id=principal.user_id,
            category_key=f"personal-{digest}",
            display_name=display,
            normalized_name=normalized,
        )
        self.db.add(row)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            raise PersonalSkillCategoryError(
                "personal_skill_category_exists"
            ) from exc
        return PersonalSkillCategorySummary(
            category_key=str(row.category_key),
            display_name=str(row.display_name),
            scope="personal",
            is_builtin=False,
        )

    async def validate_upload_category(
        self,
        *,
        principal: Principal,
        category_key: str,
    ) -> tuple[str, str]:
        key = category_key.strip() or "uncategorized"
        if len(key) > 64:
            raise PersonalSkillCategoryError("personal_skill_category_invalid")
        if key == "uncategorized":
            return key, "uncategorized"
        if key in {"system", "personal"}:
            raise PersonalSkillCategoryError("personal_skill_category_invalid")
        organization = await self.db.scalar(
            select(SkillCategory.id).where(
                SkillCategory.org_id == principal.org_id,
                SkillCategory.category_key == key,
                SkillCategory.is_personal_visible.is_(True),
            )
        )
        if organization is not None:
            return key, "organization"
        personal = await self.db.scalar(
            select(PersonalSkillCategory.id).where(
                PersonalSkillCategory.org_id == principal.org_id,
                PersonalSkillCategory.owner_user_id == principal.user_id,
                PersonalSkillCategory.category_key == key,
            )
        )
        if personal is not None:
            return key, "personal"
        raise PersonalSkillCategoryError("personal_skill_category_unavailable")

    async def scope_for_key(
        self,
        *,
        principal: Principal,
        category_key: str,
    ) -> str:
        try:
            _key, scope = await self.validate_upload_category(
                principal=principal,
                category_key=category_key,
            )
        except PersonalSkillCategoryError:
            return "uncategorized"
        return scope


__all__ = ["PersonalSkillCategoryError", "PersonalSkillCategoryService"]
