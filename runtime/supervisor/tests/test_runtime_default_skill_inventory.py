from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPOSITORY_ROOT / "runtime" / "hermes-runtime-overlay" / "skill_inventory.py"
VISIBILITY_MODULE_PATH = (
    REPOSITORY_ROOT / "runtime" / "hermes-runtime-overlay" / "verify_skill_visibility.py"
)
INVENTORY_PATH = REPOSITORY_ROOT / "runtime" / "hermes-runtime-overlay" / "skill-inventory.json"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "hermes_runtime_skill_inventory",
        MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_visibility_module():
    spec = importlib.util.spec_from_file_location(
        "hermes_runtime_skill_visibility",
        VISIBILITY_MODULE_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_platform_default_skill_inventory_is_exact_and_verifiable() -> None:
    module = _load_module()

    report = module.verify_inventory(
        repository_root=REPOSITORY_ROOT,
        inventory_path=INVENTORY_PATH,
    )

    assert report["status"] == "passed"
    assert report["skill_count"] == 6
    assert report["skill_names"] == [
        "hermes-browser-zh",
        "live-data-report-builder",
        "minimax-docx",
        "minimax-pdf",
        "minimax-xlsx",
        "ppt-master",
    ]
    payload = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    targets = [item["target"] for item in payload["skills"]]
    assert len(targets) == len(set(targets)) == 6
    assert all("hermes-default-skills-zh-v2" not in item["source"] for item in payload["skills"])
    assert all("__pycache__" not in path for path in report["files"])
    assert all(not path.endswith((".pyc", ".pyo")) for path in report["files"])


def test_inventory_rejects_a_nonempty_staging_destination(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "skill"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: exact-skill\ndescription: test\n---\n",
        encoding="utf-8",
    )
    digest = module.sha256_file(source / "SKILL.md")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "version": 1,
                "skills": [
                    {
                        "name": "exact-skill",
                        "source": "skill",
                        "target": "test/exact-skill",
                        "capability": "test",
                        "license": "test",
                        "files": {"SKILL.md": digest},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "staged"
    destination.mkdir()
    (destination / "unexpected.txt").write_text(
        "not declared",
        encoding="utf-8",
    )

    with pytest.raises(module.SkillInventoryError, match="目标目录必须为空"):
        module.stage_inventory(
            repository_root=tmp_path,
            inventory_path=inventory,
            destination=destination,
            expected_names=("exact-skill",),
        )


def test_inventory_staging_copies_only_declared_files(tmp_path: Path) -> None:
    module = _load_module()
    destination = tmp_path / "staged"

    report = module.stage_inventory(
        repository_root=REPOSITORY_ROOT,
        inventory_path=INVENTORY_PATH,
        destination=destination,
    )

    assert report["status"] == "passed"
    staged = sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    )
    assert staged == sorted(report["target_files"])
    assert report["root"] == "platform-default-skills"
    assert len(list(destination.rglob("SKILL.md"))) == 5

    staged_report = module.verify_staged_inventory(
        inventory_path=INVENTORY_PATH,
        destination=destination,
    )
    assert staged_report["status"] == "passed"
    assert staged_report["skill_count"] == 5


def test_readonly_root_stages_ppt_master_only(tmp_path: Path) -> None:
    module = _load_module()
    destination = tmp_path / "readonly-staged"

    report = module.stage_inventory(
        repository_root=REPOSITORY_ROOT,
        inventory_path=INVENTORY_PATH,
        destination=destination,
        root="platform-readonly-skills",
    )

    assert report["status"] == "passed"
    assert report["root"] == "platform-readonly-skills"
    assert report["skill_names"] == ["ppt-master"]
    skill_md_list = list(destination.rglob("SKILL.md"))
    assert len(skill_md_list) == 1
    assert skill_md_list[0].relative_to(destination).as_posix() == (
        "productivity/ppt-master/SKILL.md"
    )
    assert (destination / "productivity/ppt-master/PLATFORM_BOUNDARY.md").is_file()
    assert not (destination / "productivity/ppt-master/scripts/image_gen.py").exists()

    staged_report = module.verify_staged_inventory(
        inventory_path=INVENTORY_PATH,
        destination=destination,
        root="platform-readonly-skills",
    )
    assert staged_report["status"] == "passed"
    assert staged_report["skill_count"] == 1


def test_visibility_policy_v2_defaults_all_fixed_official_skills() -> None:
    module = _load_visibility_module()

    assert module.OFFICIAL_SOURCE_COUNT == 81
    assert module.PLATFORM_PACKAGED_SKILL_NAMES == {
        "live-data-report-builder",
        "hermes-browser-zh",
        "minimax-docx",
        "minimax-pdf",
        "minimax-xlsx",
        "ppt-master",
    }
    assert module.RETIRED_PLATFORM_SKILL_NAME == "hermes-desktop-plugins"
    assert module.USER_DISABLED_SKILL_NAME == "user-disabled"
    assert not hasattr(module, "OFFICIAL_DEFAULT_SKILL_NAMES")
    assert not hasattr(module, "DEFAULT_SKILL_NAMES")


def test_staged_inventory_rejects_modified_and_extra_assets(tmp_path: Path) -> None:
    module = _load_module()
    destination = tmp_path / "staged"
    module.stage_inventory(
        repository_root=REPOSITORY_ROOT,
        inventory_path=INVENTORY_PATH,
        destination=destination,
    )
    skill = next(destination.rglob("SKILL.md"))
    skill.write_text("modified\n", encoding="utf-8")
    (destination / "unexpected.txt").write_text("extra\n", encoding="utf-8")

    with pytest.raises(
        module.SkillInventoryError,
        match="最终 Skill 资产集合不一致",
    ):
        module.verify_staged_inventory(
            inventory_path=INVENTORY_PATH,
            destination=destination,
        )
