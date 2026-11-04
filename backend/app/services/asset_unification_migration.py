from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import shutil
from pathlib import Path

from sqlalchemy import delete, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ExpertVersionArtifact,
    ExpertPackage,
    ExpertPackageVersion,
    HermesRuntimeBinding,
    InstalledExpertBinding,
    Mcp,
    McpVersion,
    Permission,
    ProfileKnowledgeBinding,
    ProfileMcpBinding,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    Skill,
    SkillVersion,
    UserSkillHubInstallation,
)
from app.models.legacy_assets import (
    LegacyProfileAssetDraft,
    LegacyPublicAsset,
    LegacyPublicMcpAsset,
    LegacyPublicSkillAsset,
    LegacyPublicSkillVersion,
    LegacyRuntimeKbBinding,
    LegacyRuntimeMcpBinding,
    LegacyRuntimeSkillBinding,
    LegacyUserRuntimeMcp,
    LegacyUserRuntimeSkill,
)
from app.schemas.asset_catalog import (
    CreateMcpAssetCommand,
    CreateSkillAssetCommand,
    McpVersionMaterial,
    PublishMcpVersionCommand,
    PublishSkillVersionCommand,
    SkillVersionMaterial,
)
from app.schemas.profile_desired import (
    ProfileDesiredCommand,
    ProfileDesiredDefinition,
    ProfileDesiredMcpRef,
    ProfileDesiredSkillRef,
)
from app.services.asset_catalog import AssetCatalogError, AssetCatalogService
from app.services.expert_rollout import ExpertRolloutService
from app.services.legacy_expert_asset_migration import (
    LegacyExpertAssetMigrationError,
    LegacyExpertAssetMigrationService,
)
from app.services.profile_desired import ProfileDesiredError, ProfileDesiredService
from app.services.plaza.public_catalog.package_parser import sha256_directory
from app.services.plaza.user_skill_inventory.runtime_paths import runtime_skills_root
from app.config import settings


class AssetUnificationMigrationError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class AssetUnificationMigrationResult:
    public_skill_assets: int
    public_skill_versions: int
    public_mcp_assets: int
    personal_skill_assets: int
    migrated_profiles: int
    rewritten_expert_versions: int
    normalized_profile_lifecycles: int
    normalized_expert_permissions: int
    normalized_skill_bindings: int
    backfilled_installation_epochs: int


def _canonical_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _legacy_skill_slug(asset_id: int) -> str:
    return f"legacy-public-skill-{asset_id}"


