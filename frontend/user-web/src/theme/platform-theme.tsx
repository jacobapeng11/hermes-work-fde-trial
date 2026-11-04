import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

// 平台主题唯一状态源：读取 localStorage["hermes.theme"]；无有效偏好时
// 缺省 light，显式 dark 仍保持深色。本文件与 antd-theme.ts 的映射值如需
// 调整，必须与 tokens.css 人工同步。
export const PLATFORM_THEME_STORAGE_KEY = "hermes.theme";

export type PlatformTheme = "dark" | "light";

interface PlatformThemeContextValue {
  theme: PlatformTheme;
  setTheme: (next: PlatformTheme) => void;
}

const PlatformThemeContext = createContext<PlatformThemeContextValue>({
  // 无 Provider 时的回退：等价于各组件迁移前的逐渲染直读语义，
  // 使独立渲染的测试（不经 main.tsx 根 Provider）行为保持不变。
  get theme(): PlatformTheme {
    return readStoredTheme();
  },
  setTheme(next: PlatformTheme) {
    localStorage.setItem(PLATFORM_THEME_STORAGE_KEY, next);
  },
});

function readStoredTheme(): PlatformTheme {
  return localStorage.getItem(PLATFORM_THEME_STORAGE_KEY) === "dark" ? "dark" : "light";
}

const readInitialTheme = readStoredTheme;

export function PlatformThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<PlatformTheme>(readInitialTheme);
  // body 同步 data-theme：antd Modal/Drawer 经 Portal 挂在 body 直下（.appShell 之外），
  // 各模块 CSS 的 [data-theme="dark"] 暗色规则需要 body 级属性才能命中 Portal 内容。
  useEffect(() => {
    document.body.dataset.theme = theme;
  }, [theme]);
  const setTheme = useCallback((next: PlatformTheme) => {
    localStorage.setItem(PLATFORM_THEME_STORAGE_KEY, next);
    setThemeState(next);
  }, []);
  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme]);
  return <PlatformThemeContext.Provider value={value}>{children}</PlatformThemeContext.Provider>;
}

export function usePlatformTheme(): PlatformThemeContextValue {
  return useContext(PlatformThemeContext);
}
