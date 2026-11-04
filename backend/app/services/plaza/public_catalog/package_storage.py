from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.services.plaza.public_catalog.package_parser import (
    SkillPackageError,
    extract_skill_package,
    parse_skill_metadata,
    sha256_directory,
)


@dataclass(frozen=True)
class StoredSkillPackage:
    original_package_uri: str
    deployment_package_uri: str
    original_sha256: str
    deployment_sha256: str
    manifest: dict
    skill_metadata: dict
    previous_package_uri: str | None = None


class PublicSkillPackageStorage:
    """Store one current public package per stable asset outside MySQL.

    SkillVersion rows remain the audit chain. The filesystem is a deployment
    cache and therefore has no version directory.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def store(
        self,
        *,
        org_id: int,
        normalized_name: str,
        version_number: int,
        content: bytes,
        category_key: str,
    ) -> StoredSkillPackage:
        if not content:
            raise SkillPackageError("skill package is empty")
        skill_metadata = parse_skill_metadata(content)
        del version_number
        base = self.root / "public-skills" / str(org_id) / normalized_name / "current"
        staging = base.with_name(f".{base.name}.{hashlib.sha256(content).hexdigest()[:12]}.staging")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        original = staging / "original.zip"
        deployment = staging / "deployment"
        original.write_bytes(content)
        try:
            file_tree, skill_md = extract_skill_package(content, deployment)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        manifest = {
            "kind": "skill",
            "entrypoint": "SKILL.md",
            "files": file_tree,
            "category_key": category_key,
            "readme_excerpt": skill_md.strip()[:600],
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        base.parent.mkdir(parents=True, exist_ok=True)
        previous = None
        if base.exists():
            previous = base.with_name(f".previous-{uuid.uuid4().hex}")
            base.replace(previous)
        staging.replace(base)
        return StoredSkillPackage(
            original_package_uri=str(base / "original.zip"),
            deployment_package_uri=str(base / "deployment"),
            original_sha256=hashlib.sha256(content).hexdigest(),
            deployment_sha256=sha256_directory(base / "deployment"),
            manifest=manifest,
            skill_metadata=skill_metadata,
            previous_package_uri=str(previous) if previous else None,
        )

    def delete_asset(self, *, org_id: int, normalized_name: str) -> None:
        shutil.rmtree(self.root / "public-skills" / str(org_id) / normalized_name, ignore_errors=True)

    def discard_package(self, package: StoredSkillPackage) -> None:
        current = Path(package.original_package_uri).parent
        shutil.rmtree(current, ignore_errors=True)
        if package.previous_package_uri:
            previous = Path(package.previous_package_uri)
            if previous.exists():
                previous.replace(current)

    def finalize_package(self, package: StoredSkillPackage) -> None:
        if package.previous_package_uri:
            shutil.rmtree(Path(package.previous_package_uri), ignore_errors=True)
