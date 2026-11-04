import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ExpertSkillsPanelView } from "./ExpertSkillsPanel";

vi.mock("../experts", () => ({
  ExpertPanelView: (props: { runtimeWorkspaceId: number | null; embedded?: boolean; createTrigger?: number }) => (
    <section aria-label="专家面板 mock">专家工作区 {props.runtimeWorkspaceId} embedded={String(props.embedded ?? false)} createTrigger={props.createTrigger ?? 0}</section>
  )
}));

vi.mock("../skills", () => ({
  PlazaSkillPanelView: (props: { runtimeWorkspaceId: number | null; chromeless?: boolean; uploadTrigger?: number }) => (
    <section aria-label="技能面板 mock">技能工作区 {props.runtimeWorkspaceId} chromeless={String(props.chromeless ?? false)} uploadTrigger={props.uploadTrigger ?? 0}</section>
  )
}));

describe("ExpertSkillsPanelView", () => {
  it("默认展示专家页签并切换到技能页签", async () => {
    const user = userEvent.setup();

    render(
      <ExpertSkillsPanelView
        runtimeWorkspaceId={7}
        onInstalledExpertsChange={vi.fn()}
        onStartExpertChat={vi.fn()}
      />
    );

    expect(screen.getByRole("region", { name: "专家-技能" })).toBeVisible();
    expect(await screen.findByRole("region", { name: "专家面板 mock" })).toHaveTextContent("专家工作区 7");
    expect(screen.queryByRole("region", { name: "技能面板 mock" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "技能" }));

    expect(await screen.findByRole("region", { name: "技能面板 mock" })).toHaveTextContent("技能工作区 7");
  });

  it("以大卡片结构渲染，专家面板走嵌入模式；页头「创建专家」经信号驱动面板", async () => {
    const user = userEvent.setup();

    render(
      <ExpertSkillsPanelView
        runtimeWorkspaceId={7}
        onInstalledExpertsChange={vi.fn()}
        onStartExpertChat={vi.fn()}
      />
    );

    expect(screen.getByRole("heading", { name: "专家与技能" })).toBeVisible();
    expect(document.querySelector(".expertSkillsWorkspace")).not.toBeNull();
    const panel = await screen.findByRole("region", { name: "专家面板 mock" });
    expect(panel).toHaveTextContent("embedded=true");
    expect(panel).toHaveTextContent("createTrigger=0");

    await user.click(screen.getByRole("button", { name: "创建专家" }));

    expect(await screen.findByRole("region", { name: "专家面板 mock" })).toHaveTextContent("createTrigger=1");
  });

  it("上传主按钮仅在技能页签出现，点击递增上传信号传给技能面板", async () => {
    const user = userEvent.setup();

    render(
      <ExpertSkillsPanelView
        runtimeWorkspaceId={7}
        onInstalledExpertsChange={vi.fn()}
        onStartExpertChat={vi.fn()}
      />
    );

    expect(screen.queryByRole("button", { name: "上传技能" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "技能" }));

    const skillPanel = await screen.findByRole("region", { name: "技能面板 mock" });
    expect(skillPanel).toHaveTextContent("chromeless=true");
    expect(skillPanel).toHaveTextContent("uploadTrigger=0");

    await user.click(screen.getByRole("button", { name: "上传技能" }));

    expect(await screen.findByRole("region", { name: "技能面板 mock" })).toHaveTextContent("uploadTrigger=1");
  });
});
