import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { userApi, type AppCenterListItem } from "../../api/client";
import { AppCenterPage } from "./AppCenterPage";

vi.mock("../../api/client", () => ({
  userApi: {
    listApps: vi.fn(),
    copyApp: vi.fn(),
    deleteApp: vi.fn(),
    downloadAppPackage: vi.fn(),
  }
}));

const apps: AppCenterListItem[] = [
  {
    id: 1,
    name: "销售看板",
    description: "销售数据实时概览",
    accessUrl: "https://demo.example/api/v1/user/data-apps/1/index.html",
    sourceType: "agent_html",
    shareMode: "scope",
    creatorName: "创建者",
    mine: true,
    updatedAt: "2026-10-16T12:00:00",
    scopeSummary: "全员",
    publishVersionId: "3567",
    scope: { organization: true, departmentIds: [], userIds: [] }
  },
  {
    id: 2,
    name: "广场报表",
    description: "共享经营报表",
    accessUrl: "https://demo.example/api/v1/user/data-apps/2/index.html",
    sourceType: "manual_zip",
    shareMode: "scope",
    creatorName: "其他用户",
    mine: false,
    copiedFromAppId: 1,
    copiedFromCreatorName: "创建者",
    updatedAt: "2026-10-16T13:00:00",
    scopeSummary: "2 人",
    publishVersionId: "3568",
    scope: { organization: false, departmentIds: [], userIds: [8] }
  }
];

describe("AppCenterPage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  // 卡片参照用户 demo 调优后：封面遮罩主操作 + 角标 + link 主操作 + 图标副操作组；
  // mine 主操作为打开看板，plaza 主操作（含点击卡片）为克隆。
  it("separates owned and shared apps and emits edit commands", async () => {
    vi.mocked(userApi.listApps).mockResolvedValue(apps);
    vi.mocked(userApi.copyApp).mockResolvedValue({
      ...apps[1],
      id: 3,
      mine: true,
      name: "广场报表-副本"
    });
    const onEditApp = vi.fn();
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<AppCenterPage onEditApp={onEditApp} onCreateAppSession={async () => true}/>);

    expect(await screen.findByText("销售看板")).toBeVisible();
    expect(screen.getByText("销售数据实时概览")).toBeVisible();
    expect(screen.queryByText(/AI 生成/)).not.toBeInTheDocument();
    expect(screen.queryByText(/2026\/10\/16/)).not.toBeInTheDocument();
    expect(screen.getByTitle("销售看板首页缩略图"))
      .toHaveAttribute("src", "/api/v1/app-center/1/index.html?embed=thumbnail");
    expect(screen.queryByText("广场报表")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "更新 销售看板" })).not.toBeInTheDocument();
    const mineCard = screen.getByTitle("销售看板首页缩略图").closest(".appCenterCard")!;
    // 014 §14：卡片左上角归属角标已按用户需求移除，归属由 Tab 表达。
    expect(within(mineCard).queryByText("我的看板")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除 销售看板" })).toHaveClass("appCardDelete");
    expect(screen.getByRole("button", { name: "编辑 销售看板" })).toHaveClass("ant-btn-text");
    expect(screen.getByRole("button", { name: "权限 销售看板" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "进入看板 销售看板" })).toBeInTheDocument();
    const footer = screen.getByRole("button", { name: "进入看板 销售看板" }).closest("footer");
    expect(footer).toHaveClass("appCenterCardFooter");
    // 底部操作区：主操作 + 编辑/权限/下载/复制链接/删除（删除按用户要求移入操作组）
    expect(footer?.querySelectorAll("button")).toHaveLength(6);
    fireEvent.click(screen.getByRole("button", { name: "编辑 销售看板" }));
    expect(onEditApp).toHaveBeenCalledWith(apps[0]);

    // mine 卡片点击 = 打开看板
    fireEvent.click(screen.getByText("销售看板"));
    expect(open).toHaveBeenCalledWith(
      "https://demo.example/api/v1/user/data-apps/1/index.html",
      "_blank",
      "noopener,noreferrer"
    );

    // plaza 卡片主操作 = 克隆（不再直接打开）
    fireEvent.click(screen.getByRole("tab", { name: /看板广场/ }));
    expect(await screen.findByText("广场报表")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "克隆看板 广场报表" }));
    await waitFor(() => expect(userApi.copyApp).toHaveBeenCalledWith(2));
  });

  it("copies a shared app into the mine tab", async () => {
    const created = { ...apps[1], id: 3, mine: true, name: "广场报表-副本" };
    vi.mocked(userApi.listApps).mockResolvedValue(apps);
    vi.mocked(userApi.copyApp).mockResolvedValue(created);
    render(<AppCenterPage onEditApp={() => undefined} onCreateAppSession={async () => true}/>);

    await screen.findByText("销售看板");
    fireEvent.click(screen.getByRole("tab", { name: /看板广场/ }));
    fireEvent.click(await screen.findByRole("button", { name: "克隆看板 广场报表" }));
    await waitFor(() => expect(userApi.copyApp).toHaveBeenCalledWith(2));
    expect(await screen.findByText("广场报表-副本")).toBeVisible();
  });

  it("copies a link from every owned app card", async () => {
    vi.mocked(userApi.listApps).mockResolvedValue(apps);
    const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true
    });
    render(<AppCenterPage onEditApp={() => undefined} onCreateAppSession={async () => true}/>);

    await screen.findByText("销售看板");
    fireEvent.click(screen.getByRole("button", { name: "复制链接 销售看板" }));

    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        "https://demo.example/api/v1/user/data-apps/1/index.html"
      ));
    expect(await screen.findByText("链接已复制")).toBeVisible();
    if (originalClipboard) {
      Object.defineProperty(navigator, "clipboard", originalClipboard);
    } else {
      Reflect.deleteProperty(navigator, "clipboard");
    }
  });

  // T3 起删除确认从卡片内嵌小确认框改为危险确认弹窗（对齐技能/知识库基线）。
  // antd Modal 的 rc-motion appear 阶段 opacity:0，可见性断言统一走 waitFor/findBy。
  it("confirms deletion through the danger modal", async () => {
    vi.mocked(userApi.listApps).mockResolvedValue(apps);
    vi.mocked(userApi.deleteApp).mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm");
    render(<AppCenterPage onEditApp={() => undefined} onCreateAppSession={async () => true}/>);

    await screen.findByText("销售看板");
    fireEvent.click(screen.getByRole("button", { name: "删除 销售看板" }));
    const modal = await screen.findByRole("dialog", { name: "删除看板" });
    await waitFor(() => expect(within(modal).getByText("即将删除")).toBeVisible());
    expect(within(modal).getByText("销售看板")).toBeVisible();
    expect(userApi.deleteApp).not.toHaveBeenCalled();

    fireEvent.click(within(modal).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "删除看板" })).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "删除 销售看板" }));
    const modalAgain = await screen.findByRole("dialog", { name: "删除看板" });
    fireEvent.click(within(modalAgain).getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(userApi.deleteApp).toHaveBeenCalledWith(1));
    await waitFor(() => expect(screen.queryByText("销售看板")).not.toBeInTheDocument());
    expect(confirm).not.toHaveBeenCalled();
  });
});
