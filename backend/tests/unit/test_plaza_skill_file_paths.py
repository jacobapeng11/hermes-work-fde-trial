from __future__ import annotations

from types import SimpleNamespace

from app.services.plaza.user_skill_inventory.runtime_paths import runtime_skills_root
from app.services.plaza.user_skill_operations.skill_installer import install_skill_directory, rollback_skill_install


def test_runtime_skill_path_uses_persistent_host_root_and_home_subpath(tmp_path) -> None:
    binding = SimpleNamespace(
        runtime_root_host_path=str(tmp_path / "runtime"),
        hermes_home_sub_path="home",
        hermes_home_mount_path="/opt/data",
    )

    assert runtime_skills_root(binding) == tmp_path / "runtime" / "home" / "skills"


def test_install_uses_unique_backup_when_stale_backup_exists(tmp_path) -> None:
    root = tmp_path / "skills"
    target = root / "github" / "market-15"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: old\n---\n", encoding="utf-8")
    stale = target.with_name(".market-15.backup")
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("stale", encoding="utf-8")

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: package\n---\n", encoding="utf-8")

    backup = install_skill_directory(
        source_directory=source,
        skills_root=root,
        category_key="github",
        directory_key="market-15",
        runtime_skill_name="market-runtime-name",
    )

    assert (target / "SKILL.md").read_text(encoding="utf-8").find("name: market-runtime-name") >= 0
    assert backup != stale
    assert (backup / "SKILL.md").read_text(encoding="utf-8").find("name: old") >= 0
    assert (stale / "old.txt").read_text(encoding="utf-8") == "stale"


def test_rollback_new_install_removes_target_without_backup(tmp_path) -> None:
    root = tmp_path / "skills"
    target = root / "personal" / "new-skill"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("new", encoding="utf-8")

    rollback_skill_install(
        backup=target.with_name(".new-skill.backup-missing"),
        skills_root=root,
        category_key="personal",
        directory_key="new-skill",
    )

    assert not target.exists()


def test_rollback_update_replaces_nonempty_target_with_old_backup(tmp_path) -> None:
    root = tmp_path / "skills"
    target = root / "productivity" / "market-1"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("new", encoding="utf-8")
    (target / "new.txt").write_text("new", encoding="utf-8")
    backup = target.with_name(".market-1.backup-owned")
    backup.mkdir(parents=True)
    (backup / "SKILL.md").write_text("old", encoding="utf-8")

    rollback_skill_install(
        backup=backup,
        skills_root=root,
        category_key="productivity",
        directory_key="market-1",
    )

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "old"
    assert not backup.exists()
