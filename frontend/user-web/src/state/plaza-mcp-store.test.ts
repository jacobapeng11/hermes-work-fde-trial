import { afterEach, describe, expect, it, vi } from "vitest";
import type { DefaultProfileAssetView } from "@hermes-cloud/schemas";
import { userApi } from "../api/client";
import { PlazaMcpStore } from "./plaza-mcp-store";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

const accepted = {
  operationId: "op-new",
  runtimeWorkspaceId: 7,
  runtimeAgentProfileId: 70,
  assetType: "mcp" as const,
  assetId: 8,
  action: "install" as const,
  desiredRevision: 2,
  appliedRevision: 1,
  convergenceStatus: "updating",
};

const view = (desired: boolean, applied: boolean): DefaultProfileAssetView => ({
  runtimeWorkspaceId: 7,
  runtimeAgentProfileId: 70,
  desiredRevision: desired ? 2 : 1,
  appliedRevision: applied ? 2 : 1,
  convergenceStatus: desired === applied ? "applied" : "updating",
  skills: [],
  mcps: [{
    assetType: "mcp",
    assetId: 8,
    currentVersionId: 18,
    desired,
    applied,
    name: "Fresh MCP",
    status: "published",
    transport: "stdio",
  }],
});

describe("PlazaMcpStore", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.clearAllTimers();
  });

  it("keeps the post-install view when an earlier list request completes last", async () => {
    const oldRead = deferred<DefaultProfileAssetView>();
    const freshRead = deferred<DefaultProfileAssetView>();
    vi.spyOn(userApi, "defaultProfileAssets")
      .mockReturnValueOnce(oldRead.promise)
      .mockReturnValueOnce(freshRead.promise);
    vi.spyOn(userApi, "installDefaultProfileAsset").mockResolvedValue(accepted);
    const store = new PlazaMcpStore();

    void store.load(7);
    await Promise.resolve();
    const install = store.submitInstall(8);
    await Promise.resolve();
    expect(userApi.defaultProfileAssets).toHaveBeenCalledTimes(2);

    freshRead.resolve(view(true, false));
    await install;
    oldRead.resolve(view(false, false));
    await Promise.resolve();

    expect(store.getSnapshot().items[0]?.desired).toBe(true);
    expect(store.getSnapshot().items[0]?.applied).toBe(false);
    expect(store.getSnapshot().items[0]?.convergenceStatus).toBe("updating");
  });

  it("shows an install operation immediately and keeps it until applied", async () => {
    const submission = deferred<typeof accepted>();
    const convergingRead = deferred<DefaultProfileAssetView>();
    vi.spyOn(userApi, "defaultProfileAssets")
      .mockResolvedValueOnce(view(false, false))
      .mockReturnValueOnce(convergingRead.promise)
      .mockResolvedValueOnce(view(true, true));
    vi.spyOn(userApi, "installDefaultProfileAsset").mockReturnValue(submission.promise);
    const store = new PlazaMcpStore();
    await store.load(7);

    const install = store.submitInstall(8);
    await store.submitInstall(8);
    expect(userApi.installDefaultProfileAsset).toHaveBeenCalledTimes(1);
    expect(store.getSnapshot().operations[8]).toBe("install");
    expect(store.getSnapshot().items[0]?.desired).toBe(false);

    submission.resolve(accepted);
    await install;
    expect(store.getSnapshot().operations[8]).toBe("install");
    expect(store.getSnapshot().items[0]?.desired).toBe(true);
    expect(store.getSnapshot().items[0]?.applied).toBe(false);

    convergingRead.resolve(view(true, false));
    await Promise.resolve();
    await store.load(7);
    expect(store.getSnapshot().operations[8]).toBeUndefined();
    expect(store.getSnapshot().items[0]?.applied).toBe(true);
  });

  it("does not roll an accepted install back when one status refresh fails", async () => {
    vi.spyOn(userApi, "defaultProfileAssets")
      .mockResolvedValueOnce(view(false, false))
      .mockRejectedValueOnce(new Error("temporary status failure"));
    vi.spyOn(userApi, "installDefaultProfileAsset").mockResolvedValue(accepted);
    const store = new PlazaMcpStore();
    await store.load(7);

    await store.submitInstall(8);
    await Promise.resolve();
    await Promise.resolve();

    expect(store.getSnapshot().operations[8]).toBe("install");
    expect(store.getSnapshot().items[0]?.desired).toBe(true);
    expect(store.getSnapshot().error).toBeNull();
  });

  it("clears the local operation when the install submission is rejected", async () => {
    vi.spyOn(userApi, "defaultProfileAssets").mockResolvedValue(view(false, false));
    vi.spyOn(userApi, "installDefaultProfileAsset")
      .mockRejectedValue(new Error("install unavailable"));
    const store = new PlazaMcpStore();
    await store.load(7);

    await store.submitInstall(8);

    expect(store.getSnapshot().operations[8]).toBeUndefined();
    expect(store.getSnapshot().items[0]?.desired).toBe(false);
    expect(store.getSnapshot().error).toBe("install unavailable");
  });
});
