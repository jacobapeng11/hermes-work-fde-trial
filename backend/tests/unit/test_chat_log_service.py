from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.dialects import mysql

from app.models import ChatLog, RuntimeRun, RuntimeRunEvent, RuntimeSession
from app.services.chat_log import (
    ChatLogProjector,
    SqlAlchemyChatLogStore,
    assistant_text,
    public_history_messages,
    reasoning_text,
    run_activity_history_messages,
    terminal_assistant_text,
)


NOW = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)


def event(seq: int, name: str, payload: dict) -> RuntimeRunEvent:
    return RuntimeRunEvent(
        id=seq,
        run_id=10,
        runtime_run_public_id="rr-1",
        seq=seq,
        event_name=name,
        payload=payload,
    )


def run(
    run_id: int,
    runtime_run_id: str,
    message: str,
    *,
    status: str = "completed",
    channel: str = "web",
) -> RuntimeRun:
    return RuntimeRun(
        id=run_id,
        org_id=7,
        user_id=9,
        runtime_workspace_id=3,
        session_id=5,
        runtime_session_id="rs-3-a",
        runtime_run_id=runtime_run_id,
        model_entry_id=2,
        status=status,
        source_type="web",
        request_payload={
            "message": f"runtime:{message}",
            "audit_message": message,
            "channel": channel,
        },
        started_at=NOW,
        finished_at=NOW if status == "completed" else None,
    )


