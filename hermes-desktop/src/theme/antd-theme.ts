import { theme as antdTheme } from "antd";
import type { ThemeConfig } from "antd";
import type { PlatformTheme } from "./platform-theme";

// 与 tokens.css 人工同步的静态映射（设计文档 §6.3）。
// 主色/字体/圆角/控件高度两套共用；表面、文字、边框、语义色按主题取现网值，
// 保证 antd 迁移页面与周围遗留 chrome 融合。修改 tokens.css 时须同步本文件。
const BRAND_TOKENS = {
  colorPrimary: "#2563eb",
  colorLink: "#2563eb",
  colorInfo: "#2563eb",
  // 标准字体栈（与 frontend-engineering Skill 一致）：不显式列中文字体，
  // 让浏览器走系统回退链，与协作平台的中文渲染路径一致。
  fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif',
  fontSize: 14,
  borderRadius: 6,
  controlHeight: 32,
} as const;

const LIGHT_TOKENS = {
  colorBgLayout: "#F7F8FA",
  colorBgContainer: "#FFFFFF",
  colorBgElevated: "#FFFFFF",
  colorText: "#172033",
  colorTextSecondary: "#526078",
  colorBorder: "#CDD5E0",
  colorBorderSecondary: "#D0D8E4",
  colorSuccess: "#047857",
  colorWarning: "#a16207",
  colorError: "#b42318",
} as const;

const DARK_TOKENS = {
  colorBgLayout: "#212121",
  colorBgContainer: "#1b1b1b",
  colorBgElevated: "#303030",
  colorText: "#ececec",
  colorTextSecondary: "#b4b4b4",
  colorBorder: "rgba(255, 255, 255, 0.10)",
  colorSuccess: "#22c55e",
  colorWarning: "#f59e0b",
  colorError: "#ef4444",
} as const;

export function antdThemeFor(theme: PlatformTheme): ThemeConfig {
  return theme === "dark"
    ? { algorithm: antdTheme.darkAlgorithm, token: { ...BRAND_TOKENS, ...DARK_TOKENS } }
    : { algorithm: antdTheme.defaultAlgorithm, token: { ...BRAND_TOKENS, ...LIGHT_TOKENS } };
}
