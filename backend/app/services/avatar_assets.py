from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import struct
from uuid import uuid4

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AvatarAsset
from app.schemas.avatar_assets import AvatarAssetSummary
from app.services.runtime_workspace import Principal


BUILTIN_AVATAR_IDS = tuple(f"avatar-{index:02d}" for index in range(1, 9))
BUILTIN_AVATAR_PREFIX = "builtin:"
ACCEPTED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


class AvatarAssetError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class StoredAvatar:
    url: str
    key: str


class AvatarObjectStorage:
    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def upload(
        self,
        *,
        principal: Principal,
        content: bytes,
        content_type: str,
        extension: str,
    ) -> StoredAvatar:
        public_id = uuid4().hex
        filename = f"{public_id}.{extension}"
        folder = (
            f"avatars/{principal.org_id}/{principal.user_id}/"
            f"{datetime.now().strftime('%Y%m%d')}/"
        )
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=60, trust_env=False)
        try:
            response = await client.post(
                settings.AVATAR_OSS_UPLOAD_URL,
                files={"file": (filename, content, content_type)},
                data={
                    "bucketName": settings.AVATAR_OSS_BUCKET,
                    "folderName": folder,
                    "fileName": filename,
                },
            )
        except httpx.HTTPError as exc:
            raise AvatarAssetError("avatar_upload_failed", status_code=502) from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code != 200:
            raise AvatarAssetError("avatar_upload_failed", status_code=502)
        try:
            raw_url = str((response.json() or {}).get("url") or "")
        except ValueError as exc:
            raise AvatarAssetError("avatar_upload_failed", status_code=502) from exc
        if not raw_url:
            raise AvatarAssetError("avatar_upload_failed", status_code=502)
        if settings.AVATAR_OSS_INTERNAL_HOST in raw_url:
            suffix_start = raw_url.find(settings.AVATAR_OSS_INTERNAL_HOST) + len(
                settings.AVATAR_OSS_INTERNAL_HOST
            )
            url = (
                f"{settings.AVATAR_OSS_PUBLIC_BASE_URL.rstrip('/')}"
                f"{raw_url[suffix_start:]}"
            )
        else:
            url = raw_url
        return StoredAvatar(url=url, key=f"{folder}{filename}")


