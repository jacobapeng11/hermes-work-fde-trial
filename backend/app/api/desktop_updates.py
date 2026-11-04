from __future__ import annotations

import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from app.config import settings


router = APIRouter(prefix="/desktop-updates", tags=["desktop-updates"])

_INSTALLER_SUFFIXES = (".exe", ".exe.blockmap", ".dmg", ".zip", ".blockmap")


def _release_root() -> Path:
    return settings.DESKTOP_RELEASE_DIR.expanduser().resolve()


def _resolve_release_file(
    filename: str,
    *,
    allowed_suffixes: tuple[str, ...],
) -> Path | None:
    if not filename or filename in {".", ".."}:
        return None
    if "/" in filename or "\\" in filename or "\x00" in filename:
        return None
    if Path(filename).name != filename:
        return None
    if not filename.endswith(allowed_suffixes):
        return None

    root = _release_root()
    requested = root / filename
    try:
        requested_stat = requested.lstat()
        resolved = requested.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None
    if not stat.S_ISREG(requested_stat.st_mode):
        return None
    if resolved.parent != root:
        return None
    return resolved


@router.get("/latest.yml")
async def get_desktop_latest_manifest(
) -> FileResponse:
    manifest = _resolve_release_file("latest.yml", allowed_suffixes=(".yml",))
    if manifest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="update manifest not found",
        )
    return FileResponse(
        manifest,
        media_type="text/plain",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/latest-mac.yml")
async def get_desktop_latest_mac_manifest(
) -> FileResponse:
    manifest = _resolve_release_file("latest-mac.yml", allowed_suffixes=(".yml",))
    if manifest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="mac update manifest not found",
        )
    return FileResponse(
        manifest,
        media_type="text/plain",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{filename}")
async def get_desktop_update_file(
    filename: str,
) -> FileResponse:
    release_file = _resolve_release_file(filename, allowed_suffixes=_INSTALLER_SUFFIXES)
    if release_file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="update file not found",
        )
    return FileResponse(
        release_file,
        headers={"Cache-Control": "no-store"},
    )
