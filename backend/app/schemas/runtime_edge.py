from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.model_runtime_capabilities import RuntimeModelCapabilities
from app.schemas.provider_routes import ProviderRouteGrantV1


RUNTIME_EDGE_SCHEMA_VERSION = 1
RUNTIME_EDGE_WEBSOCKET_PROTOCOL = "hermes-runtime-v1"


class StrictRuntimeEdgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeAccessClaims(StrictRuntimeEdgeModel):
    token_type: Literal["runtime_access"] = "runtime_access"
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    sub: str = Field(min_length=1, max_length=128)
    user_id: int
    org_id: int
    runtime_id: str = Field(min_length=1, max_length=128)
    admission_generation: int = Field(gt=0)
    allowed_origin: str = Field(min_length=1, max_length=512)
    connection_nonce: str = Field(min_length=16, max_length=256)
    issued_at: int = Field(ge=0)
    expires_at: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_lifetime(self) -> "RuntimeAccessClaims":
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        if self.expires_at - self.issued_at > 300:
            raise ValueError("RuntimeAccess lifetime cannot exceed 300 seconds")
        return self


class GrantedAttachment(StrictRuntimeEdgeModel):
    file_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    relative_path: str = Field(min_length=1, max_length=1024)
    mime_type: str | None = Field(default=None, max_length=255)


class SessionGrantClaims(StrictRuntimeEdgeModel):
    token_type: Literal["session_grant"] = "session_grant"
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    user_id: int
    org_id: int
    runtime_id: str = Field(min_length=1, max_length=128)
    admission_generation: int = Field(gt=0)
    session_id: str = Field(min_length=1, max_length=256)
    profile_id: str = Field(min_length=1, max_length=128)
    runtime_agent_profile_id: int = Field(ge=0)
    application_agent_id: int | None = Field(default=None, ge=1)
    model_id: str = Field(min_length=1, max_length=128)
    upstream_model_id: str = Field(min_length=1, max_length=200)
    context_length: int = Field(default=128_000, ge=1, le=10_000_000)
    runtime_capabilities: RuntimeModelCapabilities = Field(
        default_factory=RuntimeModelCapabilities
    )
    provider_route: ProviderRouteGrantV1 | None = None
    config_revision: str = Field(min_length=1, max_length=128)
    profile_desired_revision: int = Field(ge=0)
    profile_applied_revision: int = Field(ge=0)
    profile_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    skill_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    mcp_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    knowledge_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    max_prompt_bytes: int = Field(gt=0, le=16 * 1024 * 1024)
    max_output_tokens: int = Field(gt=0, le=1_000_000)
    attachments: list[GrantedAttachment] = Field(
        default_factory=list,
        max_length=20,
    )
    issued_at: int = Field(ge=0)
    expires_at: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_lifetime(self) -> "SessionGrantClaims":
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        if self.expires_at - self.issued_at > 1800:
            raise ValueError("SessionGrant lifetime cannot exceed 1800 seconds")
        if self.profile_applied_revision > self.profile_desired_revision:
            raise ValueError(
                "profile_applied_revision cannot exceed profile_desired_revision"
            )
        if self.application_agent_id is not None and self.runtime_agent_profile_id != 0:
            raise ValueError("application Agent grant cannot carry a Profile id")
        return self


class FileTransferTicketClaims(StrictRuntimeEdgeModel):
    token_type: Literal["file_transfer"] = "file_transfer"
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    ticket_id: str = Field(min_length=16, max_length=128)
    operation: Literal["upload", "download"]
    user_id: int
    org_id: int
    runtime_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)
    file_id: int = Field(gt=0)
    relative_path: str = Field(min_length=1, max_length=1024)
    display_name: str = Field(min_length=1, max_length=255)
    mime_type: str | None = Field(default=None, max_length=255)
    max_bytes: int = Field(gt=0, le=100 * 1024 * 1024)
    issued_at: int = Field(ge=0)
    expires_at: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_lifetime(self) -> "FileTransferTicketClaims":
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        if self.expires_at - self.issued_at > 300:
            raise ValueError(
                "FileTransferTicket lifetime cannot exceed 300 seconds"
            )
        return self


class RuntimeEdgeGrantRequest(StrictRuntimeEdgeModel):
    runtime_workspace_id: int = Field(gt=0)
    runtime_session_id: str = Field(min_length=1, max_length=256)
    model_entry_id: int | None = Field(default=None, gt=0)
    attachment_file_ids: list[int] = Field(default_factory=list, max_length=20)


