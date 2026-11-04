import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AppCenterListItem } from "../../api/client";
import { HtmlPreviewPanel } from "./HtmlPreviewPanel";

const publishedApp: AppCenterListItem = {
  id: 11,
  name: "销售看板",
  description: "销售数据一屏看",
  accessUrl: "https://demo.example/11",
  sourceType: "agent_html",
  shareMode: "scope",
  creatorName: "创建者",
  mine: true,
  sourceSessionId: "session-1",
  updatedAt: "2026-10-18T00:00:00Z",
  scopeSummary: "仅自己",
  publishVersionId: "3567",
  scope: { organization: false, departmentIds: [], userIds: [] }
};

describe("HtmlPreviewPanel", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a sandboxed document and supports external preview", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const createObjectURL = vi.fn(() => "blob:preview");
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: createObjectURL
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      writable: true,
      value: vi.fn()
    });

    render(<HtmlPreviewPanel
      html="<!doctype html><html><body>preview</body></html>"
      version={2}
      sourceName="看板.html"
      fallbackName="看板"
      sharedApp={null}
      previouslySharedApp={null}
      editTargetName={null}
      onSharedAppChange={() => undefined}
      onUpdateApp={() => undefined}
      onUpdateAppPackage={() => undefined}
      onClose={() => undefined}
      onOpenCenter={() => undefined}
    />);

    const frame = screen.getByTitle("HTML 看板预览 v2");
    expect(frame).toHaveAttribute("sandbox", "allow-scripts");
    expect(frame).toHaveAttribute("srcdoc", expect.stringContaining("<html>"));
    fireEvent.click(screen.getByRole("button", { name: /新窗口/ }));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith("blob:preview", "_blank", "noopener,noreferrer");
  });

  it("switches the primary action to app update in edit mode", () => {
    const onUpdateApp = vi.fn();
    render(<HtmlPreviewPanel
      html="<!doctype html><html><body>preview</body></html>"
      version={3}
      sourceName="看板.html"
      fallbackName="看板"
      sharedApp={null}
      previouslySharedApp={null}
      editTargetName="看板"
      onSharedAppChange={() => undefined}
      onUpdateApp={onUpdateApp}
      onUpdateAppPackage={() => undefined}
      onClose={() => undefined}
      onOpenCenter={() => undefined}
    />);

    fireEvent.click(screen.getByRole("button", { name: "更新看板" }));
    expect(onUpdateApp).toHaveBeenCalledWith("<!doctype html><html><body>preview</body></html>", 3);
  });

  it("keeps the published state read-only", () => {
    render(<HtmlPreviewPanel
      html="<!doctype html><html><body>preview</body></html>"
      version={3}
      sourceName="看板.html"
      fallbackName="看板"
      sharedApp={{ app: publishedApp, version: 3 }}
      previouslySharedApp={publishedApp}
      editTargetName={null}
      onSharedAppChange={() => undefined}
      onUpdateApp={() => undefined}
      onUpdateAppPackage={() => undefined}
      onClose={() => undefined}
      onOpenCenter={() => undefined}
    />);

    const publishedState = screen.getByRole("button", { name: "已分享" });
    expect(publishedState).toBeDisabled();
    fireEvent.click(publishedState);
    expect(screen.queryByRole("dialog", { name: "分享看板" })).not.toBeInTheDocument();
  });
});

describe("HtmlPreviewPanel package source", () => {
  it("uses the original package for app updates and explains preview limits", () => {
    const onUpdateAppPackage = vi.fn();
    render(<HtmlPreviewPanel
      html="<!doctype html><html><body>preview</body></html>"
      packageSource={{ fileId: 92, displayName: "dashboard.zip" }}
      version={4}
      sourceName="dashboard.zip/index.html"
      fallbackName="看板"
      sharedApp={null}
      previouslySharedApp={null}
      editTargetName="看板"
      onSharedAppChange={() => undefined}
      onUpdateApp={() => undefined}
      onUpdateAppPackage={onUpdateAppPackage}
      onClose={() => undefined}
      onOpenCenter={() => undefined}
    />);

    fireEvent.click(screen.getByRole("button", { name: "更新看板" }));
    expect(onUpdateAppPackage).toHaveBeenCalledWith(
      { fileId: 92, displayName: "dashboard.zip" },
      4
    );
    expect(
      screen.getByText("已识别 ZIP 看板包。当前预览仅加载根 index.html，包内多文件资源发布后完整生效。")
    ).toBeVisible();
  });
});
