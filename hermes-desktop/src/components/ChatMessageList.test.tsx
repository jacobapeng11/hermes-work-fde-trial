import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatMessageList } from "./ChatMessageList";

describe("ChatMessageList", () => {
  it("renders a safe capability card independently from the assistant reply", () => {
    render(<ChatMessageList streaming={false} messages={[
      {
        id: "capability-caperr-12345678",
        kind: "capability-error",
        role: "system",
        capabilityType: "knowledge_base",
        errorCode: "KNOWLEDGE_BASE_NO_PERMISSION",
        displayMessage: "资产访问权限不足",
        recoverable: true,
        errorId: "caperr-12345678"
      },
      { id: "assistant-run-1", role: "assistant", content: "仍然可以继续回答。" }
    ]}/>);

    expect(screen.getByText("资产访问权限不足")).toBeVisible();
    expect(screen.getByText(/专家将继续使用其他能力回答/)).toBeVisible();
    expect(screen.getByText("仍然可以继续回答。")).toBeVisible();
  });
  afterEach(() => {
    vi.useRealTimers();
    Reflect.deleteProperty(navigator, "clipboard");
  });

  it("copies raw content from user and assistant messages", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<ChatMessageList streaming={false} messages={[
      { id: "u-copy", role: "user", content: "# 用户原始内容" },
      { id: "a-copy", role: "assistant", content: "**助手原始内容**" }
    ]}/>);

    const copyButtons = screen.getAllByRole("button", { name: "复制消息" });
    expect(copyButtons).toHaveLength(2);
    await user.click(copyButtons[0]);
    expect(writeText).toHaveBeenLastCalledWith("# 用户原始内容");
    expect(screen.getByRole("button", { name: "已复制" })).toBeVisible();

    await user.click(copyButtons[1]);
    expect(writeText).toHaveBeenLastCalledWith("**助手原始内容**");
    expect(screen.getAllByRole("button", { name: "已复制" })).toHaveLength(2);
  });

  it("keeps the copy control available briefly after the pointer leaves a message", () => {
    vi.useFakeTimers();
    const { container } = render(<ChatMessageList streaming={false} messages={[
      { id: "copy-hover-delay", role: "assistant", content: "离开后仍可复制" }
    ]}/>);
    const message = container.querySelector('[data-message-id="copy-hover-delay"]');
    expect(message).not.toBeNull();

    fireEvent.mouseEnter(message as HTMLElement);
    expect(message).toHaveAttribute("data-copy-visible", "true");
    fireEvent.mouseLeave(message as HTMLElement);
    expect(message).toHaveAttribute("data-copy-visible", "true");

    act(() => { vi.advanceTimersByTime(649); });
    expect(message).toHaveAttribute("data-copy-visible", "true");
    act(() => { vi.advanceTimersByTime(1); });
    expect(message).not.toHaveAttribute("data-copy-visible");
  });

  it("keeps content visible and reports failure when clipboard rejects", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<ChatMessageList streaming={false} messages={[
      { id: "a-copy-failed", role: "assistant", content: "仍然可见", error: "错误说明不参与复制" }
    ]}/>);

    await user.click(screen.getByRole("button", { name: "复制消息" }));

    expect(writeText).toHaveBeenCalledWith("仍然可见");
    expect(screen.getByText("仍然可见")).toBeVisible();
    expect(screen.getByText("错误说明不参与复制")).toBeVisible();
    expect(screen.getByRole("button", { name: "复制失败" })).toBeVisible();
  });

  it("reports failure without throwing when the Clipboard API is unavailable", async () => {
    const user = userEvent.setup();
    Reflect.deleteProperty(navigator, "clipboard");
    render(<ChatMessageList streaming={false} messages={[
      { id: "u-copy-unavailable", role: "user", content: "正文保留" }
    ]}/>);

    await user.click(screen.getByRole("button", { name: "复制消息" }));

    expect(screen.getByText("正文保留")).toBeVisible();
    expect(screen.getByRole("button", { name: "复制失败" })).toBeVisible();
  });

  it("renders content-only compact bubbles while keeping assistant TTFT", () => {
    const { container } = render(<ChatMessageList streaming={false} messages={[
      { id: "u-compact", role: "user", content: "用户正文" },
      { id: "a-compact", role: "assistant", content: "助手正文", ttftMs: 1234 }
    ]}/>);

    expect(screen.getByText("用户正文")).toBeVisible();
    expect(screen.getByText("助手正文")).toBeVisible();
    expect(screen.queryByText("你")).not.toBeInTheDocument();
    expect(screen.queryByText("示例业务数字人")).not.toBeInTheDocument();
    const userBubble = container.querySelector('.message[data-role="user"]');
    expect(userBubble?.parentElement).toBe(container);
    const assistant = container.querySelector('.message[data-role="assistant"]');
    expect(assistant).toHaveAttribute("data-has-ttft", "true");
    expect(assistant?.querySelector(":scope > .ttftBadge")).toHaveTextContent("TTFT 1.2s");
    expect(assistant?.querySelector(":scope > .ttftBadge")).toHaveAttribute(
      "title",
      "从发送请求到首次可见 Agent 内容"
    );
  });

  it("shows TTFT on the first visible background activity with the approved tooltip", () => {
    render(<ChatMessageList streaming messages={[{
      id: "background-rr-1-sub-1",
      kind: "background",
      role: "system",
      taskId: "sub-1",
      status: "running",
      activity: "正在委派任务",
      ttftMs: 910
    }]}/>);

    expect(screen.getByText("TTFT 0.9s")).toHaveAttribute(
      "title",
      "从发送请求到首次可见 Agent 内容"
    );
  });

  it("renders the same thinking bubble for the started phase", () => {
    render(<ChatMessageList streaming messages={[{
      id: "assistant-rr-1",
      role: "assistant",
      content: "",
      status: "streaming",
      thinkingPhase: "started",
      ttftMs: 240
    }]}/>);

    expect(screen.getByRole("status")).toHaveTextContent("正在思考...");
    expect(screen.getByRole("status")).toHaveAttribute("data-thinking-phase", "started");
    expect(screen.queryByText("TTFT 0.2s")).not.toBeInTheDocument();
  });

  it("keeps the thinking placeholder stationary while its spinner remains active", () => {
    render(<ChatMessageList streaming messages={[{
      id: "assistant-thinking-stationary",
      role: "assistant",
      content: "",
      status: "streaming",
      thinkingPhase: "submitting"
    }]}/>);

    const placeholder = screen.getByRole("status");
    expect(placeholder).toHaveClass("thinkingPlaceholder");
    expect(placeholder.querySelector(".reasoningSpinner")).toBeInTheDocument();
  });

  it("keeps the thinking placeholder root when the first answer delta arrives", () => {
    const { container, rerender } = render(<ChatMessageList streaming messages={[{
      id: "assistant-stable-lane",
      role: "assistant",
      content: "",
      status: "streaming",
      thinkingPhase: "started"
    }]}/>);
    const placeholder = container.querySelector('[data-message-id="assistant-stable-lane"]');

    rerender(<ChatMessageList streaming messages={[{
      id: "assistant-stable-lane",
      role: "assistant",
      content: "第一个流式片段",
      status: "streaming"
    }]}/>);

    expect(container.querySelector('[data-message-id="assistant-stable-lane"]')).toBe(placeholder);
    expect(placeholder).not.toHaveAttribute("data-thinking-phase");
    expect(screen.getByText("第一个流式片段")).toBeVisible();
  });

  it("renders an actionable scheduled source badge with the task id", async () => {
    const onOpenScheduledTask = vi.fn();
    const user = userEvent.setup();
    const { container } = render(<ChatMessageList streaming={false} onOpenScheduledTask={onOpenScheduledTask} messages={[
      {
        id: "u-scheduled",
        role: "user",
        content: "每日总结",
        scheduledSource: {
          type: "scheduled",
          scheduledTaskId: 123,
          scheduledTaskName: "每日工作总结",
          triggerKind: "scheduled",
          taskAvailable: true
        }
      }
    ]}/>);

    const badge = screen.getByRole("button", { name: "定时任务 · 每日工作总结" });
    const scheduledUserBubble = container.querySelector('.message[data-role="user"]');
    expect(scheduledUserBubble?.parentElement).toBe(container);
    expect(badge).toHaveAttribute("data-scheduled-task-id", "123");
    expect(badge).toHaveTextContent("定时执行");
    expect(badge).toHaveTextContent("每日工作总结");
    await user.click(badge);
    expect(onOpenScheduledTask).toHaveBeenCalledWith(123);
  });

  it("renders a deleted scheduled task source as static text", () => {
    render(<ChatMessageList streaming={false} messages={[
      {
        id: "u-deleted-scheduled",
        role: "user",
        content: "旧任务",
        scheduledSource: {
          type: "scheduled",
          scheduledTaskId: 8,
          scheduledTaskName: "已删除任务",
          triggerKind: "scheduled",
          taskAvailable: false
        }
      }
    ]}/>);

    expect(screen.getByText("已删除任务").closest(".scheduledSourceBadge")).toHaveAttribute("data-scheduled-task-id", "8");
    expect(screen.getByText("定时执行")).toBeVisible();
    expect(screen.queryByRole("button", { name: "定时任务 · 已删除任务" })).not.toBeInTheDocument();
  });

  it("renders assistant markdown while keeping user and error text literal", () => {
    const { container } = render(<ChatMessageList streaming={false} messages={[
      { id: "u-md", role: "user", content: "# 用户原文" },
      { id: "a-md", role: "assistant", content: "# 助手标题\n\n- 条目", error: "# 错误原文" }
    ]}/>);

    expect(screen.getByText("# 用户原文")).toHaveClass("messagePlainText");
    expect(screen.getByRole("heading", { level: 1, name: "助手标题" })).toBeVisible();
    expect(screen.getByText("条目").closest("li")).toBeVisible();
    expect(screen.getByText("# 错误原文")).toHaveClass("messageError");
    expect(container.querySelector('.message[data-role="user"] h1')).not.toBeInTheDocument();
    expect(container.querySelector('.messageError h1')).not.toBeInTheDocument();
  });

  it("automatically expands live reasoning and still allows manual toggling", async () => {
    render(<ChatMessageList streaming messages={[
      { id: "r-1", kind: "reasoning", role: "assistant", text: "step by step" },
      { id: "a-1", role: "assistant", content: "", status: "streaming" }
    ]}/>);

    expect(screen.getByText("思考中…")).toBeVisible();
    expect(screen.getByText("step by step")).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: /思考中/ }));
    expect(screen.queryByText("step by step")).not.toBeInTheDocument();
  });

  it("keeps the same assistant message root without duplicate streaming indicators", () => {
    const { container, rerender } = render(<ChatMessageList streaming messages={[
      { id: "a-streaming", role: "assistant", content: "现在编写生成脚本。", status: "streaming" }
    ]}/>);

    const streamingMessage = container.querySelector('[data-message-id="a-streaming"]');
    expect(streamingMessage?.parentElement).toBe(container);
    expect(container.querySelector(".agentActivityAvatar")).not.toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "数字人正在生成" })).not.toBeInTheDocument();
    expect(container.querySelector('[data-streaming-markdown="true"]')).toBeInTheDocument();

    rerender(<ChatMessageList streaming={false} messages={[
      { id: "a-streaming", role: "assistant", content: "现在编写生成脚本。", status: "completed" }
    ]}/>);

    expect(container.querySelector('[data-message-id="a-streaming"]')).toBe(streamingMessage);
    expect(container.querySelector('[data-streaming-markdown="true"]')).not.toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "数字人正在生成" })).not.toBeInTheDocument();
  });

  it("keeps the execution spinner while answer-like text streams before completion", () => {
    const { container } = render(<ChatMessageList streaming messages={[
      { id: "r-finished", kind: "reasoning", role: "assistant", text: "先规划" },
      { id: "a-streaming", role: "assistant", content: "正在编写脚本。", status: "streaming" }
    ]}/>);

    const trace = container.querySelector('[data-kind="execution-trace"]');
    expect(trace?.querySelector(".reasoningSpinner")).toBeInTheDocument();
    expect(container.querySelector(".agentActivityAvatar")).not.toBeInTheDocument();
    expect(container.querySelector(".assistantStreamingIndicator")).not.toBeInTheDocument();
  });

  it("animates only the current reasoning status in a long running task", () => {
    const { container } = render(<ChatMessageList streaming messages={[
      { id: "r-completed", kind: "reasoning", role: "assistant", text: "完成的思考" },
      { id: "r-current", kind: "reasoning", role: "assistant", text: "当前思考" },
      { id: "a-long-task", role: "assistant", content: "", status: "streaming" }
    ]}/>);

    const traces = container.querySelectorAll('[data-kind="execution-trace"]');
    expect(traces).toHaveLength(1);
    expect(container.querySelector(".agentActivityAvatar")).not.toBeInTheDocument();
    expect(traces[0].querySelector(".reasoningSpinner")).toHaveAttribute("width", "14");
    expect(traces[0]).toHaveTextContent("完成的思考");
    expect(traces[0]).toHaveTextContent("当前思考");
  });

  it("shows TTFT beside the reasoning state", () => {
    const { rerender } = render(<ChatMessageList streaming messages={[
      { id: "r-ttft", kind: "reasoning", role: "assistant", text: "step", ttftMs: 1234 },
      { id: "a-ttft", role: "assistant", content: "", status: "streaming" }
    ]}/>);

    expect(screen.getByText("TTFT 1.2s")).toBeVisible();
    rerender(<ChatMessageList streaming={false} messages={[
      { id: "r-ttft", kind: "reasoning", role: "assistant", text: "step", ttftMs: 1234 },
      { id: "a-ttft", role: "assistant", content: "answer" }
    ]}/>);
    const title = screen.getByText("思考过程");
    const traceHeader = title.closest(".executionTraceHeader");
    const ttft = screen.getByText("TTFT 1.2s");

    expect(title).toBeVisible();
    expect(ttft).toBeVisible();
    expect(traceHeader).toBeInTheDocument();
    expect(traceHeader).toContainElement(title);
    expect(traceHeader?.querySelector(".executionTraceChevron")).toBeInTheDocument();
    expect(traceHeader).toContainElement(ttft);
  });

  it("groups consecutive tools with semantic user-facing states", () => {
    const { container } = render(<ChatMessageList streaming={false} messages={[
      { id: "t-running", kind: "tool", role: "tool", callId: "c-running", name: "search", status: "running", preview: "querying" },
      { id: "t-completed", kind: "tool", role: "tool", callId: "c-completed", name: "fetch", status: "completed", result: "done" },
      { id: "t-failed", kind: "tool", role: "tool", callId: "c-failed", name: "write", status: "failed", preview: "failed detail" }
    ]}/>);

    expect(container.querySelectorAll('[data-kind="execution-trace"]')).toHaveLength(1);
    const rows = container.querySelectorAll(".toolTraceStep");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveAttribute("data-status", "running");
    expect(rows[0]).toHaveTextContent("正在执行操作…");
    expect(rows[1]).toHaveAttribute("data-status", "completed");
    expect(rows[1]).toHaveTextContent("操作已完成");
    expect(rows[2]).toHaveAttribute("data-status", "failed");
    expect(rows[2]).toHaveTextContent("操作失败");
    expect(screen.getByText("1 个操作未完成")).toBeVisible();
  });

  it("summarizes a web search with its sanitized query", async () => {
    const user = userEvent.setup();
    render(<ChatMessageList streaming={false} messages={[
      {
        id: "t-weather",
        kind: "tool",
        role: "tool",
        callId: "c-weather",
        name: "web_search",
        status: "completed",
        input: '{"query":"深圳今天天气"}'
      }
    ]}/>);

    await user.click(screen.getByRole("button", { name: /思考过程/ }));
    expect(screen.getByText("调用搜索网页，查询“深圳今天天气”")).toBeVisible();
  });

  it("renders one live execution trace when reasoning continues after tool calls", () => {
    const { container } = render(<ChatMessageList streaming messages={[
      { id: "r-before-tool", kind: "reasoning", role: "assistant", text: "先查哈尔滨" },
      { id: "tool-weather", kind: "tool", role: "tool", callId: "weather", name: "web_search", status: "completed", result: "哈尔滨天气" },
      { id: "r-after-tool", kind: "reasoning", role: "assistant", text: "哈尔滨查到了，接下来查北京" },
      { id: "assistant-weather", role: "assistant", content: "", status: "streaming" }
    ]}/>);

    expect(container.querySelectorAll('[data-kind="execution-trace"]')).toHaveLength(1);
    expect(screen.getByText("思考中…")).toBeVisible();
    expect(screen.getByText("先查哈尔滨")).toBeVisible();
    expect(screen.getByText("哈尔滨查到了，接下来查北京")).toBeVisible();
    expect(screen.queryByText("已思考")).not.toBeInTheDocument();
    expect(container.querySelectorAll(".toolActivityGroup")).toHaveLength(0);
    expect(container.querySelector(".executionTimelineNode"))
      .toHaveAttribute("data-message-id", "execution-timeline-r-before-tool");
  });

  it("keeps collaboration and recoverable capability errors inside the same Run timeline", () => {
    const { container } = render(<ChatMessageList streaming messages={[
      { id: "r-run", kind: "reasoning", role: "assistant", runtimeRunId: "rr-run", text: "先分析数据" },
      { id: "bg-run", kind: "background", role: "system", runtimeRunId: "rr-run", taskId: "sub-1", status: "running", activity: "正在委派校验" },
      { id: "cap-run", kind: "capability-error", role: "system", runtimeRunId: "rr-run", capabilityType: "skill", errorCode: "UNAVAILABLE", displayMessage: "校验 Skill 暂不可用", recoverable: true, errorId: "cap-run" },
      { id: "tool-run", kind: "tool", role: "tool", runtimeRunId: "rr-run", callId: "read", name: "read_file", status: "completed" },
      { id: "a-run", role: "assistant", runtimeRunId: "rr-run", content: "", status: "streaming" }
    ]}/>);

    expect(container.querySelectorAll(".executionTimelineNode")).toHaveLength(1);
    expect(screen.getByText("正在协作 1 个任务")).toBeVisible();
    expect(screen.getByText("校验 Skill 暂不可用")).toBeVisible();
    expect(container.querySelectorAll('[data-kind="execution-trace"]')).toHaveLength(1);
  });

  it("keeps a live trace open while answer-like commentary streams before the run completes", () => {
    const { rerender } = render(<ChatMessageList streaming messages={[
      { id: "r-1", kind: "reasoning", role: "assistant", text: "step" },
      { id: "a-1", role: "assistant", content: "", status: "streaming" }
    ]}/>);

    expect(screen.getByText("step")).toBeVisible();
    rerender(<ChatMessageList streaming messages={[
      { id: "r-1", kind: "reasoning", role: "assistant", text: "step" },
      { id: "a-1", role: "assistant", content: "answer", status: "streaming" }
    ]}/>);

    expect(screen.getByText("思考中…")).toBeVisible();
    expect(screen.getByText("step")).toBeVisible();
    expect(screen.getByText("answer")).toBeVisible();

    rerender(<ChatMessageList streaming={false} messages={[
      { id: "r-1", kind: "reasoning", role: "assistant", text: "step" },
      { id: "a-1", role: "assistant", content: "answer", status: "completed" }
    ]}/>);
    expect(screen.getByText("思考过程")).toBeVisible();
    expect(screen.queryByText("step")).not.toBeInTheDocument();
  });

  it("shows the thinking bubble before the first stream event", () => {
    render(<ChatMessageList streaming messages={[
      {
        id: "a-1",
        role: "assistant",
        content: "",
        status: "streaming",
        thinkingPhase: "submitting"
      }
    ]}/>);

    expect(screen.getByRole("status")).toHaveTextContent("正在思考...");
    expect(screen.getByRole("status")).toHaveAttribute("data-thinking-phase", "submitting");
    expect(document.querySelectorAll(".reasoningSpinner")).toHaveLength(1);
    expect(document.querySelector(".agentActivityAvatar")).not.toBeInTheDocument();
  });

  it("replaces the initial thinking bubble when reasoning starts", () => {
    const { rerender } = render(<ChatMessageList streaming messages={[
      {
        id: "a-1",
        role: "assistant",
        content: "",
        status: "streaming",
        thinkingPhase: "submitting"
      }
    ]}/>);

    expect(screen.getByRole("status")).toHaveTextContent("正在思考...");
    rerender(<ChatMessageList streaming messages={[
      { id: "r-1", kind: "reasoning", role: "assistant", text: "first thought" },
      { id: "a-1", role: "assistant", content: "", status: "streaming" }
    ]}/>);

    expect(screen.queryByText("正在思考...")).not.toBeInTheDocument();
    expect(screen.getByText("思考中…")).toBeVisible();
  });

  it("replaces the active reasoning spinner with a compact completed state", () => {
    const { rerender } = render(<ChatMessageList streaming messages={[
      { id: "r-1", kind: "reasoning", role: "assistant", text: "step" },
      { id: "a-1", role: "assistant", content: "", status: "streaming" }
    ]}/>);

    expect(document.querySelector(".reasoningSpinner")).toBeInTheDocument();
    rerender(<ChatMessageList streaming={false} messages={[
      { id: "r-1", kind: "reasoning", role: "assistant", text: "step" },
      { id: "a-1", role: "assistant", content: "answer", status: "completed" }
    ]}/>);

    expect(document.querySelector(".reasoningSpinner")).not.toBeInTheDocument();
    expect(screen.getByText("思考过程")).toBeVisible();
  });

  it("shows a parked background task instead of the foreground typing animation", () => {
    render(<ChatMessageList streaming messages={[
      { id: "bg-1", kind: "background", role: "system", taskId: "sub-1", status: "running", goal: "搜索最新新闻", activity: "正在检索网页" },
      { id: "a-1", role: "assistant", content: "", status: "streaming" }
    ]}/>);

    expect(screen.getByText("正在协作 1 个任务")).toBeVisible();
    expect(screen.getByText("正在检索网页")).toBeVisible();
    expect(screen.queryByLabelText("数字人正在生成")).not.toBeInTheDocument();
  });

  it("keeps completed activity in place and collapses each trace without reparenting", async () => {
    const user = userEvent.setup();
    render(<ChatMessageList streaming={false} messages={[
      { id: "u-1", role: "user", content: "生成 PPT" },
      { id: "activity-1", kind: "activity", role: "assistant", text: "正在进行视觉 QA。" },
      { id: "reasoning-1", kind: "reasoning", role: "assistant", text: "检查文件结构" },
      { id: "a-1", role: "assistant", content: "PPT 已生成。", status: "completed" }
    ]}/>);

    expect(screen.getByText("PPT 已生成。")).toBeVisible();
    const trace = screen.getByRole("button", { name: /思考过程/ });
    expect(trace).not.toHaveTextContent("深度思考");
    expect(trace.compareDocumentPosition(screen.getByText("PPT 已生成。")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText("正在进行视觉 QA。")).not.toBeInTheDocument();
    expect(screen.queryByText("检查文件结构")).not.toBeInTheDocument();
    expect(screen.getByText("思考过程")).toBeVisible();
    await user.click(trace);
    expect(screen.getByText("正在进行视觉 QA。")).toBeVisible();
    expect(screen.getByText("检查文件结构")).toBeVisible();
    await user.click(trace);
    expect(screen.queryByText("正在进行视觉 QA。")).not.toBeInTheDocument();
  });
});
