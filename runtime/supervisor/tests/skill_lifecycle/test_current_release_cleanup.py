import json
from pathlib import Path

from platform_supervisor.profile_projection import ProfileProjectionController


def test_profile_projection_prunes_legacy_skill_releases(tmp_path: Path) -> None:
    controller = object.__new__(ProfileProjectionController)
    controller.home = tmp_path / "home"
    asset_root = controller.home / "platform/profile-assets/skills/releases/59"
    current = asset_root / "current"
    legacy = asset_root / "69"
    current.mkdir(parents=True)
    legacy.mkdir()
    (current / "SKILL.md").write_text("# Current", encoding="utf-8")
    (legacy / "SKILL.md").write_text("# Old", encoding="utf-8")
    pointer = controller.home / "platform/profile-assets/profiles/1/current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        json.dumps({"release_paths": [str(current)]}), encoding="utf-8"
    )

    controller._prune_old_skill_releases()

    assert (current / "SKILL.md").is_file()
    assert not legacy.exists()


def test_profile_projection_keeps_release_referenced_by_another_profile(
    tmp_path: Path,
) -> None:
    controller = object.__new__(ProfileProjectionController)
    controller.home = tmp_path / "home"
    releases = controller.home / "platform/profile-assets/skills/releases"
    current = releases / "59/current"
    legacy = releases / "1603/1599"
    current.mkdir(parents=True)
    legacy.mkdir(parents=True)
    pointer = controller.home / "platform/profile-assets/profiles/1/current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(
        json.dumps({"release_paths": [str(current), str(legacy)]}),
        encoding="utf-8",
    )

    controller._prune_old_skill_releases()

    assert current.is_dir()
    assert legacy.is_dir()

    pointer.write_text(
        json.dumps({"release_paths": [str(current)]}), encoding="utf-8"
    )
    controller._prune_old_skill_releases()

    assert current.is_dir()
    assert not legacy.exists()


def test_profile_projection_keeps_all_releases_when_pointer_is_invalid(
    tmp_path: Path,
) -> None:
    controller = object.__new__(ProfileProjectionController)
    controller.home = tmp_path / "home"
    legacy = controller.home / "platform/profile-assets/skills/releases/59/69"
    legacy.mkdir(parents=True)
    pointer = controller.home / "platform/profile-assets/profiles/1/current.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("not-json", encoding="utf-8")

    controller._prune_old_skill_releases()

    assert legacy.is_dir()
