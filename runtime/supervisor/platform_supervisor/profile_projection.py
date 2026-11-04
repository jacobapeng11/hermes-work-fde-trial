from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urljoin
from uuid import uuid4
import zipfile

import httpx

from platform_supervisor.asset_contracts import (
    ProfileCapabilityEvidence,
    ProfileCapabilityTarget,
    ProfileExecutionControlRequest,
    ProfileExecutionControlResponse,
    ProfileArtifactManifestEntry,
    ProfileProjectionApplyRequest,
    ProfileProjectionApplyResponse,
)
from platform_supervisor.profile_projection_state import (
    ProfileProjectionStateError,
    ProfileProjectionStateStore,
)
from platform_supervisor.toolset_policy import normalize_platform_cli_toolsets


MAX_PROFILE_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_PROFILE_ARTIFACT_FILES = 2_000
MAX_PROFILE_ARTIFACT_FILE_BYTES = 32 * 1024 * 1024
PLATFORM_BASELINE_PROFILE_PLUGINS = (
    "kanban-lifecycle",
    "clarify-lifecycle",
    "platform-main-conversation-boundary-v1",
)
_REQUIRED_PROFILE_COMPRESSION_FIELDS = (
    "max_attempts",
    "target_ratio",
    "proactive_prune_tokens",
)


def _without_none_mapping_values(value: Any) -> Any:
    """Match Hermes save_config's omission of optional null mapping values."""

    if isinstance(value, dict):
        return {
            key: _without_none_mapping_values(child)
            for key, child in value.items()
            if child is not None
        }
    if isinstance(value, list):
        return [_without_none_mapping_values(child) for child in value]
    return value


def _projected_profile_plugins(
    *,
    enabled: list[Any],
    previous_plugins: list[Any],
    requested_plugins: list[str],
) -> list[str]:
    managed_plugins = set(previous_plugins)
    retained_plugins = [
        item
        for item in enabled
        if item not in managed_plugins
    ]
    return list(
        dict.fromkeys(
            [
                *PLATFORM_BASELINE_PROFILE_PLUGINS,
                *retained_plugins,
                *requested_plugins,
            ]
        )
    )


class ProfileProjectionError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def _required_profile_compression(document: dict[str, Any]) -> dict[str, Any]:
    compression = document.get("compression")
    if not isinstance(compression, dict):
        raise ProfileProjectionError("PROFILE_COMPRESSION_CONFIG_INVALID")
    for field in _REQUIRED_PROFILE_COMPRESSION_FIELDS:
        if field not in compression:
            raise ProfileProjectionError("PROFILE_COMPRESSION_CONFIG_INVALID")
    max_attempts = compression["max_attempts"]
    target_ratio = compression["target_ratio"]
    prune_tokens = compression["proactive_prune_tokens"]
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= 10
        or isinstance(target_ratio, bool)
        or not isinstance(target_ratio, (int, float))
        or not 0.10 <= float(target_ratio) <= 0.80
        or isinstance(prune_tokens, bool)
        or not isinstance(prune_tokens, int)
        or prune_tokens < 0
    ):
        raise ProfileProjectionError("PROFILE_COMPRESSION_CONFIG_INVALID")
    return deepcopy(compression)


class ProfileAssetPending(ProfileProjectionError):
    pass


class ProfileProjectionDashboard(Protocol):
    async def ensure_profile(
        self,
        *,
        profile_name: str,
        description: str,
        soul_content: str,
    ) -> None: ...

    async def read_profile_config(self, profile_name: str) -> dict[str, Any]: ...

    async def write_profile_config(
        self,
        profile_name: str,
        document: dict[str, Any],
    ) -> None: ...


class ProfileArtifactDownloader(Protocol):
    async def download(
        self,
        *,
        request: ProfileProjectionApplyRequest,
        artifact: ProfileArtifactManifestEntry,
    ) -> bytes: ...


