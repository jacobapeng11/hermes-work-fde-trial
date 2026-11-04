import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeSessionSummary, ScheduledTask } from "@hermes-cloud/schemas";
import { ScheduledTaskDialog } from "./ScheduledTaskDialog";

const activeSession: RuntimeSessionSummary = {
  sessionId: 1,
  runtimeWorkspaceId: 10,
  runtimeSessionId: "session-active",
  title: "项目讨论",
  sourceType: "hermes",
  messageCount: 4,
  status: "active"
};

const deletedSession: RuntimeSessionSummary = { ...activeSession, runtimeSessionId: "session-deleted", title: "已删除会话", status: "deleted" };

const unconfirmedWeComSession: RuntimeSessionSummary = {
  ...activeSession,
  runtimeSessionId: "session-wecom-unconfirmed",
  title: "未确认企微会话",
  sourceType: "wecom",
  scheduledTaskEligible: false,
};

const confirmedWeComSession: RuntimeSessionSummary = {
  ...unconfirmedWeComSession,
  runtimeSessionId: "session-wecom-confirmed",
  title: "已确认企微会话",
  scheduledTaskEligible: true,
};

const expertSession: RuntimeSessionSummary = {
  ...activeSession,
  runtimeSessionId: "session-expert",
  title: "选题规划",
  agentProfileKind: "expert",
  agentDisplayName: "写作专家"
};

// antd Modal appear 阶段内容 opacity:0，统一等弹窗可见后再继续交互/断言。
async function renderDialog(ui: React.ReactElement) {
  render(ui);
  await waitFor(() => expect(screen.getByRole("dialog")).toBeVisible());
}

// antd Select 下拉用 rc-motion zoom 动画，初始父容器 opacity:0；等下拉可见后再断言。
async function openTargetPicker() {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "输出对话" }));
  await waitFor(() => expect(document.querySelector(".ant-select-dropdown")).not.toBeNull());
  await waitFor(() => {
    const dd = document.querySelector(".ant-select-dropdown") as HTMLElement;
    expect(getComputedStyle(dd).opacity).not.toBe("0");
  });
}
async function selectTarget(optionTitle: string) {
  await openTargetPicker();
  fireEvent.click(screen.getByTitle(optionTitle));
}
function optionByTitle(title: string) {
  // 限定下拉容器，避免与 Select 已选值区（同 title 的 selection-item）撞名。
  const dd = document.querySelector(".ant-select-dropdown") as HTMLElement;
  return dd.querySelector(`.ant-select-item-option[title="${title}"]`);
}

const completedTask: ScheduledTask = {
  taskId: 7,
  taskName: "一次性复盘",
  prompt: "生成项目复盘",
  targetRuntimeSessionId: activeSession.runtimeSessionId,
  scheduleConfig: { type: "once", runAt: "2026-07-18T10:00:00.000Z" },
  scheduleSummary: "一次",
  status: "completed",
  runCount: 1,
  isRunning: false
};

