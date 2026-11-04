import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  applicationAgentAccess: vi.fn(),
  applicationAgents: vi.fn(),
  applicationAgent: vi.fn(),
  applicationAgentClients: vi.fn(),
  createApplicationAgent: vi.fn(),
  deleteApplicationAgent: vi.fn(),
  createApplicationAgentClient: vi.fn(),
  updateApplicationAgentClient: vi.fn(),
  deleteApplicationAgentClient: vi.fn(),
  publishApplicationAgent: vi.fn(),
  disableApplicationAgent: vi.fn(),
  resumeApplicationAgent: vi.fn()
}));

vi.mock("../api/client", () => ({ userApi: api }));
vi.mock("../auth", () => ({
  getPrincipal: () => ({ id: 1, name: "测试用户" })
}));
vi.mock("../profile", () => ({
  loadUserProfile: () => ({ displayName: "测试用户" })
}));
vi.mock("../components/AppSidebar", () => ({
  AppSidebar: (props: {
    sessions: Array<{ runtimeSessionId: string; title: string }>;
    onSessionChange: (id: string) => void;
    onCreateSession: () => void;
    onDeleteSession?: (id: string) => Promise<void>;
  }) => (
    <aside>
      {props.sessions.map((session) => (
        <div key={session.runtimeSessionId}>
          <button
            type="button"
            onClick={() => props.onSessionChange(session.runtimeSessionId)}
          >
            {session.title}
          </button>
          {props.onDeleteSession && (
            <button
              type="button"
              onClick={() => void props.onDeleteSession?.(session.runtimeSessionId)}
            >
              删除 {session.title}
            </button>
          )}
        </div>
      ))}
      <button type="button" onClick={props.onCreateSession}>
        新建应用 Agent
      </button>
    </aside>
  )
}));

import { ApplicationAgentPage } from "./ApplicationAgentPage";

const detail = {
  applicationAgentId: 31,
  slug: "ticket-agent",
  name: "工单助手",
  avatarUrl: null,
  status: "unpublished",
  runtimeStatus: "ready",
  configGeneration: 2,
  appliedGeneration: null,
  applyStatus: "applied",
  lifecycleStatus: "active",
  clientCount: 0,
  activeRunCount: 0,
  queuedRunCount: 0,
  personalKnowledgeEnabledCount: 0,
  personalKnowledgeRevokedCount: 0,
  publishedAt: null,
  updatedAt: null,
  accessMode: "owner",
  creatorName: "管理员甲",
  creatorUserCode: "A001",
  creatorStatus: "active",
  lastApplyErrorCode: null,
  config: {
    systemPrompt: "只处理工单",
    modelEntryId: null,
    skillAssetIds: [],
    applicationSkillIds: [],
    mcpAssetIds: [],
    enterpriseKnowledgeEnabled: false,
    memoryEnabled: true
  }
};

