from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.models import RuntimeAdmissionAudit
from app.schemas.runtime_internal import RuntimeAdmissionAuditBatch
from app.services.runtime_admission_audit import RuntimeAdmissionAuditService
from app.services.runtime_workspace import RuntimeApiKeyCodec


class FakeDb:
    def __init__(self) -> None:
        self.binding = SimpleNamespace(
            id=17,
            runtime_id="runtime-1",
            org_id=3,
            user_id=7,
            runtime_workspace_id=11,
            api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-secret"),
        )
        self.records: list[RuntimeAdmissionAudit] = []
        self.pending: RuntimeAdmissionAudit | None = None
        self.commits = 0

    async def scalar(self, statement: Any) -> Any:
        sql = str(statement)
        if "FROM hermes_runtime_binding" in sql:
            return self.binding
        if "FROM application_agent_runtime_binding" in sql:
            return None
        if "FROM runtime_admission_audit" in sql:
            event_id = next(iter(statement.compile().params.values()))
            return next((row for row in self.records if row.event_id == event_id), None)
        raise AssertionError(sql)

    def add(self, row: RuntimeAdmissionAudit) -> None:
        self.records.append(row)

    async def commit(self) -> None:
        self.commits += 1


def batch(*, status: str = "rejected") -> RuntimeAdmissionAuditBatch:
    return RuntimeAdmissionAuditBatch.model_validate(
        {
            "schema_version": 1,
            "runtime_id": "runtime-1",
            "binding_id": "17",
            "items": [
                {
                    "event_id": "event-00000001",
                    "runtime_id": "runtime-1",
                    "occurred_at": "2026-08-11T14:12:40+00:00",
                    "operation": "run.submit",
                    "status": status,
                    "outcome": "rejected",
                    "stage": "edge.run_submit.persist_dispatch_intent",
                    "session_id": "rs-12345678",
                    "client_message_id": "message-12345678",
                    "error_code": "SESSION_BUSY",
                    "prompt_bytes": 21,
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_runtime_admission_audit_is_binding_scoped_and_idempotent() -> None:
    db = FakeDb()
    service = RuntimeAdmissionAuditService(db)  # type: ignore[arg-type]
    await service.authenticate(
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="runtime-secret",
    )

    first = await service.record_batch(batch())
    duplicate = await service.record_batch(batch())
    conflict = await service.record_batch(batch(status="different"))

    assert first.accepted_event_ids == ["event-00000001"]
    assert duplicate.duplicate_event_ids == ["event-00000001"]
    assert conflict.rejected_event_ids == ["event-00000001"]
    assert len(db.records) == 1
    assert db.records[0].org_id == 3
    assert db.records[0].prompt_bytes == 21
