import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ScheduledTask } from "@hermes-cloud/schemas";
import { ScheduledTaskCard } from "./ScheduledTaskCard";

const task: ScheduledTask = {
  taskId: 12,
  taskName: "日报汇总",
  prompt: "汇总今天的项目进展和风险。",
  targetRuntimeSessionId: "session-1",
  targetSessionTitle: "项目讨论",
  targetSessionStatus: "active",
  scheduleConfig: { type: "cron", preset: "daily", time: "09:00" },
  scheduleSummary: "每天 09:00",
  status: "active",
  nextRunAt: "2026-07-18T01:00:00.000Z",
  lastRunAt: "2026-07-17T01:00:00.000Z",
  lastRunStatus: "success",
  lastRuntimeRunId: "run-12",
  runCount: 3,
  isRunning: false,
  lastErrorCode: null,
  lastError: null
};

describe("ScheduledTaskCard", () => {
  it("shows a compact task row with the prompt summary and expands its execution detail", () => {
    render(<ScheduledTaskCard task={task} onEdit={vi.fn()} onPause={vi.fn()} onResume={vi.fn()} onTrigger={vi.fn()} onOpenSession={vi.fn()}/>);

    expect(screen.getByText("日报汇总")).toBeVisible();
    expect(screen.getByText("汇总今天的项目进展和风险。")).toBeVisible();
    expect(screen.getByText("每天 09:00")).toBeVisible();
    expect(screen.getByRole("button", { name: "打开对话 项目讨论" })).toBeVisible();
    expect(screen.getByText("已启用")).toBeVisible();
    // 参考定版：footer 常显「上次执行」，「下次执行」进展开详情。
    expect(screen.getByText(/上次执行/)).toBeVisible();
    expect(screen.queryByText(/下次执行/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "展开详情" }));
    expect(screen.getByText(/上次执行/)).toBeVisible();
    expect(screen.getByText("上次结果")).toBeVisible();
    expect(screen.getByText("执行次数")).toBeVisible();
    expect(screen.getByText("最近运行")).toBeVisible();
  });

  it("expands an execution error by default without exposing internal error details", () => {
    render(<ScheduledTaskCard task={{ ...task, lastErrorCode: "SESSION_DELETED", lastError: "internal database detail" }} onEdit={vi.fn()} onPause={vi.fn()} onResume={vi.fn()} onTrigger={vi.fn()}/>);
    expect(screen.getByRole("button", { name: "收起详情" })).toBeVisible();
    expect(screen.getByText("输出对话已删除，请编辑并重新选择对话")).toBeVisible();
    expect(screen.queryByText("internal database detail")).not.toBeInTheDocument();
  });

  it("triggers, pauses and resumes according to the task state", () => {
    const onPause = vi.fn();
    const onResume = vi.fn();
    const onTrigger = vi.fn();
    const { rerender } = render(<ScheduledTaskCard task={task} onEdit={vi.fn()} onPause={onPause} onResume={onResume} onTrigger={onTrigger}/>);

    fireEvent.click(screen.getByRole("button", { name: "立即执行" }));
    fireEvent.click(screen.getByRole("switch", { name: "暂停任务" }));
    expect(onTrigger).toHaveBeenCalledWith(task);
    expect(onPause).toHaveBeenCalledWith(task);
    expect(screen.getByRole("button", { name: "展开详情" })).toBeVisible();

    rerender(<ScheduledTaskCard task={{ ...task, status: "paused" }} onEdit={vi.fn()} onPause={onPause} onResume={onResume} onTrigger={onTrigger}/>);
    fireEvent.click(screen.getByRole("switch", { name: "恢复任务" }));
    expect(onResume).toHaveBeenCalledWith(expect.objectContaining({ status: "paused" }));
  });

  it("keeps the immediate trigger visible but disabled when its target session is unavailable", () => {
    render(<ScheduledTaskCard task={{ ...task, targetSessionStatus: "deleted" }} onEdit={vi.fn()} onPause={vi.fn()} onResume={vi.fn()} onTrigger={vi.fn()}/>);
    expect(screen.getByRole("button", { name: "立即执行" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "收起详情" })).toBeVisible();
    expect(screen.getByText("输出对话不可用，请编辑并重新选择对话")).toBeVisible();
  });
});
