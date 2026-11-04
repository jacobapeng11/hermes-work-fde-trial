import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PersonalKnowledgeSummary } from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { PersonalKnowledgePanel } from "./PersonalKnowledgePanel";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}


function card(
  knowledgeBaseId: number,
  effectiveRole: PersonalKnowledgeSummary["effectiveRole"],
  runtimeEnabled = false
): PersonalKnowledgeSummary {
  return {
    knowledgeBaseId,
    name: effectiveRole === "owner" ? "我的案例库" : "共享资料库",
    description: "园区网案例",
    ownerUserId: effectiveRole === "owner" ? 1 : 2,
    ownerName: effectiveRole === "owner" ? "我" : "同事",
    effectiveRole,
    runtimeEnabled,
    fileCount: 3,
    readyFileCount: 2,
    visibility: "shared",
    status: "active",
    createdAt: "2026-07-20T08:00:00",
    updatedAt: "2026-07-20T08:00:00"
  };
}


describe("PersonalKnowledgePanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders owner actions and keeps retrieve-only cards non-enterable", async () => {
    vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({
      items: [card(1, "owner"), card(2, "retrieve")],
      total: 2
    });

    render(<PersonalKnowledgePanel runtimeWorkspaceId={7}/>);

    expect(await screen.findByText("我的案例库")).toBeVisible();
    expect(screen.getByRole("button", { name: "权限设置 我的案例库" })).toBeVisible();
    expect(screen.getByRole("button", { name: "删除 我的案例库" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "删除 共享资料库" })).not.toBeInTheDocument();
    expect(screen.getByText("仅勾选检索")).toBeVisible();
    expect(screen.getByRole("button", { name: /共享资料库/ })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "用于对话检索 共享资料库" })).toBeEnabled();
    expect(screen.getByText("无浏览权限")).toBeVisible();
    expect(screen.getAllByRole("switch", { name: /用于对话检索/ })).toHaveLength(2);
  });

  it("optimistically selects and reports next-turn activation", async () => {
    vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({
      items: [card(1, "owner")],
      total: 1
    });
    const update = vi.spyOn(userApi, "setPersonalKnowledgeEnabled").mockResolvedValue({
      runtimeWorkspaceId: 7,
      knowledgeBaseId: 1,
      enabled: true,
      effectiveFrom: "next_turn",
      warning: null
    });

    render(<PersonalKnowledgePanel runtimeWorkspaceId={7}/>);
    const checkbox = await screen.findByRole("switch", { name: "用于对话检索 我的案例库" });
    fireEvent.click(checkbox);

    expect(checkbox).toHaveAttribute("aria-checked", "true");
    await waitFor(() => expect(update).toHaveBeenCalledWith(7, 1, true));
    expect(await screen.findByText("已保存，将从下一轮对话生效")).toBeVisible();
  });

  it("rolls back one card when selection fails", async () => {
    vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({
      items: [card(1, "owner")],
      total: 1
    });
    vi.spyOn(userApi, "setPersonalKnowledgeEnabled").mockRejectedValue(new Error("保存失败"));

    render(<PersonalKnowledgePanel runtimeWorkspaceId={7}/>);
    const checkbox = await screen.findByRole("switch", { name: "用于对话检索 我的案例库" });
    fireEvent.click(checkbox);
    expect(checkbox).toHaveAttribute("aria-checked", "true");

    await waitFor(() => expect(checkbox).not.toBeChecked());
    expect(screen.getByText("保存失败")).toBeVisible();
  });

  it("keeps cards mounted and ignores a list value made stale by a toggle", async () => {
    const staleReload = deferred<{ items: PersonalKnowledgeSummary[]; total: number }>();
    vi.spyOn(userApi, "listPersonalKnowledge")
      .mockResolvedValueOnce({ items: [card(1, "owner")], total: 1 })
      .mockReturnValueOnce(staleReload.promise);
    const update = vi.spyOn(userApi, "setPersonalKnowledgeEnabled").mockImplementation(
      async (runtimeWorkspaceId, knowledgeBaseId, enabled) => ({
        runtimeWorkspaceId,
        knowledgeBaseId,
        enabled,
        effectiveFrom: "next_turn",
        warning: null
      })
    );

    const { rerender } = render(<PersonalKnowledgePanel runtimeWorkspaceId={7}/>);
    const checkbox = await screen.findByRole("switch", { name: "用于对话检索 我的案例库" });

    rerender(<PersonalKnowledgePanel runtimeWorkspaceId={8}/>);
    // antd Checkbox 的视觉 input 是 opacity:0（外观由 span 渲染），可见性断言改存在性。
    expect(screen.getByRole("switch", { name: "用于对话检索 我的案例库" })).toBeInTheDocument();
    fireEvent.click(checkbox);
    await waitFor(() => expect(update).toHaveBeenLastCalledWith(8, 1, true));

    staleReload.resolve({ items: [card(1, "owner", false)], total: 1 });
    await waitFor(() => expect(screen.queryByText("正在更新列表…")).not.toBeInTheDocument());
    expect(checkbox).toHaveAttribute("aria-checked", "true");

    fireEvent.click(checkbox);
    await waitFor(() => expect(update).toHaveBeenLastCalledWith(8, 1, false));
  });

  it("clears a card toggle error when its retry succeeds", async () => {
    vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({
      items: [card(1, "owner")],
      total: 1
    });
    vi.spyOn(userApi, "setPersonalKnowledgeEnabled")
      .mockRejectedValueOnce(new Error("保存失败"))
      .mockResolvedValueOnce({
        runtimeWorkspaceId: 7,
        knowledgeBaseId: 1,
        enabled: true,
        effectiveFrom: "next_turn",
        warning: null
      });

    render(<PersonalKnowledgePanel runtimeWorkspaceId={7}/>);
    const checkbox = await screen.findByRole("switch", { name: "用于对话检索 我的案例库" });
    fireEvent.click(checkbox);
    expect(await screen.findByText("保存失败")).toBeVisible();

    fireEvent.click(checkbox);
    expect(await screen.findByText("已保存，将从下一轮对话生效")).toBeVisible();
    expect(screen.queryByText("保存失败")).not.toBeInTheDocument();
  });

  it("creates a knowledge base and opens its detail", async () => {
    vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({ items: [], total: 0 });
    vi.spyOn(userApi, "createPersonalKnowledge").mockResolvedValue({
      ...card(9, "owner"),
      canEnter: true,
      canPreview: true,
      canDownload: true,
      canManage: true
    });
    vi.spyOn(userApi, "personalKnowledgeDetail").mockResolvedValue({
      ...card(9, "owner"),
      canEnter: true,
      canPreview: true,
      canDownload: true,
      canManage: true
    });
    vi.spyOn(userApi, "personalKnowledgeFiles").mockResolvedValue({ items: [], total: 0 });

    // 页头「新建知识库」按钮已上移 KnowledgePage，这里用递增信号触发创建弹窗。
    render(<PersonalKnowledgePanel runtimeWorkspaceId={7} createTrigger={1}/>);
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "我的案例库" } });
    fireEvent.click(screen.getByRole("button", { name: "创建并上传文件" }));

    await waitFor(() => expect(userApi.createPersonalKnowledge).toHaveBeenCalledWith({
      name: "我的案例库",
      description: null
    }));
    expect(await screen.findByText("还没有文件")).toBeVisible();
  });

  it("keeps the create dialog open for content clicks and closes it from the backdrop", async () => {
    vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({ items: [], total: 0 });

    render(<PersonalKnowledgePanel runtimeWorkspaceId={7} createTrigger={1}/>);

    const dialog = screen.getByRole("dialog", { name: "创建个人知识库" });
    fireEvent.click(dialog);
    // antd Modal 打开动画（rc-motion）在 jsdom 中需等待推进后再断言可见性。
    await waitFor(() => expect(dialog).toBeVisible());

    fireEvent.click(dialog.parentElement as HTMLElement);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "创建个人知识库" })).not.toBeInTheDocument());
  });

  it("distinguishes filtered empty results and clears the active filters", async () => {
    const load = vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({ items: [], total: 0 });

    render(<PersonalKnowledgePanel runtimeWorkspaceId={7}/>);
    expect(await screen.findByText("从第一个个人知识库开始")).toBeVisible();

    fireEvent.change(screen.getByRole("textbox", { name: "搜索个人知识库" }), { target: { value: "不存在" } });
    await waitFor(() => expect(load).toHaveBeenLastCalledWith(7, "all", "不存在"));
    expect(await screen.findByText("未找到匹配的知识库")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "清除筛选" }));
    await waitFor(() => expect(load).toHaveBeenLastCalledWith(7, "all", ""));
    expect(await screen.findByText("从第一个个人知识库开始")).toBeVisible();
  });

  it("edits an owned knowledge base in an application dialog", async () => {
    vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({ items: [card(1, "owner")], total: 1 });
    const update = vi.spyOn(userApi, "updatePersonalKnowledge").mockResolvedValue({
      ...card(1, "owner"),
      name: "更新后的案例库",
      canEnter: true,
      canPreview: true,
      canDownload: true,
      canManage: true
    });
    const prompt = vi.spyOn(window, "prompt");

    render(<PersonalKnowledgePanel runtimeWorkspaceId={7}/>);
    fireEvent.click(await screen.findByRole("button", { name: "编辑 我的案例库" }));

    const dialog = screen.getByRole("dialog", { name: "编辑知识库" });
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "更新后的案例库" } });
    fireEvent.change(screen.getByLabelText("描述"), { target: { value: "更新后的说明" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(update).toHaveBeenCalledWith(1, { name: "更新后的案例库", description: "更新后的说明" }));
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(prompt).not.toHaveBeenCalled();
  });

  it("confirms and removes an owned knowledge base from the card list", async () => {
    vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({
      items: [card(1, "owner")],
      total: 1
    });
    const remove = vi.spyOn(userApi, "deletePersonalKnowledge").mockResolvedValue(undefined);

    render(<PersonalKnowledgePanel runtimeWorkspaceId={7}/>);
    fireEvent.click(await screen.findByRole("button", { name: "删除 我的案例库" }));
    await waitFor(() => expect(screen.getByRole("dialog", { name: "永久删除知识库" })).toBeVisible());

    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(remove).not.toHaveBeenCalled();
    expect(screen.getByText("我的案例库")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "删除 我的案例库" }));
    fireEvent.click(screen.getByRole("button", { name: "确认永久删除" }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith(1));
    await waitFor(() => expect(screen.queryByText("我的案例库")).not.toBeInTheDocument());
    expect(screen.queryByRole("dialog", { name: "永久删除知识库" })).not.toBeInTheDocument();
  });

  it("keeps the delete dialog open after failure and allows retry", async () => {
    vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({
      items: [card(1, "owner")],
      total: 1
    });
    const remove = vi.spyOn(userApi, "deletePersonalKnowledge")
      .mockRejectedValueOnce(new Error("外部数据集删除失败"))
      .mockResolvedValueOnce(undefined);

    render(<PersonalKnowledgePanel runtimeWorkspaceId={7}/>);
    fireEvent.click(await screen.findByRole("button", { name: "删除 我的案例库" }));
    fireEvent.click(screen.getByRole("button", { name: "确认永久删除" }));

    expect(await screen.findByText("外部数据集删除失败")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("外部数据集删除失败")).toBeVisible());
    expect(screen.getAllByText("我的案例库").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "确认永久删除" }));

    await waitFor(() => expect(remove).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("我的案例库")).not.toBeInTheDocument());
  });

  it("disables deletion while the runtime selection is being saved", async () => {
    vi.spyOn(userApi, "listPersonalKnowledge").mockResolvedValue({
      items: [card(1, "owner")],
      total: 1
    });
    const update = deferred<{
      runtimeWorkspaceId: number;
      knowledgeBaseId: number;
      enabled: boolean;
      effectiveFrom: "next_turn";
      warning: null;
    }>();
    vi.spyOn(userApi, "setPersonalKnowledgeEnabled").mockReturnValue(update.promise);

    render(<PersonalKnowledgePanel runtimeWorkspaceId={7}/>);
    fireEvent.click(await screen.findByRole("switch", { name: "用于对话检索 我的案例库" }));

    expect(screen.getByRole("button", { name: "删除 我的案例库" })).toBeDisabled();
    update.resolve({
      runtimeWorkspaceId: 7,
      knowledgeBaseId: 1,
      enabled: true,
      effectiveFrom: "next_turn",
      warning: null
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "删除 我的案例库" })).toBeEnabled());
  });

  it("does not restore a deleted card from an older list response", async () => {
    const staleReload = deferred<{ items: PersonalKnowledgeSummary[]; total: number }>();
    vi.spyOn(userApi, "listPersonalKnowledge")
      .mockResolvedValueOnce({ items: [card(1, "owner")], total: 1 })
      .mockReturnValueOnce(staleReload.promise);
    vi.spyOn(userApi, "deletePersonalKnowledge").mockResolvedValue(undefined);

    const { rerender } = render(<PersonalKnowledgePanel runtimeWorkspaceId={7}/>);
    await screen.findByRole("button", { name: "删除 我的案例库" });
    rerender(<PersonalKnowledgePanel runtimeWorkspaceId={8}/>);

    fireEvent.click(screen.getByRole("button", { name: "删除 我的案例库" }));
    fireEvent.click(screen.getByRole("button", { name: "确认永久删除" }));
    await waitFor(() => expect(screen.queryByText("我的案例库")).not.toBeInTheDocument());

    staleReload.resolve({ items: [card(1, "owner")], total: 1 });
    await waitFor(() => expect(screen.queryByText("我的案例库")).not.toBeInTheDocument());
  });
});
