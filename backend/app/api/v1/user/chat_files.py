from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import (
    get_principal,
    get_runtime_infrastructure,
)
from app.database import get_db
from app.schemas.runtime_chat import (
    ChatDownloadIntent,
    ChatFileSummary,
    ChatFileTransferIntent,
    ChatUploadIntentRequest,
    OfficePreviewSession,
    RegisterChatFileRequest,
    RenameChatFileRequest,
)
from app.services.chat_files import (
    ChatFileError,
    ChatFileIndexService,
    ChatFilePermissionError,
    ChatFileSessionDeletedError,
    ChatFileRecord,
    RegisterChatFileCommand,
    SqlAlchemyChatFileStore,
    normalize_workspace_relative_path,
)
from app.services.runtime_workspace import Principal
from app.config import settings
from app.hermes_runtime.dashboard_protocol import HermesDashboardProtocolAdapter
from app.hermes_runtime.protocol import HermesProtocolError
from app.services.runtime_infrastructure import (
    RuntimeInfrastructure,
    build_runtime_protocol_adapters,
)
from app.services.runtime_chat import RuntimeChatService, RuntimeChatPreparationError, SqlAlchemyRuntimeChatStore
from app.services.runtime_ticket_issuer import RuntimeTicketIssuer
from app.services.office_preview import (
    OfficePreviewError,
    build_office_config,
    create_office_file_token,
    decode_office_file_token,
    office_file_type,
)

router = APIRouter(prefix="/chat-files", tags=["user-chat-files"])


