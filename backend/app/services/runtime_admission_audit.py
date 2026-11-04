from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuntimeAdmissionAudit
from app.schemas.runtime_internal import (
    RuntimeAdmissionAuditBatch,
    RuntimeAdmissionAuditItem,
    canonical_payload_digest,
)
from app.services.runtime_projection import (
    RuntimeProjectionAuthenticationError,
    RuntimeProjectionBindingError,
    RuntimeProjectionService,
)


@dataclass(frozen=True)
class RuntimeAdmissionAuditResult:
    accepted_event_ids: list[str]
    duplicate_event_ids: list[str]
    rejected_event_ids: list[str]


class RuntimeAdmissionAuditService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._auth = RuntimeProjectionService(db, materialize=False)
        self._binding: object | None = None

    async def authenticate(
        self,
        *,
        runtime_id: str,
        binding_id: str,
        api_server_key: str,
    ) -> None:
        await self._auth.authenticate(
            runtime_id=runtime_id,
            binding_id=binding_id,
            api_server_key=api_server_key,
        )
        self._binding = self._auth._personal_binding or self._auth._application_binding

    async def record_batch(
        self,
        batch: RuntimeAdmissionAuditBatch,
    ) -> RuntimeAdmissionAuditResult:
        if self._binding is None:
            raise RuntimeProjectionAuthenticationError("Runtime audit was not authenticated")
        accepted: list[str] = []
        duplicates: list[str] = []
        rejected: list[str] = []
        for item in batch.items:
            digest = _item_digest(item)
            existing = await self.db.scalar(
                select(RuntimeAdmissionAudit).where(
                    RuntimeAdmissionAudit.event_id == item.event_id
                )
            )
            if existing is not None:
                if existing.payload_digest == digest:
                    duplicates.append(item.event_id)
                else:
                    rejected.append(item.event_id)
                continue
            self.db.add(
                RuntimeAdmissionAudit(
                    event_id=item.event_id,
                    payload_digest=digest,
                    runtime_id=batch.runtime_id,
                    binding_id=batch.binding_id,
                    org_id=int(getattr(self._binding, "org_id")),
                    user_id=_optional_int(getattr(self._binding, "user_id", None)),
                    runtime_workspace_id=_optional_int(
                        getattr(self._binding, "runtime_workspace_id", None)
                    ),
                    runtime_session_id=item.session_id,
                    request_id=item.request_id,
                    client_message_id=item.client_message_id,
                    runtime_run_id=item.run_id,
                    operation=item.operation,
                    status=item.status,
                    outcome=item.outcome,
                    stage=item.stage,
                    error_code=item.error_code,
                    exception_type=item.exception_type,
                    profile_id=item.profile_id,
                    model_id=item.model_id,
                    config_revision=item.config_revision,
                    attachment_count=item.attachment_count,
                    prompt_bytes=item.prompt_bytes,
                    upstream_status=item.upstream_status,
                    upstream_path=item.upstream_path,
                    occurred_at=item.occurred_at,
                )
            )
            accepted.append(item.event_id)
        await self.db.commit()
        return RuntimeAdmissionAuditResult(accepted, duplicates, rejected)


def _item_digest(item: RuntimeAdmissionAuditItem) -> str:
    return canonical_payload_digest(item.model_dump(mode="json", exclude_none=True))


def _optional_int(value: object) -> int | None:
    return int(value) if value is not None else None


__all__ = [
    "RuntimeAdmissionAuditService",
    "RuntimeAdmissionAuditResult",
    "RuntimeProjectionAuthenticationError",
    "RuntimeProjectionBindingError",
]
