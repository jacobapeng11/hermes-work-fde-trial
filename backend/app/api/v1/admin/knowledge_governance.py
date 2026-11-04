from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.api.v1.admin.audit_logs import get_admin_audit_log_service
from app.api.v1.admin.deps import get_admin_principal
from app.services.admin_audit import AdminAuditLogService
from app.services.admin_auth import AdminPrincipal
from app.services.knowledge_governance import (
    KnowledgeGovernanceError,
    KnowledgeGovernanceProvider,
    get_knowledge_governance_provider,
    governance_asset,
    governance_impact,
    governance_summary,
    list_governance_assets,
)


router = APIRouter(
    prefix="/knowledge-governance",
    tags=["admin-knowledge-governance"],
)


def _provider() -> KnowledgeGovernanceProvider:
    return get_knowledge_governance_provider()


def _error_response(exc: KnowledgeGovernanceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.as_error()},
    )


async def _snapshot(provider: KnowledgeGovernanceProvider):
    try:
        return await provider.get_snapshot()
    except KnowledgeGovernanceError as exc:
        return _error_response(exc)


@router.get("/summary")
async def knowledge_governance_summary(
    _principal: AdminPrincipal = Depends(get_admin_principal),
    provider: KnowledgeGovernanceProvider = Depends(_provider),
):
    result = await _snapshot(provider)
    return result if isinstance(result, JSONResponse) else governance_summary(result)


@router.get("/validation")
async def knowledge_governance_validation(
    _principal: AdminPrincipal = Depends(get_admin_principal),
    provider: KnowledgeGovernanceProvider = Depends(_provider),
):
    result = await _snapshot(provider)
    if isinstance(result, JSONResponse):
        return result
    return {
        "validation": result.snapshot["validation"],
        "cache": result.cache,
        "latest_attempt": result.latest_attempt,
    }


@router.get("/assets")
async def knowledge_governance_assets(
    q: str | None = Query(default=None, max_length=200),
    asset_type: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    _principal: AdminPrincipal = Depends(get_admin_principal),
    provider: KnowledgeGovernanceProvider = Depends(_provider),
):
    result = await _snapshot(provider)
    if isinstance(result, JSONResponse):
        return result
    return list_governance_assets(
        result,
        query=q.strip() if q and q.strip() else None,
        asset_type=asset_type.strip() if asset_type and asset_type.strip() else None,
        page=page,
        page_size=page_size,
    )


@router.get("/assets/{asset_id:path}")
async def knowledge_governance_asset(
    asset_id: str,
    _principal: AdminPrincipal = Depends(get_admin_principal),
    provider: KnowledgeGovernanceProvider = Depends(_provider),
):
    result = await _snapshot(provider)
    if isinstance(result, JSONResponse):
        return result
    payload = governance_asset(result, asset_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="knowledge_governance_asset_not_found")
    return payload


@router.get("/impact/{asset_id:path}")
async def knowledge_governance_asset_impact(
    asset_id: str,
    _principal: AdminPrincipal = Depends(get_admin_principal),
    provider: KnowledgeGovernanceProvider = Depends(_provider),
):
    result = await _snapshot(provider)
    if isinstance(result, JSONResponse):
        return result
    payload = governance_impact(result, asset_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="knowledge_governance_asset_not_found")
    return payload


@router.get("/metric-contracts")
async def knowledge_governance_metric_contracts(
    _principal: AdminPrincipal = Depends(get_admin_principal),
    provider: KnowledgeGovernanceProvider = Depends(_provider),
):
    result = await _snapshot(provider)
    if isinstance(result, JSONResponse):
        return result
    return {
        "metric_contracts": result.snapshot["metric_contracts"],
        "cache": result.cache,
        "latest_attempt": result.latest_attempt,
    }


@router.post("/refresh")
async def refresh_knowledge_governance(
    principal: AdminPrincipal = Depends(get_admin_principal),
    provider: KnowledgeGovernanceProvider = Depends(_provider),
    audit: AdminAuditLogService = Depends(get_admin_audit_log_service),
):
    try:
        result = await provider.get_snapshot(force=True)
    except KnowledgeGovernanceError as exc:
        await audit.record(
            principal=principal,
            action="knowledge_governance.refresh",
            target_type="knowledge_governance",
            target_id="enterprise",
            detail={"status": "failed", "error_code": exc.code},
        )
        return _error_response(exc)
    await audit.record(
        principal=principal,
        action="knowledge_governance.refresh",
        target_type="knowledge_governance",
        target_id="enterprise",
        detail={
            "status": result.latest_attempt.get("status"),
            "source_hash": result.snapshot["source_hash"],
            "cache_state": result.cache["state"],
        },
    )
    return governance_summary(result)
