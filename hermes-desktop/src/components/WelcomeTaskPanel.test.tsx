import { fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TaskPromptConfigState } from "@hermes-cloud/schemas";
import type { AppCenterListItem } from "../api/client";
import { userApi } from "../api/client";
import { WelcomeTaskPanel } from "./WelcomeTaskPanel";

vi.mock("../api/client", () => ({
  userApi: {
    listApps: vi.fn(),
    taskPromptConfig: vi.fn()
  }
}));

const taskPromptConfig: TaskPromptConfigState = {
  revision: 7,
  config: {
    tabs: [
      {
        label: "看板",
        description: "围绕经营数据、项目进度和业务指标生成可交互看板。",
        isDefault: true,
        showBoardGallery: true,
        topics: [
          {
            title: "销售分析",
            tasks: [
              { summary: "生成销售趋势", detail: "请生成近 30 天销售趋势看板。" },
              { summary: "分析区域达成率", detail: "请按区域分析销售目标达成率。" }
            ]
          },
          {
            title: "项目进度",
            tasks: [
              { summary: "生成项目里程碑", detail: "请生成当前项目的里程碑进度看板。" }
            ]
          }
        ]
      },
      {
        label: "办公",
        description: "处理文档、流程和日常办公协作。",
        isDefault: false,
        showBoardGallery: false,
        topics: [
          {
            title: "文档创作",
            tasks: [
              { summary: "写项目周报", detail: "请根据本周任务记录生成项目周报。" }
            ]
          }
        ]
      }
    ]
  }
};

function board(id: number, name: string, mine = false): AppCenterListItem {
  return {
    id,
    name,
    description: `${name}描述`,
    accessUrl: `/board/${id}`,
    sourceType: "agent_html",
    shareMode: "scope",
    creatorName: mine ? "我" : "其他用户",
    mine,
    updatedAt: `2026-10-29T00:00:${String(id).padStart(2, "0")}Z`,
    scopeSummary: "全员",
    publishVersionId: String(id),
    scope: { organization: true, departmentIds: [], userIds: [] }
  };
}

function renderPanel(overrides: Partial<ComponentProps<typeof WelcomeTaskPanel>> = {}) {
  const props = {
    ownerIdentity: null,
    onTaskPreview: vi.fn(),
    onTaskCommit: vi.fn(),
    onOpenBoard: vi.fn(),
    onViewAllBoards: vi.fn(),
    ...overrides
  };
  return { ...render(<WelcomeTaskPanel {...props} />), props };
}

