import type { AdminClusterCapacity, AdminResourceAmount, AdminResourceUsage } from "@hermes-cloud/schemas";

type Props = { capacity?: AdminClusterCapacity | null };

const groupLabels: Record<string, string> = {
  application_runtime: "Application Runtime",
  personal_runtime: "Personal Runtime",
  knative: "Knative / Kourier",
  kubernetes_system: "Kubernetes System",
  other: "Other"
};

function cpu(value?: number | null) {
  return value == null ? "未采集" : `${value}m`;
}

function memory(value?: number | null) {
  if (value == null) return "未采集";
  if (value === 0) return "0Mi";
  return `${Math.max(1, Math.round(value / 1024 / 1024))}Mi`;
}

function ratio(value?: number | null) {
  return value == null ? "-" : `${Math.round(value * 100)}%`;
}

function amountText(amount: AdminResourceAmount) {
  return `${cpu(amount.cpuMillicores)} / ${memory(amount.memoryBytes)}`;
}

function usageText(usage: AdminResourceUsage) {
  return `${cpu(usage.cpuMillicores)} / ${memory(usage.memoryBytes)}`;
}

function severity(value?: number | null) {
  if (value == null) return "unknown";
  if (value >= 0.9) return "critical";
  if (value >= 0.8) return "warning";
  return "normal";
}

export function AdminClusterCapacityOverview({ capacity }: Props) {
  if (!capacity) return null;
  const cpuSeverity = severity(capacity.requestRatio.cpu);
  const memorySeverity = severity(capacity.requestRatio.memory);
  return (
    <section className="clusterCapacityOverview" aria-label="Kubernetes 集群容量">
      <div className="clusterCapacityHeader">
        <div>
          <h3>集群容量</h3>
          <span>Requests / Actual Usage（allocatable 分母）</span>
        </div>
        <div className="clusterCapacityStatus">
          {capacity.pendingPodCount > 0 && <span data-severity="critical">Pending {capacity.pendingPodCount}</span>}
          {capacity.pressureNodeCount > 0 && <span data-severity="critical">Node Pressure {capacity.pressureNodeCount}</span>}
          {capacity.metricsStale && <span data-severity="warning">Metrics 不完整</span>}
        </div>
      </div>
      <div className="clusterCapacityCards">
        <CapacityCard label="CPU" requests={cpu(capacity.requests.cpuMillicores)} requestRatio={ratio(capacity.requestRatio.cpu)} usage={cpu(capacity.actualUsage.cpuMillicores)} usageRatio={ratio(capacity.usageRatio.cpu)} headroom={cpu(capacity.headroom.cpuMillicores)} severity={cpuSeverity} />
        <CapacityCard label="内存" requests={memory(capacity.requests.memoryBytes)} requestRatio={ratio(capacity.requestRatio.memory)} usage={memory(capacity.actualUsage.memoryBytes)} usageRatio={ratio(capacity.usageRatio.memory)} headroom={memory(capacity.headroom.memoryBytes)} severity={memorySeverity} />
      </div>
      <div className="clusterCapacityMeta">Allocatable：{amountText(capacity.allocatable)} · Capacity：{amountText(capacity.capacity)} · 指标覆盖率：{Math.round(capacity.metricsCoverage * 100)}%</div>
      <div className="clusterCapacityGroups">
        <div className="clusterCapacityGroupRow clusterCapacityGroupHeader"><span>分组</span><span>Pod</span><span>Requests (CPU / 内存)</span><span>Actual (CPU / 内存)</span><span>缺失</span></div>
        {capacity.groups.map((group) => (
          <div className="clusterCapacityGroupRow" key={group.key}>
            <span>{groupLabels[group.key] ?? group.key}</span>
            <span>{group.podCount}（运行 {group.runningPodCount}）</span>
            <span>{amountText(group.requests)}</span>
            <span>{usageText(group.actualUsage)}</span>
            <span>{group.missingRequestPodCount || group.missingMetricsPodCount ? `${group.missingRequestPodCount}/${group.missingMetricsPodCount}` : "-"}</span>
          </div>
        ))}
      </div>
      <div className="clusterCapacityMeta">采集时间：{new Date(capacity.observedAt).toLocaleString()}</div>
    </section>
  );
}

function CapacityCard({ label, requests, requestRatio, usage, usageRatio, headroom, severity: level }: { label: string; requests: string; requestRatio: string; usage: string; usageRatio: string; headroom: string; severity: string }) {
  return (
    <article className="clusterCapacityCard" data-severity={level}>
      <strong>{label}</strong>
      <span>Requests {requests}（{requestRatio}）</span>
      <span>Actual {usage}（{usageRatio}）</span>
      <small>可调度余量 {headroom}</small>
    </article>
  );
}
