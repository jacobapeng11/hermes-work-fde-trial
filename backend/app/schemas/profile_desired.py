from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictProfileDesiredModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileDesiredDefinition(StrictProfileDesiredModel):
    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    avatar_url: str | None = Field(default=None, max_length=500)
    soul_content: str = Field(default="", max_length=200_000)
    system_prompt: str | None = Field(default=None, max_length=200_000)
    enterprise_knowledge_enabled: bool = False
    plugin_ids: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("display_name must not be blank")
        return normalized

    @field_validator("description", "avatar_url", "system_prompt")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("plugin_ids")
    @classmethod
    def normalize_plugin_ids(cls, value: list[str]) -> list[str]:
        normalized = sorted({item.strip() for item in value if item.strip()})
        if len(normalized) != len(value):
            raise ValueError("plugin_ids must be non-empty and unique")
        return normalized


class ProfileDesiredSkillRef(StrictProfileDesiredModel):
    skill_id: int = Field(gt=0)
    version_policy: Literal["follow_latest", "pinned"] = "follow_latest"
    skill_version_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_pinned_version(self) -> "ProfileDesiredSkillRef":
        if self.version_policy == "pinned" and self.skill_version_id is None:
            raise ValueError("pinned Skill requires skill_version_id")
        if self.version_policy == "follow_latest" and self.skill_version_id is not None:
            raise ValueError("follow_latest Skill must not supply skill_version_id")
        return self


class ProfileDesiredMcpRef(StrictProfileDesiredModel):
    mcp_id: int = Field(gt=0)


class ProfileDesiredCommand(StrictProfileDesiredModel):
    org_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    runtime_workspace_id: int = Field(gt=0)
    runtime_agent_profile_id: int = Field(gt=0)
    definition: ProfileDesiredDefinition
    skills: list[ProfileDesiredSkillRef] = Field(default_factory=list, max_length=256)
    mcps: list[ProfileDesiredMcpRef] = Field(default_factory=list, max_length=128)
    knowledge_base_ids: list[int] = Field(default_factory=list, max_length=128)
    expert_package_version_id: int | None = Field(default=None, gt=0)
    source_type: Literal[
        "default_user",
        "private_expert",
        "market_expert",
        "asset_rollout",
        "migration",
        "forced_revoke",
    ]
    source_id: str | None = Field(default=None, min_length=1, max_length=128)
    created_by_user_id: int | None = Field(default=None, gt=0)

    @field_validator("knowledge_base_ids")
    @classmethod
    def normalize_knowledge_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("knowledge_base_ids must be positive")
        normalized = sorted(set(value))
        if len(normalized) != len(value):
            raise ValueError("knowledge_base_ids must be unique")
        return normalized

    @model_validator(mode="after")
    def require_unique_assets(self) -> "ProfileDesiredCommand":
        skill_ids = [item.skill_id for item in self.skills]
        mcp_ids = [item.mcp_id for item in self.mcps]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("skills must be unique by skill_id")
        if len(mcp_ids) != len(set(mcp_ids)):
            raise ValueError("mcps must be unique by mcp_id")
        self.skills = sorted(self.skills, key=lambda item: item.skill_id)
        self.mcps = sorted(self.mcps, key=lambda item: item.mcp_id)
        return self


class ProfileDesiredResult(StrictProfileDesiredModel):
    runtime_agent_profile_id: int = Field(gt=0)
    desired_revision: int = Field(gt=0)
    applied_revision: int = Field(ge=0)
    profile_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    skill_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    mcp_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    knowledge_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    convergence_status: Literal["waiting", "updating", "applied"]
    created_revision: bool


class DefaultProfileAssetMutation(StrictProfileDesiredModel):
    org_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    runtime_workspace_id: int = Field(gt=0)
    runtime_agent_profile_id: int = Field(gt=0)
    asset_type: Literal["skill", "mcp", "knowledge"]
    action: Literal["install", "uninstall"]
    asset_id: int = Field(gt=0)
    operation_id: str = Field(min_length=8, max_length=128)


class DefaultProfileAssetItem(StrictProfileDesiredModel):
    asset_type: Literal["skill", "mcp"]
    asset_id: int = Field(gt=0)
    current_version_id: int | None = Field(default=None, gt=0)
    asset_scope: Literal["public", "personal"] | None = None
    version: int | None = Field(default=None, gt=0)
    configured: bool = False
    desired: bool
    applied: bool
    name: str
    description: str | None = None
    status: str
    category_key: str | None = None
    transport: str | None = None


class DefaultProfileAssetView(StrictProfileDesiredModel):
    runtime_workspace_id: int = Field(gt=0)
    runtime_agent_profile_id: int = Field(gt=0)
    desired_revision: int = Field(ge=0)
    applied_revision: int = Field(ge=0)
    convergence_status: str
    last_error: str | None = None
    skills: list[DefaultProfileAssetItem] = Field(default_factory=list)
    mcps: list[DefaultProfileAssetItem] = Field(default_factory=list)


class UserAssetCatalogView(StrictProfileDesiredModel):
    skills: list[DefaultProfileAssetItem] = Field(default_factory=list)
    mcps: list[DefaultProfileAssetItem] = Field(default_factory=list)


class DefaultProfileAssetOperationAccepted(StrictProfileDesiredModel):
    operation_id: str = Field(min_length=8, max_length=128)
    runtime_workspace_id: int = Field(gt=0)
    runtime_agent_profile_id: int = Field(gt=0)
    asset_type: Literal["skill", "mcp"]
    asset_id: int = Field(gt=0)
    action: Literal["install", "uninstall"]
    desired_revision: int = Field(gt=0)
    applied_revision: int = Field(ge=0)
    convergence_status: Literal["waiting", "updating", "applied"]
