import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  AdminModelCatalogPublishResponse,
  AdminModelConfigResponse,
  AdminPrincipalSummary,
  AdminUsageSummaryResponse
} from "@hermes-cloud/schemas";

const api = vi.hoisted(() => ({
  me: vi.fn(),
  modelConfig: vi.fn(),
  runtimeResources: vi.fn(),
  usageSummary: vi.fn(),
  auditLogs: vi.fn(),
  publicMcps: vi.fn(),
  publicSkillCatalog: vi.fn(),
  skillCategories: vi.fn(),
  modelVisibilityDirectoryTree: vi.fn(),
  publishModelConfig: vi.fn()
}));

vi.mock("../api/client", () => ({ adminApi: api }));

import { AdminDashboardPage } from "./AdminDashboardPage";

const principal: AdminPrincipalSummary = { orgId: 1, userId: 99, roles: ["admin"] };
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
const modelConfig: AdminModelConfigResponse = {
  entries: [
    {
      providerId: 1,
      providerName: "provider-a",
      providerDisplayName: "Provider A",
      providerType: "openai_compatible",
      apiEndpoint: "https://provider.example/v1",
      credentialConfigured: true,
      providerStatus: "active",
      providerCatalogVersion: 1,
      modelEntryId: 5,
      modelKey: "model-a",
      modelDisplayName: "Model A",
      contextLength: 128000,
      tags: [],
      isRestricted: true,
      visibilityScope: {
        organization: false,
        departmentIds: ["root"],
        userIds: [101]
      },
      visibilityUsers: [{
        id: 101,
        name: "李四",
        userCode: "l002",
        status: "active"
      }],
      isDefault: false,
      modelStatus: "active",
      modelCatalogVersion: 1,
      runtimeCapabilities: {
        thinkingMode: "provider_default",
        reasoningReplay: "not_supported"
      }
    },
    {
      providerId: 1,
      providerName: "provider-a",
      providerDisplayName: "Provider A",
      providerType: "openai_compatible",
      apiEndpoint: "https://provider.example/v1",
      credentialConfigured: true,
      providerStatus: "active",
      providerCatalogVersion: 1,
      modelEntryId: 6,
      modelKey: "model-b",
      modelDisplayName: "Model B",
      contextLength: 128000,
      tags: [],
      isRestricted: false,
      visibilityScope: {
        organization: true,
        departmentIds: [],
        userIds: []
      },
      visibilityUsers: [],
      isDefault: false,
      modelStatus: "active",
      modelCatalogVersion: 1,
      runtimeCapabilities: {
        thinkingMode: "provider_default",
        reasoningReplay: "not_supported"
      }
    }
  ],
  latestRelease: null
};
const publishResponse: AdminModelCatalogPublishResponse = {
  releaseId: 9,
  catalogVersion: 2,
  previousCatalogVersion: 1,
  configHash: "a".repeat(64),
  status: "published",
  publishedAt: "2026-09-24T00:00:00Z"
};
const directory = [{
  nodeType: "organization" as const,
  organizationId: 1,
  name: "示例业务技术",
  children: [
    {
      nodeType: "department" as const,
      departmentId: "root",
      name: "总部",
      parentId: null,
      children: [
        {
          nodeType: "user" as const,
          userId: 101,
          name: "李四",
          userCode: "l002",
          departmentId: "root",
          status: "active" as const
        }
      ]
    }
  ]
}];

describe("Admin model visibility configuration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, "", "/admin/models");
    api.me.mockResolvedValue(principal);
    api.modelConfig.mockResolvedValue(modelConfig);
    api.runtimeResources.mockResolvedValue([]);
    api.usageSummary.mockResolvedValue(usage);
    api.auditLogs.mockResolvedValue([]);
    api.publicMcps.mockResolvedValue([]);
    api.publicSkillCatalog.mockResolvedValue([]);
    api.skillCategories.mockResolvedValue([]);
    api.modelVisibilityDirectoryTree.mockResolvedValue(directory);
    api.publishModelConfig.mockResolvedValue(publishResponse);
  });

  it("loads one shared organization tree and clears a restricted scope back to organization-wide", async () => {
    const user = userEvent.setup();
    render(<AdminDashboardPage />);

    expect(await screen.findByText("Model A")).toBeVisible();
    expect(screen.getByText("Model B")).toBeVisible();
    await waitFor(() =>
      expect(api.modelVisibilityDirectoryTree).toHaveBeenCalledTimes(1)
    );

    const buttons = screen.getAllByRole("button", { name: /配置可见权限/ });
    expect(buttons).toHaveLength(2);
    expect(buttons[0]).toHaveTextContent("当前：1 个部门 / 1 名人员");
    expect(buttons[1]).toHaveTextContent("当前：全员可见");
    expect(screen.getByText("可见部门（1）")).toBeVisible();
    expect(screen.getByText("总部")).toBeVisible();
    expect(screen.getByText("可见人员（1）")).toBeVisible();
    expect(screen.getByText("李四（l002）")).toBeVisible();
    expect(screen.queryByRole("radio", { name: "全员可见" })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "自定义可见" })).not.toBeInTheDocument();

    await user.click(buttons[0]);
    const dialog = screen.getByRole("dialog", { name: "配置可见权限" });
    expect(within(dialog).getByText("总部")).toBeVisible();
    await user.click(within(dialog).getByRole("checkbox", { name: "总部" }));
    await user.click(within(dialog).getByRole("button", { name: "确认" }));

    expect(buttons[0]).toHaveTextContent("当前：全员可见");
    expect(screen.queryByText(/^可见部门/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^可见人员/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "发布当前草稿" }));

    await waitFor(() =>
      expect(api.publishModelConfig).toHaveBeenCalledWith([
        expect.objectContaining({
          modelKey: "model-a",
          isRestricted: false,
          visibilityScope: {
            organization: true,
            departmentIds: [],
            userIds: []
          }
        }),
        expect.objectContaining({
          modelKey: "model-b",
          isRestricted: false,
          visibilityScope: {
            organization: true,
            departmentIds: [],
            userIds: []
          }
        })
      ])
    );
    expect(api.modelVisibilityDirectoryTree).toHaveBeenCalledTimes(1);
  });
});
