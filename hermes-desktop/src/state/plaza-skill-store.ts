import { useSyncExternalStore } from "react";
import type {
  DefaultProfileAssetView,
  ProfileSkillConfiguredItem,
  ProfileSkillCatalogItem,
  PendingSkillOperation,
  PersonalSkillAssetSummary,
  SkillCategorySummary,
  SystemSkillSummary,
} from "@hermes-cloud/schemas";
import { plazaApi } from "../api/plaza-api";
import { userApi } from "../api/client";

export interface PlazaSkillStoreState {
  marketItems: ProfileSkillCatalogItem[];
  personalItems: MineSkillViewItem[];
  installedItems: ProfileSkillConfiguredItem[];
  categories: SkillCategorySummary[];
  pendingOperations: PendingSkillOperation[];
  loading: boolean;
  error: string | null;
}

export interface PersonalSkillRemovalResult {
  skillId: number;
  rolloutId: string;
  affectedProfileIds: number[];
  desiredRevisions: Record<string, number>;
}

export interface PersonalSkillViewItem extends ProfileSkillConfiguredItem {
  source: "personal" | "hermes";
  desired: boolean;
  applied: boolean;
  status: string;
}

export interface SystemSkillViewItem extends ProfileSkillConfiguredItem {
  source: "system";
  systemKey: string;
}

export type MineSkillViewItem = PersonalSkillViewItem | SystemSkillViewItem;

const POLL_INTERVAL_MS = 1_000;
const RETRY_DELAYS_MS = [2_000, 4_000, 8_000, 10_000];

const emptyState: PlazaSkillStoreState = {
  marketItems: [],
  personalItems: [],
  installedItems: [],
  categories: [],
  pendingOperations: [],
  loading: false,
  error: null
};

const isVisible = () => typeof document === "undefined" || document.visibilityState === "visible";
const isAbort = (error: unknown) => error instanceof DOMException && error.name === "AbortError";

export class PlazaSkillStore {
  private state: PlazaSkillStoreState = emptyState;
  private listeners = new Set<() => void>();
  private refreshInFlight: Promise<void> | null = null;
  private refreshController: AbortController | null = null;
  private requestGeneration = 0;
  private timer: number | undefined;
  private failedPolls = 0;
  private runtimeWorkspaceId: number | null = null;
  // Browser-only records survive an older list response while a submission is
  // still waiting for 202 (or has failed before it ever reached the queue).
  private localOperations = new Map<string, PendingSkillOperation>();
  private temporaryOperationSequence = 0;

