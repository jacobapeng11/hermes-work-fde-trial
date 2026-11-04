from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.profile_desired import DefaultProfileAssetOperationAccepted


class PersonalSkillSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: int
    skill_version_id: int | None
    slug: str
    version: int | None
    name: str
    description: str | None
    category_key: str = "uncategorized"
    category_scope: Literal["organization", "personal", "uncategorized"] = (
        "uncategorized"
    )
    status: str
    source: str = "personal_asset"
    applied: bool = False


class PersonalSkillDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset: PersonalSkillSummary
    detail: dict


class PersonalSkillInstallAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset: PersonalSkillSummary
    operation: DefaultProfileAssetOperationAccepted


class PersonalSkillRemovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: int
    rollout_id: str
    affected_profile_ids: list[int]
    desired_revisions: dict[int, int]


class PersonalSkillCategorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_key: str
    display_name: str
    scope: Literal["system", "organization", "personal", "uncategorized"]
    is_builtin: bool = False


class CreatePersonalSkillCategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=100)


class SkillDescriptionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default="", max_length=2000)
    name: str | None = Field(default=None, min_length=1, max_length=200)


class SystemSkillSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    description: str | None = None
    category_key: Literal["system"] = "system"
    source: Literal["system"] = "system"
