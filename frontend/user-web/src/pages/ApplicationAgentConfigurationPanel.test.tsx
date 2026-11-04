import type { ReactNode } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  applicationAgent: vi.fn(),
  runtimeModels: vi.fn(),
  updateApplicationAgent: vi.fn(),
  userAssetCatalog: vi.fn(),
  personalSkillCategories: vi.fn(),
  applicationAgentSkills: vi.fn(),
  systemSkills: vi.fn(),
  deleteApplicationAgentSkill: vi.fn()
}));

vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  userApi: api
}));

// 技能面板经 React.lazy 动态加载；antd Provider 包装与页面断言无关，
// mock 掉以避免测试运行时触发 antd 的重量级 transform（antd 渲染由组件级测试覆盖）。
vi.mock("../theme/antd-module", () => ({
  AntdModuleProviders: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
// 技能面板经 React.lazy 动态加载且自身静态依赖 antd；antd 在 vitest 的 transform
// 成本（冷启动实测 15-35s）远超单测超时。beforeAll 预热模块（独立超时），
// 同 worker 内后续动态 import 直接命中缓存。
beforeAll(async () => {
  await import("../components/skills");
}, 120_000);

import { ApplicationAgentConfigurationPanel } from "./ApplicationAgentConfigurationPanel";

const detail = {
  applicationAgentId: 31,
  slug: "ticket-agent",
  name: "工单助手",
  avatarUrl: null,
  status: "unpublished",
  runtimeStatus: "absent",
  configGeneration: 2,
  appliedGeneration: null,
  applyStatus: "not_applied",
  clientCount: 0,
  activeRunCount: 0,
  queuedRunCount: 0,
  personalKnowledgeEnabledCount: 0,
  personalKnowledgeRevokedCount: 0,
  publishedAt: null,
  updatedAt: null,
  lastApplyErrorCode: null,
  config: {
    systemPrompt: "",
    modelEntryId: 99,
    skillAssetIds: [],
    applicationSkillIds: [],
    mcpAssetIds: [],
    enterpriseKnowledgeEnabled: false,
    memoryEnabled: true
  }
};

describe("ApplicationAgentConfigurationPanel model visibility", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.applicationAgent.mockResolvedValue(detail);
    api.runtimeModels.mockResolvedValue([
      {
        modelEntryId: 3,
        modelKey: "model-a",
        displayName: "模型 A",
        providerId: 1,
        providerDisplayName: "Provider",
        isDefault: false,
        tags: []
      },
      {
        modelEntryId: 4,
        modelKey: "model-b",
        displayName: "模型 B",
        providerId: 1,
        providerDisplayName: "Provider",
        isDefault: false,
        tags: []
      }
    ]);
    api.updateApplicationAgent.mockImplementation(
      (_agentId: number, payload: { config: typeof detail.config }) => ({
        ...detail,
        configGeneration: 3,
        appliedGeneration: 3,
        applyStatus: "applied",
        config: payload.config
      })
    );
    api.userAssetCatalog.mockResolvedValue({ skills: [], mcps: [] });
    api.personalSkillCategories.mockResolvedValue([
      { categoryKey: "personal", displayName: "个人", isBuiltin: true }
    ]);
    api.applicationAgentSkills.mockResolvedValue([]);
    api.systemSkills.mockResolvedValue([]);
    api.deleteApplicationAgentSkill.mockResolvedValue(undefined);
  });

  it("warns when the configured model is invisible and requires an explicit new choice", async () => {
    const user = userEvent.setup();
    render(
      <ApplicationAgentConfigurationPanel
        applicationAgentId={31}
        section="settings"
      />
    );

    expect(
      await screen.findByText(/当前配置模型已不可用，请重新选择/)
    ).toBeVisible();
    const select = await screen.findByLabelText("默认模型");
    expect(
      (within(select).getByText("当前配置模型已不可用") as HTMLOptionElement).selected
    ).toBe(true);
    expect(screen.getByText(/将回退为：模型 A/)).toBeVisible();

    await user.selectOptions(select, "3");
    await user.click(screen.getByRole("button", { name: "保存并应用" }));

    await waitFor(() =>
      expect(api.updateApplicationAgent).toHaveBeenCalledWith(
        31,
        expect.objectContaining({
          config: expect.objectContaining({ modelEntryId: 3 })
        })
      )
    );
    expect(await screen.findByText("已保存并应用")).toBeVisible();
  });

  it("deletes an Application Agent private Skill through the deletion confirmation", async () => {
    const user = userEvent.setup();
    api.applicationAgent.mockResolvedValue({
      ...detail,
      config: {
        ...detail.config,
        applicationSkillIds: [41]
      }
    });
    api.applicationAgentSkills.mockResolvedValue([
      {
        applicationAgentSkillId: 41,
        applicationAgentId: 31,
        slug: "app-31-private-ticket",
        name: "私有工单",
        description: "应用独有",
        categoryKey: "personal",
        packageSha256: "a".repeat(64),
        skillVersionId: 42,
        version: 1,
        createdAt: null,
        updatedAt: null
      }
    ]);
    render(
      <ApplicationAgentConfigurationPanel
        applicationAgentId={31}
        section="skills"
      />
    );

    const card = await screen.findByLabelText("查看 私有工单 详情");
    await user.click(within(card).getByRole("button", { name: "删除" }));
    const deletion = screen.getByRole("dialog", { name: "删除应用私有技能" });
    await waitFor(() =>
      expect(within(deletion).getByText(/移除该私有技能及其上传包/)).toBeVisible()
    );
    await user.click(within(deletion).getByRole("button", { name: "确认删除" }));

    await waitFor(() =>
      expect(api.deleteApplicationAgentSkill).toHaveBeenCalledWith(31, 41)
    );
    expect(await screen.findByText(/“私有工单”已删除/)).toBeVisible();
  });
});
