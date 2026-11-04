from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

from app.config import Settings, settings
from app.services.plaza.public_catalog.package_parser import (
    SkillPackageError,
    extract_skill_package,
    parse_skill_metadata,
)


@dataclass(frozen=True)
class StoredSkillHubPackage:
    package_uri: str
    deployment_uri: str
    metadata: dict


class UserSkillHubPackageStorage:
    """Store immutable upstream packages in the platform asset repository."""

    def __init__(self, *, config: Settings = settings) -> None:
        self.config = config

    def store(
        self,
        *,
        asset_root: Path,
        namespace: str,
        public_slug: str,
        version: str,
        content: bytes,
    ) -> StoredSkillHubPackage:
        self._validate_archive(content)
        coordinate_hash = hashlib.sha256(f"{namespace}:{public_slug}".encode()).hexdigest()[:32]
        version_hash = hashlib.sha256(version.encode()).hexdigest()[:16]
        base = asset_root / coordinate_hash / version_hash
        staging = base.with_name(f".{base.name}.staging")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        original = staging / "original.zip"
        deployment = staging / "deployment"
        original.write_bytes(content)
        try:
            metadata = parse_skill_metadata(content)
            extract_skill_package(content, deployment)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        (staging / "manifest.json").write_text(
            json.dumps(
                {"namespace": namespace, "public_slug": public_slug, "version": version},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        if base.exists():
            shutil.rmtree(base, ignore_errors=True)
        base.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(base)
        return StoredSkillHubPackage(str(base / "original.zip"), str(base / "deployment"), metadata)

    def discard(self, package_uri: str) -> None:
        shutil.rmtree(Path(package_uri).parent, ignore_errors=True)

    def _validate_archive(self, content: bytes) -> None:
        if not content or len(content) > self.config.SKILLHUB_MAX_PACKAGE_BYTES:
            raise SkillPackageError("SkillHub package exceeds platform size limit")
        try:
            with ZipFile(__import__("io").BytesIO(content)) as archive:
                entries = [item for item in archive.infolist() if not item.is_dir()]
                if len(entries) > self.config.SKILLHUB_MAX_FILE_COUNT:
                    raise SkillPackageError("SkillHub package exceeds platform file-count limit")
                unpacked = 0
                for item in entries:
                    if item.file_size > self.config.SKILLHUB_MAX_SINGLE_FILE_BYTES:
                        raise SkillPackageError(
                            "SkillHub package exceeds platform single-file limit"
                        )
                    if item.external_attr >> 16 & 0o170000 == 0o120000:
                        raise SkillPackageError("SkillHub package contains symbolic link")
                    unpacked += item.file_size
                    if unpacked > self.config.SKILLHUB_MAX_UNPACKED_BYTES:
                        raise SkillPackageError(
                            "SkillHub package exceeds platform unpacked-size limit"
                        )
        except SkillPackageError:
            raise
        except Exception as exc:
            raise SkillPackageError("SkillHub package must be a valid zip") from exc
