from __future__ import annotations

from datetime import datetime, timezone
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ApplicationAgentRuntimeBinding, HermesRuntimeBinding
from app.services.runtime_access_nonce import (
    RuntimeAccessNonceStore,
    RuntimeAccessNonceUnavailable,
    shared_runtime_access_nonce_store,
)
from app.services.runtime_ticket_issuer import (
    RuntimeTicketError,
    RuntimeTicketIssuer,
)
from app.services.runtime_workspace import RuntimeApiKeyCodec


router = APIRouter(tags=["internal-runtime-edge-auth"])
_HANDLE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,126}[a-z0-9])?$")


def get_runtime_access_nonce_store() -> RuntimeAccessNonceStore:
    return shared_runtime_access_nonce_store()


def _access_token(protocol_header: str | None) -> str:
    protocols = [
        item.strip()
        for item in str(protocol_header or "").split(",")
        if item.strip()
    ]
    tokens = [
        item.removeprefix("runtime-access.")
        for item in protocols
        if item.startswith("runtime-access.")
    ]
    if "hermes-runtime-v1" not in protocols or len(tokens) != 1 or not tokens[0]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="RuntimeAccess is required",
        )
    return tokens[0]


def _bearer_token(authorization: str | None) -> str:
    scheme, _, token = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="FileTransferTicket is required",
        )
    return token


async def _runtime_binding(
    db: AsyncSession,
    runtime_handle: str,
):
    binding = await db.scalar(
        select(HermesRuntimeBinding).where(
            HermesRuntimeBinding.runtime_id == runtime_handle,
            HermesRuntimeBinding.status == "ready",
        )
    )
    if binding is None:
        binding = await db.scalar(
            select(ApplicationAgentRuntimeBinding).where(
                ApplicationAgentRuntimeBinding.runtime_id == runtime_handle,
                ApplicationAgentRuntimeBinding.status == "ready",
            )
        )
    return binding


@router.get("/v1/runtime-edge-auth/{runtime_handle}")
async def authorize_runtime_edge(
    runtime_handle: str,
    origin: str | None = Header(default=None),
    sec_websocket_protocol: str | None = Header(
        default=None,
        alias="Sec-WebSocket-Protocol",
    ),
    db: AsyncSession = Depends(get_db),
    nonce_store: RuntimeAccessNonceStore = Depends(
        get_runtime_access_nonce_store
    ),
) -> Response:
    if not origin or not _HANDLE.fullmatch(runtime_handle):
        raise HTTPException(status_code=401, detail="RuntimeAccess is invalid")
    binding = await _runtime_binding(db, runtime_handle)
    if binding is None or not binding.endpoint_host_header:
        raise HTTPException(status_code=404, detail="Runtime is unavailable")
    secret = RuntimeApiKeyCodec().decrypt(str(binding.api_key_encrypted))
    try:
        claims = RuntimeTicketIssuer(
            runtime_id=runtime_handle,
            secret=secret,
        ).verify_runtime_access(
            _access_token(sec_websocket_protocol),
            expected_origin=origin,
        )
    except RuntimeTicketError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="RuntimeAccess is invalid",
        ) from exc
    now = int(datetime.now(timezone.utc).timestamp())
    try:
        consumed = await nonce_store.consume(
            claims.connection_nonce,
            ttl_seconds=max(1, claims.expires_at - now),
        )
    except RuntimeAccessNonceUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RuntimeAccess validation is temporarily unavailable",
        ) from exc
    if not consumed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="RuntimeAccess has already been used",
        )
    return Response(
        status_code=204,
        headers={
            "X-Hermes-Runtime-Host": str(binding.endpoint_host_header),
            "X-Hermes-Runtime-Id": runtime_handle,
            "Cache-Control": "no-store",
        },
    )


@router.get("/v1/runtime-file-auth/{runtime_handle}")
async def authorize_runtime_file(
    runtime_handle: str,
    authorization: str | None = Header(default=None),
    x_hermes_file_operation: str | None = Header(
        default=None,
        alias="X-Hermes-File-Operation",
    ),
    db: AsyncSession = Depends(get_db),
) -> Response:
    operation = str(x_hermes_file_operation or "")
    if (
        not _HANDLE.fullmatch(runtime_handle)
        or operation not in {"upload", "download"}
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="FileTransferTicket is invalid",
        )
    binding = await _runtime_binding(db, runtime_handle)
    if binding is None or not binding.endpoint_host_header:
        raise HTTPException(status_code=404, detail="Runtime is unavailable")
    secret = RuntimeApiKeyCodec().decrypt(str(binding.api_key_encrypted))
    try:
        RuntimeTicketIssuer(
            runtime_id=runtime_handle,
            secret=secret,
        ).verify_file_transfer(
            _bearer_token(authorization),
            operation=operation,
        )
    except RuntimeTicketError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="FileTransferTicket is invalid",
        ) from exc
    return Response(
        status_code=204,
        headers={
            "X-Hermes-Runtime-Host": str(binding.endpoint_host_header),
            "X-Hermes-Runtime-Id": runtime_handle,
            "Cache-Control": "no-store",
        },
    )
