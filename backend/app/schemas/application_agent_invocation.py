from __future__ import annotations

from typing import Any, Literal
from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class ApplicationAgentResponseRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    input: str = Field(min_length=1, max_length=200_000)
    conversation_key: str | None = Field(default=None, min_length=1, max_length=255)
    external_user_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    request_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def _reject_attachment_file_ids(self) -> "ApplicationAgentResponseRequest":
        if "attachment_file_ids" in (self.__pydantic_extra__ or {}):
            raise ValueError("attachment_file_ids is not supported")
        return self

    @field_validator("external_user_id")
    @classmethod
    def _nonblank_external_user_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("external_user_id must not be blank")
        return normalized


class ApplicationAgentResponseIdentity(BaseModel):
    type: Literal["user", "external_user", "service"]
    key: str


class ApplicationAgentProducedFile(BaseModel):
    file_id: int
    display_name: str
    mime_type: str | None = None
    size_bytes: int
    download_url: str


class ApplicationAgentFileSummary(BaseModel):
    file_id: int
    display_name: str
    mime_type: str | None = None
    size_bytes: int
    source_type: str
    runtime_run_id: str
    created_at: datetime | None = None
    download_url: str


class ApplicationAgentResponse(BaseModel):
    response_id: str
    request_id: str
    application_agent: str
    conversation_key: str | None
    runtime_session_id: str
    identity: ApplicationAgentResponseIdentity
    output_text: str
    status: Literal["completed"]
    produced_files: list[ApplicationAgentProducedFile] | None = None


class ApplicationAgentStudioResponseRequest(BaseModel):
    input: str = Field(min_length=1, max_length=200_000)
    runtime_session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )


class ApplicationAgentStudioResponse(BaseModel):
    response_id: str
    request_id: str
    runtime_session_id: str
    output_text: str
    status: Literal["completed"]


class ApplicationAgentStudioHistory(BaseModel):
    runtime_session_id: str
    messages: list[dict[str, Any]]
