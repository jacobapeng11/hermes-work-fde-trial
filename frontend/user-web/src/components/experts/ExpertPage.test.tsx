import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

import { ExpertPage } from "./ExpertPage";

// antd Tree 契约：节点勾选点击 .ant-tree-checkbox（无 role name），同看板区先例。
async function selectWholeOrganization(dialog: HTMLElement, user: ReturnType<typeof userEvent.setup>) {
  await user.click(within(dialog).getByRole("button", { name: "添加可见对象" }));
  const visibilityDialog = await screen.findByRole("dialog", { name: "添加可见对象" });
  await waitFor(() => expect(visibilityDialog).toBeVisible());
  const rootRow = within(visibilityDialog).getByText("示例业务技术").closest(".ant-tree-treenode");
  fireEvent.click(rootRow?.querySelector(".ant-tree-checkbox") as HTMLElement);
  await user.click(within(visibilityDialog).getByRole("button", { name: "确认" }));
}

const expert = {
  runtimeAgentProfileId: 7,
  runtimeWorkspaceId: 1,
  hermesProfileName: "expert-7",
  profileKind: "expert" as const,
  displayName: "写作专家",
  description: "公众号文章写作",
  avatarUrl: null,
  soulContent: "# 写作专家",
  ownershipType: "self_created",
  inheritedFromProfileId: null,
  expertPackageId: 17,
  installedVersionId: null,
  availableVersionId: 3,
  updateAvailable: false,
  capabilities: {
    skillIds: [],
    mcpIds: [],
    knowledgeBaseIds: [],
    enterpriseKnowledgeEnabled: false,
    pluginIds: []
  },
  shareScope: { organization: false, userIds: [], departmentIds: [] },
  shareScopeUsers: [],
  reviewId: null,
  reviewStatus: null,
  reviewComment: null,
  allowedActions: {
    viewDetail: true,
    install: false,
    uninstall: false,
    summon: true,
    edit: true,
    copy: true,
    publish: true,
    unlist: false,
    delete: true
  },
  status: "active",
  lastError: null
};

const installableMarketContract = {
  providerName: "专家市场",
  installed: false,
  catalogStatus: "published" as const,
  executionStatus: "enabled" as const,
  allowedActions: {
    viewDetail: true,
    install: true,
    uninstall: false,
    summon: false,
    edit: false,
    copy: false,
    publish: false,
    unlist: false,
    delete: false
  }
};

