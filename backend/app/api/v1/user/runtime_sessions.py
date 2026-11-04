from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.api.v1.user.chat import get_chat_log_projector, get_runtime_chat_service
from app.api.v1.user.runtime_models import get_runtime_model_service
from app.config import settings
from app.database import get_db
from app.hermes_runtime.dashboard_protocol import HermesDashboardProtocolAdapter
from app.hermes_runtime.protocol import HermesProtocolError
from app.schemas.runtime_chat import (
    CreateRuntimeSessionRequest,
    RenameRuntimeSessionRequest,
    RuntimeSessionSummary,
)
from app.services.runtime_sessions import (
    CreateRuntimeSessionCommand,
    RuntimeSessionRecord,
    RuntimeSessionActiveRunError,
    RuntimeSessionContextBranchError,
    RuntimeSessionDeletedError,
    RuntimeSessionImmutableError,
    RuntimeSessionProfilePreparingError,
    RuntimeSessionService,
    RuntimeSessionTitleConflictError,
    SqlAlchemyRuntimeSessionStore,
    default_runtime_session_id,
    utc_now,
)
from app.services.chat_log import ChatLogProjector
from app.services.chat_log import ChatLogSessionNotFound
from app.services.runtime_workspace import Principal
from app.services.runtime_commands import (
    RuntimeCommandClient,
    RuntimeCommandError,
    load_personal_runtime_command_target,
)
from app.services.runtime_chat import RuntimeChatPreparationError, RuntimeChatService
from app.services.runtime_models import (
    RuntimeModelService,
    resolve_default_model_entry_id,
)
from app.services.model_catalog_cache import ModelCatalogCacheCircuitOpenError, ModelCatalogCacheFillThrottledError

router = APIRouter(prefix="/runtime-workspace", tags=["user-runtime-sessions"])


def _require_writable_session(session: RuntimeSessionRecord) -> None:
    if session.is_read_only:
        raise RuntimeSessionDeletedError(RuntimeSessionDeletedError.code)
    if session.is_immutable:
        raise RuntimeSessionImmutableError(
            RuntimeSessionImmutableError.code
        )


def _session_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RuntimeSessionImmutableError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.code,
        )
    if isinstance(exc, RuntimeSessionDeletedError):
        return HTTPException(status_code=status.HTTP_410_GONE, detail=exc.code)
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _profile_preparing_http_error(exc: RuntimeSessionProfilePreparingError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=exc.code,
        headers={"Retry-After": "3"},
    )