class InMemoryChatFileStore:
    def __init__(self) -> None:
        self.records: dict[int, ChatFileRecord] = {}
        self.owners: dict[int, tuple[int, int]] = {}
        self.next_file_id = 1

    async def session_workspace_relative_root(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> str:
        if org_id != 1 or user_id != 1 or runtime_workspace_id != 1:
            raise ChatFilePermissionError("runtime workspace not found")
        return f"workspace/sessions/{runtime_session_id}"

    async def register_file(self, *, org_id: int, user_id: int, command: RegisterChatFileCommand) -> ChatFileRecord:
        if org_id != 1 or user_id != 1 or command.runtime_workspace_id != 1:
            raise ChatFilePermissionError("runtime workspace not found")
        matches = [
            record
            for record in self.records.values()
            if self.owners.get(record.file_id) == (org_id, user_id)
            and record.runtime_workspace_id == command.runtime_workspace_id
            and record.runtime_session_id == command.runtime_session_id
            and record.relative_path == command.relative_path
        ]
        if matches:
            canonical = max(matches, key=lambda item: item.file_id)
            for duplicate in matches:
                if duplicate.file_id != canonical.file_id:
                    self.records.pop(duplicate.file_id, None)
                    self.owners.pop(duplicate.file_id, None)
            record = ChatFileRecord(
                file_id=canonical.file_id,
                runtime_workspace_id=command.runtime_workspace_id,
                runtime_session_id=command.runtime_session_id,
                runtime_run_id=command.runtime_run_id,
                relative_path=command.relative_path,
                display_name=command.relative_path.rsplit("/", 1)[-1],
                mime_type=command.mime_type,
                size_bytes=command.size_bytes,
                source_type=command.source_type,
                created_at=canonical.created_at,
            )
            self.records[record.file_id] = record
            return record
        record = ChatFileRecord(
            file_id=self.next_file_id,
            runtime_workspace_id=command.runtime_workspace_id,
            runtime_session_id=command.runtime_session_id,
            runtime_run_id=command.runtime_run_id,
            relative_path=command.relative_path,
            display_name=command.relative_path.rsplit("/", 1)[-1],
            mime_type=command.mime_type,
            size_bytes=command.size_bytes,
            source_type=command.source_type,
        )
        self.records[record.file_id] = record
        self.owners[record.file_id] = (org_id, user_id)
        self.next_file_id += 1
        return record

    async def reserve_upload(
        self,
        *,
        org_id: int,
        user_id: int,
        command: RegisterChatFileCommand,
    ) -> ChatFileRecord:
        if org_id != 1 or user_id != 1 or command.runtime_workspace_id != 1:
            raise ChatFilePermissionError("runtime workspace not found")
        requested = command.relative_path.rsplit("/", 1)
        prefix = requested[0]
        filename = requested[-1]
        record = ChatFileRecord(
            file_id=self.next_file_id,
            runtime_workspace_id=command.runtime_workspace_id,
            runtime_session_id=command.runtime_session_id,
            runtime_run_id=None,
            relative_path=f"{prefix}/{self.next_file_id}/{filename}",
            display_name=filename,
            mime_type=command.mime_type,
            size_bytes=command.size_bytes,
            source_type="upload",
            status="pending",
        )
        self.records[record.file_id] = record
        self.owners[record.file_id] = (org_id, user_id)
        self.next_file_id += 1
        return record

    async def list_files(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str | None = None,
        runtime_run_id: str | None = None,
    ) -> list[ChatFileRecord]:
        if org_id != 1 or user_id != 1 or runtime_workspace_id != 1:
            raise ChatFilePermissionError("runtime workspace not found")
        return [
            record for record in self.records.values()
            if record.runtime_workspace_id == runtime_workspace_id
            and self.owners.get(record.file_id) == (org_id, user_id)
            and record.status == "active"
            and (runtime_session_id is None or record.runtime_session_id == runtime_session_id)
            and (runtime_run_id is None or record.runtime_run_id == runtime_run_id)
        ]

    async def get_file(self, *, org_id: int, user_id: int, file_id: int) -> ChatFileRecord:
        record = self.records.get(file_id)
        if (
            record is None
            or record.status != "active"
            or self.owners.get(file_id) != (org_id, user_id)
        ):
            raise ChatFilePermissionError("chat file not found")
        return record

    async def get_files(self, *, org_id: int, user_id: int, file_ids: list[int]) -> list[ChatFileRecord]:
        return [
            record
            for file_id in file_ids
            if (record := self.records.get(file_id)) is not None
            and record.status == "active"
            and self.owners.get(file_id) == (org_id, user_id)
        ]

    async def rename_file(self, *, org_id: int, user_id: int, file_id: int, relative_path: str) -> ChatFileRecord:
        record = await self.get_file(org_id=org_id, user_id=user_id, file_id=file_id)
        renamed = ChatFileRecord(
            file_id=record.file_id,
            runtime_workspace_id=record.runtime_workspace_id,
            runtime_session_id=record.runtime_session_id,
            runtime_run_id=record.runtime_run_id,
            relative_path=relative_path,
            display_name=relative_path.rsplit("/", 1)[-1],
            mime_type=record.mime_type,
            size_bytes=record.size_bytes,
            source_type=record.source_type,
            created_at=record.created_at,
        )
        self.records[file_id] = renamed
        return renamed

    async def delete_file(self, *, org_id: int, user_id: int, file_id: int) -> ChatFileRecord:
        record = await self.get_file(org_id=org_id, user_id=user_id, file_id=file_id)
        del self.records[file_id]
        self.owners.pop(file_id, None)
        return record


_chat_file_store = InMemoryChatFileStore()


async def get_chat_file_service(db: AsyncSession = Depends(get_db)) -> ChatFileIndexService:
    from app.config import settings

    if settings.RUNTIME_CHAT_MODE == "real":
        return ChatFileIndexService(SqlAlchemyChatFileStore(db))
    return ChatFileIndexService(_chat_file_store)


async def get_chat_file_runtime_service(db: AsyncSession = Depends(get_db)) -> RuntimeChatService:
    return RuntimeChatService(SqlAlchemyRuntimeChatStore(db))


async def get_chat_file_protocol_adapter(
    infrastructure: RuntimeInfrastructure | None = Depends(
        get_runtime_infrastructure
    ),
) -> HermesDashboardProtocolAdapter:
    if infrastructure is not None:
        return infrastructure.dashboard_adapter
    return build_runtime_protocol_adapters().dashboard_adapter


def _summary(record: ChatFileRecord) -> ChatFileSummary:
    return ChatFileSummary(
        file_id=record.file_id,
        runtime_workspace_id=record.runtime_workspace_id,
        runtime_session_id=record.runtime_session_id,
        runtime_run_id=record.runtime_run_id,
        relative_path=record.relative_path,
        display_name=record.display_name,
        mime_type=record.mime_type,
        size_bytes=record.size_bytes,
        source_type=record.source_type,
        created_at=record.created_at,
    )


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ChatFileError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, ChatFileSessionDeletedError):
        return HTTPException(status_code=status.HTTP_410_GONE, detail=exc.code)
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _safe_upload_name(raw_name: str) -> str:
    name = unquote(raw_name).strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in {".", ".."} or "\x00" in name:
        raise ChatFileError("upload filename is invalid")
    if len(name) > 255:
        raise ChatFileError("upload filename is too long")
    return name


