from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from zipfile import BadZipFile, ZipFile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Skill, SkillVersion
from app.config import settings
from app.schemas.asset_catalog import (
    CreateSkillAssetCommand,
    PublishSkillVersionCommand,
    SkillVersionMaterial,
)
from app.services.asset_catalog import AssetCatalogError, AssetCatalogService
from app.services.asset_rollout import AssetRolloutService
from app.schemas.asset_rollout import AssetRolloutCommand
from app.services.plaza.public_catalog.package_parser import (
    SkillPackageError,
    extract_skill_package,
    normalize_skill_name,
    parse_skill_metadata,
    runtime_slug_from_normalized_name,
    sha256_directory,
    skill_excerpt,
    validate_skill_slug,
)
from app.services.runtime_workspace import Principal
from app.hermes_runtime.skill_visibility_policy import is_platform_default_skill_name
from app.services.skill_description import (
    SkillDescriptionError,
    default_skill_description,
    normalize_skill_description,
)


class PersonalSkillAssetError(ValueError):
    pass


class PersonalSkillPackageStorage:
    """Owns cleanup of personal Skill package directories on the PVC."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def remove_all(self, *, org_id: int, user_id: int, slug: str) -> None:
        managed_root = (self.root / "personal-skills").resolve()
        asset_root = (managed_root / str(org_id) / str(user_id) / slug).resolve()
        if managed_root not in asset_root.parents:
            raise PersonalSkillAssetError("personal skill package path is unmanaged")
        shutil.rmtree(asset_root, ignore_errors=True)


@dataclass(frozen=True)
class PersonalSkillAssetRecord:
    skill_id: int
    org_id: int
    owner_user_id: int
    slug: str
    name: str
    description: str | None
    category_key: str
    status: str
    skill_version_id: int | None
    version: int | None
    package_uri: str | None
    manifest: dict[str, Any]
    created_at: datetime | None = None


@dataclass(frozen=True)
class PersonalSkillUploadResult:
    record: PersonalSkillAssetRecord
    created_package_path: Path | None


class PersonalSkillAssetStore(Protocol):
    async def create(
        self,
        *,
        principal: Principal,
        slug: str,
        name: str,
        description: str | None,
        category_key: str,
        version: int,
        package_uri: str,
        package_sha256: str,
        manifest: dict[str, Any],
    ) -> PersonalSkillAssetRecord:
        ...

    async def list_for_user(self, *, principal: Principal) -> list[PersonalSkillAssetRecord]:
        ...

    async def get_for_user(self, *, principal: Principal, skill_id: int) -> PersonalSkillAssetRecord | None:
        ...

    async def get_version_for_user(
        self,
        *,
        principal: Principal,
        skill_id: int,
        skill_version_id: int | None,
    ) -> PersonalSkillAssetRecord | None:
        ...

    async def update_description(
        self,
        *,
        principal: Principal,
        skill_id: int,
        description: str | None,
        name: str | None = None,
    ) -> PersonalSkillAssetRecord:
        ...

    async def delete_skill(self, *, principal: Principal, skill_id: int) -> None:
        ...


class InMemoryPersonalSkillAssetStore:
    def __init__(self) -> None:
        self.records: dict[int, PersonalSkillAssetRecord] = {}
        self.next_skill_id = 1
        self.next_version_id = 1

    async def create(
        self,
        *,
        principal: Principal,
        slug: str,
        name: str,
        description: str | None,
        category_key: str,
        version: int,
        package_uri: str,
        package_sha256: str,
        manifest: dict[str, Any],
    ) -> PersonalSkillAssetRecord:
        del package_sha256
        record = PersonalSkillAssetRecord(
            skill_id=self.next_skill_id,
            org_id=principal.org_id,
            owner_user_id=principal.user_id,
            slug=slug,
            name=name,
            description=description,
            category_key=category_key,
            status="private",
            skill_version_id=self.next_version_id,
            version=version,
            package_uri=package_uri,
            manifest=manifest,
        )
        existing = next(
            (
                item
                for item in self.records.values()
                if item.org_id == principal.org_id
                and item.owner_user_id == principal.user_id
                and item.slug == slug
                and item.status != "deleted"
            ),
            None,
        )
        if existing is not None:
            record = PersonalSkillAssetRecord(
                skill_id=existing.skill_id,
                org_id=existing.org_id,
                owner_user_id=existing.owner_user_id,
                slug=existing.slug,
                name=name,
                description=description,
                category_key=category_key,
                status="private",
                skill_version_id=self.next_version_id,
                version=int(existing.version or 0) + 1,
                package_uri=package_uri,
                manifest=manifest,
                created_at=existing.created_at,
            )
        else:
            self.next_skill_id += 1
        self.records[record.skill_id] = record
        self.next_version_id += 1
        return record

    async def list_for_user(self, *, principal: Principal) -> list[PersonalSkillAssetRecord]:
        return sorted(
            (
                record
                for record in self.records.values()
                if record.org_id == principal.org_id and record.owner_user_id == principal.user_id and record.status != "deleted"
            ),
            key=lambda record: (record.slug, record.version or 0),
        )

    async def get_for_user(self, *, principal: Principal, skill_id: int) -> PersonalSkillAssetRecord | None:
        record = self.records.get(skill_id)
        if record is None or record.org_id != principal.org_id or record.owner_user_id != principal.user_id or record.status == "deleted":
            return None
        return record

    async def get_version_for_user(
        self,
        *,
        principal: Principal,
        skill_id: int,
        skill_version_id: int | None,
    ) -> PersonalSkillAssetRecord | None:
        record = await self.get_for_user(principal=principal, skill_id=skill_id)
        if record is None:
            return None
        if skill_version_id is not None and record.skill_version_id != skill_version_id:
            return None
        return record

    async def delete_skill(self, *, principal: Principal, skill_id: int) -> None:
        record = self.records.get(skill_id)
        if record is None or record.org_id != principal.org_id or record.owner_user_id != principal.user_id or record.status == "deleted":
            raise PersonalSkillAssetError("personal skill not found")
        self.records[skill_id] = PersonalSkillAssetRecord(
            **{
                **record.__dict__,
                "slug": _deleted_slug(record.slug, record.skill_id),
                "status": "deleted",
            }
        )

    async def update_description(
        self,
        *,
        principal: Principal,
        skill_id: int,
        description: str | None,
        name: str | None = None,
    ) -> PersonalSkillAssetRecord:
        record = await self.get_for_user(principal=principal, skill_id=skill_id)
        if record is None:
            raise PersonalSkillAssetError("personal skill not found")
        try:
            value = normalize_skill_description(
                description,
                default=default_skill_description(record.manifest),
            )
        except SkillDescriptionError as exc:
            raise PersonalSkillAssetError(str(exc)) from exc
        updated = PersonalSkillAssetRecord(
            **{
                **record.__dict__,
                "description": value,
                "name": name.strip() if name else record.name,
            }
        )
        self.records[skill_id] = updated
        return updated

in_memory_personal_skill_asset_store = InMemoryPersonalSkillAssetStore()


class SqlAlchemyPersonalSkillAssetStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        principal: Principal,
        slug: str,
        name: str,
        description: str | None,
        category_key: str,
        version: int,
        package_uri: str,
        package_sha256: str,
        manifest: dict[str, Any],
    ) -> PersonalSkillAssetRecord:
        del version
        public_name_conflict = await self.db.scalar(
            select(Skill.id).where(
                Skill.org_id == principal.org_id,
                Skill.asset_scope == "public",
                Skill.status != "deleted",
                Skill.market_visible.is_(True),
            )
        )
        if public_name_conflict is not None:
            public_names = await self.db.scalars(
                select(Skill.name).where(
                    Skill.org_id == principal.org_id,
                    Skill.asset_scope == "public",
                    Skill.status != "deleted",
                    Skill.market_visible.is_(True),
                )
            )
            if normalize_skill_name(name) in {
                normalize_skill_name(str(item)) for item in public_names
            }:
                raise PersonalSkillAssetError("personal_skill_name_conflict")
        material = SkillVersionMaterial(
            package_uri=package_uri,
            package_sha256=package_sha256,
            manifest=manifest,
        )
        existing = await self.db.scalar(
            select(Skill)
            .where(
                Skill.org_id == principal.org_id,
                Skill.owner_user_id == principal.user_id,
                Skill.slug == slug,
                Skill.asset_scope == "personal",
                Skill.status != "deleted",
            )
            .with_for_update()
        )
        source_version_id = (
            int(existing.current_version_id)
            if existing is not None and existing.current_version_id is not None
            else None
        )
        try:
            if existing is None:
                result = await AssetCatalogService(self.db).create_skill(
                    CreateSkillAssetCommand(
                        org_id=principal.org_id,
                        owner_user_id=principal.user_id,
                        slug=slug,
                        name=name,
                        description=description,
                        category_key=category_key,
                        asset_scope="personal",
                        visibility="private",
                        material=material,
                    )
                )
            else:
                # Archiving hides an asset from Runtime execution but keeps
                # its stable slug and immutable version history.  A later
                # upload with the same name reopens that asset instead of
                # reporting a name conflict for an invisible row.
                if existing.status == "archived" or existing.archived_at is not None:
                    existing.status = "private"
                    existing.archived_at = None
                    existing.revoked_at = None
                existing.name = name
                existing.description = description
                existing.category_key = category_key
                result = await AssetCatalogService(self.db).publish_skill_version(
                    PublishSkillVersionCommand(
                        org_id=principal.org_id,
                        skill_id=int(existing.id),
                        material=material,
                    )
                )
        except AssetCatalogError as exc:
            raise PersonalSkillAssetError(str(exc)) from exc
        await self.db.flush()
        skill = await self.db.get(Skill, result.asset_id)
        skill_version = await self.db.get(SkillVersion, result.version_id)
        assert skill is not None and skill_version is not None
        if source_version_id is not None and source_version_id != result.version_id:
            await AssetRolloutService(self.db).create(
                AssetRolloutCommand(
                    rollout_id=(
                        f"personal-skill-{int(skill.id)}-version-{int(result.version_id)}"
                    ),
                    org_id=principal.org_id,
                    asset_type="skill",
                    asset_id=int(skill.id),
                    action="publish",
                    source_version_id=source_version_id,
                    target_version_id=int(result.version_id),
                    created_by_user_id=principal.user_id,
                )
            )
        return self._record(skill, skill_version)

    async def list_for_user(self, *, principal: Principal) -> list[PersonalSkillAssetRecord]:
        result = await self.db.execute(
            select(Skill, SkillVersion)
            .outerjoin(SkillVersion, SkillVersion.id == Skill.current_version_id)
            .where(
                Skill.org_id == principal.org_id,
                Skill.owner_user_id == principal.user_id,
                Skill.asset_scope == "personal",
                Skill.status.in_(("private", "draft", "rejected", "archived")),
            )
            .order_by(Skill.slug.asc())
        )
        return [self._record(skill, version) for skill, version in result.all()]

    async def get_for_user(self, *, principal: Principal, skill_id: int) -> PersonalSkillAssetRecord | None:
        result = await self.db.execute(
            select(Skill, SkillVersion)
            .outerjoin(SkillVersion, SkillVersion.id == Skill.current_version_id)
            .where(
                Skill.org_id == principal.org_id,
                Skill.owner_user_id == principal.user_id,
                Skill.id == skill_id,
                Skill.asset_scope == "personal",
                Skill.status != "deleted",
            )
        )
        row = result.first()
        if row is None:
            return None
        return self._record(row[0], row[1])

    async def get_version_for_user(
        self,
        *,
        principal: Principal,
        skill_id: int,
        skill_version_id: int | None,
    ) -> PersonalSkillAssetRecord | None:
        statement = (
            select(Skill, SkillVersion)
            .join(SkillVersion, SkillVersion.skill_id == Skill.id)
            .where(
                Skill.org_id == principal.org_id,
                Skill.owner_user_id == principal.user_id,
                Skill.id == skill_id,
                Skill.asset_scope == "personal",
                Skill.status != "deleted",
            )
        )
        if skill_version_id is not None:
            statement = statement.where(SkillVersion.id == skill_version_id)
        else:
            statement = statement.where(SkillVersion.id == Skill.current_version_id)
        row = (await self.db.execute(statement)).first()
        if row is None:
            return None
        return self._record(row[0], row[1])

    async def update_description(
        self,
        *,
        principal: Principal,
        skill_id: int,
        description: str | None,
        name: str | None = None,
    ) -> PersonalSkillAssetRecord:
        result = await self.db.execute(
            select(Skill, SkillVersion)
            .outerjoin(SkillVersion, SkillVersion.id == Skill.current_version_id)
            .where(
                Skill.org_id == principal.org_id,
                Skill.owner_user_id == principal.user_id,
                Skill.id == skill_id,
                Skill.asset_scope == "personal",
                Skill.status != "deleted",
            )
            .with_for_update()
        )
        row = result.first()
        if row is None:
            raise PersonalSkillAssetError("personal skill not found")
        skill, version = row
        if name is not None:
            normalized_name = normalize_skill_name(name)
            other_names = await self.db.scalars(
                select(Skill.name).where(
                    Skill.org_id == principal.org_id,
                    Skill.owner_user_id == principal.user_id,
                    Skill.asset_scope == "personal",
                    Skill.status != "deleted",
                    Skill.id != skill_id,
                )
            )
            public_names = await self.db.scalars(
                select(Skill.name).where(
                    Skill.org_id == principal.org_id,
                    Skill.asset_scope == "public",
                    Skill.status != "deleted",
                    Skill.market_visible.is_(True),
                )
            )
            if normalized_name in {
                normalize_skill_name(str(item))
                for item in [*other_names, *public_names]
            }:
                raise PersonalSkillAssetError("personal_skill_name_conflict")
            skill.name = name.strip()
        try:
            value = normalize_skill_description(
                description,
                default=default_skill_description(version.manifest if version else None),
            )
        except SkillDescriptionError as exc:
            raise PersonalSkillAssetError(str(exc)) from exc
        skill.description = value
        await self.db.flush()
        await self.db.commit()
        return self._record(skill, version)

    async def delete_skill(self, *, principal: Principal, skill_id: int) -> None:
        result = await self.db.execute(
            select(Skill, SkillVersion)
            .outerjoin(SkillVersion, SkillVersion.id == Skill.current_version_id)
            .where(
                Skill.org_id == principal.org_id,
                Skill.owner_user_id == principal.user_id,
                Skill.id == skill_id,
                Skill.asset_scope == "personal",
                Skill.status != "deleted",
            )
        )
        row = result.first()
        if row is None:
            raise PersonalSkillAssetError("personal skill not found")
        skill, _version = row
        skill.slug = _deleted_slug(skill.slug, int(skill.id))
        skill.status = "deleted"
        versions = await self.db.execute(
            select(SkillVersion).where(
                SkillVersion.org_id == principal.org_id,
                SkillVersion.skill_id == skill_id,
            )
        )
        for item in versions.scalars().all():
            item.status = "deleted"
        await self.db.commit()

    @staticmethod
    def _record(skill: Skill, version: SkillVersion | None) -> PersonalSkillAssetRecord:
        return PersonalSkillAssetRecord(
            skill_id=int(skill.id),
            org_id=int(skill.org_id),
            owner_user_id=int(skill.owner_user_id),
            slug=skill.slug,
            name=skill.name,
            description=skill.description,
            category_key=str(skill.category_key or "uncategorized"),
            status=skill.status,
            skill_version_id=int(version.id) if version else None,
            version=int(version.version) if version else None,
            package_uri=version.package_uri if version else None,
            manifest=version.manifest or {} if version else {},
            created_at=skill.created_at,
        )


class PersonalSkillAssetService:
    def __init__(self, *, store: PersonalSkillAssetStore, root: str) -> None:
        self.store = store
        self.root = Path(root)

    async def upload_skill_zip(
        self,
        *,
        principal: Principal,
        content: bytes,
        category_key: str = "uncategorized",
        description: str | None = None,
    ) -> PersonalSkillAssetRecord:
        return (
            await self.upload_skill_zip_with_artifact(
                principal=principal,
                content=content,
                category_key=category_key,
                description=description,
            )
        ).record

    async def upload_skill_zip_with_artifact(
        self,
        *,
        principal: Principal,
        content: bytes,
        category_key: str = "uncategorized",
        description: str | None = None,
    ) -> PersonalSkillUploadResult:
        _validate_personal_skill_archive(content)
        try:
            metadata = parse_skill_metadata(content)
        except SkillPackageError as exc:
            raise PersonalSkillAssetError(_personal_package_error(exc)) from exc
        name = str(metadata["name"])
        slug = _personal_slug(
            runtime_slug_from_normalized_name(normalize_skill_name(name))
        )
        if is_platform_default_skill_name(name) or is_platform_default_skill_name(slug):
            raise PersonalSkillAssetError("SKILL_SYSTEM_RESERVED")
        version_number = 1
        asset_root = (
            self.root
            / "personal-skills"
            / str(principal.org_id)
            / str(principal.user_id)
            / slug
        )
        target = (
            asset_root / "current"
        )
        staging = asset_root / f".staging-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
        backup = asset_root / f".previous-{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
        try:
            file_tree, skill_md = extract_skill_package(content, staging)
            asset_root.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.replace(backup)
            staging.replace(target)
        except SkillPackageError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise PersonalSkillAssetError(_personal_package_error(exc)) from exc
        manifest = {
            "kind": "skill",
            "slug": slug,
            "version": version_number,
            "entrypoint": "SKILL.md",
            "files": file_tree,
            "readme_excerpt": skill_excerpt(skill_md),
            "skill_metadata": metadata,
        }
        try:
            effective_description = normalize_skill_description(
                description,
                default=default_skill_description(manifest),
            )
        except SkillDescriptionError as exc:
            shutil.rmtree(target, ignore_errors=True)
            if backup.exists():
                backup.replace(target)
            raise PersonalSkillAssetError(str(exc)) from exc
        try:
            record = await self.store.create(
                principal=principal,
                slug=slug,
                name=name,
                description=effective_description,
                category_key=category_key,
                version=version_number,
                package_uri=str(target),
                package_sha256=sha256_directory(target),
                manifest=manifest,
            )
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            if backup.exists():
                backup.replace(target)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        record_path = Path(record.package_uri) if record.package_uri else None
        created_package_path = (
            target
            if record_path is not None
            and record_path.resolve() == target.resolve()
            else None
        )
        if created_package_path is None:
            # Content-addressed idempotency may reuse an existing immutable
            # version. The just-extracted directory is then unreferenced.
            shutil.rmtree(target, ignore_errors=True)
        return PersonalSkillUploadResult(
            record=record,
            created_package_path=created_package_path,
        )

    def discard_upload(self, upload: PersonalSkillUploadResult) -> None:
        path = upload.created_package_path
        if path is None:
            return
        managed_root = (self.root / "personal-skills").resolve()
        resolved = path.resolve()
        if resolved == managed_root or managed_root not in resolved.parents:
            raise PersonalSkillAssetError("personal skill package path is unmanaged")
        shutil.rmtree(resolved, ignore_errors=True)

    async def list_skills(self, *, principal: Principal) -> list[PersonalSkillAssetRecord]:
        return await self.store.list_for_user(principal=principal)

    async def get_skill(self, *, principal: Principal, skill_id: int) -> PersonalSkillAssetRecord:
        if skill_id <= 0:
            raise PersonalSkillAssetError("skill_id must be positive")
        record = await self.store.get_for_user(principal=principal, skill_id=skill_id)
        if record is None:
            raise PersonalSkillAssetError("personal skill not found")
        return record

    async def update_description(
        self,
        *,
        principal: Principal,
        skill_id: int,
        description: str | None,
        name: str | None = None,
    ) -> PersonalSkillAssetRecord:
        if skill_id <= 0:
            raise PersonalSkillAssetError("skill_id must be positive")
        return await self.store.update_description(
            principal=principal,
            skill_id=skill_id,
            description=description,
            name=name,
        )

    async def delete_skill(self, *, principal: Principal, skill_id: int) -> None:
        if skill_id <= 0:
            raise PersonalSkillAssetError("skill_id must be positive")
        await self.store.delete_skill(principal=principal, skill_id=skill_id)


def _personal_slug(value: str) -> str:
    try:
        return validate_skill_slug(value)
    except SkillPackageError as exc:
        raise PersonalSkillAssetError(str(exc)) from exc


def _validate_personal_skill_archive(content: bytes) -> None:
    if not content:
        raise PersonalSkillAssetError("personal_skill_package_empty")
    if len(content) > settings.SKILLHUB_MAX_PACKAGE_BYTES:
        raise PersonalSkillAssetError("personal_skill_package_too_large")
    try:
        with ZipFile(__import__("io").BytesIO(content)) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            if not entries:
                raise PersonalSkillAssetError("personal_skill_package_empty")
            if len(entries) > settings.SKILLHUB_MAX_FILE_COUNT:
                raise PersonalSkillAssetError("personal_skill_package_too_many_files")
            unpacked = 0
            for item in entries:
                if item.flag_bits & 0x1:
                    raise PersonalSkillAssetError("personal_skill_package_encrypted")
                if item.external_attr >> 16 & 0o170000 == 0o120000:
                    raise PersonalSkillAssetError("personal_skill_package_symlink")
                if item.file_size > settings.SKILLHUB_MAX_SINGLE_FILE_BYTES:
                    raise PersonalSkillAssetError(
                        "personal_skill_package_single_file_too_large"
                    )
                unpacked += item.file_size
                if unpacked > settings.SKILLHUB_MAX_UNPACKED_BYTES:
                    raise PersonalSkillAssetError(
                        "personal_skill_package_unpacked_too_large"
                    )
    except PersonalSkillAssetError:
        raise
    except BadZipFile as exc:
        raise PersonalSkillAssetError("personal_skill_package_invalid") from exc


def _personal_package_error(exc: SkillPackageError) -> str:
    message = str(exc)
    if "must contain SKILL.md" in message:
        return "personal_skill_package_missing_manifest"
    if "unsafe path" in message or "invalid Skill package paths" in message:
        return "personal_skill_package_unsafe_path"
    if "valid zip" in message:
        return "personal_skill_package_invalid"
    if "utf-8" in message:
        return "personal_skill_manifest_invalid_encoding"
    if "name is required" in message or "slug must" in message:
        return "personal_skill_manifest_invalid"
    return "personal_skill_package_invalid"


def _personal_version(value: int | None) -> int:
    version = value or 1
    if version <= 0:
        raise PersonalSkillAssetError("version must be positive")
    return version


def _deleted_slug(slug: str, skill_id: int) -> str:
    suffix = f"__deleted_{skill_id}"
    return f"{slug[:128 - len(suffix)]}{suffix}"