describe("ExpertPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    api.expertMarket.mockResolvedValue([]);
    api.marketExpertDetail.mockResolvedValue(null);
    api.avatarAssets.mockResolvedValue([]);
    api.myExperts.mockResolvedValue([expert]);
    api.runnableExperts.mockResolvedValue([expert]);
    api.expertDetail.mockResolvedValue(expert);
    api.expertInheritanceSources.mockResolvedValue([
      {
        ...expert,
        runtimeAgentProfileId: 1,
        hermesProfileName: "default",
        profileKind: "default",
        displayName: "自己",
        ownershipType: "default"
      }
    ]);
    api.expertCapabilityCatalog.mockResolvedValue({
      knowledgeBases: [],
      marketSkills: [],
      installedSkills: [{ assetId: 41, versionId: 91, name: "我的写作技能", description: "个人上传", source: "default_installed", categoryKey: "personal" }],
      skillCategories: [{ categoryKey: "personal", displayName: "我的技能" }],
      marketMcps: [],
      installedMcps: [],
      personalSkills: [],
      personalKnowledgeBases: [{ assetId: 51, name: "我的资料库", description: "私人资料" }],
      pluginIds: ["enterprise-knowledge", "personal-knowledge"]
    });
    api.organizationDirectoryTree.mockResolvedValue([{
      nodeType: "organization",
      organizationId: 1,
      name: "示例业务技术",
      children: []
    }]);
    api.searchDirectoryUsers.mockResolvedValue([]);
    api.createExpert.mockResolvedValue(expert);
    api.copyExpert.mockResolvedValue({ ...expert, runtimeAgentProfileId: 8, displayName: "写作专家-副本" });
    api.installMarketExpert.mockResolvedValue(expert);
    api.uploadPersonalSkill.mockResolvedValue({
      skillId: 61,
      skillVersionId: 81,
      slug: "personal-writing",
      version: 1,
      name: "个人写作 Skill",
      status: "private",
      source: "personal_asset",
      applied: false
    });
    api.saveExpertConfiguration.mockResolvedValue(expert);
    api.uninstallExpert.mockResolvedValue(undefined);
  });

  it("loads market and installed experts without changing other modules", async () => {
    const onInstalledChange = vi.fn();
    const onStartChat = vi.fn();
    render(
      <ExpertPage
        runtimeWorkspaceId={1}
        onStartChat={onStartChat}
        onInstalledChange={onInstalledChange}
      />
    );

    expect(await screen.findByRole("region", { name: "专家" })).toBeVisible();
    expect(screen.queryByText("让不同的 Agent 专注完成擅长的工作")).not.toBeInTheDocument();
    await waitFor(() => expect(api.expertMarket).toHaveBeenCalledWith(1));
    expect(onInstalledChange).toHaveBeenCalledWith([expert]);

    await userEvent.setup().click(screen.getByRole("tab", { name: /我的/ }));
    expect(await screen.findByText("写作专家")).toBeVisible();
    const card = screen.getByRole("heading", { name: "写作专家" }).closest("article");
    expect(card).not.toBeNull();
    const actions = card?.querySelector(".expertInstalledActions");
    expect(actions).not.toBeNull();
    expect(within(actions as HTMLElement).getByRole("button", { name: "召唤" })).toBeVisible();
    expect(within(actions as HTMLElement).getByRole("button", { name: /^配置/ })).toBeVisible();
    expect(within(card as HTMLElement).getByRole("button", { name: "删除 写作专家" })).toBeVisible();
    expect(within(actions as HTMLElement).queryByRole("button", { name: "对话" })).not.toBeInTheDocument();

    await userEvent.setup().click(within(actions as HTMLElement).getByRole("button", { name: "召唤" }));
    expect(onStartChat).toHaveBeenCalledWith(expert);
  });

  it("refreshes the matching expert list whenever a market or installed tab is clicked", async () => {
    const user = userEvent.setup();
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await screen.findByRole("region", { name: "专家" });
    await waitFor(() => {
      expect(api.expertMarket).toHaveBeenCalledTimes(1);
      expect(api.myExperts).toHaveBeenCalledTimes(1);
    });

    await user.click(screen.getByRole("tab", { name: /^专家市场/ }));
    await waitFor(() => expect(api.expertMarket).toHaveBeenCalledTimes(2));

    await user.click(screen.getByRole("tab", { name: /我的/ }));
    await waitFor(() => expect(api.myExperts).toHaveBeenCalledTimes(2));

    await user.click(screen.getByRole("tab", { name: /我的/ }));
    await waitFor(() => expect(api.myExperts).toHaveBeenCalledTimes(3));
  });

  it("shows automatic expert convergence without a manual update action", async () => {
    const updateAvailableExpert = {
      ...expert,
      ownershipType: "market_installed",
      expertPackageId: 17,
      installedVersionId: 31,
      installedVersion: 1,
      availableVersionId: 32,
      updateAvailable: true,
      desiredRevision: 2,
      appliedRevision: 1,
      configuredExpertVersionId: 32,
      appliedExpertVersionId: 31,
      convergenceStatus: "updating",
      effectiveStatus: "updating"
    };
    const marketExpert = {
      ...installableMarketContract,
      templateId: 31,
      expertPackageId: 17,
      expertPackageVersionId: 32,
      name: "写作专家",
      description: "公众号文章写作",
      avatarUrl: null,
      authorUserId: 2,
      version: 2,
      capabilities: expert.capabilities,
      installedRuntimeAgentProfileId: 7,
      installed: true,
      allowedActions: { ...installableMarketContract.allowedActions, install: false, uninstall: true, summon: true },
      state: "update_available" as const,
      updatedAt: null
    };
    api.expertMarket.mockResolvedValue([marketExpert]);
    api.myExperts.mockResolvedValue([]);
    api.runnableExperts.mockResolvedValue([updateAvailableExpert]);
    api.marketExpertDetail.mockResolvedValue({
      ...installableMarketContract,
      expertPackageId: 17,
      expertPackageVersionId: 32,
      name: "写作专家",
      description: "公众号文章写作",
      avatarUrl: null,
      version: 2,
      installedRuntimeAgentProfileId: 7,
      soulContent: "",
      systemPrompt: null,
      skills: [],
      mcps: [],
      knowledgeBases: []
    });
    const user = userEvent.setup();
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await user.click(screen.getByRole("tab", { name: /^专家市场/ }));

    const marketCard = (await screen.findByRole("heading", { name: "写作专家" })).closest("article");
    expect(marketCard).not.toBeNull();
    expect(within(marketCard as HTMLElement).getByText(/提供 · v2/)).toBeVisible();
    expect(marketCard?.querySelector(".expertVersionBadge")).toBeNull();
    expect(marketCard?.querySelector(".expertUpdateButton")).toBeNull();

    await user.click(marketCard as HTMLElement);
    const dialog = await screen.findByRole("dialog", { name: /写作专家/ });
    expect(dialog.querySelector(".expertUpdateButton")).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "关闭" }));

    expect(within(marketCard as HTMLElement).getByRole("button", { name: "召唤" })).toBeVisible();
    expect(within(marketCard as HTMLElement).getByRole("button", { name: "卸载" })).toBeVisible();
    expect(screen.getByRole("tab", { name: /我的 0/ })).toBeVisible();
  });

  it("renders the market without waiting for configuration data or chat expert synchronization", async () => {
    let resolveCatalog: ((value: unknown) => void) | undefined;
    let resolveSources: ((value: unknown) => void) | undefined;
    let resolveSync: (() => void) | undefined;
    api.expertMarket.mockResolvedValue([
      {
        ...installableMarketContract,
        templateId: 31,
        expertPackageId: 17,
        expertPackageVersionId: 3,
        name: "首屏市场专家",
        description: "不应等待配置资料",
        avatarUrl: null,
        authorUserId: 2,
        version: 3,
        capabilities: expert.capabilities,
        installedRuntimeAgentProfileId: null,
        state: "installable",
        updatedAt: null
      }
    ]);
    api.expertInheritanceSources.mockImplementation(() => new Promise((resolve) => {
      resolveSources = resolve;
    }));
    api.expertCapabilityCatalog.mockImplementation(() => new Promise((resolve) => {
      resolveCatalog = resolve;
    }));
    const onInstalledChange = vi.fn(() => new Promise<void>((resolve) => {
      resolveSync = resolve;
    }));

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()} onInstalledChange={onInstalledChange}/>);
    await userEvent.setup().click(screen.getByRole("tab", { name: /^专家市场/ }));

    expect(await screen.findByRole("heading", { name: "首屏市场专家" })).toBeVisible();
    expect(api.expertInheritanceSources).not.toHaveBeenCalled();
    expect(api.expertCapabilityCatalog).not.toHaveBeenCalled();
    expect(screen.queryByText("正在加载专家…")).not.toBeInTheDocument();
    expect(onInstalledChange).toHaveBeenCalledWith([expert]);

    resolveSync?.();
    resolveCatalog?.({
      marketSkills: [], installedSkills: [], skillCategories: [],
      marketMcps: [], installedMcps: [], knowledgeBases: [],
      personalSkills: [], personalKnowledgeBases: [], pluginIds: []
    });
    resolveSources?.([]);
  });

  it("keeps the market usable when installed experts fail and retries only that tab", async () => {
    api.expertMarket.mockResolvedValue([
      {
        ...installableMarketContract,
        templateId: 31,
        expertPackageId: 17,
        expertPackageVersionId: 3,
        name: "市场仍可浏览",
        description: "已安装列表失败不影响市场",
        avatarUrl: null,
        authorUserId: 2,
        version: 3,
        capabilities: expert.capabilities,
        installedRuntimeAgentProfileId: null,
        state: "installable",
        updatedAt: null
      }
    ]);
    api.myExperts
      .mockRejectedValueOnce(new Error("installed unavailable"))
      .mockRejectedValueOnce(new Error("installed unavailable"));
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await userEvent.setup().click(screen.getByRole("tab", { name: /^专家市场/ }));

    expect(await screen.findByRole("heading", { name: "市场仍可浏览" })).toBeVisible();
    await userEvent.setup().click(screen.getByRole("tab", { name: /我的/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("我的专家加载失败：installed unavailable");
    await userEvent.setup().click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("heading", { name: "写作专家" })).toBeVisible();
  });

  it("loads configuration only after opening a dialog and reuses it until a mutation invalidates it", async () => {
    api.expertMarket.mockResolvedValue([
      {
        ...installableMarketContract,
        templateId: 31,
        expertPackageId: 17,
        expertPackageVersionId: 3,
        name: "待安装专家",
        description: "验证配置缓存失效",
        avatarUrl: null,
        authorUserId: 2,
        version: 3,
        capabilities: expert.capabilities,
        installedRuntimeAgentProfileId: null,
        state: "installable",
        updatedAt: null
      }
    ]);
    const user = userEvent.setup();
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await user.click(screen.getByRole("tab", { name: /^专家市场/ }));
    await screen.findByRole("heading", { name: "待安装专家" });
    expect(api.expertCapabilityCatalog).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "创建专家" }));
    await waitFor(() => expect(api.expertCapabilityCatalog).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "取消" }));
    await user.click(screen.getByRole("button", { name: "创建专家" }));
    expect(api.expertCapabilityCatalog).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "取消" }));

    await user.click(screen.getByRole("button", { name: "安装" }));
    await waitFor(() => expect(api.installMarketExpert).toHaveBeenCalledWith(1, 17));
    await user.click(screen.getByRole("button", { name: "创建专家" }));
    await waitFor(() => expect(api.expertCapabilityCatalog).toHaveBeenCalledTimes(2));
    expect(api.expertInheritanceSources).not.toHaveBeenCalled();
  });

  it("keeps the single-page editor usable when the capability catalog fails and retries it", async () => {
    api.expertCapabilityCatalog
      .mockRejectedValueOnce(new Error("catalog unavailable"))
      .mockResolvedValueOnce({
        knowledgeBases: [],
        marketSkills: [],
        installedSkills: [],
        skillCategories: [],
        marketMcps: [],
        installedMcps: [],
        personalSkills: [],
        personalKnowledgeBases: [],
        pluginIds: []
      });
    const user = userEvent.setup();
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await user.click(await screen.findByRole("button", { name: "创建专家" }));
    const dialog = screen.getByRole("dialog", { name: /^创建专家/ });
    // antd Modal appear 阶段内容 opacity:0，统一等弹窗可见后再断言。
    await waitFor(() => expect(dialog).toBeVisible());
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "能力目录加载失败：catalog unavailable"
    );
    await user.click(within(dialog).getByRole("button", { name: "重试能力目录" }));

    await waitFor(() => expect(api.expertCapabilityCatalog).toHaveBeenCalledTimes(2));
    expect(api.expertInheritanceSources).not.toHaveBeenCalled();
    await waitFor(() => expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument());
  });

  it("ignores a stale market response after the runtime workspace changes", async () => {
    let resolveFirstMarket: ((value: unknown) => void) | undefined;
    api.expertMarket.mockImplementation((workspaceId: number) => workspaceId === 1
      ? new Promise((resolve) => { resolveFirstMarket = resolve; })
      : Promise.resolve([
        {
          ...installableMarketContract,
          templateId: 32,
          expertPackageId: 18,
          expertPackageVersionId: 1,
          name: "工作区二专家",
          description: "应保留最新工作区的数据",
          avatarUrl: null,
          authorUserId: 2,
          version: 1,
          capabilities: expert.capabilities,
          installedRuntimeAgentProfileId: null,
          state: "installable",
          updatedAt: null
        }
      ]));
    const rendered = render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await waitFor(() => expect(api.expertMarket).toHaveBeenCalledWith(1));
    rendered.rerender(<ExpertPage runtimeWorkspaceId={2} onStartChat={vi.fn()}/>);
    await userEvent.setup().click(screen.getByRole("tab", { name: /^专家市场/ }));

    expect(await screen.findByRole("heading", { name: "工作区二专家" })).toBeVisible();
    resolveFirstMarket?.([
      {
        ...installableMarketContract,
        templateId: 31,
        expertPackageId: 17,
        expertPackageVersionId: 3,
        name: "过期工作区专家",
        description: "不应覆盖工作区二",
        avatarUrl: null,
        authorUserId: 2,
        version: 3,
        capabilities: expert.capabilities,
        installedRuntimeAgentProfileId: null,
        state: "installable",
        updatedAt: null
      }
    ]);
    await waitFor(() => expect(screen.queryByRole("heading", { name: "过期工作区专家" })).not.toBeInTheDocument());
  });

  it("closes scoped editors and does not leak a create draft across workspaces", async () => {
    const user = userEvent.setup();
    const rendered = render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await user.click(await screen.findByRole("button", { name: "创建专家" }));
    const firstDialog = screen.getByRole("dialog", { name: /^创建专家/ });
    // antd Modal appear 阶段内容 opacity:0，统一等弹窗可见后再断言。
    await waitFor(() => expect(firstDialog).toBeVisible());
    await user.type(within(firstDialog).getByLabelText("名称（必填）"), "工作区一草稿");
    await waitFor(() => expect(window.localStorage.getItem("hermes.expert-create-draft.v1:1"))
      .toContain("工作区一草稿"));

    rendered.rerender(<ExpertPage runtimeWorkspaceId={2} onStartChat={vi.fn()}/>);

    await waitFor(() => expect(screen.queryByRole("dialog", { name: /^创建专家/ })).not.toBeInTheDocument());
    expect(window.localStorage.getItem("hermes.expert-create-draft.v1:2")).toBeNull();
    await user.click(screen.getByRole("button", { name: "创建专家" }));
    expect(within(screen.getByRole("dialog", { name: /^创建专家/ }))
      .getByLabelText("名称（必填）")).toHaveValue("");
  });

  it("marks the creator's published expert as owned without installing it again", async () => {
    api.expertMarket.mockResolvedValue([
      {
        ...installableMarketContract,
        templateId: 31,
        expertPackageId: 17,
        expertPackageVersionId: 3,
        name: "写作专家",
        description: "公众号文章写作",
        avatarUrl: null,
        authorUserId: 1,
        version: 3,
        capabilities: expert.capabilities,
        installedRuntimeAgentProfileId: 7,
        allowedActions: { ...installableMarketContract.allowedActions, install: false },
        state: "owned",
        updatedAt: null
      },
      {
        ...installableMarketContract,
        templateId: 32,
        expertPackageId: 18,
        expertPackageVersionId: 1,
        name: "市场研究专家",
        description: "市场趋势研究",
        avatarUrl: null,
        authorUserId: 2,
        version: 1,
        capabilities: expert.capabilities,
        installedRuntimeAgentProfileId: null,
        state: "installable",
        updatedAt: null
      }
    ]);
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await userEvent.setup().click(screen.getByRole("tab", { name: /^专家市场/ }));

    const ownedCard = (await screen.findByRole("heading", {
      name: "写作专家"
    })).closest("article");
    expect(ownedCard).not.toBeNull();
    const ownedAction = within(ownedCard as HTMLElement).getByRole("button", {
      name: "我创建的"
    });
    expect(ownedAction).toBeDisabled();
    expect(within(ownedCard as HTMLElement).queryByRole("button", {
      name: "安装"
    })).not.toBeInTheDocument();

    const installableCard = screen.getByRole("heading", {
      name: "市场研究专家"
    }).closest("article");
    expect(installableCard).not.toBeNull();
    await userEvent.setup().click(
      within(installableCard as HTMLElement).getByRole("button", {
        name: "安装"
      })
    );
    await waitFor(() => {
      expect(api.installMarketExpert).toHaveBeenCalledWith(1, 18);
    });
  });

  it("loads frozen capability details only after opening a market expert", async () => {
    api.expertMarket.mockResolvedValue([
      {
        ...installableMarketContract,
        templateId: 31,
        expertPackageId: 17,
        expertPackageVersionId: 3,
        name: "市场写作专家",
        description: "安装前可查看能力",
        avatarUrl: null,
        authorUserId: 2,
        version: 3,
        capabilities: expert.capabilities,
        installedRuntimeAgentProfileId: null,
        state: "installable",
        updatedAt: null
      }
    ]);
    api.marketExpertDetail.mockResolvedValue({
      ...installableMarketContract,
      expertPackageId: 17,
      expertPackageVersionId: 3,
      name: "市场写作专家",
      description: "安装前可查看能力",
      avatarUrl: null,
      version: 3,
      installedRuntimeAgentProfileId: null,
      soulContent: "# 冻结市场人格",
      systemPrompt: "冻结市场系统提示词",
      skills: [{ name: "冻结写作 Skill", description: "按发布版本写作" }],
      mcps: [{ name: "冻结搜索 MCP", description: "检索资料" }],
      knowledgeBases: [{ name: "冻结品牌知识库", description: "品牌资料" }]
    });
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await userEvent.setup().click(screen.getByRole("tab", { name: /^专家市场/ }));

    expect(api.marketExpertDetail).not.toHaveBeenCalled();
    const card = (await screen.findByRole("heading", { name: "市场写作专家" })).closest("article");
    expect(card).not.toBeNull();
    await userEvent.setup().click(card as HTMLElement);

    const dialog = await screen.findByRole("dialog", { name: "市场写作专家" });
    await waitFor(() => expect(dialog).toBeVisible());
    expect(api.marketExpertDetail).toHaveBeenCalledWith(1, 17);
    expect(within(dialog).getByText(/创建者授权公开的当前市场版本配置/)).toBeVisible();
    expect(within(dialog).getByText(/冻结市场人格/)).toBeVisible();
    expect(within(dialog).getByText("冻结市场系统提示词")).toBeVisible();
    expect(within(dialog).getByText("冻结写作 Skill")).toBeVisible();
  });

  it("does not open market detail when installing from a card", async () => {
    api.expertMarket.mockResolvedValue([
      {
        ...installableMarketContract,
        templateId: 31,
        expertPackageId: 17,
        expertPackageVersionId: 3,
        name: "直接安装专家",
        description: null,
        avatarUrl: null,
        authorUserId: 2,
        version: 3,
        capabilities: expert.capabilities,
        installedRuntimeAgentProfileId: null,
        state: "installable",
        updatedAt: null
      }
    ]);
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await userEvent.setup().click(screen.getByRole("tab", { name: /^专家市场/ }));
    await userEvent.setup().click(await screen.findByRole("button", { name: "安装" }));

    await waitFor(() => expect(api.installMarketExpert).toHaveBeenCalledWith(1, 17));
    expect(api.marketExpertDetail).not.toHaveBeenCalled();
  });

  it("creates a blank Profile expert through the single-page editor", async () => {
    render(
      <ExpertPage
        runtimeWorkspaceId={1}
        onStartChat={vi.fn()}
      />
    );
    await screen.findByRole("region", { name: "专家" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "创建专家" }));
    const dialog = screen.getByRole("dialog", { name: /^创建专家/ });
    // antd Modal appear 阶段内容 opacity:0，统一等弹窗可见后再断言。
    await waitFor(() => expect(dialog).toBeVisible());

    expect(within(dialog).getByRole("heading", { name: "基础信息" })).toBeVisible();
    expect(within(dialog).getByRole("heading", { name: "可见范围" })).toBeVisible();
    expect(within(dialog).queryByText("继承设置")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "选择默认头像" }));
    const firstAvatar = within(dialog).getByRole("radio", { name: "默认头像 1" });
    expect(firstAvatar).toBeVisible();
    await user.click(firstAvatar);
    expect(within(dialog).queryByText(/头像 URL/)).not.toBeInTheDocument();

    await user.type(within(dialog).getByPlaceholderText("例如：微信公众号文章专家"), "微信文章专家");
    await user.type(within(dialog).getByRole("textbox", { name: "专家个性" }), "# 写作人格");
    await selectWholeOrganization(dialog, user);
    await user.click(within(dialog).getByRole("button", { name: /从技能市场选择/ }));
    const skillPicker = screen.getByRole("dialog", { name: "选择技能" });
    await user.click(within(skillPicker).getByRole("checkbox", { name: /我的写作技能/ }));
    await user.click(within(skillPicker).getByRole("button", { name: "确认选择" }));
    const createKnowledgeSection = within(dialog).getByRole("heading", { name: "知识库" }).closest(".ant-card");
    await user.click(within(createKnowledgeSection as HTMLElement).getByRole("button", { name: /选择知识库/ }));
    const knowledgePicker = screen.getByRole("dialog", { name: "选择知识库" });
    const enterpriseKnowledge = within(knowledgePicker).getByRole("checkbox", {
      name: "企业知识库"
    });
    expect(enterpriseKnowledge).not.toBeChecked();
    await user.click(enterpriseKnowledge);
    await user.click(within(knowledgePicker).getByRole("tab", { name: "个人知识库" }));
    await user.click(within(knowledgePicker).getByRole("checkbox", { name: /我的资料库/ }));
    await user.click(within(knowledgePicker).getByRole("button", { name: "确认选择" }));
    await user.click(within(dialog).getByRole("button", { name: "创建专家" }));

    await waitFor(() => expect(api.createExpert).toHaveBeenCalledWith(
      expect.objectContaining({
        runtimeWorkspaceId: 1,
        name: "微信文章专家",
        soulContent: "# 写作人格",
        shareScope: { organization: true, userIds: [], departmentIds: [] },
        capabilities: expect.objectContaining({
          enterpriseKnowledgeEnabled: true,
          skills: [{ source: "default_installed", assetId: 41, versionId: 91 }],
          knowledgeBases: [{ source: "personal", assetId: 51 }]
        })
      })
    ));
  });

  it("requires a name before creating from the single-page editor", async () => {
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "创建专家" }));
    const dialog = screen.getByRole("dialog", { name: /^创建专家/ });
    // antd Modal appear 阶段内容 opacity:0，统一等弹窗可见后再断言。
    await waitFor(() => expect(dialog).toBeVisible());
    const body = dialog.querySelector<HTMLElement>(".ant-drawer-body");
    expect(body).not.toBeNull();
    if (body) body.scrollTop = 420;

    await user.click(within(dialog).getByRole("button", { name: "创建专家" }));

    const alert = within(dialog).getByRole("alert");
    expect(alert).toHaveTextContent("请填写专家名称");
    expect(alert).toHaveClass("expert-drawer-error");
    // 抽屉形态：error 常驻 body 顶部，滚动位置不被重置。
    expect(body?.scrollTop).toBe(420);
    expect(within(dialog).getByRole("heading", { name: "基础信息" })).toBeVisible();
    await waitFor(() => expect(within(dialog).getByPlaceholderText("例如：微信公众号文章专家")).toHaveFocus());
  });

  it("confirms before closing a changed create draft and restores it next time", async () => {
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "创建专家" }));
    const dialog = screen.getByRole("dialog", { name: /^创建专家/ });
    // antd Modal appear 阶段内容 opacity:0，统一等弹窗可见后再断言。
    await waitFor(() => expect(dialog).toBeVisible());
    await user.type(within(dialog).getByPlaceholderText("例如：微信公众号文章专家"), "稍后继续专家");

    await user.click(within(dialog).getByRole("button", { name: "取消" }));
    const confirmation = screen.getByRole("dialog", { name: "关闭专家编辑器" });
    await waitFor(() => expect(within(confirmation).getByText(/草稿已自动保存在当前浏览器/)).toBeVisible());
    await user.click(within(confirmation).getByRole("button", { name: "继续编辑" }));
    expect(screen.getByRole("dialog", { name: /^创建专家/ })).toBeVisible();

    await user.click(within(dialog).getByRole("button", { name: "取消" }));
    await user.click(within(screen.getByRole("dialog", { name: "关闭专家编辑器" }))
      .getByRole("button", { name: "确认关闭" }));
    expect(screen.queryByRole("dialog", { name: /^创建专家/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "创建专家" }));
    expect(screen.getByPlaceholderText("例如：微信公众号文章专家")).toHaveValue("稍后继续专家");
  });

  it("refreshes the authoritative error Profile after create fails and keeps the draft", async () => {
    api.myExperts
      .mockResolvedValueOnce([])
      .mockResolvedValue([{
        ...expert,
        displayName: "失败专家",
        desiredRevision: 1,
        appliedRevision: 0,
        convergenceStatus: "error",
        allowedActions: { ...expert.allowedActions, summon: false },
        lastError: "files projection failed",
        lastOperationAction: "create",
        lastOperationStatus: "failed"
      }]);
    api.createExpert.mockRejectedValue(new Error("create request failed"));
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "创建专家" }));
    const dialog = screen.getByRole("dialog", { name: /^创建专家/ });
    // antd Modal appear 阶段内容 opacity:0，统一等弹窗可见后再断言。
    await waitFor(() => expect(dialog).toBeVisible());
    const name = within(dialog).getByPlaceholderText("例如：微信公众号文章专家");

    await user.type(name, "失败专家");
    await user.click(within(dialog).getByRole("button", { name: "创建专家" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "create request failed"
    );
    expect(name).toHaveValue("失败专家");
    await user.click(within(dialog).getByRole("button", { name: "取消" }));
    await user.click(screen.getByRole("tab", { name: /我的/ }));
    expect(await screen.findByText(/配置失败/)).toBeVisible();
    expect(screen.queryByText(/初始化未完成：/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "召唤" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  });

  it("opens a prefilled create page when copying and does not expose inheritance", async () => {
    api.expertDetail.mockResolvedValue({
      ...expert,
      capabilities: { ...expert.capabilities, enterpriseKnowledgeEnabled: true }
    });
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^复制/ }));
    const dialog = screen.getByRole("dialog", { name: /^创建专家/ });
    // antd Modal appear 阶段内容 opacity:0，统一等弹窗可见后再断言。
    await waitFor(() => expect(dialog).toBeVisible());

    expect(api.expertDetail).toHaveBeenCalledWith(1, 7);
    expect(within(dialog).getByPlaceholderText("例如：微信公众号文章专家")).toHaveValue("写作专家-副本");
    expect(within(dialog).queryByText(/继承/)).not.toBeInTheDocument();
    expect(api.copyExpert).not.toHaveBeenCalled();
  });

  it("increments a copy name across both self-created and market-installed experts", async () => {
    api.expertMarket.mockResolvedValue([{
      ...installableMarketContract,
      templateId: 32,
      expertPackageId: 18,
      expertPackageVersionId: 33,
      name: "写作专家-副本",
      description: null,
      avatarUrl: null,
      authorUserId: 2,
      version: 1,
      installedRuntimeAgentProfileId: 8,
      installed: true,
      state: "installed" as const,
      updatedAt: null
    }]);
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^复制/ }));

    expect(screen.getByPlaceholderText("例如：微信公众号文章专家"))
      .toHaveValue("写作专家-副本2");
  });

  it("confirms before discarding changes to an existing expert", async () => {
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^配置/ }));
    const dialog = screen.getByRole("dialog", { name: /写作专家/ });
    await user.type(within(dialog).getByRole("textbox", { name: "简介" }), "新增说明");

    await user.click(within(dialog).getByRole("button", { name: "取消" }));
    const confirmation = screen.getByRole("dialog", { name: "关闭专家编辑器" });
    await waitFor(() => expect(within(confirmation).getByText(/关闭后将丢失/)).toBeVisible());
    await user.click(within(confirmation).getByRole("button", { name: "继续编辑" }));
    expect(dialog).toBeVisible();
  });

  it("shows a no-change message without marking or refreshing authoritative state", async () => {
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^配置/ }));
    const dialog = screen.getByRole("dialog", { name: /写作专家/ });
    await waitFor(() => expect(api.myExperts).toHaveBeenCalledTimes(1));

    await user.click(within(dialog).getByRole("button", { name: "保存" }));

    expect(within(dialog).getByRole("alert")).toHaveTextContent("未检测到专家配置变化");
    expect(api.saveExpertConfiguration).not.toHaveBeenCalled();
    expect(api.replaceExpertPermissions).not.toHaveBeenCalled();
    expect(api.myExperts).toHaveBeenCalledTimes(1);
  });

  it("shares an existing expert with the whole organization", async () => {
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: /我的/ }));
    await user.click(screen.getByRole("button", { name: /^配置/ }));
    const dialog = screen.getByRole("dialog", { name: /写作专家/ });
    // antd Modal appear 阶段内容 opacity:0，统一等弹窗可见后再断言。
    await waitFor(() => expect(dialog).toBeVisible());

    await selectWholeOrganization(dialog, user);

    expect(within(dialog).getByText("示例业务技术")).toBeVisible();
    expect(within(dialog).getByText("全员可见")).toBeVisible();
    await user.click(within(dialog).getByRole("button", {
      name: "保存并提交审核"
    }));

    const expectedScope = {
      organization: true,
      userIds: [],
      departmentIds: []
    };
    await waitFor(() => {
      expect(api.replaceExpertPermissions).toHaveBeenCalledWith(
        1,
        7,
        expectedScope,
        null,
      );
      expect(api.saveExpertConfiguration).not.toHaveBeenCalled();
      expect(api.submitExpertReview).not.toHaveBeenCalled();
    });
  });

  it("saves capability changes when the user switches to sharing before publishing", async () => {
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: /我的/ }));
    await user.click(screen.getByRole("button", { name: /^配置/ }));
    const dialog = screen.getByRole("dialog", { name: /写作专家/ });
    // antd Modal appear 阶段内容 opacity:0，统一等弹窗可见后再断言。
    await waitFor(() => expect(dialog).toBeVisible());

    const skillSection = within(dialog).getByRole("heading", { name: "技能" }).closest(".ant-card");
    await user.click(within(skillSection as HTMLElement).getByRole("button", { name: /从技能市场选择/ }));
    const skillPicker = screen.getByRole("dialog", { name: "配置技能" });
    await user.click(within(skillPicker).getByRole("checkbox", { name: /我的写作技能/ }));
    await user.click(within(skillPicker).getByRole("button", { name: "确认选择" }));
    const knowledgeSection = within(dialog).getByRole("heading", { name: "知识库" }).closest(".ant-card");
    await user.click(within(knowledgeSection as HTMLElement).getByRole("button", { name: /选择知识库/ }));
    const knowledgePicker = screen.getByRole("dialog", { name: "配置知识库" });
    const enterpriseKnowledge = within(knowledgePicker).getByRole("checkbox", {
      name: "企业知识库"
    });
    expect(enterpriseKnowledge).not.toBeChecked();
    await user.click(enterpriseKnowledge);
    await user.click(within(knowledgePicker).getByRole("button", { name: "确认选择" }));
    await selectWholeOrganization(dialog, user);
    await user.type(
      within(dialog).getByRole("textbox", { name: "本次版本改动简介" }),
      "新增写作技能",
    );
    await user.click(within(dialog).getByRole("button", { name: "保存并提交审核" }));

    await waitFor(() => {
      expect(api.saveExpertConfiguration).toHaveBeenCalledWith(1, 7, expect.objectContaining({
        capabilities: expect.objectContaining({
          enterpriseKnowledgeEnabled: true,
          skills: [{ source: "default_installed", assetId: 41, versionId: 91 }]
        }),
        shareScope: { organization: true, userIds: [], departmentIds: [] },
        changeSummary: "新增写作技能"
      }));
    });
    expect(api.replaceExpertCapabilities).not.toHaveBeenCalled();
    expect(api.replaceExpertPermissions).not.toHaveBeenCalled();
    expect(api.updateExpert).not.toHaveBeenCalled();
  });

  it("requires a change summary before republishing an approved expert", async () => {
    api.myExperts.mockResolvedValue([{
      ...expert,
      reviewStatus: "approved",
      shareScope: { organization: true, userIds: [], departmentIds: [] }
    }]);
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: /我的/ }));
    await user.click(screen.getByRole("button", { name: /^配置/ }));
    const dialog = screen.getByRole("dialog", { name: /写作专家/ });
    // antd Modal appear 阶段内容 opacity:0，统一等弹窗可见后再断言。
    await waitFor(() => expect(dialog).toBeVisible());

    const knowledgeSection = within(dialog).getByRole("heading", { name: "知识库" }).closest(".ant-card");
    await user.click(within(knowledgeSection as HTMLElement).getByRole("button", { name: /选择知识库/ }));
    const knowledgePicker = screen.getByRole("dialog", { name: "配置知识库" });
    await user.click(within(knowledgePicker).getByRole("checkbox", {
      name: "企业知识库"
    }));
    await user.click(within(knowledgePicker).getByRole("button", { name: "确认选择" }));
    await user.click(within(dialog).getByRole("button", {
      name: "保存并提交审核"
    }));

    expect(api.saveExpertConfiguration).not.toHaveBeenCalled();
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "发布新版本前，请填写本次版本改动简介"
    );
    const changeSummary = within(dialog).getByRole("textbox", {
      name: "本次版本改动简介（必填）"
    });
    expect(changeSummary).toBeRequired();

    await user.type(changeSummary, "启用企业知识库");
    await user.click(within(dialog).getByRole("button", {
      name: "保存并提交审核"
    }));

    await waitFor(() => expect(api.saveExpertConfiguration).toHaveBeenCalledWith(
      1,
      7,
      expect.objectContaining({
        capabilities: expect.objectContaining({
          enterpriseKnowledgeEnabled: true
        }),
        changeSummary: "启用企业知识库"
      })
    ));
  });

  it("shows the enterprise knowledge checkbox without a general Plugin editor", async () => {
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: /我的/ }));
    await user.click(screen.getByRole("button", { name: /^配置/ }));
    const dialog = screen.getByRole("dialog", { name: /写作专家/ });
    // antd Modal appear 阶段内容 opacity:0，统一等弹窗可见后再断言。
    await waitFor(() => expect(dialog).toBeVisible());
    const knowledgeSection = within(dialog).getByRole("heading", { name: "知识库" }).closest(".ant-card");
    await user.click(within(knowledgeSection as HTMLElement).getByRole("button", { name: /选择知识库/ }));
    const picker = screen.getByRole("dialog", { name: "配置知识库" });

    await waitFor(() => expect(within(picker).getByRole("heading", { name: "公共知识库" })).toBeVisible());
    expect(within(picker).getByRole("tab", { name: "企业知识库" })).toBeVisible();
    expect(within(picker).getByRole("tab", { name: "个人知识库" })).toBeVisible();
    expect(within(picker).getByRole("checkbox", { name: "企业知识库" })).not.toBeChecked();
    await user.click(within(picker).getByRole("tab", { name: "个人知识库" }));
    expect(within(picker).getByRole("heading", { name: "我的知识库" })).toBeVisible();
    expect(within(picker).queryByRole("heading", { name: "Plugin" })).not.toBeInTheDocument();
  });

  it("only selects existing knowledge assets in the expert editor", async () => {
    const user = userEvent.setup();
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await user.click(await screen.findByRole("button", { name: /^配置/ }));
    const editor = screen.getByRole("dialog", { name: /写作专家/ });
    const knowledgeSection = within(editor).getByRole("heading", { name: "知识库" }).closest(".ant-card");
    await user.click(within(knowledgeSection as HTMLElement).getByRole("button", { name: /选择知识库/ }));
    const picker = screen.getByRole("dialog", { name: "配置知识库" });
    await user.click(within(picker).getByRole("tab", { name: "个人知识库" }));
    const knowledgeCheckbox = within(picker).getByRole("checkbox", { name: /我的资料库/ });
    expect(knowledgeCheckbox).not.toBeChecked();

    expect(within(picker).queryByRole("button", { name: "上传文件" })).not.toBeInTheDocument();
    expect(knowledgeCheckbox).not.toBeChecked();
    expect(api.saveExpertConfiguration).not.toHaveBeenCalled();
  });

  it("keeps a submitted expert editable and replaces the pending review on save", async () => {
    const pendingExpert = {
      ...expert,
      reviewStatus: "pending" as const,
      shareScope: { organization: false, userIds: [2], departmentIds: [] },
    };
    api.myExperts.mockResolvedValue([pendingExpert]);
    api.expertDetail.mockResolvedValue(pendingExpert);
    api.saveExpertConfiguration.mockResolvedValue({ ...expert, displayName: "待审期间继续修改", reviewStatus: "pending" });
    const user = userEvent.setup();
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await user.click(await screen.findByRole("button", { name: /^配置/ }));
    const editor = screen.getByRole("dialog", { name: /写作专家/ });
    await waitFor(() => expect(editor).toBeVisible());

    await waitFor(() => expect(within(editor).getByText("市场版本待管理员审核")).toBeVisible());
    const name = within(editor).getByLabelText("名称（必填）");
    await user.clear(name);
    await user.type(name, "待审期间继续修改");
    await user.clear(within(editor).getByLabelText(/本次版本改动简介/));
    await user.type(within(editor).getByLabelText(/本次版本改动简介/), "替换上一份待审版本");
    const save = within(editor).getByRole("button", { name: /保存并提交审核/ });
    expect(save).toBeEnabled();
    await user.click(save);
    await waitFor(() => expect(api.saveExpertConfiguration).toHaveBeenCalledWith(
      1,
      7,
      expect.objectContaining({
        name: "待审期间继续修改",
        changeSummary: "替换上一份待审版本",
      }),
    ));
    expect(within(editor).queryByRole("button", { name: /撤回/ })).not.toBeInTheDocument();
  });

  it("filters experts in the current entry without changing the authoritative list", async () => {
    const user = userEvent.setup();
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    // 2026-11-03-016 对齐技能广场：搜索框改为 Input + 前缀放大镜，role 为 textbox。
    const search = await screen.findByRole("textbox", { name: "搜索专家" });

    await user.type(search, "不存在");
    expect(screen.queryByRole("heading", { name: "写作专家" })).not.toBeInTheDocument();
    await user.clear(search);
    expect(await screen.findByRole("heading", { name: "写作专家" })).toBeVisible();
  });

  it("removes an uninstalled expert from the authoritative installed list and preserves history read-only", async () => {
    render(
      <ExpertPage
        runtimeWorkspaceId={1}
        onStartChat={vi.fn()}
      />
    );
    await userEvent.setup().click(await screen.findByRole("tab", { name: /我的/ }));
    fireEvent.click(screen.getByRole("button", { name: "删除 写作专家" }));
    const dialog = screen.getByRole("dialog", { name: "删除专家" });

    await waitFor(() => expect(within(dialog).getByText(/从“我的”移除.*历史会话保留只读/)).toBeVisible());
    api.myExperts.mockResolvedValue([]);
    await userEvent.setup().click(within(dialog).getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(api.uninstallExpert).toHaveBeenCalledWith(1, 7));
    await waitFor(() => expect(screen.queryByRole("heading", { name: "写作专家" })).not.toBeInTheDocument());
  });

  it("hides internal projection fields from installed expert cards", async () => {
    api.myExperts.mockResolvedValue([{
      ...expert,
      desiredRevision: 2,
      appliedRevision: 1,
      configuredExpertVersionId: 32,
      appliedExpertVersionId: 31,
      convergenceStatus: "updating",
      effectiveStatus: "updating",
      status: "updating"
    }]);
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);

    await userEvent.setup().click(await screen.findByRole("tab", { name: /我的/ }));
    expect(await screen.findByRole("heading", { name: "写作专家" })).toBeVisible();
    expect(screen.queryByText("已配置")).not.toBeInTheDocument();
    expect(screen.queryByText("已投影")).not.toBeInTheDocument();
    expect(screen.queryByText("运行生效")).not.toBeInTheDocument();
    expect(screen.queryByText("updating")).not.toBeInTheDocument();
    expect(screen.getByText(/准备中/)).toBeVisible();
  });

  it("keeps a withdrawn market expert usable with a business-level notice", async () => {
    api.myExperts.mockResolvedValue([{
      ...expert,
      ownershipType: "market_installed",
      marketRemoved: true
    }]);
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);

    await userEvent.setup().click(await screen.findByRole("tab", { name: /我的/ }));
    expect(await screen.findByText("该专家已从广场移除")).toBeVisible();
    expect(screen.getByRole("button", { name: "召唤" })).toBeEnabled();
  });

  it("refreshes authoritative expert state after uninstall failure", async () => {
    api.uninstallExpert.mockRejectedValue(new Error("uninstall failed"));
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: /我的/ }));
    await user.click(screen.getByRole("button", { name: "删除 写作专家" }));
    api.myExperts.mockResolvedValue([{
      ...expert,
      status: "error",
      allowedActions: { ...expert.allowedActions, summon: false },
      lastError: "workspace cleanup failed",
      lastOperationAction: "delete",
      lastOperationStatus: "failed"
    }]);

    await user.click(within(screen.getByRole("dialog", { name: "删除专家" }))
      .getByRole("button", { name: "确认删除" }));

    expect(await screen.findByText(/暂不可用/)).toBeVisible();
    expect(screen.queryByText(/初始化未完成：/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "召唤" })).toBeDisabled();
    expect(within(screen.getByRole("dialog", { name: "删除专家" }))
      .getByRole("alert")).toHaveTextContent("uninstall failed");
  });

  it("keeps the primary mutation error when authoritative refresh also fails", async () => {
    api.uninstallExpert.mockRejectedValue(new Error("primary uninstall error"));
    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: /我的/ }));
    await user.click(screen.getByRole("button", { name: "删除 写作专家" }));
    api.myExperts.mockRejectedValue(new Error("refresh error"));

    await user.click(within(screen.getByRole("dialog", { name: "删除专家" }))
      .getByRole("button", { name: "确认删除" }));

    expect(await within(screen.getByRole("dialog", { name: "删除专家" }))
      .findByRole("alert")).toHaveTextContent("primary uninstall error");
    expect(screen.queryByRole("button", { name: "召唤" })).not.toBeInTheDocument();
  });

  it("shows an interrupted install without a user retry action", async () => {
    api.myExperts.mockResolvedValue([
      {
        ...expert,
        status: "error",
        lastError: "backend restarted",
        lastOperationAction: "install",
        lastOperationStatus: "failed"
      }
    ]);
    render(
      <ExpertPage
        runtimeWorkspaceId={1}
        onStartChat={vi.fn()}
      />
    );
    await userEvent.setup().click(await screen.findByRole("tab", { name: /我的/ }));

    expect(await screen.findByRole("heading", { name: "写作专家" })).toBeVisible();
    expect(screen.queryByText(/初始化未完成：/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  });
});
