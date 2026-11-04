from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.experts import ExpertShareScope


AppShareScope = ExpertShareScope


class AppCenterCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=20)
    html_content: str = Field(min_length=1, max_length=16_000_000)
    share_mode: Literal["scope", "link"]
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=64)
    scope: AppShareScope | None = None
    source_session_id: str | None = Field(default=None, min_length=1, max_length=64)
    source_run_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("name must not be empty")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("description must not be empty")
        return normalized


class AppCenterUploadMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=20)
    share_mode: Literal["scope", "link"] = "scope"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=64)
    scope: AppShareScope | None = None
    source_session_id: str | None = Field(default=None, min_length=1, max_length=64)
    source_run_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("name must not be empty")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("description must not be empty")
        return normalized


class AppCenterPermissionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: AppShareScope


class AppCenterBrowserLaunchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    launch_url: str
    expires_in: int


class AppCenterPackageUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    html_content: str = Field(min_length=1, max_length=16_000_000)
    # Optional profile edits alongside a content refresh (the preview-panel
    # "update shared app" flow prefills the original name/description).
    name: str | None = Field(default=None, min_length=1, max_length=50)
    description: str | None = Field(default=None, min_length=1, max_length=20)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("name must not be empty")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("description must not be empty")
        return normalized


class AppCenterListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    description: str
    access_url: str
    source_type: Literal["agent_html", "manual_zip"]
    share_mode: Literal["scope", "link"]
    creator_name: str
    mine: bool
    copied_from_app_id: int | None = None
    copied_from_creator_name: str | None = None
    # Lets the preview panel reconnect to an app previously shared from the
    # same chat session after the in-memory share state is lost (reload).
    source_session_id: str | None = None
    updated_at: datetime
    scope_summary: str
    publish_version_id: str
    scope: AppShareScope | None = None
