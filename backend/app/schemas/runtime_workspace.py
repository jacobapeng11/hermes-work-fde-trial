from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RuntimeWorkspaceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    runtime_workspace_id: int
    cloud_profile_id: int
    org_id: int
    user_id: int
    name: str
    status: str
    default_model_id: int | None = None
    runtime_status: str | None = None
    pending_reload: bool = False
    config_generation: int = 0
    updated_at: datetime | None = None


class RuntimeStartupContextResponse(BaseModel):
    runtime_workspace_id: int | None = None
    context: Literal["first_allocation", "environment_recovery", "none"]


class RuntimeRunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: int
    runtime_workspace_id: int
    runtime_run_id: str
    session_id: int | None = None
    model_entry_id: int | None = None
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RuntimeModelSummary(BaseModel):
    model_entry_id: int
    model_key: str
    display_name: str
    provider_id: int
    provider_display_name: str
    context_length: int | None = None
    tags: list[str] | None = None
    is_default: bool = False
    supports_vision: bool = False


class SetDefaultModelRequest(BaseModel):
    model_entry_id: int


class DefaultModelUpdateResponse(BaseModel):
    runtime_workspace_id: int
    model_entry_id: int
    projection_scope: str
    pending_reload: bool


class RuntimeSkillPolicyUpdateRequest(BaseModel):
    policy_version: int = Field(ge=1, le=3)
    expected_config_generation: int = Field(ge=0)


class RuntimeSkillPolicyUpdateResponse(BaseModel):
    runtime_workspace_id: int
    policy_version: int
    config_generation: int
    pending_reload: bool


class RuntimeCapabilitySummary(BaseModel):
    asset_type: str
    asset_id: int
    display_name: str
    status: str
    source_type: str | None = None


class RuntimeCapabilitiesResponse(BaseModel):
    runtime_workspace_id: int
    experts: list[RuntimeCapabilitySummary] = []
    skills: list[RuntimeCapabilitySummary] = []
    mcps: list[RuntimeCapabilitySummary] = []
    knowledge_bases: list[RuntimeCapabilitySummary] = []
