import { useSyncExternalStore } from "react";
import type { ProfileMcpAssetItem } from "@hermes-cloud/schemas";
import { userApi } from "../api/client";

export interface PlazaMcpStoreState {
  items: ProfileMcpAssetItem[];
  operations: Readonly<Record<number, "install" | "remove">>;
  loading: boolean;
  error: string | null;
}

const POLL_INTERVAL_MS = 1_000;
const RETRY_DELAYS_MS = [2_000, 4_000, 8_000, 10_000];
const emptyState: PlazaMcpStoreState = {
  items: [],
  operations: {},
  loading: false,
  error: null,
};
const isVisible = () => typeof document === "undefined" || document.visibilityState === "visible";
const isAbort = (error: unknown) => error instanceof DOMException && error.name === "AbortError";

export class PlazaMcpStore {
  private state = emptyState;
  private listeners = new Set<() => void>();
  private refreshInFlight: Promise<void> | null = null;
  private controller: AbortController | null = null;
  private requestGeneration = 0;
  private timer: number | undefined;
  private failedPolls = 0;
  private runtimeWorkspaceId: number | null = null;
  private localOperations = new Map<number, "install" | "remove">();

  constructor() {
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", this.onVisibilityChange);
    if (typeof window !== "undefined") window.addEventListener("pagehide", this.stop);
  }

  getSnapshot = (): PlazaMcpStoreState => this.state;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  load = (runtimeWorkspaceId?: number): Promise<void> => {
    if (runtimeWorkspaceId !== undefined) this.runtimeWorkspaceId = runtimeWorkspaceId;
    return this.refresh({ foreground: true, force: true });
  };

  submitInstall = (assetId: number): Promise<void> => this.submit(assetId, "install");

  submitUninstall = (assetId: number): Promise<void> => this.submit(assetId, "remove");

  private async submit(assetId: number, action: "install" | "remove"): Promise<void> {
    if (this.localOperations.has(assetId)) return;
    this.localOperations.set(assetId, action);
    this.setState({ operations: this.operationSnapshot(), error: null });
    this.schedule(0);
    try {
      const accepted = action === "install"
        ? await userApi.installDefaultProfileAsset(this.requireWorkspaceId(), "mcp", assetId)
        : await userApi.uninstallDefaultProfileAsset(this.requireWorkspaceId(), "mcp", assetId);
      if (this.localOperations.get(assetId) !== action) return;
      const desired = action === "install";
      this.setState({
        items: this.state.items.map((item) => item.assetId === assetId ? {
          ...item,
          desired,
          convergenceStatus: accepted.convergenceStatus,
          errorMessage: null,
        } : item),
        operations: this.operationSnapshot(),
        error: null,
      });
      void this.refresh({ foreground: false, force: true }).catch(() => undefined);
    } catch (error) {
      if (this.localOperations.get(assetId) !== action) return;
      this.localOperations.delete(assetId);
      this.setState({
        operations: this.operationSnapshot(),
        error: error instanceof Error
          ? error.message
          : action === "install" ? "MCP 安装提交失败" : "MCP 卸载提交失败",
      });
    }
  }

  private async refresh({
    foreground,
    force = false,
  }: {
    foreground: boolean;
    force?: boolean;
  }): Promise<void> {
    if (!foreground && !isVisible()) return;
    if (!force && this.refreshInFlight) return this.refreshInFlight;
    if (force) this.controller?.abort();
    if (foreground) this.setState({ loading: true, error: null });
    const controller = new AbortController();
    const generation = ++this.requestGeneration;
    this.controller = controller;
    this.refreshInFlight = userApi.defaultProfileAssets(this.requireWorkspaceId(), controller.signal)
      .then((view) => {
        if (generation !== this.requestGeneration) return;
        this.failedPolls = 0;
        let items: ProfileMcpAssetItem[] = view.mcps.map((item) => ({
          assetId: item.assetId,
          currentVersionId: item.currentVersionId,
          slug: `mcp-${item.assetId}`,
          name: item.name,
          description: item.description,
          transport: item.transport ?? "platform",
          status: item.status,
          desired: item.desired,
          applied: item.applied,
          convergenceStatus: item.desired === item.applied
            ? "applied"
            : view.convergenceStatus,
          errorMessage: item.desired === item.applied ? null : view.lastError,
        }));
        for (const [assetId, action] of this.localOperations) {
          const item = items.find((candidate) => candidate.assetId === assetId);
          if (!item) continue;
          const complete = action === "install"
            ? item.desired && item.applied
            : !item.desired && !item.applied;
          if (complete || item.convergenceStatus === "failed") {
            this.localOperations.delete(assetId);
          }
        }
        items = items.map((item) => {
          const action = this.localOperations.get(item.assetId);
          if (!action) return item;
          const desired = action === "install";
          return {
            ...item,
            desired,
            convergenceStatus: desired === item.applied ? item.convergenceStatus : "updating",
            errorMessage: null,
          };
        });
        this.setState({
          items,
          operations: this.operationSnapshot(),
          loading: false,
          error: null,
        });
        this.schedule(POLL_INTERVAL_MS);
      })
      .catch((error) => {
        if (generation !== this.requestGeneration || isAbort(error)) return;
        this.failedPolls += 1;
        this.setState({
          loading: false,
          error: this.localOperations.size > 0
            ? null
            : error instanceof Error ? error.message : "MCP 广场加载失败",
        });
        this.schedule(RETRY_DELAYS_MS[Math.min(this.failedPolls - 1, RETRY_DELAYS_MS.length - 1)]);
        throw error;
      })
      .finally(() => {
        if (generation !== this.requestGeneration) return;
        if (this.controller === controller) this.controller = null;
        this.refreshInFlight = null;
      });
    return this.refreshInFlight;
  }

  private hasActiveOperation() {
    return this.localOperations.size > 0
      || this.state.items.some((item) => item.desired !== item.applied);
  }
  private operationSnapshot(): Readonly<Record<number, "install" | "remove">> {
    return Object.fromEntries(this.localOperations);
  }
  private schedule(delay: number) {
    this.clearTimer();
    if (!this.hasActiveOperation() || !isVisible()) return;
    this.timer = window.setTimeout(() => {
      this.timer = undefined;
      void this.refresh({ foreground: false, force: true }).catch(() => undefined);
    }, delay);
  }
  private clearTimer = () => {
    if (this.timer !== undefined) window.clearTimeout(this.timer);
    this.timer = undefined;
  };
  private stop = () => {
    this.clearTimer();
    this.controller?.abort();
    this.controller = null;
    this.refreshInFlight = null;
    this.requestGeneration += 1;
  };
  private onVisibilityChange = () => {
    if (!isVisible()) return this.stop();
    if (this.hasActiveOperation()) void this.refresh({ foreground: false, force: true }).catch(() => undefined);
  };
  private setState(patch: Partial<PlazaMcpStoreState>) {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener());
  }
  private requireWorkspaceId(): number {
    if (this.runtimeWorkspaceId === null) throw new Error("Runtime Workspace 尚未就绪");
    return this.runtimeWorkspaceId;
  }
}

export const plazaMcpStore = new PlazaMcpStore();

export function usePlazaMcpStore(): PlazaMcpStoreState & Pick<PlazaMcpStore, "load" | "submitInstall" | "submitUninstall"> {
  const snapshot = useSyncExternalStore(plazaMcpStore.subscribe, plazaMcpStore.getSnapshot);
  return {
    ...snapshot,
    load: plazaMcpStore.load,
    submitInstall: plazaMcpStore.submitInstall,
    submitUninstall: plazaMcpStore.submitUninstall,
  };
}
