import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ExpertMarketItem, RuntimeAgentProfileSummary } from "@hermes-cloud/schemas";

const api = vi.hoisted(() => ({
  expertMarket: vi.fn(),
  myExperts: vi.fn(),
  runnableExperts: vi.fn(),
  expertDetail: vi.fn(),
  marketExpertDetail: vi.fn()
}));

vi.mock("../../api/client", () => ({ userApi: api }));

import { ExpertPage } from "./ExpertPage";

const expert: RuntimeAgentProfileSummary = {
  runtimeAgentProfileId: 7,
  runtimeWorkspaceId: 1,
  hermesProfileName: "expert-7",
  profileKind: "expert",
  displayName: "写作专家",
  description: "公众号文章写作",
  avatarUrl: null,
  soulContent: "# 写作专家",
  ownershipType: "self_created",
  inheritedFromProfileId: null,
  expertPackageId: 17,
  localVersion: 1,
  installedVersionId: null,
  installedVersion: null,
  availableVersionId: null,
  updateAvailable: false,
  desiredRevision: 1,
  appliedRevision: 1,
  configuredExpertVersionId: null,
  appliedExpertVersionId: null,
  convergenceStatus: "applied",
  effectiveStatus: "applied",
  marketRemoved: false,
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
  lastOperationId: null,
  lastOperationAction: null,
  lastOperationStatus: null,
  lastOperationStep: null,
  lastOperationError: null,
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

const pendingExpert = {
  ...expert,
  appliedRevision: 0,
  convergenceStatus: "waiting",
  effectiveStatus: "waiting",
  allowedActions: { ...expert.allowedActions, summon: false }
};

const pendingInstalledMarketExpert = {
  ...expert,
  runtimeAgentProfileId: 8,
  displayName: "市场安装专家",
  ownershipType: "market_installed",
  appliedRevision: 0,
  convergenceStatus: "waiting",
  effectiveStatus: "waiting",
  allowedActions: { ...expert.allowedActions, summon: false }
};

const readyInstalledMarketExpert = {
  ...pendingInstalledMarketExpert,
  appliedRevision: 1,
  convergenceStatus: "applied",
  effectiveStatus: "applied",
  allowedActions: { ...expert.allowedActions, summon: true }
};

const pendingMarketItem: ExpertMarketItem = {
  expertPackageId: 18,
  expertPackageVersionId: 32,
  name: "市场安装专家",
  description: null,
  avatarUrl: null,
  providerName: "专家市场",
  version: 1,
  installed: true,
  installedRuntimeAgentProfileId: 8,
  catalogStatus: "published",
  executionStatus: "enabled",
  allowedActions: {
    viewDetail: true,
    install: false,
    uninstall: true,
    summon: false,
    edit: false,
    copy: false,
    publish: false,
    unlist: false,
    delete: false
  }
};

describe("Expert convergence polling", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    api.expertMarket.mockResolvedValue([]);
    api.myExperts.mockResolvedValue([expert]);
    api.runnableExperts.mockResolvedValue([expert]);
    api.expertDetail.mockResolvedValue(expert);
    api.marketExpertDetail.mockResolvedValue({
      expertPackageId: 18,
      expertPackageVersionId: 32,
      name: "市场安装专家",
      description: null,
      avatarUrl: null,
      providerName: "专家市场",
      version: 1,
      installed: true,
      installedRuntimeAgentProfileId: 8,
      catalogStatus: "published",
      executionStatus: "enabled",
      allowedActions: pendingMarketItem.allowedActions,
      soulContent: "# 市场专家",
      systemPrompt: null,
      skills: [],
      mcps: [],
      knowledgeBases: []
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("updates a waiting expert automatically after Runtime projection", async () => {
    const readyExpert = {
      ...expert,
      appliedRevision: 1,
      convergenceStatus: "applied",
      effectiveStatus: "applied",
      allowedActions: { ...expert.allowedActions, summon: true }
    };
    api.myExperts.mockResolvedValue([pendingExpert]);
    api.runnableExperts
      .mockResolvedValueOnce([])
      .mockResolvedValue([readyExpert]);
    api.expertDetail
      .mockResolvedValueOnce(pendingExpert)
      .mockResolvedValueOnce(readyExpert)
      .mockResolvedValue(readyExpert);
    const onInstalledChange = vi.fn();

    render(
      <ExpertPage
        runtimeWorkspaceId={1}
        onStartChat={vi.fn()}
        onInstalledChange={onInstalledChange}
      />
    );
    await act(async () => {
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });

    expect(screen.getByText(/准备中/)).toBeVisible();
    expect(screen.getByRole("button", { name: "召唤" })).toBeDisabled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_499);
    });
    expect(api.expertDetail).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
    expect(api.expertDetail).toHaveBeenCalledWith(1, 7);
    expect(screen.getByText(/准备中/)).toBeVisible();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });

    expect(api.expertDetail).toHaveBeenCalledTimes(2);
    expect(api.runnableExperts).toHaveBeenCalledTimes(2);
    expect(onInstalledChange).toHaveBeenCalledWith([readyExpert]);
    expect(screen.queryByText(/准备中/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "召唤" })).toBeEnabled();
  });

  it("stops polling and reports an authoritative projection failure", async () => {
    const failedExpert = {
      ...pendingExpert,
      convergenceStatus: "failed",
      effectiveStatus: "failed",
      lastError: "PROFILE_PROJECTION_FAILED"
    };
    api.myExperts.mockResolvedValue([pendingExpert]);
    api.runnableExperts.mockResolvedValue([]);
    api.expertDetail.mockResolvedValueOnce(failedExpert);

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await act(async () => {
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });

    expect(api.expertDetail).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/配置失败/)).toBeVisible();
    expect(screen.queryByText(/初始化未完成：/)).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
    expect(api.expertDetail).toHaveBeenCalledTimes(1);
  });

  it("updates an installed market expert automatically after Runtime projection", async () => {
    api.expertMarket.mockResolvedValue([pendingMarketItem]);
    api.myExperts.mockResolvedValue([]);
    api.runnableExperts
      .mockResolvedValueOnce([])
      .mockResolvedValue([readyInstalledMarketExpert]);
    api.expertDetail
      .mockResolvedValueOnce(pendingInstalledMarketExpert)
      .mockResolvedValueOnce(readyInstalledMarketExpert);
    const onInstalledChange = vi.fn();

    render(
      <ExpertPage
        runtimeWorkspaceId={1}
        onStartChat={vi.fn()}
        onInstalledChange={onInstalledChange}
      />
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("tab", { name: /^专家市场/ }));
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
    expect(screen.getByText(/准备中/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "召唤" })).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
    expect(api.expertDetail).toHaveBeenCalledWith(1, 8);
    expect(screen.getByText(/准备中/)).toBeVisible();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });

    expect(api.expertDetail).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/准备中/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "召唤" })).toBeEnabled();
    expect(onInstalledChange).toHaveBeenLastCalledWith([readyInstalledMarketExpert]);
  });

  it("stops market expert polling after an authoritative projection failure", async () => {
    api.expertMarket.mockResolvedValue([pendingMarketItem]);
    api.myExperts.mockResolvedValue([]);
    api.runnableExperts.mockResolvedValue([]);
    api.expertDetail.mockResolvedValueOnce({
      ...pendingInstalledMarketExpert,
      convergenceStatus: "failed",
      effectiveStatus: "failed",
      lastError: "PROFILE_PROJECTION_FAILED"
    });

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await act(async () => {
      fireEvent.click(screen.getByRole("tab", { name: /^专家市场/ }));
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });

    expect(api.expertDetail).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/配置失败/)).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
    expect(api.expertDetail).toHaveBeenCalledTimes(1);
  });

  it("updates an open market expert detail dialog after projection completes", async () => {
    api.expertMarket.mockResolvedValue([pendingMarketItem]);
    api.myExperts.mockResolvedValue([]);
    api.runnableExperts
      .mockResolvedValueOnce([])
      .mockResolvedValue([readyInstalledMarketExpert]);
    api.expertDetail
      .mockResolvedValueOnce(pendingInstalledMarketExpert)
      .mockResolvedValueOnce(readyInstalledMarketExpert);

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await act(async () => {
      fireEvent.click(screen.getByRole("tab", { name: /^专家市场/ }));
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "查看详情" }));
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
    const dialog = screen.getByRole("dialog", { name: "市场安装专家" });
    expect(within(dialog).queryByRole("button", { name: "召唤" })).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });

    expect(api.expertDetail).toHaveBeenCalledTimes(2);
    expect(within(dialog).getByRole("button", { name: "召唤" })).toBeEnabled();
  });

  it("does not poll a force-disabled market expert", async () => {
    api.expertMarket.mockResolvedValue([{
      ...pendingMarketItem,
      executionStatus: "force_disabled"
    }]);
    api.myExperts.mockResolvedValue([]);
    api.runnableExperts.mockResolvedValue([]);

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await act(async () => {
      fireEvent.click(screen.getByRole("tab", { name: /^专家市场/ }));
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });

    expect(screen.getByText(/已被平台强制停用/)).toBeVisible();
    expect(api.expertDetail).not.toHaveBeenCalled();
  });

  it("cancels market expert polling after the installation disappears from the market list", async () => {
    api.expertMarket
      .mockResolvedValueOnce([pendingMarketItem])
      .mockResolvedValue([{
        ...pendingMarketItem,
        installed: false,
        installedRuntimeAgentProfileId: null,
        allowedActions: {
          ...pendingMarketItem.allowedActions,
          install: true,
          uninstall: false
        }
      }]);
    api.myExperts.mockResolvedValue([]);
    api.runnableExperts.mockResolvedValue([]);

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    await act(async () => {
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("tab", { name: /^专家市场/ }));
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });

    expect(api.expertMarket).toHaveBeenCalledTimes(2);
    expect(api.expertDetail).not.toHaveBeenCalled();
  });
});
