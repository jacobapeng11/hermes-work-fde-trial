from __future__ import annotations

from app.im_gateway.wecom_projection import (
    ACTIVITY_STATE_PREFIX,
    WeComProjection,
    sanitize_visible_text,
)


def _apply_all(
    projection: WeComProjection,
    events: list[tuple[str, dict]],
) -> WeComProjection:
    for name, data in events:
        projection.apply(name, data)
    return projection


def test_reasoning_uses_one_native_fold_across_tool_boundaries() -> None:
    projection = _apply_all(
        WeComProjection(),
        [
            ("chat.reasoning.delta", {"delta": "思考一"}),
            (
                "chat.tool.event",
                {
                    "name": "search",
                    "status": "completed",
                    "callId": "search-1",
                    "hasStableCallId": True,
                },
            ),
            ("chat.reasoning.delta", {"delta": "思考二"}),
            ("chat.message.completed", {"text": "最终结论"}),
            ("chat.done", {}),
        ],
    )

    rendered = projection.render_passive()
    assert projection.reasoning_text == "思考一思考二"
    assert rendered.count("<think>") == 1
    assert rendered.count("</think>") == 1
    assert "<think>\n思考一思考二\n\n**执行过程**\n✓ 资料搜索完成\n</think>" in rendered
    assert "✓ 资料搜索完成" in rendered
    assert rendered.endswith("最终结论")


def test_tool_event_reclassifies_streamed_preamble_as_activity() -> None:
    projection = WeComProjection()

    assert projection.apply(
        "chat.message.delta",
        {"delta": "我先并行加载相关技能和了解企业知识工具。"},
    )
    assert projection.apply(
        "chat.tool.event",
        {"name": "knowledge_search", "status": "running", "callId": "call-1"},
    )

    assert projection.message_text == ""
    assert projection.activity_lines == ["我先并行加载相关技能和了解企业知识工具。"]
    assert projection.tools[0].name == "knowledge_search"


def test_agent_activity_preamble_is_not_reclassified_as_final_text() -> None:
    projection = _apply_all(
        WeComProjection(),
        [
            ("chat.activity.delta", {"delta": "好的，我来先准备工具。", "source": "agent"}),
            ("chat.tool.event", {"name": "search", "status": "running", "callId": "call-1"}),
            ("chat.message.completed", {"text": "最终答案"}),
        ],
    )

    assert "好的，我来先准备工具。" not in projection.render_passive()
    assert projection.render_passive().endswith("最终答案")


def test_completed_preamble_is_reclassified_when_tools_continue() -> None:
    projection = _apply_all(
        WeComProjection(),
        [
            ("chat.message.delta", {"delta": "我先检查知识库。"}),
            ("chat.message.completed", {"text": "我先检查知识库。"}),
            ("chat.tool.event", {"name": "knowledge_search", "status": "running", "callId": "call-1"}),
        ],
    )

    assert projection.message_text == ""
    assert projection.final_received is False
    assert projection.activity_lines == ["我先检查知识库。"]


def test_tool_updates_in_place_and_never_renders_raw_payload() -> None:
    projection = WeComProjection()
    projection.apply(
        "chat.tool.event",
        {
            "name": "write_file",
            "status": "running",
            "callId": "write-1",
            "hasStableCallId": True,
            "input": '{"path":"<path>/祖国颂.md"}',
        },
    )
    assert projection.render_passive() == (
        "<think>\n**执行过程**\n正在写入《祖国颂.md》…\n</think>"
    )

    projection.apply(
        "chat.tool.event",
        {
            "name": "write_file",
            "status": "completed",
            "callId": "write-1",
            "hasStableCallId": True,
            "result": (
                '{"bytes_written":7059,"dirs_created":true,'
                '"resolved_path":"<path>/祖国颂.md"}'
            ),
        },
    )
    rendered = projection.render_passive()
    assert rendered == "<think>\n**执行过程**\n✓ 已写入《祖国颂.md》\n</think>"
    assert "正在写入" not in rendered
    assert "bytes_written" not in rendered
    assert "dirs_created" not in rendered
    assert "resolved_path" not in rendered
    assert "completed" not in rendered


