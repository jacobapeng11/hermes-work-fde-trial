from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.config import settings
from app.database import get_db
from app.api.v1.user.chat import get_runtime_chat_service
from app.schemas.runtime_clarify import (
    RuntimeClarifyAnswerRequest,
    RuntimeClarifyCreateRequest,
    RuntimeClarifyCreateResponse,
    RuntimeClarifyRequestListResponse,
    RuntimeClarifyRequestView,
    RuntimeClarifyStatusResponse,
)
from app.services.runtime_chat import RuntimeChatService
from app.services.runtime_clarify import RuntimeClarifyError, RuntimeClarifyService
from app.services.runtime_workspace import Principal


runtime_router = APIRouter(prefix="/runtime-clarify/{runtime_workspace_id}", tags=["runtime-clarify"])
user_router = APIRouter(prefix="/runtime-workspace", tags=["user-runtime-clarify"])


def _bearer(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing runtime token")
    return token


def _service(db: AsyncSession) -> RuntimeClarifyService:
    return RuntimeClarifyService(
        db,
        timeout_seconds=settings.HERMES_USER_WEB_CLARIFY_TIMEOUT_SECONDS,
        auto_default_seconds=(
            settings.HERMES_USER_WEB_CLARIFY_AUTO_DEFAULT_SECONDS
        ),
        claim_lease_seconds=(
            settings.HERMES_USER_WEB_CLARIFY_CLAIM_LEASE_SECONDS
        ),
    )


def _view(value) -> RuntimeClarifyRequestView:
    return RuntimeClarifyRequestView(
        request_id=value.request_id,
        runtime_session_id=value.runtime_session_id,
        # The durable request owns the parent row id; this value is only a
        # display correlation and is not used for authorization or resume.
        parent_runtime_run_id=str(value.parent_run_id),
        question=value.question,
        choices=list(value.choices or []),
        multi_select=bool(value.multi_select),
        status=value.status,
        answer=value.answer,
        answer_source=value.answer_source,
        auto_default_at=value.auto_default_at,
        expires_at=value.expires_at,
        version=int(value.version),
        created_at=value.created_at,
        answered_at=value.answered_at,
        canceled_at=value.canceled_at,
        resume_runtime_run_id=value.resume_run_id,
        claim_expires_at=value.claim_expires_at,
        resume_attempt_count=int(value.resume_attempt_count or 0),
        next_resume_at=value.next_resume_at,
        mode=str(value.mode or "web_run"),
        requested_by=value.requested_by,
        expert_profile_id=value.expert_profile_id,
    )


@runtime_router.post("/requests", response_model=RuntimeClarifyCreateResponse)
async def create_runtime_clarify_request(
    runtime_workspace_id: int,
    payload: RuntimeClarifyCreateRequest,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> RuntimeClarifyCreateResponse:
    try:
        receipt = await _service(db).accept_runtime_request(
            runtime_workspace_id=runtime_workspace_id,
            runtime_token=_bearer(authorization),
            payload=payload,
        )
    except RuntimeClarifyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    request = receipt.request
    return RuntimeClarifyCreateResponse(
        enabled=receipt.enabled,
        request_id=request.request_id if request is not None else "disabled",
        duplicate=receipt.duplicate,
        expires_at=request.expires_at if request is not None else None,
        auto_default_at=request.auto_default_at if request is not None else None,
        mode=receipt.mode,
    )


@runtime_router.get(
    "/requests/{request_id}",
    response_model=RuntimeClarifyStatusResponse,
)
async def get_runtime_clarify_request_status(
    runtime_workspace_id: int,
    request_id: str,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> RuntimeClarifyStatusResponse:
    request = await _service(db).runtime_request_status(
        runtime_workspace_id=runtime_workspace_id,
        request_id=request_id,
        runtime_token=_bearer(authorization),
    )
    if request is None:
        raise HTTPException(status_code=404, detail="clarify request not found")
    return RuntimeClarifyStatusResponse(
        request_id=request.request_id,
        status=request.status,
        answer=request.answer,
        auto_default_at=request.auto_default_at,
    )


@user_router.get("/{runtime_workspace_id}/sessions/{runtime_session_id}/clarify-requests", response_model=RuntimeClarifyRequestListResponse)
async def list_user_clarify_requests(
    runtime_workspace_id: int,
    runtime_session_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeClarifyRequestListResponse:
    rows = await _service(db).list_for_session(principal=principal, runtime_workspace_id=runtime_workspace_id, runtime_session_id=runtime_session_id)
    return RuntimeClarifyRequestListResponse(items=[_view(row) for row in rows])


@user_router.post("/{runtime_workspace_id}/clarify-requests/{request_id}/answer", response_model=RuntimeClarifyRequestView)
async def answer_user_clarify_request(
    runtime_workspace_id: int,
    request_id: str,
    payload: RuntimeClarifyAnswerRequest,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
    chat_service: RuntimeChatService = Depends(get_runtime_chat_service),
) -> RuntimeClarifyRequestView:
    try:
        request = await _service(db).answer(
            principal=principal, runtime_workspace_id=runtime_workspace_id,
            request_id=request_id, answer=payload.answer, version=payload.version,
        )
    except RuntimeClarifyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    if str(request.mode or "web_run") == "expert_worker":
        request = await _service(db).finalize_expert_worker_request(request)
        return _view(request)
    try:
        request = await _service(db).resume_claimed_request(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            request=request,
            chat_service=chat_service,
        )
    except RuntimeClarifyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    return _view(request)


@user_router.post("/{runtime_workspace_id}/clarify-requests/{request_id}/cancel", response_model=RuntimeClarifyRequestView)
async def cancel_user_clarify_request(
    runtime_workspace_id: int,
    request_id: str,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> RuntimeClarifyRequestView:
    try:
        request = await _service(db).cancel(principal=principal, runtime_workspace_id=runtime_workspace_id, request_id=request_id)
    except RuntimeClarifyError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    return _view(request)
