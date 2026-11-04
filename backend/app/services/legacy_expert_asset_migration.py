from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ExpertPackage,
    ExpertPackageVersion,
    ExpertVersionArtifact,
    Mcp,
    RuntimeAgentProfile,
    Skill,
    SkillVersion,
)
from app.schemas.asset_catalog import (
    CreateSkillAssetCommand,
    PublishSkillVersionCommand,
    SkillVersionMaterial,
)
from app.services.asset_catalog import AssetCatalogService
from app.services.plaza.public_catalog.package_parser import sha256_directory


class LegacyExpertAssetMigrationError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class _UnresolvedReference:
    asset_type: str
    source: str
    legacy_asset_id: int
    legacy_version_id: int | None
    reason: str

    def as_manifest(self) -> dict[str, object]:
        return {
            "asset_type": self.asset_type,
            "source": self.source,
            "legacy_asset_id": self.legacy_asset_id,
            "legacy_version_id": self.legacy_version_id,
            "reason": self.reason,
        }


class LegacyExpertAssetMigrationService:
    """Convert immutable legacy expert snapshots to stable catalog references.

    Public catalog assets keep the approved follow-latest policy.  A Skill
    snapshot whose source identity no longer exists is preserved as a hidden
    personal lineage plus an expert-frozen copy; it is never guessed from a
    same-name current asset.  Unrecoverable references are retained as audit
    facts in the version manifest and are allowed only when no live Profile or
    installable market head still requires them.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.catalog = AssetCatalogService(db)
        self._legacy_sources_to_archive: set[int] = set()

    async def migrate(
        self,
        *,
        skill_map: dict[int, int],
        skill_version_map: dict[int, int],
        mcp_map: dict[int, int],
        installed_skill_map: dict[int, int],
    ) -> int:
        versions = list(
            (
                await self.db.scalars(
                    select(ExpertPackageVersion).order_by(ExpertPackageVersion.id.asc())
                )
            ).all()
        )
        changed = 0
        for version in versions:
            manifest = dict(version.manifest or {})
            if manifest.get("asset_identity_schema") == "stable-catalog/v1":
                continue
            package = await self.db.get(ExpertPackage, int(version.expert_package_id))
            if package is None:
                raise LegacyExpertAssetMigrationError(
                    "LEGACY_EXPERT_PACKAGE_MISSING",
                    f"expert_package_version:{version.id} has no package",
                )
            artifacts = list(
                (
                    await self.db.scalars(
                        select(ExpertVersionArtifact)
                        .where(ExpertVersionArtifact.expert_package_version_id == version.id)
                        .order_by(ExpertVersionArtifact.id.asc())
                    )
                ).all()
            )
            by_key: dict[tuple[str, str, int], list[ExpertVersionArtifact]] = defaultdict(list)
            for artifact in artifacts:
                if artifact.source_asset_id is not None:
                    by_key[
                        (
                            str(artifact.artifact_type),
                            str(artifact.source_type),
                            int(artifact.source_asset_id),
                        )
                    ].append(artifact)

            next_skills, skill_unresolved = await self._rewrite_skill_refs(
                version=version,
                package=package,
                artifacts=by_key,
                skill_map=skill_map,
                skill_version_map=skill_version_map,
                installed_skill_map=installed_skill_map,
            )
            next_mcps, mcp_unresolved = await self._rewrite_mcp_refs(
                version=version,
                artifacts=by_key,
                mcp_map=mcp_map,
            )
            next_knowledge, knowledge_unresolved = await self._rewrite_knowledge_refs(
                version=version
            )
            self._assert_unique_refs(version=version, refs=next_skills, asset_type="skill")
            self._assert_unique_refs(version=version, refs=next_mcps, asset_type="mcp")
            self._assert_unique_refs(
                version=version,
                refs=next_knowledge,
                asset_type="knowledge_base",
            )
            unresolved = skill_unresolved + mcp_unresolved + knowledge_unresolved
            if unresolved and await self._version_is_live_required(
                version=version,
                package=package,
                unresolved=unresolved,
            ):
                item = unresolved[0]
                raise LegacyExpertAssetMigrationError(
                    "LEGACY_EXPERT_REQUIRED_ASSET_UNRECOVERABLE",
                    (
                        f"expert_package_version:{version.id} requires unavailable "
                        f"{item.asset_type}:{item.legacy_asset_id}"
                    ),
                )

            version.skill_refs = next_skills
            version.mcp_refs = next_mcps
            version.knowledge_refs = next_knowledge
            capabilities = dict(manifest.get("capabilities") or {})
            capabilities["skill_ids"] = sorted(
                {
                    int(item["asset_id"])
                    for item in next_skills
                    if item.get("source") == "public"
                }
            )
            capabilities["mcp_ids"] = sorted(
                {int(item["asset_id"]) for item in next_mcps}
            )
            capabilities["knowledge_base_ids"] = sorted(
                {int(item["asset_id"]) for item in next_knowledge}
            )
            capabilities["skills"] = next_skills
            capabilities["mcps"] = next_mcps
            capabilities["knowledge_bases"] = next_knowledge
            manifest["capabilities"] = capabilities
            manifest["asset_identity_schema"] = "stable-catalog/v1"
            manifest["asset_migration"] = {
                "status": "partial" if unresolved else "complete",
                "unresolved_refs": [item.as_manifest() for item in unresolved],
            }
            version.manifest = manifest
            changed += 1

        for skill_id in self._legacy_sources_to_archive:
            skill = await self.db.get(Skill, skill_id)
            if skill is not None:
                skill.status = "archived"
                skill.archived_at = skill.archived_at or datetime.utcnow()
        await self.db.flush()
        return changed

    async def _rewrite_skill_refs(
        self,
        *,
        version: ExpertPackageVersion,
        package: ExpertPackage,
        artifacts: dict[tuple[str, str, int], list[ExpertVersionArtifact]],
        skill_map: dict[int, int],
        skill_version_map: dict[int, int],
        installed_skill_map: dict[int, int],
    ) -> tuple[list[dict[str, object]], list[_UnresolvedReference]]:
        result: list[dict[str, object]] = []
        unresolved: list[_UnresolvedReference] = []
        for raw in version.skill_refs or []:
            parsed = self._parse_ref(raw, legacy_key="skill_id", asset_type="skill")
            if parsed is None:
                continue
            source, old_id, old_version_id = parsed
            if source == "public" and old_id in skill_map:
                item: dict[str, object] = {
                    "source": "public",
                    "asset_id": skill_map[old_id],
                }
                if old_version_id is not None:
                    mapped_version = skill_version_map.get(old_version_id)
                    if mapped_version is None:
                        raise LegacyExpertAssetMigrationError(
                            "LEGACY_EXPERT_PUBLIC_SKILL_VERSION_MISSING",
                            f"expert_package_version:{version.id} references skill version {old_version_id}",
                        )
                    item["version_id"] = mapped_version
                await self._verify_public_skill_artifact(
                    version=version,
                    old_id=old_id,
                    old_version_id=old_version_id,
                    artifacts=artifacts,
                    skill_version_map=skill_version_map,
                )
                result.append(item)
                continue

            mapped_skill_id = installed_skill_map.get(old_id) if source == "default_installed" else None
            if mapped_skill_id is not None:
                mapped_skill = await self.db.get(Skill, mapped_skill_id)
                if mapped_skill is not None and str(mapped_skill.asset_scope) == "public":
                    result.append({"source": "public", "asset_id": mapped_skill_id})
                    continue

            artifact = self._single_artifact(
                artifacts,
                artifact_type="skill_package",
                source=source,
                old_id=old_id,
                version_id=int(version.id),
            )
            if artifact is None and source == "public":
                artifact = self._single_artifact(
                    artifacts,
                    artifact_type="skill_package",
                    source="public",
                    old_id=old_id,
                    version_id=int(version.id),
                )
            if artifact is None:
                unresolved.append(
                    _UnresolvedReference(
                        asset_type="skill",
                        source=source,
                        legacy_asset_id=old_id,
                        legacy_version_id=old_version_id,
                        reason="immutable_artifact_missing",
                    )
                )
                continue
            source_skill, source_version = await self._source_skill_for_artifact(
                package=package,
                artifact=artifact,
                mapped_skill_id=mapped_skill_id,
            )
            result.append(
                {
                    "source": "personal",
                    "asset_id": int(source_skill.id),
                    "version_id": int(source_version.id),
                }
            )
        return result, unresolved

    async def _rewrite_mcp_refs(
        self,
        *,
        version: ExpertPackageVersion,
        artifacts: dict[tuple[str, str, int], list[ExpertVersionArtifact]],
        mcp_map: dict[int, int],
    ) -> tuple[list[dict[str, object]], list[_UnresolvedReference]]:
        result: list[dict[str, object]] = []
        unresolved: list[_UnresolvedReference] = []
        for raw in version.mcp_refs or []:
            parsed = self._parse_ref(raw, legacy_key="mcp_id", asset_type="mcp")
            if parsed is None:
                continue
            source, old_id, old_version_id = parsed
            mapped = mcp_map.get(old_id)
            if mapped is not None:
                mcp = await self.db.get(Mcp, mapped)
                if mcp is None:
                    raise LegacyExpertAssetMigrationError(
                        "LEGACY_EXPERT_MCP_MAPPING_INVALID",
                        f"expert_package_version:{version.id} maps to missing mcp:{mapped}",
                    )
                result.append({"source": "public", "asset_id": mapped})
                continue
            artifact = self._single_artifact(
                artifacts,
                artifact_type="mcp_config",
                source=source,
                old_id=old_id,
                version_id=int(version.id),
            )
            unresolved.append(
                _UnresolvedReference(
                    asset_type="mcp",
                    source=source,
                    legacy_asset_id=old_id,
                    legacy_version_id=old_version_id,
                    reason=(
                        "stable_source_missing_with_audit_artifact"
                        if artifact is not None
                        else "immutable_artifact_missing"
                    ),
                )
            )
        return result, unresolved

    async def _rewrite_knowledge_refs(
        self,
        *,
        version: ExpertPackageVersion,
    ) -> tuple[list[dict[str, object]], list[_UnresolvedReference]]:
        from app.models import KnowledgeBase

        result: list[dict[str, object]] = []
        unresolved: list[_UnresolvedReference] = []
        for raw in version.knowledge_refs or []:
            parsed = self._parse_ref(
                raw,
                legacy_key="knowledge_base_id",
                asset_type="knowledge_base",
            )
            if parsed is None:
                continue
            source, old_id, old_version_id = parsed
            knowledge = await self.db.scalar(
                select(KnowledgeBase).where(
                    KnowledgeBase.id == old_id,
                    KnowledgeBase.org_id == version.org_id,
                )
            )
            if knowledge is None:
                unresolved.append(
                    _UnresolvedReference(
                        asset_type="knowledge_base",
                        source=source,
                        legacy_asset_id=old_id,
                        legacy_version_id=old_version_id,
                        reason="knowledge_reference_missing",
                    )
                )
                continue
            result.append({"source": source, "asset_id": old_id})
        return result, unresolved

    async def _verify_public_skill_artifact(
        self,
        *,
        version: ExpertPackageVersion,
        old_id: int,
        old_version_id: int | None,
        artifacts: dict[tuple[str, str, int], list[ExpertVersionArtifact]],
        skill_version_map: dict[int, int],
    ) -> None:
        artifact = self._single_artifact(
            artifacts,
            artifact_type="skill_package",
            source="public",
            old_id=old_id,
            version_id=int(version.id),
        )
        if artifact is None or old_version_id is None:
            return
        mapped_id = skill_version_map.get(old_version_id)
        mapped = await self.db.get(SkillVersion, mapped_id) if mapped_id is not None else None
        if mapped is None or str(mapped.package_sha256).lower() != str(artifact.sha256).lower():
            raise LegacyExpertAssetMigrationError(
                "LEGACY_EXPERT_PUBLIC_SKILL_ARTIFACT_CONFLICT",
                f"expert_package_version:{version.id} public skill artifact differs from catalog version",
            )

    async def _source_skill_for_artifact(
        self,
        *,
        package: ExpertPackage,
        artifact: ExpertVersionArtifact,
        mapped_skill_id: int | None,
    ) -> tuple[Skill, SkillVersion]:
        path, digest = self._validated_skill_artifact(artifact)
        if mapped_skill_id is not None:
            mapped = await self.db.get(Skill, mapped_skill_id)
            if mapped is not None:
                version = await self.db.scalar(
                    select(SkillVersion).where(
                        SkillVersion.skill_id == mapped.id,
                        SkillVersion.package_sha256 == digest,
                        SkillVersion.status != "deleted",
                    )
                )
                if version is not None:
                    return mapped, version

        legacy_id = int(artifact.source_asset_id or 0)
        slug = f"legacy-expert-source-{legacy_id}"
        source = await self.db.scalar(
            select(Skill).where(
                Skill.org_id == package.org_id,
                Skill.owner_user_id == package.owner_user_id,
                Skill.slug == slug,
                Skill.asset_scope == "personal",
            )
        )
        material = SkillVersionMaterial(
            package_uri=str(path),
            package_sha256=digest,
            manifest={
                "entrypoint": "SKILL.md",
                "legacy_expert_artifact": {
                    "artifact_id": int(artifact.id),
                    "source_type": str(artifact.source_type),
                    "source_asset_id": legacy_id,
                },
            },
        )
        if source is None:
            created = await self.catalog.create_skill(
                CreateSkillAssetCommand(
                    org_id=int(package.org_id),
                    owner_user_id=int(package.owner_user_id),
                    slug=slug,
                    name=str(artifact.name),
                    description=artifact.description,
                    asset_scope="personal",
                    visibility="private",
                    material=material,
                )
            )
            source = await self.db.get(Skill, created.asset_id)
            assert source is not None
            source_version = await self.db.get(SkillVersion, created.version_id)
        else:
            source.archived_at = None
            source.status = "private"
            created = await self.catalog.publish_skill_version(
                PublishSkillVersionCommand(
                    org_id=int(package.org_id),
                    skill_id=int(source.id),
                    material=material,
                )
            )
            source_version = await self.db.get(SkillVersion, created.version_id)
        assert source_version is not None
        self._legacy_sources_to_archive.add(int(source.id))
        return source, source_version

    @staticmethod
    def _validated_skill_artifact(artifact: ExpertVersionArtifact) -> tuple[Path, str]:
        digest = str(artifact.sha256 or "").lower()
        path = Path(str(artifact.storage_uri or "")).resolve(strict=False)
        if (
            str(artifact.status) != "ready"
            or len(digest) != 64
            or not path.is_dir()
            or not (path / "SKILL.md").is_file()
            or sha256_directory(path) != digest
        ):
            raise LegacyExpertAssetMigrationError(
                "LEGACY_EXPERT_SKILL_ARTIFACT_INVALID",
                f"expert_version_artifact:{artifact.id} is unavailable or has an invalid digest",
            )
        return path, digest

    @staticmethod
    def _single_artifact(
        artifacts: dict[tuple[str, str, int], list[ExpertVersionArtifact]],
        *,
        artifact_type: str,
        source: str,
        old_id: int,
        version_id: int,
    ) -> ExpertVersionArtifact | None:
        rows = artifacts.get((artifact_type, source, old_id), [])
        if len(rows) > 1:
            raise LegacyExpertAssetMigrationError(
                "LEGACY_EXPERT_ARTIFACT_AMBIGUOUS",
                f"expert_package_version:{version_id} has duplicate {artifact_type}:{old_id}",
            )
        return rows[0] if rows else None

    @staticmethod
    def _parse_ref(
        raw: object,
        *,
        legacy_key: str,
        asset_type: str,
    ) -> tuple[str, int, int | None] | None:
        if not isinstance(raw, dict):
            raise LegacyExpertAssetMigrationError(
                "LEGACY_EXPERT_REFERENCE_INVALID",
                f"expert {asset_type} reference must be an object",
            )
        value = raw.get("asset_id", raw.get(legacy_key))
        if value is None:
            raise LegacyExpertAssetMigrationError(
                "LEGACY_EXPERT_REFERENCE_INVALID",
                f"expert {asset_type} reference has no asset identity",
            )
        try:
            asset_id = int(value)
            version_id = int(raw["version_id"]) if raw.get("version_id") is not None else None
        except (TypeError, ValueError) as exc:
            raise LegacyExpertAssetMigrationError(
                "LEGACY_EXPERT_REFERENCE_INVALID",
                f"expert {asset_type} reference has a non-numeric identity",
            ) from exc
        if asset_id <= 0 or (version_id is not None and version_id <= 0):
            raise LegacyExpertAssetMigrationError(
                "LEGACY_EXPERT_REFERENCE_INVALID",
                f"expert {asset_type} reference identity must be positive",
            )
        return str(raw.get("source") or "public"), asset_id, version_id

    async def _version_is_live_required(
        self,
        *,
        version: ExpertPackageVersion,
        package: ExpertPackage,
        unresolved: list[_UnresolvedReference],
    ) -> bool:
        if str(package.status) == "published" and int(package.current_version_id or 0) == int(
            version.id
        ):
            return True
        profiles = (
            await self.db.scalars(
                select(RuntimeAgentProfile).where(
                    RuntimeAgentProfile.expert_package_id == package.id,
                    RuntimeAgentProfile.status != "deleted",
                )
            )
        ).all()
        unresolved_ids = {(item.asset_type, item.legacy_asset_id) for item in unresolved}
        for profile in profiles:
            config = dict(profile.capability_config or {})
            keys = {
                "skill": ("skills", "skill_ids"),
                "mcp": ("mcps", "mcp_ids"),
                "knowledge_base": ("knowledge_bases", "knowledge_base_ids"),
            }
            for asset_type, legacy_id in unresolved_ids:
                refs_key, ids_key = keys[asset_type]
                if legacy_id in {int(item) for item in config.get(ids_key, [])}:
                    return True
                for raw in config.get(refs_key, []) or []:
                    if isinstance(raw, dict) and int(raw.get("asset_id") or 0) == legacy_id:
                        return True
        return False

    @staticmethod
    def _assert_unique_refs(
        *,
        version: ExpertPackageVersion,
        refs: list[dict[str, object]],
        asset_type: str,
    ) -> None:
        asset_ids = [int(item["asset_id"]) for item in refs]
        if len(asset_ids) != len(set(asset_ids)):
            raise LegacyExpertAssetMigrationError(
                "LEGACY_EXPERT_STABLE_REFERENCE_DUPLICATE",
                f"expert_package_version:{version.id} contains duplicate stable {asset_type} refs",
            )


__all__ = [
    "LegacyExpertAssetMigrationError",
    "LegacyExpertAssetMigrationService",
]
