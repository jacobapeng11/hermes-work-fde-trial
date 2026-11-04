from __future__ import annotations

import base64
import binascii
from collections.abc import AsyncIterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.application_agents import (
    application_agent_memory_service,
    get_application_agent_authorization_code_service,
)
from app.config import settings
from app.database import get_db
from app.hermes_runtime.dashboard_protocol import (
    HermesDashboardProtocolAdapter,
)
from app.hermes_runtime.protocol import HermesProtocolError
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.schemas.application_agent_invocation import (
    ApplicationAgentResponse,
    ApplicationAgentResponseIdentity,
    ApplicationAgentResponseRequest,
    ApplicationAgentFileSummary,
)
from app.schemas.application_agent_authorization import (
    ApplicationAgentTokenIdentity,
    ApplicationAgentTokenRequest,
    ApplicationAgentTokenResponse,
    ApplicationAgentTrustedUserTokenRequest,
)
from app.services.application_agent_authorization import (
    ApplicationAgentAuthorizationError,
    decode_agent_token,
)
from app.services.application_agent_invocation import (
    ApplicationAgentInvocationError,
    InMemoryApplicationAgentInvocationService,
    SqlAlchemyApplicationAgentInvocationService,
)
from app.services.application_agent_conversation_files import (
    ApplicationAgentConversationFileError,
    ApplicationAgentConversationFileService,
    application_agent_file_download_url,
)
from app.services.file_url_signer import verify_file_download_signature
from app.services.runtime_prepared_knowledge import (
    shared_runtime_prepared_knowledge_service,
)
from app.services.runtime_workspace import Principal
from app.services.runtime_workspace import RuntimeApiKeyCodec


router = APIRouter(
    prefix="/application-agents",
    tags=["openapi-application-agents"],
)
application_agent_invocation_memory_service = (
    InMemoryApplicationAgentInvocationService(application_agent_memory_service)
)


async def get_application_agent_invocation_service(
    db: AsyncSession = Depends(get_db),
):
    if settings.RUNTIME_CHAT_MODE == "real":
        return SqlAlchemyApplicationAgentInvocationService(
            db,
            prepared_knowledge_service=(
                shared_runtime_prepared_knowledge_service()
            ),
        )
    return application_agent_invocation_memory_service


def _client_credentials(authorization: str | None) -> tuple[str, str]:
    scheme, _, encoded = (authorization or "").partition(" ")
    if scheme.lower() != "basic" or not encoded:
        raise HTTPException(
            status_code=401,
            detail="application_client_credentials_required",
            headers={"WWW-Authenticate": "Basic"},
        )
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        client_id, separator, client_secret = decoded.partition(":")
    except (binascii.Error, UnicodeDecodeError):
        client_id, separator, client_secret = "", "", ""
    if not separator or not client_id or not client_secret:
        raise HTTPException(
            status_code=401,
            detail="application_client_credentials_invalid",
            headers={"WWW-Authenticate": "Basic"},
        )
    return client_id, client_secret


def _delegated_principal(
    token: str | None,
    *,
    client_id: str,
    agent_slug: str,
) -> Principal | None:
    if not token:
        return None
    try:
        return decode_agent_token(
            token,
            client_id=client_id,
            agent_slug=agent_slug,
        )
    except ApplicationAgentAuthorizationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc


@router.post("/token", response_model=ApplicationAgentTokenResponse)
async def exchange_application_agent_authorization_code(
    payload: ApplicationAgentTokenRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    service=Depends(get_application_agent_authorization_code_service),
) -> ApplicationAgentTokenResponse:
    client_id, client_secret = _client_credentials(authorization)
    try:
        result = await service.exchange(
            code=payload.code,
            client_id=client_id,
            client_secret=client_secret,
        )
    except ApplicationAgentAuthorizationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return ApplicationAgentTokenResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        identity=ApplicationAgentTokenIdentity(
            user_code=result.user_code,
        ),
    )


@router.post(
    "/sqlbot2/trusted-user-token",
    response_model=ApplicationAgentTokenResponse,
)
async def issue_sqlbot2_trusted_user_token(
    payload: ApplicationAgentTrustedUserTokenRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    service=Depends(get_application_agent_authorization_code_service),
) -> ApplicationAgentTokenResponse:
    client_id, client_secret = _client_credentials(authorization)
    try:
        result = await service.issue_sqlbot2_trusted_user_token(
            user_code=payload.user_code,
            client_id=client_id,
            client_secret=client_secret,
        )
    except ApplicationAgentAuthorizationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return ApplicationAgentTokenResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        identity=ApplicationAgentTokenIdentity(
            user_code=result.user_code,
        ),
    )


@router.post(
    "/{agent_slug}/trusted-user-token",
    response_model=ApplicationAgentTokenResponse,
)
async def issue_trusted_user_token(
    agent_slug: str,
    payload: ApplicationAgentTrustedUserTokenRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    service=Depends(get_application_agent_authorization_code_service),
) -> ApplicationAgentTokenResponse:
    client_id, client_secret = _client_credentials(authorization)
    try:
        result = await service.issue_trusted_user_token(
            agent_slug=agent_slug,
            user_code=payload.user_code,
            client_id=client_id,
            client_secret=client_secret,
        )
    except ApplicationAgentAuthorizationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return ApplicationAgentTokenResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        identity=ApplicationAgentTokenIdentity(
            user_code=result.user_code,
        ),
    )


