from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Skill, SkillVersion
from app.schemas.asset_rollout import AssetRolloutCommand
from app.services.application_agent_asset_rollout import (
    ApplicationAgentAssetRolloutService,
)
from app.services.asset_rollout import AssetRolloutService
from app.services.plaza.public_catalog.package_parser import sha256_directory
from app.services.plaza.public_catalog.package_parser import (
    normalize_skill_name,
    runtime_slug_from_normalized_name,
)


class SkillStorageMigrationError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class SkillPackagePlan:
    skill_id: int
    org_id: int
    slug: str
    source_root: Path
    target_root: Path
    deployment_uri: str
    old_roots: tuple[Path, ...] = ()


@dataclass
class SkillStorageMigrationResult:
    batch_id: str
    schema_aligned: bool
    public_skills: int = 0
    public_files_planned: int = 0
    public_files_migrated: int = 0
    deleted_files_cleaned: int = 0
    profile_rollouts: int = 0
    application_agents_refreshed: int = 0
    revoked_pending_review: list[int] = field(default_factory=list)
    approved_revoked: list[int] = field(default_factory=list)


def desired_market_visible(*, asset_scope: str, status: str, revoked_at: object) -> bool:
    """Return the safe backfill value for the market-only flag."""

    if asset_scope != "public":
        return True
    return status == "published" and revoked_at is None


def package_root_from_uri(*, storage_root: Path, package_uri: str) -> Path:
    root = storage_root.resolve(strict=False)
    candidate = Path(package_uri).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SkillStorageMigrationError(
            "SKILL_PACKAGE_PATH_OUTSIDE_ROOT",
            f"package path is outside configured asset root: {package_uri}",
        ) from exc
    if candidate.name in {"deployment", "original.zip"}:
        return candidate.parent
    raise SkillStorageMigrationError(
        "SKILL_PACKAGE_PATH_INVALID",
        f"package path must point to deployment or original.zip: {package_uri}",
    )


def _validate_package_root(package_root: Path, expected_sha256: str) -> None:
    original = package_root / "original.zip"
    deployment = package_root / "deployment"
    if not original.is_file() or not deployment.is_dir() or not (deployment / "SKILL.md").is_file():
        raise SkillStorageMigrationError(
            "SKILL_PACKAGE_INCOMPLETE",
            f"package is missing original.zip, deployment, or SKILL.md: {package_root}",
        )
    # SkillVersion.package_sha256 is the deployment-tree digest consumed by
    # Supervisor, not the raw uploaded ZIP digest.
    if expected_sha256 and sha256_directory(deployment).lower() != expected_sha256.lower():
        raise SkillStorageMigrationError(
            "SKILL_PACKAGE_DIGEST_MISMATCH",
            f"package digest mismatch: {package_root}",
        )


def plan_current_package(
    *, storage_root: Path, skill: Skill, version: SkillVersion
) -> SkillPackagePlan:
    if not version.package_uri or not version.package_sha256:
        raise SkillStorageMigrationError(
            "SKILL_CURRENT_PACKAGE_MISSING",
            f"current version has no package material: skill={skill.id}",
        )
    source_root = package_root_from_uri(
        storage_root=storage_root, package_uri=str(version.package_uri)
    )
    _validate_package_root(source_root, str(version.package_sha256))
    canonical_slug = runtime_slug_from_normalized_name(
        normalize_skill_name(str(skill.name))
    )
    target_root = (
        storage_root.resolve(strict=False)
        / "public-skills"
        / str(int(skill.org_id))
        / canonical_slug
        / "current"
    )
    old_roots: set[Path] = set()
    if target_root.parent.is_dir():
        old_roots.update(
            item
            for item in target_root.parent.iterdir()
            if item.is_dir() and item.name != "current"
        )
    if source_root != target_root:
        old_roots.add(source_root)
    return SkillPackagePlan(
        skill_id=int(skill.id),
        org_id=int(skill.org_id),
        slug=str(skill.slug),
        source_root=source_root,
        target_root=target_root,
        deployment_uri=str(target_root / "deployment"),
        old_roots=tuple(sorted(old_roots)),
    )


