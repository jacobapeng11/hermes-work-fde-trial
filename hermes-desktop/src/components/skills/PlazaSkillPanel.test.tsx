import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const load = vi.fn().mockResolvedValue(undefined);
const submitPersonalInstall = vi.fn().mockResolvedValue(undefined);
const submitPersonalUninstall = vi.fn().mockResolvedValue(undefined);
const submitPersonalDelete = vi.fn().mockResolvedValue({ rolloutId: "personal-skill-delete-1", affectedProfileIds: [10, 11] });
const submitPersonalUpload = vi.fn().mockResolvedValue(undefined);
const submitDescriptionUpdate = vi.fn().mockResolvedValue(undefined);
const createPersonalCategory = vi.fn().mockResolvedValue({
  categoryKey: "personal-writing",
  displayName: "写作",
  isBuiltin: false,
  scope: "personal",
});
const installedItems = [
  {
    assetId: 2,
    source: "market",
    name: "研究 Skill",
    description: null,
    categoryKey: "research",
    versionLabel: "v2",
    updateStatus: "current",
    catalogAssetId: 2,
    operating: false
  },
  {
    assetId: 1,
    source: "personal_asset",
    name: "我的上传 Skill",
    description: null,
    categoryKey: "personal",
    versionLabel: "v1",
    updateStatus: "current",
    catalogAssetId: null,
    operating: false
  }
];

vi.mock("../../state/plaza-skill-store", () => ({
  usePlazaSkillStore: () => ({
    marketItems: [
      {
        assetId: 20, name: "已安装市场 Skill", description: null, categoryKey: "research",
        currentVersionLabel: "2", state: "current", configuredAssetId: 20,
        installedVersionLabel: "2", operating: false,
      },
      {
        assetId: 21, name: "未安装市场 Skill", description: null, categoryKey: "research",
        currentVersionLabel: "1", state: "installable", configuredAssetId: null,
        installedVersionLabel: null, operating: false,
      },
    ],
    personalItems: [
      {
        assetId: 1, source: "personal", name: "已绑定个人 Skill", description: null,
        categoryKey: "uncategorized", versionLabel: "1", updateStatus: "not_applicable",
        operating: false, desired: true, applied: true, status: "private",
      },
      {
        assetId: 2, source: "personal", name: "未绑定个人 Skill", description: null,
        categoryKey: "uncategorized", versionLabel: "1", updateStatus: "not_applicable",
        operating: false, desired: false, applied: false, status: "private",
      },
    ],
    installedItems,
    categories: [
      { categoryKey: "system", displayName: "系统技能", isBuiltin: true, scope: "system" },
      { categoryKey: "uncategorized", displayName: "未分类", isBuiltin: true, scope: "uncategorized" },
      { categoryKey: "research", displayName: "研究", isBuiltin: true, isMarketVisible: true, isPersonalVisible: true }
    ],
    pendingOperations: [],
    loading: false,
    error: null,
    load,
    submitInstall: vi.fn(),
    submitUninstall: vi.fn(),
    submitPersonalUpload,
    submitPersonalInstall,
    submitPersonalUninstall,
    submitPersonalDelete,
    createPersonalCategory,
    submitDescriptionUpdate,
  })
}));

import { PlazaSkillPanel, type SkillWorkspaceController } from "./PlazaSkillPanel";

function embeddedController(): SkillWorkspaceController {
  return {
    marketItems: [],
    personalItems: [],
    installedItems: [],
    categories: [
      { categoryKey: "research", displayName: "研究", isBuiltin: true },
      { categoryKey: "agent-automation", displayName: "智能体与自动化", isBuiltin: true },
    ],
    pendingOperations: [],
    loading: false,
    error: null,
    load: vi.fn().mockResolvedValue(undefined),
    submitInstall: vi.fn().mockResolvedValue(undefined),
    submitUninstall: vi.fn().mockResolvedValue(undefined),
  };
}

