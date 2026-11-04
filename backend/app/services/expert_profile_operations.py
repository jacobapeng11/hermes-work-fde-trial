"""Durable operation journal for expert Profile commands."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExpertProfileOperation, RuntimeAgentProfile


class ExpertProfileOperationError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


async def fail_interrupted_expert_profile_operations(db: AsyncSession) -> int:
    """Fail mutable operations left running by a Backend restart."""

    operations = (
        (
            await db.execute(
                select(ExpertProfileOperation)
                .where(
                    ExpertProfileOperation.status == "running",
                    ExpertProfileOperation.step != "mcp_activation_waiting",
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not operations:
        return 0
    now = datetime.now(timezone.utc)
    for operation in operations:
        operation.status = "failed"
        operation.active_profile_key = None
        operation.error_code = "backend_interrupted"
        operation.error_message = "backend restarted before the expert operation completed"
        operation.finished_at = now
    await db.commit()
    return len(operations)


class ExpertProfileOperationService:
    """Serialize commands per Profile and record durable progress/error state."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def begin(
        self,
        *,
        profile: RuntimeAgentProfile,
        action: str,
        rollback_snapshot: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> ExpertProfileOperation:
        active = await self.db.scalar(
            select(ExpertProfileOperation.id).where(
                ExpertProfileOperation.active_profile_key == int(profile.id),
                ExpertProfileOperation.status == "running",
            )
        )
        if active is not None:
            raise ExpertProfileOperationError(
                "expert_operation_in_progress", status_code=409
            )
        operation = ExpertProfileOperation(
            operation_id=str(uuid4()),
            org_id=int(profile.org_id),
            user_id=int(profile.user_id),
            runtime_workspace_id=int(profile.runtime_workspace_id),
            runtime_agent_profile_id=int(profile.id),
            active_profile_key=int(profile.id),
            action=action,
            status="running",
            step="prepared",
            rollback_snapshot=rollback_snapshot,
            started_at=datetime.now(timezone.utc),
        )
        self.db.add(operation)
        try:
            if commit:
                await self.db.commit()
            else:
                await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise ExpertProfileOperationError(
                "expert_operation_in_progress", status_code=409
            ) from exc
        if commit:
            await self.db.refresh(operation)
        return operation

    @staticmethod
    def step(operation: ExpertProfileOperation, value: str) -> None:
        operation.step = value

    @staticmethod
    def succeeded(
        operation: ExpertProfileOperation,
        *,
        step: str = "verified",
    ) -> None:
        operation.status = "succeeded"
        operation.step = step
        operation.active_profile_key = None
        operation.error_code = None
        operation.error_message = None
        operation.finished_at = datetime.now(timezone.utc)

    @staticmethod
    def failed(
        operation: ExpertProfileOperation,
        *,
        error_code: str,
        error: BaseException,
        step: str,
    ) -> None:
        operation.status = "failed"
        operation.step = step
        operation.active_profile_key = None
        operation.error_code = error_code
        operation.error_message = str(error)[:2000]
        operation.finished_at = datetime.now(timezone.utc)


__all__ = [
    "ExpertProfileOperationError",
    "ExpertProfileOperationService",
    "fail_interrupted_expert_profile_operations",
]
