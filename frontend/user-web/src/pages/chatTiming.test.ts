import { describe, expect, it } from "vitest";

import { TurnTimingCoordinator } from "./chatTiming";

describe("TurnTimingCoordinator", () => {
  it("migrates a local turn to a run and completes one monotonic visible sample", () => {
    const timing = new TurnTimingCoordinator();
    timing.begin("turn-1", 100, "visible", false);
    timing.markFirstEvent("turn-1", 110);
    timing.accept("turn-1", "rr-1");

    expect(timing.offerVisibleCandidate("rr-1", "assistant-rr-1", 120, "visible")).toBe(true);
    expect(timing.markVisibleCommit("rr-1", "assistant-rr-1", 125, "visible")).toBe(true);
    expect(timing.markVisiblePaint("rr-1", "assistant-rr-1", 140, "visible")).toEqual({
      requestToFirstEventMs: 10,
      requestToFirstVisibleReceivedMs: 20,
      requestToFirstVisibleCommitMs: 25,
      requestToFirstVisiblePaintMs: 40,
      dispatchToFirstVisibleReceivedMs: 20,
      dispatchToFirstVisibleCommitMs: 25,
      dispatchToFirstVisiblePaintMs: 40,
      queuedWaitMs: 0,
      requiredPrewarmWaitMs: 0,
      documentVisibility: "visible",
      reconnectBeforeFirstVisible: false,
      sendOverlappedPrewarm: false
    });
    expect(timing.markVisiblePaint("rr-1", "assistant-rr-1", 150, "visible")).toBeUndefined();
  });

  it("rejects target mismatch and allows an unpainted candidate to be abandoned", () => {
    const timing = new TurnTimingCoordinator();
    timing.begin("turn-1", 100, "visible", true);
    timing.accept("turn-1", "rr-1");
    timing.offerVisibleCandidate("rr-1", "activity-rr-1", 110, "visible");

    expect(timing.markVisibleCommit("rr-1", "assistant-rr-1", 115, "visible")).toBe(false);
    expect(timing.abandonVisibleCandidate("rr-1", "activity-rr-1")).toBe(true);
    expect(timing.offerVisibleCandidate("rr-1", "assistant-rr-1", 120, "visible")).toBe(true);
  });

  it("preserves reconnect state and cancels samples that become hidden", () => {
    const timing = new TurnTimingCoordinator();
    timing.begin("turn-1", 100, "visible", false);
    timing.accept("turn-1", "rr-1");
    timing.markReconnect("rr-1");

    expect(timing.offerVisibleCandidate("rr-1", "assistant-rr-1", 120, "hidden")).toBe(false);
    expect(timing.has("rr-1")).toBe(false);
  });

  it("keeps the required prewarm wait with the sample after run acceptance", () => {
    const timing = new TurnTimingCoordinator();
    timing.begin("turn-1", 100, "visible", true);
    timing.markRequiredPrewarmWait("turn-1", 37.5);
    timing.markDispatchStarted("turn-1", 140, 500);
    timing.accept("turn-1", "rr-1");
    timing.offerVisibleCandidate("rr-1", "assistant-rr-1", 150, "visible");
    timing.markVisibleCommit("rr-1", "assistant-rr-1", 155, "visible");

    expect(timing.markVisiblePaint(
      "rr-1",
      "assistant-rr-1",
      160,
      "visible"
    )).toEqual(expect.objectContaining({
      requiredPrewarmWaitMs: 37.5,
      queuedWaitMs: 40,
      receiptAgeMs: 500,
      dispatchToFirstVisiblePaintMs: 20,
      sendOverlappedPrewarm: true
    }));
  });

  it("cancels local and accepted samples without leaking state", () => {
    const timing = new TurnTimingCoordinator();
    timing.begin("turn-1", 100, "visible", false);
    timing.cancel("turn-1");
    expect(timing.has("turn-1")).toBe(false);

    timing.begin("turn-2", 100, "visible", false);
    timing.accept("turn-2", "rr-2");
    timing.cancel("rr-2");
    expect(timing.has("rr-2")).toBe(false);
  });
});
