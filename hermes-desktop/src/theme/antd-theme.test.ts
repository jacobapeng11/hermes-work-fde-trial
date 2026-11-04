import { theme as antdTheme } from "antd";
import { describe, expect, it } from "vitest";
import { antdThemeFor } from "./antd-theme";

describe("antdThemeFor", () => {
  it("两套主题使用公司蓝主色", () => {
    for (const mode of ["light", "dark"] as const) {
      expect(antdThemeFor(mode).token?.colorPrimary).toBe("#2563eb");
      expect(antdThemeFor(mode).token?.colorLink).toBe("#2563eb");
      expect(antdThemeFor(mode).token?.colorInfo).toBe("#2563eb");
    }
  });

  it("light 映射现网表面与文字", () => {
    const config = antdThemeFor("light");
    const token = config.token;
    expect(token?.colorBgLayout).toBe("#F7F8FA");
    expect(token?.colorBgContainer).toBe("#FFFFFF");
    expect(token?.colorText).toBe("#172033");
    expect(token?.colorTextSecondary).toBe("#526078");
    expect(token?.colorBorder).toBe("#CDD5E0");
    expect(token?.colorBorderSecondary).toBe("#D0D8E4");
    expect(token?.colorSuccess).toBe("#047857");
    expect(token?.colorWarning).toBe("#a16207");
    expect(token?.colorError).toBe("#b42318");
    expect(token?.borderRadius).toBe(6);
    expect(token?.controlHeight).toBe(32);
    expect(token?.fontSize).toBe(14);
    expect(token?.fontFamily).toContain("Inter");
    expect(config.algorithm).toBe(antdTheme.defaultAlgorithm);
  });

  it("dark 使用 darkAlgorithm 并映射现网暗色表面", () => {
    const config = antdThemeFor("dark");
    expect(config.algorithm).toBe(antdTheme.darkAlgorithm);
    expect(config.token?.colorBgLayout).toBe("#212121");
    expect(config.token?.colorBgContainer).toBe("#1b1b1b");
    expect(config.token?.colorBgElevated).toBe("#303030");
    expect(config.token?.colorText).toBe("#ececec");
    expect(config.token?.colorTextSecondary).toBe("#b4b4b4");
    expect(config.token?.colorBorder).toBe("rgba(255, 255, 255, 0.10)");
    expect(config.token?.colorSuccess).toBe("#22c55e");
    expect(config.token?.colorWarning).toBe("#f59e0b");
    expect(config.token?.colorError).toBe("#ef4444");
  });
});