@router.post("/{slug}/responses", response_model=ApplicationAgentResponse)
async def create_application_agent_response(
    slug: str,
    payload: ApplicationAgentResponseRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    delegated_user_token: str | None = Header(
        default=None,
        alias="X-Hermes-Agent-Token",
    ),
    service=Depends(get_application_agent_invocation_service),
) -> ApplicationAgentResponse:
    client_id, client_secret = _client_credentials(authorization)
    principal = _delegated_principal(
        delegated_user_token,
        client_id=client_id,
        agent_slug=slug,
    )
    try:
        result = await service.invoke(
            slug=slug,
            client_id=client_id,
            client_secret=client_secret,
            delegated_principal=principal,
            payload=payload,
        )
    except ApplicationAgentInvocationError as exc:
        headers = (
            {"Retry-After": "1"}
            if exc.code
            in {
                "application_agent_capacity_busy",
                "application_agent_runtime_draining",
            }
            else None
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
            headers=headers,
        ) from exc
    return ApplicationAgentResponse(
        response_id=result.response_id,
        request_id=result.request_id,
        application_agent=result.agent_slug,
        conversation_key=result.conversation_key,
        runtime_session_id=result.runtime_session_id,
        identity=ApplicationAgentResponseIdentity(
            type=result.subject.subject_type,
            key=result.subject.public_key,
        ),
        output_text=result.output_text,
        status="completed",
        produced_files=result.produced_files,
    )


@router.get(
    "/{slug}/conversations/{conversation_key}/files",
    response_model=list[ApplicationAgentFileSummary],
)
async def list_application_agent_conversation_files(
    slug: str,
    conversation_key: str,
    external_user_id: str | None = None,
    runtime_run_id: str | None = None,
    authorization: str | None = Header(default=None, alias="Authorization"),
    delegated_user_token: str | None = Header(
        default=None,
        alias="X-Hermes-Agent-Token",
    ),
    db: AsyncSession = Depends(get_db),
) -> list[ApplicationAgentFileSummary]:
    client_id, client_secret = _client_credentials(authorization)
    principal = _delegated_principal(
        delegated_user_token,
        client_id=client_id,
        agent_slug=slug,
    )
    service = ApplicationAgentConversationFileService(db)
    try:
        records = await service.list_files(
            slug=slug,
            client_id=client_id,
            client_secret=client_secret,
            delegated_principal=principal,
            conversation_key=conversation_key,
            external_user_id=external_user_id,
            runtime_run_id=runtime_run_id,
        )
    except ApplicationAgentConversationFileError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc
    return [
        ApplicationAgentFileSummary(
            file_id=record.file_id,
            display_name=record.display_name,
            mime_type=record.mime_type,
            size_bytes=record.size_bytes,
            source_type=record.source_type,
            runtime_run_id=record.runtime_run_id,
            created_at=record.created_at,
            download_url=application_agent_file_download_url(
                slug=slug,
                file_id=record.file_id,
            ),
        )
        for record in records
    ]


@router.get("/{slug}/files/{file_id}/content")
async def download_application_agent_conversation_file(
    slug: str,
    file_id: int,
    expires: int,
    signature: str,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    if not verify_file_download_signature(
        slug=slug,
        file_id=file_id,
        expires=expires,
        signature=signature,
        secret=settings.FILE_DOWNLOAD_SIGNING_SECRET,
    ):
        raise HTTPException(
            status_code=403,
            detail="file_download_url_invalid",
        )
    service = ApplicationAgentConversationFileService(db)
    try:
        download = await service.file_for_download(
            slug=slug,
            file_id=file_id,
        )
    except ApplicationAgentConversationFileError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.code,
        ) from exc

    endpoint = RuntimeEndpoint(
        runtime_workspace_id=int(download.agent.id),
        runtime_id=str(download.binding.runtime_id),
        endpoint_base_url=str(download.binding.endpoint_base_url),
        endpoint_host_header=str(download.binding.endpoint_host_header),
        api_key=RuntimeApiKeyCodec().decrypt(
            str(download.binding.api_key_encrypted)
        ),
        timeout_seconds=60.0,
        coordination_scope_key=(
            f"application:{download.binding.runtime_id}:{download.agent.id}"
        ),
    )
    source = HermesDashboardProtocolAdapter().stream_managed_artifact(
        endpoint,
        relative_path=download.record.relative_path,
    )
    try:
        first_chunk = await source.__anext__()
    except StopAsyncIteration:
        first_chunk = None
    except HermesProtocolError as exc:
        await source.aclose()
        raise HTTPException(
            status_code=502,
            detail="application_agent_runtime_failed",
        ) from exc
    except Exception:
        await source.aclose()
        raise

    async def content() -> AsyncIterator[bytes]:
        try:
            if first_chunk is not None:
                yield first_chunk
            async for chunk in source:
                if chunk:
                    yield chunk
        finally:
            await source.aclose()

    media_type = download.record.mime_type or "application/octet-stream"
    encoded_name = quote(download.record.display_name, safe="")
    ascii_name = (
        download.record.display_name.encode("ascii", "ignore").decode("ascii")
        or "document"
    ).replace('"', "_")
    return StreamingResponse(
        content(),
        media_type=media_type,
        headers={
            "Content-Length": str(download.record.size_bytes),
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{encoded_name}"
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