async def _import_context_branch_history(
    *,
    principal: Principal,
    runtime_workspace_id: int,
    created: RuntimeSessionRecord,
    chat_service: RuntimeChatService,
    chat_log_projector: ChatLogProjector,
) -> None:
    await chat_log_projector.project_owned_session(
        org_id=principal.org_id,
        user_id=principal.user_id,
        runtime_workspace_id=runtime_workspace_id,
        runtime_session_id=created.runtime_session_id,
    )
    history = await chat_log_projector.history_for_owner(
        org_id=principal.org_id,
        user_id=principal.user_id,
        runtime_workspace_id=runtime_workspace_id,
        runtime_session_id=created.runtime_session_id,
    )
    messages = [
        {"role": str(item["role"]), "content": str(item["content"])}
        for item in history.messages
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    if len(messages) != created.message_count:
        raise HermesProtocolError("context branch history projection is incomplete")
    control = await chat_service.get_session_history_control(
        principal=principal,
        runtime_workspace_id=runtime_workspace_id,
        runtime_session_id=created.runtime_session_id,
    )
    result = await HermesDashboardProtocolAdapter().dashboard_json_request(
        control.endpoint,
        "POST",
        "/api/sessions/import",
        json_body={
            "profile": control.hermes_profile_name,
            "sessions": [
                {
                    "id": created.runtime_session_id,
                    "source": "relay",
                    "title": f"context-{created.runtime_session_id}",
                    "messages": messages,
                }
            ],
        },
    )
    imported_ids = result.get("imported_ids")
    skipped_ids = result.get("skipped_ids")
    if (
        result.get("ok") is not True
        or not isinstance(imported_ids, list)
        or not isinstance(skipped_ids, list)
        or created.runtime_session_id not in {*imported_ids, *skipped_ids}
        or int(result.get("imported") or 0) + int(result.get("skipped") or 0) != 1
    ):
        raise HermesProtocolError(
            "Runtime Dashboard context import returned invalid result",
            path="/api/sessions/import",
        )


class InMemoryRuntimeSessionStore:
    def __init__(self) -> None:
        self.records: list[RuntimeSessionRecord] = []
        self.next_id = 1

    async def list_sessions(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
    ) -> list[RuntimeSessionRecord]:
        return [
            record for record in self.records
            if record.org_id == org_id
            and record.user_id == user_id
            and record.runtime_workspace_id == runtime_workspace_id
            and record.status in {"active", "expert_deleted"}
        ]

    async def create_session(
        self,
        *,
        org_id: int,
        user_id: int,
        command: CreateRuntimeSessionCommand,
    ) -> RuntimeSessionRecord:
        session_id = self.next_id
        self.next_id += 1
        record = RuntimeSessionRecord(
            session_id=session_id,
            runtime_workspace_id=command.runtime_workspace_id,
            org_id=org_id,
            user_id=user_id,
            runtime_session_id=default_runtime_session_id(
                runtime_workspace_id=command.runtime_workspace_id,
                session_id=session_id,
            ),
            title=command.title,
            source_type=command.source_type,
            context_source_runtime_session_id=command.context_source_runtime_session_id,
            message_count=0,
            session_model_entry_id=command.model_entry_id,
            status="active",
            last_active_at=utc_now(),
            runtime_agent_profile_id=command.runtime_agent_profile_id or 1,
            agent_display_name="自己" if command.runtime_agent_profile_id is None else None,
            agent_profile_kind="default" if command.runtime_agent_profile_id is None else "expert",
            hermes_profile_name="default" if command.runtime_agent_profile_id is None else None,
        )
        self.records.append(record)
        return record

    async def get_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> RuntimeSessionRecord:
        for record in self.records:
            if (
                record.org_id == org_id
                and record.user_id == user_id
                and record.runtime_workspace_id == runtime_workspace_id
                and record.runtime_session_id == runtime_session_id
                and record.status in {"active", "expert_deleted"}
            ):
                return record
        raise PermissionError("runtime session not found")

    async def rename_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        title: str,
    ) -> RuntimeSessionRecord:
        current = await self.get_session(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
        if current.is_immutable:
            raise RuntimeSessionImmutableError(
                RuntimeSessionImmutableError.code
            )
        await self.assert_title_available(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            title=title,
        )
        renamed = replace(current, title=title)
        self.records[self.records.index(current)] = renamed
        return renamed

    async def assert_title_available(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
        title: str,
    ) -> None:
        if any(
            record.org_id == org_id
            and record.user_id == user_id
            and record.runtime_workspace_id == runtime_workspace_id
            and record.runtime_session_id != runtime_session_id
            and record.status == "active"
            and record.title == title
            for record in self.records
        ):
            raise RuntimeSessionTitleConflictError("session title is already in use")

    async def delete_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        for index, record in enumerate(self.records):
            if (
                record.org_id == org_id
                and record.user_id == user_id
                and record.runtime_workspace_id == runtime_workspace_id
                and record.runtime_session_id == runtime_session_id
                and record.status == "active"
            ):
                if record.is_immutable:
                    raise RuntimeSessionImmutableError(
                        RuntimeSessionImmutableError.code
                    )
                del self.records[index]
                return
        raise PermissionError("runtime session not found")

    async def mark_session_deleting(self, **_kwargs) -> None:
        return None

    async def mark_hermes_materialized(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        current = await self.get_session(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
        if current.hermes_materialized_at is None:
            self.records[self.records.index(current)] = replace(
                current,
                hermes_materialized_at=utc_now(),
            )


_runtime_session_store = InMemoryRuntimeSessionStore()


async def get_runtime_session_service(db: AsyncSession = Depends(get_db)) -> RuntimeSessionService:
    if settings.RUNTIME_CHAT_MODE == "real":
        return RuntimeSessionService(SqlAlchemyRuntimeSessionStore(db))
    return RuntimeSessionService(_runtime_session_store)


@router.get("/{runtime_workspace_id}/sessions", response_model=list[RuntimeSessionSummary])
async def list_runtime_sessions(
    runtime_workspace_id: int,
    principal: Principal = Depends(get_principal),
    service: RuntimeSessionService = Depends(get_runtime_session_service),
) -> list[RuntimeSessionSummary]:
    return await service.list_sessions(
        principal=principal,
        runtime_workspace_id=runtime_workspace_id,
    )


@router.get("/{runtime_workspace_id}/sessions/{runtime_session_id}", response_model=RuntimeSessionSummary)
async def read_runtime_session(
    runtime_workspace_id: int,
    runtime_session_id: str,
    principal: Principal = Depends(get_principal),
    service: RuntimeSessionService = Depends(get_runtime_session_service),
) -> RuntimeSessionSummary:
    try:
        return await service.get_session(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{runtime_workspace_id}/sessions", response_model=RuntimeSessionSummary)
async def create_runtime_session(
    runtime_workspace_id: int,
    payload: CreateRuntimeSessionRequest,
    principal: Principal = Depends(get_principal),
    service: RuntimeSessionService = Depends(get_runtime_session_service),
    model_service: RuntimeModelService = Depends(get_runtime_model_service),
    chat_service: RuntimeChatService = Depends(get_runtime_chat_service),
    chat_log_projector: ChatLogProjector = Depends(get_chat_log_projector),
) -> RuntimeSessionSummary:
    if settings.HERMES_RUNTIME_STORAGE_MAINTENANCE:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="runtime storage maintenance is in progress")
    if payload.source_type == "wecom":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="wecom sessions are server-managed",
        )
    try:
        visible_models = await model_service.list_models(principal)
    except (ModelCatalogCacheFillThrottledError, ModelCatalogCacheCircuitOpenError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if not visible_models:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no available model")
    model_entry_id = payload.model_entry_id or resolve_default_model_entry_id(
        visible_models
    )
    if model_entry_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no available model",
        )
    if model_entry_id not in {model.model_entry_id for model in visible_models}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="model entry is not visible")
    created: RuntimeSessionRecord | None = None
    try:
        if payload.context_source_runtime_session_id is not None:
            await chat_log_projector.project_owned_session(
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=payload.context_source_runtime_session_id,
            )
            source = await service.get_session(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=payload.context_source_runtime_session_id,
            )
            if source.is_read_only or source.is_immutable:
                raise RuntimeSessionContextBranchError(
                    "context branch source must be writable"
                )
        created = await service.create_session(
            principal=principal,
            command=CreateRuntimeSessionCommand(
                runtime_workspace_id=runtime_workspace_id,
                title=payload.title,
                model_entry_id=model_entry_id,
                source_type=payload.source_type,
                runtime_agent_profile_id=payload.runtime_agent_profile_id,
                context_source_runtime_session_id=payload.context_source_runtime_session_id,
            ),
        )
        if (
            created.context_source_runtime_session_id is not None
            and settings.RUNTIME_CHAT_MODE == "real"
        ):
            await _import_context_branch_history(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                created=created,
                chat_service=chat_service,
                chat_log_projector=chat_log_projector,
            )
        return created
    except RuntimeSessionProfilePreparingError as exc:
        raise _profile_preparing_http_error(exc) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ChatLogSessionNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RuntimeSessionActiveRunError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="source session has an active run",
        ) from exc
    except RuntimeSessionContextBranchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except RuntimeChatPreparationError as exc:
        if created is not None:
            await service.mark_session_deleting(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=created.runtime_session_id,
            )
        raise _session_http_error(exc)
    except HermesProtocolError as exc:
        if created is not None:
            await service.mark_session_deleting(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=created.runtime_session_id,
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="expert context import failed",
        ) from exc


