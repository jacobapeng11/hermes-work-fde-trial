import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { emptyChatQueueSnapshot } from "./chatQueueProjection";


const api = vi.hoisted(() => ({
  activeChatRun: vi.fn(),
  backgroundCollaborations: vi.fn(),
  bootstrapRuntime: vi.fn(),
  chatFiles: vi.fn(),
  chatHistory: vi.fn(),
  chatHistoryAuthoritative: vi.fn(),
  chatHistoryPreview: vi.fn(),
  chatQueueSummary: vi.fn(),
  chatQueueSummaryPrepared: vi.fn(),
  clarifyRequests: vi.fn(),
  createRuntimeSession: vi.fn(),
  deleteRuntimeSession: vi.fn(),
  runnableExperts: vi.fn(),
  keepaliveRuntime: vi.fn(),
  listScheduledTasks: vi.fn(),
  personalSkillAssets: vi.fn(),
  prewarmRuntimeSession: vi.fn(),
  reconnectChatRun: vi.fn(),
  renameRuntimeSession: vi.fn(),
  reportChatClientTiming: vi.fn(),
  runActivity: vi.fn(),
  runtimeModels: vi.fn(),
  runtimeSessions: vi.fn(),
  runtimeWorkspace: vi.fn(),
  setSessionModel: vi.fn(),
  stopChatRun: vi.fn(),
  streamBackgroundCollaborations: vi.fn(),
  streamChatRun: vi.fn(),
  submitChatCorrection: vi.fn(),
  warmupRuntime: vi.fn(),
  applicationAgentRuntimeEdgeHistory: vi.fn(),
  applicationAgentRuntimeEdgeQueueSummary: vi.fn(),
  applicationAgentChatFiles: vi.fn(),
  applicationAgentChatFileMetadata: vi.fn(),
  applicationAgentDownloadChatFile: vi.fn(),
  applicationAgentRenameChatFile: vi.fn(),
  applicationAgentDeleteChatFile: vi.fn(),
  applicationAgentUploadChatAttachment: vi.fn(),
  applicationAgentStudioHistory: vi.fn(),
  applicationAgentStudioSessions: vi.fn(),
  applicationAgentStudioWorkspace: vi.fn(),
  createApplicationAgentStudioSession: vi.fn(),
  deleteApplicationAgentStudioSession: vi.fn(),
  keepaliveApplicationAgentStudio: vi.fn(),
  prewarmApplicationAgentRuntimeEdge: vi.fn(),
  reconnectApplicationAgentRuntimeEdge: vi.fn(),
  renameApplicationAgentStudioSession: vi.fn(),
  stopApplicationAgentRuntimeEdge: vi.fn(),
  streamApplicationAgentRuntimeEdge: vi.fn(),
  submitApplicationAgentRuntimeEdgeCorrection: vi.fn(),
  warmupApplicationAgentStudio: vi.fn(),
}));

vi.mock("../api/client", () => ({ userApi: api }));

import { AgentWorkspace, type AgentWorkspaceProps } from "./AgentWorkspace";


type TestEvent = {
  event: string;
  data: Record<string, unknown>;
};

type Context = NonNullable<AgentWorkspaceProps["context"]>;

const personalContext: Context = { kind: "personal" };
const applicationContext: Context = {
  kind: "application",
  applicationAgentId: 31,
};

const personalSession = {
  sessionId: 1,
  runtimeWorkspaceId: 1,
  runtimeSessionId: "personal-session-1",
  title: "个人会话",
  sourceType: "web",
  messageCount: 0,
  status: "active",
};

const applicationSession = {
  sessionId: 31,
  runtimeWorkspaceId: 31,
  runtimeSessionId: "application-session-31",
  title: "应用调试会话",
  sourceType: "web",
  messageCount: 0,
  status: "active",
};

