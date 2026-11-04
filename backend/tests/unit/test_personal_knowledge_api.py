from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.user.personal_knowledge import create_personal_knowledge_office_preview
from app.config import settings
from app.main import create_app
from app.services.runtime_workspace import Principal


def test_personal_knowledge_routes_are_registered() -> None:
    routes = {
        (route.path, method)
        for route in create_app().routes
        for method in (route.methods or set())
    }

    assert ("/api/v1/user/personal-knowledge", "GET") in routes
    assert ("/api/v1/user/personal-knowledge", "POST") in routes
    assert ("/api/v1/user/personal-knowledge/{knowledge_base_id}", "GET") in routes
    assert (
        "/api/v1/user/personal-knowledge/{knowledge_base_id}/permissions",
        "PUT",
    ) in routes
    assert (
        "/api/v1/user/personal-knowledge/{knowledge_base_id}/permission-directory",
        "GET",
    ) in routes
    assert (
        "/api/v1/user/personal-knowledge/{knowledge_base_id}/files",
        "POST",
    ) in routes
    assert (
        "/api/v1/user/personal-knowledge/{knowledge_base_id}/files/{file_id}/preview",
        "GET",
    ) in routes
    assert (
        "/api/v1/user/personal-knowledge/{knowledge_base_id}/files/{file_id}/office-preview",
        "POST",
    ) in routes
    assert (
        "/api/v1/user/personal-knowledge/{knowledge_base_id}/files/{file_id}/office-content",
        "GET",
    ) in routes
    assert (
        "/api/v1/user/runtime-workspace/{runtime_workspace_id}/personal-knowledge/{knowledge_base_id}",
        "PUT",
    ) in routes


class PreviewService:
    def __init__(self, *, role: str = "preview", filename: str = "report.xlsx") -> None:
        self.access = SimpleNamespace(
            role=role,
            file=SimpleNamespace(
                filename=filename,
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                size_bytes=4096,
                updated_at=datetime(2026, 7, 21, 10, 0, 0),
                oss_url="http://oss.internal/report.xlsx",
            ),
        )

    async def file_access_for_preview(self, **_kwargs):
        return self.access


@pytest.mark.asyncio
async def test_personal_knowledge_office_preview_respects_download_role(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ONLYOFFICE_PUBLIC_URL", "http://office.example.test")
    monkeypatch.setattr(settings, "ONLYOFFICE_INTERNAL_API_BASE", "http://platform.internal:8011")
    monkeypatch.setattr(settings, "ONLYOFFICE_JWT_SECRET", "office-secret")

    response = await create_personal_knowledge_office_preview(
        knowledge_base_id=9,
        file_id=18,
        principal=Principal(org_id=3, user_id=8),
        service=PreviewService(role="preview"),  # type: ignore[arg-type]
    )

    permissions = response.config["document"]["permissions"]
    assert permissions["download"] is False
    assert permissions["copy"] is False
    assert permissions["print"] is False
    assert response.config["document"]["url"].startswith(
        "http://platform.internal:8011/api/v1/user/personal-knowledge/9/files/18/office-content?token="
    )
    assert "oss.internal" not in str(response.config)


@pytest.mark.asyncio
async def test_personal_knowledge_office_preview_rejects_non_office_file(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ONLYOFFICE_PUBLIC_URL", "http://office.example.test")
    monkeypatch.setattr(settings, "ONLYOFFICE_JWT_SECRET", "office-secret")

    with pytest.raises(HTTPException) as raised:
        await create_personal_knowledge_office_preview(
            knowledge_base_id=9,
            file_id=18,
            principal=Principal(org_id=3, user_id=8),
            service=PreviewService(filename="archive.zip"),  # type: ignore[arg-type]
        )

    assert raised.value.status_code == 415
