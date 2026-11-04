from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import and_, func, or_, select

from app.config import settings
from app.models import RuntimeRun

ACTIVE_RUN_STATUSES = frozenset(
    {"accepted", "pending", "running", "canceling", "cancel_failed"}
)


class ActivitySession(Protocol):
    async def execute(self, statement: object): ...

    async def rollback(self) -> None: ...


@dataclass(frozen=True)
class RuntimeActivitySnapshot:
    run_count: int
    self_evolution_count: int

    @property
    def idle(self) -> bool:
        return self.run_count + self.self_evolution_count == 0


class SqlAlchemyRuntimeActivityStore:
    """Read drain blockers from authoritative business tables only."""

    def __init__(self, session: ActivitySession) -> None:
        self.session = session

    async def get_snapshot(
        self,
        runtime_workspace_id: int,
    ) -> RuntimeActivitySnapshot:
        if runtime_workspace_id <= 0:
            raise ValueError("runtime_workspace_id must be positive")
        now = datetime.now(timezone.utc)
        review_activity_cutoff = now - timedelta(
            seconds=settings.POST_TURN_REVIEW_IDLE_TIMEOUT_SECONDS
        )
        try:
            run_count = await self._count(
                select(func.count())
                .select_from(RuntimeRun)
                .where(
                    RuntimeRun.runtime_workspace_id == runtime_workspace_id,
                    RuntimeRun.status.in_(tuple(sorted(ACTIVE_RUN_STATUSES))),
                )
            )
            self_evolution_count = await self._count(
                select(func.count())
                .select_from(RuntimeRun)
                .where(
                    RuntimeRun.runtime_workspace_id == runtime_workspace_id,
                    or_(
                        and_(
                            RuntimeRun.self_evolution_status == "eligible",
                            RuntimeRun.self_evolution_activation_deadline_at.is_not(None),
                            RuntimeRun.self_evolution_activation_deadline_at >= now,
                            RuntimeRun.self_evolution_deadline_at.is_not(None),
                            RuntimeRun.self_evolution_deadline_at >= now,
                        ),
                        and_(
                            RuntimeRun.self_evolution_status == "running",
                            RuntimeRun.self_evolution_last_activity_at.is_not(None),
                            RuntimeRun.self_evolution_last_activity_at
                            >= review_activity_cutoff,
                            RuntimeRun.self_evolution_deadline_at.is_not(None),
                            RuntimeRun.self_evolution_deadline_at >= now,
                        ),
                    ),
                )
            )
            return RuntimeActivitySnapshot(
                run_count=run_count,
                self_evolution_count=self_evolution_count,
            )
        finally:
            # End the read transaction before any drain sleep or Kubernetes call.
            await self.session.rollback()

    async def _count(self, statement: object) -> int:
        result = await self.session.execute(statement)
        return int(result.scalar_one())
