import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  AdminModelConfigResponse,
  AdminPrincipalSummary,
  AdminUsageSummaryResponse,
  AdminAssetSummary
} from "@hermes-cloud/schemas";

const api = vi.hoisted(() => ({
  me: vi.fn(),
  modelConfig: vi.fn(),
  runtimeResources: vi.fn(),
  usageSummary: vi.fn(),
  presenceSummary: vi.fn(),
  auditLogs: vi.fn(),
  publicSkillCatalog: vi.fn(),
  publicMcps: vi.fn(),
  uploadPublicSkill: vi.fn(),
  uploadPublicSkillVersion: vi.fn(),
  publicSkillVersions: vi.fn(),
  selectPublicSkillVersion: vi.fn(),
  createPublicMcp: vi.fn(),
  updatePublicMcp: vi.fn(),
  deletePublicMcp: vi.fn(),
  forceDisablePublicMcp: vi.fn(),
  enablePublicMcp: vi.fn(),
  updatePublicSkillCategory: vi.fn(),
  unpublishPublicSkill: vi.fn(),
  republishPublicSkill: vi.fn(),
  deletePublicSkill: vi.fn(),
  forceDisablePublicSkill: vi.fn(),
  enablePublicSkill: vi.fn(),
  skillCategories: vi.fn(),
  directoryDepartmentTree: vi.fn(),
  modelVisibilityDirectoryTree: vi.fn(),
  searchDirectoryUsers: vi.fn(),
  createSkillCategory: vi.fn(),
  publishModelConfig: vi.fn(),
  taskPromptConfig: vi.fn(),
}));

vi.mock("../api/client", () => ({ adminApi: api }));

import { AdminDashboardPage } from "./AdminDashboardPage";

function publicAsset(overrides: Partial<AdminAssetSummary>): AdminAssetSummary {
  return {
    assetId: 1,
    assetType: "skill",
    slug: "demo-skill",
    version: "1",
    name: "Demo Skill",
    description: "Demo description",
    status: "published",
    storageUri: "file:///assets/skills/demo-skill/1",
    manifest: {},
    summary: { fileCount: 2 },
    createdAt: null,
    ...overrides
  };
}

const principal: AdminPrincipalSummary = { orgId: 1, userId: 99, roles: ["admin"] };

const modelConfig: AdminModelConfigResponse = { entries: [], latestRelease: null };

const thinkingModelConfig: AdminModelConfigResponse = {
  entries: [{
    providerId: 1,
    providerName: "deepseek-main",
    providerDisplayName: "DeepSeek Main",
    providerType: "openai_compatible",
    apiEndpoint: "https://provider.example/v1",
    credentialConfigured: true,
    providerStatus: "active",
    providerCatalogVersion: 1,
    modelEntryId: 1,
    modelKey: "deepseek-v4-pro",
    modelDisplayName: "DeepSeek V4 Pro",
    contextLength: 128000,
    tags: ["thinking"],
    isRestricted: false,
    visibilityScope: { organization: true, userIds: [], departmentIds: [] },
    visibilityUsers: [],
    isDefault: true,
    modelStatus: "active",
    modelCatalogVersion: 1,
    runtimeCapabilities: { thinkingMode: "enabled", reasoningReplay: "required" }
  }],
  latestRelease: null
};

const usage: AdminUsageSummaryResponse = {
  overview: {
    runCount: 0,
    activeRunCount: 0,
    modelCallCount: 0,
    auditLogCount: 0,
    errorCount: 0,
    inputTokens: 0,
    outputTokens: 0,
    totalTokens: 0,
    averageLatencyMs: 0
  },
  runtimeWorkspaces: [],
  models: [],
  recentErrors: []
};

