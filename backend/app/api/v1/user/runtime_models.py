from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.database import get_db
from app.schemas.runtime_chat import SessionModelSelectionRequest, SessionModelSelectionResponse
from app.schemas.runtime_workspace import (
    RuntimeModelSummary,
)
from app.services.runtime_models import (
    RuntimeModelNotVisibleError,
    RuntimeModelRecord,
    RuntimeSessionModelState,
    SessionModelSelectionIntent,
    RuntimeSessionModelSwitchNotAllowedError,
    RuntimeModelService,
    SqlAlchemyRuntimeModelStore,
)
from app.services.model_catalog_cache import (
    ModelCatalogCacheCircuitOpenError,
    ModelCatalogCacheFillThrottledError,
    RedisModelCatalogCache,
    shared_model_catalog_cache,
)
from app.services.runtime_workspace import Principal
from app.services.runtime_sessions import RuntimeSessionDeletedError
from app.services.runtime_commands import (
    RuntimeCommandClient,
    RuntimeCommandError,
    load_personal_runtime_command_target,
)

router = APIRouter(prefix="/runtime-workspace", tags=["user-runtime-models"])


class InMemoryRuntimeModelStore:
    def __init__(self) -> None:
        self.models = [
            RuntimeModelRecord(
                model_entry_id=1,
                model_key="gpt-4.1-mini",
                display_name="GPT-4.1 Mini",
                provider_id=1,
                provider_display_name="Platform OpenAI",
                context_length=128000,
                tags=["chat", "fast"],
                is_default=True,
            ),
            RuntimeModelRecord(
                model_entry_id=2,
                model_key="gpt-4.1",
                display_name="GPT-4.1",
                provider_id=1,
                provider_display_name="Platform OpenAI",
                context_length=128000,
                tags=["chat"],
                is_default=False,
            ),
        ]
        self.session_models: dict[tuple[int, str], int] = {}

    async def list_visible_models(self, *, org_id: int, user_id: int) -> list[RuntimeModelRecord]:
        return [
            RuntimeModelRecord(
                model_entry_id=model.model_entry_id,
                model_key=model.model_key,
                display_name=model.display_name,
                provider_id=model.provider_id,
                provider_display_name=model.provider_display_name,
                context_length=model.context_length,
                tags=model.tags,
                is_default=model.is_default,
            )
            for model in self.models
        ]

    async def get_session_model_state(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSessionModelState | None:
        selected = self.session_models.get((runtime_workspace_id, runtime_session_id))
        return RuntimeSessionModelState(
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            current_model_entry_id=selected,
            next_model_entry_id=selected,
            active_run_count=0,
        )

    async def set_next_session_model(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        model_entry_id: int,
    ) -> SessionModelSelectionIntent:
        self.session_models[(runtime_workspace_id, runtime_session_id)] = model_entry_id
        return SessionModelSelectionIntent(
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            model_entry_id=model_entry_id,
            applies_to="next_run",
            active_run_count=0,
        )


_runtime_model_store = InMemoryRuntimeModelStore()


def get_model_catalog_cache() -> RedisModelCatalogCache:
    return shared_model_catalog_cache()


async def get_runtime_model_service(
    db: AsyncSession = Depends(get_db),
    cache: RedisModelCatalogCache = Depends(get_model_catalog_cache),
) -> RuntimeModelService:
    if settings.RUNTIME_CHAT_MODE == "real":
        return RuntimeModelService(SqlAlchemyRuntimeModelStore(db, cache=cache))
    return RuntimeModelService(_runtime_model_store)


@router.get("/models", response_model=list[RuntimeModelSummary])
async def list_runtime_models(
    principal: Principal = Depends(get_principal),
    service: RuntimeModelService = Depends(get_runtime_model_service),
) -> list[RuntimeModelSummary]:
    try:
        return await service.list_models(principal)
    except (ModelCatalogCacheFillThrottledError, ModelCatalogCacheCircuitOpenError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.put(
    "/{runtime_workspace_id}/sessions/{runtime_session_id}/model",
    response_model=SessionModelSelectionResponse,
)
async def select_runtime_session_model(
    runtime_workspace_id: int,
    runtime_session_id: str,
    payload: SessionModelSelectionRequest,
    principal: Principal = Depends(get_principal),
    service: RuntimeModelService = Depends(get_runtime_model_service),
    db: AsyncSession = Depends(get_db),
) -> SessionModelSelectionResponse:
    try:
        intent = await service.select_session_model(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            model_entry_id=payload.model_entry_id,
        )
    except RuntimeModelNotVisibleError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="model entry is not visible") from exc
    except RuntimeSessionDeletedError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=exc.code) from exc
    except RuntimeSessionModelSwitchNotAllowedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    try:
        target = await load_personal_runtime_command_target(
            db,
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
        )
        client = RuntimeCommandClient()
        try:
            await client.mutate_session(
                target=target,
                operation="invalidate",
                session_id=runtime_session_id,
                profile_id="default",
            )
        finally:
            await client.close()
    except RuntimeCommandError:
        # The next SessionGrant carries the changed model identity, so a
        # failed eager invalidation cannot authorize stale execution.
        pass
    return SessionModelSelectionResponse(
        runtime_workspace_id=intent.runtime_workspace_id,
        runtime_session_id=intent.runtime_session_id,
        model_entry_id=intent.model_entry_id,
        applies_to=intent.applies_to,
        active_run_count=intent.active_run_count,
    )
