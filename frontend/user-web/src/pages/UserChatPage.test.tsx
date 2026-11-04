import type { ReactNode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  bootstrapRuntime: vi.fn(), keepaliveRuntime: vi.fn(), prewarmRuntimeSession: vi.fn(), runtimeWorkspace: vi.fn(), runtimeModels: vi.fn(), runtimeSessions: vi.fn(), expertPlaza: vi.fn(), runnableExperts: vi.fn(), expertMarket: vi.fn(), expertInheritanceSources: vi.fn(), expertCapabilityCatalog: vi.fn(), createExpert: vi.fn(), updateExpert: vi.fn(), replaceExpertCapabilities: vi.fn(), replaceExpertPermissions: vi.fn(), submitExpertReview: vi.fn(), installMarketExpert: vi.fn(), updateInstalledExpert: vi.fn(), unpublishExpert: vi.fn(), uninstallExpert: vi.fn(), organizationDirectoryTree: vi.fn(), searchDirectoryUsers: vi.fn(),
  chatHistory: vi.fn(), chatHistoryPreview: vi.fn(), chatHistoryAuthoritative: vi.fn(), chatFiles: vi.fn(), chatQueueSummary: vi.fn(), chatQueueSummaryPrepared: vi.fn(), activeChatRun: vi.fn(), backgroundCollaborations: vi.fn(), streamBackgroundCollaborations: vi.fn(), reconnectChatRun: vi.fn(), createRuntimeSession: vi.fn(), renameRuntimeSession: vi.fn(), deleteRuntimeSession: vi.fn(), streamChatRun: vi.fn(), submitChatCorrection: vi.fn(), clarifyRequests: vi.fn(), answerClarifyRequest: vi.fn(), cancelClarifyRequest: vi.fn(),
  reportChatClientTiming: vi.fn(), runActivity: vi.fn(), stopChatRun: vi.fn(), setSessionModel: vi.fn(), warmupRuntime: vi.fn(), installExpert: vi.fn(), personalSkillAssets: vi.fn(), personalSkillAssetDetail: vi.fn(), uploadPersonalSkill: vi.fn(), archivePersonalSkill: vi.fn(), deletePersonalSkill: vi.fn(), uploadChatAttachment: vi.fn(), downloadChatFile: vi.fn(), officePreviewSession: vi.fn(), renameChatFile: vi.fn(), deleteChatFile: vi.fn(), listScheduledTasks: vi.fn(), createScheduledTask: vi.fn(), updateScheduledTask: vi.fn(), pauseScheduledTask: vi.fn(), resumeScheduledTask: vi.fn(), triggerScheduledTask: vi.fn(), deleteScheduledTask: vi.fn(), listApps: vi.fn(), taskPromptConfig: vi.fn(), logout: vi.fn()
}));

vi.mock("../api/client", () => ({ userApi: api }));

