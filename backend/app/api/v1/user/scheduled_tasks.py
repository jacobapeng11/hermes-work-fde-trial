from __future__ import annotations

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.user.runtime_workspace import get_principal
from app.database import get_db
from app.repositories.scheduled_task import (
    ScheduledTaskNotFoundError,
    ScheduledTaskRecord,
    ScheduledTaskRepository,
    ScheduledTaskTargetNotFoundError,
    ScheduledTaskWeComTargetNotReadyError,
)
from app.schemas.scheduled_task import (
    CreateScheduledTaskRequest,
    ScheduledTaskListResponse,
    ScheduledTaskSummary,
    ScheduledTaskTriggerResponse,
    UpdateScheduledTaskRequest,
)
from app.services.runtime_workspace import Principal
from app.services.runtime_commands import (
    RuntimeCommandClient,
    RuntimeCommandError,
    load_personal_runtime_command_target,
)
from app.services.scheduled_task_service import (
    ScheduleConflictError,
    ScheduleValidationError,
    ScheduledTaskService,
    ScheduledTaskStateConflictError,
    ScheduledTaskTriggerConflictError,
    ScheduledTaskTriggerMaintenanceError,
)
from app.services.scheduled_task_launcher import ScheduledTaskLauncher
from app.services.scheduled_task_runner import ScheduledTaskRunner


router = APIRouter(prefix="/runtime-workspace", tags=["user-scheduled-tasks"])

# One process-wide registry is shared by Scanner and HTTP trigger.  The
# factory owns no request-scoped state; every Runner creates short-lived DB
# sessions around control-plane reads and Runtime admission.
scheduled_task_runner = ScheduledTaskRunner.for_application()
scheduled_task_launcher = ScheduledTaskLauncher(scheduled_task_runner.run)


async def get_scheduled_task_service(db: AsyncSession = Depends(get_db)) -> ScheduledTaskService:
    """Build the control-plane service only; no Runtime keepalive is needed."""
    return ScheduledTaskService(ScheduledTaskRepository(db))


def get_scheduled_task_launcher() -> ScheduledTaskLauncher:
    return scheduled_task_launcher


async def _cancel_queued_candidate(
    *,
    db: AsyncSession,
    principal: Principal,
    runtime_workspace_id: int,
    runtime_session_id: str,
    task_id: int,
) -> str:
    try:
        target = await load_personal_runtime_command_target(
            db,
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
        )
        client = RuntimeCommandClient()
        try:
            return await client.cancel_scheduled_candidate(
                target=target,
                session_id=runtime_session_id,
                scheduled_task_id=task_id,
            )
        finally:
            await client.close()
    except RuntimeCommandError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="定时任务已更新，但排队候选取消未获 Runtime 确认",
        ) from exc


def _schedule_summary(record: ScheduledTaskRecord) -> str:
    config = record.schedule_config or {}
    if record.schedule_type == "once":
        return "执行一次"
    if record.schedule_type == "interval":
        unit = config.get("unit")
        value = config.get("value")
        label = "分钟" if unit == "minutes" else "小时"
        return f"每 {value} {label}"
    preset = config.get("preset")
    value = config.get("time", "")
    if preset == "daily":
        return f"每天 {value}"
    if preset == "weekly":
        weekday = config.get("weekday")
        names = ("一", "二", "三", "四", "五", "六", "日")
        if isinstance(weekday, int) and 1 <= weekday <= 7:
            return f"每周{names[weekday - 1]} {value}"
    return ""


