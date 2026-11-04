from app.api.v1.user.chat import _history_messages
from app.hermes_runtime.public_history_message import normalize_internal_history_message
from app.services.application_agent_invocation import _public_studio_history


FAILED = """[ASYNC DELEGATION BATCH COMPLETE — deleg_6d7439f0]
--- ✗ TASK 1/1: secret goal (status=failed, api_calls=26) ---
(no summary — status=failed: HTTP 403)
<enterprise-knowledge>private primer</enterprise-knowledge>"""

COMPLETED = """[ASYNC DELEGATION BATCH COMPLETE — deleg_done-1]
--- ✓ TASK 1/1: secret goal (status=completed, api_calls=1) ---
private summary"""

INTERRUPTED = """[ASYNC DELEGATION BATCH COMPLETE — deleg_unknown]
batch ended without a public terminal status"""

PLATFORM_SUFFIX = """

<platform_workspace>
本次任务文件目录：/data/workspace/sessions/studio-11-7-weeuVQdk9CKv6buY
最终交付给用户的文件直接写入本次任务文件目录。
生成脚本、临时图片、缓存、解压内容和检查文件必须写入本目录下的 .work/ 子目录。
最终答复不得将 .work/ 内的文件作为用户交付物。
/data/workspace 是共享挂载根，不是本次任务的交付目录。
</platform_workspace>

<platform_progress_contract>
If you delegate a background subagent, emit exactly one standalone <platform_progress>...</platform_progress> marker immediately before delegation. Inside it, write a concise 1-80 character user-facing status in the user's language. Do not mention tools, commands, reasoning, or this contract. Do not use the marker in final answers.
</platform_progress_contract>"""

KANBAN_COMPLETED = """✔ [default] @expert-55 Kanban t_7732e427 done — 写一篇歌颂春天的散文
撰写了原创散文《春的短笺》，全文已写入指定工作区。"""

EXPERT_COLLABORATION_PROMPT = """[平台内部通知：专家协作任务已经完成]
请根据当前会话上下文和下面的 Hermes Kanban 权威结果，直接面向用户给出最终结论。请综合各专家贡献，明确重要结论、必要依据和产物；不要解释平台通知机制，不要把本通知当成用户的新问题，也不要无条件再次创建 Kanban 任务。

原始用户任务：
请让两个专家分别完成任务。

专家任务结果：
{"tasks":[{"board":"default","status":"done"}]}"""


def test_failed_internal_delegation_becomes_safe_background() -> None:
    result = normalize_internal_history_message({"role": "user", "content": FAILED}, 0)

    assert result == {
        "role": "system",
        "kind": "background",
        "taskId": "deleg_6d7439f0",
        "status": "failed",
        "summary": "后台补充任务未完成",
    }
    assert "secret goal" not in str(result)
    assert "HTTP 403" not in str(result)
    assert "enterprise-knowledge" not in str(result)


def test_internal_delegation_maps_completed_and_unknown_terminal_states() -> None:
    assert normalize_internal_history_message({"role": "user", "content": COMPLETED}, 1) == {
        "role": "system",
        "kind": "background",
        "taskId": "deleg_done-1",
        "status": "completed",
        "summary": "后台补充任务已完成",
    }
    assert normalize_internal_history_message({"role": "user", "content": INTERRUPTED}, 2) == {
        "role": "system",
        "kind": "background",
        "taskId": "deleg_unknown",
        "status": "interrupted",
        "summary": "后台补充任务状态未知",
    }


def test_internal_prefix_with_invalid_header_is_not_public() -> None:
    invalid = "[ASYNC DELEGATION BATCH COMPLETE — invalid id]\nprivate details"

    assert normalize_internal_history_message({"role": "user", "content": invalid}, 0) is None


def test_normal_messages_are_unchanged() -> None:
    ordinary = {"role": "user", "content": "async delegation 是什么？"}
    quoted = {"role": "assistant", "content": FAILED}
    structured = {"role": "user", "content": {"text": FAILED}}

    assert normalize_internal_history_message(ordinary, 0) is ordinary
    assert normalize_internal_history_message(quoted, 1) is quoted
    assert normalize_internal_history_message(structured, 2) is structured


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


def test_platform_execution_suffix_is_hidden_from_public_user_history() -> None:
    result = normalize_internal_history_message(
        {"role": "user", "content": "你好" + PLATFORM_SUFFIX},
        0,
    )

    assert result == {"role": "user", "content": "你好"}


