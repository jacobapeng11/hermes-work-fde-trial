from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.runtime_personal_knowledge import _bearer
from app.database import get_db
from app.schemas.personal_knowledge import (
    RuntimePersonalKnowledgeHybridSearchResponse,
    RuntimePersonalKnowledgeSearchRequest,
    RuntimePersonalKnowledgeSearchResponse,
    RuntimePersonalKnowledgeState,
    RuntimePersonalKnowledgeWikiPage,
    RuntimePersonalKnowledgeWikiReadPageRequest,
    RuntimePersonalKnowledgeWikiSearchResponse,
)
from app.services.runtime_personal_knowledge import (
    RuntimePersonalKnowledgeError,
    RuntimePersonalKnowledgeService,
    SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore,
)
from app.services.runtime_prepared_knowledge import (
    PreparedKnowledgeStoreUnavailable,
    RuntimePreparedKnowledgeKey,
    shared_runtime_prepared_knowledge_service,
)
from app.services.weknora_rerank_settings import WeKnoraRerankSettingService


router = APIRouter(
    prefix=(
        "/application-agent-runtime-personal-knowledge/"
        "{application_agent_id}"
    ),
    tags=["application-agent-runtime-personal-knowledge"],
)


def _session_id(value: str | None) -> str:
    session_id = str(value or "").strip()
    if not session_id:
        raise HTTPException(
            status_code=401,
            detail={"code": "runtime_session_identity_required"},
        )
    return session_id


def _service(
    db: AsyncSession,
    *,
    runtime_session_id: str,
    turn_id: str | None,
    tool_call_id: str | None,
) -> RuntimePersonalKnowledgeService:
    rerank_settings = WeKnoraRerankSettingService(db)
    return RuntimePersonalKnowledgeService(
        SqlAlchemyApplicationAgentRuntimePersonalKnowledgeStore(
            db,
            runtime_session_id=runtime_session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        ),
        weknora_rerank_enabled=rerank_settings.is_rerank_enabled,
    )


def _raise(exc: RuntimePersonalKnowledgeError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code},
    ) from exc


@router.get("/state", response_model=RuntimePersonalKnowledgeState)
async def application_agent_runtime_personal_knowledge_state(
    application_agent_id: int,
    authorization: str | None = Header(default=None),
    runtime_session_id: str | None = Header(
        default=None,
        alias="X-Hermes-Runtime-Session-Id",
    ),
    turn_id: str | None = Header(
        default=None,
        alias="X-Hermes-Turn-Id",
    ),
    tool_call_id: str | None = Header(
        default=None,
        alias="X-Hermes-Tool-Call-Id",
    ),
    prepared_context: str | None = Header(
        default=None,
        alias="X-Hermes-Prepared-Context",
    ),
    db: AsyncSession = Depends(get_db),
) -> RuntimePersonalKnowledgeState:
    try:
        resolved_session_id = _session_id(runtime_session_id)
        runtime_token = _bearer(authorization)
        service = _service(
            db,
            runtime_session_id=resolved_session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
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
        state = await shared_runtime_prepared_knowledge_service().load_or_prepare(
            key=key,
            kind="personal",
            loader=lambda: service.state(
                application_agent_id,
                None,
                runtime_token,
            ),
            serializer=lambda value: value.model_dump(mode="json"),
        )
        if isinstance(state, RuntimePersonalKnowledgeState):
            return state
        return RuntimePersonalKnowledgeState.model_validate(state)
    except RuntimePersonalKnowledgeError as exc:
        _raise(exc)
    except PreparedKnowledgeStoreUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "prepared_context_store_unavailable"},
        ) from exc


