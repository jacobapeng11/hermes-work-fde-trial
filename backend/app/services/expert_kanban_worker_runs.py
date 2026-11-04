from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.hermes_runtime.projection import ProfileConfigRenderer
from app.models import (
    ExpertKanbanCollaboration,
    ExpertKanbanCollaborationTask,
    RuntimeAgentProfile,
    RuntimeKanbanLeaseEvent,
    RuntimeRun,
)
from app.schemas.runtime_kanban import RuntimeKanbanLeaseEventRequest
from app.services.runtime_agent_profiles import executable_runtime_agent_profile_clauses
from app.services.runtime_models import SqlAlchemyRuntimeModelStore


_ACTIVE_STATUSES = ("accepted", "pending", "running")
_TERMINAL_COLLABORATION_STATES = (
    "delivered",
    "delivery_failed",
    "canceled",
)


class ExpertKanbanWorkerRunService:
    """Authorize native Kanban expert workers without weakening model policy.

    Hermes' standalone Kanban workers do not belong to a Web chat Run and do
    not send ``X-Hermes-Run-Id``.  A task-scoped RuntimeRun therefore provides
    the existing model proxy with the same platform-owned identity, catalog
    selection, metering and audit boundary used by every other model call.
    """

    SOURCE_TYPE = "expert_kanban_worker"

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def observe_runtime_event(
        self,
        *,
        runtime_workspace_id: int,
        event: RuntimeKanbanLeaseEventRequest,
        now: datetime | None = None,
    ) -> str | None:
        if not event.task_id:
            return None
        board = str(event.board or "default")
        now = now or datetime.now(timezone.utc)
        run = await self.ensure_for_task(
            runtime_workspace_id=runtime_workspace_id,
            board=board,
            task_id=str(event.task_id),
            assignee=event.assignee,
            now=now,
        )
        if run is None:
            return None

        if event.event_name == "kanban_task_claimed":
            run.status = "running"
            run.started_at = run.started_at or now
            run.finished_at = None
            run.error_code = None
            run.error_message = None
        elif event.event_name == "kanban_task_completed":
            self._finish_run(run, status="completed", now=now)
        elif event.event_name == "kanban_task_blocked":
            self._finish_run(
                run,
                status="failed",
                now=now,
                error_code="expert_kanban_task_blocked",
            )
        await self.session.commit()
        return str(run.runtime_run_id)

    async def ensure_for_task(
        self,
        *,
        runtime_workspace_id: int,
        board: str,
        task_id: str,
        assignee: str | None,
        now: datetime | None = None,
    ) -> RuntimeRun | None:
        """Create the one deterministic worker Run for a bound expert task."""
        task_row = (
            await self.session.execute(
                select(
                    ExpertKanbanCollaborationTask,
                    ExpertKanbanCollaboration,
                )
                .join(
                    ExpertKanbanCollaboration,
                    ExpertKanbanCollaboration.id
                    == ExpertKanbanCollaborationTask.collaboration_id,
                )
                .where(
                    ExpertKanbanCollaborationTask.runtime_workspace_id
                    == runtime_workspace_id,
                    ExpertKanbanCollaborationTask.board == board,
                    ExpertKanbanCollaborationTask.task_id == task_id,
                )
            )
        ).first()
        if task_row is None:
            return None
        task, collaboration = task_row
        runtime_run_id = self.runtime_run_id(
            runtime_workspace_id=runtime_workspace_id,
            board=board,
            task_id=task_id,
        )
        existing = await self.session.scalar(
            select(RuntimeRun).where(
                RuntimeRun.runtime_run_id == runtime_run_id
            )
        )
        if existing is not None:
            if assignee:
                profile = await self._expert_profile(
                    collaboration=collaboration,
                    assignee=assignee,
                )
                if profile is not None:
                    existing.request_payload = self._request_payload(
                        existing.request_payload,
                        collaboration=collaboration,
                        task=task,
                        profile=profile,
                    )
            return existing

        if not assignee:
            return None
        profile = await self._expert_profile(
            collaboration=collaboration,
            assignee=assignee,
        )
        if profile is None:
            return None
        model_entry_id = await self._platform_default_model_entry_id(
            org_id=int(collaboration.org_id),
            user_id=int(collaboration.user_id),
        )
        if model_entry_id is None:
            return None
        now = now or datetime.now(timezone.utc)
        run = RuntimeRun(
            org_id=int(collaboration.org_id),
            user_id=int(collaboration.user_id),
            runtime_workspace_id=runtime_workspace_id,
            # A Kanban worker is intentionally not a chat turn. Keeping both
            # session identities null prevents it from blocking user sends,
            # model switching, active-run replay or the chat SSE lifecycle.
            session_id=None,
            runtime_session_id=None,
            runtime_run_id=runtime_run_id,
            model_entry_id=model_entry_id,
            status="accepted",
            source_type=self.SOURCE_TYPE,
            request_payload=self._request_payload(
                None,
                collaboration=collaboration,
                task=task,
                profile=profile,
            ),
            started_at=now,
        )
        self.session.add(run)
        await self.session.commit()
        await self.session.refresh(run)
        return run

    async def ensure_pending_for_collaborations(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> int:
        """Recover task authorization after Backend restart or hook failure."""
        tasks = (
            await self.session.execute(
                select(
                    ExpertKanbanCollaborationTask,
                    ExpertKanbanCollaboration,
                )
                .join(
                    ExpertKanbanCollaboration,
                    ExpertKanbanCollaboration.id
                    == ExpertKanbanCollaborationTask.collaboration_id,
                )
                .where(
                    ExpertKanbanCollaboration.state.not_in(
                        _TERMINAL_COLLABORATION_STATES
                    )
                )
                .order_by(ExpertKanbanCollaborationTask.id)
                .limit(max(1, int(limit)))
            )
        ).all()
        created = 0
        for task, _collaboration in tasks:
            runtime_run_id = self.runtime_run_id(
                runtime_workspace_id=int(task.runtime_workspace_id),
                board=str(task.board),
                task_id=str(task.task_id),
            )
            if await self.session.scalar(
                select(RuntimeRun.id).where(
                    RuntimeRun.runtime_run_id == runtime_run_id
                )
            ) is not None:
                continue
            assignee = await self.session.scalar(
                select(RuntimeKanbanLeaseEvent.assignee)
                .where(
                    RuntimeKanbanLeaseEvent.runtime_workspace_id
                    == task.runtime_workspace_id,
                    RuntimeKanbanLeaseEvent.board == task.board,
                    RuntimeKanbanLeaseEvent.task_id == task.task_id,
                    RuntimeKanbanLeaseEvent.assignee.is_not(None),
                )
                .order_by(RuntimeKanbanLeaseEvent.id.desc())
                .limit(1)
            )
            run = await self.ensure_for_task(
                runtime_workspace_id=int(task.runtime_workspace_id),
                board=str(task.board),
                task_id=str(task.task_id),
                assignee=str(assignee) if assignee else None,
                now=now,
            )
            if run is not None:
                created += 1
        return created

    async def finish_task(
        self,
        *,
        runtime_workspace_id: int,
        board: str,
        task_id: str,
        task_status: str,
        now: datetime | None = None,
    ) -> bool:
        """Converge worker authorization from Hermes' authoritative Board."""
        run = await self.session.scalar(
            select(RuntimeRun).where(
                RuntimeRun.runtime_run_id
                == self.runtime_run_id(
                    runtime_workspace_id=runtime_workspace_id,
                    board=board,
                    task_id=task_id,
                )
            )
        )
        if run is None:
            return False
        normalized = task_status.strip().lower()
        now = now or datetime.now(timezone.utc)
        if normalized == "done":
            self._finish_run(run, status="completed", now=now)
        elif normalized in {"blocked", "archived"}:
            self._finish_run(
                run,
                status="failed",
                now=now,
                error_code=f"expert_kanban_task_{normalized}",
            )
        else:
            return False
        await self.session.commit()
        return True

    @classmethod
    def runtime_run_id(
        cls,
        *,
        runtime_workspace_id: int,
        board: str,
        task_id: str,
    ) -> str:
        digest = hashlib.sha256(
            f"{runtime_workspace_id}\0{board}\0{task_id}".encode("utf-8")
        ).hexdigest()[:24]
        return f"kw-{runtime_workspace_id}-{digest}"

    async def _expert_profile(
        self,
        *,
        collaboration: ExpertKanbanCollaboration,
        assignee: str,
    ) -> RuntimeAgentProfile | None:
        return await self.session.scalar(
            select(RuntimeAgentProfile).where(
                RuntimeAgentProfile.org_id == collaboration.org_id,
                RuntimeAgentProfile.user_id == collaboration.user_id,
                RuntimeAgentProfile.runtime_workspace_id
                == collaboration.runtime_workspace_id,
                RuntimeAgentProfile.hermes_profile_name == assignee,
                RuntimeAgentProfile.profile_kind == "expert",
                *executable_runtime_agent_profile_clauses(),
            )
        )

    async def _platform_default_model_entry_id(
        self,
        *,
        org_id: int,
        user_id: int,
    ) -> int | None:
        visible_models = await SqlAlchemyRuntimeModelStore(
            self.session
        ).list_visible_models(org_id=org_id, user_id=user_id)
        default_model, _provider_models = (
            ProfileConfigRenderer.resolve_provider_models(
                default_model_id=None,
                visible_models=tuple(
                    {
                        "model_entry_id": model.model_entry_id,
                        "context_length": model.context_length,
                        "tags": list(model.tags or []),
                        "is_default": model.is_default,
                    }
                    for model in visible_models
                ),
            )
        )
        return int(default_model) if default_model != "session" else None

    @staticmethod
    def _request_payload(
        current: dict[str, Any] | None,
        *,
        collaboration: ExpertKanbanCollaboration,
        task: ExpertKanbanCollaborationTask,
        profile: RuntimeAgentProfile,
    ) -> dict[str, Any]:
        return {
            **dict(current or {}),
            "kind": ExpertKanbanWorkerRunService.SOURCE_TYPE,
            "collaboration_public_id": str(collaboration.public_id),
            "board": str(task.board),
            "task_id": str(task.task_id),
            "expert_profile_id": int(profile.id),
            "expert_profile_name": str(profile.hermes_profile_name),
            "expert_display_name": str(profile.display_name),
        }

    @staticmethod
    def _finish_run(
        run: RuntimeRun,
        *,
        status: str,
        now: datetime,
        error_code: str | None = None,
    ) -> None:
        run.status = status
        run.finished_at = now
        run.error_code = error_code
        run.error_message = error_code
        run.response_summary = {
            **dict(run.response_summary or {}),
            "task_status": "done" if status == "completed" else "blocked",
        }
