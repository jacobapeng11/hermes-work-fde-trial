import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { adminApi } from "../api/client";
import { AdminAssetRolloutMonitor } from "./AdminAssetRolloutMonitor";


vi.mock("../api/client", () => ({
  adminApi: {
    assetRollouts: vi.fn(),
    assetRollout: vi.fn(),
    retryFailedAssetRollout: vi.fn(),
    retryFailedAssetRolloutTarget: vi.fn()
  }
}));

const batch = {
  rolloutId: "rollout-skill-20260824",
  assetType: "skill" as const,
  assetId: 9,
  action: "publish" as const,
  sourceVersionId: 10,
  targetVersionId: 11,
  affectedUserCount: 2,
  affectedProfileCount: 3,
  waitingCount: 0,
  updatingCount: 0,
  appliedCount: 2,
  failedCount: 1,
  supersededCount: 0,
  revokedCount: 0,
  status: "partial_failed",
  createdAt: "2026-08-24T00:00:00Z",
  startedAt: "2026-08-24T00:00:01Z",
  completedAt: "2026-08-24T00:00:02Z"
};

const detail = {
  batch,
  totalUsers: 2,
  offset: 0,
  limit: 50,
  users: [{
    userId: 47,
    userCode: "10001",
    userName: "示例用户甲",
    status: "partial_failed" as const,
    profiles: [{
      targetId: 71,
      userId: 47,
      runtimeWorkspaceId: 119,
      runtimeAgentProfileId: 5,
      profileKind: "expert",
      profileName: "写作专家",
      targetRevision: 8,
      appliedRevision: 7,
      status: "failed",
      attemptCount: 1,
      errorCode: "PROFILE_PROJECTION_FAILED",
      errorMessage: "投影失败"
    }]
  }]
};

describe("AdminAssetRolloutMonitor", () => {
  beforeEach(() => {
    vi.mocked(adminApi.assetRollouts).mockResolvedValue([batch]);
    vi.mocked(adminApi.assetRollout).mockResolvedValue(detail);
    vi.mocked(adminApi.retryFailedAssetRollout).mockResolvedValue({ rolloutId: batch.rolloutId, queuedTargetCount: 1 });
    vi.mocked(adminApi.retryFailedAssetRolloutTarget).mockResolvedValue({ rolloutId: batch.rolloutId, queuedTargetCount: 1 });
  });

  it("shows affected users and Profile errors and can retry one failed target", async () => {
    render(<AdminAssetRolloutMonitor/>);

    expect(await screen.findByText("示例用户甲")).toBeVisible();
    expect(screen.getByText(/2 用户 \/ 3 Profile/)).toBeVisible();
    expect(screen.getByText(/PROFILE_PROJECTION_FAILED: 投影失败/)).toBeVisible();
    const userCard = screen.getByText("示例用户甲").closest("article");
    expect(userCard).not.toBeNull();
    await userEvent.click(within(userCard as HTMLElement).getByRole("button", { name: "重试" }));

    await waitFor(() => expect(adminApi.retryFailedAssetRolloutTarget).toHaveBeenCalledWith(batch.rolloutId, 71));
  });

  it("can requeue all failed Profiles in the batch", async () => {
    render(<AdminAssetRolloutMonitor/>);
    await userEvent.click(await screen.findByRole("button", { name: /重试全部失败/ }));
    await waitFor(() => expect(adminApi.retryFailedAssetRollout).toHaveBeenCalledWith(batch.rolloutId));
  });
});
