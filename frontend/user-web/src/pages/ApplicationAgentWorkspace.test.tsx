import type { ReactNode } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  applicationAgent: vi.fn(),
  runtimeModels: vi.fn(),
  userAssetCatalog: vi.fn(),
  personalSkillCategories: vi.fn(),
  expertCapabilityCatalog: vi.fn(),
  applicationAgentSkills: vi.fn(),
  uploadApplicationAgentSkill: vi.fn(),
  deleteApplicationAgentSkill: vi.fn(),
  applicationAgentPersonalKnowledge: vi.fn(),
  selectApplicationAgentPersonalKnowledge: vi.fn(),
  disableApplicationAgentPersonalKnowledge: vi.fn(),
  applicationAgentAccess: vi.fn(),
  warmupApplicationAgentStudio: vi.fn(),
  applicationAgentStudioSessions: vi.fn(),
  createApplicationAgentStudioSession: vi.fn(),
  renameApplicationAgentStudioSession: vi.fn(),
  deleteApplicationAgentStudioSession: vi.fn(),
  prewarmApplicationAgentStudioSession: vi.fn(),
  prewarmApplicationAgentRuntimeEdge: vi.fn(),
  keepaliveApplicationAgentStudio: vi.fn(),
  applicationAgentStudioHistory: vi.fn(),
  applicationAgentRuntimeEdgeQueueSummary: vi.fn(),
  applicationAgentChatFiles: vi.fn(),
  applicationAgentChatFileMetadata: vi.fn(),
  applicationAgentDownloadChatFile: vi.fn(),
  applicationAgentRenameChatFile: vi.fn(),
  applicationAgentDeleteChatFile: vi.fn(),
  applicationAgentUploadChatAttachment: vi.fn(),
  sendApplicationAgentStudioMessage: vi.fn(),
  streamApplicationAgentStudioChat: vi.fn(),
  streamApplicationAgentRuntimeEdge: vi.fn(),
  submitApplicationAgentRuntimeEdgeCorrection: vi.fn(),
  reconnectApplicationAgentRuntimeEdge: vi.fn(),
  stopApplicationAgentRuntimeEdge: vi.fn(),
  cancelApplicationAgentRuntimeEdge: vi.fn(),
  reportChatClientTiming: vi.fn(),
  updateApplicationAgent: vi.fn()
}));

vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  userApi: api
}));

