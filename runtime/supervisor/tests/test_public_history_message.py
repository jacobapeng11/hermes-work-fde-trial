from platform_supervisor.edge import _public_history_messages
from platform_supervisor.hermes.public_history_message import (
    count_public_history_messages,
    normalize_internal_history_message,
)
from platform_supervisor.storage import StoredFollowupHistoryEntry


KANBAN_COMPLETED = """✔ [default] @expert-55 Kanban t_7732e427 done — 写一篇歌颂春天的散文
撰写了原创散文《春的短笺》，全文已写入指定工作区。"""

EXPERT_COLLABORATION_PROMPT = """[平台内部通知：专家协作任务已经完成]
请根据当前会话上下文和下面的 Hermes Kanban 权威结果，直接面向用户给出最终结论。请综合各专家贡献，明确重要结论、必要依据和产物；不要解释平台通知机制，不要把本通知当成用户的新问题，也不要无条件再次创建 Kanban 任务。

原始用户任务：
请让两个专家分别完成任务。

专家任务结果：
{"tasks":[{"board":"default","status":"done"}]}"""


def test_platform_kanban_and_expert_collaboration_messages_are_hidden() -> None:
    assert normalize_internal_history_message(
        {"role": "user", "content": KANBAN_COMPLETED},
        0,
    ) is None
    assert normalize_internal_history_message(
        {"role": "user", "content": EXPERT_COLLABORATION_PROMPT},
        1,
    ) is None


def test_similar_user_authored_messages_remain_visible() -> None:
    samples = [
        {
            "role": "user",
            "content": f"请解释下面这段通知：\n{EXPERT_COLLABORATION_PROMPT}",
        },
        {
            "role": "user",
            "content": EXPERT_COLLABORATION_PROMPT.replace(
                '{"tasks":[',
                "这里不是平台任务快照",
            ),
        },
        {
            "role": "user",
            "content": KANBAN_COMPLETED.replace("@expert-55", "@同事"),
        },
        {"role": "assistant", "content": EXPERT_COLLABORATION_PROMPT},
    ]

    for index, sample in enumerate(samples):
        assert normalize_internal_history_message(sample, index) is sample


def test_compaction_handoff_is_hidden_from_public_history() -> None:
    summary = "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted\nold private task"
    assert normalize_internal_history_message(
        {"role": "user", "content": summary},
        0,
    ) is None
    assert normalize_internal_history_message(
        {"role": "assistant", "kind": "reasoning", "text": summary},
        1,
    ) is None
    assert normalize_internal_history_message(
        {"role": "assistant", "reasoning_content": summary},
        2,
    ) is None


