from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.database import get_db
from app.schemas.personal_knowledge import (
    PersonalKnowledgeBindingResponse,
    PersonalKnowledgeBindingUpdate,
    PersonalKnowledgeCreate,
    PersonalKnowledgeDetail,
    PersonalKnowledgeFileListResponse,
    PersonalKnowledgeFileRename,
    PersonalKnowledgeFileSummary,
    PersonalKnowledgeListResponse,
    PersonalKnowledgePermissionReplace,
    PersonalKnowledgePermissionResponse,
    PersonalKnowledgeUpdate,
)
from app.schemas.runtime_chat import OfficePreviewSession
from app.schemas.user_directory import PersonalKnowledgePermissionDirectoryNode
from app.services.chat_files import ChatFileRecord
from app.services.office_preview import (
    OfficePreviewError,
    build_office_config,
    create_personal_knowledge_office_token,
    decode_personal_knowledge_office_token,
    office_file_type,
)
from app.services.personal_knowledge import (
    PersonalKnowledgeError,
    PersonalKnowledgeService,
)
from app.services.personal_knowledge_files import (
    PersonalKnowledgeFileAccess,
    PersonalKnowledgeFileService,
)
from app.services.personal_knowledge_acl import PersonalKnowledgeAclError
from app.services.runtime_workspace import Principal


router = APIRouter(tags=["user-personal-knowledge"])


def get_personal_knowledge_service(
    db: AsyncSession = Depends(get_db),
) -> PersonalKnowledgeService:
    return PersonalKnowledgeService(db)


def get_personal_knowledge_file_service(
    db: AsyncSession = Depends(get_db),
) -> PersonalKnowledgeFileService:
    return PersonalKnowledgeFileService(db)


def _raise(exc: PersonalKnowledgeError | PersonalKnowledgeAclError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code},
    ) from exc


@router.get(
    "/personal-knowledge",
    response_model=PersonalKnowledgeListResponse,
)
async def list_personal_knowledge(
    response: Response,
    runtime_workspace_id: int | None = Query(default=None),
    runtime_agent_profile_id: int | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=200),
    ownership: str = Query(default="all", pattern="^(all|owned|shared)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeService = Depends(get_personal_knowledge_service),
) -> PersonalKnowledgeListResponse:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        items = await service.list_for_user(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=runtime_agent_profile_id,
            keyword=keyword,
            ownership=ownership,
        )
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)
    start = (page - 1) * page_size
    return PersonalKnowledgeListResponse(
        items=items[start : start + page_size],
        total=len(items),
    )


@router.post(
    "/personal-knowledge",
    response_model=PersonalKnowledgeDetail,
    status_code=201,
)
async def create_personal_knowledge(
    payload: PersonalKnowledgeCreate,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeService = Depends(get_personal_knowledge_service),
) -> PersonalKnowledgeDetail:
    try:
        return await service.create(principal=principal, command=payload)
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)


@router.get(
    "/personal-knowledge/{knowledge_base_id}",
    response_model=PersonalKnowledgeDetail,
)
async def get_personal_knowledge(
    knowledge_base_id: int,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeService = Depends(get_personal_knowledge_service),
) -> PersonalKnowledgeDetail:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await service.detail(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
        )
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)


@router.put(
    "/personal-knowledge/{knowledge_base_id}",
    response_model=PersonalKnowledgeDetail,
)
async def update_personal_knowledge(
    knowledge_base_id: int,
    payload: PersonalKnowledgeUpdate,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeService = Depends(get_personal_knowledge_service),
) -> PersonalKnowledgeDetail:
    try:
        return await service.update(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            command=payload,
        )
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)


@router.delete(
    "/personal-knowledge/{knowledge_base_id}",
    status_code=204,
    response_class=Response,
)
async def delete_personal_knowledge(
    knowledge_base_id: int,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeService = Depends(get_personal_knowledge_service),
) -> Response:
    try:
        await service.delete(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
        )
        return Response(status_code=204)
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)


@router.get(
    "/personal-knowledge/{knowledge_base_id}/permissions",
    response_model=PersonalKnowledgePermissionResponse,
)
async def get_personal_knowledge_permissions(
    knowledge_base_id: int,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeService = Depends(get_personal_knowledge_service),
) -> PersonalKnowledgePermissionResponse:
    try:
        return await service.permissions(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
        )
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)


@router.get(
    "/personal-knowledge/{knowledge_base_id}/permission-directory",
    response_model=list[PersonalKnowledgePermissionDirectoryNode],
)
async def get_personal_knowledge_permission_directory(
    knowledge_base_id: int,
    keyword: str = Query(default="", max_length=100),
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeService = Depends(get_personal_knowledge_service),
) -> list[PersonalKnowledgePermissionDirectoryNode]:
    try:
        return await service.permission_directory(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            keyword=keyword,
        )
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)


