import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  AdminRuntimeKubernetesSnapshot,
  AdminRuntimeWorkspaceResourceSummary
} from "@hermes-cloud/schemas";

const api = vi.hoisted(() => ({
  clusterSummary: vi.fn(),
  runtimeKubernetes: vi.fn(),
  runtimeKubernetesSnapshots: vi.fn(),
  runtimeSafeYaml: vi.fn(),
  podLogs: vi.fn(),
  followPodLogs: vi.fn()
}));

vi.mock("../api/client", () => ({ adminApi: api }));

import { AdminRuntimeResourceConsole, parseSseBuffer } from "./AdminRuntimeResourceConsole";

const resource: AdminRuntimeWorkspaceResourceSummary = {
  runtimeWorkspaceId: 7,
  cloudProfileId: 7,
  resourceKey: "personal:7",
  runtimeKind: "personal",
  orgId: 1,
  userId: 3,
  employeeNo: "E003",
  userName: "Runtime Owner",
  runtimeName: "Runtime 7",
  profileStatus: "active",
  bindingStatus: "ready",
  runtimeStatus: "ready",
  sessionCount: 1,
  runCount: 2,
  activeRunCount: 0,
  installedExpertCount: 0,
  installedSkillCount: 0,
  installedMcpCount: 0,
  installedKnowledgeBaseCount: 0,
  pendingReload: false,
  hasRuntimeBinding: true,
  kserviceName: "runtime-employee-7"
};

const snapshot: AdminRuntimeKubernetesSnapshot = {
  runtimeWorkspaceId: 7,
  resourceKey: "personal:7",
  runtimeKind: "personal",
  collectedAt: "2026-07-18T00:00:00Z",
  aggregateStatus: "ready",
  wakeBehavior: "allowed",
  runtimeService: {
    role: "runtime",
    kserviceName: "runtime-employee-7",
    namespace: "hermes-runtimes",
    status: "ready",
    ready: true,
    latestCreatedRevision: "runtime-employee-7-00001",
    latestReadyRevision: "runtime-employee-7-00001",
    trafficRevision: "runtime-employee-7-00001",
    desiredScale: 1,
    actualScale: 1,
    active: true,
    scaleDownDelaySeconds: 900,
    pods: []
  },
  pods: [
    {
      name: "runtime-employee-7-00001-deployment-runtime",
      phase: "Running",
      ready: true,
      containers: ["queue-proxy", "user-container"],
      restartCount: 0,
      imageNames: ["hermes-agent:test"],
      revisionName: "runtime-employee-7-00001",
      cpuUsageMillicores: 125,
      memoryUsageBytes: 256 * 1024 * 1024,
      resourceMetricsCollectedAt: "2026-07-18T00:01:00Z",
      containerMetrics: [
        {
          name: "user-container",
          cpuUsageMillicores: 100,
          memoryUsageBytes: 192 * 1024 * 1024
        },
        {
          name: "queue-proxy",
          cpuUsageMillicores: 25,
          memoryUsageBytes: 64 * 1024 * 1024
        }
      ]
    }
  ],
  recentEvents: [],
  stateMismatch: false
};