def plan_private_current_package(
    *,
    storage_root: Path,
    application_root: Path,
    skill: Skill,
    version: SkillVersion,
) -> SkillPackagePlan:
    if not version.package_uri or not version.package_sha256:
        raise SkillStorageMigrationError(
            "SKILL_CURRENT_PACKAGE_MISSING",
            f"current version has no package material: skill={skill.id}",
        )
    source_root = Path(str(version.package_uri)).resolve(strict=False)
    allowed_root = (
        application_root.resolve(strict=False)
        if str(skill.asset_scope) == "application"
        else storage_root.resolve(strict=False)
    )
    if source_root == allowed_root or not source_root.is_relative_to(allowed_root):
        raise SkillStorageMigrationError(
            "SKILL_PACKAGE_PATH_OUTSIDE_ROOT",
            f"private package path is outside its managed root: {source_root}",
        )
    if not source_root.is_dir() or not (source_root / "SKILL.md").is_file():
        raise SkillStorageMigrationError(
            "SKILL_PACKAGE_INCOMPLETE",
            f"private package is missing SKILL.md: {source_root}",
        )
    if sha256_directory(source_root).lower() != str(version.package_sha256).lower():
        raise SkillStorageMigrationError(
            "SKILL_PACKAGE_DIGEST_MISMATCH",
            f"private package digest mismatch: {source_root}",
        )
    canonical_slug = runtime_slug_from_normalized_name(
        normalize_skill_name(str(skill.name))
    )
    if str(skill.asset_scope) == "application":
        if skill.application_agent_id is None:
            raise SkillStorageMigrationError(
                "APPLICATION_SKILL_AGENT_MISSING",
                f"application Skill has no agent: {skill.id}",
            )
        target_root = (
            application_root.resolve(strict=False)
            / str(int(skill.org_id))
            / str(int(skill.application_agent_id))
            / "uploads"
            / "skills"
            / canonical_slug
            / "current"
        )
    else:
        target_root = (
            storage_root.resolve(strict=False)
            / "personal-skills"
            / str(int(skill.org_id))
            / str(int(skill.owner_user_id))
            / canonical_slug
            / "current"
        )
    old_roots: set[Path] = {source_root} if source_root != target_root else set()
    if target_root.parent.is_dir():
        old_roots.update(
            item
            for item in target_root.parent.iterdir()
            if item.is_dir() and item.name != "current"
        )
    return SkillPackagePlan(
        skill_id=int(skill.id), org_id=int(skill.org_id), slug=canonical_slug,
        source_root=source_root, target_root=target_root,
        deployment_uri=str(target_root), old_roots=tuple(sorted(old_roots)),
    )


def materialize_current_package(plan: SkillPackagePlan) -> None:
    """Copy verified content to current while retaining the old source."""

    if plan.source_root == plan.target_root:
        return
    plan.target_root.parent.mkdir(parents=True, exist_ok=True)
    staging = plan.target_root.parent / f".current.{uuid4().hex}.staging"
    backup = plan.target_root.parent / f".current.{uuid4().hex}.previous"
    try:
        shutil.copytree(plan.source_root, staging)
        if plan.target_root.exists():
            plan.target_root.replace(backup)
        staging.replace(plan.target_root)
        shutil.rmtree(backup, ignore_errors=True)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if backup.exists() and not plan.target_root.exists():
            backup.replace(plan.target_root)
        raise SkillStorageMigrationError(
            "SKILL_CURRENT_WRITE_FAILED",
            f"could not materialize current package: {plan.target_root}",
        ) from exc


def cleanup_old_package_roots(plan: SkillPackagePlan) -> int:
    removed = 0
    for old_root in plan.old_roots:
        if old_root.exists():
            shutil.rmtree(old_root)
            removed += 1
    return removed