def _transport(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    if normalized == "http":
        return "streamable_http"
    if normalized == "streamable_http":
        return normalized
    if normalized in {"sse", "stdio"}:
        return normalized
    raise AssetUnificationMigrationError(
        "LEGACY_MCP_TRANSPORT_INVALID",
        f"unsupported legacy MCP transport: {value}",
    )


def _legacy_mcp_material(asset: LegacyPublicMcpAsset) -> McpVersionMaterial:
    runtime_config = dict(asset.runtime_config or {})
    transport = _transport(str(runtime_config.pop("transport", asset.transport)))
    runtime_config.pop("name", None)
    runtime_config.pop("enabled", None)
    if "endpoint" in runtime_config and "url" not in runtime_config:
        runtime_config["url"] = runtime_config.pop("endpoint")
    return McpVersionMaterial(
        transport=transport,
        connection_config=runtime_config,
        secret_refs={},
        tools_schema=None,
    )


def _legacy_generic_mcp_material(asset: LegacyPublicAsset) -> McpVersionMaterial:
    manifest = dict(asset.manifest or {})
    transport = _transport(str(manifest.get("transport") or ""))
    connection_config = dict(manifest.get("config") or {})
    endpoint = manifest.get("endpoint")
    if endpoint and "url" not in connection_config:
        connection_config["url"] = endpoint
    return McpVersionMaterial(
        transport=transport,
        connection_config=connection_config,
        secret_refs={},
        tools_schema=None,
    )


class AssetUnificationMigrationService:
    """Translate the retired asset identities into the sole stable catalog.

    The caller owns the transaction.  Any ambiguity aborts the whole migration;
    no method commits and no Runtime, Dashboard or PVC endpoint is contacted.
    """

    def __init__(self, db: AsyncSession, *, materialize_files: bool = True) -> None:
        self.db = db
        self.catalog = AssetCatalogService(db)
        self.materialize_files = materialize_files
        self.created_artifact_paths: list[Path] = []

    def cleanup_created_artifacts(self) -> None:
        for path in reversed(self.created_artifact_paths):
            shutil.rmtree(path, ignore_errors=True)
        self.created_artifact_paths.clear()

    async def migrate(self) -> AssetUnificationMigrationResult:
        await self._assert_no_unsaved_drafts()
        normalized_profiles = await self._normalize_profile_lifecycles()
        normalized_permissions = await self._normalize_expert_permissions()
        normalized_bindings = await self._normalize_stable_skill_bindings()
        backfilled_epochs = await self._normalize_installation_epochs()
        await self._assert_name_leases_unambiguous()
        skill_map, skill_version_map = await self._migrate_public_skills()
        generic_mcp_map = await self._migrate_generic_public_mcps()
        mcp_map = await self._migrate_public_mcps()
        installed_skill_map, personal_skill_count = await self._migrate_installed_skills(
            skill_map=skill_map,
        )
        try:
            rewritten = await LegacyExpertAssetMigrationService(self.db).migrate(
                skill_map=skill_map,
                skill_version_map=skill_version_map,
                mcp_map=mcp_map,
                installed_skill_map=installed_skill_map,
            )
        except LegacyExpertAssetMigrationError as exc:
            raise AssetUnificationMigrationError(exc.code, str(exc)) from exc
        profile_count = await self._migrate_profiles(
            skill_map=skill_map,
            skill_version_map=skill_version_map,
            mcp_map=mcp_map,
            installed_skill_map=installed_skill_map,
        )
        return AssetUnificationMigrationResult(
            public_skill_assets=len(skill_map),
            public_skill_versions=len(skill_version_map),
            public_mcp_assets=len(generic_mcp_map) + len(mcp_map),
            personal_skill_assets=personal_skill_count,
            migrated_profiles=profile_count,
            rewritten_expert_versions=rewritten,
            normalized_profile_lifecycles=normalized_profiles,
            normalized_expert_permissions=normalized_permissions,
            normalized_skill_bindings=normalized_bindings,
            backfilled_installation_epochs=backfilled_epochs,
        )

    async def _normalize_profile_lifecycles(self) -> int:
        mapping = {
            "ready": "active",
            "error": "active",
            "provisioning": "active",
            "updating": "active",
            "deleting": "disabled",
        }
        changed = 0
        for profile in await self.db.scalars(
            select(RuntimeAgentProfile).order_by(RuntimeAgentProfile.id.asc())
        ):
            current = str(profile.status)
            if current in {"active", "disabled", "deleted"}:
                continue
            target = mapping.get(current)
            if target is None:
                raise AssetUnificationMigrationError(
                    "PROFILE_LIFECYCLE_STATUS_UNSUPPORTED",
                    f"runtime_agent_profile:{profile.id} has unsupported status {current}",
                )
            profile.status = target
            changed += 1
        return changed

    async def _normalize_expert_permissions(self) -> int:
        rows = list(
            await self.db.scalars(
                select(Permission)
                .where(Permission.asset_type == "expert_package")
                .order_by(Permission.asset_id.asc(), Permission.id.asc())
            )
        )
        grouped: dict[int, list[Permission]] = {}
        for row in rows:
            grouped.setdefault(int(row.asset_id), []).append(row)
        changed = 0
        for asset_id, permissions in grouped.items():
            roots = [
                row
                for row in permissions
                if str(row.grantee_type) in {"all", "organization", "root"}
                or str(row.grantee_id) == "*"
            ]
            for row in permissions:
                if str(row.grantee_type) not in {
                    "all",
                    "organization",
                    "root",
                    "department",
                    "user",
                }:
                    raise AssetUnificationMigrationError(
                        "EXPERT_PERMISSION_GRANTEE_INVALID",
                        f"permission:{row.id} has unsupported grantee type",
                    )
                if row.kb_role is not None:
                    row.kb_role = None
                    changed += 1
            if not roots:
                continue
            root = roots[0]
            if root.grantee_type != "all" or root.grantee_id != "*":
                root.grantee_type = "all"
                root.grantee_id = "*"
                changed += 1
            obsolete_ids = [int(row.id) for row in permissions if row is not root]
            if obsolete_ids:
                await self.db.execute(
                    delete(Permission).where(Permission.id.in_(obsolete_ids))
                )
                changed += len(obsolete_ids)
        return changed

    async def _normalize_stable_skill_bindings(self) -> int:
        changed = 0
        for binding in await self.db.scalars(
            select(ProfileSkillBinding).order_by(ProfileSkillBinding.id.asc())
        ):
            if binding.version_policy == "follow_latest" and binding.skill_version_id is None:
                continue
            binding.version_policy = "follow_latest"
            binding.skill_version_id = None
            changed += 1
        return changed

    async def _normalize_installation_epochs(self) -> int:
        active_slots: dict[tuple[int, int, int, int], list[int]] = {}
        changed = 0
        bindings = list(
            await self.db.scalars(
                select(InstalledExpertBinding).order_by(InstalledExpertBinding.id.asc())
            )
        )
        for binding in bindings:
            if str(binding.status) != "enabled":
                continue
            key = (
                int(binding.org_id),
                int(binding.user_id),
                int(binding.runtime_workspace_id),
                int(binding.expert_package_id),
            )
            active_slots.setdefault(key, []).append(int(binding.id))
            if int(binding.installation_epoch or 0) < 1:
                binding.installation_epoch = 1
                changed += 1
        conflicts = [ids for ids in active_slots.values() if len(ids) > 1]
        if conflicts:
            ids = sorted(item for group in conflicts for item in group)
            raise AssetUnificationMigrationError(
                "MULTIPLE_ACTIVE_INSTALLATION_SLOTS",
                f"installed_expert_binding conflicts: {ids}",
            )
        return changed

    async def _assert_name_leases_unambiguous(self) -> None:
        active_names: dict[tuple[int, int, int, str], list[int]] = {}
        for profile in await self.db.scalars(
            select(RuntimeAgentProfile).where(
                RuntimeAgentProfile.profile_kind == "expert",
                RuntimeAgentProfile.status == "active",
            )
        ):
            key = (
                int(profile.org_id),
                int(profile.user_id),
                int(profile.runtime_workspace_id),
                " ".join(str(profile.display_name).split()).casefold(),
            )
            active_names.setdefault(key, []).append(int(profile.id))
        profile_conflicts = [ids for ids in active_names.values() if len(ids) > 1]
        if profile_conflicts:
            ids = sorted(item for group in profile_conflicts for item in group)
            raise AssetUnificationMigrationError(
                "ACTIVE_EXPERT_NAME_CONFLICT",
                f"runtime_agent_profile conflicts: {ids}",
            )

        market_names: dict[tuple[int, str], list[int]] = {}
        for package in await self.db.scalars(
            select(ExpertPackage).where(ExpertPackage.status == "published")
        ):
            key = (
                int(package.org_id),
                " ".join(str(package.name).split()).casefold(),
            )
            market_names.setdefault(key, []).append(int(package.id))
        package_conflicts = [ids for ids in market_names.values() if len(ids) > 1]
        if package_conflicts:
            ids = sorted(item for group in package_conflicts for item in group)
            raise AssetUnificationMigrationError(
                "MARKET_EXPERT_NAME_CONFLICT",
                f"expert_package conflicts: {ids}",
            )

    async def _assert_no_unsaved_drafts(self) -> None:
        draft = await self.db.scalar(
            select(LegacyProfileAssetDraft.id)
            .where(LegacyProfileAssetDraft.status.notin_(("discarded", "applied")))
            .limit(1)
        )
        if draft is not None:
            raise AssetUnificationMigrationError(
                "UNSAVED_PROFILE_DRAFT",
                f"profile_asset_draft:{draft} is not finalized",
            )

    async def _migrate_public_skills(self) -> tuple[dict[int, int], dict[int, int]]:
        assets = list(
            (
                await self.db.scalars(
                    select(LegacyPublicSkillAsset).order_by(
                        LegacyPublicSkillAsset.id.asc()
                    )
                )
            ).all()
        )
        skill_map: dict[int, int] = {}
        version_map: dict[int, int] = {}
        for legacy in assets:
            slug = _legacy_skill_slug(int(legacy.id))
            skill = await self.db.scalar(
                select(Skill).where(
                    Skill.org_id == legacy.org_id,
                    Skill.slug == slug,
                    Skill.asset_scope == "public",
                )
            )
            versions = list(
                (
                    await self.db.scalars(
                        select(LegacyPublicSkillVersion)
                        .where(
                            LegacyPublicSkillVersion.public_skill_asset_id
                            == legacy.id
                        )
                        .order_by(
                            LegacyPublicSkillVersion.version_number.asc(),
                            LegacyPublicSkillVersion.id.asc(),
                        )
                    )
                ).all()
            )
            if not versions:
                if legacy.status == "deleted" and legacy.current_version_id is None:
                    continue
                raise AssetUnificationMigrationError(
                    "LEGACY_SKILL_VERSION_MISSING",
                    f"public_skill_asset:{legacy.id} has no version",
                )
            by_legacy_version: dict[int, SkillVersion] = {}
            if skill is not None:
                existing_versions = (
                    await self.db.scalars(
                        select(SkillVersion).where(SkillVersion.skill_id == skill.id)
                    )
                ).all()
                for item in existing_versions:
                    source = dict(item.manifest or {}).get("legacy_source")
                    if isinstance(source, dict) and source.get("table") == "public_skill_version":
                        by_legacy_version[int(source["id"])] = item
            for legacy_version in versions:
                package_sha256 = str(
                    legacy_version.deployment_sha256 or legacy_version.original_sha256
                ).lower()
                manifest = dict(legacy_version.manifest or {})
                manifest["legacy_source"] = {
                    "table": "public_skill_version",
                    "id": int(legacy_version.id),
                    "asset_id": int(legacy.id),
                }
                material = SkillVersionMaterial(
                    package_uri=str(legacy_version.deployment_package_uri),
                    package_sha256=package_sha256,
                    manifest=manifest,
                )
                existing = by_legacy_version.get(int(legacy_version.id))
                if existing is not None:
                    if str(existing.package_sha256).lower() != package_sha256 or str(
                        existing.package_uri
                    ) != str(legacy_version.deployment_package_uri):
                        raise AssetUnificationMigrationError(
                            "LEGACY_SKILL_VERSION_CONFLICT",
                            f"public_skill_version:{legacy_version.id} differs from its migrated version",
                        )
                    result_asset_id = int(skill.id)
                    result_version_id = int(existing.id)
                elif skill is None:
                    result = await self.catalog.create_skill(
                        CreateSkillAssetCommand(
                            org_id=int(legacy.org_id),
                            owner_user_id=int(legacy.created_by_user_id),
                            slug=slug,
                            name=str(legacy.name),
                            description=legacy.description,
                            asset_scope="public",
                            visibility="organization",
                            material=material,
                        )
                    )
                    skill = await self.db.get(Skill, result.asset_id)
                    assert skill is not None
                    result_asset_id = result.asset_id
                    result_version_id = result.version_id
                else:
                    result = await self.catalog.publish_skill_version(
                        PublishSkillVersionCommand(
                            org_id=int(legacy.org_id),
                            skill_id=int(skill.id),
                            material=material,
                        )
                    )
                    result_asset_id = result.asset_id
                    result_version_id = result.version_id
                skill_map[int(legacy.id)] = result_asset_id
                version_map[int(legacy_version.id)] = result_version_id
            assert skill is not None
            current_id = int(legacy.current_version_id or 0)
            if current_id not in version_map:
                raise AssetUnificationMigrationError(
                    "LEGACY_SKILL_CURRENT_VERSION_INVALID",
                    f"public_skill_asset:{legacy.id} current version is unavailable",
                )
            skill.current_version_id = version_map[current_id]
            skill.category_key = str(legacy.category_key or "other")
            skill.status = "published" if legacy.status == "active" else "archived"
            if legacy.status != "active":
                skill.archived_at = legacy.deleted_at or skill.archived_at
        await self.db.flush()
        return skill_map, version_map

    async def _migrate_public_mcps(self) -> dict[int, int]:
        assets = list(
            (
                await self.db.scalars(
                    select(LegacyPublicMcpAsset).order_by(
                        LegacyPublicMcpAsset.id.asc()
                    )
                )
            ).all()
        )
        mapping: dict[int, int] = {}
        for legacy in assets:
            material = _legacy_mcp_material(legacy)
            existing = await self.db.scalar(
                select(Mcp).where(
                    Mcp.org_id == legacy.org_id,
                    Mcp.slug == legacy.slug,
                    Mcp.visibility != "private",
                )
            )
            if existing is None:
                try:
                    result = await self.catalog.create_mcp(
                        CreateMcpAssetCommand(
                            org_id=int(legacy.org_id),
                            owner_user_id=int(legacy.created_by_user_id),
                            slug=str(legacy.slug),
                            name=str(legacy.name),
                            description=legacy.description,
                            visibility="organization",
                            material=material,
                        )
                    )
                except AssetCatalogError as exc:
                    raise AssetUnificationMigrationError(exc.code, str(exc)) from exc
                existing = await self.db.get(Mcp, result.asset_id)
                assert existing is not None
            else:
                current = (
                    await self.db.get(McpVersion, int(existing.current_version_id))
                    if existing.current_version_id is not None
                    else None
                )
                expected = _canonical_digest(
                    {
                        "transport": material.transport,
                        "connection_config": material.connection_config,
                        "secret_refs": material.secret_refs,
                        "tools_schema": material.tools_schema,
                    }
                )
                if current is None or str(current.config_digest) != expected:
                    raise AssetUnificationMigrationError(
                        "LEGACY_MCP_IDENTITY_CONFLICT",
                        f"public_mcp_asset:{legacy.id} conflicts with mcp:{existing.id}",
                    )
            mapping[int(legacy.id)] = int(existing.id)
            if legacy.status != "active":
                existing.status = "archived"
                existing.archived_at = legacy.deleted_at or existing.archived_at
        await self.db.flush()
        return mapping

    async def _migrate_generic_public_mcps(self) -> dict[int, int]:
        rows = list(
            (
                await self.db.scalars(
                    select(LegacyPublicAsset).order_by(
                        LegacyPublicAsset.org_id.asc(),
                        LegacyPublicAsset.slug.asc(),
                        LegacyPublicAsset.created_at.asc(),
                        LegacyPublicAsset.id.asc(),
                    )
                )
            ).all()
        )
        unsupported = [row for row in rows if str(row.asset_type) != "mcp"]
        if unsupported:
            row = unsupported[0]
            raise AssetUnificationMigrationError(
                "LEGACY_GENERIC_ASSET_TYPE_UNSUPPORTED",
                f"public_asset:{row.id} has unsupported type {row.asset_type}",
            )

        mapping: dict[int, int] = {}
        by_identity: dict[tuple[int, str], Mcp] = {}
        active_by_identity: dict[tuple[int, str], bool] = {}
        for legacy in rows:
            material = _legacy_generic_mcp_material(legacy)
            key = (int(legacy.org_id), str(legacy.slug))
            mcp = by_identity.get(key)
            if mcp is None:
                mcp = await self.db.scalar(
                    select(Mcp).where(
                        Mcp.org_id == legacy.org_id,
                        Mcp.slug == legacy.slug,
                        Mcp.visibility != "private",
                    )
                )
            if mcp is None:
                result = await self.catalog.create_mcp(
                    CreateMcpAssetCommand(
                        org_id=int(legacy.org_id),
                        owner_user_id=int(legacy.publisher_user_id),
                        slug=str(legacy.slug),
                        name=str(legacy.name),
                        description=legacy.description,
                        visibility="organization",
                        material=material,
                    )
                )
                mcp = await self.db.get(Mcp, result.asset_id)
                assert mcp is not None
            else:
                mcp.archived_at = None
                mcp.status = "published"
                await self.catalog.publish_mcp_version(
                    PublishMcpVersionCommand(
                        org_id=int(legacy.org_id),
                        mcp_id=int(mcp.id),
                        material=material,
                    )
                )
            by_identity[key] = mcp
            mapping[int(legacy.id)] = int(mcp.id)
            active_by_identity[key] = active_by_identity.get(key, False) or str(
                legacy.status
            ) in {"active", "approved"}
        for key, mcp in by_identity.items():
            if not active_by_identity[key]:
                mcp.status = "archived"
                mcp.archived_at = mcp.archived_at or mcp.updated_at
        await self.db.flush()
        return mapping

    async def _migrate_installed_skills(
        self,
        *,
        skill_map: dict[int, int],
    ) -> tuple[dict[int, int], int]:
        installed_skills: dict[int, int] = {}
        personal_count = 0
        legacy_skillhub_links = await self._legacy_skillhub_links()
        for row in (await self.db.scalars(select(LegacyUserRuntimeSkill))).all():
            if row.source == "market" and row.public_skill_asset_id is not None:
                target = skill_map.get(int(row.public_skill_asset_id))
                if target is not None:
                    installed_skills[int(row.id)] = target
                continue
            if row.source not in {"personal", "skillhub"}:
                raise AssetUnificationMigrationError(
                    "LEGACY_SKILL_SOURCE_INVALID",
                    f"user_runtime_skill:{row.id} has unsupported source {row.source}",
                )
            migrated = await self._migrate_private_runtime_skill(row)
            if migrated is None:
                continue
            skill_id, version_id = migrated
            installed_skills[int(row.id)] = skill_id
            personal_count += 1
            if row.source == "skillhub":
                installation_id = legacy_skillhub_links.get(int(row.id))
                if installation_id is None:
                    raise AssetUnificationMigrationError(
                        "LEGACY_SKILLHUB_INSTALLATION_MISSING",
                        f"user_runtime_skill:{row.id} has no SkillHub installation",
                    )
                installation = await self.db.get(UserSkillHubInstallation, installation_id)
                if installation is None:
                    raise AssetUnificationMigrationError(
                        "LEGACY_SKILLHUB_INSTALLATION_MISSING",
                        f"user_skillhub_installation:{installation_id} is missing",
                    )
                installation.skill_id = skill_id
                installation.installed_skill_version_id = version_id
                installation.source_status = str(row.update_status)
        await self.db.flush()
        return installed_skills, personal_count

    async def _migrate_private_runtime_skill(
        self,
        row: LegacyUserRuntimeSkill,
    ) -> tuple[int, int] | None:
        slug = f"legacy-{row.source}-skill-{int(row.id)}"
        existing = await self.db.scalar(
            select(Skill).where(
                Skill.org_id == row.org_id,
                Skill.owner_user_id == row.user_id,
                Skill.slug == slug,
                Skill.asset_scope == "personal",
            )
        )
        if existing is not None:
            version = await self.db.get(SkillVersion, existing.current_version_id)
            if version is None:
                raise AssetUnificationMigrationError(
                    "LEGACY_PRIVATE_SKILL_VERSION_MISSING",
                    f"skill:{existing.id} has no current version",
                )
            source = dict(version.manifest or {}).get("legacy_source")
            if not isinstance(source, dict) or int(source.get("id", 0)) != int(row.id):
                raise AssetUnificationMigrationError(
                    "LEGACY_PRIVATE_SKILL_IDENTITY_CONFLICT",
                    f"user_runtime_skill:{row.id} conflicts with skill:{existing.id}",
                )
            return int(existing.id), int(version.id)

        binding = await self.db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.org_id == row.org_id,
                HermesRuntimeBinding.user_id == row.user_id,
                HermesRuntimeBinding.runtime_workspace_id == row.runtime_workspace_id,
            )
        )
        if binding is None:
            raise AssetUnificationMigrationError(
                "LEGACY_PRIVATE_SKILL_RUNTIME_MISSING",
                f"user_runtime_skill:{row.id} has no Runtime binding",
            )
        runtime_source = (runtime_skills_root(binding) / str(row.runtime_relative_path)).resolve(
            strict=False
        )
        expected = str(row.installed_package_sha256 or "").lower()
        source_path = runtime_source
        source_valid = source_path.is_dir() and (source_path / "SKILL.md").is_file()
        digest = sha256_directory(source_path) if source_valid else ""
        if not source_valid or (
            expected and expected != "baseline-v2" and digest != expected
        ):
            frozen = await self._matching_frozen_skill_source(row=row, digest=expected)
            if frozen is not None:
                source_path = frozen
                digest = expected
            elif not str(row.runtime_relative_path).startswith("personal/"):
                return None
            elif not source_valid:
                raise AssetUnificationMigrationError(
                    "LEGACY_PRIVATE_SKILL_ARTIFACT_MISSING",
                    f"user_runtime_skill:{row.id} directory is unavailable",
                )
            else:
                raise AssetUnificationMigrationError(
                    "LEGACY_PRIVATE_SKILL_DIGEST_MISMATCH",
                    f"user_runtime_skill:{row.id} directory digest differs from inventory",
                )
        target = (
            Path(settings.HERMES_PUBLIC_ASSET_ROOT)
            / "migrated-personal-skills"
            / str(row.org_id)
            / str(row.user_id)
            / f"legacy-{int(row.id)}"
            / "deployment"
        )
        if target.exists():
            if sha256_directory(target) != digest:
                raise AssetUnificationMigrationError(
                    "LEGACY_PRIVATE_SKILL_TARGET_CONFLICT",
                    f"user_runtime_skill:{row.id} migration target differs",
                )
        elif self.materialize_files:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_path, target)
            self.created_artifact_paths.append(target)
        result = await self.catalog.create_skill(
            CreateSkillAssetCommand(
                org_id=int(row.org_id),
                owner_user_id=int(row.user_id),
                slug=slug,
                name=str(row.name_snapshot),
                description=row.description_snapshot,
                asset_scope="personal",
                visibility="private",
                material=SkillVersionMaterial(
                    package_uri=str(target),
                    package_sha256=digest,
                    manifest={
                        "entrypoint": "SKILL.md",
                        "legacy_source": {
                            "table": "user_runtime_skill",
                            "id": int(row.id),
                            "source": str(row.source),
                        },
                    },
                ),
            )
        )
        skill = await self.db.get(Skill, result.asset_id)
        assert skill is not None
        skill.category_key = str(row.category_key_snapshot or "personal")
        return result.asset_id, result.version_id

    async def _matching_frozen_skill_source(
        self,
        *,
        row: LegacyUserRuntimeSkill,
        digest: str,
    ) -> Path | None:
        if len(digest) != 64:
            return None
        artifacts = (
            await self.db.scalars(
                select(ExpertVersionArtifact)
                .where(
                    ExpertVersionArtifact.artifact_type == "skill_package",
                    ExpertVersionArtifact.source_asset_id == row.id,
                    ExpertVersionArtifact.sha256 == digest,
                    ExpertVersionArtifact.status == "ready",
                    ExpertVersionArtifact.storage_uri.is_not(None),
                )
                .order_by(ExpertVersionArtifact.id.asc())
            )
        ).all()
        for artifact in artifacts:
            path = Path(str(artifact.storage_uri)).resolve(strict=False)
            if (
                path.is_dir()
                and (path / "SKILL.md").is_file()
                and sha256_directory(path) == digest
            ):
                return path
        return None

    async def _legacy_skillhub_links(self) -> dict[int, int]:
        connection = await self.db.connection()
        columns = await connection.run_sync(
            lambda sync_connection: {
                item["name"]
                for item in inspect(sync_connection).get_columns("user_skillhub_installation")
            }
        )
        if "user_runtime_skill_id" not in columns:
            return {}
        rows = (
            await self.db.execute(
                text(
                    "SELECT id, user_runtime_skill_id "
                    "FROM user_skillhub_installation "
                    "WHERE user_runtime_skill_id IS NOT NULL"
                )
            )
        ).all()
        return {int(row.user_runtime_skill_id): int(row.id) for row in rows}

    async def _migrate_profiles(
        self,
        *,
        skill_map: dict[int, int],
        skill_version_map: dict[int, int],
        mcp_map: dict[int, int],
        installed_skill_map: dict[int, int],
    ) -> int:
        profiles = list(
            (
                await self.db.scalars(
                    select(RuntimeAgentProfile).order_by(RuntimeAgentProfile.id.asc())
                )
            ).all()
        )
        migrated = 0
        for profile in profiles:
            if str(profile.status) == "deleted":
                continue
            expert_command = await self._expert_profile_migration_command(profile=profile)
            if expert_command is not None:
                result = await self._replace_profile(
                    profile=profile,
                    command=expert_command,
                )
                if result.created_revision:
                    migrated += 1
                continue
            skill_refs: dict[int, ProfileDesiredSkillRef] = {
                int(skill_id): ProfileDesiredSkillRef(skill_id=int(skill_id))
                for skill_id in await self.db.scalars(
                    select(ProfileSkillBinding.skill_id).where(
                        ProfileSkillBinding.runtime_agent_profile_id == profile.id,
                        ProfileSkillBinding.status == "enabled",
                    )
                )
            }
            old_skill_rows = (
                await self.db.scalars(
                    select(LegacyUserRuntimeSkill).where(
                        LegacyUserRuntimeSkill.runtime_agent_profile_id == profile.id
                    )
                )
            ).all()
            for row in old_skill_rows:
                skill_id = (
                    skill_map.get(int(row.public_skill_asset_id))
                    if row.source == "market" and row.public_skill_asset_id is not None
                    else installed_skill_map.get(int(row.id))
                )
                if skill_id is None:
                    if (
                        row.source == "personal"
                        and not str(row.runtime_relative_path).startswith("personal/")
                    ):
                        continue
                    raise AssetUnificationMigrationError(
                        "LEGACY_SKILL_MAPPING_MISSING",
                        f"user_runtime_skill:{row.id} has no stable Skill mapping",
                    )
                skill_refs[skill_id] = ProfileDesiredSkillRef(skill_id=skill_id)

            runtime_skills = (
                await self.db.scalars(
                    select(LegacyRuntimeSkillBinding).where(
                        LegacyRuntimeSkillBinding.runtime_agent_profile_id == profile.id,
                        LegacyRuntimeSkillBinding.status == "enabled",
                    )
                )
            ).all()
            for row in runtime_skills:
                candidate = ProfileDesiredSkillRef(
                    skill_id=int(row.skill_id),
                )
                existing = skill_refs.get(int(row.skill_id))
                if existing is not None and existing != candidate:
                    raise AssetUnificationMigrationError(
                        "LEGACY_PROFILE_SKILL_CONFLICT",
                        f"runtime_agent_profile:{profile.id} has conflicting Skill bindings",
                    )
                skill_refs[int(row.skill_id)] = candidate

            mcp_ids: set[int] = {
                int(value)
                for value in await self.db.scalars(
                    select(ProfileMcpBinding.mcp_id).where(
                        ProfileMcpBinding.runtime_agent_profile_id == profile.id,
                        ProfileMcpBinding.status == "enabled",
                    )
                )
            }
            old_mcp_rows = (
                await self.db.scalars(
                    select(LegacyUserRuntimeMcp).where(
                        LegacyUserRuntimeMcp.runtime_agent_profile_id == profile.id
                    )
                )
            ).all()
            for row in old_mcp_rows:
                mcp_id = mcp_map.get(int(row.public_mcp_asset_id))
                if mcp_id is None:
                    raise AssetUnificationMigrationError(
                        "LEGACY_MCP_MAPPING_MISSING",
                        f"user_runtime_mcp:{row.id} has no stable MCP mapping",
                    )
                mcp_ids.add(mcp_id)
            mcp_ids.update(
                int(value)
                for value in await self.db.scalars(
                    select(LegacyRuntimeMcpBinding.mcp_id).where(
                        LegacyRuntimeMcpBinding.runtime_agent_profile_id == profile.id,
                        LegacyRuntimeMcpBinding.status == "enabled",
                    )
                )
            )
            knowledge_ids = sorted(
                {
                    *(
                        int(value)
                        for value in await self.db.scalars(
                            select(ProfileKnowledgeBinding.knowledge_base_id).where(
                                ProfileKnowledgeBinding.runtime_agent_profile_id
                                == profile.id,
                                ProfileKnowledgeBinding.status == "enabled",
                            )
                        )
                    ),
                    *(
                    int(value)
                    for value in await self.db.scalars(
                        select(LegacyRuntimeKbBinding.knowledge_base_id).where(
                            LegacyRuntimeKbBinding.runtime_agent_profile_id == profile.id,
                            LegacyRuntimeKbBinding.status == "enabled",
                        )
                    )
                    ),
                }
            )
            capability_config = dict(profile.capability_config or {})
            plugin_ids = sorted(
                {str(item) for item in capability_config.get("plugin_ids", []) if str(item).strip()}
            )
            expert_version_id = await self.db.scalar(
                select(InstalledExpertBinding.expert_package_version_id).where(
                    InstalledExpertBinding.runtime_agent_profile_id == profile.id,
                    InstalledExpertBinding.status.in_(("enabled", "disabled")),
                )
            )
            if expert_version_id is None and profile.expert_package_id is not None:
                expert_version_id = await self.db.scalar(
                    select(ExpertPackageVersion.id)
                    .where(
                        ExpertPackageVersion.expert_package_id == profile.expert_package_id,
                        ExpertPackageVersion.status.in_(
                            ("private", "pending_review", "approved", "published")
                        ),
                    )
                    .order_by(ExpertPackageVersion.version.desc())
                    .limit(1)
                )
            result = await self._replace_profile(
                profile=profile,
                command=ProfileDesiredCommand(
                    org_id=int(profile.org_id),
                    user_id=int(profile.user_id),
                    runtime_workspace_id=int(profile.runtime_workspace_id),
                    runtime_agent_profile_id=int(profile.id),
                    definition=ProfileDesiredDefinition(
                        display_name=str(profile.display_name),
                        description=profile.description,
                        avatar_url=profile.avatar_url,
                        soul_content=str(profile.soul_content or ""),
                        system_prompt=capability_config.get("system_prompt"),
                        enterprise_knowledge_enabled=bool(
                            capability_config.get("enterprise_knowledge_enabled", False)
                        ),
                        plugin_ids=plugin_ids,
                    ),
                    skills=[skill_refs[key] for key in sorted(skill_refs)],
                    mcps=[ProfileDesiredMcpRef(mcp_id=value) for value in sorted(mcp_ids)],
                    knowledge_base_ids=knowledge_ids,
                    expert_package_version_id=(
                        int(expert_version_id) if expert_version_id is not None else None
                    ),
                    source_type="migration",
                    source_id="asset-unification-v1",
                    created_by_user_id=int(profile.user_id),
                )
            )
            if result.created_revision:
                migrated += 1
        return migrated

    async def _replace_profile(
        self,
        *,
        profile: RuntimeAgentProfile,
        command: ProfileDesiredCommand,
    ):
        try:
            return await ProfileDesiredService(self.db).replace(command)
        except ProfileDesiredError as exc:
            raise AssetUnificationMigrationError(
                f"LEGACY_PROFILE_{exc.code}",
                f"runtime_agent_profile:{profile.id}: {exc}",
            ) from exc

    async def _expert_profile_migration_command(
        self,
        *,
        profile: RuntimeAgentProfile,
    ) -> ProfileDesiredCommand | None:
        if str(profile.profile_kind) != "expert" or profile.expert_package_id is None:
            return None
        binding = await self.db.scalar(
            select(InstalledExpertBinding).where(
                InstalledExpertBinding.runtime_agent_profile_id == profile.id
            )
        )
        version: ExpertPackageVersion | None = None
        source_type = "private_expert"
        if binding is not None and str(binding.status) == "enabled":
            version = await self.db.get(
                ExpertPackageVersion,
                int(binding.expert_package_version_id),
            )
            source_type = "market_expert"
        elif binding is not None:
            return ProfileDesiredCommand(
                org_id=int(profile.org_id),
                user_id=int(profile.user_id),
                runtime_workspace_id=int(profile.runtime_workspace_id),
                runtime_agent_profile_id=int(profile.id),
                definition=ProfileDesiredDefinition(
                    display_name=str(profile.display_name),
                    description=profile.description,
                    avatar_url=profile.avatar_url,
                    soul_content=str(profile.soul_content or ""),
                ),
                source_type="migration",
                source_id="asset-unification-disabled-expert-v1",
                created_by_user_id=int(profile.user_id),
            )
        else:
            version = await self.db.scalar(
                select(ExpertPackageVersion)
                .where(
                    ExpertPackageVersion.expert_package_id == profile.expert_package_id,
                    ExpertPackageVersion.status.in_(
                        ("private", "pending_review", "approved", "published")
                    ),
                )
                .order_by(
                    ExpertPackageVersion.version.desc(),
                    ExpertPackageVersion.id.desc(),
                )
                .limit(1)
            )
        if version is None:
            return None
        return await ExpertRolloutService(self.db).build_desired_command(
            profile=profile,
            version=version,
            source_id="asset-unification-v1",
            created_by_user_id=int(profile.user_id),
            source_type=source_type,
        )


__all__ = [
    "AssetUnificationMigrationError",
    "AssetUnificationMigrationResult",
    "AssetUnificationMigrationService",
]
