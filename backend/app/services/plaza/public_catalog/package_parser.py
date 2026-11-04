from __future__ import annotations

import hashlib
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile


class SkillPackageError(ValueError):
    pass


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_skill_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", clean_text(value)).casefold()
    normalized = re.sub(r"[_-]+", " ", normalized)
    normalized = clean_text(normalized)
    if not normalized:
        raise SkillPackageError("SKILL.md.name is required")
    return normalized


def runtime_slug_from_normalized_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    if not slug:
        slug = f"skill-{digest}"
    if len(slug) > 120:
        slug = f"{slug[:103].rstrip('-')}-{digest}"
    if len(slug) < 3:
        slug = f"{slug}-skill"
    return validate_skill_slug(slug)


def validate_skill_slug(value: str) -> str:
    slug = value.strip().lower()
    if not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{1,126}[a-z0-9]",
        slug,
    ):
        raise SkillPackageError(
            "slug must be 3-128 lowercase letters, numbers, underscores, or dashes"
        )
    return slug


def skill_excerpt(content: str) -> str:
    return content.strip()[:600]


def validate_category_key(value: str) -> str:
    category = value.strip()
    if category in {"personal", "uncategorized"} or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]", category):
        raise SkillPackageError("invalid public Skill category")
    return category


def parse_skill_metadata(content: bytes) -> dict[str, Any]:
    try:
        with ZipFile(__import__("io").BytesIO(content)) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            names = [_safe_name(item.filename) for item in entries]
            names = _normalize_paths(names)
            if "SKILL.md" not in names:
                raise SkillPackageError("skill package must contain SKILL.md")
            index = names.index("SKILL.md")
            text = archive.read(entries[index]).decode("utf-8")
    except BadZipFile as exc:
        raise SkillPackageError("skill package must be a valid zip") from exc
    except UnicodeDecodeError as exc:
        raise SkillPackageError("SKILL.md must be utf-8") from exc
    metadata: dict[str, Any] = {}
    lines = text.splitlines()
    block = lines[1:] if lines and lines[0].strip() == "---" else lines[:40]
    for line in block:
        if line.strip() == "---":
            break
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip() in {"name", "description", "version"}:
                metadata[key.strip()] = value.strip().strip("\"'")
    name = metadata.get("name")
    if not isinstance(name, str) or not clean_text(name):
        raise SkillPackageError("SKILL.md.name is required")
    metadata["name"] = clean_text(name)
    if isinstance(metadata.get("description"), str):
        metadata["description"] = clean_text(metadata["description"])[:1000]
    return metadata


def extract_skill_package(content: bytes, target: Path) -> tuple[list[str], str]:
    try:
        with ZipFile(__import__("io").BytesIO(content)) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            names = _normalize_paths([_safe_name(item.filename) for item in entries])
            if len(names) != len(set(names)) or "SKILL.md" not in names:
                raise SkillPackageError("invalid Skill package paths")
            staging = target.with_name(f".{target.name}.staging")
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            for item, name in zip(entries, names, strict=True):
                destination = staging / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(item))
            shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(target)
            return sorted(names), (target / "SKILL.md").read_text(encoding="utf-8")
    except BadZipFile as exc:
        raise SkillPackageError("skill package must be a valid zip") from exc


def sha256_directory(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(file_path.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_name(raw: str) -> str:
    name = raw.replace("\\", "/")
    path = Path(name)
    if not name or name.startswith("/") or path.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
        raise SkillPackageError("skill package contains an unsafe path")
    return name


def _normalize_paths(names: list[str]) -> list[str]:
    if "SKILL.md" in names:
        return names
    roots = {name.split("/", 1)[0] for name in names}
    if len(roots) != 1 or not all("/" in name for name in names):
        return names
    root = next(iter(roots))
    return [name[len(root) + 1:] for name in names]