describe("ScheduledTaskDialog", () => {
  beforeEach(() => {
    // shouldAdvanceTime：fake 时钟下仍让动画/异步定时器自动推进，
    // 否则 antd Modal appear（rc-motion）永不完成，renderDialog 等不到可见。
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-07-22T00:00:00+08:00"));
  });
  afterEach(() => vi.useRealTimers());

  it("requires an active target session, a name, a prompt and a future once time", async () => {
    const onSubmit = vi.fn();
    await renderDialog(<ScheduledTaskDialog sessions={[deletedSession]} onClose={vi.fn()} onSubmit={onSubmit}/>);

    fireEvent.change(screen.getByLabelText("任务名称"), { target: { value: "  " } });
    fireEvent.change(screen.getByLabelText("任务指令"), { target: { value: "  " } });
    await openTargetPicker();
    expect(optionByTitle("个人 已删除会话（不可用）")).toHaveClass("ant-select-item-option-disabled");
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("请选择仍可用的输出对话")).toBeVisible();
    expect(screen.getByText("请输入任务名称")).toBeVisible();
    expect(screen.getByText("请输入任务指令")).toBeVisible();
    expect(screen.getByText("一次性任务必须安排在未来时间")).toBeVisible();
  });

  it("submits a structured future once schedule in Beijing time without profile/runtime/cron fields", async () => {
    const onSubmit = vi.fn();
    await renderDialog(<ScheduledTaskDialog sessions={[activeSession, expertSession]} onClose={vi.fn()} onSubmit={onSubmit}/>);

    fireEvent.change(screen.getByLabelText("任务名称"), { target: { value: "每日检查" } });
    fireEvent.change(screen.getByLabelText("任务指令"), { target: { value: "检查告警" } });
    await selectTarget("专家 选题规划");
    // antd DatePicker 支持直接键入日期时间文本（YYYY-MM-DD HH:mm），回车确认。
    const onceInput = screen.getByPlaceholderText("请选择日期和时间");
    fireEvent.change(onceInput, { target: { value: "2027-01-01 09:30" } });
    fireEvent.keyDown(onceInput, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "创建任务" }));

    expect(onSubmit).toHaveBeenCalledWith({
      taskName: "每日检查",
      prompt: "检查告警",
      targetRuntimeSessionId: "session-expert",
      scheduleConfig: { type: "once", runAt: "2027-01-01T01:30:00.000Z" }
    });
    expect(JSON.stringify(onSubmit.mock.calls[0][0])).not.toMatch(/profileId|runtimeId|cronExpr/);
  });

  it("rejects once schedules outside the current or next year", async () => {
    const onSubmit = vi.fn();
    await renderDialog(<ScheduledTaskDialog task={{ ...completedTask, status: "active", scheduleConfig: { type: "once", runAt: "2030-01-01T01:30:00.000Z" } }} sessions={[activeSession]} onClose={vi.fn()} onSubmit={onSubmit}/>);

    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("一次性任务只能安排在本年或明年")).toBeVisible();
  });

  it("requires completed tasks to be rescheduled before saving an edit", async () => {
    const onSubmit = vi.fn();
    await renderDialog(<ScheduledTaskDialog task={completedTask} sessions={[activeSession]} onClose={vi.fn()} onSubmit={onSubmit}/>);

    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("已完成任务需要重新安排未来执行时间或周期")).toBeVisible();

    fireEvent.click(screen.getByRole("radio", { name: "每天" }));
    const dailyInput = screen.getByLabelText("每日执行时间");
    fireEvent.change(dailyInput, { target: { value: "09:30" } });
    fireEvent.keyDown(dailyInput, { key: "Enter", keyCode: 13 });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ scheduleConfig: { type: "cron", preset: "daily", time: "09:30" } }));
  });

  it("keeps all supported schedule choices structured and labels the timezone", async () => {
    await renderDialog(<ScheduledTaskDialog sessions={[activeSession]} onClose={vi.fn()} onSubmit={vi.fn()}/>);
    expect(screen.getByText("时间均按北京时间（Asia/Shanghai）执行")).toBeVisible();
    const scheduleType = screen.getByRole("radiogroup", { name: "计划类型" });
    fireEvent.click(screen.getByRole("radio", { name: "间隔" }));
    expect(screen.getByRole("combobox", { name: "间隔数值" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "每周" }));
    expect(screen.getByLabelText("每周执行时间")).toBeInTheDocument();
  });

  it("labels output conversations by their personal or expert Profile", async () => {
    await renderDialog(<ScheduledTaskDialog sessions={[activeSession, expertSession, deletedSession]} onClose={vi.fn()} onSubmit={vi.fn()}/>);

    expect(screen.getByText("个人")).toBeVisible();
    expect(screen.getByText("项目讨论")).toBeVisible();

    await openTargetPicker();
    expect(optionByTitle("专家 选题规划")).toBeVisible();
    expect(optionByTitle("个人 已删除会话（不可用）")).toHaveClass("ant-select-item-option-disabled");
  });

  it("only exposes a personal WeCom session after an inbound message confirmed its target", async () => {
    await renderDialog(<ScheduledTaskDialog sessions={[unconfirmedWeComSession, confirmedWeComSession, activeSession]} onClose={vi.fn()} onSubmit={vi.fn()}/>);

    await openTargetPicker();
    expect(screen.queryByTitle("企微 未确认企微会话")).not.toBeInTheDocument();
    expect(optionByTitle("个人 项目讨论")).toBeVisible();
    expect(optionByTitle("企微 已确认企微会话")).toBeVisible();
  });

  it("supports keyboard navigation and closes the target picker on blur", async () => {
    await renderDialog(<ScheduledTaskDialog sessions={[activeSession, expertSession]} onClose={vi.fn()} onSubmit={vi.fn()}/>);

    const trigger = screen.getByRole("combobox", { name: "输出对话" });
    await openTargetPicker();
    // antd Select 用 keyCode 判定：↓(40) 高亮选项。
    fireEvent.keyDown(trigger, { key: "ArrowDown", keyCode: 40 });
    await waitFor(() => expect(document.querySelectorAll(".ant-select-item-option-active").length).toBeGreaterThan(0));
    // 失焦关闭（jsdom 下 dropdown 不卸载，以 hidden 类标记关闭态）。
    fireEvent.blur(trigger);
    await waitFor(() => expect(document.querySelector(".ant-select-dropdown")).toHaveClass("ant-select-dropdown-hidden"));
  });

  it("closes the target picker when pressing outside it", async () => {
    await renderDialog(<ScheduledTaskDialog sessions={[activeSession]} onClose={vi.fn()} onSubmit={vi.fn()}/>);

    await openTargetPicker();
    expect(optionByTitle("个人 项目讨论")).toBeVisible();
    // rc-select 的 outside 关闭监听 mousedown（非 pointerdown）。
    fireEvent.mouseDown(document.body);
    await waitFor(() => expect(document.querySelector(".ant-select-dropdown")).toHaveClass("ant-select-dropdown-hidden"));
  });
});