describe("WelcomeTaskPanel", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders greeting, mode switch, guide card and plaza board gallery", async () => {
    vi.mocked(userApi.taskPromptConfig).mockResolvedValue(taskPromptConfig);
    vi.mocked(userApi.listApps).mockResolvedValue([
      board(1, "我的看板", true),
      board(2, "广场一"),
      board(3, "广场二")
    ]);
    renderPanel({ ownerIdentity: "示例用户乙-10002" });

    expect(screen.getByRole("heading", { name: "示例用户乙-10002，您好 👋 我是您的数字分身" })).toBeVisible();
    expect(await screen.findByText("围绕经营数据、项目进度和业务指标生成可交互看板。")).toBeVisible();
    expect(await screen.findByRole("tab", { name: "看板" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "办公" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: "销售分析" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: "任务示例 生成销售趋势" })).toBeVisible();

    const thumbs = await screen.findAllByRole("link", { name: /^打开看板 广场/ });
    expect(thumbs).toHaveLength(2);
    expect(screen.getByRole("heading", { name: "看板广场案例参考" })).toBeVisible();
    expect(screen.getByTitle("广场一首页缩略图"))
      .toHaveAttribute("src", "/api/v1/app-center/2/index.html?embed=thumbnail");
    expect(screen.getByText("广场一")).toBeVisible();
    // 2026-11-02-004：卡片仅显示标题（描述移入悬停 title 全文）
    expect(screen.getByRole("link", { name: "打开看板 广场一" }))
      .toHaveAttribute("title", "广场一\n广场一描述");
  });

  it("selects a tag exclusively and resets selection when switching topics", async () => {
    vi.mocked(userApi.taskPromptConfig).mockResolvedValue(taskPromptConfig);
    vi.mocked(userApi.listApps).mockResolvedValue([]);
    const { props } = renderPanel();

    const tagA = await screen.findByRole("button", { name: "任务示例 生成销售趋势" });
    fireEvent.click(tagA);
    expect(tagA).toHaveAttribute("aria-pressed", "true");
    expect(props.onTaskCommit).toHaveBeenCalledWith("请生成近 30 天销售趋势看板。");

    // 同组单选：hover 预览后点击另一 Tag，前者取消高亮且预览被清除。
    const tagB = screen.getByRole("button", { name: "任务示例 分析区域达成率" });
    fireEvent.mouseEnter(tagB);
    expect(props.onTaskPreview).toHaveBeenLastCalledWith("请按区域分析销售目标达成率。");
    fireEvent.click(tagB);
    expect(tagB).toHaveAttribute("aria-pressed", "true");
    expect(tagA).toHaveAttribute("aria-pressed", "false");
    expect(props.onTaskPreview).toHaveBeenLastCalledWith(null);

    // 切换主题：Tag 列表无缝切换并重置选中状态，预览保持清除。
    fireEvent.click(screen.getByRole("tab", { name: "项目进度" }));
    expect(await screen.findByRole("button", { name: "任务示例 生成项目里程碑" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "任务示例 生成销售趋势" })).not.toBeInTheDocument();
    expect(props.onTaskPreview).toHaveBeenLastCalledWith(null);
  });

  it("previews configured detail on hover and restores it on leave", async () => {
    vi.mocked(userApi.taskPromptConfig).mockResolvedValue(taskPromptConfig);
    vi.mocked(userApi.listApps).mockResolvedValue([]);
    const { props } = renderPanel();

    const task = await screen.findByRole("button", { name: "任务示例 生成销售趋势" });
    fireEvent.mouseEnter(task);
    expect(props.onTaskPreview).toHaveBeenLastCalledWith("请生成近 30 天销售趋势看板。");
    fireEvent.mouseLeave(task);
    expect(props.onTaskPreview).toHaveBeenLastCalledWith(null);
  });

  it("commits configured detail without keeping preview tone", async () => {
    vi.mocked(userApi.taskPromptConfig).mockResolvedValue(taskPromptConfig);
    vi.mocked(userApi.listApps).mockResolvedValue([]);
    const { props } = renderPanel();

    const task = await screen.findByRole("button", { name: "任务示例 分析区域达成率" });
    fireEvent.mouseEnter(task);
    fireEvent.click(task);

    expect(props.onTaskPreview).toHaveBeenLastCalledWith(null);
    expect(props.onTaskCommit).toHaveBeenCalledWith("请按区域分析销售目标达成率。");
  });

  it("switches to the office mode with muted tip and office templates", async () => {
    vi.mocked(userApi.taskPromptConfig).mockResolvedValue(taskPromptConfig);
    vi.mocked(userApi.listApps).mockResolvedValue([board(1, "我的看板", true)]);
    const { props } = renderPanel();

    fireEvent.click(await screen.findByRole("tab", { name: "办公" }));

    expect(await screen.findByRole("button", { name: "任务示例 写项目周报" })).toBeVisible();
    expect(screen.getByText("处理文档、流程和日常办公协作。")).toBeVisible();
    // 办公模式不渲染案例广场，改渲染推荐办公模板。
    expect(screen.queryByRole("heading", { name: "看板广场案例参考" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "推荐办公模板" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "使用办公模板 周报生成器" }));
    expect(props.onTaskCommit).toHaveBeenCalledWith("请根据我本周的工作内容，润色生成一份结构清晰的周报。");
  });

  it("emits the view-all callback for the board gallery", async () => {
    vi.mocked(userApi.taskPromptConfig).mockResolvedValue(taskPromptConfig);
    vi.mocked(userApi.listApps).mockResolvedValue([board(9, "经营看板")]);
    const { props } = renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "查看全部" }));
    expect(props.onViewAllBoards).toHaveBeenCalledOnce();
  });

  it("hides the board gallery section when the plaza has no boards", async () => {
    vi.mocked(userApi.taskPromptConfig).mockResolvedValue(taskPromptConfig);
    vi.mocked(userApi.listApps).mockResolvedValue([board(1, "我的看板", true)]);
    renderPanel();

    await screen.findByRole("button", { name: "任务示例 生成销售趋势" });
    await waitForBoardsHidden();
    expect(screen.queryByRole("heading", { name: "看板广场案例参考" })).not.toBeInTheDocument();
  });

  it("shows a safe empty state when the organization has no task prompt configuration", async () => {
    vi.mocked(userApi.taskPromptConfig).mockResolvedValue({ revision: 0, config: null });
    renderPanel();

    expect(await screen.findByText("尚未配置任务提示，可直接输入任务。")).toBeVisible();
    expect(screen.queryByRole("tab", { name: "看板" })).not.toBeInTheDocument();
  });

  it("opens a plaza board through the host callback", async () => {
    vi.mocked(userApi.taskPromptConfig).mockResolvedValue(taskPromptConfig);
    vi.mocked(userApi.listApps).mockResolvedValue([board(9, "经营看板")]);
    const { props } = renderPanel();

    fireEvent.click(await screen.findByRole("link", { name: "打开看板 经营看板" }));
    expect(props.onOpenBoard).toHaveBeenCalledWith(expect.objectContaining({ id: 9 }));
  });
});

async function waitForBoardsHidden() {
  // listApps resolve 后（boards 非 null 且为空）区块卸载；给一次微任务刷新窗口。
  await new Promise((resolve) => setTimeout(resolve, 0));
}
