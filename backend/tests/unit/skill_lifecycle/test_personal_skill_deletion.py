from pathlib import Path

import pytest

from app.config import settings
from app.services.personal_skill_assets import (
    PersonalSkillAssetService,
    PersonalSkillPackageStorage,
    SqlAlchemyPersonalSkillAssetStore,
)
from app.services.personal_skill_lifecycle import PersonalSkillLifecycleService
from app.services.runtime_workspace import Principal
from tests.unit.test_default_profile_assets_api import _skill_zip
from tests.unit.test_profile_desired import _database, _seed


@pytest.mark.asyncio
async def test_personal_delete_clears_current_and_legacy_source_directories(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "HERMES_PUBLIC_ASSET_ROOT", str(tmp_path))
    engine, sessions = await _database()
    async with sessions() as db:
        await _seed(db)
        principal = Principal(org_id=1, user_id=2)
        uploaded = await PersonalSkillAssetService(
            store=SqlAlchemyPersonalSkillAssetStore(db),
            root=str(tmp_path),
        ).upload_skill_zip(
            principal=principal,
            content=_skill_zip("Delete Me"),
        )
        skill_root = tmp_path / "personal-skills" / "1" / "2" / uploaded.slug
        legacy = skill_root / "20240101000000"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("legacy", encoding="utf-8")

        removal = await PersonalSkillLifecycleService(db).delete(
            org_id=1,
            user_id=2,
            skill_id=uploaded.skill_id,
            operation_id="personal-skill-delete-test",
        )
        await db.commit()
        PersonalSkillPackageStorage(str(tmp_path)).remove_all(
            org_id=1,
            user_id=2,
            slug=removal.storage_slug,
        )

        assert not skill_root.exists()
    await engine.dispose()
