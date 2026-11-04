import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AppCenterListItem } from "../../api/client";
import { CreateAppDialog } from "./CreateAppDialog";

// 2026-11-02-001 重构后弹窗方式一为内置字典（不请求 task-prompt-config）；
// 2026-11-02-005 方式二接回真实广场看板（AppCenterPage 下传 plaza 列表），
// 选中看板卡走 onCreateFromTemplate（ZIP 模板注入编排），空白卡走空提示词会话。
// antd Modal rc-motion appear 阶段 opacity:0，可见性断言统一走 waitFor/findBy。

const plazaBoards: AppCenterListItem[] = [
  {
    id: 21,
    name: "销售经营看板",
    description: "销售趋势与商机转化",
    accessUrl: "https://demo.example/21",
    sourceType: "agent_html",
    shareMode: "scope",
    creatorName: "张三",
    mine: false,
    updatedAt: "2026-11-01T00:00:00Z",
    scopeSummary: "全员",
    publishVersionId: "21"
  },
  {
    id: 22,
    name: "工单分析看板",
    description: "工单量与积压排行",
    accessUrl: "https://demo.example/22",
    sourceType: "agent_html",
    shareMode: "scope",
    creatorName: "李四",
    mine: false,
    updatedAt: "2026-11-01T00:00:00Z",
    scopeSummary: "全员",
    publishVersionId: "22"
  }
];

function renderDialog(overrides: Partial<Parameters<typeof CreateAppDialog>[0]> = {}) {
  const props = {
    onClose: vi.fn(),
    onCreateAppSession: vi.fn().mockResolvedValue(true),
    onCreateFromTemplate: vi.fn().mockResolvedValue(true),
    plazaBoards,
    plazaLoading: false,
    ...overrides
  };
  return { ...render(<CreateAppDialog {...props} />), props };
}

