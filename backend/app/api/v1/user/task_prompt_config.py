from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.database import get_db
from app.schemas.task_prompt_config import TaskPromptConfigState
from app.services.runtime_workspace import Principal
from app.services.task_prompt_config import TaskPromptConfigService

router = APIRouter(prefix="/task-prompt-config", tags=["user-task-prompt-config"])


async def get_task_prompt_config_service(
    db: AsyncSession = Depends(get_db),
) -> TaskPromptConfigService:
    return TaskPromptConfigService(db)


@router.get("", response_model=TaskPromptConfigState)
async def get_task_prompt_config(
    principal: Principal = Depends(get_principal),
    service: TaskPromptConfigService = Depends(get_task_prompt_config_service),
) -> TaskPromptConfigState:
    return await service.get_config(org_id=principal.org_id)