def _summary(record: ScheduledTaskRecord) -> ScheduledTaskSummary:
    return ScheduledTaskSummary(
        task_id=record.task_id,
        task_name=record.task_name,
        prompt=record.prompt,
        status=record.status,
        schedule_config=record.schedule_config,
        schedule_summary=_schedule_summary(record),
        target_runtime_session_id=record.target_runtime_session_id,
        target_session_title=record.target_session_title,
        target_session_status=record.target_session_status,
        next_run_at=record.next_run_at,
        last_run_at=record.last_run_at,
        last_run_status=record.last_run_status,
        last_error_code=record.last_error_code,
        last_error=record.last_error,
        last_runtime_run_id=record.last_runtime_run_id,
        run_count=record.run_count,
        is_running=record.is_running,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _wecom_target_not_ready(
    exc: ScheduledTaskWeComTargetNotReadyError,
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=exc.code,
    )


@router.get("/{runtime_workspace_id}/scheduled-tasks", response_model=ScheduledTaskListResponse)
async def list_scheduled_tasks(
    runtime_workspace_id: int,
    principal: Principal = Depends(get_principal),
    service: ScheduledTaskService = Depends(get_scheduled_task_service),
) -> ScheduledTaskListResponse:
    try:
        records = await service.list_tasks(principal=principal, runtime_workspace_id=runtime_workspace_id)
    except (ScheduledTaskNotFoundError, ScheduledTaskTargetNotFoundError) as exc:
        raise _not_found(exc) from exc
    return ScheduledTaskListResponse(tasks=[_summary(record) for record in records])


@router.get("/{runtime_workspace_id}/scheduled-tasks/{task_id}", response_model=ScheduledTaskSummary)
async def get_scheduled_task(
    runtime_workspace_id: int,
    task_id: int,
    principal: Principal = Depends(get_principal),
    service: ScheduledTaskService = Depends(get_scheduled_task_service),
) -> ScheduledTaskSummary:
    try:
        record = await service.get_task(
            principal=principal, runtime_workspace_id=runtime_workspace_id, task_id=task_id
        )
    except (ScheduledTaskNotFoundError, ScheduledTaskTargetNotFoundError) as exc:
        raise _not_found(exc) from exc
    return _summary(record)


@router.post("/{runtime_workspace_id}/scheduled-tasks", response_model=ScheduledTaskSummary, status_code=201)
async def create_scheduled_task(
    runtime_workspace_id: int,
    payload: CreateScheduledTaskRequest,
    principal: Principal = Depends(get_principal),
    service: ScheduledTaskService = Depends(get_scheduled_task_service),
) -> ScheduledTaskSummary:
    try:
        record = await service.create_task(
            principal=principal, runtime_workspace_id=runtime_workspace_id, command=payload
        )
    except (ScheduledTaskNotFoundError, ScheduledTaskTargetNotFoundError) as exc:
        raise _not_found(exc) from exc
    except ScheduledTaskWeComTargetNotReadyError as exc:
        raise _wecom_target_not_ready(exc) from exc
    except ScheduleValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except ScheduleConflictError as exc:
        # A create/update payload is invalid when its once timestamp is past.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return _summary(record)


@router.patch("/{runtime_workspace_id}/scheduled-tasks/{task_id}", response_model=ScheduledTaskSummary)
async def update_scheduled_task(
    runtime_workspace_id: int,
    task_id: int,
    payload: UpdateScheduledTaskRequest,
    principal: Principal = Depends(get_principal),
    service: ScheduledTaskService = Depends(get_scheduled_task_service),
) -> ScheduledTaskSummary:
    try:
        record = await service.update_task(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            task_id=task_id,
            command=payload,
        )
    except (ScheduledTaskNotFoundError, ScheduledTaskTargetNotFoundError) as exc:
        raise _not_found(exc) from exc
    except ScheduledTaskWeComTargetNotReadyError as exc:
        raise _wecom_target_not_ready(exc) from exc
    except ScheduleValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except ScheduleConflictError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return _summary(record)


@router.post("/{runtime_workspace_id}/scheduled-tasks/{task_id}/pause", response_model=ScheduledTaskSummary)
async def pause_scheduled_task(
    runtime_workspace_id: int,
    task_id: int,
    principal: Principal = Depends(get_principal),
    service: ScheduledTaskService = Depends(get_scheduled_task_service),
    db: AsyncSession = Depends(get_db),
) -> ScheduledTaskSummary:
    try:
        record = await service.pause_task(
            principal=principal, runtime_workspace_id=runtime_workspace_id, task_id=task_id
        )
        await _cancel_queued_candidate(
            db=db,
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=record.target_runtime_session_id,
            task_id=task_id,
        )
    except (ScheduledTaskNotFoundError, ScheduledTaskTargetNotFoundError) as exc:
        raise _not_found(exc) from exc
    except ScheduledTaskStateConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _summary(record)


@router.post("/{runtime_workspace_id}/scheduled-tasks/{task_id}/resume", response_model=ScheduledTaskSummary)
async def resume_scheduled_task(
    runtime_workspace_id: int,
    task_id: int,
    principal: Principal = Depends(get_principal),
    service: ScheduledTaskService = Depends(get_scheduled_task_service),
) -> ScheduledTaskSummary:
    try:
        record = await service.resume_task(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            task_id=task_id,
            now=datetime.now(timezone.utc),
        )
    except (ScheduledTaskNotFoundError, ScheduledTaskTargetNotFoundError) as exc:
        raise _not_found(exc) from exc
    except ScheduledTaskWeComTargetNotReadyError as exc:
        raise _wecom_target_not_ready(exc) from exc
    except (ScheduledTaskStateConflictError, ScheduleConflictError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ScheduleValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return _summary(record)


@router.delete("/{runtime_workspace_id}/scheduled-tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scheduled_task(
    runtime_workspace_id: int,
    task_id: int,
    principal: Principal = Depends(get_principal),
    service: ScheduledTaskService = Depends(get_scheduled_task_service),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        record = await service.get_task(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            task_id=task_id,
        )
        await service.delete_task(
            principal=principal, runtime_workspace_id=runtime_workspace_id, task_id=task_id
        )
        await _cancel_queued_candidate(
            db=db,
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=record.target_runtime_session_id,
            task_id=task_id,
        )
    except (ScheduledTaskNotFoundError, ScheduledTaskTargetNotFoundError) as exc:
        raise _not_found(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{runtime_workspace_id}/scheduled-tasks/{task_id}/trigger",
    response_model=ScheduledTaskTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_scheduled_task(
    runtime_workspace_id: int,
    task_id: int,
    principal: Principal = Depends(get_principal),
    service: ScheduledTaskService = Depends(get_scheduled_task_service),
    launcher: ScheduledTaskLauncher = Depends(get_scheduled_task_launcher),
) -> ScheduledTaskTriggerResponse:
    try:
        record = await service.trigger_task(
            principal=principal,
            runtime_workspace_id=runtime_workspace_id,
            task_id=task_id,
            launcher=launcher,
        )
    except (ScheduledTaskNotFoundError, ScheduledTaskTargetNotFoundError) as exc:
        raise _not_found(exc) from exc
    except ScheduledTaskWeComTargetNotReadyError as exc:
        raise _wecom_target_not_ready(exc) from exc
    except ScheduledTaskTriggerConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ScheduledTaskTriggerMaintenanceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="系统维护中，请稍后重试",
        ) from exc
    return ScheduledTaskTriggerResponse(
        task_id=record.task_id,
        target_runtime_session_id=record.target_runtime_session_id,
        accepted_at=datetime.now(timezone.utc),
    )
