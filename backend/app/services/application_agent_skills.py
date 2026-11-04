from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgent,
    ApplicationAgentSkill,
    Skill,
    SkillVersion,
)
from app.schemas.asset_catalog import (
    CreateSkillAssetCommand,
    PublishSkillVersionCommand,
    SkillVersionMaterial,
)
from app.services.asset_catalog import AssetCatalogError, AssetCatalogService
from app.services.personal_skill_categories import (
    PersonalSkillCategoryError,
    PersonalSkillCategoryService,
)
from app.services.plaza.public_catalog.package_parser import (
    SkillPackageError,
    extract_skill_package,
    normalize_skill_name,
    parse_skill_metadata,
    sha256_directory,
)
from app.services.runtime_workspace import Principal
from app.services.skill_description import (
    SkillDescriptionError,
    default_skill_description,
    normalize_skill_description,
)


MAX_APPLICATION_SKILL_PACKAGE_BYTES = 25 * 1024 * 1024


class ApplicationAgentSkillError(ValueError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ApplicationAgentSkillRecord:
    application_agent_skill_id: int
    application_agent_id: int
    org_id: int
    created_by_user_id: int
    slug: str
    name: str
    description: str | None
    category_key: str
    package_uri: str
    package_sha256: str
    manifest: dict[str, Any]
    skill_version_id: int | None = None
    version: int | None = None
    created_at: datetime | None = None


class InMemoryApplicationAgentSkillService:
    def __init__(self) -> None:
        self.records: dict[int, ApplicationAgentSkillRecord] = {}
        self.next_id = 1

    def reset(self) -> None:
        self.records.clear()
        self.next_id = 1

    async def list_skills(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> list[ApplicationAgentSkillRecord]:
        return sorted(
            (
                record
                for record in self.records.values()
                if record.org_id == principal.org_id
                and record.application_agent_id == agent_id
            ),
            key=lambda record: (
                record.created_at or datetime.min,
                record.application_agent_skill_id,
            ),
        )

    async def upload_skill(
        self,
        *,
        principal: Principal,
        agent_id: int,
        content: bytes,
        category_key: str,
        description: str | None = None,
    ) -> ApplicationAgentSkillRecord:
        metadata = _metadata(content)
        slug = _skill_slug(str(metadata["name"]))
        if any(
            record.org_id == principal.org_id
            and record.application_agent_id == agent_id
            and record.slug == slug
            for record in self.records.values()
        ):
            raise ApplicationAgentSkillError(
                "application_agent_skill_name_conflict"
            )
        record = ApplicationAgentSkillRecord(
            application_agent_skill_id=self.next_id,
            application_agent_id=agent_id,
            org_id=principal.org_id,
            created_by_user_id=principal.user_id,
            slug=slug,
            name=str(metadata["name"]),
            description=_description(metadata, description),
            category_key=category_key,
            package_uri="",
            package_sha256=hashlib.sha256(content).hexdigest(),
            manifest={
                "kind": "skill",
                "slug": slug,
                "entrypoint": "SKILL.md",
                "skill_metadata": metadata,
            },
            created_at=_now(),
        )
        self.records[record.application_agent_skill_id] = record
        self.next_id += 1
        return record

    async def get_skill(
        self,
        *,
        principal: Principal,
        agent_id: int,
        skill_id: int,
    ) -> ApplicationAgentSkillRecord:
        record = self.records.get(skill_id)
        if (
            record is None
            or record.org_id != principal.org_id
            or record.application_agent_id != agent_id
        ):
            raise ApplicationAgentSkillError(
                "application_agent_skill_not_found",
                status_code=404,
            )
        return record

    async def delete_skill(
        self,
        *,
        principal: Principal,
        agent_id: int,
        skill_id: int,
    ) -> None:
        await self.get_skill(
            principal=principal,
            agent_id=agent_id,
            skill_id=skill_id,
        )
        self.records.pop(skill_id, None)

    async def update_description(
        self,
        *,
        principal: Principal,
        agent_id: int,
        skill_id: int,
        description: str | None,
        name: str | None = None,
    ) -> ApplicationAgentSkillRecord:
        record = await self.get_skill(
            principal=principal,
            agent_id=agent_id,
            skill_id=skill_id,
        )
        if record.created_by_user_id != principal.user_id:
            raise ApplicationAgentSkillError(
                "application_agent_owner_required",
                status_code=403,
            )
        try:
            value = normalize_skill_description(
                description,
                default=default_skill_description(record.manifest),
            )
        except SkillDescriptionError as exc:
            raise ApplicationAgentSkillError(str(exc), status_code=400) from exc
        updated = ApplicationAgentSkillRecord(
            **{
                **record.__dict__,
                "description": value,
                "name": name.strip() if name else record.name,
            }
        )
        self.records[skill_id] = updated
        return updated


class SqlAlchemyApplicationAgentSkillService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_skills(
        self,
        *,
        principal: Principal,
        agent_id: int,
    ) -> list[ApplicationAgentSkillRecord]:
        await self._agent(principal=principal, agent_id=agent_id)
        rows = (
            await self.db.execute(
                select(Skill, SkillVersion)
                .join(SkillVersion, SkillVersion.id == Skill.current_version_id)
                .where(
                    Skill.org_id == principal.org_id,
                    Skill.application_agent_id == agent_id,
                    Skill.asset_scope == "application",
                    Skill.status != "deleted",
                )
                .order_by(
                    Skill.created_at.asc(),
                    Skill.id.asc(),
                )
            )
        ).all()
        return [self._record(skill, version) for skill, version in rows]

    async def upload_skill(
        self,
        *,
        principal: Principal,
        agent_id: int,
        content: bytes,
        category_key: str,
        description: str | None = None,
    ) -> ApplicationAgentSkillRecord:
        agent = await self._agent(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
            require_owner=True,
        )
        category = await self._category(
            principal=principal,
            category_key=category_key,
        )
        metadata = _metadata(content)
        name = str(metadata["name"])
        slug = _skill_slug(name)
        selected_market_ids = [
            int(value) for value in (dict(agent.config or {}).get("skill_asset_ids") or [])
        ]
        if selected_market_ids:
            market_names = await self.db.scalars(
                select(Skill.name).where(
                    Skill.id.in_(selected_market_ids),
                    Skill.org_id == principal.org_id,
                    Skill.asset_scope == "public",
                    Skill.status != "deleted",
                )
            )
            if normalize_skill_name(name) in {
                normalize_skill_name(str(item)) for item in market_names
            }:
                raise ApplicationAgentSkillError("application_agent_skill_name_conflict")
        existing = await self.db.scalar(
            select(Skill)
            .where(
                Skill.org_id == principal.org_id,
                Skill.application_agent_id == agent_id,
                Skill.asset_scope == "application",
                Skill.slug == _catalog_slug(agent_id=agent_id, slug=slug),
            )
            .with_for_update()
        )
        target = (
            _agent_root(org_id=principal.org_id, agent_id=agent_id)
            / "uploads"
            / "skills"
            / slug
            / "current"
        )
        asset_root = target.parent
        staging = asset_root / f".staging-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}-{uuid.uuid4().hex[:8]}"
        backup = asset_root / f".previous-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}-{uuid.uuid4().hex[:8]}"
        try:
            files, skill_md = await asyncio.to_thread(
                extract_skill_package,
                content,
                staging,
            )
            package_sha256 = await asyncio.to_thread(
                sha256_directory,
                staging,
            )
            asset_root.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.replace(backup)
            staging.replace(target)
            manifest = {
                "kind": "skill",
                "slug": slug,
                "entrypoint": "SKILL.md",
                "files": files,
                "readme_excerpt": skill_md.strip()[:600],
                "skill_metadata": metadata,
            }
            material = SkillVersionMaterial(
                package_uri=str(target),
                package_sha256=package_sha256,
                manifest=manifest,
            )
            catalog = AssetCatalogService(self.db)
            if existing is None:
                result = await catalog.create_skill(
                    CreateSkillAssetCommand(
                        org_id=principal.org_id,
                        owner_user_id=principal.user_id,
                        application_agent_id=agent_id,
                        slug=_catalog_slug(agent_id=agent_id, slug=slug),
                        name=name,
                        description=_description(metadata, description),
                        category_key=category,
                        asset_scope="application",
                        visibility="private",
                        material=material,
                    )
                )
            else:
                # Application Skill deletion hides the row and removes its
                # current version, but the catalog also has a stable
                # (org, owner, slug) uniqueness constraint.  Reopen that
                # historical row so a same-name upload does not collide with
                # an invisible deleted record and can continue its version
                # history.
                if existing.status == "deleted":
                    existing.status = "private"
                    existing.archived_at = None
                    existing.revoked_at = None
                existing.name = name
                existing.description = _description(metadata, description)
                existing.category_key = category
                result = await catalog.publish_skill_version(
                    PublishSkillVersionCommand(
                        org_id=principal.org_id,
                        skill_id=int(existing.id),
                        material=material,
                    )
                )
            await self.db.commit()
            skill = await self.db.get(Skill, result.asset_id)
            version = await self.db.get(SkillVersion, result.version_id)
            assert skill is not None and version is not None
            if Path(str(version.package_uri or "")).resolve(strict=False) != target.resolve(strict=False):
                await asyncio.to_thread(shutil.rmtree, target, True)
        except IntegrityError as exc:
            await self.db.rollback()
            await asyncio.to_thread(shutil.rmtree, target, True)
            if backup.exists():
                backup.replace(target)
            raise ApplicationAgentSkillError(
                "application_agent_skill_name_conflict"
            ) from exc
        except AssetCatalogError as exc:
            await self.db.rollback()
            await asyncio.to_thread(shutil.rmtree, target, True)
            if backup.exists():
                backup.replace(target)
            raise ApplicationAgentSkillError(
                "application_agent_skill_name_conflict"
                if exc.code == "SKILL_SLUG_EXISTS"
                else "application_agent_skill_catalog_failed"
            ) from exc
        except Exception:
            await self.db.rollback()
            await asyncio.to_thread(shutil.rmtree, target, True)
            if backup.exists():
                backup.replace(target)
            raise
        if backup.exists():
            await asyncio.to_thread(shutil.rmtree, backup, True)
        return self._record(skill, version)

    async def get_skill(
        self,
        *,
        principal: Principal,
        agent_id: int,
        skill_id: int,
    ) -> ApplicationAgentSkillRecord:
        await self._agent(principal=principal, agent_id=agent_id)
        row = (
            await self.db.execute(
                select(Skill, SkillVersion)
                .join(SkillVersion, SkillVersion.id == Skill.current_version_id)
                .where(
                    Skill.id == skill_id,
                    Skill.org_id == principal.org_id,
                    Skill.application_agent_id == agent_id,
                    Skill.asset_scope == "application",
                    Skill.status != "deleted",
                )
            )
        ).first()
        if row is None:
            raise ApplicationAgentSkillError(
                "application_agent_skill_not_found",
                status_code=404,
            )
        return self._record(row[0], row[1])

    async def delete_skill(
        self,
        *,
        principal: Principal,
        agent_id: int,
        skill_id: int,
    ) -> None:
        await self._agent(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
            require_owner=True,
        )
        row = (
            await self.db.execute(
                select(Skill).where(
                    Skill.id == skill_id,
                    Skill.org_id == principal.org_id,
                    Skill.application_agent_id == agent_id,
                    Skill.asset_scope == "application",
                    Skill.status != "deleted",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ApplicationAgentSkillError(
                "application_agent_skill_not_found",
                status_code=404,
            )
        versions = list(
            (
                await self.db.execute(
                    select(SkillVersion).where(SkillVersion.skill_id == row.id)
                )
            ).scalars()
        )
        sources = [
            _validated_unified_source(
                package_uri=str(version.package_uri or ""),
                org_id=principal.org_id,
                agent_id=agent_id,
            )
            for version in versions
        ]
        moved: list[tuple[Path, Path]] = []
        try:
            for source in sources:
                trash = source.with_name(f".deleting-{source.name}-{uuid.uuid4().hex}")
                if source.exists():
                    source.replace(trash)
                    moved.append((source, trash))
            row.status = "deleted"
            row.current_version_id = None
            for version in versions:
                version.status = "deleted"
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            for source, trash in reversed(moved):
                if trash.exists():
                    trash.replace(source)
            raise
        for _source, trash in moved:
            await asyncio.to_thread(shutil.rmtree, trash, True)
        # Remove orphaned timestamp releases from the pre-current layout as
        # well as the current directory. The catalog/version history stays in
        # MySQL so audit and binding references remain readable.
        skill_slug = str(row.slug).removeprefix(f"app-{agent_id}-")
        await asyncio.to_thread(
            shutil.rmtree,
            _agent_root(org_id=principal.org_id, agent_id=agent_id)
            / "uploads"
            / "skills"
            / skill_slug,
            True,
        )

    async def update_description(
        self,
        *,
        principal: Principal,
        agent_id: int,
        skill_id: int,
        description: str | None,
        name: str | None = None,
    ) -> ApplicationAgentSkillRecord:
        await self._agent(
            principal=principal,
            agent_id=agent_id,
            for_update=True,
            require_owner=True,
        )
        row = (
            await self.db.execute(
                select(Skill, SkillVersion)
                .join(SkillVersion, SkillVersion.id == Skill.current_version_id)
                .where(
                    Skill.id == skill_id,
                    Skill.org_id == principal.org_id,
                    Skill.application_agent_id == agent_id,
                    Skill.asset_scope == "application",
                    Skill.status != "deleted",
                )
                .with_for_update()
            )
        ).first()
        if row is None:
            raise ApplicationAgentSkillError(
                "application_agent_skill_not_found",
                status_code=404,
            )
        skill, version = row
        if name is not None:
            agent = await self._agent(principal=principal, agent_id=agent_id)
            selected_ids = [
                int(item) for item in (dict(agent.config or {}).get("skill_asset_ids") or [])
            ]
            names = await self.db.scalars(
                select(Skill.name).where(
                    Skill.id.in_(selected_ids),
                    Skill.org_id == principal.org_id,
                    Skill.asset_scope == "public",
                    Skill.status != "deleted",
                )
            ) if selected_ids else []
            if normalize_skill_name(name) in {
                normalize_skill_name(str(item)) for item in names
            }:
                raise ApplicationAgentSkillError("application_agent_skill_name_conflict")
            skill.name = name.strip()
        try:
            value = normalize_skill_description(
                description,
                default=default_skill_description(version.manifest),
            )
        except SkillDescriptionError as exc:
            raise ApplicationAgentSkillError(str(exc), status_code=400) from exc
        skill.description = value
        await self.db.commit()
        return self._record(skill, version)

    async def _agent(
        self,
        *,
        principal: Principal,
        agent_id: int,
        for_update: bool = False,
        require_owner: bool = False,
    ) -> ApplicationAgent:
        statement = select(ApplicationAgent).where(
            ApplicationAgent.id == agent_id,
            ApplicationAgent.org_id == principal.org_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self.db.execute(statement)).scalar_one_or_none()
        if row is None:
            raise ApplicationAgentSkillError(
                "application_agent_not_found",
                status_code=404,
            )
        if require_owner and int(row.created_by_user_id) != principal.user_id:
            raise ApplicationAgentSkillError(
                "application_agent_owner_required",
                status_code=403,
            )
        return row

    async def _category(
        self,
        *,
        principal: Principal,
        category_key: str,
    ) -> str:
        category = category_key.strip() or "uncategorized"
        # Older Studio clients used this value for an application-local
        # category. Keep it as a compatibility alias while new clients use
        # the same category contract as personal Skill uploads.
        if category == "personal":
            return category
        try:
            normalized, _scope = await PersonalSkillCategoryService(
                self.db
            ).validate_upload_category(
                principal=principal,
                category_key=category,
            )
        except PersonalSkillCategoryError as exc:
            raise ApplicationAgentSkillError(
                "application_agent_skill_category_invalid"
            ) from exc
        return normalized

    @staticmethod
    def _record(row: Skill, version: SkillVersion) -> ApplicationAgentSkillRecord:
        manifest = dict(version.manifest or {})
        return ApplicationAgentSkillRecord(
            application_agent_skill_id=int(row.id),
            application_agent_id=int(row.application_agent_id or 0),
            org_id=int(row.org_id),
            created_by_user_id=int(row.owner_user_id),
            slug=str(manifest.get("slug") or row.slug),
            name=str(row.name),
            description=str(row.description) if row.description else None,
            category_key=str(row.category_key),
            package_uri=str(version.package_uri or ""),
            package_sha256=str(version.package_sha256 or ""),
            manifest=manifest,
            skill_version_id=int(version.id),
            version=int(version.version),
            created_at=row.created_at,
        )


def _metadata(content: bytes) -> dict[str, Any]:
    if not content:
        raise ApplicationAgentSkillError(
            "application_agent_skill_package_empty",
            status_code=400,
        )
    if len(content) > MAX_APPLICATION_SKILL_PACKAGE_BYTES:
        raise ApplicationAgentSkillError(
            "application_agent_skill_package_too_large",
            status_code=413,
        )
    try:
        return parse_skill_metadata(content)
    except SkillPackageError as exc:
        raise ApplicationAgentSkillError(
            "application_agent_skill_package_invalid",
            status_code=400,
        ) from exc


def _skill_slug(name: str) -> str:
    normalized = normalize_skill_name(name)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    if not slug:
        slug = "skill-" + hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()[:16]
    if len(slug) > 120:
        suffix = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        slug = f"{slug[:103].rstrip('-')}-{suffix}"
    if len(slug) < 3:
        slug = f"{slug}-skill"
    return unicodedata.normalize("NFKC", slug)


def _catalog_slug(*, agent_id: int, slug: str) -> str:
    prefix = f"app-{agent_id}-"
    available = 128 - len(prefix)
    if available < 3:
        raise ApplicationAgentSkillError(
            "application_agent_skill_path_invalid",
            status_code=500,
        )
    return f"{prefix}{slug[:available].rstrip('-')}"


def _description(metadata: dict[str, Any], custom: str | None = None) -> str | None:
    value = metadata.get("description")
    default = str(value) if isinstance(value, str) and value else None
    try:
        return normalize_skill_description(custom, default=default)
    except SkillDescriptionError as exc:
        raise ApplicationAgentSkillError(str(exc), status_code=400) from exc


def _agent_root(*, org_id: int, agent_id: int) -> Path:
    boundary = Path(
        settings.HERMES_APPLICATION_AGENT_RUNTIME_ROOT
    ).resolve(strict=False)
    root = (boundary / str(org_id) / str(agent_id)).resolve(strict=False)
    if root == boundary or not root.is_relative_to(boundary):
        raise ApplicationAgentSkillError(
            "application_agent_skill_path_invalid",
            status_code=500,
        )
    return root


def _skill_source_directory(
    *,
    org_id: int,
    agent_id: int,
    skill_id: int,
) -> Path:
    return (
        _agent_root(org_id=org_id, agent_id=agent_id)
        / "uploads"
        / "skills"
        / str(skill_id)
    )


def _validated_recorded_source(
    *,
    row: ApplicationAgentSkill,
    org_id: int,
    agent_id: int,
) -> Path:
    expected = _skill_source_directory(
        org_id=org_id,
        agent_id=agent_id,
        skill_id=int(row.id),
    ).resolve(strict=False)
    recorded = Path(str(row.package_uri)).resolve(strict=False)
    if recorded != expected:
        raise ApplicationAgentSkillError(
            "application_agent_skill_path_invalid",
            status_code=500,
        )
    return expected


def _validated_unified_source(
    *,
    package_uri: str,
    org_id: int,
    agent_id: int,
) -> Path:
    managed = (
        _agent_root(org_id=org_id, agent_id=agent_id)
        / "uploads"
        / "skills"
    ).resolve(strict=False)
    recorded = Path(package_uri).resolve(strict=False)
    if recorded == managed or not recorded.is_relative_to(managed):
        raise ApplicationAgentSkillError(
            "application_agent_skill_path_invalid",
            status_code=500,
        )
    return recorded


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
