from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.audit_logs import get_admin_audit_log_service
from app.api.v1.admin.deps import get_admin_principal
from app.config import settings
from app.database import get_db
from app.schemas.admin_kubernetes import (
    AdminClusterSummary,
    AdminKubernetesEventSummary,
    AdminRuntimeKubernetesSnapshot,
    AdminRuntimeHealthSnapshot,
    AdminSafeKubernetesSummary,
)
from app.models import (
    ApplicationAgentRuntimeBinding,
    HermesRuntimeBinding,
    RuntimeProjectionReceipt,
)
from app.services.admin_audit import AdminAuditLogService
from app.services.admin_auth import AdminPrincipal
from app.services.admin_kubernetes_client import (
    AdminKubectlClient,
    AdminKubernetesError,
    AdminKubernetesTimeoutError,
    AdminKubernetesValidationError,
)
from app.services.admin_kubernetes_resources import (
    AdminKubernetesNotFoundError,
    AdminKubernetesResourceService,
    InMemoryAdminKubernetesReader,
    InMemoryAdminKubernetesScopeStore,
    SqlAlchemyAdminKubernetesScopeStore,
)


router = APIRouter(prefix="/resources", tags=["admin-kubernetes-resources"])
_memory_reader = InMemoryAdminKubernetesReader()
_memory_scope_store = InMemoryAdminKubernetesScopeStore()
_follow_counts: dict[int, int] = {}
_follow_lock = asyncio.Lock()
RuntimeKind = Literal["personal", "application"]


async def get_admin_kubernetes_client():
    if settings.RUNTIME_CHAT_MODE == "real":
        if not settings.ADMIN_KUBECONFIG:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Admin Kubernetes resource access is not configured",
            )
        return AdminKubectlClient(kubeconfig=settings.ADMIN_KUBECONFIG)
    return _memory_reader


async def get_admin_kubernetes_resource_service(
    db: AsyncSession = Depends(get_db),
    client=Depends(get_admin_kubernetes_client),
) -> AdminKubernetesResourceService:
    if settings.RUNTIME_CHAT_MODE == "real":
        return AdminKubernetesResourceService(
            scope_store=SqlAlchemyAdminKubernetesScopeStore(
                db,
                runtime_namespace=settings.HERMES_RUNTIME_NAMESPACE,
            ),
            client=client,
            runtime_namespace=settings.HERMES_RUNTIME_NAMESPACE,
        )
    return AdminKubernetesResourceService(
        scope_store=_memory_scope_store,
        client=client,
        runtime_namespace=settings.HERMES_RUNTIME_NAMESPACE,
    )


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, AdminKubernetesNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, AdminKubernetesValidationError):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if isinstance(exc, AdminKubernetesTimeoutError):
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)) from exc
    if isinstance(exc, AdminKubernetesError):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Kubernetes resource data is unavailable",
        ) from exc
    raise exc


@router.get("/cluster-summary", response_model=AdminClusterSummary)
async def get_cluster_summary(
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminKubernetesResourceService = Depends(get_admin_kubernetes_resource_service),
) -> AdminClusterSummary:
    try:
        return await service.cluster_summary(principal)
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get(
    "/runtime-workspaces/kubernetes-snapshots",
    response_model=list[AdminRuntimeKubernetesSnapshot],
)
async def list_runtime_kubernetes_snapshots(
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminKubernetesResourceService = Depends(get_admin_kubernetes_resource_service),
) -> list[AdminRuntimeKubernetesSnapshot]:
    try:
        return await service.runtime_snapshots(principal)
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get(
    "/runtime-workspaces/{runtime_workspace_id}/kubernetes",
    response_model=AdminRuntimeKubernetesSnapshot,
)
async def get_runtime_kubernetes_snapshot(
    runtime_workspace_id: int,
    runtime_kind: RuntimeKind = "personal",
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminKubernetesResourceService = Depends(get_admin_kubernetes_resource_service),
) -> AdminRuntimeKubernetesSnapshot:
    try:
        return await service.runtime_snapshot(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get(
    "/runtime-workspaces/{runtime_workspace_id}/health",
    response_model=AdminRuntimeHealthSnapshot,
)
async def get_runtime_health_snapshot(
    runtime_workspace_id: int,
    runtime_kind: RuntimeKind = "personal",
    principal: AdminPrincipal = Depends(get_admin_principal),
    db: AsyncSession = Depends(get_db),
) -> AdminRuntimeHealthSnapshot:
    if runtime_kind == "application":
        binding = await db.scalar(
            select(ApplicationAgentRuntimeBinding).where(
                ApplicationAgentRuntimeBinding.org_id == principal.org_id,
                ApplicationAgentRuntimeBinding.application_agent_id
                == runtime_workspace_id,
            )
        )
    else:
        binding = await db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.org_id == principal.org_id,
                HermesRuntimeBinding.runtime_workspace_id
                == runtime_workspace_id,
            )
        )
    if binding is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runtime binding not found",
        )
    receipt = await db.scalar(
        select(RuntimeProjectionReceipt)
        .where(
            RuntimeProjectionReceipt.runtime_id == str(binding.runtime_id),
            RuntimeProjectionReceipt.projection_type == "runtime.health",
        )
        .order_by(RuntimeProjectionReceipt.occurred_at.desc())
        .limit(1)
    )
    if receipt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Runtime health has not been reported",
        )
    observed_at = receipt.occurred_at
    observed_at_utc = (
        observed_at.replace(tzinfo=timezone.utc)
        if observed_at.tzinfo is None
        else observed_at.astimezone(timezone.utc)
    )
    return AdminRuntimeHealthSnapshot(
        runtime_workspace_id=runtime_workspace_id,
        runtime_kind=runtime_kind,
        runtime_id=str(binding.runtime_id),
        observed_at=observed_at,
        received_at=receipt.created_at,
        stale=(datetime.now(timezone.utc) - observed_at_utc).total_seconds() > 75,
        summary=dict(receipt.payload or {}),
    )


