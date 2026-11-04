import { describe, expect, it } from "vitest";

import { acceptChatRun, beginChatCorrection, beginChatTurn, beginPromotedCorrectionRun, beginQueuedChatSubmission, cancelChatTurn, ensureActiveChatRun, failChatTurn, isFirstVisibleAgentEvent, projectChatAssetEvent, projectChatRunEvent, type SessionCache } from "./chatRunProjection";

describe("chat run projection reducer", () => {
  it("keeps the Run streaming after a recoverable capability error card", () => {
    const started = projectChatRunEvent(
      { messages: [], files: [], streaming: true, runId: "run-1" },
      "run-1",
      {
        event: "capability.error",
        data: {
          run_id: "run-1",
          capability_type: "knowledge_base",
          error_code: "KNOWLEDGE_BASE_NO_PERMISSION",
          display_message: "资产访问权限不足",
          recoverable: true,
          error_id: "caperr-12345678"
        }
      }
    );

    expect(started.applied).toBe(true);
    expect(started.cache.streaming).toBe(true);
    expect(started.errorMessage).toBeUndefined();
    expect(started.cache.messages).toContainEqual(expect.objectContaining({
      kind: "capability-error",
      runtimeRunId: "run-1",
      displayMessage: "资产访问权限不足",
      errorCode: "KNOWLEDGE_BASE_NO_PERMISSION"
    }));

    const continued = projectChatRunEvent(started.cache, "run-1", {
      event: "chat.message.delta",
      data: { delta: "我会继续使用其他信息回答。" }
    });
    expect(continued.cache.messages).toContainEqual(expect.objectContaining({
      role: "assistant",
      content: "我会继续使用其他信息回答。"
    }));
  });
  describe("first visible agent event", () => {
    it("accepts only events that produce visible agent output", () => {
      expect(isFirstVisibleAgentEvent({ event: "chat.message.delta", data: { delta: "answer" } })).toBe(true);
      expect(isFirstVisibleAgentEvent({ event: "chat.reasoning.delta", data: { delta: "thinking" } })).toBe(true);
      expect(isFirstVisibleAgentEvent({ event: "chat.message.completed", data: { text: "answer" } })).toBe(true);
      expect(isFirstVisibleAgentEvent({ event: "chat.tool.event", data: {} })).toBe(true);
      expect(isFirstVisibleAgentEvent({ event: "chat.turn.parked", data: {} })).toBe(true);
      expect(isFirstVisibleAgentEvent({ event: "chat.subagent.event", data: { type: "subagent.start", subagent_id: "sub-1" } })).toBe(true);

      expect(isFirstVisibleAgentEvent({ event: "chat.message.delta", data: { delta: "   " } })).toBe(false);
      expect(isFirstVisibleAgentEvent({ event: "chat.reasoning.delta", data: { delta: "\n" } })).toBe(false);
      expect(isFirstVisibleAgentEvent({ event: "chat.subagent.event", data: { type: "subagent.progress", tool_preview: "terminal" } })).toBe(false);
      expect(isFirstVisibleAgentEvent({ event: "chat.run.accepted", data: {} })).toBe(false);
      expect(isFirstVisibleAgentEvent({ event: "chat.run.started", data: {} })).toBe(false);
      expect(isFirstVisibleAgentEvent({ event: "chat.transport.reconnecting", data: {} })).toBe(false);
      expect(isFirstVisibleAgentEvent({ event: "chat.error", data: { message: "failed" } })).toBe(false);
      expect(isFirstVisibleAgentEvent({ event: "chat.done", data: {} })).toBe(false);
    });
  });

  it("reports completed-only assistant text as a visible mutation", () => {
    const result = projectChatRunEvent({ messages: [], files: [] }, "rr-completed", {
      event: "chat.message.completed",
      data: { seq: 1, text: "final answer" }
    });

    expect(result.visibleMutation).toEqual({
      targetId: "assistant-rr-completed",
      kind: "assistant-text"
    });
  });

  it("archives an interim answer before streaming and reconciling the final answer", () => {
    const accepted = acceptChatRun(
      beginChatTurn({ messages: [], files: [] }, "完成复杂任务", "turn-interim"),
      "rr-interim"
    );
    const provisional = projectChatRunEvent(accepted, "rr-interim", {
      event: "chat.message.delta",
      data: { seq: 1, delta: "我先检查现有文件。" }
    });
    const archived = projectChatRunEvent(provisional.cache, "rr-interim", {
      event: "chat.message.completed",
      data: {
        seq: 2,
        text: "我先检查现有文件。",
        status: "complete",
        interim: true
      }
    });
    const duplicate = projectChatRunEvent(archived.cache, "rr-interim", {
      event: "chat.message.completed",
      data: {
        seq: 2,
        text: "我先检查现有文件。",
        status: "complete",
        interim: true
      }
    });
    const finalDelta = projectChatRunEvent(duplicate.cache, "rr-interim", {
      event: "chat.message.delta",
      data: { seq: 3, delta: "文件已经处理" }
    });
    const completed = projectChatRunEvent(finalDelta.cache, "rr-interim", {
      event: "chat.message.completed",
      data: { seq: 4, text: "文件已经处理完成。", status: "complete" }
    });

    expect(archived.cache.messages).toContainEqual(expect.objectContaining({
      id: "activity-rr-interim-interim-2",
      kind: "activity",
      text: "我先检查现有文件。",
      status: "completed"
    }));
    expect(
      archived.cache.messages.find((message) => message.id === "assistant-rr-interim")
    ).toEqual(expect.objectContaining({ content: "", status: "streaming" }));
    expect(archived.cache.streaming).toBe(true);
    expect(duplicate.applied).toBe(false);
    expect(
      duplicate.cache.messages.filter(
        (message) => message.id === "activity-rr-interim-interim-2"
      )
    ).toHaveLength(1);
    expect(completed.cache.messages).toContainEqual(expect.objectContaining({
      id: "assistant-rr-interim",
      content: "文件已经处理完成。",
      status: "completed"
    }));
    expect(completed.cache.messages).toContainEqual(expect.objectContaining({
      id: "activity-rr-interim-interim-2",
      text: "我先检查现有文件。"
    }));
  });

  it("moves a streamed preamble into the thinking timeline when a tool starts", () => {
    const accepted = acceptChatRun(
      beginChatTurn({ messages: [], files: [] }, "查询天气", "turn-tool-preamble"),
      "rr-tool-preamble"
    );
    const preamble = projectChatRunEvent(accepted, "rr-tool-preamble", {
      event: "chat.message.delta",
      data: { seq: 1, delta: "我先并行加载相关技能和了解企业知识工具。" }
    });
    const withTool = projectChatRunEvent(preamble.cache, "rr-tool-preamble", {
      event: "chat.tool.event",
      data: { seq: 2, name: "knowledge_search", status: "running", callId: "call-1" }
    });

    expect(withTool.cache.messages).toContainEqual(expect.objectContaining({
      kind: "activity",
      text: "我先并行加载相关技能和了解企业知识工具。"
    }));
    expect(withTool.cache.messages).toContainEqual(expect.objectContaining({
      kind: "tool",
      name: "knowledge_search",
      status: "running"
    }));
    expect(withTool.cache.messages.find((message) => message.id === "assistant-rr-tool-preamble"))
      .toEqual(expect.objectContaining({ content: "", status: "streaming" }));
  });

  it("reclassifies a completed preamble when a later tool proves the turn is still running", () => {
    const accepted = acceptChatRun(
      beginChatTurn({ messages: [], files: [] }, "查询知识库", "turn-completed-preamble"),
      "rr-completed-preamble"
    );
    const preamble = projectChatRunEvent(accepted, "rr-completed-preamble", {
      event: "chat.message.delta",
      data: { seq: 1, delta: "我先检查知识库。" }
    });
    const completed = projectChatRunEvent(preamble.cache, "rr-completed-preamble", {
      event: "chat.message.completed",
      data: { seq: 2, text: "我先检查知识库。", status: "complete" }
    });
    const withTool = projectChatRunEvent(completed.cache, "rr-completed-preamble", {
      event: "chat.tool.event",
      data: { seq: 3, name: "knowledge_search", status: "running", callId: "call-1" }
    });

    expect(withTool.cache.messages).toContainEqual(expect.objectContaining({
      kind: "activity",
      text: "我先检查知识库。"
    }));
    expect(withTool.cache.messages.find((message) => message.id === "assistant-rr-completed-preamble"))
      .toEqual(expect.objectContaining({ content: "", status: "streaming" }));
  });

  it("keeps recovered in-flight text in the execution timeline until completion", () => {
    const accepted = acceptChatRun(
      beginChatTurn({ messages: [], files: [] }, "完成长任务", "turn-recovered-interim"),
      "rr-recovered-interim"
    );
    const partial = projectChatRunEvent(accepted, "rr-recovered-interim", {
      event: "chat.message.delta",
      data: { delta: "我先" }
    });
    const recovered = projectChatRunEvent(partial.cache, "rr-recovered-interim", {
      event: "chat.message.delta",
      data: { delta: "检查文件结构。", interim: true, recovered: true }
    });

    expect(recovered.cache.messages).toContainEqual(expect.objectContaining({
      kind: "activity",
      text: "我先检查文件结构。"
    }));
    expect(recovered.cache.messages.find((message) => message.id === "assistant-rr-recovered-interim"))
      .toEqual(expect.objectContaining({ content: "", status: "streaming" }));

    const completed = projectChatRunEvent(recovered.cache, "rr-recovered-interim", {
      event: "chat.message.completed",
      data: { text: "任务已完成。", status: "complete" }
    });
    expect(completed.cache.messages).toContainEqual(expect.objectContaining({
      id: "assistant-rr-recovered-interim",
      content: "任务已完成。",
      status: "completed"
    }));
  });

  it("keeps one assistant placeholder from submit through started and removes it for real content", () => {
    const accepted = acceptChatRun(
      beginChatTurn({ messages: [], files: [] }, "hello", "turn-lifecycle"),
      "rr-lifecycle",
      "assistant-pending-turn-lifecycle"
    );
    const started = projectChatRunEvent(accepted, "rr-lifecycle", {
      event: "chat.run.started",
      data: { seq: 1 },
      ttftMs: 240
    });
    const reasoning = projectChatRunEvent(started.cache, "rr-lifecycle", {
      event: "chat.reasoning.delta",
      data: { seq: 2, delta: "真实思考" },
      ttftMs: 680
    });

    expect(started.visibleMutation).toEqual({
      targetId: "assistant-rr-lifecycle",
      kind: "lifecycle"
    });
    expect(started.cache.messages).toContainEqual(expect.objectContaining({
      id: "assistant-rr-lifecycle",
      thinkingPhase: "started",
      ttftMs: 240
    }));
    expect(reasoning.cache.messages).toContainEqual(expect.objectContaining({
      id: "assistant-rr-lifecycle",
      content: ""
    }));
    expect(
      reasoning.cache.messages.find((message) => message.id === "assistant-rr-lifecycle")
    ).not.toHaveProperty("thinkingPhase");
    expect(reasoning.cache.messages).toContainEqual(expect.objectContaining({
      kind: "reasoning",
      runtimeRunId: "rr-lifecycle",
      text: "真实思考",
      ttftMs: 680
    }));
  });

  it("settles cancellation and removes only an empty assistant placeholder", () => {
    const accepted = acceptChatRun(
      beginChatTurn({ messages: [], files: [] }, "stop me", "turn-stop"),
      "rr-stop"
    );
    const canceled = cancelChatTurn(accepted, "rr-stop");

    expect(canceled.streaming).toBe(false);
    expect(canceled.runId).toBeNull();
    expect(canceled.activeTurn?.status).toBe("completed");
    expect(canceled.messages).toContainEqual(expect.objectContaining({
      role: "user",
      content: "stop me"
    }));
    expect(canceled.messages.some((message) => message.id === "assistant-rr-stop")).toBe(false);
  });

  it("preserves a real partial assistant answer while settling cancellation", () => {
    const accepted = acceptChatRun(
      beginChatTurn({ messages: [], files: [] }, "stop later", "turn-partial"),
      "rr-partial"
    );
    const completed = projectChatRunEvent(accepted, "rr-partial", {
      event: "chat.message.completed",
      data: { text: "已经完成第一部分。", status: "interrupted" }
    });
    const canceled = cancelChatTurn(completed.cache, "rr-partial");

    expect(canceled.messages).toContainEqual(expect.objectContaining({
      id: "assistant-rr-partial",
      content: "已经完成第一部分。"
    }));
    expect(canceled.messages.every((message) => !message.error)).toBe(true);
  });

  it("reports rendered progress but not parked, blank, transport, or duplicate events as visible", () => {
    const progress = projectChatRunEvent({ messages: [], files: [] }, "rr-1", {
      event: "chat.progress.delta",
      data: { seq: 1, delta: "working" }
    });
    const parked = projectChatRunEvent(progress.cache, "rr-1", {
      event: "chat.turn.parked",
      data: { seq: 2 }
    });
    const blank = projectChatRunEvent(parked.cache, "rr-1", {
      event: "chat.message.delta",
      data: { seq: 3, delta: "   " }
    });
    const transport = projectChatRunEvent(blank.cache, "rr-1", {
      event: "chat.transport.reconnecting",
      data: { seq: 4 }
    });
    const duplicate = projectChatRunEvent(transport.cache, "rr-1", {
      event: "chat.message.delta",
      data: { seq: 4, delta: "answer" }
    });

    expect(progress.visibleMutation).toEqual({
      targetId: "progress-rr-1",
      kind: "progress"
    });
    expect(parked.visibleMutation).toBeUndefined();
    expect(blank.visibleMutation).toBeUndefined();
    expect(transport.visibleMutation).toBeUndefined();
    expect(duplicate.applied).toBe(false);
    expect(duplicate.visibleMutation).toBeUndefined();
  });

  it("uses the stable rendered Run timeline as the visible mutation target", () => {
    const first = projectChatRunEvent({ messages: [], files: [] }, "rr-tool", {
      event: "chat.tool.event",
      data: { seq: 1, call_id: "call-1", name: "read_file", status: "running" }
    });
    const second = projectChatRunEvent(first.cache, "rr-tool", {
      event: "chat.tool.event",
      data: { seq: 2, call_id: "call-2", name: "write_file", status: "running" }
    });

    expect(first.visibleMutation?.targetId).toMatch(/^execution-timeline-/);
    expect(second.visibleMutation?.targetId).toBe(first.visibleMutation?.targetId);
  });

  it("projects a Clarify pause as a correlated timeline task until the Run resumes", () => {
    const paused = projectChatRunEvent({ messages: [], files: [] }, "rr-clarify", {
      event: "chat.clarify.requested",
      data: { seq: 1, request_id: "clr-1" }
    });

    expect(paused.cache.clarifyRunId).toBe("rr-clarify");
    expect(paused.cache.messages).toContainEqual(expect.objectContaining({
      kind: "background",
      runtimeRunId: "rr-clarify",
      taskId: "clarify",
      activity: "任务已暂停，等待确认"
    }));

    const resumed = projectChatRunEvent(paused.cache, "rr-clarify", {
      event: "chat.turn.resumed",
      data: { seq: 2 }
    });
    expect(resumed.cache.clarifyRunId).toBeUndefined();
    expect(resumed.cache.messages.some((message) => message.kind === "background" && message.taskId === "clarify")).toBe(false);
  });

  it("creates a correlated optimistic turn and binds it to the accepted run", () => {
    const begun = beginChatTurn({ messages: [], files: [] }, "你好", "turn-1");

    expect(begun.activeTurn).toMatchObject({
      turnId: "turn-1",
      userMessageId: "user-turn-1",
      assistantMessageId: "assistant-pending-turn-1",
      submittedText: "你好",
      status: "submitting"
    });
    expect(begun.messages).toEqual([
      expect.objectContaining({ id: "user-turn-1", role: "user", content: "你好" }),
      expect.objectContaining({
        id: "assistant-pending-turn-1",
        role: "assistant",
        status: "streaming",
        thinkingPhase: "submitting"
      })
    ]);

    const accepted = acceptChatRun(begun, "rr-1", begun.activeTurn?.assistantMessageId);
    expect(accepted.activeTurn).toMatchObject({
      runtimeRunId: "rr-1",
      assistantMessageId: "assistant-rr-1",
      status: "streaming"
    });
    expect(accepted.messages).toContainEqual(expect.objectContaining({
      id: "assistant-rr-1",
      thinkingPhase: "submitting"
    }));
  });

  it("attaches stream errors to the active assistant without deleting partial output", () => {
    const begun = beginChatTurn({ messages: [], files: [] }, "你好", "turn-1");
    const accepted = acceptChatRun(begun, "rr-1", begun.activeTurn?.assistantMessageId);
    const partial = projectChatRunEvent(accepted, "rr-1", {
      event: "chat.message.delta",
      data: { seq: 1, delta: "partial" }
    });
    const failed = projectChatRunEvent(partial.cache, "rr-1", {
      event: "chat.error",
      data: { seq: 2, message: "runtime unavailable" }
    });
    const done = projectChatRunEvent(failed.cache, "rr-1", {
      event: "chat.done",
      data: { seq: 3 }
    });

    expect(done.cache.messages).toEqual([
      expect.objectContaining({ role: "user", content: "你好" }),
      expect.objectContaining({ role: "assistant", content: "partial", status: "failed", error: "runtime unavailable" })
    ]);
    expect(done.cache.activeTurn).toMatchObject({ status: "failed", error: "runtime unavailable" });
    expect(done.cache.streaming).toBe(false);
  });

  it("marks a request failure before acceptance on the optimistic assistant", () => {
    const begun = beginChatTurn({ messages: [], files: [] }, "你好", "turn-1");
    const failed = failChatTurn(begun, "runtime is not ready");

    expect(failed.messages).toEqual([
      expect.objectContaining({ role: "user", content: "你好" }),
      expect.objectContaining({ role: "assistant", status: "failed", error: "runtime is not ready" })
    ]);
    expect(failed.activeTurn).toMatchObject({ status: "failed", error: "runtime is not ready" });
  });

  it("ignores pre-run asset status events", () => {
    const begun = beginChatTurn({ messages: [], files: [] }, "需要新资源", "turn-asset");
    const projecting = projectChatAssetEvent(begun, {
      event: "chat.asset.projecting",
      data: { generation: 2, status: "projecting" }
    });
    const reloading = projectChatAssetEvent(projecting.cache, {
      event: "chat.asset.reloading",
      data: { generation: 2, status: "applying" }
    });
    const applied = projectChatAssetEvent(reloading.cache, {
      event: "chat.asset.applied",
      data: { generation: 2, status: "applied" }
    });

    expect(projecting.applied).toBe(false);
    expect(reloading.applied).toBe(false);
    expect(applied.applied).toBe(false);
    expect(applied.cache).toBe(begun);
    expect(applied.cache.runId).toBeUndefined();
    expect(applied.cache.streaming).toBe(true);
  });

  it("ignores pre-run asset reload failures", () => {
    const begun = beginChatTurn({ messages: [], files: [] }, "需要新资源", "turn-asset");
    const failed = projectChatAssetEvent(begun, {
      event: "chat.asset.failed",
      data: { generation: 2, status: "failed", message: "dashboard reload unavailable" }
    });

    expect(failed.applied).toBe(false);
    expect(failed.errorMessage).toBeUndefined();
    expect(failed.cache).toBe(begun);
    expect(failed.cache.streaming).toBe(true);
  });

  it("projects one runtime run into stable user and assistant bubbles with seq dedupe", () => {
    const accepted = acceptChatRun(
      {
        messages: [
          { id: "user-local", role: "user", content: "question" },
          { id: "assistant-pending", role: "assistant", content: "", status: "streaming" }
        ],
        files: [],
        streaming: true
      },
      "rr-1",
      "assistant-pending"
    );

    expect(accepted.messages.map((message) => message.id)).toEqual(["user-local", "assistant-rr-1"]);
    expect(accepted.runId).toBe("rr-1");
    expect(accepted.lastSeq).toBe(0);

    const started = projectChatRunEvent(accepted, "rr-1", {
      event: "chat.run.started",
      data: { seq: 1, user_message: "question" }
    });
    const firstDelta = projectChatRunEvent(started.cache, "rr-1", {
      event: "chat.message.delta",
      data: { seq: 2, delta: "partial" }
    });
    const duplicateDelta = projectChatRunEvent(firstDelta.cache, "rr-1", {
      event: "chat.message.delta",
      data: { seq: 2, delta: "partial" }
    });
    const done = projectChatRunEvent(duplicateDelta.cache, "rr-1", {
      event: "chat.done",
      data: { seq: 3 }
    });

    expect(started.cache.messages.filter((message) => message.id === "user-rr-1")).toHaveLength(1);
    expect(duplicateDelta.applied).toBe(false);
    expect(done.cache.messages.find((message) => message.id === "assistant-rr-1")?.content).toBe("partial");
    expect(done.cache.runId).toBeNull();
    expect(done.cache.streaming).toBe(false);
    expect(done.cache.lastSeq).toBe(3);
  });

  it("projects the live scheduled source onto the user bubble", () => {
    const accepted = acceptChatRun(
      { messages: [], files: [] },
      "rr-scheduled",
    );
    const started = projectChatRunEvent(accepted, "rr-scheduled", {
      event: "chat.run.started",
      data: {
        seq: 1,
        user_message: "每日总结",
        source: {
          type: "scheduled",
          scheduledTaskId: 123,
          scheduledTaskName: "每日工作总结",
          triggerKind: "scheduled",
          taskAvailable: true
        }
      }
    });

    expect(started.cache.messages).toContainEqual(expect.objectContaining({
      id: "user-rr-scheduled",
      scheduledSource: expect.objectContaining({ scheduledTaskId: 123 })
    }));
  });

  it("merges a replayed scheduled source into an existing optimistic user bubble", () => {
    const accepted = acceptChatRun({
      messages: [{ id: "user-rr-replay", role: "user", content: "每日总结" }],
      files: []
    }, "rr-replay");
    const started = projectChatRunEvent(accepted, "rr-replay", {
      event: "chat.run.started",
      data: {
        seq: 1,
        user_message: "每日总结",
        source: {
          type: "scheduled",
          scheduled_task_id: 123,
          scheduled_task_name: "每日工作总结",
          trigger_kind: "scheduled",
          task_available: false
        }
      }
    });

    expect(started.cache.messages).toContainEqual(expect.objectContaining({
      id: "user-rr-replay",
      scheduledSource: expect.objectContaining({
        scheduledTaskId: 123,
        taskAvailable: false
      })
    }));
  });

  it("hydrates scheduled source on the latest user bubble during reconnect", () => {
    const cache = ensureActiveChatRun({
      messages: [
        { id: "user-local", role: "user", content: "每日总结" },
        { id: "assistant-rr-reconnect-source", role: "assistant", content: "", status: "streaming" }
      ],
      files: []
    }, "rr-reconnect-source", 4, {
      type: "scheduled",
      scheduledTaskId: 123,
      scheduledTaskName: "每日工作总结",
      triggerKind: "scheduled",
      taskAvailable: true
    });

    expect(cache.messages.find((message) => message.id === "user-rr-reconnect-source")).toEqual(expect.objectContaining({ scheduledSource: expect.objectContaining({ scheduledTaskId: 123 }) }));
    expect(cache.pendingScheduledSource).toMatchObject({ scheduledTaskId: 123 });

    const started = projectChatRunEvent(cache, "rr-reconnect-source", {
      event: "chat.run.started",
      data: { user_message: "每日总结", seq: 5 }
    });
    expect(started.cache.messages).toContainEqual(expect.objectContaining({
      id: "user-rr-reconnect-source",
      scheduledSource: expect.objectContaining({ scheduledTaskId: 123 })
    }));
  });

  it("keeps existing projection state when an older seq arrives", () => {
    const current: SessionCache = {
      messages: [{ id: "assistant-rr-2", role: "assistant", content: "new" }],
      files: [],
      runId: "rr-2",
      streaming: true,
      lastSeq: 5
    };

    const projected = projectChatRunEvent(current, "rr-2", {
      event: "chat.message.delta",
      data: { seq: 4, delta: "old" }
    });

    expect(projected.applied).toBe(false);
    expect(projected.cache).toBe(current);
  });

  it("keeps reasoning separate from final content and deduplicates replayed chunks", () => {
    const initial: SessionCache = {
      messages: [{ id: "assistant-rr-3", role: "assistant", content: "", status: "streaming" }],
      files: [],
      runId: "rr-3",
      streaming: true,
      lastSeq: 0
    };

    const reasoning = projectChatRunEvent(initial, "rr-3", {
      event: "chat.reasoning.delta",
      data: { seq: 1, delta: "step one " }
    });
    const continued = projectChatRunEvent(reasoning.cache, "rr-3", {
      event: "chat.reasoning.delta",
      data: { seq: 2, delta: "step two" }
    });
    const duplicate = projectChatRunEvent(continued.cache, "rr-3", {
      event: "chat.reasoning.delta",
      data: { seq: 2, delta: "step two" }
    });
    const answer = projectChatRunEvent(duplicate.cache, "rr-3", {
      event: "chat.message.delta",
      data: { seq: 3, delta: "final" }
    });

    expect(answer.cache.messages).toEqual([
      expect.objectContaining({ kind: "reasoning", text: "step one step two" }),
      expect.objectContaining({ id: "assistant-rr-3", content: "final" })
    ]);
    expect(duplicate.applied).toBe(false);
  });

  it("attaches TTFT only to the first effective output event", () => {
    const initial: SessionCache = {
      messages: [{ id: "assistant-rr-ttft", role: "assistant", content: "", status: "streaming" }],
      files: [],
      runId: "rr-ttft",
      streaming: true,
      lastSeq: 0
    };
    const reasoning = projectChatRunEvent(initial, "rr-ttft", {
      event: "chat.reasoning.delta",
      data: { seq: 1, delta: "first thought" },
      ttftMs: 1234
    });
    const answer = projectChatRunEvent(reasoning.cache, "rr-ttft", {
      event: "chat.message.delta",
      data: { seq: 2, delta: "answer" }
    });

    expect(answer.cache.messages).toEqual([
      expect.objectContaining({ kind: "reasoning", text: "first thought", ttftMs: 1234 }),
      expect.objectContaining({ id: "assistant-rr-ttft", content: "answer" })
    ]);
    expect(answer.cache.messages[1]).not.toHaveProperty("ttftMs");
  });

  it("ignores blank reasoning and attaches TTFT to the first visible reasoning row", () => {
    const initial: SessionCache = {
      messages: [{ id: "assistant-rr-visible", role: "assistant", content: "", status: "streaming" }],
      files: [],
      runId: "rr-visible",
      streaming: true,
      lastSeq: 0
    };
    const blank = projectChatRunEvent(initial, "rr-visible", {
      event: "chat.reasoning.delta",
      data: { seq: 1, delta: "  " }
    });
    const visible = projectChatRunEvent(blank.cache, "rr-visible", {
      event: "chat.reasoning.delta",
      data: { seq: 2, delta: "first thought" },
      ttftMs: 610
    });
    const continued = projectChatRunEvent(visible.cache, "rr-visible", {
      event: "chat.reasoning.delta",
      data: { seq: 3, delta: " continued" }
    });

    expect(blank.cache.messages.filter((message) => message.kind === "reasoning")).toHaveLength(0);
    expect(continued.cache.messages).toEqual([
      expect.objectContaining({ kind: "reasoning", text: "first thought continued", ttftMs: 610 }),
      expect.objectContaining({ id: "assistant-rr-visible" })
    ]);
  });

  it("uses camelized toolName and preserves TTFT across tool updates", () => {
    const initial: SessionCache = { messages: [], files: [], runId: "rr-tool-name", streaming: true, lastSeq: 0 };
    const started = projectChatRunEvent(initial, "rr-tool-name", {
      event: "chat.tool.event",
      data: { seq: 1, callId: "call-1", toolName: "search_files", status: "running" },
      ttftMs: 720
    });
    const completed = projectChatRunEvent(started.cache, "rr-tool-name", {
      event: "chat.tool.event",
      data: { seq: 2, callId: "call-1", toolName: "search_files", status: "completed", result: "done" }
    });

    expect(completed.cache.messages).toContainEqual(
      expect.objectContaining({ kind: "tool", name: "search_files", ttftMs: 720, result: "done" })
    );
  });

  it("attaches TTFT to the first visible stable subagent event", () => {
    const projected = projectChatRunEvent(
      { messages: [], files: [], runId: "rr-background", streaming: true, lastSeq: 0 },
      "rr-background",
      { event: "chat.subagent.event", data: { seq: 1, type: "subagent.start", subagent_id: "sub-1", text: "delegating" }, ttftMs: 830 }
    );

    expect(projected.cache.messages).toContainEqual(
      expect.objectContaining({ kind: "background", ttftMs: 830 })
    );
  });

  it("ignores an unstable subagent event instead of creating a background task", () => {
    const projected = projectChatRunEvent(
      { messages: [], files: [], runId: "rr-unstable", streaming: true, lastSeq: 0 },
      "rr-unstable",
      { event: "chat.subagent.event", data: { seq: 1, type: "subagent.progress", tool_preview: "terminal" }, ttftMs: 830 }
    );

    expect(projected.cache.messages.filter((message) => message.kind === "background")).toHaveLength(0);
    expect(isFirstVisibleAgentEvent({ event: "chat.subagent.event", data: { type: "subagent.progress", tool_preview: "terminal" } })).toBe(false);
  });

  it("keeps TTFT on a tool row as the tool status changes", () => {
    const initial: SessionCache = { messages: [], files: [], runId: "rr-tool-ttft", streaming: true, lastSeq: 0 };
    const started = projectChatRunEvent(initial, "rr-tool-ttft", {
      event: "chat.tool.event",
      data: { seq: 1, callId: "search-1", name: "search", status: "running" },
      ttftMs: 840
    });
    const completed = projectChatRunEvent(started.cache, "rr-tool-ttft", {
      event: "chat.tool.event",
      data: { seq: 2, callId: "search-1", name: "search", status: "completed", result: "done" }
    });

    expect(completed.cache.messages.find((message) => message.kind === "tool")).toEqual(
      expect.objectContaining({ status: "completed", result: "done", ttftMs: 840 })
    );
  });

  it("preserves preview and TTFT when a stable tool completes", () => {
    const initial: SessionCache = { messages: [], files: [], runId: "rr-stable", streaming: true, lastSeq: 0 };
    const started = projectChatRunEvent(initial, "rr-stable", {
      event: "chat.tool.event",
      data: { seq: 1, callId: "search-1", hasStableCallId: true, name: "search_files", status: "running", preview: "querying" },
      ttftMs: 840
    });
    const completed = projectChatRunEvent(started.cache, "rr-stable", {
      event: "chat.tool.event",
      data: { seq: 2, callId: "search-1", hasStableCallId: true, name: "search_files", status: "completed", result: "done" }
    });

    expect(completed.cache.messages.filter((message) => message.kind === "tool")).toEqual([
      expect.objectContaining({
        callId: "search-1",
        providerCallId: "search-1",
        hasStableCallId: true,
        status: "completed",
        preview: "querying",
        result: "done",
        ttftMs: 840
      })
    ]);
  });

  it("correlates unstable lifecycle events without merging later same-name calls", () => {
    const initial: SessionCache = { messages: [], files: [], runId: "rr-unstable", streaming: true, lastSeq: 0 };
    const events = [
      { seq: 1, callId: "search:unstable", hasStableCallId: false, name: "search_files", status: "running", preview: "first" },
      { seq: 2, callId: "search:unstable", hasStableCallId: false, name: "search_files", status: "completed", result: "first done" },
      { seq: 3, callId: "search:unstable", hasStableCallId: false, name: "search_files", status: "running", preview: "second" },
      { seq: 4, callId: "search:unstable", hasStableCallId: false, name: "search_files", status: "failed", result: "second failed" }
    ];
    const completed = events.reduce((cache, data) => projectChatRunEvent(cache, "rr-unstable", {
      event: "chat.tool.event",
      data
    }).cache, initial);
    const tools = completed.messages.filter((message) => message.kind === "tool");

    expect(tools).toHaveLength(2);
    expect(tools[0]).toMatchObject({ status: "completed", preview: "first", result: "first done", providerCallId: "search:unstable" });
    expect(tools[1]).toMatchObject({ status: "failed", preview: "second", result: "second failed", providerCallId: "search:unstable" });
    expect(tools[0].callId).not.toBe(tools[1].callId);
  });

  it("does not downgrade a stable terminal tool on a late running event", () => {
    const initial: SessionCache = { messages: [], files: [], runId: "rr-late", streaming: true, lastSeq: 0 };
    const completed = projectChatRunEvent(initial, "rr-late", {
      event: "chat.tool.event",
      data: { seq: 1, callId: "call-late", hasStableCallId: true, name: "read_file", status: "completed", result: "done" }
    });
    const late = projectChatRunEvent(completed.cache, "rr-late", {
      event: "chat.tool.event",
      data: { seq: 2, callId: "call-late", hasStableCallId: true, name: "read_file", status: "running", preview: "late preview" }
    });

    expect(late.cache.messages.find((message) => message.kind === "tool")).toMatchObject({
      status: "completed",
      preview: "late preview",
      result: "done"
    });
  });

  it("starts a new reasoning segment after a tool boundary", () => {
    const initial: SessionCache = { messages: [], files: [], runId: "rr-4", streaming: true, lastSeq: 0 };
    const beforeTool = projectChatRunEvent(initial, "rr-4", {
      event: "chat.reasoning.delta",
      data: { seq: 1, delta: "plan" }
    });
    const tool = projectChatRunEvent(beforeTool.cache, "rr-4", {
      event: "chat.tool.event",
      data: { seq: 2, callId: "call-1", name: "search", status: "completed", preview: "docs" }
    });
    const afterTool = projectChatRunEvent(tool.cache, "rr-4", {
      event: "chat.reasoning.delta",
      data: { seq: 3, delta: "review result" }
    });

    expect(afterTool.cache.messages.map((message) => message.kind ?? "message")).toEqual([
      "reasoning",
      "tool",
      "reasoning",
      "message"
    ]);
    expect(afterTool.cache.messages.filter((message) => message.kind === "reasoning")).toEqual([
      expect.objectContaining({ text: "plan" }),
      expect.objectContaining({ text: "review result" })
    ]);
  });

  it("keeps the run active while a background delegation is parked and projects its progress", () => {
    const initial: SessionCache = {
      messages: [{ id: "assistant-rr-bg", role: "assistant", content: "", status: "streaming" }],
      files: [],
      runId: "rr-bg",
      streaming: true,
      lastSeq: 0
    };
    const parked = projectChatRunEvent(initial, "rr-bg", {
      event: "chat.turn.parked",
      data: { seq: 1, active_subagents: 1 }
    });
    const started = projectChatRunEvent(parked.cache, "rr-bg", {
      event: "chat.subagent.event",
      data: { seq: 2, type: "subagent.start", subagent_id: "sub-1", goal: "搜索新闻", status: "running" }
    });
    const progress = projectChatRunEvent(started.cache, "rr-bg", {
      event: "chat.subagent.event",
      data: { seq: 3, type: "subagent.progress", subagent_id: "sub-1", text: "正在检索网页", status: "running" }
    });
    const completed = projectChatRunEvent(progress.cache, "rr-bg", {
      event: "chat.subagent.event",
      data: { seq: 4, type: "subagent.complete", subagent_id: "sub-1", summary: "已找到新闻", status: "completed" }
    });

    expect(parked.cache.streaming).toBe(true);
    expect(parked.cache.runId).toBe("rr-bg");
    expect(progress.cache.messages).toContainEqual(expect.objectContaining({
      kind: "background",
      taskId: "sub-1",
      status: "running",
      goal: "搜索新闻",
      activity: "正在检索网页"
    }));
    expect(completed.cache.messages).toContainEqual(expect.objectContaining({
      kind: "background",
      taskId: "sub-1",
      status: "completed",
      summary: "已找到新闻"
    }));
  });

  it.each([
    ["subagent.failed", "failed"],
    ["subagent.error", "failed"],
    ["subagent.interrupted", "interrupted"],
    ["subagent.canceled", "interrupted"]
  ] as const)("maps %s to %s without an explicit status", (type, status) => {
    const initial: SessionCache = {
      messages: [],
      files: [],
      runId: "rr-bg-status",
      streaming: true,
      lastSeq: 0
    };
    const result = projectChatRunEvent(initial, "rr-bg-status", {
      event: "chat.subagent.event",
      data: { seq: 1, type, subagent_id: "sub-1" }
    });

    expect(result.cache.messages).toContainEqual(
      expect.objectContaining({ kind: "background", taskId: "sub-1", status })
    );
  });

  it.each([
    ["failed", "failed"],
    ["error", "failed"],
    ["timeout", "failed"],
    ["interrupted", "interrupted"],
    ["canceled", "interrupted"],
    ["cancelled", "interrupted"]
  ] as const)("maps subagent.complete status %s to %s", (rawStatus, status) => {
    const result = projectChatRunEvent({ messages: [], files: [] }, "rr-bg-status", {
      event: "chat.subagent.event",
      data: {
        seq: 1,
        type: "subagent.complete",
        subagent_id: "sub-1",
        status: rawStatus
      }
    });

    expect(result.cache.messages).toContainEqual(
      expect.objectContaining({ kind: "background", taskId: "sub-1", status })
    );
  });

  it("keeps the run active while the runtime transport reconnects and clears the notice after resume", () => {
    const initial: SessionCache = {
      messages: [{ id: "assistant-rr-reconnect", role: "assistant", content: "partial", status: "streaming" }],
      files: [],
      runId: "rr-reconnect",
      streaming: true,
      lastSeq: 0
    };
    const reconnecting = projectChatRunEvent(initial, "rr-reconnect", {
      event: "chat.transport.reconnecting",
      data: { seq: 1, attempt: 1 }
    });
    const resumed = projectChatRunEvent(reconnecting.cache, "rr-reconnect", {
      event: "chat.transport.resumed",
      data: { seq: 2, attempt: 1 }
    });

    expect(reconnecting.cache.streaming).toBe(true);
    expect(reconnecting.cache.messages).toContainEqual(expect.objectContaining({
      kind: "background",
      taskId: "transport",
      status: "running",
      activity: "连接中断，正在恢复任务…"
    }));
    expect(resumed.cache.messages.some((message) => message.kind === "background" && message.taskId === "transport")).toBe(false);
  });

  it("shows a recovery notice before Runtime returns the durable run id", () => {
    const initial: SessionCache = {
      messages: [
        { id: "user-web-message-uncertain", role: "user", content: "hello" },
        {
          id: "assistant-pending-web-message-uncertain",
          role: "assistant",
          content: "",
          status: "streaming",
          thinkingPhase: "submitting"
        }
      ],
      files: [],
      streaming: true,
      activeTurn: {
        turnId: "web-message-uncertain",
        userMessageId: "user-web-message-uncertain",
        assistantMessageId: "assistant-pending-web-message-uncertain",
        submittedText: "hello",
        status: "submitting"
      }
    };
    const reconnecting = projectChatRunEvent(initial, "web-message-uncertain", {
      event: "chat.transport.reconnecting",
      data: {
        runtimeRunId: "web-message-uncertain",
        clientMessageId: "web-message-uncertain"
      }
    });

    expect(reconnecting.cache.runId).toBeUndefined();
    expect(reconnecting.cache.streaming).toBe(true);
    expect(reconnecting.cache.messages).toContainEqual(expect.objectContaining({
      kind: "background",
      taskId: "transport",
      activity: "连接中断，正在恢复任务…"
    }));
  });

  it("shows post-turn self-evolution without re-opening a completed user turn", () => {
    const completed: SessionCache = {
      messages: [{ id: "assistant-rr-review", role: "assistant", content: "已完成", status: "completed" }],
      files: [],
      runId: null,
      streaming: false,
      lastSeq: 4
    };

    const running = projectChatRunEvent(completed, "rr-review", {
      event: "chat.self_evolution.event",
      data: { seq: 5, status: "running", activity: "已启用本次执行复盘，正在优化相关技能" }
    });
    const finished = projectChatRunEvent(running.cache, "rr-review", {
      event: "chat.self_evolution.event",
      data: { seq: 6, status: "completed", activity: "本次执行复盘已完成" }
    });

    expect(running.cache.runId).toBeNull();
    expect(running.cache.streaming).toBe(false);
    expect(running.cache.messages).toContainEqual(expect.objectContaining({
      kind: "background",
      taskId: "self-evolution",
      status: "running",
      activity: "已启用本次执行复盘，正在优化相关技能"
    }));
    expect(finished.cache.messages).toContainEqual(expect.objectContaining({
      kind: "background",
      taskId: "self-evolution",
      status: "completed",
      activity: "本次执行复盘已完成"
    }));
  });

  it("keeps agent progress out of the terminal answer until message completion", () => {
    const accepted = acceptChatRun(beginChatTurn({ messages: [], files: [] }, "生成 PPT", "turn-activity"), "rr-activity");
    const activity = projectChatRunEvent(accepted, "rr-activity", {
      event: "chat.activity.delta",
      data: { seq: 1, delta: "PPT 已生成，正在进行视觉 QA。" }
    });
    const terminal = projectChatRunEvent(activity.cache, "rr-activity", {
      event: "chat.message.completed",
      data: { seq: 2, text: "PPT 已生成，已附在右侧文件栏。", status: "complete" }
    });

    expect(activity.cache.messages).toContainEqual(expect.objectContaining({
      kind: "activity",
      text: "PPT 已生成，正在进行视觉 QA。"
    }));
    expect(activity.cache.messages.find((message) => message.id === "assistant-rr-activity")?.content).toBe("");
    expect(terminal.cache.messages.find((message) => message.id === "assistant-rr-activity")).toEqual(expect.objectContaining({
      content: "PPT 已生成，已附在右侧文件栏。",
      status: "completed"
    }));
  });

  it("removes only the exact terminal-answer suffix from the final activity segment", () => {
    const accepted = acceptChatRun(beginChatTurn({ messages: [], files: [] }, "生成 PPT", "turn-dedup"), "rr-dedup");
    const activity = projectChatRunEvent(accepted, "rr-dedup", {
      event: "chat.activity.delta",
      data: { seq: 1, delta: "正在检查文件。PPT 已完成。" }
    });
    const terminal = projectChatRunEvent(activity.cache, "rr-dedup", {
      event: "chat.message.completed",
      data: { seq: 2, text: "PPT 已完成。", status: "complete" }
    });

    expect(terminal.cache.messages).toContainEqual(expect.objectContaining({
      kind: "activity",
      text: "正在检查文件。"
    }));
    expect(terminal.cache.messages).toContainEqual(expect.objectContaining({
      id: "assistant-rr-dedup",
      content: "PPT 已完成。",
      status: "completed"
    }));
  });

  it("removes an activity segment when it is exactly the terminal answer", () => {
    const accepted = acceptChatRun(beginChatTurn({ messages: [], files: [] }, "你好", "turn-exact-dedup"), "rr-exact-dedup");
    const activity = projectChatRunEvent(accepted, "rr-exact-dedup", {
      event: "chat.activity.delta",
      data: { seq: 1, delta: "你好，有什么可以帮你？" }
    });
    const terminal = projectChatRunEvent(activity.cache, "rr-exact-dedup", {
      event: "chat.message.completed",
      data: { seq: 2, text: "你好，有什么可以帮你？", status: "complete" }
    });

    expect(terminal.cache.messages.some((message) => message.kind === "activity")).toBe(false);
    expect(terminal.cache.messages.find((message) => message.id === "assistant-rr-exact-dedup")).toEqual(expect.objectContaining({
      content: "你好，有什么可以帮你？",
      status: "completed"
    }));
  });

  it("removes persisted fallback reasoning and activity copies of the terminal answer", () => {
    const accepted = acceptChatRun(beginChatTurn({ messages: [], files: [] }, "你好", "turn-fallback-dedup"), "rr-fallback-dedup");
    const reasoning = projectChatRunEvent(accepted, "rr-fallback-dedup", {
      event: "chat.reasoning.delta",
      data: { seq: 1, delta: "internal reasoning" }
    });
    const activity = projectChatRunEvent(reasoning.cache, "rr-fallback-dedup", {
      event: "chat.activity.delta",
      data: { seq: 2, delta: "你好，有什么可以帮你？" }
    });
    const fallback = projectChatRunEvent(activity.cache, "rr-fallback-dedup", {
      event: "chat.reasoning.delta",
      data: { seq: 3, delta: "你好，有什么可以帮你？" }
    });
    const terminal = projectChatRunEvent(fallback.cache, "rr-fallback-dedup", {
      event: "chat.message.completed",
      data: { seq: 4, text: "你好，有什么可以帮你？", status: "complete" }
    });

    expect(terminal.cache.messages.filter((message) => message.kind === "activity")).toHaveLength(0);
    expect(terminal.cache.messages.filter((message) => message.kind === "reasoning")).toEqual([
      expect.objectContaining({ text: "internal reasoning" })
    ]);
    expect(terminal.cache.messages.find((message) => message.id === "assistant-rr-fallback-dedup")).toEqual(expect.objectContaining({
      content: "你好，有什么可以帮你？",
      status: "completed"
    }));
  });

  it("preserves a completed tool input when updating its stable call", () => {
    const accepted = acceptChatRun(beginChatTurn({ messages: [], files: [] }, "检查环境", "turn-tool-input"), "rr-tool-input");
    const started = projectChatRunEvent(accepted, "rr-tool-input", {
      event: "chat.tool.event",
      data: { seq: 1, name: "terminal", callId: "call-command", hasStableCallId: true, status: "running" }
    });
    const completed = projectChatRunEvent(started.cache, "rr-tool-input", {
      event: "chat.tool.event",
      data: { seq: 2, name: "terminal", callId: "call-command", hasStableCallId: true, status: "completed", input: '{"command":"node -v"}', result: '{"output":"v22"}' }
    });

    expect(completed.cache.messages).toContainEqual(expect.objectContaining({
      kind: "tool",
      callId: "call-command",
      input: '{"command":"node -v"}',
      result: '{"output":"v22"}'
    }));
  });

  it("projects corrections independently and starts a new assistant segment", () => {
    const parent = acceptChatRun(
      beginChatTurn({ messages: [], files: [] }, "原任务", "turn-parent"),
      "run-parent",
      "assistant-pending-turn-parent"
    );
    const first = beginChatCorrection(
      parent,
      "先查日志",
      "correction-1",
      "run-parent"
    );
    const withBoth = beginChatCorrection(
      first,
      "先查日志",
      "correction-2",
      "run-parent"
    );
    const accepted = projectChatRunEvent(withBoth, "run-parent", {
      event: "run.redirect_accepted",
      data: {
        clientMessageId: "correction-1",
        redirectId: "redirect-1",
        parentRunId: "run-parent",
        eventSeq: 1
      }
    });
    const duplicate = projectChatRunEvent(accepted.cache, "run-parent", {
      event: "chat.steer.accepted",
      data: {
        clientMessageId: "correction-1",
        redirectId: "redirect-1",
        parentRunId: "run-parent",
        seq: 1,
        eventSeq: 1
      }
    });
    const consumed = projectChatRunEvent(duplicate.cache, "run-parent", {
      event: "run.redirect_consumed",
      data: {
        clientMessageId: "correction-1",
        redirectId: "redirect-1",
        parentRunId: "run-parent",
        dispatchGroupId: "group-1",
        eventSeq: 2
      }
    });

    expect(duplicate.applied).toBe(false);
    expect(consumed.cache.messages.filter((message) => (
      message.role === "user" && message.content === "先查日志"
    ))).toHaveLength(2);
    expect(consumed.cache.messages.find((message) => (
      message.role === "user"
      && "clientMessageId" in message
      && message.clientMessageId === "correction-1"
    ))).toMatchObject({
      correctionStatus: "applied",
      submissionHint: "已应用"
    });
    expect(consumed.cache.messages.find((message) => (
      message.role === "user"
      && "clientMessageId" in message
      && message.clientMessageId === "correction-2"
    ))).toMatchObject({ correctionStatus: "sending" });
    expect(consumed.cache.messages.filter((message) => (
      message.role === "assistant"
    ))).toHaveLength(2);
    expect(consumed.cache.messages[consumed.cache.messages.length - 1]).toMatchObject({
      id: "assistant-run-parent",
      role: "assistant",
      content: ""
    });
    expect(consumed.cache.runId).toBe("run-parent");
    expect(consumed.cache.streaming).toBe(true);

    const secondConsumed = projectChatRunEvent(consumed.cache, "run-parent", {
      event: "run.redirect_consumed",
      data: {
        clientMessageId: "correction-2",
        redirectId: "redirect-2",
        parentRunId: "run-parent",
        dispatchGroupId: "group-1",
        eventSeq: 3
      }
    });
    expect(secondConsumed.cache.messages.filter((message) => (
      message.role === "assistant"
    ))).toHaveLength(2);
    expect(secondConsumed.cache.responseSegmentGroupIds).toEqual(["group-1"]);
  });

  it("does not let an accepted correction regress after it is consumed", () => {
    const initial = beginChatCorrection(
      { messages: [], files: [], runId: "run-parent", streaming: true },
      "修正",
      "correction-order",
      "run-parent"
    );
    const accepted = projectChatRunEvent(initial, "run-parent", {
      event: "run.redirect_accepted",
      data: { clientMessageId: "correction-order", eventSeq: 4 }
    });
    const consumed = projectChatRunEvent(accepted.cache, "run-parent", {
      event: "run.redirect_consumed",
      data: { clientMessageId: "correction-order", eventSeq: 5 }
    });
    const lateAccepted = projectChatRunEvent(consumed.cache, "run-parent", {
      event: "run.redirect_accepted",
      data: { clientMessageId: "correction-order", eventSeq: 4 }
    });

    expect(lateAccepted.applied).toBe(false);
    expect(lateAccepted.cache.messages[0]).toMatchObject({
      correctionStatus: "applied",
      submissionHint: "已应用"
    });
  });

  it("renders pending, uncertain, and canceled as terminal ledger-specific states", () => {
    const initial = beginChatCorrection(
      beginChatCorrection(
        { messages: [], files: [], runId: "run-parent", streaming: true },
        "状态未知",
        "correction-uncertain",
        "run-parent"
      ),
      "随父任务停止",
      "correction-canceled",
      "run-parent"
    );
    const pending = projectChatRunEvent(initial, "run-parent", {
      event: "run.redirect_pending",
      data: { clientMessageId: "correction-uncertain", eventSeq: 1 }
    });
    const uncertain = projectChatRunEvent(pending.cache, "run-parent", {
      event: "run.steer_submission_uncertain",
      data: { clientMessageId: "correction-uncertain", eventSeq: 2 }
    });
    const canceled = projectChatRunEvent(uncertain.cache, "run-parent", {
      event: "run.redirect_canceled",
      data: { clientMessageId: "correction-canceled", eventSeq: 3 }
    });

    expect(pending.cache.messages[0]).toMatchObject({
      correctionStatus: "received",
      submissionHint: "已接收"
    });
    expect(canceled.cache.messages[0]).toMatchObject({
      correctionStatus: "failed",
      submissionHint: "处理异常"
    });
    expect(canceled.cache.messages[1]).toMatchObject({
      correctionStatus: "failed",
      submissionHint: "处理异常"
    });
  });

  it("creates a promoted assistant only at the exact run started boundary", () => {
    const parent = beginChatCorrection(
      { messages: [], files: [], runId: "run-parent", streaming: true },
      "下一任务",
      "correction-promoted",
      "run-parent"
    );
    const promoted = projectChatRunEvent(parent, "run-parent", {
      event: "run.redirect_promoted",
      data: {
        clientMessageId: "correction-promoted",
        parentRunId: "run-parent",
        promotedRunId: "run-promoted",
        eventSeq: 9
      }
    });
    expect(promoted.cache.messages.filter((message) => message.role === "assistant"))
      .toHaveLength(0);

    const started = beginPromotedCorrectionRun(
      promoted.cache,
      "correction-promoted",
      "run-promoted"
    );
    expect(started.messages).toContainEqual(expect.objectContaining({
      id: "assistant-run-promoted",
      role: "assistant",
      thinkingPhase: "submitting"
    }));
    expect(started.activeTurn).toMatchObject({
      userMessageId: "correction-correction-promoted",
      runtimeRunId: "run-promoted"
    });
    expect(started.messages.find((message) => message.id === "correction-correction-promoted"))
      .toMatchObject({ status: undefined, submissionHint: undefined });
  });

  it("keeps the promoted run active when the parent done arrives later", () => {
    const parent: SessionCache = {
      messages: [
        {
          id: "assistant-run-parent",
          role: "assistant",
          content: "A 的回答",
          status: "streaming"
        }
      ],
      files: [],
      runId: "run-parent",
      streaming: true
    };
    const correction = beginChatCorrection(
      parent,
      "消息 B",
      "correction-promoted",
      "run-parent"
    );
    const disposition = projectChatRunEvent(correction, "run-parent", {
      event: "run.redirect_promoted",
      data: {
        clientMessageId: "correction-promoted",
        parentRunId: "run-parent",
        promotedRunId: "run-promoted",
        eventSeq: 9
      }
    });
    const promoted = beginPromotedCorrectionRun(
      disposition.cache,
      "correction-promoted",
      "run-promoted"
    );

    const parentDone = projectChatRunEvent(promoted, "run-parent", {
      event: "chat.done",
      data: { status: "completed", seq: 10 }
    });

    expect(parentDone.cache).toMatchObject({
      runId: "run-promoted",
      streaming: true,
      activeTurn: { runtimeRunId: "run-promoted", status: "streaming" }
    });
    expect(parentDone.cache.messages).toContainEqual(expect.objectContaining({
      id: "assistant-run-parent",
      status: "completed"
    }));
    expect(parentDone.cache.messages).toContainEqual(expect.objectContaining({
      id: "assistant-run-promoted",
      status: "streaming"
    }));
  });

  it("keeps an attachment submission queued without replacing the parent lifecycle", () => {
    const parent: SessionCache = {
      messages: [{ id: "assistant-run-parent", role: "assistant", content: "", status: "streaming" }],
      files: [],
      runId: "run-parent",
      streaming: true,
      lastSeq: 8
    };
    const queued = beginQueuedChatSubmission(
      parent,
      "处理附件",
      "attachment-message"
    );

    expect(queued.runId).toBe("run-parent");
    expect(queued.streaming).toBe(true);
    expect(queued.lastSeq).toBe(8);
    expect(queued.messages).toContainEqual(expect.objectContaining({
      id: "queued-user-attachment-message",
      role: "user",
      submissionHint: "将在当前任务完成后执行"
    }));
    expect(queued.messages.filter((message) => message.role === "assistant"))
      .toHaveLength(1);
  });
});