  constructor() {
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", this.onVisibilityChange);
    }
    if (typeof window !== "undefined") {
      window.addEventListener("pagehide", this.stop);
    }
  }

  getSnapshot = (): PlazaSkillStoreState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  async load(runtimeWorkspaceId?: number): Promise<void> {
    if (runtimeWorkspaceId !== undefined) this.runtimeWorkspaceId = runtimeWorkspaceId;
    if (this.runtimeWorkspaceId === null) throw new Error("Runtime Workspace 尚未就绪");
    await this.refresh({ foreground: true, force: true });
  }

  submitInstall = async (assetId: number): Promise<unknown> => this.submit(
    async () => {
      const accepted = await plazaApi.install(this.requireWorkspaceId(), assetId);
      return { operationId: accepted.operationId, status: "running" as const };
    }, "install", `asset:${assetId}`
  );
  submitUninstall = async (skillId: number, catalogAssetId?: number | null, targetKey?: string): Promise<unknown> => this.submit(
    async () => {
      const accepted = await plazaApi.uninstall(
        this.requireWorkspaceId(),
        catalogAssetId ?? skillId,
      );
      return { operationId: accepted.operationId, status: "running" as const };
    },
    "uninstall", targetKey ?? (catalogAssetId ? `asset:${catalogAssetId}` : `skill:${skillId}`)
  );
  submitPersonalUpload = async (file: File, categoryKey: string, description?: string): Promise<unknown> => this.submit(
    async () => {
      const accepted = await userApi.uploadAndInstallPersonalSkill(
        this.requireWorkspaceId(),
        description === undefined
          ? { file, categoryKey }
          : { file, categoryKey, description },
      );
      return {
        operationId: accepted.operation.operationId,
        status: "running" as const,
        targetKey: `skill:${accepted.asset.skillId}`,
      };
    }, "personal_upload",
    // The server returns the authoritative content-hash target in its 202.
    // This stable browser key makes the click synchronous without reading the
    // entire zip before showing feedback, while still allowing different files
    // in the same category to be submitted independently.
    `personal:pending:${categoryKey}:${file.name}:${file.size}:${file.lastModified}`,
  );
  submitDescriptionUpdate = async (
    skillId: number,
    description: string | null,
    name?: string,
  ): Promise<unknown> => {
    const result = await userApi.updatePersonalSkillDescription(skillId, description, name);
    void this.refresh({ foreground: false, force: true }).catch(() => undefined);
    return result;
  };
  submitPersonalInstall = async (skillId: number): Promise<unknown> => this.submit(
    async () => {
      const accepted = await userApi.installDefaultProfileAsset(
        this.requireWorkspaceId(),
        "skill",
        skillId,
      );
      return { operationId: accepted.operationId, status: "running" as const };
    },
    "install",
    `skill:${skillId}`,
  );
  submitPersonalUninstall = async (skillId: number): Promise<unknown> => this.submit(
    async () => {
      const accepted = await userApi.uninstallDefaultProfileAsset(
        this.requireWorkspaceId(),
        "skill",
        skillId,
      );
      return { operationId: accepted.operationId, status: "running" as const };
    },
    "uninstall",
    `skill:${skillId}`,
  );
  submitPersonalDelete = async (skillId: number): Promise<PersonalSkillRemovalResult | undefined> => this.submit(
    async () => {
      const removal = await userApi.deletePersonalSkill(skillId);
      return {
        operationId: removal.rolloutId,
        status: "running" as const,
        targetKey: `skill-delete:${skillId}`,
        result: removal,
      };
    },
    "delete",
    `skill-delete:${skillId}`,
  );
  createPersonalCategory = async (displayName: string): Promise<SkillCategorySummary> => {
    const category = await userApi.createPersonalSkillCategory(displayName);
    this.setState({
      categories: mergeCategories(this.state.categories, [category]),
      error: null,
    });
    return category;
  };
  private async submit<T = unknown>(
    command: () => Promise<{ operationId: string; status: "queued" | "running"; targetKey?: string; result?: T }>,
    action: PendingSkillOperation["action"],
    targetKey: string,
  ): Promise<T | undefined> {
    const existing = this.state.pendingOperations.find(
      (item) => item.targetKey === targetKey && (item.status === "queued" || item.status === "running"),
    );
    if (existing) return undefined;

    const temporaryId = `local:${++this.temporaryOperationSequence}`;
    const temporary: PendingSkillOperation = {
      operationId: temporaryId,
      action,
      targetKey,
      status: "queued",
      submittedAt: new Date().toISOString(),
      submissionPhase: "submitting",
    };
    this.localOperations.set(targetKey, temporary);
    this.setState({
      pendingOperations: replaceOperation(this.state.pendingOperations, targetKey, temporary),
      error: null,
    });
    this.schedule(POLL_INTERVAL_MS);

    try {
      const accepted = await command();
      if (this.localOperations.get(targetKey)?.operationId !== temporaryId) return undefined;
      const acceptedTargetKey = accepted.targetKey ?? targetKey;
      const acceptedOperation: PendingSkillOperation = {
        operationId: accepted.operationId,
        action,
        targetKey: acceptedTargetKey,
        status: accepted.status,
        submittedAt: new Date().toISOString(),
        submissionPhase: "accepted",
      };
      this.localOperations.delete(targetKey);
      this.localOperations.set(acceptedTargetKey, acceptedOperation);
      this.setState({
        pendingOperations: replaceOperation(
          this.state.pendingOperations.filter((item) => item.targetKey !== targetKey),
          acceptedTargetKey,
          acceptedOperation,
        ),
        error: null,
      });
      // An accepted operation must never wait behind a list request that was
      // started before the mutation.  Its result could otherwise erase the
      // optimistic operation state and stop polling before the new skill is
      // visible.
      void this.refresh({ foreground: false, force: true }).catch(() => undefined);
      return accepted.result;
    } catch (error: unknown) {
      if (this.localOperations.get(targetKey)?.operationId !== temporaryId) return undefined;
      const message = error instanceof Error ? error.message : "技能操作提交失败";
      const failed: PendingSkillOperation = {
        ...temporary,
        status: "failed",
        errorCode: "submission_failed",
        errorMessage: `${message}，可重试`,
      };
      this.localOperations.set(targetKey, failed);
      this.setState({
        pendingOperations: replaceOperation(this.state.pendingOperations, targetKey, failed),
        error: null,
      });
      throw error instanceof Error ? error : new Error(message);
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
    if (force) this.refreshController?.abort();
    if (foreground) this.setState({ loading: true, error: null });
    const controller = new AbortController();
    const generation = ++this.requestGeneration;
    this.refreshController = controller;
    this.refreshInFlight = Promise.all([
      plazaApi.getDefaultView(this.requireWorkspaceId(), controller.signal),
      userApi.personalSkillAssets(),
      userApi.personalSkillCategories(),
      userApi.systemSkills(),
    ])
      .then(([view, personalAssets, availableCategories, systemSkills]) => {
        if (generation !== this.requestGeneration) return;
        this.failedPolls = 0;
        const remote = fromDefaultView(
          view,
          personalAssets,
          availableCategories,
          systemSkills,
        );
        for (const operation of remote.pendingOperations) {
          this.localOperations.delete(operation.targetKey);
        }
        for (const operation of this.localOperations.values()) {
          const target = /^(asset|skill):(\d+)$/.exec(operation.targetKey);
          if (!target) continue;
          const assetId = Number(target[2]);
          const item = target[1] === "asset"
            ? remote.marketItems.find((candidate) => candidate.assetId === assetId)
            : remote.personalItems.find((candidate) => candidate.assetId === assetId);
          if (!item) {
            // A personal Skill delete removes the asset from the catalog, so
            // completion is represented by the item disappearing entirely.
            if (operation.action === "uninstall") this.localOperations.delete(operation.targetKey);
            continue;
          }
          const desired = "desired" in item ? item.desired : true;
          const complete = operation.action === "uninstall"
            ? ("state" in item ? item.state === "installable" : !desired) && !item.operating
            : ("state" in item ? item.state === "current" : desired) && !item.operating;
          if (complete) this.localOperations.delete(operation.targetKey);
        }
        this.setState({
          ...remote,
          pendingOperations: mergePendingOperations(remote.pendingOperations, this.localOperations),
          loading: false,
          error: null,
        });
        this.schedule(POLL_INTERVAL_MS);
      })
      .catch((error: unknown) => {
        if (generation !== this.requestGeneration || isAbort(error)) return;
        this.failedPolls += 1;
        this.setState({
          loading: false,
          error: error instanceof Error ? error.message : "广场加载失败"
        });
        this.schedule(RETRY_DELAYS_MS[Math.min(this.failedPolls - 1, RETRY_DELAYS_MS.length - 1)]);
        throw error;
      })
      .finally(() => {
        if (generation !== this.requestGeneration) return;
        if (this.refreshController === controller) this.refreshController = null;
        this.refreshInFlight = null;
      });
    return this.refreshInFlight;
  }

  private hasActiveOperation(): boolean {
    return this.state.pendingOperations.some((item) => item.status === "queued" || item.status === "running");
  }

  private schedule(delay: number): void {
    this.clearTimer();
    if (!this.hasActiveOperation() || !isVisible()) return;
    this.timer = window.setTimeout(() => {
      this.timer = undefined;
      void this.refresh({ foreground: false, force: true }).catch(() => undefined);
    }, delay);
  }

  private clearTimer = (): void => {
    if (this.timer !== undefined) window.clearTimeout(this.timer);
    this.timer = undefined;
  };

  private stop = (): void => {
    this.clearTimer();
    this.refreshController?.abort();
    this.refreshController = null;
    this.refreshInFlight = null;
    this.requestGeneration += 1;
  };

  private onVisibilityChange = (): void => {
    if (!isVisible()) {
      this.stop();
      return;
    }
    if (this.hasActiveOperation()) {
      void this.refresh({ foreground: false, force: true }).catch(() => undefined);
    }
  };

  private setState(patch: Partial<PlazaSkillStoreState>): void {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener());
  }

  private requireWorkspaceId(): number {
    if (this.runtimeWorkspaceId === null) throw new Error("Runtime Workspace 尚未就绪");
    return this.runtimeWorkspaceId;
  }
}

