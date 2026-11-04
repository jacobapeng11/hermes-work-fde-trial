import { describe, expect, it } from "vitest";
import {
  nextFilesPaneOpen,
  runtimeConnectionStateForDisplay,
  runtimeStatusLabel,
  shouldSendOnKeyDown,
  type RuntimeConnectionState
} from "./ui-state";

describe("user web interaction rules", () => {
  it("labels observable runtime connection phases", () => {
    expect(runtimeStatusLabel("connecting")).toBe("正在连接");
    expect(runtimeStatusLabel("starting")).toBe("正在预热");
    expect(runtimeStatusLabel("mounting")).toBe("正在预热");
    expect(runtimeStatusLabel("probing")).toBe("正在预热");
    expect(runtimeStatusLabel("checking")).toBe("正在检测运行环境...");
    expect(runtimeStatusLabel("waking")).toBe("正在唤醒");
    expect(runtimeStatusLabel("ready")).toBe("已连接");
    expect(runtimeStatusLabel("failed")).toBe("连接异常");
  });

  it("keeps a healthy runtime visibly connected during session readiness work", () => {
    expect(runtimeConnectionStateForDisplay("checking", "ready", true)).toBe("ready");
    expect(runtimeConnectionStateForDisplay("connecting", "ready", true)).toBe("ready");
    expect(runtimeConnectionStateForDisplay("failed", "ready", true)).toBe("ready");
    expect(runtimeConnectionStateForDisplay("waking", "ready", true)).toBe("waking");
    expect(runtimeConnectionStateForDisplay("checking", "ready", false)).toBe("checking");
    expect(runtimeConnectionStateForDisplay("starting", "starting", false)).toBe("starting");
  });

  it("does not claim a sleep state without a non-waking signal", () => {
    const supportedStates: RuntimeConnectionState[] = ["connecting", "starting", "mounting", "probing", "checking", "waking", "ready", "failed"];
    expect(supportedStates.map(runtimeStatusLabel)).not.toContain("休眠中");
  });

  it("keeps the file pane closed by default on narrow screens", () => {
    expect(nextFilesPaneOpen(null, false)).toBe(false);
    expect(nextFilesPaneOpen(null, true)).toBe(true);
    expect(nextFilesPaneOpen("false", true)).toBe(false);
  });

  it("sends with Enter only outside IME composition", () => {
    expect(shouldSendOnKeyDown({ key: "Enter", shiftKey: false, isComposing: false })).toBe(true);
    expect(shouldSendOnKeyDown({ key: "Enter", shiftKey: true, isComposing: false })).toBe(false);
    expect(shouldSendOnKeyDown({ key: "Enter", shiftKey: false, isComposing: true })).toBe(false);
  });
});
