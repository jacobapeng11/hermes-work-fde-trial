from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.services.runtime_workspace import Principal


FORBIDDEN_MANIFEST_KEYS = {
    "api_key",
    "apikey",
    "api_key_encrypted",
    "provider_key",
    "provider_api_key",
    "secret",
    "mcp_secret",
    "password",
    "private_key",
    "token",
    "runtime_token",
    "runtime_id",
    "runtime_workspace_id",
    "workspace_id",
    "business_workspace_id",
    "copaw_agent_id",
    "session_id",
    "session_history",
    "history",
    "host_path",
    "kubeconfig",
}


class ExpertPackageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ExpertPackageRecord:
    expert_package_id: int
    org_id: int
    owner_user_id: int
    slug: str
    name: str
    description: str | None
    avatar_url: str | None
    status: str
    visibility: str
    current_version_id: int | None = None


@dataclass(frozen=True)
class ExpertPackageVersionRecord:
    expert_package_version_id: int
    org_id: int
    expert_package_id: int
    version: int
    manifest: dict[str, Any]
    prompt_snapshot: str | None
    skill_refs: list[dict] | None
    mcp_refs: list[dict] | None
    knowledge_refs: list[dict] | None
    model_preferences: dict | None
    status: str


@dataclass(frozen=True)
class CreateExpertPackageCommand:
    slug: str
    name: str
    description: str | None = None
    avatar_url: str | None = None
    visibility: str = "private"


@dataclass(frozen=True)
class CreateExpertPackageVersionCommand:
    expert_package_id: int
    manifest: dict[str, Any]
    prompt_snapshot: str | None = None
    skill_refs: list[dict] | None = None
    mcp_refs: list[dict] | None = None
    knowledge_refs: list[dict] | None = None
    model_preferences: dict | None = None


class ExpertPackageStore(Protocol):
    async def create_package(
        self,
        *,
        org_id: int,
        owner_user_id: int,
        command: CreateExpertPackageCommand,
    ) -> ExpertPackageRecord:
        ...

    async def get_package(
        self,
        *,
        org_id: int,
        owner_user_id: int,
        expert_package_id: int,
    ) -> ExpertPackageRecord | None:
        ...

    async def create_version(
        self,
        *,
        org_id: int,
        owner_user_id: int,
        command: CreateExpertPackageVersionCommand,
    ) -> ExpertPackageVersionRecord:
        ...


class ExpertPackageService:
    def __init__(self, store: ExpertPackageStore) -> None:
        self.store = store

    async def create_package(
        self,
        *,
        principal: Principal,
        command: CreateExpertPackageCommand,
    ) -> ExpertPackageRecord:
        normalized = CreateExpertPackageCommand(
            slug=validate_slug(command.slug),
            name=validate_name(command.name),
            description=command.description,
            avatar_url=command.avatar_url,
            visibility=validate_visibility(command.visibility),
        )
        return await self.store.create_package(
            org_id=principal.org_id,
            owner_user_id=principal.user_id,
            command=normalized,
        )

    async def create_version(
        self,
        *,
        principal: Principal,
        command: CreateExpertPackageVersionCommand,
    ) -> ExpertPackageVersionRecord:
        package = await self.store.get_package(
            org_id=principal.org_id,
            owner_user_id=principal.user_id,
            expert_package_id=command.expert_package_id,
        )
        if package is None:
            raise ExpertPackageValidationError("expert package not found")
        validate_manifest(command.manifest)
        return await self.store.create_version(
            org_id=principal.org_id,
            owner_user_id=principal.user_id,
            command=command,
        )


def validate_slug(slug: str) -> str:
    value = slug.strip()
    if not value:
        raise ExpertPackageValidationError("slug is required")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_")
    if any(char not in allowed for char in value):
        raise ExpertPackageValidationError("slug must use lowercase letters, numbers, dash, or underscore")
    return value


def validate_name(name: str) -> str:
    value = name.strip()
    if not value:
        raise ExpertPackageValidationError("name is required")
    return value


def validate_visibility(visibility: str) -> str:
    if visibility not in {"private", "organization", "public"}:
        raise ExpertPackageValidationError("visibility must be private, organization, or public")
    return visibility


def validate_manifest(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict) or not manifest:
        raise ExpertPackageValidationError("manifest is required")
    if manifest.get("schema_version") != "expert.package/v1":
        raise ExpertPackageValidationError("manifest schema_version must be expert.package/v1")
    _assert_no_forbidden_manifest_keys(manifest)
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ExpertPackageValidationError("manifest capabilities is required")
    hermes_plugin = capabilities.get("hermes_plugin")
    if not isinstance(hermes_plugin, dict) or not hermes_plugin.get("plugin_id"):
        raise ExpertPackageValidationError("manifest capabilities.hermes_plugin.plugin_id is required")


def _assert_no_forbidden_manifest_keys(value: Any, *, path: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in FORBIDDEN_MANIFEST_KEYS:
                raise ExpertPackageValidationError(f"manifest contains forbidden key: {path}.{key}")
            _assert_no_forbidden_manifest_keys(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_forbidden_manifest_keys(child, path=f"{path}[{index}]")
