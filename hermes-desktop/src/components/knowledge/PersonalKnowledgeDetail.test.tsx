import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PersonalKnowledgeDetail as Detail, PersonalKnowledgeFileSummary } from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { PersonalKnowledgeDetail } from "./PersonalKnowledgeDetail";

function detail(overrides: Partial<Detail> = {}): Detail {
  return {
    knowledgeBaseId: 9,
    name: "测试新知识库",
    description: "测试",
    ownerUserId: 1,
    ownerName: "示例用户乙",
    effectiveRole: "owner",
    runtimeEnabled: false,
    fileCount: 1,
    readyFileCount: 0,
    visibility: "private",
    status: "active",
    createdAt: "2026-07-21T01:00:00",
    updatedAt: "2026-07-21T01:39:24",
    canEnter: true,
    canPreview: true,
    canDownload: true,
    canManage: true,
    ...overrides
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

const file: PersonalKnowledgeFileSummary = {
  fileId: 18,
  filename: "江苏区制造业AI线索.xlsx",
  sizeBytes: 460 * 1024,
  mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  status: "failed",
  uploaderId: 1,
  uploaderName: "示例用户乙",
  updatedAt: "2026-07-21T01:39:24"
};

describe("PersonalKnowledgeDetail", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders the migrated owner header, file status, and upload dialog", async () => {
    vi.spyOn(userApi, "personalKnowledgeDetail").mockResolvedValue(detail());
    vi.spyOn(userApi, "personalKnowledgeFiles").mockResolvedValue({ items: [file], total: 1 });
    const onBack = vi.fn();

    render(<PersonalKnowledgeDetail knowledgeBaseId={9} onBack={onBack}/>);

    expect(await screen.findByRole("heading", { name: "测试新知识库" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "返回个人知识库" }));
    expect(onBack).toHaveBeenCalledTimes(1);
    expect(screen.getByText("我创建的")).toBeVisible();
    // antd 图标 svg 尺寸由 font-size 控制（无 width 属性），断言退化为图标存在。
    expect(screen.getByRole("button", { name: "编辑知识库" }).querySelector("svg")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "权限设置" })).toBeVisible();
    expect(screen.getAllByText("解析失败").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "预览 江苏区制造业AI线索.xlsx" }).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "上传文件" }));
    // antd Modal 打开动画（rc-motion）在 jsdom 中需等待推进后再断言可见性。
    await waitFor(() => expect(screen.getByRole("dialog", { name: "上传文件" })).toBeVisible());
  });

  it("hides owner-only and download actions from preview users", async () => {
    vi.spyOn(userApi, "personalKnowledgeDetail").mockResolvedValue(detail({
      effectiveRole: "preview",
      canDownload: false,
      canManage: false,
      visibility: "shared"
    }));
    vi.spyOn(userApi, "personalKnowledgeFiles").mockResolvedValue({ items: [file], total: 1 });

    render(<PersonalKnowledgeDetail knowledgeBaseId={9} onBack={vi.fn()}/>);

    expect(await screen.findByText("仅在线预览")).toBeVisible();
    expect(screen.queryByRole("button", { name: "上传文件" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "下载 江苏区制造业AI线索.xlsx" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重命名 江苏区制造业AI线索.xlsx" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删除 江苏区制造业AI线索.xlsx" })).not.toBeInTheDocument();
  });

  it("ignores an older detail and file list after the selected knowledge base changes", async () => {
    const oldDetail = deferred<Detail>();
    const oldFiles = deferred<{ items: PersonalKnowledgeFileSummary[]; total: number }>();
    vi.spyOn(userApi, "personalKnowledgeDetail")
      .mockReturnValueOnce(oldDetail.promise)
      .mockResolvedValueOnce(detail({ knowledgeBaseId: 10, name: "新的知识库" }));
    vi.spyOn(userApi, "personalKnowledgeFiles")
      .mockReturnValueOnce(oldFiles.promise)
      .mockResolvedValueOnce({ items: [], total: 0 });

    const rendered = render(<PersonalKnowledgeDetail knowledgeBaseId={9} onBack={vi.fn()}/>);
    rendered.rerender(<PersonalKnowledgeDetail knowledgeBaseId={10} onBack={vi.fn()}/>);
    expect(await screen.findByRole("heading", { name: "新的知识库" })).toBeVisible();

    oldDetail.resolve(detail({ name: "过期知识库" }));
    oldFiles.resolve({ items: [file], total: 1 });
    await Promise.resolve();

    expect(screen.queryByRole("heading", { name: "过期知识库" })).not.toBeInTheDocument();
    expect(screen.queryByText(file.filename)).not.toBeInTheDocument();
  });

  it("opens Office files with the personal-knowledge OnlyOffice session", async () => {
    vi.spyOn(userApi, "personalKnowledgeDetail").mockResolvedValue(detail());
    vi.spyOn(userApi, "personalKnowledgeFiles").mockResolvedValue({ items: [file], total: 1 });
    const officePreview = vi.spyOn(userApi, "personalKnowledgeOfficePreviewSession").mockResolvedValue({
      documentServerUrl: "http://onlyoffice.example.test",
      config: { documentType: "cell" }
    });
    const blobPreview = vi.spyOn(userApi, "personalKnowledgeFileContent");

    render(<PersonalKnowledgeDetail knowledgeBaseId={9} onBack={vi.fn()}/>);
    fireEvent.click((await screen.findAllByRole("button", { name: "预览 江苏区制造业AI线索.xlsx" }))[0]);

    await waitFor(() => expect(officePreview).toHaveBeenCalledWith(9, 18));
    expect(blobPreview).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "江苏区制造业AI线索.xlsx" })).toBeVisible();
  });

  it("edits, renames, and deletes through themed application dialogs", async () => {
    vi.spyOn(userApi, "personalKnowledgeDetail").mockResolvedValue(detail());
    vi.spyOn(userApi, "personalKnowledgeFiles").mockResolvedValue({ items: [file], total: 1 });
    const update = vi.spyOn(userApi, "updatePersonalKnowledge").mockResolvedValue(detail({ name: "更新后的知识库" }));
    const rename = vi.spyOn(userApi, "renamePersonalKnowledgeFile").mockResolvedValue({ ...file, filename: "新文件名.xlsx" });
    const remove = vi.spyOn(userApi, "deletePersonalKnowledgeFile").mockResolvedValue(undefined);
    const prompt = vi.spyOn(window, "prompt");
    const confirm = vi.spyOn(window, "confirm");

    render(<PersonalKnowledgeDetail knowledgeBaseId={9} onBack={vi.fn()}/>);
    await screen.findByRole("heading", { name: "测试新知识库" });

    fireEvent.click(screen.getByRole("button", { name: "编辑知识库" }));
    await waitFor(() => expect(screen.getByRole("dialog", { name: "编辑知识库" })).toBeVisible());
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "更新后的知识库" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(9, { name: "更新后的知识库", description: "测试" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "编辑知识库" })).not.toBeInTheDocument());

    fireEvent.click(screen.getAllByRole("button", { name: "重命名 江苏区制造业AI线索.xlsx" })[0]);
    await waitFor(() => expect(screen.getByRole("dialog", { name: "重命名文件" })).toBeVisible());
    fireEvent.change(screen.getByRole("textbox", { name: "文件名" }), { target: { value: "新文件名.xlsx" } });
    fireEvent.click(screen.getByRole("button", { name: "保存文件名" }));
    await waitFor(() => expect(rename).toHaveBeenCalledWith(9, 18, "新文件名.xlsx"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "重命名文件" })).not.toBeInTheDocument());

    fireEvent.click(screen.getAllByRole("button", { name: "删除 江苏区制造业AI线索.xlsx" })[0]);
    const deleteDialog = screen.getByRole("dialog", { name: "永久删除文件" });
    expect(deleteDialog).toHaveTextContent("江苏区制造业AI线索.xlsx");
    fireEvent.click(screen.getByRole("button", { name: "确认永久删除" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(9, 18));
    await waitFor(() => expect(deleteDialog).not.toBeInTheDocument());

    expect(prompt).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
  });
});