describe("AdminRuntimeResourceConsole", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.clusterSummary.mockResolvedValue({
      nodeCount: 1,
      readyNodeCount: 1,
      namespaceCount: 4,
      runningPodCount: 1,
      readyRuntimeCount: 1,
      scaledToZeroRuntimeCount: 0,
      startingRuntimeCount: 0,
      warningEventCount: 0,
      knativeControlPlaneReady: true,
      collectedAt: "2026-07-18T00:00:00Z",
      stale: false
    });
    api.runtimeKubernetes.mockResolvedValue(snapshot);
    api.runtimeKubernetesSnapshots.mockResolvedValue([snapshot]);
    api.runtimeSafeYaml.mockResolvedValue({
      runtimeWorkspaceId: 7,
      resourceKey: "personal:7",
      runtimeKind: "personal",
      collectedAt: "2026-07-18T00:00:00Z",
      objects: []
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps the database workspace visible when realtime data is unavailable", async () => {
    api.clusterSummary.mockRejectedValue(new Error("Kubernetes unavailable"));
    api.runtimeKubernetes.mockRejectedValue(new Error("Kubernetes unavailable"));

    render(
      <AdminRuntimeResourceConsole
        resources={[resource]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    expect((await screen.findAllByText("Runtime 7")).length).toBeGreaterThan(0);
    expect(await screen.findByText(/实时资源数据不可用/)).toBeVisible();
    expect(screen.getByText(/数据库 Workspace 列表仍可使用/)).toBeVisible();
  });

  it("shows a recoverable detail error when the selected workspace snapshot fails", async () => {
    api.runtimeKubernetes
      .mockRejectedValueOnce(new Error("Admin Kubernetes resource access is not configured"))
      .mockResolvedValueOnce(snapshot);
    const user = userEvent.setup();

    render(
      <AdminRuntimeResourceConsole
        resources={[resource]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    expect(await screen.findByRole("alert", { name: "Runtime 详情加载失败" }))
      .toHaveTextContent("Admin Kubernetes resource access is not configured");
    expect(screen.queryByText("正在加载实时 Runtime 数据…")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重试 Runtime 详情" }));

    expect(await screen.findByText("CPU 当前用量")).toBeVisible();
    expect(api.runtimeKubernetes).toHaveBeenCalledTimes(2);
  });

  it("keeps the last successful snapshot visible when a refresh fails", async () => {
    api.runtimeKubernetes
      .mockResolvedValueOnce(snapshot)
      .mockRejectedValueOnce(new Error("temporary Kubernetes failure"));
    const user = userEvent.setup();

    render(
      <AdminRuntimeResourceConsole
        resources={[resource]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    expect(await screen.findByText("125m")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "刷新" }));

    await waitFor(() => expect(api.runtimeKubernetes).toHaveBeenCalledTimes(2));
    expect(screen.getByText("125m")).toBeVisible();
    expect(screen.queryByRole("alert", { name: "Runtime 详情加载失败" })).not.toBeInTheDocument();
  });

  it("ignores a stale snapshot after switching workspaces", async () => {
    let resolveFirst: (value: AdminRuntimeKubernetesSnapshot) => void = () => undefined;
    const firstRequest = new Promise<AdminRuntimeKubernetesSnapshot>((resolve) => {
      resolveFirst = resolve;
    });
    const nextResource = {
      ...resource,
      runtimeWorkspaceId: 8,
      cloudProfileId: 8,
      resourceKey: "personal:8",
      runtimeName: "Runtime 8"
    };
    const nextSnapshot: AdminRuntimeKubernetesSnapshot = {
      ...snapshot,
      runtimeWorkspaceId: 8,
      resourceKey: "personal:8",
      runtimeService: {
        ...snapshot.runtimeService,
        kserviceName: "runtime-employee-8"
      }
    };
    api.runtimeKubernetes.mockImplementation((
      target: AdminRuntimeWorkspaceResourceSummary
    ) => (
      target.runtimeWorkspaceId === 7
        ? firstRequest
        : Promise.resolve(nextSnapshot)
    ));

    const { rerender } = render(
      <AdminRuntimeResourceConsole
        resources={[resource, nextResource]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );
    await waitFor(() => expect(api.runtimeKubernetes).toHaveBeenCalledWith(
      resource,
      expect.any(AbortSignal)
    ));

    rerender(
      <AdminRuntimeResourceConsole
        resources={[resource, nextResource]}
        selectedRuntimeId={8}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    expect(await screen.findByText("runtime-employee-8")).toBeVisible();
    resolveFirst(snapshot);
    await waitFor(() => expect(screen.queryByText("runtime-employee-7")).not.toBeInTheDocument());
  });

  it("uses user identity as the primary runtime list label", async () => {
    render(
      <AdminRuntimeResourceConsole
        resources={[resource]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    expect((await screen.findAllByText("Runtime Owner / E003")).length).toBeGreaterThan(0);
    expect(screen.getByText("Runtime 7 · rw-7")).toBeVisible();
  });

  it("renders long runtime labels in wrapping list elements", async () => {
    const longResource: AdminRuntimeWorkspaceResourceSummary = {
      ...resource,
      employeeNo: "resume-coldstart-07270749-a19649",
      userName: "未同步姓名未同步姓名未同步姓名未同步姓名",
      runtimeName: "runtime-resume-coldstart-07270749-a19649-with-a-deliberately-long-name"
    };

    render(
      <AdminRuntimeResourceConsole
        resources={[longResource]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    const row = await screen.findByRole("button", {
      name: /resume-coldstart-07270749-a19649/
    });
    expect(row.querySelector(".runtimeRowTitle strong")).toHaveTextContent(
      "未同步姓名未同步姓名未同步姓名未同步姓名 / resume-coldstart-07270749-a19649"
    );
    expect(row.querySelector(".runtimeResourceIdentity")).toHaveTextContent(
      "runtime-resume-coldstart-07270749-a19649-with-a-deliberately-long-name · rw-7"
    );
  });

  it("keeps an application Agent distinct from a personal Runtime with the same id", async () => {
    const applicationResource: AdminRuntimeWorkspaceResourceSummary = {
      ...resource,
      cloudProfileId: null,
      resourceKey: "application:7",
      runtimeKind: "application",
      applicationAgentId: 7,
      userName: "示例用户乙",
      employeeNo: "10002",
      runtimeName: "test5",
      kserviceName: "application-agent-7"
    };
    const applicationSnapshot: AdminRuntimeKubernetesSnapshot = {
      ...snapshot,
      resourceKey: "application:7",
      runtimeKind: "application",
      applicationAgentId: 7,
      runtimeService: {
        ...snapshot.runtimeService,
        kserviceName: "application-agent-7",
        latestCreatedRevision: "application-agent-7-00001",
        latestReadyRevision: "application-agent-7-00001",
        trafficRevision: "application-agent-7-00001"
      },
      pods: []
    };
    api.runtimeKubernetes.mockResolvedValue(applicationSnapshot);
    api.runtimeKubernetesSnapshots.mockResolvedValue([
      snapshot,
      applicationSnapshot
    ]);

    render(
      <AdminRuntimeResourceConsole
        resources={[resource, applicationResource]}
        selectedResourceKey="application:7"
        onSelectResource={vi.fn()}
        onError={vi.fn()}
      />
    );

    expect(await screen.findByText("application-agent-7")).toBeVisible();
    expect(screen.getByText("应用 Agent")).toBeVisible();
    expect(screen.getAllByText(/创建者 示例用户乙 \/ 10002/).length).toBeGreaterThan(0);
    expect(api.runtimeKubernetes).toHaveBeenCalledWith(
      applicationResource,
      expect.any(AbortSignal)
    );
  });

  it("marks placeholder user names as unsynchronized", async () => {
    render(
      <AdminRuntimeResourceConsole
        resources={[{ ...resource, employeeNo: "10001", userName: "用户" }]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    expect((await screen.findAllByText("未同步姓名 / 10001")).length).toBeGreaterThan(0);
  });

  it("shows runtime resource totals in the overview", async () => {
    render(
      <AdminRuntimeResourceConsole
        resources={[resource]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    expect(await screen.findByText("CPU 当前用量")).toBeVisible();
    expect(screen.getByText("125m")).toBeVisible();
    expect(screen.getByText("内存当前用量")).toBeVisible();
    expect(screen.getByText("256Mi")).toBeVisible();
    expect(screen.getByText("user-container CPU / 内存").closest("article"))
      .toHaveTextContent("100m / 192Mi");
    expect(screen.getByText("queue-proxy CPU / 内存").closest("article"))
      .toHaveTextContent("25m / 64Mi");
    expect(screen.getByText("Pod 数量").closest("article")).toHaveTextContent("1");
    expect(screen.getByText("Runtime Service").closest("article")).toHaveTextContent("Dashboard 9119");
    expect(screen.getByText("Runtime Service").closest("article")).toHaveTextContent("runtime-employee-7-00001");
    expect(screen.queryByText("Gateway 8642")).not.toBeInTheDocument();
    expect(screen.queryByText("API Service")).not.toBeInTheDocument();
    expect(screen.queryByText("Stream Service")).not.toBeInTheDocument();
    expect(screen.queryByText("runtime-employee-7-stream")).not.toBeInTheDocument();
  });

  it("shows per-pod resource metrics and missing metric fallback", async () => {
    api.runtimeKubernetes.mockResolvedValue({
      ...snapshot,
      pods: [
        {
          ...snapshot.pods[0],
          cpuUsageMillicores: null,
          memoryUsageBytes: null,
          resourceMetricsCollectedAt: null
        }
      ]
    });
    const user = userEvent.setup();
    render(
      <AdminRuntimeResourceConsole
        resources={[resource]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    await user.click(await screen.findByRole("tab", { name: "Pod" }));

    expect(screen.getAllByText("未采集").length).toBeGreaterThanOrEqual(2);
  });

  it("shows exactly one revision and autoscaler for the unified runtime", async () => {
    const user = userEvent.setup();
    const { container: rendered } = render(
      <AdminRuntimeResourceConsole
        resources={[resource]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    await screen.findByText("Runtime Service");
    await user.click(screen.getByRole("tab", { name: "Revision" }));
    expect(rendered.querySelectorAll(".runtimeInfoCard")).toHaveLength(1);
    expect(screen.getByText("Runtime")).toBeVisible();
    expect(screen.queryByText("Stream")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Autoscaler" }));
    expect(rendered.querySelectorAll(".runtimeInfoCard")).toHaveLength(1);
    expect(screen.getByText("runtime-employee-7")).toBeVisible();
  });

  it("defaults log selection to the Hermes user container", async () => {
    const user = userEvent.setup();
    render(
      <AdminRuntimeResourceConsole
        resources={[resource]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    await screen.findByText("Runtime Service");
    await user.click(screen.getByRole("tab", { name: "Logs" }));
    await waitFor(() => expect(screen.getByLabelText("容器")).toHaveValue("user-container"));
  });

  it("treats a scaled-to-zero runtime as healthy and wakeable", async () => {
    const scaledSnapshot: AdminRuntimeKubernetesSnapshot = {
      ...snapshot,
      aggregateStatus: "scaled_to_zero",
      runtimeService: {
        ...snapshot.runtimeService,
        status: "scaled_to_zero",
        ready: false,
        desiredScale: 0,
        actualScale: 0,
        active: false,
        pods: []
      },
      pods: []
    };
    api.runtimeKubernetes.mockResolvedValue(scaledSnapshot);
    api.runtimeKubernetesSnapshots.mockResolvedValue([scaledSnapshot]);

    render(
      <AdminRuntimeResourceConsole
        resources={[resource]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    expect(await screen.findByText("Runtime 已缩容到 0")).toBeVisible();
    expect(screen.getByText("Web、定时任务或 IM 的下一个请求会自动唤醒 Runtime。")).toBeVisible();
    expect(screen.queryByText("Gateway 8642")).not.toBeInTheDocument();
  });

  it("does not expose workspace operation controls in the resource view", async () => {
    render(
      <AdminRuntimeResourceConsole
        resources={[resource]}
        selectedRuntimeId={7}
        onSelectRuntime={vi.fn()}
        onError={vi.fn()}
      />
    );

    expect(await screen.findByRole("tab", { name: "概览" })).toBeVisible();
    expect(screen.queryByRole("tab", { name: "操作" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重启 Runtime" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "暂停 Runtime" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "缩容为 0（可自动唤醒）" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "确认 Runtime 操作" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Warmup" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reproject" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /重启 Pod/ })).not.toBeInTheDocument();
  });

  it("parses complete SSE frames and preserves a partial remainder", () => {
    const result = parseSseBuffer(
      'event: log.line\ndata: {"line":"one"}\n\n'
      + 'event: log.line\ndata: {"line":"tw'
    );

    expect(result.events).toEqual([{ event: "log.line", data: { line: "one" } }]);
    expect(result.remainder).toContain('{"line":"tw');
  });
});
