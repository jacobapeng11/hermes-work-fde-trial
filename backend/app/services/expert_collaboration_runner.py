from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import (
    ExpertKanbanCollaboration,
    RuntimeRun,
    RuntimeSession,
)
from app.services.runtime_admission import RuntimeAdmissionError
from app.services.runtime_chat import (
    RuntimeChatActiveRunError,
    RuntimeChatRequestIntent,
    RuntimeChatService,
    SqlAlchemyRuntimeChatStore,
)
from app.services.runtime_infrastructure import (
    build_runtime_infrastructure,
)
from app.services.runtime_internal_turns import (
    RuntimeInternalTurnError,
    execute_personal_internal_turn,
    resolve_personal_internal_turn,
)
from app.services.runtime_workspace import Principal


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Dispatch:
    collaboration_id: int
    public_id: str
    org_id: int
    user_id: int
    runtime_workspace_id: int
    session_id: int
    runtime_session_id: str
    origin_runtime_run_id: str
    original_message: str
    result_snapshot: dict[str, Any]


def _claim_context_statement(*, origin_run_id: int, session_id: int):
    return (
        select(RuntimeSession, RuntimeRun)
        .join(
            RuntimeRun,
            RuntimeRun.session_id == RuntimeSession.id,
        )
        .where(
            RuntimeRun.id == origin_run_id,
            RuntimeSession.id == session_id,
            RuntimeSession.status == "active",
        )
    )