// 技能面板经 React.lazy 动态加载；antd Provider 包装与页面断言无关，
// mock 掉以避免测试运行时触发 antd 的重量级 transform（antd 渲染由组件级测试覆盖）。
vi.mock("../theme/antd-module", () => ({
  AntdModuleProviders: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
// 技能面板经 React.lazy 动态加载且自身静态依赖 antd；antd 在 vitest 的 transform
// 成本（冷启动实测 15-35s）远超单测超时。beforeAll 预热模块（独立超时），
// 同 worker 内后续动态 import 直接命中缓存。
beforeAll(async () => {
  await import("../components/skills");
}, 120_000);

import { ApiError } from "../api/client";
import { AgentWorkspace } from "./AgentWorkspace";
import { ApplicationAgentConfigurationPanel } from "./ApplicationAgentConfigurationPanel";
import { chatPerformanceFlags } from "./chatPerformanceFlags";
import { emptyChatQueueSnapshot } from "./chatQueueProjection";
import { RUNTIME_EDGE_QUEUE_UPDATED_EVENT } from "../api/runtime-edge-client";

const mutableChatPerformanceFlags = chatPerformanceFlags as {
  firstVisibleImmediate: boolean;
  deltaFrameBatching: boolean;
};

function mockAnimationFrames() {
  let nextId = 1;
  const callbacks = new Map<number, FrameRequestCallback>();
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
    const id = nextId;
    nextId += 1;
    callbacks.set(id, callback);
    return id;
  });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation((id) => {
    callbacks.delete(id);
  });
  const runNext = () => {
    const next = callbacks.entries().next().value as
      | [number, FrameRequestCallback]
      | undefined;
    if (!next) return false;
    callbacks.delete(next[0]);
    act(() => next[1](performance.now()));
    return true;
  };
  return { callbacks, runNext };
}

type TestChatEvent = {
  event: string;
  data: Record<string, unknown>;
};

async function startApplicationTextStream(
  runtimeRunId: string,
  firstDelta: string
) {
  const frames = mockAnimationFrames();
  vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue(
    { length: 1 } as DOMRectList
  );
  let emit: ((event: TestChatEvent) => void) | undefined;
  let finishStream: (() => void) | undefined;
  api.streamApplicationAgentStudioChat.mockImplementation(async (
    _agentId: number,
    _payload: unknown,
    onEvent: (event: TestChatEvent) => void
  ) => {
    emit = onEvent;
    onEvent({
      event: "chat.run.accepted",
      data: { runtimeRunId }
    });
    onEvent({
      event: "chat.message.delta",
      data: { runtimeRunId, seq: 1, delta: firstDelta }
    });
    await new Promise<void>((resolve) => {
      finishStream = resolve;
    });
  });

  const user = userEvent.setup();
  const rendered = render(
    <AgentWorkspace
      context={{ kind: "application", applicationAgentId: 31 }}
    />
  );
  const composer = await screen.findByRole("textbox", {
    name: "消息输入"
  });
  await waitFor(() => expect(composer).toBeEnabled());
  await user.type(composer, "测试按帧提交");
  await user.click(screen.getByRole("button", { name: "发送消息" }));

  expect(await screen.findByText(firstDelta)).toBeVisible();
  await waitFor(() => expect(frames.callbacks.size).toBe(1));
  expect(frames.runNext()).toBe(true);
  await waitFor(() => expect(frames.callbacks.size).toBe(1));
  expect(frames.runNext()).toBe(true);
  await waitFor(() =>
    expect(api.reportChatClientTiming).toHaveBeenCalledWith(
      runtimeRunId,
      expect.objectContaining({ sampleKind: "content" })
    )
  );

  return {
    ...frames,
    ...rendered,
    emit: (event: TestChatEvent) => {
      if (!emit) throw new Error("stream event callback is not ready");
      emit(event);
    },
    finish: () => finishStream?.()
  };
}

const detail = {
  applicationAgentId: 31,
  slug: "ticket-agent",
  name: "工单助手",
  avatarUrl: null,
  status: "unpublished",
  runtimeStatus: "absent",
  configGeneration: 2,
  appliedGeneration: null,
  applyStatus: "not_applied",
  clientCount: 0,
  activeRunCount: 0,
  queuedRunCount: 0,
  personalKnowledgeEnabledCount: 0,
  personalKnowledgeRevokedCount: 0,
  publishedAt: null,
  updatedAt: null,
  lastApplyErrorCode: null,
  config: {
    systemPrompt: "",
    modelEntryId: null,
    skillAssetIds: [],
    applicationSkillIds: [],
    mcpAssetIds: [],
    enterpriseKnowledgeEnabled: false,
    memoryEnabled: true,
  }
};

describe("Application Agent workspace adapter", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mutableChatPerformanceFlags.firstVisibleImmediate = true;
    mutableChatPerformanceFlags.deltaFrameBatching = false;
    localStorage.clear();
    api.applicationAgent.mockResolvedValue(detail);
    api.applicationAgentAccess.mockResolvedValue({
      canManageApplicationAgents: true
    });
    api.warmupApplicationAgentStudio.mockResolvedValue({
      runtimeWorkspaceId: 31,
      cloudProfileId: 0,
      orgId: 1,
      userId: 1,
      name: "工单助手",
      status: "active",
      defaultModelId: null,
      runtimeStatus: "ready",
      pendingReload: false
    });
    api.runtimeModels.mockResolvedValue([]);
    api.applicationAgentChatFiles.mockResolvedValue([]);
    api.personalSkillCategories.mockResolvedValue([
      {
        categoryKey: "uncategorized",
        displayName: "未分类",
        isBuiltin: true,
        scope: "uncategorized"
      },
      {
        categoryKey: "productivity",
        displayName: "效率工具",
        isBuiltin: true,
        scope: "organization"
      }
    ]);
    api.userAssetCatalog.mockResolvedValue({
      skills: [{
        assetType: "skill",
        assetId: 7,
        currentVersionId: 17,
        desired: false,
        applied: false,
        name: "工单检索",
        description: "搜索企业工单",
        categoryKey: "productivity",
        status: "published"
      }],
      mcps: [{
        assetType: "mcp",
        assetId: 9,
        currentVersionId: 19,
        desired: false,
        applied: false,
        name: "工单系统",
        description: "连接企业工单系统",
        transport: "streamable_http",
        status: "published"
      }]
    });
    api.expertCapabilityCatalog.mockResolvedValue({
      marketSkills: [],
      installedSkills: [],
      skillCategories: [],
      marketMcps: [],
      installedMcps: [],
      knowledgeBases: [{
        assetId: 27,
        name: "企业工单知识库",
        description: "应用可用的组织知识",
        kind: "enterprise",
        source: "public"
      }],
      personalSkills: [],
      personalKnowledgeBases: [],
      pluginIds: []
    });
    api.applicationAgentSkills.mockResolvedValue([]);
    api.uploadApplicationAgentSkill.mockResolvedValue({
      applicationAgentSkillId: 41,
      applicationAgentId: 31,
      slug: "private-ticket",
      name: "私有工单",
      description: "应用独有",
      categoryKey: "personal",
      packageSha256: "a".repeat(64)
    });
    api.deleteApplicationAgentSkill.mockResolvedValue(undefined);
    api.applicationAgentPersonalKnowledge.mockResolvedValue({
      items: [{
        knowledgeBaseId: 17,
        name: "工单规范",
        description: "工单处理标准",
        ownerUserId: 1,
        ownerName: "应用管理员",
        effectiveRole: "owner",
        selected: false,
        fileCount: 3,
        readyFileCount: 3,
        visibility: "private",
        status: "active",
        createdAt: "2026-07-28T00:00:00Z",
        updatedAt: "2026-07-28T00:00:00Z"
      }],
      enabledCount: 0,
      revokedCount: 0,
      configGeneration: 2
    });
    api.selectApplicationAgentPersonalKnowledge.mockResolvedValue({
      items: [],
      enabledCount: 1,
      revokedCount: 0,
      configGeneration: 3
    });
    api.disableApplicationAgentPersonalKnowledge.mockResolvedValue({
      items: [],
      enabledCount: 0,
      revokedCount: 0,
      configGeneration: 3
    });
    api.applicationAgentStudioSessions.mockResolvedValue([{
      sessionId: 1,
      runtimeWorkspaceId: 31,
      runtimeSessionId: "studio-31-1-session",
      title: "新调试会话",
      sourceType: "web",
      messageCount: 0,
      status: "active"
    }]);
    api.createApplicationAgentStudioSession.mockResolvedValue({
      sessionId: 1,
      runtimeWorkspaceId: 31,
      runtimeSessionId: "studio-31-1-created",
      title: "新调试会话",
      sourceType: "web",
      messageCount: 0,
      status: "active"
    });
    api.prewarmApplicationAgentStudioSession.mockResolvedValue(undefined);
    api.prewarmApplicationAgentRuntimeEdge.mockResolvedValue({
      state: "ready",
      runtimeWorkspaceId: 31,
      runtimeSessionId: "studio-31-1-session",
      modelEntryId: 0,
      readyReceiptId: null,
      issuedAt: "2026-08-07T00:00:00Z",
      expiresAt: "2026-08-07T00:30:00Z"
    });
    api.keepaliveApplicationAgentStudio.mockResolvedValue({
      runtimeWorkspaceId: 31,
      runtimeStatus: "ready"
    });
    api.applicationAgentStudioHistory.mockResolvedValue({
      runtimeSessionId: "studio-31-1-existing",
      messages: []
    });
    api.applicationAgentRuntimeEdgeQueueSummary.mockResolvedValue(
      emptyChatQueueSnapshot()
    );
    api.streamApplicationAgentStudioChat.mockImplementation(
      async (
        _agentId: number,
        _payload: unknown,
        onEvent: (event: {
          event: string;
          data: Record<string, unknown>;
        }) => void
      ) => {
        onEvent({
          event: "chat.run.accepted",
          data: { runtimeRunId: "app-run-1" }
        });
        onEvent({
          event: "chat.message.delta",
          data: {
            runtimeRunId: "app-run-1",
            delta: "调试回复"
          }
        });
        onEvent({
          event: "chat.done",
          data: {
            runtimeRunId: "app-run-1",
            status: "completed"
          }
        });
      }
    );
    api.streamApplicationAgentRuntimeEdge.mockImplementation(
      (
        agentId: number,
        payload: unknown,
        onEvent: (event: TestChatEvent) => void,
        signal?: AbortSignal
      ) => api.streamApplicationAgentStudioChat(
        agentId,
        payload,
        onEvent,
        signal
      )
    );
    api.submitApplicationAgentRuntimeEdgeCorrection.mockResolvedValue({
      kind: "consumed",
      clientMessageId: "correction",
      parentRunId: "app-run-1"
    });
    api.stopApplicationAgentRuntimeEdge.mockResolvedValue({
      runtimeWorkspaceId: 31,
      runtimeSessionId: "studio-31-1-session",
      runtimeRunId: "app-parent-run",
      status: "canceling"
    });
    api.reportChatClientTiming.mockResolvedValue(undefined);
    api.updateApplicationAgent.mockImplementation(
      async (_agentId: number, payload: { config: typeof detail.config }) => ({
        ...detail,
        configGeneration: 3,
        config: payload.config
      })
    );
  });

  it("uses AgentWorkspace application context for chat and capability binding", async () => {
    const user = userEvent.setup();
    render(
      <AgentWorkspace
        context={{ kind: "application", applicationAgentId: 31 }}
      />
    );

    await waitFor(() =>
      expect(api.warmupApplicationAgentStudio).toHaveBeenCalledTimes(1)
    );
    expect(screen.queryByRole("button", { name: "专家" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "定时任务" })).not.toBeInTheDocument();

    const composer = screen.getByRole("textbox", { name: "消息输入" });
    await waitFor(() => expect(composer).toBeEnabled());
    expect(screen.queryByRole("button", { name: "模型" }))
      .not.toBeInTheDocument();
    expect(api.prewarmApplicationAgentStudioSession).not.toHaveBeenCalled();
    expect(api.prewarmApplicationAgentRuntimeEdge).toHaveBeenCalledWith(
      31,
      31,
      "studio-31-1-session"
    );
    expect((await screen.findAllByText("新调试会话")).length).toBeGreaterThan(0);
    const feed = document.querySelector<HTMLElement>(".messageFeed");
    expect(feed).not.toBeNull();
    Object.defineProperty(feed!, "clientHeight", {
      configurable: true,
      value: 100
    });
    await user.type(
      composer,
      "帮我检查工单"
    );
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() =>
      expect(api.streamApplicationAgentRuntimeEdge).toHaveBeenCalled()
    );
    expect(await screen.findByText("调试回复")).toBeVisible();
    expect(document.querySelector(".turnAnchorSpacer")).toHaveStyle({ height: "100px" });
    expect(api.streamApplicationAgentRuntimeEdge).toHaveBeenCalledWith(
      31,
      expect.objectContaining({
        message: "帮我检查工单",
        runtimeWorkspaceId: 31,
        runtimeSessionId: "studio-31-1-session",
        requestId: expect.stringMatching(/^web-[a-z0-9-]+$/)
      }),
      expect.any(Function),
      expect.any(AbortSignal)
    );

    await user.click(screen.getByRole("button", { name: "技能" }));
    await waitFor(() => expect(api.userAssetCatalog).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("tab", { name: /专家市场/ })).toBeVisible();
    expect(screen.getByRole("tab", { name: /^我的 \d/ })).toBeVisible();
    // 上传入口现分页头主操作与空态引导两处（2026-10-24 视觉优化），取第一处
    expect((await screen.findAllByRole("button", { name: /上传技能/ }))[0]).toBeVisible();
    await user.click(screen.getAllByRole("button", { name: /上传技能/ })[0]);
    await waitFor(() =>
      expect(screen.getByRole("dialog", { name: "上传技能配置" })).toBeVisible()
    );
    const uploadDialog = screen.getByRole("dialog", {
      name: "上传技能配置"
    });
    // antd Select 初始值保持为空，先显示搜索 placeholder；真实分类选项在下拉中断言。
    expect(within(uploadDialog).getByText("搜索或创建新分类")).toBeVisible();
    await user.click(within(uploadDialog).getByRole("combobox", { name: "技能分类" }));
    const uploadDropdown = await waitFor(() => {
      const el = document.querySelector(".ant-select-dropdown:not(.ant-select-dropdown-hidden)");
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    await waitFor(() => expect(within(uploadDropdown).getByText("未分类")).toBeVisible());
    // 经「取消」关闭上传弹窗（与 X 等价的关闭路径；fireEvent 绕开动画期可见性竞态）
    fireEvent.click(document.querySelector(".ant-modal-footer button") as HTMLElement);
    await user.click(screen.getByRole("tab", { name: /专家市场/ }));
    expect(await screen.findByText("工单检索")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "安装" }));
    await waitFor(() =>
      expect(api.updateApplicationAgent).toHaveBeenCalledWith(
        31,
        expect.objectContaining({
          expectedConfigGeneration: 2,
          config: expect.objectContaining({ skillAssetIds: [7] })
        })
      )
    );

    await user.click(screen.getByRole("button", { name: "MCP" }));
    const mcpCard = await screen.findByRole("button", {
      name: /工单系统/
    });
    expect(mcpCard).toBeVisible();
    await user.click(mcpCard);
    const mcpDialog = await screen.findByRole("dialog", {
      name: "工单系统 详情"
    });
    // antd Modal 外壳在 jsdom 中 rc-motion appear 需异步完成（prepare 阶段 opacity:0），
    // 与 ExpertCatalogPanels.test 同款 waitFor 等待模式。
    await waitFor(() => expect(mcpDialog).toBeVisible());
    await user.click(screen.getByRole("button", { name: "关闭" }));

    await user.click(screen.getByRole("button", { name: "知识库" }));
    expect(await screen.findByRole("tab", {
      name: "企业知识库"
    })).toBeVisible();
    expect(screen.queryByRole("tab", {
      name: "个人知识库"
    })).toBeVisible();
    expect(screen.getByText("只影响当前应用 Agent 的企业知识检索。")).toBeVisible();
    await user.click(screen.getByRole("switch", {
      name: "用于应用 Agent 检索"
    }));
    await waitFor(() =>
      expect(api.updateApplicationAgent).toHaveBeenCalledWith(
        31,
        expect.objectContaining({
          expectedConfigGeneration: 3,
          config: expect.objectContaining({
            enterpriseKnowledgeEnabled: true
          })
        })
      )
    );
  }, 20_000);

  it("reconnects an active Application Agent Run from the Runtime queue snapshot", async () => {
    api.applicationAgentRuntimeEdgeQueueSummary.mockResolvedValue({
      queuedCount: 0,
      scheduledQueuedCount: 0,
      queuedRuns: [],
      activeRun: {
        runId: "app-reenter-run",
        state: "RUNNING",
        cancellationRequestedAt: null
      }
    });
    api.reconnectApplicationAgentRuntimeEdge.mockImplementation(
      async (
        _agentId: number,
        _payload: unknown,
        onEvent: (event: TestChatEvent) => void
      ) => {
        onEvent({
          event: "chat.run.accepted",
          data: { runtimeRunId: "app-reenter-run" }
        });
        onEvent({
          event: "chat.message.delta",
          data: {
            runtimeRunId: "app-reenter-run",
            seq: 1,
            delta: "恢复中的回答"
          }
        });
        onEvent({
          event: "chat.done",
          data: {
            runtimeRunId: "app-reenter-run",
            status: "completed",
            seq: 2
          }
        });
      }
    );

    render(
      <AgentWorkspace
        context={{ kind: "application", applicationAgentId: 31 }}
      />
    );

    await waitFor(() => expect(
      api.reconnectApplicationAgentRuntimeEdge
    ).toHaveBeenCalled());
    expect(api.reconnectApplicationAgentRuntimeEdge).toHaveBeenCalledWith(
      31,
      expect.objectContaining({
        runtimeWorkspaceId: 31,
        runtimeSessionId: "studio-31-1-session",
        runtimeRunId: "app-reenter-run",
        afterSeq: 0
      }),
      expect.any(Function),
      expect.any(AbortSignal)
    );
  });

  it("stages Studio attachments locally and uploads them on message send", async () => {
    api.applicationAgentUploadChatAttachment.mockResolvedValue({
      file: {
        fileId: 91,
        displayName: "ticket.csv",
        relativePath: "workspace/sessions/studio-31-1-session/91/ticket.csv",
        sourceType: "upload",
        sizeBytes: 12
      }
    });
    api.streamApplicationAgentRuntimeEdge.mockImplementation(async (
      _agentId: number,
      payload: Record<string, unknown>,
      onEvent: (event: TestChatEvent) => void
    ) => {
      expect(payload.attachmentFileIds).toEqual([91]);
      onEvent({ event: "chat.run.accepted", data: { runtimeRunId: "app-upload-run" } });
      onEvent({ event: "chat.done", data: { runtimeRunId: "app-upload-run" } });
    });
    const { container } = render(
      <AgentWorkspace context={{ kind: "application", applicationAgentId: 31 }} />
    );
    const composer = await screen.findByRole("textbox", { name: "消息输入" });
    await waitFor(() => expect(composer).toBeEnabled());
    const input = container.querySelector<HTMLInputElement>('section.chatLayout > input[type="file"]');
    const dropTarget = container.querySelector<HTMLElement>("section.chatLayout");
    expect(input).not.toBeNull();
    const user = userEvent.setup();
    const attachment = new File(["ticket"], "ticket.csv", { type: "text/csv" });
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

    const queue = await screen.findByLabelText("本次消息附件");
    expect(within(queue).getByText("ticket.csv")).toBeVisible();
    expect(api.applicationAgentUploadChatAttachment).not.toHaveBeenCalled();
    await user.type(composer, "分析这个文件");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    await waitFor(() =>
      expect(api.applicationAgentUploadChatAttachment).toHaveBeenCalledTimes(1)
    );
    await waitFor(() =>
      expect(api.streamApplicationAgentRuntimeEdge).toHaveBeenCalledTimes(1)
    );
  });

  it("disables Office preview for Application Studio files while keeping download available", async () => {
    api.applicationAgentChatFiles.mockResolvedValue([{
      fileId: 92,
      displayName: "季度报告.docx",
      relativePath: "workspace/sessions/studio-31-1-session/92/季度报告.docx",
      sourceType: "runtime",
      mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      sizeBytes: 128
    }]);
    const user = userEvent.setup();
    render(
      <AgentWorkspace context={{ kind: "application", applicationAgentId: 31 }} />
    );
    const composer = await screen.findByRole("textbox", { name: "消息输入" });
    await waitFor(() => expect(composer).toBeEnabled());
    await waitFor(() => expect(api.applicationAgentChatFiles).toHaveBeenCalled());

    await screen.findByText("季度报告.docx");
    await user.click(screen.getByRole("button", { name: "预览 季度报告.docx" }));

    const dialog = screen.getByRole("dialog", { name: "季度报告.docx" });
    expect(
      within(dialog).getByText("该格式暂不支持在线预览")
    ).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "下载 季度报告.docx" }))
      .toBeVisible();
    expect(api.applicationAgentDownloadChatFile).not.toHaveBeenCalled();
  });

  it("shows OpenAPI conversations as immutable history for the creator", async () => {
    api.applicationAgentStudioSessions.mockResolvedValue([{
      sessionId: 0,
      runtimeWorkspaceId: 31,
      runtimeSessionId: "app-31-openapi",
      title: "外部用户 · customer-1001",
      sourceType: "openapi",
      messageCount: 2,
      status: "active",
      isReadOnly: true,
      isImmutable: true,
      originLabel: "API · 工单门户"
    }]);
    api.applicationAgentStudioHistory.mockResolvedValue({
      runtimeSessionId: "app-31-openapi",
      messages: [
        { role: "user", content: "API 问题" },
        { role: "assistant", content: "API 回复" }
      ]
    });
    api.applicationAgentChatFiles.mockRejectedValue(
      new Error("application_agent_studio_session_not_found")
    );

    render(
      <AgentWorkspace
        context={{ kind: "application", applicationAgentId: 31 }}
      />
    );

    expect((await screen.findAllByText(
      "外部用户 · customer-1001"
    )).length).toBeGreaterThan(0);
    expect(await screen.findByText("API 回复")).toBeVisible();
    expect(screen.getAllByText(
      "API · 工单门户 · 固定会话"
    ).length).toBeGreaterThan(0);
    expect(screen.getByText(
      "API 接入会话仅用于查看历史，请在调用方系统中继续对话。"
    )).toBeVisible();
    expect(screen.getByRole("textbox", { name: "消息输入" })).toBeDisabled();
    expect(api.prewarmApplicationAgentRuntimeEdge).not.toHaveBeenCalled();
  });

  it("reports Application Studio browser paint timing for the accepted run", async () => {
    const frames: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      frames.push(callback);
      return frames.length;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(
      () => undefined
    );
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue(
      { length: 1 } as DOMRectList
    );
    let finishStream: (() => void) | undefined;
    api.streamApplicationAgentStudioChat.mockImplementation(async (
      _agentId: number,
      _payload: unknown,
      onEvent: (event: {
        event: string;
        data: Record<string, unknown>;
      }) => void
    ) => {
      onEvent({
        event: "chat.run.accepted",
        data: { runtimeRunId: "app_run_browser_timing" }
      });
      onEvent({
        event: "chat.message.delta",
        data: {
          runtimeRunId: "app_run_browser_timing",
          delta: "应用首个可见输出"
        }
      });
      await new Promise<void>((resolve) => {
        finishStream = resolve;
      });
      onEvent({
        event: "chat.done",
        data: {
          runtimeRunId: "app_run_browser_timing",
          status: "completed"
        }
      });
    });

    render(
      <AgentWorkspace
        context={{ kind: "application", applicationAgentId: 31 }}
      />
    );
    const user = userEvent.setup();
    const composer = await screen.findByRole("textbox", {
      name: "消息输入"
    });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "测量应用 TTFT");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("应用首个可见输出")).toBeVisible();
    await waitFor(() => expect(frames).toHaveLength(1));
    act(() => frames.shift()?.(performance.now()));
    await waitFor(() => expect(frames).toHaveLength(1));
    act(() => frames.shift()?.(performance.now()));

    await waitFor(() =>
      expect(api.reportChatClientTiming).toHaveBeenCalledWith(
        "app_run_browser_timing",
        expect.objectContaining({
          sampleKind: "content",
          documentVisibility: "visible",
          sendOverlappedPrewarm: false,
          requestToFirstVisiblePaintMs: expect.any(Number)
        })
      )
    );
    expect(api.streamApplicationAgentStudioChat).toHaveBeenCalledWith(
      31,
      expect.objectContaining({
        requestId: expect.stringMatching(/^web-[a-z0-9-]+$/)
      }),
      expect.any(Function),
      expect.any(AbortSignal)
    );
    await act(async () => {
      finishStream?.();
    });
  });

  it("commits subsequent text deltas immediately when frame batching is disabled", async () => {
    const stream = await startApplicationTextStream(
      "app_run_immediate_commit",
      "第一段"
    );

    act(() => {
      stream.emit({
        event: "chat.message.delta",
        data: { runtimeRunId: "app_run_immediate_commit", seq: 2, delta: "第二段" }
      });
    });

    expect(screen.getByText("第一段第二段")).toBeVisible();
    expect(stream.callbacks.size).toBe(0);

    await act(async () => {
      stream.emit({
        event: "chat.done",
        data: {
          runtimeRunId: "app_run_immediate_commit",
          seq: 3,
          status: "completed"
        }
      });
      stream.finish();
    });
  });

  it("commits subsequent text deltas once on the next animation frame when enabled", async () => {
    mutableChatPerformanceFlags.deltaFrameBatching = true;
    const stream = await startApplicationTextStream(
      "app_run_frame_commit",
      "第一段"
    );
    expect(stream.callbacks.size).toBe(0);

    act(() => {
      stream.emit({
        event: "chat.message.delta",
        data: { runtimeRunId: "app_run_frame_commit", seq: 2, delta: "第二段" }
      });
      stream.emit({
        event: "chat.message.delta",
        data: { runtimeRunId: "app_run_frame_commit", seq: 3, delta: "第三段" }
      });
    });

    expect(screen.getByText("第一段")).toBeVisible();
    expect(screen.queryByText("第一段第二段第三段")).not.toBeInTheDocument();
    expect(stream.callbacks.size).toBe(1);
    expect(stream.runNext()).toBe(true);
    expect(await screen.findByText("第一段第二段第三段")).toBeVisible();
    expect(stream.callbacks.size).toBe(0);

    await act(async () => {
      stream.emit({
        event: "chat.done",
        data: {
          runtimeRunId: "app_run_frame_commit",
          seq: 4,
          status: "completed"
        }
      });
      stream.finish();
    });
  });

  it("commits a terminal event immediately and cancels its pending text frame", async () => {
    mutableChatPerformanceFlags.deltaFrameBatching = true;
    const stream = await startApplicationTextStream(
      "app_run_frame_terminal",
      "已有内容"
    );

    act(() => {
      stream.emit({
        event: "chat.message.delta",
        data: {
          runtimeRunId: "app_run_frame_terminal",
          seq: 2,
          delta: "，尚未绘制"
        }
      });
    });
    expect(stream.callbacks.size).toBe(1);

    act(() => {
      stream.emit({
        event: "chat.error",
        data: {
          runtimeRunId: "app_run_frame_terminal",
          seq: 3,
          message: "生成失败"
        }
      });
    });

    expect(await screen.findByText("已有内容，尚未绘制")).toBeVisible();
    expect(screen.getByText("生成失败")).toBeVisible();
    expect(stream.callbacks.size).toBe(0);

    await act(async () => {
      stream.finish();
    });
  });

  it("cancels a pending text commit frame when the workspace unmounts", async () => {
    mutableChatPerformanceFlags.deltaFrameBatching = true;
    const stream = await startApplicationTextStream(
      "app_run_frame_unmount",
      "首段"
    );

    act(() => {
      stream.emit({
        event: "chat.message.delta",
        data: {
          runtimeRunId: "app_run_frame_unmount",
          seq: 2,
          delta: "待提交"
        }
      });
    });
    expect(stream.callbacks.size).toBe(1);

    stream.unmount();
    expect(stream.callbacks.size).toBe(0);
    await act(async () => {
      stream.finish();
    });
  });

  it("keeps the busy state on only the selected public skill", async () => {
    const user = userEvent.setup();
    api.userAssetCatalog.mockResolvedValue({
      skills: [
        {
          assetType: "skill",
          assetId: 7,
          currentVersionId: 17,
          desired: false,
          applied: false,
          name: "工单检索",
          description: "搜索企业工单",
          categoryKey: "productivity",
          status: "published"
        },
        {
          assetType: "skill",
          assetId: 8,
          currentVersionId: 18,
          desired: false,
          applied: false,
          name: "日志分析",
          description: "分析运行日志",
          categoryKey: "productivity",
          status: "published"
        }
      ],
      mcps: []
    });
    api.updateApplicationAgent.mockImplementation(
      () => new Promise(() => undefined)
    );

    render(
      <ApplicationAgentConfigurationPanel
        applicationAgentId={31}
        section="skills"
      />
    );

    await user.click(await screen.findByRole("tab", { name: /专家市场/ }));
    const firstCard = (await screen.findByText("工单检索")).closest(
      ".skillCatalogCard"
    );
    const secondCard = screen.getByText("日志分析").closest(".skillCatalogCard");
    expect(firstCard).not.toBeNull();
    expect(secondCard).not.toBeNull();
    await user.click(
      within(firstCard as HTMLElement).getByRole("button", {
        name: "安装"
      })
    );

    await waitFor(() =>
      expect(
        within(firstCard as HTMLElement).getByRole("button", {
          name: "处理中"
        })
      ).toBeDisabled()
    );
    expect(
      within(secondCard as HTMLElement).getByRole("button", {
        name: "安装"
      })
    ).toBeEnabled();
  });

  it("exposes explicit personal knowledge selection without asset management", async () => {
    const user = userEvent.setup();
    api.applicationAgentPersonalKnowledge.mockResolvedValueOnce({
      items: [{
        knowledgeBaseId: 17,
        name: "新共享规范",
        description: "刚共享给创建者",
        ownerUserId: 2,
        ownerName: "同事",
        effectiveRole: "retrieve",
        selected: false,
        fileCount: 1,
        readyFileCount: 1,
        visibility: "shared",
        status: "active",
        createdAt: "2026-09-01T00:00:00Z",
        updatedAt: "2026-09-01T00:00:00Z"
      }],
      enabledCount: 0,
      revokedCount: 1,
      configGeneration: 2
    });
    render(
      <ApplicationAgentConfigurationPanel
        applicationAgentId={31}
        section="knowledge"
      />
    );

    expect(await screen.findByRole("tab", {
      name: "个人知识库"
    })).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "个人知识库" }));
    expect(screen.queryByRole("switch", {
      name: "跟随调用用户动态授权个人知识库"
    })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /新建知识库/ }))
      .not.toBeInTheDocument();
    expect(screen.getByText(/逐库选择/)).toBeVisible();
    // T3 起「用于对话检索」勾选迁移为 antd Switch（button role=switch，状态走 aria-checked）。
    expect(screen.getByRole("switch", {
      name: "用于对话检索 新共享规范"
    })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText(/有 1 个已选择的个人知识库因权限撤销/))
      .toBeVisible();
    expect(api.expertCapabilityCatalog).not.toHaveBeenCalled();
  });

  it("keeps the only system prompt editor in Agent 配置", async () => {
    const user = userEvent.setup();
    render(
      <ApplicationAgentConfigurationPanel
        applicationAgentId={31}
        section="settings"
      />
    );

    expect(await screen.findByRole("heading", {
      name: "Agent 配置"
    })).toBeVisible();
    const prompt = screen.getByLabelText("系统提示词");
    await user.type(prompt, "只处理工单");
    expect(api.updateApplicationAgent).not.toHaveBeenCalled();
    expect(screen.getByText("有未保存修改")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "保存并应用" }));

    await waitFor(() =>
      expect(api.updateApplicationAgent).toHaveBeenCalledWith(
        31,
        expect.not.objectContaining({ description: expect.anything() })
      )
    );
  });

  it("uses the semantic market Skill version instead of the database version id", async () => {
    api.userAssetCatalog.mockResolvedValueOnce({
      skills: [{
        assetType: "skill",
        assetId: 7,
        currentVersionId: 1707,
        version: 2,
        desired: false,
        applied: false,
        name: "版本校验技能",
        description: "检查版本展示",
        categoryKey: "productivity",
        status: "published"
      }],
      mcps: []
    });
    const user = userEvent.setup();

    render(
      <ApplicationAgentConfigurationPanel
        applicationAgentId={31}
        section="skills"
      />
    );

    await user.click(await screen.findByRole("tab", { name: /专家市场/ }));
    const card = (await screen.findByText("版本校验技能")).closest(".skillCatalogCard");
    expect(card).not.toBeNull();
    expect(within(card as HTMLElement).getByText("v2")).toBeVisible();
    expect(within(card as HTMLElement).getByText("效率工具")).toBeVisible();
    expect(within(card as HTMLElement).queryByText(/v1707/)).not.toBeInTheDocument();
  });

  it("binds a market MCP through the application Agent configuration contract", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    api.updateApplicationAgent.mockResolvedValue({
      ...detail,
      configGeneration: 3,
      config: { ...detail.config, mcpAssetIds: [9] }
    });
    const user = userEvent.setup();

    render(
      <ApplicationAgentConfigurationPanel
        applicationAgentId={31}
        section="mcps"
      />
    );

    await user.click(await screen.findByRole("button", { name: "安装" }));
    await waitFor(() => expect(api.updateApplicationAgent).toHaveBeenCalledWith(
      31,
      expect.objectContaining({
        expectedConfigGeneration: 2,
        config: expect.objectContaining({ mcpAssetIds: [9] })
      })
    ));
    confirm.mockRestore();
  });

  it("keeps a transient application Runtime busy state out of the page error", async () => {
    vi.useFakeTimers();
    api.warmupApplicationAgentStudio
      .mockRejectedValueOnce(new ApiError(
        "application_agent_runtime_busy",
        409,
        "application_agent_runtime_busy"
      ))
      .mockResolvedValue({
        runtimeWorkspaceId: 31,
        cloudProfileId: 0,
        orgId: 1,
        userId: 1,
        name: "工单助手",
        status: "active",
        defaultModelId: null,
        runtimeStatus: "ready",
        pendingReload: false
      });
    try {
      render(
        <AgentWorkspace
          context={{ kind: "application", applicationAgentId: 31 }}
        />
      );
      await act(async () => {
        await Promise.resolve();
      });
      expect(api.warmupApplicationAgentStudio).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });

      expect(api.warmupApplicationAgentStudio).toHaveBeenCalledTimes(2);
      expect(screen.queryByText(
        "application_agent_runtime_busy"
      )).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("adopts an applied model save after Runtime busy without a second PUT", async () => {
    const nextConfig = {
      ...detail.config,
      systemPrompt: "只处理工单"
    };
    api.updateApplicationAgent.mockRejectedValue(new ApiError(
      "application_agent_runtime_busy",
      409,
      "application_agent_runtime_busy"
    ));
    api.applicationAgent
      .mockResolvedValueOnce(detail)
      .mockResolvedValueOnce({
        ...detail,
        runtimeStatus: "ready",
        configGeneration: 3,
        appliedGeneration: 3,
        applyStatus: "applied",
        config: nextConfig
      });
    const user = userEvent.setup();

    const view = render(
      <ApplicationAgentConfigurationPanel
        applicationAgentId={31}
        section="settings"
      />
    );

    const prompt = await screen.findByLabelText("系统提示词");
    await user.type(prompt, "只处理工单");
    await user.click(screen.getByRole("button", { name: "保存并应用" }));

    expect(await screen.findByText("已保存并应用")).toBeVisible();
    // Settings 操作完成后，顶层横幅不再显示冗余的"配置 #N 已保存"；
    // 成功反馈由 Settings 表单内的状态条承接。
    expect(screen.queryByText(/配置 #\d+ 已保存/)).not.toBeInTheDocument();
    expect(
      screen.getByText("已保存并应用", { selector: "span" }).closest(
        ".applicationAgentConfigStatus"
      )
    ).not.toBeNull();
    await act(async () => {
      view.rerender(
        <ApplicationAgentConfigurationPanel applicationAgentId={31} section="mcps" />
      );
    });
    expect(screen.queryByText("已保存并应用")).not.toBeInTheDocument();
    expect(api.updateApplicationAgent).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(
      "application_agent_runtime_busy"
    )).not.toBeInTheDocument();
  });

  it("steers a mid-turn follow-up without replacing the parent stream or Stop", async () => {
    let parentSignal: AbortSignal | undefined;
    let finishParent: (() => void) | undefined;
    let emitParent: ((event: TestChatEvent) => void) | undefined;
    api.streamApplicationAgentRuntimeEdge.mockImplementation(async (
      _agentId: number,
      payload: { message?: string },
      onEvent: (event: TestChatEvent) => void,
      signal?: AbortSignal
    ) => {
      parentSignal = signal;
      emitParent = onEvent;
      onEvent({
        event: "chat.run.accepted",
        data: { runtimeRunId: "app-parent-run" }
      });
      onEvent({
        event: "chat.run.started",
        data: { runtimeRunId: "app-parent-run", seq: 1 }
      });
      onEvent({
        event: "chat.message.delta",
        data: {
          runtimeRunId: "app-parent-run",
          seq: 2,
          delta: "父任务仍在输出"
        }
      });
      await new Promise<void>((resolve) => {
        finishParent = resolve;
      });
    });
    api.submitApplicationAgentRuntimeEdgeCorrection.mockImplementation(async (
      _agentId,
      payload,
      onEvent
    ) => {
      const clientMessageId = payload.runtimeRunId ?? "followup-client";
      onEvent({
        event: "run.redirect_pending",
        data: {
          clientMessageId,
          parentRunId: "app-parent-run",
          eventSeq: 3
        }
      });
      onEvent({
        event: "run.redirect_consumed",
        data: {
          clientMessageId,
          parentRunId: "app-parent-run",
          dispatchGroupId: "followup-group-1",
          eventSeq: 4
        }
      });
      return {
        kind: "consumed",
        clientMessageId,
        parentRunId: "app-parent-run"
      };
    });

    const user = userEvent.setup();
    const { container } = render(
      <AgentWorkspace
        context={{ kind: "application", applicationAgentId: 31 }}
      />
    );
    const composer = await screen.findByRole("textbox", {
      name: "消息输入"
    });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "执行原任务");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByText("父任务仍在输出")).toBeVisible();

    api.applicationAgentRuntimeEdgeQueueSummary.mockResolvedValue({
      queuedCount: 1,
      scheduledQueuedCount: 0,
      queuedRuns: [{
        runId: "app-queued-run",
        clientMessageId: "app-queued-client-message",
        enqueueSeq: 2,
        prompt: "先检查最新日志",
        attachmentFileIds: []
      }],
      activeRun: {
        runId: "app-parent-run",
        state: "RUNNING",
        cancellationRequestedAt: null
      }
    });
    await user.type(composer, "先检查最新日志");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(await screen.findByText("先检查最新日志")).toBeVisible();
    expect(api.submitApplicationAgentRuntimeEdgeCorrection).toHaveBeenCalledWith(
      31,
      expect.objectContaining({
        message: "先检查最新日志",
        parentRuntimeRunId: "app-parent-run",
        runtimeRunId: expect.stringMatching(/^web-/)
      }),
      expect.any(Function),
      expect.any(AbortSignal)
    );
    expect(api.streamApplicationAgentRuntimeEdge).toHaveBeenCalledTimes(1);
    expect(parentSignal?.aborted).toBe(false);

    await act(async () => {
      emitParent?.({
        event: "chat.message.delta",
        data: {
          runtimeRunId: "app-parent-run",
          seq: 5,
          delta: "已检查最新日志"
        }
      });
      await Promise.resolve();
    });
    expect(await screen.findByText("已检查最新日志")).toBeVisible();
    expect(container.querySelectorAll('.message[data-role="assistant"]'))
      .toHaveLength(2);

    await user.click(screen.getByRole("button", { name: "停止生成" }));
    await waitFor(() => expect(
      api.stopApplicationAgentRuntimeEdge
    ).toHaveBeenCalledWith(31, expect.objectContaining({
      runtimeRunId: "app-parent-run"
    })));
    expect(parentSignal?.aborted).toBe(false);

    await act(async () => {
      emitParent?.({
        event: "chat.done",
        data: {
          runtimeRunId: "app-parent-run",
          status: "canceled",
          seq: 3
        }
      });
      finishParent?.();
      await Promise.resolve();
    });
    expect(screen.queryByRole("status", { name: "等待执行的消息" }))
      .not.toBeInTheDocument();
    expect(container.querySelectorAll('.message[data-role="assistant"]'))
      .toHaveLength(2);
  });
});
