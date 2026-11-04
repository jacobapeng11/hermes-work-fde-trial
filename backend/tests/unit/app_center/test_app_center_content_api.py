from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app_center_content import (
    get_content_service,
    router,
)
from app.config import settings
from app.services.app_center_contracts import AppCenterError
from app.services.app_center_content_policy import (
    PLATFORM_RETURN_MARKER as _PLATFORM_RETURN_MARKER,
)
from app.services.app_center_packages import normalize_package_content_path


HTML = "<!doctype html><html><body>self-hosted</body></html>"


class FakeContentService:
    def __init__(self, package: bytes) -> None:
        self.package = package

    async def public_package(self, *, app_id: int) -> bytes:
        if app_id != 11:
            raise AppCenterError(
                "APP_CENTER_APP_NOT_FOUND",
                "应用不存在或已删除",
                status_code=404,
                error_code=-14003,
            )
        return self.package


def _package(html: str = HTML) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("index.html", html)
        archive.writestr("style.css", "body{color:red}")
    return output.getvalue()


def _client() -> TestClient:
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[get_content_service] = lambda: FakeContentService(_package())
    return TestClient(application)


def test_content_route_serves_index_with_platform_return_link(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PUBLIC_WEB_BASE", "https://platform.example")
    response = _client().get("/api/v1/app-center/11/index.html")

    assert response.status_code == 200
    assert 'href="https://platform.example"' in response.text
    assert 'target="_top"' in response.text
    assert "返回平台" in response.text
    assert response.text.index("返回平台") > response.text.index("<body>")
    assert response.text.index("返回平台") < response.text.index("self-hosted")
    assert "self-hosted" in response.text
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == (
        "sandbox allow-scripts allow-top-navigation-by-user-activation"
    )
    assert response.headers["x-content-type-options"] == "nosniff"


def test_content_route_ignores_body_text_embedded_in_script(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PUBLIC_WEB_BASE", "https://platform.example")
    html = (
        "<!doctype html><html><head><title>区域看板</title>"
        "<script>const title = '中文标题'; "
        "const fallback = '<body style=\"margin:0\"></body>'; "
        f"const marker = '{_PLATFORM_RETURN_MARKER}';</script></head>\n"
        '<body class="应用"><main>真实内容</main></body></html>'
    )
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[get_content_service] = lambda: FakeContentService(_package(html))
    response = TestClient(application).get("/api/v1/app-center/11/index.html")
    body = response.text
    actual_body = body.find('<body class="应用">')

    assert response.status_code == 200
    assert actual_body >= 0
    assert body.startswith('<body class="应用"><a ', actual_body)
    assert actual_body < body.find("返回平台") < body.find("<main>")
    assert body.find("const fallback") < actual_body


def test_thumbnail_embed_keeps_original_html_and_csp() -> None:
    response = _client().get("/api/v1/app-center/11/index.html?embed=thumbnail")

    assert response.status_code == 200
    assert response.text == HTML
    assert response.headers["content-security-policy"] == "sandbox allow-scripts"


def test_content_route_serves_zip_asset() -> None:
    response = _client().get("/api/v1/app-center/11/style.css")

    assert response.status_code == 200
    assert response.text == "body{color:red}"
    assert "返回平台" not in response.text
    assert response.headers["content-type"].startswith("text/css")
    assert response.headers["content-security-policy"] == "sandbox allow-scripts"


def test_content_route_rejects_unsafe_and_missing_paths() -> None:
    client = _client()

    assert normalize_package_content_path("../index.html") is None
    assert client.get("/api/v1/app-center/11/missing.js").status_code == 404
    assert client.get("/api/v1/app-center/12/index.html").status_code == 404