def test_enterprise_tools_render_specific_actions_and_safe_inputs() -> None:
    projection = _apply_all(
        WeComProjection(),
        [
            (
                "chat.tool.event",
                {
                    "name": "enterprise_knowledge_read",
                    "status": "completed",
                    "callId": "knowledge-1",
                    "hasStableCallId": True,
                    "input": '{"relative_path":"PRIMER.md"}',
                },
            ),
            (
                "chat.tool.event",
                {
                    "name": "enterprise_sql_context",
                    "status": "completed",
                    "callId": "context-1",
                    "hasStableCallId": True,
                    "input": '{"query":"上海区上周商机口径"}',
                },
            ),
            (
                "chat.tool.event",
                {
                    "name": "enterprise_sql_query",
                    "status": "completed",
                    "callId": "query-1",
                    "hasStableCallId": True,
                    "input": '{"sql":"SELECT COUNT(*) FROM crm_project_info_t"}',
                },
            ),
        ],
    )

    rendered = projection.render_passive()
    assert "✓ 已读取企业知识 · 读取“PRIMER.md”" in rendered
    assert "✓ 已获取数据口径 · 获取“上海区上周商机口径”" in rendered
    assert "✓ 已查询企业数据 · 查询“SELECT COUNT(*) FROM crm_project_info_t”" in rendered
    assert "操作完成" not in rendered


def test_dynamic_search_tools_render_query_without_exposing_raw_payload() -> None:
    projection = WeComProjection()
    projection.apply(
        "chat.tool.event",
        {
            "name": "mcp__asset_615__search_web",
            "status": "completed",
            "callId": "search-1",
            "hasStableCallId": True,
            "input": '{"arguments":{"query":"深圳今天天气","api_key":"secret"}}',
        },
    )

    rendered = projection.render_passive()
    assert "✓ 资料搜索完成 · 查询“深圳今天天气”" in rendered
    assert "secret" not in rendered


def test_second_write_to_same_file_is_presented_as_update() -> None:
    projection = WeComProjection()
    for call_id in ("write-1", "write-2"):
        projection.apply(
            "chat.tool.event",
            {
                "name": "write_file",
                "status": "running",
                "callId": call_id,
                "hasStableCallId": True,
                "input": '{"path":"<path>/祖国颂.md"}',
            },
        )
        projection.apply(
            "chat.tool.event",
            {
                "name": "write_file",
                "status": "completed",
                "callId": call_id,
                "hasStableCallId": True,
                "result": '{"resolved_path":"<path>/祖国颂.md"}',
            },
        )

    assert projection.render_passive() == (
        "<think>\n**执行过程**\n"
        "✓ 已写入《祖国颂.md》\n"
        "✓ 已更新《祖国颂.md》\n</think>"
    )