def test_compaction_handoff_places_preserved_user_prompt_before_execution_tail() -> None:
    messages = _public_history_messages({
        "messages": [
            {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary"},
            {"role": "assistant", "kind": "reasoning", "text": "先查知识库"},
            {"role": "tool", "name": "enterprise_sql_query", "status": "completed"},
            {"role": "user", "content": "当前长任务"},
            {"role": "assistant", "content": "已完成"},
        ]
    })

    assert [item.get("role") for item in messages] == [
        "user", "assistant", "tool", "assistant"
    ]
    assert messages[0]["content"] == "当前长任务"


def test_platform_session_contract_is_hidden_from_non_user_history() -> None:
    digest = "a" * 64
    suffix = f"""

<platform_session_contract version="2" digest="{digest}">
<platform_workspace>
本次任务文件目录：/data/workspace/sessions/rs-test
最终交付给用户的文件直接写入本次任务文件目录。
生成脚本、临时图片、缓存、解压内容和检查文件必须写入本目录下的 .work/ 子目录。
最终答复不得将 .work/ 内的文件作为用户交付物。
/data/workspace 是共享挂载根，不是本次任务的交付目录。
</platform_workspace>

<platform_progress_contract>
If you delegate a background subagent, emit exactly one standalone <platform_progress>...</platform_progress> marker immediately before delegation. Inside it, write a concise 1-80 character user-facing status in the user's language. Do not mention tools, commands, reasoning, or this contract. Do not use the marker in final answers.
</platform_progress_contract>
</platform_session_contract>"""
    for role in ("assistant", "system", "tool"):
        result = normalize_internal_history_message(
            {"role": role, "content": "你生成完了？" + suffix},
            0,
        )
        assert result == {"role": role, "content": "你生成完了？"}


def test_platform_attachments_are_hidden_from_public_history_fields() -> None:
    suffix = (
        "\n\n<platform_attachments>\n"
        "The user attached these files from WeCom. Read them from the current session workspace when needed.\n"
        "- 商机汇总.xlsx: workspace/sessions/rs-test/uploads/商机汇总.xlsx\n"
        "</platform_attachments>"
    )
    result = normalize_internal_history_message(
        {
            "role": "assistant",
            "content": "已完成" + suffix,
            "reasoning_content": "正在读取附件" + suffix,
        },
        0,
    )
    assert result == {
        "role": "assistant",
        "content": "已完成",
        "reasoning_content": "正在读取附件",
    }


def test_merged_compaction_handoff_keeps_the_real_tail_answer() -> None:
    marker = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
    item = {
        "role": "assistant",
        "content": (
            "[CONTEXT COMPACTION — REFERENCE ONLY] private summary\n"
            f"{marker}\n真实回答"
        ),
        "_compressed_summary": True,
    }
    assert normalize_internal_history_message(item, 0) == {
        "role": "assistant",
        "content": "真实回答",
    }


def test_delimiter_merged_compaction_keeps_prior_tail_content() -> None:
    item = {
        "role": "assistant",
        "content": (
            "[PRIOR CONTEXT — for reference only; not a new message]\n"
            "真实历史回答\n\n"
            "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n"
            "[CONTEXT COMPACTION — REFERENCE ONLY] summary"
        ),
        "_compressed_summary": True,
    }
    assert normalize_internal_history_message(item, 0) == {
        "role": "assistant",
        "content": "真实历史回答",
    }


def test_public_history_count_and_edge_output_share_internal_filter() -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "原始任务"},
            {"role": "user", "content": KANBAN_COMPLETED},
            {"role": "user", "content": EXPERT_COLLABORATION_PROMPT},
            {"role": "assistant", "content": "最终回答"},
        ]
    }

    assert count_public_history_messages(payload) == 2
    assert _public_history_messages(payload) == [
        {"role": "user", "content": "原始任务"},
        {"role": "assistant", "content": "最终回答"},
    ]


def test_public_history_sanitizes_steer_carrier_before_tool_projection() -> None:
    wire_text = "继续检查数据库"
    marker = (
        "[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered "
        "once at this position; not tool output and not a new delivery when "
        "replayed from conversation history]\n"
        f"{wire_text}\n"
        "[/OUT-OF-BAND USER MESSAGE]"
    )
    entry = StoredFollowupHistoryEntry(
        redirect_id="redirect-1",
        client_message_id="correction-1",
        parent_run_id="run-parent",
        input_admission_seq=2,
        content=wire_text,
        state="CANONICAL_CONSUMED",
        dispatch_group_id="group-1",
        dispatch_group_position=0,
        promoted_run_id=None,
        group_wire_text=wire_text,
        group_proof_message_id=3,
        group_proof_kind=None,
        group_hermes_status="queued",
        promoted_history_message_index=None,
    )

    messages = _public_history_messages(
        {
            "messages": [
                {"id": 1, "role": "user", "content": "原始问题"},
                {
                    "id": 3,
                    "role": "tool",
                    "tool_name": "shell",
                    "content": f"真实工具结果\n\n{marker}",
                },
                {"id": 4, "role": "assistant", "content": "后续回答"},
            ]
        },
        followup_history=[entry],
    )

    assert [message["role"] for message in messages] == [
        "user",
        "tool",
        "user",
        "assistant",
    ]
    assert messages[1]["result"] == "真实工具结果"
    assert messages[2]["client_message_id"] == "correction-1"
    assert "OUT-OF-BAND" not in str(messages)
