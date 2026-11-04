from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RUNTIME_EDGE_SCHEMA_VERSION = 1
RUNTIME_INTERNAL_SCHEMA_VERSION = 1
RUNTIME_EDGE_WEBSOCKET_PROTOCOL = "hermes-runtime-v1"
CHANNEL_FAST_LANE_V1_SCHEMA_VERSION = 1
CHANNEL_FAST_LANE_V2_SCHEMA_VERSION = 2
SHA256_PATTERN = r"^[a-f0-9]{64}$"

ReasoningReplay = Literal["not_supported", "optional", "required"]
ThinkingMode = Literal["provider_default", "enabled", "disabled"]
VisionMode = Literal["not_supported", "native"]


def canonical_payload_digest(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeDashboardUpstreamErrorDetail(StrictContractModel):
    code: Literal["RUNTIME_DASHBOARD_UPSTREAM_ERROR"] = (
        "RUNTIME_DASHBOARD_UPSTREAM_ERROR"
    )
    message: Literal["runtime Dashboard request failed"] = (
        "runtime Dashboard request failed"
    )
    upstream_status_code: int | None = Field(default=None, ge=400, le=599)
    upstream_path: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_upstream_pair(self) -> "RuntimeDashboardUpstreamErrorDetail":
        if (self.upstream_status_code is None) != (self.upstream_path is None):
            raise ValueError("upstream status and path must be provided together")
        return self


class RuntimeModelCapabilities(StrictContractModel):
    reasoning_replay: ReasoningReplay = "not_supported"
    thinking_mode: ThinkingMode = "provider_default"
    vision: VisionMode | None = None


class ProviderRouteGrantV1(StrictContractModel):
    version: Literal[1] = 1
    capsule: str = Field(min_length=16, max_length=8192)
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class RuntimeAccessClaims(StrictContractModel):
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


class GrantedAttachment(StrictContractModel):
    file_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    relative_path: str = Field(min_length=1, max_length=1024)
    mime_type: str | None = Field(default=None, max_length=255)


class RuntimeTurnAttachment(StrictContractModel):
    file_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    relative_path: str = Field(min_length=1, max_length=1024)
    mime_type: str | None = Field(default=None, max_length=255)


class SessionGrantClaims(StrictContractModel):
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
    runtime_capabilities: RuntimeModelCapabilities | None = None
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


class FileTransferTicketClaims(StrictContractModel):
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


class RuntimeEdgePrepareRequest(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["session.prepare"] = "session.prepare"
    request_id: str = Field(min_length=8, max_length=128)
    session_grant: str = Field(min_length=32, max_length=8192)
    local_capabilities: bool = False


class RuntimeEdgeDesktopCapabilitiesRequest(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["desktop.capabilities"] = "desktop.capabilities"
    request_id: str = Field(min_length=8, max_length=128)
    local_capabilities: bool = False


class RuntimeEdgeSessionHistoryRequest(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["session.history"] = "session.history"
    request_id: str = Field(min_length=8, max_length=128)
    session_grant: str = Field(min_length=32, max_length=8192)


class RuntimeEdgeSessionQueueRequest(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["session.queue"] = "session.queue"
    request_id: str = Field(min_length=8, max_length=128)
    session_grant: str = Field(min_length=32, max_length=8192)


class RuntimeEdgeRunSubmitRequest(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.submit"] = "run.submit"
    client_message_id: str = Field(min_length=8, max_length=128)
    session_grant: str = Field(min_length=32, max_length=8192)
    prompt: str
    attachment_file_ids: list[str] = Field(default_factory=list, max_length=20)


class RuntimeEdgeFollowupSubmitRequest(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["followup.submit"] = "followup.submit"
    client_message_id: str = Field(min_length=8, max_length=128)
    parent_run_id: str = Field(min_length=8, max_length=128)
    session_grant: str = Field(min_length=32, max_length=8192)
    prompt: str
    attachment_file_ids: list[str] = Field(default_factory=list, max_length=20)


class RuntimeEdgeRunResumeRequest(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.resume"] = "run.resume"
    run_id: str = Field(min_length=8, max_length=128)
    after_seq: int = Field(default=0, ge=0)


class RuntimeEdgeRunCancelRequest(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.cancel"] = "run.cancel"
    run_id: str = Field(min_length=8, max_length=128)


LocalCapabilityName = Literal[
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


class RuntimeEdgeLocalCapabilityRequest(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["local.capability.request"] = "local.capability.request"
    request_id: str = Field(min_length=8, max_length=128)
    run_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)
    capability: LocalCapabilityName
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=512)


class RuntimeEdgeLocalCapabilityResult(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["local.capability.result"] = "local.capability.result"
    request_id: str = Field(min_length=8, max_length=128)
    run_id: str = Field(min_length=8, max_length=128)
    capability: LocalCapabilityName
    ok: bool
    value: Any | None = None
    error: str | None = Field(default=None, max_length=512)
    canceled: bool = False


class RuntimeLocalCapabilityBridgeRequest(StrictContractModel):
    """Trusted child-to-Supervisor request; the run id may be inferred by session."""

    request_id: str = Field(min_length=8, max_length=128)
    run_id: str | None = Field(default=None, min_length=8, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)
    capability: LocalCapabilityName
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=512)


class RuntimeEdgeRunCancelAck(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.cancel_ack"] = "run.cancel_ack"
    run_id: str = Field(min_length=8, max_length=128)
    status: Literal["accepted", "already_terminal", "failed"]


class RuntimeEdgePrepareReady(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["session.ready"] = "session.ready"
    request_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)
    config_revision: str = Field(min_length=1, max_length=128)


class RuntimeEdgeSessionHistory(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["session.history"] = "session.history"
    request_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)
    messages: list[dict[str, Any]] = Field(default_factory=list)


class RuntimeEdgeQueuedRun(StrictContractModel):
    run_id: str = Field(min_length=8, max_length=128)
    client_message_id: str = Field(min_length=8, max_length=128)
    enqueue_seq: int = Field(gt=0)
    prompt: str = Field(max_length=32768)
    attachment_file_ids: list[str] = Field(default_factory=list, max_length=20)


class RuntimeEdgeActiveRun(StrictContractModel):
    run_id: str = Field(min_length=8, max_length=128)
    state: str = Field(min_length=1, max_length=128)
    cancellation_requested_at: datetime | None = None


class RuntimeEdgeSessionQueue(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["session.queue"] = "session.queue"
    request_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)
    queued_count: int = Field(ge=0)
    scheduled_queued_count: int = Field(ge=0)
    queued_runs: list[RuntimeEdgeQueuedRun] = Field(default_factory=list)
    active_run: RuntimeEdgeActiveRun | None = None


class RuntimeEdgeSessionQueueUpdated(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["session.queue.updated"] = "session.queue.updated"
    session_id: str = Field(min_length=1, max_length=256)
    queued_count: int = Field(ge=0)
    scheduled_queued_count: int = Field(ge=0)


class RuntimeEdgeRunQueued(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.queued"] = "run.queued"
    run_id: str = Field(min_length=8, max_length=128)
    client_message_id: str = Field(min_length=8, max_length=128)
    enqueued_at: datetime
    enqueue_seq: int = Field(gt=0)


class RuntimeEdgeRunAccepted(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.accepted"] = "run.accepted"
    run_id: str = Field(min_length=8, max_length=128)
    client_message_id: str = Field(min_length=8, max_length=128)
    accepted_at: datetime


class RuntimeEdgeRedirectDispositionBase(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    redirect_id: str = Field(min_length=8, max_length=128)
    client_message_id: str = Field(min_length=8, max_length=128)
    parent_run_id: str = Field(min_length=8, max_length=128)
    input_admission_seq: int = Field(gt=0)
    dispatch_group_id: str | None = Field(
        min_length=8,
        max_length=128,
    )
    dispatch_group_position: int | None = Field(ge=0)
    event_seq: int = Field(gt=0)
    start_after_seq: int | None = Field(gt=0)

    @model_validator(mode="after")
    def validate_dispatch_group_pair(
        self,
    ) -> "RuntimeEdgeRedirectDispositionBase":
        if (self.dispatch_group_id is None) != (
            self.dispatch_group_position is None
        ):
            raise ValueError("redirect dispatch group identity is incomplete")
        return self


class RuntimeEdgeRunRedirectPending(RuntimeEdgeRedirectDispositionBase):
    type: Literal["run.redirect_pending"] = "run.redirect_pending"
    dispatch_group_id: Literal[None]
    dispatch_group_position: Literal[None]
    start_after_seq: Literal[None]


class RuntimeEdgeRunRedirectAccepted(RuntimeEdgeRedirectDispositionBase):
    type: Literal["run.redirect_accepted"] = "run.redirect_accepted"
    dispatch_group_id: str = Field(min_length=8, max_length=128)
    dispatch_group_position: int = Field(ge=0)
    start_after_seq: int = Field(gt=0)


class RuntimeEdgeRunRedirectConsumed(RuntimeEdgeRedirectDispositionBase):
    type: Literal["run.redirect_consumed"] = "run.redirect_consumed"
    dispatch_group_id: str = Field(min_length=8, max_length=128)
    dispatch_group_position: int = Field(ge=0)
    start_after_seq: int = Field(gt=0)


class RuntimeEdgeRunRedirectPromoted(RuntimeEdgeRedirectDispositionBase):
    type: Literal["run.redirect_promoted"] = "run.redirect_promoted"
    start_after_seq: Literal[None]
    promoted_run_id: str = Field(min_length=8, max_length=128)
    promoted_enqueue_seq: int = Field(gt=0)


class RuntimeEdgeRunRedirectCanceled(RuntimeEdgeRedirectDispositionBase):
    type: Literal["run.redirect_canceled"] = "run.redirect_canceled"
    start_after_seq: Literal[None]
    cancel_reason: Literal["PARENT_RUN_CANCELED"]


class RuntimeEdgeRunSteerSubmissionUncertain(
    RuntimeEdgeRedirectDispositionBase
):
    type: Literal[
        "run.steer_submission_uncertain"
    ] = "run.steer_submission_uncertain"
    dispatch_group_id: str = Field(min_length=8, max_length=128)
    dispatch_group_position: int = Field(ge=0)
    reason_code: Literal["REDIRECT_DELIVERY_UNPROVEN"]


RuntimeEdgeRedirectDisposition = Annotated[
    RuntimeEdgeRunRedirectPending
    | RuntimeEdgeRunRedirectAccepted
    | RuntimeEdgeRunRedirectConsumed
    | RuntimeEdgeRunRedirectPromoted
    | RuntimeEdgeRunRedirectCanceled
    | RuntimeEdgeRunSteerSubmissionUncertain,
    Field(discriminator="type"),
]


class RuntimeEdgeFollowupDispositionBase(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    followup_id: str = Field(min_length=8, max_length=128)
    client_message_id: str = Field(min_length=8, max_length=128)
    parent_run_id: str = Field(min_length=8, max_length=128)
    input_admission_seq: int = Field(gt=0)
    dispatch_group_id: str | None = Field(min_length=8, max_length=128)
    dispatch_group_position: int | None = Field(ge=0)
    event_seq: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_followup_group_pair(
        self,
    ) -> "RuntimeEdgeFollowupDispositionBase":
        if (self.dispatch_group_id is None) != (
            self.dispatch_group_position is None
        ):
            raise ValueError("follow-up dispatch group identity is incomplete")
        return self


class RuntimeEdgeFollowupReceived(RuntimeEdgeFollowupDispositionBase):
    type: Literal["followup.received"] = "followup.received"
    dispatch_group_id: Literal[None]
    dispatch_group_position: Literal[None]


class RuntimeEdgeFollowupApplying(RuntimeEdgeFollowupDispositionBase):
    type: Literal["followup.applying"] = "followup.applying"


class RuntimeEdgeFollowupApplied(RuntimeEdgeFollowupDispositionBase):
    type: Literal["followup.applied"] = "followup.applied"
    dispatch_group_id: str = Field(min_length=8, max_length=128)
    dispatch_group_position: int = Field(ge=0)
    proof_kind: Literal[
        "correction_user_row",
        "steer_oob_marker",
        "canonical_history",
    ]
    proof_digest: str | None = Field(default=None, min_length=8, max_length=128)


class RuntimeEdgeFollowupContinued(RuntimeEdgeFollowupDispositionBase):
    type: Literal["followup.continued"] = "followup.continued"
    continuation_run_id: str = Field(min_length=8, max_length=128)
    member_client_message_ids: list[str] = Field(min_length=1)


class RuntimeEdgeFollowupFailed(RuntimeEdgeFollowupDispositionBase):
    type: Literal["followup.failed"] = "followup.failed"
    error_code: Literal[
        "FOLLOWUP_DELIVERY_UNRESOLVED",
        "FOLLOWUP_CONTINUATION_FAILED",
        "FOLLOWUP_PARENT_CANCELED",
    ]


RuntimeEdgeFollowupDisposition = Annotated[
    RuntimeEdgeFollowupReceived
    | RuntimeEdgeFollowupApplying
    | RuntimeEdgeFollowupApplied
    | RuntimeEdgeFollowupContinued
    | RuntimeEdgeFollowupFailed,
    Field(discriminator="type"),
]


class RuntimeEdgeRunEvent(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.event"] = "run.event"
    run_id: str = Field(min_length=8, max_length=128)
    seq: int = Field(gt=0)
    event_type: str = Field(min_length=1, max_length=128)
    emitted_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class RuntimeEdgeRunEventBatch(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.event_batch"] = "run.event_batch"
    events: list[RuntimeEdgeRunEvent] = Field(min_length=1)


class RuntimeEdgeRunSnapshot(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_EDGE_SCHEMA_VERSION
    type: Literal["run.snapshot"] = "run.snapshot"
    run_id: str = Field(min_length=8, max_length=128)
    through_seq: int = Field(ge=0)
    status: str = Field(min_length=1, max_length=64)
    answer: str = ""
    events: list[RuntimeEdgeRunEvent] = Field(default_factory=list)


class RuntimeEdgeError(StrictContractModel):
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
    audit_event_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
    )


class RuntimeTurnIdentity(StrictContractModel):
    user_id: int
    org_id: int
    session_id: str = Field(min_length=1, max_length=256)
    profile_id: str = Field(min_length=1, max_length=128)
    runtime_agent_profile_id: int = Field(ge=0)
    application_agent_id: int | None = Field(default=None, ge=1)
    profile_desired_revision: int = Field(ge=0)
    profile_applied_revision: int = Field(ge=0)
    profile_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    skill_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    mcp_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    knowledge_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    model_id: str = Field(min_length=1, max_length=128)
    upstream_model_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    context_length: int = Field(default=128_000, ge=1, le=10_000_000)
    # Optional keeps signed internal-command payload digests compatible with
    # pre-capability senders.  The Supervisor normalizes None to the safe
    # no-replay behavior at the PrepareIdentity boundary.
    runtime_capabilities: RuntimeModelCapabilities | None = None
    provider_route: ProviderRouteGrantV1 | None = None
    config_revision: str = Field(min_length=1, max_length=128)
    admission_generation: int = Field(gt=0)
    max_prompt_bytes: int = Field(gt=0, le=16 * 1024 * 1024)
    max_output_tokens: int = Field(gt=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_profile_revision(self) -> "RuntimeTurnIdentity":
        if self.profile_applied_revision > self.profile_desired_revision:
            raise ValueError(
                "profile_applied_revision cannot exceed profile_desired_revision"
            )
        return self


class ChannelExecutionLease(StrictContractModel):
    version: Literal[1] = 1
    lease_id: str = Field(min_length=8, max_length=128)
    org_id: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    target_type: Literal["personal_agent", "application_agent"]
    target_id: int = Field(gt=0)
    subject_user_id: int = Field(ge=0)
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: str = Field(min_length=1, max_length=128)
    runtime_session_id: str = Field(min_length=1, max_length=256)
    model_entry_id: str = Field(min_length=1, max_length=128)
    profile_id: str = Field(min_length=1, max_length=128)
    application_agent_id: int | None = Field(default=None, ge=1)
    gateway_instance_id: str = Field(min_length=8, max_length=256)
    channel_lease_epoch: int = Field(gt=0)
    channel_generation: int = Field(gt=0)
    config_generation: int = Field(ge=0)
    admission_generation: int = Field(gt=0)
    authorization_epoch: int = Field(gt=0)
    issued_at: datetime
    expires_at: datetime
    signature: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_lifetime(self) -> "ChannelExecutionLease":
        lifetime = (self.expires_at - self.issued_at).total_seconds()
        if lifetime <= 0 or lifetime > 120:
            raise ValueError(
                "ChannelExecutionLease lifetime must be within 120 seconds"
            )
        return self


class PrepareCommandPayload(StrictContractModel):
    type: Literal["prepare"] = "prepare"
    identity: RuntimeTurnIdentity
    priority: Literal["interactive", "business", "background"] = "business"


class InternalTurnCommandPayload(StrictContractModel):
    type: Literal["internal_turn"] = "internal_turn"
    source: Literal[
        "web_clarify",
        "personal_wecom",
        "application_wecom",
        "openapi",
        "scheduled_task",
        "expert_collaboration",
    ]
    run_id: str = Field(min_length=8, max_length=128)
    client_message_id: str = Field(min_length=8, max_length=128)
    identity: RuntimeTurnIdentity
    prompt: str
    attachment_file_ids: list[str] = Field(default_factory=list, max_length=20)
    attachments: list[RuntimeTurnAttachment] = Field(
        default_factory=list,
        max_length=20,
    )
    channel_execution_lease: ChannelExecutionLease | None = None
    source_detail: dict[str, Any] | None = None
    scheduled_task_id: int | None = Field(default=None, gt=0)
    scheduled_occurrence_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=256,
    )


class CancelCommandPayload(StrictContractModel):
    type: Literal["cancel"] = "cancel"
    run_id: str = Field(min_length=8, max_length=128)
    clarify_request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )


class CancelActiveRunsCommandPayload(StrictContractModel):
    """Interrupt every non-terminal Run before an authorized Runtime restart."""

    type: Literal["cancel_active_runs"] = "cancel_active_runs"


class CancelScheduledCandidateCommandPayload(StrictContractModel):
    type: Literal["cancel_scheduled_candidate"] = "cancel_scheduled_candidate"
    session_id: str = Field(min_length=1, max_length=256)
    scheduled_task_id: int = Field(gt=0)


class DrainCommandPayload(StrictContractModel):
    type: Literal["drain"] = "drain"
    deadline_seconds: Literal[900] = 900


class ConfigBundleReference(StrictContractModel):
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: str = Field(min_length=1, max_length=128)
    config_revision: str = Field(min_length=1, max_length=128)
    config_digest: str = Field(pattern=SHA256_PATTERN)
    download_url: str = Field(min_length=1, max_length=2048)
    minimum_protocol_version: int = Field(default=1, ge=1)
    expires_at: datetime
    signature: str = Field(pattern=SHA256_PATTERN)


class ConfigBundlePayload(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: str = Field(min_length=1, max_length=128)
    config_revision: str = Field(min_length=1, max_length=128)
    config_digest: str = Field(pattern=SHA256_PATTERN)
    files: dict[str, str]


class ApplyConfigCommandPayload(StrictContractModel):
    type: Literal["apply_config"] = "apply_config"
    bundle: ConfigBundleReference


class RevokeCommandPayload(StrictContractModel):
    type: Literal["revoke"] = "revoke"
    scope: Literal["user", "model", "application"]
    subject_id: str = Field(min_length=1, max_length=128)
    revoked_at: datetime
    cancel_active_runs: bool = False


class SessionMutationCommandPayload(StrictContractModel):
    type: Literal["session_mutation"] = "session_mutation"
    operation: Literal["rename", "delete", "invalidate"]
    session_id: str = Field(min_length=1, max_length=256)
    profile_id: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "SessionMutationCommandPayload":
        if self.operation == "rename" and self.title is None:
            raise ValueError("rename requires title")
        if self.operation != "rename" and self.title is not None:
            raise ValueError("title is only valid for rename")
        return self


class KanbanWorkerRouteActivateCommandPayload(StrictContractModel):
    type: Literal[
        "kanban_worker_route_activate"
    ] = "kanban_worker_route_activate"
    run_id: str = Field(min_length=8, max_length=128)
    task_id: str = Field(min_length=1, max_length=100)
    board: str = Field(min_length=1, max_length=100)
    profile_id: str = Field(pattern=r"^expert-[1-9][0-9]*$")
    org_id: int = Field(gt=0)
    model_id: str = Field(min_length=1, max_length=128)
    upstream_model_id: str = Field(min_length=1, max_length=200)
    context_length: int = Field(default=128_000, ge=1, le=10_000_000)
    runtime_capabilities: RuntimeModelCapabilities = Field(
        default_factory=RuntimeModelCapabilities
    )
    provider_route: ProviderRouteGrantV1 | None = None
    max_output_tokens: int = Field(gt=0, le=1_000_000)


class KanbanWorkerRouteRevokeCommandPayload(StrictContractModel):
    type: Literal[
        "kanban_worker_route_revoke"
    ] = "kanban_worker_route_revoke"
    run_id: str = Field(min_length=8, max_length=128)
    task_id: str = Field(min_length=1, max_length=100)
    board: str = Field(min_length=1, max_length=100)
    profile_id: str = Field(pattern=r"^expert-[1-9][0-9]*$")


RuntimeCommandPayload = Annotated[
    PrepareCommandPayload
    | InternalTurnCommandPayload
    | CancelCommandPayload
    | CancelActiveRunsCommandPayload
    | CancelScheduledCandidateCommandPayload
    | DrainCommandPayload
    | ApplyConfigCommandPayload
    | RevokeCommandPayload
    | SessionMutationCommandPayload
    | KanbanWorkerRouteActivateCommandPayload
    | KanbanWorkerRouteRevokeCommandPayload,
    Field(discriminator="type"),
]


class RuntimeCommandEnvelope(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    command_id: str = Field(min_length=8, max_length=128)
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: str = Field(min_length=1, max_length=128)
    admission_generation: int = Field(gt=0)
    payload_digest: str = Field(pattern=SHA256_PATTERN)
    issued_at: datetime
    deadline_at: datetime | None = None
    payload: RuntimeCommandPayload

    @model_validator(mode="after")
    def validate_deadline(self) -> "RuntimeCommandEnvelope":
        if self.deadline_at is not None and self.deadline_at <= self.issued_at:
            raise ValueError("deadline_at must be later than issued_at")
        normalized_payload = self.payload.model_dump(
            mode="json", exclude_none=True
        )
        actual_digest = canonical_payload_digest(normalized_payload)
        # Optional identity fields were added after v1 senders had already
        # started signing payloads. Accept the wire form that explicitly
        # carries ``null`` while emitting the normalized form downstream.
        compatibility_payload = self.payload.model_dump(
            mode="json", exclude_none=True
        )
        # A v1 channel sender may have serialized the newly optional
        # application identity as an explicit null. Recreate that exact wire
        # shape without weakening validation for any other fields.
        if isinstance(compatibility_payload.get("channel_execution_lease"), dict):
            compatibility_payload["channel_execution_lease"].setdefault(
                "application_agent_id", None
            )
        compatibility_digest = canonical_payload_digest(compatibility_payload)
        if self.payload_digest not in {actual_digest, compatibility_digest}:
            raise ValueError("payload_digest does not match command payload")
        identity = getattr(self.payload, "identity", None)
        if (
            identity is not None
            and identity.admission_generation != self.admission_generation
        ):
            raise ValueError(
                "payload admission_generation does not match command envelope"
            )
        lease = getattr(self.payload, "channel_execution_lease", None)
        if (
            lease is not None
            and lease.admission_generation != self.admission_generation
        ):
            raise ValueError(
                "lease admission_generation does not match command envelope"
            )
        return self


class RuntimeCommandAck(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    command_id: str = Field(min_length=8, max_length=128)
    state: Literal["accepted", "in_progress", "completed", "rejected"]
    run_id: str | None = Field(default=None, min_length=8, max_length=128)
    result: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=128)


class ChannelFastLaneSubmitFrame(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    type: Literal["channel.turn.submit"] = "channel.turn.submit"
    request_id: str = Field(min_length=8, max_length=128)
    gateway_instance_id: str = Field(min_length=8, max_length=256)
    channel_lease_epoch: int = Field(gt=0)
    envelope: RuntimeCommandEnvelope


class ChannelFastLaneDeliveryAck(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    type: Literal["channel.delivery.ack"] = "channel.delivery.ack"
    request_id: str = Field(min_length=8, max_length=128)
    gateway_instance_id: str = Field(min_length=8, max_length=256)
    channel_lease_epoch: int = Field(gt=0)
    run_id: str = Field(min_length=8, max_length=128)
    highest_contiguous_seq: int = Field(ge=0)
    provider_request_id: str = Field(min_length=1, max_length=256)
    provider_message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    projection_version: int = Field(gt=0)
    confirmed_at: datetime


class ChannelFastLaneV2SubmitFrame(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    channel_fast_lane_schema_version: Literal[2] = (
        CHANNEL_FAST_LANE_V2_SCHEMA_VERSION
    )
    type: Literal["channel.turn.submit"] = "channel.turn.submit"
    request_id: str = Field(min_length=8, max_length=128)
    gateway_instance_id: str = Field(min_length=8, max_length=256)
    channel_lease_epoch: int = Field(gt=0)
    delivery_id: str = Field(min_length=8, max_length=256)
    client_message_id: str = Field(min_length=8, max_length=128)
    envelope: RuntimeCommandEnvelope

    @model_validator(mode="after")
    def validate_client_message_id(self) -> "ChannelFastLaneV2SubmitFrame":
        turn = self.envelope.payload
        if (
            getattr(turn, "type", None) == "internal_turn"
            and turn.client_message_id != self.client_message_id
        ):
            raise ValueError(
                "client_message_id does not match internal turn payload"
            )
        return self


class ChannelFastLaneV2ResumeFrame(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    channel_fast_lane_schema_version: Literal[2] = (
        CHANNEL_FAST_LANE_V2_SCHEMA_VERSION
    )
    type: Literal["channel.turn.resume"] = "channel.turn.resume"
    request_id: str = Field(min_length=8, max_length=128)
    gateway_instance_id: str = Field(min_length=8, max_length=256)
    channel_lease_epoch: int = Field(gt=0)
    delivery_id: str = Field(min_length=8, max_length=256)
    last_disposition_seq: int = Field(ge=0)
    last_event_seq: int = Field(ge=0)


class ChannelFastLaneV2DispositionAck(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    channel_fast_lane_schema_version: Literal[2] = (
        CHANNEL_FAST_LANE_V2_SCHEMA_VERSION
    )
    type: Literal[
        "channel.turn.disposition.ack"
    ] = "channel.turn.disposition.ack"
    request_id: str = Field(min_length=8, max_length=128)
    gateway_instance_id: str = Field(min_length=8, max_length=256)
    channel_lease_epoch: int = Field(gt=0)
    delivery_id: str = Field(min_length=8, max_length=256)
    disposition_seq: int = Field(gt=0)


class ChannelFastLaneV2DispositionFrame(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    channel_fast_lane_schema_version: Literal[2] = (
        CHANNEL_FAST_LANE_V2_SCHEMA_VERSION
    )
    type: Literal["channel.turn.disposition"] = "channel.turn.disposition"
    delivery_id: str = Field(min_length=8, max_length=256)
    client_message_id: str = Field(min_length=8, max_length=128)
    disposition_seq: int = Field(gt=0)
    state: Literal[
        "pending",
        "ordinary_run_accepted",
        "redirect_accepted",
        "redirect_consumed",
        "promoted",
        "uncertain",
        "canceled",
    ]
    canonical_run_id: str = Field(min_length=8, max_length=128)
    parent_run_id: str | None = Field(
        min_length=8,
        max_length=128,
    )
    start_after_seq: int | None = Field(ge=0)
    promoted_run_id: str | None = Field(
        min_length=8,
        max_length=128,
    )

    @model_validator(mode="after")
    def validate_state_identity(self) -> "ChannelFastLaneV2DispositionFrame":
        if self.state == "ordinary_run_accepted":
            valid = (
                self.parent_run_id is None
                and self.start_after_seq == 0
                and self.promoted_run_id is None
            )
        elif self.state in {"redirect_accepted", "redirect_consumed"}:
            valid = (
                self.parent_run_id is not None
                and self.start_after_seq is not None
                and self.start_after_seq > 0
                and self.promoted_run_id is None
                and self.canonical_run_id == self.parent_run_id
            )
        elif self.state == "promoted":
            valid = (
                self.parent_run_id is not None
                and self.start_after_seq == 0
                and self.promoted_run_id is not None
                and self.canonical_run_id == self.promoted_run_id
            )
        elif self.state == "uncertain":
            valid = (
                self.parent_run_id is not None
                and self.promoted_run_id is None
                and (
                    self.start_after_seq is None
                    or self.start_after_seq > 0
                )
                and self.canonical_run_id == self.parent_run_id
            )
        else:
            valid = (
                self.parent_run_id is not None
                and self.start_after_seq is None
                and self.promoted_run_id is None
                and self.canonical_run_id == self.parent_run_id
            )
        if not valid:
            raise ValueError("invalid Fast Lane v2 disposition identity")
        return self


class ChannelFastLaneV2DeliveryAck(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    channel_fast_lane_schema_version: Literal[2] = (
        CHANNEL_FAST_LANE_V2_SCHEMA_VERSION
    )
    type: Literal["channel.delivery.ack"] = "channel.delivery.ack"
    request_id: str = Field(min_length=8, max_length=128)
    gateway_instance_id: str = Field(min_length=8, max_length=256)
    channel_lease_epoch: int = Field(gt=0)
    owner_delivery_id: str = Field(min_length=8, max_length=256)
    canonical_run_id: str = Field(min_length=8, max_length=128)
    highest_contiguous_seq: int = Field(ge=0)
    provider_request_id: str = Field(min_length=1, max_length=256)
    provider_message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    projection_version: int = Field(gt=0)
    confirmed_at: datetime


class RuntimeProjectionItem(StrictContractModel):
    projection_id: str = Field(min_length=8, max_length=128)
    runtime_id: str = Field(min_length=1, max_length=128)
    projection_type: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    payload_digest: str = Field(pattern=SHA256_PATTERN)
    run_id: str | None = Field(default=None, min_length=8, max_length=128)
    seq: int | None = Field(default=None, gt=0)
    config_revision: str | None = Field(default=None, min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_run_sequence(self) -> "RuntimeProjectionItem":
        if self.seq is not None and self.run_id is None:
            raise ValueError("seq requires run_id")
        if self.payload_digest != canonical_payload_digest(self.payload):
            raise ValueError("payload_digest does not match projection payload")
        return self


class RuntimeProjectionBatch(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: str = Field(min_length=1, max_length=128)
    items: list[RuntimeProjectionItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_runtime_ids(self) -> "RuntimeProjectionBatch":
        if any(item.runtime_id != self.runtime_id for item in self.items):
            raise ValueError("all projection items must belong to the batch runtime")
        return self


class RuntimeProjectionRejection(StrictContractModel):
    projection_id: str = Field(min_length=8, max_length=128)
    error_code: str = Field(min_length=1, max_length=128)


class RuntimeProjectionAck(StrictContractModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    runtime_id: str = Field(min_length=1, max_length=128)
    accepted_projection_ids: list[str] = Field(default_factory=list, max_length=100)
    rejected: list[RuntimeProjectionRejection] = Field(default_factory=list, max_length=100)