class FakeChatLogStore:
    def __init__(
        self,
        *,
        session: RuntimeSession,
        runs: list[RuntimeRun] | None = None,
        logs: list[ChatLog] | None = None,
        events: dict[int, list[RuntimeRunEvent]] | None = None,
    ) -> None:
        self.session = session
        self.runs = list(runs or [])
        self.logs = list(logs or [])
        self.events = dict(events or {})
        self.commits = 0
        self.rollbacks = 0
        self.lock_calls: list[str] = []

    async def list_pending_session_ids(self, *, limit: int) -> list[int]:
        return [int(self.session.id)][:limit]

    async def get_owned_session(self, **kwargs) -> RuntimeSession | None:
        if (
            kwargs["org_id"] == self.session.org_id
            and kwargs["user_id"] == self.session.user_id
            and kwargs["runtime_workspace_id"] == self.session.runtime_workspace_id
            and kwargs["runtime_session_id"] == self.session.runtime_session_id
        ):
            return self.session
        return None

    async def lock_owned_session_for_projection(
        self,
        **kwargs,
    ) -> RuntimeSession | None:
        self.lock_calls.append("owned_projection")
        return await self.get_owned_session(**kwargs)

    async def lock_session_for_projection(
        self,
        *,
        session_id: int,
    ) -> RuntimeSession | None:
        self.lock_calls.append("session_projection")
        return self.session if session_id == self.session.id else None

    async def list_session_runs(self, *, session_id: int) -> list[RuntimeRun]:
        return sorted(
            [item for item in self.runs if item.session_id == session_id],
            key=lambda item: item.id,
        )

    async def list_session_logs(self, *, session_id: int) -> list[ChatLog]:
        return sorted(
            [item for item in self.logs if item.session_id == session_id],
            key=lambda item: item.message_seq,
        )

    async def list_run_events(self, *, run_id: int) -> list[RuntimeRunEvent]:
        return sorted(self.events.get(run_id, []), key=lambda item: item.seq)

    def add_log(self, record: ChatLog) -> None:
        self.logs.append(record)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class ProjectionLockResult:
    def __init__(self, value) -> None:
        self.value = value

    def one_or_none(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class ProjectionLockDb:
    def __init__(self, session: RuntimeSession) -> None:
        self.session = session
        self.statements = []
        self.execute_calls = 0

    async def execute(self, statement):
        self.statements.append(statement)
        self.execute_calls += 1
        if self.execute_calls == 1:
            return ProjectionLockResult(
                SimpleNamespace(
                    org_id=self.session.org_id,
                    user_id=self.session.user_id,
                    runtime_workspace_id=(
                        self.session.runtime_workspace_id
                    ),
                )
            )
        return ProjectionLockResult(self.session)

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.session.runtime_workspace_id


def complete_session() -> RuntimeSession:
    return RuntimeSession(
        id=5,
        org_id=7,
        user_id=9,
        runtime_workspace_id=3,
        runtime_session_id="rs-3-a",
        source_type="web",
        message_count_cache=0,
        status="active",
        chat_log_backfilled_at=NOW,
    )


async def test_sql_projection_lock_claims_parent_before_session() -> None:
    session = complete_session()
    db = ProjectionLockDb(session)
    store = SqlAlchemyChatLogStore(db)  # type: ignore[arg-type]

    locked = await store.lock_session_for_projection(
        session_id=int(session.id)
    )

    assert locked is session
    statements = [
        str(
            statement.compile(
                dialect=mysql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        for statement in db.statements
    ]
    assert "FROM runtime_session" in statements[0]
    assert "FOR UPDATE" not in statements[0]
    assert "FROM cloud_hermes_profile" in statements[1]
    assert "LOCK IN SHARE MODE" in statements[1]
    assert "FROM runtime_session" in statements[2]
    assert "FOR UPDATE" in statements[2]


def test_assistant_text_uses_only_message_deltas_in_sequence_order() -> None:
    events = [
        event(3, "chat.tool.event", {"result": "secret tool result"}),
        event(1, "chat.reasoning.delta", {"delta": "secret thought"}),
        event(4, "chat.message.delta", {"delta": "回答"}),
        event(2, "chat.message.delta", {"delta": "最终"}),
        event(5, "chat.message.delta", {"delta": 123}),
    ]

    assert assistant_text(events) == "最终回答"


def test_terminal_assistant_text_prefers_non_interim_completed_message() -> None:
    events = [
        event(1, "chat.message.delta", {"delta": "正在检查"}),
        event(2, "chat.message.completed", {"text": "正在检查", "interim": True}),
        event(3, "chat.message.completed", {"text": "PPT 已完成，文件见下方。"}),
    ]

    assert terminal_assistant_text(events) == "PPT 已完成，文件见下方。"


def test_reasoning_text_uses_only_reasoning_deltas_in_sequence_order() -> None:
    events = [
        event(3, "chat.message.delta", {"delta": "visible"}),
        event(2, "chat.reasoning.delta", {"delta": "第二步"}),
        event(1, "chat.reasoning.delta", {"delta": "第一步"}),
        event(4, "chat.reasoning.delta", {"delta": 123}),
    ]

    assert reasoning_text(events) == "第一步第二步"


def test_run_activity_history_messages_merges_tool_updates_and_preserves_activity_order() -> None:
    events = [
        event(1, "chat.reasoning.delta", {"delta": "先规划"}),
        event(2, "chat.tool.event", {"name": "write_file", "callId": "call-1", "hasStableCallId": True, "status": "running", "preview": "正在写入"}),
        event(3, "chat.tool.event", {"name": "write_file", "callId": "call-1", "hasStableCallId": True, "status": "completed", "input": "{\"path\":\"<path>/报告.docx\"}", "result": "已写入"}),
        event(4, "chat.subagent.event", {"type": "subagent.complete", "subagent_id": "sub-1", "summary": "视觉检查完成"}),
        event(5, "chat.reasoning.delta", {"delta": "再确认"}),
        event(6, "internal.debug", {"trace": "private"}),
    ]

    assert run_activity_history_messages(events) == [
        {"role": "assistant", "kind": "reasoning", "text": "先规划"},
        {"role": "tool", "name": "write_file", "callId": "call-1", "hasStableCallId": True, "status": "completed", "input": "{\"path\":\"<path>/报告.docx\"}", "preview": "正在写入", "result": "已写入"},
        {"role": "system", "kind": "background", "taskId": "sub-1", "status": "completed", "summary": "视觉检查完成"},
        {"role": "assistant", "kind": "reasoning", "text": "再确认"},
    ]


def test_run_activity_history_messages_normalizes_failure_like_subagent_complete() -> None:
    for raw_status in ("failed", "error", "timeout"):
        messages = run_activity_history_messages(
            [
                event(
                    1,
                    "chat.subagent.event",
                    {
                        "type": "subagent.complete",
                        "subagent_id": "sub-1",
                        "status": raw_status,
                        "summary": "not completed",
                    },
                )
            ]
        )

        assert messages == [
            {
                "role": "system",
                "kind": "background",
                "taskId": "sub-1",
                "status": "failed",
                "summary": "not completed",
            }
        ]


def test_run_activity_history_messages_removes_only_terminal_answer_suffix() -> None:
    events = [
        event(1, "chat.activity.delta", {"delta": "正在检查文件。PPT 已完成。"}),
        event(2, "chat.message.completed", {"text": "PPT 已完成。"}),
    ]

    assert run_activity_history_messages(events) == [
        {"role": "assistant", "kind": "activity", "text": "正在检查文件。"},
    ]


def test_run_activity_history_messages_drops_activity_equal_to_terminal_answer() -> None:
    events = [
        event(1, "chat.activity.delta", {"delta": "你好，有什么可以帮你？"}),
        event(2, "chat.message.completed", {"text": "你好，有什么可以帮你？"}),
    ]

    assert run_activity_history_messages(events) == []


def test_run_activity_history_ignores_subagent_events_without_stable_identity() -> None:
    messages = run_activity_history_messages([
        event(1, "chat.subagent.event", {"type": "subagent.progress", "tool_preview": "terminal"}),
        event(2, "chat.subagent.event", {"type": "subagent.start", "task_index": 4, "goal": "内部步骤"}),
        event(3, "chat.subagent.event", {"type": "unexpected", "subagent_id": "sub-1"}),
    ])

    assert not any(item.get("kind") == "background" for item in messages)

def test_public_history_messages_keeps_only_nonempty_user_and_assistant_text() -> None:
    assert public_history_messages(
        [
            {"role": "user", "content": "问题"},
            {"role": "tool", "content": "secret"},
            {"role": "assistant", "content": "答案", "reasoning": "secret"},
            {"role": "system", "content": "internal"},
            {"role": "assistant", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] private summary"},
            {"role": "assistant", "content": ""},
        ]
    ) == [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "答案"},
    ]


async def test_project_session_processes_missing_runs_in_run_order_and_is_idempotent() -> None:
    first = run(10, "rr-first", "问题一")
    second = run(11, "rr-second", "问题二")
    store = FakeChatLogStore(
        session=complete_session(),
        runs=[second, first],
        events={
            10: [event(1, "chat.message.delta", {"delta": "答案一"})],
            11: [event(1, "chat.message.delta", {"delta": "答案二"})],
        },
    )
    projector = ChatLogProjector(store)

    assert await projector.project_session_pending(session_id=5) == 4
    assert [(item.message_seq, item.role, item.content) for item in store.logs] == [
        (0, "user", "问题一"),
        (1, "assistant", "答案一"),
        (2, "user", "问题二"),
        (3, "assistant", "答案二"),
    ]
    assert first.chat_log_projected_at is not None
    assert second.chat_log_projected_at is not None

    assert await projector.project_session_pending(session_id=5) == 0
    assert len(store.logs) == 4
    assert store.rollbacks == 1


async def test_expert_collaboration_projection_hides_internal_prompt_and_keeps_final_answer() -> None:
    summary = run(
        10,
        "rr-expert-summary",
        "[平台内部通知：专家协作任务已经完成]",
        channel="expert_collaboration",
    )
    summary.source_type = "expert_collaboration"
    summary.request_payload = {
        "message": "[平台内部通知：专家协作任务已经完成]",
        "audit_message": "",
        "channel": "expert_collaboration",
    }
    store = FakeChatLogStore(
        session=complete_session(),
        runs=[summary],
        events={
            10: [
                event(
                    1,
                    "chat.message.delta",
                    {"delta": "综合专家结论：优先进入华东市场。"},
                )
            ]
        },
    )

    assert await ChatLogProjector(store).project_session_pending(
        session_id=5
    ) == 1
    assert [(item.role, item.content) for item in store.logs] == [
        ("assistant", "综合专家结论：优先进入华东市场。")
    ]


async def test_project_session_skips_legacy_session_until_backfill_is_complete() -> None:
    session = complete_session()
    session.chat_log_backfilled_at = None
    store = FakeChatLogStore(session=session, runs=[run(10, "rr-first", "问题")])

    assert await ChatLogProjector(store).project_session_pending(session_id=5) == 0
    assert store.logs == []
    assert store.commits == 0
    assert store.rollbacks == 1


async def test_project_session_persists_user_but_not_partial_assistant_for_running_run() -> None:
    active = run(10, "rr-running", "进行中的问题", status="running")
    store = FakeChatLogStore(
        session=complete_session(),
        runs=[active],
        events={10: [event(1, "chat.message.delta", {"delta": "部分回答"})]},
    )

    assert await ChatLogProjector(store).project_session_pending(session_id=5) == 1
    assert [(item.role, item.content) for item in store.logs] == [
        ("user", "进行中的问题")
    ]
    assert active.chat_log_projected_at is None


async def test_history_merges_unprojected_completed_run_tail_without_duplicates() -> None:
    completed = run(10, "rr-tail", "刚完成的问题")
    existing = ChatLog(
        id=1,
        org_id=7,
        user_id=9,
        runtime_workspace_id=3,
        session_id=5,
        runtime_session_id="rs-3-a",
        runtime_run_id="rr-old",
        message_seq=0,
        role="user",
        content="旧问题",
        content_format="text",
        message_status="accepted",
        channel="web",
        source_type="web",
        ingest_source="runtime_run",
        occurred_at=NOW,
    )
    store = FakeChatLogStore(
        session=complete_session(),
        runs=[completed],
        logs=[existing],
        events={
            10: [
                event(1, "chat.reasoning.delta", {"delta": "先分析"}),
                event(2, "chat.message.delta", {"delta": "刚完成的答案"}),
            ]
        },
    )

    result = await ChatLogProjector(store).history_for_owner(
        org_id=7,
        user_id=9,
        runtime_workspace_id=3,
        runtime_session_id="rs-3-a",
    )

    assert result.complete is True
    assert result.has_active_run is False
    assert result.messages == [
        {"role": "user", "content": "旧问题", "messageSeq": 0, "runtimeRunId": "rr-old"},
        {"role": "user", "content": "刚完成的问题", "runtimeRunId": "rr-tail"},
        {"role": "assistant", "kind": "reasoning", "text": "先分析", "runtimeRunId": "rr-tail", "eventSeq": 1},
        {"role": "assistant", "content": "刚完成的答案", "runtimeRunId": "rr-tail"},
    ]


async def test_history_marks_active_projection_and_exposes_client_identity() -> None:
    active = run(10, "rr-running", "进行中的问题", status="running")
    active.request_payload = {
        **(active.request_payload or {}),
        "client_message_id": "client-message-1",
    }
    store = FakeChatLogStore(session=complete_session(), runs=[active])

    result = await ChatLogProjector(store).history_for_owner(
        org_id=7,
        user_id=9,
        runtime_workspace_id=3,
        runtime_session_id="rs-3-a",
    )

    assert result.has_active_run is True
    assert result.messages == [{
        "role": "user",
        "content": "进行中的问题",
        "runtimeRunId": "rr-running",
        "clientMessageId": "client-message-1",
    }]


async def test_history_restores_reasoning_for_projected_assistant_log() -> None:
    completed = run(10, "rr-complete", "问题")
    completed.chat_log_projected_at = NOW
    assistant = ChatLog(
        id=1,
        org_id=7,
        user_id=9,
        runtime_workspace_id=3,
        session_id=5,
        runtime_session_id="rs-3-a",
        runtime_run_id="rr-complete",
        message_seq=0,
        role="assistant",
        content="最终答案",
        content_format="markdown",
        message_status="completed",
        channel="web",
        source_type="web",
        ingest_source="runtime_run",
        occurred_at=NOW,
        completed_at=NOW,
    )
    store = FakeChatLogStore(
        session=complete_session(),
        runs=[completed],
        logs=[assistant],
        events={
            10: [
                event(1, "chat.reasoning.delta", {"delta": "第一步"}),
                event(2, "chat.tool.event", {"name": "write_file", "callId": "call-1", "hasStableCallId": True, "status": "completed", "result": "已写入"}),
                event(3, "chat.subagent.event", {"type": "subagent.complete", "subagent_id": "sub-1", "summary": "检查完成"}),
                event(4, "chat.message.delta", {"delta": "最终答案"}),
                event(5, "chat.reasoning.delta", {"delta": "第二步"}),
            ]
        },
    )

    result = await ChatLogProjector(store).history_for_owner(
        org_id=7,
        user_id=9,
        runtime_workspace_id=3,
        runtime_session_id="rs-3-a",
    )

    assert result.messages == [
        {"role": "assistant", "kind": "reasoning", "text": "第一步", "runtimeRunId": "rr-complete", "eventSeq": 1},
        {"role": "tool", "name": "write_file", "callId": "call-1", "hasStableCallId": True, "status": "completed", "result": "已写入", "runtimeRunId": "rr-complete", "eventSeq": 2},
        {"role": "system", "kind": "background", "taskId": "sub-1", "status": "completed", "summary": "检查完成", "runtimeRunId": "rr-complete", "eventSeq": 3},
        {"role": "assistant", "kind": "reasoning", "text": "第二步", "runtimeRunId": "rr-complete", "eventSeq": 5},
        {"role": "assistant", "content": "最终答案", "messageSeq": 0, "runtimeRunId": "rr-complete"},
    ]


async def test_backfill_snapshot_filters_messages_and_marks_session_complete() -> None:
    session = complete_session()
    session.chat_log_backfilled_at = None
    old_run = run(10, "rr-old", "旧问题")
    store = FakeChatLogStore(session=session, runs=[old_run])

    completed = await ChatLogProjector(store).backfill_snapshot(
        org_id=7,
        user_id=9,
        runtime_workspace_id=3,
        runtime_session_id="rs-3-a",
        messages=[
            {"role": "user", "content": "旧问题"},
            {"role": "tool", "content": "secret"},
            {"role": "assistant", "content": "旧答案", "reasoning": "secret"},
        ],
    )

    assert completed is True
    assert [(item.message_seq, item.role, item.content) for item in store.logs] == [
        (0, "user", "旧问题"),
        (1, "assistant", "旧答案"),
    ]
    assert all(item.channel == "unknown" for item in store.logs)
    assert session.chat_log_backfilled_at is not None
    assert old_run.chat_log_projected_at is not None


async def test_backfill_snapshot_releases_projection_locks_when_already_complete() -> None:
    store = FakeChatLogStore(session=complete_session())

    completed = await ChatLogProjector(store).backfill_snapshot(
        org_id=7,
        user_id=9,
        runtime_workspace_id=3,
        runtime_session_id="rs-3-a",
        messages=[],
    )

    assert completed is True
    assert store.commits == 0
    assert store.rollbacks == 1
    assert store.lock_calls == ["owned_projection"]
