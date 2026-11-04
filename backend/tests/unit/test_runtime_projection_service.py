from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.models import (
    ImChannel,
    ImOutboundDelivery,
    RuntimeProjectionReceipt,
)
from app.im_gateway.identity import opaque_digest
from app.schemas.runtime_internal import (
    RuntimeProjectionBatch,
    RuntimeProjectionItem,
    canonical_payload_digest,
)
from app.services.runtime_projection import (
    RuntimeProjectionAuthenticationError,
    RuntimeProjectionBindingError,
    RuntimeProjectionFileError,
    RuntimeProjectionService,
    _scheduled_wecom_delivery_content,
)
from app.services.runtime_workspace import RuntimeApiKeyCodec


class _NestedTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


def test_scheduled_wecom_delivery_content_preserves_prompt_and_failure_notice() -> None:
    content = _scheduled_wecom_delivery_content(
        request_payload={"message": "检查 **生产告警**\n- 汇总风险"},
        source={"scheduled_task_name": "每日巡检"},
        result="定时任务执行失败，请在 Web 端查看详情。",
    )

    assert content == (
        "### 定时任务执行结果\n\n"
        "**任务名称：** 每日巡检\n\n"
        "**任务内容：**\n"
        "检查 **生产告警**\n- 汇总风险\n\n"
        "### 执行结果\n\n"
        "定时任务执行失败，请在 Web 端查看详情。"
    )


def test_scheduled_wecom_delivery_content_has_stable_legacy_fallbacks() -> None:
    content = _scheduled_wecom_delivery_content(
        request_payload={},
        source={},
        result="",
    )

    assert "**任务名称：** 未命名定时任务" in content
    assert "**任务内容：**\n未提供任务内容" in content
    assert content.endswith("定时任务已执行完成。")


class FakeProjectionDb:
    def __init__(self) -> None:
        self.binding = SimpleNamespace(
            id=17,
            runtime_id="runtime-1",
            api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-secret"),
        )
        self.records: list[RuntimeProjectionReceipt] = []
        self.pending: RuntimeProjectionReceipt | None = None
        self.commits = 0
        self.refreshed: list[Any] = []

    async def scalar(self, statement: Any) -> Any:
        sql = str(statement)
        params = statement.compile().params
        if "FROM hermes_runtime_binding" in sql:
            runtime_id = next(iter(params.values()))
            return self.binding if runtime_id == self.binding.runtime_id else None
        if "FROM application_agent_runtime_binding" in sql:
            return None
        if "runtime_projection_receipt.projection_id =" in sql:
            projection_id = next(
                value
                for key, value in params.items()
                if "projection_id" in key
            )
            return next(
                (
                    record
                    for record in self.records
                    if record.projection_id == projection_id
                ),
                None,
            )
        if "runtime_projection_receipt.runtime_id =" in sql:
            values = list(params.values())
            runtime_id, run_id, seq = values
            return next(
                (
                    record
                    for record in self.records
                    if record.runtime_id == runtime_id
                    and record.run_id == run_id
                    and record.seq == seq
                ),
                None,
            )
        raise AssertionError(sql)

    def add(self, record: RuntimeProjectionReceipt) -> None:
        self.pending = record

    async def flush(self) -> None:
        assert self.pending is not None
        self.records.append(self.pending)
        self.pending = None

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction()

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, value: Any) -> None:
        self.refreshed.append(value)