@router.patch(
    "/{runtime_workspace_id}/sessions/{runtime_session_id}",
    response_model=RuntimeSessionSummary,
)
async def rename_runtime_session(
    runtime_workspace_id: int,
    runtime_session_id: str,
    payload: RenameRuntimeSessionRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
    service: RuntimeSessionService = Depends(get_runtime_session_service),
    chat_service: RuntimeChatService = Depends(get_runtime_chat_service),
) -> RuntimeSessionSummary:
    if settings.HERMES_RUNTIME_STORAGE_MAINTENANCE:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="runtime storage maintenance is in progress")
    try:
        session = await service.get_session(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
        _require_writable_session(session)
        await service.assert_title_available(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            title=payload.title,
        )
        if session.hermes_materialized_at is not None:
            control = await chat_service.get_session_history_control(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=runtime_session_id,
            )
            target = await load_personal_runtime_command_target(
                db,
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
            )
            command_client = RuntimeCommandClient()
            try:
                await command_client.mutate_session(
                    target=target,
                    operation="rename",
                    session_id=runtime_session_id,
                    profile_id=control.hermes_profile_name,
                    title=payload.title,
                )
            finally:
                await command_client.close()
        return await service.rename_session(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            title=payload.title,
        )
    except (PermissionError, RuntimeChatPreparationError) as exc:
        raise _session_http_error(exc) from exc
    except RuntimeSessionTitleConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (HermesProtocolError, RuntimeCommandError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.delete(
    "/{runtime_workspace_id}/sessions/{runtime_session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_runtime_session(
    runtime_workspace_id: int,
    runtime_session_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
    service: RuntimeSessionService = Depends(get_runtime_session_service),
    chat_service: RuntimeChatService = Depends(get_runtime_chat_service),
) -> Response:
    if settings.HERMES_RUNTIME_STORAGE_MAINTENANCE:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="runtime storage maintenance is in progress")
    try:
        if settings.RUNTIME_CHAT_MODE == "real":
            session = await service.get_session(
                principal=principal,
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=runtime_session_id,
            )
            # A deleted expert Session is intentionally read-only for chat
            # and rename, but the owner must still be able to purge its
            # retained history. Its Hermes Profile no longer exists, so the
            # branch below skips the Runtime delete and removes only the
            # platform record.
            if not session.is_read_only:
                await service.mark_session_deleting(
                    principal=principal,
                    runtime_workspace_id=runtime_workspace_id,
                    runtime_session_id=runtime_session_id,
                )
                if session.hermes_materialized_at is not None:
                    control = await chat_service.get_session_history_control(
                        principal=principal,
                        runtime_workspace_id=runtime_workspace_id,
                        runtime_session_id=runtime_session_id,
                        include_deleting=True,
                    )
                    try:
                        target = await load_personal_runtime_command_target(
                            db,
                            org_id=principal.org_id,
                            user_id=principal.user_id,
                            runtime_workspace_id=runtime_workspace_id,
                        )
                        command_client = RuntimeCommandClient()
                        try:
                            await command_client.mutate_session(
                                target=target,
                                operation="delete",
                                session_id=runtime_session_id,
                                profile_id=control.hermes_profile_name,
                            )
                        finally:
                            await command_client.close()
                    except RuntimeCommandError as exc:
                        if "404" not in str(exc):
                            raise
        await service.delete_session(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
    except (PermissionError, RuntimeChatPreparationError) as exc:
        raise _session_http_error(exc) from exc
    except RuntimeSessionActiveRunError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (HermesProtocolError, RuntimeCommandError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