def test_real_writing_run_projects_to_one_clean_final_snapshot() -> None:
    reasoning_parts = [
        (
            "用户希望创作一篇约 2000 字、主题为歌颂祖国的中文散文。"
            "我会先完成初稿。"
        ),
        "初稿篇幅不足，需要继续扩充。",
        "扩充后再检查一次篇幅。",
        "篇幅已经符合要求，可以给出最终答复。",
    ]
    final_answer = (
        "已完成！散文《祖国颂》约 2300 字，保存在：\n"
        "📄 /data/workspace/sessions/rs-115-9f60ad3ade3b/祖国颂.md\n\n"
        "文章从山河、历史、时代与普通人的生活展开。"
    )
    events: list[tuple[str, dict]] = [
        ("chat.reasoning.delta", {"delta": reasoning_parts[0]}),
        (
            "chat.activity.delta",
            {
                "delta": "好的，我来创作一篇歌颂祖国的散文。",
                "source": "agent",
            },
        ),
        (
            "chat.tool.event",
            {
                "name": "write_file",
                "status": "running",
                "callId": "write-1",
                "hasStableCallId": True,
                "input": '{"path":"<path>/祖国颂.md"}',
            },
        ),
        (
            "chat.tool.event",
            {
                "name": "write_file",
                "status": "completed",
                "callId": "write-1",
                "hasStableCallId": True,
                "result": (
                    '{"bytes_written":3655,"dirs_created":true,'
                    '"lint":{"status":"skipped"},'
                    '"resolved_path":"<path>/祖国颂.md",'
                    '"files_modified":["<path>/祖国颂.md"]}'
                ),
            },
        ),
        ("chat.reasoning.delta", {"delta": reasoning_parts[1]}),
        (
            "chat.activity.delta",
            {
                "delta": "字数不够，我来扩充到 2000 字。",
                "source": "agent",
            },
        ),
        (
            "chat.tool.event",
            {
                "name": "terminal",
                "status": "running",
                "callId": "terminal-1",
                "hasStableCallId": True,
                "input": "wc -m <path>/祖国颂.md",
            },
        ),
        (
            "chat.tool.event",
            {
                "name": "terminal",
                "status": "completed",
                "callId": "terminal-1",
                "hasStableCallId": True,
                "result": (
                    '{"output":"1235 <path>/祖国颂.md",'
                    '"exit_code":0,"error":null}'
                ),
            },
        ),
        (
            "chat.tool.event",
            {
                "name": "write_file",
                "status": "running",
                "callId": "write-2",
                "hasStableCallId": True,
                "input": '{"path":"<path>/祖国颂.md"}',
            },
        ),
        (
            "chat.tool.event",
            {
                "name": "write_file",
                "status": "completed",
                "callId": "write-2",
                "hasStableCallId": True,
                "result": (
                    '{"bytes_written":7059,'
                    '"resolved_path":"<path>/祖国颂.md"}'
                ),
            },
        ),
        ("chat.reasoning.delta", {"delta": reasoning_parts[2]}),
        (
            "chat.tool.event",
            {
                "name": "terminal",
                "status": "running",
                "callId": "terminal-2",
                "hasStableCallId": True,
            },
        ),
        (
            "chat.tool.event",
            {
                "name": "terminal",
                "status": "completed",
                "callId": "terminal-2",
                "hasStableCallId": True,
                "result": (
                    '{"output":"2375 <path>/祖国颂.md",'
                    '"exit_code":0,"error":null}'
                ),
            },
        ),
        ("chat.reasoning.delta", {"delta": reasoning_parts[3]}),
        ("chat.message.completed", {"text": final_answer}),
        ("chat.artifact.created", {"file_id": 99}),
        ("chat.done", {"status": "completed"}),
    ]

    projection = _apply_all(WeComProjection(), events)
    rendered = projection.render_passive()

    assert projection.reasoning_text == "".join(reasoning_parts)
    assert rendered.count("<think>") == 1
    assert rendered.count("</think>") == 1
    assert rendered.count("已完成！散文《祖国颂》约 2300 字") == 1
    assert "好的，我来创作" not in rendered
    assert "字数不够，我来扩充" not in rendered
    assert "✓ 已写入《祖国颂.md》" in rendered
    assert "✓ 已更新《祖国颂.md》" in rendered
    assert rendered.count("✓ 检查完成") == 2
    assert "📄 《祖国颂.md》" in rendered
    for leaked in (
        "bytes_written",
        "dirs_created",
        "resolved_path",
        "exit_code",
        "<path>",
        "/data/workspace",
        "工具 ·",
        "---",
    ):
        assert leaked not in rendered


def test_completed_body_replaces_every_temporary_agent_delta() -> None:
    projection = _apply_all(
        WeComProjection(),
        [
            (
                "chat.activity.delta",
                {"source": "agent", "delta": "临时草稿一"},
            ),
            (
                "chat.activity.delta",
                {"source": "agent", "delta": "临时草稿二"},
            ),
            ("chat.message.completed", {"text": "权威最终回答"}),
            (
                "chat.activity.delta",
                {"source": "agent", "delta": "迟到的旧增量"},
            ),
        ],
    )

    assert projection.message_text == "权威最终回答"
    assert projection.render_passive() == "权威最终回答"


def test_recovered_interim_activity_keeps_reasoning_fold_open() -> None:
    projection = _apply_all(
        WeComProjection(),
        [
            ("chat.reasoning.delta", {"delta": "仍在分析。"}),
            ("chat.message.delta", {"delta": "我先"}),
            (
                "chat.activity.delta",
                {
                    "source": "recovered",
                    "interim": True,
                    "delta": "检查文件结构。",
                },
            ),
        ],
    )

    assert projection.reasoning_closed is False
    assert "我先\n检查文件结构。" in projection.render_passive()
    assert projection.message_text == ""
    assert "</think>" not in projection.render_passive()