describe("AdminDashboardPage public asset upload", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, "", "/admin");
    api.me.mockResolvedValue(principal);
    api.modelConfig.mockResolvedValue(modelConfig);
    api.runtimeResources.mockResolvedValue([]);
    api.usageSummary.mockResolvedValue(usage);
    api.presenceSummary.mockResolvedValue({
      webOnline: {
        count: 12,
        users: ["张三（10001）"]
      },
      desktopOnline: {
        count: 5,
        users: ["李四（10002）"]
      },
      dailyLogins: {
        count: 38,
        users: ["王五（10003）"]
      },
      windowSeconds: 120,
      dailyLoginSince: "2026-10-13T00:00:00+08:00",
      observedAt: "2026-10-13T08:30:00Z",
      source: "platform-session-user-directory"
    });
    api.auditLogs.mockResolvedValue([]);
    api.publicSkillCatalog.mockResolvedValue([publicAsset({})]);
    api.publicMcps.mockResolvedValue([]);
    api.publicSkillVersions.mockResolvedValue([]);
    api.skillCategories.mockResolvedValue([
      { categoryKey: "productivity", displayName: "生产力", isBuiltin: true, isVisible: true },
      { categoryKey: "research", displayName: "研究", isBuiltin: true, isVisible: true }
    ]);
    api.directoryDepartmentTree.mockResolvedValue([]);
    api.modelVisibilityDirectoryTree.mockResolvedValue([]);
    api.searchDirectoryUsers.mockResolvedValue([]);
  });

  it("maps admin tabs to stable URLs and restores a direct URL", async () => {
    window.history.replaceState({}, "", "/admin/assets");
    render(<AdminDashboardPage />);

    expect(await screen.findByRole("region", { name: "广场资产" })).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: "专家管理" }));
    expect(window.location.pathname).toBe("/admin/experts");
    expect(await screen.findByRole("heading", { name: "专家管理" })).toBeVisible();
  });

  it("maps the task prompt configuration module to a stable direct URL", async () => {
    api.taskPromptConfig.mockResolvedValue({
      revision: 0,
      config: null
    });
    window.history.replaceState({}, "", "/admin/task-prompts");
    render(<AdminDashboardPage />);

    expect(await screen.findByRole("region", { name: "任务提示配置" })).toBeVisible();
    expect(screen.getByRole("button", { name: "任务提示配置" })).toHaveAttribute(
      "data-active",
      "true"
    );
  });

  it("shows online users above the resources console without exposing session details", async () => {
    window.history.replaceState({}, "", "/admin/resources");
    render(<AdminDashboardPage />);

    const presence = await screen.findByRole("status", { name: "平台在线状态" });
    expect(presence).toHaveAttribute("data-state", "ready");
    expect(presence.compareDocumentPosition(
      screen.getByRole("region", { name: "Runtime 资源控制台" })
    )).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.getByText("网页端在线用户数")).toBeVisible();
    expect(screen.getByText("桌面端在线用户数")).toBeVisible();
    expect(screen.getByText("当天登录用户数")).toBeVisible();
    expect(screen.getByText("张三（10001）")).toBeVisible();
    expect(screen.getByText("李四（10002）")).toBeVisible();
    expect(screen.getByText("王五（10003）")).toBeVisible();
    expect(screen.getAllByText("近 2 分钟活跃 · 个人 Runtime 运行中")).toHaveLength(2);
    await waitFor(() => expect(api.presenceSummary).toHaveBeenCalled());
  });

  it("keeps expert management and removes review publishing", async () => {
    render(<AdminDashboardPage />);

    expect(await screen.findByRole("button", { name: /专家管理/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /审核发布/ })).not.toBeInTheDocument();
  });

  it("publishes the explicit thinking protocol from the model configuration draft", async () => {
    api.modelConfig.mockResolvedValue(thinkingModelConfig);
    api.publishModelConfig.mockResolvedValue({});
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /模型治理/ }));
    expect(await screen.findByLabelText("思考模型协议")).toBeChecked();
    expect(screen.getByText(/仅为已确认兼容的模型开启/)).toBeVisible();

    await user.click(screen.getByLabelText("思考模型协议"));
    expect(screen.getByRole("status")).toHaveTextContent("恢复 1 个模型");
    await user.click(screen.getByRole("button", { name: "发布当前草稿" }));

    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("恢复标准协议"));
    await waitFor(() => expect(api.publishModelConfig).toHaveBeenCalledWith([
      expect.objectContaining({
        runtimeCapabilities: { thinkingMode: "provider_default", reasoningReplay: "not_supported" }
      })
    ]));
  });

  it("batch protocol settings only modify selected model drafts", async () => {
    api.modelConfig.mockResolvedValue({
      ...thinkingModelConfig,
      entries: [
        { ...thinkingModelConfig.entries[0], modelEntryId: 1, modelKey: "model-a", modelDisplayName: "Model A", runtimeCapabilities: { thinkingMode: "provider_default", reasoningReplay: "not_supported" } },
        { ...thinkingModelConfig.entries[0], modelEntryId: 2, modelKey: "model-b", modelDisplayName: "Model B", runtimeCapabilities: { thinkingMode: "provider_default", reasoningReplay: "not_supported" } }
      ]
    });
    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /模型治理/ }));
    await user.click(screen.getByLabelText("选择 Model A 进行批量推理协议设置"));
    await user.click(screen.getByRole("button", { name: "批量启用思考协议" }));

    const toggles = screen.getAllByLabelText("思考模型协议");
    expect(toggles[0]).toBeChecked();
    expect(toggles[1]).not.toBeChecked();
  });

  it("shows the public asset management tab and refreshes after Skill upload", async () => {
    const uploaded = publicAsset({
      assetId: 2,
      slug: "sales-skill",
      name: "Sales Skill",
      version: "1.2.3",
      summary: { fileCount: 3 }
    });
    api.uploadPublicSkill.mockResolvedValue(uploaded);
    api.publicSkillCatalog
      .mockResolvedValueOnce([publicAsset({})])
      .mockResolvedValueOnce([publicAsset({}), uploaded]);

    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    expect(screen.getByRole("region", { name: "广场资产" })).toBeVisible();
    expect(screen.getByRole("button", { name: /技能管理/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /MCP 管理/ })).toBeVisible();
    expect(screen.getByText("Demo Skill")).toBeVisible();
    expect(screen.getByText("公共技能库")).toBeVisible();
    expect(screen.getAllByText("已发布")[0]).toBeVisible();
    expect(screen.queryByText("内容")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^上传技能/ }));
    await user.upload(screen.getByLabelText(/技能 zip/), new File(["zip"], "sales.zip", { type: "application/zip" }));
    await user.click(screen.getByRole("button", { name: /确认上传/ }));

    await waitFor(() => expect(api.uploadPublicSkill).toHaveBeenCalledWith({
      file: expect.any(File),
      categoryKey: "productivity",
      categoryDisplayName: null
    }));
    expect(api.uploadPublicSkill.mock.calls[0][0].file.name).toBe("sales.zip");
    expect(await screen.findByRole("status")).toHaveTextContent("上传成功");
    expect(screen.getByRole("status")).toHaveTextContent("管理员上传公共资产为可信直发，不进入审核队列。");
    expect(screen.getByText("Sales Skill")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /Sales Skill/ }));
    expect(await screen.findByRole("dialog", { name: "Sales Skill 详情" })).toBeVisible();
    expect(screen.getByText("3 个文件")).toBeVisible();
    expect(screen.queryByText(/fileCount/)).not.toBeVisible();
  });

  it("creates one Streamable HTTP MCP from mcpServers JSON", async () => {
    const uploaded = {
      assetId: 3,
      versionId: 13,
      currentVersionId: 13,
      assetType: "mcp" as const,
      slug: "governed-mcp",
      version: "1",
      name: "Governed MCP",
      description: null,
      status: "published",
      storageUri: "",
      manifest: { transport: "streamable_http" },
      summary: {},
      createdAt: null
    };
    api.publicMcps.mockResolvedValueOnce([]).mockResolvedValueOnce([uploaded]);
    api.createPublicMcp.mockResolvedValue(uploaded);

    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    await user.click(screen.getByRole("button", { name: /MCP 管理/ }));
    await user.click(screen.getByRole("button", { name: "创建 MCP" }));
    const dialog = await screen.findByRole("dialog", { name: "创建公共 MCP" });
    await user.type(within(dialog).getByLabelText("名称"), "Governed MCP");
    fireEvent.change(within(dialog).getByLabelText("mcpServers 配置"), { target: { value: JSON.stringify({
      mcpServers: { "governed-mcp": { url: "http://10.0.0.8:9572/mcp" } }
    }) } });
    await user.click(within(dialog).getByRole("button", { name: "确认创建" }));

    await waitFor(() => expect(api.createPublicMcp).toHaveBeenCalledWith({
      slug: "governed-mcp",
      name: "Governed MCP",
      description: null,
      transport: "streamable-http",
      endpoint: "http://10.0.0.8:9572/mcp",
      config: {}
    }));
    expect(await screen.findByText("Governed MCP")).toBeVisible();
    expect(screen.getByText("协议：STREAMABLE-HTTP")).toBeVisible();
  });

  it("updates MCP configuration while preserving its slug and asset identity", async () => {
    const existing = publicAsset({
      assetId: 3,
      versionId: 13,
      currentVersionId: 13,
      assetType: "mcp",
      slug: "governed-mcp",
      version: "1",
      name: "Governed MCP",
      manifest: { transport: "streamable_http", endpoint: "http://10.0.0.8/mcp" },
      summary: { connection_config: { url: "http://10.0.0.8/mcp" } },
    });
    const updated = { ...existing, versionId: 14, currentVersionId: 14, version: "2" };
    api.publicMcps.mockResolvedValue([existing]);
    api.updatePublicMcp.mockResolvedValue({
      asset: updated,
      rollout: { rolloutId: "mcp-3-update", affectedUserCount: 2, affectedProfileCount: 3, status: "waiting", created: true },
    });

    render(<AdminDashboardPage />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    await user.click(screen.getByRole("button", { name: /MCP 管理/ }));
    const row = screen.getByText("Governed MCP").closest("article") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "更新配置" }));

    const dialog = await screen.findByRole("dialog", { name: "更新 MCP 配置" });
    expect(within(dialog).getByLabelText("slug（不可修改）")).toHaveValue("governed-mcp");
    fireEvent.change(within(dialog).getByLabelText("更新 mcpServers 配置"), { target: { value: JSON.stringify({
      mcpServers: { "governed-mcp": { url: "http://10.0.0.9/mcp" } }
    }) } });
    await user.click(within(dialog).getByRole("button", { name: "确认更新" }));

    await waitFor(() => expect(api.updatePublicMcp).toHaveBeenCalledWith(expect.objectContaining({
      assetId: 3,
      expectedCurrentVersionId: 13,
      endpoint: "http://10.0.0.9/mcp",
    })));
    expect(await screen.findByText(/正在自动更新 2 位用户的 3 个 Profile/)).toBeVisible();
  });

  it("rejects changing the MCP slug from the update dialog", async () => {
    const existing = publicAsset({
      assetId: 3,
      versionId: 13,
      currentVersionId: 13,
      assetType: "mcp",
      slug: "governed-mcp",
      name: "Governed MCP",
      summary: { connection_config: { url: "http://10.0.0.8/mcp" } },
    });
    api.publicMcps.mockResolvedValue([existing]);
    render(<AdminDashboardPage />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    await user.click(screen.getByRole("button", { name: /MCP 管理/ }));
    const row = screen.getByText("Governed MCP").closest("article") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "更新配置" }));
    const dialog = await screen.findByRole("dialog", { name: "更新 MCP 配置" });
    fireEvent.change(within(dialog).getByLabelText("更新 mcpServers 配置"), { target: { value: JSON.stringify({
      mcpServers: { renamed: { url: "http://10.0.0.9/mcp" } }
    }) } });
    await user.click(within(dialog).getByRole("button", { name: "确认更新" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("必须保持为 governed-mcp");
    expect(api.updatePublicMcp).not.toHaveBeenCalled();
  });

  it("exposes only the reversible emergency lifecycle for public MCP", async () => {
    const existing = publicAsset({
      assetId: 3,
      currentVersionId: 13,
      assetType: "mcp",
      slug: "governed-mcp",
      name: "Governed MCP",
    });
    api.publicMcps.mockResolvedValue([existing]);

    render(<AdminDashboardPage />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    await user.click(screen.getByRole("button", { name: /MCP 管理/ }));
    const row = screen.getByText("Governed MCP").closest("article") as HTMLElement;
    expect(within(row).queryByRole("button", { name: "正常下架" })).not.toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "紧急停用" })).toBeVisible();
  });

  it("does not offer permanent deletion for a public Skill", async () => {
    const existing = publicAsset({ assetId: 5, name: "Protected Skill", executionStatus: "enabled" });
    api.publicSkillCatalog.mockReset();
    api.publicSkillCatalog.mockResolvedValueOnce([existing]).mockResolvedValue([]);

    render(<AdminDashboardPage />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    const list = screen.getByRole("region", { name: "公共技能列表" });
    const row = within(list).getByText("Protected Skill").closest("article") as HTMLElement;
    expect(within(row).queryByRole("button", { name: "永久删除" })).not.toBeInTheDocument();
  });

  it("publishes and unpublishes a public Skill without execution rollouts", async () => {
    const existing = publicAsset({ assetId: 5, name: "Guarded Skill", executionStatus: "enabled" });
    const unpublished = { ...existing, marketVisible: false };
    api.publicSkillCatalog.mockReset();
    api.publicSkillCatalog.mockResolvedValueOnce([existing]).mockResolvedValue([unpublished]);
    api.unpublishPublicSkill.mockResolvedValue(unpublished);
    api.republishPublicSkill.mockResolvedValue(existing);
    const prompt = vi.spyOn(window, "prompt")
      .mockReturnValueOnce("market paused")
      .mockReturnValueOnce("market restored");

    render(<AdminDashboardPage />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    let row = within(screen.getByRole("region", { name: "公共技能列表" }))
      .getByText("Guarded Skill").closest("article") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "技能下架" }));

    await waitFor(() => expect(api.unpublishPublicSkill).toHaveBeenCalledWith(5, "market paused"));

    row = within(screen.getByRole("region", { name: "公共技能列表" }))
      .getByText("Guarded Skill").closest("article") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "技能发布" }));
    await waitFor(() => expect(api.republishPublicSkill).toHaveBeenCalledWith(5, "market restored", expect.any(String)));
    expect(prompt).toHaveBeenCalledTimes(2);
  });

  it("displays readable upload failures without adding a successful result", async () => {
    api.uploadPublicSkill.mockRejectedValue(new Error("unsafe zip path: ../SKILL.md"));

    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    await user.click(screen.getByRole("button", { name: /^上传技能/ }));
    await user.upload(screen.getByLabelText(/技能 zip/), new File(["zip"], "unsafe.zip", { type: "application/zip" }));
    await user.click(screen.getByRole("button", { name: /确认上传/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("unsafe zip path: ../SKILL.md");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows a card-level version update action for Skill assets", async () => {
    api.publicSkillCatalog.mockResolvedValue([
      publicAsset({ assetId: 5, assetType: "skill", slug: "upgrade-skill", version: "1.0.0", name: "Upgrade Skill", description: "Old description" })
    ]);
    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    const row = within(screen.getByRole("region", { name: "公共技能列表" })).getByText("Upgrade Skill").closest("article") as HTMLElement;

    expect(within(row).getByRole("button", { name: "版本更新" })).toBeVisible();
  });

  it("updates a Skill from its card without choosing a category", async () => {
    const existing = publicAsset({
      assetId: 5,
      slug: "upgrade-skill",
      version: "v1",
      name: "Upgrade Skill",
      categoryKey: "productivity"
    });
    const updated = { ...existing, versionId: 12, currentVersionId: 12, version: "v2", summary: { version_count: 2 } };
    api.publicSkillCatalog.mockReset();
    api.publicSkillCatalog.mockResolvedValueOnce([existing]).mockResolvedValueOnce([updated]);
    api.uploadPublicSkillVersion.mockResolvedValue(updated);
    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    const row = within(screen.getByRole("region", { name: "公共技能列表" })).getByText("Upgrade Skill").closest("article") as HTMLElement;
    expect(within(row).getAllByRole("button").slice(-3).map((button) => button.textContent?.trim())).toEqual([
      "设置分类",
      "版本更新",
      "技能下架"
    ]);

    await user.click(within(row).getByRole("button", { name: "版本更新" }));
    const dialog = await screen.findByRole("dialog", { name: "更新「Upgrade Skill」" });
    expect(within(dialog).getByText("当前版本：v1 · 当前分类：生产力")).toBeVisible();
    expect(within(dialog).queryByLabelText("初始分类")).not.toBeInTheDocument();
    await user.upload(within(dialog).getByLabelText("技能 zip"), new File(["v2"], "upgrade-v2.zip", { type: "application/zip" }));
    await user.click(within(dialog).getByRole("button", { name: "确认更新" }));

    await waitFor(() => expect(api.uploadPublicSkillVersion).toHaveBeenCalledWith(5, expect.any(File)));
    expect(api.uploadPublicSkillVersion.mock.calls[0][1].name).toBe("upgrade-v2.zip");
    expect(await screen.findByRole("status")).toHaveTextContent("版本更新成功");
  });

  it("edits a Skill category inline without using a browser prompt", async () => {
    const existing = publicAsset({
      assetId: 11,
      assetType: "skill",
      slug: "category-skill",
      version: "v1",
      name: "Category Skill",
      categoryKey: "productivity",
      status: "published"
    });
    const updated = { ...existing, categoryKey: "research" };
    api.publicSkillCatalog.mockReset();
    api.publicSkillCatalog.mockResolvedValueOnce([existing]).mockResolvedValueOnce([updated]);
    api.updatePublicSkillCategory.mockResolvedValue(updated);
    const promptSpy = vi.spyOn(window, "prompt");
    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    const row = within(screen.getByRole("region", { name: "公共技能列表" })).getByText("Category Skill").closest("article") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "设置分类" }));
    expect(within(row).getByLabelText("技能分类")).toBeVisible();

    await user.selectOptions(within(row).getByLabelText("技能分类"), "research");
    await user.click(within(row).getByRole("button", { name: "保存分类" }));

    await waitFor(() => expect(api.updatePublicSkillCategory).toHaveBeenCalledWith(11, "research"));
    expect(promptSpy).not.toHaveBeenCalled();
    expect(await screen.findByText(/分类：研究/)).toBeVisible();
    promptSpy.mockRestore();
  });

  it("creates and selects a new organization Skill category inline", async () => {
    const existing = publicAsset({ assetId: 12, name: "Custom Category Skill" });
    api.publicSkillCatalog.mockResolvedValue([existing]);
    api.createSkillCategory.mockResolvedValue({
      categoryKey: "category-writing",
      displayName: "写作",
      isBuiltin: false,
    });
    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    const row = screen.getByText("Custom Category Skill").closest("article") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "设置分类" }));
    await user.type(within(row).getByRole("textbox", { name: "新分类名称" }), "写作");
    await user.click(within(row).getByRole("button", { name: "新建分类" }));

    await waitFor(() => expect(api.createSkillCategory).toHaveBeenCalledWith({ displayName: "写作" }));
    expect(within(row).getByLabelText("技能分类")).toHaveValue("category-writing");
  });

  it("filters public Skills by non-empty category tabs and keeps an all tab", async () => {
    api.skillCategories.mockResolvedValue([
      { categoryKey: "productivity", displayName: "生产力", isBuiltin: true, isVisible: true },
      { categoryKey: "research", displayName: "研究", isBuiltin: true, isVisible: true },
      { categoryKey: "creative", displayName: "创作", isBuiltin: true, isVisible: true }
    ]);
    api.publicSkillCatalog.mockResolvedValue([
      publicAsset({ assetId: 21, name: "Revenue Skill", slug: "revenue-skill", categoryKey: "productivity" }),
      publicAsset({ assetId: 22, name: "Research Skill", slug: "research-skill", categoryKey: "research" })
    ]);
    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    const categoryTabs = screen.getByRole("group", { name: "技能分类" });
    expect(within(categoryTabs).getByRole("button", { name: /^全部/ })).toHaveAttribute("aria-pressed", "true");
    expect(within(categoryTabs).getByRole("button", { name: /^生产力/ })).toBeVisible();
    expect(within(categoryTabs).getByRole("button", { name: /^研究/ })).toBeVisible();
    expect(within(categoryTabs).queryByRole("button", { name: /^创作/ })).not.toBeInTheDocument();

    await user.click(within(categoryTabs).getByRole("button", { name: /^研究/ }));
    const list = screen.getByRole("region", { name: "公共技能列表" });
    expect(within(list).getByText("Research Skill")).toBeVisible();
    expect(within(list).queryByText("Revenue Skill")).not.toBeInTheDocument();

    await user.click(within(categoryTabs).getByRole("button", { name: /^全部/ }));
    expect(within(list).getByText("Revenue Skill")).toBeVisible();
    expect(within(list).getByText("Research Skill")).toBeVisible();
  });

  it("does not offer manual Skill version publishing", async () => {
    const current = publicAsset({
      assetId: 15,
      versionId: 101,
      assetType: "skill",
      slug: "versioned-skill",
      version: "v1",
      name: "Versioned Skill",
      currentVersionId: 101
    });
    const draft = publicAsset({
      assetId: 15,
      versionId: 102,
      assetType: "skill",
      slug: "versioned-skill",
      version: "v2",
      name: "Versioned Skill",
      currentVersionId: 101
    });
    api.publicSkillCatalog.mockReset();
    api.publicSkillCatalog.mockResolvedValueOnce([current, draft]);
    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    expect(screen.getByText("Versioned Skill")).toBeVisible();
  });

  it("presents market versions as read-only audit cards", async () => {
    const current = publicAsset({
      assetId: 15,
      versionId: 101,
      assetType: "skill",
      slug: "versioned-skill",
      version: "2.0.0",
      name: "Versioned Skill",
      currentVersionId: 101
    });
    const historical = publicAsset({
      assetId: 15,
      versionId: 100,
      assetType: "skill",
      slug: "versioned-skill",
      version: "1.0.0",
      name: "Versioned Skill",
      currentVersionId: 101
    });
    api.publicSkillCatalog.mockResolvedValue([current]);
    api.publicSkillVersions.mockResolvedValue([current, historical]);
    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    await user.click(screen.getByRole("button", { name: /Versioned Skill/ }));
    const dialog = await screen.findByRole("dialog", { name: "Versioned Skill 详情" });

    expect(within(dialog).getByRole("heading", { name: "市场版本" })).toBeVisible();
    expect(within(dialog).getByText("当前市场版本")).toBeVisible();
    expect(within(dialog).getByText("历史版本")).toBeVisible();
    expect(within(dialog).queryByRole("combobox", { name: "当前市场版本" })).not.toBeInTheDocument();

    expect(within(dialog).getByText("仅保留审计记录")).toBeVisible();
    expect(within(dialog).queryByRole("button", { name: "将 v1.0.0 设为当前市场版本" })).not.toBeInTheDocument();
  });

  it("keeps the Skill upload close button in its dedicated no-wrap layout", async () => {
    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    await user.click(screen.getByRole("button", { name: /^上传技能/ }));
    const dialog = await screen.findByRole("dialog", { name: "上传公共技能" });

    expect(within(dialog).getByRole("button", { name: "关闭" })).toHaveClass("adminModalCloseButton");
  });

  it("rejects multiple mcpServers entries before calling the API", async () => {
    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    await user.click(screen.getByRole("button", { name: /MCP 管理/ }));
    await user.click(screen.getByRole("button", { name: "创建 MCP" }));
    const dialog = await screen.findByRole("dialog", { name: "创建公共 MCP" });
    await user.type(within(dialog).getByLabelText("名称"), "Too many MCPs");
    fireEvent.change(within(dialog).getByLabelText("mcpServers 配置"), { target: { value: JSON.stringify({
      mcpServers: { one: { url: "http://10.0.0.1/mcp" }, two: { url: "http://10.0.0.2/mcp" } }
    }) } });
    await user.click(within(dialog).getByRole("button", { name: "确认创建" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("mcpServers 必须只包含一个条目");
    expect(api.createPublicMcp).not.toHaveBeenCalled();
  });

  it("clears the selected Skill file and uploads ZIP-only new content", async () => {
    const existing = publicAsset({
      assetId: 7,
      assetType: "skill",
      slug: "selected-skill",
      version: "1.0.0",
      name: "Selected Skill",
      description: "Existing skill"
    });
    const uploaded = { ...existing, assetId: 8, version: "2.0.0", summary: { fileCount: 3 } };
    api.publicSkillCatalog.mockReset();
    api.publicSkillCatalog.mockResolvedValueOnce([existing]).mockResolvedValueOnce([existing, uploaded]);
    api.uploadPublicSkill.mockResolvedValue(uploaded);
    render(<AdminDashboardPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /广场资产/ }));
    await user.click(screen.getByRole("button", { name: /^上传技能/ }));
    await user.upload(screen.getByLabelText(/技能 zip/), new File(["old"], "old.zip", { type: "application/zip" }));
    await user.upload(screen.getByLabelText(/技能 zip/), new File(["new"], "new.zip", { type: "application/zip" }));

    await user.click(screen.getByRole("button", { name: /确认上传/ }));
    await waitFor(() => expect(api.uploadPublicSkill).toHaveBeenCalledWith({
      file: expect.any(File),
      categoryKey: "productivity",
      categoryDisplayName: null
    }));
    expect(api.uploadPublicSkill.mock.calls[0][0].file.name).toBe("new.zip");
    expect(await screen.findByRole("status")).toHaveTextContent("技能 / selected-skill / 2.0.0");
    expect(screen.queryByText("已选择 Selected Skill，上传同名技能 zip 会新增一个公共版本草稿。")).not.toBeInTheDocument();
  });

});
