from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas.runtime_kanban import (
    RuntimeKanbanLeaseEventRequest,
    RuntimeKanbanLeaseEventResponse,
)
from app.services.runtime_kanban_leases import (
    RuntimeKanbanLeaseError,
    RuntimeKanbanLeaseService,
)
from app.services.expert_collaborations import ExpertCollaborationService
from app.services.expert_kanban_worker_runs import (
    ExpertKanbanWorkerRunService,
)
from app.services.expert_kanban_worker_routes import (
    ExpertKanbanWorkerRouteService,
)


router = APIRouter(prefix="/runtime-kanban/{runtime_workspace_id}", tags=["runtime-kanban"])


def _bearer(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing runtime token",
        )
    return token


@router.post("/events", response_model=RuntimeKanbanLeaseEventResponse)
async def accept_runtime_kanban_event(
    runtime_workspace_id: int,
    payload: RuntimeKanbanLeaseEventRequest,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> RuntimeKanbanLeaseEventResponse:
    try:
        receipt = await RuntimeKanbanLeaseService(
            db,
            temporary_seconds=settings.RUNTIME_KANBAN_TEMPORARY_LEASE_SECONDS,
        ).accept_event(
            runtime_workspace_id=runtime_workspace_id,
            runtime_token=_bearer(authorization),
            event=payload,
        )
        worker_route_ready: bool | None = None
        collaboration_id: str | None = None
        if not receipt.duplicate:
            try:
                collaboration_id = await ExpertCollaborationService(
                    db
                ).observe_runtime_event(
                    runtime_workspace_id=runtime_workspace_id,
                    event=payload,
                )
            except Exception:
                # The authenticated receipt is durable. Startup/periodic
                # recovery binds any missed successful create event later.
                pass
        try:
            if (
                collaboration_id is not None
                or payload.event_name
                in {
                    "kanban_task_claimed",
                    "kanban_task_completed",
                    "kanban_task_blocked",
                }
                or (
                    payload.event_name == "post_tool_call"
                    and payload.tool_name
                    in {"kanban_create", "kanban_heartbeat"}
                    and payload.outcome == "ok"
                )
            ):
                worker_run_id = await ExpertKanbanWorkerRunService(
                    db
                ).observe_runtime_event(
                    runtime_workspace_id=runtime_workspace_id,
                    event=payload,
                )
                if worker_run_id is not None and payload.task_id:
                    routes = ExpertKanbanWorkerRouteService(db)
                    try:
                        board = str(payload.board or "default")
                        if payload.event_name in {
                            "kanban_task_completed",
                            "kanban_task_blocked",
                        }:
                            await routes.revoke_for_task(
                                runtime_workspace_id=runtime_workspace_id,
                                board=board,
                                task_id=payload.task_id,
                            )
                            worker_route_ready = False
                        else:
                            worker_route_ready = (
                                await routes.activate_for_task(
                                    runtime_workspace_id=runtime_workspace_id,
                                    board=board,
                                    task_id=payload.task_id,
                                )
                            )
                    finally:
                        await routes.close()
        except Exception:
            # Route convergence is idempotent and will be retried by duplicate
            # delivery, the next claim, heartbeat, or periodic reconciliation.
            worker_route_ready = False
    except RuntimeKanbanLeaseError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code},
        ) from exc
    return RuntimeKanbanLeaseEventResponse(
        duplicate=receipt.duplicate,
        lease_state=receipt.lease_state,
        worker_route_ready=worker_route_ready,
    )
