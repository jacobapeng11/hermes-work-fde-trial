from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.model_runtime_capabilities import RuntimeModelCapabilities
from app.schemas.provider_routes import ProviderRouteGrantV1


RUNTIME_INTERNAL_SCHEMA_VERSION = 1
SHA256_PATTERN = r"^[a-f0-9]{64}$"


def canonical_payload_digest(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class StrictRuntimeInternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeTurnAttachment(StrictRuntimeInternalModel):
    file_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    relative_path: str = Field(min_length=1, max_length=1024)
    mime_type: str | None = Field(default=None, max_length=255)


class RuntimeTurnIdentity(StrictRuntimeInternalModel):
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
    runtime_capabilities: RuntimeModelCapabilities = Field(
        default_factory=RuntimeModelCapabilities
    )
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


class ChannelExecutionLease(StrictRuntimeInternalModel):
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


class PrepareCommandPayload(StrictRuntimeInternalModel):
    type: Literal["prepare"] = "prepare"
    identity: RuntimeTurnIdentity
    priority: Literal["interactive", "business", "background"] = "business"


class InternalTurnCommandPayload(StrictRuntimeInternalModel):
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
    source_detail: dict[str, Any] | None = None
    scheduled_task_id: int | None = Field(default=None, gt=0)
    scheduled_occurrence_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=256,
    )
    channel_execution_lease: ChannelExecutionLease | None = None


class CancelCommandPayload(StrictRuntimeInternalModel):
    type: Literal["cancel"] = "cancel"
    run_id: str = Field(min_length=8, max_length=128)
    clarify_request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )


class CancelActiveRunsCommandPayload(StrictRuntimeInternalModel):
    """Interrupt every non-terminal Run without introducing restart state."""

    type: Literal["cancel_active_runs"] = "cancel_active_runs"


class CancelScheduledCandidateCommandPayload(StrictRuntimeInternalModel):
    type: Literal["cancel_scheduled_candidate"] = "cancel_scheduled_candidate"
    session_id: str = Field(min_length=1, max_length=256)
    scheduled_task_id: int = Field(gt=0)


class DrainCommandPayload(StrictRuntimeInternalModel):
    type: Literal["drain"] = "drain"
    deadline_seconds: Literal[900] = 900


class ConfigBundleReference(StrictRuntimeInternalModel):
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: str = Field(min_length=1, max_length=128)
    config_revision: str = Field(min_length=1, max_length=128)
    config_digest: str = Field(pattern=SHA256_PATTERN)
    download_url: str = Field(min_length=1, max_length=2048)
    minimum_protocol_version: int = Field(default=1, ge=1)
    expires_at: datetime
    signature: str = Field(pattern=SHA256_PATTERN)


