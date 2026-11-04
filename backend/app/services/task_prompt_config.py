from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TaskPromptConfig
from app.schemas.task_prompt_config import (
    TaskPromptConfigDocument,
    TaskPromptConfigSaveResponse,
    TaskPromptConfigState,
)


class TaskPromptConfigConflictError(RuntimeError):
    """The caller edited an obsolete task-prompt configuration revision."""


class TaskPromptConfigService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_config(self, *, org_id: int) -> TaskPromptConfigState:
        row = await self._row(org_id=org_id)
        if row is None:
            return TaskPromptConfigState(revision=0, config=None)
        return TaskPromptConfigState(
            revision=row.revision,
            config=TaskPromptConfigDocument.model_validate(row.config),
        )

    async def save_config(
        self,
        *,
        org_id: int,
        updated_by_user_id: int,
        expected_revision: int,
        config: TaskPromptConfigDocument,
    ) -> TaskPromptConfigSaveResponse:
        row = await self._row(org_id=org_id, for_update=True)
        current_revision = 0 if row is None else row.revision
        if expected_revision != current_revision:
            raise TaskPromptConfigConflictError(
                "task prompt configuration was updated by another administrator"
            )

        normalized = config.model_dump()
        if row is None:
            row = TaskPromptConfig(
                org_id=org_id,
                config=normalized,
                revision=1,
                updated_by_user_id=updated_by_user_id,
            )
            self.db.add(row)
        else:
            row.config = normalized
            row.revision += 1
            row.updated_by_user_id = updated_by_user_id

        await self.db.commit()
        await self.db.refresh(row)
        return TaskPromptConfigSaveResponse(
            revision=row.revision,
            config=TaskPromptConfigDocument.model_validate(row.config),
        )

    async def _row(self, *, org_id: int, for_update: bool = False) -> TaskPromptConfig | None:
        statement = select(TaskPromptConfig).where(TaskPromptConfig.org_id == org_id)
        if for_update:
            statement = statement.with_for_update()
        result = await self.db.execute(statement)
        return result.scalar_one_or_none()
