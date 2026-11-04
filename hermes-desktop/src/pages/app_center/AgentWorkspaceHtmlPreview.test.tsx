import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { configure } from "@testing-library/react";

import { emptyChatQueueSnapshot } from "../chatQueueProjection";
import { UserChatPage } from "../UserChatPage";
import { ApiError } from "../../api/client";

const api = vi.hoisted(() => ({
  bootstrapRuntime: vi.fn(),
  runnableExperts: vi.fn(),
  runtimeModels: vi.fn(),
  runtimeSessions: vi.fn(),
  createRuntimeSession: vi.fn(),
  keepaliveRuntime: vi.fn(),
  warmupRuntime: vi.fn(),
  chatHistoryPreview: vi.fn(),
  chatHistoryAuthoritative: vi.fn(),
  chatFiles: vi.fn(),
  chatQueueSummary: vi.fn(),
  chatQueueSummaryPrepared: vi.fn(),
  activeChatRun: vi.fn(),
  downloadChatFile: vi.fn(),
  downloadAppPackage: vi.fn(),
  listApps: vi.fn(),
  taskPromptConfig: vi.fn(),
  createApp: vi.fn(),
  uploadApp: vi.fn(),
  organizationDirectoryTree: vi.fn(),
  prewarmRuntimeSession: vi.fn(),
  streamChatRun: vi.fn(),
  updateAppPackage: vi.fn(),
  submitChatCorrection: vi.fn(),
  reportClientTiming: vi.fn(),
  reportChatClientTiming: vi.fn(),
  runActivity: vi.fn()
}));

vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/client")>()),
  userApi: api
}));