@router.put(
    "/personal-knowledge/{knowledge_base_id}/permissions",
    response_model=PersonalKnowledgePermissionResponse,
)
async def replace_personal_knowledge_permissions(
    knowledge_base_id: int,
    payload: PersonalKnowledgePermissionReplace,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeService = Depends(get_personal_knowledge_service),
) -> PersonalKnowledgePermissionResponse:
    try:
        return await service.replace_permissions(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            command=payload,
        )
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)


@router.get(
    "/personal-knowledge/{knowledge_base_id}/files",
    response_model=PersonalKnowledgeFileListResponse,
)
async def list_personal_knowledge_files(
    knowledge_base_id: int,
    response: Response,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeFileService = Depends(get_personal_knowledge_file_service),
) -> PersonalKnowledgeFileListResponse:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        items = await service.list_files(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
        )
        return PersonalKnowledgeFileListResponse(items=items, total=len(items))
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)


@router.post(
    "/personal-knowledge/{knowledge_base_id}/files",
    response_model=PersonalKnowledgeFileSummary,
    status_code=201,
)
async def upload_personal_knowledge_file(
    knowledge_base_id: int,
    file: UploadFile = File(...),
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeFileService = Depends(get_personal_knowledge_file_service),
) -> PersonalKnowledgeFileSummary:
    max_bytes = settings.PERSONAL_KNOWLEDGE_MAX_UPLOAD_MB * 1024 * 1024
    content = await file.read(max_bytes + 1)
    try:
        return await service.upload_file(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            filename=file.filename or "",
            content_type=file.content_type,
            content=content,
        )
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)


@router.put(
    "/personal-knowledge/{knowledge_base_id}/files/{file_id}",
    response_model=PersonalKnowledgeFileSummary,
)
async def rename_personal_knowledge_file(
    knowledge_base_id: int,
    file_id: int,
    payload: PersonalKnowledgeFileRename,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeFileService = Depends(get_personal_knowledge_file_service),
) -> PersonalKnowledgeFileSummary:
    try:
        return await service.rename_file(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            file_id=file_id,
            filename=payload.filename,
        )
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)


@router.delete(
    "/personal-knowledge/{knowledge_base_id}/files/{file_id}",
    status_code=204,
    response_class=Response,
)
async def delete_personal_knowledge_file(
    knowledge_base_id: int,
    file_id: int,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeFileService = Depends(get_personal_knowledge_file_service),
) -> Response:
    try:
        await service.delete_file(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            file_id=file_id,
        )
        return Response(status_code=204)
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)


def _content_disposition(filename: str, *, download: bool) -> str:
    encoded = quote(filename, safe="")
    fallback = filename.encode("ascii", "ignore").decode("ascii") or "document"
    fallback = fallback.replace('"', "_")
    mode = "attachment" if download else "inline"
    return f"{mode}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


async def _deliver_file(
    *,
    knowledge_base_id: int,
    file_id: int,
    download: bool,
    principal: Principal,
    service: PersonalKnowledgeFileService,
) -> StreamingResponse:
    try:
        access = await service.file_for_delivery(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            file_id=file_id,
            download=download,
        )
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)
    return await _stream_file(
        access=access,
        download=download,
        service=service,
    )


async def _stream_file(
    *,
    access: PersonalKnowledgeFileAccess,
    download: bool,
    service: PersonalKnowledgeFileService,
    cache_control: str | None = None,
) -> StreamingResponse:
    stream = service.stream_file_content(access=access, download=download)
    try:
        response = await stream.__aenter__()
    except PersonalKnowledgeError as exc:
        _raise(exc)

    async def chunks():
        try:
            async for chunk in response.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await stream.__aexit__(None, None, None)

    headers = {
        "Content-Disposition": _content_disposition(
            str(access.file.filename),
            download=download,
        ),
        "X-Content-Type-Options": "nosniff",
    }
    if cache_control is not None:
        headers["Cache-Control"] = cache_control

    return StreamingResponse(
        chunks(),
        media_type=access.file.mime_type or "application/octet-stream",
        headers=headers,
    )


@router.get("/personal-knowledge/{knowledge_base_id}/files/{file_id}/preview")
async def preview_personal_knowledge_file(
    knowledge_base_id: int,
    file_id: int,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeFileService = Depends(get_personal_knowledge_file_service),
) -> StreamingResponse:
    return await _deliver_file(
        knowledge_base_id=knowledge_base_id,
        file_id=file_id,
        download=False,
        principal=principal,
        service=service,
    )