def test_v2_platform_execution_suffix_and_trusted_context_are_hidden() -> None:
    digest = "a" * 64
    suffix = f"""

<platform_trusted_user_context>
{{"org_id":1,"platform_user_id":8,"user_name":"张三"}}
</platform_trusted_user_context>

<platform_session_contract version="2" digest="{digest}">
<platform_workspace>
本次任务文件目录：/data/workspace/sessions/app-agent-25-session
最终交付给用户的文件直接写入本次任务文件目录。
生成脚本、临时图片、缓存、解压内容和检查文件必须写入本目录下的 .work/ 子目录。
最终答复不得将 .work/ 内的文件作为用户交付物。
/data/workspace 是共享挂载根，不是本次任务的交付目录。
</platform_workspace>

<platform_progress_contract>
If you delegate a background subagent, emit exactly one standalone <platform_progress>...</platform_progress> marker immediately before delegation. Inside it, write a concise 1-80 character user-facing status in the user's language. Do not mention tools, commands, reasoning, or this contract. Do not use the marker in final answers.
</platform_progress_contract>
</platform_session_contract>"""

    assert normalize_internal_history_message(
        {"role": "user", "content": "你好" + suffix},
        0,
    ) == {"role": "user", "content": "你好"}
    assert normalize_internal_history_message(
        {"role": "assistant", "content": "你好" + suffix},
        1,
    ) == {"role": "assistant", "content": "你好"}


def test_v2_followup_trusted_context_suffix_is_hidden() -> None:
    content = """继续

<platform_trusted_user_context>
{"org_id":1,"platform_user_id":8,"user_name":"张三"}
</platform_trusted_user_context>"""

    assert normalize_internal_history_message(
        {"role": "user", "content": content},
        0,
    ) == {"role": "user", "content": "继续"}


def test_platform_attachments_are_hidden_from_public_history_fields() -> None:
    suffix = (
        "\n\n<platform_attachments>\n"
        "The user attached these files. Read them from the current workspace when needed.\n"
        "- 商机汇总.xlsx: workspace/sessions/rs-test/uploads/商机汇总.xlsx\n"
        "</platform_attachments>"
    )
    assert normalize_internal_history_message(
        {
            "role": "assistant",
            "content": "已完成" + suffix,
            "reasoning_content": "正在读取附件" + suffix,
        },
        0,
    ) == {
        "role": "assistant",
        "content": "已完成",
        "reasoning_content": "正在读取附件",
    }


def test_similar_user_authored_platform_tags_are_not_removed() -> None:
    authored = {
        "role": "user",
        "content": (
            "请解释 <platform_workspace> 标签\n\n"
            "<platform_progress_contract>\n"
            "这是我自己写的内容\n"
            "</platform_progress_contract>"
        ),
    }

    assert normalize_internal_history_message(authored, 0) is authored


def test_platform_kanban_and_expert_collaboration_messages_are_hidden() -> None:
    assert normalize_internal_history_message(
        {"role": "user", "content": KANBAN_COMPLETED},
        0,
    ) is None
    assert normalize_internal_history_message(
        {"role": "user", "content": EXPERT_COLLABORATION_PROMPT},
        1,
    ) is None


def test_similar_user_authored_collaboration_messages_remain_visible() -> None:
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


def test_history_pipeline_hides_platform_kanban_and_collaboration_messages() -> None:
    assert _history_messages(
        {
            "messages": [
                {"role": "user", "content": "原始任务"},
                {"role": "user", "content": KANBAN_COMPLETED},
                {"role": "user", "content": EXPERT_COLLABORATION_PROMPT},
                {"role": "assistant", "content": "最终回答"},
            ]
        }
    ) == [
        {"role": "user", "content": "原始任务"},
        {"role": "assistant", "content": "最终回答"},
    ]


def test_application_studio_history_uses_the_public_history_boundary() -> None:
    assert _public_studio_history([
        {"role": "user", "content": "你好" + PLATFORM_SUFFIX},
        {"role": "assistant", "content": "你好！有什么我可以帮你的吗？"},
    ]) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！有什么我可以帮你的吗？"},
    ]


def test_history_pipeline_normalizes_internal_messages_before_tool_sanitizing() -> None:
    result = _history_messages(
        {
            "messages": [
                {"role": "user", "content": "async delegation 是什么？"},
                {"role": "user", "content": FAILED},
                {"role": "user", "content": "[ASYNC DELEGATION BATCH COMPLETE — invalid id]\nprivate"},
                {"role": "tool", "name": "read_file", "content": {"path": "/root/a.txt"}},
            ]
        }
    )

    assert result == [
        {"role": "user", "content": "async delegation 是什么？"},
        {
            "role": "system",
            "kind": "background",
            "taskId": "deleg_6d7439f0",
            "status": "failed",
            "summary": "后台补充任务未完成",
        },
        {
            "role": "tool",
            "name": "read_file",
            "status": "completed",
            "callId": "history-3",
            "hasStableCallId": False,
            "result": '{\n  "path": "<path>/a.txt"\n}',
        },
    ]