export const plazaSkillStore = new PlazaSkillStore();

export function usePlazaSkillStore(): PlazaSkillStoreState & Pick<PlazaSkillStore, "load" | "submitInstall" | "submitUninstall" | "submitPersonalUpload" | "submitPersonalInstall" | "submitPersonalUninstall" | "submitPersonalDelete" | "createPersonalCategory" | "submitDescriptionUpdate"> {
  const snapshot = useSyncExternalStore(plazaSkillStore.subscribe, plazaSkillStore.getSnapshot);
  return {
    ...snapshot,
    load: plazaSkillStore.load.bind(plazaSkillStore),
    submitInstall: plazaSkillStore.submitInstall.bind(plazaSkillStore),
    submitUninstall: plazaSkillStore.submitUninstall.bind(plazaSkillStore),
    submitPersonalUpload: plazaSkillStore.submitPersonalUpload.bind(plazaSkillStore),
    submitPersonalInstall: plazaSkillStore.submitPersonalInstall.bind(plazaSkillStore),
    submitPersonalUninstall: plazaSkillStore.submitPersonalUninstall.bind(plazaSkillStore),
    submitPersonalDelete: plazaSkillStore.submitPersonalDelete.bind(plazaSkillStore),
    createPersonalCategory: plazaSkillStore.createPersonalCategory.bind(plazaSkillStore),
    submitDescriptionUpdate: plazaSkillStore.submitDescriptionUpdate.bind(plazaSkillStore),
  };
}