@router.post(
    "/search",
    response_model=RuntimePersonalKnowledgeSearchResponse,
)
async def application_agent_runtime_personal_knowledge_search(
    application_agent_id: int,
    payload: RuntimePersonalKnowledgeSearchRequest,
    authorization: str | None = Header(default=None),
    runtime_session_id: str | None = Header(
        default=None,
        alias="X-Hermes-Runtime-Session-Id",
    ),
    turn_id: str | None = Header(
        default=None,
        alias="X-Hermes-Turn-Id",
    ),
    tool_call_id: str | None = Header(
        default=None,
        alias="X-Hermes-Tool-Call-Id",
    ),
    db: AsyncSession = Depends(get_db),
) -> RuntimePersonalKnowledgeSearchResponse:
    try:
        return await _service(
            db,
            runtime_session_id=_session_id(runtime_session_id),
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        ).search(
            application_agent_id,
            None,
            _bearer(authorization),
            payload,
        )
    except RuntimePersonalKnowledgeError as exc:
        _raise(exc)


@router.post(
    "/hybrid-search",
    response_model=RuntimePersonalKnowledgeHybridSearchResponse,
)
async def application_agent_runtime_personal_knowledge_hybrid_search(
    application_agent_id: int,
    payload: RuntimePersonalKnowledgeSearchRequest,
    authorization: str | None = Header(default=None),
    runtime_session_id: str | None = Header(
        default=None,
        alias="X-Hermes-Runtime-Session-Id",
    ),
    turn_id: str | None = Header(
        default=None,
        alias="X-Hermes-Turn-Id",
    ),
    tool_call_id: str | None = Header(
        default=None,
        alias="X-Hermes-Tool-Call-Id",
    ),
    db: AsyncSession = Depends(get_db),
) -> RuntimePersonalKnowledgeHybridSearchResponse:
    try:
        return await _service(
            db,
            runtime_session_id=_session_id(runtime_session_id),
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        ).hybrid_search(
            application_agent_id,
            None,
            _bearer(authorization),
            payload,
        )
    except RuntimePersonalKnowledgeError as exc:
        _raise(exc)


@router.post(
    "/wiki-search",
    response_model=RuntimePersonalKnowledgeWikiSearchResponse,
)
async def application_agent_runtime_personal_knowledge_wiki_search(
    application_agent_id: int,
    payload: RuntimePersonalKnowledgeSearchRequest,
    authorization: str | None = Header(default=None),
    runtime_session_id: str | None = Header(
        default=None,
        alias="X-Hermes-Runtime-Session-Id",
    ),
    turn_id: str | None = Header(
        default=None,
        alias="X-Hermes-Turn-Id",
    ),
    tool_call_id: str | None = Header(
        default=None,
        alias="X-Hermes-Tool-Call-Id",
    ),
    db: AsyncSession = Depends(get_db),
) -> RuntimePersonalKnowledgeWikiSearchResponse:
    try:
        return await _service(
            db,
            runtime_session_id=_session_id(runtime_session_id),
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        ).wiki_search(
            application_agent_id,
            None,
            _bearer(authorization),
            payload,
        )
    except RuntimePersonalKnowledgeError as exc:
        _raise(exc)


@router.post(
    "/wiki-read-page",
    response_model=RuntimePersonalKnowledgeWikiPage,
)
async def application_agent_runtime_personal_knowledge_wiki_read_page(
    application_agent_id: int,
    payload: RuntimePersonalKnowledgeWikiReadPageRequest,
    authorization: str | None = Header(default=None),
    runtime_session_id: str | None = Header(
        default=None,
        alias="X-Hermes-Runtime-Session-Id",
    ),
    turn_id: str | None = Header(
        default=None,
        alias="X-Hermes-Turn-Id",
    ),
    tool_call_id: str | None = Header(
        default=None,
        alias="X-Hermes-Tool-Call-Id",
    ),
    db: AsyncSession = Depends(get_db),
) -> RuntimePersonalKnowledgeWikiPage:
    try:
        return await _service(
            db,
            runtime_session_id=_session_id(runtime_session_id),
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        ).wiki_read_page(
            application_agent_id,
            None,
            _bearer(authorization),
            payload,
        )
    except RuntimePersonalKnowledgeError as exc:
        _raise(exc)
