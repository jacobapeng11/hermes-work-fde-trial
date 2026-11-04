from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import secrets
import shutil
from tempfile import TemporaryDirectory
import uuid
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
import yaml

from app.config import settings
from app.database import AsyncSessionLocal
from app.hermes_runtime.constants import PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID
from app.hermes_runtime.dashboard_protocol import (
    HermesDashboardProtocolAdapter,
    RuntimeEndpoint,
)
from app.hermes_runtime.projection import ProfileConfigRenderer
from app.hermes_runtime.providers.knative import (
    KnativeRuntimeProvider,
    KnativeRuntimeSpec,
    KubectlKnativeClient,
)
from app.models import (
    ApplicationAgent,
    ApplicationAgentConfigRevision,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentSkill,
    Mcp,
    McpVersion,
    Skill,
    SkillVersion,
    User,
)
from app.services.plaza.public_catalog.package_parser import (
    normalize_skill_name,
    sha256_directory,
)
from app.services.plaza.user_skill_operations.skill_installer import (
    install_skill_directory,
)
from app.services.runtime_config_bootstrap import (
    LocalRuntimeConfigBootstrapper,
)
from app.services.runtime_commands import (
    RuntimeCommandClient,
    RuntimeCommandError,
    RuntimeCommandTarget,
    load_runtime_command_admission_generation,
)
from app.services.runtime_config_bundle import (
    config_bundle_digest,
    read_config_bundle_files,
    validate_config_bundle_files,
)
from app.services.runtime_infrastructure import build_runtime_protocol_adapters
from app.services.runtime_models import SqlAlchemyRuntimeModelStore
from app.services.runtime_workspace import (
    LocalRuntimeDirectoryProvisioner,
    RuntimeApiKeyCodec,
    RuntimeBindingReadyService,
    RuntimeBindingStatusPatch,
    RuntimeBindingPlan,
)
from app.services.runtime_image_contract import resolve_runtime_image_contract
from app.services.runtime_image_config import (
    RuntimeImageConfig,
    RuntimeImageConfigReader,
    shared_runtime_image_config_store,
)
from app.hermes_runtime.skill_visibility_policy import (
    BUNDLED_HERMES_SKILL_ROOT,
    INITIAL_SKILL_POLICY_CONFIG_KEY,
    READONLY_HERMES_SKILL_ROOT,
    SKILL_POLICY_CONFIG_KEY,
    SKILL_VISIBILITY_POLICY_V1,
    SKILL_VISIBILITY_POLICY_V2,
    SKILL_VISIBILITY_POLICY_V3,
    explicitly_installed_skill_names,
    load_legacy_upstream_skill_names,
    resolve_versioned_skill_visibility,
)
from app.services.runtime_platform_control import (
    RuntimePlatformControlKeyError,
    RuntimePlatformControlKeyMaterial,
    RuntimePlatformControlKeyStore,
    SqlAlchemyRuntimePlatformControlKeyStore,
    bind_persisted_pvc_identity,
    platform_control_secret_name,
)
from app.services.runtime_maintenance import (
    RuntimeMaintenanceError,
    RuntimeMaintenanceService,
)