class BackendProfileArtifactDownloader:
    def __init__(
        self,
        *,
        backend_base_url: str,
        api_server_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.backend_base_url = backend_base_url.rstrip("/")
        self.api_server_key = api_server_key
        self._client = client
        self._owns_client = client is None

    async def download(
        self,
        *,
        request: ProfileProjectionApplyRequest,
        artifact: ProfileArtifactManifestEntry,
    ) -> bytes:
        path = (
            "/internal/v1/profile-artifacts/"
            f"{request.runtime_agent_profile_id}/{request.desired_revision}/"
            f"{artifact.asset_id}/{artifact.version_id}"
        )
        target = urljoin(f"{self.backend_base_url}/", path.lstrip("/"))
        try:
            response = await self._shared_client().get(
                target,
                headers={
                    "Authorization": f"Bearer {self.api_server_key}",
                    "X-Hermes-Runtime-Id": request.runtime_id,
                    "X-Hermes-Binding-Id": str(request.binding_id),
                    "X-Hermes-Artifact-Sha256": artifact.sha256,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProfileProjectionError(
                "PROFILE_ARTIFACT_DOWNLOAD_FAILED"
            ) from exc
        content = response.content
        if not content or len(content) > MAX_PROFILE_ARTIFACT_BYTES:
            raise ProfileProjectionError("PROFILE_ARTIFACT_SIZE_INVALID")
        return content

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _shared_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(60, connect=5, pool=5),
            )
        return self._client


class ProfileProjectionController:
    """Apply one complete immutable Profile revision inside a Runtime."""

    def __init__(
        self,
        *,
        runtime_id: str,
        binding_id: str,
        home: Path,
        state: ProfileProjectionStateStore,
        dashboard: ProfileProjectionDashboard,
        downloader: ProfileArtifactDownloader,
        config_lock: asyncio.Lock | None = None,
    ) -> None:
        self.runtime_id = runtime_id
        self.binding_id = binding_id
        self.home = home
        self.state = state
        self.dashboard = dashboard
        self.downloader = downloader
        self._config_lock = config_lock or asyncio.Lock()
        self._profile_locks: dict[int, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._applied_sink: Callable[[], Awaitable[None]] | None = None

    def bind_applied_sink(
        self,
        sink: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self._applied_sink = sink

    async def apply(
        self,
        request: ProfileProjectionApplyRequest,
    ) -> ProfileProjectionApplyResponse:
        self._validate_target(request)
        lock = await self._profile_lock(request.runtime_agent_profile_id)
        async with lock:
            try:
                observed = await self.state.begin(request)
            except ProfileProjectionStateError as exc:
                raise ProfileProjectionError(exc.code) from exc
            if observed.status == "applied":
                current = await self.dashboard.read_profile_config(
                    request.hermes_profile_name
                )
                compression = None
                compression_ready = True
                if request.hermes_profile_name != "default":
                    try:
                        default_document = await self.dashboard.read_profile_config(
                            "default"
                        )
                        compression = _required_profile_compression(default_document)
                    except ProfileProjectionError:
                        compression_ready = False
                if (
                    compression_ready
                    and self._config_matches_projection(
                        current,
                        request,
                        compression=compression,
                    )
                    and self._existing_skill_releases_are_ready(request)
                ):
                    self._prune_old_skill_releases()
                    await self._notify_applied()
                    return observed.response()
            try:
                release_paths, capabilities = await self._prepare_artifacts(request)
                async with self._config_lock:
                    document = await self._write_profile_config(
                        request=request,
                        release_paths=release_paths,
                    )
                await self._activate_pointer(
                    request=request,
                    release_paths=release_paths,
                )
                await self._verify(
                    request=request,
                    expected_document=document,
                )
                self._prune_old_skill_releases()
                projection_status = (
                    "applied_degraded"
                    if any(item.status == "unavailable" for item in capabilities)
                    else "applied"
                )
                applied = (
                    await self.state.applied(
                        request,
                        status=projection_status,
                        capabilities=capabilities,
                    )
                ).response()
            except ProfileProjectionError as exc:
                return (
                    await self.state.failed(
                        request,
                        error_code=exc.code,
                        error_message=str(exc),
                    )
                ).response()
            except Exception as exc:
                return (
                    await self.state.failed(
                        request,
                        error_code="PROFILE_PROJECTION_FAILED",
                        error_message=type(exc).__name__,
                    )
                ).response()
            await self._notify_applied()
            return applied

    def _config_matches_projection(
        self,
        document: dict[str, Any],
        request: ProfileProjectionApplyRequest,
        *,
        compression: dict[str, Any] | None = None,
    ) -> bool:
        platform = document.get("platform")
        projection = (
            platform.get("asset_projection")
            if isinstance(platform, dict)
            else None
        )
        if not isinstance(projection, dict):
            return False
        if compression is not None and document.get("compression") != compression:
            return False
        requested_toolsets = request.profile_config.get("platform_toolsets")
        if not isinstance(requested_toolsets, dict):
            return False
        expected_toolsets = deepcopy(requested_toolsets)
        requested_cli = expected_toolsets.get("cli")
        expected_toolsets["cli"] = normalize_platform_cli_toolsets(
            requested_cli if isinstance(requested_cli, list) else ()
        )
        if document.get("platform_toolsets") != expected_toolsets:
            return False
        requested_plugins = request.profile_config.get("plugin_ids", [])
        plugins = document.get("plugins")
        enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
        if not (
            isinstance(requested_plugins, list)
            and all(isinstance(item, str) and item for item in requested_plugins)
            and isinstance(enabled, list)
            and all(isinstance(item, str) for item in enabled)
        ):
            return False
        if enabled != _projected_profile_plugins(
            enabled=enabled,
            previous_plugins=requested_plugins,
            requested_plugins=requested_plugins,
        ):
            return False
        if (
            request.hermes_profile_name != "default"
            and document.get("mcp_servers")
            != request.profile_config.get("mcp_servers")
        ):
            return False
        if not self._managed_skill_directories_match(document, request):
            return False
        return (
            projection.get("desired_revision") == request.desired_revision
            and projection.get("digests")
            == request.digests.model_dump(mode="json")
            and projection.get("definition")
            == _without_none_mapping_values(deepcopy(request.profile_config))
        )

    async def _notify_applied(self) -> None:
        if self._applied_sink is None:
            return
        try:
            await self._applied_sink()
        except Exception as exc:
            raise ProfileProjectionError("PROFILE_RUN_WAKE_FAILED") from exc

    async def status(
        self,
        runtime_agent_profile_id: int,
    ) -> ProfileProjectionApplyResponse:
        if runtime_agent_profile_id <= 0:
            raise ProfileProjectionError("PROFILE_ID_INVALID")
        state = await self.state.get(runtime_agent_profile_id)
        if state is None:
            raise ProfileProjectionError("PROFILE_PROJECTION_NOT_FOUND")
        return state.response()

    async def control(
        self,
        request: ProfileExecutionControlRequest,
    ) -> ProfileExecutionControlResponse:
        if request.runtime_id != self.runtime_id:
            raise ProfileProjectionError("PROFILE_RUNTIME_TARGET_MISMATCH")
        if str(request.binding_id) != self.binding_id:
            raise ProfileProjectionError("PROFILE_BINDING_TARGET_MISMATCH")
        try:
            return await self.state.control(request)
        except ProfileProjectionStateError as exc:
            raise ProfileProjectionError(exc.code) from exc

    async def resolve_identity(self, identity, *, allow_pending: bool = False):
        profile_id = int(identity.runtime_agent_profile_id or 0)
        if profile_id <= 0:
            return identity
        state = await self.state.get(profile_id)
        if state is not None and state.execution_revoked:
            raise ProfileProjectionError("PROFILE_EXECUTION_REVOKED")
        grant_desired_revision = int(identity.profile_desired_revision or 0)
        grant_applied_revision = int(identity.profile_applied_revision or 0)
        empty_default_without_state = (
            state is None
            and identity.profile_id == "default"
            and grant_desired_revision == 0
            and grant_applied_revision == 0
        )
        pending = (
            (state is None and not empty_default_without_state)
            or (
                state is not None
                and (
                    state.applied_revision <= 0
                    or state.applied_digests is None
                    or grant_desired_revision > state.applied_revision
                    or grant_applied_revision > state.applied_revision
                )
            )
        )
        if pending:
            if allow_pending:
                return None
            raise ProfileAssetPending(
                "PROFILE_ASSET_UPDATING",
                "Profile assets are still converging",
            )
        if empty_default_without_state:
            return identity
        if state.hermes_profile_name != identity.profile_id:
            raise ProfileProjectionError("PROFILE_IDENTITY_MISMATCH")
        return replace(
            identity,
            profile_desired_revision=state.desired_revision,
            profile_applied_revision=state.applied_revision,
            profile_digest=state.applied_digests.profile,
            skill_digest=state.applied_digests.skill,
            mcp_digest=state.applied_digests.mcp,
            knowledge_digest=state.applied_digests.knowledge,
        )

    def _validate_target(self, request: ProfileProjectionApplyRequest) -> None:
        if request.runtime_id != self.runtime_id:
            raise ProfileProjectionError("PROFILE_RUNTIME_TARGET_MISMATCH")
        if str(request.binding_id) != self.binding_id:
            raise ProfileProjectionError("PROFILE_BINDING_TARGET_MISMATCH")
        if request.hermes_profile_name not in {
            "default",
            f"expert-{request.runtime_agent_profile_id}",
        }:
            raise ProfileProjectionError("PROFILE_NAME_INVALID")
        targets: set[str] = set()
        for artifact in request.artifacts:
            expected = f"skills/releases/{artifact.asset_id}/current"
            if artifact.target_relative_path != expected or expected in targets:
                raise ProfileProjectionError("PROFILE_ARTIFACT_TARGET_INVALID")
            targets.add(expected)

    async def _profile_lock(self, profile_id: int) -> asyncio.Lock:
        async with self._locks_guard:
            return self._profile_locks.setdefault(profile_id, asyncio.Lock())

    async def _prepare_artifacts(
        self,
        request: ProfileProjectionApplyRequest,
    ) -> tuple[list[Path], tuple[ProfileCapabilityEvidence, ...]]:
        releases: list[Path] = []
        unavailable: dict[tuple[str, int], ProfileProjectionError] = {}
        targets = {
            (item.capability_type, item.capability_id): item
            for item in request.capabilities
        }
        for artifact in request.artifacts:
            try:
                target = self._asset_root / artifact.target_relative_path
                if target.is_dir():
                    if self._directory_sha256(target) != artifact.sha256:
                        raise ProfileProjectionError(
                            "PROFILE_ARTIFACT_RELEASE_CORRUPT"
                        )
                    releases.append(target)
                    continue
                content = await self.downloader.download(
                    request=request,
                    artifact=artifact,
                )
                staging = self._staging_root / (
                    f"{request.operation_id}-{uuid4().hex}"
                )
                try:
                    self._extract_zip(content, staging)
                    if self._directory_sha256(staging) != artifact.sha256:
                        raise ProfileProjectionError(
                            "PROFILE_ARTIFACT_DIGEST_MISMATCH"
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.replace(staging, target)
                    except OSError:
                        if (
                            not target.is_dir()
                            or self._directory_sha256(target) != artifact.sha256
                        ):
                            raise ProfileProjectionError(
                                "PROFILE_ARTIFACT_ATOMIC_PUBLISH_FAILED"
                            )
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
                releases.append(target)
            except ProfileProjectionError as exc:
                capability = targets.get(("skill", int(artifact.asset_id)))
                if capability is None or capability.required:
                    raise
                unavailable[("skill", int(artifact.asset_id))] = exc
        evidence = tuple(
            self._capability_evidence(
                target,
                unavailable.get((target.capability_type, target.capability_id)),
            )
            for target in request.capabilities
        )
        if any(
            target.required and item.status != "applied"
            for target, item in zip(request.capabilities, evidence, strict=True)
        ):
            raise ProfileProjectionError("PROFILE_REQUIRED_CAPABILITY_UNAVAILABLE")
        return releases, evidence

    @staticmethod
    def _capability_evidence(
        target: ProfileCapabilityTarget,
        error: ProfileProjectionError | None,
    ) -> ProfileCapabilityEvidence:
        if error is not None:
            return ProfileCapabilityEvidence(
                capability_type=target.capability_type,
                capability_id=target.capability_id,
                target_version_id=target.target_version_id,
                status="unavailable",
                error_code=error.code,
                error_message=error.code,
                retry_classification=(
                    "transient"
                    if error.code == "PROFILE_ARTIFACT_DOWNLOAD_FAILED"
                    else "deterministic"
                ),
            )
        return ProfileCapabilityEvidence(
            capability_type=target.capability_type,
            capability_id=target.capability_id,
            target_version_id=target.target_version_id,
            status="applied",
            applied_digest=target.desired_digest,
        )

    async def _write_profile_config(
        self,
        *,
        request: ProfileProjectionApplyRequest,
        release_paths: list[Path],
    ) -> dict[str, Any]:
        description = str(request.profile_config.get("description") or "")
        soul_content = str(request.profile_config.get("soul_content") or "")
        await self.dashboard.ensure_profile(
            profile_name=request.hermes_profile_name,
            description=description,
            soul_content=soul_content,
        )
        current = await self.dashboard.read_profile_config(
            request.hermes_profile_name
        )
        document = deepcopy(current)
        # Newly created named Profiles are intentionally bootstrapped with
        # ``clone_all=False``/``no_skills=True``.  Hermes therefore leaves
        # their model route unset even though the shared provider catalog is
        # present.  Preserve the Workspace default's route so a projected
        # expert can prepare a Session; asset projection must not invent a
        # provider or copy credentials, only the non-secret model selector.
        compression = None
        if request.hermes_profile_name != "default":
            default_document = await self.dashboard.read_profile_config("default")
            compression = _required_profile_compression(default_document)
            document["compression"] = deepcopy(compression)
        if request.hermes_profile_name != "default" and "model" not in document:
            default_model = default_document.get("model")
            if isinstance(default_model, (dict, str)):
                document["model"] = deepcopy(default_model)
                # ``model.provider`` commonly uses ``custom:<name>``.  A
                # fresh named Profile has its own relay provider entry, but
                # not the default provider alias referenced by that model
                # selector.  Copy only the provider definition (which holds
                # no secret bytes) so Hermes can resolve the route.
                provider_name = str(
                    default_model.get("provider")
                    if isinstance(default_model, dict)
                    else ""
                )
                provider_name = provider_name.removeprefix("custom:")
                default_providers = default_document.get("providers")
                providers = document.get("providers")
                if (
                    provider_name
                    and isinstance(default_providers, dict)
                    and isinstance(default_providers.get(provider_name), dict)
                ):
                    if not isinstance(providers, dict):
                        providers = {}
                        document["providers"] = providers
                    if provider_name not in providers:
                        providers[provider_name] = deepcopy(
                            default_providers[provider_name]
                        )
        platform = document.get("platform")
        if not isinstance(platform, dict):
            platform = {}
            document["platform"] = platform
        previous_projection = platform.get("asset_projection")
        previous_plugins = (
            previous_projection.get("managed_plugin_ids", [])
            if isinstance(previous_projection, dict)
            else []
        )
        requested_plugins = request.profile_config.get("plugin_ids", [])
        if not (
            isinstance(requested_plugins, list)
            and all(isinstance(item, str) and item for item in requested_plugins)
        ):
            raise ProfileProjectionError("PROFILE_PLUGIN_CONFIG_INVALID")
        plugins = document.get("plugins")
        if not isinstance(plugins, dict):
            plugins = {}
            document["plugins"] = plugins
        enabled = plugins.get("enabled", [])
        if not isinstance(enabled, list) or not all(isinstance(item, str) for item in enabled):
            raise ProfileProjectionError("PROFILE_PLUGIN_CONFIG_INVALID")
        plugins["enabled"] = _projected_profile_plugins(
            enabled=enabled,
            previous_plugins=previous_plugins,
            requested_plugins=requested_plugins,
        )
        existing_skills = document.get("skills")
        had_external_dirs = (
            isinstance(existing_skills, dict)
            and "external_dirs" in existing_skills
        )
        skills = existing_skills
        if not isinstance(skills, dict):
            skills = {}
        external_dirs = skills.get("external_dirs", [])
        if not (
            isinstance(external_dirs, list)
            and all(isinstance(item, str) for item in external_dirs)
        ):
            raise ProfileProjectionError("PROFILE_SKILL_CONFIG_INVALID")
        managed_root = str(self._asset_root / "skills" / "releases")
        preserved = [
            item
            for item in external_dirs
            if not self._is_managed_release_path(item, managed_root)
        ]
        projected_external_dirs = list(
            dict.fromkeys([*preserved, *(str(path) for path in release_paths)])
        )
        if projected_external_dirs or had_external_dirs:
            skills["external_dirs"] = projected_external_dirs
            document["skills"] = skills
        elif skills:
            document["skills"] = skills
        else:
            document.pop("skills", None)
        toolsets = request.profile_config.get("platform_toolsets")
        if toolsets is not None:
            if not isinstance(toolsets, dict):
                raise ProfileProjectionError("PROFILE_TOOLSET_CONFIG_INVALID")
            projected_toolsets = deepcopy(toolsets)
            cli = projected_toolsets.get("cli")
            if cli is not None and not isinstance(cli, list):
                raise ProfileProjectionError("PROFILE_TOOLSET_CONFIG_INVALID")
            projected_toolsets["cli"] = normalize_platform_cli_toolsets(cli or ())
            document["platform_toolsets"] = projected_toolsets
        mcp_servers = request.profile_config.get("mcp_servers")
        if not isinstance(mcp_servers, dict):
            raise ProfileProjectionError("PROFILE_MCP_CONFIG_INVALID")
        if request.hermes_profile_name != "default":
            document["mcp_servers"] = deepcopy(mcp_servers)
            # Profile-scoped MCP connections are discovered lazily by the
            # Hermes dashboard process.  Keep the first agent build inside
            # the same profile's bounded discovery window; the generic
            # interactive default (about 1.5s) is too short for a cold remote
            # MCP handshake and would snapshot an agent without its tools.
            # This is a per-profile timeout and does not trigger a global
            # ``reload.mcp`` or alter the process-global default registry.
            if mcp_servers:
                # Replace the generic interactive default when a named
                # Profile owns MCP connections.  ``setdefault`` is not
                # sufficient because the base config already contains 1.5s.
                document["mcp_discovery_timeout"] = 30
        platform["asset_projection"] = {
            "schema_version": 1,
            "runtime_agent_profile_id": request.runtime_agent_profile_id,
            "desired_revision": request.desired_revision,
            "digests": request.digests.model_dump(mode="json"),
            "managed_plugin_ids": requested_plugins,
            "definition": _without_none_mapping_values(
                deepcopy(request.profile_config)
            ),
        }
        await self.dashboard.write_profile_config(
            request.hermes_profile_name,
            document,
        )
        return document

    async def _activate_pointer(
        self,
        *,
        request: ProfileProjectionApplyRequest,
        release_paths: list[Path],
    ) -> None:
        profile_root = self._asset_root / "profiles" / str(
            request.runtime_agent_profile_id
        )
        profile_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "operation_id": request.operation_id,
            "runtime_agent_profile_id": request.runtime_agent_profile_id,
            "hermes_profile_name": request.hermes_profile_name,
            "desired_revision": request.desired_revision,
            "digests": request.digests.model_dump(mode="json"),
            "release_paths": [str(path) for path in release_paths],
        }
        temporary = profile_root / f".current-{uuid4().hex}.json"
        current = profile_root / "current.json"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, current)
            directory_fd = os.open(profile_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    async def _verify(
        self,
        *,
        request: ProfileProjectionApplyRequest,
        expected_document: dict[str, Any],
    ) -> None:
        observed = await self.dashboard.read_profile_config(
            request.hermes_profile_name
        )
        if observed != expected_document:
            raise ProfileProjectionError("PROFILE_CONFIG_READBACK_MISMATCH")
        current = (
            self._asset_root
            / "profiles"
            / str(request.runtime_agent_profile_id)
            / "current.json"
        )
        try:
            payload = json.loads(current.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileProjectionError("PROFILE_POINTER_READBACK_FAILED") from exc
        if (
            payload.get("operation_id") != request.operation_id
            or payload.get("desired_revision") != request.desired_revision
            or payload.get("digests") != request.digests.model_dump(mode="json")
        ):
            raise ProfileProjectionError("PROFILE_POINTER_READBACK_MISMATCH")

    @property
    def _asset_root(self) -> Path:
        return self.home / "platform" / "profile-assets"

    @property
    def _staging_root(self) -> Path:
        root = self._asset_root / ".staging"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _is_managed_release_path(value: str, managed_root: str) -> bool:
        try:
            return Path(value).is_relative_to(Path(managed_root))
        except (OSError, ValueError):
            return False

    def _managed_skill_directories_match(
        self,
        document: dict[str, Any],
        request: ProfileProjectionApplyRequest,
    ) -> bool:
        skills = document.get("skills")
        external_dirs = skills.get("external_dirs") if isinstance(skills, dict) else []
        if not isinstance(external_dirs, list) or not all(
            isinstance(item, str) for item in external_dirs
        ):
            return False
        managed_root = str(self._asset_root / "skills" / "releases")
        observed = {
            item
            for item in external_dirs
            if self._is_managed_release_path(item, managed_root)
        }
        expected = {
            str(self._asset_root / artifact.target_relative_path)
            for artifact in request.artifacts
        }
        return observed == expected

    def _prune_old_skill_releases(self) -> None:
        """Remove unreferenced managed skill releases after Profile convergence."""
        releases_root = self._asset_root / "skills" / "releases"
        if not releases_root.is_dir():
            return
        referenced = self._referenced_skill_releases(releases_root)
        if referenced is None:
            return
        for asset_root in releases_root.iterdir():
            if (
                not asset_root.is_dir()
                or asset_root.is_symlink()
                or not asset_root.name.isdigit()
            ):
                continue
            for child in asset_root.iterdir():
                if child.is_dir() and not child.is_symlink() and child not in referenced:
                    shutil.rmtree(child, ignore_errors=True)
            try:
                asset_root.rmdir()
            except OSError:
                pass

    @staticmethod
    def _referenced_skill_releases(releases_root: Path) -> set[Path] | None:
        """Read all Profile pointers; None means cleanup must fail closed."""
        profiles_root = releases_root.parent.parent / "profiles"
        if not profiles_root.is_dir():
            return set()
        referenced: set[Path] = set()
        for pointer in profiles_root.glob("*/current.json"):
            try:
                payload = json.loads(pointer.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            release_paths = payload.get("release_paths")
            if not isinstance(release_paths, list) or not all(
                isinstance(item, str) for item in release_paths
            ):
                return None
            for raw_path in release_paths:
                path = Path(raw_path)
                try:
                    relative = path.relative_to(releases_root)
                except ValueError:
                    continue
                if (
                    len(relative.parts) != 2
                    or not relative.parts[0].isdigit()
                    or not (
                        relative.parts[1] == "current"
                        or relative.parts[1].isdigit()
                    )
                ):
                    return None
                referenced.add(path)
        return referenced

    def _existing_skill_releases_are_ready(
        self, request: ProfileProjectionApplyRequest
    ) -> bool:
        """Require every reused current release to exist with its declared digest."""
        for artifact in request.artifacts:
            target = self._asset_root / artifact.target_relative_path
            if not target.is_dir() or self._directory_sha256(target) != artifact.sha256:
                return False
        return True

    @staticmethod
    def _extract_zip(content: bytes, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=False)
        try:
            from io import BytesIO

            with zipfile.ZipFile(BytesIO(content)) as archive:
                files = [item for item in archive.infolist() if not item.is_dir()]
                if not files or len(files) > MAX_PROFILE_ARTIFACT_FILES:
                    raise ProfileProjectionError("PROFILE_ARTIFACT_FILE_COUNT_INVALID")
                total = 0
                for item in files:
                    name = item.filename.replace("\\", "/")
                    parts = name.split("/")
                    mode = item.external_attr >> 16
                    if (
                        not name
                        or name.startswith("/")
                        or any(part in {"", ".", ".."} for part in parts)
                        or (mode & 0o170000) == 0o120000
                        or item.file_size > MAX_PROFILE_ARTIFACT_FILE_BYTES
                    ):
                        raise ProfileProjectionError("PROFILE_ARTIFACT_ENTRY_INVALID")
                    total += item.file_size
                    if total > MAX_PROFILE_ARTIFACT_BYTES:
                        raise ProfileProjectionError("PROFILE_ARTIFACT_SIZE_INVALID")
                    destination = target.joinpath(*parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as source, destination.open("xb") as sink:
                        shutil.copyfileobj(source, sink, length=1024 * 1024)
        except zipfile.BadZipFile as exc:
            raise ProfileProjectionError("PROFILE_ARTIFACT_ARCHIVE_INVALID") from exc
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise

    @staticmethod
    def _directory_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
            if file_path.is_symlink():
                raise ProfileProjectionError("PROFILE_ARTIFACT_SYMLINK_FORBIDDEN")
            digest.update(file_path.relative_to(path).as_posix().encode())
            digest.update(b"\0")
            digest.update(file_path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()


__all__ = [
    "BackendProfileArtifactDownloader",
    "ProfileArtifactDownloader",
    "ProfileProjectionController",
    "ProfileProjectionDashboard",
    "ProfileProjectionError",
    "ProfileAssetPending",
]
