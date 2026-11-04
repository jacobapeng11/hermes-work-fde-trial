from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AssetSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_type: str
    asset_id: int
    version_id: int | None = None
    slug: str
    name: str
    description: str | None = None
    status: str
    visibility: str | None = None
    source_type: str | None = None


class ExpertPackageSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    expert_package_id: int
    slug: str
    name: str
    description: str | None = None
    avatar_url: str | None = None
    status: str
    visibility: str
    current_version_id: int | None = None
    updated_at: datetime | None = None


class ExpertPackageVersionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    expert_package_version_id: int
    expert_package_id: int
    version: int
    status: str
    manifest: dict
    skill_refs: list[dict] | None = None
    mcp_refs: list[dict] | None = None
    knowledge_refs: list[dict] | None = None
    model_preferences: dict | None = None
    created_at: datetime | None = None


class PlazaTemplateSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    template_id: int
    slug: str
    name: str
    description: str | None = None
    icon_url: str | None = None
    category: str | None = None
    tags: list[str] = []
    visibility: str
    status: str
    current_version_id: int | None = None
    sort_order: int = 0
    updated_at: datetime | None = None


class SkillCategorySummary(BaseModel):
    category_key: str
    display_name: str
    is_builtin: bool = False
    is_market_visible: bool = True
    is_personal_visible: bool = True


class CreateSkillCategoryRequest(BaseModel):
    category_key: str | None = None
    display_name: str


class SkillHubMarketItem(BaseModel):
    namespace: str
    public_slug: str
    name: str
    description: str | None = None
    category_key: str
    upstream_category: str | None = None
    version_label: str
    downloads: int
    installs: int
    state: str
    installed_skill_id: int | None = None
    installed_version_label: str | None = None
    operating: bool = False


class SkillHubMarketPage(BaseModel):
    enabled: bool
    items: list[SkillHubMarketItem]
    next_cursor: str | None = None


class SkillHubInstallRequest(BaseModel):
    namespace: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    public_slug: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    expected_version: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+.-]*$")


class SkillOperationAccepted(BaseModel):
    operation_id: str
    status: str
    target_key: str | None = None


class InstallExpertResponse(BaseModel):
    runtime_workspace_id: int
    expert_package_id: int
    expert_package_version_id: int
    installed_expert_binding_id: int
    status: str
    projection_scope: str
    pending_reload: bool
    warnings: list[str] = []
