from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.desktop_updates import router
from app.config import settings
from app.main import create_app


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_desktop_updates_route_is_registered_on_platform_app() -> None:
    app = create_app()

    route_paths = {route.path for route in app.routes}

    assert "/api/v1/desktop-updates/latest.yml" in route_paths
    assert "/api/v1/desktop-updates/latest-mac.yml" in route_paths
    assert "/api/v1/desktop-updates/{filename}" in route_paths


def test_latest_manifest_is_returned_as_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "DESKTOP_RELEASE_DIR", tmp_path)
    (tmp_path / "latest.yml").write_text("version: 0.1.18\n", encoding="utf-8")

    response = _client(tmp_path).get("/api/v1/desktop-updates/latest.yml")

    assert response.status_code == 200
    assert response.text == "version: 0.1.18\n"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"


def test_encoded_installer_filename_supports_range_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "DESKTOP_RELEASE_DIR", tmp_path)
    payload = bytes(range(256)) * 16
    installer = tmp_path / "Sun-Cowork Setup 0.1.17.exe"
    installer.write_bytes(payload)

    response = _client(tmp_path).get(
        "/api/v1/desktop-updates/Sun-Cowork%20Setup%200.1.17.exe",
        headers={"Range": "bytes=0-1024"},
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 0-1024/{len(payload)}"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.content == payload[:1025]


def test_blockmap_is_an_allowed_installer_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "DESKTOP_RELEASE_DIR", tmp_path)
    (tmp_path / "Sun-Cowork Setup 0.1.17.exe.blockmap").write_bytes(b"blockmap")

    response = _client(tmp_path).get(
        "/api/v1/desktop-updates/Sun-Cowork%20Setup%200.1.17.exe.blockmap"
    )

    assert response.status_code == 200
    assert response.content == b"blockmap"


def test_rejects_traversal_unsupported_suffix_and_missing_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "DESKTOP_RELEASE_DIR", tmp_path)
    (tmp_path / "allowed.exe").write_bytes(b"installer")
    secret = tmp_path.parent / "desktop-update-secret.exe"
    secret.write_bytes(b"secret")
    try:
        os.symlink(secret, tmp_path / "linked.exe")

        client = _client(tmp_path)
        for filename in (
            "..%2Fdesktop-update-secret.exe",
            "..%5Cdesktop-update-secret.exe",
            "notes.txt",
            "linked.exe",
            "missing.exe",
        ):
            response = client.get(f"/api/v1/desktop-updates/{filename}")
            assert response.status_code == 404, filename
    finally:
        secret.unlink(missing_ok=True)


def test_mac_latest清单_作为文本返回(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "DESKTOP_RELEASE_DIR", tmp_path)
    (tmp_path / "latest-mac.yml").write_text("version: 0.1.18\n", encoding="utf-8")

    response = _client(tmp_path).get("/api/v1/desktop-updates/latest-mac.yml")

    assert response.status_code == 200
    assert response.text == "version: 0.1.18\n"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"


def test_mac_dmg_安装器_支持下载(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "DESKTOP_RELEASE_DIR", tmp_path)
    payload = b"fake-dmg-content"
    (tmp_path / "Sun-Cowork-0.1.18.dmg").write_bytes(payload)

    response = _client(tmp_path).get("/api/v1/desktop-updates/Sun-Cowork-0.1.18.dmg")

    assert response.status_code == 200
    assert response.content == payload


def test_mac_zip_更新包_支持下载(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "DESKTOP_RELEASE_DIR", tmp_path)
    payload = b"fake-zip-content"
    (tmp_path / "Sun-Cowork-0.1.18-mac.zip").write_bytes(payload)

    response = _client(tmp_path).get(
        "/api/v1/desktop-updates/Sun-Cowork-0.1.18-mac.zip"
    )

    assert response.status_code == 200
    assert response.content == payload


def test_mac_blockmap_支持下载(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "DESKTOP_RELEASE_DIR", tmp_path)
    (tmp_path / "Sun-Cowork-0.1.18-mac.zip.blockmap").write_bytes(b"blockmap")

    response = _client(tmp_path).get(
        "/api/v1/desktop-updates/Sun-Cowork-0.1.18-mac.zip.blockmap"
    )

    assert response.status_code == 200
    assert response.content == b"blockmap"
