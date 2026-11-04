from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuntimeClarifyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=240)
    runtime_session_id: str = Field(min_length=1, max_length=128)
    tool_call_id: str | None = Field(default=None, max_length=200)
    turn_id: str | None = Field(default=None, max_length=200)
    question: str = Field(min_length=1, max_length=4096)
    choices: list[str] = Field(default_factory=list, max_length=4)
    multi_select: bool = False
    # Set by the clarify-lifecycle plugin when a native Kanban expert worker
    # (rather than the interactive Web turn) is asking the question.
    runtime_agent_profile_id: int | None = Field(default=None, ge=1)


class RuntimeClarifyCreateResponse(BaseModel):
    enabled: bool
    request_id: str
    duplicate: bool = False
    expires_at: datetime | None = None
    auto_default_at: datetime | None = None
    mode: str = "web_run"


class RuntimeClarifyAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["text", "single", "multi", "skip"]
    text: str | None = Field(default=None, max_length=4096)
    values: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_shape(self) -> "RuntimeClarifyAnswer":
        if self.kind == "skip":
            if self.text or self.values:
                raise ValueError("skip must not carry text or values")
        elif self.kind == "multi":
            if not self.values:
                raise ValueError("multi answer requires values")
        elif not (self.text or self.values):
            raise ValueError("answer is required")
        return self


class RuntimeClarifyAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: RuntimeClarifyAnswer
    version: int = Field(ge=1)


class RuntimeClarifyRequestView(BaseModel):
    request_id: str
    runtime_session_id: str
    parent_runtime_run_id: str
    question: str
    choices: list[str] = Field(default_factory=list)
    multi_select: bool
    status: str
    answer: RuntimeClarifyAnswer | None = None
    answer_source: str | None = None
    auto_default_at: datetime | None = None
    expires_at: datetime | None = None
    version: int
    created_at: datetime
    answered_at: datetime | None = None
    canceled_at: datetime | None = None
    resume_runtime_run_id: str | None = None
    claim_expires_at: datetime | None = None
    resume_attempt_count: int = 0
    next_resume_at: datetime | None = None
    mode: str = "web_run"
    requested_by: str | None = None
    expert_profile_id: int | None = None


class RuntimeClarifyRequestListResponse(BaseModel):
    items: list[RuntimeClarifyRequestView]


class RuntimeClarifyStatusResponse(BaseModel):
    """Minimal polling view for the runtime-side clarify plugin."""

    request_id: str
    status: str
    answer: RuntimeClarifyAnswer | None = None
    auto_default_at: datetime | None = None
