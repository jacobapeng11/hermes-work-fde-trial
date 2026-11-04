from __future__ import annotations

from datetime import datetime

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RuntimeSessionLatestRunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    runtime_run_id: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RuntimeSessionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: int
    runtime_workspace_id: int
    runtime_agent_profile_id: int | None = None
    runtime_session_id: str
    title: str | None = None
    source_type: str = "web"
    message_count: int = 0
    session_model_entry_id: int | None = None
    status: str
    last_active_at: datetime | None = None
    agent_display_name: str | None = None
    agent_avatar_url: str | None = None
    agent_profile_kind: str | None = None
    is_read_only: bool = False
    is_immutable: bool = False
    origin_label: str | None = None
    unavailable_reason: Literal["expert_deleted"] | None = None
    scheduled_task_eligible: bool = True
    latest_run: RuntimeSessionLatestRunSummary | None = None
    context_source_runtime_session_id: str | None = None
    context_head_runtime_session_id: str | None = None


class CreateRuntimeSessionRequest(BaseModel):
    title: str | None = None
    model_entry_id: int | None = None
    source_type: str = "web"
    runtime_agent_profile_id: int | None = None
    context_source_runtime_session_id: str | None = None


class RenameRuntimeSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return " ".join(value.split())


class SessionModelSelectionRequest(BaseModel):
    model_entry_id: int


class SessionModelSelectionResponse(BaseModel):
    runtime_workspace_id: int
    runtime_session_id: str
    model_entry_id: int
    applies_to: str
    active_run_count: int


class RuntimeChatRequest(BaseModel):
    # Legacy enabled_* fields are intentionally not part of the API contract.
    # Pydantic ignores them when older clients send them, but they never reach
    # RuntimeChatRequestIntent and therefore cannot select Run-level assets.
    model_config = ConfigDict(extra="ignore")

    runtime_workspace_id: int
    runtime_session_id: str | None = None
    model_entry_id: int | None = None
    message: str
    source_type: str = "web"
    attachment_file_ids: list[int] = Field(default_factory=list)
    ready_receipt_id: str | None = Field(default=None, min_length=20, max_length=256)
    runtime_run_id: str | None = Field(default=None, min_length=8, max_length=128)


class RuntimeChatRunSummary(BaseModel):
    runtime_workspace_id: int
    runtime_session_id: str
    runtime_run_id: str
    model_entry_id: int | None = None
    status: str


class RuntimeChatSource(BaseModel):
    """Immutable origin metadata projected from a scheduled RuntimeRun."""

    type: str
    scheduled_task_id: int
    scheduled_task_name: str
    trigger_kind: str
    task_available: bool


class RuntimeRunActionRequest(BaseModel):
    runtime_run_id: str
    after_seq: int = 0


class RuntimeChatHistoryResponse(BaseModel):
    runtime_workspace_id: int
    runtime_session_id: str
    source: Literal["hermes", "mysql"] = "hermes"
    consistency: Literal["authoritative", "projection_preview"]
    generated_at: datetime
    has_active_run: bool
    messages: list[dict]


class RuntimeRunActivityEvent(BaseModel):
    event: str
    data: dict


class RuntimeRunActivityResponse(BaseModel):
    runtime_workspace_id: int
    runtime_session_id: str
    runtime_run_id: str
    status: str
    events: list[RuntimeRunActivityEvent]


class RuntimeChatClientTimingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_kind: Literal["lifecycle", "content"] = "content"
    request_to_first_event_ms: float | None = Field(default=None, ge=0, le=3_600_000)
    request_to_first_visible_received_ms: float = Field(ge=0, le=3_600_000)
    request_to_first_visible_commit_ms: float = Field(ge=0, le=3_600_000)
    request_to_first_visible_paint_ms: float = Field(ge=0, le=3_600_000)
    dispatch_to_first_visible_received_ms: float | None = Field(
        default=None, ge=0, le=3_600_000
    )
    dispatch_to_first_visible_commit_ms: float | None = Field(
        default=None, ge=0, le=3_600_000
    )
    dispatch_to_first_visible_paint_ms: float | None = Field(
        default=None, ge=0, le=3_600_000
    )
    queued_wait_ms: float = Field(default=0, ge=0, le=3_600_000)
    receipt_age_ms: float | None = Field(default=None, ge=0, le=3_600_000)
    document_visibility: Literal["visible", "hidden", "prerender", "unknown"]
    reconnect_before_first_visible: bool = False
    send_overlapped_prewarm: bool = False
    required_prewarm_wait_ms: float | None = Field(
        default=None,
        ge=0,
        le=3_600_000,
    )
    frontend_build: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_timing_order(self) -> "RuntimeChatClientTimingRequest":
        points = [
            value
            for value in (
                self.request_to_first_event_ms,
                self.request_to_first_visible_received_ms,
                self.request_to_first_visible_commit_ms,
                self.request_to_first_visible_paint_ms,
            )
            if value is not None
        ]
        if points != sorted(points):
            raise ValueError("client timing milestones must be monotonic")
        dispatch_points = [
            value
            for value in (
                self.dispatch_to_first_visible_received_ms,
                self.dispatch_to_first_visible_commit_ms,
                self.dispatch_to_first_visible_paint_ms,
            )
            if value is not None
        ]
        if dispatch_points != sorted(dispatch_points):
            raise ValueError(
                "dispatch timing milestones must be monotonic"
            )
        return self


class ChatFileSummary(BaseModel):
    file_id: int
    runtime_workspace_id: int
    runtime_session_id: str | None = None
    runtime_run_id: str | None = None
    relative_path: str
    display_name: str
    mime_type: str | None = None
    size_bytes: int
    source_type: str
    created_at: datetime | None = None


class ChatUploadResponse(BaseModel):
    file: ChatFileSummary


class ChatFileTransferIntent(BaseModel):
    file: ChatFileSummary
    runtime_path: str
    file_transfer_ticket: str
    expires_at: datetime


class ChatUploadIntentRequest(BaseModel):
    runtime_workspace_id: int
    runtime_session_id: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=255)
    mime_type: str | None = Field(default=None, max_length=255)
    size_bytes: int = Field(gt=0, le=100 * 1024 * 1024)


class ChatDownloadIntent(BaseModel):
    runtime_path: str
    file_transfer_ticket: str
    expires_at: datetime
    display_name: str
    mime_type: str | None = None


class OfficePreviewSession(BaseModel):
    document_server_url: str
    config: dict[str, object]


class RegisterChatFileRequest(BaseModel):
    runtime_workspace_id: int
    runtime_session_id: str | None = None
    runtime_run_id: str | None = None
    relative_path: str
    mime_type: str | None = None
    size_bytes: int = 0
    source_type: str


class RenameChatFileRequest(BaseModel):
    relative_path: str