describe("CreateAppDialog", () => {
  afterEach(() => {
    vi.clearAllMocks();
    localStorage.removeItem("hermes.theme");
  });

  it("applies the active theme inside the portaled modal", async () => {
    localStorage.setItem("hermes.theme", "dark");
    renderDialog();

    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    const themeScope = dialog.closest(".portalThemeScope");
    expect(themeScope).not.toBeNull();
    expect(themeScope).toHaveAttribute("data-theme", "dark");
  });

  it("renders banner, both creation sections, default tab tags and plaza board cards", async () => {
    renderDialog();

    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    await waitFor(() => expect(within(dialog).getByText("平台已连接：")).toBeVisible());
    expect(within(dialog).getByRole("heading", { name: "创建方式一：通过提示词新建" })).toBeInTheDocument();
    expect(within(dialog).getByRole("heading", { name: "创建方式二：模板库" })).toBeInTheDocument();
    // 分段控制器：4 个主题 Tab，默认 AI线索平台。
    expect(within(dialog).getByRole("tab", { name: "AI线索平台" })).toHaveAttribute("aria-selected", "true");
    expect(within(dialog).getByRole("tab", { name: "商机统计" })).toBeInTheDocument();
    expect(within(dialog).getByRole("tab", { name: "发货统计" })).toBeInTheDocument();
    expect(within(dialog).getByRole("tab", { name: "工单统计" })).toBeInTheDocument();
    // 默认 Tab 的快捷提示词 Tag。
    expect(within(dialog).getByRole("button", { name: "制造业线索按地市统计" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "线索跟进人员排行榜" })).toBeInTheDocument();
    // 方式二：真实广场看板卡（iframe 缩略契约）+ 从空白开始。
    expect(within(dialog).getByRole("button", { name: "使用广场模板 销售经营看板" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "使用广场模板 工单分析看板" })).toBeInTheDocument();
    expect(within(dialog).getByTitle("销售经营看板首页缩略图"))
      .toHaveAttribute("src", "/api/v1/app-center/21/index.html?embed=thumbnail");
    expect(within(dialog).getByRole("button", { name: "从空白开始" })).toBeInTheDocument();
    // 2026-11-02-001 的内置预设模板字典已退役。
    expect(within(dialog).queryByRole("button", { name: "销售漏斗" })).not.toBeInTheDocument();
    // 旧三段式结构不应再出现。
    expect(within(dialog).queryByRole("heading", { name: "看板介绍" })).not.toBeInTheDocument();
    expect(within(dialog).queryByText("任务示例")).not.toBeInTheDocument();
  });

  it("renders skeletons while the plaza list is loading and empty hint when blank", async () => {
    const { rerender } = render(
      <CreateAppDialog
        onClose={vi.fn()}
        onCreateAppSession={vi.fn().mockResolvedValue(true)}
        onCreateFromTemplate={vi.fn().mockResolvedValue(true)}
        plazaBoards={[]}
        plazaLoading
      />
    );
    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    expect(within(dialog).getByText("创建方式二：模板库")).toBeInTheDocument();
    // 加载中：骨架轨道，无看板卡、无空白卡（与骨架互斥渲染）。
    expect(within(dialog).queryByRole("button", { name: "从空白开始" })).not.toBeInTheDocument();

    rerender(
      <CreateAppDialog
        onClose={vi.fn()}
        onCreateAppSession={vi.fn().mockResolvedValue(true)}
        onCreateFromTemplate={vi.fn().mockResolvedValue(true)}
        plazaBoards={[]}
        plazaLoading={false}
      />
    );
    expect(await within(dialog).findByRole("button", { name: "从空白开始" })).toBeInTheDocument();
    expect(within(dialog).getByText("广场暂无共享看板，可直接从空白开始，或用方式一的提示词生成。")).toBeInTheDocument();
  });

  it("switches tag list with the active tab and clears the selected tag", async () => {
    renderDialog();

    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    const firstTag = await within(dialog).findByRole("button", { name: "制造业线索按地市统计" });
    fireEvent.click(firstTag);
    expect(firstTag).toHaveAttribute("aria-pressed", "true");
    expect(within(dialog).getByRole("button", { name: "生成看板" })).toBeEnabled();

    // 切换 Tab：列表切换为商机统计的 4 条，方式一已选 Tag 被清空。
    fireEvent.click(within(dialog).getByRole("tab", { name: "商机统计" }));
    expect(await within(dialog).findByRole("button", { name: "商机阶段漏斗分析" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "制造业线索按地市统计" })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "生成看板" })).toBeDisabled();
  });

  it("keeps tag selection single and mutually exclusive with plaza board cards", async () => {
    renderDialog();

    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    const tagA = await within(dialog).findByRole("button", { name: "制造业线索按地市统计" });
    fireEvent.click(tagA);
    const tagB = within(dialog).getByRole("button", { name: "AI线索处理情况统计" });
    fireEvent.click(tagB);
    expect(tagB).toHaveAttribute("aria-pressed", "true");
    expect(tagA).toHaveAttribute("aria-pressed", "false");

    // 选择广场看板卡后 Tag 选中态被取代（最后一次点击生效）。
    const boardCard = within(dialog).getByRole("button", { name: "使用广场模板 销售经营看板" });
    fireEvent.click(boardCard);
    expect(boardCard).toHaveAttribute("aria-pressed", "true");
    expect(tagB).toHaveAttribute("aria-pressed", "false");
  });

  it("submits the selected tag prompt via the generate button", async () => {
    const { props } = renderDialog();

    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    fireEvent.click(await within(dialog).findByRole("button", { name: "各渠道线索转化率" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "生成看板" }));

    await waitFor(() => expect(props.onCreateAppSession).toHaveBeenCalledWith("各渠道线索转化率"));
    expect(props.onCreateFromTemplate).not.toHaveBeenCalled();
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("submits a selected plaza board through the template pipeline", async () => {
    const { props } = renderDialog();

    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    fireEvent.click(await within(dialog).findByRole("button", { name: "使用广场模板 工单分析看板" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "生成看板" }));

    // 2026-11-02-005：广场看板卡走 ZIP 模板注入编排，不走纯提示词会话。
    await waitFor(() => expect(props.onCreateFromTemplate).toHaveBeenCalledTimes(1));
    expect(props.onCreateFromTemplate).toHaveBeenCalledWith(
      expect.objectContaining({ id: 22, name: "工单分析看板" })
    );
    expect(props.onCreateAppSession).not.toHaveBeenCalled();
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("keeps the dialog open after a template failure and allows retry", async () => {
    const onCreateFromTemplate = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const { props } = renderDialog({ onCreateFromTemplate });

    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    const boardCard = await within(dialog).findByRole("button", { name: "使用广场模板 销售经营看板" });
    fireEvent.click(boardCard);
    fireEvent.click(within(dialog).getByRole("button", { name: "生成看板" }));
    await waitFor(() => expect(screen.getByText("任务会话创建失败，请重试")).toBeVisible());
    expect(props.onClose).not.toHaveBeenCalled();

    // 失败后选中态保留，可直接重试。
    expect(boardCard).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(dialog).getByRole("button", { name: "生成看板" }));
    await waitFor(() => expect(onCreateFromTemplate).toHaveBeenCalledTimes(2));
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("submits an empty prompt from the blank template card", async () => {
    const { props } = renderDialog();

    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    fireEvent.click(await within(dialog).findByRole("button", { name: "从空白开始" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "生成看板" }));

    await waitFor(() => expect(props.onCreateAppSession).toHaveBeenCalledWith(""));
    expect(props.onCreateFromTemplate).not.toHaveBeenCalled();
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("keeps the dialog open after a failure and allows retry", async () => {
    const onCreateAppSession = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const { props } = renderDialog({ onCreateAppSession });

    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    const tag = await within(dialog).findByRole("button", { name: "超融合线索分类统计" });
    fireEvent.click(tag);
    fireEvent.click(within(dialog).getByRole("button", { name: "生成看板" }));
    await waitFor(() => expect(screen.getByText("任务会话创建失败，请重试")).toBeVisible());
    expect(props.onClose).not.toHaveBeenCalled();

    // 失败后选中态保留，可直接重试。
    expect(tag).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(within(dialog).getByRole("button", { name: "生成看板" }));
    await waitFor(() => expect(onCreateAppSession).toHaveBeenCalledTimes(2));
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("prevents duplicate submissions and disables all controls while pending", async () => {
    let resolveCreation: ((created: boolean) => void) | undefined;
    const onCreateAppSession = vi.fn().mockImplementation(() => new Promise<boolean>((resolve) => {
      resolveCreation = resolve;
    }));
    const { props } = renderDialog({ onCreateAppSession });

    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    const tag = await within(dialog).findByRole("button", { name: "AI线索处理情况统计" });
    const boardCard = within(dialog).getByRole("button", { name: "使用广场模板 工单分析看板" });
    // antd loading 按钮的 loading 图标会混入 accessible name，提交前保存按钮引用。
    const generate = within(dialog).getByRole("button", { name: "生成看板" });
    fireEvent.click(tag);
    fireEvent.click(generate);

    await waitFor(() => expect(tag).toBeDisabled());
    expect(boardCard).toBeDisabled();
    expect(within(dialog).getByRole("tab", { name: "商机统计" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "取消" })).toBeDisabled();
    expect(generate).toHaveClass("ant-btn-loading");
    // pending 中重复点击生成不产生第二次提交（rc-button loading 态阻止 onClick）。
    fireEvent.click(generate);
    expect(onCreateAppSession).toHaveBeenCalledTimes(1);

    resolveCreation?.(true);
    await waitFor(() => expect(props.onClose).toHaveBeenCalledOnce());
  });
});
