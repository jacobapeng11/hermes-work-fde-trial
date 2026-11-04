#!/usr/bin/env python3
"""Exercise Skill policy v3 against the fixed official and platform roots."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

import yaml


OFFICIAL_SOURCE_COUNT = 81
PLATFORM_PACKAGED_SKILL_NAMES = {
    "hermes-browser-zh",
    "live-data-report-builder",
    "minimax-docx",
    "minimax-pdf",
    "minimax-xlsx",
    "ppt-master",
}
BALANCED_DEFAULT_SKILL_NAMES = {
    "hermes-agent", "hermes-agent-skill-authoring", "hermes-browser-zh",
    "live-data-report-builder",
    "minimax-docx", "minimax-pdf", "minimax-xlsx", "ppt-master",
    "plan", "humanizer", "codebase-inspection", "systematic-debugging",
    "simplify-code", "test-driven-development", "architecture-diagram", "design-md",
}
RETIRED_PLATFORM_SKILL_NAME = "hermes-desktop-plugins"
USER_DISABLED_SKILL_NAME = "user-disabled"


class SkillVisibilityError(RuntimeError):
    pass


def _write_fixture_skill(root: Path, name: str) -> None:
    target = root / name
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f'description: "visibility fixture for {name}"\n'
        "platforms: [linux]\n"
        "---\n\n"
        f"# {name}\n",
        encoding="utf-8",
    )


def _frontmatter(skill_md: Path) -> dict[str, Any]:
    try:
        lines = skill_md.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SkillVisibilityError(f"cannot read Skill: {skill_md}") from exc
    if not lines or lines[0].strip() != "---":
        raise SkillVisibilityError(f"Skill has no frontmatter: {skill_md}")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise SkillVisibilityError(f"Skill frontmatter is unterminated: {skill_md}") from exc
    try:
        payload = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise SkillVisibilityError(f"Skill frontmatter is invalid: {skill_md}") from exc
    if not isinstance(payload, dict):
        raise SkillVisibilityError(f"Skill frontmatter is not a mapping: {skill_md}")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillVisibilityError(f"Skill has no frontmatter name: {skill_md}")
    payload["name"] = name.strip()
    return payload


def _inventory(root: Path, *, expected_count: int | None = None) -> dict[str, Any]:
    if not root.is_dir() or root.is_symlink():
        raise SkillVisibilityError(f"Skill root is missing or a symlink: {root}")
    resolved_root = root.resolve(strict=True)
    for item in root.rglob("*"):
        if item.is_symlink():
            raise SkillVisibilityError(f"Skill inventory contains a symlink: {item}")
    records: list[dict[str, str]] = []
    names: set[str] = set()
    for skill_md in sorted(root.rglob("SKILL.md")):
        try:
            skill_md.resolve(strict=True).relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise SkillVisibilityError(f"Skill escaped its source root: {skill_md}") from exc
        if not skill_md.is_file():
            raise SkillVisibilityError(f"Skill manifest is not a regular file: {skill_md}")
        frontmatter = _frontmatter(skill_md)
        name = str(frontmatter["name"])
        if name in names:
            raise SkillVisibilityError(f"duplicate Skill frontmatter name: {name}")
        names.add(name)
        records.append(
            {
                "name": name,
                "path": skill_md.relative_to(root).as_posix(),
                "source_digest": "sha256:" + hashlib.sha256(skill_md.read_bytes()).hexdigest(),
            }
        )
    if expected_count is not None and len(records) != expected_count:
        raise SkillVisibilityError(
            f"official Skill count mismatch: expected={expected_count}; actual={len(records)}"
        )
    return {
        "names": names,
        "records": records,
        "digest": "sha256:"
        + hashlib.sha256(
            json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _compatible_names(root: Path) -> set[str]:
    from tools.skills_tool import skill_matches_environment, skill_matches_platform

    return {
        str(frontmatter["name"])
        for skill_md in root.rglob("SKILL.md")
        for frontmatter in [_frontmatter(skill_md)]
        if skill_matches_platform(frontmatter)
        and skill_matches_environment(frontmatter)
    }


def _visible_names(skills_list: Any) -> set[str]:
    listing = json.loads(skills_list())
    if not isinstance(listing, dict) or listing.get("success") is not True:
        raise SkillVisibilityError(f"Hermes skills_list failed: {listing!r}")
    skills = listing.get("skills")
    if not isinstance(skills, list):
        raise SkillVisibilityError("Hermes skills_list returned no list")
    return {
        str(item.get("name"))
        for item in skills
        if isinstance(item, dict) and item.get("name")
    }


def verify(
    *,
    curated_root: Path,
    official_root: Path,
    readonly_root: Path,
) -> dict[str, Any]:
    packaged = _inventory(curated_root)
    readonly = _inventory(readonly_root)
    packaged_names = set(packaged["names"]) | set(readonly["names"])
    if packaged_names != PLATFORM_PACKAGED_SKILL_NAMES:
        raise SkillVisibilityError(
            "platform packaged Skill set mismatch: "
            f"expected={sorted(PLATFORM_PACKAGED_SKILL_NAMES)}; "
            f"actual={sorted(packaged_names)}"
        )
    official = _inventory(official_root, expected_count=OFFICIAL_SOURCE_COUNT)
    if official["names"] & packaged_names:
        raise SkillVisibilityError("official and platform Skill names overlap")

    environment_keys = (
        "HERMES_HOME",
        "HERMES_PROFILE",
        "HERMES_PLATFORM",
        "HERMES_SESSION_PLATFORM",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_BOARD",
    )
    previous_environment = {key: os.environ.get(key) for key in environment_keys}
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-skill-visibility-") as temporary:
            hermes_home = Path(temporary)
            skills_root = hermes_home / "skills"
            skills_root.mkdir()
            for skill_md in curated_root.rglob("SKILL.md"):
                relative = skill_md.parent.relative_to(curated_root)
                shutil.copytree(skill_md.parent, skills_root / relative)
            for skill_md in readonly_root.rglob("SKILL.md"):
                relative = skill_md.parent.relative_to(readonly_root)
                shutil.copytree(skill_md.parent, skills_root / relative)
            _write_fixture_skill(skills_root, USER_DISABLED_SKILL_NAME)
            _write_fixture_skill(skills_root, RETIRED_PLATFORM_SKILL_NAME)
            _write_fixture_skill(skills_root, "user-visible")
            effective_disabled = sorted(
                (set(official["names"]) | {RETIRED_PLATFORM_SKILL_NAME})
                - BALANCED_DEFAULT_SKILL_NAMES
                | {USER_DISABLED_SKILL_NAME}
            )
            (hermes_home / "config.yaml").write_text(
                "skills:\n"
                "  external_dirs:\n"
                f"    - {json.dumps(str(official_root))}\n"
                "  disabled:\n"
                + "".join(f"    - {json.dumps(name)}\n" for name in effective_disabled)
                + "platform:\n"
                + "  skill_visibility:\n"
                + "    policy_version: 3\n"
                + "    user_disabled:\n"
                + f"      - {json.dumps(USER_DISABLED_SKILL_NAME)}\n"
                + "    platform_disabled:\n"
                + f"      - {json.dumps(RETIRED_PLATFORM_SKILL_NAME)}\n",
                encoding="utf-8",
            )

            os.environ["HERMES_HOME"] = str(hermes_home)
            os.environ.pop("HERMES_PROFILE", None)
            os.environ.pop("HERMES_PLATFORM", None)
            os.environ.pop("HERMES_SESSION_PLATFORM", None)
            os.environ.pop("HERMES_KANBAN_TASK", None)
            os.environ.pop("HERMES_KANBAN_BOARD", None)

            from tools import skills_tool

            expected = (
                (
                    _compatible_names(official_root)
                    | _compatible_names(curated_root)
                    | _compatible_names(readonly_root)
                )
                & BALANCED_DEFAULT_SKILL_NAMES
            ) | {"user-visible"}
            ordinary_visible = _visible_names(skills_tool.skills_list)
            if ordinary_visible != expected:
                raise SkillVisibilityError(
                    "Hermes Linux visible Skill set mismatch: "
                    f"expected={sorted(expected)}; actual={sorted(ordinary_visible)}"
                )
            skills_tool._SKILLS_CACHE.clear()
            os.environ["HERMES_PLATFORM"] = "kanban"
            os.environ["HERMES_SESSION_PLATFORM"] = "kanban"
            os.environ["HERMES_KANBAN_TASK"] = "candidate-skill-policy-probe"
            os.environ["HERMES_KANBAN_BOARD"] = "candidate-skill-policy-probe"
            kanban_expected = expected
            kanban_visible = _visible_names(skills_tool.skills_list)
            if kanban_visible != kanban_expected:
                raise SkillVisibilityError(
                    "Hermes Kanban visible Skill set mismatch: "
                    f"expected={sorted(kanban_expected)}; actual={sorted(kanban_visible)}"
                )

            disabled_user = json.loads(
                skills_tool.skill_view(USER_DISABLED_SKILL_NAME, preprocess=False)
            )
            retired = json.loads(
                skills_tool.skill_view(RETIRED_PLATFORM_SKILL_NAME, preprocess=False)
            )
            user = json.loads(skills_tool.skill_view("user-visible", preprocess=False))
            official_default = json.loads(
                skills_tool.skill_view("hermes-agent", preprocess=False)
            )
            if disabled_user.get("success") is not False or retired.get("success") is not False:
                raise SkillVisibilityError("disabled user or retired platform Skill is viewable")
            if user.get("success") is not True or official_default.get("success") is not True:
                raise SkillVisibilityError("default-allowed Skill is not viewable")
    finally:
        for key, value in previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    report: dict[str, Any] = {
        "status": "passed",
        "policy_version": 3,
        "official_source_count": OFFICIAL_SOURCE_COUNT,
        "official_inventory_digest": official["digest"],
        "packaged_skill_count": len(PLATFORM_PACKAGED_SKILL_NAMES),
        "linux_effective_visible_count": len(ordinary_visible),
        "kanban_effective_visible_count": len(kanban_visible),
        "user_disabled_hidden": True,
        "retired_platform_hidden": True,
        "user_installed_visible": True,
    }
    report["report_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return report


def _main() -> int:
    report = verify(
        curated_root=Path("/opt/hermes/platform-default-skills"),
        readonly_root=Path("/opt/hermes/platform-readonly-skills"),
        official_root=Path("/opt/hermes/skills"),
    )
    json.dump(report, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except SkillVisibilityError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
