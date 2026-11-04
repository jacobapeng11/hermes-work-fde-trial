import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  avatarAssets: vi.fn(),
  uploadAvatarAsset: vi.fn(),
  expertMarket: vi.fn(),
  marketExpertDetail: vi.fn(),
  installedExperts: vi.fn(),
  myExperts: vi.fn(),
  runnableExperts: vi.fn(),
  expertDetail: vi.fn(),
  expertInheritanceSources: vi.fn(),
  expertCapabilityCatalog: vi.fn(),
  organizationDirectoryTree: vi.fn(),
  searchDirectoryUsers: vi.fn(),
  createExpert: vi.fn(),
  copyExpert: vi.fn(),
  installMarketExpert: vi.fn(),
  uploadPersonalSkill: vi.fn(),
  uploadPersonalKnowledgeFile: vi.fn(),
  saveExpertConfiguration: vi.fn(),
  updateExpert: vi.fn(),
  replaceExpertCapabilities: vi.fn(),
  replaceExpertPermissions: vi.fn(),
  submitExpertReview: vi.fn(),
  unpublishExpert: vi.fn(),
  uninstallExpert: vi.fn()
}));

vi.mock("../../api/client", () => ({ userApi: api }));

import { ExpertSkillsPanelView } from "./ExpertSkillsPanel";

// 布局结构契约（2026-11-03-016/017）：真实 ExpertPage（非 mock 面板）以
// embedded 模式挂入组合页时，「我的/专家市场」二级 Tab 与「搜索专家 + 创建专家」
// 操作区必须位于同一个 .expertCatalogToolbar 容器内（DOM 与技能页签
// skillCatalogToolbar 同构：toolbar > tabsWrap + toolbarActions），防止掉行回归。
describe("ExpertSkillsPanelView 专家工具栏布局结构", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.expertMarket.mockResolvedValue([]);
    api.marketExpertDetail.mockResolvedValue(null);
    api.avatarAssets.mockResolvedValue([]);
    api.installedExperts.mockResolvedValue([]);
    api.myExperts.mockResolvedValue([]);
    api.runnableExperts.mockResolvedValue([]);
    api.expertDetail.mockResolvedValue(null);
    api.expertInheritanceSources.mockResolvedValue([]);
    api.expertCapabilityCatalog.mockResolvedValue({ skills: [], knowledge: [], mcps: [] });
    api.organizationDirectoryTree.mockResolvedValue([]);
  });

  it("二级 Tab 与搜索框、创建按钮同处一个工具栏容器", async () => {
    render(
      <ExpertSkillsPanelView
        runtimeWorkspaceId={1}
        onInstalledExpertsChange={vi.fn()}
        onStartExpertChat={vi.fn()}
      />
    );

    // 先等懒加载的专家面板挂载（Suspense resolve），再查工具栏容器
    const mineTab = await screen.findByRole("tab", { name: /我的/ });
    const toolbar = document.querySelector(".expertCatalogToolbar");
    expect(toolbar).not.toBeNull();
    expect(toolbar?.contains(mineTab)).toBe(true);
    const marketTab = screen.getByRole("tab", { name: /专家市场/ });
    expect(toolbar?.contains(marketTab)).toBe(true);

    // 工具栏右侧仅搜索框；「创建专家」在页头（与技能页签「上传技能」同位），
    // 空态卡片内的旧按钮已删（017），全页有且仅有页头这一处。
    const actions = toolbar?.querySelector(".expertCatalogToolbarActions");
    expect(actions).not.toBeNull();
    within(actions as HTMLElement).getByRole("textbox", { name: "搜索专家" });
    expect(within(actions as HTMLElement).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "创建专家" })).toHaveLength(1);

    // 工具栏两个直接子节点：左 tabsWrap、右 toolbarActions（与技能侧同构，无换行包裹层）
    const [tabsWrapNode, actionsNode] = Array.from(toolbar?.children ?? []);
    expect(tabsWrapNode?.className).toBe("expertCatalogTabsWrap");
    expect(tabsWrapNode?.querySelector(".expertAntTabs")?.className).toContain("secondaryLaneTabs");
    expect(actionsNode?.className).toBe("expertCatalogToolbarActions");
  });

  it("页头「创建专家」经 createTrigger 信号打开真实创建编辑器", async () => {
    const user = userEvent.setup();

    render(
      <ExpertSkillsPanelView
        runtimeWorkspaceId={1}
        onInstalledExpertsChange={vi.fn()}
        onStartExpertChat={vi.fn()}
      />
    );

    await screen.findByRole("tab", { name: /我的/ });
    await user.click(screen.getByRole("button", { name: "创建专家" }));

    expect(await screen.findByRole("dialog", { name: /^创建专家/ })).toBeVisible();
  });
});