@router.get(
    "/runtime-workspaces/{runtime_workspace_id}/events",
    response_model=list[AdminKubernetesEventSummary],
)
async def get_runtime_kubernetes_events(
    runtime_workspace_id: int,
    runtime_kind: RuntimeKind = "personal",
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminKubernetesResourceService = Depends(get_admin_kubernetes_resource_service),
) -> list[AdminKubernetesEventSummary]:
    try:
        return await service.runtime_events(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get(
    "/runtime-workspaces/{runtime_workspace_id}/kubernetes/yaml-summary",
    response_model=AdminSafeKubernetesSummary,
)
async def get_runtime_safe_yaml_summary(
    runtime_workspace_id: int,
    runtime_kind: RuntimeKind = "personal",
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminKubernetesResourceService = Depends(get_admin_kubernetes_resource_service),
) -> AdminSafeKubernetesSummary:
    try:
        return await service.safe_yaml_summary(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
        )
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get("/runtime-workspaces/{runtime_workspace_id}/pods/{pod_name}/logs")
async def get_runtime_pod_logs(
    runtime_workspace_id: int,
    pod_name: str,
    runtime_kind: RuntimeKind = "personal",
    container: str = Query(min_length=1, max_length=253),
    tail_lines: int = Query(default=500, ge=1, le=2000),
    previous: bool = False,
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminKubernetesResourceService = Depends(get_admin_kubernetes_resource_service),
    audit_service: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> PlainTextResponse:
    try:
        content = await service.pod_logs(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
            pod_name=pod_name,
            container=container,
            tail_lines=tail_lines,
            previous=previous,
        )
        await audit_service.record(
            principal=principal,
            action="admin.kubernetes.logs_viewed",
            target_type="runtime_pod",
            target_id=pod_name,
            runtime_workspace_id=(
                runtime_workspace_id
                if runtime_kind == "personal"
                else None
            ),
            detail={
                "runtime_kind": runtime_kind,
                "runtime_resource_id": runtime_workspace_id,
                "pod_name": pod_name,
                "container": container,
                "tail_lines": tail_lines,
                "previous": previous,
            },
        )
        return PlainTextResponse(content)
    except Exception as exc:
        _raise_http(exc)
        raise


@router.get("/runtime-workspaces/{runtime_workspace_id}/pods/{pod_name}/logs/stream")
async def stream_runtime_pod_logs(
    runtime_workspace_id: int,
    pod_name: str,
    runtime_kind: RuntimeKind = "personal",
    container: str = Query(min_length=1, max_length=253),
    tail_lines: int = Query(default=100, ge=1, le=2000),
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminKubernetesResourceService = Depends(get_admin_kubernetes_resource_service),
    audit_service: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> StreamingResponse:
    await _reserve_follow(principal.user_id)
    try:
        await service.validate_pod_container(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
            pod_name=pod_name,
            container=container,
        )
        await audit_service.record(
            principal=principal,
            action="admin.kubernetes.logs_followed",
            target_type="runtime_pod",
            target_id=pod_name,
            runtime_workspace_id=(
                runtime_workspace_id
                if runtime_kind == "personal"
                else None
            ),
            detail={
                "runtime_kind": runtime_kind,
                "runtime_resource_id": runtime_workspace_id,
                "pod_name": pod_name,
                "container": container,
                "tail_lines": tail_lines,
            },
        )
    except Exception as exc:
        await _release_follow(principal.user_id)
        _raise_http(exc)
        raise

    async def events() -> AsyncIterator[str]:
        stream = service.stream_pod_logs(
            principal,
            runtime_workspace_id,
            runtime_kind=runtime_kind,
            pod_name=pod_name,
            container=container,
            tail_lines=tail_lines,
        )
        try:
            async for line in stream:
                yield _sse("log.line", {"line": line})
            yield _sse("log.done", {"reason": "completed"})
        except asyncio.CancelledError:
            raise
        except Exception:
            yield _sse("log.error", {"message": "日志流已断开"})
        finally:
            await stream.aclose()
            await _release_follow(principal.user_id)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _reserve_follow(user_id: int) -> None:
    async with _follow_lock:
        count = _follow_counts.get(user_id, 0)
        if count >= 3:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="最多同时查看 3 个实时日志流",
            )
        _follow_counts[user_id] = count + 1


async def _release_follow(user_id: int) -> None:
    async with _follow_lock:
        count = _follow_counts.get(user_id, 0)
        if count <= 1:
            _follow_counts.pop(user_id, None)
        else:
            _follow_counts[user_id] = count - 1


def _sse(event: str, payload: dict) -> str:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )
