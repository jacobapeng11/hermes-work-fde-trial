from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.personal_knowledge import PersonalKnowledgeSelectedUser


EXPERT_ERROR_CODES = frozenset(
    {
        "EXPERT_NOT_VISIBLE",
        "EXPERT_NAME_CONFLICT",
        "EXPERT_REVIEW_STATE_CONFLICT",
        "EXPERT_INSTALLATION_DISABLED",
        "PROFILE_CORE_UNAVAILABLE",
        "SKILL_VERSION_UNAVAILABLE",
        "MCP_REGISTRY_UNAVAILABLE",
        "KNOWLEDGE_BASE_NO_PERMISSION",
        "CAPABILITY_FORCE_DISABLED",
    }
)


class ExpertCapabilityReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ``default_installed`` always addresses an installation record on the
    # caller's default Profile. It never accepts a client supplied Profile id.
    source: Literal["public", "personal", "default_installed"]
    asset_id: int
    version_id: int | None = None

    @field_validator("asset_id", "version_id")
    @classmethod
    def normalize_reference_ids(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("asset ids must be positive")
        return value


class ExpertCapabilitySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_ids: list[int] = Field(default_factory=list)
    mcp_ids: list[int] = Field(default_factory=list)
    knowledge_base_ids: list[int] = Field(default_factory=list)
    enterprise_knowledge_enabled: bool = False
    plugin_ids: list[str] = Field(default_factory=list)
    # Source-aware fields are the write model.  Numeric lists remain for
    # legacy callers and are interpreted as public assets.
    skills: list[ExpertCapabilityReference] = Field(default_factory=list)
    mcps: list[ExpertCapabilityReference] = Field(default_factory=list)
    knowledge_bases: list[ExpertCapabilityReference] = Field(default_factory=list)

    @field_validator("skill_ids", "mcp_ids", "knowledge_base_ids")
    @classmethod
    def normalize_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("asset ids must be positive")
        return sorted(set(value))

    @field_validator("plugin_ids")
    @classmethod
    def normalize_plugin_ids(cls, value: list[str]) -> list[str]:
        normalized = sorted({item.strip() for item in value if item.strip()})
        if len(normalized) != len(value):
            raise ValueError("plugin ids must be non-empty and unique")
        return normalized

    @model_validator(mode="after")
    def normalize_references(self) -> "ExpertCapabilitySelection":
        def normalized(
            refs: list[ExpertCapabilityReference],
            legacy_ids: list[int],
        ) -> list[ExpertCapabilityReference]:
            source = refs or [
                ExpertCapabilityReference(source="public", asset_id=item)
                for item in legacy_ids
            ]
            unique: dict[tuple[str, int, int | None], ExpertCapabilityReference] = {}
            for item in source:
                unique[(item.source, item.asset_id, item.version_id)] = item
            return [
                unique[key]
                for key in sorted(unique, key=lambda item: (item[0], item[1], item[2] or 0))
            ]

        self.skills = normalized(self.skills, self.skill_ids)
        self.mcps = normalized(self.mcps, self.mcp_ids)
        self.knowledge_bases = normalized(
            self.knowledge_bases,
            self.knowledge_base_ids,
        )
        self.skill_ids = sorted(
            {item.asset_id for item in self.skills if item.source == "public"}
        )
        self.mcp_ids = sorted(
            {item.asset_id for item in self.mcps if item.source == "public"}
        )
        self.knowledge_base_ids = sorted(
            {
                item.asset_id
                for item in self.knowledge_bases
                if item.source == "public"
            }
        )
        return self


class ExpertShareScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization: bool = False
    user_ids: list[int] = Field(default_factory=list)
    department_ids: list[str] = Field(default_factory=list)

    @field_validator("user_ids")
    @classmethod
    def normalize_user_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("user ids must be positive")
        return sorted(set(value))

    @field_validator("department_ids")
    @classmethod
    def normalize_department_ids(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("department ids must be non-empty and unique")
        retained = [item.strip() for item in value]
        normalized = sorted(set(retained))
        if len(normalized) != len(retained):
            raise ValueError("department ids must be non-empty and unique")
        return normalized

    @model_validator(mode="after")
    def require_exclusive_scope(self) -> "ExpertShareScope":
        if self.organization and (self.user_ids or self.department_ids):
            raise ValueError(
                "organization scope must not include users or departments"
            )
        return self

    @property
    def is_private(self) -> bool:
        return (
            not self.organization
            and not self.user_ids
            and not self.department_ids
        )


class CreateExpertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_workspace_id: int
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    avatar_url: str | None = Field(default=None, max_length=500)
    soul_content: str = Field(default="", max_length=200_000)
    inherited_from_profile_id: int | None = None
    capabilities: ExpertCapabilitySelection = Field(
        default_factory=ExpertCapabilitySelection
    )
    share_scope: ExpertShareScope = Field(default_factory=ExpertShareScope)
    change_summary: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class UpdateExpertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    avatar_url: str | None = Field(default=None, max_length=500)
    soul_content: str | None = Field(default=None, max_length=200_000)
    change_summary: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value is not None else None


class UpdateExpertConfigurationRequest(BaseModel):
    """Complete editor draft persisted by one atomic expert save."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    avatar_url: str | None = Field(default=None, max_length=500)
    soul_content: str = Field(default="", max_length=200_000)
    capabilities: ExpertCapabilitySelection
    share_scope: ExpertShareScope
    change_summary: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class UpdateExpertCapabilitiesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: ExpertCapabilitySelection
    change_summary: str | None = Field(default=None, max_length=2000)


class UpdateExpertPermissionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    share_scope: ExpertShareScope
    change_summary: str | None = Field(default=None, max_length=2000)


class ExpertAllowedActions(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    view_detail: bool = False
    install: bool = False
    uninstall: bool = False
    summon: bool = False
    edit: bool = False
    copy_expert: bool = Field(default=False, alias="copy", serialization_alias="copy")
    publish: bool = False
    unlist: bool = False
    delete: bool = False


class RuntimeAgentProfileSummary(BaseModel):
    runtime_agent_profile_id: int
    runtime_workspace_id: int
    hermes_profile_name: str
    profile_kind: str
    display_name: str
    description: str | None = None
    avatar_url: str | None = None
    soul_content: str = ""
    ownership_type: str
    inherited_from_profile_id: int | None = None
    expert_package_id: int | None = None
    local_version: int | None = None
    installed_version_id: int | None = None
    installed_version: int | None = None
    available_version_id: int | None = None
    update_available: bool = False
    desired_revision: int = 0
    applied_revision: int = 0
    configured_expert_version_id: int | None = None
    applied_expert_version_id: int | None = None
    convergence_status: str = "unconfigured"
    effective_status: str = "unconfigured"
    market_removed: bool = False
    capabilities: ExpertCapabilitySelection = Field(
        default_factory=ExpertCapabilitySelection
    )
    share_scope: ExpertShareScope = Field(default_factory=ExpertShareScope)
    share_scope_users: list[PersonalKnowledgeSelectedUser] = Field(
        default_factory=list
    )
    review_id: int | None = None
    review_status: str | None = None
    review_comment: str | None = None
    last_operation_id: str | None = None
    last_operation_action: str | None = None
    last_operation_status: str | None = None
    last_operation_step: str | None = None
    last_operation_error: str | None = None
    allowed_actions: ExpertAllowedActions = Field(default_factory=ExpertAllowedActions)
    status: str
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ExpertOperationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    action: str
    status: Literal["pending", "running", "succeeded", "failed"]
    stage: str
    target_id: int | None = None
    profile_id: int | None = None
    completed_units: int | None = Field(default=None, ge=0)
    total_units: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    error_message: str | None = None


class ExpertMarketItem(BaseModel):
    expert_package_id: int
    expert_package_version_id: int
    name: str
    description: str | None = None
    avatar_url: str | None = None
    provider_name: str
    version: int
    installed: bool = False
    installed_runtime_agent_profile_id: int | None = None
    catalog_status: Literal["published", "unlisted", "deleted"] = "published"
    execution_status: Literal["enabled", "force_disabled"] = "enabled"
    allowed_actions: ExpertAllowedActions = Field(default_factory=ExpertAllowedActions)
    operation: ExpertOperationSummary | None = None
    updated_at: datetime | None = None


class ExpertMarketCapabilitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None


class ExpertMarketDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expert_package_id: int
    expert_package_version_id: int
    name: str
    description: str | None = None
    avatar_url: str | None = None
    provider_name: str
    version: int
    installed: bool = False
    installed_runtime_agent_profile_id: int | None = None
    catalog_status: Literal["published", "unlisted", "deleted"] = "published"
    execution_status: Literal["enabled", "force_disabled"] = "enabled"
    allowed_actions: ExpertAllowedActions = Field(default_factory=ExpertAllowedActions)
    soul_content: str = ""
    system_prompt: str | None = None
    skills: list[ExpertMarketCapabilitySummary] = Field(default_factory=list)
    mcps: list[ExpertMarketCapabilitySummary] = Field(default_factory=list)
    knowledge_bases: list[ExpertMarketCapabilitySummary] = Field(default_factory=list)


class ExpertCapabilityCatalogItem(BaseModel):
    asset_id: int
    name: str
    description: str | None = None
    kind: str | None = None
    source: Literal["public", "personal", "default_installed"] = "public"
    version_id: int | None = None
    category_key: str | None = None
    runtime_skill_name: str | None = None


class ExpertSkillCategory(BaseModel):
    category_key: str
    display_name: str


class ExpertCapabilityCatalog(BaseModel):
    market_skills: list[ExpertCapabilityCatalogItem] = Field(default_factory=list)
    installed_skills: list[ExpertCapabilityCatalogItem] = Field(default_factory=list)
    skill_categories: list[ExpertSkillCategory] = Field(default_factory=list)
    market_mcps: list[ExpertCapabilityCatalogItem] = Field(default_factory=list)
    installed_mcps: list[ExpertCapabilityCatalogItem] = Field(default_factory=list)
    knowledge_bases: list[ExpertCapabilityCatalogItem] = Field(default_factory=list)
    personal_skills: list[ExpertCapabilityCatalogItem] = Field(default_factory=list)
    personal_knowledge_bases: list[ExpertCapabilityCatalogItem] = Field(default_factory=list)
    plugin_ids: list[str] = Field(default_factory=list)


class ExpertMutationResponse(BaseModel):
    runtime_agent_profile_id: int
    status: str


class ExpertWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_workspace_id: int


class SubmitExpertReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_workspace_id: int
    share_scope: ExpertShareScope | None = None
    change_summary: str | None = Field(default=None, max_length=2000)


class ExpertReviewSummary(BaseModel):
    review_id: int
    expert_package_id: int
    expert_package_version_id: int
    submitter_user_id: int
    name: str
    description: str | None = None
    avatar_url: str | None = None
    soul_content: str
    system_prompt: str | None = None
    capabilities: ExpertCapabilitySelection
    share_scope: ExpertShareScope
    share_scope_users: list[PersonalKnowledgeSelectedUser] = Field(
        default_factory=list
    )
    change_summary: str | None = None
    status: str
    review_comment: str | None = None
    execution_status: Literal["enabled", "force_disabled"] = "enabled"
    execution_status_reason: str | None = None
    active_installation_count: int = Field(default=0, ge=0)
    created_at: datetime | None = None
    reviewed_at: datetime | None = None
    updated_at: datetime | None = None


class ApproveExpertReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_comment: str | None = Field(default=None, max_length=2000)


class RejectExpertReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_comment: str = Field(min_length=1, max_length=2000)

    @field_validator("review_comment")
    @classmethod
    def normalize_comment(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("review comment is required")
        return normalized


class ExpertExecutionSecurityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason is required")
        return normalized