class RuntimeEdgeSessionGrantResponse(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    runtime_id: str = Field(min_length=1, max_length=128)
    runtime_session_id: str = Field(min_length=1, max_length=256)
    model_entry_id: int = Field(gt=0)
    profile_id: str = Field(min_length=1, max_length=128)
    runtime_agent_profile_id: int = Field(ge=0)
    application_agent_id: int | None = Field(default=None, ge=1)
    config_revision: str = Field(min_length=1, max_length=128)
    profile_desired_revision: int = Field(ge=0)
    profile_applied_revision: int = Field(ge=0)
    profile_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    skill_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    mcp_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    knowledge_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    session_grant: str = Field(min_length=32, max_length=8192)
    session_grant_expires_at: datetime


class RuntimeEdgeBootstrapResponse(RuntimeEdgeSessionGrantResponse):
    runtime_handle: str = Field(min_length=1, max_length=128)
    websocket_path: str = Field(min_length=1, max_length=512)
    websocket_protocol: Literal["hermes-runtime-v1"] = (
        RUNTIME_EDGE_WEBSOCKET_PROTOCOL
    )
    runtime_access: str = Field(min_length=32, max_length=8192)
    runtime_access_expires_at: datetime


class RuntimeEdgePrepareRequest(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["session.prepare"] = "session.prepare"
    request_id: str = Field(min_length=8, max_length=128)
    session_grant: str = Field(min_length=32, max_length=8192)
    local_capabilities: bool = False


class RuntimeEdgeDesktopCapabilitiesRequest(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["desktop.capabilities"] = "desktop.capabilities"
    request_id: str = Field(min_length=8, max_length=128)
    local_capabilities: bool = False


class RuntimeEdgeRunSubmitRequest(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.submit"] = "run.submit"
    client_message_id: str = Field(min_length=8, max_length=128)
    session_grant: str = Field(min_length=32, max_length=8192)
    prompt: str
    attachment_file_ids: list[str] = Field(default_factory=list, max_length=20)


class RuntimeEdgeRunResumeRequest(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.resume"] = "run.resume"
    run_id: str = Field(min_length=8, max_length=128)
    after_seq: int = Field(default=0, ge=0)


class RuntimeEdgeRunCancelRequest(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.cancel"] = "run.cancel"
    run_id: str = Field(min_length=8, max_length=128)


class RuntimeEdgeLocalCapabilityRequest(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["local.capability.request"] = "local.capability.request"
    request_id: str = Field(min_length=8, max_length=128)
    run_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)
    capability: Literal[
        "workspace.list",
        "workspace.read",
        "workspace.write",
        "workspace.mkdir",
        "workspace.trash",
        "command.run",
        "browser.open",
        "browser.session_start",
        "browser.navigate",
        "browser.snapshot",
        "browser.click",
        "browser.type",
        "browser.press",
        "browser.scroll",
        "browser.screenshot",
        "browser.session_close",
        "browser.read",
        "browser.wait",
        "browser.hover",
        "browser.select",
        "browser.upload",
        "browser.downloads",
        "browser.back",
        "browser.reload",
        "browser.tab_list",
        "browser.tab_new",
        "browser.tab_select",
        "browser.tab_close",
    ]
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=512)


class RuntimeEdgeLocalCapabilityResult(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["local.capability.result"] = "local.capability.result"
    request_id: str = Field(min_length=8, max_length=128)
    run_id: str = Field(min_length=8, max_length=128)
    capability: Literal[
        "workspace.list",
        "workspace.read",
        "workspace.write",
        "workspace.mkdir",
        "workspace.trash",
        "command.run",
        "browser.open",
        "browser.session_start",
        "browser.navigate",
        "browser.snapshot",
        "browser.click",
        "browser.type",
        "browser.press",
        "browser.scroll",
        "browser.screenshot",
        "browser.session_close",
        "browser.read",
        "browser.wait",
        "browser.hover",
        "browser.select",
        "browser.upload",
        "browser.downloads",
        "browser.back",
        "browser.reload",
        "browser.tab_list",
        "browser.tab_new",
        "browser.tab_select",
        "browser.tab_close",
    ]
    ok: bool
    value: Any | None = None
    error: str | None = Field(default=None, max_length=512)
    canceled: bool = False


class RuntimeEdgeRunCancelAck(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.cancel_ack"] = "run.cancel_ack"
    run_id: str = Field(min_length=8, max_length=128)
    status: Literal["accepted", "already_terminal", "failed"]


class RuntimeEdgePrepareReady(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["session.ready"] = "session.ready"
    request_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)
    config_revision: str = Field(min_length=1, max_length=128)


class RuntimeEdgeRunAccepted(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.accepted"] = "run.accepted"
    run_id: str = Field(min_length=8, max_length=128)
    client_message_id: str = Field(min_length=8, max_length=128)
    accepted_at: datetime


class RuntimeEdgeRunEvent(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.event"] = "run.event"
    run_id: str = Field(min_length=8, max_length=128)
    seq: int = Field(gt=0)
    event_type: str = Field(min_length=1, max_length=128)
    emitted_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class RuntimeEdgeRunEventBatch(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.event_batch"] = "run.event_batch"
    events: list[RuntimeEdgeRunEvent] = Field(min_length=1)


class RuntimeEdgeRunSnapshot(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.snapshot"] = "run.snapshot"
    run_id: str = Field(min_length=8, max_length=128)
    through_seq: int = Field(ge=0)
    status: str = Field(min_length=1, max_length=64)
    answer: str = ""
    events: list[RuntimeEdgeRunEvent] = Field(default_factory=list)


class RuntimeEdgeError(StrictRuntimeEdgeModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["error"] = "error"
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=512)
    retryable: bool = False
    request_id: str | None = Field(default=None, min_length=8, max_length=128)
    client_message_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
    )
    run_id: str | None = Field(default=None, min_length=8, max_length=128)
    phase: Literal[
        "live_execution_release",
        "model_route",
        "session_import",
        "dashboard_prewarm",
        "profile_prompt_refresh",
        "unknown",
    ] | None = None


RuntimeEdgeClientMessage = (
    RuntimeEdgePrepareRequest
    | RuntimeEdgeRunSubmitRequest
    | RuntimeEdgeRunResumeRequest
    | RuntimeEdgeRunCancelRequest
    | RuntimeEdgeLocalCapabilityResult
)

RuntimeEdgeServerMessage = (
    RuntimeEdgePrepareReady
    | RuntimeEdgeRunAccepted
    | RuntimeEdgeRunCancelAck
    | RuntimeEdgeRunEvent
    | RuntimeEdgeRunEventBatch
    | RuntimeEdgeRunSnapshot
    | RuntimeEdgeLocalCapabilityRequest
    | RuntimeEdgeError
)
