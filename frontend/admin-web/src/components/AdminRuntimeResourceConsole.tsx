import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Boxes,
  CircleStop,
  Flame,
  Gauge,
  Play,
  RefreshCw,
  ScrollText,
  Server,
  SquareTerminal,
  Workflow
} from "lucide-react";
import type {
  AdminClusterSummary,
  AdminPodSummary,
  AdminRuntimeHealthSnapshot,
  AdminRuntimeKubernetesSnapshot,
  AdminRuntimeWorkspaceResourceSummary,
  AdminSafeKubernetesSummary
} from "@hermes-cloud/schemas";
import { adminApi } from "../api/client";
import { AdminClusterCapacityOverview } from "./AdminClusterCapacityOverview";

type DetailTab = "overview" | "pods" | "revisions" | "autoscaler" | "events" | "logs" | "yaml";

type Props = {
  resources: AdminRuntimeWorkspaceResourceSummary[];
  selectedResourceKey?: string | null;
  onSelectResource?: (resourceKey: string) => void | Promise<void>;
  /** Compatibility for older callers and focused component tests. */
  selectedRuntimeId?: number | null;
  onSelectRuntime?: (runtimeWorkspaceId: number) => void | Promise<void>;
  onError: (message: string | null) => void;
};

const tabs: Array<{ key: DetailTab; label: string }> = [
  { key: "overview", label: "概览" },
  { key: "pods", label: "Pod" },
  { key: "revisions", label: "Revision" },
  { key: "autoscaler", label: "Autoscaler" },
  { key: "events", label: "Events" },
  { key: "logs", label: "Logs" },
  { key: "yaml", label: "YAML 摘要" }
];

const statusText: Record<string, string> = {
  scaled_to_zero: "已缩容到 0",
  starting: "启动中",
  ready: "运行中",
  stopping: "停止中",
  degraded: "降级",
  failed: "异常",
  unknown: "未知"
};

