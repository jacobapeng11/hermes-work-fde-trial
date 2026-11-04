from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Skill, SkillVersion
from tests.unit.test_admin_skill_assets_api import _admin_client, _skill_zip, _upload, _zip


@pytest.mark.asyncio
async def test_market_skill_update_keeps_one_file_and_mysql_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async with _admin_client(tmp_path, monkeypatch) as (client, db):
        created = await _upload(client, _skill_zip(name="market_update"))
        assert created.status_code == 201
        skill_id = int(created.json()["asset_id"])

        updated = await client.post(
            f"/api/v1/admin/assets/skills/{skill_id}/versions",
            params={"category_key": "productivity"},
            files={
                "file": (
                    "skill.zip",
                    _zip({
                        "SKILL.md": "---\nname: market_update\ndescription: v2\n---\n",
                    }),
                    "application/zip",
                )
            },
        )
        assert updated.status_code == 201

        asset_root = tmp_path / "public-skills" / "1" / "market-update"
        assert sorted(path.name for path in asset_root.iterdir()) == ["current"]
        assert await db.scalar(
            select(SkillVersion.id).where(SkillVersion.skill_id == skill_id)
        ) is not None
        assert await db.scalar(
            select(SkillVersion.version).where(
                SkillVersion.skill_id == skill_id,
                SkillVersion.version == 1,
            )
        ) == 1
        assert await db.scalar(
            select(SkillVersion.version).where(
                SkillVersion.skill_id == skill_id,
                SkillVersion.version == 2,
            )
        ) == 2


@pytest.mark.asyncio
async def test_market_unpublish_and_republish_keep_asset_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async with _admin_client(tmp_path, monkeypatch) as (client, db):
        created = await _upload(client, _skill_zip(name="market_state"))
        skill_id = int(created.json()["asset_id"])

        hidden = await client.post(
            f"/api/v1/admin/assets/skills/{skill_id}/unpublish",
            json={"reason": "temporary review"},
        )
        assert hidden.status_code == 200
        assert hidden.json()["market_visible"] is False

        visible = await client.post(
            f"/api/v1/admin/assets/skills/{skill_id}/publish",
            headers={"Idempotency-Key": "market-state-republish"},
            json={"reason": "review complete"},
        )
        assert visible.status_code == 200
        assert visible.json()["asset_id"] == skill_id
        assert visible.json()["market_visible"] is True

        skill = await db.get(Skill, skill_id)
        assert skill is not None and skill.revoked_at is None
