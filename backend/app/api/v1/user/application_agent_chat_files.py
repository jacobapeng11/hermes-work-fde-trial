from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.chat_files import get_chat_file_protocol_adapter
from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.database import get_db
from app.hermes_runtime.dashboard_protocol import HermesDashboardProtocolAdapter
from app.hermes_runtime.protocol import HermesProtocolError
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models.application_agent import (
    ApplicationAgent,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentStudioSession,
)
from app.models.im_gateway import ImInboundMessage, ImMessageAttachment
from app.schemas.runtime_chat import (
    ChatDownloadIntent,
    ChatFileSummary,
    ChatFileTransferIntent,
    RenameChatFileRequest,
)
from app.services.application_agent_studio_chat_files import (
    ApplicationAgentStudioChatFileService,
)
from app.services.chat_files import (
    ChatFileError,
    ChatFilePermissionError,
    ChatFileRecord,
)
from app.services.runtime_ticket_issuer import RuntimeTicketIssuer
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec


router = APIRouter(
    prefix="/application-agents/{agent_id}/chat-files",
    tags=["application-agent-chat-files"],
)


class ApplicationAgentUploadIntentRequest(BaseModel):
    runtime_session_id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    mime_type: str | None = Field(default=None, max_length=255)
    size_bytes: int = Field(gt=0, le=100 * 1024 * 1024)


async def _verify_agent_creator(
    agent_id: int,
    principal: Principal,
    db: AsyncSession,
) -> ApplicationAgent:
    agent = await db.scalar(
        select(ApplicationAgent).where(
            ApplicationAgent.id == agent_id,
            ApplicationAgent.org_id == principal.org_id,
            ApplicationAgent.created_by_user_id == principal.user_id,
            ApplicationAgent.lifecycle_status == "active",
        )
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="application_agent_not_found",
        )
    return agent


async def _verify_studio_session(
    agent_id: int,
    runtime_session_id: str,
    principal: Principal,
    db: AsyncSession,
) -> ApplicationAgentStudioSession:
    session = await db.scalar(
        select(ApplicationAgentStudioSession).where(
            ApplicationAgentStudioSession.application_agent_id == agent_id,
            ApplicationAgentStudioSession.org_id == principal.org_id,
            ApplicationAgentStudioSession.user_id == principal.user_id,
            ApplicationAgentStudioSession.runtime_session_id == runtime_session_id,
            ApplicationAgentStudioSession.status == "active",
        )
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="application_agent_studio_session_not_found",
        )
    return session


