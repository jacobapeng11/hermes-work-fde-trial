from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.mcp_connection import validate_mcp_connection_target


_SENSITIVE_KEYS = {
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
            normalized = "".join(
                character for character in str(key).casefold() if character.isalnum()
            )
            if normalized in _SENSITIVE_KEYS or _contains_plaintext_credential(item):
                return True
    elif isinstance(value, list):
        return any(_contains_plaintext_credential(item) for item in value)
    elif isinstance(value, str) and "://" in value:
        parsed = urlparse(value)
        return parsed.username is not None or parsed.password is not None
    return False


class StrictAssetCatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillVersionMaterial(StrictAssetCatalogModel):
    package_uri: str = Field(min_length=1, max_length=1000)
    package_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest: dict = Field(default_factory=dict)


class CreateSkillAssetCommand(StrictAssetCatalogModel):
    org_id: int = Field(gt=0)
    owner_user_id: int = Field(gt=0)
    slug: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    asset_scope: Literal["public", "personal", "application"]
    application_agent_id: int | None = Field(default=None, gt=0)
    visibility: Literal["public", "organization", "private"]
    category_key: str = Field(default="other", min_length=1, max_length=64)
    material: SkillVersionMaterial

    @model_validator(mode="after")
    def validate_scope(self) -> "CreateSkillAssetCommand":
        if self.asset_scope == "personal" and self.visibility != "private":
            raise ValueError("personal Skill must be private")
        if self.asset_scope == "public" and self.visibility == "private":
            raise ValueError("public Skill must be visible")
        if self.asset_scope == "application":
            if self.visibility != "private" or self.application_agent_id is None:
                raise ValueError("application Skill must be private and application-owned")
        elif self.application_agent_id is not None:
            raise ValueError("only application Skill can have application_agent_id")
        return self


class PublishSkillVersionCommand(StrictAssetCatalogModel):
    org_id: int = Field(gt=0)
    skill_id: int = Field(gt=0)
    material: SkillVersionMaterial


class McpVersionMaterial(StrictAssetCatalogModel):
    transport: Literal["streamable_http", "sse", "stdio"]
    connection_config: dict
    secret_refs: dict[str, str] = Field(default_factory=dict)
    tools_schema: list[dict] | None = Field(default=None, max_length=512)

    @field_validator("connection_config")
    @classmethod
    def reject_plaintext_credentials(cls, value: dict) -> dict:
        if _contains_plaintext_credential(value):
            raise ValueError("connection_config must contain only non-secret settings")
        return value

    @field_validator("secret_refs")
    @classmethod
    def validate_secret_refs(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() or not reference.strip() for key, reference in value.items()):
            raise ValueError("secret_refs keys and values must be non-empty")
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def validate_credential_target(self) -> "McpVersionMaterial":
        target = "env" if self.transport == "stdio" else "headers"
        existing = self.connection_config.get(target)
        if existing is not None and not isinstance(existing, dict):
            raise ValueError(f"connection_config.{target} must be an object")
        validate_mcp_connection_target(self.transport, self.connection_config)
        return self


class CreateMcpAssetCommand(StrictAssetCatalogModel):
    org_id: int = Field(gt=0)
    owner_user_id: int = Field(gt=0)
    slug: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    visibility: Literal["public", "organization", "private"]
    material: McpVersionMaterial


class PublishMcpVersionCommand(StrictAssetCatalogModel):
    org_id: int = Field(gt=0)
    mcp_id: int = Field(gt=0)
    expected_current_version_id: int | None = Field(default=None, gt=0)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    material: McpVersionMaterial


class AssetCatalogResult(StrictAssetCatalogModel):
    asset_type: Literal["skill", "mcp"]
    asset_id: int = Field(gt=0)
    version_id: int = Field(gt=0)
    version: int = Field(gt=0)
    created_version: bool
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class StableAssetSummary(StrictAssetCatalogModel):
    asset_type: Literal["skill", "mcp"]
    asset_id: int = Field(gt=0)
    current_version_id: int | None = Field(default=None, gt=0)
    version: int | None = Field(default=None, gt=0)
    slug: str
    name: str
    description: str | None = None
    status: str
    market_visible: bool = True
    execution_status: Literal["enabled", "force_disabled"] = "enabled"
    visibility: str
    category_key: str | None = None
    transport: str | None = None
    manifest: dict = Field(default_factory=dict)
    connection_config: dict = Field(default_factory=dict)
    tools_schema: list[dict] | None = None


class AssetExecutionSecurityRequest(StrictAssetCatalogModel):
    reason: str = Field(min_length=1, max_length=2000)


class StableAssetVersionSummary(StrictAssetCatalogModel):
    asset_type: Literal["skill", "mcp"]
    asset_id: int = Field(gt=0)
    version_id: int = Field(gt=0)
    version: int = Field(gt=0)
    status: str
    content_digest: str
    manifest: dict = Field(default_factory=dict)


class CreateStableMcpRequest(StrictAssetCatalogModel):
    slug: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    visibility: Literal["public", "organization"] = "organization"
    material: McpVersionMaterial


class PublishStableMcpVersionRequest(StrictAssetCatalogModel):
    expected_current_version_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    material: McpVersionMaterial
    change_summary: str | None = Field(default=None, max_length=2000)


class StableMcpPublicationResponse(StrictAssetCatalogModel):
    asset: StableAssetSummary
    rollout: "AssetRolloutResult | None" = None


from app.schemas.asset_rollout import AssetRolloutResult  # noqa: E402


StableMcpPublicationResponse.model_rebuild()
