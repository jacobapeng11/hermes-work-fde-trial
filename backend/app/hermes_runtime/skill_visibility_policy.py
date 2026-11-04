from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from collections.abc import Mapping
from typing import Any, Iterable


PLATFORM_DEFAULT_SKILL_NAMES = frozenset(
    {
        "hermes-agent",
        "hermes-agent-skill-authoring",
        "hermes-browser-zh",
        "live-data-report-builder",
        "minimax-docx",
        "minimax-pdf",
        "minimax-xlsx",
        "ppt-master",
    }
)


def normalize_skill_identity(value: str) -> str:
    """Normalize a user-facing Skill name to its Runtime identity form."""
    return re.sub(r"[\s_]+", "-", value.strip().casefold())


def is_platform_default_skill_name(value: str) -> bool:
    return normalize_skill_identity(value) in PLATFORM_DEFAULT_SKILL_NAMES


BALANCED_DEFAULT_SKILL_NAMES = frozenset(
    {
        *PLATFORM_DEFAULT_SKILL_NAMES,
        "plan",
        "humanizer",
        "codebase-inspection",
        "systematic-debugging",
        "simplify-code",
        "test-driven-development",
        "architecture-diagram",
        "design-md",
    }
)
SKILL_VISIBILITY_POLICY_V1 = 1
SKILL_VISIBILITY_POLICY_V2 = 2
SKILL_VISIBILITY_POLICY_V3 = 3
INITIAL_DEFAULT_SKILL_POLICY_VERSION = SKILL_VISIBILITY_POLICY_V3
INITIAL_SKILL_POLICY_CONFIG_KEY = "_initial_skill_policy_version"
SKILL_POLICY_CONFIG_KEY = "_skill_policy_version"
RETIRED_PLATFORM_SKILL_NAMES = frozenset({"hermes-desktop-plugins", "pptx-generator"})
OFFICIAL_HERMES_SKILL_ROOT = "/opt/hermes/skills"
BUNDLED_HERMES_SKILL_ROOT = "/opt/hermes/platform-default-skills"
READONLY_HERMES_SKILL_ROOT = "/opt/hermes/platform-readonly-skills"
DEFAULT_LEGACY_MANIFEST = Path(__file__).with_name("legacy-upstream-skills.json")
DEFAULT_OFFICIAL_MANIFEST = Path(__file__).with_name("official-upstream-skills.json")


class SkillVisibilityPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class SkillVisibility:
    platform_disabled: frozenset[str]
    effective_disabled: frozenset[str]


def _names(values: Iterable[object], *, label: str) -> frozenset[str]:
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise SkillVisibilityPolicyError(f"{label} contains an invalid Skill name")
        normalized.add(value.strip())
    return frozenset(normalized)


