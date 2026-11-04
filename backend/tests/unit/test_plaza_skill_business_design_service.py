from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile, ZipInfo

import pytest

from app.config import settings
from app.services.personal_skill_assets import (
    InMemoryPersonalSkillAssetStore,
    PersonalSkillAssetError,
    PersonalSkillAssetService,
)
from app.services.runtime_workspace import Principal


def _skill_zip(*, name: str = "Market Skill", description: str = "Market description.", body: str = "Body") -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: {description}\nversion: 9.9.9\n---\n# {body}\n",
        )
        archive.writestr("README.md", body)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_personal_skill_same_name_advances_stable_asset_version(tmp_path) -> None:
    store = InMemoryPersonalSkillAssetStore()
    service = PersonalSkillAssetService(store=store, root=str(tmp_path))
    principal = Principal(org_id=1, user_id=1)

    uploaded = await service.upload_skill_zip(principal=principal, content=_skill_zip(name="Personal Skill"))

    assert uploaded.slug == "personal-skill"
    assert uploaded.version == 1
    assert uploaded.skill_version_id == 1
    updated = await service.upload_skill_zip(
        principal=principal,
        content=_skill_zip(name="Personal   Skill", body="changed"),
    )
    assert updated.skill_id == uploaded.skill_id
    assert updated.skill_version_id != uploaded.skill_version_id
    assert updated.version == 2

    await service.delete_skill(principal=principal, skill_id=uploaded.skill_id)
    assert await service.list_skills(principal=principal) == []

    replacement = await service.upload_skill_zip(principal=principal, content=_skill_zip(name="Personal Skill", body="changed again"))
    assert replacement.slug == "personal-skill"
    assert replacement.skill_id != uploaded.skill_id


@pytest.mark.asyncio
async def test_personal_skill_description_override_and_clear_restore_default(tmp_path) -> None:
    store = InMemoryPersonalSkillAssetStore()
    service = PersonalSkillAssetService(store=store, root=str(tmp_path))
    principal = Principal(org_id=1, user_id=1)

    uploaded = await service.upload_skill_zip(
        principal=principal,
        content=_skill_zip(description="包内默认说明"),
    )
    custom = await service.update_description(
        principal=principal,
        skill_id=uploaded.skill_id,
        description="  我的业务说明  ",
    )
    assert custom.description == "我的业务说明"

    restored = await service.update_description(
        principal=principal,
        skill_id=uploaded.skill_id,
        description="",
    )
    assert restored.description == "包内默认说明"
    assert restored.skill_version_id == uploaded.skill_version_id

    with pytest.raises(PersonalSkillAssetError, match="^skill_description_too_long$"):
        await service.update_description(
            principal=principal,
            skill_id=uploaded.skill_id,
            description="x" * 2001,
        )


def _zip_with_entries(entries: list[tuple[str | ZipInfo, bytes | str]]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, body in entries:
            archive.writestr(name, body)
    return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", "personal_skill_package_empty"),
        (b"not-a-zip", "personal_skill_package_invalid"),
        (
            _zip_with_entries([("README.md", "missing")]),
            "personal_skill_package_missing_manifest",
        ),
        (
            _zip_with_entries(
                [
                    ("SKILL.md", "---\nname: Unsafe\n---\n"),
                    ("../escape.txt", "escape"),
                ]
            ),
            "personal_skill_package_unsafe_path",
        ),
        (
            _zip_with_entries(
                [
                    ("SKILL.md", "---\nname: Absolute\n---\n"),
                    ("/absolute.txt", "escape"),
                ]
            ),
            "personal_skill_package_unsafe_path",
        ),
    ],
)
async def test_personal_skill_rejects_invalid_or_unsafe_archives(
    tmp_path,
    content: bytes,
    code: str,
) -> None:
    service = PersonalSkillAssetService(
        store=InMemoryPersonalSkillAssetStore(),
        root=str(tmp_path),
    )

    with pytest.raises(PersonalSkillAssetError, match=f"^{code}$"):
        await service.upload_skill_zip(
            principal=Principal(org_id=1, user_id=1),
            content=content,
        )
    assert not list(tmp_path.rglob("SKILL.md"))


@pytest.mark.asyncio
async def test_personal_skill_rejects_symbolic_links(tmp_path) -> None:
    link = ZipInfo("link")
    link.external_attr = 0o120777 << 16
    content = _zip_with_entries(
        [
            ("SKILL.md", "---\nname: Symlink\n---\n"),
            (link, "target"),
        ]
    )
    service = PersonalSkillAssetService(
        store=InMemoryPersonalSkillAssetStore(),
        root=str(tmp_path),
    )

    with pytest.raises(
        PersonalSkillAssetError,
        match="^personal_skill_package_symlink$",
    ):
        await service.upload_skill_zip(
            principal=Principal(org_id=1, user_id=1),
            content=content,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting_name", "limit", "code"),
    [
        ("SKILLHUB_MAX_PACKAGE_BYTES", 1, "personal_skill_package_too_large"),
        ("SKILLHUB_MAX_FILE_COUNT", 1, "personal_skill_package_too_many_files"),
        (
            "SKILLHUB_MAX_SINGLE_FILE_BYTES",
            4,
            "personal_skill_package_single_file_too_large",
        ),
        (
            "SKILLHUB_MAX_UNPACKED_BYTES",
            8,
            "personal_skill_package_unpacked_too_large",
        ),
    ],
)
async def test_personal_skill_enforces_archive_resource_limits(
    monkeypatch,
    tmp_path,
    setting_name: str,
    limit: int,
    code: str,
) -> None:
    monkeypatch.setattr(settings, setting_name, limit)
    content = _zip_with_entries(
        [
            ("SKILL.md", "---\nname: Limited\n---\n# body\n"),
            ("README.md", "more body"),
        ]
    )
    service = PersonalSkillAssetService(
        store=InMemoryPersonalSkillAssetStore(),
        root=str(tmp_path),
    )

    with pytest.raises(PersonalSkillAssetError, match=f"^{code}$"):
        await service.upload_skill_zip(
            principal=Principal(org_id=1, user_id=1),
            content=content,
        )
