import { describe, expect, it } from "vitest";

import type { ActiveTurn, ChatMessage, SessionCache } from "./chatRunProjection";
import { projectChatRunEvent } from "./chatRunProjection";
import { historyMessages, reconcileSessionHistory } from "./chatHistory";

describe("historyMessages", () => {
  it("restores redirect ledger identity and keeps repeated correction text distinct", () => {
    const messages = historyMessages([
      {
        role: "user",
        content: "继续检查",
        client_message_id: "correction-1",
        followup_id: "redirect-1",
        parent_run_id: "run-parent",
        followup_status: "applied"
      },
      {
        role: "user",
        content: "继续检查",
        client_message_id: "correction-2",
        followup_id: "redirect-2",
        parent_run_id: "run-parent"
      }
    ]);

    expect(messages).toEqual([
      expect.objectContaining({
        id: "correction-correction-1",
        clientMessageId: "correction-1",
        redirectId: "redirect-1",
        correctionStatus: "applied",
        submissionHint: "已应用"
      }),
      expect.objectContaining({
        id: "correction-correction-2",
        clientMessageId: "correction-2",
        redirectId: "redirect-2"
      })
    ]);
  });

  it("restores explicit stable tool metadata and isolates old unstable history", () => {
    expect(historyMessages([{
      role: "tool",
      name: "read_file",
      status: "completed",
      callId: "call-1",
      hasStableCallId: true,
      input: '{"path":"<path>/report.txt"}',
      result: "done"
    }])[0]).toMatchObject({
      kind: "tool",
      callId: "call-1",
      providerCallId: "call-1",
      hasStableCallId: true,
      input: '{"path":"<path>/report.txt"}'
    });
    expect(historyMessages([{
      role: "tool",
      name: "read_file",
      callId: "runtime-fallback",
      result: "done"
    }])[0]).toMatchObject({
      kind: "tool",
      callId: "history-0",
      providerCallId: "runtime-fallback",
      hasStableCallId: false
    });
    expect(historyMessages([{
      role: "tool",
      name: "search_files",
      status: "running",
      result: "querying"
    }])[0]).toMatchObject({ status: "running" });
    expect(historyMessages([{
      role: "tool",
      name: "search_files",
      status: "error",
      result: "permission denied"
    }])[0]).toMatchObject({ status: "failed" });
    expect(historyMessages([{
      role: "tool",
      name: "search_files",
      error_message: "permission denied"
    }])[0]).toMatchObject({ status: "failed" });
    expect(historyMessages([{
      role: "tool",
      name: "search_files",
      result: { error: "permission denied" }
    }])[0]).toMatchObject({ status: "failed" });
  });

  it("restores safe background history and ignores malformed background records", () => {
    expect(historyMessages([{
      role: "system",
      kind: "background",
      taskId: "deleg_1",
      status: "failed",
      summary: "后台补充任务未完成"
    }])).toEqual([
      expect.objectContaining({
        kind: "background",
        role: "system",
        taskId: "deleg_1",
        status: "failed",
        summary: "后台补充任务未完成"
      })
    ]);

    expect(historyMessages([{
      role: "system",
      kind: "background",
      taskId: "",
      status: "failed",
      summary: "unsafe"
    }])).toEqual([]);

    expect(historyMessages([{
      role: "system",
      kind: "background",
      taskId: "task-41",
      status: "running",
      activity: "terminal"
    }])).toEqual([]);
  });

  it("splits persisted reasoning from the visible assistant answer", () => {
    expect(historyMessages([{
      role: "assistant",
      reasoning_content: "private trace",
      content: "visible answer"
    }])).toEqual([
      expect.objectContaining({ kind: "reasoning", text: "private trace" }),
      expect.objectContaining({ role: "assistant", content: "visible answer" })
    ]);
  });

  it("does not leak a duplicated reasoning prefix into the assistant answer", () => {
    expect(historyMessages([{
      role: "assistant",
      reasoning_content: "先查区域口径，再查询业绩。",
      content: "先查区域口径，再查询业绩。\n\n上海区本周完成 80%。"
    }])).toEqual([
      expect.objectContaining({ kind: "reasoning", text: "先查区域口径，再查询业绩。" }),
      expect.objectContaining({ role: "assistant", content: "上海区本周完成 80%。" })
    ]);
  });

  it("restores standalone reasoning rows and camelized history fields", () => {
    expect(historyMessages([
      { kind: "reasoning", role: "assistant", text: "first" },
      { role: "assistant", reasoningContent: "second", content: "done" }
    ])).toEqual([
      expect.objectContaining({ kind: "reasoning", text: "first" }),
      expect.objectContaining({ kind: "reasoning", text: "second" }),
      expect.objectContaining({ content: "done" })
    ]);
  });

  it("does not create an empty assistant answer for reasoning-only history", () => {
    const messages = historyMessages([{ role: "assistant", reasoning: "thinking", content: "" }]);

    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({ kind: "reasoning", text: "thinking" });
  });

  it("hides a platform prompt echoed into historical reasoning", () => {
    const prompt = "请处理附件中的表格";
    const attachmentSuffix = `\n\n<platform_attachments>\nThe user attached these files. Read them from the current workspace when needed.\n- result.xlsx: workspace/sessions/rs-1/uploads/result.xlsx\n</platform_attachments>`;
    expect(historyMessages([
      { role: "user", content: prompt },
      {
        role: "assistant",
        reasoning_content: prompt + attachmentSuffix,
        content: "已完成"
      }
    ])).toEqual([
      expect.objectContaining({ role: "user", content: prompt }),
      expect.objectContaining({ role: "assistant", content: "已完成" })
    ]);
  });

  it("keeps genuine reasoning even when platform metadata is present", () => {
    const prompt = "请处理附件中的表格";
    const attachmentSuffix = `\n\n<platform_attachments>\nThe user attached these files. Read them from the current workspace when needed.\n- result.xlsx: workspace/sessions/rs-1/uploads/result.xlsx\n</platform_attachments>`;
    expect(historyMessages([
      { role: "user", content: prompt },
      {
        role: "assistant",
        reasoning_content: "先检查表头和数据范围" + attachmentSuffix,
        content: "已完成"
      }
    ])).toEqual([
      expect.objectContaining({ role: "user", content: prompt }),
      expect.objectContaining({ kind: "reasoning", text: "先检查表头和数据范围" }),
      expect.objectContaining({ role: "assistant", content: "已完成" })
    ]);
  });

  it("hides persisted context-compaction handoffs from the public timeline", () => {
    expect(historyMessages([
      {
        role: "user",
        content: "[CONTEXT COMPACTION — REFERENCE ONLY] private summary"
      },
      {
        role: "assistant",
        kind: "reasoning",
        text: "[CONTEXT COMPACTION - REFERENCE ONLY] private summary"
      },
      {
        role: "assistant",
        reasoning_content: "[CONTEXT SUMMARY]: private summary"
      }
    ])).toEqual([]);
  });

  it("keeps the real answer when a compaction handoff was merged into it", () => {
    const marker = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---";
    expect(historyMessages([{
      role: "assistant",
      content: `[CONTEXT COMPACTION — REFERENCE ONLY] private summary\n${marker}\n真实回答`,
      _compressed_summary: true
    }])).toEqual([
      expect.objectContaining({ role: "assistant", content: "真实回答" })
    ]);
  });

  it("keeps prior tail content when the summary uses the delimiter form", () => {
    expect(historyMessages([{
      role: "assistant",
      content: "[PRIOR CONTEXT — for reference only; not a new message]\n真实历史回答\n\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]\n[CONTEXT COMPACTION — REFERENCE ONLY] summary",
      _compressed_summary: true
    }])).toEqual([
      expect.objectContaining({ role: "assistant", content: "真实历史回答" })
    ]);
  });

  it("keeps assistant tool-call intermediates in the execution timeline", () => {
    expect(historyMessages([
      {
        role: "assistant",
        content: "先查上海区目标，再汇总业绩。",
        tool_calls: [{ id: "call-1", type: "function" }],
        finish_reason: "tool_calls"
      },
      {
        role: "tool",
        name: "enterprise_sql_query",
        status: "completed",
        result: "查询完成"
      },
      { role: "assistant", content: "最终结论" }
    ])).toEqual([
      expect.objectContaining({ kind: "activity", text: "先查上海区目标，再汇总业绩。" }),
      expect.objectContaining({ kind: "tool", name: "enterprise_sql_query" }),
      expect.objectContaining({ role: "assistant", content: "最终结论" })
    ]);
  });

  it("projects scheduled source metadata onto only the persisted user message", () => {
    const messages = historyMessages([
      {
        role: "user",
        content: "总结今天的工作内容",
        source: {
          type: "scheduled",
          scheduledTaskId: 123,
          scheduledTaskName: "每日工作总结",
          triggerKind: "scheduled",
          taskAvailable: true
        }
      },
      {
        role: "assistant",
        content: "好的"
      }
    ]);

    expect(messages[0]).toMatchObject({
      role: "user",
      scheduledSource: {
        type: "scheduled",
        scheduledTaskId: 123,
        scheduledTaskName: "每日工作总结",
        triggerKind: "scheduled",
        taskAvailable: true
      }
    });
    expect(messages[1]).not.toHaveProperty("scheduledSource");
  });

  it("keeps a deleted scheduled task source as a non-clickable snapshot", () => {
    const [message] = historyMessages([{
      role: "user",
      content: "旧任务",
      source: {
        type: "scheduled",
        scheduled_task_id: 8,
        scheduled_task_name: "已删除任务",
        trigger_kind: "manual",
        task_available: false
      }
    }]);

    expect(message).toMatchObject({
      scheduledSource: {
        scheduledTaskId: 8,
        scheduledTaskName: "已删除任务",
        triggerKind: "manual",
        taskAvailable: false
      }
    });
  });

  it("preserves local scheduled source when equivalent Runtime history omits it", () => {
    const source = {
      type: "scheduled" as const,
      scheduledTaskId: 123,
      scheduledTaskName: "每日工作总结",
      triggerKind: "scheduled",
      taskAvailable: true
    };
    const local: ChatMessage[] = [
      { id: "local-user", role: "user", content: "总结今天", scheduledSource: source },
      { id: "local-assistant", role: "assistant", content: "完成" }
    ];
    const remote: ChatMessage[] = [
      { id: "remote-user", role: "user", content: "总结今天" },
      { id: "remote-assistant", role: "assistant", content: "完成" }
    ];

    expect(reconcileSessionHistory(remote, local)[0]).toMatchObject({
      id: "local-user",
      scheduledSource: source
    });
  });

  it("keeps local messages when refreshed history is empty or behind", () => {
    const local: ChatMessage[] = [
      { id: "local-user-1", role: "user", content: "first question" },
      { id: "local-assistant-1", role: "assistant", content: "first answer" },
      { id: "local-user-2", role: "user", content: "你好" },
      { id: "local-assistant-2", role: "assistant", content: "partial", status: "streaming" }
    ];
    const behind: ChatMessage[] = [
      { id: "history-user-1", role: "user", content: "first question" },
      { id: "history-assistant-1", role: "assistant", content: "first answer" }
    ];

    expect(reconcileSessionHistory([], local)).toEqual(local);
    expect(reconcileSessionHistory(behind, local)).toEqual(local);
  });

  it("keeps the active SSE projection until history contains the submitted turn", () => {
    const local: ChatMessage[] = [
      { id: "local-user", role: "user", content: "你好" },
      { id: "local-assistant", role: "assistant", content: "正在回答", status: "streaming" }
    ];
    const remote: ChatMessage[] = [
      { id: "history-user", role: "user", content: "你好" },
      { id: "history-assistant", role: "assistant", content: "远端旧内容" }
    ];

    expect(reconcileSessionHistory(remote, local, {
      turnId: "turn-1",
      userMessageId: "local-user",
      assistantMessageId: "local-assistant",
      submittedText: "你好",
      status: "streaming"
    })).toEqual(local);
  });

  it("prepends delayed history when a cold-start submission is the only local turn", () => {
    const remote: ChatMessage[] = [
      { id: "history-user", role: "user", content: "之前的问题" },
      { id: "history-assistant", role: "assistant", content: "之前的回答" }
    ];
    const local: ChatMessage[] = [
      { id: "local-user", role: "user", content: "你好" },
      { id: "local-assistant", role: "assistant", content: "正在回答", status: "streaming" }
    ];

    expect(reconcileSessionHistory(remote, local, {
      turnId: "turn-cold",
      userMessageId: "local-user",
      assistantMessageId: "local-assistant",
      submittedText: "你好",
      status: "streaming"
    })).toEqual([...remote, ...local]);
  });

  it("adopts complete delayed history after a cold-start turn finishes", () => {
    const local: ChatMessage[] = [
      { id: "local-user", role: "user", content: "你好" },
      { id: "local-assistant", role: "assistant", content: "最新回答", ttftMs: 350 }
    ];
    const remote: ChatMessage[] = [
      { id: "history-old-user", role: "user", content: "之前的问题" },
      { id: "history-old-assistant", role: "assistant", content: "之前的回答" },
      { id: "history-new-user", role: "user", content: "你好" },
      { id: "history-new-assistant", role: "assistant", content: "最新回答" }
    ];

    expect(reconcileSessionHistory(remote, local, {
      turnId: "turn-cold",
      userMessageId: "local-user",
      assistantMessageId: "local-assistant",
      submittedText: "你好",
      status: "completed"
    })).toEqual([
      remote[0],
      remote[1],
      expect.objectContaining({ id: "local-user", content: "你好" }),
      expect.objectContaining({ id: "local-assistant", content: "最新回答", ttftMs: 350 })
    ]);
  });

  it("adopts caught-up history while retaining exact local identity and TTFT", () => {
    const local: ChatMessage[] = [
      { id: "local-user", role: "user", content: " 你好 " },
      { id: "local-assistant", role: "assistant", content: "完整回答", ttftMs: 420, status: "streaming" }
    ];
    const remote: ChatMessage[] = [
      { id: "history-user", role: "user", content: "你好" },
      { id: "history-assistant", role: "assistant", content: "完整回答" }
    ];

    expect(reconcileSessionHistory(remote, local, {
      turnId: "turn-1",
      userMessageId: "local-user",
      assistantMessageId: "local-assistant",
      submittedText: "你好",
      status: "completed"
    })).toEqual([
      expect.objectContaining({ id: "local-user", content: "你好" }),
      expect.objectContaining({ id: "local-assistant", content: "完整回答", ttftMs: 420 })
    ]);
  });

  it("preserves current-turn reasoning when completed history omits it", () => {
    const local: ChatMessage[] = [
      { id: "local-user", role: "user", content: "问题" },
      { id: "local-reasoning", kind: "reasoning", role: "assistant", text: "分析", ttftMs: 420 },
      { id: "local-assistant", role: "assistant", content: "答案" }
    ];
    const remote: ChatMessage[] = [
      { id: "history-user", role: "user", content: "问题" },
      { id: "history-assistant", role: "assistant", content: "答案" }
    ];

    expect(reconcileSessionHistory(remote, local, {
      turnId: "turn-1",
      userMessageId: "local-user",
      assistantMessageId: "local-assistant",
      submittedText: "问题",
      status: "completed"
    })).toEqual([
      expect.objectContaining({ id: "local-user", content: "问题" }),
      expect.objectContaining({ id: "local-reasoning", kind: "reasoning", text: "分析", ttftMs: 420 }),
      expect.objectContaining({ id: "local-assistant", content: "答案" })
    ]);
  });

  it("preserves current-turn tools and background activity when completed history omits them", () => {
    const local: ChatMessage[] = [
      { id: "local-user", role: "user", content: "生成演示文稿" },
      { id: "local-reasoning", kind: "reasoning", role: "assistant", text: "规划内容" },
      { id: "local-tool", kind: "tool", role: "tool", callId: "call-1", name: "write_file", status: "completed", result: "已生成" },
      { id: "local-background", kind: "background", role: "system", taskId: "sub-1", status: "completed", summary: "视觉检查完成" },
      { id: "local-assistant", role: "assistant", content: "演示文稿已完成" }
    ];
    const remote: ChatMessage[] = [
      { id: "history-user", role: "user", content: "生成演示文稿" },
      { id: "history-assistant", role: "assistant", content: "演示文稿已完成" }
    ];

    const result = reconcileSessionHistory(remote, local, {
      turnId: "turn-1",
      userMessageId: "local-user",
      assistantMessageId: "local-assistant",
      submittedText: "生成演示文稿",
      status: "completed"
    });

    expect(result.map((message) => message.id)).toEqual([
      "local-user", "local-reasoning", "local-tool", "local-background", "local-assistant"
    ]);
  });

  it("assigns the active run id to authoritative execution history", () => {
    const remote = historyMessages([
      { role: "user", content: "查业绩" },
      {
        role: "assistant",
        content: "我来查一下上海区的业绩完成情况。",
        reasoning_content: "加载区域口径",
        tool_calls: [{ id: "call-1" }],
        finish_reason: "tool_calls"
      },
      { role: "tool", name: "enterprise_sql_query", callId: "call-1", hasStableCallId: true, status: "completed", result: "完成" },
      { role: "assistant", content: "最终答案" }
    ]);
    const local: ChatMessage[] = [
      { id: "user-current", role: "user", content: "查业绩" },
      { id: "assistant-current", role: "assistant", content: "最终答案", status: "completed" }
    ];
    const result = reconcileSessionHistory(remote, local, {
      turnId: "turn-current",
      userMessageId: "user-current",
      assistantMessageId: "assistant-current",
      submittedText: "查业绩",
      runtimeRunId: "run-current",
      status: "completed"
    });
    expect(result.filter((message) => message.kind === "reasoning" || message.kind === "activity" || message.kind === "tool"))
      .toEqual(expect.arrayContaining([
        expect.objectContaining({ runtimeRunId: "run-current" })
      ]));
  });

  it("does not duplicate current-turn reasoning already present in completed history", () => {
    const local: ChatMessage[] = [
      { id: "local-user", role: "user", content: "问题" },
      { id: "local-reasoning", kind: "reasoning", role: "assistant", text: "分析", ttftMs: 420 },
      { id: "local-assistant", role: "assistant", content: "答案" }
    ];
    const remote: ChatMessage[] = [
      { id: "history-user", role: "user", content: "问题" },
      { id: "history-reasoning", kind: "reasoning", role: "assistant", text: "分析" },
      { id: "history-assistant", role: "assistant", content: "答案" }
    ];

    const result = reconcileSessionHistory(remote, local, {
      turnId: "turn-1",
      userMessageId: "local-user",
      assistantMessageId: "local-assistant",
      submittedText: "问题",
      status: "completed"
    });

    expect(result.filter((message) => message.kind === "reasoning")).toEqual([
      expect.objectContaining({ id: "local-reasoning", text: "分析", ttftMs: 420 })
    ]);
  });

  it("deduplicates a streaming first follow-up when remote history already contains the turn", () => {
    const remote: ChatMessage[] = [
      { id: "history-old-user", role: "user", content: "历史问题" },
      { id: "history-old-assistant", role: "assistant", content: "历史回答" },
      { id: "history-current-user", role: "user", content: "今天几号" },
      { id: "history-current-assistant", role: "assistant", content: "今天是 2026 年 7 月 18 日" }
    ];
    const local: ChatMessage[] = [
      { id: "local-current-user", role: "user", content: "今天几号" },
      {
        id: "local-current-reasoning",
        kind: "reasoning",
        role: "assistant",
        text: "先读取当前日期",
        ttftMs: 320
      },
      {
        id: "local-current-assistant",
        role: "assistant",
        content: "今天是 2026 年 7 月 18 日",
        status: "streaming",
        ttftMs: 320
      }
    ];

    const result = reconcileSessionHistory(remote, local, {
      turnId: "turn-current",
      userMessageId: "local-current-user",
      assistantMessageId: "local-current-assistant",
      submittedText: "今天几号",
      status: "streaming"
    });

    expect(result.filter((message) =>
      message.role === "user" && message.kind === undefined && message.content === "今天几号"
    )).toHaveLength(1);
    expect(result.filter((message) =>
      message.role === "assistant" && message.kind === undefined
      && message.content === "今天是 2026 年 7 月 18 日"
    )).toHaveLength(1);
    expect(result).toContainEqual(expect.objectContaining({
      id: "local-current-reasoning",
      kind: "reasoning",
      ttftMs: 320
    }));
    expect(result).toContainEqual(expect.objectContaining({
      id: "local-current-user",
      content: "今天几号"
    }));
    expect(result).toContainEqual(expect.objectContaining({
      id: "local-current-assistant",
      content: "今天是 2026 年 7 月 18 日",
      ttftMs: 320
    }));
  });

  it("preserves local tool and background rows while deduplicating the active turn", () => {
    const remote: ChatMessage[] = [
      { id: "history-old-user", role: "user", content: "历史问题" },
      { id: "history-old-assistant", role: "assistant", content: "历史回答" },
      { id: "history-current-user", role: "user", content: "查询状态" },
      { id: "history-current-assistant", role: "assistant", content: "查询完成" }
    ];
    const local: ChatMessage[] = [
      { id: "local-current-user", role: "user", content: "查询状态" },
      {
        id: "local-tool",
        kind: "tool",
        role: "tool",
        callId: "call-1",
        name: "status_lookup",
        status: "completed",
        result: "ok"
      },
      {
        id: "local-background",
        kind: "background",
        role: "system",
        taskId: "task-1",
        status: "completed",
        summary: "后台检查完成"
      },
      {
        id: "local-current-assistant",
        role: "assistant",
        content: "查询完成",
        status: "streaming"
      }
    ];

    const result = reconcileSessionHistory(remote, local, {
      turnId: "turn-current",
      userMessageId: "local-current-user",
      assistantMessageId: "local-current-assistant",
      submittedText: "查询状态",
      status: "streaming"
    });

    expect(result.filter((message) => message.kind === "tool")).toHaveLength(1);
    expect(result.filter((message) => message.kind === "background")).toHaveLength(1);
    expect(result.filter((message) =>
      message.role === "user" && message.kind === undefined && message.content === "查询状态"
    )).toHaveLength(1);
    expect(result.filter((message) =>
      message.role === "assistant" && message.kind === undefined && message.content === "查询完成"
    )).toHaveLength(1);
  });

  it("keeps the local streaming assistant when remote history has only accepted the active user", () => {
    const remote: ChatMessage[] = [
      { id: "history-old-user", role: "user", content: "历史问题" },
      { id: "history-old-assistant", role: "assistant", content: "历史回答" },
      { id: "history-current-user", role: "user", content: "今天几号" }
    ];
    const local: ChatMessage[] = [
      { id: "local-current-user", role: "user", content: "今天几号" },
      { id: "local-reasoning", kind: "reasoning", role: "assistant", text: "读取日期" },
      {
        id: "local-current-assistant",
        role: "assistant",
        content: "今天是",
        status: "streaming"
      }
    ];

    const result = reconcileSessionHistory(remote, local, {
      turnId: "turn-current",
      userMessageId: "local-current-user",
      assistantMessageId: "local-current-assistant",
      submittedText: "今天几号",
      status: "streaming"
    });

    expect(result.filter((message) =>
      message.role === "user" && message.kind === undefined && message.content === "今天几号"
    )).toHaveLength(1);
    expect(result).toContainEqual(expect.objectContaining({
      id: "local-current-assistant",
      content: "今天是",
      status: "streaming"
    }));
    expect(result).toContainEqual(expect.objectContaining({
      id: "local-reasoning",
      kind: "reasoning"
    }));
  });

  it("merges only the last repeated question as the active turn", () => {
    const remote: ChatMessage[] = [
      { id: "history-first-user", role: "user", content: "今天几号" },
      { id: "history-first-assistant", role: "assistant", content: "第一次回答" },
      { id: "history-current-user", role: "user", content: "今天几号" },
      { id: "history-current-assistant", role: "assistant", content: "第二次回答" }
    ];
    const local: ChatMessage[] = [
      { id: "local-current-user", role: "user", content: "今天几号" },
      { id: "local-reasoning", kind: "reasoning", role: "assistant", text: "重新读取日期" },
      {
        id: "local-current-assistant",
        role: "assistant",
        content: "第二次回答",
        status: "streaming"
      }
    ];

    const result = reconcileSessionHistory(remote, local, {
      turnId: "turn-current",
      userMessageId: "local-current-user",
      assistantMessageId: "local-current-assistant",
      submittedText: "今天几号",
      status: "streaming"
    });

    expect(result.filter((message) =>
      message.role === "user" && message.kind === undefined && message.content === "今天几号"
    )).toHaveLength(2);
    expect(result.filter((message) =>
      message.role === "assistant" && message.kind === undefined
      && message.content === "第二次回答"
    )).toHaveLength(1);
    expect(result).toContainEqual(expect.objectContaining({
      id: "history-first-user",
      content: "今天几号"
    }));
    expect(result).toContainEqual(expect.objectContaining({
      id: "local-current-user",
      content: "今天几号"
    }));
  });

  it("keeps one local reasoning row when remote history contains its completed prefix", () => {
    const remote: ChatMessage[] = [
      { id: "history-old-user", role: "user", content: "历史问题" },
      { id: "history-old-assistant", role: "assistant", content: "历史回答" },
      { id: "history-current-user", role: "user", content: "本月有啥节日？" },
      {
        id: "history-current-reasoning",
        kind: "reasoning",
        role: "assistant",
        text: "正在分析 本月节日"
      },
      { id: "history-current-assistant", role: "assistant", content: "本月节日如下" }
    ];
    const local: ChatMessage[] = [
      { id: "user-rr-current", role: "user", content: "本月有啥节日？" },
      {
        id: "reasoning-rr-current-2",
        kind: "reasoning",
        role: "assistant",
        text: "正在分析本月",
        status: "streaming",
        ttftMs: 420
      },
      {
        id: "assistant-rr-current",
        role: "assistant",
        content: "",
        status: "streaming"
      }
    ];
    const activeTurn: ActiveTurn = {
      turnId: "turn-current",
      userMessageId: "user-rr-current",
      assistantMessageId: "assistant-rr-current",
      submittedText: "本月有啥节日？",
      runtimeRunId: "rr-current",
      status: "streaming"
    };

    const merged = reconcileSessionHistory(remote, local, activeTurn);

    expect(merged.filter((message) => message.kind === "reasoning")).toEqual([
      expect.objectContaining({
        id: "reasoning-rr-current-2",
        text: "正在分析本月",
        ttftMs: 420
      })
    ]);

    const cache: SessionCache = {
      messages: merged,
      files: [],
      runId: "rr-current",
      lastSeq: 2,
      streaming: true,
      activeTurn
    };
    const projected = projectChatRunEvent(cache, "rr-current", {
      event: "chat.reasoning.delta",
      data: { seq: 3, delta: "节日" }
    });

    expect(projected.cache.messages.filter((message) => message.kind === "reasoning")).toEqual([
      expect.objectContaining({
        id: "reasoning-rr-current-2",
        text: "正在分析本月节日",
        ttftMs: 420
      })
    ]);
  });

  it("collapses reasoning copies already present when completed history catches up", () => {
    const remote: ChatMessage[] = [
      { id: "history-old-user", role: "user", content: "历史问题" },
      { id: "history-old-assistant", role: "assistant", content: "历史回答" },
      { id: "history-current-user", role: "user", content: "本月有啥节日？" },
      {
        id: "history-current-reasoning",
        kind: "reasoning",
        role: "assistant",
        text: "完整 分析"
      },
      { id: "history-current-assistant", role: "assistant", content: "完整回答" }
    ];
    const local: ChatMessage[] = [
      { id: "local-current-user", role: "user", content: "本月有啥节日？" },
      {
        id: "local-history-copy",
        kind: "reasoning",
        role: "assistant",
        text: "完整分析"
      },
      {
        id: "local-sse-copy",
        kind: "reasoning",
        role: "assistant",
        text: "完整分析",
        ttftMs: 420
      },
      { id: "local-current-assistant", role: "assistant", content: "完整回答" }
    ];

    const result = reconcileSessionHistory(remote, local, {
      turnId: "turn-current",
      userMessageId: "local-current-user",
      assistantMessageId: "local-current-assistant",
      submittedText: "本月有啥节日？",
      runtimeRunId: "rr-current",
      status: "completed"
    });

    expect(result.filter((message) => message.kind === "reasoning")).toEqual([
      expect.objectContaining({
        id: "local-sse-copy",
        text: "完整 分析",
        ttftMs: 420
      })
    ]);
  });

  it("keeps equal reasoning segments on opposite sides of a tool boundary", () => {
    const remote: ChatMessage[] = [
      { id: "history-current-user", role: "user", content: "查询节日" },
      {
        id: "history-reasoning-before-tool",
        kind: "reasoning",
        role: "assistant",
        text: "分析 节日信息"
      },
      {
        id: "history-tool",
        kind: "tool",
        role: "tool",
        callId: "call-1",
        hasStableCallId: true,
        name: "calendar_lookup",
        status: "completed",
        result: "ok"
      },
      {
        id: "history-reasoning-after-tool",
        kind: "reasoning",
        role: "assistant",
        text: "分析 节日信息"
      },
      { id: "history-current-assistant", role: "assistant", content: "查询完成" }
    ];
    const local: ChatMessage[] = [
      { id: "local-current-user", role: "user", content: "查询节日" },
      {
        id: "local-reasoning-before-tool",
        kind: "reasoning",
        role: "assistant",
        text: "分析节日"
      },
      {
        id: "local-tool",
        kind: "tool",
        role: "tool",
        callId: "call-1",
        hasStableCallId: true,
        name: "calendar_lookup",
        status: "completed",
        result: "ok"
      },
      {
        id: "local-reasoning-after-tool",
        kind: "reasoning",
        role: "assistant",
        text: "分析节日"
      },
      {
        id: "local-current-assistant",
        role: "assistant",
        content: "",
        status: "streaming"
      }
    ];

    const result = reconcileSessionHistory(remote, local, {
      turnId: "turn-current",
      userMessageId: "local-current-user",
      assistantMessageId: "local-current-assistant",
      submittedText: "查询节日",
      runtimeRunId: "rr-current",
      status: "streaming"
    });

    expect(result.filter((message) => message.kind === "reasoning")).toEqual([
      expect.objectContaining({
        id: "local-reasoning-before-tool",
        text: "分析节日"
      }),
      expect.objectContaining({
        id: "local-reasoning-after-tool",
        text: "分析节日"
      })
    ]);
    expect(result.filter((message) => message.kind === "tool")).toHaveLength(1);
  });

  it("preserves a failed local turn instead of replacing it with incomplete history", () => {
    const local: ChatMessage[] = [
      { id: "local-user", role: "user", content: "你好" },
      { id: "local-assistant", role: "assistant", content: "", status: "failed", error: "runtime unavailable" }
    ];
    const remote: ChatMessage[] = [{ id: "history-user", role: "user", content: "你好" }];

    expect(reconcileSessionHistory(remote, local, {
      turnId: "turn-1",
      userMessageId: "local-user",
      assistantMessageId: "local-assistant",
      submittedText: "你好",
      status: "failed",
      error: "runtime unavailable"
    })).toEqual(local);
  });

  it("reconciles equal correction text only by ledger identity", () => {
    const local: ChatMessage[] = [
      {
        id: "correction-correction-1",
        role: "user",
        content: "相同修正",
        clientMessageId: "correction-1"
      },
      {
        id: "correction-correction-2",
        role: "user",
        content: "相同修正",
        clientMessageId: "correction-2"
      }
    ];
    const remote: ChatMessage[] = [
      {
        id: "history-correction-1",
        role: "user",
        content: "相同修正",
        clientMessageId: "correction-1"
      }
    ];

    expect(reconcileSessionHistory(remote, local)).toEqual(local);
    expect(reconcileSessionHistory([
      ...remote,
      {
        id: "history-correction-2",
        role: "user",
        content: "相同修正",
        clientMessageId: "correction-2"
      }
    ], local)).toEqual([
      expect.objectContaining({ id: "correction-correction-1" }),
      expect.objectContaining({ id: "correction-correction-2" })
    ]);
  });
});
