import { describe, expect, it } from "vitest";
import {
  firstMessageTaskTitle,
  isDefaultTaskTitle,
  numberedTaskTitle
} from "./taskPresentation";

describe("task presentation", () => {
  it("recognizes current and legacy default task titles", () => {
    expect(isDefaultTaskTitle("新任务")).toBe(true);
    expect(isDefaultTaskTitle("新会话")).toBe(true);
    expect(isDefaultTaskTitle("  ")).toBe(true);
    expect(isDefaultTaskTitle("发布计划")).toBe(false);
  });

  it("normalizes whitespace and limits first-message titles to ten Unicode characters", () => {
    expect(firstMessageTaskTitle("  帮我\n\n整理   发布计划  ")).toBe("帮我 整理 发布计划");
    expect(firstMessageTaskTitle("一二三四五六七八九十十一")).toBe("一二三四五六七八九十…");
    expect(firstMessageTaskTitle("😀😀😀😀😀😀😀😀😀😀😀")).toBe("😀😀😀😀😀😀😀😀😀😀…");
  });

  it("adds a full-width duplicate suffix from the second candidate onward", () => {
    expect(numberedTaskTitle("你好", 1)).toBe("你好");
    expect(numberedTaskTitle("你好", 2)).toBe("你好（2）");
    expect(numberedTaskTitle("你好", 20)).toBe("你好（20）");
  });
});
