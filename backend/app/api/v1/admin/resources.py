from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.deps import get_admin_principal
from app.config import settings
from app.database import get_db
from app.schemas.admin import AdminRuntimeWorkspaceResourceSummary
from app.services.admin_auth import AdminPrincipal
from app.services.admin_resources import (
    AdminResourceViewService,
    AdminRuntimeWorkspaceResourceFilter,
    AdminRuntimeWorkspaceResourceRecord,
    InMemoryAdminResourceViewStore,
    SqlAlchemyAdminResourceViewStore,
)

router = APIRouter(prefix="/resources", tags=["admin-resources"])

_resource_store = InMemoryAdminResourceViewStore()


async def get_admin_resource_view_service(db: AsyncSession = Depends(get_db)) -> AdminResourceViewService:
    if settings.RUNTIME_CHAT_MODE == "real":
        return AdminResourceViewService(SqlAlchemyAdminResourceViewStore(db))
    return AdminResourceViewService(_resource_store)


def _resource_summary(record: AdminRuntimeWorkspaceResourceRecord) -> AdminRuntimeWorkspaceResourceSummary:
    return AdminRuntimeWorkspaceResourceSummary(
        runtime_workspace_id=record.runtime_workspace_id,
        cloud_profile_id=record.cloud_profile_id,
        resource_key=record.resource_key,
        runtime_kind=record.runtime_kind,
        application_agent_id=record.application_agent_id,
        org_id=record.org_id,
        user_id=record.user_id,
        employee_no=record.employee_no,
        user_name=record.user_name,
        runtime_name=record.runtime_name,
        profile_status=record.profile_status,
        binding_status=record.binding_status,
        runtime_status=record.runtime_status,
        default_model_entry_id=record.default_model_entry_id,
        default_model_display_name=record.default_model_display_name,
        session_count=record.session_count,
        run_count=record.run_count,
        active_run_count=record.active_run_count,
        installed_expert_count=record.installed_expert_count,
        installed_skill_count=record.installed_skill_count,
        installed_mcp_count=record.installed_mcp_count,
        installed_knowledge_base_count=record.installed_knowledge_base_count,
        pending_reload=record.pending_reload,
        has_runtime_binding=record.has_runtime_binding,
        kservice_name=record.kservice_name,
        latest_error=record.latest_error,
    )


@router.get("/runtime-workspaces", response_model=list[AdminRuntimeWorkspaceResourceSummary])
async def list_runtime_workspace_resources(
    employee_no: str | None = None,
    user_name: str | None = None,
    runtime_status: str | None = None,
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: AdminResourceViewService = Depends(get_admin_resource_view_service),
) -> list[AdminRuntimeWorkspaceResourceSummary]:
    filters = AdminRuntimeWorkspaceResourceFilter(
        employee_no=employee_no.strip() if employee_no and employee_no.strip() else None,
        user_name=user_name.strip() if user_name and user_name.strip() else None,
        runtime_status=runtime_status.strip() if runtime_status and runtime_status.strip() else None,
    )
    return [_resource_summary(record) for record in await service.list_runtime_workspaces(principal, filters=filters)]
