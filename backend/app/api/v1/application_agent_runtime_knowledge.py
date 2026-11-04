from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException

from app.api.v1.runtime_knowledge import _bearer, runtime_knowledge_error_response
from app.config import settings
from app.database import get_db
from app.schemas.enterprise_knowledge import (
    RuntimeKnowledgeDocumentSearchRequest,
    RuntimeKnowledgeSqlContextRequest,
    RuntimeKnowledgeSqlQueryRequest,
    RuntimeKnowledgeStateResponse,
)
from app.services.knowledge_assertion import KnowledgeAssertionIssuer
from app.services.runtime_knowledge import (
    RuntimeKnowledgeError,
    RuntimeKnowledgeService,
    SqlAlchemyApplicationAgentRuntimeKnowledgeStore,
)
from app.services.runtime_prepared_knowledge import (
    PreparedKnowledgeStoreUnavailable,
    RuntimePreparedKnowledgeKey,
    shared_runtime_prepared_knowledge_service,
)
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession


router = APIRouter(
    prefix="/application-agent-runtime-knowledge/{application_agent_id}",
    tags=["application-agent-runtime-knowledge"],
)


def _service(
    db: AsyncSession,
    runtime_session_id: str,
    turn_id: str | None,
    tool_call_id: str | None,
    request_id: str | None = None,
) -> RuntimeKnowledgeService:
    return RuntimeKnowledgeService(
        SqlAlchemyApplicationAgentRuntimeKnowledgeStore(
            db,
            runtime_session_id=runtime_session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        ),
        issuer=KnowledgeAssertionIssuer(
            issuer=settings.KNOWLEDGE_ASSERTION_ISSUER,
            ttl_seconds=settings.KNOWLEDGE_ASSERTION_TTL_SECONDS,
        ),
        request_id=request_id,
    )


def _session_id(value: str | None) -> str:
    session_id = str(value or "").strip()
    if not session_id:
        raise HTTPException(
            status_code=401,
            detail={"code": "runtime_session_identity_required"},
        )
    return session_id


@router.get("/state", response_model=RuntimeKnowledgeStateResponse)
async def application_agent_runtime_knowledge_state(
    application_agent_id: int,
    authorization: str | None = Header(default=None),
    runtime_session_id: str | None = Header(
        default=None,
        alias="X-Hermes-Runtime-Session-Id",
    ),
    turn_id: str | None = Header(default=None, alias="X-Hermes-Turn-Id"),
    tool_call_id: str | None = Header(
        default=None,
        alias="X-Hermes-Tool-Call-Id",
    ),
    knowledge_request_id: str | None = Header(
        default=None,
        alias="X-Hermes-Knowledge-Request-Id",
    ),
    prepared_context: str | None = Header(
        default=None,
        alias="X-Hermes-Prepared-Context",
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        resolved_session_id = _session_id(runtime_session_id)
        runtime_token = _bearer(authorization)
        service = _service(
            db,
            resolved_session_id,
            turn_id,
            tool_call_id,
            knowledge_request_id,
        )
        if prepared_context != "1":
            return await service.state(
                application_agent_id,
                None,
                runtime_token,
            )
        key = RuntimePreparedKnowledgeKey.application_agent(
            application_agent_id=application_agent_id,
            runtime_session_id=resolved_session_id,
            runtime_token=runtime_token,
        )
        return await shared_runtime_prepared_knowledge_service().load_or_prepare(
            key=key,
            kind="enterprise",
            loader=lambda: service.state(
                application_agent_id,
                None,
                runtime_token,
            ),
            serializer=lambda state: state,
        )
    except RuntimeKnowledgeError as exc:
        return runtime_knowledge_error_response(exc)
    except PreparedKnowledgeStoreUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "prepared_context_store_unavailable"},
        ) from exc


async def _proxy(
    call_name: str,
    *,
    application_agent_id: int,
    authorization: str | None,
    runtime_session_id: str | None,
    turn_id: str | None,
    tool_call_id: str | None,
    knowledge_request_id: str | None,
    payload: Any,
    db: AsyncSession,
) -> dict[str, Any]:
    service = _service(
        db,
        _session_id(runtime_session_id),
        turn_id,
        tool_call_id,
        knowledge_request_id,
    )
    try:
        call = getattr(service, call_name)
        return await call(
            application_agent_id,
            None,
            _bearer(authorization),
            payload.model_dump(),
        )
    except RuntimeKnowledgeError as exc:
        return runtime_knowledge_error_response(exc)


@router.post("/sql/context")
async def application_agent_runtime_knowledge_sql_context(
    application_agent_id: int,
    payload: RuntimeKnowledgeSqlContextRequest,
    authorization: str | None = Header(default=None),
    runtime_session_id: str | None = Header(
        default=None,
        alias="X-Hermes-Runtime-Session-Id",
    ),
    turn_id: str | None = Header(default=None, alias="X-Hermes-Turn-Id"),
    tool_call_id: str | None = Header(
        default=None,
        alias="X-Hermes-Tool-Call-Id",
    ),
    knowledge_request_id: str | None = Header(
        default=None,
        alias="X-Hermes-Knowledge-Request-Id",
    ),
    db: AsyncSession = Depends(get_db),
):
    return await _proxy(
        "sql_context",
        application_agent_id=application_agent_id,
        authorization=authorization,
        runtime_session_id=runtime_session_id,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        knowledge_request_id=knowledge_request_id,
        payload=payload,
        db=db,
    )


@router.post("/sql/query")
async def application_agent_runtime_knowledge_sql_query(
    application_agent_id: int,
    payload: RuntimeKnowledgeSqlQueryRequest,
    authorization: str | None = Header(default=None),
    runtime_session_id: str | None = Header(
        default=None,
        alias="X-Hermes-Runtime-Session-Id",
    ),
    turn_id: str | None = Header(default=None, alias="X-Hermes-Turn-Id"),
    tool_call_id: str | None = Header(
        default=None,
        alias="X-Hermes-Tool-Call-Id",
    ),
    knowledge_request_id: str | None = Header(
        default=None,
        alias="X-Hermes-Knowledge-Request-Id",
    ),
    db: AsyncSession = Depends(get_db),
):
    return await _proxy(
        "sql_query",
        application_agent_id=application_agent_id,
        authorization=authorization,
        runtime_session_id=runtime_session_id,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        knowledge_request_id=knowledge_request_id,
        payload=payload,
        db=db,
    )


@router.post("/documents/search")
async def application_agent_runtime_knowledge_document_search(
    application_agent_id: int,
    payload: RuntimeKnowledgeDocumentSearchRequest,
    authorization: str | None = Header(default=None),
    runtime_session_id: str | None = Header(
        default=None,
        alias="X-Hermes-Runtime-Session-Id",
    ),
    turn_id: str | None = Header(default=None, alias="X-Hermes-Turn-Id"),
    tool_call_id: str | None = Header(
        default=None,
        alias="X-Hermes-Tool-Call-Id",
    ),
    knowledge_request_id: str | None = Header(
        default=None,
        alias="X-Hermes-Knowledge-Request-Id",
    ),
    db: AsyncSession = Depends(get_db),
):
    return await _proxy(
        "document_search",
        application_agent_id=application_agent_id,
        authorization=authorization,
        runtime_session_id=runtime_session_id,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        knowledge_request_id=knowledge_request_id,
        payload=payload,
        db=db,
    )