function fromDefaultView(
  view: DefaultProfileAssetView,
  personalAssets: PersonalSkillAssetSummary[],
  availableCategories: SkillCategorySummary[],
  systemSkills: SystemSkillSummary[],
): Omit<PlazaSkillStoreState, "loading" | "error"> {
  const systemSkillNames = new Set(
    systemSkills.flatMap((skill) => [skill.key, skill.name])
      .map((value) => value.trim().toLocaleLowerCase().replace(/[\s_]+/g, "-")),
  );
  const personalBindings = view.skills.filter((item) => item.assetScope === "personal");
  const knownCategoryKeys = new Set(
    availableCategories.map((category) => category.categoryKey),
  );
  const fallbackCategories = [...new Set(
    view.skills.map((item) => item.categoryKey ?? "other"),
  )]
    .filter((key) => !knownCategoryKeys.has(key))
    .map((key) => ({
      categoryKey: key,
      displayName: key,
      isBuiltin: true,
      isMarketVisible: true,
      isPersonalVisible: true,
      scope: "organization" as const,
    }));
  const versionLabel = (item: DefaultProfileAssetView["skills"][number]) => (
    String(item.version ?? item.currentVersionId ?? "latest")
  );
  const pendingOperations: PendingSkillOperation[] = view.skills
    .filter((item) => item.desired !== item.applied)
    .map((item) => ({
      operationId: `profile-${view.runtimeAgentProfileId}-revision-${view.desiredRevision}-skill-${item.assetId}`,
      action: item.desired ? "install" : "uninstall",
      targetKey: `${item.assetScope === "personal" ? "skill" : "asset"}:${item.assetId}`,
      status: view.convergenceStatus === "failed" ? "failed" : "running",
      submittedAt: new Date().toISOString(),
      ...(view.convergenceStatus === "failed" ? {
        errorCode: view.lastError ?? "PROFILE_PROJECTION_FAILED",
        errorMessage: view.lastError ?? "PROFILE_PROJECTION_FAILED",
      } : {}),
    }));
  return {
    marketItems: view.skills.filter((item) => (
      item.assetScope === "public"
      && !systemSkillNames.has(item.name.trim().toLocaleLowerCase().replace(/[\s_]+/g, "-"))
    )).map((item) => ({
      assetId: item.assetId,
      name: item.name,
      description: item.description,
      categoryKey: item.categoryKey ?? "other",
      currentVersionLabel: versionLabel(item),
      state: (item.configured ?? item.desired) ? "current" as const : "installable" as const,
      configuredAssetId: (item.configured ?? item.desired) ? item.assetId : null,
      installedVersionLabel: (item.configured ?? item.desired) ? versionLabel(item) : null,
      operating: item.desired !== item.applied,
      executionStatus: item.status === "force_disabled" ? "force_disabled" as const : "enabled" as const,
    })).sort((left, right) => Number(right.state === "current") - Number(left.state === "current") || left.name.localeCompare(right.name)),
    personalItems: [
      ...systemSkills.map((skill, index) => ({
        assetId: -(index + 1),
        systemKey: skill.key,
        source: "system" as const,
        name: skill.name,
        description: skill.description,
        categoryKey: "system",
        versionLabel: "runtime",
        updateStatus: "not_applicable" as const,
        operating: false,
      })),
      ...personalAssets.filter((asset) => (
        !systemSkillNames.has(asset.name.trim().toLocaleLowerCase().replace(/[\s_]+/g, "-"))
      )).map((asset) => {
        const binding = view.skills.find((item) => item.assetId === asset.skillId);
        return {
          assetId: asset.skillId,
          source: asset.source === "hermes_managed" ? "hermes" as const : "personal" as const,
          name: asset.name,
          description: asset.description,
          categoryKey: asset.categoryKey || "uncategorized",
          versionLabel: String(asset.version ?? "latest"),
          updateStatus: "not_applicable" as const,
          operating: Boolean(binding && binding.desired !== binding.applied),
          desired: asset.source === "hermes_managed" ? true : binding?.configured ?? binding?.desired ?? false,
          applied: asset.source === "hermes_managed" ? true : binding?.applied ?? false,
          status: asset.status,
        };
      }),
      ...personalBindings
        .filter((binding) => !personalAssets.some((asset) => asset.skillId === binding.assetId))
        .map((binding) => ({
          assetId: binding.assetId,
          source: "personal" as const,
          name: binding.name,
          description: binding.description,
          categoryKey: binding.categoryKey && !["other", "personal"].includes(binding.categoryKey)
            ? binding.categoryKey
            : "uncategorized",
          versionLabel: versionLabel(binding),
          updateStatus: "not_applicable" as const,
          operating: binding.desired !== binding.applied,
          desired: binding.configured ?? binding.desired,
          applied: binding.applied,
          status: binding.status,
        })),
    ],
    installedItems: view.skills.filter((item) => item.desired).map((item) => ({
      assetId: item.assetId,
      source: item.assetScope === "personal" ? "personal" : "market",
      name: item.name,
      description: item.description,
      categoryKey: item.categoryKey ?? "other",
      versionLabel: versionLabel(item),
      updateStatus: "current",
      catalogAssetId: item.assetId,
      operating: item.desired !== item.applied,
    })),
    categories: mergeCategories(
      availableCategories,
      fallbackCategories,
    ),
    pendingOperations
  };
}

function mergeCategories(
  current: SkillCategorySummary[],
  incoming: SkillCategorySummary[],
): SkillCategorySummary[] {
  const byKey = new Map(current.map((item) => [item.categoryKey, item]));
  for (const category of incoming) {
    byKey.set(category.categoryKey, { ...byKey.get(category.categoryKey), ...category });
  }
  return [...byKey.values()];
}

function replaceOperation(
  operations: PendingSkillOperation[],
  targetKey: string,
  replacement: PendingSkillOperation,
): PendingSkillOperation[] {
  return [...operations.filter((item) => item.targetKey !== targetKey), replacement];
}

function mergePendingOperations(
  remote: PendingSkillOperation[],
  local: ReadonlyMap<string, PendingSkillOperation>,
): PendingSkillOperation[] {
  const localTargets = new Set(local.keys());
  return [...remote.filter((item) => !localTargets.has(item.targetKey)), ...local.values()];
}
