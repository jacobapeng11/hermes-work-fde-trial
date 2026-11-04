from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import KnowledgeBase, KnowledgeBaseFile, User
from app.schemas.personal_knowledge import PersonalKnowledgeFileSummary
from app.services.personal_knowledge import PersonalKnowledgeError
from app.services.personal_knowledge_acl import (
    EffectiveRole,
    PersonalKnowledgeAclService,
)
from app.services.personal_knowledge_oss import (
    PersonalKnowledgeOssClient,
    PersonalKnowledgeOssError,
)
from app.services.personal_knowledge_provider import (
    PersonalKnowledgeLifecycleProvider,
    PersonalKnowledgeProviderError,
    build_personal_knowledge_provider,
)
from app.services.runtime_workspace import Principal


logger = logging.getLogger(__name__)

FILE_STATUS = {0: "pending", 1: "ready", -1: "failed"}
ALLOWED_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".txt",
    ".md",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
}


@dataclass(frozen=True)
class PersonalKnowledgeFileAccess:
    knowledge_base: KnowledgeBase
    file: KnowledgeBaseFile
    role: EffectiveRole


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 for character in value)


class PersonalKnowledgeFileService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        provider: PersonalKnowledgeLifecycleProvider | None = None,
        providers: Mapping[str, PersonalKnowledgeLifecycleProvider] | None = None,
        oss: PersonalKnowledgeOssClient | None = None,
        acl: PersonalKnowledgeAclService | None = None,
    ) -> None:
        self.session = session
        self._provider_override = provider
        self._providers = dict(providers or {})
        self.oss = oss or PersonalKnowledgeOssClient()
        self.acl = acl or PersonalKnowledgeAclService(session)

    def _provider_for(
        self,
        provider_name: str,
    ) -> PersonalKnowledgeLifecycleProvider:
        provider = self._providers.get(provider_name)
        if provider is not None:
            return provider
        if self._provider_override is not None:
            return self._provider_override
        return build_personal_knowledge_provider(provider_name)

    async def _owner_name(self, owner_user_id: int) -> str:
        return await self._user_name(owner_user_id)

    async def _user_name(self, user_id: int) -> str:
        user = await self.session.get(User, user_id)
        return str(user.name) if user is not None else str(user_id)

    async def _summary(
        self,
        file: KnowledgeBaseFile,
        *,
        uploader_name: str | None = None,
    ) -> PersonalKnowledgeFileSummary:
        return PersonalKnowledgeFileSummary(
            file_id=int(file.id),
            filename=str(file.filename),
            size_bytes=int(file.size_bytes),
            mime_type=file.mime_type,
            status=FILE_STATUS.get(int(file.file_status), "parsing"),
            uploader_id=int(file.uploader_id),
            uploader_name=uploader_name or await self._owner_name(int(file.uploader_id)),
            updated_at=file.updated_at,
        )

    async def list_files(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
    ) -> list[PersonalKnowledgeFileSummary]:
        knowledge_base, role = await self.acl.accessible(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            capability="preview",
        )
        rows = (
            await self.session.execute(
                select(KnowledgeBaseFile, User.name)
                .join(User, User.id == KnowledgeBaseFile.uploader_id)
                .where(
                    KnowledgeBaseFile.org_id == principal.org_id,
                    KnowledgeBaseFile.knowledge_base_id == knowledge_base_id,
                    KnowledgeBaseFile.is_deleted == 0,
                )
                .order_by(KnowledgeBaseFile.updated_at.desc())
            )
        ).all()
        assert role in {"owner", "download", "preview"}
        if knowledge_base.provider == "weknora":
            await self._sync_weknora_files([file for file, _name in rows])
        return [
            PersonalKnowledgeFileSummary(
                file_id=int(file.id),
                filename=str(file.filename),
                size_bytes=int(file.size_bytes),
                mime_type=file.mime_type,
                status=FILE_STATUS.get(int(file.file_status), "parsing"),
                uploader_id=int(file.uploader_id),
                uploader_name=str(uploader_name),
                updated_at=file.updated_at,
            )
            for file, uploader_name in rows
        ]

    async def _sync_weknora_files(
        self,
        files: list[KnowledgeBaseFile],
    ) -> None:
        candidates = [
            file
            for file in files
            if int(file.file_status) == 0 and file.provider_document_id
        ]
        if not candidates:
            return
        try:
            provider = self._provider_for("weknora")
        except PersonalKnowledgeProviderError as exc:
            synced_at = datetime.utcnow()
            for file in candidates:
                file.provider_error = exc.code
                file.provider_synced_at = synced_at
            await self.session.commit()
            return
        semaphore = asyncio.Semaphore(4)

        async def fetch(file: KnowledgeBaseFile):
            async with semaphore:
                try:
                    document = await provider.get_document(  # type: ignore[attr-defined]
                        str(file.provider_document_id)
                    )
                    return file, document, None
                except PersonalKnowledgeProviderError as exc:
                    return file, None, exc

        results = await asyncio.gather(*(fetch(file) for file in candidates))
        synced_at = datetime.utcnow()
        for file, document, error in results:
            file.provider_synced_at = synced_at
            if error is not None:
                file.provider_error = error.code
                continue
            assert document is not None
            file.provider_status = document.parse_status
            file.provider_error = None
            next_status = {
                "pending": 0,
                "processing": 0,
                "finalizing": 0,
                "completed": 1,
                "failed": -1,
                "cancelled": -1,
            }.get(document.parse_status)
            if next_status is None:
                file.provider_error = "unknown_provider_status"
                continue
            if int(file.file_status) != next_status:
                file.file_status = next_status
                file.updated_at = synced_at
        await self.session.commit()

    async def upload_file(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
        filename: str,
        content_type: str | None,
        content: bytes,
    ) -> PersonalKnowledgeFileSummary:
        knowledge_base, _role = await self.acl.accessible(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            capability="manage",
        )
        normalized_name = filename.strip()
        extension = (
            f".{normalized_name.rsplit('.', 1)[-1].lower()}"
            if "." in normalized_name
            else ""
        )
        max_bytes = settings.PERSONAL_KNOWLEDGE_MAX_UPLOAD_MB * 1024 * 1024
        if (
            not content
            or len(content) > max_bytes
            or not normalized_name
            or _has_control_characters(normalized_name)
            or extension not in ALLOWED_EXTENSIONS
        ):
            raise PersonalKnowledgeError(
                "invalid_personal_knowledge_file", status_code=400
            )
        if knowledge_base.provider == "weknora":
            return await self._upload_weknora(
                principal=principal,
                knowledge_base=knowledge_base,
                filename=normalized_name,
                content_type=content_type,
                content=content,
            )
        return await self._upload_filebase(
            principal=principal,
            knowledge_base=knowledge_base,
            filename=normalized_name,
            content_type=content_type,
            content=content,
        )

    async def _upload_weknora(
        self,
        *,
        principal: Principal,
        knowledge_base: KnowledgeBase,
        filename: str,
        content_type: str | None,
        content: bytes,
    ) -> PersonalKnowledgeFileSummary:
        if not knowledge_base.dataset_id:
            raise PersonalKnowledgeError(
                "personal_knowledge_provider_mapping_required", status_code=409
            )
        try:
            provider = self._provider_for("weknora")
            document = await provider.upload_document(  # type: ignore[attr-defined]
                dataset_id=str(knowledge_base.dataset_id),
                filename=filename,
                content_type=content_type,
                content=content,
            )
        except PersonalKnowledgeProviderError as exc:
            if exc.status_code == 409:
                raise PersonalKnowledgeError(
                    "personal_knowledge_file_duplicate", status_code=409
                ) from exc
            raise PersonalKnowledgeError(
                "personal_knowledge_upload_failed", status_code=502
            ) from exc
        file = KnowledgeBaseFile(
            org_id=principal.org_id,
            knowledge_base_id=int(knowledge_base.id),
            filename=filename,
            oss_url=None,
            oss_key=None,
            provider_document_id=document.document_id,
            provider_status=document.parse_status,
            provider_synced_at=datetime.utcnow(),
            size_bytes=len(content),
            mime_type=content_type,
            file_status=0,
            uploader_id=principal.user_id,
            updater_id=principal.user_id,
            is_deleted=0,
        )
        self.session.add(file)
        try:
            await self.session.commit()
        except Exception:
            await self.session.rollback()
            try:
                await provider.delete_document(  # type: ignore[attr-defined]
                    document.document_id
                )
            except PersonalKnowledgeProviderError:
                logger.warning(
                    "personal knowledge WeKnora upload compensation failed"
                )
            raise
        await self.session.refresh(file)
        return await self._summary(file)

    async def _upload_filebase(
        self,
        *,
        principal: Principal,
        knowledge_base: KnowledgeBase,
        filename: str,
        content_type: str | None,
        content: bytes,
    ) -> PersonalKnowledgeFileSummary:
        try:
            uploaded = await self.oss.upload_bytes(
                content=content,
                original_filename=filename,
                content_type=content_type or "application/octet-stream",
            )
        except PersonalKnowledgeOssError as exc:
            raise PersonalKnowledgeError(str(exc), status_code=502) from exc
        file = KnowledgeBaseFile(
            org_id=principal.org_id,
            knowledge_base_id=int(knowledge_base.id),
            filename=filename,
            oss_url=uploaded.url,
            oss_key=uploaded.key,
            size_bytes=len(content),
            mime_type=content_type,
            file_status=0,
            uploader_id=principal.user_id,
            updater_id=principal.user_id,
            is_deleted=0,
        )
        self.session.add(file)
        await self.session.commit()
        await self.session.refresh(file)
        return await self._summary(file)

    async def _file_access(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
        file_id: int,
        capability: str,
    ) -> PersonalKnowledgeFileAccess:
        knowledge_base, role = await self.acl.accessible(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            capability=capability,  # type: ignore[arg-type]
        )
        file = await self.session.scalar(
            select(KnowledgeBaseFile).where(
                KnowledgeBaseFile.id == file_id,
                KnowledgeBaseFile.org_id == principal.org_id,
                KnowledgeBaseFile.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseFile.is_deleted == 0,
            )
        )
        if file is None:
            raise PersonalKnowledgeError(
                "personal_knowledge_file_not_found", status_code=404
            )
        return PersonalKnowledgeFileAccess(
            knowledge_base=knowledge_base,
            file=file,
            role=role,
        )

    async def rename_file(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
        file_id: int,
        filename: str,
    ) -> PersonalKnowledgeFileSummary:
        access = await self._file_access(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            file_id=file_id,
            capability="manage",
        )
        if _has_control_characters(filename):
            raise PersonalKnowledgeError(
                "invalid_personal_knowledge_file", status_code=400
            )
        access.file.filename = filename
        access.file.updater_id = principal.user_id
        access.file.updated_at = datetime.utcnow()
        await self.session.commit()
        await self.session.refresh(access.file)
        return await self._summary(access.file)

    async def delete_file(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
        file_id: int,
    ) -> None:
        access = await self._file_access(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            file_id=file_id,
            capability="manage",
        )
        if access.knowledge_base.provider == "weknora":
            await self._delete_weknora_document(access)
        access.file.is_deleted = 1
        access.file.updater_id = principal.user_id
        access.file.updated_at = datetime.utcnow()
        await self.session.commit()

    async def _delete_weknora_document(
        self,
        access: PersonalKnowledgeFileAccess,
    ) -> None:
        document_id = str(access.file.provider_document_id or "").strip()
        if not document_id:
            raise PersonalKnowledgeError(
                "personal_knowledge_provider_mapping_required", status_code=409
            )
        try:
            provider = self._provider_for("weknora")
        except PersonalKnowledgeProviderError as exc:
            raise PersonalKnowledgeError(
                "personal_knowledge_delete_failed", status_code=502
            ) from exc
        try:
            await provider.delete_document(document_id)  # type: ignore[attr-defined]
        except PersonalKnowledgeProviderError as exc:
            if exc.status_code != 404 and not await self._weknora_delete_confirmed(
                provider, document_id
            ):
                raise PersonalKnowledgeError(
                    "personal_knowledge_delete_failed", status_code=502
                ) from exc
        access.file.provider_status = "deleting"
        access.file.provider_error = None
        access.file.provider_synced_at = datetime.utcnow()

    async def _weknora_delete_confirmed(
        self,
        provider: PersonalKnowledgeLifecycleProvider,
        document_id: str,
    ) -> bool:
        try:
            document = await provider.get_document(document_id)  # type: ignore[attr-defined]
        except PersonalKnowledgeProviderError as exc:
            return exc.status_code == 404
        return document.parse_status == "deleting"

    async def file_for_delivery(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
        file_id: int,
        download: bool,
    ) -> PersonalKnowledgeFileAccess:
        return await self._file_access(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            file_id=file_id,
            capability="download" if download else "preview",
        )

    async def file_access_for_preview(
        self,
        *,
        principal: Principal,
        knowledge_base_id: int,
        file_id: int,
    ) -> PersonalKnowledgeFileAccess:
        return await self._file_access(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            file_id=file_id,
            capability="preview",
        )

    @asynccontextmanager
    async def stream_file_content(
        self,
        *,
        access: PersonalKnowledgeFileAccess,
        download: bool,
    ) -> AsyncIterator[httpx.Response]:
        if access.knowledge_base.provider == "weknora":
            document_id = str(access.file.provider_document_id or "").strip()
            if not document_id:
                raise PersonalKnowledgeError(
                    "personal_knowledge_provider_mapping_required", status_code=409
                )
            try:
                provider = self._provider_for("weknora")
                async with provider.stream_document(  # type: ignore[attr-defined]
                    document_id,
                    download=download,
                ) as response:
                    yield response
            except PersonalKnowledgeProviderError as exc:
                raise PersonalKnowledgeError(
                    "personal_knowledge_file_delivery_failed", status_code=502
                ) from exc
            return
        if not access.file.oss_url:
            raise PersonalKnowledgeError(
                "personal_knowledge_file_delivery_failed", status_code=502
            )
        async with httpx.AsyncClient(timeout=600, trust_env=False) as client:
            async with client.stream("GET", str(access.file.oss_url)) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise PersonalKnowledgeError(
                        "personal_knowledge_file_delivery_failed", status_code=502
                    )
                yield response
