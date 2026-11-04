import { describe, expect, it } from "vitest";
import { formatScheduledTime, scheduledErrorMessage, scheduleStatusLabel } from "./schedulePresentation";

describe("scheduled task presentation", () => {
  it("formats instants in Beijing time regardless of the browser timezone", () => {
    expect(formatScheduledTime("2026-07-18T10:00:00.000Z")).toBe("2026/07/18 18:00");
    expect(formatScheduledTime(null)).toBe("—");
  });

  it("keeps schedule status distinct from a transient run", () => {
    expect(scheduleStatusLabel("active", false)).toBe("已启用");
    expect(scheduleStatusLabel("active", true)).toBe("已启用 · 执行中");
    expect(scheduleStatusLabel("paused", false)).toBe("已暂停");
    expect(scheduleStatusLabel("completed", false)).toBe("已完成");
  });

  it("maps the stable deleted-session code and otherwise uses the safe fallback", () => {
    expect(scheduledErrorMessage("SESSION_DELETED", "internal detail")).toBe("输出对话已删除，请编辑并重新选择对话");
    expect(scheduledErrorMessage("RUN_FAILED", "运行失败，请稍后重试")).toBe("运行失败，请稍后重试");
    expect(scheduledErrorMessage(null, null)).toBeNull();
  });
});