def _available_upload_name(records: list[ChatFileRecord], filename: str) -> str:
    existing = {record.display_name.casefold() for record in records}
    if filename.casefold() not in existing:
        return filename
    stem, separator, suffix = filename.rpartition(".")
    base = stem if separator and stem else filename
    extension = f".{suffix}" if separator and stem else ""
    counter = 2
    while f"{base} ({counter}){extension}".casefold() in existing:
        counter += 1
    return f"{base} ({counter}){extension}"


@router.get("", response_model=list[ChatFileSummary])
async def list_chat_files(
    runtime_workspace_id: int,
    runtime_session_id: str | None = None,
    runtime_run_id: str | None = None,
    principal: Principal = Depends(get_principal),
    service: ChatFileIndexService = Depends(get_chat_file_service),
) -> list[ChatFileSummary]:
    try:
        records = await service.list_files(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            runtime_run_id=runtime_run_id,
        )
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc
    return [_summary(record) for record in records]


@router.post("", response_model=ChatFileSummary)
async def register_chat_file(
    payload: RegisterChatFileRequest,
    principal: Principal = Depends(get_principal),
    service: ChatFileIndexService = Depends(get_chat_file_service),
) -> ChatFileSummary:
    try:
        record = await service.register_file(
            principal=principal,
            command=RegisterChatFileCommand(
                runtime_workspace_id=payload.runtime_workspace_id,
                runtime_session_id=payload.runtime_session_id,
                runtime_run_id=payload.runtime_run_id,
                relative_path=payload.relative_path,
                mime_type=payload.mime_type,
                size_bytes=payload.size_bytes,
                source_type=payload.source_type,
            ),
        )
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc
    return _summary(record)


@router.post("/upload", include_in_schema=False)
async def retired_chat_attachment_upload() -> None:
    """Do not allow a compatibility route to put file bodies back in Backend."""

    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="use /chat-files/upload-intent and upload directly to Runtime",
    )


@router.post(
    "/upload-intent",
    response_model=ChatFileTransferIntent,
)
async def create_chat_upload_intent(
    payload: ChatUploadIntentRequest,
    principal: Principal = Depends(get_principal),
    service: ChatFileIndexService = Depends(get_chat_file_service),
    runtime_service: RuntimeChatService = Depends(
        get_chat_file_runtime_service
    ),
) -> ChatFileTransferIntent:
    max_bytes = settings.CHAT_FILE_MAX_UPLOAD_MB * 1024 * 1024
    if payload.size_bytes > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="file is too large",
        )
    try:
        filename = _safe_upload_name(payload.display_name)
        await service.assert_writable_session(
            principal=principal,
            runtime_workspace_id=payload.runtime_workspace_id,
            runtime_session_id=payload.runtime_session_id,
        )
        endpoint = await runtime_service.get_runtime_endpoint(
            principal=principal,
            runtime_workspace_id=payload.runtime_workspace_id,
        )
        records = await service.list_files(
            principal=principal,
            runtime_workspace_id=payload.runtime_workspace_id,
            runtime_session_id=payload.runtime_session_id,
        )
        filename = _available_upload_name(records, filename)
        record = await service.reserve_upload(
            principal=principal,
            command=RegisterChatFileCommand(
                runtime_workspace_id=payload.runtime_workspace_id,
                runtime_session_id=payload.runtime_session_id,
                runtime_run_id=None,
                relative_path=f"workspace/uploads/{filename}",
                mime_type=payload.mime_type,
                size_bytes=payload.size_bytes,
                source_type="upload",
            ),
        )
        now = int(datetime.now(timezone.utc).timestamp())
        ticket = RuntimeTicketIssuer(
            runtime_id=endpoint.runtime_id,
            secret=endpoint.api_key,
        ).file_transfer(
            operation="upload",
            user_id=principal.user_id,
            org_id=principal.org_id,
            session_id=payload.runtime_session_id,
            file_id=record.file_id,
            relative_path=record.relative_path,
            display_name=record.display_name,
            mime_type=record.mime_type,
            max_bytes=payload.size_bytes,
            issued_at=now,
        )
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc
    except RuntimeChatPreparationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return ChatFileTransferIntent(
        file=_summary(record),
        runtime_path=f"/runtime-files/{endpoint.runtime_id}/upload",
        file_transfer_ticket=ticket,
        expires_at=datetime.fromtimestamp(now + 300, tz=timezone.utc),
    )


