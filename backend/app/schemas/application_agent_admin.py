from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ApplicationAgentAdminCandidateSummary(BaseModel):
    user_id: int | None = None
    user_code: str
    name: str
    department_name: str | None = None
    status: str
    is_application_agent_admin: bool


class ApplicationAgentAdminSummary(BaseModel):
    user_id: int
    user_code: str | None = None
    name: str
    department_name: str | None = None
    granted_by_user_id: int | None = None
    granted_at: datetime | None = None


class ApplicationAgentAdminReplaceRequest(BaseModel):
    user_codes: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("user_codes")
    @classmethod
    def normalize_user_codes(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("user_code_required")
        if any(len(item) > 64 for item in normalized):
            raise ValueError("user_code_too_long")
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate_user_code")
        return normalized
