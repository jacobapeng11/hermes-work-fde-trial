from __future__ import annotations

from functools import lru_cache
from typing import Protocol

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import get_platform_session_store
from app.api.v1.admin.deps import get_admin_principal
from app.api.v1.admin.kubernetes_resources import (
    _raise_http,
    get_admin_kubernetes_resource_service,
)
from app.config import settings
from app.database import get_db
from app.schemas.admin import (
    AdminModelUsageSummary,
    AdminPresenceUserGroup,
    AdminPresenceSummaryResponse,
    AdminRuntimeWorkspaceUsageSummary,
    AdminUsageOverviewSummary,
    AdminUsageRecentErrorSummary,
    AdminUsageSummaryResponse,
)
from app.services.admin_auth import AdminPrincipal
from app.services.admin_usage import (
    AdminModelUsageRecord,
    AdminRuntimeWorkspaceUsageRecord,
    AdminUsageOverviewRecord,
    AdminUsageRecentErrorRecord,
    AdminUsageService,
    InMemoryAdminUsageStore,
    SqlAlchemyAdminUsageStore,
)
from app.services.admin_kubernetes_client import AdminKubernetesError
from app.services.admin_kubernetes_resources import AdminKubernetesResourceService
from app.services.platform_presence import (
    PlatformPresenceDirectoryService,
    PlatformPresenceMonitor,
    PlatformPresenceUser,
)
from app.services.platform_session import (
    PlatformPresenceSummary,
    PlatformSessionUnavailable,
)


class PlatformPresenceRuntimeGate(Protocol):
    async def personal_runtime_ready_user_ids(
        self,
        principal: AdminPrincipal,
    ) -> frozenset[int]:
        ...

router = APIRouter(prefix="/usage", tags=["admin-usage"])

_usage_store = InMemoryAdminUsageStore()


async def get_admin_usage_service(db: AsyncSession = Depends(get_db)) -> AdminUsageService:
    if settings.RUNTIME_CHAT_MODE == "real":
        return AdminUsageService(SqlAlchemyAdminUsageStore(db))
    return AdminUsageService(_usage_store)


@lru_cache(maxsize=1)
def get_platform_presence_monitor() -> PlatformPresenceMonitor:
    return PlatformPresenceMonitor(
        get_platform_session_store(),
        window_seconds=settings.ADMIN_PRESENCE_ONLINE_WINDOW_SECONDS,
        cache_ttl_seconds=settings.ADMIN_PRESENCE_CACHE_TTL_SECONDS,
    )


async def get_platform_presence_runtime_gate(
    service: AdminKubernetesResourceService = Depends(
        get_admin_kubernetes_resource_service,
    ),
) -> PlatformPresenceRuntimeGate:
    return service


def _overview(record: AdminUsageOverviewRecord) -> AdminUsageOverviewSummary:
    return AdminUsageOverviewSummary(
        run_count=record.run_count,
        active_run_count=record.active_run_count,
        model_call_count=record.model_call_count,
        audit_log_count=record.audit_log_count,
        error_count=record.error_count,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        total_tokens=record.total_tokens,
        average_latency_ms=record.average_latency_ms,
    )


def _runtime_workspace(record: AdminRuntimeWorkspaceUsageRecord) -> AdminRuntimeWorkspaceUsageSummary:
    return AdminRuntimeWorkspaceUsageSummary(
        runtime_workspace_id=record.runtime_workspace_id,
        user_id=record.user_id,
        run_count=record.run_count,
        active_run_count=record.active_run_count,
        model_call_count=record.model_call_count,
        error_count=record.error_count,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        total_tokens=record.total_tokens,
        average_latency_ms=record.average_latency_ms,
        last_error=record.last_error,
    )


def _model(record: AdminModelUsageRecord) -> AdminModelUsageSummary:
    return AdminModelUsageSummary(
        model_entry_id=record.model_entry_id,
        model_display_name=record.model_display_name,
        provider_id=record.provider_id,
        model_call_count=record.model_call_count,
        error_count=record.error_count,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        total_tokens=record.total_tokens,
        average_latency_ms=record.average_latency_ms,
    )


def _recent_error(record: AdminUsageRecentErrorRecord) -> AdminUsageRecentErrorSummary:
    return AdminUsageRecentErrorSummary(
        source=record.source,
        runtime_workspace_id=record.runtime_workspace_id,
        runtime_run_id=record.runtime_run_id,
        model_entry_id=record.model_entry_id,
        action=record.action,
        error_code=record.error_code,
        message=record.message,
        created_at=record.created_at,
    )


def _presence_group(
    users: tuple[PlatformPresenceUser, ...],
) -> AdminPresenceUserGroup:
    return AdminPresenceUserGroup(
        count=len(users),
        users=[user.display_name for user in users],
    )


def _require_personal_runtime(
    presence: PlatformPresenceSummary,
    ready_user_ids: frozenset[int],
) -> PlatformPresenceSummary:
    return PlatformPresenceSummary(
        web_user_ids=presence.web_user_ids & ready_user_ids,
        desktop_user_ids=presence.desktop_user_ids & ready_user_ids,
        window_seconds=presence.window_seconds,
        observed_at=presence.observed_at,
    )


@router.get("/summary", response_model=AdminUsageSummaryResponse)
async def get_admin_usage_summary(
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminUsageService = Depends(get_admin_usage_service),
) -> AdminUsageSummaryResponse:
    summary = await service.summary(principal)
    return AdminUsageSummaryResponse(
        overview=_overview(summary.overview),
        runtime_workspaces=[_runtime_workspace(record) for record in summary.runtime_workspaces],
        models=[_model(record) for record in summary.models],
        recent_errors=[_recent_error(record) for record in summary.recent_errors],
    )


@router.get("/presence", response_model=AdminPresenceSummaryResponse)
async def get_admin_usage_presence(
    principal: AdminPrincipal = Depends(get_admin_principal),
    monitor: PlatformPresenceMonitor = Depends(get_platform_presence_monitor),
    runtime_gate: PlatformPresenceRuntimeGate = Depends(
        get_platform_presence_runtime_gate,
    ),
    db: AsyncSession = Depends(get_db),
) -> AdminPresenceSummaryResponse:
    try:
        summary = await monitor.summary(org_id=principal.org_id)
    except PlatformSessionUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "session_store_unavailable",
                "message": "authentication session service is temporarily unavailable",
            },
        ) from exc

    try:
        ready_user_ids = await runtime_gate.personal_runtime_ready_user_ids(principal)
    except AdminKubernetesError as exc:
        _raise_http(exc)
        raise

    summary = _require_personal_runtime(summary, ready_user_ids)
    dashboard = await PlatformPresenceDirectoryService(db).dashboard(
        summary,
        org_id=principal.org_id,
    )

    return AdminPresenceSummaryResponse(
        web_online=_presence_group(dashboard.web_users),
        desktop_online=_presence_group(dashboard.desktop_users),
        daily_logins=_presence_group(dashboard.daily_login_users),
        window_seconds=dashboard.window_seconds,
        daily_login_since=dashboard.daily_login_since,
        observed_at=dashboard.observed_at,
        source="platform-session-user-directory",
    )