class AvatarAssetService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        storage: AvatarObjectStorage | None = None,
    ) -> None:
        self.db = db
        self.storage = storage or AvatarObjectStorage()

    async def list_available(
        self,
        *,
        principal: Principal,
    ) -> list[AvatarAssetSummary]:
        custom = (
            await self.db.execute(
                select(AvatarAsset)
                .where(
                    AvatarAsset.org_id == principal.org_id,
                    AvatarAsset.status == "active",
                    or_(
                        AvatarAsset.source_kind == "organization",
                        AvatarAsset.owner_user_id == principal.user_id,
                    ),
                )
                .order_by(AvatarAsset.sort_order, AvatarAsset.id)
            )
        ).scalars().all()
        return [
            AvatarAssetSummary(
                avatar_value=f"{BUILTIN_AVATAR_PREFIX}{avatar_id}",
                label=f"默认头像 {index}",
                source_kind="builtin",
                image_url=None,
                sort_order=index,
            )
            for index, avatar_id in enumerate(BUILTIN_AVATAR_IDS, start=1)
        ] + [self._summary(item) for item in custom]

    async def upload(
        self,
        *,
        principal: Principal,
        filename: str,
        content_type: str | None,
        content: bytes,
    ) -> AvatarAssetSummary:
        normalized_type, extension = validate_avatar_image(
            content=content,
            declared_content_type=content_type,
        )
        stored = await self.storage.upload(
            principal=principal,
            content=content,
            content_type=normalized_type,
            extension=extension,
        )
        row = AvatarAsset(
            public_id=uuid4().hex,
            org_id=principal.org_id,
            owner_user_id=principal.user_id,
            source_kind="user_upload",
            label=_avatar_label(filename),
            avatar_url=stored.url,
            storage_key=stored.key,
            mime_type=normalized_type,
            status="active",
            sort_order=1000,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return self._summary(row)

    @staticmethod
    def _summary(row: AvatarAsset) -> AvatarAssetSummary:
        return AvatarAssetSummary(
            avatar_value=str(row.avatar_url),
            label=str(row.label),
            source_kind=str(row.source_kind),
            image_url=str(row.avatar_url),
            sort_order=int(row.sort_order),
        )


async def validate_avatar_value(
    db: AsyncSession,
    *,
    principal: Principal,
    avatar_value: str | None,
) -> str | None:
    if avatar_value is None:
        return None
    value = avatar_value.strip()
    if not value:
        return None
    if value.startswith(BUILTIN_AVATAR_PREFIX):
        avatar_id = value.removeprefix(BUILTIN_AVATAR_PREFIX)
        if avatar_id not in BUILTIN_AVATAR_IDS:
            raise AvatarAssetError("avatar_not_available", status_code=403)
        return value
    row = await db.scalar(
        select(AvatarAsset).where(
            AvatarAsset.org_id == principal.org_id,
            AvatarAsset.avatar_url == value,
            AvatarAsset.status == "active",
            or_(
                AvatarAsset.source_kind == "organization",
                AvatarAsset.owner_user_id == principal.user_id,
            ),
        )
    )
    if row is None:
        raise AvatarAssetError("avatar_not_available", status_code=403)
    return str(row.avatar_url)


def validate_avatar_image(
    *,
    content: bytes,
    declared_content_type: str | None,
) -> tuple[str, str]:
    if not content:
        raise AvatarAssetError("avatar_file_empty")
    if len(content) > settings.AVATAR_MAX_UPLOAD_MB * 1024 * 1024:
        raise AvatarAssetError("avatar_file_too_large", status_code=413)
    detected_type, width, height = _image_metadata(content)
    if detected_type is None:
        raise AvatarAssetError("avatar_file_invalid", status_code=415)
    if declared_content_type not in ACCEPTED_IMAGE_TYPES:
        raise AvatarAssetError("avatar_file_type_not_allowed", status_code=415)
    if detected_type != declared_content_type:
        raise AvatarAssetError("avatar_file_type_mismatch", status_code=415)
    if width <= 0 or height <= 0 or width * height > settings.AVATAR_MAX_SOURCE_PIXELS:
        raise AvatarAssetError("avatar_dimensions_invalid", status_code=415)
    extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[
        detected_type
    ]
    return detected_type, extension


def _image_metadata(content: bytes) -> tuple[str | None, int, int]:
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        width, height = struct.unpack(">II", content[16:24])
        return "image/png", width, height
    if content.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(content):
            if content[offset] != 0xFF:
                offset += 1
                continue
            marker = content[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(content):
                break
            segment_length = int.from_bytes(content[offset : offset + 2], "big")
            if segment_length < 2 or offset + segment_length > len(content):
                break
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                height = int.from_bytes(content[offset + 3 : offset + 5], "big")
                width = int.from_bytes(content[offset + 5 : offset + 7], "big")
                return "image/jpeg", width, height
            offset += segment_length
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP" and len(content) >= 30:
        chunk = content[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(content[24:27], "little")
            height = 1 + int.from_bytes(content[27:30], "little")
            return "image/webp", width, height
        if chunk == b"VP8L" and len(content) >= 25:
            bits = int.from_bytes(content[21:25], "little")
            return "image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 " and content[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(content[26:28], "little") & 0x3FFF
            height = int.from_bytes(content[28:30], "little") & 0x3FFF
            return "image/webp", width, height
    return None, 0, 0


def _avatar_label(filename: str) -> str:
    stem = os.path.basename(filename or "自定义头像").rsplit(".", 1)[0].strip()
    return (stem or "自定义头像")[:100]
