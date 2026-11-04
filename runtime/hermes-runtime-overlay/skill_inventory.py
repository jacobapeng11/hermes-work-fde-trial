#!/usr/bin/env python3
"""Verify and stage the platform's exact default Hermes Skill assets."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from typing import Any, Iterable


INVENTORY_VERSION = 1
SKILL_ROOTS = (
    "platform-default-skills",
    "platform-readonly-skills",
)
DEFAULT_SKILL_ROOT = SKILL_ROOTS[0]
DEFAULT_SKILL_NAMES = (
    "hermes-browser-zh",
    "live-data-report-builder",
    "minimax-docx",
    "minimax-pdf",
    "minimax-xlsx",
    "ppt-master",
)
FORBIDDEN_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo"}


class SkillInventoryError(RuntimeError):
    pass


def _canonical_relative(value: object, *, label: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise SkillInventoryError(f"{label} 必须是字符串")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise SkillInventoryError(f"{label} 不是规范相对路径: {value!r}")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_inventory(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillInventoryError(f"无法读取 Skill inventory: {path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != INVENTORY_VERSION
        or not isinstance(payload.get("skills"), list)
    ):
        raise SkillInventoryError("Skill inventory 格式或版本无效")
    return payload


def _frontmatter_name(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n(?P<header>.*?)\n---(?:\s*\n|\Z)", content, re.S)
    if match is None:
        raise SkillInventoryError(f"Skill 缺少 YAML frontmatter: {path}")
    for line in match.group("header").splitlines():
        name = re.fullmatch(r"name:\s*([^#\s][^#]*?)\s*", line)
        if name is not None:
            return name.group(1).strip().strip("\"'")
    raise SkillInventoryError(f"Skill frontmatter 缺少 name: {path}")


def _expected_names(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(values) if values is not None else DEFAULT_SKILL_NAMES


def _item_root(item: dict[str, Any], *, label: str) -> str:
    value = item.get("root", DEFAULT_SKILL_ROOT)
    if value not in SKILL_ROOTS:
        raise SkillInventoryError(f"{label}.root 必须是 {SKILL_ROOTS} 之一: {value!r}")
    return value


def verify_inventory(
    *,
    repository_root: Path,
    inventory_path: Path,
    expected_names: Iterable[str] | None = None,
) -> dict[str, Any]:
    root = repository_root.resolve(strict=True)
    payload = _load_inventory(inventory_path)
    expected = _expected_names(expected_names)
    skills = payload["skills"]
    names: list[str] = []
    targets: list[str] = []
    source_files: list[str] = []
    target_files: list[str] = []

    for index, raw in enumerate(skills):
        if not isinstance(raw, dict):
            raise SkillInventoryError(f"skills[{index}] 必须是对象")
        name = raw.get("name")
        capability = raw.get("capability")
        license_reference = raw.get("license")
        if not isinstance(name, str) or not name:
            raise SkillInventoryError(f"skills[{index}].name 无效")
        if not isinstance(capability, str) or not capability:
            raise SkillInventoryError(f"{name} 缺少 capability")
        if not isinstance(license_reference, str) or not license_reference:
            raise SkillInventoryError(f"{name} 缺少 license")
        _item_root(raw, label=name)
        source = _canonical_relative(raw.get("source"), label=f"{name}.source")
        target = _canonical_relative(raw.get("target"), label=f"{name}.target")
        if target.name != name:
            raise SkillInventoryError(f"{name} 的目标目录名称不一致")
        files = raw.get("files")
        if not isinstance(files, dict) or "SKILL.md" not in files:
            raise SkillInventoryError(f"{name} 必须声明 SKILL.md")

        source_root = root.joinpath(*source.parts)
        if not source_root.is_dir():
            raise SkillInventoryError(f"{name} 来源目录不存在: {source}")
        for raw_relative, expected_digest in sorted(files.items()):
            relative = _canonical_relative(
                raw_relative,
                label=f"{name}.files",
            )
            if FORBIDDEN_PARTS.intersection(relative.parts) or relative.suffix in (
                FORBIDDEN_SUFFIXES
            ):
                raise SkillInventoryError(f"{name} 声明了禁止的缓存文件: {relative}")
            if (
                not isinstance(expected_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
            ):
                raise SkillInventoryError(f"{name} 文件摘要无效: {relative}")
            source_path = source_root.joinpath(*relative.parts)
            if source_path.is_symlink() or not source_path.is_file():
                raise SkillInventoryError(
                    f"{name} 声明文件不存在或不是普通文件: {relative}"
                )
            actual_digest = sha256_file(source_path)
            if actual_digest != expected_digest:
                raise SkillInventoryError(
                    f"{name} 文件摘要不一致: {relative}; "
                    f"expected={expected_digest}; actual={actual_digest}"
                )
            source_files.append((source / relative).as_posix())
            target_files.append((target / relative).as_posix())

        skill_file = source_root / "SKILL.md"
        if _frontmatter_name(skill_file) != name:
            raise SkillInventoryError(f"{name} 与 SKILL.md frontmatter name 不一致")
        names.append(name)
        targets.append(target.as_posix())

    if sorted(names) != sorted(expected) or len(names) != len(expected):
        raise SkillInventoryError(
            "平台默认 Skill 集合不精确: "
            f"expected={sorted(expected)}; actual={sorted(names)}"
        )
    if len(names) != len(set(names)):
        raise SkillInventoryError("平台默认 Skill 名称重复")
    if len(targets) != len(set(targets)):
        raise SkillInventoryError("平台默认 Skill 目标目录重复")

    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "status": "passed",
        "inventory_sha256": hashlib.sha256(canonical).hexdigest(),
        "skill_count": len(names),
        "skill_names": sorted(names),
        "files": sorted(source_files),
        "target_files": sorted(target_files),
    }


def stage_inventory(
    *,
    repository_root: Path,
    inventory_path: Path,
    destination: Path,
    root: str = DEFAULT_SKILL_ROOT,
    expected_names: Iterable[str] | None = None,
) -> dict[str, Any]:
    if root not in SKILL_ROOTS:
        raise SkillInventoryError(f"未知 Skill root: {root!r}")
    report = verify_inventory(
        repository_root=repository_root,
        inventory_path=inventory_path,
        expected_names=expected_names,
    )
    if destination.exists() and any(destination.iterdir()):
        raise SkillInventoryError(f"staging 目标目录必须为空: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    payload = _load_inventory(inventory_path)
    repository = repository_root.resolve(strict=True)
    staged_names: list[str] = []
    staged_target_files: list[str] = []
    for item in payload["skills"]:
        if _item_root(item, label=item["name"]) != root:
            continue
        name = item["name"]
        source = _canonical_relative(item["source"], label=f"{name}.source")
        target = _canonical_relative(item["target"], label=f"{name}.target")
        for raw_relative in sorted(item["files"]):
            relative = _canonical_relative(raw_relative, label="files")
            source_path = repository.joinpath(*source.parts, *relative.parts)
            target_path = destination.joinpath(*target.parts, *relative.parts)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            staged_target_files.append((target / relative).as_posix())
        staged_names.append(name)
    if not staged_names:
        raise SkillInventoryError(f"inventory 中没有属于 {root} 的 Skill")
    return {
        **report,
        "root": root,
        "skill_count": len(staged_names),
        "skill_names": sorted(staged_names),
        "target_files": sorted(staged_target_files),
    }


def verify_staged_inventory(
    *,
    inventory_path: Path,
    destination: Path,
    root: str = DEFAULT_SKILL_ROOT,
    expected_names: Iterable[str] | None = None,
) -> dict[str, Any]:
    if root not in SKILL_ROOTS:
        raise SkillInventoryError(f"未知 Skill root: {root!r}")
    payload = _load_inventory(inventory_path)
    root_items = [
        item for item in payload["skills"] if _item_root(item, label=item["name"]) == root
    ]
    root_names = {item["name"] for item in root_items}
    expected = tuple(
        name for name in _expected_names(expected_names) if name in root_names
    )
    staged_root = destination.resolve(strict=True)
    names: list[str] = []
    expected_files: dict[str, str] = {}
    for index, raw in enumerate(root_items):
        if not isinstance(raw, dict):
            raise SkillInventoryError(f"{root} skills[{index}] 必须是对象")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise SkillInventoryError(f"skills[{index}].name 无效")
        target = _canonical_relative(raw.get("target"), label=f"{name}.target")
        files = raw.get("files")
        if not isinstance(files, dict) or "SKILL.md" not in files:
            raise SkillInventoryError(f"{name} 必须声明 SKILL.md")
        for raw_relative, digest in files.items():
            relative = _canonical_relative(raw_relative, label=f"{name}.files")
            if not isinstance(digest, str):
                raise SkillInventoryError(f"{name} 文件摘要无效: {relative}")
            expected_files[(target / relative).as_posix()] = digest
        names.append(name)

    actual_files = {
        path.relative_to(staged_root).as_posix()
        for path in staged_root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    modified = sorted(
        relative
        for relative, digest in expected_files.items()
        if relative in actual_files
        and (
            (staged_root / relative).is_symlink()
            or sha256_file(staged_root / relative) != digest
        )
    )
    missing = sorted(set(expected_files) - actual_files)
    unexpected = sorted(actual_files - set(expected_files))
    if (
        sorted(names) != sorted(expected)
        or len(names) != len(expected)
        or modified
        or missing
        or unexpected
    ):
        raise SkillInventoryError(
            "最终 Skill 资产集合不一致: "
            f"modified={modified}; missing={missing}; unexpected={unexpected}"
        )
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "status": "passed",
        "root": root,
        "inventory_sha256": hashlib.sha256(canonical).hexdigest(),
        "skill_count": len(names),
        "skill_names": sorted(names),
        "target_files": sorted(expected_files),
    }


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("verify", "stage", "verify-staged"))
    parser.add_argument("--repository-root")
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--destination")
    parser.add_argument(
        "--root",
        choices=SKILL_ROOTS,
        default=DEFAULT_SKILL_ROOT,
        help="stage/verify-staged 只处理该目标根目录下的 Skill",
    )
    args = parser.parse_args()
    if args.action == "verify-staged":
        if not args.destination:
            parser.error("--destination is required for verify-staged")
        report = verify_staged_inventory(
            inventory_path=Path(args.inventory),
            destination=Path(args.destination),
            root=args.root,
        )
    elif args.action == "stage":
        if not args.repository_root:
            parser.error("--repository-root is required for stage")
        if not args.destination:
            parser.error("--destination is required for stage")
        report = stage_inventory(
            repository_root=Path(args.repository_root),
            inventory_path=Path(args.inventory),
            destination=Path(args.destination),
            root=args.root,
        )
    else:
        if not args.repository_root:
            parser.error("--repository-root is required for verify")
        report = verify_inventory(
            repository_root=Path(args.repository_root),
            inventory_path=Path(args.inventory),
        )
    json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