def _batch() -> RuntimeProjectionBatch:
    payload = {
        "run_id": "run-00000001",
        "state": "COMPLETED",
        "terminal_seq": 8,
    }
    return RuntimeProjectionBatch.model_validate(
        {
            "schema_version": 1,
            "runtime_id": "runtime-1",
            "binding_id": "17",
            "items": [
                {
                    "projection_id": "run-00000001:terminal",
                    "runtime_id": "runtime-1",
                    "projection_type": "run.terminal",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "payload_digest": canonical_payload_digest(payload),
                    "run_id": "run-00000001",
                    "seq": 8,
                    "config_revision": "revision-18",
                    "payload": payload,
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_projection_authentication_and_idempotent_receipt() -> None:
    db = FakeProjectionDb()
    service = RuntimeProjectionService(
        db,  # type: ignore[arg-type]
        materialize=False,
    )
    await service.authenticate(
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="runtime-secret",
    )
    first = await service.project_batch(_batch())
    repeated = await service.project_batch(_batch())

    assert first.accepted_projection_ids == ["run-00000001:terminal"]
    assert repeated.accepted_projection_ids == ["run-00000001:terminal"]
    assert len(db.records) == 1
    assert db.records[0].payload["state"] == "COMPLETED"

    with pytest.raises(RuntimeProjectionBindingError):
        await service.authenticate(
            runtime_id="runtime-1",
            binding_id="18",
            api_server_key="runtime-secret",
        )
    with pytest.raises(RuntimeProjectionAuthenticationError):
        await service.authenticate(
            runtime_id="runtime-1",
            binding_id="17",
            api_server_key="wrong-secret",
        )


@pytest.mark.asyncio
async def test_file_rejection_isolated_and_refreshes_cached_binding() -> None:
    class RejectingProjectionService(RuntimeProjectionService):
        async def _materialize(self, item: RuntimeProjectionItem) -> None:
            del item
            raise RuntimeProjectionFileError("mismatched file intent")

    db = FakeProjectionDb()
    service = RejectingProjectionService(db)  # type: ignore[arg-type]
    await service.authenticate(
        runtime_id="runtime-1",
        binding_id="17",
        api_server_key="runtime-secret",
    )

    result = await service.project_batch(_batch())

    assert result.accepted_projection_ids == []
    assert [(item.projection_id, item.error_code) for item in result.rejected] == [
        ("run-00000001:terminal", "FILE_EVENT_INVALID")
    ]
    assert db.refreshed == [db.binding]


async def test_file_ready_projection_activates_matching_pending_metadata() -> None:
    artifact = SimpleNamespace(
        id=19,
        org_id=3,
        user_id=7,
        runtime_workspace_id=11,
        session_id=23,
        relative_path="workspace/sessions/session-1/19/report.txt",
        status="pending",
        size_bytes=17,
        sha256_digest=None,
        mime_type=None,
    )
    session = SimpleNamespace(id=23, runtime_session_id="session-1")

    class FileProjectionDb:
        flushed = False

        async def scalar(self, statement: Any) -> Any:
            sql = str(statement)
            if "FROM chat_file_artifact" in sql:
                return artifact
            if "FROM runtime_session" in sql:
                return session
            raise AssertionError(sql)

        async def flush(self) -> None:
            self.flushed = True

    payload = {
        "file_id": 19,
        "user_id": 7,
        "org_id": 3,
        "session_id": "session-1",
        "relative_path": "workspace/sessions/session-1/19/report.txt",
        "display_name": "report.txt",
        "mime_type": "text/plain",
        "size_bytes": 17,
        "sha256": "a" * 64,
    }
    item = RuntimeProjectionItem(
        projection_id="file:19:ready:" + "a" * 32,
        runtime_id="runtime-1",
        projection_type="file.ready",
        occurred_at=datetime.now(timezone.utc),
        payload_digest=canonical_payload_digest(payload),
        payload=payload,
    )
    db = FileProjectionDb()
    service = RuntimeProjectionService(db)  # type: ignore[arg-type]

    await service._materialize_file_status(
        item,
        SimpleNamespace(org_id=3, user_id=7, runtime_workspace_id=11),
    )

    assert artifact.status == "active"
    assert artifact.sha256_digest == "a" * 64
    assert artifact.mime_type == "text/plain"
    assert db.flushed is True


@pytest.mark.asyncio
async def test_late_file_ready_recovers_matching_failed_metadata() -> None:
    artifact = SimpleNamespace(
        id=19,
        org_id=3,
        user_id=7,
        runtime_workspace_id=11,
        session_id=23,
        relative_path="workspace/sessions/session-1/19/report.txt",
        status="failed",
        size_bytes=17,
        sha256_digest=None,
        mime_type=None,
    )
    session = SimpleNamespace(id=23, runtime_session_id="session-1")

    class FileProjectionDb:
        flushed = False

        async def scalar(self, statement: Any) -> Any:
            sql = str(statement)
            if "FROM chat_file_artifact" in sql:
                return artifact
            if "FROM runtime_session" in sql:
                return session
            raise AssertionError(sql)

        async def flush(self) -> None:
            self.flushed = True

    payload = {
        "file_id": 19,
        "user_id": 7,
        "org_id": 3,
        "session_id": "session-1",
        "relative_path": "workspace/sessions/session-1/19/report.txt",
        "display_name": "report.txt",
        "mime_type": "text/plain",
        "size_bytes": 17,
        "sha256": "a" * 64,
    }
    item = RuntimeProjectionItem(
        projection_id="file:19:ready:" + "a" * 32,
        runtime_id="runtime-1",
        projection_type="file.ready",
        occurred_at=datetime.now(timezone.utc),
        payload_digest=canonical_payload_digest(payload),
        payload=payload,
    )
    db = FileProjectionDb()

    await RuntimeProjectionService(
        db,  # type: ignore[arg-type]
    )._materialize_file_status(
        item,
        SimpleNamespace(org_id=3, user_id=7, runtime_workspace_id=11),
    )

    assert artifact.status == "active"
    assert artifact.sha256_digest == "a" * 64
    assert artifact.mime_type == "text/plain"
    assert db.flushed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("projection_type", "payload"),
    [
        (
            "run.accepted",
            {
                "run_id": "run-removed-session",
                "session_id": "session-removed",
                "accepted_at": datetime.now(timezone.utc).isoformat(),
            },
        ),
        (
            "run.event",
            {
                "run_id": "run-removed-session",
                "seq": 1,
                "event_type": "assistant.delta",
                "event_data": {},
            },
        ),
    ],
)
async def test_obsolete_personal_projection_is_accepted_as_noop(
    projection_type: str,
    payload: dict[str, object],
) -> None:
    """Old Runtime outbox entries must not block later conversations."""

    class MissingTargetDb:
        async def scalar(self, statement: Any) -> None:
            sql = str(statement)
            assert "FROM runtime_run" in sql or "FROM runtime_session" in sql
            return None

    item = RuntimeProjectionItem(
        projection_id=f"{payload['run_id']}:{projection_type}",
        runtime_id="runtime-1",
        projection_type=projection_type,
        occurred_at=datetime.now(timezone.utc),
        payload_digest=canonical_payload_digest(payload),
        run_id=str(payload["run_id"]),
        seq=int(payload.get("seq") or 0) or None,
        payload=payload,
    )

    await RuntimeProjectionService(
        MissingTargetDb(),  # type: ignore[arg-type]
    )._materialize_personal(
        item,
        SimpleNamespace(org_id=3, user_id=7, runtime_workspace_id=11),
    )


@pytest.mark.asyncio
async def test_personal_acceptance_promotes_reserved_wecom_run() -> None:
    accepted_at = datetime.now(timezone.utc)
    run = SimpleNamespace(
        status="pending",
        started_at=None,
        model_entry_id=None,
        request_payload={},
    )

    class ExistingRunDb:
        async def scalar(self, statement: Any) -> Any:
            assert "FROM runtime_run" in str(statement)
            return run

    payload = {
        "run_id": "rr-im-wecom-101",
        "session_id": "rs-wecom-1",
        "accepted_at": accepted_at.isoformat(),
        "model_id": 19,
        "source": "personal_wecom",
        "client_message_id": "im-wecom-101",
        "config_revision": "cfg-1",
        "history_message_count_before_run": 12,
        "source_detail": {
            "type": "scheduled",
            "scheduled_task_id": 7,
            "scheduled_task_name": "daily",
            "trigger_kind": "scheduled",
            "task_available": True,
        },
    }
    item = RuntimeProjectionItem(
        projection_id="rr-im-wecom-101:accepted",
        runtime_id="runtime-1",
        projection_type="run.accepted",
        occurred_at=accepted_at,
        payload_digest=canonical_payload_digest(payload),
        run_id="rr-im-wecom-101",
        payload=payload,
    )
    binding = SimpleNamespace(
        org_id=3,
        user_id=7,
        runtime_workspace_id=11,
        active_run_count=0,
    )

    await RuntimeProjectionService(
        ExistingRunDb(),  # type: ignore[arg-type]
    )._materialize_personal(item, binding)

    assert run.status == "running"
    assert run.started_at == accepted_at
    assert run.request_payload["history_message_count_before_run"] == 12
    assert run.request_payload["source"]["scheduled_task_id"] == 7
    assert run.model_entry_id == 19
    assert binding.active_run_count == 1


@pytest.mark.asyncio
async def test_personal_acceptance_promotes_queued_scheduled_run() -> None:
    accepted_at = datetime.now(timezone.utc)
    run = SimpleNamespace(
        status="queued",
        started_at=None,
        model_entry_id=19,
        request_payload={"source": {"type": "scheduled"}},
    )

    class ExistingRunDb:
        async def scalar(self, statement: Any) -> Any:
            assert "FROM runtime_run" in str(statement)
            return run

    payload = {
        "run_id": "rr-scheduled-101",
        "session_id": "rs-scheduled-1",
        "accepted_at": accepted_at.isoformat(),
        "source": "scheduled_task",
        "client_message_id": "scheduled-message-101",
        "config_revision": "cfg-1",
    }
    item = RuntimeProjectionItem(
        projection_id="rr-scheduled-101:accepted",
        runtime_id="runtime-1",
        projection_type="run.accepted",
        occurred_at=accepted_at,
        payload_digest=canonical_payload_digest(payload),
        run_id="rr-scheduled-101",
        payload=payload,
    )
    binding = SimpleNamespace(
        org_id=3,
        user_id=7,
        runtime_workspace_id=11,
        active_run_count=0,
    )

    await RuntimeProjectionService(
        ExistingRunDb(),  # type: ignore[arg-type]
    )._materialize_personal(item, binding)

    assert run.status == "running"
    assert run.started_at == accepted_at
    assert binding.active_run_count == 1


@pytest.mark.asyncio
async def test_personal_queued_projection_enriches_precreated_scheduled_run() -> None:
    run = SimpleNamespace(status="queued", request_payload={"message": "summary"})

    class ExistingRunDb:
        async def scalar(self, statement: Any) -> Any:
            assert "FROM runtime_run" in str(statement)
            return run

    source_detail = {
        "type": "scheduled",
        "scheduled_task_id": 7,
        "scheduled_task_name": "daily",
        "trigger_kind": "scheduled",
        "task_available": True,
    }
    payload = {
        "run_id": "rr-scheduled-queued",
        "session_id": "rs-scheduled-1",
        "client_message_id": "scheduled-message-queued",
        "config_revision": "cfg-1",
        "source": "scheduled_task",
        "source_detail": source_detail,
        "prompt": "summary",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    item = RuntimeProjectionItem(
        projection_id="rr-scheduled-queued:queued",
        runtime_id="runtime-1",
        projection_type="run.queued",
        occurred_at=datetime.now(timezone.utc),
        payload_digest=canonical_payload_digest(payload),
        run_id="rr-scheduled-queued",
        payload=payload,
    )

    await RuntimeProjectionService(
        ExistingRunDb(),  # type: ignore[arg-type]
    )._materialize_personal(
        item,
        SimpleNamespace(org_id=3, user_id=7, runtime_workspace_id=11),
    )

    assert run.status == "queued"
    assert run.request_payload["source"] == source_detail
    assert run.request_payload["scheduled_task_id"] == 7


@pytest.mark.asyncio
async def test_application_queued_projection_materializes_fast_lane_run() -> None:
    enqueued_at = datetime.now(timezone.utc)

    class MissingApplicationRunDb:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.flushed = False

        async def scalar(self, statement: Any) -> None:
            assert "FROM application_agent_run" in str(statement)
            return None

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            self.flushed = True

    payload = {
        "run_id": "app-run-fast-lane-1",
        "session_id": "app-session-1",
        "client_message_id": "im-message-1",
        "source": "application_wecom",
        "user_id": 7,
        "enqueued_at": enqueued_at.isoformat(),
    }
    item = RuntimeProjectionItem(
        projection_id="app-run-fast-lane-1:queued",
        runtime_id="application-runtime-1",
        projection_type="run.queued",
        occurred_at=enqueued_at,
        payload_digest=canonical_payload_digest(payload),
        run_id="app-run-fast-lane-1",
        payload=payload,
    )
    binding = SimpleNamespace(
        id=17,
        org_id=3,
        application_agent_id=25,
        active_run_count=0,
        queued_run_count=0,
    )
    db = MissingApplicationRunDb()

    await RuntimeProjectionService(
        db,  # type: ignore[arg-type]
    )._materialize_application(item, binding)

    assert db.flushed is True
    assert len(db.added) == 1
    run = db.added[0]
    assert run.runtime_run_id == "app-run-fast-lane-1"
    assert run.runtime_session_id == "app-session-1"
    assert run.channel == "wecom"
    assert run.status == "queued"
    assert run.submission_state == "reserved"
    assert binding.queued_run_count == 1
    assert binding.active_run_count == 0


@pytest.mark.asyncio
async def test_application_web_projection_is_recorded_as_studio() -> None:
    class MissingApplicationRunDb:
        def __init__(self) -> None:
            self.added: list[Any] = []

        async def scalar(self, statement: Any) -> None:
            sql = str(statement)
            if "FROM application_agent_run" in sql:
                return None
            if "FROM application_agent_studio_session" in sql:
                return SimpleNamespace(status="active", last_active_at=None)
            raise AssertionError(sql)

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            return None

    occurred_at = datetime.now(timezone.utc)
    payload = {
        "run_id": "app-run-studio-1",
        "session_id": "studio-25-7-session",
        "client_message_id": "studio-message-1",
        "source": "web",
        "user_id": 7,
        "enqueued_at": occurred_at.isoformat(),
    }
    item = RuntimeProjectionItem(
        projection_id="app-run-studio-1:queued",
        runtime_id="application-runtime-1",
        projection_type="run.queued",
        occurred_at=occurred_at,
        payload_digest=canonical_payload_digest(payload),
        run_id="app-run-studio-1",
        payload=payload,
    )
    binding = SimpleNamespace(
        id=17,
        org_id=3,
        application_agent_id=25,
        active_run_count=0,
        queued_run_count=0,
    )
    db = MissingApplicationRunDb()

    await RuntimeProjectionService(
        db,  # type: ignore[arg-type]
    )._materialize_application(item, binding)

    assert len(db.added) == 1
    assert db.added[0].channel == "studio"


def test_personal_web_projection_channel_remains_web() -> None:
    from app.services.runtime_projection import _projection_channel

    assert _projection_channel("web") == "web"


@pytest.mark.asyncio
async def test_application_queued_run_counters_move_on_accept_and_terminal() -> None:
    accepted_at = datetime.now(timezone.utc)
    run = SimpleNamespace(
        status="queued",
        capacity_released_at=None,
        submission_state="reserved",
        dashboard_accepted_at=None,
        runtime_session_id="app-session-1",
        active_session_key="app-session-1",
        finished_at=None,
        error_code=None,
    )

    class ExistingApplicationRunDb:
        async def scalar(self, statement: Any) -> Any:
            sql = str(statement)
            if "FROM application_agent_run" in sql:
                return run
            if "FROM application_agent_studio_session" in sql:
                return None
            raise AssertionError(sql)

        async def flush(self) -> None:
            return None

    binding = SimpleNamespace(
        id=17,
        org_id=3,
        application_agent_id=25,
        active_run_count=0,
        queued_run_count=1,
    )
    service = RuntimeProjectionService(
        ExistingApplicationRunDb(),  # type: ignore[arg-type]
    )
    accepted_payload = {
        "run_id": "app-run-fast-lane-1",
        "session_id": "app-session-1",
        "source": "application_wecom",
        "accepted_at": accepted_at.isoformat(),
    }
    await service._materialize_application(
        RuntimeProjectionItem(
            projection_id="app-run-fast-lane-1:accepted",
            runtime_id="application-runtime-1",
            projection_type="run.accepted",
            occurred_at=accepted_at,
            payload_digest=canonical_payload_digest(accepted_payload),
            run_id="app-run-fast-lane-1",
            payload=accepted_payload,
        ),
        binding,
    )
    assert run.status == "running"
    assert binding.queued_run_count == 0
    assert binding.active_run_count == 1

    terminal_payload = {
        "run_id": "app-run-fast-lane-1",
        "state": "FAILED",
        "error_code": "RUNTIME_ADMISSION_DISPATCH_INTENT_FAILED",
        "terminal_at": accepted_at.isoformat(),
    }
    await service._materialize_application(
        RuntimeProjectionItem(
            projection_id="app-run-fast-lane-1:terminal",
            runtime_id="application-runtime-1",
            projection_type="run.terminal",
            occurred_at=accepted_at,
            payload_digest=canonical_payload_digest(terminal_payload),
            run_id="app-run-fast-lane-1",
            payload=terminal_payload,
        ),
        binding,
    )
    assert run.status == "failed"
    assert run.error_code == "RUNTIME_ADMISSION_DISPATCH_INTENT_FAILED"
    assert binding.queued_run_count == 0
    assert binding.active_run_count == 0


@pytest.mark.asyncio
async def test_sessionless_worker_usage_is_projected_without_session() -> None:
    run = SimpleNamespace(
        id=41,
        session_id=None,
        request_payload={"client_message_id": "kanban:t_123"},
        model_entry_id=19,
    )

    class SessionlessWorkerDb:
        def __init__(self) -> None:
            self.added: list[Any] = []

        async def scalar(self, statement: Any) -> Any:
            sql = str(statement)
            if "FROM runtime_run" in sql:
                return run
            if "FROM model_entry" in sql:
                return None
            raise AssertionError(sql)

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            return None

    payload = {
        "run_id": "kw-000000000000000000000001",
        "request_id": "kanban:t_123",
        "usage": {"prompt_tokens": 7, "completion_tokens": 11},
    }
    item = RuntimeProjectionItem(
        projection_id="kw-000000000000000000000001:usage",
        runtime_id="runtime-1",
        projection_type="run.usage",
        occurred_at=datetime.now(timezone.utc),
        payload_digest=canonical_payload_digest(payload),
        run_id="kw-000000000000000000000001",
        payload=payload,
    )
    db = SessionlessWorkerDb()

    await RuntimeProjectionService(
        db,  # type: ignore[arg-type]
    )._materialize_personal(
        item,
        SimpleNamespace(org_id=3, user_id=7, runtime_workspace_id=11),
    )

    assert len(db.added) == 1
    assert db.added[0].session_id is None
    assert db.added[0].run_id == 41


@pytest.mark.asyncio
async def test_personal_scheduled_acceptance_preserves_wecom_target_for_terminal_outbox() -> None:
    terminal_at = datetime.now(timezone.utc)
    run = SimpleNamespace(
        id=41,
        runtime_run_id="run-review-0001",
        session_id=23,
        status="queued",
        started_at=None,
        model_entry_id=19,
        finished_at=None,
        response_summary={},
        error_code=None,
        self_evolution_status=None,
        self_evolution_activation_deadline_at=None,
        self_evolution_started_at=None,
        self_evolution_last_activity_at=None,
        self_evolution_deadline_at=None,
        self_evolution_error_message=None,
        request_payload={
            "message": "生成今天的完整日报\n包含风险和明日计划。",
            "source": {
                "type": "scheduled",
                "scheduled_task_id": 7,
                "im_channel_id": 51,
                "im_bot_id_digest": opaque_digest("bot-51"),
                "im_target_digest": "d" * 64,
            },
        },
    )
    session = SimpleNamespace(
        id=23,
        last_active_at=None,
        message_count_cache=0,
    )
    scheduled_task = SimpleNamespace(
        id=7,
        task_name="已修改的任务名称",
        prompt="已修改的任务内容",
        last_run_at=None,
        last_run_status="queued",
        last_error_code=None,
        last_error=None,
        last_runtime_run_id=run.runtime_run_id,
        run_count=0,
    )
    channel = ImChannel(
        id=51,
        target_type="personal_agent",
        provider="wecom",
        enabled=True,
        bot_id="bot-51",
        secret_encrypted="secret-ciphertext",
        status="connected",
        runtime_session_row_id=23,
        proactive_target_encrypted="target-ciphertext",
        proactive_target_digest="d" * 64,
        proactive_target_confirmed_at=terminal_at,
    )

    class _ScalarRows:
        def scalars(self) -> "_ScalarRows":
            return self

        def __iter__(self):
            return iter([701, 702])

    class ReviewProjectionDb:
        def __init__(self) -> None:
            self.events: list[Any] = []
            self.flushed = 0

        async def scalar(self, statement: Any) -> Any:
            sql = str(statement)
            if "FROM runtime_run_event" in sql:
                return 9
            if "FROM runtime_run" in sql:
                return run
            if "FROM runtime_session" in sql:
                return session
            if "FROM scheduled_task" in sql:
                return scheduled_task
            if "FROM im_channel" in sql:
                return channel
            if "FROM im_outbound_delivery" in sql:
                return None
            raise AssertionError(sql)

        async def execute(self, statement: Any) -> Any:
            sql = str(statement)
            if "FROM chat_file_artifact" in sql:
                return _ScalarRows()
            raise AssertionError(sql)

        def add(self, value: Any) -> None:
            self.events.append(value)

        async def flush(self) -> None:
            self.flushed += 1

    db = ReviewProjectionDb()
    service = RuntimeProjectionService(db)  # type: ignore[arg-type]
    binding = SimpleNamespace(
        org_id=3,
        user_id=7,
        runtime_workspace_id=11,
        active_run_count=0,
    )
    accepted_payload = {
        "run_id": run.runtime_run_id,
        "session_id": "session-1",
        "source": "scheduled_task",
        "accepted_at": terminal_at.isoformat(),
        "source_detail": {
            "type": "scheduled",
            "scheduled_task_id": 7,
            "scheduled_task_name": "每日工作总结",
            "trigger_kind": "scheduled",
            "task_available": True,
        },
    }
    await service._materialize_personal(
        RuntimeProjectionItem(
            projection_id=f"{run.runtime_run_id}:accepted",
            runtime_id="runtime-1",
            projection_type="run.accepted",
            occurred_at=terminal_at,
            payload_digest=canonical_payload_digest(accepted_payload),
            run_id=run.runtime_run_id,
            payload=accepted_payload,
        ),
        binding,
    )
    assert run.request_payload["source"]["im_channel_id"] == 51
    assert run.request_payload["source"]["im_bot_id_digest"] == opaque_digest(
        "bot-51"
    )
    assert run.request_payload["source"]["im_target_digest"] == "d" * 64

    terminal_payload = {
        "run_id": run.runtime_run_id,
        "state": "COMPLETED",
        "answer": "done",
        "terminal_seq": 9,
        "terminal_at": terminal_at.isoformat(),
    }
    terminal_item = RuntimeProjectionItem(
        projection_id=f"{run.runtime_run_id}:terminal",
        runtime_id="runtime-1",
        projection_type="run.terminal",
        occurred_at=terminal_at,
        payload_digest=canonical_payload_digest(terminal_payload),
        run_id=run.runtime_run_id,
        seq=9,
        payload=terminal_payload,
    )

    await service._materialize_personal(
        terminal_item,
        binding,
    )
    assert run.status == "completed"
    assert run.self_evolution_status == "eligible"
    assert run.self_evolution_activation_deadline_at > terminal_at
    assert run.self_evolution_deadline_at > terminal_at
    assert scheduled_task.last_run_status == "success"
    assert scheduled_task.last_runtime_run_id == run.runtime_run_id
    assert scheduled_task.run_count == 1
    deliveries = [
        event for event in db.events
        if isinstance(event, ImOutboundDelivery)
    ]
    assert len(deliveries) == 1
    assert deliveries[0].delivery_key == "scheduled:7:run-review-0001"
    assert deliveries[0].artifact_ids == [701, 702]
    assert deliveries[0].content == (
        "### 定时任务执行结果\n\n"
        "**任务名称：** 每日工作总结\n\n"
        "**任务内容：**\n"
        "生成今天的完整日报\n包含风险和明日计划。\n\n"
        "### 执行结果\n\n"
        "done"
    )

    await service._materialize_personal(terminal_item, binding)
    assert len(
        [
            event for event in db.events
            if isinstance(event, ImOutboundDelivery)
        ]
    ) == 1

    activity_at = terminal_at.replace(microsecond=0)
    activity_payload = {
        "run_id": run.runtime_run_id,
        "request_id": "review-request-1",
        "observed_at": activity_at.isoformat(),
    }
    activity_item = RuntimeProjectionItem(
        projection_id=f"{run.runtime_run_id}:self-review:request-1",
        runtime_id="runtime-1",
        projection_type="run.self_evolution.activity",
        occurred_at=activity_at,
        payload_digest=canonical_payload_digest(activity_payload),
        run_id=run.runtime_run_id,
        payload=activity_payload,
    )
    await service._materialize_self_evolution_activity(
        item=activity_item,
        run=run,  # type: ignore[arg-type]
    )

    assert run.self_evolution_status == "running"
    assert run.self_evolution_last_activity_at == activity_at
    review_events = [
        event for event in db.events
        if getattr(event, "event_name", None) == "chat.self_evolution.event"
    ]
    assert len(review_events) == 1
    assert review_events[0].seq == 10
    assert review_events[0].payload["status"] == "running"
