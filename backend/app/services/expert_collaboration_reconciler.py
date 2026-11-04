from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.hermes_runtime.profile_adapter import HermesProfileAdapter
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models import (
    ExpertKanbanCollaboration,
    ExpertKanbanCollaborationTask,
    HermesRuntimeBinding,
    RuntimeRun,
    RuntimeSession,
)
from app.services.expert_collaboration_runner import ExpertCollaborationRunner
from app.services.expert_collaborations import ExpertCollaborationService
from app.services.expert_kanban_worker_runs import (
    ExpertKanbanWorkerRunService,
)
from app.services.runtime_workspace import RuntimeApiKeyCodec


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class _TaskRef:
    board: str
    task_id: str


@dataclass(frozen=True)
class _WaitingSnapshot:
    collaboration_id: int
    runtime_session_id: str
    endpoint: RuntimeEndpoint
    tasks: tuple[_TaskRef, ...]


class ExpertCollaborationReconciler:
    """Reconcile delivery state against Hermes' authoritative native Board."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        profile_adapter: HermesProfileAdapter,
        runner: ExpertCollaborationRunner,
        interval_seconds: float | None = None,
        max_attempts: int | None = None,
        wait_backoff_max_seconds: int | None = None,
        wait_max_attempts: int | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.profile_adapter = profile_adapter
        self.runner = runner
        self.interval_seconds = float(
            interval_seconds
            or settings.EXPERT_COLLABORATION_RECONCILE_INTERVAL_SECONDS
        )
        self.max_attempts = int(
            max_attempts
            or settings.EXPERT_COLLABORATION_MAX_SYNTHESIS_ATTEMPTS
        )
        self.wait_backoff_max_seconds = float(
            wait_backoff_max_seconds
            if wait_backoff_max_seconds is not None
            else settings.EXPERT_COLLABORATION_WAIT_BACKOFF_MAX_SECONDS
        )
        self.wait_max_attempts = int(
            wait_max_attempts
            if wait_max_attempts is not None
            else settings.EXPERT_COLLABORATION_WAIT_MAX_ATTEMPTS
        )
        self.codec = RuntimeApiKeyCodec()

    async def reconcile_once(self, *, now: datetime | None = None) -> int:
        now = _as_utc(now or datetime.now(timezone.utc)) or datetime.now(
            timezone.utc
        )
        async with self.session_factory() as session:
            await ExpertCollaborationService(
                session
            ).bind_pending_create_events(now=now)
            await ExpertKanbanWorkerRunService(
                session
            ).ensure_pending_for_collaborations(now=now)
        await self._recover_synthesizing(now)
        snapshots = await self._waiting_snapshots(now)
        for snapshot in snapshots:
            await self._reconcile_waiting(snapshot, now=now)
        launched = await self._launch_synthesis(now)
        return len(snapshots) + launched

    async def _waiting_snapshots(
        self,
        now: datetime,
    ) -> list[_WaitingSnapshot]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        ExpertKanbanCollaboration,
                        RuntimeSession,
                        HermesRuntimeBinding,
                    )
                    .join(
                        RuntimeSession,
                        RuntimeSession.id
                        == ExpertKanbanCollaboration.session_id,
                    )
                    .join(
                        HermesRuntimeBinding,
                        HermesRuntimeBinding.runtime_workspace_id
                        == ExpertKanbanCollaboration.runtime_workspace_id,
                    )
                    .where(
                        ExpertKanbanCollaboration.state.in_(
                            ("waiting_tasks", "attention_required")
                        ),
                        ExpertKanbanCollaboration.next_reconcile_at <= now,
                        RuntimeSession.status == "active",
                        HermesRuntimeBinding.status == "ready",
                        HermesRuntimeBinding.endpoint_base_url.is_not(None),
                        HermesRuntimeBinding.endpoint_host_header.is_not(None),
                    )
                    .order_by(ExpertKanbanCollaboration.id)
                    .limit(100)
                )
            ).all()
            snapshots: list[_WaitingSnapshot] = []
            for collaboration, runtime_session, binding in rows:
                tasks = (
                    await session.execute(
                        select(ExpertKanbanCollaborationTask)
                        .where(
                            ExpertKanbanCollaborationTask.collaboration_id
                            == collaboration.id
                        )
                        .order_by(ExpertKanbanCollaborationTask.id)
                    )
                ).scalars().all()
                if not tasks:
                    continue
                snapshots.append(
                    _WaitingSnapshot(
                        collaboration_id=int(collaboration.id),
                        runtime_session_id=str(runtime_session.runtime_session_id),
                        endpoint=RuntimeEndpoint(
                            runtime_workspace_id=int(
                                binding.runtime_workspace_id
                            ),
                            runtime_id=str(binding.runtime_id),
                            endpoint_base_url=str(binding.endpoint_base_url),
                            endpoint_host_header=str(binding.endpoint_host_header),
                            api_key=self.codec.decrypt(
                                str(binding.api_key_encrypted)
                            ),
                        ),
                        tasks=tuple(
                            _TaskRef(
                                board=str(item.board or "default"),
                                task_id=str(item.task_id),
                            )
                            for item in tasks
                        ),
                    )
                )
            return snapshots

    async def _reconcile_waiting(
        self,
        snapshot: _WaitingSnapshot,
        *,
        now: datetime,
    ) -> None:
        results: list[dict[str, Any]] = []
        blocked = False
        all_done = True
        try:
            for task_ref in snapshot.tasks:
                payload = await self.profile_adapter.get_kanban_task_detail(
                    snapshot.endpoint,
                    task_id=task_ref.task_id,
                    board=task_ref.board,
                )
                task = dict(payload["task"])
                task_session_id = str(task.get("session_id") or "").strip()
                if task_session_id != snapshot.runtime_session_id:
                    raise RuntimeError("kanban task session identity mismatch")
                status = str(task.get("status") or "").lower()
                blocked = blocked or status in {"blocked", "archived"}
                all_done = all_done and status == "done"
                results.append(self._bounded_result(task_ref, payload))
        except Exception as exc:
            await self._record_wait_failure(
                snapshot.collaboration_id,
                now=now,
                error_code=type(exc).__name__.lower()[:100],
            )
            return
        async with self.session_factory() as session:
            worker_runs = ExpertKanbanWorkerRunService(session)
            for result in results:
                await worker_runs.finish_task(
                    runtime_workspace_id=snapshot.endpoint.runtime_workspace_id,
                    board=str(result["board"]),
                    task_id=str(result["task_id"]),
                    task_status=str(result["status"]),
                    now=now,
                )
            collaboration = await session.get(
                ExpertKanbanCollaboration,
                snapshot.collaboration_id,
            )
            if collaboration is None or collaboration.state in {
                "delivered",
                "delivery_failed",
                "canceled",
            }:
                return
            next_state = (
                "synthesis_pending"
                if all_done
                else "attention_required"
                if blocked
                else "waiting_tasks"
            )
            changed = str(collaboration.state) != next_state
            if all_done:
                collaboration.wait_attempt_count = 0
            elif changed:
                collaboration.wait_attempt_count = 0
            else:
                collaboration.wait_attempt_count = (
                    int(collaboration.wait_attempt_count) + 1
                )
                if (
                    int(collaboration.wait_attempt_count)
                    >= self.wait_max_attempts
                ):
                    collaboration.state = "delivery_failed"
                    collaboration.lease_expires_at = None
                    collaboration.last_error_code = "wait_reconcile_exhausted"
                    collaboration.last_error_message = (
                        "wait_reconcile_exhausted"
                    )
                    collaboration.next_reconcile_at = now
                    collaboration.revision = (
                        int(collaboration.revision) + 1
                    )
                    await session.commit()
                    return
            collaboration.state = next_state
            collaboration.next_reconcile_at = (
                now
                if all_done
                else now
                + timedelta(
                    seconds=self._wait_delay(
                        int(collaboration.wait_attempt_count)
                    )
                )
            )
            collaboration.last_error_code = None
            collaboration.last_error_message = None
            if all_done:
                collaboration.result_snapshot = {"tasks": results}
            if changed or all_done:
                collaboration.revision = int(collaboration.revision) + 1
            await session.commit()

    async def _record_wait_failure(
        self,
        collaboration_id: int,
        *,
        now: datetime,
        error_code: str,
    ) -> None:
        async with self.session_factory() as session:
            collaboration = await session.get(
                ExpertKanbanCollaboration,
                collaboration_id,
            )
            if collaboration is None:
                return
            collaboration.last_error_code = error_code
            collaboration.last_error_message = error_code
            collaboration.wait_attempt_count = (
                int(collaboration.wait_attempt_count) + 1
            )
            if int(collaboration.wait_attempt_count) >= self.wait_max_attempts:
                collaboration.state = "delivery_failed"
                collaboration.lease_expires_at = None
                collaboration.last_error_code = "wait_reconcile_exhausted"
                collaboration.last_error_message = "wait_reconcile_exhausted"
                collaboration.revision = int(collaboration.revision) + 1
                await session.commit()
                return
            collaboration.next_reconcile_at = now + timedelta(
                seconds=self._wait_delay(
                    int(collaboration.wait_attempt_count)
                )
            )
            await session.commit()

    def _wait_delay(self, attempt_count: int) -> float:
        """Exponential backoff for waiting collaborations without progress.

        Board polling rides the same Runtime ingress as user traffic. A fixed
        two-second loop keeps otherwise idle Knative Routines at one replica
        forever, so unproductive waits double their delay up to the configured
        cap and eventually become terminal.
        """
        exponential = self.interval_seconds * (
            2 ** max(0, min(attempt_count, 16))
        )
        return float(min(exponential, self.wait_backoff_max_seconds))

    async def _recover_synthesizing(self, now: datetime) -> None:
        async with self.session_factory() as session:
            collaborations = (
                await session.execute(
                    select(ExpertKanbanCollaboration)
                    .where(
                        ExpertKanbanCollaboration.state == "synthesizing"
                    )
                    .order_by(ExpertKanbanCollaboration.id)
                    .limit(100)
                )
            ).scalars().all()
            for collaboration in collaborations:
                run = (
                    await session.get(RuntimeRun, collaboration.final_run_id)
                    if collaboration.final_run_id is not None
                    else None
                )
                if run is not None and str(run.status) == "completed":
                    collaboration.state = "delivered"
                    collaboration.delivered_at = now
                    collaboration.lease_expires_at = None
                    collaboration.next_reconcile_at = now
                    collaboration.last_error_code = None
                    collaboration.last_error_message = None
                    collaboration.revision = int(collaboration.revision) + 1
                    continue
                if run is not None and str(run.status) in {
                    "accepted",
                    "pending",
                    "running",
                }:
                    collaboration.next_reconcile_at = now + timedelta(
                        seconds=self.interval_seconds
                    )
                    continue
                lease_expired = (
                    collaboration.lease_expires_at is None
                    or (_as_utc(collaboration.lease_expires_at) or now) <= now
                )
                if run is None and not lease_expired:
                    continue
                if int(collaboration.attempt_count) >= self.max_attempts:
                    collaboration.state = "delivery_failed"
                else:
                    collaboration.state = "synthesis_pending"
                    collaboration.final_run_id = None
                    collaboration.next_reconcile_at = now
                collaboration.lease_expires_at = None
                collaboration.last_error_code = "summary_run_interrupted"
                collaboration.last_error_message = "summary_run_interrupted"
                collaboration.revision = int(collaboration.revision) + 1
            await session.commit()

    async def _launch_synthesis(self, now: datetime) -> int:
        async with self.session_factory() as session:
            ids = (
                await session.execute(
                    select(ExpertKanbanCollaboration.id)
                    .where(
                        ExpertKanbanCollaboration.state == "synthesis_pending",
                        ExpertKanbanCollaboration.next_reconcile_at <= now,
                    )
                    .order_by(ExpertKanbanCollaboration.id)
                    .limit(20)
                )
            ).scalars().all()
        return sum(1 for item in ids if self.runner.launch(int(item)))

    @staticmethod
    def _bounded_result(
        task_ref: _TaskRef,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        task = dict(payload.get("task") or {})
        child_results = payload.get("child_results")
        safe_children: list[dict[str, str]] = []
        if isinstance(child_results, list):
            for child in child_results[:40]:
                if not isinstance(child, dict):
                    continue
                safe_children.append(
                    {
                        "task_id": str(child.get("id") or "")[:100],
                        "title": str(child.get("title") or "")[:500],
                        "status": str(child.get("status") or "")[:32],
                        "summary": str(
                            child.get("latest_summary")
                            or child.get("result")
                            or ""
                        )[:8000],
                    }
                )
        return {
            "task_id": task_ref.task_id,
            "board": task_ref.board,
            "title": str(task.get("title") or "")[:500],
            "assignee": str(task.get("assignee") or "")[:128],
            "status": str(task.get("status") or "")[:32],
            "summary": str(
                task.get("latest_summary") or task.get("result") or ""
            )[:16000],
            "children": safe_children,
        }
