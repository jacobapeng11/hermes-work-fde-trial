from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError

from app.schemas.mcp_connection import validate_mcp_connection_target


Digest = str
_SENSITIVE_CONFIG_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "clientsecret",
    "credential",
    "credentials",
    "password",
    "proxyauthorization",
    "secret",
    "token",
}


def _contains_plaintext_credential(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = "".join(
                character for character in str(key).casefold() if character.isalnum()
            )
            if normalized_key in _SENSITIVE_CONFIG_KEYS:
                return True
            if _contains_plaintext_credential(item):
                return True
    elif isinstance(value, list):
        return any(_contains_plaintext_credential(item) for item in value)
    elif isinstance(value, str) and "://" in value:
        parsed = urlparse(value)
        return parsed.username is not None or parsed.password is not None
    return False


class StrictAssetProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileComponentDigests(StrictAssetProjectionModel):
    profile: Digest = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    skill: Digest = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    mcp: Digest = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    knowledge: Digest = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ProfileArtifactManifestEntry(StrictAssetProjectionModel):
    asset_type: Literal["skill", "personal_skill_artifact"]
    asset_id: int = Field(gt=0)
    version_id: int = Field(gt=0)
    storage_uri: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_relative_path: str = Field(min_length=1, max_length=500)

    @field_validator("target_relative_path")
    @classmethod
    def validate_target_relative_path(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("target_relative_path must be a canonical relative path")
        return value


class ProfileCapabilityTarget(StrictAssetProjectionModel):
    capability_type: Literal["skill", "mcp", "knowledge_base"]
    capability_id: int = Field(gt=0)
    target_version_id: int | None = Field(default=None, gt=0)
    desired_digest: Digest | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    required: bool = False


class ProfileCapabilityEvidence(StrictAssetProjectionModel):
    capability_type: Literal["skill", "mcp", "knowledge_base"]
    capability_id: int = Field(gt=0)
    target_version_id: int | None = Field(default=None, gt=0)
    status: Literal["applied", "unavailable", "revoked"]
    applied_digest: Digest | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=1000)
    retry_classification: Literal[
        "none", "transient", "deterministic", "authorization"
    ] = "none"


class ProfileProjectionApplyRequest(StrictAssetProjectionModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=8, max_length=128)
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: int = Field(gt=0)
    runtime_workspace_id: int = Field(gt=0)
    runtime_agent_profile_id: int = Field(gt=0)
    hermes_profile_name: str = Field(min_length=1, max_length=128)
    desired_revision: int = Field(gt=0)
    expected_applied_revision: int = Field(ge=0)
    expected_effective_generation: int = Field(default=0, ge=0)
    digests: ProfileComponentDigests
    profile_config: dict
    artifacts: list[ProfileArtifactManifestEntry] = Field(
        default_factory=list, max_length=256
    )
    capabilities: list[ProfileCapabilityTarget] = Field(
        default_factory=list, max_length=512
    )


class ProfileProjectionApplyResponse(StrictAssetProjectionModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=8, max_length=128)
    runtime_agent_profile_id: int = Field(gt=0)
    desired_revision: int = Field(gt=0)
    applied_revision: int = Field(ge=0)
    effective_generation: int = Field(default=0, ge=0)
    status: Literal[
        "pending",
        "applied",
        "applied_degraded",
        "failed",
        "superseded",
        "revoked",
    ]
    digests: ProfileComponentDigests | None = None
    capabilities: list[ProfileCapabilityEvidence] = Field(
        default_factory=list, max_length=512
    )
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=1000)


class ProfileExecutionControlRequest(StrictAssetProjectionModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=8, max_length=128)
    action: Literal["revoke", "enable"]
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: int = Field(gt=0)
    runtime_workspace_id: int = Field(gt=0)
    runtime_agent_profile_id: int = Field(gt=0)
    admission_generation: int = Field(gt=0)
    reason_code: str = Field(min_length=1, max_length=128)
    cancel_active_runs: bool = False