@router.get("/{file_id}/download", response_model=ChatFileSummary)
async def download_chat_file_metadata(
    file_id: int,
    principal: Principal = Depends(get_principal),
    service: ChatFileIndexService = Depends(get_chat_file_service),
) -> ChatFileSummary:
    try:
        return _summary(await service.get_file(principal=principal, file_id=file_id))
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc


@router.post(
    "/{file_id}/download-intent",
    response_model=ChatDownloadIntent,
)
async def create_chat_download_intent(
    file_id: int,
    principal: Principal = Depends(get_principal),
    service: ChatFileIndexService = Depends(get_chat_file_service),
    runtime_service: RuntimeChatService = Depends(
        get_chat_file_runtime_service
    ),
) -> ChatDownloadIntent:
    try:
        record = await service.get_file(
            principal=principal,
            file_id=file_id,
        )
        endpoint = await runtime_service.get_runtime_endpoint(
            principal=principal,
            runtime_workspace_id=record.runtime_workspace_id,
        )
        if record.runtime_session_id is None:
            raise ChatFilePermissionError(
                "runtime session is unavailable"
            )
        now = int(datetime.now(timezone.utc).timestamp())
        ticket = RuntimeTicketIssuer(
            runtime_id=endpoint.runtime_id,
            secret=endpoint.api_key,
        ).file_transfer(
            operation="download",
            user_id=principal.user_id,
            org_id=principal.org_id,
            session_id=record.runtime_session_id,
            file_id=record.file_id,
            relative_path=record.relative_path,
            display_name=record.display_name,
            mime_type=record.mime_type,
            max_bytes=max(1, record.size_bytes),
            issued_at=now,
        )
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc
    except RuntimeChatPreparationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return ChatDownloadIntent(
        runtime_path=f"/runtime-files/{endpoint.runtime_id}/download",
        file_transfer_ticket=ticket,
        expires_at=datetime.fromtimestamp(now + 300, tz=timezone.utc),
        display_name=record.display_name,
        mime_type=record.mime_type,
    )


@router.post("/{file_id}/office-preview", response_model=OfficePreviewSession)
async def create_office_preview_session(
    file_id: int,
    principal: Principal = Depends(get_principal),
    service: ChatFileIndexService = Depends(get_chat_file_service),
) -> OfficePreviewSession:
    if not settings.ONLYOFFICE_PUBLIC_URL or not settings.office_preview_jwt_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Office preview is not configured")
    try:
        record = await service.get_file(principal=principal, file_id=file_id)
        office_file_type(record.display_name)
        file_token = create_office_file_token(
            principal=principal,
            file_id=file_id,
            secret=settings.office_preview_jwt_secret,
            ttl_seconds=settings.ONLYOFFICE_PREVIEW_TOKEN_TTL_SECONDS,
        )
        internal_api_base = (settings.ONLYOFFICE_INTERNAL_API_BASE or settings.PUBLIC_API_BASE).rstrip("/")
        content_url = f"{internal_api_base}/api/v1/user/chat-files/{file_id}/office-content?token={quote(file_token)}"
        config = build_office_config(
            record=record,
            content_url=content_url,
            secret=settings.office_preview_jwt_secret,
            user_id=principal.user_id,
            ttl_seconds=settings.ONLYOFFICE_PREVIEW_TOKEN_TTL_SECONDS,
        )
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc
    except OfficePreviewError as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)) from exc
    return OfficePreviewSession(
        document_server_url=settings.ONLYOFFICE_PUBLIC_URL.rstrip("/"),
        config=config,
    )


