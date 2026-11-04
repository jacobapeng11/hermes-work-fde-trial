from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuntimeAuditLog
from app.services.admin_auth import AdminPrincipal


FORBIDDEN_AUDIT_DETAIL_KEYS = {
    "api_key",
    "api_key_encrypted",
    "provider_key",
    "provider_api_key",
    "runtime_token",
    "runtime_api_key",
    "mcp_secret",
    "kubeconfig",
    "host_path",
    "runtime_root_host_path",
}


@dataclass(frozen=True)
class AdminAuditLogRecord:
    audit_log_id: int
    org_id: int
    actor_user_id: int | None
    runtime_workspace_id: int | None
    action: str
    target_type: str
    target_id: str
    detail: dict[str, Any] | None
    created_at: datetime | None = None


@dataclass(frozen=True)
class AdminAuditLogCreate:
    action: str
    target_type: str
    target_id: str
    runtime_workspace_id: int | None = None
    detail: dict[str, Any] | None = None


class AdminAuditLogStore(Protocol):
    async def record(self, *, org_id: int, actor_user_id: int, payload: AdminAuditLogCreate) -> AdminAuditLogRecord:
        ...

    async def list_logs(
        self,
        *,
        org_id: int,
        action: str | None,
        target_type: str | None,
        limit: int,
    ) -> list[AdminAuditLogRecord]:
        ...


class AdminAuditLogService:
    def __init__(self, store: AdminAuditLogStore) -> None:
        self.store = store

    async def record(
        self,
        *,
        principal: AdminPrincipal,
        action: str,
        target_type: str,
        target_id: str | int,
        runtime_workspace_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AdminAuditLogRecord:
        return await self.store.record(
            org_id=principal.org_id,
            actor_user_id=principal.user_id,
            payload=AdminAuditLogCreate(
                action=action,
                target_type=target_type,
                target_id=str(target_id),
                runtime_workspace_id=runtime_workspace_id,
                detail=_sanitize_detail(detail or {}),
            ),
        )

    async def list_logs(
        self,
        *,
        principal: AdminPrincipal,
        action: str | None = None,
        target_type: str | None = None,
        limit: int = 50,
    ) -> list[AdminAuditLogRecord]:
        normalized_limit = max(1, min(limit, 200))
        return await self.store.list_logs(
            org_id=principal.org_id,
            action=action,
            target_type=target_type,
            limit=normalized_limit,
        )


class InMemoryAdminAuditLogStore:
    def __init__(self) -> None:
        self.logs: list[AdminAuditLogRecord] = []
        self.next_id = 1

    async def record(
        self,
        *,
        org_id: int,
        actor_user_id: int,
        payload: AdminAuditLogCreate,
    ) -> AdminAuditLogRecord:
        record = AdminAuditLogRecord(
            audit_log_id=self.next_id,
            org_id=org_id,
            actor_user_id=actor_user_id,
            runtime_workspace_id=payload.runtime_workspace_id,
            action=payload.action,
            target_type=payload.target_type,
            target_id=payload.target_id,
            detail=payload.detail,
            created_at=None,
        )
        self.next_id += 1
        self.logs.append(record)
        return record

    async def list_logs(
        self,
        *,
        org_id: int,
        action: str | None,
        target_type: str | None,
        limit: int,
    ) -> list[AdminAuditLogRecord]:
        records = [record for record in self.logs if record.org_id == org_id]
        if action is not None:
            records = [record for record in records if record.action == action]
        if target_type is not None:
            records = [record for record in records if record.target_type == target_type]
        return list(reversed(records))[:limit]


class SqlAlchemyAdminAuditLogStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def record(
        self,
        *,
        org_id: int,
        actor_user_id: int,
        payload: AdminAuditLogCreate,
    ) -> AdminAuditLogRecord:
        row = RuntimeAuditLog(
            org_id=org_id,
            actor_user_id=actor_user_id,
            runtime_workspace_id=payload.runtime_workspace_id,
            action=payload.action,
            target_type=payload.target_type,
            target_id=payload.target_id,
            detail=payload.detail,
        )
        self.db.add(row)
        await self.db.commit()
        await self.db.refresh(row)
        return self._record(row)

    async def list_logs(
        self,
        *,
        org_id: int,
        action: str | None,
        target_type: str | None,
        limit: int,
    ) -> list[AdminAuditLogRecord]:
        statement = select(RuntimeAuditLog).where(RuntimeAuditLog.org_id == org_id)
        if action is not None:
            statement = statement.where(RuntimeAuditLog.action == action)
        if target_type is not None:
            statement = statement.where(RuntimeAuditLog.target_type == target_type)
        result = await self.db.execute(statement.order_by(RuntimeAuditLog.created_at.desc(), RuntimeAuditLog.id.desc()).limit(limit))
        return [self._record(row) for row in result.scalars().all()]

    @staticmethod
    def _record(row: RuntimeAuditLog) -> AdminAuditLogRecord:
        return AdminAuditLogRecord(
            audit_log_id=int(row.id),
            org_id=int(row.org_id),
            actor_user_id=int(row.actor_user_id) if row.actor_user_id is not None else None,
            runtime_workspace_id=int(row.runtime_workspace_id) if row.runtime_workspace_id is not None else None,
            action=str(row.action),
            target_type=str(row.target_type),
            target_id=str(row.target_id),
            detail=dict(row.detail or {}),
            created_at=row.created_at,
        )


def _sanitize_detail(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in FORBIDDEN_AUDIT_DETAIL_KEYS:
                continue
            sanitized[str(key)] = _sanitize_detail(child)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_detail(item) for item in value]
    return value