class SkillStorageMigrationService:
    """Migrate Skill rows and public package trees in one explicit batch."""

    def __init__(
        self, db: AsyncSession, *, storage_root: str | Path, application_root: str | Path
    ) -> None:
        self.db = db
        self.storage_root = Path(storage_root)
        self.application_root = Path(application_root)
        self._plans: list[SkillPackagePlan] = []
        self._deleted_directories: set[Path] = set()

    async def apply(
        self,
        *,
        batch_id: str,
        created_by_user_id: int,
        approved_revoked_ids: Iterable[int] = (),
    ) -> SkillStorageMigrationResult:
        approved = {int(item) for item in approved_revoked_ids}
        skills = list(await self.db.scalars(select(Skill).order_by(Skill.id.asc())))
        result = SkillStorageMigrationResult(batch_id=batch_id, schema_aligned=True)
        public_ids = {int(skill.id) for skill in skills if str(skill.asset_scope) == "public"}
        unknown_approved = approved - public_ids
        if unknown_approved:
            raise SkillStorageMigrationError(
                "SKILL_APPROVAL_NOT_FOUND",
                f"approved revoked Skill IDs do not exist: {sorted(unknown_approved)}",
            )

        result.revoked_pending_review = [
            int(skill.id)
            for skill in skills
            if int(skill.id) in public_ids
            and (str(skill.status) == "revoked" or skill.revoked_at is not None)
            and int(skill.id) not in approved
        ]
        for skill in skills:
            if int(skill.id) in approved:
                if str(skill.status) != "revoked" and skill.revoked_at is None:
                    raise SkillStorageMigrationError(
                        "SKILL_APPROVAL_NOT_REVOKED",
                        f"approved Skill is not revoked: {skill.id}",
                    )
                skill.status = "published"
                skill.revoked_at = None
                result.approved_revoked.append(int(skill.id))
            skill.market_visible = desired_market_visible(
                asset_scope=str(skill.asset_scope),
                status=str(skill.status),
                revoked_at=skill.revoked_at,
            )
            if str(skill.asset_scope) != "public":
                if str(skill.status) != "deleted":
                    if skill.current_version_id is None:
                        raise SkillStorageMigrationError("SKILL_CURRENT_VERSION_MISSING", f"private Skill has no current version: {skill.id}")
                    version = await self.db.scalar(select(SkillVersion).where(SkillVersion.id == int(skill.current_version_id), SkillVersion.skill_id == int(skill.id)))
                    if version is None:
                        raise SkillStorageMigrationError("SKILL_CURRENT_VERSION_MISSING", f"private Skill version row is missing: {skill.id}")
                    plan = plan_private_current_package(storage_root=self.storage_root, application_root=self.application_root, skill=skill, version=version)
                    materialize_current_package(plan)
                    version.package_uri = plan.deployment_uri
                    self._plans.append(plan)
                continue
            result.public_skills += 1
            if str(skill.status) == "deleted":
                if skill.current_version_id is not None:
                    version = await self.db.scalar(
                        select(SkillVersion).where(SkillVersion.id == int(skill.current_version_id))
                    )
                    if version and version.package_uri:
                        self._deleted_directories.add(
                            package_root_from_uri(
                                storage_root=self.storage_root,
                                package_uri=str(version.package_uri),
                            ).parent
                        )
                continue
            if str(skill.status) != "published":
                continue
            if skill.current_version_id is None:
                raise SkillStorageMigrationError(
                    "SKILL_CURRENT_VERSION_MISSING",
                    f"published public Skill has no current version: {skill.id}",
                )
            version = await self.db.scalar(
                select(SkillVersion).where(
                    SkillVersion.id == int(skill.current_version_id),
                    SkillVersion.skill_id == int(skill.id),
                )
            )
            if version is None:
                raise SkillStorageMigrationError(
                    "SKILL_CURRENT_VERSION_MISSING",
                    f"current version row is missing: {skill.id}",
                )
            plan = plan_current_package(
                storage_root=self.storage_root, skill=skill, version=version
            )
            materialize_current_package(plan)
            version.package_uri = plan.deployment_uri
            self._plans.append(plan)
            result.public_files_planned += 1

        await self.db.flush()
        rollout_service = AssetRolloutService(self.db)
        app_rollout_service = ApplicationAgentAssetRolloutService(self.db)
        for skill in skills:
            if str(skill.asset_scope) == "application":
                continue
            if str(skill.status) in {"published", "private"}:
                rollout = await rollout_service.create(
                    AssetRolloutCommand(
                        rollout_id=f"{batch_id}-skill-{int(skill.id)}",
                        org_id=int(skill.org_id),
                        asset_type="skill",
                        asset_id=int(skill.id),
                        action="publish",
                        target_version_id=int(skill.current_version_id),
                        created_by_user_id=int(created_by_user_id),
                        change_summary="skill storage migration",
                    )
                )
                result.profile_rollouts += int(rollout.affected_profile_count)
                result.application_agents_refreshed += await app_rollout_service.refresh_public_asset(
                    org_id=int(skill.org_id),
                    asset_type="skill",
                    asset_id=int(skill.id),
                    created_by_user_id=int(created_by_user_id),
                )
            elif str(skill.status) == "deleted":
                rollout = await rollout_service.create(
                    AssetRolloutCommand(
                        rollout_id=f"{batch_id}-skill-{int(skill.id)}",
                        org_id=int(skill.org_id),
                        asset_type="skill",
                        asset_id=int(skill.id),
                        action="delete",
                        created_by_user_id=int(created_by_user_id),
                        change_summary="remove deleted Skill storage",
                    )
                )
                result.profile_rollouts += int(rollout.affected_profile_count)
                result.application_agents_refreshed += await app_rollout_service.remove_public_asset(
                    org_id=int(skill.org_id),
                    asset_type="skill",
                    asset_id=int(skill.id),
                    created_by_user_id=int(created_by_user_id),
                )
        return result

    def finalize_files(self, result: SkillStorageMigrationResult) -> None:
        for plan in self._plans:
            result.public_files_migrated += cleanup_old_package_roots(plan)
        for directory in self._deleted_directories:
            if directory.exists():
                shutil.rmtree(directory)
                result.deleted_files_cleaned += 1


__all__ = [
    "SkillPackagePlan",
    "SkillStorageMigrationError",
    "SkillStorageMigrationResult",
    "SkillStorageMigrationService",
    "cleanup_old_package_roots",
    "desired_market_visible",
    "materialize_current_package",
    "package_root_from_uri",
    "plan_current_package",
    "plan_private_current_package",
]