function authoritativeMessages(clientMessageIds: string[]) {
  return [
    { role: "user", content: "执行原任务" },
    { role: "assistant", content: "父任务前半段" },
    ...["追问 A", "追问 B", "追问 C"].map((content, index) => ({
      role: "user",
      content,
      client_message_id: clientMessageIds[index],
      redirect_id: `redirect-${index + 1}`,
      parent_run_id: "parent-run-1",
      dispatch_group_id: "followup-group-1",
      dispatch_group_position: index,
      redirect_state: "CANONICAL_CONSUMED",
    })),
    { role: "assistant", content: "已应用全部追问" },
  ];
}

function personalHistory(messages: Record<string, unknown>[]) {
  return {
    runtimeWorkspaceId: 1,
    runtimeSessionId: personalSession.runtimeSessionId,
    source: "hermes",
    consistency: "authoritative",
    generatedAt: "2026-08-27T00:00:00Z",
    hasActiveRun: false,
    messages,
  };
}

describe.each([
  ["个人聊天", personalContext],
  ["应用 Agent", applicationContext],
] as const)("%s 追问终态与刷新恢复", (_label, context) => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    api.runtimeModels.mockResolvedValue([]);
    api.runnableExperts.mockResolvedValue([]);
    api.personalSkillAssets.mockResolvedValue([]);
    api.listScheduledTasks.mockResolvedValue([]);
    api.chatFiles.mockResolvedValue([]);
    api.applicationAgentChatFiles.mockResolvedValue([]);
    api.activeChatRun.mockResolvedValue({ run: null });
    api.backgroundCollaborations.mockResolvedValue({
      collaborations: [],
      pendingCount: 0,
    });
    api.streamBackgroundCollaborations.mockResolvedValue(undefined);
    api.clarifyRequests.mockResolvedValue([]);
    api.reportChatClientTiming.mockResolvedValue(undefined);
    api.runActivity.mockRejectedValue(
      Object.assign(new Error("trace expired"), { status: 410 })
    );
    api.chatQueueSummary.mockResolvedValue(emptyChatQueueSnapshot());
    api.chatQueueSummaryPrepared.mockImplementation(
      (workspaceId: number, sessionId: string) =>
        api.chatQueueSummary(workspaceId, sessionId)
    );

    api.runtimeWorkspace.mockResolvedValue({
      runtimeWorkspaceId: 1,
      runtimeStatus: "ready",
    });
    api.bootstrapRuntime.mockResolvedValue({
      runtimeWorkspaceId: 1,
      runtimeStatus: "ready",
    });
    api.warmupRuntime.mockResolvedValue({
      runtimeWorkspaceId: 1,
      runtimeStatus: "ready",
    });
    api.keepaliveRuntime.mockResolvedValue(undefined);
    api.runtimeSessions.mockResolvedValue([personalSession]);
    api.prewarmRuntimeSession.mockResolvedValue({
      state: "ready",
      readyReceiptId: "personal-ready-1",
      runtimeWorkspaceId: 1,
      runtimeSessionId: personalSession.runtimeSessionId,
      modelEntryId: null,
      issuedAt: "2026-08-27T00:00:00Z",
      expiresAt: "2026-08-27T00:30:00Z",
    });
    api.chatHistory.mockResolvedValue(personalHistory([]));
    api.chatHistoryPreview.mockResolvedValue({
      ...personalHistory([]),
      source: "mysql",
      consistency: "projection_preview",
    });
    api.chatHistoryAuthoritative.mockResolvedValue(personalHistory([]));

    api.applicationAgentStudioWorkspace.mockResolvedValue({
      runtimeWorkspaceId: 31,
      runtimeStatus: "ready",
    });
    api.warmupApplicationAgentStudio.mockResolvedValue({
      runtimeWorkspaceId: 31,
      runtimeStatus: "ready",
    });
    api.keepaliveApplicationAgentStudio.mockResolvedValue({
      runtimeWorkspaceId: 31,
      runtimeStatus: "ready",
    });
    api.applicationAgentStudioSessions.mockResolvedValue([
      applicationSession,
    ]);
    api.prewarmApplicationAgentRuntimeEdge.mockResolvedValue({
      state: "ready",
      runtimeWorkspaceId: 31,
      runtimeSessionId: applicationSession.runtimeSessionId,
      modelEntryId: 0,
      readyReceiptId: null,
      issuedAt: "2026-08-27T00:00:00Z",
      expiresAt: "2026-08-27T00:30:00Z",
    });
    api.applicationAgentRuntimeEdgeHistory.mockResolvedValue([]);
    api.applicationAgentStudioHistory.mockResolvedValue({
      runtimeSessionId: applicationSession.runtimeSessionId,
      messages: [],
    });
    api.applicationAgentRuntimeEdgeQueueSummary.mockResolvedValue(
      emptyChatQueueSnapshot()
    );
  });

  it("keeps A/B/C after natural done, Authority refresh, and remount", async () => {
    let emitParent: ((event: TestEvent) => void) | undefined;
    let finishParent: (() => void) | undefined;
    const correctionClientMessageIds: string[] = [];
    const stream = vi.fn(async (
      _payload: unknown,
      onEvent: (event: TestEvent) => void
    ) => {
      emitParent = onEvent;
      onEvent({
        event: "chat.run.accepted",
        data: { runtimeRunId: "parent-run-1" },
      });
      onEvent({
        event: "chat.run.started",
        data: { runtimeRunId: "parent-run-1", seq: 1 },
      });
      onEvent({
        event: "chat.message.delta",
        data: {
          runtimeRunId: "parent-run-1",
          seq: 2,
          delta: "父任务前半段",
        },
      });
      await new Promise<void>((resolve) => {
        finishParent = resolve;
      });
    });
    const correction = vi.fn(async (
      payload: { runtimeRunId?: string },
      onEvent: (event: TestEvent) => void
    ) => {
      const index = correction.mock.calls.length;
      const clientMessageId = payload.runtimeRunId ?? `correction-${index}`;
      correctionClientMessageIds.push(clientMessageId);
      onEvent({
        event: "run.redirect_pending",
        data: {
          clientMessageId,
          parentRunId: "parent-run-1",
          eventSeq: index * 2 + 1,
        },
      });
      onEvent({
        event: "run.redirect_consumed",
        data: {
          clientMessageId,
          parentRunId: "parent-run-1",
          dispatchGroupId: "followup-group-1",
          eventSeq: index * 2 + 2,
        },
      });
      return {
        kind: "consumed",
        clientMessageId,
        parentRunId: "parent-run-1",
      };
    });
    if (context.kind === "personal") {
      api.streamChatRun.mockImplementation(stream);
      api.submitChatCorrection.mockImplementation(correction);
    } else {
      api.streamApplicationAgentRuntimeEdge.mockImplementation(
        (_agentId: number, payload: unknown, onEvent: (event: TestEvent) => void) =>
          stream(payload, onEvent)
      );
      api.submitApplicationAgentRuntimeEdgeCorrection.mockImplementation(
        (
          _agentId: number,
          payload: { runtimeRunId?: string },
          onEvent: (event: TestEvent) => void
        ) => correction(payload, onEvent)
      );
    }

    const user = userEvent.setup();
    const first = render(<AgentWorkspace context={context} />);
    const composer = await screen.findByRole("textbox", { name: "消息输入" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "执行原任务");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByText("父任务前半段")).toBeVisible();

    for (const followup of ["追问 A", "追问 B", "追问 C"]) {
      await user.type(composer, followup);
      await user.click(screen.getByRole("button", { name: "发送消息" }));
      expect(await screen.findByText(followup)).toBeVisible();
    }
    await waitFor(() => expect(correction).toHaveBeenCalledTimes(3));

    const ledgerMessages = authoritativeMessages(correctionClientMessageIds);
    api.chatHistoryAuthoritative.mockResolvedValue(
      personalHistory(ledgerMessages)
    );
    api.applicationAgentRuntimeEdgeHistory.mockResolvedValue(ledgerMessages);
    await act(async () => {
      emitParent?.({
        event: "chat.message.delta",
        data: {
          runtimeRunId: "parent-run-1",
          seq: 9,
          delta: "已应用全部追问",
        },
      });
      emitParent?.({
        event: "chat.done",
        data: {
          runtimeRunId: "parent-run-1",
          status: "completed",
          seq: 10,
        },
      });
      finishParent?.();
      await Promise.resolve();
    });

    const authority = context.kind === "personal"
      ? api.chatHistoryAuthoritative
      : api.applicationAgentRuntimeEdgeHistory;
    await waitFor(() => expect(authority.mock.calls.length).toBeGreaterThan(1));
    for (const text of ["执行原任务", "追问 A", "追问 B", "追问 C"]) {
      expect(screen.getAllByText(text)).toHaveLength(1);
    }

    first.unmount();
    render(<AgentWorkspace context={context} />);
    for (const text of ["执行原任务", "追问 A", "追问 B", "追问 C"]) {
      expect(await screen.findByText(text)).toBeVisible();
      expect(screen.getAllByText(text)).toHaveLength(1);
    }
  });

  it("shows B as processing immediately after A promotes it", async () => {
    let emitParent: ((event: TestEvent) => void) | undefined;
    let finishParent: (() => void) | undefined;
    let emitPromoted: ((event: TestEvent) => void) | undefined;
    const stream = vi.fn(async (
      _payload: unknown,
      onEvent: (event: TestEvent) => void
    ) => {
      emitParent = onEvent;
      onEvent({
        event: "chat.run.accepted",
        data: { runtimeRunId: "parent-run-1" },
      });
      onEvent({
        event: "chat.run.started",
        data: { runtimeRunId: "parent-run-1", seq: 1 },
      });
      onEvent({
        event: "chat.message.delta",
        data: {
          runtimeRunId: "parent-run-1",
          seq: 2,
          delta: "消息 A 正在回答",
        },
      });
      await new Promise<void>((resolve) => {
        finishParent = resolve;
      });
    });
    const correction = vi.fn(async (
      payload: { runtimeRunId?: string },
      onEvent: (event: TestEvent) => void
    ) => {
      const clientMessageId = payload.runtimeRunId ?? "correction-b";
      emitPromoted = onEvent;
      onEvent({
        event: "run.redirect_pending",
        data: { clientMessageId, parentRunId: "parent-run-1", eventSeq: 3 },
      });
      onEvent({
        event: "run.redirect_promoted",
        data: {
          clientMessageId,
          parentRunId: "parent-run-1",
          promotedRunId: "promoted-run-b",
          eventSeq: 4,
        },
      });
      return {
        kind: "promoted",
        clientMessageId,
        parentRunId: "parent-run-1",
        promotedRunId: "promoted-run-b",
      };
    });
    if (context.kind === "personal") {
      api.streamChatRun.mockImplementation(stream);
      api.submitChatCorrection.mockImplementation(correction);
    } else {
      api.streamApplicationAgentRuntimeEdge.mockImplementation(
        (_agentId: number, payload: unknown, onEvent: (event: TestEvent) => void) =>
          stream(payload, onEvent)
      );
      api.submitApplicationAgentRuntimeEdgeCorrection.mockImplementation(
        (
          _agentId: number,
          payload: { runtimeRunId?: string },
          onEvent: (event: TestEvent) => void
        ) => correction(payload, onEvent)
      );
    }

    const user = userEvent.setup();
    render(<AgentWorkspace context={context} />);
    const composer = await screen.findByRole("textbox", { name: "消息输入" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "消息 A");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByText("消息 A 正在回答")).toBeVisible();

    await user.type(composer, "消息 B");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByText("消息 B")).toBeVisible();
    expect(await screen.findByText("已继续")).toBeVisible();

    await act(async () => {
      emitParent?.({
        event: "chat.done",
        data: { runtimeRunId: "parent-run-1", status: "completed", seq: 5 },
      });
      finishParent?.();
      await Promise.resolve();
    });

    expect(screen.getByRole("button", { name: "停止生成" })).toBeVisible();
    expect(screen.getByText("正在思考...")).toBeVisible();

    expect(emitPromoted).toBeTypeOf("function");
  });

  it("clears processing when the parent terminal fails an unproven followup", async () => {
    let emitParent: ((event: TestEvent) => void) | undefined;
    let finishParent: (() => void) | undefined;
    let failCorrection: (() => void) | undefined;
    const stream = vi.fn(async (
      _payload: unknown,
      onEvent: (event: TestEvent) => void
    ) => {
      emitParent = onEvent;
      onEvent({
        event: "chat.run.accepted",
        data: { runtimeRunId: "parent-run-1" },
      });
      onEvent({
        event: "chat.run.started",
        data: { runtimeRunId: "parent-run-1", seq: 1 },
      });
      onEvent({
        event: "chat.message.delta",
        data: {
          runtimeRunId: "parent-run-1",
          seq: 2,
          delta: "消息 A 正在回答",
        },
      });
      await new Promise<void>((resolve) => {
        finishParent = resolve;
      });
    });
    const correction = vi.fn(async (
      payload: { runtimeRunId?: string },
      onEvent: (event: TestEvent) => void
    ) => {
      const clientMessageId = payload.runtimeRunId ?? "correction-b";
      onEvent({
        event: "run.redirect_pending",
        data: { clientMessageId, parentRunId: "parent-run-1", eventSeq: 3 },
      });
      await new Promise<void>((resolve) => {
        failCorrection = () => {
          onEvent({
            event: "followup.failed",
            data: {
              clientMessageId,
              parentRunId: "parent-run-1",
              eventSeq: 5,
              errorCode: "FOLLOWUP_PARENT_TERMINATED_BEFORE_DELIVERY",
            },
          });
          resolve();
        };
      });
      return {
        kind: "failed",
        clientMessageId,
        parentRunId: "parent-run-1",
        errorCode: "FOLLOWUP_PARENT_TERMINATED_BEFORE_DELIVERY",
      };
    });
    if (context.kind === "personal") {
      api.streamChatRun.mockImplementation(stream);
      api.submitChatCorrection.mockImplementation(correction);
    } else {
      api.streamApplicationAgentRuntimeEdge.mockImplementation(
        (_agentId: number, payload: unknown, onEvent: (event: TestEvent) => void) =>
          stream(payload, onEvent)
      );
      api.submitApplicationAgentRuntimeEdgeCorrection.mockImplementation(
        (
          _agentId: number,
          payload: { runtimeRunId?: string },
          onEvent: (event: TestEvent) => void
        ) => correction(payload, onEvent)
      );
    }

    const user = userEvent.setup();
    render(<AgentWorkspace context={context} />);
    const composer = await screen.findByRole("textbox", { name: "消息输入" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "消息 A");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByText("消息 A 正在回答")).toBeVisible();

    await user.type(composer, "消息 B");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(correction).toHaveBeenCalledOnce());

    await act(async () => {
      emitParent?.({
        event: "chat.done",
        data: { runtimeRunId: "parent-run-1", status: "completed", seq: 4 },
      });
      failCorrection?.();
      finishParent?.();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "停止生成" })).toBeNull();
      expect(screen.queryByText("正在思考...")).toBeNull();
    });
    expect(composer).toBeEnabled();
  });

  if (context.kind === "application") {
    it("uses Studio fallback without dropping projected followups", async () => {
      const messages = authoritativeMessages([
        "fallback-correction-1",
        "fallback-correction-2",
        "fallback-correction-3",
      ]);
      api.applicationAgentRuntimeEdgeHistory.mockRejectedValue(
        new Error("runtime edge disconnected")
      );
      api.applicationAgentStudioHistory.mockResolvedValue({
        runtimeSessionId: applicationSession.runtimeSessionId,
        messages,
      });

      render(<AgentWorkspace context={applicationContext} />);

      for (const text of ["执行原任务", "追问 A", "追问 B", "追问 C"]) {
        expect(await screen.findByText(text)).toBeVisible();
        expect(screen.getAllByText(text)).toHaveLength(1);
      }
      expect(api.applicationAgentStudioHistory).toHaveBeenCalledWith(
        31,
        applicationSession.runtimeSessionId
      );
    });
  }
});
