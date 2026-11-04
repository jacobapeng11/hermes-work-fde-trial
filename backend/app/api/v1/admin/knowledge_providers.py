from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.audit_logs import get_admin_audit_log_service
from app.api.v1.admin.deps import get_admin_principal
from app.database import get_db
from app.schemas.admin import (
    AdminWeKnoraRerankConfigResponse,
    AdminWeKnoraRerankConfigUpdateRequest,
    AdminWeKnoraRerankModelSummary,
)
from app.services.admin_audit import AdminAuditLogService
from app.services.admin_auth import AdminPrincipal
from app.services.weknora_rerank_governance import (
    WeKnoraRerankConfigSnapshot,
    WeKnoraRerankGovernanceError,
    WeKnoraRerankGovernanceService,
)
from app.services.weknora_rerank_settings import (
    WeKnoraRerankMode,
    WeKnoraRerankSettingService,
    WeKnoraRerankSettingStoreError,
)


router = APIRouter(prefix="/knowledge-providers", tags=["admin-knowledge-providers"])


def get_weknora_rerank_service() -> WeKnoraRerankGovernanceService:
    return WeKnoraRerankGovernanceService()


def get_weknora_rerank_setting_service(
    db: AsyncSession = Depends(get_db),
) -> WeKnoraRerankSettingService:
    return WeKnoraRerankSettingService(db)


def _setting_store_error() -> WeKnoraRerankGovernanceError:
    return WeKnoraRerankGovernanceError(
        code="weknora_rerank_config_unavailable",
        message="WeKnora Rerank 平台开关暂不可用",
        status_code=503,
    )


def _config_response(
    snapshot: WeKnoraRerankConfigSnapshot | None,
    *,
    mode: WeKnoraRerankMode,
) -> AdminWeKnoraRerankConfigResponse:
    disabled = mode == "disabled"
    models = () if snapshot is None else snapshot.models
    return AdminWeKnoraRerankConfigResponse(
        provider="weknora",
        mode=mode,
        configured_model_id="" if snapshot is None else snapshot.configured_model_id,
        effective_model_id="" if disabled or snapshot is None else snapshot.effective_model_id,
        config_source="disabled" if disabled else (
            "none" if snapshot is None else snapshot.config_source
        ),
        models=[
            AdminWeKnoraRerankModelSummary(
                id=model.id,
                name=model.name,
                display_name=model.display_name,
                source=model.source,
                status=model.status,
            )
            for model in models
        ],
    )


def _error_response(exc: WeKnoraRerankGovernanceError, request_id: str) -> JSONResponse:
    error_codes = {
        "weknora_rerank_config_unavailable": -13005,
        "weknora_rerank_model_invalid": -13006,
    }
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "error_code": error_codes[exc.code],
            "message": exc.message,
            "request_id": request_id,
        },
    )


async def _record_update_failure(
    *,
    audit: AdminAuditLogService,
    principal: AdminPrincipal,
    payload: AdminWeKnoraRerankConfigUpdateRequest,
    error_code: str,
    request_id: str,
) -> None:
    try:
        await audit.record(
            principal=principal,
            action="admin.weknora.rerank_config_update_failed",
            target_type="knowledge_provider",
            target_id="weknora",
            detail={
                "status": "failed",
                "mode": payload.mode,
                "selected_model_id": payload.rerank_model_id,
                "error_code": error_code,
                "request_id": request_id,
            },
        )
    except Exception:
        pass


@router.get("/weknora/rerank-config", response_model=AdminWeKnoraRerankConfigResponse)
async def get_weknora_rerank_config(
    request: Request,
    _principal: AdminPrincipal = Depends(get_admin_principal),
    service: WeKnoraRerankGovernanceService = Depends(get_weknora_rerank_service),
    settings: WeKnoraRerankSettingService = Depends(
        get_weknora_rerank_setting_service
    ),
) -> AdminWeKnoraRerankConfigResponse:
    request_id = getattr(request.state, "request_id", None) or uuid4().hex
    try:
        mode = await settings.get_mode()
        snapshot = await service.get_config()
    except WeKnoraRerankSettingStoreError:
        return _error_response(_setting_store_error(), request_id)
    except WeKnoraRerankGovernanceError as exc:
        return _error_response(exc, request_id)
    return _config_response(snapshot, mode=mode)


@router.put("/weknora/rerank-config", response_model=AdminWeKnoraRerankConfigResponse)
async def update_weknora_rerank_config(
    payload: AdminWeKnoraRerankConfigUpdateRequest,
    request: Request,
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: WeKnoraRerankGovernanceService = Depends(get_weknora_rerank_service),
    settings: WeKnoraRerankSettingService = Depends(
        get_weknora_rerank_setting_service
    ),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> AdminWeKnoraRerankConfigResponse:
    request_id = getattr(request.state, "request_id", None) or uuid4().hex
    snapshot: WeKnoraRerankConfigSnapshot | None = None
    try:
        if payload.mode == "disabled":
            mode = await settings.set_mode(
                mode="disabled",
                updated_by_user_id=principal.user_id,
            )
        else:
            selected_model_id = str(payload.rerank_model_id or "").strip()
            snapshot = await service.update_config(
                rerank_model_id=selected_model_id,
            )
            mode = await settings.set_mode(
                mode="model",
                updated_by_user_id=principal.user_id,
            )
    except WeKnoraRerankSettingStoreError:
        await _record_update_failure(
            audit=audit,
            principal=principal,
            payload=payload,
            error_code="weknora_rerank_config_unavailable",
            request_id=request_id,
        )
        return _error_response(_setting_store_error(), request_id)
    except WeKnoraRerankGovernanceError as exc:
        await _record_update_failure(
            audit=audit,
            principal=principal,
            payload=payload,
            error_code=exc.code,
            request_id=request_id,
        )
        return _error_response(exc, request_id)

    try:
        await audit.record(
            principal=principal,
            action="admin.weknora.rerank_config_updated",
            target_type="knowledge_provider",
            target_id="weknora",
            detail={
                "status": "succeeded",
                "mode": mode,
                "configured_model_id": "" if snapshot is None else snapshot.configured_model_id,
                "effective_model_id": "" if snapshot is None else snapshot.effective_model_id,
                "request_id": request_id,
            },
        )
    except Exception:
        pass
    return _config_response(snapshot, mode=mode)