class ProfileExecutionControlResponse(StrictAssetProjectionModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=8, max_length=128)
    runtime_agent_profile_id: int = Field(gt=0)
    action: Literal["revoke", "enable"]
    admission_generation: int = Field(gt=0)
    status: Literal["applied", "already_applied", "failed"]
    affected_active_run_ids: list[str] = Field(default_factory=list, max_length=256)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=1000)


class CapabilityErrorEventData(StrictAssetProjectionModel):
    run_id: str = Field(min_length=1, max_length=128)
    capability_type: Literal["skill", "mcp", "knowledge_base"]
    error_code: Literal[
        "KNOWLEDGE_BASE_NO_PERMISSION",
        "MCP_USER_AUTH_REQUIRED",
        "CAPABILITY_FORCE_DISABLED",
        "CAPABILITY_PROJECTION_UNAVAILABLE",
    ]
    display_message: str = Field(min_length=1, max_length=200)
    recoverable: bool
    error_id: str = Field(min_length=8, max_length=128)


class CapabilityErrorEvent(StrictAssetProjectionModel):
    type: Literal["capability.error"] = "capability.error"
    data: CapabilityErrorEventData


class RuntimeMcpRegistryServer(StrictAssetProjectionModel):
    """One Runtime connection keyed by a stable internal alias.

    ``server_slug`` is retained as a v1 wire-field name. Its value is the
    Backend-generated ``asset-<mcp_id>`` alias after identity migration, not
    the editable business slug from the MCP catalog.
    """

    mcp_id: int = Field(gt=0)
    mcp_version_id: int = Field(gt=0)
    server_slug: str = Field(min_length=1, max_length=128)
    transport: Literal["streamable_http", "sse", "stdio"]
    connection_config: dict
    credential_refs: dict[str, str] = Field(default_factory=dict)
    config_digest: Digest = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @field_validator("connection_config")
    @classmethod
    def reject_plaintext_credentials(cls, value: dict) -> dict:
        if _contains_plaintext_credential(value):
            raise ValueError("connection_config must use credential_refs")
        return value

    @model_validator(mode="after")
    def validate_connection_target(self) -> "RuntimeMcpRegistryServer":
        validate_mcp_connection_target(self.transport, self.connection_config)
        return self


def runtime_mcp_registry_servers_are_valid(raw_servers: object) -> bool:
    if not isinstance(raw_servers, list):
        return False
    try:
        for raw_server in raw_servers:
            RuntimeMcpRegistryServer.model_validate(raw_server)
    except (ValueError, ValidationError):
        return False
    return True


class RuntimeMcpRegistryApplyRequest(StrictAssetProjectionModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=8, max_length=128)
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: int = Field(gt=0)
    runtime_workspace_id: int = Field(gt=0)
    desired_revision: int = Field(gt=0)
    expected_applied_revision: int = Field(ge=0)
    registry_digest: Digest = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    servers: list[RuntimeMcpRegistryServer] = Field(max_length=128)


class RuntimeMcpRegistryServerEvidence(StrictAssetProjectionModel):
    server_slug: str = Field(min_length=1, max_length=128)
    status: Literal["ready", "failed"]
    tools_schema_digest: Digest | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    tool_names: list[str] = Field(default_factory=list, max_length=512)
    error_code: str | None = Field(default=None, max_length=128)


class RuntimeMcpRegistryApplyResponse(StrictAssetProjectionModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=8, max_length=128)
    desired_revision: int = Field(gt=0)
    applied_revision: int = Field(ge=0)
    status: Literal["reloading", "ready", "failed"]
    registry_digest: Digest | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    servers: list[RuntimeMcpRegistryServerEvidence] = Field(default_factory=list)
    affected_active_run_ids: list[str] = Field(default_factory=list, max_length=256)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=1000)