// 技能/专家面板经 React.lazy 动态加载；antd Provider 包装与页面断言无关，
// mock 掉以避免测试运行时触发 antd 的重量级 transform（antd 渲染由组件级测试覆盖）。
vi.mock("../theme/antd-module", () => ({
  AntdModuleProviders: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
// 技能面板经 React.lazy 动态加载且自身静态依赖 antd；antd 在 vitest 的 transform
// 成本（冷启动实测 15-35s）远超单测超时。beforeAll 预热模块（独立超时），
// 同 worker 内后续动态 import 直接命中缓存。
beforeAll(async () => {
  await Promise.all([
    import("../components/skills"),
    import("../components/experts")
  ]);
}, 120_000);

import { UserChatPage } from "./UserChatPage";
import { clearSession, setSession } from "../auth";
import {
  RUNTIME_EDGE_QUEUE_UPDATED_EVENT,
  RuntimeEdgeTransportError
} from "../api/runtime-edge-client";
import { emptyChatQueueSnapshot } from "./chatQueueProjection";
import { ERROR_NOTICE_AUTO_DISMISS_MS } from "./useAutoDismissError";

function taskHeader() {
  const header = document.querySelector<HTMLElement>(".appHeader");
  if (!header) throw new Error("task header is not rendered");
  return { header, scope: within(header) };
}

const readyReceipt = (
  runtimeSessionId: string,
  overrides: Record<string, unknown> = {}
) => ({
  state: "ready" as const,
  readyReceiptId: `receipt-${runtimeSessionId}`,
  runtimeWorkspaceId: 1,
  runtimeSessionId,
  modelEntryId: null,
  runtimeAgentProfileId: null,
  profileGeneration: 0,
  selectionEpoch: 1,
  issuedAt: "2026-08-03T00:00:00Z",
  expiresAt: "2026-08-03T00:01:00Z",
  ...overrides
});

const taskPromptConfig = {
  revision: 1,
  config: {
    tabs: [
      {
        label: "看板",
        description: "快速生成经营数据、项目进度和业务指标看板。",
        isDefault: true,
        showBoardGallery: true,
        topics: [
          {
            title: "数据看板",
            tasks: [
              { summary: "XXXX", detail: "XXXX 的详细任务描述" },
              { summary: "YYYYYY", detail: "YYYYYY 的详细任务描述" }
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
            tasks: [{ summary: "3333", detail: "3333 的详细任务描述" }]
          }
        ]
      }
    ]
  }
};

describe("UserChatPage", () => {
  afterEach(() => {
    vi.useRealTimers();
    delete (api as Record<string, unknown>).plazaMcps;
  });

  beforeEach(() => {
    vi.clearAllMocks();
    api.prewarmRuntimeSession.mockReset();
    api.streamChatRun.mockReset();
    api.submitChatCorrection.mockReset();
    api.runActivity.mockReset();
    api.renameRuntimeSession.mockReset();
    window.confirm = vi.fn().mockReturnValue(true);
    localStorage.setItem("hermes.files-pane", "true");
    api.bootstrapRuntime.mockResolvedValue({ runtimeWorkspaceId: 1, runtimeStatus: "ready" });
    api.keepaliveRuntime.mockResolvedValue(undefined);
    api.prewarmRuntimeSession.mockResolvedValue(undefined);
    api.streamChatRun.mockResolvedValue(undefined);
    api.submitChatCorrection.mockResolvedValue({
      kind: "consumed",
      clientMessageId: "correction",
      parentRunId: "run-parent"
    });
    api.warmupRuntime.mockResolvedValue({ runtimeWorkspaceId: 1, runtimeStatus: "ready" });
    api.runtimeWorkspace.mockResolvedValue({ runtimeWorkspaceId: 1, runtimeStatus: "ready" });
    api.runtimeModels.mockResolvedValue([]);
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-a", title: "会话 A", messageCount: 1, sourceType: "web" },
      { runtimeSessionId: "rs-1-b", title: "会话 B", messageCount: 1, sourceType: "web" }
    ]);
    api.renameRuntimeSession.mockImplementation(async (
      _runtimeWorkspaceId: number,
      runtimeSessionId: string,
      payload: { title: string }
    ) => ({
      runtimeSessionId,
      title: payload.title,
      messageCount: 0,
      sourceType: "web",
      status: "active"
    }));
    api.runnableExperts.mockResolvedValue([]);
    api.expertMarket.mockResolvedValue([]);
    api.expertInheritanceSources.mockResolvedValue([]);
    api.expertCapabilityCatalog.mockResolvedValue({
      skills: [],
      mcps: [],
      knowledgeBases: [],
      pluginIds: []
    });
    api.organizationDirectoryTree.mockResolvedValue([]);
    api.searchDirectoryUsers.mockResolvedValue([]);
    api.expertPlaza.mockResolvedValue([]);
    api.personalSkillAssets.mockResolvedValue([]);
    api.personalSkillAssetDetail.mockResolvedValue({ asset: { skillId: 2, skillVersionId: 3, slug: "personal", version: 1, name: "Personal", status: "private", source: "personal_asset", applied: false }, detail: { fileTree: ["SKILL.md"] } });
    api.uploadPersonalSkill.mockResolvedValue({ skillId: 2, skillVersionId: 3, slug: "personal", version: 1, name: "Personal", status: "private", source: "personal_asset", applied: false });
    api.archivePersonalSkill.mockResolvedValue({});
    api.deletePersonalSkill.mockResolvedValue({ rolloutId: "personal-skill-delete-2", affectedProfileIds: [] });
    api.chatHistory.mockResolvedValue({ messages: [] });
    api.chatHistoryPreview.mockImplementation(async (runtimeWorkspaceId: number, runtimeSessionId: string) => ({
      ...(await api.chatHistory(runtimeWorkspaceId, runtimeSessionId)),
      runtimeWorkspaceId,
      runtimeSessionId,
      source: "mysql",
      consistency: "projection_preview",
      generatedAt: "2026-08-21T02:00:00.000Z",
      hasActiveRun: false
    }));
    api.chatHistoryAuthoritative.mockImplementation(async (runtimeWorkspaceId: number, runtimeSessionId: string) => ({
      ...(await api.chatHistory(runtimeWorkspaceId, runtimeSessionId)),
      runtimeWorkspaceId,
      runtimeSessionId,
      source: "hermes",
      consistency: "authoritative",
      generatedAt: "2026-08-21T02:00:01.000Z",
      hasActiveRun: false
    }));
    api.chatQueueSummary.mockResolvedValue(emptyChatQueueSnapshot());
    api.chatQueueSummaryPrepared.mockImplementation(
      (runtimeWorkspaceId: number, runtimeSessionId: string) => (
        api.chatQueueSummary(runtimeWorkspaceId, runtimeSessionId)
      )
    );
    api.activeChatRun.mockResolvedValue({ run: null });
    api.clarifyRequests.mockResolvedValue([]);
    api.answerClarifyRequest.mockResolvedValue(undefined);
    api.cancelClarifyRequest.mockResolvedValue(undefined);
    api.backgroundCollaborations.mockResolvedValue({ collaborations: [], pendingCount: 0 });
    api.streamBackgroundCollaborations.mockResolvedValue(undefined);
    api.reportChatClientTiming.mockResolvedValue(undefined);
    api.runActivity.mockRejectedValue(
      Object.assign(new Error("trace expired"), { status: 410 })
    );
    api.listScheduledTasks.mockResolvedValue([]);
    api.listApps.mockResolvedValue([]);
    api.taskPromptConfig.mockResolvedValue(taskPromptConfig);
    api.uploadChatAttachment.mockImplementation(async (file: File) => ({ file: { fileId: 9, displayName: file.name, relativePath: `workspace/sessions/rs-1-a/uploads/${file.name}`, sourceType: "upload", sizeBytes: file.size } }));
    api.chatFiles.mockImplementation(async (_workspaceId: number, sessionId: string) => [{
      fileId: sessionId === "rs-1-a" ? 1 : 2,
      displayName: sessionId === "rs-1-a" ? "A 文件.txt" : "B 文件.txt",
      sourceType: "upload",
      sizeBytes: 1
    }]);
  });

  it("shows and queues one optimistic turn while the exact session prewarm is pending", async () => {
    let finishKeepalive: (() => void) | undefined;
    let finishStream: (() => void) | undefined;
    let runActive = false;
    api.keepaliveRuntime.mockImplementationOnce(() => new Promise<void>((resolve) => { finishKeepalive = resolve; }));
    api.prewarmRuntimeSession.mockResolvedValueOnce(readyReceipt("rs-1-a"));
    api.activeChatRun.mockImplementation(async () => ({
      run: runActive ? { runtimeRunId: "rr-cold-start", status: "running", lastSeq: 0 } : null
    }));
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      runActive = true;
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-cold-start" } });
      await new Promise<void>((resolve) => { finishStream = resolve; });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-cold-start", seq: 1 } });
    });

    render(<UserChatPage />);

    expect(await screen.findByRole("button", { name: "会话 A" })).toBeVisible();
    expect(screen.queryByText("正在为你分配云资源，稍等，马上就好……")).not.toBeInTheDocument();
    expect(screen.queryByText("正在连接平台服务…")).not.toBeInTheDocument();
    expect(api.runtimeModels).toHaveBeenCalledTimes(1);
    expect(api.runtimeSessions).toHaveBeenCalledWith(1);
    await waitFor(() => expect(api.chatHistory).toHaveBeenCalledWith(1, "rs-1-a"));

    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "冷启动期间提交");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(screen.getByText("冷启动期间提交")).toBeVisible();
    expect(
      await screen.findByText(
        "会话准备中，你的消息将在就绪后自动发送…",
        {},
        { timeout: 9_000 }
      )
    ).toBeVisible();
    expect(screen.queryByText("消息已排队")).not.toBeInTheDocument();
    expect(screen.queryByText(
      "会话准备中，就绪后自动发送，无需重复输入。"
    )).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "停止生成" })).toBeVisible();
    expect(api.streamChatRun).not.toHaveBeenCalled();

    expect(api.keepaliveRuntime).toHaveBeenCalledTimes(1);
    await act(async () => { finishKeepalive?.(); });
    await waitFor(() => expect(api.streamChatRun).toHaveBeenCalledTimes(1));
    expect(api.streamChatRun.mock.calls[0]?.[0]).toEqual(expect.objectContaining({
      runtimeRunId: expect.stringMatching(/^web-/),
      readyReceiptId: "receipt-rs-1-a"
    }));
    await screen.findByText("A 文件.txt");
    expect(screen.getByText("冷启动期间提交")).toBeVisible();
    expect(api.reconnectChatRun).not.toHaveBeenCalled();
    await act(async () => { finishStream?.(); });
    await waitFor(() => expect(screen.getByRole("button", { name: "发送消息" })).toBeVisible());
    expect(api.prewarmRuntimeSession.mock.calls.length).toBe(1);
    expect(screen.getByLabelText("消息输入")).toBeVisible();
    expect(screen.queryByText("正在检测运行环境...")).not.toBeInTheDocument();
  });

  it("shows a scheduled queue hint without changing the current run", async () => {
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void,
      signal: AbortSignal
    ) => {
      onEvent({
        event: "chat.run.accepted",
        data: { runtimeRunId: "rr-current" }
      });
      await new Promise<void>((resolve) => {
        signal.addEventListener("abort", () => resolve(), { once: true });
      });
    });

    const view = render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(
      screen.getByRole("textbox", { name: "消息输入" }),
      "当前任务"
    );
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(
      await screen.findByRole("button", { name: "停止生成" })
    ).toBeVisible();

    act(() => {
      window.dispatchEvent(new CustomEvent(RUNTIME_EDGE_QUEUE_UPDATED_EVENT, {
        detail: {
          runtimeSessionId: "rs-1-a",
          queuedCount: 1,
          scheduledQueuedCount: 1
        }
      }));
    });

    expect(screen.getByText("有定时任务排队中…")).toBeVisible();
    expect(screen.getByText("当前任务")).toBeVisible();
    expect(screen.getByRole("button", { name: "停止生成" })).toBeVisible();

    act(() => {
      window.dispatchEvent(new CustomEvent(RUNTIME_EDGE_QUEUE_UPDATED_EVENT, {
        detail: {
          runtimeSessionId: "rs-1-a",
          queuedCount: 0,
          scheduledQueuedCount: 0
        }
      }));
    });

    expect(screen.queryByText("有定时任务排队中…")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "停止生成" })).toBeVisible();
    view.unmount();
  });

  it("keeps the newest queued runs when an older queue snapshot returns late", async () => {
    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");

    let resolveOlder: ((value: ReturnType<typeof emptyChatQueueSnapshot>) => void) | undefined;
    let resolveNewer: ((value: ReturnType<typeof emptyChatQueueSnapshot>) => void) | undefined;
    const older = new Promise<ReturnType<typeof emptyChatQueueSnapshot>>((resolve) => {
      resolveOlder = resolve;
    });
    const newer = new Promise<ReturnType<typeof emptyChatQueueSnapshot>>((resolve) => {
      resolveNewer = resolve;
    });
    api.chatQueueSummary
      .mockImplementationOnce(() => older)
      .mockImplementationOnce(() => newer);

    act(() => {
      for (let queuedCount = 1; queuedCount <= 2; queuedCount += 1) {
        window.dispatchEvent(new CustomEvent(RUNTIME_EDGE_QUEUE_UPDATED_EVENT, {
          detail: {
            runtimeSessionId: "rs-1-a",
            queuedCount,
            scheduledQueuedCount: 0
          }
        }));
      }
    });

    await waitFor(() => expect(api.chatQueueSummary.mock.calls.length).toBeGreaterThanOrEqual(3));
    await act(async () => {
      resolveNewer?.({
        queuedCount: 2,
        scheduledQueuedCount: 0,
        queuedRuns: [
          { runId: "queued-2", clientMessageId: "message-2", enqueueSeq: 2, prompt: "第二条等待消息", attachmentFileIds: [] },
          { runId: "queued-3", clientMessageId: "message-3", enqueueSeq: 3, prompt: "第三条等待消息", attachmentFileIds: [] }
        ],
        activeRun: { runId: "parent-run", state: "RUNNING", cancellationRequestedAt: null }
      });
    });
    expect(await screen.findByText("第二条等待消息")).toBeVisible();
    expect(screen.getByText("第三条等待消息")).toBeVisible();

    await act(async () => {
      resolveOlder?.({
        queuedCount: 1,
        scheduledQueuedCount: 0,
        queuedRuns: [
          { runId: "queued-1", clientMessageId: "message-1", enqueueSeq: 1, prompt: "第一条旧快照消息", attachmentFileIds: [] }
        ],
        activeRun: { runId: "parent-run", state: "RUNNING", cancellationRequestedAt: null }
      });
    });

    expect(screen.queryByText("第一条旧快照消息")).not.toBeInTheDocument();
    expect(screen.getByText("第二条等待消息")).toBeVisible();
    expect(screen.getByText("第三条等待消息")).toBeVisible();
  });

  it("does not derive a queue hint from the submit receipt or block streaming on queue reconciliation", async () => {
    let continueAfterQueued: (() => void) | undefined;
    let finishStream: (() => void) | undefined;
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({
        event: "chat.run.queued",
        data: { runtimeRunId: "rr-immediate", enqueueSeq: 1 }
      });
      await new Promise<void>((resolve) => { continueAfterQueued = resolve; });
      onEvent({
        event: "chat.run.accepted",
        data: { runtimeRunId: "rr-immediate" }
      });
      onEvent({
        event: "chat.message.delta",
        data: { runtimeRunId: "rr-immediate", seq: 1, delta: "立即流式回答" }
      });
      await new Promise<void>((resolve) => { finishStream = resolve; });
      onEvent({
        event: "chat.done",
        data: { runtimeRunId: "rr-immediate", seq: 2, status: "completed" }
      });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const queueCallsBeforeSend = api.chatQueueSummary.mock.calls.length;
    api.chatQueueSummary.mockImplementationOnce(() => new Promise(() => undefined));
    const user = userEvent.setup();
    await user.type(
      screen.getByRole("textbox", { name: "消息输入" }),
      "不要闪烁也不要阻塞"
    );
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(api.streamChatRun).toHaveBeenCalledTimes(1));

    expect(screen.queryByText("有消息排队中…")).not.toBeInTheDocument();

    await act(async () => { continueAfterQueued?.(); });
    expect(await screen.findByText("立即流式回答")).toBeVisible();
    expect(api.chatQueueSummary.mock.calls.length).toBeGreaterThan(
      queueCallsBeforeSend
    );

    await act(async () => { finishStream?.(); });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "发送消息" })).toBeVisible();
    });
  });

  it("accepts one queued send while the Runtime is still being allocated", async () => {
    let finishPrewarm: (
      receipt: ReturnType<typeof readyReceipt>
    ) => void = () => undefined;
    api.bootstrapRuntime.mockResolvedValue({
      runtimeWorkspaceId: 1,
      runtimeStatus: "starting"
    });
    api.prewarmRuntimeSession.mockImplementationOnce(() => new Promise(
      (resolve) => { finishPrewarm = resolve; }
    ));
    api.streamChatRun.mockImplementationOnce(async (
      payload: { runtimeRunId: string },
      onEvent: (
        event: {
          event: string;
          data: Record<string, unknown>;
        }
      ) => void
    ) => {
      onEvent({
        event: "chat.run.accepted",
        data: { runtimeRunId: payload.runtimeRunId }
      });
      onEvent({
        event: "chat.done",
        data: { runtimeRunId: payload.runtimeRunId, seq: 1 }
      });
    });

    render(<UserChatPage />);

    const textbox = await screen.findByRole("textbox", {
      name: "消息输入"
    });
    expect(textbox).toBeEnabled();
    fireEvent.change(textbox, {
      target: { value: "Runtime 冷启动排队消息" }
    });
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));

    expect(screen.getByText("Runtime 冷启动排队消息")).toBeVisible();
    expect(screen.getByText("会话准备中，你的消息将在就绪后自动发送…")).toBeVisible();
    expect(api.prewarmRuntimeSession).toHaveBeenCalledTimes(1);
    expect(api.streamChatRun).not.toHaveBeenCalled();

    await act(async () => {
      finishPrewarm(readyReceipt("rs-1-a"));
    });

    await waitFor(() => {
      expect(api.streamChatRun).toHaveBeenCalledTimes(1);
    });
    expect(screen.queryByText("正在为你分配云资源，稍等，马上就好……")).not.toBeInTheDocument();
    expect(api.streamChatRun.mock.calls[0]?.[0]).toEqual(
      expect.objectContaining({
        message: "Runtime 冷启动排队消息",
        readyReceiptId: "receipt-rs-1-a",
        runtimeRunId: expect.stringMatching(/^web-/)
      })
    );
    expect(api.prewarmRuntimeSession.mock.calls.length).toBe(1);
  });

  it.each([
    ["null", null],
    ["absent", "absent"]
  ])("polls Runtime status when bootstrap returns %s", async (_label, runtimeStatus) => {
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    api.bootstrapRuntime.mockResolvedValue({ runtimeWorkspaceId: 1, runtimeStatus });
    api.runtimeWorkspace.mockResolvedValue({ runtimeWorkspaceId: 1, runtimeStatus: "ready" });

    render(<UserChatPage />);

    expect(await screen.findByRole("button", { name: "会话 A" })).toBeVisible();
    const pollingCall = setIntervalSpy.mock.calls.find(([, delay]) => delay === 3000);
    expect(pollingCall?.[0]).toEqual(expect.any(Function));

    await act(async () => {
      (pollingCall?.[0] as () => void)();
      await Promise.resolve();
    });

    await waitFor(() => expect(api.runtimeWorkspace).toHaveBeenCalledTimes(1));
    setIntervalSpy.mockRestore();
  });

  it("does not keep retrying a legacy draining response past the retry window", async () => {
    let rejectBlockedPrewarm: ((reason: Error) => void) | undefined;
    let prewarmCalls = 0;
    const performanceNow = vi.spyOn(performance, "now").mockReturnValue(0);
    api.prewarmRuntimeSession.mockImplementation(
      async (_workspaceId: number, targetSessionId: string) => {
        if (targetSessionId !== "rs-1-b") {
          return readyReceipt(targetSessionId);
        }
        prewarmCalls += 1;
        if (prewarmCalls === 1) {
          return new Promise((_, reject) => {
            rejectBlockedPrewarm = reject;
          });
        }
        return readyReceipt("rs-1-b");
      }
    );
    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await waitFor(() => expect(prewarmCalls).toBe(1));
    await user.type(
      screen.getByRole("textbox", { name: "消息输入" }),
      "等待旧会话结束"
    );
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(
      await screen.findByText(
        "会话准备中，你的消息将在就绪后自动发送…",
        {},
        { timeout: 9_000 }
      )
    ).toBeVisible();

    performanceNow.mockReturnValue(31_000);
    await act(async () => {
      rejectBlockedPrewarm?.(
        Object.assign(
          new Error("上一会话仍在执行，结束后将自动发送"),
          {
            status: 409,
            code: "DASHBOARD_CHANNEL_DRAINING",
            runCreated: false
          }
        )
      );
    });

    await waitFor(() => expect(prewarmCalls).toBe(1));
    expect(api.streamChatRun).not.toHaveBeenCalled();
    expect(
      await screen.findByText("上一会话仍在执行，结束后将自动发送")
    ).toBeVisible();
    performanceNow.mockRestore();
  });

  it("does not resubmit a prompt after a receipt conflict", async () => {
    api.prewarmRuntimeSession.mockImplementation(async () => {
      return readyReceipt("rs-1-a", {
        readyReceiptId: "receipt-rs-1-a-old",
        selectionEpoch: 1
      });
    });
    api.streamChatRun.mockRejectedValueOnce(Object.assign(
      new Error("当前会话准备状态已变化"),
      {
        status: 409,
        code: "READY_RECEIPT_INVALIDATED",
        runCreated: false
      }
    ));

    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());

    const user = userEvent.setup();
    await user.type(
      screen.getByRole("textbox", { name: "消息输入" }),
      "Receipt 竞态只发一轮"
    );
    await user.click(
      screen.getByRole("button", { name: "发送消息" })
    );

    await waitFor(() => expect(api.streamChatRun).toHaveBeenCalledTimes(1));
    const firstPayload = api.streamChatRun.mock.calls[0]?.[0];
    expect(firstPayload).toEqual(expect.objectContaining({
      readyReceiptId: "receipt-rs-1-a-old",
      runtimeRunId: expect.stringMatching(/^web-/)
    }));
    expect(
      screen.getAllByText("Receipt 竞态只发一轮").filter(
        (element) => element.matches(".messagePlainText")
      )
    ).toHaveLength(1);
    expect(screen.getByRole("textbox", { name: "消息输入" }))
      .toHaveValue("Receipt 竞态只发一轮");
    expect(
      await screen.findByText("当前会话准备状态已变化")
    ).toBeVisible();
  });

  it.each([
    [
      "PERSONAL_ACTIVE_SESSION_LIMIT",
      "当前工作中的会话已达到 10 个，请等待其中一个完成或停止后再试"
    ],
    [
      "SESSION_BUSY",
      "当前会话正在运行，请等待完成或手动结束后再试"
    ]
  ])("shows the exact %s admission message", async (code, message) => {
    api.streamChatRun.mockRejectedValue(
      Object.assign(new Error(message), { code, retryable: false })
    );

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "admission test");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText(message)).toBeVisible();
  });

  it("retries an unaccepted Runtime Edge transport failure with the same message id", async () => {
    api.streamChatRun
      .mockRejectedValueOnce(Object.assign(
        new Error("Runtime 实时连接已中断"),
        { retryable: true }
      ))
      .mockImplementationOnce(async (
        payload: { runtimeRunId: string },
        onEvent: (
          event: { event: string; data: Record<string, unknown> }
        ) => void
      ) => {
        onEvent({
          event: "chat.run.accepted",
          data: { runtimeRunId: "rr-recovered-before-acceptance" }
        });
        onEvent({
          event: "chat.done",
          data: {
            runtimeRunId: "rr-recovered-before-acceptance",
            seq: 1
          }
        });
      });

    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    const user = userEvent.setup();
    await user.type(
      screen.getByRole("textbox", { name: "消息输入" }),
      "保持幂等重试"
    );
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(api.streamChatRun).toHaveBeenCalledTimes(2));
    expect(api.streamChatRun.mock.calls[1]?.[0]).toEqual(
      api.streamChatRun.mock.calls[0]?.[0]
    );
    expect(await screen.findByRole("button", { name: "发送消息" })).toBeVisible();
  });

  it("starts one initial load under React StrictMode", async () => {
    render(<StrictMode><UserChatPage /></StrictMode>);

    expect(await screen.findByRole("button", { name: "会话 A" })).toBeVisible();
    expect(api.bootstrapRuntime).toHaveBeenCalledTimes(1);
    expect(api.keepaliveRuntime).toHaveBeenCalledTimes(1);
    expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-a");
  });

  it("shows MySQL preview then replaces it with Hermes history without duplicate turns", async () => {
    let resolveAuthority: ((value: Record<string, unknown>) => void) | undefined;
    api.chatHistoryPreview.mockResolvedValue({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-1-a",
      source: "mysql",
      consistency: "projection_preview",
      generatedAt: "2026-08-21T02:00:00.000Z",
      hasActiveRun: false,
      messages: [
        { role: "user", content: "问题", runtimeRunId: "run-1", messageSeq: 0 },
        { role: "assistant", content: "旧答案", runtimeRunId: "run-1", messageSeq: 1 }
      ]
    });
    api.chatHistoryAuthoritative.mockImplementation(() => new Promise((resolve) => {
      resolveAuthority = resolve;
    }));

    render(<UserChatPage />);

    expect(await screen.findByText("旧答案")).toBeVisible();
    expect(screen.queryByText("正在同步最新记录…")).not.toBeInTheDocument();
    await act(async () => {
      resolveAuthority?.({
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-a",
        source: "hermes",
        consistency: "authoritative",
        generatedAt: "2026-08-21T02:00:01.000Z",
        hasActiveRun: false,
        messages: [
          { role: "user", content: "问题", runtimeRunId: "run-1" },
          { role: "assistant", content: "新答案", runtimeRunId: "run-1" }
        ]
      });
    });

    expect(await screen.findByText("新答案")).toBeVisible();
    expect(screen.queryByText("旧答案")).not.toBeInTheDocument();
    expect(screen.getAllByText("问题")).toHaveLength(1);
    expect(screen.getAllByText("新答案")).toHaveLength(1);
    expect(screen.queryByText("正在同步最新记录…")).not.toBeInTheDocument();
    expect(api.prewarmRuntimeSession.mock.calls.filter(
      ([workspaceId, selectedSessionId]) => (
        workspaceId === 1 && selectedSessionId === "rs-1-a"
      )
    )).toHaveLength(1);
  });

  it.each([
    ["an empty session list", []],
    ["only invisible sessions", [
      { runtimeSessionId: "rs-smoke", title: "Smoke", messageCount: 0, sourceType: "smoke" }
    ]]
  ])("creates and selects an initial task for %s", async (_label, sessions) => {
    api.runtimeModels.mockResolvedValue([
      { modelEntryId: 19, displayName: "Model A", modelKey: "model-a", providerId: 1, providerDisplayName: "Provider A", isDefault: true }
    ]);
    api.runtimeSessions.mockResolvedValue(sessions);
    api.createRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-1-initial",
      title: "新任务",
      messageCount: 0,
      sourceType: "web",
      sessionModelEntryId: 19
    });

    render(<UserChatPage />);

    await waitFor(() => expect(api.createRuntimeSession).toHaveBeenCalledWith(1, {
      title: "新任务",
      modelEntryId: 19,
      sourceType: "web"
    }));
    expect(await taskHeader().scope.findByText("新任务")).toBeVisible();
    expect(screen.getByRole("button", { name: "新任务" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "消息输入" })).toBeEnabled();
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-initial"));
  });

  it("does not create an initial task when a visible session already exists", async () => {
    render(<UserChatPage />);

    expect(await taskHeader().scope.findByText("会话 A")).toBeVisible();
    expect(api.createRuntimeSession).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("textbox", { name: "消息输入" })).toHaveFocus());
  });

  it("renews the selected Runtime Edge session when the page becomes visible", async () => {
    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    api.prewarmRuntimeSession.mockClear();

    act(() => document.dispatchEvent(new Event("visibilitychange")));

    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(
      1,
      "rs-1-a"
    ));
  });

  it("returns focus to the composer after switching sessions or returning to tasks", async () => {
    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "消息输入" })).toHaveFocus());

    await user.click(screen.getByRole("button", { name: "专家-技能" }));
    await user.click(await screen.findByRole("tab", { name: "技能" }));
    expect(await screen.findByRole("region", { name: "技能广场" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "任务" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "消息输入" })).toHaveFocus());
  });

  it("keeps application-only MCP configuration out of the personal navigation", async () => {
    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    expect(screen.queryByRole("button", { name: "MCP" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "专家" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "技能" })).not.toBeInTheDocument();
  });

  it("keeps the healthy Runtime connected while the selected session is prewarmed", async () => {
    let finishSessionPrewarm: (() => void) | undefined;
    api.prewarmRuntimeSession.mockImplementationOnce(() => new Promise<void>(
      (resolve) => { finishSessionPrewarm = resolve; }
    ));

    render(<UserChatPage />);

    expect(await screen.findByRole("button", { name: "会话 A" })).toBeVisible();
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-a"));
    expect(screen.getByLabelText("消息输入")).toBeVisible();
    expect(screen.queryByText("正在检测运行环境...")).not.toBeInTheDocument();

    await act(async () => { finishSessionPrewarm?.(); });

    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
  });

  it("ignores a late prewarm result after the exact session target changes", async () => {
    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());

    let finishB!: (state: "ready") => void;
    let finishA!: (state: "ready") => void;
    api.prewarmRuntimeSession.mockImplementation(
      (_workspaceId: number, selectedSessionId: string) => (
        new Promise<"ready">((resolve) => {
          if (selectedSessionId === "rs-1-b") finishB = resolve;
          else finishA = resolve;
        })
      )
    );
    api.prewarmRuntimeSession.mockClear();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-b"));
    expect(screen.getByLabelText("消息输入")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "会话 A" }));
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-a"));

    await act(async () => { finishB("ready"); });
    expect(screen.getByLabelText("消息输入")).toBeVisible();
    expect(screen.queryByText("正在检测运行环境...")).not.toBeInTheDocument();

    await act(async () => { finishA("ready"); });
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
  });

  it("ignores a late initial-session prewarm failure after the new session is ready", async () => {
    let failInitialA!: (reason: Error) => void;
    let finishB!: (state: "ready") => void;
    let aCalls = 0;
    api.prewarmRuntimeSession.mockImplementation(
      (_workspaceId: number, selectedSessionId: string) => {
        if (selectedSessionId === "rs-1-b") {
          return new Promise<"ready">((resolve) => { finishB = resolve; });
        }
        aCalls += 1;
        if (aCalls === 1) {
          return new Promise<"ready">((_resolve, reject) => {
            failInitialA = reject;
          });
        }
        return Promise.reject(new Error("stale initial prewarm failed"));
      }
    );

    render(<UserChatPage />);
    expect(await screen.findByRole("button", { name: "会话 A" })).toBeVisible();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-b"));
    await act(async () => { finishB("ready"); });
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    await waitFor(() => expect(api.activeChatRun).toHaveBeenCalledWith(1, "rs-1-b"));

    await act(async () => { failInitialA(new Error("stale initial prewarm failed")); });
    await waitFor(() => expect(aCalls).toBeGreaterThanOrEqual(2));
    expect(screen.getByLabelText("消息输入")).toBeVisible();
    expect(screen.queryByText("stale initial prewarm failed")).not.toBeInTheDocument();
  });

  it("stops a locally queued prewarm turn without creating or stopping a backend run", async () => {
    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());

    let finishPrewarm!: (state: ReturnType<typeof readyReceipt>) => void;
    api.prewarmRuntimeSession.mockImplementation(
      (_workspaceId: number, selectedSessionId: string) => (
        selectedSessionId === "rs-1-b"
          ? new Promise<ReturnType<typeof readyReceipt>>((resolve) => { finishPrewarm = resolve; })
          : Promise.resolve(readyReceipt(selectedSessionId))
      )
    );
    api.streamChatRun.mockClear();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-b"));

    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "等待预热后发送");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(screen.getByText("等待预热后发送")).toBeVisible();
    expect(api.streamChatRun).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "停止生成" }));
    expect(await screen.findByRole("button", { name: "发送消息" })).toBeVisible();
    expect(api.stopChatRun).not.toHaveBeenCalled();
    expect(api.streamChatRun).not.toHaveBeenCalled();

    await act(async () => { finishPrewarm(readyReceipt("rs-1-b")); });
    expect(api.streamChatRun).not.toHaveBeenCalled();
  });

  it("cancels a locally queued turn when the current session is selected again", async () => {
    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());

    let finishPrewarm!: (state: ReturnType<typeof readyReceipt>) => void;
    api.prewarmRuntimeSession.mockImplementation(
      (_workspaceId: number, selectedSessionId: string) => (
        selectedSessionId === "rs-1-b"
          ? new Promise<ReturnType<typeof readyReceipt>>((resolve) => { finishPrewarm = resolve; })
          : Promise.resolve(readyReceipt(selectedSessionId))
      )
    );
    api.streamChatRun.mockClear();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-b"));

    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "重新选择当前会话");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(screen.getByRole("button", { name: "停止生成" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "会话 B" }));
    expect(await screen.findByRole("button", { name: "发送消息" })).toBeVisible();
    expect(api.stopChatRun).not.toHaveBeenCalled();
    expect(api.streamChatRun).not.toHaveBeenCalled();

    await act(async () => { finishPrewarm(readyReceipt("rs-1-b")); });
    expect(api.streamChatRun).not.toHaveBeenCalled();
  });

  it("renders an inline failure and does not submit when required prewarm fails", async () => {
    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    let rejectPrewarm!: (reason: Error) => void;
    api.prewarmRuntimeSession.mockImplementation(
      (_workspaceId: number, selectedSessionId: string) => (
        selectedSessionId === "rs-1-b"
          ? new Promise<ReturnType<typeof readyReceipt>>((_resolve, reject) => {
            rejectPrewarm = reject;
          })
          : Promise.resolve(readyReceipt(selectedSessionId))
      )
    );
    api.streamChatRun.mockClear();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-b"));
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "不要提前发送");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(
      screen.getAllByText("不要提前发送").filter(
        (element) => element.matches(".messagePlainText")
      )
    ).toHaveLength(1);
    await act(async () => {
      rejectPrewarm(Object.assign(
        new RuntimeEdgeTransportError(
          "Unknown provider custom:runtime-secret-route key=secret",
          "RUNTIME_SESSION_PREPARE_FAILED",
          true,
          {
            phase: "live_execution_release",
            auditEventId: "audit-prepare-0001"
          }
        ),
        { status: 422 }
      ));
    });
    expect(await screen.findByText(
      "运行环境准备失败（阶段：释放旧会话；诊断编号：audit-prepare-0001），请重试或联系管理员。"
    )).toBeVisible();
    expect(screen.queryByText(/custom:runtime-secret-route/)).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "消息输入" }))
      .toHaveValue("不要提前发送");
    expect(api.streamChatRun).not.toHaveBeenCalled();

    // The failed prewarm must not retain the one global queued-turn slot and
    // block a send after switching to another ready conversation.
    api.prewarmRuntimeSession.mockResolvedValue(readyReceipt("rs-1-a"));
    await user.click(screen.getByRole("button", { name: "会话 A" }));
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-a"));
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "其他会话仍可发送");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(api.streamChatRun).toHaveBeenCalledWith(
      expect.objectContaining({ runtimeSessionId: "rs-1-a", message: "其他会话仍可发送" }),
      expect.any(Function),
      expect.any(AbortSignal)
    ));
  });

  it("keeps a fixed WeCom history read-only without entering required prewarm", async () => {
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-a", title: "会话 A", messageCount: 1, sourceType: "web" },
      {
        runtimeSessionId: "rs-1-wecom",
        title: "企微固定会话",
        messageCount: 1,
        sourceType: "wecom",
        isReadOnly: true,
        isImmutable: true,
        originLabel: "个人企微"
      }
    ]);
    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    api.prewarmRuntimeSession.mockClear();

    await userEvent.setup().click(
      screen.getByRole("button", { name: /企微固定会话/ })
    );

    expect(await screen.findByText("查看企微会话")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "消息输入" })).toBeDisabled();
    expect(screen.getByLabelText("消息输入")).toBeVisible();
    expect(api.prewarmRuntimeSession).not.toHaveBeenCalled();
  });

  it("opens an uninstalled expert history read-only without a sync failure retry", async () => {
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-a", title: "会话 A", messageCount: 1, sourceType: "web" },
      {
        runtimeSessionId: "rs-expert-history",
        title: "已卸载专家历史",
        messageCount: 1,
        sourceType: "web",
        agentProfileKind: "expert",
        agentDisplayName: "市场洞察专家",
        status: "expert_deleted",
        isReadOnly: true,
        unavailableReason: "expert_deleted"
      }
    ]);
    api.chatHistory.mockImplementation(async (_workspaceId: number, sessionId: string) => ({
      messages: sessionId === "rs-expert-history"
        ? [{ id: "history-message", role: "assistant", content: "历史洞察结论" }]
        : []
    }));
    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    api.prewarmRuntimeSession.mockClear();

    await userEvent.setup().click(
      screen.getByTitle("已卸载专家历史 · 市场洞察专家")
    );

    expect(await screen.findByText("历史洞察结论")).toBeVisible();
    expect(screen.getByText(/该专家已卸载或删除.*重新安装会创建新的专家入口/)).toBeVisible();
    expect(screen.getByRole("textbox", { name: "消息输入" })).toBeDisabled();
    expect(screen.queryByText(/最新记录同步失败/)).not.toBeInTheDocument();
    expect(api.prewarmRuntimeSession).not.toHaveBeenCalled();
    expect(api.chatHistoryAuthoritative).not.toHaveBeenCalledWith(
      1,
      "rs-expert-history"
    );
  });

  it("prewarms the selected session while workspace keepalive is still pending", async () => {
    let finishKeepalive: (() => void) | undefined;
    api.keepaliveRuntime.mockImplementationOnce(() => new Promise<void>(
      (resolve) => { finishKeepalive = resolve; }
    ));

    render(<UserChatPage />);

    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-a"));
    expect(screen.queryByText("正在为你分配云资源，稍等，马上就好……")).not.toBeInTheDocument();
    expect(screen.queryByText("已连接")).not.toBeInTheDocument();

    await act(async () => { finishKeepalive?.(); });
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
  });

  it("prewarms an existing session before model and expert catalogs finish loading", async () => {
    let finishModels: (() => void) | undefined;
    let finishExperts: (() => void) | undefined;
    api.runtimeModels.mockImplementationOnce(() => new Promise<[]>(
      (resolve) => { finishModels = () => resolve([]); }
    ));
    api.runnableExperts.mockImplementationOnce(() => new Promise<[]>(
      (resolve) => { finishExperts = () => resolve([]); }
    ));

    render(<UserChatPage />);

    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-a"));
    expect(screen.queryByRole("button", { name: "会话 A" })).not.toBeInTheDocument();

    await act(async () => {
      finishModels?.();
      finishExperts?.();
    });
    expect(await screen.findByRole("button", { name: "会话 A" })).toBeVisible();
  });

  it("prewarms an auto-created initial session before the expert catalog finishes loading", async () => {
    let finishExperts: (() => void) | undefined;
    api.runtimeModels.mockResolvedValue([
      { modelEntryId: 19, displayName: "Model A", modelKey: "model-a", providerId: 1, providerDisplayName: "Provider A", isDefault: true }
    ]);
    api.runtimeSessions.mockResolvedValue([]);
    api.runnableExperts.mockImplementationOnce(() => new Promise<[]>(
      (resolve) => { finishExperts = () => resolve([]); }
    ));
    api.createRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-1-initial",
      title: "新任务",
      messageCount: 0,
      sourceType: "web",
      sessionModelEntryId: 19
    });

    render(<UserChatPage />);

    await waitFor(() => expect(api.createRuntimeSession).toHaveBeenCalledWith(1, {
      title: "新任务",
      modelEntryId: 19,
      sourceType: "web"
    }));
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-initial"));
    expect(screen.queryByRole("button", { name: "新任务" })).not.toBeInTheDocument();

    await act(async () => { finishExperts?.(); });
    expect(await screen.findByRole("button", { name: "新任务" })).toBeVisible();
  });

  it("retries an early session prewarm after workspace keepalive succeeds", async () => {
    let finishKeepalive: (() => void) | undefined;
    api.keepaliveRuntime.mockImplementationOnce(() => new Promise<void>(
      (resolve) => { finishKeepalive = resolve; }
    ));
    api.prewarmRuntimeSession
      .mockRejectedValueOnce(new Error("runtime is starting"))
      .mockResolvedValueOnce(undefined);

    render(<UserChatPage />);

    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledTimes(1));
    await act(async () => { finishKeepalive?.(); });

    await waitFor(() => expect(api.prewarmRuntimeSession.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(api.prewarmRuntimeSession).toHaveBeenLastCalledWith(1, "rs-1-a");
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
  });

  it("probes a ready runtime before metadata loading completes", async () => {
    let releaseModels: (() => void) | undefined;
    api.runtimeModels.mockImplementationOnce(() => new Promise<[]>(
      (resolve) => { releaseModels = () => resolve([]); }
    ));

    render(<UserChatPage />);

    await waitFor(() => expect(api.keepaliveRuntime).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("button", { name: "会话 A" })).not.toBeInTheDocument();
    await act(async () => { releaseModels?.(); });
    expect(await screen.findByRole("button", { name: "会话 A" })).toBeVisible();
  });

  it("warms up a stale ready runtime before loading workspace data", async () => {
    let releaseModels: (() => void) | undefined;
    let finishWarmup: (() => void) | undefined;
    api.runtimeModels.mockImplementationOnce(() => new Promise<[]>(
      (resolve) => { releaseModels = () => resolve([]); }
    ));
    api.keepaliveRuntime.mockRejectedValueOnce(new Error('{"detail":"runtime is not ready"}'));
    api.warmupRuntime.mockImplementationOnce(() => new Promise(
      (resolve) => { finishWarmup = () => resolve({ runtimeWorkspaceId: 1, runtimeStatus: "ready" }); }
    ));
    render(<UserChatPage />);

    await waitFor(() => expect(api.warmupRuntime).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("正在唤醒")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "刷新运行状态" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "会话 A" })).not.toBeInTheDocument();
    await act(async () => { releaseModels?.(); });
    await act(async () => { finishWarmup?.(); });
    await screen.findByText("A 文件.txt");
    expect(screen.getByLabelText("消息输入")).toBeVisible();
    expect(api.keepaliveRuntime).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(api.runtimeModels).toHaveBeenCalled());
    expect(api.runtimeSessions).toHaveBeenCalledWith(1);
    expect(screen.queryByText('{"detail":"runtime is not ready"}')).not.toBeInTheDocument();
  });

  it("retries session prewarm after recovering a stale ready runtime", async () => {
    let finishWarmup: (() => void) | undefined;
    api.keepaliveRuntime.mockRejectedValueOnce(new Error("runtime is stale"));
    api.warmupRuntime.mockImplementationOnce(() => new Promise(
      (resolve) => {
        finishWarmup = () => resolve({
          runtimeWorkspaceId: 1,
          runtimeStatus: "ready"
        });
      }
    ));

    render(<UserChatPage />);

    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.warmupRuntime).toHaveBeenCalledTimes(1));
    await act(async () => { finishWarmup?.(); });

    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledTimes(2));
    expect(api.prewarmRuntimeSession).toHaveBeenLastCalledWith(1, "rs-1-a");
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
  });

  it("does not wait for stale session prewarm before warming the recovered runtime", async () => {
    let finishWarmup: (() => void) | undefined;
    let finishStalePrewarm: (() => void) | undefined;
    api.keepaliveRuntime.mockRejectedValueOnce(new Error("runtime is stale"));
    api.warmupRuntime.mockImplementationOnce(() => new Promise(
      (resolve) => {
        finishWarmup = () => resolve({
          runtimeWorkspaceId: 1,
          runtimeStatus: "ready"
        });
      }
    ));
    api.prewarmRuntimeSession
      .mockImplementationOnce(() => new Promise(
        (resolve) => { finishStalePrewarm = () => resolve(undefined); }
      ))
      .mockResolvedValueOnce(undefined);

    render(<UserChatPage />);

    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.warmupRuntime).toHaveBeenCalledTimes(1));
    await act(async () => { finishWarmup?.(); });

    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    await act(async () => { finishStalePrewarm?.(); });
  });

  it("does not repeat the completed initial session prewarm on ready effect", async () => {
    render(<UserChatPage />);

    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    await waitFor(() => expect(api.chatHistory).toHaveBeenCalledWith(1, "rs-1-a"));

    expect(api.prewarmRuntimeSession.mock.calls.filter(
      ([workspaceId, selectedSessionId]) => (
        workspaceId === 1 && selectedSessionId === "rs-1-a"
      )
    )).toHaveLength(1);
  });

  it("shows a connection error when runtime recovery fails", async () => {
    api.keepaliveRuntime.mockRejectedValueOnce(new Error("runtime is asleep"));
    api.warmupRuntime.mockRejectedValueOnce(new Error("runtime warmup failed"));

    render(<UserChatPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("runtime warmup failed");
    expect(screen.queryByText("连接异常")).not.toBeInTheDocument();
  });

  it("does not expose a runtime refresh control in conversation chrome", async () => {
    api.keepaliveRuntime.mockImplementationOnce(() => new Promise<void>(() => undefined));

    render(<UserChatPage />);

    await screen.findByRole("button", { name: "会话 A" });
    expect(screen.queryByRole("button", { name: "刷新运行状态" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    expect(api.keepaliveRuntime).toHaveBeenCalledTimes(1);
  });

  it("keeps the conversation header free of the runtime status capsule", async () => {
    const { container } = render(<UserChatPage />);

    await screen.findByRole("button", { name: "会话 A" });
    const { header, scope } = taskHeader();
    expect(header).toHaveAttribute("data-view", "chat");
    expect(scope.getByText("会话 A")).toBeVisible();
    expect(scope.queryByText("当前任务：会话 A")).not.toBeInTheDocument();
    expect(screen.queryByText("默认数字人 · 当前会话")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "任务" })).toBeVisible();
    expect(screen.getByText("最近任务")).toBeVisible();
    const identity = container.querySelector(".appHeader > .headerIdentity");
    expect(identity).not.toBeNull();
    expect(Array.from(identity?.children ?? []).map((element) => element.className)).toEqual(["chatTitle"]);
    expect(identity?.querySelector(".runtimePill")).not.toBeInTheDocument();
    expect(container.querySelector(".runtimePill")).not.toBeInTheDocument();
    expect(screen.queryByText("已连接")).not.toBeInTheDocument();
  });

  it("reopens the files drawer from the screen-edge handle after closing it", async () => {
    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");

    const user = userEvent.setup();
    expect(screen.getByRole("complementary", { name: "当前任务文件" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "关闭当前任务文件" }));

    expect(screen.queryByRole("complementary", { name: "当前任务文件" })).not.toBeInTheDocument();
    const reopen = screen.getByRole("button", { name: "打开当前任务文件" });
    expect(reopen).toBeVisible();
    expect(reopen).toHaveAttribute("title", "打开当前任务文件");
    expect(reopen).toHaveTextContent("1");

    await user.click(reopen);
    expect(screen.getByRole("complementary", { name: "当前任务文件" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "打开当前任务文件" })).not.toBeInTheDocument();
  });

  it("presents a legacy default conversation title as a new task", async () => {
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-legacy", title: "新会话", messageCount: 0, sourceType: "web" }
    ]);

    render(<UserChatPage />);

    expect(await taskHeader().scope.findByText("新任务")).toBeVisible();
    expect(screen.getByRole("button", { name: "新任务" })).toBeVisible();
    expect(taskHeader().scope.queryByText("新会话")).not.toBeInTheDocument();
  });

  it("keeps platform session history visible while the runtime is not ready", async () => {
    api.bootstrapRuntime.mockResolvedValue({ runtimeWorkspaceId: 1, runtimeStatus: "starting" });
    api.chatHistory.mockResolvedValue({
      messages: [
        { role: "user", content: "之前的问题" },
        { role: "assistant", content: "之前的答案" }
      ]
    });

    const { container } = render(<UserChatPage />);

    expect(await screen.findByRole("button", { name: "会话 A" })).toBeVisible();
    expect(screen.queryByText("正在为你分配云资源，稍等，马上就好……")).not.toBeInTheDocument();
    expect(await screen.findByText("之前的问题")).toBeVisible();
    expect(screen.getByText("之前的答案")).toBeVisible();
    expect(container.querySelector(".messageFeedInner")).toHaveAttribute("data-has-messages", "true");
    expect(api.runtimeSessions).toHaveBeenCalledWith(1);
    expect(api.chatHistory).toHaveBeenCalledWith(1, "rs-1-a");
    expect(api.prewarmRuntimeSession).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "消息输入" })).toBeEnabled();
  });

  it("auto-creates the first task before runtime bootstrap is ready", async () => {
    api.bootstrapRuntime.mockResolvedValue({ runtimeWorkspaceId: 1, runtimeStatus: "starting" });
    api.runtimeSessions.mockReset().mockResolvedValue([]);
    api.runtimeModels.mockResolvedValue([
      { modelEntryId: 19, displayName: "Model A", modelKey: "model-a", providerId: 1, providerDisplayName: "Provider A", isDefault: true }
    ]);
    api.createRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-1-first",
      title: "新任务",
      messageCount: 0,
      sourceType: "web",
      sessionModelEntryId: 19,
      status: "active"
    });

    render(<UserChatPage />);

    await waitFor(() => expect(api.createRuntimeSession).toHaveBeenCalledWith(1, {
      title: "新任务",
      modelEntryId: 19,
      sourceType: "web"
    }));
    expect(screen.queryByText("正在为你分配云资源，稍等，马上就好……")).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "新任务" })).toBeVisible();
    expect(api.prewarmRuntimeSession).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "消息输入" })).toBeEnabled();
  });

  it("retries the first task while the default runtime profile is preparing", async () => {
    api.runtimeSessions.mockReset().mockResolvedValue([]);
    api.createRuntimeSession.mockReset();
    api.createRuntimeSession
      .mockRejectedValueOnce(Object.assign(
        new Error("云资源准备中，请稍后重试"),
        { code: "runtime_agent_profile_preparing", status: 503 }
      ))
      .mockResolvedValueOnce({
        runtimeSessionId: "rs-1-first",
        title: "新任务",
        messageCount: 0,
        sourceType: "web",
        status: "active"
      });

    render(<UserChatPage />);

    await waitFor(() => expect(api.createRuntimeSession).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("正在为你分配云资源，稍等，马上就好……")).not.toBeInTheDocument();

    await waitFor(
      () => expect(api.createRuntimeSession).toHaveBeenCalledTimes(2),
      { timeout: 5_000 }
    );
    expect(screen.queryByText("正在为你分配云资源，稍等，马上就好……")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新任务" })).toBeVisible();
  });

  it("switches the file rail to the selected session files", async () => {
    render(<UserChatPage />);

    await screen.findByText("A 文件.txt");
    await userEvent.setup().click(screen.getByRole("button", { name: "会话 B" }));

    await screen.findByText("B 文件.txt");
    await waitFor(() => expect(screen.queryByText("A 文件.txt")).not.toBeInTheDocument());
    expect(api.chatFiles).toHaveBeenCalledWith(1, "rs-1-b");
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-b"));
  });

  it("notifies backend when a previously visited session is selected again", async () => {
    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-a"));
    const initialACalls = api.prewarmRuntimeSession.mock.calls.filter(
      ([workspaceId, selectedSessionId]) => (
        workspaceId === 1 && selectedSessionId === "rs-1-a"
      )
    ).length;

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-b"));
    await user.click(screen.getByRole("button", { name: "会话 A" }));

    await waitFor(() => {
      const currentACalls = api.prewarmRuntimeSession.mock.calls.filter(
        ([workspaceId, selectedSessionId]) => (
          workspaceId === 1 && selectedSessionId === "rs-1-a"
        )
      ).length;
      expect(currentACalls).toBeGreaterThan(initialACalls);
    });
  });

  it("opens an existing task from a non-task view", async () => {
    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "专家-技能" }));
    await user.click(await screen.findByRole("tab", { name: "技能" }));
    expect(await screen.findByRole("region", { name: "技能广场" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "会话 B" }));

    expect(await taskHeader().scope.findByText("会话 B")).toBeVisible();
    expect(screen.queryByRole("region", { name: "技能广场" })).not.toBeInTheDocument();
  });

  it("opens a newly created task from a non-task view", async () => {
    api.createRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-1-new",
      title: "新任务",
      messageCount: 0,
      sourceType: "web"
    });
    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "专家-技能" }));
    await user.click(await screen.findByRole("tab", { name: "技能" }));
    expect(await screen.findByRole("region", { name: "技能广场" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "新建任务" }));

    expect(await taskHeader().scope.findByText("新任务")).toBeVisible();
    expect(screen.queryByRole("region", { name: "技能广场" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("textbox", { name: "消息输入" })).toHaveFocus());
  });

  it("starts explicit-session prewarm before selecting the created session", async () => {
    let finishPrewarm: (() => void) | undefined;
    let newSessionWasVisibleWhenPrewarmStarted: boolean | undefined;
    api.createRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-1-new",
      title: "新任务",
      messageCount: 0,
      sourceType: "web"
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    api.prewarmRuntimeSession.mockImplementation(
      async (_workspaceId: number, selectedSessionId: string) => {
        if (selectedSessionId !== "rs-1-new") return undefined;
        newSessionWasVisibleWhenPrewarmStarted = Boolean(
          screen.queryByRole("button", { name: "新任务" })
        );
        await new Promise<void>((resolve) => { finishPrewarm = resolve; });
        return undefined;
      }
    );
    api.prewarmRuntimeSession.mockClear();

    await userEvent.setup().click(screen.getByRole("button", { name: "新建任务" }));

    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-new"));
    expect(newSessionWasVisibleWhenPrewarmStarted).toBe(false);
    expect(await screen.findByRole("button", { name: "新任务" })).toBeVisible();
    expect(api.prewarmRuntimeSession.mock.calls.filter(
      ([workspaceId, selectedSessionId]) => workspaceId === 1 && selectedSessionId === "rs-1-new"
    )).toHaveLength(1);

    await act(async () => { finishPrewarm?.(); });
  });

  it("does not prewarm an explicitly created session while runtime is not ready", async () => {
    api.bootstrapRuntime.mockResolvedValue({
      runtimeWorkspaceId: 1,
      runtimeStatus: "starting"
    });
    api.createRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-1-cold-new",
      title: "新任务",
      messageCount: 0,
      sourceType: "web"
    });

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    api.prewarmRuntimeSession.mockClear();

    await userEvent.setup().click(screen.getByRole("button", { name: "新建任务" }));

    await waitFor(() => expect(api.createRuntimeSession).toHaveBeenCalled());
    expect(await screen.findByRole("button", { name: "新任务" })).toBeVisible();
    expect(api.prewarmRuntimeSession).not.toHaveBeenCalled();
  });

  it("does not repeat a completed explicit-session prewarm in ready effect", async () => {
    api.createRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-1-fast-new",
      title: "新任务",
      messageCount: 0,
      sourceType: "web"
    });

    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    api.prewarmRuntimeSession.mockClear();

    await userEvent.setup().click(screen.getByRole("button", { name: "新建任务" }));

    await waitFor(() => expect(api.chatHistory).toHaveBeenCalledWith(
      1,
      "rs-1-fast-new"
    ));
    expect(api.prewarmRuntimeSession.mock.calls.filter(
      ([workspaceId, selectedSessionId]) => (
        workspaceId === 1 && selectedSessionId === "rs-1-fast-new"
      )
    )).toHaveLength(1);
  });

  it("renews the selected session agent every minute", async () => {
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "会话 A" });
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalled());
    const callsBeforeRenewal = api.prewarmRuntimeSession.mock.calls.length;
    const renewal = setIntervalSpy.mock.calls.find(
      ([, delay]) => delay === 60_000
    )?.[0] as (() => void) | undefined;
    expect(renewal).toBeTypeOf("function");

    await act(async () => { renewal?.(); });

    await waitFor(() => {
      expect(api.prewarmRuntimeSession).toHaveBeenCalledTimes(callsBeforeRenewal + 1);
    });
    expect(api.prewarmRuntimeSession).toHaveBeenLastCalledWith(1, "rs-1-a");
    setIntervalSpy.mockRestore();
  });

  it("keeps an active grant connected when one background renewal fails", async () => {
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    render(<UserChatPage />);

    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    await waitFor(() => {
      expect(
        setIntervalSpy.mock.calls.some(([, delay]) => delay === 60_000)
      ).toBe(true);
    });
    const renewal = setIntervalSpy.mock.calls.find(
      ([, delay]) => delay === 60_000
    )?.[0] as (() => void) | undefined;
    expect(renewal).toBeTypeOf("function");
    api.prewarmRuntimeSession.mockRejectedValueOnce(new Error("channel lost"));

    await act(async () => { renewal?.(); });

    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    setIntervalSpy.mockRestore();
  });

  it("updates the sidebar and active header after renaming a session", async () => {
    api.renameRuntimeSession.mockResolvedValue({
      sessionId: 1,
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-1-a",
      title: "发布计划",
      messageCount: 1,
      sourceType: "web",
      status: "active"
    });
    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "任务操作：会话 A" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名任务：会话 A" }));
    const input = screen.getByRole("textbox", { name: "重命名任务：会话 A" });
    await user.clear(input);
    await user.type(input, "发布计划{Enter}");

    await waitFor(() => expect(api.renameRuntimeSession).toHaveBeenCalledWith(1, "rs-1-a", { title: "发布计划" }));
    expect(await screen.findByRole("button", { name: "发布计划" })).toBeVisible();
    expect(taskHeader().scope.getByText("发布计划")).toBeVisible();
  });

  it("deletes a session through the inline sidebar confirmation without window.confirm", async () => {
    api.deleteRuntimeSession.mockResolvedValue(undefined);
    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "任务操作：会话 A" }));
    await user.click(screen.getByRole("menuitem", { name: "删除任务：会话 A" }));

    expect(api.deleteRuntimeSession).not.toHaveBeenCalled();
    expect(window.confirm).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(api.deleteRuntimeSession).toHaveBeenCalledWith(1, "rs-1-a"));
    expect(screen.queryByRole("button", { name: "会话 A" })).not.toBeInTheDocument();
    expect(await taskHeader().scope.findByText("会话 B")).toBeVisible();
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("opens text files in the in-app preview without a browser popup", async () => {
    api.downloadChatFile.mockResolvedValue(new Blob(["preview contents"], { type: "text/plain" }));
    render(<UserChatPage />);

    await screen.findByText("A 文件.txt");
    await userEvent.setup().click(screen.getByRole("button", { name: "预览 A 文件.txt" }));

    expect(await screen.findByRole("dialog", { name: "A 文件.txt" })).toBeVisible();
    expect(await screen.findByText("preview contents")).toBeVisible();
    expect(api.downloadChatFile).toHaveBeenCalledWith(1);
  });

  it("opens Office files through a read-only ONLYOFFICE preview session", async () => {
    api.chatFiles.mockResolvedValue([{ fileId: 7, displayName: "季度报告.xlsx", relativePath: "workspace/季度报告.xlsx", sourceType: "upload", sizeBytes: 128 }]);
    api.officePreviewSession.mockResolvedValue({
      documentServerUrl: "https://office.example.test",
      config: { documentType: "cell", editorConfig: { mode: "view" } }
    });
    render(<UserChatPage />);

    await screen.findByText("季度报告.xlsx");
    await userEvent.setup().click(screen.getByRole("button", { name: "预览 季度报告.xlsx" }));

    expect(await screen.findByRole("dialog", { name: "季度报告.xlsx" })).toBeVisible();
    await waitFor(() => expect(api.officePreviewSession).toHaveBeenCalledWith(7));
    expect(api.downloadChatFile).not.toHaveBeenCalled();
  });

  it("stages a selected file locally and uploads it only when the message is sent", async () => {
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-upload" } });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-upload" } });
    });
    const { container } = render(<UserChatPage />);
    await screen.findByText("A 文件.txt");

    const input = container.querySelector<HTMLInputElement>('section.chatLayout > input[type="file"]');
    const dropTarget = container.querySelector<HTMLElement>("section.chatLayout");
    expect(input).not.toBeNull();
    const attachment = new File(["quarterly data"], "季度数据.csv", { type: "text/csv" });
    const user = userEvent.setup();
    await act(async () => {
      fireEvent.drop(dropTarget!, {
        bubbles: true,
        cancelable: true,
        dataTransfer: {
          types: ["Files"],
          items: [{ kind: "file", getAsFile: () => attachment }]
        }
      });
    });

    const composerQueue = await screen.findByLabelText("本次消息附件");
    expect(within(composerQueue).getByText("季度数据.csv")).toBeVisible();
    expect(within(composerQueue).getByText("待发送")).toBeVisible();
    expect(screen.queryByRole("region", { name: "本次待发送" })).not.toBeInTheDocument();
    expect(api.uploadChatAttachment).not.toHaveBeenCalled();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "分析这个文件");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(api.uploadChatAttachment).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.streamChatRun).toHaveBeenCalledWith(
      expect.objectContaining({ attachmentFileIds: [9], message: "分析这个文件" }),
      expect.any(Function),
      expect.any(AbortSignal)
    ));
  });

  it("persists model selection on the current session and restores it when switching sessions", async () => {
    api.runtimeModels.mockResolvedValue([
      { modelEntryId: 1, displayName: "Model A", modelKey: "model-a", providerId: 1, providerDisplayName: "Provider A", isDefault: false },
      { modelEntryId: 2, displayName: "Model B", modelKey: "model-b", providerId: 1, providerDisplayName: "Provider A", isDefault: false }
    ]);
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-a", title: "会话 A", messageCount: 1, sourceType: "web", sessionModelEntryId: 1 },
      { runtimeSessionId: "rs-1-b", title: "会话 B", messageCount: 1, sourceType: "web", sessionModelEntryId: 2 }
    ]);
    api.setSessionModel.mockResolvedValue({ runtimeWorkspaceId: 1, runtimeSessionId: "rs-1-a", modelEntryId: 2, appliesTo: "next_run", activeRunCount: 0 });

    render(<UserChatPage />);

    const modelTrigger = await screen.findByRole("button", { name: "模型" });
    expect(modelTrigger).toHaveTextContent("Model A");
    const user = userEvent.setup();
    await user.click(modelTrigger);
    await user.click(screen.getByRole("option", { name: "Model B" }));
    await waitFor(() => expect(api.setSessionModel).toHaveBeenCalledWith(1, "rs-1-a", 2));
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenLastCalledWith(1, "rs-1-a"));
    expect(api.prewarmRuntimeSession.mock.calls.length).toBeGreaterThanOrEqual(2);

    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await waitFor(() => expect(modelTrigger).toHaveTextContent("Model B"));
  });

  it("keeps a successful model update after switching away before the PUT completes", async () => {
    api.runtimeModels.mockResolvedValue([
      { modelEntryId: 1, displayName: "Model A", modelKey: "model-a", providerId: 1, providerDisplayName: "Provider A", isDefault: false },
      { modelEntryId: 2, displayName: "Model B", modelKey: "model-b", providerId: 1, providerDisplayName: "Provider A", isDefault: false }
    ]);
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-a", title: "会话 A", messageCount: 1, sourceType: "web", sessionModelEntryId: 1 },
      { runtimeSessionId: "rs-1-b", title: "会话 B", messageCount: 1, sourceType: "web", sessionModelEntryId: 1 }
    ]);
    let finishModelUpdate!: (result: {
      runtimeWorkspaceId: number;
      runtimeSessionId: string;
      modelEntryId: number;
      appliesTo: string;
      activeRunCount: number;
    }) => void;
    api.setSessionModel.mockImplementation(() => new Promise(
      (resolve) => { finishModelUpdate = resolve; }
    ));

    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    const modelTrigger = screen.getByRole("button", { name: "模型" });
    const user = userEvent.setup();
    await user.click(modelTrigger);
    await user.click(screen.getByRole("option", { name: "Model B" }));
    await waitFor(() => expect(api.setSessionModel).toHaveBeenCalledWith(1, "rs-1-a", 2));

    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await act(async () => {
      finishModelUpdate({
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-a",
        modelEntryId: 2,
        appliesTo: "next_run",
        activeRunCount: 0
      });
    });
    await user.click(screen.getByRole("button", { name: "会话 A" }));

    await waitFor(() => expect(modelTrigger).toHaveTextContent("Model B"));
  });

  it("does not release a queued new-model turn when maintenance prewarm finishes first", async () => {
    api.runtimeModels.mockResolvedValue([
      { modelEntryId: 1, displayName: "Model A", modelKey: "model-a", providerId: 1, providerDisplayName: "Provider A", isDefault: false },
      { modelEntryId: 2, displayName: "Model B", modelKey: "model-b", providerId: 1, providerDisplayName: "Provider A", isDefault: false }
    ]);
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-a", title: "会话 A", messageCount: 1, sourceType: "web", sessionModelEntryId: 1 }
    ]);
    let finishModelUpdate!: (result: {
      runtimeWorkspaceId: number;
      runtimeSessionId: string;
      modelEntryId: number;
      appliesTo: string;
      activeRunCount: number;
    }) => void;
    api.setSessionModel.mockImplementation(() => new Promise(
      (resolve) => { finishModelUpdate = resolve; }
    ));
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-model-gated" } });
      onEvent({ event: "chat.message.completed", data: { runtimeRunId: "rr-model-gated", seq: 1, text: "新模型回答" } });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-model-gated", seq: 2 } });
    });

    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    api.prewarmRuntimeSession.mockClear();
    let prewarmCalls = 0;
    let finishRequiredPrewarm!: (state: "ready") => void;
    api.prewarmRuntimeSession.mockImplementation(() => {
      prewarmCalls += 1;
      if (prewarmCalls === 1) return Promise.resolve("ready" as const);
      if (prewarmCalls === 2) {
        return new Promise<"ready">((resolve) => {
          finishRequiredPrewarm = resolve;
        });
      }
      return Promise.resolve("ready" as const);
    });

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "模型" }));
    await user.click(screen.getByRole("option", { name: "Model B" }));
    await waitFor(() => expect(api.setSessionModel).toHaveBeenCalledWith(1, "rs-1-a", 2));
    expect(screen.getByLabelText("消息输入")).toBeVisible();

    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText("消息输入")).toBeVisible();
    expect(screen.queryByText("正在检测运行环境...")).not.toBeInTheDocument();

    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "使用新模型");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(screen.getByText("使用新模型")).toBeVisible();
    expect(api.streamChatRun).not.toHaveBeenCalled();

    await act(async () => {
      finishModelUpdate({
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-a",
        modelEntryId: 2,
        appliesTo: "next_run",
        activeRunCount: 0
      });
    });
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("消息输入")).toBeVisible();
    expect(api.streamChatRun).not.toHaveBeenCalled();

    await act(async () => { finishRequiredPrewarm("ready"); });
    await waitFor(() => expect(api.streamChatRun).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("新模型回答")).toBeVisible();
    expect(screen.getByLabelText("消息输入")).toBeVisible();
  }, 10_000);

  it("fails a queued model-switch turn instead of rerouting it to the restored model", async () => {
    api.runtimeModels.mockResolvedValue([
      { modelEntryId: 1, displayName: "Model A", modelKey: "model-a", providerId: 1, providerDisplayName: "Provider A", isDefault: false },
      { modelEntryId: 2, displayName: "Model B", modelKey: "model-b", providerId: 1, providerDisplayName: "Provider A", isDefault: false }
    ]);
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-a", title: "会话 A", messageCount: 1, sourceType: "web", sessionModelEntryId: 1 }
    ]);
    let rejectModelUpdate!: (reason: Error) => void;
    api.setSessionModel.mockImplementation(() => new Promise(
      (_resolve, reject) => { rejectModelUpdate = reject; }
    ));

    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    api.prewarmRuntimeSession.mockClear();
    const user = userEvent.setup();
    const modelTrigger = screen.getByRole("button", { name: "模型" });
    await user.click(modelTrigger);
    await user.click(screen.getByRole("option", { name: "Model B" }));
    await waitFor(() => expect(api.setSessionModel).toHaveBeenCalledWith(1, "rs-1-a", 2));

    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "不能改投旧模型");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(screen.getByText("不能改投旧模型")).toBeVisible();
    expect(api.streamChatRun).not.toHaveBeenCalled();

    await act(async () => { rejectModelUpdate(new Error("model update failed")); });
    expect((await screen.findAllByText("model update failed")).length).toBeGreaterThan(0);
    await waitFor(() => expect(modelTrigger).toHaveTextContent("Model A"));
    expect(api.streamChatRun).not.toHaveBeenCalled();
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-a"));
  });

  it("preserves the created task model for its first run", async () => {
    api.runtimeModels.mockResolvedValue([
      { modelEntryId: 1, displayName: "Model A", modelKey: "model-a", providerId: 1, providerDisplayName: "Provider A", isDefault: false },
      { modelEntryId: 19, displayName: "Model B", modelKey: "model-b", providerId: 1, providerDisplayName: "Provider A", isDefault: true }
    ]);
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-a", title: "任务 A", messageCount: 1, sourceType: "web", sessionModelEntryId: 19 }
    ]);
    api.createRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-1-new",
      title: "新任务",
      messageCount: 0,
      sourceType: "web",
      sessionModelEntryId: 19
    });
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-new-task" } });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-new-task" } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "新建任务" }));
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "hello啊");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(api.streamChatRun).toHaveBeenCalledWith(
      expect.objectContaining({ runtimeSessionId: "rs-1-new", modelEntryId: 19, message: "hello啊" }),
      expect.any(Function),
      expect.any(AbortSignal)
    ));
  });

  it("switches pages without blocking on asset changes", async () => {
    render(<UserChatPage />);

    await screen.findByText("A 文件.txt");
    await userEvent.setup().click(screen.getByRole("button", { name: "专家-技能" }));

    expect(await screen.findByRole("region", { name: "专家" })).toBeVisible();
    expect(screen.queryByRole("dialog", { name: "有未保存的资产变更" })).not.toBeInTheDocument();
  });

  it("creates a context expert branch and switches back to its source session", async () => {
    let resolveExpertAuthority!: (value: { messages: Array<{ role: string; content: string }> }) => void;
    const historyFor = async (workspaceId: number, sessionId: string) => ({
      runtimeWorkspaceId: workspaceId,
      runtimeSessionId: sessionId,
      messages: sessionId === "rs-1-a"
        ? [{ role: "user", content: "原始上下文" }]
        : sessionId === "rs-1-expert"
          ? [{ role: "assistant", content: "专家分支上下文" }]
          : [{ role: "assistant", content: "个人延续上下文" }]
    });
    api.chatHistory.mockImplementation(historyFor);
    api.chatHistoryPreview.mockImplementation(async (_workspaceId: number, sessionId: string) => (
      sessionId === "rs-1-expert"
        ? new Promise(() => undefined)
        : historyFor(_workspaceId, sessionId)
    ));
    api.chatHistoryAuthoritative.mockImplementation(async (_workspaceId: number, sessionId: string) => (
      sessionId === "rs-1-expert"
        ? new Promise((resolve) => { resolveExpertAuthority = resolve; })
        : historyFor(_workspaceId, sessionId)
    ));
    const expert = {
      runtimeAgentProfileId: 7,
      runtimeWorkspaceId: 1,
      hermesProfileName: "expert-7",
      profileKind: "expert",
      displayName: "写作专家",
      description: "公众号文章写作",
      soulContent: "# 写作专家",
      ownershipType: "self_created",
      updateAvailable: false,
      capabilities: { skillIds: [], mcpIds: [], knowledgeBaseIds: [], enterpriseKnowledgeEnabled: false, pluginIds: [] },
      shareScope: { organization: false, userIds: [], departmentIds: [] },
      shareScopeUsers: [],
      status: "active",
      allowedActions: {
        viewDetail: true,
        install: false,
        uninstall: false,
        summon: true,
        edit: true,
        copy: true,
        publish: true,
        unlist: false,
        delete: true
      }
    };
    api.runnableExperts.mockResolvedValue([expert]);
    api.createRuntimeSession
      .mockResolvedValueOnce({
        runtimeSessionId: "rs-1-expert",
        runtimeAgentProfileId: 7,
        contextSourceRuntimeSessionId: "rs-1-a",
        title: "会话 A",
        messageCount: 0,
        sourceType: "web",
        agentDisplayName: "写作专家",
        agentProfileKind: "expert"
      })
      .mockResolvedValueOnce({
        runtimeSessionId: "rs-1-default-next",
        runtimeAgentProfileId: 1,
        contextSourceRuntimeSessionId: "rs-1-expert",
        title: "新任务",
        messageCount: 0,
        sourceType: "web",
        agentDisplayName: "自己",
        agentProfileKind: "default"
      });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "当前对话对象：个人" }));
    const picker = screen.getByRole("menu", { name: "可选对象" });
    await user.click(within(picker).getByRole("menuitemradio", { name: /写作专家/ }));

    await waitFor(() => expect(api.createRuntimeSession).toHaveBeenCalledWith(
      1,
      expect.objectContaining({
        title: "会话 A",
        sourceType: "web",
        runtimeAgentProfileId: 7,
        contextSourceRuntimeSessionId: "rs-1-a"
      })
    ));
    expect(screen.getByText("原始上下文")).toBeVisible();
    expect(screen.queryByText("专家分支上下文")).not.toBeInTheDocument();
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-expert"));
    expect(taskHeader().scope.getByText("写作专家")).toBeVisible();

    await act(async () => {
      resolveExpertAuthority({
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-expert",
        messages: [{ role: "assistant", content: "专家分支上下文" }]
      });
    });
    expect(await screen.findByText("专家分支上下文")).toBeVisible();
    await waitFor(() => expect(screen.queryByText("原始上下文")).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "当前对话对象：写作专家" }));
    await user.click(within(screen.getByRole("menu", { name: "可选对象" }))
      .getByRole("menuitemradio", { name: "个人" }));

    await waitFor(() => expect(api.createRuntimeSession).toHaveBeenNthCalledWith(
      2,
      1,
      expect.objectContaining({
        title: "会话 A",
        sourceType: "web",
        runtimeAgentProfileId: null,
        contextSourceRuntimeSessionId: "rs-1-expert"
      })
    ));
    await waitFor(() => expect(api.prewarmRuntimeSession)
      .toHaveBeenCalledWith(1, "rs-1-default-next"));
    expect(await screen.findByText("B 文件.txt")).toBeVisible();
    expect(screen.getByRole("button", { name: "当前对话对象：个人" })).toBeVisible();
  });


  it("shows the WorkBuddy-style welcome view for an empty session history", async () => {
    setSession({
      accessToken: "test-access-token",
      tokenType: "Bearer",
      principal: {
        orgId: 1,
        userId: 10002,
        name: "示例用户乙",
        userCode: "10002"
      }
    });
    const { container, rerender } = render(<UserChatPage />);

    expect(await screen.findByRole("heading", { name: "示例用户乙-10002，您好 👋 我是您的数字分身" })).toBeVisible();
    clearSession();
    rerender(<UserChatPage />);
    expect(await screen.findByRole("heading", { name: "您好 👋 我是您的数字分身" })).toBeVisible();
    expect(await screen.findByText("快速生成经营数据、项目进度和业务指标看板。")).toBeVisible();
    expect(screen.getByRole("tab", { name: "看板" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "办公" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "数据看板" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: "任务示例 XXXX" })).toBeVisible();
    expect(screen.queryByText("今天想完成什么？")).not.toBeInTheDocument();
    expect(screen.queryByText("分析文档")).not.toBeInTheDocument();
    expect(screen.queryByText("创建专家包")).not.toBeInTheDocument();
    expect(container.querySelector(".welcomeMark")).not.toBeInTheDocument();
    expect(container.querySelector(".messageFeedInner")).toHaveAttribute("data-has-messages", "false");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("previews and commits welcome tasks without sending them", async () => {
    render(<UserChatPage />);

    const task = await screen.findByRole("button", { name: "任务示例 YYYYYY" });
    const input = screen.getByLabelText("消息输入");
    fireEvent.mouseEnter(task);
    expect(input).toHaveValue("YYYYYY 的详细任务描述");
    expect(input).toHaveAttribute("data-input-tone", "preview");
    expect(screen.getByLabelText("发送消息")).toBeDisabled();

    fireEvent.mouseLeave(task);
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("data-input-tone", "normal");

    fireEvent.click(task);
    expect(input).toHaveValue("YYYYYY 的详细任务描述");
    expect(input).toHaveAttribute("data-input-tone", "normal");
    expect(input).toHaveFocus();
    expect(api.streamChatRun).not.toHaveBeenCalled();
  });

  it("freezes the header, conversation, composer, and right file rail regions", async () => {
    const { container } = render(<UserChatPage />);

    await screen.findByText("A 文件.txt");
    const mainArea = container.querySelector(".mainArea");
    const chatLayout = container.querySelector(".chatLayout");
    const conversation = container.querySelector(".conversation");

    expect(Array.from(mainArea?.children ?? []).map((node) => node.className)).toEqual(["appHeader", "chatLayout"]);
    expect(Array.from(chatLayout?.children ?? []).filter((node) => node.tagName !== "INPUT").map((node) => node.className)).toEqual(["conversation", "filesPane"]);
    // 2026-11-03-014 主轴重构：Composer 包进 .composerFooter > .main-axis（输入区 flex 固定沉底），
    // 免责声明回到输入区内部；消息列与输入区共用同一 .main-axis 宽度权威，状态切换不发生宽度跳变。
    expect(Array.from(conversation?.children ?? []).map((node) => node.className)).toEqual(["messageFeedStage", "composerFooter"]);
    expect(conversation?.querySelector(":scope > .messageFeedStage > .messageFeed")).not.toBeNull();
    expect(conversation?.querySelector(":scope > .messageFeedStage > .messageFeed > .main-axis")).not.toBeNull();
    expect(conversation?.querySelector(":scope > .composerFooter > .main-axis > .composerCard")).not.toBeNull();
    expect(conversation?.querySelector(":scope > .composerFooter > .main-axis > .composerDisclaimer")).not.toBeNull();
  });

  it("reconnects a fresh active session from the first unapplied ledger event", async () => {
    api.activeChatRun.mockResolvedValue({ run: { runtimeRunId: "rr-1-a", status: "running", lastSeq: 4 } });
    api.reconnectChatRun.mockResolvedValue(undefined);

    render(<UserChatPage />);

    await waitFor(() => expect(api.reconnectChatRun).toHaveBeenCalledWith(
      {
        runtimeRunId: "rr-1-a",
        afterSeq: 0,
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-a"
      },
      expect.any(Function),
      expect.any(AbortSignal)
    ));
  });

  it("opens one observer when the initial session has an active run", async () => {
    api.activeChatRun.mockResolvedValue({ run: { runtimeRunId: "rr-1-a", status: "running", lastSeq: 4 } });
    api.reconnectChatRun.mockResolvedValue(undefined);

    render(<UserChatPage />);

    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 25)); });
    expect(api.reconnectChatRun).toHaveBeenCalledTimes(1);
  });

  it("rebuilds an active run after hard reload from the history checkpoint and ledger", async () => {
    api.chatHistory.mockResolvedValue({
      messages: [
        { role: "user", content: "old question" },
        { role: "assistant", content: "old answer" },
        { role: "user", content: "current question" },
        { role: "assistant", content: "provider partial" }
      ]
    });
    api.activeChatRun.mockResolvedValue({
      run: {
        runtimeRunId: "rr-1-a",
        status: "running",
        lastSeq: 4,
        historyMessageCountBeforeRun: 2
      }
    });
    api.reconnectChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.run.started", data: { seq: 1, user_message: "current question" } });
      onEvent({ event: "chat.message.delta", data: { seq: 2, delta: "recovered answer" } });
    });

    render(<UserChatPage />);

    expect(await screen.findByText("old question")).toBeVisible();
    expect(screen.getByText("old answer")).toBeVisible();
    expect(await screen.findByText("current question")).toBeVisible();
    expect(await screen.findByText("recovered answer")).toBeVisible();
    expect(screen.queryByText("provider partial")).not.toBeInTheDocument();
    expect(api.reconnectChatRun).toHaveBeenCalledWith(
      {
        runtimeRunId: "rr-1-a",
        afterSeq: 0,
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-a"
      },
      expect.any(Function),
      expect.any(AbortSignal)
    );
  });

  it("does not truncate history when the active-run projection is terminal", async () => {
    api.chatHistory.mockResolvedValue({
      messages: [
        { role: "user", content: "old question" },
        { role: "assistant", content: "old answer" },
        { role: "user", content: "current question" },
        { role: "assistant", content: "complete answer" }
      ]
    });
    api.activeChatRun.mockResolvedValue({
      run: {
        runtimeRunId: "rr-1-a",
        status: "completed",
        lastSeq: 4,
        historyMessageCountBeforeRun: 2
      }
    });

    render(<UserChatPage />);

    expect(await screen.findByText("old question")).toBeVisible();
    expect(screen.getByText("old answer")).toBeVisible();
    expect(screen.getAllByText("current question").length).toBeGreaterThan(0);
    expect(screen.getByText("complete answer")).toBeVisible();
    expect(api.reconnectChatRun).not.toHaveBeenCalled();
  });

  it("streams the main agent conclusion when a background expert collaboration creates its final run", async () => {
    api.backgroundCollaborations.mockResolvedValue({
      pendingCount: 1,
      collaborations: [{
        collaborationId: "ec-1",
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-a",
        originRuntimeRunId: "rr-origin",
        finalRuntimeRunId: null,
        state: "waiting_tasks",
        revision: 1,
        taskCount: 2,
        updatedAt: "2026-07-26T10:00:00Z"
      }]
    });
    api.streamBackgroundCollaborations.mockImplementation(async (
      _workspaceId: number,
      _sessionId: string,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({
        event: "chat.background.snapshot",
        data: {
          pendingCount: 1,
          collaborations: [{
            collaborationId: "ec-1",
            state: "waiting_tasks",
            finalRuntimeRunId: null
          }]
        }
      });
      onEvent({
        event: "chat.background.run.created",
        data: {
          collaborationId: "ec-1",
          runtimeSessionId: "rs-1-a",
          finalRuntimeRunId: "rr-final",
          state: "synthesizing",
          revision: 2
        }
      });
    });
    api.reconnectChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({
        event: "chat.message.completed",
        data: {
          runtimeRunId: "rr-final",
          seq: 1,
          text: "综合两位专家的结果，建议优先进入华东市场。"
        }
      });
      onEvent({
        event: "chat.done",
        data: { runtimeRunId: "rr-final", seq: 2, status: "completed" }
      });
    });

    render(<UserChatPage />);

    await waitFor(() => expect(api.backgroundCollaborations).toHaveBeenCalled());
    await waitFor(() => expect(api.streamBackgroundCollaborations).toHaveBeenCalled());
    await waitFor(() => expect(api.reconnectChatRun).toHaveBeenCalled());
    expect(await screen.findByText("综合两位专家的结果，建议优先进入华东市场。")).toBeVisible();
    expect(api.reconnectChatRun).toHaveBeenCalledWith(
      {
        runtimeRunId: "rr-final",
        afterSeq: 0,
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-a"
      },
      expect.any(Function),
      expect.any(AbortSignal)
    );
  });

  it("surfaces a blocked background expert collaboration from the initial snapshot", async () => {
    api.backgroundCollaborations.mockResolvedValue({
      pendingCount: 1,
      collaborations: [{
        collaborationId: "ec-blocked",
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-a",
        originRuntimeRunId: "rr-origin-blocked",
        finalRuntimeRunId: null,
        state: "attention_required",
        revision: 3,
        taskCount: 1,
        updatedAt: "2026-07-27T00:34:06Z"
      }]
    });
    api.streamBackgroundCollaborations.mockImplementation(async (
      _workspaceId: number,
      _sessionId: string,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void,
      signal: AbortSignal
    ) => {
      onEvent({
        event: "chat.background.snapshot",
        data: {
          pendingCount: 1,
          collaborations: [{
            collaborationId: "ec-blocked",
            state: "attention_required",
            finalRuntimeRunId: null
          }]
        }
      });
      await new Promise<void>((resolve) => {
        signal.addEventListener("abort", () => resolve(), { once: true });
      });
    });

    const { unmount } = render(<UserChatPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "专家任务执行失败或需要处理，请重新派发任务，或查看 Kanban 阻塞原因"
    );
    expect(api.reconnectChatRun).not.toHaveBeenCalled();
    unmount();
  });

  it("continues a detached run in its original assistant bubble", async () => {
    let sessionARunActive = false;
    let emit: ((event: { event: string; data: Record<string, unknown> }) => void) | undefined;
    api.activeChatRun.mockImplementation(async (_workspaceId: number, sessionId: string) => ({
      run: sessionId === "rs-1-a" && sessionARunActive
        ? { runtimeRunId: "rr-1-a", status: "running", lastSeq: 2 }
        : null
    }));
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void, signal: AbortSignal) => {
      emit = onEvent;
      sessionARunActive = true;
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-1-a" } });
      onEvent({ event: "chat.message.delta", data: { seq: 1, delta: "partial" } });
      await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
    });
    api.reconnectChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void, signal: AbortSignal) => {
      onEvent({ event: "chat.message.delta", data: { seq: 2, delta: " answer" } });
      await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "continue me");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByText("partial");
    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await screen.findByText("B 文件.txt");
    await act(async () => {
      emit?.({ event: "chat.message.delta", data: { seq: 2, runtimeRunId: "rr-1-a", delta: " answer" } });
    });
    await user.click(screen.getByRole("button", { name: "会话 A" }));

    await screen.findByText("partial answer");
    expect(document.querySelectorAll('article[data-role="assistant"]')).toHaveLength(1);
  });

  it("keeps a submitted turn when final history is still empty", async () => {
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-empty-history" } });
      onEvent({ event: "chat.message.delta", data: { seq: 1, runtimeRunId: "rr-empty-history", delta: "你好，有什么可以帮你？" } });
      onEvent({ event: "chat.done", data: { seq: 2, runtimeRunId: "rr-empty-history" } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const historyReadsBeforeSend = api.chatHistory.mock.calls.length;
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "你好");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("你好")).toBeVisible();
    expect(await screen.findByText("你好，有什么可以帮你？")).toBeVisible();
    await waitFor(() => expect(api.chatHistory.mock.calls.length)
      .toBeGreaterThan(historyReadsBeforeSend));
    expect(screen.getByText("你好")).toBeVisible();
    expect(screen.getByText("你好，有什么可以帮你？")).toBeVisible();
  });

  it("refreshes and opens the file rail as soon as an artifact event arrives", async () => {
    localStorage.setItem("hermes.files-pane", "false");
    const objectUrl = "blob:runtime-generated-image";
    URL.createObjectURL = vi.fn(() => objectUrl);
    URL.revokeObjectURL = vi.fn();
    api.downloadChatFile.mockResolvedValue(new Blob(["image"], { type: "image/jpeg" }));
    let fileReads = 0;
    api.chatFiles.mockImplementation(async () => {
      fileReads += 1;
      return fileReads >= 2 ? [
        {
          fileId: 22,
          displayName: "货币原理.pptx",
          relativePath: "workspace/sessions/rs-1-a/货币原理.pptx",
          sourceType: "tool",
          sizeBytes: 309_000
        },
        {
          fileId: 23,
          runtimeWorkspaceId: 1,
          runtimeSessionId: "rs-1-a",
          runtimeRunId: "rr-artifact",
          relativePath: "workspace/sessions/rs-1-a/dogs/dog.jpg",
          displayName: "dog.jpg",
          mimeType: "image/jpeg",
          sizeBytes: 2048,
          sourceType: "runtime"
        }
      ] : [];
    });
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-artifact" } });
      onEvent({ event: "chat.artifact.created", data: { runtimeRunId: "rr-artifact", fileId: 22 } });
      onEvent({ event: "chat.message.completed", data: { runtimeRunId: "rr-artifact", text: "PPT 已生成。" } });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-artifact" } });
    });

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "生成货币原理 PPT");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("货币原理.pptx")).toBeVisible();
    expect(await screen.findByAltText("dog.jpg")).toBeInTheDocument();
    expect(api.downloadChatFile).toHaveBeenCalledWith(23);
    expect(screen.getByRole("complementary", { name: "当前任务文件" })).toBeVisible();
    expect(api.chatFiles).toHaveBeenCalledWith(1, "rs-1-a");
  });

  it("uses the first successful message as a new task title", async () => {
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-new", title: "新任务", messageCount: 0, sourceType: "web" }
    ]);
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-first-title" } });
      onEvent({ event: "chat.message.delta", data: { seq: 1, runtimeRunId: "rr-first-title", delta: "好的" } });
      onEvent({ event: "chat.done", data: { seq: 2, runtimeRunId: "rr-first-title" } });
    });
    api.renameRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-1-new",
      title: "帮我 整理 发布计划",
      messageCount: 1,
      sourceType: "web"
    });

    render(<UserChatPage />);
    await taskHeader().scope.findByText("新任务");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "  帮我\n\n整理   发布计划  ");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(api.renameRuntimeSession).toHaveBeenCalledWith(
      1,
      "rs-1-new",
      { title: "帮我 整理 发布计划" }
    ));
    expect(await taskHeader().scope.findByText("帮我 整理 发布计划")).toBeVisible();
  });

  it("retries the next numbered title after a conflict", async () => {
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-new", title: "新任务", messageCount: 0, sourceType: "web" },
      { runtimeSessionId: "rs-1-existing", title: "你好", messageCount: 1, sourceType: "web" },
      { runtimeSessionId: "rs-1-racing", title: "其他任务", messageCount: 1, sourceType: "web" }
    ]);
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-duplicate-title" } });
      onEvent({ event: "chat.done", data: { seq: 1, runtimeRunId: "rr-duplicate-title" } });
    });
    api.renameRuntimeSession
      .mockRejectedValueOnce(Object.assign(new Error("session title is already in use"), { status: 409 }))
      .mockResolvedValueOnce({
        runtimeSessionId: "rs-1-new",
        title: "你好（3）",
        messageCount: 1,
        sourceType: "web"
      });

    render(<UserChatPage />);
    await taskHeader().scope.findByText("新任务");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "你好");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() => expect(api.renameRuntimeSession).toHaveBeenNthCalledWith(
      1,
      1,
      "rs-1-new",
      { title: "你好（2）" }
    ));
    await waitFor(() => expect(api.renameRuntimeSession).toHaveBeenNthCalledWith(
      2,
      1,
      "rs-1-new",
      { title: "你好（3）" }
    ));
    expect(await taskHeader().scope.findByText("你好（3）")).toBeVisible();
  });

  it("does not overwrite a manual rename completed while the first turn streams", async () => {
    let finishStream: (() => void) | undefined;
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-new", title: "新任务", messageCount: 0, sourceType: "web" }
    ]);
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-manual-title" } });
      await new Promise<void>((resolve) => { finishStream = resolve; });
      onEvent({ event: "chat.done", data: { seq: 1, runtimeRunId: "rr-manual-title" } });
    });
    api.renameRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-1-new",
      title: "手动标题",
      messageCount: 1,
      sourceType: "web"
    });

    render(<UserChatPage />);
    await taskHeader().scope.findByText("新任务");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "首次输入");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(api.streamChatRun).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("button", { name: "任务操作：新任务" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名任务：新任务" }));
    const renameInput = screen.getByRole("textbox", { name: "重命名任务：新任务" });
    await user.clear(renameInput);
    await user.type(renameInput, "手动标题{Enter}");
    expect(await taskHeader().scope.findByText("手动标题")).toBeVisible();

    await act(async () => { finishStream?.(); });
    await waitFor(() => expect(screen.getByRole("button", { name: "发送消息" })).toBeVisible());
    expect(api.renameRuntimeSession).toHaveBeenCalledTimes(1);
    expect(api.renameRuntimeSession).toHaveBeenCalledWith(1, "rs-1-new", { title: "手动标题" });
  });

  it("does not auto-title an existing task or a failed first turn", async () => {
    api.streamChatRun.mockRejectedValue(new Error("runtime unavailable"));

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "不要覆盖标题");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("runtime unavailable")).toBeVisible();
    expect(api.renameRuntimeSession).not.toHaveBeenCalled();
  });

  it("keeps the default title when the first turn fails", async () => {
    api.runtimeSessions.mockResolvedValue([
      { runtimeSessionId: "rs-1-new", title: "新任务", messageCount: 0, sourceType: "web" }
    ]);
    api.streamChatRun.mockRejectedValue(new Error("runtime unavailable"));

    render(<UserChatPage />);
    await taskHeader().scope.findByText("新任务");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "首次输入");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("runtime unavailable")).toBeVisible();
    expect(api.renameRuntimeSession).not.toHaveBeenCalled();
    expect(taskHeader().scope.getByText("新任务")).toBeVisible();
  });

  it("keeps the user turn and renders an inline failure when stream submission rejects", async () => {
    api.streamChatRun.mockRejectedValue(new Error("runtime unavailable"));

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const prewarmCallsBeforeSend = (
      api.prewarmRuntimeSession.mock.calls.length
    );
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "你好");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("runtime unavailable")).toBeVisible();
    expect(screen.getAllByText("你好")).toHaveLength(2);
    expect(screen.getByRole("textbox", { name: "消息输入" })).toHaveValue("你好");
    expect(screen.getByText("runtime unavailable").closest("article")).toHaveAttribute("data-status", "failed");
    await waitFor(() => {
      expect(
        api.prewarmRuntimeSession.mock.calls.length
      ).toBeGreaterThan(prewarmCallsBeforeSend);
    });
  });

  it("reconnects an accepted run when the initial Runtime Edge stream drops", async () => {
    let runAccepted = false;
    api.activeChatRun.mockImplementation(async () => ({
      run: runAccepted ? { runtimeRunId: "rr-reconnect-after-drop", status: "running", lastSeq: 0 } : null
    }));
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      runAccepted = true;
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-reconnect-after-drop" } });
      throw new Error("network error");
    });
    api.reconnectChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.message.delta", data: { seq: 1, runtimeRunId: "rr-reconnect-after-drop", delta: "任务仍在继续，已恢复连接。" } });
      onEvent({ event: "chat.done", data: { seq: 2, runtimeRunId: "rr-reconnect-after-drop" } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "生成 PPT");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("任务仍在继续，已恢复连接。")).toBeVisible();
    expect(api.reconnectChatRun).toHaveBeenCalledWith(
      {
        runtimeRunId: "rr-reconnect-after-drop",
        afterSeq: 0,
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-a"
      },
      expect.any(Function),
      expect.any(AbortSignal)
    );
    expect(screen.queryByText("network error")).not.toBeInTheDocument();
  });

  it("reconnects when an accepted initial stream reaches EOF before chat.done", async () => {
    api.activeChatRun.mockResolvedValue({ run: null });
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-eof" } });
      onEvent({
        event: "chat.reasoning.delta",
        data: { runtimeRunId: "rr-eof", seq: 4, delta: "正在生成" }
      });
    });
    api.reconnectChatRun.mockImplementation(async (
      payload: { runtimeRunId: string; afterSeq: number },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      expect(payload).toEqual({
        runtimeRunId: "rr-eof",
        afterSeq: 4,
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-a"
      });
      onEvent({
        event: "chat.message.completed",
        data: { runtimeRunId: "rr-eof", seq: 5, text: "生成完成" }
      });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-eof", seq: 6 } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "生成 PPT");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("生成完成")).toBeVisible();
    expect(api.reconnectChatRun).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("network error")).not.toBeInTheDocument();
  });

  it("replays a completed accepted run when active run lookup is empty", async () => {
    api.activeChatRun.mockResolvedValue({ run: null });
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-completed" } });
      throw new Error("network error");
    });
    api.reconnectChatRun.mockImplementation(async (
      payload: { runtimeRunId: string; afterSeq: number },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      expect(payload).toEqual({
        runtimeRunId: "rr-completed",
        afterSeq: 0,
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-a"
      });
      onEvent({
        event: "chat.message.completed",
        data: { runtimeRunId: "rr-completed", seq: 1, text: "已从账本恢复" }
      });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-completed", seq: 2 } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "生成 PPT");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("已从账本恢复")).toBeVisible();
    expect(api.reconnectChatRun).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("network error")).not.toBeInTheDocument();
  });

  it("keeps chat.done successful if the transport throws afterward", async () => {
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-done" } });
      onEvent({
        event: "chat.message.completed",
        data: { runtimeRunId: "rr-done", seq: 1, text: "已经完成" }
      });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-done", seq: 2 } });
      throw new Error("network error");
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "生成 PPT");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("已经完成")).toBeVisible();
    expect(api.reconnectChatRun).not.toHaveBeenCalled();
    expect(screen.queryByText("network error")).not.toBeInTheDocument();
  });

  it("shows a transport recovery notice when replay also disconnects", async () => {
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-double-drop" } });
      onEvent({
        event: "chat.reasoning.delta",
        data: { runtimeRunId: "rr-double-drop", seq: 1, delta: "任务仍在执行" }
      });
      throw new Error("network error");
    });
    api.reconnectChatRun.mockRejectedValue(new Error("network error"));

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "生成 PPT");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("任务仍在执行")).toBeVisible();
    expect(await screen.findByText("连接暂时中断，任务仍在后台执行，请稍后刷新查看结果。")).toBeVisible();
    expect(screen.queryByText("network error")).not.toBeInTheDocument();
  });

  it("ignores backend asset status events before the real run is accepted", async () => {
    let continueStream: (() => void) | undefined;
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.asset.projecting", data: { generation: 2, status: "projecting" } });
      onEvent({ event: "chat.asset.reloading", data: { generation: 2, status: "applying" } });
      await new Promise<void>((resolve) => { continueStream = resolve; });
      onEvent({ event: "chat.asset.applied", data: { generation: 2, status: "applied" } });
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-assets" } });
      onEvent({ event: "chat.message.delta", data: { seq: 1, runtimeRunId: "rr-assets", delta: "资源已生效" } });
      onEvent({ event: "chat.done", data: { seq: 2, runtimeRunId: "rr-assets" } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "用新资源回答");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(screen.queryByText("正在安装资源：资产配置处理中…")).not.toBeInTheDocument();
    expect(screen.queryByText("正在重载资源…")).not.toBeInTheDocument();
    expect(screen.queryByText("资源已重载，正在开始处理消息…")).not.toBeInTheDocument();
    await act(async () => { continueStream?.(); });
    expect(await screen.findByText("资源已生效")).toBeVisible();
  });

  it("does not fail the submitted turn for backend asset failure events", async () => {
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.asset.reloading", data: { generation: 2, status: "applying" } });
      onEvent({ event: "chat.asset.failed", data: { generation: 2, status: "failed", message: "dashboard reload unavailable" } });
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-assets" } });
      onEvent({ event: "chat.message.delta", data: { seq: 1, runtimeRunId: "rr-assets", delta: "回答继续" } });
      onEvent({ event: "chat.done", data: { seq: 2, runtimeRunId: "rr-assets" } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "用新资源回答");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("回答继续")).toBeVisible();
    expect(screen.queryByText("dashboard reload unavailable")).not.toBeInTheDocument();
    expect(screen.getByText("回答继续").closest("article")).not.toHaveAttribute("data-status", "failed");
  });

  it("replays a detached terminal run before accepting stale history", async () => {
    api.activeChatRun.mockResolvedValue({ run: null });
    let emit: ((event: { event: string; data: Record<string, unknown> }) => void) | undefined;
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void, signal: AbortSignal) => {
      emit = onEvent;
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-detached-terminal" } });
      onEvent({ event: "chat.message.delta", data: { seq: 1, runtimeRunId: "rr-detached-terminal", delta: "partial" } });
      await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
    });
    api.reconnectChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.message.delta", data: { seq: 2, runtimeRunId: "rr-detached-terminal", delta: " answer" } });
      onEvent({ event: "chat.done", data: { seq: 3, runtimeRunId: "rr-detached-terminal" } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "continue me");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByText("partial");
    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await screen.findByText("B 文件.txt");
    await act(async () => {
      emit?.({ event: "chat.message.delta", data: { seq: 2, runtimeRunId: "rr-detached-terminal", delta: " answer" } });
      emit?.({ event: "chat.done", data: { seq: 3, runtimeRunId: "rr-detached-terminal" } });
    });
    await user.click(screen.getByRole("button", { name: "会话 A" }));

    expect(await screen.findByText("partial answer")).toBeVisible();
    expect(api.reconnectChatRun).not.toHaveBeenCalled();
    expect(screen.getByText("continue me")).toBeVisible();
  });

  it("sends in a new session while the previous session run remains detached", async () => {
    api.createRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-1-c",
      title: "新会话",
      messageCount: 0,
      sourceType: "web"
    });
    api.streamChatRun.mockImplementation(async (payload: { runtimeSessionId: string }, onEvent: (event: { event: string; data: Record<string, unknown> }) => void, signal: AbortSignal) => {
      if (payload.runtimeSessionId === "rs-1-a") {
        onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-session-a" } });
        onEvent({ event: "chat.message.delta", data: { seq: 1, runtimeRunId: "rr-session-a", delta: "working" } });
        await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
        return;
      }
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-session-c" } });
      onEvent({ event: "chat.message.delta", data: { seq: 1, runtimeRunId: "rr-session-c", delta: "new session answer" } });
      onEvent({ event: "chat.done", data: { seq: 2, runtimeRunId: "rr-session-c" } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "long task");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByText("working");
    await user.click(screen.getByRole("button", { name: "新建任务" }));
    await waitFor(() => expect(api.createRuntimeSession).toHaveBeenCalledWith(1, expect.objectContaining({ title: "新任务" })));
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "new task");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("new session answer")).toBeVisible();
    expect(api.streamChatRun).toHaveBeenLastCalledWith(
      expect.objectContaining({ runtimeSessionId: "rs-1-c", message: "new task" }),
      expect.any(Function),
      expect.any(AbortSignal)
    );
    expect(api.stopChatRun).not.toHaveBeenCalled();
  });

  it("refreshes Hermes history after a detached run reaches its terminal state", async () => {
    let sessionARunActive = false;
    let sessionACompleted = false;
    let emit: ((event: { event: string; data: Record<string, unknown> }) => void) | undefined;
    let finishStream: (() => void) | undefined;
    api.chatHistory.mockImplementation(async (_workspaceId: number, sessionId: string) => ({
      messages: sessionId === "rs-1-a" && sessionACompleted
        ? [
            { role: "user", content: "finish away" },
            { role: "assistant", content: "complete answer" }
          ]
        : []
    }));
    api.activeChatRun.mockImplementation(async (_workspaceId: number, sessionId: string) => ({
      run: sessionId === "rs-1-a" && sessionARunActive
        ? { runtimeRunId: "rr-1-a", status: "running", lastSeq: 1 }
        : null
    }));
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void, signal: AbortSignal) => {
      emit = onEvent;
      sessionARunActive = true;
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-1-a" } });
      onEvent({ event: "chat.message.delta", data: { seq: 1, delta: "partial" } });
      await new Promise<void>((resolve) => {
        finishStream = resolve;
        signal.addEventListener("abort", () => resolve(), { once: true });
      });
    });
    api.reconnectChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.done", data: { seq: 2, runtimeRunId: "rr-1-a" } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "finish away");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByText("partial");
    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await screen.findByText("B 文件.txt");
    sessionARunActive = false;
    sessionACompleted = true;
    await act(async () => {
      emit?.({ event: "chat.done", data: { seq: 2, runtimeRunId: "rr-1-a" } });
      finishStream?.();
    });
    await user.click(screen.getByRole("button", { name: "会话 A" }));

    expect(await screen.findByText("complete answer")).toBeVisible();
    expect(screen.queryByText("partial")).not.toBeInTheDocument();
  });

  it("scrolls to the newest message when switching sessions", async () => {
    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const feed = document.querySelector<HTMLElement>(".messageFeed");
    expect(feed).not.toBeNull();
    Object.defineProperties(feed!, {
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 600 },
      scrollTop: { configurable: true, writable: true, value: 0 }
    });

    await userEvent.setup().click(screen.getByRole("button", { name: "会话 B" }));

    await waitFor(() => expect(feed!.scrollTop).toBe(600));
  });

  it("scrolls to the newest message when reopening the current session", async () => {
    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const feed = document.querySelector<HTMLElement>(".messageFeed");
    expect(feed).not.toBeNull();
    Object.defineProperties(feed!, {
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 600 },
      scrollTop: { configurable: true, writable: true, value: 0 }
    });

    await userEvent.setup().click(screen.getByRole("button", { name: "会话 A" }));

    await waitFor(() => expect(feed!.scrollTop).toBe(600));
  });

  it("does not force scrolling after the user reads older messages", async () => {
    let emit: ((event: { event: string; data: Record<string, unknown> }) => void) | undefined;
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void, signal: AbortSignal) => {
      emit = onEvent;
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-1-a" } });
      onEvent({ event: "chat.message.delta", data: { seq: 1, delta: "first" } });
      await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const feed = document.querySelector<HTMLElement>(".messageFeed");
    expect(feed).not.toBeNull();
    Object.defineProperties(feed!, {
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 600 },
      scrollTop: { configurable: true, writable: true, value: 600 }
    });
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "keep reading");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await screen.findByText("first");
    expect(document.querySelector(".turnAnchorSpacer")).toHaveStyle({ height: "100px" });
    feed!.scrollTop = 0;
    fireEvent.scroll(feed!);
    await act(async () => { emit?.({ event: "chat.message.delta", data: { seq: 2, delta: " second" } }); });

    expect(await screen.findByText("first second")).toBeVisible();
    expect(feed!.scrollTop).toBe(0);
  });

  it("keeps the same session observer active while switching away and back", async () => {
    const observers: Array<{ signal: AbortSignal; finish: () => void }> = [];
    api.activeChatRun.mockImplementation(async (_workspaceId: number, sessionId: string) => ({
      run: sessionId === "rs-1-a" ? { runtimeRunId: "rr-1-a", status: "running", lastSeq: 0 } : null
    }));
    api.reconnectChatRun.mockImplementation(async (_payload: unknown, _onEvent: unknown, signal: AbortSignal) => new Promise<void>((resolve) => {
      observers.push({ signal, finish: resolve });
    }));

    render(<UserChatPage />);
    await waitFor(() => expect(observers).toHaveLength(1));
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "会话 B" }));
    await screen.findByText("B 文件.txt");
    await user.click(screen.getByRole("button", { name: "会话 A" }));
    expect(observers).toHaveLength(1);
    expect(observers[0].signal.aborted).toBe(false);

    expect(screen.getByRole("button", { name: "停止生成" })).toBeVisible();
    await act(async () => { observers[0].finish(); });
  });

  it("quietly settles an explicitly stopped run and refreshes its history", async () => {
    let emit: ((event: { event: string; data: Record<string, unknown> }) => void) | undefined;
    let streamSignal: AbortSignal | undefined;
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: typeof emit,
      signal: AbortSignal
    ) => {
      emit = onEvent;
      streamSignal = signal;
      onEvent?.({ event: "chat.run.accepted", data: { runtimeRunId: "rr-stop" } });
      await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
    });
    api.stopChatRun.mockResolvedValueOnce({
      runtimeRunId: "rr-stop",
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-1-a",
      status: "canceling"
    });

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "stop me");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await user.click(await screen.findByRole("button", { name: "停止生成" }));

    await waitFor(() => expect(api.stopChatRun).toHaveBeenCalledWith({
      runtimeRunId: "rr-stop",
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-1-a"
    }));
    expect(streamSignal?.aborted).toBe(false);
    expect(screen.getByRole("button", { name: "停止生成" })).toBeDisabled();
    await act(async () => {
      emit?.({
        event: "chat.done",
        data: { runtimeRunId: "rr-stop", status: "canceled" }
      });
    });
    await waitFor(() => expect(streamSignal?.aborted).toBe(true));
    await waitFor(() => expect(api.chatHistory).toHaveBeenCalledWith(1, "rs-1-a"));
    expect(screen.queryByText(/Operation interrupted: waiting for model response/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送消息" })).toBeVisible();
  });

  it("restores the stop button and shows an explicit error when cancel is unconfirmed", async () => {
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void,
      signal: AbortSignal
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-stop-failed" } });
      await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
    });
    api.stopChatRun.mockRejectedValueOnce(
      new RuntimeEdgeTransportError(
        "停止请求未送达，任务仍在执行，请重试",
        "RUNTIME_CANCEL_UNCONFIRMED"
      )
    );

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "cannot stop yet");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await user.click(await screen.findByRole("button", { name: "停止生成" }));

    expect(await screen.findByText("停止请求未送达，任务仍在执行，请重试")).toBeVisible();
    expect(screen.getByRole("button", { name: "停止生成" })).toBeEnabled();
  });

  it("queues one stop request until chat.run.accepted provides the run id", async () => {
    let emit: ((event: { event: string; data: Record<string, unknown> }) => void) | undefined;
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: typeof emit, signal: AbortSignal) => {
      emit = onEvent;
      await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
    });
    api.stopChatRun.mockResolvedValueOnce({
      runtimeRunId: "rr-late-accept",
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-1-a",
      status: "canceling"
    });

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "stop before accepted");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await user.click(await screen.findByRole("button", { name: "停止生成" }));
    expect(api.stopChatRun).not.toHaveBeenCalled();

    await act(async () => {
      emit?.({ event: "chat.run.accepted", data: { runtimeRunId: "rr-late-accept" } });
    });
    await waitFor(() => expect(api.stopChatRun).toHaveBeenCalledTimes(1));

    await act(async () => {
      emit?.({ event: "chat.done", data: { runtimeRunId: "rr-late-accept", status: "canceled" } });
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "发送消息" })).toBeVisible());
  });

  it("shows reasoning SSE live and keeps it separate from the visible answer", async () => {
    let emit: ((event: { event: string; data: Record<string, unknown> }) => void) | undefined;
    api.streamChatRun.mockImplementation(async (_payload: unknown, onEvent: typeof emit, signal: AbortSignal) => {
      emit = onEvent;
      onEvent?.({ event: "chat.run.accepted", data: { runtimeRunId: "rr-reasoning" } });
      onEvent?.({ event: "chat.reasoning.delta", data: { seq: 1, delta: "分析问题" } });
      await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "请回答");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("思考中…")).toBeVisible();
    expect(screen.getByText(/^TTFT \d+\.\ds$/)).toBeVisible();
    expect(screen.getByText("分析问题")).toBeVisible();
    await user.click(screen.getByText("思考中…"));
    expect(screen.getByText("分析问题")).not.toBeVisible();
    await user.click(screen.getByText("思考中…"));
    expect(screen.getByText("分析问题")).toBeVisible();

    await act(async () => emit?.({ event: "chat.message.delta", data: { seq: 2, delta: "最终答案" } }));
    expect(await screen.findByText("最终答案")).toBeVisible();
    expect(screen.getByText("思考过程")).toBeVisible();
  });

  it("keeps reasoning after chat.done refreshes caught-up history without it", async () => {
    let completed = false;
    api.chatHistory.mockImplementation(async () => completed
      ? {
          messages: [
            { role: "user", content: "请回答" },
            { role: "assistant", content: "最终答案" }
          ]
        }
      : { messages: [] });
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-reasoning-complete" } });
      onEvent({ event: "chat.reasoning.delta", data: { seq: 1, delta: "分析问题" } });
      onEvent({ event: "chat.message.delta", data: { seq: 2, delta: "最终答案" } });
      completed = true;
      onEvent({ event: "chat.done", data: { seq: 3 } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const historyCallsBeforeSend = api.chatHistory.mock.calls.length;
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "请回答");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("最终答案")).toBeVisible();
    await waitFor(() => expect(api.chatHistory.mock.calls.length).toBeGreaterThan(historyCallsBeforeSend));
    const completedSummary = await screen.findByText("思考过程");
    const completedDetails = completedSummary.closest("details");
    if (!completedDetails?.open) await user.click(completedSummary);
    expect(screen.getByText("分析问题")).toBeVisible();
    expect(screen.getByText(/^TTFT \d+\.\ds$/)).toBeVisible();
  });

  it("captures TTFT on the first visible event that can be projected to the accepted run", async () => {
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.tool.event", data: { seq: 1, name: "orphan_tool" } });
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-projectable" } });
      onEvent({ event: "chat.message.delta", data: { seq: 1, delta: "visible answer" } });
      onEvent({ event: "chat.done", data: { seq: 2 } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "测试 TTFT");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    const answer = await screen.findByText("visible answer");
    const article = answer.closest('article[data-role="assistant"]');
    expect(article?.querySelector(".ttftBadge")).toHaveTextContent(/^TTFT \d+\.\ds$/);
    expect(screen.queryByText(/orphan_tool/)).not.toBeInTheDocument();
  });

  it("shows one thinking bubble immediately and keeps the prepared target hot", async () => {
    let continueSubmit: (() => void) | undefined;
    let continueStream: (() => void) | undefined;
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      await new Promise<void>((resolve) => { continueSubmit = resolve; });
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-lifecycle" } });
      onEvent({ event: "chat.run.started", data: { runtimeRunId: "rr-lifecycle", seq: 1 } });
      await new Promise<void>((resolve) => { continueStream = resolve; });
      onEvent({ event: "chat.message.delta", data: { runtimeRunId: "rr-lifecycle", seq: 2, delta: "真实回答" } });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-lifecycle", seq: 3 } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const prewarmCallsBeforeSend = api.prewarmRuntimeSession.mock.calls.length;
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "测试生命周期");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("正在思考...")).toBeVisible();
    expect(screen.getByRole("status")).toHaveAttribute("data-thinking-phase", "submitting");
    await act(async () => { continueSubmit?.(); });
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveAttribute("data-thinking-phase", "started");
    });
    expect(screen.getAllByText("正在思考...")).toHaveLength(1);
    await act(async () => { continueStream?.(); });
    expect(await screen.findByText("真实回答")).toBeVisible();
    expect(screen.queryByText("正在思考...")).not.toBeInTheDocument();
    expect(api.prewarmRuntimeSession.mock.calls.length).toBe(prewarmCallsBeforeSend);
  });

  it("still records content paint when run-start is replaced before its first frame", async () => {
    const frames: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      frames.push(callback);
      return frames.length;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue(
      { length: 1 } as DOMRectList
    );
    let finishStream: (() => void) | undefined;
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-fast-content" } });
      onEvent({ event: "chat.run.started", data: { runtimeRunId: "rr-fast-content", seq: 1 } });
      onEvent({ event: "chat.message.completed", data: { runtimeRunId: "rr-fast-content", seq: 2, text: "立即回答" } });
      await new Promise<void>((resolve) => { finishStream = resolve; });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-fast-content", seq: 3 } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "测试连续首帧");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("立即回答")).toBeVisible();
    await waitFor(() => expect(frames.length).toBeGreaterThanOrEqual(2));
    while (frames.length > 0) {
      act(() => frames.shift()?.(performance.now()));
    }
    await waitFor(() => expect(api.reportChatClientTiming).toHaveBeenCalledWith(
      "rr-fast-content",
      expect.objectContaining({ sampleKind: "content" })
    ));
    await act(async () => { finishStream?.(); });
  });

  it("reports completed-only TTFT only after the second paint frame", async () => {
    const frames: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      frames.push(callback);
      return frames.length;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue({ length: 1 } as DOMRectList);
    let finishStream: (() => void) | undefined;
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-painted" } });
      onEvent({ event: "chat.progress.delta", data: { runtimeRunId: "rr-painted", seq: 1, delta: "working" } });
      onEvent({ event: "chat.message.completed", data: { runtimeRunId: "rr-painted", seq: 2, text: "painted answer" } });
      await new Promise<void>((resolve) => { finishStream = resolve; });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-painted", seq: 3 } });
    });

    render(<UserChatPage />);
    await screen.findByText("A 文件.txt");
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "测试真实绘制");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("painted answer")).toBeVisible();
    await waitFor(() => expect(frames).toHaveLength(1));
    expect(api.reportChatClientTiming).not.toHaveBeenCalled();
    act(() => frames.shift()?.(performance.now()));
    await waitFor(() => expect(frames).toHaveLength(1));
    expect(api.reportChatClientTiming).not.toHaveBeenCalled();
    act(() => frames.shift()?.(performance.now()));

    await waitFor(() => expect(api.reportChatClientTiming).toHaveBeenCalledTimes(1));
    expect(api.reportChatClientTiming).toHaveBeenCalledWith(
      "rr-painted",
      expect.objectContaining({
        documentVisibility: "visible",
        reconnectBeforeFirstVisible: false,
        sendOverlappedPrewarm: false,
        requestToFirstVisiblePaintMs: expect.any(Number)
      })
    );
    await act(async () => { finishStream?.(); });
  });

  it("reports when sending overlaps an inflight session prewarm", async () => {
    const frames: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      frames.push(callback);
      return frames.length;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue(
      { length: 1 } as DOMRectList
    );
    let finishPrewarm: ((state: "ready") => void) | undefined;
    let finishStream: (() => void) | undefined;
    api.streamChatRun.mockImplementation(async (
      _payload: unknown,
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "rr-prewarm-overlap" } });
      onEvent({
        event: "chat.message.completed",
        data: { runtimeRunId: "rr-prewarm-overlap", seq: 1, text: "overlap answer" }
      });
      await new Promise<void>((resolve) => { finishStream = resolve; });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-prewarm-overlap", seq: 2 } });
    });

    render(<UserChatPage />);
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toBeVisible());
    api.prewarmRuntimeSession.mockClear();
    api.prewarmRuntimeSession.mockImplementationOnce(
      () => new Promise<"ready">((resolve) => { finishPrewarm = resolve; })
    ).mockResolvedValue("ready");
    act(() => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-1-a"));
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "消息输入" }), "测试预热重叠");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("overlap answer")).toBeVisible();
    await waitFor(() => expect(frames).toHaveLength(1));
    act(() => frames.shift()?.(performance.now()));
    await waitFor(() => expect(frames).toHaveLength(1));
    act(() => frames.shift()?.(performance.now()));

    await waitFor(() => expect(api.reportChatClientTiming).toHaveBeenCalledWith(
      "rr-prewarm-overlap",
      expect.objectContaining({
        sampleKind: "content",
        sendOverlappedPrewarm: true,
        requiredPrewarmWaitMs: 0
      })
    ));
    await act(async () => { finishPrewarm?.("ready"); });
    await act(async () => { finishStream?.(); });
  });

  it("restores reasoning from Hermes session history", async () => {
    api.chatHistory.mockResolvedValue({
      messages: [{ role: "assistant", reasoning_content: "历史思考", content: "历史答案" }]
    });

    render(<UserChatPage />);

    expect(await screen.findByText("思考过程")).toBeVisible();
    await userEvent.setup().click(screen.getByText("思考过程"));
    expect(screen.getByText("历史思考")).toBeVisible();
    expect(screen.getByText("历史答案")).toBeVisible();
  });

  it("opens the trigger target immediately, reconnects its active scheduled run, and keeps the source projection", async () => {
    api.listScheduledTasks.mockResolvedValue([{
      taskId: 9, taskName: "每日总结", prompt: "总结今天", targetRuntimeSessionId: "rs-1-b", targetSessionTitle: "会话 B", targetSessionStatus: "active",
      scheduleConfig: { type: "cron", preset: "daily", time: "09:00" }, scheduleSummary: "每天 09:00", status: "active", nextRunAt: null, lastRunAt: "2026-07-18T00:50:00Z",
      lastRunStatus: null, lastErrorCode: null, lastError: null, lastRuntimeRunId: null, runCount: 0, isRunning: false
    }]);
    api.triggerScheduledTask.mockResolvedValue({ taskId: 9, targetRuntimeSessionId: "rs-1-b", acceptedAt: "2026-07-18T00:50:00Z" });
    api.chatHistory.mockImplementation(async (_workspaceId: number, targetSessionId: string) => ({
      messages: targetSessionId === "rs-1-b" ? [{
        role: "user", content: "总结今天",
        source: { type: "scheduled", scheduledTaskId: 9, scheduledTaskName: "每日总结", triggerKind: "manual", taskAvailable: true }
      }] : []
    }));
    let resolveScheduledRun: ((value: unknown) => void) | undefined;
    const scheduledRun = new Promise((resolve) => { resolveScheduledRun = resolve; });
    api.activeChatRun.mockImplementation(async (_workspaceId: number, targetSessionId: string) => {
      if (targetSessionId !== "rs-1-b") return { run: null };
      return scheduledRun;
    });
    const activeScheduledRun = {
      run: {
        runtimeRunId: "rr-scheduled", status: "running", lastSeq: 0, historyMessageCountBeforeRun: 1,
        source: { type: "scheduled", scheduledTaskId: 9, scheduledTaskName: "每日总结", triggerKind: "manual", taskAvailable: true }
      }
    };
    api.reconnectChatRun.mockImplementationOnce(async (_request: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-scheduled", seq: 1 } });
    });
    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    const prewarmsBeforeSchedules = api.prewarmRuntimeSession.mock.calls.length;
    await userEvent.setup().click(screen.getByRole("button", { name: "定时任务" }));
    expect(await screen.findByRole("heading", { name: "定时任务" })).toBeVisible();
    expect(api.listScheduledTasks).toHaveBeenCalledWith(1);
    expect(api.prewarmRuntimeSession).toHaveBeenCalledTimes(prewarmsBeforeSchedules);
    await userEvent.setup().click(screen.getByRole("button", { name: "立即执行" }));
    expect(await taskHeader().scope.findByText("会话 B")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("正在等待定时任务开始执行");
    await waitFor(() => expect(api.activeChatRun).toHaveBeenCalledWith(1, "rs-1-b", expect.any(AbortSignal)));
    resolveScheduledRun?.(activeScheduledRun);
    expect(api.activeChatRun).toHaveBeenCalledWith(1, "rs-1-b", expect.any(AbortSignal));
    await waitFor(() => expect(api.reconnectChatRun).toHaveBeenCalledWith({
      runtimeRunId: "rr-scheduled",
      afterSeq: 0,
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-1-b"
    }, expect.any(Function), expect.any(AbortSignal)));
    expect(screen.getByRole("button", { name: "定时任务 · 每日总结" })).toBeVisible();
    await waitFor(() => expect(screen.queryByText("正在等待定时任务开始执行…")).not.toBeInTheDocument());
  });

  it("keeps scheduled reasoning and tool events after terminal history refresh, then releases the composer", async () => {
    const scheduledSource = {
      type: "scheduled",
      scheduledTaskId: 9,
      scheduledTaskName: "每日总结",
      triggerKind: "manual",
      taskAvailable: true
    };
    api.listScheduledTasks.mockResolvedValue([{
      taskId: 9, taskName: "每日总结", prompt: "总结今天", targetRuntimeSessionId: "rs-1-b", targetSessionTitle: "会话 B", targetSessionStatus: "active",
      scheduleConfig: { type: "cron", preset: "daily", time: "09:00" }, scheduleSummary: "每天 09:00", status: "active", nextRunAt: null, lastRunAt: null,
      lastRunStatus: null, lastErrorCode: null, lastError: null, lastRuntimeRunId: null, runCount: 0, isRunning: false
    }]);
    api.triggerScheduledTask.mockResolvedValue({ taskId: 9, targetRuntimeSessionId: "rs-1-b", acceptedAt: "2026-07-20T00:00:00Z" });
    api.activeChatRun.mockImplementation(async (_workspaceId: number, targetSessionId: string) => ({
      run: targetSessionId === "rs-1-b" ? {
        runtimeRunId: "rr-scheduled-trace", status: "running", lastSeq: 0, historyMessageCountBeforeRun: 0,
        source: scheduledSource
      } : null
    }));
    api.chatHistory.mockImplementation(async (_workspaceId: number, targetSessionId: string) => ({
      messages: targetSessionId === "rs-1-b" ? [
        { role: "user", content: "总结今天", source: scheduledSource },
        { role: "assistant", content: "今日总结完成" }
      ] : []
    }));
    api.reconnectChatRun.mockImplementation(async (_request: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.run.started", data: { runtimeRunId: "rr-scheduled-trace", user_message: "总结今天", source: scheduledSource, seq: 1 } });
      onEvent({ event: "chat.reasoning.delta", data: { runtimeRunId: "rr-scheduled-trace", delta: "先整理今日工作", seq: 2 } });
      onEvent({ event: "chat.tool.event", data: { runtimeRunId: "rr-scheduled-trace", name: "terminal", callId: "call-summary", hasStableCallId: true, status: "completed", input: "collect", result: "done", seq: 3 } });
      onEvent({ event: "chat.message.completed", data: { runtimeRunId: "rr-scheduled-trace", text: "今日总结完成", seq: 4 } });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-scheduled-trace", seq: 5 } });
    });

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "定时任务" }));
    await user.click(await screen.findByRole("button", { name: "立即执行" }));

    expect(await screen.findByText("今日总结完成")).toBeVisible();
    const detailsSummary = await screen.findByText("思考过程");
    await user.click(detailsSummary);
    expect(screen.getByText("先整理今日工作")).toBeVisible();
    expect(screen.getByRole("button", { name: /已完成 1 个操作/ })).toBeVisible();
    expect(await screen.findByRole("button", { name: "发送消息" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "消息输入" })).toBeEnabled();
  });

  it("recovers a scheduled trigger from reconnect transport failure without a refresh", async () => {
    api.listScheduledTasks.mockResolvedValue([{
      taskId: 9, taskName: "每日总结", prompt: "总结今天", targetRuntimeSessionId: "rs-1-b", targetSessionTitle: "会话 B", targetSessionStatus: "active",
      scheduleConfig: { type: "cron", preset: "daily", time: "09:00" }, scheduleSummary: "每天 09:00", status: "active", nextRunAt: null, lastRunAt: null,
      lastRunStatus: null, lastErrorCode: null, lastError: null, lastRuntimeRunId: null, runCount: 0, isRunning: false
    }]);
    api.triggerScheduledTask.mockResolvedValue({ taskId: 9, targetRuntimeSessionId: "rs-1-b", acceptedAt: "2026-07-18T00:50:00Z" });
    let resolveScheduledRun: ((value: unknown) => void) | undefined;
    const scheduledRun = new Promise((resolve) => { resolveScheduledRun = resolve; });
    api.activeChatRun.mockImplementation(async (_workspaceId: number, targetSessionId: string) => (
      targetSessionId === "rs-1-b" ? scheduledRun : { run: null }
    ));
    api.reconnectChatRun.mockRejectedValueOnce(new Error("Network Error"));
    api.chatHistory.mockImplementation(async (_workspaceId: number, targetSessionId: string) => ({
      messages: targetSessionId === "rs-1-b" ? [
        { role: "user", content: "总结今天", source: { type: "scheduled", scheduledTaskId: 9, scheduledTaskName: "每日总结", triggerKind: "manual", taskAvailable: true } },
        { role: "assistant", content: "恢复后的完整回答" }
      ] : []
    }));

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    await userEvent.setup().click(screen.getByRole("button", { name: "定时任务" }));
    await screen.findByRole("button", { name: "立即执行" });
    await userEvent.setup().click(screen.getByRole("button", { name: "立即执行" }));
    resolveScheduledRun?.({
      run: {
        runtimeRunId: "rr-scheduled", status: "running", lastSeq: 0, historyMessageCountBeforeRun: 1,
        source: { type: "scheduled", scheduledTaskId: 9, scheduledTaskName: "每日总结", triggerKind: "manual", taskAvailable: true }
      }
    });

    await waitFor(() => expect(api.reconnectChatRun).toHaveBeenCalledWith(
      {
        runtimeRunId: "rr-scheduled",
        afterSeq: 0,
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-b"
      },
      expect.any(Function),
      expect.any(AbortSignal)
    ));
    expect(await screen.findByText("恢复后的完整回答")).toBeVisible();
    expect(screen.queryByText("Network Error")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("retries a premature scheduled reconnect EOF without a refresh", async () => {
    api.listScheduledTasks.mockResolvedValue([{
      taskId: 9, taskName: "每日总结", prompt: "总结今天", targetRuntimeSessionId: "rs-1-b", targetSessionTitle: "会话 B", targetSessionStatus: "active",
      scheduleConfig: { type: "cron", preset: "daily", time: "09:00" }, scheduleSummary: "每天 09:00", status: "active", nextRunAt: null, lastRunAt: null,
      lastRunStatus: null, lastErrorCode: null, lastError: null, lastRuntimeRunId: null, runCount: 0, isRunning: false
    }]);
    api.triggerScheduledTask.mockResolvedValue({ taskId: 9, targetRuntimeSessionId: "rs-1-b", acceptedAt: "2026-07-18T00:50:00Z" });
    api.activeChatRun.mockImplementation(async (_workspaceId: number, targetSessionId: string) => ({
      run: targetSessionId === "rs-1-b" ? {
        runtimeRunId: "rr-scheduled", status: "running", lastSeq: 0, historyMessageCountBeforeRun: 1,
        source: { type: "scheduled", scheduledTaskId: 9, scheduledTaskName: "每日总结", triggerKind: "manual", taskAvailable: true }
      } : null
    }));
    api.reconnectChatRun
      .mockResolvedValueOnce(undefined)
      .mockImplementationOnce(async (_request: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
        onEvent({ event: "chat.done", data: { runtimeRunId: "rr-scheduled", seq: 1 } });
      });
    api.chatHistory.mockImplementation(async (_workspaceId: number, targetSessionId: string) => ({
      messages: targetSessionId === "rs-1-b" ? [
        { role: "user", content: "总结今天", source: { type: "scheduled", scheduledTaskId: 9, scheduledTaskName: "每日总结", triggerKind: "manual", taskAvailable: true } },
        { role: "assistant", content: "EOF 后恢复的完整回答" }
      ] : []
    }));

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    fireEvent.click(screen.getByRole("button", { name: "定时任务" }));
    await screen.findByRole("button", { name: "立即执行" });
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "立即执行" })); });

    await act(async () => { await Promise.resolve(); });
    expect(api.reconnectChatRun).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(2_999); });
    expect(api.reconnectChatRun).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(api.reconnectChatRun).toHaveBeenCalledTimes(2);
    expect(screen.getByText("EOF 后恢复的完整回答")).toBeVisible();
    expect(screen.queryByText("Network Error")).not.toBeInTheDocument();
  });

  it("aborts an unresolved scheduled reconnect after 15 seconds before retrying", async () => {
    api.listScheduledTasks.mockResolvedValue([{
      taskId: 9, taskName: "每日总结", prompt: "总结今天", targetRuntimeSessionId: "rs-1-b", targetSessionTitle: "会话 B", targetSessionStatus: "active",
      scheduleConfig: { type: "cron", preset: "daily", time: "09:00" }, scheduleSummary: "每天 09:00", status: "active", nextRunAt: null, lastRunAt: null,
      lastRunStatus: null, lastErrorCode: null, lastError: null, lastRuntimeRunId: null, runCount: 0, isRunning: false
    }]);
    api.triggerScheduledTask.mockResolvedValue({ taskId: 9, targetRuntimeSessionId: "rs-1-b", acceptedAt: "2026-07-18T00:50:00Z" });
    api.activeChatRun.mockImplementation(async (_workspaceId: number, targetSessionId: string) => ({
      run: targetSessionId === "rs-1-b" ? {
        runtimeRunId: "rr-scheduled", status: "running", lastSeq: 0, historyMessageCountBeforeRun: 1,
        source: { type: "scheduled", scheduledTaskId: 9, scheduledTaskName: "每日总结", triggerKind: "manual", taskAvailable: true }
      } : null
    }));
    let firstReconnectSignal: AbortSignal | undefined;
    api.reconnectChatRun
      .mockImplementationOnce(async (_request: unknown, _onEvent: unknown, signal: AbortSignal) => new Promise<void>((resolve) => {
        firstReconnectSignal = signal;
        signal.addEventListener("abort", () => resolve(), { once: true });
      }))
      .mockImplementationOnce(async (_request: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
        onEvent({ event: "chat.done", data: { runtimeRunId: "rr-scheduled", seq: 1 } });
      });

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    fireEvent.click(screen.getByRole("button", { name: "定时任务" }));
    await screen.findByRole("button", { name: "立即执行" });
    vi.useFakeTimers();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "立即执行" })); });

    await act(async () => { await Promise.resolve(); });
    expect(firstReconnectSignal?.aborted).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(14_999); });
    expect(firstReconnectSignal?.aborted).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(firstReconnectSignal?.aborted).toBe(true);
    expect(api.reconnectChatRun).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(api.reconnectChatRun).toHaveBeenCalledTimes(2);
  });

  it("observes an automatic scheduled run while its target session is already open", async () => {
    let targetSessionChecks = 0;
    api.activeChatRun.mockImplementation(async (_workspaceId: number, targetSessionId: string) => ({
      run: targetSessionId === "rs-1-b" && targetSessionChecks++ > 0 ? {
        runtimeRunId: "rr-auto", status: "running", lastSeq: 0, historyMessageCountBeforeRun: 0,
        source: { type: "scheduled", scheduledTaskId: 22, scheduledTaskName: "自动日报", triggerKind: "automatic", taskAvailable: true }
      } : null
    }));
    api.reconnectChatRun.mockImplementation(async (_request: unknown, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "chat.run.started", data: { runtimeRunId: "rr-auto", user_message: "生成自动日报", source: { type: "scheduled", scheduledTaskId: 22, scheduledTaskName: "自动日报", triggerKind: "automatic", taskAvailable: true }, seq: 1 } });
      onEvent({ event: "chat.message.delta", data: { runtimeRunId: "rr-auto", delta: "自动回复", seq: 2 } });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-auto", seq: 3 } });
    });

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 B" });
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "会话 B" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    await act(async () => { await Promise.resolve(); });

    expect(taskHeader().scope.getByText("会话 B")).toBeVisible();
    expect(screen.getByRole("button", { name: "定时任务 · 自动日报" })).toBeVisible();
    expect(screen.getByText("自动回复")).toBeVisible();
    expect(api.reconnectChatRun).toHaveBeenCalledTimes(1);

    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "定时任务 · 自动日报" })); });
    expect(screen.getByRole("heading", { name: "定时任务" })).toBeVisible();
  });

  it("discovers a scheduled artifact when active-run polling misses a fast run", async () => {
    localStorage.setItem("hermes.files-pane", "false");
    let artifactAvailable = false;
    api.activeChatRun.mockResolvedValue({ run: null });
    api.chatFiles.mockImplementation(async (_workspaceId: number, targetSessionId: string) => (
      targetSessionId === "rs-1-b" && artifactAvailable
        ? [{ fileId: 81, displayName: "自动日报.pptx", sourceType: "tool", sizeBytes: 12 }]
        : []
    ));

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 B" });
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "会话 B" }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByText("自动日报.pptx")).not.toBeInTheDocument();

    artifactAvailable = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });

    expect(screen.getByText("自动日报.pptx")).toBeVisible();
    expect(screen.getByRole("complementary", { name: "当前任务文件" })).toBeVisible();
  });

  it("refreshes and opens scheduled artifacts when returning to the same chat session", async () => {
    localStorage.setItem("hermes.files-pane", "false");
    let artifactAvailable = false;
    api.chatFiles.mockImplementation(async (_workspaceId: number, targetSessionId: string) => (
      targetSessionId === "rs-1-a" && artifactAvailable
        ? [{ fileId: 82, displayName: "后台生成.pptx", sourceType: "tool", sizeBytes: 16 }]
        : []
    ));

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    await waitFor(() => expect(api.chatFiles).toHaveBeenCalledWith(1, "rs-1-a"));
    fireEvent.click(screen.getByRole("button", { name: "定时任务" }));
    await screen.findByRole("heading", { name: "定时任务" });

    artifactAvailable = true;
    fireEvent.click(screen.getByRole("button", { name: "任务" }));

    expect(await screen.findByText("后台生成.pptx")).toBeVisible();
    expect(screen.getByRole("complementary", { name: "当前任务文件" })).toBeVisible();
  });

  it("refreshes history for a trigger that finishes before active polling finds a run", async () => {
    api.listScheduledTasks
      .mockResolvedValueOnce([{
        taskId: 9, taskName: "快速总结", prompt: "完成后结果", targetRuntimeSessionId: "rs-1-b", targetSessionTitle: "会话 B", targetSessionStatus: "active",
        scheduleConfig: { type: "cron", preset: "daily", time: "09:00" }, scheduleSummary: "每天 09:00", status: "active", nextRunAt: null, lastRunAt: null,
        lastRunStatus: null, lastErrorCode: null, lastError: null, lastRuntimeRunId: null, runCount: 0, isRunning: false
      }])
      .mockResolvedValue([{
        taskId: 9, taskName: "快速总结", prompt: "完成后结果", targetRuntimeSessionId: "rs-1-b", targetSessionTitle: "会话 B", targetSessionStatus: "active",
        scheduleConfig: { type: "cron", preset: "daily", time: "09:00" }, scheduleSummary: "每天 09:00", status: "active", nextRunAt: null, lastRunAt: "2026-07-18T00:50:01Z",
        lastRunStatus: "success", lastErrorCode: null, lastError: null, lastRuntimeRunId: "rr-finished", runCount: 1, isRunning: false
      }]);
    api.triggerScheduledTask.mockResolvedValue({ taskId: 9, targetRuntimeSessionId: "rs-1-b", acceptedAt: "2026-07-18T00:50:00Z" });
    api.activeChatRun.mockResolvedValue({ run: null });
    api.chatHistory.mockImplementation(async (_workspaceId: number, targetSessionId: string) => ({
      messages: targetSessionId === "rs-1-b" ? [
        { role: "user", content: "完成后结果", source: { type: "scheduled", scheduledTaskId: 9, scheduledTaskName: "快速总结", triggerKind: "manual", taskAvailable: true } },
        { role: "assistant", content: "已完成" }
      ] : []
    }));
    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    await userEvent.setup().click(screen.getByRole("button", { name: "定时任务" }));
    await screen.findByRole("button", { name: "立即执行" });
    await userEvent.setup().click(screen.getByRole("button", { name: "立即执行" }));

    expect(await screen.findByText("已完成")).toBeVisible();
    expect(screen.getByRole("button", { name: "定时任务 · 快速总结" })).toBeVisible();
    await waitFor(() => expect(screen.queryByText("正在等待定时任务开始执行…")).not.toBeInTheDocument());
  });

  it("does not overlap a pending scheduled-task poll while its active-run request is slow", async () => {
    let resolveActive: ((value: unknown) => void) | undefined;
    const slowActive = new Promise((resolve) => { resolveActive = resolve; });
    api.listScheduledTasks.mockResolvedValue([{
      taskId: 9, taskName: "慢速任务", prompt: "等待运行", targetRuntimeSessionId: "rs-1-b", targetSessionTitle: "会话 B", targetSessionStatus: "active",
      scheduleConfig: { type: "cron", preset: "daily", time: "09:00" }, scheduleSummary: "每天 09:00", status: "active", nextRunAt: null, lastRunAt: null,
      lastRunStatus: null, lastErrorCode: null, lastError: null, lastRuntimeRunId: null, runCount: 0, isRunning: false
    }]);
    api.triggerScheduledTask.mockResolvedValue({ taskId: 9, targetRuntimeSessionId: "rs-1-b", acceptedAt: "2026-07-18T00:50:00Z" });
    api.activeChatRun.mockImplementation(async (_workspaceId: number, targetSessionId: string) => (
      targetSessionId === "rs-1-b" ? slowActive : { run: null }
    ));

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    fireEvent.click(screen.getByRole("button", { name: "定时任务" }));
    await screen.findByRole("button", { name: "立即执行" });
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "立即执行" }));
      await Promise.resolve();
    });
    expect(taskHeader().scope.getByText("会话 B")).toBeVisible();

    expect(api.activeChatRun.mock.calls.filter(([, targetSessionId]) => targetSessionId === "rs-1-b")).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.activeChatRun.mock.calls.filter(([, targetSessionId]) => targetSessionId === "rs-1-b")).toHaveLength(1);

    await act(async () => { resolveActive?.({ run: null }); });
  });

  it("backs off and ends pending scheduled-task observation after repeated server failures", async () => {
    api.listScheduledTasks.mockResolvedValue([{
      taskId: 9, taskName: "失败任务", prompt: "等待运行", targetRuntimeSessionId: "rs-1-b", targetSessionTitle: "会话 B", targetSessionStatus: "active",
      scheduleConfig: { type: "cron", preset: "daily", time: "09:00" }, scheduleSummary: "每天 09:00", status: "active", nextRunAt: null, lastRunAt: null,
      lastRunStatus: null, lastErrorCode: null, lastError: null, lastRuntimeRunId: null, runCount: 0, isRunning: false
    }]);
    api.triggerScheduledTask.mockResolvedValue({ taskId: 9, targetRuntimeSessionId: "rs-1-b", acceptedAt: "2026-07-18T00:50:00Z" });
    api.activeChatRun.mockImplementation(async (_workspaceId: number, targetSessionId: string) => {
      if (targetSessionId === "rs-1-b") throw Object.assign(new Error("server error"), { status: 500 });
      return { run: null };
    });

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    fireEvent.click(screen.getByRole("button", { name: "定时任务" }));
    await screen.findByRole("button", { name: "立即执行" });
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "立即执行" }));
      await Promise.resolve();
    });
    expect(taskHeader().scope.getByText("会话 B")).toBeVisible();
    // 报错框 5 秒后自动消失（useAutoDismissError），逐步推进并在 alert 出现时立即捕获，
    // 不能一次性推进全部虚拟时间后再断言。
    let alertText: string | null = null;
    for (let attempt = 0; attempt < 600 && !alertText; attempt += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
      alertText = screen.queryByRole("alert")?.textContent ?? null;
    }

    expect(api.activeChatRun.mock.calls.filter(([, targetSessionId]) => targetSessionId === "rs-1-b").length).toBeGreaterThanOrEqual(20);
    expect(alertText).toContain("无法确认定时任务是否已开始执行");
    expect(screen.queryByText("正在等待定时任务开始执行…")).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(ERROR_NOTICE_AUTO_DISMISS_MS); });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("cancels a pending scheduled-task request when the chat page unmounts", async () => {
    let receivedSignal: AbortSignal | undefined;
    let resolveActive: ((value: unknown) => void) | undefined;
    const slowActive = new Promise((resolve) => { resolveActive = resolve; });
    api.listScheduledTasks.mockResolvedValue([{
      taskId: 9, taskName: "待取消任务", prompt: "等待运行", targetRuntimeSessionId: "rs-1-b", targetSessionTitle: "会话 B", targetSessionStatus: "active",
      scheduleConfig: { type: "cron", preset: "daily", time: "09:00" }, scheduleSummary: "每天 09:00", status: "active", nextRunAt: null, lastRunAt: null,
      lastRunStatus: null, lastErrorCode: null, lastError: null, lastRuntimeRunId: null, runCount: 0, isRunning: false
    }]);
    api.triggerScheduledTask.mockResolvedValue({ taskId: 9, targetRuntimeSessionId: "rs-1-b", acceptedAt: "2026-07-18T00:50:00Z" });
    api.activeChatRun.mockImplementation(async (_workspaceId: number, targetSessionId: string, signal?: AbortSignal) => {
      if (targetSessionId === "rs-1-b") {
        receivedSignal = signal;
        return slowActive;
      }
      return { run: null };
    });

    const page = render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    fireEvent.click(screen.getByRole("button", { name: "定时任务" }));
    await screen.findByRole("button", { name: "立即执行" });
    fireEvent.click(screen.getByRole("button", { name: "立即执行" }));
    await taskHeader().scope.findByText("会话 B");
    await waitFor(() => expect(receivedSignal).toBeDefined());

    page.unmount();
    expect(receivedSignal?.aborted).toBe(true);
    await act(async () => { resolveActive?.({ run: null }); });
  });

  it("replays the clarify continuation as soon as the user confirms an answer", async () => {
    api.clarifyRequests.mockResolvedValue([{
      requestId: "clr-math",
      runtimeSessionId: "rs-1-a",
      parentRuntimeRunId: "rr-parent",
      question: "17 × 23 = ？",
      choices: ["389", "391", "400", "410"],
      multiSelect: false,
      status: "waiting",
      version: 1,
      createdAt: "2026-08-13T02:37:36Z"
    }]);
    api.answerClarifyRequest.mockResolvedValue({
      requestId: "clr-math",
      runtimeSessionId: "rs-1-a",
      parentRuntimeRunId: "rr-parent",
      question: "17 × 23 = ？",
      choices: ["389", "391", "400", "410"],
      multiSelect: false,
      status: "answered",
      version: 2,
      createdAt: "2026-08-13T02:37:36Z",
      answeredAt: "2026-08-13T02:37:56Z",
      resumeRuntimeRunId: "rr-clarify-math"
    });
    api.reconnectChatRun.mockImplementation(async (
      payload: { runtimeRunId: string; afterSeq: number },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      expect(payload).toEqual({
        runtimeRunId: "rr-clarify-math",
        afterSeq: 0,
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-1-a"
      });
      onEvent({
        event: "chat.message.completed",
        data: { runtimeRunId: "rr-clarify-math", seq: 1, text: "你选的是 389，正确答案是 391。" }
      });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-clarify-math", seq: 2 } });
    });

    render(<UserChatPage />);
    await screen.findByRole("button", { name: "会话 A" });
    expect(await screen.findByText("17 × 23 = ？")).toBeVisible();

    const user = userEvent.setup();
    await user.click(screen.getByRole("radio", { name: "389" }));
    await user.click(screen.getByRole("button", { name: "确认并继续" }));

    expect(await screen.findByText("你选的是 389，正确答案是 391。")).toBeVisible();
    expect(api.reconnectChatRun).toHaveBeenCalledTimes(1);
    await waitFor(
      () => expect(screen.queryByText("已继续执行")).not.toBeInTheDocument(),
      { timeout: 4_000 }
    );
  });

  it("hides an auto-defaulted clarify card while replaying its continuation", async () => {
    const baseRequest = {
      requestId: "clr-auto",
      runtimeSessionId: "rs-1-a",
      parentRuntimeRunId: "rr-parent",
      question: "需要选择输出格式吗？",
      choices: ["PDF", "Word"],
      multiSelect: false,
      autoDefaultAt: "2026-09-23T02:01:00Z",
      version: 1,
      createdAt: "2026-09-23T02:00:00Z"
    };
    let clarifyPollCount = 0;
    api.clarifyRequests.mockImplementation(async () => {
      clarifyPollCount += 1;
      if (clarifyPollCount === 1) return [{ ...baseRequest, status: "waiting" }];
      if (clarifyPollCount === 2) {
        return [{
          ...baseRequest,
          status: "claimed",
          answer: { kind: "skip" },
          answerSource: "auto",
          version: 2
        }];
      }
      return [{
        ...baseRequest,
        status: "answered",
        answer: { kind: "skip" },
        answerSource: "auto",
        resumeRuntimeRunId: "rr-clarify-auto",
        version: 3
      }];
    });
    api.reconnectChatRun.mockImplementation(async (
      payload: { runtimeRunId: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      expect(payload.runtimeRunId).toBe("rr-clarify-auto");
      onEvent({
        event: "chat.message.completed",
        data: { runtimeRunId: "rr-clarify-auto", seq: 1, text: "已按默认格式继续。" }
      });
      onEvent({ event: "chat.done", data: { runtimeRunId: "rr-clarify-auto", seq: 2 } });
    });

    render(<UserChatPage />);
    expect(await screen.findByText("需要选择输出格式吗？")).toBeVisible();

    await waitFor(
      () => expect(screen.queryByText("需要选择输出格式吗？")).not.toBeInTheDocument(),
      { timeout: 4_000 }
    );
    expect(screen.queryByText("正在按默认值继续…")).not.toBeInTheDocument();
    expect(await screen.findByText("已按默认格式继续。", undefined, { timeout: 4_000 })).toBeVisible();
    expect(api.reconnectChatRun).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByText("已继续执行")).not.toBeInTheDocument());
  });

  it("does not turn an auto-default resume failure into a user prompt", async () => {
    api.clarifyRequests
      .mockResolvedValueOnce([{
        requestId: "clr-auto-failed",
        runtimeSessionId: "rs-1-a",
        parentRuntimeRunId: "rr-parent",
        question: "需要选择输出格式吗？",
        choices: ["PDF", "Word"],
        multiSelect: false,
        status: "waiting",
        version: 1,
        createdAt: "2026-09-23T02:00:00Z"
      }])
      .mockResolvedValue([{
      requestId: "clr-auto-failed",
      runtimeSessionId: "rs-1-a",
      parentRuntimeRunId: "rr-parent",
      question: "需要选择输出格式吗？",
      choices: ["PDF", "Word"],
      multiSelect: false,
      status: "resume_failed",
      answer: { kind: "skip" },
      answerSource: "auto",
      resumeRuntimeRunId: "rr-clarify-auto-failed",
      resumeAttempts: 1,
      version: 3,
      createdAt: "2026-09-23T02:00:00Z"
      }]);

    render(<UserChatPage />);
    expect(await screen.findByText("需要选择输出格式吗？")).toBeVisible();

    await act(async () => {
      await new Promise((resolve) => { window.setTimeout(resolve, 2_200); });
    });
    expect(screen.queryByText("需要选择输出格式吗？")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试继续" })).not.toBeInTheDocument();
  });

  it("converges a clarify card after one retryable replay attempt", async () => {
    api.reconnectChatRun.mockReset();
    api.clarifyRequests.mockResolvedValue([{
      requestId: "clr-retryable-replay",
      runtimeSessionId: "rs-1-a",
      parentRuntimeRunId: "rr-parent",
      question: "需要继续处理吗？",
      choices: ["继续"],
      multiSelect: false,
      status: "answered",
      answer: { kind: "single", text: "继续" },
      answerSource: "user",
      resumeRuntimeRunId: "rr-clarify-retryable",
      version: 2,
      createdAt: "2026-09-23T02:00:00Z"
    }]);
    let targetReplays = 0;
    api.reconnectChatRun.mockImplementation(async (
      payload: { runtimeRunId: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
    ) => {
      if (payload.runtimeRunId !== "rr-clarify-retryable") return;
      targetReplays += 1;
      // The replay stream closes without a terminal event. The card must
      // still converge instead of pinning the session in streaming state.
    });

    render(<UserChatPage />);
    await waitFor(
      () => expect(targetReplays).toBeGreaterThanOrEqual(1),
      { timeout: 4_000 }
    );
    await waitFor(
      () => expect(screen.queryByText("已继续执行")).not.toBeInTheDocument(),
      { timeout: 4_000 }
    );
    // The card must stay converged across later poll cycles instead of
    // returning as a permanently mounted "已继续执行" card.
    await new Promise((resolve) => setTimeout(resolve, 2_500));
    expect(screen.queryByText("已继续执行")).not.toBeInTheDocument();
  });

  it("does not leak a clarify card from one session into another", async () => {
    api.clarifyRequests.mockImplementation(async (_workspaceId: number, runtimeSessionId: string) => (
      runtimeSessionId === "rs-1-a"
        ? [{
            requestId: "clr-session-a",
            runtimeSessionId: "rs-1-a",
            parentRuntimeRunId: "rr-parent",
            question: "会话 A 的确认",
            choices: ["保持现状"],
            multiSelect: false,
            status: "resume_failed",
            answer: { kind: "single", text: "保持现状" },
            answerSource: "user",
            resumeRuntimeRunId: "rr-clarify-a",
            resumeAttempts: 1,
            version: 3,
            createdAt: "2026-09-23T02:00:00Z"
          }]
        : []
    ));

    render(<UserChatPage />);
    expect(await screen.findByText("会话 A 的确认")).toBeVisible();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "会话 B" }));

    await waitFor(() => expect(screen.queryByText("会话 A 的确认")).not.toBeInTheDocument());
    expect(api.clarifyRequests).toHaveBeenCalledWith(1, "rs-1-b");
  });

  it("does not let a stale clarify answer overwrite the newly selected session", async () => {
    let resolveAnswer: ((value: unknown) => void) | undefined;
    api.clarifyRequests.mockImplementation(async (_workspaceId: number, runtimeSessionId: string) => (
      runtimeSessionId === "rs-1-a"
        ? [{
            requestId: "clr-session-a",
            runtimeSessionId: "rs-1-a",
            parentRuntimeRunId: "rr-parent",
            question: "会话 A 的确认",
            choices: ["保持现状"],
            multiSelect: false,
            status: "waiting",
            version: 1,
            createdAt: "2026-09-23T02:00:00Z"
          }]
        : [{
            requestId: "clr-session-b",
            runtimeSessionId: "rs-1-b",
            parentRuntimeRunId: "rr-parent-b",
            question: "会话 B 的确认",
            choices: ["继续"],
            multiSelect: false,
            status: "waiting",
            version: 1,
            createdAt: "2026-09-23T02:01:00Z"
          }]
    ));
    api.answerClarifyRequest.mockImplementation(() => new Promise((resolve) => {
      resolveAnswer = resolve;
    }));

    render(<UserChatPage />);
    expect(await screen.findByText("会话 A 的确认")).toBeVisible();

    const user = userEvent.setup();
    await user.click(screen.getByRole("radio", { name: "保持现状" }));
    await user.click(screen.getByRole("button", { name: "确认并继续" }));
    await user.click(screen.getByRole("button", { name: "会话 B" }));
    expect(await screen.findByText("会话 B 的确认")).toBeVisible();

    await act(async () => {
      resolveAnswer?.({
        requestId: "clr-session-a",
        runtimeSessionId: "rs-1-a",
        parentRuntimeRunId: "rr-parent",
        question: "会话 A 的确认",
        choices: ["保持现状"],
        multiSelect: false,
        status: "answered",
        answer: { kind: "single", text: "保持现状" },
        answerSource: "user",
        resumeRuntimeRunId: "rr-clarify-a",
        version: 2,
        createdAt: "2026-09-23T02:00:00Z"
      });
    });

    expect(await screen.findByText("会话 B 的确认")).toBeVisible();
    expect(screen.queryByText("会话 A 的确认")).not.toBeInTheDocument();
    expect(api.reconnectChatRun).not.toHaveBeenCalledWith(
      expect.objectContaining({ runtimeRunId: "rr-clarify-a" }),
      expect.any(Function)
    );
  });

  it("allows retrying a failed clarify continuation", async () => {
    api.clarifyRequests.mockResolvedValue([{
      requestId: "clr-retry",
      runtimeSessionId: "rs-1-a",
      parentRuntimeRunId: "rr-parent",
      question: "需要选择输出格式吗？",
      choices: ["PDF", "Word"],
      multiSelect: false,
      status: "resume_failed",
      answer: { kind: "single", text: "PDF" },
      answerSource: "user",
      resumeRuntimeRunId: "rr-clarify-retry",
      resumeAttempts: 1,
      version: 3,
      createdAt: "2026-09-23T02:00:00Z"
    }]);
    api.answerClarifyRequest.mockResolvedValue({
      requestId: "clr-retry",
      runtimeSessionId: "rs-1-a",
      parentRuntimeRunId: "rr-parent",
      question: "需要选择输出格式吗？",
      choices: ["PDF", "Word"],
      multiSelect: false,
      status: "claimed",
      answer: { kind: "single", text: "PDF" },
      answerSource: "user",
      resumeRuntimeRunId: "rr-clarify-retry",
      resumeAttempts: 2,
      version: 4,
      createdAt: "2026-09-23T02:00:00Z"
    });

    render(<UserChatPage />);
    expect(await screen.findByText("继续失败，可重试")).toBeVisible();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "重试继续" }));

    await waitFor(() => expect(api.answerClarifyRequest).toHaveBeenCalledWith(
      1,
      "clr-retry",
      { kind: "single", text: "PDF" },
      3
    ));
  });
});
