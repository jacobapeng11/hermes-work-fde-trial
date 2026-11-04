import { describe, expect, it } from "vitest";

import {
  initialSessionReadinessState,
  reduceSessionReadiness,
  type SessionReadinessTarget
} from "./sessionReadinessState";

const target = (epoch = 1): SessionReadinessTarget => ({
  workspaceId: 1,
  sessionId: "rs-1",
  modelId: 7,
  agentProfileKind: "personal",
  runtimeAgentProfileId: null,
  epoch
});

describe("reduceSessionReadiness", () => {
  it("accepts readiness only for the exact target epoch", () => {
    const started = reduceSessionReadiness(initialSessionReadinessState, {
      type: "prewarm_started",
      target: target(2)
    });
    const stale = reduceSessionReadiness(started, {
      type: "prewarm_ready",
      target: target(1),
      receipt: { state: "ready", readyReceiptId: "stale" }
    });
    const ready = reduceSessionReadiness(stale, {
      type: "prewarm_ready",
      target: target(2),
      receipt: { state: "ready", readyReceiptId: "current" }
    });

    expect(stale).toBe(started);
    expect(ready.status).toBe("ready");
    expect(ready.receipt?.readyReceiptId).toBe("current");
  });

  it("keeps a queued turn queued when its receipt arrives", () => {
    const started = reduceSessionReadiness(initialSessionReadinessState, {
      type: "prewarm_started",
      target: target()
    });
    const queued = reduceSessionReadiness(started, {
      type: "queued",
      target: target()
    });
    const ready = reduceSessionReadiness(queued, {
      type: "prewarm_ready",
      target: target(),
      receipt: { state: "ready", readyReceiptId: "receipt" }
    });

    expect(ready.status).toBe("queued");
    expect(ready.receipt?.readyReceiptId).toBe("receipt");
  });

  it("keeps a queued turn visible while prewarm retries", () => {
    const started = reduceSessionReadiness(initialSessionReadinessState, {
      type: "prewarm_started",
      target: target()
    });
    const queued = reduceSessionReadiness(started, {
      type: "queued",
      target: target()
    });
    const retrying = reduceSessionReadiness(queued, {
      type: "prewarm_retry_wait",
      target: target(),
      error: "channel draining"
    });

    expect(retrying.status).toBe("queued");
    expect(retrying.error).toBe("channel draining");
  });
});