@router.get("/{file_id}/office-content", include_in_schema=False)
async def stream_office_preview_content(
    file_id: int,
    token: str,
    service: ChatFileIndexService = Depends(get_chat_file_service),
    runtime_service: RuntimeChatService = Depends(get_chat_file_runtime_service),
    protocol_adapter: HermesDashboardProtocolAdapter = Depends(
        get_chat_file_protocol_adapter
    ),
):
    try:
        principal, token_file_id = decode_office_file_token(token, secret=settings.office_preview_jwt_secret)
        if token_file_id != file_id:
            raise OfficePreviewError("invalid Office preview token")
    except OfficePreviewError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    if settings.RUNTIME_CHAT_MODE != "real":
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="runtime file download is unavailable")
    try:
        record = await service.get_file(principal=principal, file_id=file_id)
        office_file_type(record.display_name)
        endpoint = await runtime_service.get_runtime_endpoint(
            principal=principal,
            runtime_workspace_id=record.runtime_workspace_id,
        )
        iterator = protocol_adapter.stream_managed_artifact(
            endpoint,
            relative_path=record.relative_path,
        )
    except OfficePreviewError as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)) from exc
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc
    except (HermesProtocolError, RuntimeChatPreparationError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return StreamingResponse(
        iterator,
        media_type=record.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(record.display_name)}",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/{file_id}/content")
async def retired_chat_file_content(file_id: int) -> None:
    """OnlyOffice keeps its controlled stream; browsers use a download ticket."""

    del file_id
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="use /chat-files/{file_id}/download-intent",
    )


@router.patch("/{file_id}", response_model=ChatFileSummary)
async def rename_chat_file(
    file_id: int,
    payload: RenameChatFileRequest,
    principal: Principal = Depends(get_principal),
    service: ChatFileIndexService = Depends(get_chat_file_service),
    runtime_service: RuntimeChatService = Depends(get_chat_file_runtime_service),
    protocol_adapter: HermesDashboardProtocolAdapter = Depends(
        get_chat_file_protocol_adapter
    ),
) -> ChatFileSummary:
    try:
        record = await service.get_file(principal=principal, file_id=file_id)
        target_path = normalize_workspace_relative_path(payload.relative_path)
        if record.runtime_session_id is not None:
            target_path = await service.scoped_session_relative_path(
                principal=principal,
                runtime_workspace_id=record.runtime_workspace_id,
                runtime_session_id=record.runtime_session_id,
                relative_path=target_path,
            )
        if settings.RUNTIME_CHAT_MODE == "real":
            endpoint = await runtime_service.get_runtime_endpoint(
                principal=principal,
                runtime_workspace_id=record.runtime_workspace_id,
            )
            await protocol_adapter.rename_managed_artifact(
                endpoint,
                source_relative_path=record.relative_path,
                target_relative_path=target_path,
            )
        return _summary(
            await service.rename_file(
                principal=principal,
                file_id=file_id,
                relative_path=target_path,
            )
        )
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc
    except (HermesProtocolError, RuntimeChatPreparationError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.delete("/{file_id}", response_model=ChatFileSummary)
async def delete_chat_file(
    file_id: int,
    principal: Principal = Depends(get_principal),
    service: ChatFileIndexService = Depends(get_chat_file_service),
    runtime_service: RuntimeChatService = Depends(get_chat_file_runtime_service),
    protocol_adapter: HermesDashboardProtocolAdapter = Depends(
        get_chat_file_protocol_adapter
    ),
) -> ChatFileSummary:
    try:
        record = await service.get_file(principal=principal, file_id=file_id)
        if settings.RUNTIME_CHAT_MODE == "real":
            endpoint = await runtime_service.get_runtime_endpoint(
                principal=principal,
                runtime_workspace_id=record.runtime_workspace_id,
            )
            await protocol_adapter.delete_managed_artifact(
                endpoint,
                relative_path=record.relative_path,
            )
        return _summary(await service.delete_file(principal=principal, file_id=file_id))
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc
    except (HermesProtocolError, RuntimeChatPreparationError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
