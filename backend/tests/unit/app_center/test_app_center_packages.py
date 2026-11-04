from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from app.config import settings
from app.services.app_center_contracts import AppCenterError
from app.services.app_center_packages import (
    build_html_package,
    require_complete_html,
    validate_static_package,
)


def _zip(entries: dict[str, str | bytes]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_build_html_package_uses_root_index() -> None:
    html = "<!doctype html><html><body>ok</body></html>"
    package = build_html_package(html)

    with ZipFile(BytesIO(package.content)) as archive:
        assert archive.namelist() == ["index.html"]
        assert archive.read("index.html").decode("utf-8") == html


def test_require_complete_html_rejects_fragment() -> None:
    with pytest.raises(AppCenterError) as error:
        require_complete_html("<div>片段</div>")

    assert error.value.code == "APP_CENTER_PACKAGE_INVALID"
    assert error.value.error_code == -14001


def test_validate_static_package_accepts_root_index() -> None:
    validate_static_package(_zip({"index.html": "<!doctype html>", "app.js": "console.log(1)"}))


def test_validate_static_package_rejects_missing_root_index() -> None:
    with pytest.raises(AppCenterError) as error:
        validate_static_package(_zip({"web/index.html": "<!doctype html>"}))

    assert "index.html" in error.value.message


@pytest.mark.parametrize(
    "name",
    ["../index.html", "/tmp/index.html", "C:/index.html"],
)
def test_validate_static_package_rejects_unsafe_path(name: str) -> None:
    with pytest.raises(AppCenterError) as error:
        validate_static_package(_zip({name: "<!doctype html>"}))

    assert "不安全" in error.value.message


def test_validate_static_package_rejects_oversized_package() -> None:
    with pytest.raises(AppCenterError) as error:
        validate_static_package(b"0" * 3)

    assert "请上传有效的 ZIP 应用包" in error.value.message


def test_validate_static_package_uses_configured_package_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_CENTER_MAX_PACKAGE_BYTES", 2)

    with pytest.raises(AppCenterError) as error:
        validate_static_package(b"000")

    assert error.value.message == "应用包不能超过 0 MB"


def test_validate_static_package_rejects_excessive_entry_count() -> None:
    entries = {
        "index.html": "<!doctype html>",
        **{
            f"assets/file-{index}.js": str(index)
            for index in range(2_000)
        },
    }

    with pytest.raises(AppCenterError) as error:
        validate_static_package(_zip(entries))

    assert "文件数量" in error.value.message
