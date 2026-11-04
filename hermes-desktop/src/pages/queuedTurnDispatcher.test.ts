import { describe, expect, it } from "vitest";

import { QueuedTurnDispatcher } from "./queuedTurnDispatcher";
import type { SessionReadinessTarget } from "./sessionReadinessState";

const target = (epoch = 1): SessionReadinessTarget => ({
  workspaceId: 1,
  sessionId: "rs-1",
  modelId: 7,
  agentProfileKind: "personal",
  runtimeAgentProfileId: null,
  epoch
});

describe("QueuedTurnDispatcher", () => {
  it("allows one queued turn and one synchronous dispatch claim", () => {
    const dispatcher = new QueuedTurnDispatcher<{ message: string }>();
    expect(dispatcher.enqueue({
      runtimeRunId: "web-run-1",
      target: target(),
      payload: { message: "hello" },
      userClickedAt: 10
    })).toBe(true);
    expect(dispatcher.enqueue({
      runtimeRunId: "web-run-2",
      target: target(),
      payload: { message: "second" },
      userClickedAt: 11
    })).toBe(false);

    expect(dispatcher.claimForDispatch("web-run-1", target())?.payload.message)
      .toBe("hello");
    expect(dispatcher.claimForDispatch("web-run-1", target())).toBeNull();
  });

  it("rejects stale targets and returns the frozen turn on cancel", () => {
    const dispatcher = new QueuedTurnDispatcher<{ message: string }>();
    dispatcher.enqueue({
      runtimeRunId: "web-run-1",
      target: target(3),
      payload: { message: "frozen" },
      userClickedAt: 10
    });

    expect(dispatcher.claimForDispatch("web-run-1", target(4))).toBeNull();
    expect(dispatcher.cancel("web-run-1")?.payload.message).toBe("frozen");
    expect(dispatcher.peek()).toBeNull();
  });
});
