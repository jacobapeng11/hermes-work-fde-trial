import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  avatarAssets: vi.fn(),
  expertMarket: vi.fn(),
  myExperts: vi.fn(),
  runnableExperts: vi.fn(),
  expertDetail: vi.fn(),
  expertCapabilityCatalog: vi.fn(),
  organizationDirectoryTree: vi.fn(),
  searchDirectoryUsers: vi.fn(),
  uploadPersonalSkill: vi.fn(),
  saveExpertConfiguration: vi.fn(),
  replaceExpertPermissions: vi.fn()
}));

vi.mock("../../api/client", () => ({ userApi: api }));

import { ExpertPage } from "./ExpertPage";

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

const capabilityCatalog = (overrides: Partial<{
  marketSkills: { assetId: number; versionId?: number; name: string; description?: string; source?: string; categoryKey?: string }[];
  installedSkills: { assetId: number; versionId?: number; name: string; description?: string; source?: string; categoryKey?: string }[];
  personalSkills: { assetId: number; versionId?: number; name: string; description?: string; source?: string; categoryKey?: string }[];
}> = {}) => ({
  marketSkills: [],
  installedSkills: [],
  skillCategories: [],
  marketMcps: [],
  installedMcps: [],
  knowledgeBases: [],
  personalSkills: [],
  personalKnowledgeBases: [],
  pluginIds: [],
  ...overrides
});

async function openEditor() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /^配置/ }));
  const dialog = screen.getByRole("dialog", { name: /写作专家/ });
  await waitFor(() => expect(dialog).toBeVisible());
  return { user, dialog };
}

async function uploadSkillC(dialog: HTMLElement, user: ReturnType<typeof userEvent.setup>) {
  await user.click(within(dialog).getByRole("button", { name: /上传个人技能/ }));
  const uploadDialog = await screen.findByRole("dialog", { name: "上传个人技能" });
  fireEvent.change(within(uploadDialog).getByLabelText("选择专家个人技能 zip 包"), {
    target: { files: [new File(["zip"], "skill-c.zip", { type: "application/zip" })] }
  });
  await user.click(within(uploadDialog).getByRole("button", { name: "开始上传" }));
  await waitFor(() => expect(api.uploadPersonalSkill).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(
    screen.queryByRole("dialog", { name: "上传个人技能" })
  ).not.toBeInTheDocument());
}

