from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import uuid

import httpx

from app.config import settings


class PersonalKnowledgeOssError(RuntimeError):
    pass


@dataclass(frozen=True)
class UploadedObject:
    url: str
    key: str
    name: str


def _build_filename(original: str) -> str:
    base = original.rsplit(".", 1)[0]
    extension = original.rsplit(".", 1)[-1] if "." in original else ""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    random_suffix = uuid.uuid4().hex[:6]
    stem = f"{base}_{timestamp}_{random_suffix}"
    return f"{stem}.{extension}" if extension else stem


class PersonalKnowledgeOssClient:
    def __init__(
        self,
        *,
        upload_url: str | None = None,
        bucket_name: str | None = None,
        public_base_url: str | None = None,
        internal_host: str | None = None,
        timeout_seconds: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.upload_url = upload_url or settings.PERSONAL_KNOWLEDGE_OSS_UPLOAD_URL
        self.bucket_name = bucket_name or settings.PERSONAL_KNOWLEDGE_OSS_BUCKET
        self.public_base_url = (
            public_base_url or settings.PERSONAL_KNOWLEDGE_OSS_PUBLIC_BASE_URL
        ).rstrip("/")
        self.internal_host = (
            internal_host or settings.PERSONAL_KNOWLEDGE_OSS_INTERNAL_HOST
        )
        self.timeout_seconds = timeout_seconds or max(
            600,
            settings.PERSONAL_KNOWLEDGE_TIMEOUT_SECONDS,
        )
        self.client = client

    async def upload_bytes(
        self,
        *,
        content: bytes,
        original_filename: str,
        content_type: str = "application/octet-stream",
    ) -> UploadedObject:
        filename = _build_filename(os.path.basename(original_filename))
        folder_path = f"knowledge/{datetime.now().strftime('%Y%m%d')}/"
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            trust_env=False,
        )
        try:
            response = await client.post(
                self.upload_url,
                files={
                    "file": (
                        filename,
                        content,
                        content_type or "application/octet-stream",
                    )
                },
                data={
                    "bucketName": self.bucket_name,
                    "folderName": folder_path,
                    "fileName": filename,
                },
            )
        except httpx.HTTPError as exc:
            raise PersonalKnowledgeOssError("oss_upload_failed") from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code != 200:
            raise PersonalKnowledgeOssError("oss_upload_failed")
        try:
            raw_url = str((response.json() or {}).get("url") or "")
        except ValueError as exc:
            raise PersonalKnowledgeOssError("oss_upload_failed") from exc
        if not raw_url:
            raise PersonalKnowledgeOssError("oss_upload_failed")
        if self.internal_host in raw_url:
            suffix_start = raw_url.find(self.internal_host) + len(self.internal_host)
            url = f"{self.public_base_url}{raw_url[suffix_start:]}"
        else:
            url = raw_url
        return UploadedObject(
            url=url,
            key=f"{folder_path}{filename}",
            name=filename,
        )
