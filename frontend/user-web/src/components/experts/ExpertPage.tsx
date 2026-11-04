import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Input, Tabs, type InputRef } from "antd";
import { PlusOutlined, RobotOutlined, SearchOutlined } from "@ant-design/icons";
import { LoaderCircle } from "lucide-react";
import type {
  ExpertCapabilityCatalog,
  ExpertCapabilityReference,
  ExpertCapabilitySelection,
  ExpertMarketDetail,
  ExpertMarketItem,
  ExpertShareScope,
  OrganizationDirectoryNode,
  RuntimeAgentProfileSummary,
  UpdateExpertConfigurationRequest
} from "@hermes-cloud/schemas";
import { userApi } from "../../api/client";
import { DEFAULT_AVATAR_VALUE } from "../../avatar";
import { AvatarPicker } from "../AvatarPicker";
import { ExpertPersonalSkillUpload } from "./ExpertPersonalSkillUpload";
import { ExpertAvatar } from "./ExpertAvatar";
import {
  ExpertMarketDetailDialog,
  ExpertMarketPanel,
  ExpertMinePanel,
  ExpertUninstallDialog
} from "./ExpertCatalogPanels";
import {
  CreateCapabilityCard,
  SharingFields
} from "./ExpertEditorFields";
import {
  ExpertCapabilityPicker,
  capabilityReferences,
  replaceCapabilityReferences,
  type ExpertCapabilityPickerKind
} from "./ExpertCapabilityPicker";
import { ExpertCreateEditorDialog, ExpertEditEditorDialog } from "./ExpertEditorDialog";
import {
  ExpertCapabilityRemovalDialog,
  ExpertEditorCloseDialog,
  ExpertStaleSkillSaveDialog
} from "./ExpertOperationNotice";
import {
  canonicalScope,
  copyCapabilities,
  createDraftStorageKey,
  emptyCapabilities,
  emptyCreateDraft,
  emptyScope,
  loadCreateDraft,
  runtimeCapabilitiesEqual,
  type ExpertCreateDraft
} from "./expertEditorModel";
import { useExpertEditorAnchors } from "./useExpertEditorAnchors";
import { useExpertConvergencePolling } from "./useExpertConvergencePolling";

type ExpertTab = "mine" | "market";
type CapabilityPicker = ExpertCapabilityPickerKind;

// 目录 Tab 计数（2026-11-03-017 轻量角标 pill）：常显语义（含 0）、
// 未选灰底灰字 / 选中浅蓝底蓝字，样式见 styles.css .laneTabCount。
function TabCount({ count }: { count: number }) {
  return <span className="laneTabCount">{count}</span>;
}

interface ExpertPageProps {
  runtimeWorkspaceId: number | null;
  onStartChat: (profile: RuntimeAgentProfileSummary) => void;
  onInstalledChange?: (profiles: RuntimeAgentProfileSummary[]) => Promise<void> | void;
  /** 嵌入模式（2026-11-03-016）：不渲染灰底页/白色工作卡/模块页头，
   * 目录 Tabs 与搜索合并为一行工具栏，直接嵌入外层大卡片（专家-技能组合页）。 */
  embedded?: boolean;
  /** 创建信号（递增即打开创建编辑器）：外层大卡片页头的「创建专家」按钮经此触发，
   * 语义同技能面板的 uploadTrigger（2026-11-03-017 按钮上移页头）。 */
  createTrigger?: number;
}

interface CapabilityRemovalConfirmation {
  label: string;
  apply: () => void;
}

interface StalePersonalSkillSaveConfirmation {
  profileId: number;
  staleReferences: ExpertCapabilityReference[];
  skillNames: string[];
  payload: UpdateExpertConfigurationRequest;
}

type EditorCloseTarget = "create" | "edit";

const emptyCapabilityCatalog = (): ExpertCapabilityCatalog => ({ marketSkills: [], installedSkills: [], skillCategories: [], marketMcps: [], installedMcps: [], knowledgeBases: [], personalSkills: [], personalKnowledgeBases: [], pluginIds: [] });

const stalePersonalSkillReferences = (
  capabilities: ExpertCapabilitySelection,
  catalog: ExpertCapabilityCatalog
) => {
  const personalSkillIds = new Set(catalog.personalSkills.map((item) => item.assetId));
  return capabilityReferences(capabilities, "skills", "skillIds")
    .filter((reference) => reference.source === "personal" && !personalSkillIds.has(reference.assetId));
};

