import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { userApi } from "../../api/client";
import { KnowledgePage } from "./KnowledgePage";

describe("KnowledgePage", () => {
  afterEach(() => vi.restoreAllMocks());

  it("loads disabled state and saves an enabled toggle", async () => {
    vi.spyOn(userApi, "enterpriseKnowledgeSettings").mockResolvedValue({ runtimeWorkspaceId: 7, enabled: false, mode: "full" });
    const update = vi.spyOn(userApi, "updateEnterpriseKnowledgeSettings").mockResolvedValue({ runtimeWorkspaceId: 7, enabled: true, mode: "full" });
    render(<KnowledgePage runtimeWorkspaceId={7}/>);
    const control = await screen.findByRole("switch", { name: "用于对话检索" });
    await waitFor(() => expect(control).toHaveAttribute("aria-checked", "false"));
    expect(screen.getByText("未启用")).toBeVisible();
    expect(screen.getByText("仅根据你的身份权限，访问可授权的企业知识。")).toBeVisible();
    expect(screen.queryByText("遵循现有权限")).not.toBeInTheDocument();
    expect(screen.queryByText("账号级设置")).not.toBeInTheDocument();
    fireEvent.click(control);
    await waitFor(() => expect(update).toHaveBeenCalledWith(7, { enabled: true }));
    await waitFor(() => expect(control).toHaveAttribute("aria-checked", "true"));
    expect(screen.getByText("已启用")).toBeVisible();
  });

  it("loads the personal knowledge tab without another enterprise request", async () => {
    const load = vi.spyOn(userApi, "enterpriseKnowledgeSettings").mockResolvedValue({ runtimeWorkspaceId: 7, enabled: false, mode: "full" });
    vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({ items: [], total: 0 });
    render(<KnowledgePage runtimeWorkspaceId={7}/>);
    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("tab", { name: /个人知识库/ }));
    expect(await screen.findByText("从第一个个人知识库开始")).toBeInTheDocument();
    expect(screen.getByRole("tabpanel", { name: /个人知识库/ })).toContainElement(document.getElementById("personal-knowledge-panel"));
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("shows the create button only on the personal list layer", async () => {
    vi.spyOn(userApi, "enterpriseKnowledgeSettings").mockResolvedValue({ runtimeWorkspaceId: 7, enabled: false, mode: "full" });
    vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({ items: [{
      knowledgeBaseId: 3,
      name: "我的案例库",
      description: "园区网案例",
      ownerUserId: 1,
      ownerName: "我",
      effectiveRole: "owner",
      runtimeEnabled: false,
      fileCount: 3,
      readyFileCount: 2,
      visibility: "private",
      status: "active",
      createdAt: "2026-07-20T08:00:00",
      updatedAt: "2026-07-20T08:00:00"
    }], total: 1 });
    vi.spyOn(userApi, "personalKnowledgeDetail").mockResolvedValue({
      knowledgeBaseId: 3,
      name: "我的案例库",
      description: "园区网案例",
      ownerUserId: 1,
      ownerName: "我",
      effectiveRole: "owner",
      runtimeEnabled: false,
      fileCount: 3,
      readyFileCount: 2,
      visibility: "private",
      status: "active",
      createdAt: "2026-07-20T08:00:00",
      updatedAt: "2026-07-20T08:00:00",
      canEnter: true,
      canPreview: true,
      canDownload: true,
      canManage: true
    });
    vi.spyOn(userApi, "personalKnowledgeFiles").mockResolvedValue({ items: [], total: 0 });
    render(<KnowledgePage runtimeWorkspaceId={7}/>);

    // 企业知识库 tab：不展示新建按钮。
    expect(screen.queryByRole("button", { name: "新建知识库" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /个人知识库/ }));
    expect(await screen.findByRole("button", { name: "新建知识库" })).toBeInTheDocument();

    // 进入详情页：隐藏；返回列表：恢复。
    fireEvent.click(screen.getByRole("button", { name: "进入 我的案例库" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "新建知识库" })).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "返回个人知识库" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "新建知识库" })).toBeInTheDocument());
  });

  it("shows an enterprise load error and retries without changing tabs", async () => {
    const load = vi.spyOn(userApi, "enterpriseKnowledgeSettings")
      .mockRejectedValueOnce(new Error("企业设置读取失败"))
      .mockResolvedValueOnce({ runtimeWorkspaceId: 7, enabled: true, mode: "full" });

    render(<KnowledgePage runtimeWorkspaceId={7}/>);

    expect(await screen.findByRole("alert")).toHaveTextContent("企业设置读取失败");
    fireEvent.click(screen.getByRole("button", { name: "重新读取" }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("已启用")).toBeVisible();
    expect(screen.getByRole("tab", { name: /企业知识库/ })).toHaveAttribute("aria-selected", "true");
  });

  it("keeps enterprise access separate from application-owned knowledge", async () => {
    render(
      <KnowledgePage
        runtimeWorkspaceId={null}
        adapter={{
          context: "application",
          getEnterpriseSettings: async () => ({ enabled: false }),
          updateEnterpriseSettings: async (enabled) => ({ enabled }),
        }}
      />
    );
    expect(await screen.findByRole("switch", {
      name: "用于应用 Agent 检索"
    })).toBeVisible();
    expect(screen.queryByText("应用绑定知识库")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "个人知识库" })).not.toBeInTheDocument();
  });
});
