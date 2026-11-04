from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import case, func, select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ExpertKanbanCollaboration,
    ExpertKanbanCollaborationTask,
    RuntimeAgentProfile,
    RuntimeKanbanLeaseEvent,
    RuntimeRun,
    RuntimeSession,
)
from app.schemas.runtime_kanban import RuntimeKanbanLeaseEventRequest
from app.schemas.expert_collaborations import (
    ExpertCollaborationListResponse,
    ExpertCollaborationSummary,
)
from app.services.runtime_agent_profiles import executable_runtime_agent_profile_clauses


logger = logging.getLogger(__name__)
# The Kanban create hook is emitted after the tool call has been persisted;
# the short Web Run can already be terminal by the time Backend receives the
# event.  A completed origin is still eligible for exactly this binding while
# session/profile/source/ordering constraints below prevent cross-run binding.
_ACTIVE_RUN_STATUSES = ("accepted", "pending", "running", "completed")
_TERMINAL_COLLABORATION_STATES = (
    "delivered",
    "delivery_failed",
    "canceled",
)


class ExpertCollaborationService:
    """Bind authenticated Kanban events to their originating Web chat Run."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def observe_runtime_event(
        self,
        *,
        runtime_workspace_id: int,
        event: RuntimeKanbanLeaseEventRequest,
        now: datetime | None = None,
    ) -> str | None:
        now = now or datetime.now(timezone.utc)
        if (
            event.event_name == "post_tool_call"
            and event.tool_name == "kanban_create"
            and event.outcome == "ok"
        ):
            return await self._bind_create(
                runtime_workspace_id=runtime_workspace_id,
                event=event,
                now=now,
            )
        if event.task_id and event.event_name in {
            "kanban_task_completed",
            "kanban_task_blocked",
            "kanban_task_claimed",
        }:
            await self._mark_task_due(
                runtime_workspace_id=runtime_workspace_id,
                board=event.board or "default",
                task_id=event.task_id,
                now=now,
            )
        return None

    async def bind_pending_create_events(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> int:
        """Recover create receipts that were committed before binding failed."""
        now = now or datetime.now(timezone.utc)
        events = (
            await self.session.execute(
                select(RuntimeKanbanLeaseEvent)
                .where(
                    RuntimeKanbanLeaseEvent.event_name == "post_tool_call",
                    RuntimeKanbanLeaseEvent.tool_name == "kanban_create",
                    RuntimeKanbanLeaseEvent.outcome == "ok",
                    RuntimeKanbanLeaseEvent.task_id.is_not(None),
                    RuntimeKanbanLeaseEvent.runtime_session_id.is_not(None),
                )
                .order_by(RuntimeKanbanLeaseEvent.id.desc())
                .limit(max(1, int(limit)))
            )
        ).scalars().all()
        bound = 0
        for record in reversed(events):
            existing = await self.session.scalar(
                select(ExpertKanbanCollaborationTask.id).where(
                    ExpertKanbanCollaborationTask.runtime_workspace_id
                    == record.runtime_workspace_id,
                    ExpertKanbanCollaborationTask.board
                    == str(record.board or "default"),
                    ExpertKanbanCollaborationTask.task_id == record.task_id,
                )
            )
            if existing is not None:
                continue
            public_id = await self._bind_create(
                runtime_workspace_id=int(record.runtime_workspace_id),
                event=RuntimeKanbanLeaseEventRequest(
                    event_id=str(record.event_id),
                    event_name="post_tool_call",
                    runtime_agent_profile_id=record.runtime_agent_profile_id,
                    tool_name="kanban_create",
                    task_id=record.task_id,
                    board=record.board,
                    outcome=record.outcome,
                    runtime_session_id=record.runtime_session_id,
                    tool_call_id=record.tool_call_id,
                    turn_id=record.turn_id,
                    assignee=record.assignee,
                    profile_name=record.profile_name,
                ),
                now=now,
            )
            if public_id is not None:
                bound += 1
        return bound

    async def _bind_create(
        self,
        *,
        runtime_workspace_id: int,
        event: RuntimeKanbanLeaseEventRequest,
        now: datetime,
    ) -> str | None:
        if not event.task_id or not event.runtime_session_id:
            return None
        row = (
            await self.session.execute(
                select(RuntimeSession, RuntimeAgentProfile, RuntimeRun)
                .join(
                    RuntimeAgentProfile,
                    RuntimeAgentProfile.id
                    == RuntimeSession.runtime_agent_profile_id,
                )
                .join(RuntimeRun, RuntimeRun.session_id == RuntimeSession.id)
                .where(
                    RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                    RuntimeSession.runtime_session_id == event.runtime_session_id,
                    RuntimeSession.status == "active",
                    RuntimeAgentProfile.profile_kind == "default",
                    *executable_runtime_agent_profile_clauses(),
                    RuntimeRun.status.in_(_ACTIVE_RUN_STATUSES),
                    RuntimeRun.source_type == "web",
                    (
                        RuntimeAgentProfile.id
                        == event.runtime_agent_profile_id
                        if event.runtime_agent_profile_id is not None
                        else RuntimeAgentProfile.profile_kind == "default"
                    ),
                )
                .order_by(RuntimeRun.id.desc())
            )
        ).first()
        if row is None:
            return None
        runtime_session, profile, origin_run = row
        existing_task = await self.session.scalar(
            select(ExpertKanbanCollaborationTask.id).where(
                ExpertKanbanCollaborationTask.runtime_workspace_id
                == runtime_workspace_id,
                ExpertKanbanCollaborationTask.board
                == (event.board or "default"),
                ExpertKanbanCollaborationTask.task_id == event.task_id,
            )
        )
        if existing_task is not None:
            collaboration = await self.session.scalar(
                select(ExpertKanbanCollaboration)
                .join(
                    ExpertKanbanCollaborationTask,
                    ExpertKanbanCollaborationTask.collaboration_id
                    == ExpertKanbanCollaboration.id,
                )
                .where(ExpertKanbanCollaborationTask.id == existing_task)
            )
            return str(collaboration.public_id) if collaboration is not None else None

        collaboration = await self.session.scalar(
            select(ExpertKanbanCollaboration).where(
                ExpertKanbanCollaboration.origin_run_id == origin_run.id
            )
        )
        if collaboration is None:
            collaboration = ExpertKanbanCollaboration(
                public_id=f"ec-{uuid4().hex}",
                org_id=int(origin_run.org_id),
                user_id=int(origin_run.user_id),
                runtime_workspace_id=runtime_workspace_id,
                session_id=int(runtime_session.id),
                origin_run_id=int(origin_run.id),
                state="waiting_tasks",
                revision=1,
                next_reconcile_at=now,
            )
            self.session.add(collaboration)
            await self.session.flush()
        elif collaboration.state not in _TERMINAL_COLLABORATION_STATES:
            collaboration.state = "waiting_tasks"
            collaboration.next_reconcile_at = now
            collaboration.revision = int(collaboration.revision) + 1

        self.session.add(
            ExpertKanbanCollaborationTask(
                collaboration_id=int(collaboration.id),
                runtime_workspace_id=runtime_workspace_id,
                board=str(event.board or "default"),
                task_id=str(event.task_id),
                tool_call_id=event.tool_call_id,
            )
        )
        await self.session.commit()
        logger.info(
            "expert collaboration task bound collaboration_id=%s task_id=%s profile_id=%s",
            collaboration.public_id,
            event.task_id,
            profile.id,
        )
        return str(collaboration.public_id)

    async def _mark_task_due(
        self,
        *,
        runtime_workspace_id: int,
        board: str,
        task_id: str,
        now: datetime,
    ) -> None:
        collaboration = await self.session.scalar(
            select(ExpertKanbanCollaboration)
            .join(
                ExpertKanbanCollaborationTask,
                ExpertKanbanCollaborationTask.collaboration_id
                == ExpertKanbanCollaboration.id,
            )
            .where(
                ExpertKanbanCollaborationTask.runtime_workspace_id
                == runtime_workspace_id,
                ExpertKanbanCollaborationTask.board == board,
                ExpertKanbanCollaborationTask.task_id == task_id,
                ExpertKanbanCollaboration.state.not_in(
                    _TERMINAL_COLLABORATION_STATES
                ),
            )
        )
        if collaboration is None:
            return
        collaboration.next_reconcile_at = now
        collaboration.revision = int(collaboration.revision) + 1
        await self.session.commit()

    async def pending_count(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str | None = None,
    ) -> int:
        clauses = [
            ExpertKanbanCollaboration.org_id == org_id,
            ExpertKanbanCollaboration.user_id == user_id,
            ExpertKanbanCollaboration.runtime_workspace_id
            == runtime_workspace_id,
            ExpertKanbanCollaboration.state.not_in(
                _TERMINAL_COLLABORATION_STATES
            ),
        ]
        statement = select(func.count(ExpertKanbanCollaboration.id))
        if runtime_session_id is not None:
            statement = statement.join(
                RuntimeSession,
                RuntimeSession.id == ExpertKanbanCollaboration.session_id,
            )
            clauses.append(
                RuntimeSession.runtime_session_id == runtime_session_id
            )
        value = await self.session.scalar(statement.where(*clauses))
        return int(value or 0)

    async def list_owned(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str | None = None,
        include_recent_terminal: bool = True,
        limit: int = 100,
    ) -> ExpertCollaborationListResponse:
        origin_run = aliased(RuntimeRun)
        final_run = aliased(RuntimeRun)
        task_counts = (
            select(
                ExpertKanbanCollaborationTask.collaboration_id.label(
                    "collaboration_id"
                ),
                func.count(ExpertKanbanCollaborationTask.id).label(
                    "task_count"
                ),
            )
            .group_by(ExpertKanbanCollaborationTask.collaboration_id)
            .subquery()
        )
        clauses = [
            ExpertKanbanCollaboration.org_id == org_id,
            ExpertKanbanCollaboration.user_id == user_id,
            ExpertKanbanCollaboration.runtime_workspace_id
            == runtime_workspace_id,
            RuntimeSession.id == ExpertKanbanCollaboration.session_id,
        ]
        if runtime_session_id is not None:
            clauses.append(
                RuntimeSession.runtime_session_id == runtime_session_id
            )
        if not include_recent_terminal:
            clauses.append(
                ExpertKanbanCollaboration.state.not_in(
                    _TERMINAL_COLLABORATION_STATES
                )
            )
        rows = (
            await self.session.execute(
                select(
                    ExpertKanbanCollaboration,
                    RuntimeSession,
                    origin_run,
                    final_run,
                    func.coalesce(task_counts.c.task_count, 0),
                )
                .join(
                    RuntimeSession,
                    RuntimeSession.id == ExpertKanbanCollaboration.session_id,
                )
                .join(
                    origin_run,
                    origin_run.id == ExpertKanbanCollaboration.origin_run_id,
                )
                .outerjoin(
                    final_run,
                    final_run.id == ExpertKanbanCollaboration.final_run_id,
                )
                .outerjoin(
                    task_counts,
                    task_counts.c.collaboration_id
                    == ExpertKanbanCollaboration.id,
                )
                .where(*clauses)
                .order_by(
                    case(
                        (
                            ExpertKanbanCollaboration.state.not_in(
                                _TERMINAL_COLLABORATION_STATES
                            ),
                            0,
                        ),
                        else_=1,
                    ),
                    ExpertKanbanCollaboration.updated_at.desc(),
                    ExpertKanbanCollaboration.id.desc(),
                )
                .limit(max(1, min(int(limit), 200)))
            )
        ).all()
        summaries = [
            ExpertCollaborationSummary(
                collaboration_id=str(collaboration.public_id),
                runtime_workspace_id=int(collaboration.runtime_workspace_id),
                runtime_session_id=str(runtime_session.runtime_session_id),
                origin_runtime_run_id=str(origin.runtime_run_id),
                final_runtime_run_id=(
                    str(final.runtime_run_id) if final is not None else None
                ),
                state=str(collaboration.state),
                revision=int(collaboration.revision),
                task_count=int(task_count or 0),
                last_error_code=collaboration.last_error_code,
                updated_at=collaboration.updated_at,
            )
            for collaboration, runtime_session, origin, final, task_count in rows
        ]
        return ExpertCollaborationListResponse(
            collaborations=summaries,
            pending_count=await self.pending_count(
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=runtime_session_id,
            ),
        )

    @staticmethod
    def retry_at(now: datetime, attempt_count: int) -> datetime:
        seconds = min(60, max(2, 2 ** min(max(0, attempt_count), 5)))
        return now + timedelta(seconds=seconds)
