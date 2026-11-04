from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictAssetRolloutModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssetRolloutCommand(StrictAssetRolloutModel):
    rollout_id: str = Field(min_length=8, max_length=64)
    org_id: int = Field(gt=0)
    asset_type: Literal["skill", "mcp", "expert"]
    asset_id: int = Field(gt=0)
    action: Literal["publish", "rollback", "revoke", "enable", "delete"]
    source_version_id: int | None = Field(default=None, gt=0)
    target_version_id: int | None = Field(default=None, gt=0)
    created_by_user_id: int = Field(gt=0)
    change_summary: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_versions(self) -> "AssetRolloutCommand":
        if self.action in {"publish", "rollback"} and self.target_version_id is None:
            raise ValueError("publish and rollback require target_version_id")
        if self.action in {"revoke", "enable", "delete"} and self.target_version_id is not None:
            raise ValueError("execution control and delete must not supply target_version_id")
        return self


class AssetRolloutResult(StrictAssetRolloutModel):
    rollout_id: str
    affected_user_count: int = Field(ge=0)
    affected_profile_count: int = Field(ge=0)
    status: Literal["waiting", "updating", "completed", "partial_failed", "failed"]
    created: bool


class AssetRolloutBatchSummary(StrictAssetRolloutModel):
    rollout_id: str
    asset_type: Literal["skill", "mcp", "expert"]
    asset_id: int
    action: Literal["publish", "rollback", "revoke", "enable", "delete"]
    source_version_id: int | None
    target_version_id: int | None
    affected_user_count: int
    affected_profile_count: int
    waiting_count: int
    updating_count: int
    applied_count: int
    failed_count: int
    superseded_count: int
    revoked_count: int
    status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class AssetRolloutProfileProgress(StrictAssetRolloutModel):
    target_id: int
    user_id: int
    runtime_workspace_id: int
    runtime_agent_profile_id: int
    profile_kind: str
    profile_name: str
    target_revision: int
    applied_revision: int
    status: str
    attempt_count: int
    error_code: str | None
    error_message: str | None


class AssetRolloutUserProgress(StrictAssetRolloutModel):
    user_id: int
    user_code: str | None
    user_name: str | None
    status: Literal["success", "updating", "partial_failed", "failed"]
    profiles: list[AssetRolloutProfileProgress]


class AssetRolloutDetail(StrictAssetRolloutModel):
    batch: AssetRolloutBatchSummary
    total_users: int
    offset: int
    limit: int
    users: list[AssetRolloutUserProgress]


class AssetRolloutRetryResult(StrictAssetRolloutModel):
    rollout_id: str
    queued_target_count: int = Field(ge=0)