class ApplicationAgentRuntimeError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 503) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class ApplicationAgentConfigArtifactStore:
    """Immutable per-generation ConfigBundle artifacts outside Hermes home."""

    _SCHEMA_VERSION = 1
    _LEGACY_BASELINE_EPOCH = 1

    @classmethod
    def write(
        cls,
        *,
        runtime_root_host_path: str,
        generation: int,
        baseline_epoch: int = 1,
        files: dict[str, str],
        digest: str,
    ) -> None:
        normalized = validate_config_bundle_files(files)
        observed = config_bundle_digest(normalized)
        if observed != digest:
            raise ApplicationAgentRuntimeError(
                "application_agent_config_artifact_digest_mismatch"
            )
        path = cls._path(
            runtime_root_host_path=runtime_root_host_path,
            generation=generation,
            baseline_epoch=baseline_epoch,
        )
        payload = json.dumps(
            {
                "schema_version": cls._SCHEMA_VERSION,
                "generation": generation,
                "baseline_epoch": baseline_epoch,
                "config_digest": digest,
                "files": normalized,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            existing_files, existing_digest = cls.read(
                runtime_root_host_path=runtime_root_host_path,
                generation=generation,
                baseline_epoch=baseline_epoch,
            )
            if existing_digest != digest or existing_files != normalized:
                raise ApplicationAgentRuntimeError(
                    "application_agent_config_artifact_conflict"
                )
            return
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            temporary.chmod(0o600)
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing_files, existing_digest = cls.read(
                    runtime_root_host_path=runtime_root_host_path,
                    generation=generation,
                    baseline_epoch=baseline_epoch,
                )
                if existing_digest != digest or existing_files != normalized:
                    raise ApplicationAgentRuntimeError(
                        "application_agent_config_artifact_conflict"
                    )
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def read(
        cls,
        *,
        runtime_root_host_path: str,
        generation: int,
        baseline_epoch: int = 1,
    ) -> tuple[dict[str, str], str]:
        path = cls._path(
            runtime_root_host_path=runtime_root_host_path,
            generation=generation,
            baseline_epoch=baseline_epoch,
        )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != cls._SCHEMA_VERSION
                or payload.get("generation") != generation
                or payload.get("baseline_epoch", cls._LEGACY_BASELINE_EPOCH)
                != baseline_epoch
                or not isinstance(payload.get("files"), dict)
                or not isinstance(payload.get("config_digest"), str)
            ):
                raise ValueError("invalid artifact envelope")
            files = validate_config_bundle_files(payload["files"])
            digest = str(payload["config_digest"])
            if config_bundle_digest(files) != digest:
                raise ValueError("invalid artifact digest")
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ApplicationAgentRuntimeError(
                "application_agent_config_artifact_invalid"
            ) from exc
        return files, digest

    @staticmethod
    def _path(
        *,
        runtime_root_host_path: str,
        generation: int,
        baseline_epoch: int = 1,
    ) -> Path:
        if generation <= 0:
            raise ApplicationAgentRuntimeError(
                "application_agent_config_artifact_generation_invalid"
            )
        if baseline_epoch < ApplicationAgentConfigArtifactStore._LEGACY_BASELINE_EPOCH:
            raise ApplicationAgentRuntimeError(
                "application_agent_config_artifact_baseline_invalid"
            )
        root = Path(runtime_root_host_path).resolve(strict=False)
        epoch_suffix = (
            ""
            if baseline_epoch == ApplicationAgentConfigArtifactStore._LEGACY_BASELINE_EPOCH
            else f"-baseline-{baseline_epoch}"
        )
        path = (
            root
            / ".platform-config-bundles"
            / "application"
            / f"generation-{generation}{epoch_suffix}.json"
        ).resolve(strict=False)
        if not path.is_relative_to(root):
            raise ApplicationAgentRuntimeError(
                "application_agent_config_artifact_path_invalid"
            )
        return path


ApplicationAgentRuntimeApplyScope = Literal[
    "availability_only",
    "full",
    "settings_only",
    "skills_only",
]

APPLICATION_AGENT_BASELINE_PLUGINS = (
    "enterprise-knowledge",
    "personal-knowledge",
    PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID,
)
APPLICATION_AGENT_BASELINE_KNOWLEDGE_TOOLS = frozenset(
    {
        "enterprise_knowledge_read",
        "enterprise_sql_context",
        "enterprise_sql_query",
        "enterprise_document_search",
        "personal_knowledge_search",
    }
)
APPLICATION_AGENT_HARD_DISABLED_TOOLSETS = (
    "clarify",
    "cronjob",
    "kanban",
)


def _application_model_entry_id(config: dict[str, Any] | None) -> int | None:
    value = (config or {}).get("model_entry_id")
    if value is None:
        return None
    if isinstance(value, bool):
        raise ApplicationAgentRuntimeError("application_agent_model_invalid")
    try:
        model_entry_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ApplicationAgentRuntimeError("application_agent_model_invalid") from exc
    if model_entry_id <= 0:
        raise ApplicationAgentRuntimeError("application_agent_model_invalid")
    return model_entry_id


@dataclass(frozen=True)
class ApplicationAgentSkillProjection:
    asset_id: int
    version_id: int
    runtime_name: str
    source_directory: Path
    deployment_sha256: str
    display_name: str | None = None
    source_type: str = "market"
    application_skill_id: int | None = None

    @property
    def directory_key(self) -> str:
        if self.source_type == "application_private":
            return f"private-{self.application_skill_id or self.asset_id}"
        return f"market-{self.asset_id}"


@dataclass(frozen=True)
class ApplicationAgentMcpProjection:
    asset_id: int
    slug: str
    name: str
    runtime_config: dict[str, Any]


@dataclass(frozen=True)
class ApplicationAgentCapabilityPlan:
    skills: tuple[ApplicationAgentSkillProjection, ...] = ()
    mcps: tuple[ApplicationAgentMcpProjection, ...] = ()

    def __post_init__(self) -> None:
        normalized_skill_names: set[str] = set()
        for skill in self.skills:
            normalized_name = normalize_skill_name(skill.runtime_name)
            if not normalized_name or normalized_name in normalized_skill_names:
                raise ApplicationAgentRuntimeError("application_agent_skill_name_conflict")
            normalized_skill_names.add(normalized_name)

    @property
    def skill_names(self) -> frozenset[str]:
        return frozenset(item.runtime_name for item in self.skills)

    @property
    def mcp_slugs(self) -> frozenset[str]:
        return frozenset(item.slug for item in self.mcps)

    @property
    def mcp_identifiers(self) -> frozenset[str]:
        return frozenset(
            identifier for item in self.mcps for identifier in (item.slug, item.name) if identifier
        )


@dataclass
class ApplicationAgentProjectionReceipt:
    spec: KnativeRuntimeSpec
    config_original: bytes | None
    soul_original: bytes | None
    contract_original: bytes | None
    skills_target: Path
    skills_backup: Path | None = None
    rolled_back: bool = False
    committed: bool = False

    @property
    def home(self) -> Path:
        return Path(self.spec.runtime_root_host_path) / self.spec.hermes_home_sub_path

    def rollback(self) -> None:
        if self.rolled_back or self.committed:
            return
        if self.skills_target.exists() or self.skills_target.is_symlink():
            if self.skills_target.is_dir() and not self.skills_target.is_symlink():
                shutil.rmtree(self.skills_target)
            else:
                self.skills_target.unlink(missing_ok=True)
        if self.skills_backup is not None and self.skills_backup.exists():
            self.skills_backup.replace(self.skills_target)
        self._restore(self.home / "config.yaml", self.config_original)
        self._restore(self.home / "SOUL.md", self.soul_original)
        self._restore(
            self.home / "platform" / "application-agent.json",
            self.contract_original,
        )
        self.rolled_back = True

    def commit(self) -> None:
        if self.rolled_back or self.committed:
            return
        if self.skills_backup is not None and self.skills_backup.exists():
            shutil.rmtree(self.skills_backup, ignore_errors=True)
        self.committed = True

    def _restore(self, path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
            return
        LocalRuntimeConfigBootstrapper._atomic_replace(
            path,
            content.decode("utf-8"),
            self.spec,
        )


class ApplicationAgentCapabilityResolver:
    """Resolve application configuration into immutable Runtime inputs."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def resolve(
        self,
        *,
        agent: ApplicationAgent,
        config: dict[str, Any] | None = None,
    ) -> ApplicationAgentCapabilityPlan:
        config = config if config is not None else (agent.config or {})
        snapshot = config.get("asset_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        pinned_skills = {
            int(item["asset_id"]): int(item["version_id"])
            for item in snapshot.get("skills", [])
            if isinstance(item, dict)
            and str(item.get("asset_id", "")).isdigit()
            and str(item.get("version_id", "")).isdigit()
        }
        pinned_mcps = {
            int(item["asset_id"]): int(item["version_id"])
            for item in snapshot.get("mcps", [])
            if isinstance(item, dict)
            and str(item.get("asset_id", "")).isdigit()
            and str(item.get("version_id", "")).isdigit()
        }
        pinned_application_skills = {
            int(item["asset_id"]): int(item["version_id"])
            for item in snapshot.get("application_skills", [])
            if isinstance(item, dict)
            and str(item.get("asset_id", "")).isdigit()
            and str(item.get("version_id", "")).isdigit()
        }
        skills: list[ApplicationAgentSkillProjection] = []
        for asset_id in config.get("skill_asset_ids") or []:
            asset = (
                await self.db.execute(
                    select(Skill).where(
                        Skill.id == int(asset_id),
                        Skill.org_id == int(agent.org_id),
                        Skill.asset_scope == "public",
                        Skill.status == "published",
                        Skill.revoked_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if asset is None or asset.current_version_id is None:
                raise ApplicationAgentRuntimeError("application_agent_skill_not_active")
            version = (
                await self.db.execute(
                    select(SkillVersion).where(
                        SkillVersion.id
                        == pinned_skills.get(int(asset.id), int(asset.current_version_id)),
                        SkillVersion.skill_id == int(asset.id),
                        SkillVersion.org_id == int(agent.org_id),
                        SkillVersion.status == "published",
                    )
                )
            ).scalar_one_or_none()
            if version is None:
                raise ApplicationAgentRuntimeError("application_agent_skill_not_active")
            source = Path(str(version.package_uri)).resolve(strict=False)
            if not source.is_dir() or not (source / "SKILL.md").is_file():
                raise ApplicationAgentRuntimeError("application_agent_skill_package_invalid")
            skills.append(
                ApplicationAgentSkillProjection(
                    asset_id=int(asset.id),
                    version_id=int(version.id),
                    runtime_name=(f"application-{int(agent.id)}-{int(asset.id)}"),
                    source_directory=source,
                    deployment_sha256=(
                        str(version.package_sha256)
                        if version.package_sha256
                        else await asyncio.to_thread(
                            sha256_directory,
                            source,
                        )
                    ),
                    display_name=str(asset.name).strip(),
                )
            )

        for skill_id in config.get("application_skill_ids") or []:
            unified_skill = await self.db.scalar(
                select(Skill).where(
                    Skill.id == int(skill_id),
                    Skill.org_id == int(agent.org_id),
                    Skill.application_agent_id == int(agent.id),
                    Skill.asset_scope == "application",
                    Skill.status != "deleted",
                )
            )
            if unified_skill is not None and unified_skill.current_version_id is not None:
                unified_version = await self.db.scalar(
                    select(SkillVersion).where(
                        SkillVersion.id
                        == pinned_application_skills.get(
                            int(unified_skill.id),
                            int(unified_skill.current_version_id),
                        ),
                        SkillVersion.skill_id == int(unified_skill.id),
                        SkillVersion.org_id == int(agent.org_id),
                        SkillVersion.status != "deleted",
                    )
                )
                if unified_version is None:
                    raise ApplicationAgentRuntimeError(
                        "application_agent_private_skill_not_found"
                    )
                source = Path(str(unified_version.package_uri or "")).resolve(
                    strict=False
                )
                managed = (
                    Path(settings.HERMES_APPLICATION_AGENT_RUNTIME_ROOT)
                    .resolve(strict=False)
                    / str(agent.org_id)
                    / str(agent.id)
                    / "uploads"
                    / "skills"
                ).resolve(strict=False)
                if (
                    source == managed
                    or not source.is_relative_to(managed)
                    or not source.is_dir()
                    or not (source / "SKILL.md").is_file()
                ):
                    raise ApplicationAgentRuntimeError(
                        "application_agent_private_skill_package_invalid"
                    )
                observed_sha256 = await asyncio.to_thread(
                    sha256_directory,
                    source,
                )
                if observed_sha256 != str(unified_version.package_sha256 or ""):
                    raise ApplicationAgentRuntimeError(
                        "application_agent_private_skill_package_invalid"
                    )
                manifest = dict(unified_version.manifest or {})
                runtime_name = str(manifest.get("slug") or "").strip()
                if not runtime_name:
                    raise ApplicationAgentRuntimeError(
                        "application_agent_private_skill_package_invalid"
                    )
                skills.append(
                    ApplicationAgentSkillProjection(
                        asset_id=int(unified_skill.id),
                        version_id=int(unified_version.id),
                        runtime_name=runtime_name,
                        source_directory=source,
                        deployment_sha256=observed_sha256,
                        display_name=(
                            str(unified_skill.name).strip() or runtime_name
                        ),
                        source_type="application_private",
                        application_skill_id=int(unified_skill.id),
                    )
                )
                continue
            private_skill = (
                await self.db.execute(
                    select(ApplicationAgentSkill).where(
                        ApplicationAgentSkill.id == int(skill_id),
                        ApplicationAgentSkill.org_id == int(agent.org_id),
                        ApplicationAgentSkill.application_agent_id == int(agent.id),
                    )
                )
            ).scalar_one_or_none()
            if private_skill is None:
                raise ApplicationAgentRuntimeError("application_agent_private_skill_not_found")
            expected = (
                Path(settings.HERMES_APPLICATION_AGENT_RUNTIME_ROOT).resolve(strict=False)
                / str(agent.org_id)
                / str(agent.id)
                / "uploads"
                / "skills"
                / str(private_skill.id)
            ).resolve(strict=False)
            source = Path(str(private_skill.package_uri)).resolve(strict=False)
            if source != expected or not source.is_dir() or not (source / "SKILL.md").is_file():
                raise ApplicationAgentRuntimeError(
                    "application_agent_private_skill_package_invalid"
                )
            observed_sha256 = await asyncio.to_thread(
                sha256_directory,
                source,
            )
            if observed_sha256 != str(private_skill.package_sha256):
                raise ApplicationAgentRuntimeError(
                    "application_agent_private_skill_package_invalid"
                )
            skills.append(
                ApplicationAgentSkillProjection(
                    asset_id=int(private_skill.id),
                    version_id=int(private_skill.id),
                    runtime_name=str(private_skill.slug).strip(),
                    source_directory=source,
                    deployment_sha256=observed_sha256,
                    display_name=(
                        str(private_skill.name).strip() or str(private_skill.slug).strip()
                    ),
                    source_type="application_private",
                    application_skill_id=int(private_skill.id),
                )
            )

        mcps: list[ApplicationAgentMcpProjection] = []
        normalized_slugs: set[str] = set()
        for asset_id in config.get("mcp_asset_ids") or []:
            asset = (
                await self.db.execute(
                    select(Mcp).where(
                        Mcp.id == int(asset_id),
                        Mcp.org_id == int(agent.org_id),
                        Mcp.visibility != "private",
                        Mcp.status == "published",
                        Mcp.revoked_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if asset is None or asset.current_version_id is None:
                raise ApplicationAgentRuntimeError("application_agent_mcp_not_active")
            version = await self.db.scalar(
                select(McpVersion).where(
                    McpVersion.id
                    == pinned_mcps.get(int(asset.id), int(asset.current_version_id)),
                    McpVersion.mcp_id == int(asset.id),
                    McpVersion.org_id == int(agent.org_id),
                    McpVersion.status == "published",
                )
            )
            if version is None:
                raise ApplicationAgentRuntimeError("application_agent_mcp_not_active")
            slug = str(asset.slug).strip()
            if not slug or slug in normalized_slugs:
                raise ApplicationAgentRuntimeError("application_agent_mcp_config_invalid")
            runtime_config = copy.deepcopy(version.connection_config)
            runtime_config["transport"] = str(version.transport)
            try:
                ProfileConfigRenderer.validate_mcp_entry(
                    runtime_config,
                    path=f"application_agent.mcp_servers.{slug}",
                )
            except ValueError as exc:
                raise ApplicationAgentRuntimeError("application_agent_mcp_config_invalid") from exc
            normalized_slugs.add(slug)
            mcps.append(
                ApplicationAgentMcpProjection(
                    asset_id=int(asset.id),
                    slug=slug,
                    name=str(asset.name),
                    runtime_config=runtime_config,
                )
            )

        return ApplicationAgentCapabilityPlan(
            skills=tuple(skills),
            mcps=tuple(mcps),
        )


class ApplicationAgentRuntimeManager(Protocol):
    async def ensure_runtime(
        self,
        *,
        org_id: int,
        agent_id: int,
        apply_scope: ApplicationAgentRuntimeApplyScope = "full",
    ) -> None: ...

    async def ensure_runtime_available(
        self,
        *,
        org_id: int,
        agent_id: int,
    ) -> None: ...

    async def delete_runtime(
        self,
        *,
        org_id: int,
        agent_id: int,
    ) -> None: ...


class NoopApplicationAgentRuntimeManager:
    async def ensure_runtime(
        self,
        *,
        org_id: int,
        agent_id: int,
        apply_scope: ApplicationAgentRuntimeApplyScope = "full",
    ) -> None:
        del org_id, agent_id, apply_scope

    async def ensure_runtime_available(
        self,
        *,
        org_id: int,
        agent_id: int,
    ) -> None:
        del org_id, agent_id

    async def delete_runtime(
        self,
        *,
        org_id: int,
        agent_id: int,
    ) -> None:
        del org_id, agent_id


class SqlAlchemyApplicationAgentBindingStatusStore:
    def __init__(
        self,
        db: AsyncSession,
        *,
        agent_id: int,
        expected_config_generation: int,
    ) -> None:
        self.db = db
        self.agent_id = agent_id
        self.expected_config_generation = expected_config_generation

    async def update_binding_status(
        self,
        patch: RuntimeBindingStatusPatch,
    ) -> None:
        binding = (
            await self.db.execute(
                select(ApplicationAgentRuntimeBinding).where(
                    ApplicationAgentRuntimeBinding.application_agent_id == self.agent_id
                )
            )
        ).scalar_one()
        agent = (
            await self.db.execute(
                select(ApplicationAgent).where(ApplicationAgent.id == self.agent_id)
            )
        ).scalar_one()
        binding.endpoint_base_url = patch.endpoint_base_url
        binding.endpoint_host_header = patch.endpoint_host_header
        binding.status = patch.status
        binding.last_error_code = (
            "application_agent_runtime_reconcile_failed" if patch.status == "failed" else None
        )
        generation = self.expected_config_generation
        if patch.status == "ready":
            binding.config_generation = max(
                int(binding.config_generation or 0),
                generation,
            )
            agent.applied_generation = max(
                int(agent.applied_generation or 0),
                generation,
            )
            if int(agent.config_generation) == generation:
                agent.apply_status = "applied"
                agent.last_apply_error_code = None
        elif patch.status == "failed":
            if int(agent.config_generation) == generation:
                agent.apply_status = "failed"
                agent.last_apply_error_code = binding.last_error_code
        revision = (
            await self.db.execute(
                select(ApplicationAgentConfigRevision).where(
                    ApplicationAgentConfigRevision.application_agent_id == self.agent_id,
                    ApplicationAgentConfigRevision.generation == generation,
                )
            )
        ).scalar_one_or_none()
        if revision is not None:
            revision.apply_status = (
                "applied" if patch.status == "ready" else "failed"
            )
            revision.error_code = (
                None if patch.status == "ready" else binding.last_error_code
            )
            revision.applied_at = (
                datetime.utcnow() if patch.status == "ready" else None
            )
        await self.db.commit()

    async def bind_pvc_identity(
        self,
        *,
        runtime_workspace_id: int,
        pvc_uid: str,
        pvc_storage_identity_hash: str,
    ) -> None:
        if runtime_workspace_id != self.agent_id:
            raise ApplicationAgentRuntimeError(
                "application_agent_runtime_binding_identity_mismatch"
            )
        binding = (
            await self.db.execute(
                select(ApplicationAgentRuntimeBinding)
                .where(ApplicationAgentRuntimeBinding.application_agent_id == self.agent_id)
                .with_for_update()
            )
        ).scalar_one()
        try:
            bind_persisted_pvc_identity(
                binding,
                pvc_uid=pvc_uid,
                pvc_storage_identity_hash=pvc_storage_identity_hash,
            )
        except RuntimePlatformControlKeyError as exc:
            raise ApplicationAgentRuntimeError(
                "application_agent_runtime_pvc_identity_mismatch"
            ) from exc
        await self.db.commit()


class ApplicationAgentConfigBootstrapper:
    def __init__(
        self,
        base: LocalRuntimeConfigBootstrapper,
    ) -> None:
        self.base = base

    async def ensure_config(
        self,
        spec: KnativeRuntimeSpec,
        *,
        agent: ApplicationAgent,
        capabilities: ApplicationAgentCapabilityPlan | None = None,
    ) -> ApplicationAgentProjectionReceipt:
        plan = capabilities or ApplicationAgentCapabilityPlan()
        home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
        receipt = ApplicationAgentProjectionReceipt(
            spec=spec,
            config_original=self._read_optional(home / "config.yaml"),
            soul_original=self._read_optional(home / "SOUL.md"),
            contract_original=self._read_optional(home / "platform" / "application-agent.json"),
            skills_target=home / "skills" / "application",
        )
        try:
            await self.base.ensure_config(spec)
            await asyncio.to_thread(
                self._write_agent_contract,
                spec,
                agent,
                plan,
                receipt,
            )
            await asyncio.to_thread(
                self.verify_skill_projection,
                spec=spec,
                capabilities=plan,
            )
        except Exception:
            await asyncio.to_thread(receipt.rollback)
            raise
        return receipt

    async def render_bundle(
        self,
        spec: KnativeRuntimeSpec,
        *,
        agent: ApplicationAgent,
        capabilities: ApplicationAgentCapabilityPlan | None = None,
    ) -> tuple[dict[str, str], str]:
        """Render a deterministic bundle without touching the Runtime PVC."""
        with TemporaryDirectory(prefix="hermes-application-config-") as root:
            render_spec = replace(
                spec,
                runtime_root_host_path=root,
                runtime_user_uid=os.getuid(),
                runtime_shared_gid=os.getgid(),
            )
            receipt = await self.ensure_config(
                render_spec,
                agent=agent,
                capabilities=capabilities,
            )
            receipt.commit()
            files = read_config_bundle_files(Path(root) / render_spec.hermes_home_sub_path)
        return files, config_bundle_digest(files)

    @staticmethod
    def _read_optional(path: Path) -> bytes | None:
        return path.read_bytes() if path.is_file() else None

    @staticmethod
    def _write_agent_contract(
        spec: KnativeRuntimeSpec,
        agent: ApplicationAgent,
        capabilities: ApplicationAgentCapabilityPlan,
        receipt: ApplicationAgentProjectionReceipt,
    ) -> None:
        home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
        prompt = str((agent.config or {}).get("system_prompt") or "").strip()
        model_entry_id = _application_model_entry_id(agent.config)
        soul = (
            "# 应用 Agent\n\n"
            "你是由企业平台发布的应用 Agent。只执行当前应用配置中授权的"
            " Skill、MCP 和知识库能力；不得切换为个人 Agent，也不得跨应用"
            "读取会话或长期记忆。\n"
        )
        if prompt:
            soul += f"\n## 应用指令\n\n{prompt}\n"
        contract = {
            "application_agent_id": int(agent.id),
            "slug": str(agent.slug),
            "config_generation": int(agent.config_generation),
            "model_entry_id": model_entry_id,
            "skill_asset_ids": list((agent.config or {}).get("skill_asset_ids") or []),
            "application_skill_ids": list((agent.config or {}).get("application_skill_ids") or []),
            "skills": [
                {
                    "asset_id": skill.asset_id,
                    "version_id": skill.version_id,
                    "source_type": skill.source_type,
                    "application_skill_id": skill.application_skill_id,
                    "display_name": (skill.display_name or skill.runtime_name),
                    "runtime_name": skill.runtime_name,
                    "deployment_sha256": skill.deployment_sha256,
                }
                for skill in capabilities.skills
            ],
            "mcp_asset_ids": list((agent.config or {}).get("mcp_asset_ids") or []),
            "mcps": [
                {
                    "asset_id": mcp.asset_id,
                    "slug": mcp.slug,
                    "name": mcp.name,
                }
                for mcp in capabilities.mcps
            ],
            "personal_knowledge_binding_digest": str(
                agent.personal_knowledge_binding_digest
            ),
            "enterprise_knowledge_enabled": bool(
                (agent.config or {}).get(
                    "enterprise_knowledge_enabled",
                    False,
                )
            ),
            "memory_enabled": bool((agent.config or {}).get("memory_enabled", True)),
            "capability_projection": {
                "version": 1,
                "config_generation": int(agent.config_generation),
            },
        }
        platform_dir = home / "platform"
        platform_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = home / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        runtime_logs = tuple(
            logs_dir / name
            for name in (
                "agent.log",
                "errors.log",
                "dashboard-auth.log",
                "gui.log",
            )
        )
        for log_path in runtime_logs:
            # The image starts root-supervised sidecars before the Dashboard
            # drops to the Hermes user. Pre-creating the shared log files
            # prevents that startup race from leaving root-owned 0644 files
            # that make session resume/prewarm fail.
            log_path.touch(exist_ok=True)
        config_path = home / "config.yaml"
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ApplicationAgentRuntimeError("application_agent_runtime_config_invalid")
        runtime_agent = document.get("agent")
        if runtime_agent is None:
            runtime_agent = {}
            document["agent"] = runtime_agent
        if not isinstance(runtime_agent, dict):
            raise ApplicationAgentRuntimeError("application_agent_runtime_config_invalid")
        runtime_agent.pop("system_prompt", None)
        disabled_toolsets = runtime_agent.get("disabled_toolsets", [])
        if not (
            isinstance(disabled_toolsets, list)
            and all(isinstance(item, str) and item.strip() for item in disabled_toolsets)
        ):
            raise ApplicationAgentRuntimeError("application_agent_runtime_config_invalid")
        runtime_agent["disabled_toolsets"] = list(
            dict.fromkeys(
                [*disabled_toolsets, *APPLICATION_AGENT_HARD_DISABLED_TOOLSETS]
            )
        )
        if model_entry_id is not None:
            model = document.get("model")
            providers = document.get("providers")
            provider = providers.get("platform") if isinstance(providers, dict) else None
            models = provider.get("models") if isinstance(provider, dict) else None
            if (
                not isinstance(model, dict)
                or not isinstance(provider, dict)
                or not isinstance(models, dict)
                or (str(model_entry_id) not in models and model_entry_id not in models)
            ):
                raise ApplicationAgentRuntimeError("application_agent_model_not_visible")
            model["default"] = str(model_entry_id)
            model["default_model_entry_id"] = model_entry_id
            provider["default_model"] = str(model_entry_id)
        platform = document.setdefault("platform", {})
        if not isinstance(platform, dict):
            raise ApplicationAgentRuntimeError("application_agent_runtime_config_invalid")
        platform["runtime_workspace_id"] = int(agent.id)
        platform["runtime_id"] = str(spec.runtime_id)
        platform["config_generation"] = int(agent.config_generation)
        policy_version = (agent.config or {}).get(SKILL_POLICY_CONFIG_KEY)
        if policy_version not in {
            SKILL_VISIBILITY_POLICY_V1,
            SKILL_VISIBILITY_POLICY_V2,
            SKILL_VISIBILITY_POLICY_V3,
        }:
            policy_version = (agent.config or {}).get(INITIAL_SKILL_POLICY_CONFIG_KEY)
        if policy_version not in {
            SKILL_VISIBILITY_POLICY_V1,
            SKILL_VISIBILITY_POLICY_V2,
            SKILL_VISIBILITY_POLICY_V3,
        }:
            # Existing Applications created before the one-time marker keep
            # the target-config policy they already had; newly created rows
            # persist INITIAL_DEFAULT_SKILL_POLICY_VERSION at creation time.
            policy_version = SKILL_VISIBILITY_POLICY_V3
        skills = document.get("skills")
        if skills is None:
            skills = {}
            document["skills"] = skills
        if not isinstance(skills, dict):
            raise ApplicationAgentRuntimeError("application_agent_runtime_config_invalid")
        external_dirs = skills.get("external_dirs")
        if not isinstance(external_dirs, list):
            external_dirs = []
        if policy_version == SKILL_VISIBILITY_POLICY_V3:
            skills["external_dirs"] = list(
                dict.fromkeys(
                    [
                        *[str(item) for item in external_dirs if isinstance(item, str)],
                        BUNDLED_HERMES_SKILL_ROOT,
                        READONLY_HERMES_SKILL_ROOT,
                    ]
                )
            )
        selected_skill_names = explicitly_installed_skill_names(
            {
                "runtime_skill_name": item.runtime_name,
                "name": item.display_name,
            }
            for item in capabilities.skills
        )
        visibility = resolve_versioned_skill_visibility(
            policy_version=policy_version,
            legacy_upstream_names=load_legacy_upstream_skill_names(),
            explicitly_installed_names=selected_skill_names,
            user_disabled_names=(),
        )
        skills["disabled"] = sorted(visibility.effective_disabled)
        platform["skill_visibility"] = {
            "policy_version": policy_version,
            "initial_policy_version": policy_version,
            "platform_disabled": sorted(visibility.platform_disabled),
        }
        document["memory"] = {
            # Native Hermes memory is PVC-global. Application Agent identity
            # is per active Run, so platform-scoped conversations/memory must
            # remain authoritative instead of sharing creator/user memory.
            "memory_enabled": False,
            "user_profile_enabled": False,
        }
        # Knowledge integrations and the authenticated main-conversation
        # boundary are application Runtime baseline capabilities. Selection
        # and authorization remain dynamic in the platform routes.
        document["plugins"] = {"enabled": list(APPLICATION_AGENT_BASELINE_PLUGINS)}
        document["mcp_servers"] = {
            mcp.slug: copy.deepcopy(mcp.runtime_config) for mcp in capabilities.mcps
        }
        document["toolsets"] = [
            item
            for item in (document.get("toolsets") or [])
            if item not in APPLICATION_AGENT_HARD_DISABLED_TOOLSETS
        ]
        ApplicationAgentConfigBootstrapper._publish_skills(
            home=home,
            capabilities=capabilities,
            receipt=receipt,
        )
        for root, directories, files in os.walk(receipt.skills_target):
            os.chown(
                root,
                spec.runtime_user_uid,
                spec.runtime_shared_gid,
            )
            for name in [*directories, *files]:
                os.chown(
                    Path(root) / name,
                    spec.runtime_user_uid,
                    spec.runtime_shared_gid,
                )
        LocalRuntimeConfigBootstrapper._atomic_replace(
            config_path,
            yaml.safe_dump(
                document,
                allow_unicode=True,
                sort_keys=False,
            ),
            spec,
        )
        for user_memory in (
            home / "memories" / "USER.md",
            home / "memories" / "MEMORY.md",
        ):
            if user_memory.exists():
                user_memory.unlink()
        LocalRuntimeConfigBootstrapper._atomic_replace(
            home / "SOUL.md",
            soul,
            spec,
        )
        LocalRuntimeConfigBootstrapper._atomic_replace(
            platform_dir / "application-agent.json",
            json.dumps(
                contract,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            spec,
        )
        for path in (
            home / "SOUL.md",
            config_path,
            platform_dir,
            platform_dir / "application-agent.json",
            logs_dir,
            *runtime_logs,
        ):
            os.chown(
                path,
                spec.runtime_user_uid,
                spec.runtime_shared_gid,
            )
        platform_dir.chmod(0o770)
        logs_dir.chmod(0o770)
        for log_path in runtime_logs:
            log_path.chmod(0o660)
        (home / "SOUL.md").chmod(0o660)
        (platform_dir / "application-agent.json").chmod(0o660)

    @staticmethod
    def _publish_skills(
        *,
        home: Path,
        capabilities: ApplicationAgentCapabilityPlan,
        receipt: ApplicationAgentProjectionReceipt,
    ) -> None:
        skills_root = home / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        staging_name = f".application.staging-{uuid.uuid4().hex}"
        staging = skills_root / staging_name
        staging.mkdir()
        try:
            for skill in capabilities.skills:
                install_skill_directory(
                    source_directory=skill.source_directory,
                    skills_root=skills_root,
                    category_key=staging_name,
                    directory_key=skill.directory_key,
                    runtime_skill_name=skill.runtime_name,
                )
            target = receipt.skills_target
            if target.exists() or target.is_symlink():
                backup = target.with_name(f".application.backup-{uuid.uuid4().hex}")
                target.replace(backup)
                receipt.skills_backup = backup
            staging.replace(target)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    @staticmethod
    def contract_is_current(
        *,
        runtime_root_host_path: str,
        hermes_home_sub_path: str,
        config_generation: int,
        model_entry_id: int | None,
    ) -> bool:
        path = (
            Path(runtime_root_host_path)
            / hermes_home_sub_path
            / "platform"
            / "application-agent.json"
        )
        try:
            contract = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        projection = contract.get("capability_projection") if isinstance(contract, dict) else None
        return (
            isinstance(projection, dict)
            and projection.get("version") == 1
            and projection.get("config_generation") == int(config_generation)
            and (
                (contract.get("model_entry_id") == int(model_entry_id))
                if model_entry_id is not None
                else contract.get("model_entry_id") is None
            )
        )

    @staticmethod
    def verify_skill_projection(
        *,
        spec: KnativeRuntimeSpec,
        capabilities: ApplicationAgentCapabilityPlan,
    ) -> None:
        home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
        skills_target = home / "skills" / "application"
        expected_directories = {item.directory_key for item in capabilities.skills}
        observed_directories = (
            {
                item.name
                for item in skills_target.iterdir()
                if item.is_dir() and not item.is_symlink()
            }
            if skills_target.is_dir()
            else set()
        )
        if observed_directories != expected_directories or any(
            not (skills_target / item.directory_key / "SKILL.md").is_file()
            for item in capabilities.skills
        ):
            raise ApplicationAgentRuntimeError("application_agent_skill_projection_failed")
        try:
            contract = json.loads(
                (home / "platform" / "application-agent.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ApplicationAgentRuntimeError("application_agent_skill_projection_failed") from exc
        if not isinstance(contract, dict):
            raise ApplicationAgentRuntimeError("application_agent_skill_projection_failed")
        observed_names = frozenset(
            str(item.get("runtime_name") or "").strip()
            for item in contract.get("skills") or []
            if isinstance(item, dict) and str(item.get("runtime_name") or "").strip()
        )
        if observed_names != capabilities.skill_names:
            raise ApplicationAgentRuntimeError("application_agent_skill_projection_failed")

    @staticmethod
    async def _reload_skills(
        *,
        adapter: HermesDashboardProtocolAdapter,
        endpoint: RuntimeEndpoint,
    ) -> None:
        await adapter.reload_skills(endpoint)

    @staticmethod
    async def reload_skills_and_verify(
        *,
        adapter: HermesDashboardProtocolAdapter,
        endpoint: RuntimeEndpoint,
        capabilities: ApplicationAgentCapabilityPlan,
    ) -> None:
        await ApplicationAgentConfigBootstrapper._reload_skills(
            adapter=adapter,
            endpoint=endpoint,
        )
        observed = await adapter.list_skills(endpoint)
        observed_names = {
            str(item.get("name") or item.get("skill_name") or item.get("id") or "")
            .strip()
            .casefold()
            for item in observed
            if isinstance(item, dict)
            and str(item.get("name") or item.get("skill_name") or item.get("id") or "").strip()
        }
        expected_names = {item.casefold() for item in capabilities.skill_names}
        if expected_names - observed_names:
            raise ApplicationAgentRuntimeError("application_agent_skill_reload_failed")

    @staticmethod
    async def reload_and_verify(
        *,
        adapter: HermesDashboardProtocolAdapter,
        endpoint: RuntimeEndpoint,
        capabilities: ApplicationAgentCapabilityPlan,
    ) -> None:
        await ApplicationAgentConfigBootstrapper._reload_skills(
            adapter=adapter,
            endpoint=endpoint,
        )
        await adapter.reload_mcp(endpoint)
        observed = await adapter.capabilities(endpoint)
        skill_names = {
            str(item.get("name") or item.get("skill_name") or "").strip()
            for item in observed.get("skills") or []
            if isinstance(item, dict)
        }
        missing_skills = sorted(capabilities.skill_names - skill_names)
        if missing_skills:
            raise ApplicationAgentRuntimeError("application_agent_skill_reload_failed")
        mcp_identifiers = {
            str(item.get(field) or "").strip()
            for item in observed.get("mcp_servers") or []
            if isinstance(item, dict)
            for field in ("slug", "id", "name")
            if str(item.get(field) or "").strip()
        }
        missing_mcps = [
            item.slug
            for item in capabilities.mcps
            if not {item.slug, item.name}.intersection(mcp_identifiers)
        ]
        if missing_mcps:
            raise ApplicationAgentRuntimeError("application_agent_mcp_reload_failed")
        tool_names = {
            str(item.get("name") or "").strip()
            for item in observed.get("resolved_tools") or []
            if isinstance(item, dict)
        }
        if APPLICATION_AGENT_BASELINE_KNOWLEDGE_TOOLS - tool_names:
            raise ApplicationAgentRuntimeError("application_agent_knowledge_plugin_reload_failed")


class SqlAlchemyApplicationAgentRuntimeManager:
    def __init__(
        self,
        db: AsyncSession,
        *,
        provider: KnativeRuntimeProvider | None = None,
        k8s_client: KubectlKnativeClient | None = None,
        directory_provisioner: LocalRuntimeDirectoryProvisioner | None = None,
        codec: RuntimeApiKeyCodec | None = None,
        runtime_image_config: RuntimeImageConfigReader | None = None,
        repository_root: Path | None = None,
        platform_control_key_store: RuntimePlatformControlKeyStore | None = None,
        maintenance_service: RuntimeMaintenanceService | None = None,
    ) -> None:
        self.db = db
        self.provider = provider or KnativeRuntimeProvider()
        self.k8s_client = k8s_client or KubectlKnativeClient(kubeconfig=settings.KUBECONFIG or None)
        self.directory_provisioner = directory_provisioner or LocalRuntimeDirectoryProvisioner()
        self.codec = codec or RuntimeApiKeyCodec()
        self.runtime_image_config = runtime_image_config or shared_runtime_image_config_store()
        self.repository_root = repository_root or Path(__file__).resolve().parents[3]
        self.platform_control_key_store = (
            platform_control_key_store or SqlAlchemyRuntimePlatformControlKeyStore(self.db)
        )
        self.maintenance_service = maintenance_service or RuntimeMaintenanceService(db)

    async def _load_or_create_config_artifact(
        self,
        *,
        runtime_root_host_path: str,
        generation: int,
        baseline_epoch: int,
        spec: KnativeRuntimeSpec,
        agent: ApplicationAgent,
    ) -> tuple[dict[str, str], str]:
        try:
            return await asyncio.to_thread(
                ApplicationAgentConfigArtifactStore.read,
                runtime_root_host_path=runtime_root_host_path,
                generation=generation,
                baseline_epoch=baseline_epoch,
            )
        except FileNotFoundError:
            capabilities = await ApplicationAgentCapabilityResolver(self.db).resolve(
                agent=agent
            )
            files, digest = await ApplicationAgentConfigBootstrapper(
                LocalRuntimeConfigBootstrapper(
                    model_store=SqlAlchemyRuntimeModelStore(self.db),
                    kanban_orchestrator_enabled=False,
                )
            ).render_bundle(
                spec,
                agent=agent,
                capabilities=capabilities,
            )
            await asyncio.to_thread(
                ApplicationAgentConfigArtifactStore.write,
                runtime_root_host_path=runtime_root_host_path,
                generation=generation,
                baseline_epoch=baseline_epoch,
                files=files,
                digest=digest,
            )
            return files, digest

    async def _desired_runtime_contract(
        self,
        *,
        binding: ApplicationAgentRuntimeBinding | None,
    ) -> tuple[str, str | None, str, int, int]:
        if binding is not None and str(getattr(binding, "image", "")).strip():
            image = str(binding.image)
            image_digest = (
                str(binding.image_digest) if getattr(binding, "image_digest", None) else None
            )
        else:
            configured = self.runtime_image_config.read()
            image = configured.image
            image_digest = configured.config_id
        contract = resolve_runtime_image_contract(
            repository_root=self.repository_root,
            image_reference=image,
        )
        return (
            image,
            image_digest,
            contract.upstream_commit,
            contract.state_schema,
            contract.config_schema,
        )

    @staticmethod
    def _binding_is_current(
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
    ) -> bool:
        return (
            binding.status == "ready"
            and bool(binding.runtime_root_pvc_uid)
            and bool(binding.pvc_storage_identity_hash)
            and agent.apply_status == "applied"
            and int(binding.config_generation) == int(agent.config_generation)
            and ApplicationAgentConfigBootstrapper.contract_is_current(
                runtime_root_host_path=str(binding.runtime_root_host_path),
                hermes_home_sub_path="home",
                config_generation=int(agent.config_generation),
                model_entry_id=_application_model_entry_id(agent.config),
            )
        )

    async def _binding_is_reusable(
        self,
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
    ) -> bool:
        if not self._binding_is_current(agent=agent, binding=binding):
            return False
        _exists, ready, _template_matches = await self._kservice_generation_status(
            namespace=str(binding.k8s_namespace),
            name=str(binding.knative_service),
            expected_generation=int(
                getattr(
                    agent,
                    "config_generation",
                    getattr(binding, "config_generation", 0),
                )
            ),
        )
        return ready

    async def _binding_is_available(
        self,
        *,
        binding: ApplicationAgentRuntimeBinding,
    ) -> bool:
        if not (
            binding.status == "ready"
            and bool(binding.runtime_root_pvc_uid)
            and bool(binding.pvc_storage_identity_hash)
            and bool(binding.endpoint_base_url)
            and bool(binding.endpoint_host_header)
        ):
            return False
        _exists, ready, _template_matches = await self._kservice_generation_status(
            namespace=str(binding.k8s_namespace),
            name=str(binding.knative_service),
            expected_generation=int(binding.config_generation or 0),
        )
        return ready

    async def _kservice_generation_status(
        self,
        *,
        namespace: str,
        name: str,
        expected_generation: int,
    ) -> tuple[bool | None, bool, bool | None]:
        """Return ``(exists, ready, template_matches)`` for a KService.

        Binding rows are durable Desired/Applied metadata, but they can lag
        Kubernetes while a Pod is being replaced.  Reusing a binding only
        after checking the live KService prevents an old template generation
        from becoming the next Pod's initial ConfigBundle.
        """

        kservice_exists_probe = getattr(self.k8s_client, "kservice_exists", None)
        if not callable(kservice_exists_probe):
            return True, True, True
        try:
            exists = await kservice_exists_probe(
                namespace=namespace,
                name=name,
            )
        except Exception:
            return None, False, None
        if not exists:
            return False, False, False
        get_kservice = getattr(self.k8s_client, "get_kservice", None)
        # Minimal test doubles and alternative providers may only expose an
        # existence probe. Preserve their historical behavior while the real
        # kubectl client performs the stronger generation/readiness check.
        if not callable(get_kservice):
            return True, True, True
        try:
            payload = await get_kservice(namespace=namespace, name=name)
        except Exception:
            return True, False, None
        if not isinstance(payload, dict):
            return True, False, None
        template_revision = self._kservice_template_config_revision(payload)
        template_matches = (
            str(template_revision) == f"generation-{int(expected_generation)}"
            if template_revision is not None
            else None
        )
        status = payload.get("status")
        if not isinstance(status, dict):
            return True, False, template_matches
        conditions = status.get("conditions")
        ready_condition = next(
            (
                condition
                for condition in conditions or []
                if isinstance(condition, dict)
                and condition.get("type") == "Ready"
            ),
            None,
        )
        if not isinstance(ready_condition, dict) or str(
            ready_condition.get("status")
        ).lower() != "true":
            return True, False, template_matches
        created_revision = status.get("latestCreatedRevisionName")
        ready_revision = status.get("latestReadyRevisionName")
        if created_revision and ready_revision and created_revision != ready_revision:
            return True, False, template_matches
        if template_matches is None:
            return True, False, template_matches
        list_pods = getattr(self.k8s_client, "list_kservice_pods", None)
        if not callable(list_pods):
            return True, True, template_matches
        try:
            pods = await list_pods(
                namespace=namespace,
                service_name=name,
            )
        except Exception:
            return True, False, template_matches
        if not pods:
            return True, False, template_matches
        for pod in pods:
            if not isinstance(pod, dict) or pod.get("metadata", {}).get(
                "deletionTimestamp"
            ):
                continue
            pod_status = pod.get("status", {})
            if not isinstance(pod_status, dict) or pod_status.get("phase") != "Running":
                continue
            container_statuses = pod_status.get("containerStatuses")
            if isinstance(container_statuses, list) and container_statuses and all(
                isinstance(container, dict) and container.get("ready") is True
                for container in container_statuses
            ):
                return True, True, template_matches
        return True, False, template_matches

    @staticmethod
    def _kservice_template_config_revision(
        payload: dict[str, Any],
    ) -> str | None:
        spec = payload.get("spec")
        template = spec.get("template") if isinstance(spec, dict) else None
        template_spec = (
            template.get("spec") if isinstance(template, dict) else None
        )
        containers = (
            template_spec.get("containers")
            if isinstance(template_spec, dict)
            else None
        )
        if not isinstance(containers, list) or not containers:
            return None
        first = containers[0]
        env = first.get("env") if isinstance(first, dict) else None
        if not isinstance(env, list):
            return None
        for item in env:
            if (
                isinstance(item, dict)
                and item.get("name") == "RUNTIME_CONFIG_REVISION"
            ):
                value = item.get("value")
                return str(value) if value is not None else None
        return None

    async def _record_online_apply_pending(
        self,
        *,
        agent_id: int,
        generation: int,
    ) -> None:
        """Keep a Desired revision retryable while its Pod is unavailable."""

        agent = await self.db.scalar(
            select(ApplicationAgent)
            .where(ApplicationAgent.id == agent_id)
            .with_for_update()
        )
        revision = await self.db.scalar(
            select(ApplicationAgentConfigRevision).where(
                ApplicationAgentConfigRevision.application_agent_id == agent_id,
                ApplicationAgentConfigRevision.generation == generation,
            )
        )
        if revision is not None:
            revision.apply_status = "not_applied"
            revision.error_code = "application_agent_runtime_not_ready"
            revision.applied_at = None
        if agent is not None and int(agent.config_generation) == generation:
            agent.apply_status = "applying"
            agent.last_apply_error_code = None
        await self.db.commit()

    @staticmethod
    def _binding_update_is_stale(
        binding: ApplicationAgentRuntimeBinding,
        *,
        now: datetime | None = None,
    ) -> bool:
        updated_at = binding.updated_at
        if updated_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        if updated_at.tzinfo is None:
            current = current.replace(tzinfo=None)
        return updated_at <= current - timedelta(
            seconds=settings.HERMES_RUNTIME_READY_TIMEOUT_SECONDS
        )

    @staticmethod
    def _binding_supports_online_apply(
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
    ) -> bool:
        applied_generation = int(binding.config_generation or 0)
        desired_generation = int(agent.config_generation or 0)
        return (
            desired_generation > applied_generation >= 0
            and binding.status == "ready"
            and bool(binding.runtime_root_pvc_uid)
            and bool(binding.pvc_storage_identity_hash)
            and bool(binding.endpoint_base_url)
            and bool(binding.endpoint_host_header)
        )

    @staticmethod
    def _build_runtime_spec(
        *,
        agent: ApplicationAgent,
        creator: User,
        binding: ApplicationAgentRuntimeBinding,
        upstream_commit: str,
        state_schema: int,
        config_schema: int,
    ) -> KnativeRuntimeSpec:
        plan = RuntimeBindingPlan(
            org_id=int(agent.org_id),
            user_id=int(creator.id),
            user_name=str(creator.name),
            user_code=str(creator.user_code or f"agent-admin-{creator.id}"),
            runtime_workspace_id=int(agent.id),
            runtime_id=str(binding.runtime_id),
            service_name=str(binding.knative_service),
            secret_name=f"{binding.knative_service}-api-key",
            hermes_home_sub_path="home",
            workspace_sub_path="workspace",
            runtime_root_pvc_name=str(binding.runtime_root_pvc),
            runtime_root_host_path=str(binding.runtime_root_host_path),
            binding_id=str(binding.id),
            config_revision=f"generation-{int(agent.config_generation)}",
            upstream_commit=upstream_commit,
            state_schema=state_schema,
            config_schema=config_schema,
            platform_control_secret_name=platform_control_secret_name(str(binding.knative_service)),
            runtime_root_pvc_uid=binding.runtime_root_pvc_uid,
            pvc_storage_identity_hash=binding.pvc_storage_identity_hash,
        )
        spec = RuntimeBindingReadyService.build_knative_spec(
            plan=plan,
            namespace=str(binding.k8s_namespace),
            image=str(binding.image),
            service_account=settings.HERMES_RUNTIME_SERVICE_ACCOUNT,
            startup_mode=(
                "steady_state"
                if binding.runtime_root_pvc_uid and binding.pvc_storage_identity_hash
                else "fresh_bootstrap"
            ),
            runtime_node_name=settings.HERMES_RUNTIME_NODE_NAME,
            default_model_entry_id=_application_model_entry_id(agent.config),
        )
        return replace(
            spec,
            application_agent_id=int(agent.id),
            min_scale=1,
            max_scale=1,
            application_agent_cpu_request=(settings.APPLICATION_AGENT_CPU_REQUEST),
            application_agent_memory_request=(settings.APPLICATION_AGENT_MEMORY_REQUEST),
            pvc_storage_size="20Gi",
            runtime_root_access_mode=(settings.HERMES_APPLICATION_AGENT_PVC_ACCESS_MODE),
        )

    async def _preserve_existing_pvc_contract(
        self,
        spec: KnativeRuntimeSpec,
        *,
        binding_created: bool,
    ) -> KnativeRuntimeSpec:
        if binding_created:
            return spec
        get_pvc = getattr(
            self.k8s_client,
            "get_persistent_volume_claim",
            None,
        )
        if not callable(get_pvc):
            return spec
        pvc = await get_pvc(
            namespace=spec.namespace,
            name=spec.runtime_root_pvc_name,
        )
        pvc_spec = pvc.get("spec") or {}
        access_modes = pvc_spec.get("accessModes") or []
        storage = ((pvc_spec.get("resources") or {}).get("requests") or {}).get("storage")
        if (
            len(access_modes) != 1
            or access_modes[0] not in {"ReadWriteOnce", "ReadWriteOncePod"}
            or not isinstance(storage, str)
            or not storage.strip()
        ):
            raise ApplicationAgentRuntimeError("application_agent_runtime_pvc_contract_invalid")
        return replace(
            spec,
            pvc_storage_size=storage.strip(),
            runtime_root_access_mode=access_modes[0],
        )

    @staticmethod
    def _runtime_startup_mode(
        *,
        binding_created: bool,
        previous_binding_status: str,
        runtime_root_pvc_uid: str | None,
        pvc_storage_identity_hash: str | None,
    ) -> str:
        del previous_binding_status
        if (
            not binding_created
            and bool(str(runtime_root_pvc_uid or "").strip())
            and bool(str(pvc_storage_identity_hash or "").strip())
        ):
            return "steady_state"
        return "fresh_bootstrap"

    async def _active_run_count(self, *, agent_id: int) -> int:
        return int(
            (
                await self.db.execute(
                    select(func.count(ApplicationAgentRun.id)).where(
                        ApplicationAgentRun.application_agent_id == agent_id,
                        ApplicationAgentRun.status.in_(("queued", "running")),
                    )
                )
            ).scalar_one()
        )

    async def _apply_online_config(
        self,
        *,
        agent: ApplicationAgent,
        creator: User,
        binding: ApplicationAgentRuntimeBinding,
        api_key: str,
    ) -> None:
        previous_endpoint_base_url = str(binding.endpoint_base_url)
        previous_endpoint_host_header = str(binding.endpoint_host_header)
        agent.apply_status = "applying"
        agent.last_apply_error_code = None
        await self.db.commit()

        contract = resolve_runtime_image_contract(
            repository_root=self.repository_root,
            image_reference=str(binding.image),
        )
        spec = self._build_runtime_spec(
            agent=agent,
            creator=creator,
            binding=binding,
            upstream_commit=contract.upstream_commit,
            state_schema=contract.state_schema,
            config_schema=contract.config_schema,
        )
        admission_generation = await load_runtime_command_admission_generation(self.db)
        target = RuntimeCommandTarget(
            runtime_id=str(binding.runtime_id),
            endpoint_host_header=previous_endpoint_host_header,
            binding_id=str(binding.id),
            api_server_key=api_key,
            admission_generation=admission_generation,
        )
        client = RuntimeCommandClient(
            ingress_base_url=previous_endpoint_base_url,
        )
        try:
            capabilities = await ApplicationAgentCapabilityResolver(self.db).resolve(agent=agent)
            await self.directory_provisioner.ensure_directory(spec)
            files, digest = await ApplicationAgentConfigBootstrapper(
                LocalRuntimeConfigBootstrapper(
                    model_store=SqlAlchemyRuntimeModelStore(self.db),
                    kanban_orchestrator_enabled=False,
                )
            ).render_bundle(
                spec,
                agent=agent,
                capabilities=capabilities,
            )
            await asyncio.to_thread(
                ApplicationAgentConfigArtifactStore.write,
                runtime_root_host_path=str(binding.runtime_root_host_path),
                generation=int(agent.config_generation),
                baseline_epoch=ProfileConfigRenderer.BASELINE_EPOCH,
                files=files,
                digest=digest,
            )
            try:
                await client.apply_config(
                    target=target,
                    config_revision=(f"generation-{int(agent.config_generation)}"),
                    config_digest=digest,
                    download_url=(
                        "/internal/v1/config-bundles/application/"
                        f"{int(agent.id)}/{int(agent.config_generation)}"
                        f"?baseline={ProfileConfigRenderer.BASELINE_EPOCH}"
                    ),
                )
            except Exception as exc:
                # A Pod can enter termination between the health check above
                # and command delivery. Keep the Desired revision pending so
                # the rollout worker can retry after the replacement is ready;
                # do not turn a transient restart race into a terminal failed
                # revision.
                _exists, ready, _template_matches = await self._kservice_generation_status(
                    namespace=str(binding.k8s_namespace),
                    name=str(binding.knative_service),
                    expected_generation=int(agent.config_generation),
                )
                if not ready or self._is_transient_runtime_command_error(exc):
                    await self._record_online_apply_pending(
                        agent_id=int(agent.id),
                        generation=int(agent.config_generation),
                    )
                    return
                raise
        except Exception as exc:
            runtime_error_code = self._runtime_command_error_code(exc)
            await self._record_online_apply_failure(
                agent_id=int(agent.id),
                generation=int(agent.config_generation),
                error_code=(
                    runtime_error_code
                    or "application_agent_config_apply_failed"
                ),
            )
            if isinstance(exc, ApplicationAgentRuntimeError):
                raise
            if runtime_error_code is not None:
                raise ApplicationAgentRuntimeError(
                    runtime_error_code,
                    status_code=409,
                ) from exc
            raise ApplicationAgentRuntimeError(
                "application_agent_runtime_reconcile_failed"
            ) from exc
        finally:
            await client.close()

        await self._record_online_apply_success(
            agent_id=int(agent.id),
            generation=int(agent.config_generation),
        )

    @staticmethod
    def _is_transient_runtime_command_error(exc: Exception) -> bool:
        if not isinstance(exc, RuntimeCommandError):
            return False
        message = str(exc).lower()
        return (
            "http 404" in message
            or "http 502" in message
            or "http 503" in message
            or "http 504" in message
            or "delivery failed" in message
        )

    @staticmethod
    def _runtime_command_error_code(exc: Exception) -> str | None:
        if not isinstance(exc, RuntimeCommandError):
            return None
        value = str(exc).strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,99}", value):
            return value
        return None

    async def _record_online_apply_success(
        self,
        *,
        agent_id: int,
        generation: int,
    ) -> None:
        binding = await self.db.scalar(
            select(ApplicationAgentRuntimeBinding)
            .where(
                ApplicationAgentRuntimeBinding.application_agent_id
                == agent_id
            )
            .with_for_update()
        )
        agent = await self.db.scalar(
            select(ApplicationAgent)
            .where(ApplicationAgent.id == agent_id)
            .with_for_update()
        )
        if binding is None or agent is None:
            raise ApplicationAgentRuntimeError(
                "application_agent_runtime_not_found",
                status_code=404,
            )
        binding.config_generation = max(
            int(binding.config_generation or 0),
            generation,
        )
        revision = await self.db.scalar(
            select(ApplicationAgentConfigRevision).where(
                ApplicationAgentConfigRevision.application_agent_id
                == agent_id,
                ApplicationAgentConfigRevision.generation == generation,
            )
        )
        if revision is not None:
            revision.apply_status = "applied"
            revision.error_code = None
            revision.applied_at = datetime.utcnow()
        agent.applied_generation = max(
            int(agent.applied_generation or 0),
            generation,
        )
        if int(agent.config_generation) == generation:
            agent.apply_status = "applied"
            agent.last_apply_error_code = None
            binding.last_error_code = None
        await self.db.commit()

    async def _record_online_apply_failure(
        self,
        *,
        agent_id: int,
        generation: int,
        error_code: str = "application_agent_config_apply_failed",
    ) -> None:
        agent = await self.db.scalar(
            select(ApplicationAgent)
            .where(ApplicationAgent.id == agent_id)
            .with_for_update()
        )
        revision = await self.db.scalar(
            select(ApplicationAgentConfigRevision).where(
                ApplicationAgentConfigRevision.application_agent_id
                == agent_id,
                ApplicationAgentConfigRevision.generation == generation,
            )
        )
        if revision is not None:
            revision.apply_status = "failed"
            revision.error_code = error_code
            revision.applied_at = None
        if agent is not None and int(agent.config_generation) == generation:
            agent.apply_status = "failed"
            agent.last_apply_error_code = error_code
        await self.db.commit()

    async def ensure_runtime(
        self,
        *,
        org_id: int,
        agent_id: int,
        apply_scope: ApplicationAgentRuntimeApplyScope = "full",
    ) -> None:
        if apply_scope not in {
            "availability_only",
            "full",
            "settings_only",
            "skills_only",
        }:
            raise ValueError("invalid application Agent Runtime apply scope")
        try:
            await self.maintenance_service.require_admission_open(lock=True)
        except RuntimeMaintenanceError as exc:
            raise ApplicationAgentRuntimeError(
                exc.code,
                status_code=503,
            ) from exc
        await self._converge_runtime(
            org_id=org_id,
            agent_id=agent_id,
            apply_scope=apply_scope,
            reload_owned=False,
            expected_binding_id=None,
        )

    async def ensure_runtime_available(
        self,
        *,
        org_id: int,
        agent_id: int,
    ) -> None:
        await self.ensure_runtime(
            org_id=org_id,
            agent_id=agent_id,
            apply_scope="availability_only",
        )

    async def reload_runtime(
        self,
        *,
        binding_id: int,
        target: RuntimeImageConfig,
        maintenance_epoch: str | None = None,
    ) -> None:
        if not target.config_id or not target.manifest_digest:
            raise ValueError("Runtime reload target identity is incomplete")
        snapshot = await self.maintenance_service.snapshot(lock=True)
        if maintenance_epoch is None:
            if snapshot.maintenance_active:
                raise ApplicationAgentRuntimeError(
                    "RUNTIME_MAINTENANCE_FROZEN",
                    status_code=503,
                )
        elif not snapshot.maintenance_active or snapshot.maintenance_epoch != maintenance_epoch:
            raise ApplicationAgentRuntimeError(
                "RUNTIME_MAINTENANCE_OWNERSHIP_MISMATCH",
                status_code=409,
            )
        binding = await self.db.scalar(
            select(ApplicationAgentRuntimeBinding)
            .where(ApplicationAgentRuntimeBinding.id == int(binding_id))
            .with_for_update()
        )
        if binding is None:
            raise ApplicationAgentRuntimeError(
                "application_agent_runtime_not_found",
                status_code=404,
            )
        if not binding.runtime_root_pvc_uid or not binding.pvc_storage_identity_hash:
            raise ApplicationAgentRuntimeError(
                "application_agent_runtime_pvc_identity_missing",
                status_code=409,
            )
        binding.image = target.image
        binding.image_digest = target.config_id
        binding.status = "updating"
        binding.last_error_code = None
        org_id = int(binding.org_id)
        agent_id = int(binding.application_agent_id)
        await self.db.commit()
        await self._converge_runtime(
            org_id=org_id,
            agent_id=agent_id,
            apply_scope="full",
            reload_owned=True,
            expected_binding_id=int(binding_id),
        )

    async def _converge_runtime(
        self,
        *,
        org_id: int,
        agent_id: int,
        apply_scope: ApplicationAgentRuntimeApplyScope,
        reload_owned: bool,
        expected_binding_id: int | None,
    ) -> None:
        row = (
            await self.db.execute(
                select(ApplicationAgent, User)
                .join(User, User.id == ApplicationAgent.created_by_user_id)
                .where(
                    ApplicationAgent.id == agent_id,
                    ApplicationAgent.org_id == org_id,
                )
            )
        ).first()
        if row is None:
            raise ApplicationAgentRuntimeError(
                "application_agent_not_found",
                status_code=404,
            )
        agent, creator = row
        binding = (
            await self.db.execute(
                select(ApplicationAgentRuntimeBinding)
                .where(ApplicationAgentRuntimeBinding.application_agent_id == agent_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        binding_created = binding is None
        if (
            binding is not None
            and not reload_owned
            and str(binding.status)
            in {
                "starting",
                "updating",
            }
            and not self._binding_update_is_stale(binding)
        ):
            raise ApplicationAgentRuntimeError(
                "application_agent_runtime_busy",
                status_code=409,
            )
        platform_control_key: RuntimePlatformControlKeyMaterial | None = None
        platform_control_key_created = False
        if reload_owned and (
            binding is None
            or int(binding.id) != expected_binding_id
            or str(binding.status) != "updating"
        ):
            raise ApplicationAgentRuntimeError(
                "application_agent_runtime_reload_ownership_lost",
                status_code=409,
            )
        if (
            binding is not None
            and not reload_owned
            and apply_scope == "availability_only"
            and await self._binding_is_available(binding=binding)
        ):
            return
        (
            desired_runtime_image,
            desired_runtime_image_digest,
            desired_upstream_commit,
            desired_state_schema,
            desired_config_schema,
        ) = await self._desired_runtime_contract(binding=binding)
        if binding is not None:
            control_key_provision = await self.platform_control_key_store.ensure_active_key(
                binding_type="application_runtime",
                binding_id=int(binding.id),
                runtime_id=str(binding.runtime_id),
            )
            platform_control_key = control_key_provision.material
            platform_control_key_created = control_key_provision.created
            if control_key_provision.created:
                await self.db.commit()
        previous_binding_status = str(binding.status) if binding is not None else "absent"
        previous_endpoint_base_url = (
            str(binding.endpoint_base_url)
            if binding is not None and binding.endpoint_base_url
            else None
        )
        previous_endpoint_host_header = (
            str(binding.endpoint_host_header)
            if binding is not None and binding.endpoint_host_header
            else None
        )
        api_key: str
        if binding is None:
            api_key = secrets.token_urlsafe(36)
            service_name = f"application-agent-{agent_id}"
            root_path = (
                Path(settings.HERMES_APPLICATION_AGENT_RUNTIME_ROOT) / str(org_id) / str(agent_id)
            ).resolve(strict=False)
            boundary = Path(settings.HERMES_APPLICATION_AGENT_RUNTIME_ROOT).resolve(strict=False)
            if not root_path.is_relative_to(boundary):
                raise ApplicationAgentRuntimeError("application_agent_runtime_path_invalid")
            binding = ApplicationAgentRuntimeBinding(
                org_id=org_id,
                application_agent_id=agent_id,
                runtime_id=f"application-agent-runtime-{agent_id}",
                k8s_namespace=settings.HERMES_RUNTIME_NAMESPACE,
                knative_service=service_name,
                runtime_root_pvc=f"{service_name}-root",
                runtime_root_host_path=str(root_path),
                config_generation=int(agent.config_generation),
                image=desired_runtime_image,
                image_digest=desired_runtime_image_digest,
                api_key_encrypted=self.codec.encrypt(api_key),
                status="starting",
            )
            self.db.add(binding)
        else:
            api_key = self.codec.decrypt(str(binding.api_key_encrypted))
            if (
                not reload_owned
                and not platform_control_key_created
                and apply_scope != "availability_only"
                and self._binding_supports_online_apply(
                    agent=agent,
                    binding=binding,
                )
            ):
                (
                    kservice_exists,
                    kservice_ready,
                    _template_matches,
                ) = await self._kservice_generation_status(
                    namespace=str(binding.k8s_namespace),
                    name=str(binding.knative_service),
                    expected_generation=int(agent.config_generation),
                )
                if kservice_exists is None or (
                    kservice_exists
                    and not kservice_ready
                ):
                    await self._record_online_apply_pending(
                        agent_id=int(agent.id),
                        generation=int(agent.config_generation),
                    )
                    return
                if kservice_exists:
                    await self._apply_online_config(
                        agent=agent,
                        creator=creator,
                        binding=binding,
                        api_key=api_key,
                    )
                    return
            if (
                not reload_owned
                and not platform_control_key_created
                and await self._binding_is_reusable(
                    agent=agent,
                    binding=binding,
                )
            ):
                return
            if not reload_owned and binding is not None:
                (
                    kservice_exists,
                    kservice_ready,
                    _template_matches,
                ) = await self._kservice_generation_status(
                    namespace=str(binding.k8s_namespace),
                    name=str(binding.knative_service),
                    expected_generation=int(agent.config_generation),
                )
                if kservice_exists is None:
                    raise ApplicationAgentRuntimeError(
                        "application_agent_runtime_not_ready",
                        status_code=503,
                    )
                if kservice_exists and not kservice_ready:
                    if apply_scope == "availability_only":
                        raise ApplicationAgentRuntimeError(
                            "application_agent_runtime_not_ready",
                            status_code=503,
                        )
                    await self._record_online_apply_pending(
                        agent_id=int(agent.id),
                        generation=int(agent.config_generation),
                    )
                    return
            active_runs = 0 if reload_owned else await self._active_run_count(agent_id=agent_id)
            if active_runs:
                raise ApplicationAgentRuntimeError(
                    "application_agent_runtime_busy",
                    status_code=409,
                )
            binding.status = "updating"
            binding.last_error_code = None
        agent.apply_status = "applying"
        agent.last_apply_error_code = None
        await self.db.commit()

        if platform_control_key is None:
            control_key_provision = await self.platform_control_key_store.ensure_active_key(
                binding_type="application_runtime",
                binding_id=int(binding.id),
                runtime_id=str(binding.runtime_id),
            )
            platform_control_key = control_key_provision.material
            await self.db.commit()

        spec = self._build_runtime_spec(
            agent=agent,
            creator=creator,
            binding=binding,
            upstream_commit=desired_upstream_commit,
            state_schema=desired_state_schema,
            config_schema=desired_config_schema,
        )
        spec = replace(
            spec,
            platform_control_key_version=platform_control_key.key_version,
        )
        spec = await self._preserve_existing_pvc_contract(
            spec,
            binding_created=binding_created,
        )
        spec = replace(
            spec,
            startup_mode=self._runtime_startup_mode(
                binding_created=binding_created,
                previous_binding_status=previous_binding_status,
                runtime_root_pvc_uid=binding.runtime_root_pvc_uid,
                pvc_storage_identity_hash=binding.pvc_storage_identity_hash,
            ),
        )
        if reload_owned:
            spec = replace(spec, startup_mode="steady_state")
        protocols = build_runtime_protocol_adapters()

        async def prepare_schema_startup(
            target_spec: KnativeRuntimeSpec,
            ingress_base_url: str,
        ) -> KnativeRuntimeSpec:
            from app.services.runtime_fresh_init import (
                RuntimeFreshInitAuthoringStore,
                RuntimeFreshInitProvisioner,
            )

            return await RuntimeFreshInitProvisioner(
                kubernetes=self.k8s_client,
                provider=self.provider,
                authoring_store=RuntimeFreshInitAuthoringStore(AsyncSessionLocal),
                timeout_seconds=(settings.HERMES_RUNTIME_READY_TIMEOUT_SECONDS),
            ).prepare(target_spec, ingress_base_url)

        async def complete_fresh_bootstrap(target_spec: KnativeRuntimeSpec) -> None:
            if not target_spec.bootstrap_intent_id:
                raise ApplicationAgentRuntimeError(
                    "application_agent_runtime_bootstrap_intent_missing"
                )
            from app.services.runtime_fresh_init import RuntimeFreshInitAuthoringStore

            await RuntimeFreshInitAuthoringStore(AsyncSessionLocal).mark_completed(
                intent_id=target_spec.bootstrap_intent_id
            )

        try:
            await self.directory_provisioner.ensure_directory(spec)
            _bundle_files, bundle_digest = await self._load_or_create_config_artifact(
                runtime_root_host_path=str(binding.runtime_root_host_path),
                generation=int(agent.config_generation),
                baseline_epoch=ProfileConfigRenderer.BASELINE_EPOCH,
                spec=spec,
                agent=agent,
            )
            spec = replace(
                spec,
                initial_config_download_url=(
                    "/internal/v1/config-bundles/application/"
                    f"{int(agent.id)}/"
                    f"{int(agent.config_generation)}"
                    f"?baseline={ProfileConfigRenderer.BASELINE_EPOCH}"
                ),
                initial_config_digest=bundle_digest,
            )
            if not binding_created:
                await self._remove_existing_kservice(binding)
            ready = await RuntimeBindingReadyService(
                k8s_client=self.k8s_client,
                binding_store=SqlAlchemyApplicationAgentBindingStatusStore(
                    self.db,
                    agent_id=agent_id,
                    expected_config_generation=int(
                        agent.config_generation
                    ),
                ),
                protocol_adapter=protocols.dashboard_adapter,
                provider=self.provider,
                ready_timeout_seconds=(settings.HERMES_RUNTIME_READY_TIMEOUT_SECONDS),
                schema_startup_preparer=prepare_schema_startup,
                bootstrap_completion=complete_fresh_bootstrap,
            ).ensure_kservice_ready(
                spec=spec,
                api_server_key=api_key,
                platform_control_key=platform_control_key,
                ingress_base_url=settings.HERMES_RUNTIME_INGRESS_BASE_URL,
            )
        except Exception as exc:
            await self._record_apply_failure(
                agent_id=agent_id,
                attempted_generation=int(agent.config_generation),
                previous_binding_status=previous_binding_status,
                previous_endpoint_base_url=previous_endpoint_base_url,
                previous_endpoint_host_header=(previous_endpoint_host_header),
            )
            if isinstance(exc, ApplicationAgentRuntimeError):
                raise
            raise ApplicationAgentRuntimeError(
                "application_agent_runtime_reconcile_failed"
            ) from exc
        if ready.status != "ready":
            await self._record_apply_failure(
                agent_id=agent_id,
                attempted_generation=int(agent.config_generation),
                previous_binding_status=previous_binding_status,
                previous_endpoint_base_url=previous_endpoint_base_url,
                previous_endpoint_host_header=(previous_endpoint_host_header),
            )
            raise ApplicationAgentRuntimeError("application_agent_runtime_reconcile_failed")
        binding.image_digest = desired_runtime_image_digest
        await self.db.commit()

    async def _remove_existing_kservice(
        self,
        binding: ApplicationAgentRuntimeBinding,
    ) -> None:
        namespace = str(binding.k8s_namespace)
        service_name = str(binding.knative_service)
        await self.k8s_client.delete_kservice(
            namespace=namespace,
            name=service_name,
        )
        list_pods = getattr(self.k8s_client, "list_kservice_pods", None)
        if not callable(list_pods):
            raise ApplicationAgentRuntimeError("application_agent_runtime_pod_drain_unavailable")
        deadline = asyncio.get_running_loop().time() + settings.HERMES_RUNTIME_READY_TIMEOUT_SECONDS
        while True:
            pods = await list_pods(
                namespace=namespace,
                service_name=service_name,
            )
            if not pods:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise ApplicationAgentRuntimeError("application_agent_runtime_pod_drain_timeout")
            await asyncio.sleep(1)

    async def _record_apply_failure(
        self,
        *,
        agent_id: int,
        attempted_generation: int,
        previous_binding_status: str,
        previous_endpoint_base_url: str | None,
        previous_endpoint_host_header: str | None,
    ) -> None:
        binding = (
            await self.db.execute(
                select(ApplicationAgentRuntimeBinding).where(
                    ApplicationAgentRuntimeBinding.application_agent_id == agent_id
                )
            )
        ).scalar_one()
        agent = (
            await self.db.execute(select(ApplicationAgent).where(ApplicationAgent.id == agent_id))
        ).scalar_one()
        recovered = (
            previous_binding_status == "ready"
            and previous_endpoint_base_url is not None
            and previous_endpoint_host_header is not None
        )
        binding.status = "ready" if recovered else "failed"
        if recovered:
            binding.endpoint_base_url = previous_endpoint_base_url
            binding.endpoint_host_header = previous_endpoint_host_header
        binding.last_error_code = "application_agent_capability_apply_failed"
        if int(agent.config_generation) == attempted_generation:
            agent.apply_status = "failed"
            agent.last_apply_error_code = binding.last_error_code
        revision = (
            await self.db.execute(
                select(ApplicationAgentConfigRevision).where(
                    ApplicationAgentConfigRevision.application_agent_id == agent_id,
                    ApplicationAgentConfigRevision.generation
                    == attempted_generation,
                )
            )
        ).scalar_one_or_none()
        if revision is not None:
            revision.apply_status = "failed"
            revision.error_code = binding.last_error_code
            revision.applied_at = None
        await self.db.commit()

    @staticmethod
    async def _reload_previous_capabilities(
        *,
        adapter: HermesDashboardProtocolAdapter,
        endpoint: RuntimeEndpoint,
    ) -> None:
        try:
            await adapter.reload_skills(endpoint)
            await adapter.reload_mcp(endpoint)
        except Exception:
            # The original files are already restored. A later ensure retries
            # the reload; never replace the primary projection error.
            return

    @staticmethod
    async def _reload_previous_skills(
        *,
        adapter: HermesDashboardProtocolAdapter,
        endpoint: RuntimeEndpoint,
    ) -> None:
        try:
            await adapter.reload_skills(endpoint)
        except Exception:
            # The original files are already restored. A later ensure retries
            # the reload; never replace the primary projection error.
            return

    async def delete_runtime(
        self,
        *,
        org_id: int,
        agent_id: int,
    ) -> None:
        binding = (
            await self.db.execute(
                select(ApplicationAgentRuntimeBinding).where(
                    ApplicationAgentRuntimeBinding.application_agent_id == agent_id,
                    ApplicationAgentRuntimeBinding.org_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if binding is None:
            expected_path = (
                Path(settings.HERMES_APPLICATION_AGENT_RUNTIME_ROOT) / str(org_id) / str(agent_id)
            )
            await asyncio.to_thread(
                self._delete_runtime_directory,
                org_id=org_id,
                agent_id=agent_id,
                recorded_path=str(expected_path),
            )
            return
        namespace = str(binding.k8s_namespace)
        service_name = str(binding.knative_service)
        pvc_name = str(binding.runtime_root_pvc)
        try:
            await self.k8s_client.delete_kservice(
                namespace=namespace,
                name=service_name,
            )
            await self.k8s_client.delete_secret(
                namespace=namespace,
                name=f"{service_name}-api-key",
            )
            await self.k8s_client.delete_secret(
                namespace=namespace,
                name=platform_control_secret_name(service_name),
            )
            await self.k8s_client.delete_persistent_volume_claim(
                namespace=namespace,
                name=pvc_name,
            )
            await self.k8s_client.delete_persistent_volume(
                name=f"{pvc_name}-pv",
            )
            await asyncio.to_thread(
                self._delete_runtime_directory,
                org_id=org_id,
                agent_id=agent_id,
                recorded_path=str(binding.runtime_root_host_path),
            )
        except ApplicationAgentRuntimeError:
            raise
        except Exception as exc:
            raise ApplicationAgentRuntimeError("application_agent_delete_failed") from exc

    @staticmethod
    def _delete_runtime_directory(
        *,
        org_id: int,
        agent_id: int,
        recorded_path: str,
    ) -> None:
        root = Path(settings.HERMES_APPLICATION_AGENT_RUNTIME_ROOT).resolve(strict=False)
        expected = root / str(org_id) / str(agent_id)
        resolved_expected = expected.resolve(strict=False)
        resolved_recorded = Path(recorded_path).resolve(strict=False)
        if (
            resolved_expected == root
            or resolved_expected == root / str(org_id)
            or not resolved_expected.is_relative_to(root)
            or resolved_recorded != resolved_expected
            or expected.is_symlink()
        ):
            raise ApplicationAgentRuntimeError("application_agent_runtime_path_invalid")
        if expected.exists():
            shutil.rmtree(expected)
