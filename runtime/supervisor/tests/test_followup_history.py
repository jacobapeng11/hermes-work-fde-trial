from __future__ import annotations

from platform_supervisor.hermes.followup_history import project_followup_history
from platform_supervisor.storage import StoredFollowupHistoryEntry


def _entry(
    *,
    sequence: int,
    content: str,
    state: str = "CANONICAL_CONSUMED",
    group_id: str | None = "group-1",
    group_position: int | None = None,
    wire_text: str | None = None,
    proof_message_id: int | None = None,
    proof_kind: str | None = None,
    hermes_status: str | None = "redirected",
    promoted_run_id: str | None = None,
    promoted_history_index: int | None = None,
) -> StoredFollowupHistoryEntry:
    return StoredFollowupHistoryEntry(
        redirect_id=f"redirect-{sequence}",
        client_message_id=f"correction-{sequence}",
        parent_run_id="run-parent",
        input_admission_seq=sequence,
        content=content,
        state=state,
        dispatch_group_id=group_id,
        dispatch_group_position=group_position,
        promoted_run_id=promoted_run_id,
        group_wire_text=wire_text,
        group_proof_message_id=proof_message_id,
        group_proof_kind=proof_kind,
        group_hermes_status=hermes_status,
        promoted_history_message_index=promoted_history_index,
    )


def test_redirect_replaces_aggregate_with_individual_ledger_messages() -> None:
    wire_text = "A\n\n[Additional user correction]\n相同\n\n[Additional user correction]\n相同"
    messages = [
        {"id": 1, "role": "user", "content": "原始问题"},
        {"id": 2, "role": "assistant", "content": "原回答前半段"},
        {"id": 3, "role": "user", "content": wire_text},
        {"id": 4, "role": "assistant", "content": "修正后的回答"},
    ]
    entries = [
        _entry(
            sequence=11,
            content="A",
            group_position=0,
            wire_text=wire_text,
            proof_message_id=3,
            proof_kind="correction_user_row",
        ),
        _entry(
            sequence=12,
            content="相同",
            group_position=1,
            wire_text=wire_text,
            proof_message_id=3,
            proof_kind="correction_user_row",
        ),
        _entry(
            sequence=13,
            content="相同",
            group_position=2,
            wire_text=wire_text,
            proof_message_id=3,
            proof_kind="correction_user_row",
        ),
    ]

    projected = project_followup_history(messages, entries)

    assert [message.get("content") for message in projected] == [
        "原始问题",
        "原回答前半段",
        "A",
        "相同",
        "相同",
        "修正后的回答",
    ]
    followups = [item for item in projected if item.get("client_message_id")]
    assert [item["client_message_id"] for item in followups] == [
        "correction-11",
        "correction-12",
        "correction-13",
    ]
    assert all(item["followup_status"] == "applied" for item in followups)


def test_steer_keeps_tool_result_then_inserts_followups_without_oob_marker() -> None:
    wire_text = "先查日志\n\n[Additional user correction]\n再看数据库"
    marker = (
        "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered "
        "once at this position; not tool output and not a new delivery when "
        "replayed from conversation history]\n"
        f"{wire_text}\n"
        "[/OUT-OF-BAND USER MESSAGE]"
    )
    messages = [
        {"id": 1, "role": "user", "content": "排查问题"},
        {"id": 2, "role": "assistant", "content": "我先调用工具"},
        {"id": 3, "role": "tool", "tool_name": "shell", "content": f"日志结果\n\n{marker}"},
        {"id": 4, "role": "assistant", "content": "综合结论"},
    ]
    entries = [
        _entry(
            sequence=21,
            content="先查日志",
            group_position=0,
            wire_text=wire_text,
            proof_message_id=3,
            hermes_status="queued",
        ),
        _entry(
            sequence=22,
            content="再看数据库",
            group_position=1,
            wire_text=wire_text,
            proof_message_id=3,
            hermes_status="queued",
        ),
    ]

    projected = project_followup_history(messages, entries)

    assert [item.get("role") for item in projected] == [
        "user",
        "assistant",
        "tool",
        "user",
        "user",
        "assistant",
    ]
    assert projected[2]["content"] == "日志结果"
    assert "OUT-OF-BAND" not in str(projected)
    assert [projected[3]["content"], projected[4]["content"]] == [
        "先查日志",
        "再看数据库",
    ]


