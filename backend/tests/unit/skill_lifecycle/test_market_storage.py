from pathlib import Path

from app.services.plaza.public_catalog.package_storage import PublicSkillPackageStorage


def _zip_bytes(name: str, marker: str) -> bytes:
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile

    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: test\n---\n{marker}\n",
        )
    return buffer.getvalue()


def test_public_storage_keeps_only_current_asset_package(tmp_path: Path) -> None:
    storage = PublicSkillPackageStorage(tmp_path)

    first = storage.store(
        org_id=1,
        normalized_name="skill-7",
        version_number=1,
        content=_zip_bytes("Release Skill", "v1"),
        category_key="productivity",
    )
    second = storage.store(
        org_id=1,
        normalized_name="skill-7",
        version_number=2,
        content=_zip_bytes("Release Skill", "v2"),
        category_key="productivity",
    )
    storage.finalize_package(second)

    asset_root = tmp_path / "public-skills" / "1" / "skill-7"
    assert (asset_root / "current" / "deployment" / "SKILL.md").read_text(
        encoding="utf-8"
    ).endswith("v2\n")
    assert sorted(path.name for path in asset_root.iterdir()) == ["current"]
    assert "/current/" in second.deployment_package_uri
    assert first.deployment_package_uri == second.deployment_package_uri


def test_public_storage_restores_previous_current_when_update_is_discarded(
    tmp_path: Path,
) -> None:
    storage = PublicSkillPackageStorage(tmp_path)
    storage.store(
        org_id=1,
        normalized_name="skill-7",
        version_number=1,
        content=_zip_bytes("Release Skill", "v1"),
        category_key="productivity",
    )
    second = storage.store(
        org_id=1,
        normalized_name="skill-7",
        version_number=2,
        content=_zip_bytes("Release Skill", "v2"),
        category_key="productivity",
    )

    storage.discard_package(second)

    asset_root = tmp_path / "public-skills" / "1" / "skill-7"
    assert (asset_root / "current" / "deployment" / "SKILL.md").read_text(
        encoding="utf-8"
    ).endswith("v1\n")
    assert sorted(path.name for path in asset_root.iterdir()) == ["current"]