class ConfigBundlePayload(StrictRuntimeInternalModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: str = Field(min_length=1, max_length=128)
    config_revision: str = Field(min_length=1, max_length=128)
    config_digest: str = Field(pattern=SHA256_PATTERN)
    files: dict[str, str]


class ApplyConfigCommandPayload(StrictRuntimeInternalModel):
    type: Literal["apply_config"] = "apply_config"
    bundle: ConfigBundleReference


class RevokeCommandPayload(StrictRuntimeInternalModel):
    type: Literal["revoke"] = "revoke"
    scope: Literal["user", "model", "application"]
    subject_id: str = Field(min_length=1, max_length=128)
    revoked_at: datetime
    cancel_active_runs: bool = False


class SessionMutationCommandPayload(StrictRuntimeInternalModel):
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


class KanbanWorkerRouteActivateCommandPayload(StrictRuntimeInternalModel):
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


class KanbanWorkerRouteRevokeCommandPayload(StrictRuntimeInternalModel):
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


class RuntimeCommandEnvelope(StrictRuntimeInternalModel):
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
        actual_digest = canonical_payload_digest(
            self.payload.model_dump(mode="json", exclude_none=True)
        )
        if self.payload_digest != actual_digest:
            raise ValueError("payload_digest does not match command payload")
        return self


class RuntimeCommandAck(StrictRuntimeInternalModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    command_id: str = Field(min_length=8, max_length=128)
    state: Literal["accepted", "in_progress", "completed", "rejected"]
    run_id: str | None = Field(default=None, min_length=8, max_length=128)
    result: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=128)


class ChannelFastLaneSubmitFrame(StrictRuntimeInternalModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    type: Literal["channel.turn.submit"] = "channel.turn.submit"
    request_id: str = Field(min_length=8, max_length=128)
    gateway_instance_id: str = Field(min_length=8, max_length=256)
    channel_lease_epoch: int = Field(gt=0)
    envelope: RuntimeCommandEnvelope


class ChannelFastLaneDeliveryAck(StrictRuntimeInternalModel):
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


class ChannelFastLaneV2SubmitFrame(StrictRuntimeInternalModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    channel_fast_lane_schema_version: Literal[2] = 2
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


class ChannelFastLaneV2ResumeFrame(StrictRuntimeInternalModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    channel_fast_lane_schema_version: Literal[2] = 2
    type: Literal["channel.turn.resume"] = "channel.turn.resume"
    request_id: str = Field(min_length=8, max_length=128)
    gateway_instance_id: str = Field(min_length=8, max_length=256)
    channel_lease_epoch: int = Field(gt=0)
    delivery_id: str = Field(min_length=8, max_length=256)
    last_disposition_seq: int = Field(ge=0)
    last_event_seq: int = Field(ge=0)


class ChannelFastLaneV2DispositionAck(StrictRuntimeInternalModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    channel_fast_lane_schema_version: Literal[2] = 2
    type: Literal[
        "channel.turn.disposition.ack"
    ] = "channel.turn.disposition.ack"
    request_id: str = Field(min_length=8, max_length=128)
    gateway_instance_id: str = Field(min_length=8, max_length=256)
    channel_lease_epoch: int = Field(gt=0)
    delivery_id: str = Field(min_length=8, max_length=256)
    disposition_seq: int = Field(gt=0)


class ChannelFastLaneV2DispositionFrame(StrictRuntimeInternalModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    channel_fast_lane_schema_version: Literal[2] = 2
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
    parent_run_id: str | None = Field(min_length=8, max_length=128)
    start_after_seq: int | None = Field(ge=0)
    promoted_run_id: str | None = Field(min_length=8, max_length=128)

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
                and (self.start_after_seq is None or self.start_after_seq > 0)
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


class ChannelFastLaneV2DeliveryAck(StrictRuntimeInternalModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    channel_fast_lane_schema_version: Literal[2] = 2
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


class RuntimeProjectionItem(StrictRuntimeInternalModel):
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


class RuntimeProjectionBatch(StrictRuntimeInternalModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: str = Field(min_length=1, max_length=128)
    items: list[RuntimeProjectionItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_runtime_ids(self) -> "RuntimeProjectionBatch":
        if any(item.runtime_id != self.runtime_id for item in self.items):
            raise ValueError("all projection items must belong to the batch runtime")
        return self


class RuntimeProjectionRejection(StrictRuntimeInternalModel):
    projection_id: str = Field(min_length=8, max_length=128)
    error_code: str = Field(min_length=1, max_length=128)


class RuntimeProjectionAck(StrictRuntimeInternalModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    runtime_id: str = Field(min_length=1, max_length=128)
    accepted_projection_ids: list[str] = Field(default_factory=list, max_length=100)
    rejected: list[RuntimeProjectionRejection] = Field(default_factory=list, max_length=100)


class RuntimeAdmissionAuditItem(StrictRuntimeInternalModel):
    event_id: str = Field(min_length=8, max_length=128)
    runtime_id: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    operation: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=64)
    outcome: Literal["succeeded", "rejected", "internal_error"]
    stage: str = Field(min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
    request_id: str | None = Field(default=None, min_length=8, max_length=128)
    client_message_id: str | None = Field(default=None, min_length=8, max_length=128)
    run_id: str | None = Field(default=None, min_length=8, max_length=128)
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    exception_type: str | None = Field(default=None, min_length=1, max_length=128)
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    config_revision: str | None = Field(default=None, min_length=1, max_length=128)
    attachment_count: int | None = Field(default=None, ge=0, le=20)
    prompt_bytes: int | None = Field(default=None, ge=0, le=16 * 1024 * 1024)
    upstream_status: int | None = Field(default=None, ge=100, le=599)
    upstream_path: str | None = Field(default=None, min_length=1, max_length=512)


class RuntimeAdmissionAuditBatch(StrictRuntimeInternalModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: str = Field(min_length=1, max_length=128)
    items: list[RuntimeAdmissionAuditItem] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_runtime_ids(self) -> "RuntimeAdmissionAuditBatch":
        if any(item.runtime_id != self.runtime_id for item in self.items):
            raise ValueError("all audit items must belong to the batch runtime")
        return self


class RuntimeAdmissionAuditAck(StrictRuntimeInternalModel):
    schema_version: Literal[1] = RUNTIME_INTERNAL_SCHEMA_VERSION
    runtime_id: str = Field(min_length=1, max_length=128)
    accepted_event_ids: list[str] = Field(default_factory=list, max_length=50)
    duplicate_event_ids: list[str] = Field(default_factory=list, max_length=50)
    rejected_event_ids: list[str] = Field(default_factory=list, max_length=50)
