from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin.deps import get_admin_principal
from app.database import get_db
from app.schemas.task_prompt_config import (
    TaskPromptConfigSaveRequest,
    TaskPromptConfigSaveResponse,
    TaskPromptConfigState,
)
from app.services.admin_audit import AdminAuditLogService, SqlAlchemyAdminAuditLogStore
from app.services.admin_auth import AdminPrincipal
from app.services.task_prompt_config import (
    TaskPromptConfigConflictError,
    TaskPromptConfigService,
)

router = APIRouter(prefix="/task-prompt-config", tags=["admin-task-prompt-config"])


async def get_task_prompt_config_service(
    db: AsyncSession = Depends(get_db),
) -> TaskPromptConfigService:
    return TaskPromptConfigService(db)


async def get_admin_audit_log_service(
    db: AsyncSession = Depends(get_db),
) -> AdminAuditLogService:
    return AdminAuditLogService(SqlAlchemyAdminAuditLogStore(db))


@router.get("", response_model=TaskPromptConfigState)
async def get_task_prompt_config(
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: TaskPromptConfigService = Depends(get_task_prompt_config_service),
) -> TaskPromptConfigState:
    return await service.get_config(org_id=principal.org_id)


@router.put("", response_model=TaskPromptConfigSaveResponse)
async def save_task_prompt_config(
    payload: TaskPromptConfigSaveRequest,
    principal: AdminPrincipal = Depends(get_admin_principal),
    service: TaskPromptConfigService = Depends(get_task_prompt_config_service),
    audit_service: AdminAuditLogService = Depends(get_admin_audit_log_service),
) -> TaskPromptConfigSaveResponse:
    try:
        result = await service.save_config(
            org_id=principal.org_id,
            updated_by_user_id=principal.user_id,
            expected_revision=payload.revision,
            config=payload.config,
        )
    except TaskPromptConfigConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="task prompt configuration was updated by another administrator",
        ) from exc

    await audit_service.record(
        principal=principal,
        action="admin.task_prompt_config.published",
        target_type="task_prompt_config",
        target_id=principal.org_id,
        detail={
            "revision": result.revision,
            "tab_count": len(result.config.tabs),
            "topic_count": sum(len(tab.topics) for tab in result.config.tabs),
            "task_count": sum(
                len(topic.tasks)
                for tab in result.config.tabs
                for topic in tab.topics
            ),
        },
    )
    return result
