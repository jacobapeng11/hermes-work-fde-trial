import { describe, expect, it, vi } from "vitest";

import { SessionPrewarmCoordinator } from "./sessionPrewarmCoordinator";

describe("SessionPrewarmCoordinator", () => {
  it("shares only a concurrently running request", async () => {
    let release!: (state: { state: "ready"; readyReceiptId: string }) => void;
    const firstRequest = vi.fn(() => new Promise<{ state: "ready"; readyReceiptId: string }>(
      (resolve) => { release = resolve; }
    ));
    const coordinator = new SessionPrewarmCoordinator();

    expect(coordinator.has("1:rs-1")).toBe(false);
    const first = coordinator.ensure("1:rs-1", firstRequest);
    const concurrent = coordinator.ensure("1:rs-1", firstRequest);
    expect(coordinator.has("1:rs-1")).toBe(true);
    expect(firstRequest).toHaveBeenCalledTimes(1);
    release({ state: "ready", readyReceiptId: "receipt-1" });
    await Promise.all([first, concurrent]);
    expect(coordinator.has("1:rs-1")).toBe(false);

    const laterRequest = vi.fn(async () => ({ state: "ready" as const }));
    await coordinator.ensure("1:rs-1", laterRequest);
    expect(laterRequest).toHaveBeenCalledTimes(1);
  });

  it("does not cache skipped, failed, or completed results", async () => {
    const coordinator = new SessionPrewarmCoordinator();
    const request = vi.fn()
      .mockResolvedValueOnce({ state: "skipped-unmaterialized" as const })
      .mockRejectedValueOnce(new Error("not ready"))
      .mockResolvedValueOnce({ state: "ready" as const })
      .mockResolvedValueOnce({ state: "ready" as const });

    await coordinator.ensure("1:rs-1", request);
    await expect(coordinator.ensure("1:rs-1", request)).rejects.toThrow("not ready");
    expect(coordinator.has("1:rs-1")).toBe(false);
    await coordinator.ensure("1:rs-1", request);
    await coordinator.ensure("1:rs-1", request);
    expect(request).toHaveBeenCalledTimes(4);
  });

  it("invalidation permits a new request while an obsolete request settles", async () => {
    let releaseOld!: (state: { state: "ready" }) => void;
    const oldRequest = vi.fn(() => new Promise<{ state: "ready" }>(
      (resolve) => { releaseOld = resolve; }
    ));
    const newRequest = vi.fn(async () => ({ state: "ready" as const }));
    const coordinator = new SessionPrewarmCoordinator();

    const oldPending = coordinator.ensure("7:rs-1", oldRequest);
    expect(coordinator.has("7:rs-1")).toBe(true);
    coordinator.invalidate("7:rs-1");
    expect(coordinator.has("7:rs-1")).toBe(false);
    await coordinator.ensure("7:rs-1", newRequest);
    releaseOld({ state: "ready" });
    await oldPending;

    expect(oldRequest).toHaveBeenCalledTimes(1);
    expect(newRequest).toHaveBeenCalledTimes(1);
  });
});