def load_legacy_upstream_skill_names(
    path: Path = DEFAULT_LEGACY_MANIFEST,
) -> frozenset[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillVisibilityPolicyError(
            f"cannot read legacy upstream Skill manifest: {path}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("upstream_repository") != "NousResearch/hermes-agent"
        or re.fullmatch(r"[0-9a-f]{40}", str(payload.get("upstream_commit") or ""))
        is None
        or payload.get("source_root") != "skills"
        or not isinstance(payload.get("skill_names"), list)
    ):
        raise SkillVisibilityPolicyError("invalid legacy upstream Skill manifest")
    names = _names(payload["skill_names"], label="legacy_upstream_names")
    if len(names) != len(payload["skill_names"]):
        raise SkillVisibilityPolicyError("legacy upstream Skill names are duplicated")
    return names


def load_official_upstream_skill_names(
    path: Path = DEFAULT_OFFICIAL_MANIFEST,
) -> frozenset[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillVisibilityPolicyError(
            f"cannot read official upstream Skill manifest: {path}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("upstream_repository") != "NousResearch/hermes-agent"
        or re.fullmatch(r"[0-9a-f]{40}", str(payload.get("upstream_commit") or ""))
        is None
        or payload.get("source_root") != "skills"
        or not isinstance(payload.get("skill_names"), list)
        or payload.get("official_source_count") != len(payload.get("skill_names"))
    ):
        raise SkillVisibilityPolicyError("invalid official upstream Skill manifest")
    names = _names(payload["skill_names"], label="official_upstream_names")
    if len(names) != len(payload["skill_names"]):
        raise SkillVisibilityPolicyError("official upstream Skill names are duplicated")
    if len(names) != int(payload["official_source_count"]):
        raise SkillVisibilityPolicyError("official upstream Skill source count mismatch")
    return names


def explicitly_installed_skill_names(
    records: Iterable[dict[str, Any]],
) -> frozenset[str]:
    values: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise SkillVisibilityPolicyError("installed Skill record must be an object")
        manifest = record.get("manifest")
        candidates = [
            record.get("runtime_skill_name"),
            record.get("slug"),
            record.get("name"),
            manifest.get("name") if isinstance(manifest, dict) else None,
        ]
        values.update(
            value.strip() for value in candidates if isinstance(value, str) and value.strip()
        )
    return frozenset(values)


def projected_skill_names(files: Mapping[str, bytes]) -> frozenset[str]:
    names: set[str] = set()
    for relative, content in files.items():
        if not relative.endswith("/SKILL.md"):
            continue
        if not isinstance(content, bytes):
            raise SkillVisibilityPolicyError(f"projected Skill file must be bytes: {relative}")
        try:
            document = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillVisibilityPolicyError(f"projected Skill is not UTF-8: {relative}") from exc
        header = re.match(r"\A---\s*\n(?P<header>.*?)\n---(?:\s*\n|\Z)", document, re.S)
        if header is None:
            fallback = Path(relative).parent.name.strip()
            if not fallback:
                raise SkillVisibilityPolicyError(
                    f"projected Skill has no frontmatter identity: {relative}"
                )
            names.add(fallback)
            continue
        match = re.search(
            r"(?m)^name:\s*([^#\s][^#]*?)\s*$",
            header.group("header"),
        )
        if match is None:
            fallback = Path(relative).parent.name.strip()
            if not fallback:
                raise SkillVisibilityPolicyError(f"projected Skill has no identity: {relative}")
            names.add(fallback)
            continue
        name = match.group(1).strip().strip("\"'")
        if not name:
            raise SkillVisibilityPolicyError(f"projected Skill has an empty name: {relative}")
        names.add(name)
    return frozenset(names)


def resolve_skill_visibility(
    *,
    legacy_upstream_names: Iterable[str],
    platform_default_names: Iterable[str],
    explicitly_installed_names: Iterable[str],
    user_disabled_names: Iterable[str],
    retired_platform_names: Iterable[str] = RETIRED_PLATFORM_SKILL_NAMES,
) -> SkillVisibility:
    legacy = _names(legacy_upstream_names, label="legacy_upstream_names")
    retired = _names(retired_platform_names, label="retired_platform_names")
    defaults = _names(platform_default_names, label="platform_default_names")
    installed = _names(
        explicitly_installed_names,
        label="explicitly_installed_names",
    )
    user_disabled = _names(user_disabled_names, label="user_disabled_names")
    platform_disabled = (legacy | retired) - defaults - installed
    return SkillVisibility(
        platform_disabled=platform_disabled,
        effective_disabled=frozenset(platform_disabled | user_disabled),
    )


def platform_default_skill_names_for_policy(
    *,
    policy_version: int,
    legacy_upstream_names: Iterable[str],
) -> frozenset[str]:
    """Return the allow-by-default set for one immutable config contract.

    Policy v1 intentionally preserves the 0.19.1 seven-Skill platform
    baseline. Policy v2 exposes every official Skill and lets Hermes apply its
    own platform/environment filters; the retired desktop integration is the
    sole platform-level deny. Policy v3 is the balanced allow-list and is
    evaluated against the immutable official v0.20.1 manifest, so target-only
    names are denied unless explicitly installed.
    """

    legacy = _names(
        legacy_upstream_names,
        label="legacy_upstream_names",
    )
    if policy_version == SKILL_VISIBILITY_POLICY_V1:
        return PLATFORM_DEFAULT_SKILL_NAMES
    if policy_version == SKILL_VISIBILITY_POLICY_V2:
        return frozenset(
            (legacy | PLATFORM_DEFAULT_SKILL_NAMES)
            - RETIRED_PLATFORM_SKILL_NAMES
        )
    if policy_version == SKILL_VISIBILITY_POLICY_V3:
        return BALANCED_DEFAULT_SKILL_NAMES
    raise SkillVisibilityPolicyError(
        f"unsupported Skill visibility policy: {policy_version}"
    )


def resolve_versioned_skill_visibility(
    *,
    policy_version: int,
    legacy_upstream_names: Iterable[str],
    explicitly_installed_names: Iterable[str],
    user_disabled_names: Iterable[str],
) -> SkillVisibility:
    legacy = _names(
        legacy_upstream_names,
        label="legacy_upstream_names",
    )
    visibility_source = (
        load_official_upstream_skill_names()
        if policy_version == SKILL_VISIBILITY_POLICY_V3
        else legacy
    )
    return resolve_skill_visibility(
        legacy_upstream_names=visibility_source,
        platform_default_names=platform_default_skill_names_for_policy(
            policy_version=policy_version,
            legacy_upstream_names=visibility_source,
        ),
        explicitly_installed_names=explicitly_installed_names,
        user_disabled_names=user_disabled_names,
    )


def initial_default_skill_visibility(
    *,
    explicitly_installed_names: Iterable[str] = (),
) -> SkillVisibility:
    """Resolve the immutable baseline used by a newly initialized Runtime."""
    legacy = load_legacy_upstream_skill_names()
    return resolve_versioned_skill_visibility(
        policy_version=INITIAL_DEFAULT_SKILL_POLICY_VERSION,
        legacy_upstream_names=legacy,
        explicitly_installed_names=explicitly_installed_names,
        user_disabled_names=(),
    )


def projection_disabled_skill_names(
    *,
    installed_skills: Iterable[dict[str, Any]],
    user_disabled_names: Iterable[str] = (),
    manifest_path: Path = DEFAULT_LEGACY_MANIFEST,
    policy_version: int = SKILL_VISIBILITY_POLICY_V1,
) -> tuple[str, ...]:
    visibility = resolve_versioned_skill_visibility(
        policy_version=policy_version,
        legacy_upstream_names=load_legacy_upstream_skill_names(manifest_path),
        explicitly_installed_names=explicitly_installed_skill_names(installed_skills),
        user_disabled_names=user_disabled_names,
    )
    return tuple(sorted(visibility.effective_disabled))