def test_no_reasoning_does_not_create_a_fake_native_fold() -> None:
    projection = _apply_all(
        WeComProjection(),
        [
            ("chat.message.completed", {"text": "直接回答"}),
            ("chat.done", {}),
        ],
    )

    assert projection.render_passive() == "直接回答"
    assert "<think>" not in projection.render_passive()


def test_unknown_tools_are_bounded_while_all_failures_stay_visible() -> None:
    projection = WeComProjection()
    for index in range(10):
        projection.apply(
            "chat.tool.event",
            {
                "name": f"custom_tool_{index}",
                "status": "completed",
                "callId": f"tool-{index}",
                "hasStableCallId": True,
            },
        )
    projection.apply(
        "chat.tool.event",
        {
            "name": "custom_tool_failed",
            "status": "failed",
            "callId": "tool-failed",
            "hasStableCallId": True,
            "result": '{"error":"private detail"}',
        },
    )

    rendered = projection.render_passive()
    assert rendered.count("✓ 已完成工具调用") == 8
    assert "操作完成" not in rendered
    assert "另有 2 项操作已完成" in rendered
    assert "⚠ 工具调用失败 · custom_tool_failed，请重试" in rendered
    assert "private detail" not in rendered


def test_unknown_tool_keeps_a_safe_operation_name_instead_of_generic_status() -> None:
    projection = WeComProjection()
    projection.apply(
        "chat.tool.event",
        {
            "name": "mcp__asset_615__crm_create_follow_up",
            "status": "completed",
            "callId": "custom-1",
            "hasStableCallId": True,
        },
    )

    rendered = projection.render_passive()
    assert "✓ 已完成MCP 工具 · crm_create_follow_up" in rendered
    assert "操作完成" not in rendered


def test_unstable_tool_ids_pair_running_and_completion_fifo() -> None:
    projection = WeComProjection()
    for _ in range(2):
        projection.apply(
            "chat.tool.event",
            {
                "name": "terminal",
                "status": "running",
                "callId": "terminal:unstable",
                "hasStableCallId": False,
            },
        )
    projection.apply(
        "chat.tool.event",
        {
            "name": "terminal",
            "status": "completed",
            "callId": "terminal:unstable",
            "hasStableCallId": False,
        },
    )

    assert [tool.status for tool in projection.tools] == [
        "completed",
        "running",
    ]


def test_projection_state_round_trips_at_every_event_boundary() -> None:
    events: list[tuple[str, dict]] = [
        ("chat.reasoning.delta", {"delta": "精确推理 A"}),
        (
            "chat.tool.event",
            {
                "name": "write_file",
                "status": "running",
                "callId": "write-1",
                "hasStableCallId": True,
                "input": '{"path":"<path>/报告.md"}',
            },
        ),
        (
            "chat.tool.event",
            {
                "name": "write_file",
                "status": "completed",
                "callId": "write-1",
                "hasStableCallId": True,
                "result": '{"resolved_path":"<path>/报告.md"}',
            },
        ),
        ("chat.reasoning.delta", {"delta": "精确推理 B"}),
        (
            "chat.activity.delta",
            {"source": "agent", "delta": "临时答复"},
        ),
        ("chat.message.completed", {"text": "最终答复"}),
        ("chat.done", {}),
    ]
    expected = _apply_all(WeComProjection(), events)

    for boundary in range(len(events) + 1):
        partial = _apply_all(WeComProjection(), events[:boundary])
        serialized = partial.serialize_activity()
        assert serialized.startswith(ACTIVITY_STATE_PREFIX)
        restored = WeComProjection.restore(
            reasoning_text=partial.reasoning_text,
            message_text=partial.message_text,
            activity_text=serialized,
        )
        assert restored is not None
        _apply_all(restored, events[boundary:])
        assert restored.reasoning_text == expected.reasoning_text
        assert restored.message_text == expected.message_text
        assert restored.render_passive() == expected.render_passive()
        assert (
            restored.serialize_activity()
            == expected.serialize_activity()
        )


def test_visible_body_replaces_internal_paths_with_filenames() -> None:
    value = (
        "文件位于 `/workspace/private/a.pptx`，另见 "
        "/data/workspace/sessions/rs-secret/祖国颂.md。"
    )

    assert sanitize_visible_text(value) == (
        "文件位于 《a.pptx》，另见 《祖国颂.md》。"
    )
