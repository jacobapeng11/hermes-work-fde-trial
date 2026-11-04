import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, userApi, type AppCenterListItem } from "../../api/client";
import { ShareAppDialog } from "./ShareAppDialog";

vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    userApi: {
      organizationDirectoryTree: vi.fn(),
      createApp: vi.fn(),
      uploadApp: vi.fn(),
      downloadChatFile: vi.fn(),
      updateAppPermissions: vi.fn(),
      updateAppPackage: vi.fn()
    }
  };
});

const app: AppCenterListItem = {
  id: 11,
  name: "销售看板",
  description: "销售数据一屏看",
  accessUrl: "https://demo.example/11",
  sourceType: "agent_html",
  shareMode: "scope",
  creatorName: "创建者",
  mine: true,
  updatedAt: "2026-10-16T12:00:00",
  scopeSummary: "全员",
  publishVersionId: "3567",
  scope: { organization: false, departmentIds: [], userIds: [8] }
};

function renderDialog() {
  return render(<ShareAppDialog
    defaultName="销售看板"
    html="<!doctype html><html><body>app</body></html>"
    currentVersion={1}
    sharedApp={null}
    sourceSessionId="session-1"
    sourceRunId="run-1"
    onClose={() => undefined}
    onSaved={() => undefined}
  />);
}

describe("ShareAppDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("publishes the original zip artifact without rebuilding index.html", async () => {
    vi.mocked(userApi.organizationDirectoryTree).mockResolvedValue([]);
    vi.mocked(userApi.downloadChatFile).mockResolvedValue(
      new Blob(["zip-content"], { type: "application/zip" })
    );
    vi.mocked(userApi.uploadApp).mockResolvedValue(app);
    render(<ShareAppDialog
      defaultName="销售看板"
      html="<!doctype html><html><body>app</body></html>"
      packageSource={{ fileId: 92, displayName: "dashboard.zip" }}
      currentVersion={1}
      sharedApp={null}
      sourceSessionId="session-1"
      sourceRunId="run-1"
      onClose={() => undefined}
      onSaved={() => undefined}
    />);

    fireEvent.click(screen.getByRole("button", { name: "确认分享" }));

    await waitFor(() => expect(userApi.uploadApp).toHaveBeenCalledTimes(1));
    expect(userApi.downloadChatFile).toHaveBeenCalledWith(92);
    expect(userApi.uploadApp).toHaveBeenCalledWith(expect.objectContaining({
      name: "销售看板",
      description: "HTML 看板",
      shareMode: "scope",
      sourceSessionId: "session-1",
      sourceRunId: "run-1",
      file: expect.any(File)
    }));
    expect(userApi.createApp).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText("已发布")).toBeVisible());
  });

  it("publishes the current html artifact to app center", async () => {
    vi.mocked(userApi.organizationDirectoryTree).mockResolvedValue([]);
    vi.mocked(userApi.createApp).mockResolvedValue(app);
    renderDialog();

    fireEvent.click(screen.getByRole("button", { name: "确认分享" }));

    await waitFor(() => expect(screen.getByText("已发布")).toBeVisible());
    expect(userApi.createApp).toHaveBeenCalledWith(expect.objectContaining({
      name: "销售看板",
      description: "HTML 看板",
      htmlContent: expect.stringContaining("<html>"),
      shareMode: "scope",
      sourceSessionId: "session-1",
      sourceRunId: "run-1"
    }));
  });

  it("reuses the published result while the artifact version is unchanged", async () => {
    vi.mocked(userApi.organizationDirectoryTree).mockResolvedValue([]);
    render(
      <ShareAppDialog
        defaultName="销售看板"
        html="<!doctype html><html><body>app</body></html>"
        currentVersion={2}
        sharedApp={{ app, version: 2 }}
        onClose={() => undefined}
        onSaved={() => undefined}
      />
    );

    await waitFor(() => expect(userApi.organizationDirectoryTree).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByDisplayValue("https://demo.example/11")).toBeVisible());
    expect(screen.queryByRole("button", { name: "确认分享" })).not.toBeInTheDocument();
    expect(userApi.createApp).not.toHaveBeenCalled();
  });

  it("lets the user clear and replace the default app name", async () => {
    vi.mocked(userApi.organizationDirectoryTree).mockResolvedValue([]);
    vi.mocked(userApi.createApp).mockResolvedValue(app);
    renderDialog();

    const input = screen.getByDisplayValue("销售看板");
    fireEvent.change(input, { target: { value: "" } });
    expect(input).toHaveValue("");
    expect(screen.getByRole("button", { name: "确认分享" })).toBeDisabled();

    fireEvent.change(input, { target: { value: "自定义看板名" } });
    fireEvent.change(screen.getByDisplayValue("HTML 看板"), { target: { value: "自定义描述" } });
    fireEvent.click(screen.getByRole("button", { name: "确认分享" }));

    await waitFor(() => expect(userApi.createApp).toHaveBeenCalledTimes(1));
    expect(userApi.createApp).toHaveBeenCalledWith(expect.objectContaining({
      name: "自定义看板名",
      description: "自定义描述"
    }));
  });

  it("drops the stale app reference when the update target was deleted", async () => {
    vi.mocked(userApi.organizationDirectoryTree).mockResolvedValue([]);
    vi.mocked(userApi.updateAppPackage).mockRejectedValue(
      new ApiError("看板不存在或已删除，请刷新看板区", 404, "APP_CENTER_APP_NOT_FOUND")
    );
    const onAppMissing = vi.fn();
    render(<ShareAppDialog
      defaultName="销售看板"
      html="<!doctype html><html><body>app</body></html>"
      currentVersion={2}
      sharedApp={null}
      updateApp={app}
      onClose={() => undefined}
      onSaved={() => undefined}
      onAppMissing={onAppMissing}
    />);

    fireEvent.click(screen.getByRole("button", { name: "更新分享" }));

    await waitFor(() => expect(onAppMissing).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText("原看板已被删除，关闭后可重新分享")).toBeVisible());
  });

  it("explains the self-hosted platform link semantics", async () => {
    vi.mocked(userApi.organizationDirectoryTree).mockResolvedValue([]);
    vi.mocked(userApi.createApp).mockResolvedValue({ ...app, shareMode: "link" });
    renderDialog();

    await waitFor(() => expect(
      screen.getByText("权限只控制哪些用户会在看板区的“看板广场”看到该看板。")
    ).toBeVisible());

    fireEvent.click(screen.getByRole("tab", { name: /分享链接/ }));
    await waitFor(() => expect(
      screen.getByText("链接使用本项目域名，可访问本项目平台的用户即可打开。")
    ).toBeVisible());
    fireEvent.click(screen.getByRole("button", { name: "生成链接" }));

    await waitFor(() => expect(userApi.createApp).toHaveBeenCalledWith(
      expect.objectContaining({ shareMode: "link", scope: null })
    ));
    await waitFor(() => expect(screen.getByText("已发布")).toBeVisible());
  });
});
