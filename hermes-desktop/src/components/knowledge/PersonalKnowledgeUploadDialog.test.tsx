import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { userApi } from "../../api/client";
import { PersonalKnowledgeUploadDialog } from "./PersonalKnowledgeUploadDialog";

function uploaded(filename: string, fileId = 1) {
  return {
    fileId,
    filename,
    sizeBytes: 12,
    mimeType: "text/plain",
    status: "pending" as const,
    uploaderId: 1,
    uploaderName: "示例用户乙",
    updatedAt: "2026-07-21T01:39:24"
  };
}

describe("PersonalKnowledgeUploadDialog", () => {
  afterEach(() => vi.restoreAllMocks());

  it("queues selected files and uploads only after confirmation", async () => {
    const upload = vi.spyOn(userApi, "uploadPersonalKnowledgeFile")
      .mockImplementation(async (_knowledgeBaseId, file) => uploaded(file.name));
    const onClose = vi.fn();
    const onUploaded = vi.fn();
    render(<PersonalKnowledgeUploadDialog knowledgeBaseId={9} onClose={onClose} onUploaded={onUploaded}/>);

    const files = [new File(["one"], "first.pdf"), new File(["two"], "second.txt")];
    fireEvent.change(screen.getByLabelText("选择要上传的文件"), { target: { files } });

    // antd Modal 打开动画（rc-motion）在 jsdom 中需等待推进后再断言可见性。
    await waitFor(() => expect(screen.getByText("first.pdf")).toBeVisible());
    await waitFor(() => expect(screen.getByText("second.txt")).toBeVisible());
    expect(upload).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "开始上传" }));
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(2));
    expect(onUploaded).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("removes pending files and reports the 100 MB limit", () => {
    vi.spyOn(userApi, "uploadPersonalKnowledgeFile");
    render(<PersonalKnowledgeUploadDialog knowledgeBaseId={9} onClose={vi.fn()} onUploaded={vi.fn()}/>);

    const removable = new File(["remove"], "remove.txt");
    const tooLarge = new File(["large"], "large.zip");
    Object.defineProperty(tooLarge, "size", { value: 100 * 1024 * 1024 + 1 });
    const input = screen.getByLabelText("选择要上传的文件");
    fireEvent.change(input, { target: { files: [removable] } });
    fireEvent.click(screen.getByRole("button", { name: "移除 remove.txt" }));
    expect(screen.queryByText("remove.txt")).not.toBeInTheDocument();

    fireEvent.change(input, { target: { files: [tooLarge] } });
    expect(screen.getByRole("alert")).toHaveTextContent("large.zip 超过 100 MB");
  });

  it("starts at most three uploads concurrently", async () => {
    const releases: Array<() => void> = [];
    let active = 0;
    let peak = 0;
    vi.spyOn(userApi, "uploadPersonalKnowledgeFile").mockImplementation((_knowledgeBaseId, file) => {
      active += 1;
      peak = Math.max(peak, active);
      return new Promise((resolve) => releases.push(() => {
        active -= 1;
        resolve(uploaded(file.name));
      }));
    });
    const onClose = vi.fn();
    render(<PersonalKnowledgeUploadDialog knowledgeBaseId={9} onClose={onClose} onUploaded={vi.fn()}/>);
    fireEvent.change(screen.getByLabelText("选择要上传的文件"), {
      target: { files: [1, 2, 3, 4].map((index) => new File([String(index)], `${index}.txt`)) }
    });
    fireEvent.click(screen.getByRole("button", { name: "开始上传" }));

    await waitFor(() => expect(releases).toHaveLength(3));
    expect(peak).toBe(3);
    await act(async () => { releases.shift()?.(); });
    await waitFor(() => expect(releases).toHaveLength(3));
    await act(async () => { while (releases.length) releases.shift()?.(); });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });
});
