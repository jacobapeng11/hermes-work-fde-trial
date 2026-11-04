import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrainCircuit, Clock3, Database, MessageSquare, Sparkles } from "lucide-react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppSidebar } from "./AppSidebar";

describe("AppSidebar", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("uses Hermes Work as the product brand", () => {
    const { container } = render(<AppSidebar view="chat" views={[{ id: "chat", label: "任务", icon: MessageSquare }]} onViewChange={() => undefined} sessions={[]} activeSessionId={null} onSessionChange={() => undefined} onCreateSession={() => undefined}/>);
    expect(screen.getByText("Hermes Work")).toBeVisible();
    expect(container.querySelector(".brandMark")).not.toBeInTheDocument();
    expect(screen.queryByText("示例业务数字人")).not.toBeInTheDocument();
  });

  it("opens MCP from the digital-human menu alongside IM", async () => {
    const onProfileOpen = vi.fn();
    const onThemeToggle = vi.fn();
    const onImOpen = vi.fn();
    const onMcpOpen = vi.fn();
    const user = userEvent.setup();
    render(<AppSidebar view="chat" views={[{ id: "chat", label: "任务", icon: MessageSquare }]} onViewChange={() => undefined} sessions={[]} activeSessionId={null} onSessionChange={() => undefined} onCreateSession={() => undefined} menuOpen onMenuToggle={() => undefined} onProfileOpen={onProfileOpen} onThemeToggle={onThemeToggle} onMcpOpen={onMcpOpen} onImOpen={onImOpen}/>);

    const menu = screen.getByRole("menu", { name: "数字人菜单" });
    expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "个人资料",
      "切换至浅色主题",
      "MCP",
      "IM"
    ]);
    expect(screen.queryByText("通知")).not.toBeInTheDocument();
    expect(screen.queryByText("退出登录")).not.toBeInTheDocument();

    await user.click(within(menu).getByRole("menuitem", { name: "个人资料" }));
    await user.click(within(menu).getByRole("menuitem", { name: "切换至浅色主题" }));
    await user.click(within(menu).getByRole("menuitem", { name: "MCP" }));
    await user.click(within(menu).getByRole("menuitem", { name: "IM" }));
    expect(onProfileOpen).toHaveBeenCalledOnce();
    expect(onThemeToggle).toHaveBeenCalledOnce();
    expect(onMcpOpen).toHaveBeenCalledOnce();
    expect(onImOpen).toHaveBeenCalledOnce();
  });

  it("shows the admin entry only for an authorized principal", async () => {
    const onAdminOpen = vi.fn();
    const onProfileOpen = vi.fn();
    const onImOpen = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <AppSidebar
        view="chat"
        views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[]}
        activeSessionId={null}
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        menuOpen
        onMenuToggle={() => undefined}
        onProfileOpen={onProfileOpen}
        onImOpen={onImOpen}
        isAdmin
        onAdminOpen={onAdminOpen}
      />
    );

    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "个人资料",
      "切换至浅色主题",
      "IM",
      "管理后台"
    ]);
    await user.click(screen.getByRole("menuitem", { name: "管理后台" }));
    expect(onAdminOpen).toHaveBeenCalledOnce();

    rerender(
      <AppSidebar
        view="chat"
        views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[]}
        activeSessionId={null}
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        menuOpen
        onMenuToggle={() => undefined}
        onProfileOpen={onProfileOpen}
        onImOpen={onImOpen}
        isAdmin={false}
      />
    );
    expect(screen.queryByRole("menuitem", { name: "管理后台" })).not.toBeInTheDocument();
  });

  it("marks the WeCom session as fixed and offers no mutation actions", () => {
    const onRenameSession = vi.fn();
    const onDeleteSession = vi.fn();
    render(
      <AppSidebar
        view="chat"
        views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[{
          runtimeSessionId: "rs-wecom",
          title: "企微对话",
          messageCount: 2,
          sourceType: "wecom",
          isImmutable: true,
          originLabel: "企微"
        }]}
        activeSessionId="rs-wecom"
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        onRenameSession={onRenameSession}
        onDeleteSession={onDeleteSession}
      />
    );

    expect(screen.getByText("企微 · 固定会话")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "任务选项：企微对话" })
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /重命名任务/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /删除任务/ })).not.toBeInTheDocument();
  });

  it("reuses the sidebar shell for application Agent mode", () => {
    render(
      <AppSidebar
        view="application-agents"
        views={[{
          id: "application-agents",
          label: "应用 Agent",
          icon: MessageSquare
        }]}
        onViewChange={() => undefined}
        sessions={[{
          runtimeSessionId: "31",
          title: "工单助手",
          messageCount: 0
        }]}
        activeSessionId="31"
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        menuOpen
        onMenuToggle={() => undefined}
        canManageApplicationAgents
        applicationAgentMode
        sessionSectionLabel="应用 Agent"
        onApplicationAgentsOpen={() => undefined}
      />
    );

    expect(screen.getByRole("button", { name: "新建应用 Agent" })).toBeVisible();
    expect(screen.getByText("工单助手")).toBeVisible();
    expect(
      screen.getByRole("menuitem", { name: "个人 Agent" })
    ).toBeVisible();
  });

  it("closes the digital-human menu when a pointer lands outside it", () => {
    const onMenuToggle = vi.fn();
    render(<AppSidebar view="chat" views={[{ id: "chat", label: "任务", icon: MessageSquare }]} onViewChange={() => undefined} sessions={[]} activeSessionId={null} onSessionChange={() => undefined} onCreateSession={() => undefined} menuOpen onMenuToggle={onMenuToggle}/>);

    fireEvent.pointerDown(document.body);
    expect(onMenuToggle).toHaveBeenCalledOnce();

    fireEvent.pointerDown(screen.getByRole("menu", { name: "数字人菜单" }));
    expect(onMenuToggle).toHaveBeenCalledOnce();
  });

  it("places recent tasks directly beneath the primary navigation and normalizes legacy titles", () => {
    const { container } = render(
      <AppSidebar
        view="chat"
        views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[
          { runtimeSessionId: "session-1", title: "发布迁移设计", messageCount: 2 },
          { runtimeSessionId: "session-2", title: "新会话", messageCount: 0 }
        ]}
        activeSessionId="session-1"
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
      />
    );
    const sidebar = container.querySelector("aside");
    expect(sidebar?.querySelector("nav")?.compareDocumentPosition(screen.getByText("最近任务"))).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.getByText("发布迁移设计")).toBeInTheDocument();
    expect(screen.getByText("新任务")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建任务" })).toBeEnabled();
    expect(container.querySelectorAll(".sessionStatusDot")).toHaveLength(2);
    expect(container.querySelector('.sessionStatusDot[data-active="true"]')).toBeInTheDocument();
    expect(container.querySelector(".sessionList small")).not.toBeInTheDocument();
  });

  it("shows running, unseen-completion, and error indicators beside their sessions", () => {
    render(
      <AppSidebar
        view="chat"
        views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[
          { runtimeSessionId: "session-running", title: "执行中", messageCount: 1 },
          { runtimeSessionId: "session-done", title: "已完成", messageCount: 2 },
          { runtimeSessionId: "session-error", title: "执行出错", messageCount: 3 }
        ]}
        activeSessionId={null}
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        sessionActivities={{
          "session-running": { state: "running", runtimeRunId: "run-1" },
          "session-done": { state: "completed-unseen", runtimeRunId: "run-2" },
          "session-error": { state: "error-unseen", runtimeRunId: "run-3" }
        }}
      />
    );

    expect(document.querySelector('.sessionRunSpinner[data-status="执行中"]')).toBeInTheDocument();
    expect(document.querySelector('.sessionCompletedDot[data-status="已完成，待查看"]')).toBeInTheDocument();
    expect(document.querySelector('.sessionErrorDot[data-status="执行出错，待查看"]')).toBeInTheDocument();
    expect([...document.querySelectorAll<HTMLButtonElement>(".sessionSelect")].map((button) => button.textContent?.trim())).toEqual([
      "执行中",
      "已完成",
      "执行出错"
    ]);
  });

  it("offers expansion only when sessions exceed the space above the profile", async () => {
    const clientHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight");
    const offsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const height = this.classList.contains("sessionRow") ? 34 : 0;
      return { x: 0, y: 0, width: 200, height, top: 0, right: 200, bottom: height, left: 0, toJSON: () => ({}) };
    });
    Object.defineProperty(HTMLElement.prototype, "clientHeight", {
      configurable: true,
      get(this: HTMLElement) { return this.classList.contains("embeddedSessions") ? 100 : 0; }
    });
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
      configurable: true,
      get(this: HTMLElement) { return this.classList.contains("sessionHead") ? 20 : 0; }
    });
    try {
      const user = userEvent.setup();
      const { unmount } = render(
        <AppSidebar
          view="chat"
          views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
          onViewChange={() => undefined}
          sessions={[1, 2, 3, 4].map((id) => ({
            runtimeSessionId: `session-${id}`,
            title: `任务 ${id}`,
            messageCount: id
          }))}
          activeSessionId="session-4"
          onSessionChange={() => undefined}
          onCreateSession={() => undefined}
          collapsibleSessions
        />
      );

      expect(screen.getByRole("button", { name: "点击展开更多" })).toBeVisible();
      expect(screen.getByText("任务 4")).toBeInTheDocument();
      expect(screen.queryByText("任务 1")).not.toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "点击展开更多" }));
      expect(screen.queryByRole("button", { name: "点击展开更多" })).not.toBeInTheDocument();
      expect(screen.getByText("任务 1")).toBeInTheDocument();
      unmount();
    } finally {
      rect.mockRestore();
      if (clientHeight) Object.defineProperty(HTMLElement.prototype, "clientHeight", clientHeight);
      if (offsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", offsetHeight);
    }
  });

  it("places the create action beside the task navigation without coupling their clicks", async () => {
    const onCreateSession = vi.fn();
    const onViewChange = vi.fn();
    const { container } = render(
      <AppSidebar
        view="skills"
        views={[
          { id: "chat", label: "任务", icon: MessageSquare },
          { id: "skills", label: "Skill", icon: BrainCircuit }
        ]}
        onViewChange={onViewChange}
        sessions={[]}
        activeSessionId={null}
        onSessionChange={() => undefined}
        onCreateSession={onCreateSession}
      />
    );

    const taskRow = screen.getByRole("button", { name: "任务" }).closest(".navRow");
    expect(taskRow).not.toBeNull();
    const createButton = within(taskRow as HTMLElement).getByRole("button", { name: "新建任务" });
    expect(container.querySelector(".sessionHead")).not.toContainElement(createButton);
    expect(container.querySelectorAll(".navCreateButton")).toHaveLength(1);

    const user = userEvent.setup();
    await user.click(createButton);
    expect(onCreateSession).toHaveBeenCalledTimes(1);
    expect(onViewChange).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "任务" }));
    expect(onViewChange).toHaveBeenCalledWith("chat");
  });

  it("freezes the navigation, recent session, and profile region order", () => {
    const views = [
      { id: "chat", label: "任务", icon: MessageSquare },
      { id: "skills", label: "Skill", icon: BrainCircuit },
      { id: "knowledge", label: "知识库", icon: Database },
      { id: "experts", label: "专家", icon: Sparkles },
      { id: "schedules", label: "定时任务", icon: Clock3 }
    ];
    const { container } = render(
      <AppSidebar
        view="chat"
        views={views}
        onViewChange={() => undefined}
        sessions={[]}
        activeSessionId={null}
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
      />
    );

    const sidebar = container.querySelector(".primarySidebar");
    const regions = Array.from(sidebar?.children ?? []);
    expect(regions.map((node) => node.className || node.tagName.toLowerCase())).toEqual([
      "brand",
      "nav",
      "embeddedSessions",
      "profileMenu"
    ]);
    expect(Array.from(sidebar?.querySelectorAll("nav .navButton") ?? []).map((button) => button.textContent)).toEqual([
      "任务",
      "Skill",
      "知识库",
      "专家",
      "定时任务"
    ]);
    expect(screen.queryByRole("button", { name: "MCP" })).not.toBeInTheDocument();
  });

  it("offers a delete action for each conversation", async () => {
    const onDeleteSession = vi.fn().mockResolvedValue(undefined);
    render(
      <AppSidebar
        view="chat"
        views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[{ runtimeSessionId: "session-1", title: "待删除会话", messageCount: 0 }]}
        activeSessionId="session-1"
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        onDeleteSession={onDeleteSession}
      />
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "任务操作：待删除会话" }));
    await user.click(screen.getByRole("menuitem", { name: "删除任务：待删除会话" }));

    const confirmation = screen.getByRole("dialog", { name: "确认删除任务：待删除会话" });
    expect(within(confirmation).getByText("删除“待删除会话”？")).toBeVisible();
    expect(within(confirmation).getByText("将删除该任务的全部消息，且无法恢复。")).toBeVisible();
    expect(onDeleteSession).not.toHaveBeenCalled();

    await user.click(within(confirmation).getByRole("button", { name: "取消" }));
    expect(screen.getByRole("menu", { name: "任务操作：待删除会话" })).toBeVisible();
    await user.click(screen.getByRole("menuitem", { name: "删除任务：待删除会话" }));
    await user.click(screen.getByRole("button", { name: "确认删除" }));

    expect(onDeleteSession).toHaveBeenCalledWith("session-1");
  });

  it("uses the same inline delete menu with application Agent wording", async () => {
    const onDeleteSession = vi.fn().mockResolvedValue(undefined);
    render(
      <AppSidebar
        view="application-agents"
        views={[{ id: "application-agents", label: "应用 Agent", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[{ runtimeSessionId: "31", title: "test5", messageCount: 0 }]}
        activeSessionId="31"
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        onDeleteSession={onDeleteSession}
        applicationAgentMode
        sessionSectionLabel="应用 Agent"
      />
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "应用 Agent 操作：test5" }));
    await user.click(screen.getByRole("menuitem", { name: "删除应用 Agent：test5" }));

    const confirmation = screen.getByRole("dialog", {
      name: "确认删除应用 Agent：test5"
    });
    expect(within(confirmation).getByText(
      "将永久删除配置、调用方、会话、记忆以及全部 Runtime 资源，且无法恢复。"
    )).toBeVisible();
    await user.click(within(confirmation).getByRole("button", { name: "确认删除" }));

    expect(onDeleteSession).toHaveBeenCalledWith("31");
  });

  it("hides delete actions for an application Agent already deleting", () => {
    render(
      <AppSidebar
        view="application-agents"
        views={[{ id: "application-agents", label: "应用 Agent", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[{
          runtimeSessionId: "32",
          title: "删除中 Agent",
          messageCount: 0,
          status: "deleting"
        }]}
        activeSessionId="32"
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        onDeleteSession={vi.fn()}
        applicationAgentMode
        sessionSectionLabel="应用 Agent"
      />
    );

    expect(screen.queryByRole("button", { name: "应用 Agent 操作：删除中 Agent" }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "删除应用 Agent：删除中 Agent" }))
      .not.toBeInTheDocument();
  });

  it("locks duplicate application Agent deletes and explains slow cleanup after two seconds", async () => {
    vi.useFakeTimers();
    let finishDelete: (() => void) | undefined;
    const onDeleteSession = vi.fn().mockImplementation(
      () => new Promise<void>((resolve) => {
        finishDelete = resolve;
      })
    );
    render(
      <AppSidebar
        view="application-agents"
        views={[{ id: "application-agents", label: "应用 Agent", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[{ runtimeSessionId: "7", title: "test4", messageCount: 0 }]}
        activeSessionId="7"
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        onDeleteSession={onDeleteSession}
        applicationAgentMode
        sessionSectionLabel="应用 Agent"
      />
    );

    fireEvent.click(screen.getByRole("button", {
      name: "应用 Agent 操作：test4"
    }));
    fireEvent.click(screen.getByRole("menuitem", {
      name: "删除应用 Agent：test4"
    }));
    const confirmation = screen.getByRole("dialog", {
      name: "确认删除应用 Agent：test4"
    });
    const confirmButton = within(confirmation).getByRole("button", {
      name: "确认删除"
    });
    act(() => {
      confirmButton.click();
      confirmButton.click();
    });

    expect(onDeleteSession).toHaveBeenCalledTimes(1);
    expect(onDeleteSession).toHaveBeenCalledWith("7");
    expect(within(confirmation).queryByText(
      "正在停止 Runtime 并清理资源，通常需要约 30 秒…"
    )).not.toBeInTheDocument();

    act(() => vi.advanceTimersByTime(1999));
    expect(within(confirmation).queryByText(
      "正在停止 Runtime 并清理资源，通常需要约 30 秒…"
    )).not.toBeInTheDocument();

    act(() => vi.advanceTimersByTime(1));
    expect(within(confirmation).getByText(
      "正在停止 Runtime 并清理资源，通常需要约 30 秒…"
    )).toBeVisible();

    await act(async () => {
      finishDelete?.();
      await Promise.resolve();
    });
    expect(screen.queryByRole("dialog", {
      name: "确认删除应用 Agent：test4"
    })).not.toBeInTheDocument();
  });

  it("keeps deletion available but hides rename for a deleted expert conversation", async () => {
    const onDeleteSession = vi.fn().mockResolvedValue(undefined);
    render(
      <AppSidebar
        view="chat"
        views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[{
          runtimeSessionId: "session-deleted-expert",
          title: "已删除专家的历史会话",
          messageCount: 2,
          agentProfileKind: "expert",
          status: "expert_deleted",
          isReadOnly: true
        }]}
        activeSessionId="session-deleted-expert"
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        onRenameSession={vi.fn().mockResolvedValue(true)}
        onDeleteSession={onDeleteSession}
      />
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "任务操作：已删除专家的历史会话" }));

    const menu = screen.getByRole("menu", { name: "任务操作：已删除专家的历史会话" });
    expect(within(menu).getByRole("menuitem", { name: "删除任务：已删除专家的历史会话" })).toBeVisible();
    expect(within(menu).queryByRole("menuitem", { name: "重命名任务：已删除专家的历史会话" })).not.toBeInTheDocument();

    await user.click(within(menu).getByRole("menuitem", { name: "删除任务：已删除专家的历史会话" }));
    await user.click(screen.getByRole("button", { name: "确认删除" }));
    expect(onDeleteSession).toHaveBeenCalledWith("session-deleted-expert");
  });

  it("keeps the inline confirmation open while deleting and after a failure", async () => {
    let finishDelete: (() => void) | undefined;
    const onDeleteSession = vi.fn()
      .mockImplementationOnce(() => new Promise<void>((resolve) => { finishDelete = resolve; }))
      .mockRejectedValueOnce(new Error("删除服务暂不可用"));
    render(
      <AppSidebar
        view="chat"
        views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[{ runtimeSessionId: "session-1", title: "失败任务", messageCount: 0 }]}
        activeSessionId="session-1"
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        onDeleteSession={onDeleteSession}
      />
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "任务操作：失败任务" }));
    await user.click(screen.getByRole("menuitem", { name: "删除任务：失败任务" }));
    await user.click(screen.getByRole("button", { name: "确认删除" }));

    expect(screen.getByRole("button", { name: "删除中…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "删除中…" }));
    expect(onDeleteSession).toHaveBeenCalledTimes(1);
    await user.keyboard("{Escape}");
    await user.click(screen.getByText("Hermes Work"));
    expect(screen.getByRole("dialog", { name: "确认删除任务：失败任务" })).toBeVisible();

    await act(async () => { finishDelete?.(); });
    expect(screen.queryByRole("dialog", { name: "确认删除任务：失败任务" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "任务操作：失败任务" }));
    await user.click(screen.getByRole("menuitem", { name: "删除任务：失败任务" }));
    await user.click(screen.getByRole("button", { name: "确认删除" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("删除服务暂不可用");
    expect(screen.getByRole("dialog", { name: "确认删除任务：失败任务" })).toBeVisible();
    expect(screen.getByRole("button", { name: "确认删除" })).toBeEnabled();
  });

  it("keeps task options hidden until the action button is clicked", async () => {
    render(
      <AppSidebar
        view="chat"
        views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[{ runtimeSessionId: "session-1", title: "悬停任务", messageCount: 0 }]}
        activeSessionId="session-1"
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        onRenameSession={vi.fn().mockResolvedValue(true)}
        onDeleteSession={vi.fn()}
      />
    );

    const options = screen.getByRole("button", { name: "任务操作：悬停任务" });
    expect(options).toBeVisible();
    fireEvent.pointerEnter(options);
    expect(screen.queryByRole("menu", { name: "任务操作：悬停任务" })).not.toBeInTheDocument();

    await userEvent.setup().click(options);
    expect(screen.getByRole("menu", { name: "任务操作：悬停任务" })).toBeVisible();
    fireEvent.pointerLeave(options);
    fireEvent.pointerEnter(screen.getByRole("menu", { name: "任务操作：悬停任务" }));
    expect(screen.getByRole("menu", { name: "任务操作：悬停任务" })).toBeVisible();
    await userEvent.setup().click(options);
    expect(screen.queryByRole("menu", { name: "任务操作：悬停任务" })).not.toBeInTheDocument();
  });

  it("renames a conversation inline without selecting it", async () => {
    const onRenameSession = vi.fn().mockResolvedValue(true);
    const onSessionChange = vi.fn();
    render(
      <AppSidebar
        view="chat"
        views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[{ runtimeSessionId: "session-1", title: "旧标题", messageCount: 2 }]}
        activeSessionId="session-1"
        onSessionChange={onSessionChange}
        onCreateSession={() => undefined}
        onRenameSession={onRenameSession}
      />
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "任务操作：旧标题" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名任务：旧标题" }));
    const input = screen.getByRole("textbox", { name: "重命名任务：旧标题" });
    await user.clear(input);
    await user.type(input, "发布计划{Enter}");

    expect(onRenameSession).toHaveBeenCalledWith("session-1", "发布计划");
    expect(onSessionChange).not.toHaveBeenCalled();
  });

  it("cancels rename with Escape and preserves the editor after a failed save", async () => {
    const onRenameSession = vi.fn().mockResolvedValue(false);
    render(
      <AppSidebar
        view="chat"
        views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[{ runtimeSessionId: "session-1", title: "旧标题", messageCount: 2 }]}
        activeSessionId="session-1"
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        onRenameSession={onRenameSession}
      />
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "任务操作：旧标题" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名任务：旧标题" }));
    const input = screen.getByRole("textbox", { name: "重命名任务：旧标题" });
    await user.clear(input);
    await user.type(input, "冲突标题{Enter}");
    expect(await screen.findByDisplayValue("冲突标题")).toBeInTheDocument();

    await user.type(input, "{Escape}");
    expect(screen.queryByRole("textbox", { name: "重命名任务：旧标题" })).not.toBeInTheDocument();
    expect(screen.getByText("旧标题")).toBeInTheDocument();
  });

  it("closes the task options menu with Escape or an outside click", async () => {
    render(
      <AppSidebar
        view="chat"
        views={[{ id: "chat", label: "任务", icon: MessageSquare }]}
        onViewChange={() => undefined}
        sessions={[{ runtimeSessionId: "session-1", title: "任务标题", messageCount: 2 }]}
        activeSessionId="session-1"
        onSessionChange={() => undefined}
        onCreateSession={() => undefined}
        onRenameSession={vi.fn().mockResolvedValue(true)}
        onDeleteSession={vi.fn()}
      />
    );

    const user = userEvent.setup();
    const options = screen.getByRole("button", { name: "任务操作：任务标题" });
    await user.click(options);
    expect(screen.getByRole("menu", { name: "任务操作：任务标题" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu", { name: "任务操作：任务标题" })).not.toBeInTheDocument();

    await user.click(options);
    await user.click(screen.getByText("Hermes Work"));
    expect(screen.queryByRole("menu", { name: "任务操作：任务标题" })).not.toBeInTheDocument();
  });
});
