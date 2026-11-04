import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import {
  retryApplicationRuntimeBusy,
  waitForApplicationConfigApplyAfterBusy
} from "./applicationRuntimeRecovery";

const targetConfig = {
  systemPrompt: "只处理工单",
  modelEntryId: 1,
  skillAssetIds: [],
  mcpAssetIds: [],
  enterpriseKnowledgeEnabled: false,
  memoryEnabled: true
};

function agent(overrides: Record<string, unknown> = {}) {
  return {
    applicationAgentId: 31,
    slug: "ticket-agent",
    name: "工单助手",
    avatarUrl: null,
    status: "published" as const,
    runtimeStatus: "updating",
    configGeneration: 3,
    appliedGeneration: 2,
    applyStatus: "applying" as const,
    clientCount: 0,
    activeRunCount: 0,
    queuedRunCount: 0,
    publishedAt: null,
    updatedAt: null,
    accessMode: "owner" as const,
    lifecycleStatus: "active" as const,
    personalKnowledgeEnabledCount: 0,
    personalKnowledgeRevokedCount: 0,
    lastApplyErrorCode: null,
    config: targetConfig,
    ...overrides
  };
}

describe("application Runtime busy recovery", () => {
  it("retries only application_agent_runtime_busy and returns the ready result", async () => {
    const operation = vi.fn()
      .mockRejectedValueOnce(new ApiError(
        "application_agent_runtime_busy",
        409,
        "application_agent_runtime_busy"
      ))
      .mockResolvedValue({ runtimeStatus: "ready" });

    await expect(retryApplicationRuntimeBusy(operation, {
      delayMs: 0,
      timeoutMs: 100
    })).resolves.toEqual({ runtimeStatus: "ready" });
    expect(operation).toHaveBeenCalledTimes(2);
  });

  it("does not hide an unrelated conflict", async () => {
    const conflict = new ApiError(
      "application_agent_config_conflict",
      409,
      "application_agent_config_conflict"
    );

    await expect(retryApplicationRuntimeBusy(
      () => Promise.reject(conflict),
      { delayMs: 0, timeoutMs: 100 }
    )).rejects.toBe(conflict);
  });

  it("adopts the already-persisted target generation without a second PUT", async () => {
    const loadAgent = vi.fn()
      .mockResolvedValueOnce(agent())
      .mockResolvedValueOnce(agent({
        runtimeStatus: "ready",
        appliedGeneration: 3,
        applyStatus: "applied"
      }));

    await expect(waitForApplicationConfigApplyAfterBusy({
      reason: new ApiError(
        "application_agent_runtime_busy",
        409,
        "application_agent_runtime_busy"
      ),
      expectedGeneration: 3,
      targetConfig,
      loadAgent,
      delayMs: 0,
      timeoutMs: 100
    })).resolves.toMatchObject({
      configGeneration: 3,
      appliedGeneration: 3,
      applyStatus: "applied"
    });
    expect(loadAgent).toHaveBeenCalledTimes(2);
  });

  it("does not adopt another tab's configuration", async () => {
    const loadAgent = vi.fn().mockResolvedValue(agent({
      config: { ...targetConfig, modelEntryId: 19 }
    }));
    const busy = new ApiError(
      "application_agent_runtime_busy",
      409,
      "application_agent_runtime_busy"
    );

    await expect(waitForApplicationConfigApplyAfterBusy({
      reason: busy,
      expectedGeneration: 3,
      targetConfig,
      loadAgent,
      delayMs: 0,
      timeoutMs: 100
    })).rejects.toBe(busy);
  });

  it("surfaces a failed target generation instead of reporting success", async () => {
    await expect(waitForApplicationConfigApplyAfterBusy({
      reason: new ApiError(
        "application_agent_runtime_busy",
        409,
        "application_agent_runtime_busy"
      ),
      expectedGeneration: 3,
      targetConfig,
      loadAgent: () => Promise.resolve(agent({
        applyStatus: "failed",
        lastApplyErrorCode: "application_agent_runtime_reconcile_failed"
      })),
      delayMs: 0,
      timeoutMs: 100
    })).rejects.toThrow("application_agent_runtime_reconcile_failed");
  });

  it("times out while the matching target generation remains applying", async () => {
    await expect(waitForApplicationConfigApplyAfterBusy({
      reason: new ApiError(
        "application_agent_runtime_busy",
        409,
        "application_agent_runtime_busy"
      ),
      expectedGeneration: 3,
      targetConfig,
      loadAgent: () => Promise.resolve(agent()),
      delayMs: 0,
      timeoutMs: 0
    })).rejects.toThrow("应用 Agent Runtime 重新预热超时");
  });
});