@router.post(
    "/personal-knowledge/{knowledge_base_id}/files/{file_id}/office-preview",
    response_model=OfficePreviewSession,
)
async def create_personal_knowledge_office_preview(
    knowledge_base_id: int,
    file_id: int,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeFileService = Depends(get_personal_knowledge_file_service),
) -> OfficePreviewSession:
    if not settings.ONLYOFFICE_PUBLIC_URL or not settings.office_preview_jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Office preview is not configured",
        )
    try:
        access = await service.file_access_for_preview(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            file_id=file_id,
        )
        office_file_type(str(access.file.filename))
        file_token = create_personal_knowledge_office_token(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            file_id=file_id,
            secret=settings.office_preview_jwt_secret,
            ttl_seconds=settings.ONLYOFFICE_PREVIEW_TOKEN_TTL_SECONDS,
        )
        internal_api_base = (
            settings.ONLYOFFICE_INTERNAL_API_BASE or settings.PUBLIC_API_BASE
        ).rstrip("/")
        content_url = (
            f"{internal_api_base}/api/v1/user/personal-knowledge/{knowledge_base_id}"
            f"/files/{file_id}/office-content?token={quote(file_token)}"
        )
        record = ChatFileRecord(
            file_id=file_id,
            runtime_workspace_id=knowledge_base_id,
            runtime_session_id=None,
            runtime_run_id=None,
            relative_path=f"personal-knowledge/{knowledge_base_id}/{access.file.filename}",
            display_name=str(access.file.filename),
            mime_type=access.file.mime_type,
            size_bytes=int(access.file.size_bytes),
            source_type="personal_knowledge",
            created_at=access.file.updated_at,
        )
        config = build_office_config(
            record=record,
            content_url=content_url,
            secret=settings.office_preview_jwt_secret,
            user_id=principal.user_id,
            ttl_seconds=settings.ONLYOFFICE_PREVIEW_TOKEN_TTL_SECONDS,
            allow_download=access.role in {"owner", "download"},
        )
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)
    except OfficePreviewError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc
    return OfficePreviewSession(
        document_server_url=settings.ONLYOFFICE_PUBLIC_URL.rstrip("/"),
        config=config,
    )


@router.get(
    "/personal-knowledge/{knowledge_base_id}/files/{file_id}/office-content",
    include_in_schema=False,
)
async def stream_personal_knowledge_office_content(
    knowledge_base_id: int,
    file_id: int,
    token: str,
    service: PersonalKnowledgeFileService = Depends(get_personal_knowledge_file_service),
) -> StreamingResponse:
    try:
        principal, token_knowledge_base_id, token_file_id = (
            decode_personal_knowledge_office_token(
                token,
                secret=settings.office_preview_jwt_secret,
            )
        )
        if token_knowledge_base_id != knowledge_base_id or token_file_id != file_id:
            raise OfficePreviewError("invalid Office preview token")
    except OfficePreviewError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc
    try:
        access = await service.file_access_for_preview(
            principal=principal,
            knowledge_base_id=knowledge_base_id,
            file_id=file_id,
        )
        office_file_type(str(access.file.filename))
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)
    except OfficePreviewError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc

    return await _stream_file(
        access=access,
        download=False,
        service=service,
        cache_control="private, no-store",
    )


@router.get("/personal-knowledge/{knowledge_base_id}/files/{file_id}/download")
async def download_personal_knowledge_file(
    knowledge_base_id: int,
    file_id: int,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeFileService = Depends(get_personal_knowledge_file_service),
) -> StreamingResponse:
    return await _deliver_file(
        knowledge_base_id=knowledge_base_id,
        file_id=file_id,
        download=True,
        principal=principal,
        service=service,
    )


@router.put(
    "/runtime-workspace/{runtime_workspace_id}/personal-knowledge/{knowledge_base_id}",
    response_model=PersonalKnowledgeBindingResponse,
)
async def set_personal_knowledge_enabled(
    runtime_workspace_id: int,
    knowledge_base_id: int,
    payload: PersonalKnowledgeBindingUpdate,
    principal: Principal = Depends(get_principal),
    service: PersonalKnowledgeService = Depends(get_personal_knowledge_service),
) -> PersonalKnowledgeBindingResponse:
    try:
        return await service.set_runtime_enabled(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_agent_profile_id=payload.runtime_agent_profile_id,
            knowledge_base_id=knowledge_base_id,
            enabled=payload.enabled,
        )
    except (PersonalKnowledgeError, PersonalKnowledgeAclError) as exc:
        _raise(exc)
