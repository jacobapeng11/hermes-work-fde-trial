import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PlazaSkillPanelView } from "./PlazaSkillPanelView";

// 覆盖 AntdModuleProviders 包裹路径：页面级测试 mock 掉 antd-module，
// antd 渲染由本文件与组件级测试承担（antd 在本文件为静态导入，transform 成本发生在收集期）。
describe("PlazaSkillPanelView", () => {
  it("包裹 antd Provider 渲染技能广场 region", async () => {
    render(<PlazaSkillPanelView />);
    expect(await screen.findByRole("region", { name: "技能广场" })).toBeInTheDocument();
  });
});
