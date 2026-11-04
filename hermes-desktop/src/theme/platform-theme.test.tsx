import { renderHook, act } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { PlatformThemeProvider, usePlatformTheme, PLATFORM_THEME_STORAGE_KEY } from "./platform-theme";

const wrapper = ({ children }: { children: ReactNode }) => (
  <PlatformThemeProvider>{children}</PlatformThemeProvider>
);

describe("PlatformThemeProvider", () => {
  beforeEach(() => localStorage.clear());

  it("新用户缺省主题为 light", () => {
    const { result } = renderHook(() => usePlatformTheme(), { wrapper });
    expect(result.current.theme).toBe("light");
  });

  it("从 localStorage 读取显式 light", () => {
    localStorage.setItem(PLATFORM_THEME_STORAGE_KEY, "light");
    const { result } = renderHook(() => usePlatformTheme(), { wrapper });
    expect(result.current.theme).toBe("light");
  });

  it("从 localStorage 读取显式 dark", () => {
    localStorage.setItem(PLATFORM_THEME_STORAGE_KEY, "dark");
    const { result } = renderHook(() => usePlatformTheme(), { wrapper });
    expect(result.current.theme).toBe("dark");
  });

  it("异常主题值回退为 light", () => {
    localStorage.setItem(PLATFORM_THEME_STORAGE_KEY, "unknown");
    const { result } = renderHook(() => usePlatformTheme(), { wrapper });
    expect(result.current.theme).toBe("light");
  });

  it("setTheme 持久化并通知消费者", () => {
    const { result } = renderHook(() => usePlatformTheme(), { wrapper });
    act(() => result.current.setTheme("light"));
    expect(result.current.theme).toBe("light");
    expect(localStorage.getItem(PLATFORM_THEME_STORAGE_KEY)).toBe("light");
  });

  it("无 Provider 时回退为直读 localStorage 且不抛错", () => {
    localStorage.setItem(PLATFORM_THEME_STORAGE_KEY, "light");
    const { result } = renderHook(() => usePlatformTheme());
    expect(result.current.theme).toBe("light");
    act(() => result.current.setTheme("dark"));
    expect(localStorage.getItem(PLATFORM_THEME_STORAGE_KEY)).toBe("dark");
  });
});
