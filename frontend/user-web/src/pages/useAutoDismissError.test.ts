import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ERROR_NOTICE_AUTO_DISMISS_MS, useAutoDismissError } from "./useAutoDismissError";

describe("useAutoDismissError", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("默认无错误", () => {
    const { result } = renderHook(() => useAutoDismissError());
    expect(result.current[0]).toBeNull();
  });

  it("设置错误后在默认时长后自动清除", () => {
    const { result } = renderHook(() => useAutoDismissError());
    act(() => result.current[1]("当前模型不支持图片识别，请切换模型后重试"));
    expect(result.current[0]).toBe("当前模型不支持图片识别，请切换模型后重试");
    act(() => vi.advanceTimersByTime(ERROR_NOTICE_AUTO_DISMISS_MS - 1));
    expect(result.current[0]).not.toBeNull();
    act(() => vi.advanceTimersByTime(1));
    expect(result.current[0]).toBeNull();
  });

  it("相同文案重复设置时重新计时", () => {
    const { result } = renderHook(() => useAutoDismissError(1000));
    act(() => result.current[1]("提交确认失败"));
    act(() => vi.advanceTimersByTime(800));
    act(() => result.current[1]("提交确认失败"));
    act(() => vi.advanceTimersByTime(800));
    expect(result.current[0]).toBe("提交确认失败");
    act(() => vi.advanceTimersByTime(200));
    expect(result.current[0]).toBeNull();
  });

  it("新错误替换旧错误时按新错误重新计时", () => {
    const { result } = renderHook(() => useAutoDismissError(1000));
    act(() => result.current[1]("旧错误"));
    act(() => vi.advanceTimersByTime(900));
    act(() => result.current[1]("新错误"));
    act(() => vi.advanceTimersByTime(900));
    expect(result.current[0]).toBe("新错误");
  });

  it("显式清除后立即消失且不再触发定时器", () => {
    const { result } = renderHook(() => useAutoDismissError(1000));
    act(() => result.current[1]("读取任务失败"));
    act(() => result.current[1](null));
    expect(result.current[0]).toBeNull();
    act(() => vi.advanceTimersByTime(2000));
    expect(result.current[0]).toBeNull();
  });
});
