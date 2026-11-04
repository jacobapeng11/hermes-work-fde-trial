from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class ApplicationAgentConfig(BaseModel):
    system_prompt: str = Field(default="", max_length=50_000)
    model_entry_id: int | None = None
    skill_asset_ids: list[int] = Field(default_factory=list, max_length=100)
    application_skill_ids: list[int] = Field(
        default_factory=list,
        max_length=100,
    )
    mcp_asset_ids: list[int] = Field(default_factory=list, max_length=100)
    enterprise_knowledge_enabled: bool = False
    memory_enabled: bool = True

    @field_validator(
        "skill_asset_ids",
        "application_skill_ids",
        "mcp_asset_ids",
    )
    @classmethod
    def unique_positive_ids(cls, values: list[int]) -> list[int]:
        if any(value <= 0 for value in values):
            raise ValueError("asset ids must be positive")
        return list(dict.fromkeys(values))

class ApplicationAgentSkillSummary(BaseModel):
    application_agent_skill_id: int
    application_agent_id: int
    slug: str
    name: str
    description: str | None = None
    category_key: str
    package_sha256: str
    skill_version_id: int | None = None
    version: int | None = None
    created_at: datetime | None = None


class ApplicationAgentAccessSummary(BaseModel):
    can_manage_application_agents: bool


class ApplicationAgentCreateRequest(BaseModel):
    slug: str = Field(min_length=2, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=100)
    avatar_url: str | None = Field(default=None, max_length=1000)
    config: ApplicationAgentConfig = Field(default_factory=ApplicationAgentConfig)


class ApplicationAgentUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    avatar_url: str | None = Field(default=None, max_length=1000)
    config: ApplicationAgentConfig
    expected_config_generation: int | None = Field(default=None, ge=1)


class ApplicationAgentSummary(BaseModel):
    application_agent_id: int
    slug: str
    name: str
    avatar_url: str | None = None
    status: Literal["unpublished", "published", "disabled"]
    runtime_status: str
    config_generation: int
    applied_generation: int | None = None
    apply_status: str
    lifecycle_status: Literal["active", "deleting"] = "active"
    delete_requested_at: datetime | None = None
    delete_last_error_code: str | None = None
    client_count: int = 0
    active_run_count: int = 0
    queued_run_count: int = 0
    published_at: datetime | None = None
    updated_at: datetime | None = None
    access_mode: Literal["owner", "viewer"]
    creator_name: str | None = None
    creator_user_code: str | None = None
    creator_status: str | None = None
    personal_knowledge_enabled_count: int = 0
    personal_knowledge_revoked_count: int = 0


class ApplicationAgentPersonalKnowledgeCandidate(BaseModel):
    knowledge_base_id: int
    name: str
    description: str | None = None
    owner_user_id: int
    owner_name: str
    effective_role: Literal["owner", "download", "preview", "retrieve"]
    file_count: int = 0
    ready_file_count: int = 0
    visibility: Literal["private", "shared"]
    status: str
    created_at: datetime
    updated_at: datetime
    selected: bool = False


class ApplicationAgentPersonalKnowledgeCatalog(BaseModel):
    items: list[ApplicationAgentPersonalKnowledgeCandidate]
    enabled_count: int
    revoked_count: int
    config_generation: int


class ApplicationAgentDetail(ApplicationAgentSummary):
    config: ApplicationAgentConfig
    last_apply_error_code: str | None = None


class ApplicationAgentSkillPolicyUpdateRequest(BaseModel):
    policy_version: int = Field(ge=1, le=3)
    expected_config_generation: int = Field(ge=1)


class ApplicationAgentSkillPolicyUpdateResponse(BaseModel):
    application_agent_id: int
    policy_version: int
    config_generation: int
    applied_generation: int | None = None
    apply_status: str
    pending_reload: bool


class ApplicationAgentConfigRevisionSummary(BaseModel):
    revision_id: int
    generation: int
    config_hash: str
    apply_status: str
    applied_at: datetime | None = None
    error_code: str | None = None
    created_by_user_id: int
    created_at: datetime | None = None


class ApplicationClientCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    allow_user_delegation: bool = True
    allow_service_invocation: bool = False
    scopes: list[str] = Field(default_factory=lambda: ["responses:create"], max_length=50)
    allowed_redirect_uris: list[str] = Field(
        default_factory=list,
        max_length=20,
    )

    @field_validator("allowed_redirect_uris")
    @classmethod
    def valid_redirect_uris(cls, values: list[str]) -> list[str]:
        return _validated_redirect_uris(values)

    @model_validator(mode="after")
    def exactly_one_identity_mode(self) -> "ApplicationClientCreateRequest":
        if self.allow_user_delegation == self.allow_service_invocation:
            raise ValueError("application client identity mode must be exclusive")
        return self


class ApplicationClientUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    allow_user_delegation: bool
    allow_service_invocation: bool
    allowed_redirect_uris: list[str] = Field(max_length=20)

    @field_validator("allowed_redirect_uris")
    @classmethod
    def valid_redirect_uris(cls, values: list[str]) -> list[str]:
        return _validated_redirect_uris(values)

    @model_validator(mode="after")
    def exactly_one_identity_mode(self) -> "ApplicationClientUpdateRequest":
        if self.allow_user_delegation == self.allow_service_invocation:
            raise ValueError("application client identity mode must be exclusive")
        return self


class ApplicationClientSummary(BaseModel):
    application_client_id: int
    client_id: str
    name: str
    status: str
    allow_user_delegation: bool
    allow_service_invocation: bool
    auth_mode: Literal[
        "delegated_user",
        "service",
        "legacy_dual",
        "invalid",
    ]
    scopes: list[str]
    allowed_redirect_uris: list[str]
    created_at: datetime | None = None


class ApplicationClientCreated(ApplicationClientSummary):
    client_secret: str


_REDIRECT_RESERVED_QUERY_KEYS = frozenset({"code", "state", "error"})
def _validated_redirect_uris(values: list[str]) -> list[str]:
    normalized_values: list[str] = []
    for raw_value in values:
        value = raw_value.strip()
        if not value or len(value) > 2_048 or "*" in value:
            raise ValueError("application client redirect URI is invalid")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError(
                "application client redirect URI is invalid"
            ) from exc
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65_535)
        ):
            raise ValueError("application client redirect URI is invalid")
        query_keys = {key for key, _value in parse_qsl(parsed.query)}
        if query_keys & _REDIRECT_RESERVED_QUERY_KEYS:
            raise ValueError(
                "application client redirect URI contains reserved query fields"
            )
        normalized_values.append(value)
    return list(dict.fromkeys(normalized_values))
