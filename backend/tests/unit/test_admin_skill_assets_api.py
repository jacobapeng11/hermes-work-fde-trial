from __future__ import annotations

from contextlib import asynccontextmanager
from io import BytesIO
import re
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest
from sqlalchemy import select

from app.api.v1.admin.deps import get_admin_principal
from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Skill, SkillCategory
from app.services.admin_auth import AdminPrincipal
from tests.unit.test_asset_catalog import _database


def _zip(entries: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def _skill_zip(*, name: str) -> bytes:
    return _zip(
        {
            "SKILL.md": (
                f"---\nname: {name}\n"
                "description: Admin upload regression\n---\n"
            )
        }
    )


@asynccontextmanager
async def _admin_client(tmp_path, monkeypatch):
    engine, sessions = await _database()
    async with sessions() as db:
        db.add(
            SkillCategory(
                org_id=1,
                category_key="productivity",
                display_name="Productivity",
                is_builtin=True,
                is_market_visible=True,
                is_personal_visible=True,
            )
        )
        await db.commit()

        async def database():
            yield db

        async def admin_principal() -> AdminPrincipal:
            return AdminPrincipal(org_id=1, user_id=990095, roles=("admin",))

        previous_db = app.dependency_overrides.get(get_db)
        previous_admin = app.dependency_overrides.get(get_admin_principal)
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_admin_principal] = admin_principal
        monkeypatch.setattr(settings, "HERMES_PUBLIC_ASSET_ROOT", str(tmp_path))
        try:
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                yield client, db
        finally:
            if previous_db is None:
                app.dependency_overrides.pop(get_db, None)
            else:
                app.dependency_overrides[get_db] = previous_db
            if previous_admin is None:
                app.dependency_overrides.pop(get_admin_principal, None)
            else:
                app.dependency_overrides[get_admin_principal] = previous_admin
    await engine.dispose()


async def _upload(client: httpx.AsyncClient, content: bytes) -> httpx.Response:
    return await client.post(
        "/api/v1/admin/assets/skills",
        params={"category_key": "productivity"},
        files={"file": ("skill.zip", content, "application/zip")},
    )


@pytest.mark.asyncio
async def test_admin_skill_multipart_upload_normalizes_underscore_slug(
    tmp_path,
    monkeypatch,
) -> None:
    async with _admin_client(tmp_path, monkeypatch) as (client, db):
        response = await _upload(client, _skill_zip(name="scheduled_task"))
        assert response.status_code == 201
        assert response.json()["slug"] == "scheduled-task"
        skill = await db.scalar(select(Skill).where(Skill.slug == "scheduled-task"))
        assert skill is not None
        assert skill.category_key == "productivity"


@pytest.mark.asyncio
async def test_admin_skill_upload_normalizes_unicode_name(
    tmp_path,
    monkeypatch,
) -> None:
    async with _admin_client(tmp_path, monkeypatch) as (client, _db):
        response = await _upload(client, _skill_zip(name="定时任务"))

        assert response.status_code == 201
        assert re.fullmatch(r"skill-[a-f0-9]{16}", response.json()["slug"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "detail"),
    [
        (b"not-a-zip", "skill package must be a valid zip"),
        (_zip({"README.md": "missing skill"}), "skill package must contain SKILL.md"),
        (
            _zip({"../SKILL.md": "---\nname: unsafe\n---\n"}),
            "skill package contains an unsafe path",
        ),
    ],
)
async def test_admin_skill_upload_maps_invalid_packages_to_400_without_asset(
    tmp_path,
    monkeypatch,
    content: bytes,
    detail: str,
) -> None:
    async with _admin_client(tmp_path, monkeypatch) as (client, db):
        response = await _upload(client, content)

        assert response.status_code == 400
        assert response.json() == {"detail": detail}
        assert await db.scalar(select(Skill.id)) is None


@pytest.mark.asyncio
async def test_admin_skill_upload_unpublish_republish_and_delete_guard(
    tmp_path,
    monkeypatch,
) -> None:
    async with _admin_client(tmp_path, monkeypatch) as (client, db):
        uploaded = await _upload(client, _skill_zip(name="lifecycle_skill"))
        assert uploaded.status_code == 201
        skill_id = int(uploaded.json()["asset_id"])

        disabled = await client.post(
            f"/api/v1/admin/assets/skills/{skill_id}/unpublish",
            json={"reason": "regression test"},
        )
        assert disabled.status_code == 200
        skill = await db.get(Skill, skill_id)
        assert skill is not None and skill.market_visible is False
        assert skill.revoked_at is None
        assert skill.status == "published"

        enabled = await client.post(
            f"/api/v1/admin/assets/skills/{skill_id}/publish",
            headers={"Idempotency-Key": "skill-lifecycle-enable"},
            json={"reason": "regression test complete"},
        )
        assert enabled.status_code == 200
        await db.refresh(skill)
        assert skill.market_visible is True

        deleted = await client.delete(f"/api/v1/admin/assets/skills/{skill_id}")
        assert deleted.status_code == 410
        await db.refresh(skill)
        assert skill.status == "published"