function displayTime(value?: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function scaleText(desired?: number | null, actual?: number | null) {
  return `${desired ?? "-"} / ${actual ?? "-"}`;
}

function userIdentity(resource: AdminRuntimeWorkspaceResourceSummary) {
  const normalizedName = resource.userName.trim();
  const displayName = normalizedName && normalizedName !== "用户" ? normalizedName : "未同步姓名";
  return `${displayName} / ${resource.employeeNo ?? "未绑定员工编号"}`;
}

function resourceIdentity(resource: AdminRuntimeWorkspaceResourceSummary) {
  return resource.runtimeKind === "application"
    ? `创建者 ${userIdentity(resource)}`
    : userIdentity(resource);
}

function resourceIdLabel(resource: AdminRuntimeWorkspaceResourceSummary) {
  return resource.runtimeKind === "application"
    ? `agent-${resource.applicationAgentId ?? resource.runtimeWorkspaceId}`
    : `rw-${resource.runtimeWorkspaceId}`;
}

function formatCpu(value?: number | null) {
  return value == null ? "未采集" : `${value}m`;
}

function formatMemory(value?: number | null) {
  if (value == null) return "未采集";
  if (value <= 0) return "0Mi";
  return `${Math.max(1, Math.round(value / 1024 / 1024))}Mi`;
}

function sumKnown(values: Array<number | null | undefined>) {
  const known = values.filter((value): value is number => value != null);
  return known.length ? known.reduce((sum, value) => sum + value, 0) : null;
}

function containerMetricTotals(
  pods: AdminPodSummary[],
  containerName: string
) {
  const metrics = pods.flatMap((pod) => pod.containerMetrics ?? [])
    .filter((item) => item.name === containerName);
  return {
    cpu: sumKnown(metrics.map((item) => item.cpuUsageMillicores)),
    memory: sumKnown(metrics.map((item) => item.memoryUsageBytes))
  };
}

function defaultContainerName(pod?: AdminPodSummary) {
  if (!pod) return "";
  return pod.containers.includes("user-container") ? "user-container" : pod.containers[0] ?? "";
}

export function AdminRuntimeResourceConsole({
  resources,
  selectedResourceKey,
  onSelectResource,
  selectedRuntimeId,
  onSelectRuntime,
  onError
}: Props) {
  const [cluster, setCluster] = useState<AdminClusterSummary | null>(null);
  const [snapshot, setSnapshot] = useState<AdminRuntimeKubernetesSnapshot | null>(null);
  const [runtimeHealth, setRuntimeHealth] = useState<AdminRuntimeHealthSnapshot | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [liveSnapshots, setLiveSnapshots] = useState<Record<string, AdminRuntimeKubernetesSnapshot>>({});
  const [realtimeError, setRealtimeError] = useState<string | null>(null);
  const [detailTab, setDetailTab] = useState<DetailTab>("overview");
  const [safeYaml, setSafeYaml] = useState<AdminSafeKubernetesSummary | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [resourceSearch, setResourceSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [podName, setPodName] = useState("");
  const [container, setContainer] = useState("");
  const [tailLines, setTailLines] = useState(500);
  const [previous, setPrevious] = useState(false);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [logState, setLogState] = useState<"idle" | "loading" | "following" | "disconnected">("idle");
  const snapshotRequest = useRef<AbortController | null>(null);
  const clusterRequest = useRef<AbortController | null>(null);
  const listRequest = useRef<AbortController | null>(null);
  const logController = useRef<AbortController | null>(null);
  const snapshotResourceKey = useRef<string | null>(null);
  const snapshotBusy = useRef(false);
  const clusterBusy = useRef(false);
  const listBusy = useRef(false);

  const selectedResource = useMemo(
    () => resources.find((item) => (
      selectedResourceKey
        ? item.resourceKey === selectedResourceKey
        : item.runtimeWorkspaceId === selectedRuntimeId
    )) ?? resources[0] ?? null,
    [resources, selectedResourceKey, selectedRuntimeId]
  );
  const filteredResources = useMemo(() => {
    const needle = resourceSearch.trim().toLowerCase();
    return resources.filter((resource) => {
      const liveStatus = liveSnapshots[resource.resourceKey]?.aggregateStatus;
      const matchesSearch = !needle || [
        resource.runtimeName,
        resource.userName,
        resource.employeeNo,
        String(resource.runtimeWorkspaceId),
        resource.resourceKey,
        resource.kserviceName
      ].some((value) => value?.toLowerCase().includes(needle));
      return matchesSearch && (!statusFilter || liveStatus === statusFilter);
    });
  }, [resources, resourceSearch, statusFilter, liveSnapshots]);

  async function refreshCluster() {
    if (clusterBusy.current || document.hidden) return;
    clusterBusy.current = true;
    clusterRequest.current?.abort();
    const controller = new AbortController();
    clusterRequest.current = controller;
    try {
      const next = await adminApi.clusterSummary(controller.signal);
      if (!controller.signal.aborted) {
        setCluster(next);
        setRealtimeError(null);
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        setRealtimeError(error instanceof Error ? error.message : "实时资源数据不可用");
      }
    } finally {
      if (clusterRequest.current === controller) clusterRequest.current = null;
      clusterBusy.current = false;
    }
  }

  async function refreshSnapshot(resource = selectedResource) {
    if (
      !resource
      || !resource.hasRuntimeBinding
      || snapshotBusy.current
      || document.hidden
    ) return;
    snapshotBusy.current = true;
    snapshotRequest.current?.abort();
    const controller = new AbortController();
    snapshotRequest.current = controller;
    try {
      const [next, health] = await Promise.all([
        adminApi.runtimeKubernetes(resource, controller.signal),
        typeof adminApi.runtimeHealth === "function"
          ? adminApi.runtimeHealth(resource, controller.signal).catch(() => null)
          : Promise.resolve(null)
      ]);
      if (!controller.signal.aborted) {
        setSnapshot(next);
        setRuntimeHealth(health);
        setSnapshotError(null);
        setLiveSnapshots((current) => ({
          ...current,
          [resource.resourceKey]: next
        }));
        setRealtimeError(null);
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        const message = error instanceof Error ? error.message : "实时资源数据不可用";
        setSnapshotError(message);
        setRealtimeError(message);
      }
    } finally {
      if (snapshotRequest.current === controller) {
        snapshotRequest.current = null;
        snapshotBusy.current = false;
      }
    }
  }

  async function refreshRuntimeList() {
    if (listBusy.current || document.hidden) return;
    listBusy.current = true;
    listRequest.current?.abort();
    const controller = new AbortController();
    listRequest.current = controller;
    try {
      const snapshots = await adminApi.runtimeKubernetesSnapshots(controller.signal);
      if (!controller.signal.aborted) {
        setLiveSnapshots(Object.fromEntries(
          snapshots.map((item) => [item.resourceKey, item])
        ));
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        setRealtimeError(error instanceof Error ? error.message : "Runtime 列表实时状态不可用");
      }
    } finally {
      if (listRequest.current === controller) listRequest.current = null;
      listBusy.current = false;
    }
  }

  useEffect(() => {
    void refreshCluster();
    if (!autoRefresh) return;
    const timer = window.setInterval(() => void refreshCluster(), 30_000);
    return () => window.clearInterval(timer);
  }, [autoRefresh]);

  useEffect(() => {
    void refreshRuntimeList();
    if (!autoRefresh) return;
    const timer = window.setInterval(() => void refreshRuntimeList(), 5_000);
    return () => {
      window.clearInterval(timer);
      listRequest.current?.abort();
    };
  }, [autoRefresh, resources.length]);

  useEffect(() => {
    const resourceKey = selectedResource?.resourceKey ?? null;
    const workspaceChanged = snapshotResourceKey.current !== resourceKey;
    if (workspaceChanged) {
      snapshotResourceKey.current = resourceKey;
      setSnapshot(null);
      setRuntimeHealth(null);
      setSnapshotError(null);
      setSafeYaml(null);
      setPodName("");
      setContainer("");
      stopFollowing();
    }
    snapshotRequest.current?.abort();
    snapshotBusy.current = false;
    if (!selectedResource?.hasRuntimeBinding) return;
    void refreshSnapshot(selectedResource);
    if (!autoRefresh) return;
    const timer = window.setInterval(
      () => void refreshSnapshot(selectedResource),
      3_000
    );
    return () => {
      window.clearInterval(timer);
      snapshotRequest.current?.abort();
    };
  }, [selectedResource?.resourceKey, selectedResource?.hasRuntimeBinding, autoRefresh]);

  useEffect(() => {
    const firstPod = snapshot?.pods[0];
    if (!firstPod) return;
    if (!snapshot.pods.some((pod) => pod.name === podName)) {
      setPodName(firstPod.name);
      setContainer(defaultContainerName(firstPod));
    }
  }, [snapshot, podName]);

  useEffect(() => () => {
    stopFollowing();
  }, []);

  useEffect(() => {
    if (detailTab !== "yaml" || !selectedResource || safeYaml) return;
    adminApi.runtimeSafeYaml(selectedResource)
      .then(setSafeYaml)
      .catch((error) => onError(error instanceof Error ? error.message : "YAML 摘要加载失败"));
  }, [detailTab, selectedResource?.resourceKey, safeYaml]);

  function selectPod(nextPod: string) {
    setPodName(nextPod);
    const pod = snapshot?.pods.find((item) => item.name === nextPod);
    setContainer(defaultContainerName(pod));
    stopFollowing();
  }

  async function loadLogSnapshot() {
    if (!selectedResource || !podName || !container) return;
    stopFollowing();
    setLogState("loading");
    const controller = new AbortController();
    logController.current = controller;
    try {
      const text = await adminApi.podLogs(
        selectedResource,
        podName,
        { container, tailLines, previous },
        controller.signal
      );
      setLogLines(text.split(/\r?\n/).filter(Boolean).slice(-5000));
      setLogState("idle");
    } catch (error) {
      if (!controller.signal.aborted) {
        setLogState("disconnected");
        onError(error instanceof Error ? error.message : "日志加载失败");
      }
    }
  }

  async function startFollowing() {
    if (!selectedResource || !podName || !container) return;
    stopFollowing();
    const controller = new AbortController();
    logController.current = controller;
    setLogState("following");
    try {
      const response = await adminApi.followPodLogs(
        selectedResource,
        podName,
        { container, tailLines: Math.min(tailLines, 2000) },
        controller.signal
      );
      if (!response.body) throw new Error("浏览器不支持实时日志流");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (!controller.signal.aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parsed = parseSseBuffer(buffer);
        buffer = parsed.remainder;
        for (const event of parsed.events) {
          if (event.event === "log.line" && typeof event.data.line === "string") {
            setLogLines((current) => [...current, event.data.line as string].slice(-5000));
          }
          if (event.event === "log.error") {
            throw new Error(String(event.data.message || "日志流已断开"));
          }
        }
      }
      if (!controller.signal.aborted) setLogState("disconnected");
    } catch (error) {
      if (!controller.signal.aborted) {
        setLogState("disconnected");
        onError(error instanceof Error ? error.message : "日志流已断开");
      }
    }
  }

  function stopFollowing() {
    logController.current?.abort();
    logController.current = null;
    setLogState((current) => current === "following" || current === "loading" ? "idle" : current);
  }

  return (
    <section className="runtimeConsole" aria-label="Runtime 资源控制台">
      <div className="runtimeConsoleToolbar">
        <div>
          <h2>Kubernetes / Knative 资源</h2>
          <span>平台归属校验后的实时视图</span>
        </div>
        <div className="runtimeToolbarActions">
          <label className="toggleLabel">
            <input type="checkbox" checked={autoRefresh} onChange={(event) => setAutoRefresh(event.target.checked)} />
            自动刷新
          </label>
          <button type="button" className="secondaryButton" onClick={() => void Promise.all([refreshCluster(), refreshRuntimeList(), refreshSnapshot()])}>
            <RefreshCw size={15} aria-hidden />
            刷新
          </button>
        </div>
      </div>

      {realtimeError && (
        <div className="runtimeRealtimeError" role="status">
          <AlertTriangle size={16} aria-hidden />
          <span>实时资源数据不可用：{realtimeError}。数据库 Workspace 列表仍可使用。</span>
        </div>
      )}

      <AdminClusterCapacityOverview capacity={cluster?.capacity} />

      <div className="clusterMetricGrid">
        <Metric icon={Server} label="Ready Nodes" value={cluster ? `${cluster.readyNodeCount}/${cluster.nodeCount}` : "-"} />
        <Metric icon={Boxes} label="Running Pods" value={cluster?.runningPodCount ?? "-"} />
        <Metric icon={Gauge} label="Ready Runtime" value={cluster?.readyRuntimeCount ?? "-"} />
        <Metric icon={CircleStop} label="Scale to zero" value={cluster?.scaledToZeroRuntimeCount ?? "-"} />
        <Metric icon={Play} label="Starting" value={cluster?.startingRuntimeCount ?? "-"} />
        <Metric icon={Flame} label="Warning Events" value={cluster?.warningEventCount ?? "-"} />
        <Metric icon={Workflow} label="Knative" value={cluster ? (cluster.knativeControlPlaneReady ? "健康" : "异常") : "-"} />
      </div>
      <p className="runtimeCollectedAt">采集时间：{displayTime(cluster?.collectedAt)}</p>

      <div className="runtimeConsoleBody">
        <aside className="runtimeResourceList" aria-label="Runtime Workspace 列表">
          <div className="runtimeListHeader">
            <strong>Runtime Workspaces</strong>
            <span>{resources.length}</span>
          </div>
          <div className="runtimeFilters">
            <input
              aria-label="筛选 Runtime"
              value={resourceSearch}
              onChange={(event) => setResourceSearch(event.target.value)}
              placeholder="用户 / 员工号 / Workspace"
            />
            <select aria-label="筛选 Runtime 状态" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
              <option value="">全部状态</option>
              {Object.entries(statusText).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
          </div>
          {filteredResources.map((resource) => {
            const liveStatus =
              liveSnapshots[resource.resourceKey]?.aggregateStatus ?? null;
            return (
              <button
                className="runtimeResourceRow"
                data-active={
                  resource.resourceKey === selectedResource?.resourceKey
                }
                type="button"
                key={resource.resourceKey}
                onClick={() => {
                  if (onSelectResource) {
                    void onSelectResource(resource.resourceKey);
                  } else {
                    void onSelectRuntime?.(resource.runtimeWorkspaceId);
                  }
                }}
              >
                <span className="runtimeRowTitle">
                  <strong>
                    <em
                      className="runtimeKindBadge"
                      data-kind={resource.runtimeKind}
                    >
                      {resource.runtimeKind === "application"
                        ? "应用 Agent"
                        : "个人 Agent"}
                    </em>
                    {resource.runtimeKind === "application"
                      ? resource.runtimeName
                      : userIdentity(resource)}
                  </strong>
                  <i className={`runtimeStatus runtimeStatus-${liveStatus ?? "unknown"}`}>
                    {liveStatus
                      ? statusText[liveStatus]
                      : resource.hasRuntimeBinding
                        ? resource.runtimeStatus || "等待实时数据"
                        : "Runtime 尚未创建"}
                  </i>
                </span>
                <small className="runtimeResourceIdentity">
                  {resource.runtimeKind === "application"
                    ? resourceIdentity(resource)
                    : resource.runtimeName}
                  {" · "}
                  {resourceIdLabel(resource)}
                </small>
                <small>{resource.activeRunCount} active runs</small>
              </button>
            );
          })}
          {filteredResources.length === 0 && <p className="muted">没有符合条件的 Runtime Workspace。</p>}
        </aside>

        <section className="runtimeDetail" aria-label="Runtime 资源详情">
          {selectedResource ? (
            <>
              <div className="runtimeDetailHeader">
                <div>
                  <h3>{selectedResource.runtimeName}</h3>
                  <span>
                    {resourceIdentity(selectedResource)}
                    {" / "}
                    {resourceIdLabel(selectedResource)}
                  </span>
                </div>
                {snapshot && (
                  <i className={`runtimeStatus runtimeStatus-${snapshot.aggregateStatus}`}>
                    {statusText[snapshot.aggregateStatus]}
                  </i>
                )}
              </div>
              {snapshot?.stateMismatch && (
                <div className="runtimeMismatch">数据库 Binding 与 Kubernetes 实时状态不一致，请以两侧明细排查。</div>
              )}
              <div className="runtimeTabs" role="tablist">
                {tabs.map((item) => (
                  <button
                    type="button"
                    role="tab"
                    aria-selected={detailTab === item.key}
                    key={item.key}
                    onClick={() => setDetailTab(item.key)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
              <div className="runtimeTabPanel">{renderDetailTab()}</div>
            </>
          ) : (
            <p className="muted">选择一个 Runtime Workspace 查看详情。</p>
          )}
        </section>
      </div>

    </section>
  );

  function renderDetailTab() {
    if (selectedResource && !selectedResource.hasRuntimeBinding) {
      return (
        <div className="runtimeSnapshotError" role="status">
          <AlertTriangle size={18} aria-hidden />
          <div>
            <strong>Runtime 尚未创建</strong>
            <span>该应用 Agent 尚未生成 Runtime Binding，调试或发布后会自动出现在实时资源视图中。</span>
          </div>
        </div>
      );
    }
    if (!snapshot) {
      if (snapshotError) {
        return (
          <div className="runtimeSnapshotError" role="alert" aria-label="Runtime 详情加载失败">
            <AlertTriangle size={18} aria-hidden />
            <div>
              <strong>Runtime 详情加载失败</strong>
              <span>{snapshotError}</span>
            </div>
            <button
              type="button"
              className="secondaryButton"
              aria-label="重试 Runtime 详情"
              onClick={() => {
                setSnapshotError(null);
                void refreshSnapshot();
              }}
            >
              <RefreshCw size={15} aria-hidden />
              重试
            </button>
          </div>
        );
      }
      return <p className="muted">正在加载实时 Runtime 数据…</p>;
    }
    if (detailTab === "overview") {
      const totalCpu = sumKnown(snapshot.pods.map((pod) => pod.cpuUsageMillicores));
      const totalMemory = sumKnown(snapshot.pods.map((pod) => pod.memoryUsageBytes));
      const userContainer = containerMetricTotals(snapshot.pods, "user-container");
      const queueProxy = containerMetricTotals(snapshot.pods, "queue-proxy");
      return (
        <div className="runtimeOverviewGrid">
          {snapshot.aggregateStatus === "scaled_to_zero" && (
            <article className="runtimeInfoCard">
              <span>自动唤醒</span>
              <strong>Runtime 已缩容到 0</strong>
              <small>Web、定时任务或 IM 的下一个请求会自动唤醒 Runtime。</small>
            </article>
          )}
          <ResourceMetricCard label="CPU 当前用量" value={formatCpu(totalCpu)} />
          <ResourceMetricCard label="内存当前用量" value={formatMemory(totalMemory)} />
          <ResourceMetricCard
            label="user-container CPU / 内存"
            value={`${formatCpu(userContainer.cpu)} / ${formatMemory(userContainer.memory)}`}
          />
          <ResourceMetricCard
            label="queue-proxy CPU / 内存"
            value={`${formatCpu(queueProxy.cpu)} / ${formatMemory(queueProxy.memory)}`}
          />
          <ResourceMetricCard label="Pod 数量" value={snapshot.pods.length} />
          {runtimeHealth && (
            <>
              <ResourceMetricCard
                label="Supervisor / Hermes"
                value={`${runtimeHealth.summary.state ?? "UNKNOWN"}${runtimeHealth.stale ? "（已过期）" : ""}`}
              />
              <ResourceMetricCard
                label="活动 Run"
                value={`${runtimeHealth.summary.runs?.active ?? "-"}/${runtimeHealth.summary.runs?.limit ?? "-"}`}
              />
              <ResourceMetricCard
                label="外部 WebSocket"
                value={`${runtimeHealth.summary.websockets?.active ?? "-"}/${runtimeHealth.summary.websockets?.limit ?? "-"}`}
              />
              <ResourceMetricCard
                label="常驻 / 热 Session"
                value={`${runtimeHealth.summary.sessions?.resident ?? "-"}/${runtimeHealth.summary.sessions?.residentLimit ?? "-"} · 热 ${runtimeHealth.summary.sessions?.warmIdle ?? "-"}`}
              />
              <ResourceMetricCard
                label="PVC 水位"
                value={runtimeHealth.summary.storage
                  ? `${runtimeHealth.summary.storage.usedPercent ?? "-"}% · ${runtimeHealth.summary.storage.level ?? "unknown"}`
                  : "未上报"}
              />
              <ResourceMetricCard
                label="Outbox"
                value={`${runtimeHealth.summary.outbox?.pendingCount ?? "-"} 待投影 · 最老 ${runtimeHealth.summary.outbox?.oldestAgeSeconds ?? "-"}s`}
              />
              <ResourceMetricCard
                label="Model Relay"
                value={`${runtimeHealth.summary.modelRelay?.active ?? "-"}/${runtimeHealth.summary.modelRelay?.capacity ?? "-"} · ${runtimeHealth.summary.modelRelay?.transportWarmup ?? "unknown"}`}
              />
              <ResourceMetricCard
                label="Config Revision"
                value={`${runtimeHealth.summary.configRevision ?? "-"} · ${runtimeHealth.summary.configDigest
                  ? String(runtimeHealth.summary.configDigest).slice(0, 12)
                  : "无 digest"}`}
              />
              <ResourceMetricCard
                label="SQLite 提交 P95"
                value={`${runtimeHealth.summary.sqlite?.commitP95Ms ?? "-"}ms · ${runtimeHealth.summary.sqlite?.writerQueueDepth ?? "-"} 排队`}
              />
              <ResourceMetricCard
                label="实时缓冲"
                value={`${runtimeHealth.summary.realtime?.bufferBytes ?? "-"}B · 慢连接 ${runtimeHealth.summary.realtime?.slowClientDisconnects ?? "-"}`}
              />
            </>
          )}
          <ServiceCard service={snapshot.runtimeService} />
        </div>
      );
    }
    if (detailTab === "pods") {
      return (
        <div className="runtimeTable">
          <div className="runtimeTableRow runtimeTableHead"><span>Pod</span><span>Phase / Ready</span><span>CPU</span><span>内存</span><span>容器</span><span>重启</span><span>节点 / 原因</span></div>
          {snapshot.pods.map((pod) => (
            <div className="runtimeTableRow" key={pod.name}>
              <strong>{pod.name}</strong>
              <span>{pod.phase} / {pod.ready ? "Ready" : "Not Ready"}</span>
              <span>{formatCpu(pod.cpuUsageMillicores)}</span>
              <span>{formatMemory(pod.memoryUsageBytes)}</span>
              <span>{pod.containers.join(", ")}</span>
              <span>{pod.restartCount}</span>
              <span>{pod.nodeName ?? "-"} / {pod.waitingReason ?? pod.terminatedReason ?? "-"}</span>
            </div>
          ))}
          {snapshot.pods.length === 0 && <p className="muted">当前没有 Pod，Runtime 可能已缩容到 0。</p>}
        </div>
      );
    }
    if (detailTab === "revisions") {
      return <div className="runtimeOverviewGrid"><RevisionCard title="Runtime" service={snapshot.runtimeService} /></div>;
    }
    if (detailTab === "autoscaler") {
      return <div className="runtimeOverviewGrid"><AutoscalerCard service={snapshot.runtimeService} /></div>;
    }
    if (detailTab === "events") {
      return (
        <div className="eventList">
          {snapshot.recentEvents.map((event, index) => (
            <article key={`${event.involvedName}-${event.reason}-${index}`} data-warning={event.eventType === "Warning"}>
              <strong>{event.eventType} · {event.reason}</strong>
              <span>{event.involvedKind}/{event.involvedName} · {displayTime(event.occurredAt)}</span>
              <p>{event.message}</p>
            </article>
          ))}
          {snapshot.recentEvents.length === 0 && <p className="muted">最近没有相关 Event。</p>}
        </div>
      );
    }
    if (detailTab === "logs") return renderLogs(snapshot.pods);
    if (detailTab === "yaml") {
      return <pre className="safeYaml">{safeYaml ? JSON.stringify(safeYaml.objects, null, 2) : "正在生成安全摘要…"}</pre>;
    }
    return null;
  }

  function renderLogs(pods: AdminPodSummary[]) {
    return (
      <div className="runtimeLogs">
        <div className="logControls">
          <label>Pod<select value={podName} onChange={(event) => selectPod(event.target.value)}>{pods.map((pod) => <option key={pod.name}>{pod.name}</option>)}</select></label>
          <label>容器<select value={container} onChange={(event) => setContainer(event.target.value)}>{(pods.find((pod) => pod.name === podName)?.containers ?? []).map((name) => <option key={name}>{name}</option>)}</select></label>
          <label>行数<input type="number" min={1} max={2000} value={tailLines} onChange={(event) => setTailLines(Math.min(2000, Math.max(1, Number(event.target.value) || 1)))} /></label>
          <label className="toggleLabel"><input type="checkbox" checked={previous} onChange={(event) => setPrevious(event.target.checked)} />Previous</label>
          <button type="button" onClick={() => void loadLogSnapshot()} disabled={!podName || !container || logState === "following"}><ScrollText size={15} />快照</button>
          {logState === "following"
            ? <button type="button" className="secondaryButton" onClick={stopFollowing}><CircleStop size={15} />停止 Follow</button>
            : <button type="button" onClick={() => void startFollowing()} disabled={!podName || !container}><SquareTerminal size={15} />Follow</button>}
        </div>
        <p className="logSafetyNotice">日志可能包含敏感业务信息；平台会做常见凭证脱敏，但不会保存日志正文。</p>
        <pre className="logViewer">{logLines.length ? logLines.join("\n") : "选择 Pod 和容器后加载日志。"}</pre>
        {logState === "disconnected" && <button type="button" className="secondaryButton" onClick={() => void startFollowing()}>重新连接</button>}
      </div>
    );
  }
}

function Metric({ icon: Icon, label, value }: { icon: typeof Server; label: string; value: string | number }) {
  return <div><Icon size={17} aria-hidden /><span>{label}</span><strong>{value}</strong></div>;
}

function ResourceMetricCard({ label, value }: { label: string; value: string | number }) {
  return (
    <article className="runtimeInfoCard runtimeResourceMetricCard">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function ServiceCard({ service }: { service: AdminRuntimeKubernetesSnapshot["runtimeService"] }) {
  return (
    <article className="runtimeInfoCard">
      <span>Runtime Service</span>
      <strong>{service.kserviceName}</strong>
      <small>Dashboard 9119</small>
      <small>状态：{statusText[service.status]}</small>
      <small>Revision：{service.latestReadyRevision ?? service.latestCreatedRevision ?? "-"}</small>
      <small>Desired / Actual：{scaleText(service.desiredScale, service.actualScale)}</small>
    </article>
  );
}

function RevisionCard({ title, service }: { title: string; service: AdminRuntimeKubernetesSnapshot["runtimeService"] }) {
  return (
    <article className="runtimeInfoCard">
      <strong>{title}</strong>
      <small>Latest Created：{service.latestCreatedRevision ?? "-"}</small>
      <small>Latest Ready：{service.latestReadyRevision ?? "-"}</small>
      <small>Traffic：{service.trafficRevision ?? "-"}</small>
    </article>
  );
}

function AutoscalerCard({ service }: { service: AdminRuntimeKubernetesSnapshot["runtimeService"] }) {
  return (
    <article className="runtimeInfoCard">
      <strong>{service.kserviceName}</strong>
      <small>Desired / Actual：{scaleText(service.desiredScale, service.actualScale)}</small>
      <small>Active：{service.active == null ? "-" : service.active ? "True" : "False"}</small>
      <small>Scale-down delay：{service.scaleDownDelaySeconds == null ? "-" : `${service.scaleDownDelaySeconds}s`}</small>
    </article>
  );
}

export function parseSseBuffer(buffer: string): {
  events: Array<{ event: string; data: Record<string, unknown> }>;
  remainder: string;
} {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const remainder = blocks.pop() ?? "";
  const events = blocks.flatMap((block) => {
    let event = "message";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return [];
    try {
      const data = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
      return [{ event, data }];
    } catch {
      return [];
    }
  });
  return { events, remainder };
}