async def _get_runtime_endpoint(
    agent: ApplicationAgent,
    db: AsyncSession,
) -> RuntimeEndpoint:
    binding = await db.scalar(
        select(ApplicationAgentRuntimeBinding).where(
            ApplicationAgentRuntimeBinding.application_agent_id == agent.id,
            ApplicationAgentRuntimeBinding.org_id == agent.org_id,
            ApplicationAgentRuntimeBinding.status == "ready",
        )
    )
    if binding is None or not binding.endpoint_host_header:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="application_agent_runtime_not_ready",
        )
    return RuntimeEndpoint(
        runtime_workspace_id=int(agent.id),
        runtime_id=str(binding.runtime_id),
        endpoint_base_url=str(binding.endpoint_base_url or ""),
        endpoint_host_header=str(binding.endpoint_host_header),
        api_key=RuntimeApiKeyCodec().decrypt(str(binding.api_key_encrypted)),
        timeout_seconds=60.0,
    )


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
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("", response_model=list[ChatFileSummary])
async def list_application_agent_chat_files(
    agent_id: int,
    runtime_session_id: str | None = None,
    runtime_run_id: str | None = None,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> list[ChatFileSummary]:
    agent = await _verify_agent_creator(agent_id, principal, db)
    if runtime_session_id is not None:
        await _verify_studio_session(agent.id, runtime_session_id, principal, db)
    service = ApplicationAgentStudioChatFileService(db)
    try:
        records = await service.list_files(
            principal=principal,
            application_agent_id=agent.id,
            runtime_session_id=runtime_session_id,
            runtime_run_id=runtime_run_id,
        )
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc
    return [_summary(record) for record in records]


@router.post("/upload-intent", response_model=ChatFileTransferIntent)
async def create_application_agent_upload_intent(
    agent_id: int,
    payload: ApplicationAgentUploadIntentRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> ChatFileTransferIntent:
    agent = await _verify_agent_creator(agent_id, principal, db)
    await _verify_studio_session(agent.id, payload.runtime_session_id, principal, db)
    endpoint = await _get_runtime_endpoint(agent, db)
    max_bytes = settings.CHAT_FILE_MAX_UPLOAD_MB * 1024 * 1024
    if payload.size_bytes > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="file is too large",
        )

    service = ApplicationAgentStudioChatFileService(db)
    try:
        active_files = await service.list_files(
            principal=principal,
            application_agent_id=agent.id,
            runtime_session_id=payload.runtime_session_id,
        )
        existing = {record.display_name.casefold() for record in active_files}
        filename = _safe_upload_filename(payload.display_name)
        filename = _available_upload_name(existing, filename)
        record = await service.reserve_upload(
            principal=principal,
            application_agent_id=agent.id,
            runtime_session_id=payload.runtime_session_id,
            display_name=filename,
            mime_type=payload.mime_type,
            size_bytes=payload.size_bytes,
        )
        await db.commit()
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc

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
    return ChatFileTransferIntent(
        file=_summary(record),
        runtime_path=f"/runtime-files/{endpoint.runtime_id}/upload",
        file_transfer_ticket=ticket,
        expires_at=datetime.fromtimestamp(now + 300, tz=timezone.utc),
    )


@router.get("/{file_id}/download", response_model=ChatFileSummary)
async def get_application_agent_chat_file_metadata(
    agent_id: int,
    file_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> ChatFileSummary:
    agent = await _verify_agent_creator(agent_id, principal, db)
    service = ApplicationAgentStudioChatFileService(db)
    try:
        record = await service.get_file(
            principal=principal,
            application_agent_id=agent.id,
            file_id=file_id,
            include_pending=True,
        )
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc
    if record.status == "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="chat file is pending",
        )
    if record.status != "active":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="chat file not found",
        )
    return _summary(record)


