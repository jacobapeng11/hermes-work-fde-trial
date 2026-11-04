from pathlib import Path

import pytest

from app.models import Skill, SkillVersion
from app.services.skill_storage_migration import (
    SkillStorageMigrationError,
    desired_market_visible,
    materialize_current_package,
    package_root_from_uri,
    plan_current_package,
    plan_private_current_package,
)
from app.services.plaza.public_catalog.package_parser import sha256_directory


def test_market_visible_backfill_keeps_private_assets_visible() -> None:
    assert desired_market_visible(asset_scope="personal", status="private", revoked_at=None)
    assert desired_market_visible(asset_scope="application", status="private", revoked_at=None)


@pytest.mark.parametrize(
    ("status", "revoked_at", "expected"),
    [("published", None, True), ("revoked", None, False), ("published", "2026-09-01", False), ("deleted", "2026-09-01", False)],
)
def test_public_market_visible_backfill_is_safe_for_historical_status(status: str, revoked_at: object, expected: bool) -> None:
    assert desired_market_visible(asset_scope="public", status=status, revoked_at=revoked_at) is expected


def test_package_uri_must_remain_inside_public_asset_root(tmp_path: Path) -> None:
    with pytest.raises(SkillStorageMigrationError, match="outside configured asset root"):
        package_root_from_uri(storage_root=tmp_path, package_uri=str(tmp_path.parent / "escape" / "deployment"))


def test_package_uri_resolves_to_version_root(tmp_path: Path) -> None:
    package = tmp_path / "public-skills" / "1" / "search" / "v7"
    package.mkdir(parents=True)
    assert package_root_from_uri(storage_root=tmp_path, package_uri=str(package / "deployment")) == package


def test_package_root_requires_known_material_path(tmp_path: Path) -> None:
    with pytest.raises(SkillStorageMigrationError, match="must point"):
        package_root_from_uri(storage_root=tmp_path, package_uri=str(tmp_path / "public-skills" / "1" / "search" / "v1"))


def test_materialize_current_package_cleans_old_version_directories(tmp_path: Path) -> None:
    source = tmp_path / "public-skills" / "1" / "search" / "v1"
    (source / "deployment").mkdir(parents=True)
    (source / "deployment" / "SKILL.md").write_text("name: search\n", encoding="utf-8")
    original = source / "original.zip"
    original.write_bytes(b"package")
    skill = Skill(id=7, org_id=1, owner_user_id=2, slug="search", name="Search")
    version = SkillVersion(
        id=8,
        org_id=1,
        skill_id=7,
        version=1,
        package_uri=str(source / "deployment"),
        package_sha256=sha256_directory(source / "deployment"),
    )
    plan = plan_current_package(storage_root=tmp_path, skill=skill, version=version)
    materialize_current_package(plan)

    assert (tmp_path / "public-skills" / "1" / "search" / "current" / "deployment" / "SKILL.md").exists()
    assert source.exists()


def test_legacy_catalog_slug_does_not_become_public_storage_slug(tmp_path: Path) -> None:
    source = tmp_path / "public-skills" / "1" / "daily-report" / "v1"
    (source / "deployment").mkdir(parents=True)
    (source / "deployment" / "SKILL.md").write_text("name: daily-report\n", encoding="utf-8")
    (source / "original.zip").write_bytes(b"package")
    from app.services.plaza.public_catalog.package_parser import sha256_directory

    skill = Skill(id=7, org_id=1, owner_user_id=2, slug="legacy-public-skill-29", name="daily-report")
    version = SkillVersion(
        id=8,
        org_id=1,
        skill_id=7,
        version=1,
        package_uri=str(source / "deployment"),
        package_sha256=sha256_directory(source / "deployment"),
    )
    plan = plan_current_package(storage_root=tmp_path, skill=skill, version=version)

    assert plan.target_root == tmp_path / "public-skills" / "1" / "daily-report" / "current"


def test_private_skill_uses_one_current_directory(tmp_path: Path) -> None:
    source = tmp_path / "migrated-personal-skills" / "1" / "2" / "legacy-7" / "deployment"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("name: private-search\n", encoding="utf-8")
    skill = Skill(id=7, org_id=1, owner_user_id=2, slug="legacy-personal-skill-7", name="private-search")
    version = SkillVersion(id=8, org_id=1, skill_id=7, version=1, package_uri=str(source), package_sha256=sha256_directory(source))

    plan = plan_private_current_package(storage_root=tmp_path, application_root=tmp_path / "application-agents", skill=skill, version=version)

    assert plan.target_root == tmp_path / "personal-skills" / "1" / "2" / "private-search" / "current"