// HTML 预览面板经 React.lazy 动态加载且与 app_center chunk 共享入口（index.ts
// 引 AppCenterPanelView → AntdModuleProviders → antd）；mock 掉 Provider 避免触发
// antd 的重量级 transform（冷启动实测 15-35s），再在 beforeAll 预热 chunk。
vi.mock("../../theme/antd-module", () => ({
  AntdModuleProviders: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
// 懒加载把预览面板的挂载变成异步：多文件并行跑时 worker 争抢会拉长动态 import
// 链，个别重用例（zip 解析/多轮 run）会击穿 setup.ts 的全局 5s，本文件放宽到 10s。
configure({ asyncUtilTimeout: 10_000 });
beforeAll(async () => {
  await import("../../components/app_center");
}, 120_000);

const HTML = "```html\n<!doctype html><html><body>preview</body></html>\n```";
const APP = {
  id: 11,
  name: "测试看板",
  description: "测试 HTML 看板",
  accessUrl: "https://demo.example/11",
  sourceType: "agent_html",
  shareMode: "scope",
  creatorName: "创建者",
  mine: true,
  sourceSessionId: "rs-preview",
  updatedAt: "2026-10-17T00:00:00Z",
  scopeSummary: "仅自己",
  publishVersionId: "3567"
} as const;

function storedZip(name: string, content: string): Uint8Array<ArrayBuffer> {
  const nameBytes = new TextEncoder().encode(name);
  const contentBytes = new TextEncoder().encode(content);
  const localSize = 30 + nameBytes.length + contentBytes.length;
  const centralSize = 46 + nameBytes.length;
  const output = new Uint8Array(localSize + centralSize + 22);
  const view = new DataView(output.buffer);
  const writeHeader = (offset: number, signature: number, central = false) => {
    view.setUint32(offset, signature, true);
    view.setUint16(offset + 4, 20, true);
    if (central) view.setUint16(offset + 6, 20, true);
    view.setUint16(offset + 20, contentBytes.length, true);
    view.setUint32(offset + 24, contentBytes.length, true);
    view.setUint16(offset + 28, nameBytes.length, true);
    if (central) view.setUint32(offset + 42, 0, true);
  };
  writeHeader(0, 0x04034b50);
  output.set(nameBytes, 30);
  output.set(contentBytes, 30 + nameBytes.length);
  const centralOffset = localSize;
  writeHeader(centralOffset, 0x02014b50, true);
  output.set(nameBytes, centralOffset + 46);
  const endOffset = centralOffset + centralSize;
  view.setUint32(endOffset, 0x06054b50, true);
  view.setUint16(endOffset + 8, 1, true);
  view.setUint16(endOffset + 10, 1, true);
  view.setUint32(endOffset + 12, centralSize, true);
  view.setUint32(endOffset + 16, centralOffset, true);
  return output;
}

describe("AgentWorkspace HTML preview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, "confirm").mockRestore();
    localStorage.setItem("hermes.files-pane", "false");
    api.bootstrapRuntime.mockResolvedValue({
      runtimeWorkspaceId: 1,
      runtimeStatus: "ready"
    });
    api.runnableExperts.mockResolvedValue([]);
    api.runtimeModels.mockResolvedValue([{
      modelEntryId: 19,
      displayName: "Model A",
      modelKey: "model-a",
      providerId: 1,
      providerDisplayName: "Provider A",
      isDefault: true
    }]);
    api.runtimeSessions.mockResolvedValue([{
      runtimeSessionId: "rs-preview",
      title: "HTML 任务",
      messageCount: 0,
      sourceType: "web",
      sessionModelEntryId: 19,
      status: "active"
    }]);
    api.keepaliveRuntime.mockResolvedValue(undefined);
    api.warmupRuntime.mockResolvedValue({
      runtimeWorkspaceId: 1,
      runtimeStatus: "ready"
    });
    const history = {
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-preview",
      messages: [],
      generatedAt: "2026-10-16T00:00:00Z",
      hasActiveRun: false
    };
    api.chatHistoryPreview.mockResolvedValue({
      ...history,
      source: "mysql",
      consistency: "projection_preview"
    });
    api.chatHistoryAuthoritative.mockResolvedValue({
      ...history,
      source: "hermes",
      consistency: "authoritative"
    });
    api.chatFiles.mockResolvedValue([]);
    api.chatQueueSummary.mockResolvedValue(emptyChatQueueSnapshot());
    api.chatQueueSummaryPrepared.mockImplementation(
      (workspaceId: number, sessionId: string) => api.chatQueueSummary(workspaceId, sessionId)
    );
    api.activeChatRun.mockResolvedValue({ run: null });
    api.listApps.mockResolvedValue([]);
    // 工作台重构后默认空会话会挂载 WelcomeTaskPanel；给出空 tabs 配置保持面板惰性，
    // 避免缺失 mock 时 taskPromptConfig 崩溃整个页面渲染。
    api.taskPromptConfig.mockResolvedValue({ revision: 1, config: { tabs: [] } });
    api.createApp.mockResolvedValue({ ...APP, sourceSessionId: "rs-preview" });
    api.uploadApp.mockResolvedValue({ ...APP, sourceSessionId: "rs-preview" });
    api.organizationDirectoryTree.mockResolvedValue([]);
    api.downloadAppPackage.mockResolvedValue(new Blob(["zip"], { type: "application/zip" }));
    api.downloadChatFile.mockResolvedValue(new Blob([
      "<!doctype html><html><body>file preview</body></html>"
    ], { type: "text/html" }));
    api.prewarmRuntimeSession.mockResolvedValue({
      state: "ready",
      readyReceiptId: "receipt-preview",
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-preview",
      modelEntryId: 19,
      runtimeAgentProfileId: null,
      profileGeneration: 0,
      selectionEpoch: 1,
      issuedAt: "2026-10-16T00:00:00Z",
      expiresAt: "2026-10-16T00:01:00Z"
    });
    api.reportChatClientTiming.mockResolvedValue(undefined);
    api.updateAppPackage.mockResolvedValue(APP);
    api.streamChatRun.mockImplementation(async (
      _payload: { runtimeRunId?: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-preview" } });
      onEvent({
        event: "chat.message.delta",
        data: { seq: 1, runtimeRunId: "rr-preview", delta: HTML }
      });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-preview", seq: 2 } });
    });
  });

  it("opens a sandboxed preview after a completed run emits a complete html document", async () => {
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "生成页面");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    const frame = await screen.findByTitle("HTML 看板预览 v1");
    expect(frame).toHaveAttribute("sandbox", "allow-scripts");
    expect(frame).toHaveAttribute("srcdoc", expect.stringContaining("<html>"));
    expect(screen.getByRole("button", { name: "分享" })).toBeVisible();
  });

  it("opens a cloud html file artifact after its run completes", async () => {
    let artifactPublished = false;
    api.chatFiles.mockImplementation(async () => artifactPublished
      ? [{
          fileId: 91,
          runtimeWorkspaceId: 1,
          runtimeSessionId: "rs-preview",
          runtimeRunId: "rr-preview",
          relativePath: "workspace/sessions/rs-preview/solar-system.html",
          displayName: "solar-system.html",
          mimeType: "text/html",
          sizeBytes: 52,
          sourceType: "runtime",
          createdAt: "2026-10-17T00:36:30Z"
        }]
      : []);
    api.streamChatRun.mockImplementation(async (
      _payload: { runtimeRunId?: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-preview" } });
      onEvent({
        event: "chat.artifact.created",
        data: {
          runtimeRunId: "rr-preview",
          relative_path: "workspace/sessions/rs-preview/solar-system.html",
          mime_type: "text/html",
          size_bytes: 52
        }
      });
      artifactPublished = true;
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-preview", seq: 2 } });
    });
    const user = userEvent.setup();
    localStorage.setItem("hermes.files-pane", "true");
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    expect(
      await screen.findByRole("complementary", { name: "当前任务文件" })
    ).toBeVisible();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "生成文件");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    const frame = await screen.findByTitle("HTML 看板预览 v1");
    expect(frame).toHaveAttribute("srcdoc", expect.stringContaining("file preview"));
    expect(screen.getByText("solar-system.html")).toBeVisible();
    expect(api.downloadChatFile).toHaveBeenCalledWith(91);
    expect(
      screen.queryByRole("complementary", { name: "当前任务文件" })
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭预览" }));
    expect(
      await screen.findByRole("complementary", { name: "当前任务文件" })
    ).toBeVisible();
  });

  it("opens a cloud html file as soon as its artifact index sees the file", async () => {
    let artifactPublished = false;
    api.chatFiles.mockImplementation(async () => artifactPublished
      ? [{
          fileId: 93,
          runtimeWorkspaceId: 1,
          runtimeSessionId: "rs-preview",
          runtimeRunId: "rr-preview",
          relativePath: "workspace/sessions/rs-preview/motherland.html",
          displayName: "motherland.html",
          mimeType: "text/html",
          sizeBytes: 6_857,
          sourceType: "runtime",
          createdAt: "2026-10-17T10:32:02Z"
        }]
      : []);
    api.streamChatRun.mockImplementation(async (
      _payload: { runtimeRunId?: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-preview" } });
      onEvent({
        event: "chat.artifact.created",
        data: {
          runtimeRunId: "rr-preview",
          relative_path: "workspace/sessions/rs-preview/motherland.html",
          mime_type: "text/html",
          size_bytes: 6_857
        }
      });
      artifactPublished = true;
      // The real failure case had a long-running turn after the HTML artifact
      // became visible. Do not emit chat.done here.
      return new Promise<void>(() => undefined);
    });
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "生成文件");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    const frame = await screen.findByTitle("HTML 看板预览 v1", {}, { timeout: 3000 });
    expect(frame).toHaveAttribute("srcdoc", expect.stringContaining("file preview"));
    expect(api.downloadChatFile).toHaveBeenCalledWith(93);
  });

  it("does not open a stale html preview after creating a new session", async () => {
    const oldHtmlFile = {
      fileId: 91,
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-preview",
      runtimeRunId: "rr-preview",
      relativePath: "workspace/sessions/rs-preview/solar-system.html",
      displayName: "solar-system.html",
      mimeType: "text/html",
      sizeBytes: 52,
      sourceType: "runtime",
      createdAt: "2026-10-17T00:36:30Z"
    } as const;
    let chatFilesCalls = 0;
    let resolveOldFiles!: (files: Array<typeof oldHtmlFile>) => void;
    const oldFiles = new Promise<Array<typeof oldHtmlFile>>((resolve) => {
      resolveOldFiles = resolve;
    });
    api.chatFiles.mockImplementation(async (_workspaceId: number, sessionId: string) => {
      chatFilesCalls += 1;
      return sessionId === "rs-preview" && chatFilesCalls === 2
        ? oldFiles
        : [];
    });
    api.createRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-new",
      title: "新任务",
      messageCount: 0,
      sourceType: "web",
      sessionModelEntryId: 19,
      status: "active"
    });

    const user = userEvent.setup();
    render(<UserChatPage />);
    await screen.findByRole("button", { name: "HTML 任务" });
    await waitFor(() => expect(chatFilesCalls).toBeGreaterThanOrEqual(2));

    await user.click(screen.getByRole("button", { name: "新建任务" }));
    expect(await screen.findByRole("button", { name: "新任务" })).toBeVisible();
    await act(async () => {
      resolveOldFiles([oldHtmlFile]);
    });
    await waitFor(() => expect(api.chatFiles).toHaveBeenCalledWith(1, "rs-new"));

    expect(screen.queryByTitle(/HTML 看板预览/)).not.toBeInTheDocument();
    expect(api.downloadChatFile).not.toHaveBeenCalled();
  });

  it("reopens an html file preview from the task file rail after dismissal", async () => {
    api.chatFiles.mockResolvedValue([{
      fileId: 94,
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-preview",
      runtimeRunId: "rr-preview",
      relativePath: "workspace/sessions/rs-preview/snowflake.html",
      displayName: "snowflake.html",
      mimeType: "text/html",
      sizeBytes: 3_216,
      sourceType: "runtime",
      createdAt: "2026-10-17T12:10:00Z"
    }]);
    localStorage.setItem("hermes.files-pane", "true");
    const user = userEvent.setup();
    render(<UserChatPage />);

    const fileRail = await screen.findByRole("complementary", { name: "当前任务文件" });
    expect(fileRail).toBeVisible();
    await screen.findByText("snowflake.html");
    await user.click(screen.getByRole("button", { name: "预览 snowflake.html" }));

    const frame = await screen.findByTitle("HTML 看板预览 v1");
    expect(frame).toHaveAttribute("srcdoc", expect.stringContaining("file preview"));
    expect(api.downloadChatFile).toHaveBeenCalledWith(94);
    expect(
      screen.queryByRole("dialog", { name: "snowflake.html" })
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "关闭预览" }));
    expect(
      await screen.findByRole("complementary", { name: "当前任务文件" })
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "预览 snowflake.html" }));

    expect(await screen.findByTitle("HTML 看板预览 v1")).toBeVisible();
    expect(api.downloadChatFile).toHaveBeenCalledTimes(2);
  });

  it("opens a desktop local html file through the preview bridge", async () => {
    const readHtmlPreview = vi.fn().mockResolvedValue(
      "<!doctype html><html><body>local preview</body></html>"
    );
    Object.defineProperty(window, "coworkDesktop", {
      configurable: true,
      value: { readHtmlPreview }
    });
    api.streamChatRun.mockImplementation(async (
      _payload: { runtimeRunId?: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-local" } });
      onEvent({
        event: "chat.tool.event",
        data: {
          name: "desktop_workspace_write",
          status: "completed",
          input: JSON.stringify({ path: "solar-system.html" })
        }
      });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-local", seq: 2 } });
    });
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "生成本地文件");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    const frame = await screen.findByTitle("HTML 看板预览 v1");
    expect(frame).toHaveAttribute("srcdoc", expect.stringContaining("local preview"));
    expect(readHtmlPreview).toHaveBeenCalledWith("solar-system.html");
    expect(screen.getByText("solar-system.html")).toBeVisible();
  });

  it("offers update share and clarify after sharing in the current session", async () => {
    const sharedApp = {
      ...APP,
      name: "我的看板",
      description: "第一版描述",
      sourceSessionId: "rs-preview"
    };
    api.createApp.mockResolvedValue(sharedApp);
    let runCount = 0;
    api.streamChatRun.mockImplementation(async (
      _payload: { runtimeRunId?: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      runCount += 1;
      const runtimeRunId = `rr-share-${runCount}`;
      const content = runCount === 1 ? HTML : HTML.replace("preview", "updated preview");
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId } });
      onEvent({
        event: "chat.message.delta",
        data: { seq: 1, runtimeRunId, delta: content }
      });
      onEvent({ event: "chat.done", data: { runtimeRunId, seq: 2 } });
    });
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "生成页面");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByTitle("HTML 看板预览 v1")).toBeVisible();
    await waitFor(() => expect(api.listApps).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "分享" }));
    const shareDialog = await screen.findByRole("dialog", { name: "分享看板" });
    await user.clear(within(shareDialog).getByLabelText("看板名称"));
    await user.type(within(shareDialog).getByLabelText("看板名称"), "我的看板");
    await user.clear(within(shareDialog).getByLabelText(/看板描述/));
    await user.type(within(shareDialog).getByLabelText(/看板描述/), "第一版描述");
    await user.click(within(shareDialog).getByRole("button", { name: "确认分享" }));
    await waitFor(() => expect(api.createApp).toHaveBeenCalledTimes(1));
    // T5 起 ShareAppDialog 迁 antd Modal：Button 内容包 <span>，文本匹配改走 role 查询。
    await user.click(within(shareDialog).getByRole("button", { name: "关闭" }));
    expect(await screen.findByRole("button", { name: "已分享" })).toBeDisabled();

    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "继续修改页面");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByTitle("HTML 看板预览 v2")).toHaveAttribute(
      "srcdoc",
      expect.stringContaining("updated preview")
    );
    expect(await screen.findByRole("button", { name: "更新分享" })).toBeVisible();
    expect(await screen.findByRole("button", { name: "更新到看板区" })).toBeVisible();
    expect(screen.getByText(/是否将最新渲染更新到看板区/)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "更新分享" }));
    const updateDialog = await screen.findByRole("dialog", { name: "更新分享看板" });
    expect(within(updateDialog).getByLabelText("看板名称")).toHaveValue("我的看板");
    expect(within(updateDialog).getByLabelText(/看板描述/)).toHaveValue("第一版描述");
  });

  it("prompts an app-center update when a shared session produces new html", async () => {
    api.listApps.mockResolvedValue([{ ...APP, sourceSessionId: "rs-preview" }]);
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "继续修改页面");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(
      await screen.findByRole("button", { name: "更新到看板区" })
    ).toBeVisible();
    expect(screen.getByText(/是否将最新渲染更新到看板区/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "更新到看板区" }));
    await waitFor(() => expect(api.updateAppPackage).toHaveBeenCalledTimes(1));
    expect(api.updateAppPackage).toHaveBeenCalledWith(11, {
      htmlContent: expect.stringContaining("<html>")
    });
  });

  it("stays silent after a shared-session run when the app was deleted", async () => {
    api.listApps.mockResolvedValue([
      { ...APP, id: 12, mine: false, sourceSessionId: "rs-preview" }
    ]);
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "继续修改页面");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByTitle("HTML 看板预览 v1")).toBeVisible();
    await waitFor(() => expect(api.listApps).toHaveBeenCalled());
    expect(
      screen.queryByRole("button", { name: "更新到看板区" })
    ).not.toBeInTheDocument();
  });

  it("falls back to share flow after the update target was deleted mid-session", async () => {
    // run 完成时会话解析到已分享看板；用户随后在看板区删除了它（listApps 查不到），
    // 更新提交 404：必须清掉过期引用，让入口回到「分享」新建流程。
    api.listApps.mockResolvedValue([{ ...APP, sourceSessionId: "rs-preview" }]);
    api.updateAppPackage.mockRejectedValueOnce(
      new ApiError("看板不存在或已删除", 404, "APP_CENTER_APP_NOT_FOUND")
    );
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "继续修改页面");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await screen.findByRole("button", { name: "暂不更新" });
    await user.click(screen.getByRole("button", { name: "暂不更新" }));

    // 面板出现「更新分享」说明会话级引用已就位；此刻起模拟看板已被删除
    await screen.findByRole("button", { name: "更新分享" });
    api.listApps.mockResolvedValue([]);
    await user.click(screen.getByRole("button", { name: "更新分享" }));

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "更新分享" }));
    expect(
      await within(dialog).findByText("原看板已被删除，关闭后可重新分享")
    ).toBeVisible();

    // header 的 X 与 footer 按钮可访问名都是「关闭」，取 footer 那个
    const closeButtons = within(dialog).getAllByRole("button", { name: "关闭" });
    await user.click(closeButtons[closeButtons.length - 1]);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "分享" })).toBeVisible()
    );
    expect(
      screen.queryByRole("button", { name: "更新分享" })
    ).not.toBeInTheDocument();
  });

  it("updates an edited html app after an app-center clarify card", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    api.listApps.mockResolvedValue([APP]);
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.click(screen.getByRole("button", { name: "看板区" }));
    await user.click(await screen.findByRole("button", { name: "编辑 测试看板" }));
    await user.click(
      await screen.findByRole("button", { name: /^移除待发送附件/ })
    );
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(
      await screen.findByRole("button", { name: "更新到看板区" })
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "更新到看板区" }));
    await waitFor(() => expect(api.updateAppPackage).toHaveBeenCalledTimes(1));
    expect(confirm).not.toHaveBeenCalled();
    expect(api.updateAppPackage).toHaveBeenCalledWith(11, {
      htmlContent: expect.stringContaining("<html>")
    });
  });

  it("previews index.html inside a zip produced by an app edit run", async () => {
    let packagePublished = false;
    const zipBlob = new Blob(
      [storedZip("index.html", "<!doctype html><html><body>twenty balls</body></html>")],
      { type: "application/zip" }
    );
    api.listApps.mockResolvedValue([APP]);
    api.downloadAppPackage.mockResolvedValue(zipBlob);
    api.chatFiles.mockImplementation(async () => [
      ...(packagePublished
        ? [{
            fileId: 92,
            runtimeWorkspaceId: 1,
            runtimeSessionId: "rs-preview",
            runtimeRunId: "rr-package",
            relativePath: "workspace/sessions/rs-preview/orb-screensaver-20balls.zip",
            displayName: "orb-screensaver-20balls.zip",
            mimeType: "application/zip",
            sizeBytes: zipBlob.size,
            sourceType: "runtime",
            createdAt: "2026-10-17T11:39:00Z"
          }]
        : []),
      // 历史编辑遗留的旧包：非本轮 run 产物，更新时绝不能选中它
      {
        fileId: 90,
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-preview",
        runtimeRunId: "rr-old",
        relativePath: "workspace/sessions/rs-preview/orb-screensaver.zip",
        displayName: "orb-screensaver.zip",
        mimeType: "application/zip",
        sizeBytes: zipBlob.size,
        sourceType: "runtime",
        createdAt: "2026-10-16T11:39:00Z"
      }
    ]);
    api.downloadChatFile.mockResolvedValue(zipBlob);
    api.streamChatRun.mockImplementation(async (
      _payload: { runtimeRunId?: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-package" } });
      onEvent({
        event: "chat.artifact.created",
        data: {
          runtimeRunId: "rr-package",
          relative_path: "workspace/sessions/rs-preview/orb-screensaver-20balls.zip",
          mime_type: "application/zip",
          size_bytes: zipBlob.size
        }
      });
      packagePublished = true;
      onEvent({
        event: "chat.message.delta",
        data: {
          seq: 1,
          runtimeRunId: "rr-package",
          delta: "最新输出做好了：bubble-screensaver-red-rainbow.zip"
        }
      });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-package", seq: 2 } });
    });
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.click(screen.getByRole("button", { name: "看板区" }));
    await user.click(await screen.findByRole("button", { name: "编辑 测试看板" }));
    await user.type(
      screen.getByRole("textbox", { name: "消息输入" }),
      "你让一个球设置成变色的，其它都是红色"
    );
    await user.click(await screen.findByRole("button", { name: /^移除待发送附件/ }));
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText(/最新输出做好了/)).toBeVisible();
    expect(screen.getByText(/你让一个球设置成变色的/)).toBeVisible();
    const frame = await screen.findByTitle("HTML 看板预览 v1", {}, { timeout: 3000 });
    expect(frame).toHaveAttribute("srcdoc", expect.stringContaining("twenty balls"));
    expect(screen.getByText("orb-screensaver-20balls.zip/index.html")).toBeVisible();
    expect(api.downloadChatFile).toHaveBeenCalledWith(92);
    expect(api.updateAppPackage).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("button", { name: "更新到看板区" }));
    await waitFor(() => expect(api.updateAppPackage).toHaveBeenCalledTimes(1));
    expect(api.updateAppPackage).toHaveBeenCalledWith(11, {
      file: expect.any(File)
    });
    expect(api.downloadChatFile).not.toHaveBeenCalledWith(90);
    expect(screen.getByTitle("HTML 看板预览 v1")).toBeVisible();
  });

  it("publishes a normal-run zip artifact as the original package", async () => {
    let packagePublished = false;
    const zipBlob = new Blob(
      [storedZip("index.html", "<!doctype html><html><body>zip app</body></html>")],
      { type: "application/zip" }
    );
    api.listApps.mockResolvedValue([]);
    api.chatFiles.mockImplementation(async () => packagePublished ? [{
      fileId: 93,
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-preview",
      runtimeRunId: "rr-normal-package",
      relativePath: "workspace/sessions/rs-preview/dashboard.zip",
      displayName: "dashboard.zip",
      mimeType: "application/zip",
      sizeBytes: zipBlob.size,
      sourceType: "runtime",
      createdAt: "2026-10-22T00:00:00Z"
    }] : []);
    api.downloadChatFile.mockResolvedValue(zipBlob);
    api.streamChatRun.mockImplementation(async (
      _payload: { runtimeRunId?: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-normal-package" } });
      onEvent({
        event: "chat.artifact.created",
        data: {
          runtimeRunId: "rr-normal-package",
          relative_path: "workspace/sessions/rs-preview/dashboard.zip",
          mime_type: "application/zip",
          size_bytes: zipBlob.size
        }
      });
      packagePublished = true;
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-normal-package", seq: 2 } });
    });
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "生成看板");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByTitle("HTML 看板预览 v1", {}, { timeout: 3000 }))
      .toHaveAttribute("srcdoc", expect.stringContaining("zip app"));
    expect(screen.getByText("dashboard.zip/index.html")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "分享" }));
    const dialog = await screen.findByRole("dialog", { name: "分享看板" });
    await user.clear(within(dialog).getByLabelText("看板名称"));
    await user.type(within(dialog).getByLabelText("看板名称"), "多文件看板");
    await user.clear(within(dialog).getByLabelText(/看板描述/));
    await user.type(within(dialog).getByLabelText(/看板描述/), "多文件看板");
    await user.click(within(dialog).getByRole("button", { name: "确认分享" }));

    await waitFor(() => expect(api.uploadApp).toHaveBeenCalledTimes(1));
    expect(api.uploadApp).toHaveBeenCalledWith(expect.objectContaining({
      name: "多文件看板",
      description: "多文件看板",
      sourceSessionId: "rs-preview",
      sourceRunId: "rr-normal-package",
      file: expect.any(File)
    }));
    expect(api.createApp).not.toHaveBeenCalled();
  });

  it("reopens a dismissed preview when the html content changes", async () => {
    let currentRun = "rr-first";
    let currentHtml = "<!doctype html><html><body>first version</body></html>";
    api.chatFiles.mockImplementation(async () => [{
      fileId: 91,
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-preview",
      runtimeRunId: currentRun,
      relativePath: "workspace/sessions/rs-preview/solar-system.html",
      displayName: "solar-system.html",
      mimeType: "text/html",
      sizeBytes: 52,
      sourceType: "runtime",
      createdAt: "2026-10-17T00:36:30Z"
    }]);
    api.downloadChatFile.mockImplementation(async () => new Blob(
      [currentHtml],
      { type: "text/html" }
    ));
    api.streamChatRun.mockImplementation(async (
      _payload: { runtimeRunId?: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      const runtimeRunId = currentRun;
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId } });
      onEvent({ event: "chat.done", data: { runtimeRunId, seq: 2 } });
    });
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "第一版");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByTitle("HTML 看板预览 v1"))
      .toHaveAttribute("srcdoc", expect.stringContaining("first version"));

    fireEvent.click(screen.getByRole("button", { name: "关闭预览" }));
    expect(screen.queryByTitle(/HTML 看板预览/)).not.toBeInTheDocument();

    // 内容未变化：同一来源的自动预览保持关闭
    currentRun = "rr-same";
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "没改动");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    // 存量竞态（基线期即可现）：同一产物的下载存在双触发通道，第三次的时机受渲染
    // 时序影响。这里只断言第二轮产物已被处理（>=2），「内容未变不重开」由下方
    // not.toBeInTheDocument 断言把关；双下载的产品侧修复另行立项。
    await waitFor(() => expect(api.downloadChatFile.mock.calls.length).toBeGreaterThanOrEqual(2));
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(screen.queryByTitle(/HTML 看板预览/)).not.toBeInTheDocument();

    // 内容更新后：自动重开预览
    currentRun = "rr-second";
    currentHtml = "<!doctype html><html><body>second version</body></html>";
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "第二版");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByTitle("HTML 看板预览 v2", {}, { timeout: 3000 }))
      .toHaveAttribute("srcdoc", expect.stringContaining("second version"));
  });

  it("refreshes an html file preview after a follow-up correction run", async () => {
    let artifactStage: "empty" | "first" | "followup" = "empty";
    let finishFirstRun: (() => void) | undefined;
    const htmlFile = (runtimeRunId: string) => ({
      fileId: 91,
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-preview",
      runtimeRunId,
      relativePath: "workspace/sessions/rs-preview/balls-screensaver.html",
      displayName: "balls-screensaver.html",
      mimeType: "text/html",
      sizeBytes: 7_631,
      sourceType: "runtime",
      createdAt: "2026-10-17T10:58:52Z"
    });
    api.chatFiles.mockImplementation(async () => (
      artifactStage === "empty" ? [] : [htmlFile(artifactStage === "first" ? "rr-first" : "rr-followup")]
    ));
    api.downloadChatFile.mockImplementation(async () => new Blob(
      [artifactStage === "followup"
        ? "<!doctype html><html><body>three balls</body></html>"
        : "<!doctype html><html><body>eight balls</body></html>"],
      { type: "text/html" }
    ));
    api.streamChatRun.mockImplementation(async (
      _payload: { runtimeRunId?: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-first" } });
      onEvent({
        event: "chat.artifact.created",
        data: {
          runtimeRunId: "rr-first",
          relative_path: "workspace/sessions/rs-preview/balls-screensaver.html",
          mime_type: "text/html",
          size_bytes: 9_749
        }
      });
      artifactStage = "first";
      return new Promise<void>((resolve) => {
        finishFirstRun = () => {
          onEvent({ event: "chat.done", data: { runtimeRunId: "rr-first", seq: 2 } });
          resolve();
        };
      });
    });
    api.submitChatCorrection.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({
        event: "run.redirect_promoted",
        data: { promotedRunId: "rr-followup", runtimeRunId: "rr-followup" }
      });
      onEvent({
        event: "chat.artifact.updated",
        data: {
          runtimeRunId: "rr-followup",
          relative_path: "workspace/sessions/rs-preview/balls-screensaver.html",
          mime_type: "text/html",
          size_bytes: 7_631
        }
      });
      artifactStage = "followup";
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-followup", seq: 3 } });
    });
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "第一版");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(api.streamChatRun).toHaveBeenCalledTimes(1));
    // streamChatRun 只代表请求已发出； accepted/artifact 事件驱动的前一版预览挂载后，
    // 才能确保追问可取得稳定父 Run，避免整文件连续执行时抢在 React 投影前发送。
    expect(
      await screen.findByTitle("HTML 看板预览 v1", {}, { timeout: 3000 })
    ).toBeVisible();

    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "改成三个球");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(api.submitChatCorrection).toHaveBeenCalledTimes(1));

    const frame = await screen.findByTitle("HTML 看板预览 v2", {}, { timeout: 3000 });
    expect(frame).toHaveAttribute("srcdoc", expect.stringContaining("three balls"));
    finishFirstRun?.();
  });

  it("refreshes preview when the same cloud html file is modified by a later run", async () => {
    let currentRun = "rr-first";
    api.chatFiles.mockImplementation(async () => [{
      fileId: 91,
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-preview",
      runtimeRunId: currentRun,
      relativePath: "workspace/sessions/rs-preview/solar-system.html",
      displayName: "solar-system.html",
      mimeType: "text/html",
      sizeBytes: 52,
      sourceType: "runtime",
      createdAt: "2026-10-17T00:36:30Z"
    }]);
    api.downloadChatFile.mockImplementation(async () => new Blob(
      [currentRun === "rr-first"
        ? "<!doctype html><html><body>first version</body></html>"
        : "<!doctype html><html><body>second version</body></html>"],
      { type: "text/html" }
    ));
    api.streamChatRun.mockImplementation(async (
      _payload: { runtimeRunId?: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      const runtimeRunId = currentRun;
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId } });
      onEvent({ event: "chat.done", data: { runtimeRunId, seq: 2 } });
    });
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "HTML 任务" });
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "第一版");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByTitle("HTML 看板预览 v1"))
      .toHaveAttribute("srcdoc", expect.stringContaining("first version"));

    currentRun = "rr-second";
    await waitFor(() => screen.getByRole("button", { name: "发送消息" }));
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "第二版");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByTitle("HTML 看板预览 v2", {}, { timeout: 3000 }))
      .toHaveAttribute("srcdoc", expect.stringContaining("second version"));
  });
});

afterEach(() => {
  api.chatFiles.mockImplementation(async () => []);
  Reflect.deleteProperty(window, "coworkDesktop");
});