describe("PlazaSkillPanel", () => {
  afterEach(() => vi.clearAllMocks());

  it("loads one stable snapshot without the retired inventory initialization channel", async () => {
    const originalEventSource = globalThis.EventSource;
    const eventSource = vi.fn();
    Object.defineProperty(globalThis, "EventSource", { configurable: true, value: eventSource });

    render(<PlazaSkillPanel runtimeWorkspaceId={3} />);

    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
    expect(eventSource).not.toHaveBeenCalled();
    expect(screen.queryByText(/正在后台整理 Hermes 自带 Skill/)).not.toBeInTheDocument();

    Object.defineProperty(globalThis, "EventSource", { configurable: true, value: originalEventSource });
  });

  it("shows every personal asset in My and separates uninstall from deletion", async () => {
    const user = userEvent.setup();
    render(<PlazaSkillPanel />);

    expect(screen.getByRole("tab", { name: /^我的 2$/ })).toBeVisible();
    expect(screen.getByRole("tab", { name: /^专家市场 2$/ })).toBeVisible();
    expect(screen.getByRole("group", { name: "技能分类" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "搜索技能" })).toBeVisible();
    expect(screen.getByText("已绑定个人 Skill")).toBeVisible();
    expect(screen.getByText("未绑定个人 Skill")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "安装" }));
    expect(submitPersonalInstall).toHaveBeenCalledWith(2);
    const installedCard = screen.getByLabelText("查看 已绑定个人 Skill 详情");
    await user.click(within(installedCard).getByRole("button", { name: "卸载" }));
    const uninstall = screen.getByRole("dialog", { name: "卸载个人技能" });
    expect(within(uninstall).queryByText(/删除个人技能资产/)).not.toBeInTheDocument();
    await user.click(within(uninstall).getByRole("button", { name: "确认卸载" }));
    expect(submitPersonalUninstall).toHaveBeenCalledWith(1);

    await user.click(within(installedCard).getByRole("button", { name: "删除 已绑定个人 Skill" }));
    const deletion = screen.getByRole("dialog", { name: "删除个人技能" });
    await waitFor(() => expect(within(deletion).getByText(/从全部已绑定 Profile 移除/)).toBeVisible());
    await user.click(within(deletion).getByRole("button", { name: "确认删除" }));
    expect(submitPersonalDelete).toHaveBeenCalledWith(1);
  });

  it("shows public Skill updates as automatic and does not expose a manual update action", async () => {
    const controller = embeddedController();
    controller.installedItems = [{
      assetId: 8,
      source: "market",
      name: "公共写作 Skill",
      description: null,
      categoryKey: "research",
      versionLabel: "v1",
      updateStatus: "update_available",
      catalogAssetId: 4,
      operating: false
    }];
    render(<PlazaSkillPanel controller={controller}/>);
    const user = userEvent.setup();

    expect(screen.getByText("自动更新中")).toBeVisible();
    expect(screen.queryByRole("button", { name: "更新" })).not.toBeInTheDocument();
    await user.click(screen.getByText("公共写作 Skill"));
    expect(screen.queryByRole("button", { name: "更新技能" })).not.toBeInTheDocument();
  });

  it("labels Application Agent private Skill removal as deletion", async () => {
    const submitUninstall = vi.fn().mockResolvedValue(undefined);
    const controller = embeddedController();
    controller.installedItems = [{
      assetId: 41,
      applicationAgentSkillId: 41,
      source: "application",
      name: "私有工单",
      description: "应用独有",
      categoryKey: "personal",
      versionLabel: "v1",
      updateStatus: "current",
      catalogAssetId: null,
      operating: false,
    }];
    controller.submitUninstall = submitUninstall;
    render(<PlazaSkillPanel controller={controller} />);
    const user = userEvent.setup();

    // 卡片按钮直接标注为「删除」（存在性断言；点击路径经下方详情弹窗触发，避免与
    // jsdom 中 Modal 关闭动画的可达性窗口赛跑）
    const card = screen.getByText("私有工单").closest(".skillCatalogCard") as HTMLElement;
    expect(within(card).getByRole("button", { name: "删除" })).toBeInTheDocument();

    await user.click(screen.getByText("私有工单"));
    const detail = screen.getByRole("dialog", { name: "技能详情" });
    await waitFor(() => expect(within(detail).getByRole("button", { name: "删除" })).toBeVisible());
    await user.click(within(detail).getByRole("button", { name: "删除" }));
    const deletion = await screen.findByRole("dialog", { name: "删除应用私有技能" });
    await waitFor(() => expect(within(deletion).getByText(/移除该私有技能及其上传包/)).toBeVisible());
    await user.click(within(deletion).getByRole("button", { name: "确认删除" }));

    expect(submitUninstall).toHaveBeenCalledWith(41, null);
    expect(screen.getByText(/“私有工单”已删除/)).toBeVisible();
  });

  it("shows archived personal Skills as unavailable instead of offering install", async () => {
    const controller = {
      ...embeddedController(),
      installedItems: [{
        assetId: 7,
        source: "personal" as const,
        name: "已归档 Skill",
        description: null,
        categoryKey: "uncategorized",
        versionLabel: "v1",
        updateStatus: "not_applicable" as const,
        operating: false,
        desired: false,
        applied: false,
        status: "archived",
      }],
    } as unknown as SkillWorkspaceController;
    render(<PlazaSkillPanel controller={controller} />);

    expect(screen.getByText("已归档，请重新上传同名 Skill 恢复。")).toBeVisible();
    expect(screen.queryByRole("button", { name: "安装" })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByLabelText("查看 已归档 Skill 详情"));
    const dialog = screen.getByRole("dialog", { name: "技能详情" });
    await waitFor(() => expect(within(dialog).getByText("已归档")).toBeVisible());
    expect(within(dialog).queryByRole("button", { name: "安装到当前 Hermes" })).not.toBeInTheDocument();
  });

  it("places system default skills after other items in the all category", () => {
    const controller = {
      ...embeddedController(),
      installedItems: [
      {
        assetId: 1, source: "personal", name: "个人技能", description: null,
        categoryKey: "uncategorized", versionLabel: "1", updateStatus: "not_applicable",
        operating: false, desired: true, applied: true, status: "private",
      },
      {
        assetId: 2, source: "system", name: "系统默认技能", description: null,
        categoryKey: "system", versionLabel: "1", updateStatus: "not_applicable",
        operating: false, systemKey: "system-default",
      }
      ],
      categories: [
      { categoryKey: "system", displayName: "系统技能", isBuiltin: true },
      { categoryKey: "uncategorized", displayName: "未分类", isBuiltin: true }
      ]
    } as unknown as SkillWorkspaceController;
    render(<PlazaSkillPanel controller={controller} />);

    const cards = document.querySelectorAll(".skillCatalogCard");
    expect([...cards].map((card) => card.querySelector("h3")?.textContent)).toEqual(["个人技能", "系统默认技能"]);
  });

  it("does not expose a SkillHub user directory", () => {
    render(<PlazaSkillPanel />);

    expect(screen.queryByRole("button", { name: "Skill市场" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "搜索 SkillHub 技能" })).not.toBeInTheDocument();
  });

  it("keeps installed market Skills first and supports install and uninstall in the market", async () => {
    const user = userEvent.setup();
    render(<PlazaSkillPanel />);
    await user.click(screen.getByRole("tab", { name: /^专家市场 2$/ }));
    const headings = screen.getAllByRole("heading", { level: 3 }).map((item) => item.textContent);
    expect(headings).toEqual(["已安装市场 Skill", "未安装市场 Skill"]);
    expect(screen.getByRole("button", { name: "卸载" })).toBeVisible();
    expect(screen.getByRole("button", { name: "安装" })).toBeVisible();
  });

  it("uploads a personal skill with a user-facing category", async () => {
    const user = userEvent.setup();
    render(<PlazaSkillPanel />);
    await user.click(screen.getByRole("button", { name: "上传个人技能" }));
    const dialog = screen.getByRole("dialog", { name: "上传个人技能" });

    await waitFor(() => expect(within(dialog).getByText("搜索或创建新分类")).toBeVisible());
    const file = new File(["zip"], "writing.zip", { type: "application/zip" });
    await user.upload(within(dialog).getByLabelText(/选择技能 zip 包/), file);
    await user.click(within(dialog).getByRole("button", { name: "开始上传" }));
    await waitFor(() => expect(submitPersonalUpload).toHaveBeenCalledWith(file, "uncategorized"));
  });

  it("submits an optional custom description with the uploaded Skill", async () => {
    const user = userEvent.setup();
    render(<PlazaSkillPanel />);
    await user.click(screen.getByRole("button", { name: "上传个人技能" }));
    const dialog = screen.getByRole("dialog", { name: "上传个人技能" });
    const file = new File(["zip"], "writing.zip", { type: "application/zip" });
    await user.upload(within(dialog).getByLabelText(/选择技能 zip 包/), file);
    await user.type(within(dialog).getByRole("textbox", { name: "技能描述" }), "团队周报专用");
    await user.click(within(dialog).getByRole("button", { name: "开始上传" }));

    await waitFor(() => expect(submitPersonalUpload).toHaveBeenCalledWith(
      file,
      "uncategorized",
      "团队周报专用",
    ));
  });

  it("edits a personal Skill name and description", async () => {
    const user = userEvent.setup();
    render(<PlazaSkillPanel />);

    await user.click(screen.getByLabelText("查看 已绑定个人 Skill 详情"));
    const dialog = screen.getByRole("dialog", { name: "技能详情" });
    await user.click(within(dialog).getByRole("button", { name: "编辑技能信息" }));
    const nameEditor = within(dialog).getByRole("textbox", { name: "编辑技能名称" });
    await user.clear(nameEditor);
    await user.type(nameEditor, "已重命名个人 Skill");
    const editor = within(dialog).getByRole("textbox", { name: "编辑技能描述" });
    await user.clear(editor);
    expect(within(dialog).getByText(/清空后保存将恢复技能包默认描述/)).toBeVisible();
    await user.type(editor, "新的团队说明");
    await user.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(submitDescriptionUpdate).toHaveBeenCalledWith(
      1,
      "新的团队说明",
      "已重命名个人 Skill",
    ));
  });

  it("shows the real system description without an edit action", async () => {
    const user = userEvent.setup();
    const controller = {
      ...embeddedController(),
      installedItems: [{
        assetId: -1,
        source: "system" as const,
        systemKey: "hermes-agent",
        name: "hermes-agent",
        description: "配置、扩展或贡献 Hermes Agent。",
        categoryKey: "system",
        versionLabel: "runtime",
        updateStatus: "not_applicable" as const,
        operating: false,
      }],
    } as unknown as SkillWorkspaceController;
    render(<PlazaSkillPanel controller={controller} />);

    await user.click(screen.getByLabelText("查看 hermes-agent 详情"));
    const dialog = screen.getByRole("dialog", { name: "技能详情" });
    await waitFor(() => expect(within(dialog).getByText("配置、扩展或贡献 Hermes Agent。")).toBeVisible());
    expect(within(dialog).queryByRole("button", { name: "编辑描述" })).not.toBeInTheDocument();
  });

  it("creates and selects a private category during upload", async () => {
    const user = userEvent.setup();
    render(<PlazaSkillPanel />);
    await user.click(screen.getByRole("button", { name: "上传个人技能" }));
    const dialog = screen.getByRole("dialog", { name: "上传个人技能" });

    // 在 Select 搜索框中输入，触发下拉出现「＋ 新建」选项
    const combobox = within(dialog).getByRole("combobox", { name: "技能分类" });
    await user.click(combobox);
    await user.type(combobox, "写作");
    const createOption = await screen.findByText(/新建/);
    await user.click(createOption);

    await waitFor(() => expect(createPersonalCategory).toHaveBeenCalledWith("写作"));
    expect(within(dialog).getByText("写作")).toBeVisible();
  });

  it("keeps the upload dialog open and exposes an inline error after submission failure", async () => {
    submitPersonalUpload.mockRejectedValueOnce(new Error("SKILL.md 校验失败"));
    const user = userEvent.setup();
    render(<PlazaSkillPanel />);
    await user.click(screen.getByRole("button", { name: "上传个人技能" }));
    const dialog = screen.getByRole("dialog", { name: "上传个人技能" });
    const file = new File(["zip"], "broken.zip", { type: "application/zip" });
    await user.upload(within(dialog).getByLabelText("选择技能 zip 包"), file);
    await user.click(within(dialog).getByRole("button", { name: "开始上传" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("SKILL.md 校验失败");
    expect(dialog).toBeVisible();
  });
  it("upload dialog blocks close while submitting", async () => {
    const user = userEvent.setup();
    submitPersonalUpload.mockImplementationOnce(() => new Promise(() => undefined));
    render(<PlazaSkillPanel />);
    await user.click(screen.getByRole("button", { name: "上传个人技能" }));
    const dialog = screen.getByRole("dialog", { name: "上传个人技能" });
    const file = new File(["zip"], "writing.zip", { type: "application/zip" });
    await user.upload(within(dialog).getByLabelText("选择技能 zip 包"), file);
    await user.click(within(dialog).getByRole("button", { name: "开始上传" }));

    await user.keyboard("{Escape}");
    fireEvent.click(document.querySelector(".ant-modal-wrap") ?? document.body);
    expect(within(dialog).getByRole("button", { name: "取消" })).toBeDisabled();
    expect(screen.getByRole("dialog", { name: "上传个人技能" })).toBeVisible();
  });

  it("upload category selection persists across dialog reopen", async () => {
    const user = userEvent.setup();
    render(<PlazaSkillPanel />);
    await user.click(screen.getByRole("button", { name: "上传个人技能" }));
    let dialog = screen.getByRole("dialog", { name: "上传个人技能" });
    await user.click(within(dialog).getByRole("combobox", { name: "技能分类" }));
    const dropdown = await waitFor(() => {
      const el = document.querySelector(".ant-select-dropdown:not(.ant-select-dropdown-hidden)");
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    // rc-select virtual 模式下可点击项不带 role=option（role=option 属于隐藏无障碍列表），按文本点真实项
    fireEvent.click(within(dropdown).getByText("研究"));
    await user.click(within(dialog).getByRole("button", { name: "取消" }));

    await user.click(screen.getByRole("button", { name: "上传个人技能" }));
    dialog = screen.getByRole("dialog", { name: "上传个人技能" });
    await waitFor(() => expect(within(dialog).getByText("研究")).toBeVisible());
  });

  it("我的技能为空时提供上传引导入口", async () => {
    const user = userEvent.setup();
    const controller = {
      ...embeddedController(),
      submitUpload: vi.fn().mockResolvedValue(undefined),
    } as unknown as SkillWorkspaceController;
    render(<PlazaSkillPanel controller={controller} />);

    const uploadEntries = await screen.findAllByRole("button", { name: "上传技能配置" });
    expect(uploadEntries).toHaveLength(2); // 页头主操作 + 空态引导
    await user.click(uploadEntries[1]);
    // controller 模式未传 uploadTitle，弹窗名用默认「上传个人技能」
    expect(await screen.findByRole("dialog", { name: "上传个人技能" })).toBeVisible();
  });

  it("卡片正文与操作区位于 body 容器内（skills.css 子代选择器的结构契约）", async () => {
    render(<PlazaSkillPanel />);
    const description = (await screen.findAllByText("个人上传的技能能力。"))[0];
    const card = description.closest(".skillCatalogCard");
    expect(description.closest(".skillCatalogCardBody")).not.toBeNull();
    const footer = card?.querySelector("footer");
    expect(footer?.closest(".skillCatalogCardBody")).not.toBeNull();
  });

  it("detail edit draft resets when switching selection", async () => {
    const user = userEvent.setup();
    render(<PlazaSkillPanel />);
    await user.click(screen.getByLabelText("查看 已绑定个人 Skill 详情"));
    let dialog = screen.getByRole("dialog", { name: "技能详情" });
    await user.click(within(dialog).getByRole("button", { name: "编辑技能信息" }));
    await user.clear(within(dialog).getByRole("textbox", { name: "编辑技能名称" }));
    await user.type(within(dialog).getByRole("textbox", { name: "编辑技能名称" }), "中途草稿");
    await user.click(within(dialog).getByRole("button", { name: "取消编辑" }));
    await user.click(within(dialog).getByRole("button", { name: "关闭" }));

    await user.click(screen.getByLabelText("查看 未绑定个人 Skill 详情"));
    dialog = screen.getByRole("dialog", { name: "技能详情" });
    await user.click(within(dialog).getByRole("button", { name: "编辑技能信息" }));
    expect(within(dialog).getByRole("textbox", { name: "编辑技能名称" })).toHaveValue("未绑定个人 Skill");
  });
});
