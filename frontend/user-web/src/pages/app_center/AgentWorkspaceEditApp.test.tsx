import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { configure } from "@testing-library/react";

import { emptyChatQueueSnapshot } from "../chatQueueProjection";
import { UserChatPage } from "../UserChatPage";

const api = vi.hoisted(() => ({
  bootstrapRuntime: vi.fn(),
  runnableExperts: vi.fn(),
  runtimeModels: vi.fn(),
  runtimeSessions: vi.fn(),
  runtimeSession: vi.fn(),
  createRuntimeSession: vi.fn(),
  chatHistoryPreview: vi.fn(),
  chatHistoryAuthoritative: vi.fn(),
  chatFiles: vi.fn(),
  chatQueueSummary: vi.fn(),
  chatQueueSummaryPrepared: vi.fn(),
  activeChatRun: vi.fn(),
  downloadAppPackage: vi.fn(),
  listApps: vi.fn(),
  taskPromptConfig: vi.fn(),
  organizationDirectoryTree: vi.fn(),
  prewarmRuntimeSession: vi.fn(),
  reportClientTiming: vi.fn(),
  reportChatClientTiming: vi.fn(),
  runActivity: vi.fn()
}));

vi.mock("../../api/client", () => ({ userApi: api }));

// 看板区家族经 React.lazy 动态加载（index.ts 引 AppCenterPanelView →
// AntdModuleProviders → antd）；mock Provider + 预热，同 AgentWorkspaceHtmlPreview。
vi.mock("../../theme/antd-module", () => ({
  AntdModuleProviders: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
// 同 HtmlPreview：并行争抢下放宽异步查询窗口，避免懒加载链击穿全局 5s。
configure({ asyncUtilTimeout: 10_000 });
beforeAll(async () => {
  await import("../../components/app_center");
}, 120_000);

const CURRENT_SESSION = {
  runtimeSessionId: "rs-current",
  title: "当前任务",
  messageCount: 0,
  sourceType: "web",
  sessionModelEntryId: 19,
  status: "active"
} as const;

const SOURCE_SESSION = {
  runtimeSessionId: "rs-source",
  title: "来源任务",
  messageCount: 2,
  sourceType: "web",
  sessionModelEntryId: 19,
  status: "active"
} as const;

const READONLY_SESSION = {
  ...CURRENT_SESSION,
  runtimeSessionId: "rs-readonly",
  title: "只读任务",
  isReadOnly: true
} as const;

const NEW_SESSION = {
  ...CURRENT_SESSION,
  runtimeSessionId: "rs-new",
  title: "新任务"
} as const;

// 2026-11-02-005：弹窗方式二模板库展示真实广场看板（非本人），选中后走
// downloadAppPackage ZIP 注入链路。
const PLAZA_APP = {
  id: 21,
  name: "广场模板看板",
  description: "广场共享看板",
  accessUrl: "https://demo.example/21",
  sourceType: "agent_html",
  shareMode: "scope",
  creatorName: "张三",
  mine: false,
  updatedAt: "2026-11-01T00:00:00Z",
  scopeSummary: "全员",
  publishVersionId: "21"
} as const;

const APP = {
  id: 11,
  name: "测试看板",
  description: "测试 HTML 看板",
  accessUrl: "https://demo.example/11",
  sourceType: "agent_html",
  shareMode: "scope",
  creatorName: "创建者",
  mine: true,
  sourceSessionId: "rs-source",
  updatedAt: "2026-10-19T00:00:00Z",
  scopeSummary: "仅自己",
  publishVersionId: "3567"
} as const;

const EXPECTED_PROMPT = "请以本次附件中的看板包为当前最新版本，继续修改「测试看板」，不要误用历史会话中的旧文件。只修改我要求的部分，其余功能、样式、资源和目录结构保持不变；不要新增外部 CDN 或网络依赖，看板继续保持包内自包含。修改完成后，重新打包为根目录包含 index.html 的 ZIP，直接写入本次任务文件目录（不要放入 .work/，不要只保存到本地工作文件夹，也不要只输出代码），并告知文件名。我的修改要求：";

function history(sessionId: string) {
  return {
    runtimeWorkspaceId: 1,
    runtimeSessionId: sessionId,
    messages: [],
    generatedAt: "2026-10-19T00:00:00Z",
    hasActiveRun: false
  };
}

async function openEditor(user: ReturnType<typeof userEvent.setup>) {
  render(<UserChatPage />);
  await screen.findByRole("button", { name: "当前任务" });
  await user.click(screen.getByRole("button", { name: "看板区" }));
  await user.click(await screen.findByRole("button", { name: "编辑 测试看板" }));
}

describe("AgentWorkspace app-center actions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
    api.runtimeSessions.mockResolvedValue([CURRENT_SESSION, SOURCE_SESSION]);
    api.runtimeSession.mockRejectedValue(new Error("runtime session not found"));
    api.createRuntimeSession.mockResolvedValue(NEW_SESSION);
    api.chatHistoryPreview.mockImplementation(async (_workspaceId: number, sessionId: string) => ({
      ...history(sessionId),
      source: "mysql",
      consistency: "projection_preview"
    }));
    api.chatHistoryAuthoritative.mockImplementation(async (_workspaceId: number, sessionId: string) => ({
      ...history(sessionId),
      source: "hermes",
      consistency: "authoritative"
    }));
    api.chatFiles.mockResolvedValue([]);
    api.chatQueueSummary.mockResolvedValue(emptyChatQueueSnapshot());
    api.chatQueueSummaryPrepared.mockImplementation(
      (workspaceId: number, sessionId: string) => api.chatQueueSummary(workspaceId, sessionId)
    );
    api.activeChatRun.mockResolvedValue({ run: null });
    api.listApps.mockResolvedValue([APP]);
    api.taskPromptConfig.mockResolvedValue({
      revision: 1,
      config: {
        tabs: [{
          label: "看板",
          description: "看板创建介绍",
          isDefault: true,
          showBoardGallery: false,
          topics: [{
            title: "销售分析",
            tasks: [{ summary: "销售趋势", detail: "请生成近 30 天销售趋势看板。" }]
          }]
        }]
      }
    });
    api.downloadAppPackage.mockResolvedValue(new Blob(["zip"], { type: "application/zip" }));
    api.organizationDirectoryTree.mockResolvedValue([]);
    api.prewarmRuntimeSession.mockImplementation(async (_workspaceId: number, sessionId: string) => ({
      state: "ready",
      readyReceiptId: `receipt-${sessionId}`,
      runtimeWorkspaceId: 1,
      runtimeSessionId: sessionId,
      modelEntryId: 19,
      runtimeAgentProfileId: null,
      profileGeneration: 0,
      selectionEpoch: 1,
      issuedAt: "2026-10-19T00:00:00Z",
      expiresAt: "2026-10-19T00:01:00Z"
    }));
    api.reportChatClientTiming.mockResolvedValue(undefined);
  });

  it("returns to the app source session and prefill latest-package instructions", async () => {
    const user = userEvent.setup();
    await openEditor(user);

    await waitFor(() => {
      expect(screen.getByText("来源任务", { selector: ".chatTitle strong" })).toBeVisible();
    });
    expect(api.createRuntimeSession).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "消息输入" })).toHaveValue(EXPECTED_PROMPT);
    expect(screen.getByRole("button", { name: /^移除待发送附件/ })).toBeVisible();
  });

  it("uses an editable source session even when the current session is read-only", async () => {
    api.runtimeSessions.mockResolvedValue([SOURCE_SESSION, READONLY_SESSION]);
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "来源任务" });
    await user.click(screen.getByRole("button", { name: "只读任务" }));
    await user.click(screen.getByRole("button", { name: "看板区" }));
    await user.click(await screen.findByRole("button", { name: "编辑 测试看板" }));

    await waitFor(() => {
      expect(screen.getByText("来源任务", { selector: ".chatTitle strong" })).toBeVisible();
    });
    expect(api.createRuntimeSession).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /^移除待发送附件/ })).toBeVisible();
  });

  it("fetches an editable source session that is outside the recent session list", async () => {
    api.runtimeSessions.mockResolvedValue([CURRENT_SESSION]);
    api.runtimeSession.mockResolvedValue(SOURCE_SESSION);
    const user = userEvent.setup();
    await openEditor(user);

    await waitFor(() => {
      expect(screen.getByText("来源任务", { selector: ".chatTitle strong" })).toBeVisible();
    });
    expect(api.runtimeSession).toHaveBeenCalledWith(1, "rs-source");
    expect(api.createRuntimeSession).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /^移除待发送附件/ })).toBeVisible();
  });

  it.each([null, "rs-missing"])("creates a new session when the source is %s", async (sourceSessionId) => {
    api.listApps.mockResolvedValue([{ ...APP, sourceSessionId }]);
    const user = userEvent.setup();
    await openEditor(user);

    await waitFor(() => {
      expect(screen.getByText("新任务", { selector: ".chatTitle strong" })).toBeVisible();
    });
    if (sourceSessionId === null) {
      expect(api.runtimeSession).not.toHaveBeenCalled();
    } else {
      expect(api.runtimeSession).toHaveBeenCalledWith(1, sourceSessionId);
    }
    expect(api.createRuntimeSession).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: /^移除待发送附件/ })).toBeVisible();
  });

  // 2026-11-02-001：弹窗为内置字典 + Tag 单选 + 底部「生成看板」提交，
  // 不再读取 task-prompt-config；提交仍新建独立会话并预填提示词，不自动发送。
  it("creates a fresh draft session from a selected prompt tag", async () => {
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "当前任务" });
    await user.click(screen.getByRole("button", { name: "看板区" }));
    await user.click(screen.getByRole("button", { name: "新建看板" }));
    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    await waitFor(() => expect(within(dialog).getByText("平台已连接：")).toBeVisible());
    expect(within(dialog).queryByRole("tab", { name: "会话创建" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("tab", { name: "上传ZIP方式创建" })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "制造业线索按地市统计" }));
    await user.click(within(dialog).getByRole("button", { name: "生成看板" }));

    await waitFor(() => expect(api.createRuntimeSession).toHaveBeenCalledTimes(1));
    expect(api.createRuntimeSession).toHaveBeenCalledWith(1, expect.objectContaining({
      sourceType: "web",
      runtimeAgentProfileId: null
    }));
    expect(await screen.findByText("新任务", { selector: ".chatTitle strong" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: "消息输入" }))
      .toHaveValue("制造业线索按地市统计");
    expect(screen.queryByRole("dialog", { name: "新建看板" })).not.toBeInTheDocument();
  });

  // 创建方式二（2026-11-02-005）：真实广场看板卡 → 下载模板 ZIP 注入附件 +
  // 预填模板修改提示词，新建独立会话；与 startEditApp 同管道但会话恒为新会话。
  it("creates a fresh session with the plaza template zip attached and prompt prefilled", async () => {
    api.listApps.mockResolvedValue([APP, PLAZA_APP]);
    const user = userEvent.setup();
    render(<UserChatPage />);

    await screen.findByRole("button", { name: "当前任务" });
    await user.click(screen.getByRole("button", { name: "看板区" }));
    await user.click(screen.getByRole("button", { name: "新建看板" }));
    const dialog = await screen.findByRole("dialog", { name: "新建看板" });
    await user.click(await within(dialog).findByRole("button", { name: "使用广场模板 广场模板看板" }));
    await user.click(within(dialog).getByRole("button", { name: "生成看板" }));

    await waitFor(() => expect(api.downloadAppPackage).toHaveBeenCalledWith(21));
    await waitFor(() => expect(api.createRuntimeSession).toHaveBeenCalledTimes(1));
    expect(api.runtimeSession).not.toHaveBeenCalled();
    expect(await screen.findByText("新任务", { selector: ".chatTitle strong" })).toBeVisible();
    expect((screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement).value)
      .toContain("附件中的看板包「广场模板看板」来自看板广场");
    expect(screen.getByRole("button", { name: /^移除待发送附件/ })).toBeVisible();
    expect(screen.queryByRole("dialog", { name: "新建看板" })).not.toBeInTheDocument();
  });
});