class ExpertCollaborationRunner:
    """Launch summary Runs without retaining an HTTP request or DB session."""

    def __init__(
        self,
        *,
        broker: Any | None = None,
        lease_seconds: int | None = None,
        max_attempts: int | None = None,
    ) -> None:
        del broker
        self.lease_seconds = int(
            lease_seconds or settings.EXPERT_COLLABORATION_LEASE_SECONDS
        )
        self.max_attempts = int(
            max_attempts
            or settings.EXPERT_COLLABORATION_MAX_SYNTHESIS_ATTEMPTS
        )
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._stopping = False

    def start(self) -> None:
        self._stopping = False

    def launch(self, collaboration_id: int) -> bool:
        if self._stopping or collaboration_id in self._tasks:
            return False
        task = asyncio.create_task(self._execute(collaboration_id))
        self._tasks[collaboration_id] = task
        task.add_done_callback(
            lambda completed, key=collaboration_id: self._task_done(key, completed)
        )
        return True

    def _task_done(self, collaboration_id: int, task: asyncio.Task[None]) -> None:
        self._tasks.pop(collaboration_id, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "expert collaboration runner escaped collaboration_id=%s error_type=%s",
                collaboration_id,
                type(error).__name__,
            )

    async def stop(self) -> None:
        self._stopping = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _execute(self, collaboration_id: int) -> None:
        dispatch = await self._claim(collaboration_id)
        if dispatch is None:
            return
        created = None
        try:
            endpoint = await self._activate_runtime(dispatch)
            created, history = await self._create_run(dispatch)
            await self._bind_final_run(
                collaboration_id=dispatch.collaboration_id,
                runtime_run_id=created.runtime_run_id,
            )
            terminal_state = await self._execute_run(
                dispatch=dispatch,
                created=created,
                endpoint=endpoint,
            )
            await self._finish_from_run(
                collaboration_id=dispatch.collaboration_id,
                runtime_run_id=created.runtime_run_id,
                terminal_state=terminal_state,
            )
        except asyncio.CancelledError:
            raise
        except RuntimeChatActiveRunError:
            await self._retry(
                dispatch.collaboration_id,
                error_code="session_busy",
                clear_final_run=created is None,
            )
        except RuntimeAdmissionError:
            await self._retry(
                dispatch.collaboration_id,
                error_code="runtime_not_ready",
                clear_final_run=created is None,
            )
        except Exception as exc:
            logger.warning(
                "expert collaboration synthesis failed collaboration_id=%s error_type=%s",
                dispatch.collaboration_id,
                type(exc).__name__,
            )
            if created is None:
                await self._retry(
                    dispatch.collaboration_id,
                    error_code=type(exc).__name__.lower()[:100],
                    clear_final_run=True,
                )
            else:
                await self._attention_required(
                    dispatch.collaboration_id,
                    error_code=(
                        exc.code
                        if isinstance(exc, RuntimeInternalTurnError)
                        else type(exc).__name__.lower()[:100]
                    ),
                )

    async def _claim(self, collaboration_id: int) -> _Dispatch | None:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            collaboration = (
                await db.execute(
                    select(ExpertKanbanCollaboration)
                    .where(
                        ExpertKanbanCollaboration.id == collaboration_id,
                        ExpertKanbanCollaboration.state == "synthesis_pending",
                        ExpertKanbanCollaboration.next_reconcile_at <= now,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if collaboration is None:
                return None
            row = (
                await db.execute(
                    _claim_context_statement(
                        origin_run_id=int(collaboration.origin_run_id),
                        session_id=int(collaboration.session_id),
                    )
                )
            ).first()
            if row is None:
                collaboration.state = "canceled"
                collaboration.revision = int(collaboration.revision) + 1
                collaboration.lease_expires_at = None
                await db.commit()
                return None
            session, origin_run = row
            payload = dict(origin_run.request_payload or {})
            collaboration.state = "synthesizing"
            collaboration.attempt_count = int(collaboration.attempt_count) + 1
            collaboration.lease_expires_at = now + timedelta(
                seconds=self.lease_seconds
            )
            collaboration.next_reconcile_at = collaboration.lease_expires_at
            collaboration.revision = int(collaboration.revision) + 1
            await db.commit()
            return _Dispatch(
                collaboration_id=int(collaboration.id),
                public_id=str(collaboration.public_id),
                org_id=int(collaboration.org_id),
                user_id=int(collaboration.user_id),
                runtime_workspace_id=int(collaboration.runtime_workspace_id),
                session_id=int(session.id),
                runtime_session_id=str(session.runtime_session_id),
                origin_runtime_run_id=str(origin_run.runtime_run_id),
                original_message=str(payload.get("message") or "")[:16000],
                result_snapshot=dict(collaboration.result_snapshot or {}),
            )

    async def _activate_runtime(self, dispatch: _Dispatch):
        principal = Principal(org_id=dispatch.org_id, user_id=dispatch.user_id)
        async with AsyncSessionLocal() as db:
            infrastructure = build_runtime_infrastructure(db)
            profile = await infrastructure.workspace_service.ensure_default_for_user(
                principal
            )
            if profile.runtime_workspace_id != dispatch.runtime_workspace_id:
                raise RuntimeError("runtime workspace ownership mismatch")
            if profile.runtime_status != "ready":
                profile = await infrastructure.warmup_runner.warmup(profile)
            return await infrastructure.workspace_store.get_runtime_endpoint(profile)

    async def _create_run(self, dispatch: _Dispatch):
        principal = Principal(org_id=dispatch.org_id, user_id=dispatch.user_id)
        prompt = self._prompt(dispatch)
        async with AsyncSessionLocal() as db:
            service = RuntimeChatService(SqlAlchemyRuntimeChatStore(db))
            history = await service.get_session_history_control(
                principal=principal,
                runtime_workspace_id=dispatch.runtime_workspace_id,
                runtime_session_id=dispatch.runtime_session_id,
            )
            created = await service.create_run(
                principal=principal,
                intent=RuntimeChatRequestIntent(
                    runtime_workspace_id=dispatch.runtime_workspace_id,
                    runtime_session_id=dispatch.runtime_session_id,
                    message=prompt,
                    source_type="expert_collaboration",
                    audit_message="",
                    channel="expert_collaboration",
                ),
            )
            await service.record_history_checkpoint(
                principal=principal,
                runtime_run_id=created.runtime_run_id,
                history_message_count_before_run=int(history.message_count),
                source_snapshot={
                    "type": "expert_collaboration",
                    "collaboration_id": dispatch.public_id,
                    "origin_runtime_run_id": dispatch.origin_runtime_run_id,
                },
            )
            return created, history

    async def _bind_final_run(
        self,
        *,
        collaboration_id: int,
        runtime_run_id: str,
    ) -> None:
        async with AsyncSessionLocal() as db:
            collaboration = await db.get(
                ExpertKanbanCollaboration,
                collaboration_id,
            )
            run = await db.scalar(
                select(RuntimeRun).where(
                    RuntimeRun.runtime_run_id == runtime_run_id
                )
            )
            if collaboration is None or run is None:
                raise RuntimeError("expert collaboration final run binding missing")
            collaboration.final_run_id = int(run.id)
            collaboration.revision = int(collaboration.revision) + 1
            await db.commit()

    async def _execute_run(
        self,
        *,
        dispatch: _Dispatch,
        created,
        endpoint,
    ) -> str:
        principal = Principal(org_id=dispatch.org_id, user_id=dispatch.user_id)
        async with AsyncSessionLocal() as db:
            context = await resolve_personal_internal_turn(
                db,
                principal=principal,
                created=created,
                endpoint=endpoint,
            )
        try:
            result = await execute_personal_internal_turn(
                context=context,
                created=created,
                prompt=self._prompt(dispatch),
                source="expert_collaboration",
                execution_key=(
                    f"{dispatch.public_id}:"
                    f"{created.runtime_run_id}"
                ),
                capacity_wait_seconds=60,
                timeout_seconds=600,
            )
        except RuntimeInternalTurnError as exc:
            if exc.retryable_before_admission:
                async with AsyncSessionLocal() as db:
                    await RuntimeChatService(
                        SqlAlchemyRuntimeChatStore(db)
                    ).mark_runtime_run_finished(
                        org_id=principal.org_id,
                        user_id=principal.user_id,
                        runtime_run_id=created.runtime_run_id,
                        runtime_provider_run_id=None,
                        status="failed",
                        error_message=exc.code,
                    )
            raise
        return str(result.get("status") or "").upper()

    async def _finish_from_run(
        self,
        *,
        collaboration_id: int,
        runtime_run_id: str,
        terminal_state: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        if terminal_state != "COMPLETED":
            await self._attention_required(
                collaboration_id,
                error_code=terminal_state or "summary_run_failed",
            )
            return
        async with AsyncSessionLocal() as db:
            collaboration = await db.get(
                ExpertKanbanCollaboration,
                collaboration_id,
            )
            if collaboration is None:
                return
            collaboration.state = "delivered"
            collaboration.delivered_at = now
            collaboration.last_error_code = None
            collaboration.last_error_message = None
            collaboration.lease_expires_at = None
            collaboration.next_reconcile_at = now
            collaboration.revision = int(collaboration.revision) + 1
            await db.commit()

    async def _attention_required(
        self,
        collaboration_id: int,
        *,
        error_code: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            collaboration = await db.get(
                ExpertKanbanCollaboration,
                collaboration_id,
            )
            if collaboration is None or collaboration.state == "delivered":
                return
            collaboration.state = "attention_required"
            collaboration.lease_expires_at = None
            collaboration.next_reconcile_at = now
            collaboration.last_error_code = error_code[:100]
            collaboration.last_error_message = error_code[:200]
            collaboration.revision = int(collaboration.revision) + 1
            await db.commit()

    async def _retry(
        self,
        collaboration_id: int,
        *,
        error_code: str,
        clear_final_run: bool,
    ) -> None:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            collaboration = await db.get(
                ExpertKanbanCollaboration,
                collaboration_id,
            )
            if collaboration is None or collaboration.state == "delivered":
                return
            if int(collaboration.attempt_count) >= self.max_attempts:
                collaboration.state = "delivery_failed"
                collaboration.next_reconcile_at = now
            else:
                collaboration.state = "synthesis_pending"
                collaboration.next_reconcile_at = now + timedelta(
                    seconds=min(60, 2 ** min(int(collaboration.attempt_count), 5))
                )
            if clear_final_run:
                collaboration.final_run_id = None
            collaboration.lease_expires_at = None
            collaboration.last_error_code = error_code[:100]
            collaboration.last_error_message = error_code[:200]
            collaboration.revision = int(collaboration.revision) + 1
            await db.commit()

    @staticmethod
    def _prompt(dispatch: _Dispatch) -> str:
        snapshot = json.dumps(
            dispatch.result_snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
        )[:48000]
        return (
            "[平台内部通知：专家协作任务已经完成]\n"
            "请根据当前会话上下文和下面的 Hermes Kanban 权威结果，直接面向用户"
            "给出最终结论。请综合各专家贡献，明确重要结论、必要依据和产物；"
            "不要解释平台通知机制，不要把本通知当成用户的新问题，也不要无条件"
            "再次创建 Kanban 任务。\n\n"
            f"原始用户任务：\n{dispatch.original_message}\n\n"
            f"专家任务结果：\n{snapshot}"
        )


expert_collaboration_runner = ExpertCollaborationRunner()
