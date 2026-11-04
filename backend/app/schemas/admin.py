from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.model_runtime_capabilities import RuntimeModelCapabilities


class AdminPrincipalSummary(BaseModel):
    org_id: int
    user_id: int
    roles: list[str]


class AdminModelSummary(BaseModel):
    model_entry_id: int
    model_key: str
    display_name: str
    provider_id: int
    provider_display_name: str
    status: str
    is_default: bool
    is_restricted: bool
    context_length: int | None = None
    tags: list[str] = []


class AdminModelStatusRequest(BaseModel):
    status: str


class AdminModelVisibilityScope(BaseModel):
    organization: bool = True
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
        return sorted({item.strip() for item in value})

    @model_validator(mode="after")
    def require_non_empty_custom_scope(self) -> "AdminModelVisibilityScope":
        if self.organization and (self.user_ids or self.department_ids):
            raise ValueError("organization scope must not include users or departments")
        if not self.organization and not (self.user_ids or self.department_ids):
            raise ValueError("custom scope requires at least one user or department")
        return self


class AdminModelVisibilityUserSummary(BaseModel):
    id: int
    name: str
    user_code: str | None = None
    status: str


class AdminModelConfigEntryRequest(BaseModel):
    provider_name: str
    provider_display_name: str
    provider_type: str = "openai_compatible"
    api_endpoint: str
    provider_credential: str | None = None
    model_key: str
    model_display_name: str
    context_length: int | None = None
    tags: list[str] = []
    is_restricted: bool = False
    visibility_scope: AdminModelVisibilityScope | None = None
    model_status: str = "active"
    is_default: bool = False
    runtime_capabilities: RuntimeModelCapabilities = RuntimeModelCapabilities()


class AdminModelCatalogPublishRequest(BaseModel):
    entries: list[AdminModelConfigEntryRequest]


class AdminModelCatalogPublishResponse(BaseModel):
    release_id: int
    catalog_version: int
    previous_catalog_version: int | None = None
    config_hash: str
    status: str
    published_at: datetime


class AdminModelConfigEntrySummary(BaseModel):
    provider_id: int
    provider_name: str
    provider_display_name: str
    provider_type: str
    api_endpoint: str | None = None
    credential_configured: bool
    provider_status: str
    provider_catalog_version: int
    model_entry_id: int
    model_key: str
    model_display_name: str
    context_length: int | None = None
    tags: list[str] = []
    is_restricted: bool
    visibility_scope: AdminModelVisibilityScope
    visibility_users: list[AdminModelVisibilityUserSummary] = []
    is_default: bool
    model_status: str
    model_catalog_version: int
    runtime_capabilities: RuntimeModelCapabilities = RuntimeModelCapabilities()


class AdminWeKnoraRerankModelSummary(BaseModel):
    id: str
    name: str
    display_name: str
    source: str = ""
    status: str


class AdminWeKnoraRerankConfigResponse(BaseModel):
    provider: Literal["weknora"]
    mode: Literal["model", "disabled"]
    configured_model_id: str
    effective_model_id: str
    config_source: Literal["tenant", "auto", "none", "disabled"]
    models: list[AdminWeKnoraRerankModelSummary] = []


class AdminWeKnoraRerankConfigUpdateRequest(BaseModel):
    mode: Literal["model", "disabled"] = "model"
    rerank_model_id: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_mode(self) -> "AdminWeKnoraRerankConfigUpdateRequest":
        model_id = str(self.rerank_model_id or "").strip()
        if self.mode == "model" and not model_id:
            raise ValueError("rerank_model_id is required when mode is model")
        if self.mode == "disabled" and model_id:
            raise ValueError("rerank_model_id must be empty when mode is disabled")
        return self


class AdminModelCatalogReleaseSummary(BaseModel):
    release_id: int
    catalog_version: int | None = None
    previous_catalog_version: int | None = None
    config_hash: str | None = None
    status: str
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AdminModelConfigResponse(BaseModel):
    entries: list[AdminModelConfigEntrySummary]
    latest_release: AdminModelCatalogReleaseSummary | None = None


class AdminModelUpdateResponse(BaseModel):
    model_entry_id: int
    status: str
    is_default: bool
    upgrade_log_id: int | None = None
    affected_runtime_workspace_count: int


class AdminRuntimeWorkspaceResourceSummary(BaseModel):
    runtime_workspace_id: int
    cloud_profile_id: int | None = None
    resource_key: str
    runtime_kind: Literal["personal", "application"]
    application_agent_id: int | None = None
    org_id: int
    user_id: int
    employee_no: str | None = None
    user_name: str
    runtime_name: str
    profile_status: str
    binding_status: str | None = None
    runtime_status: str | None = None
    default_model_entry_id: int | None = None
    default_model_display_name: str | None = None
    session_count: int
    run_count: int
    active_run_count: int
    installed_expert_count: int
    installed_skill_count: int
    installed_mcp_count: int
    installed_knowledge_base_count: int
    pending_reload: bool
    has_runtime_binding: bool
    kservice_name: str | None = None
    latest_error: str | None = None


class AdminAuditLogSummary(BaseModel):
    audit_log_id: int
    org_id: int
    actor_user_id: int | None = None
    runtime_workspace_id: int | None = None
    action: str
    target_type: str
    target_id: str
    detail: dict | None = None
    created_at: datetime | None = None


class AdminRuntimeAdmissionAuditSummary(BaseModel):
    event_id: str
    runtime_id: str
    runtime_workspace_id: int | None = None
    user_id: int | None = None
    session_id: str | None = None
    request_id: str | None = None
    client_message_id: str | None = None
    run_id: str | None = None
    operation: str
    status: str
    outcome: str
    stage: str
    error_code: str | None = None
    exception_type: str | None = None
    profile_id: str | None = None
    model_id: str | None = None
    config_revision: str | None = None
    attachment_count: int | None = None
    prompt_bytes: int | None = None
    upstream_status: int | None = None
    upstream_path: str | None = None
    occurred_at: datetime
    reported_at: datetime


class AdminUsageOverviewSummary(BaseModel):
    run_count: int
    active_run_count: int
    model_call_count: int
    audit_log_count: int
    error_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    average_latency_ms: int


class AdminPresenceUserGroup(BaseModel):
    count: int
    users: list[str]


class AdminPresenceSummaryResponse(BaseModel):
    web_online: AdminPresenceUserGroup
    desktop_online: AdminPresenceUserGroup
    daily_logins: AdminPresenceUserGroup
    window_seconds: int
    daily_login_since: datetime
    observed_at: datetime
    source: str


class AdminRuntimeWorkspaceUsageSummary(BaseModel):
    runtime_workspace_id: int
    user_id: int | None = None
    run_count: int
    active_run_count: int
    model_call_count: int
    error_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    average_latency_ms: int
    last_error: str | None = None


class AdminModelUsageSummary(BaseModel):
    model_entry_id: int | None = None
    model_display_name: str | None = None
    provider_id: int | None = None
    model_call_count: int
    error_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    average_latency_ms: int


class AdminUsageRecentErrorSummary(BaseModel):
    source: str
    runtime_workspace_id: int | None = None
    runtime_run_id: str | None = None
    model_entry_id: int | None = None
    action: str | None = None
    error_code: str | None = None
    message: str | None = None
    created_at: datetime | None = None


class AdminUsageSummaryResponse(BaseModel):
    overview: AdminUsageOverviewSummary
    runtime_workspaces: list[AdminRuntimeWorkspaceUsageSummary]
    models: list[AdminModelUsageSummary]
    recent_errors: list[AdminUsageRecentErrorSummary]