export function ExpertPage({
  runtimeWorkspaceId,
  onStartChat,
  onInstalledChange,
  embedded = false,
  createTrigger
}: ExpertPageProps) {
  const [tab, setTab] = useState<ExpertTab>("mine");
  const [searchQuery, setSearchQuery] = useState("");
  const [market, setMarket] = useState<ExpertMarketItem[]>([]);
  const [mine, setMine] = useState<RuntimeAgentProfileSummary[]>([]);
  const [runnable, setRunnable] = useState<RuntimeAgentProfileSummary[]>([]);
  const [catalog, setCatalog] = useState<ExpertCapabilityCatalog>(emptyCapabilityCatalog);
  const [organizationDirectory, setOrganizationDirectory] = useState<OrganizationDirectoryNode[]>([]);
  const [directoryLoading, setDirectoryLoading] = useState(false);
  const [directoryError, setDirectoryError] = useState<string | null>(null);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketError, setMarketError] = useState<string | null>(null);
  const [mineLoading, setMineLoading] = useState(false);
  const [mineError, setMineError] = useState<string | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cardErrors, setCardErrors] = useState<Record<string, string>>({});
  const [uncertainProfileIds, setUncertainProfileIds] = useState<Set<number>>(
    () => new Set()
  );
  const [marketDetailTarget, setMarketDetailTarget] = useState<ExpertMarketItem | null>(null);
  const [marketDetail, setMarketDetail] = useState<ExpertMarketDetail | null>(null);
  const [marketDetailLoading, setMarketDetailLoading] = useState(false);
  const [marketDetailError, setMarketDetailError] = useState<string | null>(null);
  const [marketProfileStatuses, setMarketProfileStatuses] = useState<
    Record<number, RuntimeAgentProfileSummary>
  >({});
  const [createOpen, setCreateOpen] = useState(false);
  const [createDraft, setCreateDraft] = useState<ExpertCreateDraft>(emptyCreateDraft);
  const [createCapabilityPicker, setCreateCapabilityPicker] = useState<CapabilityPicker | null>(null);
  const [createPickerCapabilities, setCreatePickerCapabilities] = useState<ExpertCapabilitySelection | null>(null);
  const [editing, setEditing] = useState<RuntimeAgentProfileSummary | null>(null);
  const [editorCapabilityPicker, setEditorCapabilityPicker] = useState<CapabilityPicker | null>(null);
  const [editorPickerCapabilities, setEditorPickerCapabilities] = useState<ExpertCapabilitySelection | null>(null);
  const [capabilityRemoval, setCapabilityRemoval] = useState<CapabilityRemovalConfirmation | null>(null);
  const [staleSkillSave, setStaleSkillSave] = useState<StalePersonalSkillSaveConfirmation | null>(null);
  const [editorCloseTarget, setEditorCloseTarget] = useState<EditorCloseTarget | null>(null);
  const [editorIdentity, setEditorIdentity] = useState({
    name: "",
    description: "",
    avatarUrl: DEFAULT_AVATAR_VALUE,
    soulContent: ""
  });
  const [editorCapabilities, setEditorCapabilities] = useState<ExpertCapabilitySelection>(emptyCapabilities);
  const [editorScope, setEditorScope] = useState<ExpertShareScope>(emptyScope);
  const [editorChangeSummary, setEditorChangeSummary] = useState("");
  const [uninstallTarget, setUninstallTarget] = useState<RuntimeAgentProfileSummary | null>(null);
  const createAnchors = useExpertEditorAnchors("create", createOpen);
  const editAnchors = useExpertEditorAnchors("edit", Boolean(editing));
  const createNameInputRef = useRef<InputRef>(null);
  const editorNameInputRef = useRef<InputRef>(null);
  const workspaceRef = useRef<number | null>(runtimeWorkspaceId);
  const createDraftWorkspaceRef = useRef<number | null>(null);
  const requestGenerationRef = useRef({ market: 0, mine: 0, runnable: 0, configuration: 0, marketDetail: 0 });
  const configurationCacheRef = useRef({
    workspaceId: null as number | null,
    catalog: false
  });
  workspaceRef.current = runtimeWorkspaceId;

  const loadOrganizationDirectory = async () => {
    setDirectoryLoading(true);
    setDirectoryError(null);
    try {
      setOrganizationDirectory(await userApi.organizationDirectoryTree());
    } catch (reason) {
      setDirectoryError(reason instanceof Error ? reason.message : "组织目录加载失败");
    } finally {
      setDirectoryLoading(false);
    }
  };

  const isCurrentRequest = (kind: "market" | "mine" | "runnable" | "configuration" | "marketDetail", generation: number, workspaceId: number) =>
    workspaceRef.current === workspaceId && requestGenerationRef.current[kind] === generation;

  const loadMarket = async () => {
    if (!runtimeWorkspaceId) return;
    const workspaceId = runtimeWorkspaceId;
    const generation = ++requestGenerationRef.current.market;
    setMarketLoading(true);
    setMarketError(null);
    try {
      const nextMarket = await userApi.expertMarket(workspaceId);
      if (isCurrentRequest("market", generation, workspaceId)) {
        setMarket(nextMarket);
        setMarketDetailTarget((current) => (
          current
            ? nextMarket.find((item) => item.expertPackageId === current.expertPackageId) ?? current
            : current
        ));
      }
    } catch (reason) {
      if (isCurrentRequest("market", generation, workspaceId)) {
        setMarketError(reason instanceof Error ? reason.message : "专家市场加载失败");
      }
    } finally {
      if (isCurrentRequest("market", generation, workspaceId)) setMarketLoading(false);
    }
  };

  const loadMine = async () => {
    if (!runtimeWorkspaceId) return;
    const workspaceId = runtimeWorkspaceId;
    const generation = ++requestGenerationRef.current.mine;
    setMineLoading(true);
    setMineError(null);
    try {
      const nextMine = await userApi.myExperts(workspaceId);
      if (!isCurrentRequest("mine", generation, workspaceId)) return;
      setMine(nextMine);
      setUncertainProfileIds(new Set());
    } catch (reason) {
      if (isCurrentRequest("mine", generation, workspaceId)) {
        setMineError(reason instanceof Error ? reason.message : "我的专家加载失败");
      }
    } finally {
      if (isCurrentRequest("mine", generation, workspaceId)) setMineLoading(false);
    }
  };

  const loadRunnable = async ({ awaitSync = false }: { awaitSync?: boolean } = {}) => {
    if (!runtimeWorkspaceId) return;
    const workspaceId = runtimeWorkspaceId;
    const generation = ++requestGenerationRef.current.runnable;
    try {
      const nextRunnable = await userApi.runnableExperts(workspaceId);
      if (!isCurrentRequest("runnable", generation, workspaceId)) return;
      setRunnable(nextRunnable);
      const sync = Promise.resolve(onInstalledChange?.(nextRunnable));
      if (awaitSync) await sync;
      else void sync.catch(() => undefined);
    } catch {
      // The market and authoring lists remain independently usable. Chat owns
      // its existing roster until Backend returns a new runnable snapshot.
    }
  };

  const pendingMarketProfileIds = useMemo(
    () => market.flatMap((item) => (
      item.installed
      && item.executionStatus === "enabled"
      && item.installedRuntimeAgentProfileId != null
      && !item.allowedActions.summon
        ? [item.installedRuntimeAgentProfileId]
        : []
    )),
    [market]
  );
  const pendingMarketProfileIdSet = useMemo(
    () => new Set(pendingMarketProfileIds),
    [pendingMarketProfileIds]
  );
  const marketProfileStatusMap = useMemo(
    () => new Map(
      Object.entries(marketProfileStatuses)
        .map(([id, profile]) => [Number(id), profile] as const)
    ),
    [marketProfileStatuses]
  );

  useExpertConvergencePolling({
    runtimeWorkspaceId,
    profiles: mine,
    pendingProfileIds: pendingMarketProfileIds,
    onProfileUpdated: (nextProfile) => {
      setMine((current) => current.map((profile) => (
        profile.runtimeAgentProfileId === nextProfile.runtimeAgentProfileId
          ? nextProfile
          : profile
      )));
      setMarketProfileStatuses((current) => ({
        ...current,
        [nextProfile.runtimeAgentProfileId]: nextProfile
      }));
      setMarket((current) => current.map((item) => (
        item.installedRuntimeAgentProfileId === nextProfile.runtimeAgentProfileId
          ? {
              ...item,
              allowedActions: {
                ...item.allowedActions,
                summon: nextProfile.allowedActions.summon && item.executionStatus === "enabled"
              }
            }
          : item
      )));
      setMarketDetailTarget((current) => (
        current?.installedRuntimeAgentProfileId === nextProfile.runtimeAgentProfileId
          ? {
              ...current,
              allowedActions: {
                ...current.allowedActions,
                summon: nextProfile.allowedActions.summon && current.executionStatus === "enabled"
              }
            }
          : current
      ));
    },
    onProjectionApplied: loadRunnable
  });

  const loadConfiguration = async ({
    force = false
  }: {
    force?: boolean;
  } = {}): Promise<ExpertCapabilityCatalog | null> => {
    if (!runtimeWorkspaceId) return null;
    const workspaceId = runtimeWorkspaceId;
    if (configurationCacheRef.current.workspaceId !== workspaceId) {
      configurationCacheRef.current = {
        workspaceId,
        catalog: false
      };
    }
    if (!force && configurationCacheRef.current.catalog) return catalog;
    const generation = ++requestGenerationRef.current.configuration;
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      const nextCatalog = await userApi.expertCapabilityCatalog();
      if (!isCurrentRequest("configuration", generation, workspaceId)) return null;
      setCatalog(nextCatalog);
      configurationCacheRef.current.catalog = true;
      return nextCatalog;
    } catch (reason) {
      if (!isCurrentRequest("configuration", generation, workspaceId)) return null;
      setCatalogError(reason instanceof Error ? reason.message : "能力目录加载失败");
      configurationCacheRef.current.catalog = false;
      return null;
    } finally {
      if (isCurrentRequest("configuration", generation, workspaceId)) setCatalogLoading(false);
    }
  };

  const invalidateConfiguration = () => {
    configurationCacheRef.current = {
      workspaceId: null,
      catalog: false
    };
    ++requestGenerationRef.current.configuration;
  };

  const closeMarketDetail = () => {
    ++requestGenerationRef.current.marketDetail;
    setMarketDetailTarget(null);
    setMarketDetail(null);
    setMarketDetailError(null);
    setMarketDetailLoading(false);
  };

  const openMarketDetail = async (item: ExpertMarketItem) => {
    if (!runtimeWorkspaceId) return;
    const workspaceId = runtimeWorkspaceId;
    const generation = ++requestGenerationRef.current.marketDetail;
    setMarketDetailTarget(item);
    setMarketDetail(null);
    setMarketDetailError(null);
    setMarketDetailLoading(true);
    try {
      const detail = await userApi.marketExpertDetail(workspaceId, item.expertPackageId);
      if (isCurrentRequest("marketDetail", generation, workspaceId)) setMarketDetail(detail);
    } catch (reason) {
      if (isCurrentRequest("marketDetail", generation, workspaceId)) {
        setMarketDetailError(reason instanceof Error ? reason.message : "专家详情加载失败");
      }
    } finally {
      if (isCurrentRequest("marketDetail", generation, workspaceId)) setMarketDetailLoading(false);
    }
  };

  const refreshAfterMutation = async () => {
    invalidateConfiguration();
    await Promise.all([loadMarket(), loadMine(), loadRunnable({ awaitSync: true })]);
  };

  const refreshAfterMutationFailure = async (profileId?: number) => {
    if (profileId != null) {
      setUncertainProfileIds((current) => new Set(current).add(profileId));
    }
    invalidateConfiguration();
    await Promise.allSettled([
      loadMarket(),
      loadMine(),
      loadRunnable({ awaitSync: true })
    ]);
  };

  useEffect(() => {
    workspaceRef.current = runtimeWorkspaceId;
    configurationCacheRef.current = {
      workspaceId: null,
      catalog: false
    };
    ++requestGenerationRef.current.market;
    ++requestGenerationRef.current.mine;
    ++requestGenerationRef.current.runnable;
    ++requestGenerationRef.current.configuration;
    ++requestGenerationRef.current.marketDetail;
    setMarket([]);
    setMine([]);
    setRunnable([]);
    setUncertainProfileIds(new Set());
    setCatalog(emptyCapabilityCatalog());
    setCatalogLoading(false);
    setCatalogError(null);
    setMarketError(null);
    setMineError(null);
    setMarketDetailTarget(null);
    setMarketDetail(null);
    setMarketDetailError(null);
    setMarketDetailLoading(false);
    setMarketProfileStatuses({});
    createDraftWorkspaceRef.current = null;
    setCreateOpen(false);
    setCreateDraft(emptyCreateDraft());
    setCreateCapabilityPicker(null);
    setCreatePickerCapabilities(null);
    setEditing(null);
    setEditorCapabilityPicker(null);
    setEditorPickerCapabilities(null);
    setCapabilityRemoval(null);
    setEditorCloseTarget(null);
    setUninstallTarget(null);
    setError(null);
    setBusy(null);
    if (!runtimeWorkspaceId) return;
    void loadMarket();
    void loadMine();
    void loadRunnable();
    return () => {
      ++requestGenerationRef.current.market;
      ++requestGenerationRef.current.mine;
      ++requestGenerationRef.current.runnable;
      ++requestGenerationRef.current.configuration;
      ++requestGenerationRef.current.marketDetail;
    };
  }, [runtimeWorkspaceId]);

  useEffect(() => {
    if ((createOpen || editing) && organizationDirectory.length === 0 && !directoryLoading) {
      void loadOrganizationDirectory();
    }
  }, [createOpen, editing]);

  useEffect(() => {
    if (
      !createOpen
      || !runtimeWorkspaceId
      || createDraftWorkspaceRef.current !== runtimeWorkspaceId
      || typeof window === "undefined"
    ) return;
    window.localStorage.setItem(createDraftStorageKey(runtimeWorkspaceId), JSON.stringify(createDraft));
  }, [createDraft, createOpen, runtimeWorkspaceId]);

  const runnableByProfileId = useMemo(
    () => new Map(runnable.map((item) => [item.runtimeAgentProfileId, item])),
    [runnable]
  );
  const normalizedSearchQuery = searchQuery.trim().toLocaleLowerCase();
  const visibleMine = mine.filter((item) => !normalizedSearchQuery
    || item.displayName.toLocaleLowerCase().includes(normalizedSearchQuery)
    || (item.description || "").toLocaleLowerCase().includes(normalizedSearchQuery));
  const visibleMarket = market.filter((item) => !normalizedSearchQuery
    || item.name.toLocaleLowerCase().includes(normalizedSearchQuery)
    || (item.description || "").toLocaleLowerCase().includes(normalizedSearchQuery));
  const openCreateEditor = () => {
    createDraftWorkspaceRef.current = runtimeWorkspaceId;
    setCreateDraft(loadCreateDraft(runtimeWorkspaceId));
    setCreateCapabilityPicker(null);
    setCreatePickerCapabilities(null);
    setCapabilityRemoval(null);
    setCreateOpen(true);
    void loadConfiguration();
  };

  // 外层大卡片页头的「创建专家」信号（2026-11-03-017）：递增即打开创建编辑器；
  // ref 比较保证幂等，初始挂载（0 → 0）不触发（同技能面板 uploadTrigger 模式）。
  const createSignalRef = useRef(createTrigger ?? 0);
  useEffect(() => {
    const signal = createTrigger ?? 0;
    if (signal > createSignalRef.current) openCreateEditor();
    createSignalRef.current = signal;
  }, [createTrigger]);
  const openCreateCapabilityPicker = (kind: CapabilityPicker) => {
    setCreatePickerCapabilities(copyCapabilities(createDraft.capabilities));
    setCreateCapabilityPicker(kind);
  };
  const closeCreateCapabilityPicker = () => {
    setCreateCapabilityPicker(null);
    setCreatePickerCapabilities(null);
  };
  const confirmCreateCapabilityPicker = () => {
    if (createPickerCapabilities) {
      setCreateDraft((current) => ({
        ...current,
        capabilities: copyCapabilities(createPickerCapabilities),
      }));
    }
    closeCreateCapabilityPicker();
  };
  const createExpert = async () => {
    if (!runtimeWorkspaceId) return;
    const name = createDraft.name.trim();
    if (!name) {
      setError("请填写专家名称");
      window.setTimeout(() => createNameInputRef.current?.focus());
      return;
    }
    setBusy("create");
    setError(null);
    try {
      const created = await userApi.createExpert({
        runtimeWorkspaceId,
        name,
        description: createDraft.description.trim() || null,
        avatarUrl: createDraft.avatarUrl.trim() || null,
        soulContent: createDraft.soulContent,
        inheritedFromProfileId: null,
        capabilities: { ...createDraft.capabilities, pluginIds: [] },
        changeSummary: createDraft.changeSummary.trim() || null,
        shareScope: createDraft.shareScope
      });
      createDraftWorkspaceRef.current = null;
      setCreateOpen(false);
      if (typeof window !== "undefined") window.localStorage.removeItem(createDraftStorageKey(runtimeWorkspaceId));
      setCreateDraft(emptyCreateDraft());
      setTab("mine");
      await refreshAfterMutation();
      setMine((current) => (
        current.some((profile) => profile.runtimeAgentProfileId === created.runtimeAgentProfileId)
          ? current
          : [created, ...current]
      ));
    } catch (reason) {
      const primaryError = reason instanceof Error ? reason.message : "创建专家失败";
      setError(primaryError);
      await refreshAfterMutationFailure();
      setError(primaryError);
    } finally {
      setBusy(null);
    }
  };

  const install = async (item: ExpertMarketItem) => {
    if (!runtimeWorkspaceId) return;
    setBusy(`install:${item.expertPackageId}`);
    setCardErrors((current) => ({ ...current, [`install:${item.expertPackageId}`]: "" }));
    try {
      await userApi.installMarketExpert(runtimeWorkspaceId, item.expertPackageId);
      await refreshAfterMutation();
      closeMarketDetail();
    } catch (reason) {
      const primaryError = reason instanceof Error ? reason.message : "安装专家失败";
      setCardErrors((current) => ({ ...current, [`install:${item.expertPackageId}`]: primaryError }));
      await refreshAfterMutationFailure(item.installedRuntimeAgentProfileId ?? undefined);
      setCardErrors((current) => ({ ...current, [`install:${item.expertPackageId}`]: primaryError }));
    } finally {
      setBusy(null);
    }
  };

  const copyExpert = async (profile: RuntimeAgentProfileSummary) => {
    if (!runtimeWorkspaceId) return;
    setBusy(`copy:${profile.runtimeAgentProfileId}`);
    setError(null);
    try {
      const detail = await userApi.expertDetail(runtimeWorkspaceId, profile.runtimeAgentProfileId);
      const baseName = `${detail.displayName}-副本`;
      const existingNames = new Set([
        ...mine.map((item) => item.displayName),
        ...market.map((item) => item.name)
      ]);
      let name = baseName;
      let suffix = 2;
      while (existingNames.has(name)) name = `${baseName}${suffix++}`;
      setCreateDraft({
        name,
        description: detail.description || "",
        avatarUrl: detail.avatarUrl || DEFAULT_AVATAR_VALUE,
        soulContent: detail.soulContent || "",
        shareScope: emptyScope(),
        capabilities: copyCapabilities(detail.capabilities),
        changeSummary: ""
      });
      setCreateCapabilityPicker(null);
      setCreatePickerCapabilities(null);
      createDraftWorkspaceRef.current = runtimeWorkspaceId;
      setCreateOpen(true);
      void loadConfiguration();
    } catch (reason) {
      const primaryError = reason instanceof Error ? reason.message : "复制专家失败";
      setError(primaryError);
      await refreshAfterMutationFailure(profile.runtimeAgentProfileId);
      setError(primaryError);
    } finally {
      setBusy(null);
    }
  };

  const marketProfile = async (item: ExpertMarketItem): Promise<RuntimeAgentProfileSummary | null> => {
    const profileId = item.installedRuntimeAgentProfileId;
    if (!runtimeWorkspaceId || profileId == null) return null;
    return runnableByProfileId.get(profileId)
      ?? await userApi.expertDetail(runtimeWorkspaceId, profileId);
  };

  const summonMarketExpert = async (item: ExpertMarketItem) => {
    setBusy(`summon:${item.expertPackageId}`);
    setCardErrors((current) => ({ ...current, [`summon:${item.expertPackageId}`]: "" }));
    try {
      const profile = await marketProfile(item);
      if (!profile) throw new Error("专家当前不可召唤");
      onStartChat(profile);
    } catch (reason) {
      setCardErrors((current) => ({
        ...current,
        [`summon:${item.expertPackageId}`]: reason instanceof Error ? reason.message : "召唤专家失败"
      }));
    } finally {
      setBusy(null);
    }
  };

  const requestMarketUninstall = async (item: ExpertMarketItem) => {
    setBusy(`resolve-uninstall:${item.expertPackageId}`);
    try {
      const profile = await marketProfile(item);
      if (!profile) throw new Error("专家安装记录不存在");
      setUninstallTarget(profile);
      closeMarketDetail();
    } catch (reason) {
      setCardErrors((current) => ({
        ...current,
        [`uninstall:${item.expertPackageId}`]: reason instanceof Error ? reason.message : "读取安装状态失败"
      }));
    } finally {
      setBusy(null);
    }
  };

  const openEditor = (profile: RuntimeAgentProfileSummary) => {
    void loadConfiguration();
    setEditing(profile);
    setEditorCapabilityPicker(null);
    setEditorPickerCapabilities(null);
    setCapabilityRemoval(null);
    setStaleSkillSave(null);
    setEditorIdentity({
      name: profile.displayName,
      description: profile.description || "",
      avatarUrl: profile.avatarUrl || DEFAULT_AVATAR_VALUE,
      soulContent: profile.soulContent || ""
    });
    setEditorCapabilities({
      skillIds: [...profile.capabilities.skillIds],
      mcpIds: [...profile.capabilities.mcpIds],
      knowledgeBaseIds: [...profile.capabilities.knowledgeBaseIds],
      enterpriseKnowledgeEnabled: (
        profile.capabilities.enterpriseKnowledgeEnabled ?? false
      ),
      pluginIds: [...profile.capabilities.pluginIds],
      skills: profile.capabilities.skills?.map((item) => ({ ...item })),
      mcps: profile.capabilities.mcps?.map((item) => ({ ...item })),
      knowledgeBases: profile.capabilities.knowledgeBases?.map((item) => ({ ...item }))
    });
    setEditorScope({
      organization: profile.shareScope.organization,
      userIds: [...profile.shareScope.userIds],
      departmentIds: [...profile.shareScope.departmentIds]
    });
    setEditorChangeSummary("");
  };
  const openEditorCapabilityPicker = (kind: CapabilityPicker) => {
    setEditorPickerCapabilities(copyCapabilities(editorCapabilities));
    setEditorCapabilityPicker(kind);
  };
  const closeEditorCapabilityPicker = () => {
    setEditorCapabilityPicker(null);
    setEditorPickerCapabilities(null);
  };
  const confirmEditorCapabilityPicker = () => {
    if (editorPickerCapabilities) {
      setEditorCapabilities(copyCapabilities(editorPickerCapabilities));
    }
    closeEditorCapabilityPicker();
  };

  const submitEditorConfiguration = async (
    profileId: number,
    payload: UpdateExpertConfigurationRequest,
    saveConfiguration: boolean
  ) => {
    if (!runtimeWorkspaceId) return;
    try {
      if (saveConfiguration) {
        await userApi.saveExpertConfiguration(runtimeWorkspaceId, profileId, payload);
      } else {
        await userApi.replaceExpertPermissions(
          runtimeWorkspaceId,
          profileId,
          payload.shareScope,
          payload.changeSummary
        );
      }
      setEditing(null);
      setStaleSkillSave(null);
      await refreshAfterMutation();
    } catch (reason) {
      const primaryError = reason instanceof Error ? reason.message : "保存专家失败";
      setError(primaryError);
      await refreshAfterMutationFailure(profileId);
      setError(primaryError);
      setStaleSkillSave(null);
    } finally {
      setBusy(null);
    }
  };

  const cancelStaleSkillSave = () => {
    setStaleSkillSave(null);
    setBusy(null);
  };

  const confirmStaleSkillSave = () => {
    if (!staleSkillSave || !runtimeWorkspaceId) return;
    const staleAssetIds = new Set(
      staleSkillSave.staleReferences.map((reference) => reference.assetId)
    );
    const payload: UpdateExpertConfigurationRequest = {
      ...staleSkillSave.payload,
      capabilities: {
        ...staleSkillSave.payload.capabilities,
        skills: (staleSkillSave.payload.capabilities.skills || [])
          .filter((reference) => !staleAssetIds.has(reference.assetId)),
        skillIds: (staleSkillSave.payload.capabilities.skillIds || [])
          .filter((assetId) => !staleAssetIds.has(assetId))
      }
    };
    void submitEditorConfiguration(staleSkillSave.profileId, payload, true);
  };

  const saveEditor = async () => {
    if (!runtimeWorkspaceId || !editing) return;
    const normalizedName = editorIdentity.name.trim();
    if (!normalizedName) {
      setError("请填写专家名称");
      window.setTimeout(() => editorNameInputRef.current?.focus());
      return;
    }
    const publishing = (
      editorScope.organization
      || editorScope.userIds.length > 0
      || editorScope.departmentIds.length > 0
    );
    if (
      publishing
      && editing.reviewStatus !== null
      && !editorChangeSummary.trim()
    ) {
      setError("发布新版本前，请填写本次版本改动简介");
      return;
    }
    const id = editing.runtimeAgentProfileId;
    const payload: UpdateExpertConfigurationRequest = {
      name: normalizedName,
      description: editorIdentity.description.trim() || null,
      avatarUrl: editorIdentity.avatarUrl.trim() || null,
      soulContent: editorIdentity.soulContent,
      capabilities: {
        ...editorCapabilities,
        pluginIds: []
      },
      shareScope: editorScope,
      changeSummary: editorChangeSummary.trim() || null
    };
    const identityChanged = (
      payload.name !== editing.displayName
      || (payload.description || "") !== (editing.description || "")
      || (payload.avatarUrl || DEFAULT_AVATAR_VALUE)
        !== (editing.avatarUrl || DEFAULT_AVATAR_VALUE)
      || payload.soulContent !== (editing.soulContent || "")
    );
    const capabilitiesChanged = !runtimeCapabilitiesEqual(
      editorCapabilities,
      editing.capabilities
    );
    const shareChanged = JSON.stringify(canonicalScope(editorScope))
      !== JSON.stringify(canonicalScope(editing.shareScope));
    if (!identityChanged && !capabilitiesChanged && !shareChanged) {
      setError("未检测到专家配置变化");
      return;
    }
    setBusy(`save:${id}`);
    setError(null);
    let catalogForSave: ExpertCapabilityCatalog | null = catalog;
    if (capabilitiesChanged && !configurationCacheRef.current.catalog) {
      catalogForSave = await loadConfiguration({ force: true });
      if (!catalogForSave) {
        setBusy(null);
        return;
      }
    }
    if (capabilitiesChanged && catalogForSave) {
      const staleReferences = stalePersonalSkillReferences(
        editorCapabilities,
        catalogForSave
      );
      if (staleReferences.length > 0) {
        const knownSkillNames = new Map([
          ...catalogForSave.personalSkills,
          ...catalogForSave.installedSkills,
          ...catalogForSave.marketSkills
        ].map((item) => [item.assetId, item.name]));
        setStaleSkillSave({
          profileId: id,
          payload,
          staleReferences,
          skillNames: staleReferences.map((reference) => (
            knownSkillNames.get(reference.assetId) ?? `不可用资产 #${reference.assetId}`
          ))
        });
        return;
      }
    }
    await submitEditorConfiguration(id, payload, identityChanged || capabilitiesChanged);
  };

  const createDraftChanged = (
    Boolean(createDraft.name.trim())
    || Boolean(createDraft.description.trim())
    || createDraft.avatarUrl !== DEFAULT_AVATAR_VALUE
    || Boolean(createDraft.soulContent)
    || Boolean(createDraft.changeSummary.trim())
    || !runtimeCapabilitiesEqual(createDraft.capabilities, emptyCapabilities())
    || JSON.stringify(canonicalScope(createDraft.shareScope))
      !== JSON.stringify(canonicalScope(emptyScope()))
  );
  const editorDraftChanged = Boolean(editing) && (
    editorIdentity.name.trim() !== editing?.displayName
    || editorIdentity.description.trim() !== (editing?.description || "")
    || (editorIdentity.avatarUrl || DEFAULT_AVATAR_VALUE)
      !== (editing?.avatarUrl || DEFAULT_AVATAR_VALUE)
    || editorIdentity.soulContent !== (editing?.soulContent || "")
    || !runtimeCapabilitiesEqual(editorCapabilities, editing?.capabilities || emptyCapabilities())
    || JSON.stringify(canonicalScope(editorScope))
      !== JSON.stringify(canonicalScope(editing?.shareScope || emptyScope()))
    || Boolean(editorChangeSummary.trim())
  );
  const requestCloseEditor = (target: EditorCloseTarget) => {
    if (target === "create") {
      if (busy === "create") return;
      if (createDraftChanged) setEditorCloseTarget("create");
      else {
        createDraftWorkspaceRef.current = null;
        setCreateOpen(false);
      }
      return;
    }
    if (!editing || busy === `save:${editing.runtimeAgentProfileId}`) return;
    if (editorDraftChanged) setEditorCloseTarget("edit");
    else setEditing(null);
  };
  const confirmCloseEditor = () => {
    if (editorCloseTarget === "create") {
      createDraftWorkspaceRef.current = null;
      setCreateOpen(false);
    }
    if (editorCloseTarget === "edit") setEditing(null);
    setEditorCloseTarget(null);
    setError(null);
  };

  const uninstall = async () => {
    if (!runtimeWorkspaceId || !uninstallTarget) return;
    const id = uninstallTarget.runtimeAgentProfileId;
    setBusy(`uninstall:${id}`);
    setError(null);
    try {
      await userApi.uninstallExpert(runtimeWorkspaceId, id);
      setUninstallTarget(null);
      setEditing(null);
      await refreshAfterMutation();
    } catch (reason) {
      const primaryError = reason instanceof Error ? reason.message : "卸载专家失败";
      setError(primaryError);
      await refreshAfterMutationFailure(id);
      setError(primaryError);
    } finally {
      setBusy(null);
    }
  };

  const configurationStatus = () => <>
    {catalogLoading && <p className="expertLoading"><LoaderCircle className="uploadSpinner" size={17}/>正在加载能力目录…</p>}
    {catalogError && <div className="expertDialogError" role="alert">能力目录加载失败：{catalogError}<button type="button" className="secondary" aria-label="重试能力目录" onClick={() => void loadConfiguration({ force: true })}>重试</button></div>}
  </>;
  const renderMineTab = <>
    {error && !createOpen && !editing && !uninstallTarget && <Alert className="expertErrorAlert" role="alert" type="error" showIcon message={error}/>}
    <ExpertMinePanel
      items={visibleMine}
      loading={mineLoading}
      error={mineError}
      busy={busy}
      uncertainProfileIds={uncertainProfileIds}
      onRetry={() => void loadMine()}
      onStartChat={onStartChat}
      onEdit={openEditor}
      onCopy={(profile) => void copyExpert(profile)}
      onDelete={setUninstallTarget}
    />
  </>;
  const renderMarketTab = <>
    {error && !createOpen && !editing && !uninstallTarget && <Alert className="expertErrorAlert" role="alert" type="error" showIcon message={error}/>}
    <ExpertMarketPanel
      items={visibleMarket}
      loading={marketLoading}
      error={marketError}
      busy={busy}
      cardErrors={cardErrors}
      profileStatuses={marketProfileStatusMap}
      pendingProfileIds={pendingMarketProfileIdSet}
      onRetry={() => void loadMarket()}
      onOpenDetail={(item) => void openMarketDetail(item)}
      onInstall={(item) => void install(item)}
      onSummon={(item) => void summonMarketExpert(item)}
      onUninstall={requestMarketUninstall}
    />
  </>;
  // 2026-11-03-016：目录 Tabs 与页头操作区抽为变量——独立模式保持
  // 「页头（标题+操作）→ Tabs」原结构；embedded 模式合并为一行工具栏
  // （左 Tabs 右搜索+创建），嵌入专家-技能大卡片。
  // 搜索框对齐技能广场 skillCatalogSearch：Input + 前缀放大镜（不再是
  // Input.Search 尾部按钮组合框），embedded 下同宽 260px。
  const searchControl = <Input
            className="expertSearchInput"
            prefix={<SearchOutlined aria-hidden/>}
            type="text"
            aria-label="搜索专家"
            placeholder="搜索专家"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            allowClear
          />;
  const createControl = <Button
            type="primary"
            autoInsertSpace={false}
            icon={<PlusOutlined aria-hidden/>}
            onClick={openCreateEditor}
            disabled={!runtimeWorkspaceId}
          >创建专家</Button>;
  const headerActions = <div className="expertModuleHeaderActions">{searchControl}{createControl}</div>;
  // 2026-11-03-017 根因修复：Tabs 的 items 不再携带 children——antd 的 pane
  // 内容属于 Tabs DOM 子树，会把 tabsWrap 撑到列表全高，align-items:center 遂把
  // 右侧操作区垂直居中到列表中段（“掉到卡片同一水平线”）。改为仅导航的 Tabs +
  // 工具栏外的条件渲染列表（对齐技能广场 SkillToolbar 的分离架构）。
  const directoryTabs = <Tabs
        className="expertAntTabs secondaryLaneTabs"
        activeKey={tab}
        onTabClick={(key) => {
          // rc-tabs 的 onChange 只在 key 变化时触发；保留“点击当前 tab 即刷新”的原行为。
          if (key === "mine") {
            setTab("mine");
            void loadMine();
          } else {
            setTab("market");
            void loadMarket();
          }
        }}
        items={[
          {
            key: "mine",
            label: <>我的 <TabCount count={mine.length}/></>
          },
          {
            key: "market",
            label: <>专家市场 <TabCount count={market.length}/></>
          }
        ]}
      />;
  const directoryContent = tab === "mine" ? renderMineTab : renderMarketTab;
  return <section className={embedded ? "expertPageEmbedded" : "expertPage"} aria-label="专家">
    {embedded
      ? <>
          <div className="expertCatalogToolbar">
            <div className="expertCatalogTabsWrap">{directoryTabs}</div>
            <div className="expertCatalogToolbarActions">{searchControl}</div>
          </div>
          {directoryContent}
        </>
      : <div className="expertWorkspace">
          <header className="expertModuleHeader">
            <div className="expertModuleHeaderMain">
              <span className="expertModuleHeaderIcon"><RobotOutlined aria-hidden/></span>
              <div className="expertModuleHeaderText">
                <h2 className="expertModuleHeaderTitle">专家</h2>
                <p className="expertModuleHeaderSubtitle">创建和管理专属专家，按需召唤执行任务</p>
              </div>
            </div>
            {headerActions}
          </header>
          {directoryTabs}
          {directoryContent}
        </div>}


    {marketDetailTarget && <ExpertMarketDetailDialog target={marketDetailTarget} detail={marketDetail} loading={marketDetailLoading} error={marketDetailError} busy={busy} profileStatus={marketDetailTarget.installedRuntimeAgentProfileId != null ? marketProfileStatuses[marketDetailTarget.installedRuntimeAgentProfileId] ?? null : null} pendingProfileIds={pendingMarketProfileIdSet} onClose={closeMarketDetail} onRetry={() => void openMarketDetail(marketDetailTarget)} onInstall={() => void install(marketDetailTarget)} onSummon={() => void summonMarketExpert(marketDetailTarget)} onUninstall={() => void requestMarketUninstall(marketDetailTarget)}/>}


    {createOpen && <ExpertCreateEditorDialog
      busy={busy} error={error}
      catalog={catalog} catalogLoading={catalogLoading} catalogError={catalogError}
      configurationStatus={configurationStatus()}
      organizationDirectory={organizationDirectory} directoryLoading={directoryLoading} directoryError={directoryError}
      onRetryDirectory={() => void loadOrganizationDirectory()}
      onRequestClose={() => requestCloseEditor("create")}
      onErrorClear={() => { if (error) setError(null); }}
      onRemoval={(label, apply) => setCapabilityRemoval({ label, apply })}
      draft={createDraft} setDraft={setCreateDraft}
      nameInputRef={createNameInputRef}
      picker={createCapabilityPicker} onOpenPicker={openCreateCapabilityPicker}
      onPersonalSkillUploaded={async (asset) => {
        setCreateDraft((current) => ({ ...current, capabilities: replaceCapabilityReferences(current.capabilities, "skills", "skillIds", "personal", [...new Set([...capabilityReferences(current.capabilities, "skills", "skillIds").filter((item) => item.source === "personal").map((item) => item.assetId), asset.skillId])], new Map([[asset.skillId, asset.skillVersionId ?? null]])) }));
        await loadConfiguration({ force: true });
      }}
      onSubmit={() => void createExpert()}
    />}

    {createOpen && createCapabilityPicker && createPickerCapabilities && <ExpertCapabilityPicker
      mode="create"
      kind={createCapabilityPicker}
      catalog={catalog}
      value={createPickerCapabilities}
      onChange={setCreatePickerCapabilities}
      onCancel={closeCreateCapabilityPicker}
      onConfirm={confirmCreateCapabilityPicker}
    />}

    {editing && <ExpertEditEditorDialog
      busy={busy} error={error}
      catalog={catalog} catalogLoading={catalogLoading} catalogError={catalogError}
      configurationStatus={configurationStatus()}
      organizationDirectory={organizationDirectory} directoryLoading={directoryLoading} directoryError={directoryError}
      onRetryDirectory={() => void loadOrganizationDirectory()}
      onRequestClose={() => requestCloseEditor("edit")}
      onErrorClear={() => { if (error) setError(null); }}
      onRemoval={(label, apply) => setCapabilityRemoval({ label, apply })}
      editing={editing}
      identity={editorIdentity} setIdentity={setEditorIdentity}
      capabilities={editorCapabilities} setCapabilities={setEditorCapabilities}
      scope={editorScope} setScope={setEditorScope}
      changeSummary={editorChangeSummary} onChangeSummary={(value) => { setEditorChangeSummary(value); if (error) setError(null); }}
      nameInputRef={editorNameInputRef}
      picker={editorCapabilityPicker} onOpenPicker={openEditorCapabilityPicker}
      onPersonalSkillUploaded={async (asset) => {
        setEditorCapabilities((current) => replaceCapabilityReferences(current, "skills", "skillIds", "personal", [...new Set([...capabilityReferences(current, "skills", "skillIds").filter((item) => item.source === "personal").map((item) => item.assetId), asset.skillId])], new Map([[asset.skillId, asset.skillVersionId ?? null]])));
        await loadConfiguration({ force: true });
      }}
      onSubmit={() => void saveEditor()}
    />}

    {editing && editorCapabilityPicker && editorPickerCapabilities && <ExpertCapabilityPicker
      mode="edit"
      expertName={editing.displayName}
      kind={editorCapabilityPicker}
      catalog={catalog}
      value={editorPickerCapabilities}
      onChange={setEditorPickerCapabilities}
      onCancel={closeEditorCapabilityPicker}
      onConfirm={confirmEditorCapabilityPicker}
    />}

    {capabilityRemoval && <ExpertCapabilityRemovalDialog
      label={capabilityRemoval.label}
      onCancel={() => setCapabilityRemoval(null)}
      onConfirm={() => {
        capabilityRemoval.apply();
        setCapabilityRemoval(null);
      }}
    />}

    {editorCloseTarget && <ExpertEditorCloseDialog
      target={editorCloseTarget}
      onCancel={() => setEditorCloseTarget(null)}
      onConfirm={confirmCloseEditor}
    />}

    {staleSkillSave && <ExpertStaleSkillSaveDialog
      skillNames={staleSkillSave.skillNames}
      onCancel={cancelStaleSkillSave}
      onConfirm={confirmStaleSkillSave}
    />}

    {uninstallTarget && <ExpertUninstallDialog target={uninstallTarget} busy={busy === `uninstall:${uninstallTarget.runtimeAgentProfileId}`} error={error} onCancel={() => setUninstallTarget(null)} onConfirm={() => void uninstall()}/>}
  </section>;
}
