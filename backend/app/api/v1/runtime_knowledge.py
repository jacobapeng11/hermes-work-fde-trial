from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.enterprise_knowledge import (
    RuntimeKnowledgeDocumentSearchRequest,
    RuntimeKnowledgeSqlContextRequest,
    RuntimeKnowledgeSqlQueryBatchRequest,
    RuntimeKnowledgeSqlQueryRequest,
    RuntimeKnowledgeStateResponse,
)
from app.services.knowledge_assertion import KnowledgeAssertionIssuer
from app.services.runtime_knowledge import (
    RuntimeKnowledgeError,
    RuntimeKnowledgeService,
    SqlAlchemyRuntimeKnowledgeStore,
)
from app.services.runtime_prepared_knowledge import (
    PreparedKnowledgeStoreUnavailable,
    RuntimePreparedKnowledgeKey,
    shared_runtime_prepared_knowledge_service,
)


router = APIRouter(prefix="/runtime-knowledge/{runtime_workspace_id}", tags=["runtime-knowledge"])


def get_runtime_knowledge_service(db: AsyncSession = Depends(get_db)) -> RuntimeKnowledgeService:
    return RuntimeKnowledgeService(
        SqlAlchemyRuntimeKnowledgeStore(db),
        issuer=KnowledgeAssertionIssuer(
            issuer=settings.KNOWLEDGE_ASSERTION_ISSUER,
            ttl_seconds=settings.KNOWLEDGE_ASSERTION_TTL_SECONDS,
        ),
    )


def _bearer(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing runtime token")
    return token


def runtime_knowledge_error_response(exc: RuntimeKnowledgeError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.as_error()},
    )


@router.get("/state", response_model=RuntimeKnowledgeStateResponse)
async def runtime_knowledge_state(
    runtime_workspace_id: int,
    runtime_agent_profile_id: int | None = Query(default=None, ge=1),
    authorization: str | None = Header(default=None),
    runtime_session_id: str | None = Header(
        default=None,
        alias="X-Hermes-Runtime-Session-Id",
    ),
    prepared_context: str | None = Header(
        default=None,
        alias="X-Hermes-Prepared-Context",
    ),
    service: RuntimeKnowledgeService = Depends(get_runtime_knowledge_service),
):
    try:
        runtime_token = _bearer(authorization)
        if (
            prepared_context != "1"
            or not str(runtime_session_id or "").strip()
        ):
            return await service.state(
                runtime_workspace_id,
                runtime_agent_profile_id,
                runtime_token,
            )
        key = RuntimePreparedKnowledgeKey.workspace(
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=str(runtime_session_id),
            runtime_agent_profile_id=runtime_agent_profile_id,
            runtime_token=runtime_token,
        )
        return await shared_runtime_prepared_knowledge_service().load_or_prepare(
            key=key,
            kind="enterprise",
            loader=lambda: service.state(
                runtime_workspace_id,
                runtime_agent_profile_id,
                runtime_token,
            ),
            serializer=lambda state: state,
        )
    except RuntimeKnowledgeError as exc:
        return runtime_knowledge_error_response(exc)
    except PreparedKnowledgeStoreUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "prepared_context_store_unavailable"},
        ) from exc


async def _proxy(
    call,
    runtime_workspace_id: int,
    runtime_agent_profile_id: int | None,
    authorization: str | None,
    payload,
) -> dict[str, Any]:
    try:
        return await call(
            runtime_workspace_id,
            runtime_agent_profile_id,
            _bearer(authorization),
            payload.model_dump(),
        )
    except RuntimeKnowledgeError as exc:
        return runtime_knowledge_error_response(exc)


@router.post("/sql/context")
async def runtime_knowledge_sql_context(
    runtime_workspace_id: int,
    payload: RuntimeKnowledgeSqlContextRequest,
    runtime_agent_profile_id: int | None = Query(default=None, ge=1),
    authorization: str | None = Header(default=None),
    service: RuntimeKnowledgeService = Depends(get_runtime_knowledge_service),
):
    return await _proxy(
        service.sql_context,
        runtime_workspace_id,
        runtime_agent_profile_id,
        authorization,
        payload,
    )


@router.post("/sql/query")
async def runtime_knowledge_sql_query(
    runtime_workspace_id: int,
    payload: RuntimeKnowledgeSqlQueryRequest,
    runtime_agent_profile_id: int | None = Query(default=None, ge=1),
    authorization: str | None = Header(default=None),
    service: RuntimeKnowledgeService = Depends(get_runtime_knowledge_service),
):
    return await _proxy(
        service.sql_query,
        runtime_workspace_id,
        runtime_agent_profile_id,
        authorization,
        payload,
    )


@router.post("/sql/query-batch")
async def runtime_knowledge_sql_query_batch(
    runtime_workspace_id: int,
    payload: RuntimeKnowledgeSqlQueryBatchRequest,
    authorization: str | None = Header(default=None),
    service: RuntimeKnowledgeService = Depends(get_runtime_knowledge_service),
):
    """Execute multiple SQL queries in batch without requiring session context.

    This endpoint uses relaxed authentication that only validates the API key,
    making it suitable for service-to-service calls from scripts (e.g., batch_executor).
    """
    try:
        runtime_token = _bearer(authorization)
        return await service.sql_query_batch(
            runtime_workspace_id,
            runtime_token,
            payload.model_dump(),
        )
    except RuntimeKnowledgeError as exc:
        return runtime_knowledge_error_response(exc)


@router.post("/documents/search")
async def runtime_knowledge_document_search(
    runtime_workspace_id: int,
    payload: RuntimeKnowledgeDocumentSearchRequest,
    runtime_agent_profile_id: int | None = Query(default=None, ge=1),
    authorization: str | None = Header(default=None),
    service: RuntimeKnowledgeService = Depends(get_runtime_knowledge_service),
):
    return await _proxy(
        service.document_search,
        runtime_workspace_id,
        runtime_agent_profile_id,
        authorization,
        payload,
    )
