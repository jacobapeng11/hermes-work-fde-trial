import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { FilePreviewDialog, filePreviewKind } from "./FilePreviewDialog";

const file = {
  fileId: 1,
  runtimeWorkspaceId: 1,
  relativePath: "workspace/report.docx",
  displayName: "report.docx",
  mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  sizeBytes: 128,
  sourceType: "upload"
};
const unsupportedFile = { ...file, relativePath: "workspace/archive.zip", displayName: "archive.zip", mimeType: "application/zip" };

describe("filePreviewKind", () => {
  it("uses both MIME type and extension to identify browser-previewable files", () => {
    expect(filePreviewKind({ displayName: "photo.bin", mimeType: "image/png" })).toBe("image");
    expect(filePreviewKind({ displayName: "report.pdf", mimeType: "application/octet-stream" })).toBe("pdf");
    expect(filePreviewKind({ displayName: "notes.md", mimeType: "application/octet-stream" })).toBe("text");
    expect(filePreviewKind(file)).toBe("office");
  });
});

describe("FilePreviewDialog", () => {
  it("keeps unsupported formats in-app with download and close actions", () => {
    const onClose = vi.fn();
    const onDownload = vi.fn();
    render(<FilePreviewDialog preview={{ file: unsupportedFile, status: "ready", kind: "unsupported" }} onClose={onClose} onDownload={onDownload}/>);

    expect(screen.getByRole("dialog", { name: "archive.zip" })).toBeVisible();
    expect(screen.getByText("该格式暂不支持在线预览")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "下载 archive.zip" }));
    expect(onDownload).toHaveBeenCalledWith(unsupportedFile);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("hides every download fallback when downloads are forbidden", () => {
    render(
      <FilePreviewDialog
        preview={{ file: unsupportedFile, status: "ready", kind: "unsupported" }}
        onClose={vi.fn()}
        onDownload={vi.fn()}
        canDownload={false}
      />
    );

    expect(screen.queryByRole("button", { name: "下载 archive.zip" })).not.toBeInTheDocument();
    expect(screen.queryByText("下载文件")).not.toBeInTheDocument();
  });
});