describe("ApplicationAgentPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/user/application-agents");
    window.sessionStorage.clear();
    api.applicationAgentAccess.mockResolvedValue({
      canManageApplicationAgents: true
    });
    api.applicationAgents.mockResolvedValue([detail]);
    api.applicationAgent.mockResolvedValue(detail);
    api.applicationAgentClients.mockResolvedValue([]);
    api.createApplicationAgent.mockResolvedValue(detail);
  });

  it("shows configuration in management and keeps creation minimal", async () => {
    const user = userEvent.setup();
    render(<ApplicationAgentPage/>);

    await user.click(await screen.findByRole("button", { name: "工单助手" }));
    expect(await screen.findByRole("heading", { name: "API 调用方" })).toBeVisible();
    expect(screen.getByRole("button", { name: "进入配置与调试" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Agent 配置" })).toBeVisible();
    expect(screen.getByLabelText("系统提示词只读预览")).toHaveTextContent("只处理工单");
    expect(screen.getByText(/管理员甲/)).toBeVisible();
    expect(screen.queryByLabelText("系统提示词")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /保存/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "新建应用 Agent" }));
    const dialog = await screen.findByRole("dialog", {
      name: "新建应用 Agent"
    });
    await user.type(within(dialog).getByLabelText("名称"), "新助手");
    await user.type(within(dialog).getByLabelText("接口标识"), "new-agent");
    expect(within(dialog).queryByLabelText("说明")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "创建" }));
    await waitFor(() =>
      expect(api.createApplicationAgent).toHaveBeenCalledWith(
        expect.not.objectContaining({ description: expect.anything() })
      )
    );
    await waitFor(() => expect(api.applicationAgent).toHaveBeenCalledWith(31));
  });

  it("routes manage deletion through the backend and marks the session deleting", async () => {
    const user = userEvent.setup();
    render(<ApplicationAgentPage/>);

    await user.click(await screen.findByRole("button", { name: "工单助手" }));
    await user.click(screen.getByRole("button", { name: "删除 工单助手" }));

    await waitFor(() => expect(api.deleteApplicationAgent).toHaveBeenCalledWith(31));
    expect(screen.getByRole("button", { name: "删除中" })).toBeDisabled();
    expect(screen.getByText("应用 Agent 删除请求已提交，后台正在清理资源。"))
      .toBeVisible();
  });

  it("falls back to the Manage root when the selected agent no longer exists", async () => {
    window.history.replaceState(null, "", "/user/application-agents/31/manage");
    api.applicationAgents.mockResolvedValue([]);

    render(<ApplicationAgentPage/>);

    await waitFor(() =>
      expect(window.location.pathname).toBe("/user/application-agents")
    );
    expect(api.applicationAgent).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "创建第一个应用 Agent" }))
      .toBeVisible();
  });

  it("gives another administrator a read-only management entry", async () => {
    window.history.replaceState(null, "", "/user/application-agents/31/manage");
    const viewerDetail = {
      ...detail,
      accessMode: "viewer",
      creatorName: "管理员乙",
      creatorUserCode: "A002"
    };
    api.applicationAgents.mockImplementation(async () => [viewerDetail]);
    api.applicationAgent.mockImplementation(async () => viewerDetail);
    render(<ApplicationAgentPage/>);

    expect(
      await screen.findByText("仅应用创建者可以配置和调试；你可以继续查看 Manage 摘要。")
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "进入配置与调试" }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "创建调用方" }))
      .not.toBeInTheDocument();
    expect(screen.getByText(/管理员乙/)).toBeVisible();
  });

  it("preserves the owner-required notice after a direct Debug redirect", async () => {
    window.history.replaceState(
      null,
      "",
      "/user/application-agents/31/manage"
    );
    window.sessionStorage.setItem(
      "application-agent-manage-notice",
      "只有应用创建者可以进入配置与调试。"
    );

    render(<ApplicationAgentPage/>);

    expect(await screen.findByText("只有应用创建者可以进入配置与调试。"))
      .toBeVisible();
    expect(window.sessionStorage.getItem("application-agent-manage-notice"))
      .toBeNull();
  });

  it("shows a plain API example for independent external-user conversations", async () => {
    const user = userEvent.setup();
    render(<ApplicationAgentPage/>);

    await user.click(await screen.findByRole("button", { name: "工单助手" }));

    await user.click(screen.getByText("API 调用说明"));
    expect(screen.getByText(
      "/openapi/v1/application-agents/ticket-agent/responses"
    )).toBeVisible();
    expect(screen.getAllByText(/external_user_id/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/conversation_key/).length).toBeGreaterThan(0);
    expect(screen.getByText(/output_text/)).toBeVisible();
    expect(screen.getAllByText(/Client Secret/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/授权回调地址/)).not.toBeInTheDocument();
    expect(screen.queryByText(/redirect_uri/)).not.toBeInTheDocument();
    expect(screen.queryByText("Enterprise SSO 用户授权入口")).not.toBeInTheDocument();
  });

  it("does not expose client creation on Manage", async () => {
    const user = userEvent.setup();
    api.createApplicationAgentClient.mockResolvedValue({
      applicationClientId: 92,
      clientId: "app_31_new",
      clientSecret: "secret-once",
      name: "SQLBot",
      status: "active",
      allowUserDelegation: true,
      allowServiceInvocation: false,
      authMode: "delegated_user",
      scopes: ["responses:create"],
      allowedRedirectUris: [],
      createdAt: null
    });
    render(<ApplicationAgentPage/>);

    await user.click(await screen.findByRole("button", { name: "工单助手" }));
    expect(screen.queryByLabelText(/授权回调地址/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("调用方名称")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "创建调用方" }))
      .not.toBeInTheDocument();
    expect(api.createApplicationAgentClient).not.toHaveBeenCalled();
  });

  it("shows legacy clients as summaries without edit controls", async () => {
    const user = userEvent.setup();
    const client = {
      applicationClientId: 91,
      clientId: "app_31_sqlbot",
      name: "SQLBot",
      status: "active",
      allowUserDelegation: true,
      allowServiceInvocation: true,
      authMode: "legacy_dual",
      scopes: ["responses:create"],
      allowedRedirectUris: [
        "https://sqlbot.example.com/old-callback"
      ],
      createdAt: null
    };
    api.applicationAgentClients.mockResolvedValue([client]);
    render(<ApplicationAgentPage/>);

    await user.click(await screen.findByRole("button", { name: "工单助手" }));
    const card = screen.getByText("app_31_sqlbot").closest("article");
    expect(card).not.toBeNull();
    expect(within(card!).queryByText(/授权回调地址/)).not.toBeInTheDocument();
    expect(within(card!).queryByText(/sqlbot\.example\.com/))
      .not.toBeInTheDocument();
    expect(within(card!).queryByRole("button")).not.toBeInTheDocument();
    expect(api.updateApplicationAgentClient).not.toHaveBeenCalled();
  });
});
