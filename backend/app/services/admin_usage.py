from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelEntry, ModelUsageLog, RuntimeAuditLog, RuntimeRun
from app.services.admin_auth import AdminPrincipal


ACTIVE_RUN_STATUSES = {"accepted", "pending", "running"}
ERROR_RUN_STATUSES = {"failed", "error", "cancelled"}


@dataclass(frozen=True)
class AdminUsageOverviewRecord:
    run_count: int
    active_run_count: int
    model_call_count: int
    audit_log_count: int
    error_count: int
    input_tokens: int
    output_tokens: int
    average_latency_ms: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class AdminRuntimeWorkspaceUsageRecord:
    runtime_workspace_id: int
    user_id: int | None
    run_count: int
    active_run_count: int
    model_call_count: int
    error_count: int
    input_tokens: int
    output_tokens: int
    average_latency_ms: int
    last_error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class AdminModelUsageRecord:
    model_entry_id: int | None
    model_display_name: str | None
    provider_id: int | None
    model_call_count: int
    error_count: int
    input_tokens: int
    output_tokens: int
    average_latency_ms: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class AdminUsageRecentErrorRecord:
    source: str
    runtime_workspace_id: int | None
    runtime_run_id: str | None
    model_entry_id: int | None
    action: str | None
    error_code: str | None
    message: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class AdminUsageSummaryRecord:
    overview: AdminUsageOverviewRecord
    runtime_workspaces: list[AdminRuntimeWorkspaceUsageRecord]
    models: list[AdminModelUsageRecord]
    recent_errors: list[AdminUsageRecentErrorRecord]


class AdminUsageStore(Protocol):
    async def summary(self, *, org_id: int) -> AdminUsageSummaryRecord:
        ...


class AdminUsageService:
    def __init__(self, store: AdminUsageStore) -> None:
        self.store = store

    async def summary(self, principal: AdminPrincipal) -> AdminUsageSummaryRecord:
        return await self.store.summary(org_id=principal.org_id)


class InMemoryAdminUsageStore:
    async def summary(self, *, org_id: int) -> AdminUsageSummaryRecord:
        return AdminUsageSummaryRecord(
            overview=AdminUsageOverviewRecord(
                run_count=5,
                active_run_count=1,
                model_call_count=3,
                audit_log_count=2,
                error_count=1,
                input_tokens=34,
                output_tokens=55,
                average_latency_ms=240,
            ),
            runtime_workspaces=[
                AdminRuntimeWorkspaceUsageRecord(
                    runtime_workspace_id=1,
                    user_id=1,
                    run_count=5,
                    active_run_count=1,
                    model_call_count=3,
                    error_count=1,
                    input_tokens=34,
                    output_tokens=55,
                    average_latency_ms=240,
                    last_error="contract upstream timeout",
                )
            ],
            models=[
                AdminModelUsageRecord(
                    model_entry_id=1,
                    model_display_name="GPT-4.1 Mini",
                    provider_id=1,
                    model_call_count=3,
                    error_count=1,
                    input_tokens=34,
                    output_tokens=55,
                    average_latency_ms=240,
                )
            ],
            recent_errors=[
                AdminUsageRecentErrorRecord(
                    source="model_relay",
                    runtime_workspace_id=1,
                    runtime_run_id="run-contract-5",
                    model_entry_id=1,
                    action=None,
                    error_code="upstream_504",
                    message="contract upstream timeout",
                    created_at=None,
                )
            ],
        )


class SqlAlchemyAdminUsageStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def summary(self, *, org_id: int) -> AdminUsageSummaryRecord:
        return AdminUsageSummaryRecord(
            overview=await self._overview(org_id=org_id),
            runtime_workspaces=await self._runtime_workspaces(org_id=org_id),
            models=await self._models(org_id=org_id),
            recent_errors=await self._recent_errors(org_id=org_id),
        )

    async def _overview(self, *, org_id: int) -> AdminUsageOverviewRecord:
        runs = await self.db.execute(
            select(
                func.count(RuntimeRun.id),
                func.coalesce(
                    func.sum(case((RuntimeRun.status.in_(tuple(ACTIVE_RUN_STATUSES)), 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((RuntimeRun.status.in_(tuple(ERROR_RUN_STATUSES)), 1), else_=0)),
                    0,
                ),
            ).where(RuntimeRun.org_id == org_id)
        )
        run_count, active_run_count, run_error_count = runs.one()

        usage = await self.db.execute(
            select(
                func.count(ModelUsageLog.id),
                func.coalesce(func.sum(ModelUsageLog.input_tokens), 0),
                func.coalesce(func.sum(ModelUsageLog.output_tokens), 0),
                func.coalesce(func.avg(ModelUsageLog.latency_ms), 0),
                func.coalesce(
                    func.sum(case((ModelUsageLog.status != "success", 1), else_=0)),
                    0,
                ),
            ).where(ModelUsageLog.org_id == org_id)
        )
        model_call_count, input_tokens, output_tokens, avg_latency, usage_error_count = usage.one()

        audit_count = await self._count(RuntimeAuditLog, org_id=org_id)
        return AdminUsageOverviewRecord(
            run_count=int(run_count or 0),
            active_run_count=int(active_run_count or 0),
            model_call_count=int(model_call_count or 0),
            audit_log_count=audit_count,
            error_count=int((run_error_count or 0) + (usage_error_count or 0)),
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            average_latency_ms=int(avg_latency or 0),
        )

    async def _runtime_workspaces(self, *, org_id: int) -> list[AdminRuntimeWorkspaceUsageRecord]:
        run_rows = await self.db.execute(
            select(
                RuntimeRun.runtime_workspace_id,
                func.min(RuntimeRun.user_id),
                func.count(RuntimeRun.id),
                func.coalesce(
                    func.sum(case((RuntimeRun.status.in_(tuple(ACTIVE_RUN_STATUSES)), 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((RuntimeRun.status.in_(tuple(ERROR_RUN_STATUSES)), 1), else_=0)),
                    0,
                ),
            )
            .where(RuntimeRun.org_id == org_id)
            .group_by(RuntimeRun.runtime_workspace_id)
        )
        records: dict[int, AdminRuntimeWorkspaceUsageRecord] = {}
        for workspace_id, user_id, run_count, active_run_count, run_error_count in run_rows.all():
            records[int(workspace_id)] = AdminRuntimeWorkspaceUsageRecord(
                runtime_workspace_id=int(workspace_id),
                user_id=int(user_id) if user_id is not None else None,
                run_count=int(run_count or 0),
                active_run_count=int(active_run_count or 0),
                model_call_count=0,
                error_count=int(run_error_count or 0),
                input_tokens=0,
                output_tokens=0,
                average_latency_ms=0,
                last_error=await self._latest_error(org_id=org_id, runtime_workspace_id=int(workspace_id)),
            )

        usage_rows = await self.db.execute(
            select(
                ModelUsageLog.runtime_workspace_id,
                func.min(ModelUsageLog.user_id),
                func.count(ModelUsageLog.id),
                func.coalesce(func.sum(ModelUsageLog.input_tokens), 0),
                func.coalesce(func.sum(ModelUsageLog.output_tokens), 0),
                func.coalesce(func.avg(ModelUsageLog.latency_ms), 0),
                func.coalesce(
                    func.sum(case((ModelUsageLog.status != "success", 1), else_=0)),
                    0,
                ),
            )
            .where(ModelUsageLog.org_id == org_id)
            .group_by(ModelUsageLog.runtime_workspace_id)
        )
        for workspace_id, user_id, call_count, input_tokens, output_tokens, avg_latency, usage_error_count in usage_rows.all():
            key = int(workspace_id)
            current = records.get(key)
            records[key] = AdminRuntimeWorkspaceUsageRecord(
                runtime_workspace_id=key,
                user_id=(current.user_id if current is not None else int(user_id) if user_id is not None else None),
                run_count=current.run_count if current is not None else 0,
                active_run_count=current.active_run_count if current is not None else 0,
                model_call_count=int(call_count or 0),
                error_count=(current.error_count if current is not None else 0) + int(usage_error_count or 0),
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                average_latency_ms=int(avg_latency or 0),
                last_error=current.last_error if current is not None else await self._latest_error(org_id=org_id, runtime_workspace_id=key),
            )
        return sorted(records.values(), key=lambda item: (-item.error_count, -item.model_call_count, item.runtime_workspace_id))

    async def _models(self, *, org_id: int) -> list[AdminModelUsageRecord]:
        rows = await self.db.execute(
            select(
                ModelUsageLog.model_entry_id,
                ModelEntry.display_name,
                ModelUsageLog.provider_id,
                func.count(ModelUsageLog.id),
                func.coalesce(
                    func.sum(case((ModelUsageLog.status != "success", 1), else_=0)),
                    0,
                ),
                func.coalesce(func.sum(ModelUsageLog.input_tokens), 0),
                func.coalesce(func.sum(ModelUsageLog.output_tokens), 0),
                func.coalesce(func.avg(ModelUsageLog.latency_ms), 0),
            )
            .outerjoin(ModelEntry, ModelEntry.id == ModelUsageLog.model_entry_id)
            .where(ModelUsageLog.org_id == org_id)
            .group_by(ModelUsageLog.model_entry_id, ModelEntry.display_name, ModelUsageLog.provider_id)
            .order_by(func.count(ModelUsageLog.id).desc())
        )
        return [
            AdminModelUsageRecord(
                model_entry_id=int(model_entry_id) if model_entry_id is not None else None,
                model_display_name=str(display_name) if display_name is not None else None,
                provider_id=int(provider_id) if provider_id is not None else None,
                model_call_count=int(call_count or 0),
                error_count=int(error_count or 0),
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                average_latency_ms=int(avg_latency or 0),
            )
            for model_entry_id, display_name, provider_id, call_count, error_count, input_tokens, output_tokens, avg_latency in rows.all()
        ]

    async def _recent_errors(self, *, org_id: int) -> list[AdminUsageRecentErrorRecord]:
        usage_rows = await self.db.execute(
            select(ModelUsageLog)
            .where(
                ModelUsageLog.org_id == org_id,
                ModelUsageLog.status != "success",
            )
            .order_by(ModelUsageLog.created_at.desc(), ModelUsageLog.id.desc())
            .limit(5)
        )
        run_rows = await self.db.execute(
            select(RuntimeRun)
            .where(
                RuntimeRun.org_id == org_id,
                RuntimeRun.error_message.is_not(None),
            )
            .order_by(RuntimeRun.created_at.desc(), RuntimeRun.id.desc())
            .limit(5)
        )
        records = [
            AdminUsageRecentErrorRecord(
                source="model_relay",
                runtime_workspace_id=int(row.runtime_workspace_id),
                runtime_run_id=row.runtime_run_id,
                model_entry_id=int(row.model_entry_id) if row.model_entry_id is not None else None,
                action=None,
                error_code=row.error_code,
                message=_truncate(row.error_code or row.status),
                created_at=row.created_at,
            )
            for row in usage_rows.scalars().all()
        ]
        records.extend(
            AdminUsageRecentErrorRecord(
                source="runtime_run",
                runtime_workspace_id=int(row.runtime_workspace_id),
                runtime_run_id=row.runtime_run_id,
                model_entry_id=int(row.model_entry_id) if row.model_entry_id is not None else None,
                action=None,
                error_code=row.error_code,
                message=_truncate(row.error_message),
                created_at=row.created_at,
            )
            for row in run_rows.scalars().all()
        )
        return sorted(records, key=lambda item: item.created_at or datetime.min, reverse=True)[:10]

    async def _latest_error(self, *, org_id: int, runtime_workspace_id: int) -> str | None:
        result = await self.db.execute(
            select(RuntimeRun.error_message)
            .where(
                RuntimeRun.org_id == org_id,
                RuntimeRun.runtime_workspace_id == runtime_workspace_id,
                RuntimeRun.error_message.is_not(None),
            )
            .order_by(RuntimeRun.created_at.desc(), RuntimeRun.id.desc())
            .limit(1)
        )
        return _truncate(result.scalar_one_or_none())

    async def _count(self, model: type, *, org_id: int) -> int:
        result = await self.db.execute(select(func.count()).select_from(model).where(model.org_id == org_id))
        return int(result.scalar_one())


def _truncate(value: str | None, *, limit: int = 240) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."
