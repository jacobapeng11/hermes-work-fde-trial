from __future__ import annotations

import re
from io import BytesIO
from mimetypes import guess_type
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from app.config import settings
from app.services.app_center_contracts import AppCenterError, AppCenterPackage


MAX_UNPACKED_PACKAGE_BYTES = 300 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 2_000


def require_complete_html(html_content: str) -> None:
    if not re.search(r"<html\b|<!doctype\s+html", html_content, re.IGNORECASE):
        raise AppCenterError(
            "APP_CENTER_PACKAGE_INVALID",
            "请提供包含 html 或 doctype 的完整 HTML 文档",
            status_code=400,
            error_code=-14001,
        )


def build_html_package(html_content: str) -> AppCenterPackage:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("index.html", html_content)
    package = output.getvalue()
    if len(package) > settings.APP_CENTER_MAX_PACKAGE_BYTES:
        raise AppCenterError(
            "APP_CENTER_PACKAGE_INVALID",
            f"应用包不能超过 {settings.APP_CENTER_MAX_PACKAGE_BYTES // (1024 * 1024)} MB",
            status_code=400,
            error_code=-14001,
        )
    return AppCenterPackage(content=package)


def validate_static_package(content: bytes) -> None:
    max_bytes = settings.APP_CENTER_MAX_PACKAGE_BYTES
    if not content:
        raise _package_error("不能上传空应用包")
    if len(content) > max_bytes:
        raise _package_error(
            f"应用包不能超过 {max_bytes // (1024 * 1024)} MB"
        )
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            if not entries:
                raise _package_error("应用包不能为空")
            if len(entries) > MAX_PACKAGE_ENTRIES:
                raise _package_error("应用包文件数量过多")
            names: set[str] = set()
            unpacked = 0
            for item in entries:
                normalized = item.filename.replace("\\", "/")
                if _unsafe_zip_path(normalized) or item.flag_bits & 0x1 or _is_symlink(item):
                    raise _package_error("应用包包含不安全的文件路径")
                names.add(normalized.lstrip("./"))
                unpacked += item.file_size
                if unpacked > MAX_UNPACKED_PACKAGE_BYTES:
                    raise _package_error("应用包解压后过大")
            if "index.html" not in names:
                raise _package_error("应用包根目录必须包含 index.html")
    except BadZipFile as exc:
        raise _package_error("请上传有效的 ZIP 应用包") from exc


def normalize_package_content_path(path: str) -> str | None:
    if not path or "\x00" in path or "\\" in path:
        return None
    normalized = path.strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if (
        not normalized
        or normalized in {".", ".."}
        or normalized.startswith("../")
        or "/../" in normalized
    ):
        return None
    if normalized.endswith("/"):
        normalized += "index.html"
    return normalized


def read_package_entry(content: bytes, path: str) -> tuple[bytes, str]:
    normalized = normalize_package_content_path(path)
    if normalized is None:
        raise KeyError(path)
    try:
        with ZipFile(BytesIO(content)) as archive:
            names = {
                item.filename.replace("\\", "/").lstrip("./"): item
                for item in archive.infolist()
            }
            entry = names.get(normalized)
            if (
                entry is None
                or entry.is_dir()
                or entry.flag_bits & 0x1
                or entry.file_size > MAX_UNPACKED_PACKAGE_BYTES
            ):
                raise KeyError(normalized)
            package_entry = archive.read(entry)
    except BadZipFile as exc:
        raise RuntimeError("invalid app package") from exc
    if len(package_entry) > MAX_UNPACKED_PACKAGE_BYTES:
        raise KeyError(normalized)
    media_type = guess_type(normalized, strict=False)[0] or "application/octet-stream"
    if normalized.endswith((".html", ".htm")):
        media_type = "text/html; charset=utf-8"
    return package_entry, media_type


def _unsafe_zip_path(normalized: str) -> bool:
    return (
        normalized.startswith("/")
        or normalized.startswith("../")
        or "/../" in normalized
        or normalized == ".."
        or ":" in normalized.split("/", 1)[0]
    )


def _is_symlink(item: object) -> bool:
    return (getattr(item, "external_attr", 0) >> 16 & 0o170000) == 0o120000


def _package_error(message: str) -> AppCenterError:
    return AppCenterError(
        "APP_CENTER_PACKAGE_INVALID",
        message,
        status_code=400,
        error_code=-14001,
    )


__all__ = [
    "build_html_package",
    "read_package_entry",
    "require_complete_html",
    "validate_static_package",
]
