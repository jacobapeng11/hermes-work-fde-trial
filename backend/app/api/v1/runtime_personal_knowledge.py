from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.personal_knowledge import (
    RuntimePersonalKnowledgeSearchRequest,
    RuntimePersonalKnowledgeHybridSearchResponse,
    RuntimePersonalKnowledgeSearchResponse,
    RuntimePersonalKnowledgeState,
    RuntimePersonalKnowledgeWikiPage,
    RuntimePersonalKnowledgeWikiReadPageRequest,
    RuntimePersonalKnowledgeWikiSearchResponse,
)
from app.services.runtime_personal_knowledge import (
    RuntimePersonalKnowledgeError,
    RuntimePersonalKnowledgeService,
    SqlAlchemyRuntimePersonalKnowledgeStore,
)
from app.services.runtime_prepared_knowledge import (
    PreparedKnowledgeStoreUnavailable,
    RuntimePreparedKnowledgeKey,
    shared_runtime_prepared_knowledge_service,
)
from app.services.weknora_rerank_settings import WeKnoraRerankSettingService


router = APIRouter(
    prefix="/runtime-personal-knowledge/{runtime_workspace_id}",
    tags=["runtime-personal-knowledge"],
)


def get_runtime_personal_knowledge_service(
    db: AsyncSession = Depends(get_db),
) -> RuntimePersonalKnowledgeService:
    rerank_settings = WeKnoraRerankSettingService(db)
    return RuntimePersonalKnowledgeService(
        SqlAlchemyRuntimePersonalKnowledgeStore(db),
        weknora_rerank_enabled=rerank_settings.is_rerank_enabled,
    )


def _bearer(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail={"code": "missing_runtime_token"})
    return token


def _raise(exc: RuntimePersonalKnowledgeError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code},
    ) from exc


@router.get("/state", response_model=RuntimePersonalKnowledgeState)
async def runtime_personal_knowledge_state(
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
    service: RuntimePersonalKnowledgeService = Depends(
        get_runtime_personal_knowledge_service
    ),
) -> RuntimePersonalKnowledgeState:
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
        state = await shared_runtime_prepared_knowledge_service().load_or_prepare(
            key=key,
            kind="personal",
            loader=lambda: service.state(
                runtime_workspace_id,
                runtime_agent_profile_id,
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
async def runtime_personal_knowledge_search(
    runtime_workspace_id: int,
    payload: RuntimePersonalKnowledgeSearchRequest,
    runtime_agent_profile_id: int | None = Query(default=None, ge=1),
    authorization: str | None = Header(default=None),
    service: RuntimePersonalKnowledgeService = Depends(
        get_runtime_personal_knowledge_service
    ),
) -> RuntimePersonalKnowledgeSearchResponse:
    try:
        return await service.search(
            runtime_workspace_id,
            runtime_agent_profile_id,
            _bearer(authorization),
            payload,
        )
    except RuntimePersonalKnowledgeError as exc:
        _raise(exc)


@router.post(
    "/hybrid-search",
    response_model=RuntimePersonalKnowledgeHybridSearchResponse,
)
async def runtime_personal_knowledge_hybrid_search(
    runtime_workspace_id: int,
    payload: RuntimePersonalKnowledgeSearchRequest,
    runtime_agent_profile_id: int | None = Query(default=None, ge=1),
    authorization: str | None = Header(default=None),
    service: RuntimePersonalKnowledgeService = Depends(
        get_runtime_personal_knowledge_service
    ),
) -> RuntimePersonalKnowledgeHybridSearchResponse:
    try:
        return await service.hybrid_search(
            runtime_workspace_id,
            runtime_agent_profile_id,
            _bearer(authorization),
            payload,
        )
    except RuntimePersonalKnowledgeError as exc:
        _raise(exc)


@router.post(
    "/wiki-search",
    response_model=RuntimePersonalKnowledgeWikiSearchResponse,
)
async def runtime_personal_knowledge_wiki_search(
    runtime_workspace_id: int,
    payload: RuntimePersonalKnowledgeSearchRequest,
    runtime_agent_profile_id: int | None = Query(default=None, ge=1),
    authorization: str | None = Header(default=None),
    service: RuntimePersonalKnowledgeService = Depends(
        get_runtime_personal_knowledge_service
    ),
) -> RuntimePersonalKnowledgeWikiSearchResponse:
    try:
        return await service.wiki_search(
            runtime_workspace_id,
            runtime_agent_profile_id,
            _bearer(authorization),
            payload,
        )
    except RuntimePersonalKnowledgeError as exc:
        _raise(exc)


@router.post(
    "/wiki-read-page",
    response_model=RuntimePersonalKnowledgeWikiPage,
)
async def runtime_personal_knowledge_wiki_read_page(
    runtime_workspace_id: int,
    payload: RuntimePersonalKnowledgeWikiReadPageRequest,
    runtime_agent_profile_id: int | None = Query(default=None, ge=1),
    authorization: str | None = Header(default=None),
    service: RuntimePersonalKnowledgeService = Depends(
        get_runtime_personal_knowledge_service
    ),
) -> RuntimePersonalKnowledgeWikiPage:
    try:
        return await service.wiki_read_page(
            runtime_workspace_id,
            runtime_agent_profile_id,
            _bearer(authorization),
            payload,
        )
    except RuntimePersonalKnowledgeError as exc:
        _raise(exc)
