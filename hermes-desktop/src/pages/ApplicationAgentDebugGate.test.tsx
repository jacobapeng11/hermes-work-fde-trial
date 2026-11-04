import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  applicationAgent: vi.fn()
}));

vi.mock("../api/client", () => ({ userApi: api }));
vi.mock("./AgentWorkspace", () => ({
  AgentWorkspace: () => <div>workspace</div>
}));

import { ApplicationAgentDebugGate } from "./ApplicationAgentDebugGate";

describe("ApplicationAgentDebugGate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.sessionStorage.clear();
  });

  it("lets only the creator enter the Debug workspace", async () => {
    api.applicationAgent.mockResolvedValue({
      accessMode: "owner",
      runtimeStatus: "ready",
      lifecycleStatus: "active"
    });

    render(
      <ApplicationAgentDebugGate
        applicationAgentId={31}
        onReturnManage={vi.fn()}
      />
    );

    expect(await screen.findByText("workspace")).toBeVisible();
  });

  it("returns a viewer to Manage without exposing the workspace", async () => {
    api.applicationAgent.mockResolvedValue({ accessMode: "viewer" });
    const onReturnManage = vi.fn();

    render(
      <ApplicationAgentDebugGate
        applicationAgentId={31}
        onReturnManage={onReturnManage}
      />
    );

    await waitFor(() => expect(onReturnManage).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("workspace")).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem("application-agent-manage-notice"))
      .toMatch(/只有应用创建者/);
  });

  it("keeps the creator in Manage until Runtime is ready", async () => {
    api.applicationAgent.mockResolvedValue({
      accessMode: "owner",
      runtimeStatus: "absent",
      lifecycleStatus: "active"
    });
    const onReturnManage = vi.fn();

    render(
      <ApplicationAgentDebugGate
        applicationAgentId={31}
        onReturnManage={onReturnManage}
      />
    );

    await waitFor(() => expect(onReturnManage).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("workspace")).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem("application-agent-manage-notice"))
      .toMatch(/Runtime 正在准备/);
  });

  it("returns the creator to Manage while deletion is pending", async () => {
    api.applicationAgent.mockResolvedValue({
      accessMode: "owner",
      runtimeStatus: "stopped",
      lifecycleStatus: "deleting"
    });
    const onReturnManage = vi.fn();

    render(
      <ApplicationAgentDebugGate
        applicationAgentId={31}
        onReturnManage={onReturnManage}
      />
    );

    await waitFor(() => expect(onReturnManage).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("workspace")).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem("application-agent-manage-notice"))
      .toMatch(/正在删除/);
  });
});