describe("ExpertPage save race handling", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    api.avatarAssets.mockResolvedValue([]);
    api.expertMarket.mockResolvedValue([]);
    api.myExperts.mockResolvedValue([expert]);
    api.runnableExperts.mockResolvedValue([expert]);
    api.expertDetail.mockResolvedValue(expert);
    api.organizationDirectoryTree.mockResolvedValue([{
      nodeType: "organization",
      organizationId: 1,
      name: "示例业务技术",
      children: []
    }]);
    api.searchDirectoryUsers.mockResolvedValue([]);
    api.uploadPersonalSkill.mockResolvedValue({
      skillId: 63,
      skillVersionId: 83,
      slug: "skill-c",
      version: 1,
      name: "Skill C",
      status: "private",
      source: "personal_asset",
      applied: false
    });
    api.saveExpertConfiguration.mockResolvedValue(expert);
  });

  it("waits for the capability catalog refresh and continues saving automatically", async () => {
    const catalogError = new Error("目录暂时不可用");
    api.expertCapabilityCatalog
      .mockRejectedValueOnce(catalogError)
      .mockRejectedValueOnce(catalogError)
      .mockResolvedValueOnce(capabilityCatalog({
        personalSkills: [{ assetId: 63, versionId: 83, name: "Skill C", source: "personal" }]
      }));

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const { user, dialog } = await openEditor();
    await uploadSkillC(dialog, user);

    await user.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(api.expertCapabilityCatalog).toHaveBeenCalledTimes(3));
    await waitFor(() => {
      expect(api.saveExpertConfiguration).toHaveBeenCalledWith(1, 7, expect.objectContaining({
        capabilities: expect.objectContaining({
          skills: [{ source: "personal", assetId: 63, versionId: 83 }]
        })
      }));
    });
    expect(api.expertCapabilityCatalog).toHaveBeenCalledTimes(3);
    expect(within(dialog).queryByText("能力目录尚未加载完成，请重试后再保存能力修改")).not.toBeInTheDocument();
  });

  it("does not submit when the catalog refresh fails and keeps the draft", async () => {
    const catalogError = new Error("目录仍然不可用");
    api.expertCapabilityCatalog.mockRejectedValue(catalogError);

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const { user, dialog } = await openEditor();
    await uploadSkillC(dialog, user);

    await user.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(within(dialog).getByText(/能力目录加载失败：目录仍然不可用/)).toBeVisible());
    expect(api.saveExpertConfiguration).not.toHaveBeenCalled();
    expect(dialog).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "重试能力目录" })).toBeVisible();
  });

  it("confirms removal of stale personal skills before saving without them", async () => {
    api.myExperts.mockResolvedValue([{
      ...expert,
      capabilities: {
        ...expert.capabilities,
        skills: [{ source: "personal", assetId: 61, versionId: 81 }]
      }
    }]);
    api.expertCapabilityCatalog.mockResolvedValue(capabilityCatalog({
      personalSkills: [
        { assetId: 62, versionId: 82, name: "Skill B", source: "personal" },
        { assetId: 63, versionId: 83, name: "Skill C", source: "personal" }
      ]
    }));

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const { user, dialog } = await openEditor();
    await uploadSkillC(dialog, user);

    await user.click(within(dialog).getByRole("button", { name: "保存" }));

    const confirmation = await screen.findByRole("dialog", { name: "保存前确认移除失效技能" });
    await waitFor(() => expect(within(confirmation).getByText("不可用资产 #61")).toBeVisible());
    await user.click(within(confirmation).getByRole("button", { name: "确认移除并保存" }));

    await waitFor(() => {
      expect(api.saveExpertConfiguration).toHaveBeenCalledWith(1, 7, expect.objectContaining({
        capabilities: expect.objectContaining({
          skills: [{ source: "personal", assetId: 63, versionId: 83 }]
        })
      }));
    });
  });

  it("keeps the draft and skips submission when stale skill removal is cancelled", async () => {
    api.myExperts.mockResolvedValue([{
      ...expert,
      capabilities: {
        ...expert.capabilities,
        skills: [{ source: "personal", assetId: 61, versionId: 81 }]
      }
    }]);
    api.expertCapabilityCatalog.mockResolvedValue(capabilityCatalog({
      personalSkills: [{ assetId: 63, versionId: 83, name: "Skill C", source: "personal" }]
    }));

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const { user, dialog } = await openEditor();
    await uploadSkillC(dialog, user);

    await user.click(within(dialog).getByRole("button", { name: "保存" }));

    const confirmation = await screen.findByRole("dialog", { name: "保存前确认移除失效技能" });
    await user.click(within(confirmation).getByRole("button", { name: "取消" }));

    expect(api.saveExpertConfiguration).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "保存前确认移除失效技能" })).not.toBeInTheDocument());
    expect(dialog).toBeVisible();
  });

  it("does not refresh the catalog when capabilities are unchanged", async () => {
    api.expertCapabilityCatalog.mockResolvedValue(capabilityCatalog());

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const { user, dialog } = await openEditor();
    await waitFor(() => expect(api.expertCapabilityCatalog).toHaveBeenCalledTimes(1));
    await user.type(within(dialog).getByRole("textbox", { name: "简介" }), "更新说明");

    await user.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(api.saveExpertConfiguration).toHaveBeenCalledTimes(1));
    expect(api.expertCapabilityCatalog).toHaveBeenCalledTimes(1);
  });

  it("keeps public and default installed skills when checking stale personal skills", async () => {
    api.myExperts.mockResolvedValue([{
      ...expert,
      capabilities: {
        ...expert.capabilities,
        skills: [{ source: "default_installed", assetId: 41, versionId: 91 }]
      }
    }]);
    api.expertCapabilityCatalog.mockResolvedValue(capabilityCatalog({
      installedSkills: [{ assetId: 41, versionId: 91, name: "默认写作技能", source: "default_installed" }],
      personalSkills: [{ assetId: 62, versionId: 82, name: "Skill B", source: "personal" }]
    }));

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const { user, dialog } = await openEditor();
    const skillSection = within(dialog).getByRole("heading", { name: "技能" }).closest(".ant-card");
    await user.click(within(skillSection as HTMLElement).getByRole("button", { name: /从技能市场选择/ }));
    const skillPicker = screen.getByRole("dialog", { name: "配置技能" });
    await user.click(within(skillPicker).getByRole("checkbox", { name: /Skill B/ }));
    await user.click(within(skillPicker).getByRole("button", { name: "确认选择" }));

    await user.click(within(dialog).getByRole("button", { name: "保存" }));

    expect(screen.queryByRole("dialog", { name: "保存前确认移除失效技能" })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(api.saveExpertConfiguration).toHaveBeenCalledWith(1, 7, expect.objectContaining({
        capabilities: expect.objectContaining({
          skills: [
            { source: "default_installed", assetId: 41, versionId: 91 },
            { source: "personal", assetId: 62, versionId: 82 }
          ]
        })
      }));
    });
  });

  it("keeps the create button disabled while the catalog is unavailable", async () => {
    api.expertCapabilityCatalog.mockRejectedValue(new Error("目录仍然不可用"));

    render(<ExpertPage runtimeWorkspaceId={1} onStartChat={vi.fn()}/>);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "创建专家" }));
    const dialog = screen.getByRole("dialog", { name: /^创建专家/ });
    await waitFor(() => expect(within(dialog).getByText(/能力目录加载失败：目录仍然不可用/)).toBeVisible());

    expect(within(dialog).getByRole("button", { name: "创建专家" })).toBeDisabled();
  });
});
