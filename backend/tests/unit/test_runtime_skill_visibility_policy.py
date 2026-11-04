import hashlib
import json
from pathlib import Path

from app.hermes_runtime.skill_visibility_policy import (
    OFFICIAL_HERMES_SKILL_ROOT,
    PLATFORM_DEFAULT_SKILL_NAMES,
    RETIRED_PLATFORM_SKILL_NAMES,
    SKILL_VISIBILITY_POLICY_V1,
    SKILL_VISIBILITY_POLICY_V2,
    SKILL_VISIBILITY_POLICY_V3,
    explicitly_installed_skill_names,
    load_legacy_upstream_skill_names,
    load_official_upstream_skill_names,
    projected_skill_names,
    resolve_skill_visibility,
    resolve_versioned_skill_visibility,
)
from app.hermes_runtime.system_skill_catalog import (
    SYSTEM_SKILL_DESCRIPTION_CATALOG,
    system_skill_descriptions,
)


MANIFEST = (
    Path(__file__).resolve().parents[2] / "app" / "hermes_runtime" / "legacy-upstream-skills.json"
)


def test_manifest_and_platform_defaults_are_exact() -> None:
    legacy = load_legacy_upstream_skill_names(MANIFEST)
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert len(legacy) == 70
    assert payload["upstream_commit"] == "cc4cab2f592e60a197e796506de9168f74baf3ea"
    assert hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest() == "eaf89438f20a8090c8490268482c3b4276d9be06baf97d2c6ecac03a6bd46a7b"
    assert PLATFORM_DEFAULT_SKILL_NAMES == frozenset(
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
    assert RETIRED_PLATFORM_SKILL_NAMES == frozenset(
        {"hermes-desktop-plugins", "pptx-generator"}
    )
    assert OFFICIAL_HERMES_SKILL_ROOT == "/opt/hermes/skills"
    assert {"hermes-agent", "hermes-agent-skill-authoring"} <= legacy


def test_system_skill_description_catalog_matches_platform_defaults() -> None:
    descriptions = system_skill_descriptions()
    assert set(descriptions) == set(PLATFORM_DEFAULT_SKILL_NAMES)
    assert all(value.strip() for value in descriptions.values())
    assert descriptions == SYSTEM_SKILL_DESCRIPTION_CATALOG


def test_legacy_policy_hides_only_known_upstream_assets() -> None:
    legacy = load_legacy_upstream_skill_names(MANIFEST)

    result = resolve_skill_visibility(
        legacy_upstream_names=legacy,
        platform_default_names=PLATFORM_DEFAULT_SKILL_NAMES,
        explicitly_installed_names=(),
        user_disabled_names=(),
    )

    assert len(result.platform_disabled) == 70
    assert "github-auth" in result.effective_disabled
    assert "hermes-desktop-plugins" in result.effective_disabled
    assert "pptx-generator" in result.effective_disabled
    assert "hermes-agent" not in result.effective_disabled
    assert "hermes-browser-zh" not in result.effective_disabled
    assert "unknown-personal-skill" not in result.effective_disabled


def test_policy_v2_allows_all_official_skills_but_keeps_user_and_retired_denies() -> None:
    legacy = load_legacy_upstream_skill_names(MANIFEST)

    old = resolve_versioned_skill_visibility(
        policy_version=SKILL_VISIBILITY_POLICY_V1,
        legacy_upstream_names=legacy,
        explicitly_installed_names=(),
        user_disabled_names=("hermes-agent",),
    )
    target = resolve_versioned_skill_visibility(
        policy_version=SKILL_VISIBILITY_POLICY_V2,
        legacy_upstream_names=legacy,
        explicitly_installed_names=(),
        user_disabled_names=("hermes-agent",),
    )

    assert "github-auth" in old.platform_disabled
    assert target.platform_disabled == {"hermes-desktop-plugins", "pptx-generator"}
    assert target.effective_disabled == {
        "hermes-agent",
        "hermes-desktop-plugins",
        "pptx-generator",
    }


def test_policy_v3_keeps_balanced_baseline_and_hides_other_official_skills() -> None:
    legacy = load_legacy_upstream_skill_names(MANIFEST)
    official = load_official_upstream_skill_names()
    target = resolve_versioned_skill_visibility(
        policy_version=SKILL_VISIBILITY_POLICY_V3,
        legacy_upstream_names=legacy,
        explicitly_installed_names=(),
        user_disabled_names=(),
    )

    assert len(official) == 81
    assert "codebase-inspection" not in target.effective_disabled
    assert "github-auth" in target.effective_disabled
    assert "hermes-desktop-plugins" in target.effective_disabled
    assert "pptx-generator" in target.effective_disabled
    assert "ppt-master" not in target.effective_disabled


def test_explicit_install_can_replace_a_retired_platform_skill() -> None:
    legacy = load_legacy_upstream_skill_names(MANIFEST)

    result = resolve_skill_visibility(
        legacy_upstream_names=legacy,
        platform_default_names=PLATFORM_DEFAULT_SKILL_NAMES,
        explicitly_installed_names=("hermes-desktop-plugins",),
        user_disabled_names=(),
    )

    assert "hermes-desktop-plugins" not in result.effective_disabled


def test_explicit_install_exempts_a_legacy_name_but_user_disable_wins() -> None:
    legacy = load_legacy_upstream_skill_names(MANIFEST)

    result = resolve_skill_visibility(
        legacy_upstream_names=legacy,
        platform_default_names=PLATFORM_DEFAULT_SKILL_NAMES,
        explicitly_installed_names=("github-auth", "unknown-personal-skill"),
        user_disabled_names=("github-auth", "unknown-personal-skill"),
    )

    assert "github-auth" not in result.platform_disabled
    assert result.effective_disabled >= {
        "github-auth",
        "unknown-personal-skill",
    }


def test_installed_names_use_runtime_identity_fields() -> None:
    names = explicitly_installed_skill_names(
        (
            {"runtime_skill_name": "github-auth", "name": "显示名称"},
            {"slug": "personal-helper"},
            {"manifest": {"name": "manifest-skill"}},
        )
    )

    assert names == frozenset({"github-auth", "显示名称", "personal-helper", "manifest-skill"})


def test_projected_skill_names_read_frontmatter_from_named_profile_files() -> None:
    names = projected_skill_names(
        {
            "expert/github-auth/SKILL.md": (b"---\nname: github-auth\ndescription: test\n---\n"),
            "expert/helper/SKILL.md": ("---\nname: 中文助手\ndescription: test\n---\n".encode()),
            "expert/helper/reference.md": b"not a skill",
            "expert/legacy-skill/SKILL.md": b"# legacy asset\n",
        }
    )

    assert names == frozenset({"github-auth", "中文助手", "legacy-skill"})
