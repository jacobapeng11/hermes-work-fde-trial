import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AdminClusterCapacity } from "@hermes-cloud/schemas";
import { AdminClusterCapacityOverview } from "./AdminClusterCapacityOverview";

const capacity: AdminClusterCapacity = {
  capacity: { cpuMillicores: 4000, memoryBytes: 16 * 1024 * 1024 * 1024 },
  allocatable: { cpuMillicores: 3800, memoryBytes: 15 * 1024 * 1024 * 1024 },
  requests: { cpuMillicores: 2950, memoryBytes: 8 * 1024 * 1024 * 1024 },
  limits: { cpuMillicores: 8000, memoryBytes: 24 * 1024 * 1024 * 1024 },
  actualUsage: { cpuMillicores: 900, memoryBytes: 2 * 1024 * 1024 * 1024 },
  requestRatio: { cpu: 0.7763, memory: 0.5333 },
  usageRatio: { cpu: 0.2368, memory: 0.1333 },
  headroom: { cpuMillicores: 850, memoryBytes: 7 * 1024 * 1024 * 1024 },
  pendingPodCount: 0,
  pendingReasons: [],
  pressureNodeCount: 0,
  nodes: [],
  groups: [{ key: "application_runtime", podCount: 4, runningPodCount: 4, pendingPodCount: 0, requests: { cpuMillicores: 900, memoryBytes: 8 * 1024 * 1024 * 1024 }, limits: { cpuMillicores: 4000, memoryBytes: 16 * 1024 * 1024 * 1024 }, actualUsage: { cpuMillicores: 300, memoryBytes: 1024 * 1024 * 1024 }, missingRequestPodCount: 0, missingMetricsPodCount: 0 }],
  observedAt: "2026-09-01T00:00:00Z",
  metricsCoverage: 1,
  missingMetricsPodCount: 0,
  metricsStale: false
};

describe("AdminClusterCapacityOverview", () => {
  it("shows requests, actual usage and workload groups", () => {
    render(<AdminClusterCapacityOverview capacity={capacity} />);
    expect(screen.getByText("集群容量")).toBeVisible();
    expect(screen.getByText(/Requests 2950m/)).toBeVisible();
    expect(screen.getByText(/Actual 900m/)).toBeVisible();
    expect(screen.getByText("Application Runtime")).toBeVisible();
  });
});