def test_promoted_replaces_promoted_run_aggregate_at_history_checkpoint() -> None:
    wire_text = "A\n\n[Additional user correction]\nB"
    messages = [
        {"id": 1, "role": "user", "content": "原始问题"},
        {"id": 2, "role": "assistant", "content": "原 Run 回答"},
        {"id": 3, "role": "user", "content": wire_text},
        {"id": 4, "role": "assistant", "content": "继续任务回答"},
    ]
    entries = [
        _entry(
            sequence=31,
            content="A",
            state="PROMOTED_TO_RUN",
            group_position=0,
            wire_text=wire_text,
            hermes_status="rejected",
            promoted_run_id="run-promoted",
            promoted_history_index=2,
        ),
        _entry(
            sequence=32,
            content="B",
            state="PROMOTED_TO_RUN",
            group_position=1,
            wire_text=wire_text,
            hermes_status="rejected",
            promoted_run_id="run-promoted",
            promoted_history_index=2,
        ),
    ]

    projected = project_followup_history(messages, entries)

    assert [item.get("content") for item in projected] == [
        "原始问题",
        "原 Run 回答",
        "A",
        "B",
        "继续任务回答",
    ]
    assert projected[2]["continuation_run_id"] == "run-promoted"
    assert projected[3]["continuation_run_id"] == "run-promoted"


def test_promoted_physical_groups_share_one_continuation_history_anchor() -> None:
    contents = [f"追问-{index:02d}" for index in range(35)]
    aggregate = "\n\n[Additional user correction]\n".join(contents)
    first_wire = "\n\n[Additional user correction]\n".join(contents[:32])
    second_wire = "\n\n[Additional user correction]\n".join(contents[32:])
    messages = [
        {"id": 1, "role": "user", "content": "原始问题"},
        {"id": 2, "role": "assistant", "content": "原 Run 回答"},
        {"id": 3, "role": "user", "content": aggregate},
        {"id": 4, "role": "assistant", "content": "唯一延续回答"},
    ]
    entries = [
        _entry(
            sequence=100 + index,
            content=content,
            state="PROMOTED_TO_RUN",
            group_id="group-1" if index < 32 else "group-2",
            group_position=index if index < 32 else index - 32,
            wire_text=first_wire if index < 32 else second_wire,
            hermes_status=None,
            promoted_run_id="run-one-continuation",
            promoted_history_index=2,
        )
        for index, content in enumerate(contents)
    ]

    projected = project_followup_history(messages, entries)

    followups = [item for item in projected if item.get("followup_id")]
    assert [item["content"] for item in followups] == contents
    assert {
        item["continuation_run_id"] for item in followups
    } == {"run-one-continuation"}
    assert aggregate not in [item.get("content") for item in projected]
    assert projected[-1]["content"] == "唯一延续回答"


def test_unanchored_states_remain_visible_once_in_admission_order() -> None:
    messages = [
        {"id": 1, "role": "user", "content": "原始问题"},
        {"id": 2, "role": "assistant", "content": "原回答"},
    ]
    entries = [
        _entry(
            sequence=43,
            content="状态未知",
            state="SUBMISSION_UNCERTAIN",
            group_id=None,
            hermes_status=None,
        ),
        _entry(
            sequence=41,
            content="仍在等待",
            state="WAITING_AGENT_READY",
            group_id=None,
            hermes_status=None,
        ),
        _entry(
            sequence=42,
            content="已取消",
            state="CANCELED_WITH_PARENT",
            group_id=None,
            hermes_status=None,
        ),
    ]

    projected = project_followup_history(messages, entries)
    replayed = project_followup_history(projected, entries)

    assert [item.get("content") for item in projected[-3:]] == [
        "仍在等待",
        "已取消",
        "状态未知",
    ]
    assert [item.get("client_message_id") for item in replayed].count(
        "correction-41"
    ) == 1
    assert replayed == projected


def test_legacy_history_without_followups_is_unchanged() -> None:
    messages = [
        {"id": 1, "role": "user", "content": "普通问题"},
        {"id": 2, "role": "assistant", "content": "普通回答"},
    ]

    assert project_followup_history(messages, []) == messages
