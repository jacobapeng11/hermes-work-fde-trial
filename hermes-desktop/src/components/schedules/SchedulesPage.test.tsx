import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listScheduledTasks: vi.fn(), createScheduledTask: vi.fn(), updateScheduledTask: vi.fn(),
  pauseScheduledTask: vi.fn(), resumeScheduledTask: vi.fn(), triggerScheduledTask: vi.fn(), deleteScheduledTask: vi.fn()
}));
vi.mock("../../api/client", () => ({ userApi: api }));

import { SchedulesPage } from "./SchedulesPage";

const task = {
  taskId: 9, taskName: "每日总结", prompt: "总结今天", targetRuntimeSessionId: "session-a",
  targetSessionTitle: "对话 A", targetSessionStatus: "active", scheduleConfig: { type: "cron" as const, preset: "daily" as const, time: "09:00" },
  scheduleSummary: "每天 09:00", status: "active" as const, nextRunAt: null, lastRunAt: null, lastRunStatus: null,
  lastErrorCode: null, lastError: null, lastRuntimeRunId: null, runCount: 0, isRunning: false
};

describe("SchedulesPage", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    api.listScheduledTasks.mockResolvedValue([task]);
    api.triggerScheduledTask.mockResolvedValue({ taskId: 9, targetRuntimeSessionId: "session-a", acceptedAt: "2026-07-17T00:00:00Z" });
  });
  afterEach(() => vi.useRealTimers());

  it("loads the workspace without waking or prewarming it, opens the accepted trigger target, and refreshes", async () => {
    vi.useRealTimers();
    const onOpenSession = vi.fn();
    render(<SchedulesPage runtimeWorkspaceId={7} sessions={[{ sessionId: 1, runtimeWorkspaceId: 7, runtimeSessionId: "session-a", title: "对话 A", status: "active", sourceType: "web", messageCount: 0 }]} onOpenSession={onOpenSession}/>);

    await waitFor(() => expect(api.listScheduledTasks).toHaveBeenCalledWith(7));
    fireEvent.click(await screen.findByRole("button", { name: "打开对话 对话 A" }));
    expect(onOpenSession).toHaveBeenCalledWith("session-a", 9);
    fireEvent.click(screen.getByRole("button", { name: "立即执行" }));
    await waitFor(() => expect(api.triggerScheduledTask).toHaveBeenCalledWith(7, 9));
    expect(onOpenSession).toHaveBeenCalledWith("session-a", 9, "2026-07-17T00:00:00Z", "每日总结", "总结今天");
    expect(await screen.findByRole("status")).toHaveTextContent("任务已提交");
    expect(api.listScheduledTasks).toHaveBeenCalledTimes(2);
  });

  it("uses ten-second polling, switches to three seconds while running, and clears timers on unmount", async () => {
    const { unmount, rerender } = render(<SchedulesPage runtimeWorkspaceId={7} sessions={[]} onOpenSession={vi.fn()}/>);
    await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(api.listScheduledTasks).toHaveBeenCalledTimes(2);
    api.listScheduledTasks.mockResolvedValueOnce([{ ...task, isRunning: true }]);
    rerender(<SchedulesPage runtimeWorkspaceId={7} sessions={[]} onOpenSession={vi.fn()}/>);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(api.listScheduledTasks).toHaveBeenCalledTimes(4);
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(api.listScheduledTasks).toHaveBeenCalledTimes(4);
  });

  it("does not call the API without a workspace and provides a recoverable retry action", () => {
    const onRetryWorkspace = vi.fn();
    render(<SchedulesPage runtimeWorkspaceId={null} sessions={[]} onOpenSession={vi.fn()} onRetryWorkspace={onRetryWorkspace}/>);
    expect(api.listScheduledTasks).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "重试连接" }));
    expect(onRetryWorkspace).toHaveBeenCalledTimes(1);
  });
});
