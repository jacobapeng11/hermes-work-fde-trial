import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  bootstrapRuntime: vi.fn(),
  runtimeStartupContext: vi.fn(),
  keepaliveRuntime: vi.fn(),
  runnableExperts: vi.fn(),
  runtimeModels: vi.fn(),
  runtimeSessions: vi.fn(),
  taskPromptConfig: vi.fn(),
  createRuntimeSession: vi.fn(),
  chatHistoryPreview: vi.fn(),
  chatHistoryAuthoritative: vi.fn(),
  chatFiles: vi.fn(),
  chatQueueSummary: vi.fn(),
  chatQueueSummaryPrepared: vi.fn(),
  activeChatRun: vi.fn(),
  prewarmRuntimeSession: vi.fn(),
  streamChatRun: vi.fn(),
  reportChatClientTiming: vi.fn(),
}));

vi.mock("../../api/client", () => ({ userApi: api }));

import { UserChatPage } from "../UserChatPage";

describe("initial runtime allocation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem("hermes.files-pane", "false");
    api.bootstrapRuntime.mockResolvedValue({
      runtimeWorkspaceId: 1,
      runtimeStatus: "starting"
    });
    api.keepaliveRuntime.mockResolvedValue(undefined);
    api.runtimeStartupContext.mockResolvedValue({
      runtimeWorkspaceId: 1,
      context: "first_allocation"
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
    api.taskPromptConfig.mockResolvedValue({
      revision: 1,
      config: { tabs: [] }
    });
    api.runtimeSessions.mockResolvedValue([]);
    api.createRuntimeSession.mockResolvedValue({
      runtimeSessionId: "rs-first-login",
      title: "新任务",
      messageCount: 0,
      sourceType: "web",
      sessionModelEntryId: 19,
      status: "active"
    });
    const emptyHistory = {
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-first-login",
      messages: [],
      source: "mysql",
      consistency: "projection_preview",
      generatedAt: "2026-10-12T00:00:00.000Z",
      hasActiveRun: false
    };
    api.chatHistoryPreview.mockResolvedValue(emptyHistory);
    api.chatHistoryAuthoritative.mockResolvedValue({
      ...emptyHistory,
      source: "hermes",
      consistency: "authoritative"
    });
    api.chatFiles.mockResolvedValue([]);
    api.chatQueueSummary.mockResolvedValue({
      queuedRuns: [],
      scheduledQueuedCount: 0
    });
    api.chatQueueSummaryPrepared.mockImplementation(
      (workspaceId: number, sessionId: string) => api.chatQueueSummary(workspaceId, sessionId)
    );
    api.activeChatRun.mockResolvedValue({ run: null });
    api.reportChatClientTiming.mockResolvedValue(undefined);
  });

  it("creates the first personal task and queues its first send during allocation", async () => {
    let finishPrewarm: ((value: unknown) => void) | undefined;
    api.prewarmRuntimeSession.mockImplementationOnce(() => new Promise((resolve) => {
      finishPrewarm = resolve;
    }));
    api.streamChatRun.mockImplementationOnce(async (
      payload: { runtimeRunId: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
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

    await waitFor(() => expect(api.createRuntimeSession).toHaveBeenCalledWith(1, {
      title: "新任务",
      modelEntryId: 19,
      sourceType: "web"
    }));
    const input = screen.getByRole("textbox", { name: "消息输入" });
    expect(input).toBeEnabled();
    expect(api.prewarmRuntimeSession).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "首次登录排队消息" } });
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));

    expect(screen.getByText("首次登录排队消息")).toBeVisible();
    expect(
      screen.getByText("首次登录，给您分配云上资源，稍等，您可以先输入任务……")
    ).toBeVisible();
    expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(1, "rs-first-login");
    expect(api.streamChatRun).not.toHaveBeenCalled();

    await act(async () => {
      finishPrewarm?.({
        state: "ready",
        readyReceiptId: "receipt-first-login",
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-first-login",
        modelEntryId: 19,
        issuedAt: new Date().toISOString(),
        expiresAt: new Date(Date.now() + 60_000).toISOString()
      });
    });

    await waitFor(() => expect(api.streamChatRun).toHaveBeenCalledTimes(1));
    expect(api.streamChatRun.mock.calls[0]?.[0]).toEqual(expect.objectContaining({
      runtimeSessionId: "rs-first-login",
      message: "首次登录排队消息",
      readyReceiptId: "receipt-first-login"
    }));
  });

  it("labels a returning user's scaled-down runtime as environment recovery", async () => {
    api.runtimeStartupContext.mockResolvedValueOnce({
      runtimeWorkspaceId: 1,
      context: "environment_recovery"
    });

    render(<UserChatPage />);

    expect(
      await screen.findByText("环境恢复中，马上好，您可以先输入任务……")
    ).toBeVisible();
  });

  it("keeps environment recovery copy while the first queued send is prewarming", async () => {
    let finishPrewarm: ((value: unknown) => void) | undefined;
    api.bootstrapRuntime.mockResolvedValue({
      runtimeWorkspaceId: 1,
      runtimeStatus: "ready"
    });
    api.runtimeStartupContext.mockResolvedValue({
      runtimeWorkspaceId: 1,
      context: "environment_recovery"
    });
    api.prewarmRuntimeSession.mockImplementationOnce(() => new Promise((resolve) => {
      finishPrewarm = resolve;
    }));
    api.streamChatRun.mockImplementationOnce(async (
      payload: { runtimeRunId: string },
      onEvent: (event: { event: string; data: Record<string, unknown> }) => void
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

    await waitFor(() => expect(api.createRuntimeSession).toHaveBeenCalledWith(1, {
      title: "新任务",
      modelEntryId: 19,
      sourceType: "web"
    }));
    const input = screen.getByRole("textbox", { name: "消息输入" });
    fireEvent.change(input, { target: { value: "环境恢复首条消息" } });
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));

    expect(
      screen.getByText("环境恢复中，马上好，您可以先输入任务……")
    ).toBeVisible();
    expect(api.streamChatRun).not.toHaveBeenCalled();

    await act(async () => {
      finishPrewarm?.({
        state: "ready",
        readyReceiptId: "receipt-rs-first-login",
        runtimeWorkspaceId: 1,
        runtimeSessionId: "rs-first-login",
        modelEntryId: 19,
        issuedAt: new Date().toISOString(),
        expiresAt: new Date(Date.now() + 60_000).toISOString()
      });
    });

    await waitFor(() => expect(api.streamChatRun).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(
      screen.queryByText("环境恢复中，马上好，您可以先输入任务……")
    ).not.toBeInTheDocument());
  });

  it("clears environment recovery copy after initial prewarm is ready", async () => {
    api.bootstrapRuntime.mockResolvedValue({
      runtimeWorkspaceId: 1,
      runtimeStatus: "ready"
    });
    api.runtimeStartupContext.mockResolvedValue({
      runtimeWorkspaceId: 1,
      context: "environment_recovery"
    });
    api.prewarmRuntimeSession.mockResolvedValue({
      state: "ready",
      runtimeWorkspaceId: 1,
      runtimeSessionId: "rs-first-login",
      modelEntryId: 19
    });

    render(<UserChatPage />);

    await waitFor(() => expect(api.prewarmRuntimeSession).toHaveBeenCalledWith(
      1,
      "rs-first-login"
    ));
    await waitFor(() => expect(
      screen.queryByText("环境恢复中，马上好，您可以先输入任务……")
    ).not.toBeInTheDocument());
  });

  it.each([
    ["none", Promise.resolve({ runtimeWorkspaceId: 1, context: "none" })],
    ["pending", new Promise(() => undefined)]
  ])("does not show an environment hint when startup context is %s", async (_label, startupContext) => {
    api.runtimeStartupContext.mockReturnValueOnce(startupContext);

    render(<UserChatPage />);

    expect(await screen.findByRole("textbox", { name: "消息输入" })).toBeEnabled();
    expect(screen.queryByText("正在为你分配云资源，稍等，马上就好……")).not.toBeInTheDocument();
    expect(screen.queryByText("首次登录，给您分配云上资源，稍等，您可以先输入任务……")).not.toBeInTheDocument();
    expect(screen.queryByText("环境恢复中，马上好，您可以先输入任务……")).not.toBeInTheDocument();
  });
});
