from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


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


def validate_mcp_connection_target(transport: str, config: object) -> None:
    if not isinstance(config, dict):
        raise ValueError("MCP connection config must be an object")
    if transport in {"streamable_http", "sse"}:
        url = config.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("MCP HTTP connection target requires a URL")
        parsed = urlparse(url)
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("MCP HTTP connection target requires a valid URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(character.isspace() for character in url)
        ):
            raise ValueError("MCP HTTP connection target requires a safe HTTP(S) URL")
    elif transport == "stdio":
        command = config.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("MCP stdio connection target requires a command")
    else:
        raise ValueError("MCP transport is unsupported")


class StrictAssetControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileComponentDigests(StrictAssetControlModel):
    profile: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    skill: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    mcp: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    knowledge: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ProfileArtifactManifestEntry(StrictAssetControlModel):
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


class ProfileCapabilityTarget(StrictAssetControlModel):
    capability_type: Literal["skill", "mcp", "knowledge_base"]
    capability_id: int = Field(gt=0)
    target_version_id: int | None = Field(default=None, gt=0)
    desired_digest: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    required: bool = False


class ProfileCapabilityEvidence(StrictAssetControlModel):
    capability_type: Literal["skill", "mcp", "knowledge_base"]
    capability_id: int = Field(gt=0)
    target_version_id: int | None = Field(default=None, gt=0)
    status: Literal["applied", "unavailable", "revoked"]
    applied_digest: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=1000)
    retry_classification: Literal[
        "none", "transient", "deterministic", "authorization"
    ] = "none"


class ProfileProjectionApplyRequest(StrictAssetControlModel):
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


class ProfileProjectionApplyResponse(StrictAssetControlModel):
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


class ProfileExecutionControlRequest(StrictAssetControlModel):
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


class ProfileExecutionControlResponse(StrictAssetControlModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=8, max_length=128)
    runtime_agent_profile_id: int = Field(gt=0)
    action: Literal["revoke", "enable"]
    admission_generation: int = Field(gt=0)
    status: Literal["applied", "already_applied", "failed"]
    affected_active_run_ids: list[str] = Field(default_factory=list, max_length=256)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=1000)


class CapabilityErrorEventData(StrictAssetControlModel):
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


class CapabilityErrorEvent(StrictAssetControlModel):
    type: Literal["capability.error"] = "capability.error"
    data: CapabilityErrorEventData


class RuntimeMcpRegistryServer(StrictAssetControlModel):
    """A registry server whose legacy field name carries an internal alias."""

    mcp_id: int = Field(gt=0)
    mcp_version_id: int = Field(gt=0)
    server_slug: str = Field(min_length=1, max_length=128)
    transport: Literal["streamable_http", "sse", "stdio"]
    connection_config: dict
    credential_refs: dict[str, str] = Field(default_factory=dict)
    config_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

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


class RuntimeMcpRegistryApplyRequest(StrictAssetControlModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=8, max_length=128)
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_id: int = Field(gt=0)
    runtime_workspace_id: int = Field(gt=0)
    desired_revision: int = Field(gt=0)
    expected_applied_revision: int = Field(ge=0)
    registry_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    servers: list[RuntimeMcpRegistryServer] = Field(max_length=128)


class RuntimeMcpRegistryServerEvidence(StrictAssetControlModel):
    server_slug: str = Field(min_length=1, max_length=128)
    status: Literal["ready", "failed"]
    tools_schema_digest: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    tool_names: list[str] = Field(default_factory=list, max_length=512)
    error_code: str | None = Field(default=None, max_length=128)


class RuntimeMcpRegistryApplyResponse(StrictAssetControlModel):
    schema_version: Literal[1] = 1
    operation_id: str = Field(min_length=8, max_length=128)
    desired_revision: int = Field(gt=0)
    applied_revision: int = Field(ge=0)
    status: Literal["reloading", "ready", "failed"]
    registry_digest: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )
    servers: list[RuntimeMcpRegistryServerEvidence] = Field(default_factory=list)
    affected_active_run_ids: list[str] = Field(default_factory=list, max_length=256)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=1000)