@router.post("/{file_id}/download-intent", response_model=ChatDownloadIntent)
async def create_application_agent_download_intent(
    agent_id: int,
    file_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> ChatDownloadIntent:
    agent = await _verify_agent_creator(agent_id, principal, db)
    service = ApplicationAgentStudioChatFileService(db)
    try:
        record = await service.get_file(
            principal=principal,
            application_agent_id=agent.id,
            file_id=file_id,
        )
        endpoint = await _get_runtime_endpoint(agent, db)
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
    return ChatDownloadIntent(
        runtime_path=f"/runtime-files/{endpoint.runtime_id}/download",
        file_transfer_ticket=ticket,
        expires_at=datetime.fromtimestamp(now + 300, tz=timezone.utc),
        display_name=record.display_name,
        mime_type=record.mime_type,
    )


@router.post(
    "/wecom-attachments/{attachment_id}/download-intent",
    response_model=ChatDownloadIntent,
)
async def create_application_agent_wecom_attachment_download_intent(
    agent_id: int,
    attachment_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> ChatDownloadIntent:
    agent = await _verify_agent_creator(agent_id, principal, db)
    row = (
        await db.execute(
            select(
                ImMessageAttachment,
                ApplicationAgentRun.runtime_session_id,
            )
            .join(
                ImInboundMessage,
                ImInboundMessage.id
                == ImMessageAttachment.inbound_message_id,
            )
            .join(
                ApplicationAgentRun,
                ApplicationAgentRun.id
                == ImInboundMessage.application_agent_run_id,
            )
            .where(
                ImMessageAttachment.id == attachment_id,
                ImInboundMessage.application_agent_id == agent.id,
                ImInboundMessage.org_id == principal.org_id,
                ImMessageAttachment.staging_status == "ready",
                ApplicationAgentRun.org_id == principal.org_id,
                ApplicationAgentRun.application_agent_id == agent.id,
            )
            .limit(1)
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="application_agent_wecom_attachment_not_found",
        )
    attachment, runtime_session_id = row
    relative_path = str(attachment.runtime_relative_path or "")
    if not relative_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="application_agent_wecom_attachment_not_ready",
        )
    endpoint = await _get_runtime_endpoint(agent, db)
    now = int(datetime.now(timezone.utc).timestamp())
    ticket = RuntimeTicketIssuer(
        runtime_id=endpoint.runtime_id,
        secret=endpoint.api_key,
    ).file_transfer(
        operation="download",
        user_id=principal.user_id,
        org_id=principal.org_id,
        session_id=str(runtime_session_id),
        file_id=int(attachment.id),
        relative_path=relative_path,
        display_name=str(attachment.display_name or relative_path),
        mime_type=attachment.mime_type,
        max_bytes=max(1, int(attachment.size_bytes or 0)),
        issued_at=now,
    )
    return ChatDownloadIntent(
        runtime_path=f"/runtime-files/{endpoint.runtime_id}/download",
        file_transfer_ticket=ticket,
        expires_at=datetime.fromtimestamp(now + 300, tz=timezone.utc),
        display_name=str(attachment.display_name or relative_path),
        mime_type=attachment.mime_type,
    )


@router.patch("/{file_id}", response_model=ChatFileSummary)
async def rename_application_agent_chat_file(
    agent_id: int,
    file_id: int,
    payload: RenameChatFileRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
    protocol_adapter: HermesDashboardProtocolAdapter = Depends(get_chat_file_protocol_adapter),
) -> ChatFileSummary:
    agent = await _verify_agent_creator(agent_id, principal, db)
    service = ApplicationAgentStudioChatFileService(db)
    try:
        record = await service.get_file(
            principal=principal,
            application_agent_id=agent.id,
            file_id=file_id,
        )
        target_filename = _safe_upload_filename(payload.relative_path)
        target_parent = record.relative_path.rsplit("/", 1)[0]
        target_path = f"{target_parent}/{target_filename}"
        if settings.RUNTIME_CHAT_MODE == "real":
            endpoint = await _get_runtime_endpoint(agent, db)
            await protocol_adapter.rename_managed_artifact(
                endpoint,
                source_relative_path=record.relative_path,
                target_relative_path=target_path,
            )
        renamed = await service.rename_file(
            principal=principal,
            application_agent_id=agent.id,
            file_id=file_id,
            relative_path=target_path,
        )
        await db.commit()
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc
    except (HermesProtocolError,) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    return _summary(renamed)


@router.delete("/{file_id}", response_model=ChatFileSummary)
async def delete_application_agent_chat_file(
    agent_id: int,
    file_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
    protocol_adapter: HermesDashboardProtocolAdapter = Depends(get_chat_file_protocol_adapter),
) -> ChatFileSummary:
    agent = await _verify_agent_creator(agent_id, principal, db)
    service = ApplicationAgentStudioChatFileService(db)
    try:
        record = await service.get_file(
            principal=principal,
            application_agent_id=agent.id,
            file_id=file_id,
        )
        if settings.RUNTIME_CHAT_MODE == "real":
            endpoint = await _get_runtime_endpoint(agent, db)
            await protocol_adapter.delete_managed_artifact(
                endpoint,
                relative_path=record.relative_path,
            )
        deleted = await service.delete_file(
            principal=principal,
            application_agent_id=agent.id,
            file_id=file_id,
        )
        await db.commit()
    except (ChatFileError, ChatFilePermissionError) as exc:
        raise _http_error(exc) from exc
    except (HermesProtocolError,) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    return _summary(deleted)


def _safe_upload_filename(raw_name: str) -> str:
    name = str(raw_name).strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in {".", ".."} or "\x00" in name or len(name) > 255:
        raise ChatFileError("upload filename is invalid")
    return name


def _available_upload_name(existing: set[str], filename: str) -> str:
    if filename.casefold() not in existing:
        return filename
    stem, separator, suffix = filename.rpartition(".")
    base = stem if separator and stem else filename
    extension = f".{suffix}" if separator and stem else ""
    counter = 2
    while f"{base} ({counter}){extension}".casefold() in existing:
        counter += 1
    return f"{base} ({counter}){extension}"
