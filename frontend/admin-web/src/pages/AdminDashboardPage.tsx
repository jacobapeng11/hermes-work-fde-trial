import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  BarChart3,
  Boxes,
  FileClock,
  Gauge,
  ListChecks,
  RefreshCw,
  Shield,
  Sparkles,
  Star,
  Upload,
  UserCog,
  Wrench
} from "lucide-react";
import type {
  AdminAuditLogSummary,
  AdminModelConfigEntryRequest,
  AdminModelConfigResponse,
  AdminPrincipalSummary,
  AdminRuntimeWorkspaceResourceSummary,
  AdminUsageSummaryResponse,
  AdminPresenceSummaryResponse,
  AdminAssetSummary,
  AdminModelVisibilityUserSummary,
  ModelVisibilityScope,
  OrganizationDirectoryNode,
  RuntimeModelCapabilities,
  SkillCategorySummary
} from "@hermes-cloud/schemas";
import { adminApi } from "../api/client";
import { AdminRuntimeResourceConsole } from "../components/AdminRuntimeResourceConsole";
import { AdminExpertManagement } from "../components/AdminExpertManagement";
import { AdminApplicationAgentPermissions } from "../components/AdminApplicationAgentPermissions";
import { AdminKnowledgeGovernance } from "../components/AdminKnowledgeGovernance";
import { AdminAssetRolloutMonitor } from "../components/AdminAssetRolloutMonitor";
import { AdminModelVisibilityEditor } from "../components/AdminModelVisibilityEditor";
import { AdminWeKnoraRerankConfig } from "../components/AdminWeKnoraRerankConfig";
import { AdminPresenceSummary } from "../components/AdminPresenceSummary";
import { AdminTaskPromptConfig } from "../components/AdminTaskPromptConfig";

type LoadState = "loading" | "ready" | "failed";
type AdminTab =
  | "usage"
  | "models"
  | "resources"
  | "assets"
  | "rollouts"
  | "experts"
  | "application-agent-permissions"
  | "knowledge-governance"
  | "task-prompts"
  | "audit";
type AssetConsoleTab = "skill" | "mcp";
type SkillUploadDraft = {
  file: File | null;
  categoryKey: string;
};
type SkillVersionUpdateDraft = {
  asset: AdminAssetSummary;
  file: File | null;
};
type SkillCategoryDraft = {
  assetId: number;
  categoryKey: string;
};
type McpUploadDraft = {
  name: string;
  description: string;
  transport: "streamable-http";
  serverConfigText: string;
};
type McpVersionUpdateDraft = McpUploadDraft & {
  asset: AdminAssetSummary;
};
type SkillCategoryTab = {
  key: string;
  label: string;
  count: number;
};
type ModelConfigDraftEntry = {
  draftId: string;
  providerName: string;
  providerDisplayName: string;
  providerType: string;
  apiEndpoint: string;
  credentialInput: string;
  credentialConfigured: boolean;
  modelKey: string;
  modelDisplayName: string;
  contextLengthInput: string;
  tagsInput: string;
  isRestricted: boolean;
  visibilityScope: ModelVisibilityScope;
  visibilityUsers: AdminModelVisibilityUserSummary[];
  modelStatus: "active" | "disabled";
  isDefault: boolean;
  runtimeCapabilities: RuntimeModelCapabilities;
};

const DEFAULT_MODEL_CONTEXT_LENGTH = 1_000_000;
const STANDARD_RUNTIME_CAPABILITIES: RuntimeModelCapabilities = {
  thinkingMode: "provider_default",
  reasoningReplay: "not_supported"
};
const THINKING_RUNTIME_CAPABILITIES: RuntimeModelCapabilities = {
  thinkingMode: "enabled",
  reasoningReplay: "required"
};

const navItems: Array<{ key: AdminTab; label: string; icon: typeof Gauge }> = [
  { key: "usage", label: "用量概览", icon: BarChart3 },
  { key: "models", label: "模型治理", icon: Gauge },
  { key: "resources", label: "资源视图", icon: Boxes },
  { key: "assets", label: "广场资产", icon: Upload },
  { key: "rollouts", label: "资产推送", icon: Activity },
  { key: "experts", label: "专家管理", icon: Sparkles },
  { key: "application-agent-permissions", label: "应用 Agent 权限", icon: UserCog },
  { key: "knowledge-governance", label: "知识治理", icon: Shield },
  { key: "task-prompts", label: "任务提示配置", icon: ListChecks },
  { key: "audit", label: "审计日志", icon: FileClock }
];

const ADMIN_TAB_PATHS: Record<AdminTab, string> = {
  usage: "/admin",
  models: "/admin/models",
  resources: "/admin/resources",
  assets: "/admin/assets",
  rollouts: "/admin/rollouts",
  experts: "/admin/experts",
  "application-agent-permissions": "/admin/application-agent-permissions",
  "knowledge-governance": "/admin/knowledge-governance",
  "task-prompts": "/admin/task-prompts",
  audit: "/admin/audit",
};

function adminTabFromPath(pathname: string): AdminTab {
  const normalized = pathname.replace(/\/+$/, "") || "/admin";
  return (Object.entries(ADMIN_TAB_PATHS).find(([, path]) => path === normalized)?.[0] as AdminTab | undefined)
    ?? "usage";
}

function adminTabPath(tab: AdminTab): string {
  return ADMIN_TAB_PATHS[tab];
}

const DEFAULT_SKILL_UPLOAD_DRAFT: SkillUploadDraft = {
  file: null,
  categoryKey: "productivity"
};

const FALLBACK_SKILL_CATEGORIES: SkillCategorySummary[] = [
  { categoryKey: "productivity", displayName: "生产力", isBuiltin: true, isVisible: true },
  { categoryKey: "software-development", displayName: "软件开发", isBuiltin: true, isVisible: true },
  { categoryKey: "data-science", displayName: "数据科学", isBuiltin: true, isVisible: true },
  { categoryKey: "research", displayName: "研究", isBuiltin: true, isVisible: true },
  { categoryKey: "creative", displayName: "创作", isBuiltin: true, isVisible: true },
  { categoryKey: "other", displayName: "其他", isBuiltin: true, isVisible: true }
];

const DEFAULT_MCP_UPLOAD_DRAFT: McpUploadDraft = {
  name: "",
  description: "",
  transport: "streamable-http",
  serverConfigText: `{
  "mcpServers": {
    "my-http-server": {
      "url": "https://mcp.example.com/mcp"
    }
  }
}`
};

function parseMcpServerConfig(source: string): { slug: string; endpoint: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(source);
  } catch {
    throw new Error("MCP 配置必须是有效的 JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("MCP 配置必须是包含 mcpServers 的对象");
  }
  const mcpServers = (parsed as Record<string, unknown>).mcpServers;
  if (!mcpServers || typeof mcpServers !== "object" || Array.isArray(mcpServers)) {
    throw new Error("MCP 配置需要一个 mcpServers 对象");
  }
  const entries = Object.entries(mcpServers as Record<string, unknown>);
  if (entries.length !== 1) {
    throw new Error("一次只能创建一个 MCP；mcpServers 必须只包含一个条目");
  }
  const [slug, entry] = entries[0];
  if (!slug.trim() || !entry || typeof entry !== "object" || Array.isArray(entry)) {
    throw new Error("MCP 条目必须使用非空名称和对象配置");
  }
  const endpoint = (entry as Record<string, unknown>).url;
  if (typeof endpoint !== "string" || !/^https?:\/\/[^\s]+$/i.test(endpoint.trim())) {
    throw new Error("MCP 条目需要 http:// 或 https:// 的 url");
  }
  if (Object.keys(entry as Record<string, unknown>).some((key) => key !== "url")) {
    throw new Error("本期远程 Streamable HTTP MCP 只接受 url 配置");
  }
  return { slug: slug.trim(), endpoint: endpoint.trim() };
}

function mcpVersionUpdateDraft(asset: AdminAssetSummary): McpVersionUpdateDraft {
  const connectionConfig = asset.summary?.connection_config;
  const endpoint = connectionConfig && typeof connectionConfig === "object"
    && typeof (connectionConfig as Record<string, unknown>).url === "string"
    ? String((connectionConfig as Record<string, unknown>).url)
    : typeof asset.manifest?.endpoint === "string"
      ? asset.manifest.endpoint
      : "";
  return {
    asset,
    name: asset.name,
    description: asset.description ?? "",
    transport: "streamable-http",
    serverConfigText: JSON.stringify(
      { mcpServers: { [asset.slug]: { url: endpoint } } },
      null,
      2
    ),
  };
}

function detailText(detail: Record<string, unknown> | null | undefined): string {
  if (!detail) {
    return "-";
  }
  return Object.entries(detail)
    .map(([key, value]) => `${key}:${String(value)}`)
    .join(" / ");
}

function normalizedModelVisibilityScope(
  scope: ModelVisibilityScope
): ModelVisibilityScope {
  if (!scope.organization && scope.departmentIds.length === 0 && scope.userIds.length === 0) {
    return { organization: true, departmentIds: [], userIds: [] };
  }
  return scope;
}

function modelConfigDraftsFromResponse(modelConfig: AdminModelConfigResponse | null): ModelConfigDraftEntry[] {
  return (modelConfig?.entries ?? []).map((entry) => {
    const visibilityScope = normalizedModelVisibilityScope(
      entry.visibilityScope ?? {
        organization: !entry.isRestricted,
        departmentIds: [],
        userIds: []
      }
    );
    return ({
    draftId: `model-${entry.modelEntryId}`,
    providerName: entry.providerName,
    providerDisplayName: entry.providerDisplayName,
    providerType: entry.providerType || "openai_compatible",
    apiEndpoint: entry.apiEndpoint ?? "",
    credentialInput: "",
    credentialConfigured: entry.credentialConfigured,
    modelKey: entry.modelKey,
    modelDisplayName: entry.modelDisplayName,
    contextLengthInput: String(entry.contextLength ?? DEFAULT_MODEL_CONTEXT_LENGTH),
    tagsInput: (entry.tags ?? []).join(", "),
    isRestricted: !visibilityScope.organization,
    visibilityScope,
    visibilityUsers: visibilityScope.organization ? [] : entry.visibilityUsers ?? [],
    modelStatus: entry.modelStatus === "disabled" ? "disabled" : "active",
    isDefault: entry.isDefault,
    runtimeCapabilities: entry.runtimeCapabilities ?? STANDARD_RUNTIME_CAPABILITIES
    });
  });
}

function newModelConfigDraft(): ModelConfigDraftEntry {
  const draftId = `new-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  return {
    draftId,
    providerName: "",
    providerDisplayName: "",
    providerType: "openai_compatible",
    apiEndpoint: "",
    credentialInput: "",
    credentialConfigured: false,
    modelKey: "",
    modelDisplayName: "",
    contextLengthInput: String(DEFAULT_MODEL_CONTEXT_LENGTH),
    tagsInput: "",
    isRestricted: false,
    visibilityScope: { organization: true, userIds: [], departmentIds: [] },
    visibilityUsers: [],
    modelStatus: "active",
    isDefault: false,
    runtimeCapabilities: STANDARD_RUNTIME_CAPABILITIES
  };
}

function draftEntriesToPublishPayload(drafts: ModelConfigDraftEntry[]): AdminModelConfigEntryRequest[] {
  return drafts.map((entry) => {
    const visibilityScope = normalizedModelVisibilityScope(entry.visibilityScope);
    return ({
    providerName: entry.providerName.trim(),
    providerDisplayName: entry.providerDisplayName.trim(),
    providerType: entry.providerType.trim() || "openai_compatible",
    apiEndpoint: entry.apiEndpoint.trim(),
    providerCredential: entry.credentialInput.trim() ? entry.credentialInput : null,
    modelKey: entry.modelKey.trim(),
    modelDisplayName: entry.modelDisplayName.trim(),
    contextLength: entry.contextLengthInput.trim() ? Number(entry.contextLengthInput) : DEFAULT_MODEL_CONTEXT_LENGTH,
    tags: entry.tagsInput
      .split(",")
      .map((tag) => tag.trim())
      .filter(Boolean),
    isRestricted: !visibilityScope.organization,
    visibilityScope,
    modelStatus: entry.modelStatus,
    isDefault: entry.isDefault,
    runtimeCapabilities: entry.runtimeCapabilities
    });
  });
}

function hasThinkingRuntimeProtocol(capabilities: RuntimeModelCapabilities): boolean {
  return capabilities.thinkingMode === "enabled" && capabilities.reasoningReplay === "required";
}

function runtimeCapabilitiesKey(capabilities: RuntimeModelCapabilities): string {
  return `${capabilities.thinkingMode}:${capabilities.reasoningReplay}:${capabilities.vision ?? ""}`;
}

function validateModelConfigDrafts(drafts: ModelConfigDraftEntry[]): string | null {
  if (drafts.length === 0) {
    return "至少需要一个模型配置条目";
  }
  for (const [index, entry] of drafts.entries()) {
    const rowLabel = `第 ${index + 1} 行`;
    if (!entry.providerName.trim() || !entry.providerDisplayName.trim()) {
      return `${rowLabel} 缺少 Provider 标识或名称`;
    }
    if (!entry.apiEndpoint.trim().startsWith("http://") && !entry.apiEndpoint.trim().startsWith("https://")) {
      return `${rowLabel} 的 Provider URL 必须以 http:// 或 https:// 开头`;
    }
    if (!entry.modelKey.trim() || !entry.modelDisplayName.trim()) {
      return `${rowLabel} 缺少 Model 标识或名称`;
    }
    if (entry.contextLengthInput.trim()) {
      const contextLength = Number(entry.contextLengthInput);
      if (!Number.isInteger(contextLength) || contextLength <= 0) {
        return `${rowLabel} 的上下文长度必须是正整数`;
      }
    }
    if (entry.isDefault && entry.modelStatus !== "active") {
      return `${rowLabel} 被设为默认模型时必须保持启用`;
    }
  }
  if (drafts.filter((entry) => entry.isDefault).length > 1) {
    return "只能设置一个默认模型";
  }
  const identities = new Set<string>();
  for (const entry of drafts) {
    const identity = `${entry.providerName.trim()}::${entry.modelKey.trim()}`;
    if (identities.has(identity)) {
      return "Provider 标识和 Model 标识不能重复";
    }
    identities.add(identity);
  }
  return null;
}

function publicAssetSummaryText(asset: AdminAssetSummary): string {
  const summary = asset.summary ?? {};
  const fileCount = typeof summary.fileCount === "number" ? `${summary.fileCount} files` : null;
  const transport = typeof asset.manifest?.transport === "string" ? `transport ${asset.manifest.transport}` : null;
  return [publicAssetTypeLabel(asset.assetType), asset.slug, asset.version, fileCount ?? transport, asset.status]
    .filter(Boolean)
    .join(" / ");
}

function publicAssetTypeLabel(assetType: string): string {
  return assetType === "skill" ? "技能" : "MCP";
}

function publicAssetStatusLabel(status: string): string {
  if (status === "published") {
    return "已发布";
  }
  if (status === "archived" || status === "deleted") {
    return "已删除";
  }
  return status;
}

function publicAssetCategoryLabel(asset: AdminAssetSummary, categories: SkillCategorySummary[]): string | null {
  if (asset.assetType !== "skill") {
    return null;
  }
  const category = asset.categoryKey || "other";
  return categories.find((item) => item.categoryKey === category)?.displayName ?? category;
}

function normalizedSkillCategoryKey(asset: { categoryKey?: string | null }): string {
  return asset.categoryKey?.trim() || "other";
}

function buildSkillCategoryTabs(items: Array<{ categoryKey?: string | null }>, categories: SkillCategorySummary[]): SkillCategoryTab[] {
  const counts = new Map<string, number>();
  for (const item of items) {
    const categoryKey = normalizedSkillCategoryKey(item);
    counts.set(categoryKey, (counts.get(categoryKey) ?? 0) + 1);
  }
  const tabs: SkillCategoryTab[] = [{ key: "all", label: "全部", count: items.length }];
  const knownKeys = new Set<string>();
  for (const category of categories) {
    knownKeys.add(category.categoryKey);
    const count = counts.get(category.categoryKey) ?? 0;
    if (count > 0) {
      tabs.push({ key: category.categoryKey, label: category.displayName, count });
    }
  }
  const unknownTabs = [...counts.entries()]
    .filter(([categoryKey]) => !knownKeys.has(categoryKey))
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([categoryKey, count]) => ({ key: categoryKey, label: categoryKey, count }));
  return [...tabs, ...unknownTabs];
}

function formalSkillCategoryKey(asset: AdminAssetSummary, categories: SkillCategorySummary[]): string {
  const category = asset.categoryKey || "";
  return categories.some((option) => option.categoryKey === category) ? category : categories[0]?.categoryKey ?? "productivity";
}

function publicAssetMetricText(asset: AdminAssetSummary): string {
  const versionCount = typeof asset.summary?.version_count === "number" ? `${asset.summary.version_count} 个版本` : null;
  const fileCount = typeof asset.summary?.fileCount === "number" ? `${asset.summary.fileCount} 个文件` : null;
  const transport = typeof asset.manifest?.transport === "string" ? asset.manifest.transport.toUpperCase() : null;
  return versionCount ?? fileCount ?? transport ?? "无扩展信息";
}

function publicAssetVersionCount(asset: AdminAssetSummary): string {
  return typeof asset.summary?.version_count === "number" ? `${asset.summary.version_count} 个版本` : "1 个版本";
}

function publicAssetFileList(asset: AdminAssetSummary): string[] {
  const summary = asset.summary ?? {};
  const candidates = [
    summary.files,
    summary.fileTree,
    summary.file_tree,
    asset.manifest?.files,
    asset.manifest?.fileTree,
    asset.manifest?.file_tree
  ];
  for (const candidate of candidates) {
    if (Array.isArray(candidate)) {
      return candidate.filter((item): item is string => typeof item === "string").slice(0, 80);
    }
  }
  return [];
}

function publicAssetFileCount(asset: AdminAssetSummary): string {
  const summary = asset.summary ?? {};
  const count = typeof summary.fileCount === "number"
    ? summary.fileCount
    : typeof summary.file_count === "number"
      ? summary.file_count
      : publicAssetFileList(asset).length;
  return count > 0 ? `${count} 个文件` : "文件信息未记录";
}

function publicAssetTransportLabel(asset: AdminAssetSummary): string {
  return typeof asset.manifest?.transport === "string"
    ? asset.manifest.transport.replace(/_/g, "-").toUpperCase()
    : "未记录";
}

function publicAssetEndpointLabel(asset: AdminAssetSummary): string {
  const endpoint = asset.manifest?.endpoint;
  return typeof endpoint === "string" && endpoint.trim() ? endpoint : "未记录";
}

async function loadAdminPublicMcps(): Promise<AdminAssetSummary[]> {
  return adminApi.publicMcps();
}

async function loadAdminPublicSkills(): Promise<AdminAssetSummary[]> {
  return adminApi.publicSkillCatalog();
}

export function AdminDashboardPage() {
  const [tab, setTab] = useState<AdminTab>(() => adminTabFromPath(window.location.pathname));
  const [assetConsoleTab, setAssetConsoleTab] = useState<AssetConsoleTab>("skill");
  const [adminSkillCategoryTab, setAdminSkillCategoryTab] = useState("all");
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [error, setError] = useState<string | null>(null);
  const [principal, setPrincipal] = useState<AdminPrincipalSummary | null>(null);
  const [resources, setResources] = useState<AdminRuntimeWorkspaceResourceSummary[]>([]);
  const [selectedRuntimeResourceKey, setSelectedRuntimeResourceKey] =
    useState<string | null>(null);
  const [auditLogs, setAuditLogs] = useState<AdminAuditLogSummary[]>([]);
  const [modelConfig, setModelConfig] = useState<AdminModelConfigResponse | null>(null);
  const [modelConfigDrafts, setModelConfigDrafts] = useState<ModelConfigDraftEntry[]>([]);
  const [modelDirectory, setModelDirectory] = useState<OrganizationDirectoryNode[]>([]);
  const [modelDirectoryLoading, setModelDirectoryLoading] = useState(false);
  const [modelDirectoryError, setModelDirectoryError] = useState<string | null>(null);
  const modelDirectoryLoadedRef = useRef(false);
  const modelDirectoryLoadingRef = useRef(false);
  const [selectedReasoningProtocolDraftIds, setSelectedReasoningProtocolDraftIds] = useState<Set<string>>(new Set());
  const [usageSummary, setUsageSummary] = useState<AdminUsageSummaryResponse | null>(null);
  const [presenceSummary, setPresenceSummary] = useState<AdminPresenceSummaryResponse | null>(null);
  const [presenceError, setPresenceError] = useState<string | null>(null);
  const [publicAssets, setPublicAssets] = useState<AdminAssetSummary[]>([]);
  const [skillCategories, setSkillCategories] = useState<SkillCategorySummary[]>(FALLBACK_SKILL_CATEGORIES);
  const [lastUploadedAsset, setLastUploadedAsset] = useState<AdminAssetSummary | null>(null);
  const [lastSkillOperation, setLastSkillOperation] = useState<"upload" | "version-update" | null>(null);
  const [selectedPublicAssetDetail, setSelectedPublicAssetDetail] = useState<AdminAssetSummary | null>(null);
  const [publicSkillVersions, setPublicSkillVersions] = useState<AdminAssetSummary[]>([]);
  const [switchingPublicSkillVersionId, setSwitchingPublicSkillVersionId] = useState<number | null>(null);
  const [skillUploadModalOpen, setSkillUploadModalOpen] = useState(false);
  const [skillUploadDraft, setSkillUploadDraft] = useState<SkillUploadDraft>(DEFAULT_SKILL_UPLOAD_DRAFT);
  const [skillUploadError, setSkillUploadError] = useState<string | null>(null);
  const [skillVersionUpdateDraft, setSkillVersionUpdateDraft] = useState<SkillVersionUpdateDraft | null>(null);
  const [skillVersionUpdateError, setSkillVersionUpdateError] = useState<string | null>(null);
  const [editingSkillCategory, setEditingSkillCategory] = useState<SkillCategoryDraft | null>(null);
  const [newSkillCategoryName, setNewSkillCategoryName] = useState("");
  const [creatingSkillCategory, setCreatingSkillCategory] = useState(false);
  const skillFileInputRef = useRef<HTMLInputElement>(null);
  const skillVersionFileInputRef = useRef<HTMLInputElement>(null);
  const [mcpUploadModalOpen, setMcpUploadModalOpen] = useState(false);
  const [mcpUploadDraft, setMcpUploadDraft] = useState<McpUploadDraft>(DEFAULT_MCP_UPLOAD_DRAFT);
  const [mcpUploadError, setMcpUploadError] = useState<string | null>(null);
  const [mcpVersionUpdateDraftState, setMcpVersionUpdateDraftState] = useState<McpVersionUpdateDraft | null>(null);
  const [mcpVersionUpdateError, setMcpVersionUpdateError] = useState<string | null>(null);
  const [lastMcpRolloutNotice, setLastMcpRolloutNotice] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const publicSkills = useMemo(() => {
    const byAssetId = new Map<number, AdminAssetSummary>();
    for (const asset of publicAssets) {
      if (asset.assetType !== "skill") {
        continue;
      }
      const current = byAssetId.get(asset.assetId);
      const isCurrentVersion = Boolean(asset.currentVersionId) && asset.versionId === asset.currentVersionId;
      const currentIsCurrentVersion = Boolean(current?.currentVersionId) && current?.versionId === current?.currentVersionId;
      if (!current || isCurrentVersion || (!currentIsCurrentVersion && (asset.versionId ?? 0) > (current.versionId ?? 0))) {
        byAssetId.set(asset.assetId, asset);
      }
    }
    return [...byAssetId.values()];
  }, [publicAssets]);
  const reasoningProtocolChanges = useMemo(() => {
    const publishedByDraftId = new Map(
      modelConfigDraftsFromResponse(modelConfig).map((entry) => [entry.draftId, entry.runtimeCapabilities])
    );
    return modelConfigDrafts.filter((entry) => (
      runtimeCapabilitiesKey(publishedByDraftId.get(entry.draftId) ?? STANDARD_RUNTIME_CAPABILITIES)
        !== runtimeCapabilitiesKey(entry.runtimeCapabilities)
    ));
  }, [modelConfig, modelConfigDrafts]);

  const loadModelDirectory = useCallback(async () => {
    if (modelDirectoryLoadedRef.current || modelDirectoryLoadingRef.current) return;
    modelDirectoryLoadingRef.current = true;
    setModelDirectoryLoading(true);
    setModelDirectoryError(null);
    try {
      setModelDirectory(await adminApi.modelVisibilityDirectoryTree());
      modelDirectoryLoadedRef.current = true;
    } catch (nextError) {
      setModelDirectoryError(
        nextError instanceof Error ? nextError.message : "组织目录暂时不可用，请重试"
      );
    } finally {
      modelDirectoryLoadingRef.current = false;
      setModelDirectoryLoading(false);
    }
  }, []);

  useEffect(() => {
    function handlePopState() {
      setTab(adminTabFromPath(window.location.pathname));
    }
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  function selectAdminTab(nextTab: AdminTab) {
    setTab(nextTab);
    const nextPath = adminTabPath(nextTab);
    if (window.location.pathname !== nextPath) {
      window.history.pushState({}, "", nextPath);
    }
  }
  // Deleted MCP rows remain in the database for installed-user tombstones, but
  // are not actionable marketplace entries and must disappear from this list.
  const publicMcps = useMemo(
    () => publicAssets.filter((asset) => asset.assetType === "mcp" && asset.status === "published"),
    [publicAssets]
  );
  const adminSkillCategoryTabs = useMemo(
    () => buildSkillCategoryTabs(publicSkills, skillCategories),
    [publicSkills, skillCategories]
  );
  const visiblePublicSkills = useMemo(() => {
    if (adminSkillCategoryTab === "all") {
      return publicSkills;
    }
    return publicSkills.filter((asset) => normalizedSkillCategoryKey(asset) === adminSkillCategoryTab);
  }, [adminSkillCategoryTab, publicSkills]);
  const visiblePublicAssets = assetConsoleTab === "skill" ? visiblePublicSkills : publicMcps;

  useEffect(() => {
    void loadInitialData();
  }, []);

  useEffect(() => {
    if (tab !== "models") return;
    void loadModelDirectory();
  }, [loadModelDirectory, tab]);

  useEffect(() => {
    if (tab !== "resources") return;
    let cancelled = false;
    const refreshPresence = async () => {
      if (document.visibilityState === "hidden") return;
      try {
        const nextPresence = await adminApi.presenceSummary();
        if (!cancelled) {
          setPresenceSummary(nextPresence);
          setPresenceError(null);
        }
      } catch {
        if (!cancelled) {
          setPresenceError("会话或 Runtime 状态暂不可用");
        }
      }
    };
    void refreshPresence();
    const timer = window.setInterval(() => void refreshPresence(), 30_000);
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") void refreshPresence();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [tab]);

  useEffect(() => {
    if (!adminSkillCategoryTabs.some((item) => item.key === adminSkillCategoryTab)) {
      setAdminSkillCategoryTab("all");
    }
  }, [adminSkillCategoryTab, adminSkillCategoryTabs]);

  useEffect(() => {
    if (!selectedPublicAssetDetail || selectedPublicAssetDetail.assetType !== "skill") {
      setPublicSkillVersions([]);
      return;
    }
    void adminApi.publicSkillVersions(selectedPublicAssetDetail.assetId)
      .then(setPublicSkillVersions)
      .catch(() => setPublicSkillVersions([]));
  }, [selectedPublicAssetDetail]);

  async function loadInitialData() {
    setLoadState("loading");
    setError(null);
    try {
      const [me, currentModelConfig, runtimeResources, usage, logs, publicAssetList, publicSkillList, categories] = await Promise.all([
        adminApi.me(),
        adminApi.modelConfig(),
        adminApi.runtimeResources(),
        adminApi.usageSummary(),
        adminApi.auditLogs(),
        loadAdminPublicMcps(),
        loadAdminPublicSkills(),
        adminApi.skillCategories()
      ]);
      setPrincipal(me);
      setModelConfig(currentModelConfig);
      setModelConfigDrafts(modelConfigDraftsFromResponse(currentModelConfig));
      setResources(runtimeResources);
      const resourceKey = runtimeResources[0]?.resourceKey ?? null;
      setSelectedRuntimeResourceKey(resourceKey);
      setUsageSummary(usage);
      setAuditLogs(logs);
      setPublicAssets([...publicSkillList, ...publicAssetList]);
      setSkillCategories(categories.length > 0 ? categories : FALLBACK_SKILL_CATEGORIES);
      setLoadState("ready");
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "加载失败");
      setLoadState("failed");
    }
  }

  async function refreshAudit(action?: string) {
    setAuditLogs(await adminApi.auditLogs(action));
  }

  async function refreshUsage() {
    setUsageSummary(await adminApi.usageSummary());
    try {
      setPresenceSummary(await adminApi.presenceSummary());
      setPresenceError(null);
    } catch {
      setPresenceError("会话或 Runtime 状态暂不可用");
    }
  }

  async function refreshPublicAssets() {
    const [assets, skillAssets, categories] = await Promise.all([
      loadAdminPublicMcps(),
      loadAdminPublicSkills(),
      adminApi.skillCategories()
    ]);
    setPublicAssets([...skillAssets, ...assets]);
    setSkillCategories(categories.length > 0 ? categories : FALLBACK_SKILL_CATEGORIES);
  }

  function currentPublishEntries(): AdminModelConfigEntryRequest[] {
    return draftEntriesToPublishPayload(modelConfigDrafts);
  }

  function updateModelConfigDraft(draftId: string, changes: Partial<ModelConfigDraftEntry>) {
    setError(null);
    setModelConfigDrafts((current) => current.map((entry) => (entry.draftId === draftId ? { ...entry, ...changes } : entry)));
  }

  function updateModelVisibility(
    draftId: string,
    visibilityScope: ModelVisibilityScope,
    visibilityUsers: AdminModelVisibilityUserSummary[]
  ) {
    updateModelConfigDraft(draftId, {
      visibilityScope,
      visibilityUsers,
      isRestricted: !visibilityScope.organization
    });
  }

  function setThinkingRuntimeProtocol(draftId: string, enabled: boolean) {
    setModelConfigDrafts((current) => current.map((entry) => {
      if (entry.draftId !== draftId) return entry;
      return {
        ...entry,
        runtimeCapabilities: {
          ...entry.runtimeCapabilities,
          thinkingMode: enabled ? "enabled" : "provider_default",
          reasoningReplay: enabled ? "required" : "not_supported"
        }
      };
    }));
  }

  function setVisionRuntimeProtocol(draftId: string, enabled: boolean) {
    setModelConfigDrafts((current) => current.map((entry) => {
      if (entry.draftId !== draftId) return entry;
      return {
        ...entry,
        runtimeCapabilities: {
          ...entry.runtimeCapabilities,
          vision: enabled ? "native" : "not_supported"
        }
      };
    }));
  }

  function toggleReasoningProtocolSelection(draftId: string) {
    setSelectedReasoningProtocolDraftIds((current) => {
      const next = new Set(current);
      if (next.has(draftId)) {
        next.delete(draftId);
      } else {
        next.add(draftId);
      }
      return next;
    });
  }

  function applyBatchThinkingRuntimeProtocol(enabled: boolean) {
    if (selectedReasoningProtocolDraftIds.size === 0) {
      setError("请先选择至少一个模型草稿");
      return;
    }
    setError(null);
    setModelConfigDrafts((current) => current.map((entry) => (
      selectedReasoningProtocolDraftIds.has(entry.draftId)
        ? {
          ...entry,
          runtimeCapabilities: {
            ...entry.runtimeCapabilities,
            thinkingMode: enabled ? "enabled" : "provider_default",
            reasoningReplay: enabled ? "required" : "not_supported"
          }
        }
        : entry
    )));
  }

  function addModelConfigDraft() {
    setError(null);
    setModelConfigDrafts((current) => [...current, newModelConfigDraft()]);
  }

  function removeModelConfigDraft(draftId: string) {
    setError(null);
    setModelConfigDrafts((current) => current.filter((entry) => entry.draftId !== draftId));
    setSelectedReasoningProtocolDraftIds((current) => {
      const next = new Set(current);
      next.delete(draftId);
      return next;
    });
  }

  function toggleModelConfigDraftStatus(draftId: string) {
    setError(null);
    setModelConfigDrafts((current) =>
      current.map((entry) => {
        if (entry.draftId !== draftId) {
          return entry;
        }
        const nextStatus = entry.modelStatus === "active" ? "disabled" : "active";
        return {
          ...entry,
          modelStatus: nextStatus,
          isDefault: nextStatus === "active" ? entry.isDefault : false
        };
      })
    );
  }

  function setDefaultModelConfigDraft(draftId: string) {
    setError(null);
    setModelConfigDrafts((current) =>
      current.map((entry) => ({
        ...entry,
        isDefault: entry.draftId === draftId
      }))
    );
  }

  function resetModelConfigDrafts() {
    setError(null);
    setModelConfigDrafts(modelConfigDraftsFromResponse(modelConfig));
    setSelectedReasoningProtocolDraftIds(new Set());
  }

  async function retryPublishModelConfig() {
    setError(null);
    const validationError = validateModelConfigDrafts(modelConfigDrafts);
    if (validationError) {
      setError(validationError);
      return;
    }
    const entries = currentPublishEntries();
    if (entries.length === 0) {
      setError("没有可重新发布的模型配置");
      return;
    }
    if (reasoningProtocolChanges.length > 0) {
      const changeList = reasoningProtocolChanges
        .map((entry) => `${entry.modelDisplayName.trim() || entry.modelKey.trim() || "未命名模型"}：${hasThinkingRuntimeProtocol(entry.runtimeCapabilities) ? "启用思考模型协议" : "恢复标准协议"}`)
        .join("\n");
      if (!window.confirm(`以下推理协议变更仅影响后续新 Run：\n${changeList}\n\n确认发布当前草稿？`)) {
        return;
      }
    }
    setBusyAction("model-config:publish");
    try {
      await adminApi.publishModelConfig(entries);
      const currentModelConfig = await adminApi.modelConfig();
      setModelConfig(currentModelConfig);
      setModelConfigDrafts(modelConfigDraftsFromResponse(currentModelConfig));
      setSelectedReasoningProtocolDraftIds(new Set());
      setError(null);
      await refreshAudit("admin.model.catalog_published");
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "模型目录发布失败");
      const currentModelConfig = await adminApi.modelConfig();
      setModelConfig(currentModelConfig);
      setModelConfigDrafts(modelConfigDraftsFromResponse(currentModelConfig));
      setSelectedReasoningProtocolDraftIds(new Set());
      await refreshAudit("admin.model.catalog_publish_failed");
    } finally {
      setBusyAction(null);
    }
  }

  function selectRuntimeResource(resourceKey: string) {
    setSelectedRuntimeResourceKey(resourceKey);
  }

  function updateSkillUploadDraft(changes: Partial<SkillUploadDraft>) {
    setError(null);
    setSkillUploadError(null);
    setSkillUploadDraft((current) => ({ ...current, ...changes }));
  }

  function openSkillUploadModal() {
    setError(null);
    setSkillUploadError(null);
    setSkillUploadDraft(DEFAULT_SKILL_UPLOAD_DRAFT);
    setSkillUploadModalOpen(true);
  }

  function openSkillVersionUpdateModal(asset: AdminAssetSummary) {
    setError(null);
    setSkillVersionUpdateError(null);
    setSkillVersionUpdateDraft({ asset, file: null });
  }

  function closeSkillVersionUpdateModal() {
    setSkillVersionUpdateError(null);
    setSkillVersionUpdateDraft(null);
    if (skillVersionFileInputRef.current) {
      skillVersionFileInputRef.current.value = "";
    }
  }

  function openMcpUploadModal() {
    setError(null);
    setMcpUploadError(null);
    setMcpUploadDraft(DEFAULT_MCP_UPLOAD_DRAFT);
    setMcpUploadModalOpen(true);
  }

  function openMcpVersionUpdateModal(asset: AdminAssetSummary) {
    setError(null);
    setMcpVersionUpdateError(null);
    setMcpVersionUpdateDraftState(mcpVersionUpdateDraft(asset));
    setSelectedPublicAssetDetail(null);
  }

  function closeMcpVersionUpdateModal() {
    setMcpVersionUpdateError(null);
    setMcpVersionUpdateDraftState(null);
  }

  async function uploadPublicSkill() {
    if (!skillUploadDraft.file) {
      setSkillUploadError("技能上传需要 zip 文件");
      return;
    }
    const categoryKey = skillUploadDraft.categoryKey;
    if (!categoryKey) {
      setSkillUploadError("请选择技能分类");
      return;
    }
    setBusyAction("public-skill:upload");
    try {
      const uploaded = await adminApi.uploadPublicSkill({
        file: skillUploadDraft.file,
        categoryKey,
        categoryDisplayName: null
      });
      setLastUploadedAsset(uploaded);
      setLastSkillOperation("upload");
      setSkillUploadDraft(DEFAULT_SKILL_UPLOAD_DRAFT);
      if (skillFileInputRef.current) {
        skillFileInputRef.current.value = "";
      }
      setSkillUploadModalOpen(false);
      setSkillUploadError(null);
      setError(null);
      await refreshPublicAssets();
    } catch (nextError) {
      setSkillUploadError(nextError instanceof Error ? nextError.message : "公共技能上传失败");
    } finally {
      setBusyAction(null);
    }
  }

  async function uploadPublicSkillVersion() {
    if (!skillVersionUpdateDraft?.file) {
      setSkillVersionUpdateError("技能更新需要 zip 文件");
      return;
    }
    const { asset, file } = skillVersionUpdateDraft;
    setBusyAction(`public-skill:${asset.assetId}:version-update`);
    try {
      const updated = await adminApi.uploadPublicSkillVersion(asset.assetId, file);
      setLastUploadedAsset(updated);
      setLastSkillOperation("version-update");
      closeSkillVersionUpdateModal();
      setError(null);
      await refreshPublicAssets();
    } catch (nextError) {
      setSkillVersionUpdateError(nextError instanceof Error ? nextError.message : "公共技能版本更新失败");
    } finally {
      setBusyAction(null);
    }
  }

  function startEditSkillCategory(asset: AdminAssetSummary) {
    setEditingSkillCategory({
      assetId: asset.assetId,
      categoryKey: formalSkillCategoryKey(asset, skillCategories)
    });
  }

  async function saveSkillCategory(asset: AdminAssetSummary) {
    const nextCategory = editingSkillCategory?.assetId === asset.assetId
      ? editingSkillCategory.categoryKey
      : formalSkillCategoryKey(asset, skillCategories);
    setBusyAction(`public-skill:${asset.assetId}:category`);
    try {
      const updated = await adminApi.updatePublicSkillCategory(asset.assetId, nextCategory);
      setLastUploadedAsset(updated);
      setLastSkillOperation(null);
      setEditingSkillCategory(null);
      setError(null);
      await refreshPublicAssets();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "更新技能分类失败");
    } finally {
      setBusyAction(null);
    }
  }

  async function createSkillCategory(selectCreated: (categoryKey: string) => void) {
    const displayName = newSkillCategoryName.trim();
    if (!displayName || creatingSkillCategory) return;
    setCreatingSkillCategory(true);
    try {
      const created = await adminApi.createSkillCategory({ displayName });
      setSkillCategories((current) => [
        ...current.filter((item) => item.categoryKey !== created.categoryKey),
        created,
      ]);
      selectCreated(created.categoryKey);
      setNewSkillCategoryName("");
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "新建技能分类失败");
    } finally {
      setCreatingSkillCategory(false);
    }
  }

  async function setPublicAssetExecution(asset: AdminAssetSummary, enabled: boolean) {
    if (asset.assetType === "skill") {
      const reason = window.prompt(enabled ? "填写技能重新发布说明" : "填写技能下架说明")?.trim();
      if (!reason) return;
      setBusyAction(`public-skill:${asset.assetId}:market-${enabled ? "publish" : "unpublish"}`);
      try {
        const updated = enabled
          ? await adminApi.republishPublicSkill(asset.assetId, reason, globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${asset.assetId}`)
          : await adminApi.unpublishPublicSkill(asset.assetId, reason);
        setLastMcpRolloutNotice(`技能${enabled ? "发布" : "下架"}成功：${updated.name}；已安装用户继续保留使用。`);
        setSelectedPublicAssetDetail(null);
        setError(null);
        await refreshPublicAssets();
      } catch (nextError) {
        setError(nextError instanceof Error ? nextError.message : `技能${enabled ? "发布" : "下架"}失败`);
      } finally {
        setBusyAction(null);
      }
      return;
    }
    const reason = window.prompt(enabled ? "填写恢复使用的处置说明" : "填写紧急停用的安全原因")?.trim();
    if (!reason) return;
    setBusyAction(`public-${asset.assetType}:${asset.assetId}:${enabled ? "enable" : "force-disable"}`);
    try {
      const idempotencyKey = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${asset.assetType}-${asset.assetId}`;
      const rollout = enabled
        ? await adminApi.enablePublicMcp(asset.assetId, reason, idempotencyKey)
        : await adminApi.forceDisablePublicMcp(asset.assetId, reason, idempotencyKey);
      setLastMcpRolloutNotice(
        `${enabled ? "恢复使用" : "紧急停用"}已接纳：影响 ${rollout.affectedUserCount} 位用户、${rollout.affectedProfileCount} 个 Profile；Rollout ${rollout.rolloutId}。`
      );
      setSelectedPublicAssetDetail(null);
      setError(null);
      await refreshPublicAssets();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : `${enabled ? "恢复使用" : "紧急停用"}失败`);
    } finally {
      setBusyAction(null);
    }
  }

  async function permanentlyDeletePublicAsset(asset: AdminAssetSummary) {
    if (!window.confirm(`确定永久删除「${asset.name}」？删除后无法恢复，已绑定的 Profile 和应用 Agent 将异步移除该资产。`)) {
      return;
    }
    setBusyAction(`public-${asset.assetType}:${asset.assetId}:delete`);
    try {
      const rollout = asset.assetType === "skill"
        ? await adminApi.deletePublicSkill(asset.assetId)
        : await adminApi.deletePublicMcp(asset.assetId);
      setLastMcpRolloutNotice(
        `永久删除已接纳：影响 ${rollout.affectedUserCount} 位用户、${rollout.affectedProfileCount} 个 Profile；Rollout ${rollout.rolloutId}。`
      );
      setSelectedPublicAssetDetail(null);
      setError(null);
      await refreshPublicAssets();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "永久删除失败");
    } finally {
      setBusyAction(null);
    }
  }

  async function selectPublicSkillVersion(asset: AdminAssetSummary, versionId: number) {
    setBusyAction(`public-skill:${asset.assetId}:current-version`);
    setSwitchingPublicSkillVersionId(versionId);
    try {
      const updated = await adminApi.selectPublicSkillVersion(asset.assetId, versionId);
      setSelectedPublicAssetDetail(updated);
      setLastUploadedAsset(updated);
      setLastSkillOperation(null);
      await refreshPublicAssets();
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "切换技能当前版本失败");
    } finally {
      setBusyAction(null);
      setSwitchingPublicSkillVersionId(null);
    }
  }

  async function uploadPublicMcp() {
    setBusyAction("public-mcp:upload");
    try {
      if (!mcpUploadDraft.name.trim()) throw new Error("请填写 MCP 名称");
      const parsed = parseMcpServerConfig(mcpUploadDraft.serverConfigText);
      const created = await adminApi.createPublicMcp({
        slug: parsed.slug,
        name: mcpUploadDraft.name.trim(),
        description: mcpUploadDraft.description.trim() || null,
        transport: mcpUploadDraft.transport,
        endpoint: parsed.endpoint,
        config: {}
      });
      const uploaded = created;
      setLastUploadedAsset(uploaded);
      setLastSkillOperation(null);
      setLastMcpRolloutNotice(null);
      setMcpUploadModalOpen(false);
      setMcpUploadError(null);
      setError(null);
      await refreshPublicAssets();
    } catch (nextError) {
      setMcpUploadError(nextError instanceof Error ? nextError.message : "创建公共 MCP 失败");
    } finally {
      setBusyAction(null);
    }
  }

  async function updatePublicMcp() {
    const draft = mcpVersionUpdateDraftState;
    if (!draft) return;
    setBusyAction(`public-mcp:${draft.asset.assetId}:version-update`);
    try {
      if (!draft.name.trim()) throw new Error("请填写 MCP 名称");
      if (!draft.asset.currentVersionId) throw new Error("当前 MCP 版本不可用，请刷新列表后重试");
      const parsed = parseMcpServerConfig(draft.serverConfigText);
      if (parsed.slug !== draft.asset.slug) {
        throw new Error(`更新配置中的 mcpServers 键必须保持为 ${draft.asset.slug}`);
      }
      const publication = await adminApi.updatePublicMcp({
        assetId: draft.asset.assetId,
        expectedCurrentVersionId: draft.asset.currentVersionId,
        name: draft.name.trim(),
        description: draft.description.trim() || null,
        transport: draft.transport,
        endpoint: parsed.endpoint,
        config: {},
        changeSummary: "管理员更新 MCP 配置",
      });
      setLastUploadedAsset(publication.asset);
      setLastSkillOperation("version-update");
      setLastMcpRolloutNotice(publication.rollout
        ? `新配置已发布，正在自动更新 ${publication.rollout.affectedUserCount} 位用户的 ${publication.rollout.affectedProfileCount} 个 Profile。`
        : "新配置已发布；当前没有需要自动更新的用户 Profile。");
      closeMcpVersionUpdateModal();
      setError(null);
      await refreshPublicAssets();
    } catch (nextError) {
      setMcpVersionUpdateError(nextError instanceof Error ? nextError.message : "更新公共 MCP 失败");
    } finally {
      setBusyAction(null);
    }
  }

  return (
    <main className="adminShell">
      <aside className="adminSidebar" aria-label="管理端导航">
        <div className="adminBrand">
          <Shield size={24} aria-hidden />
          <span>Hermes Console</span>
        </div>
        <nav className="adminNav">
          {navItems.map((item) => (
            <button
              className="adminNavButton"
              data-active={tab === item.key}
              key={item.key}
              type="button"
              onClick={() => selectAdminTab(item.key)}
            >
              <item.icon size={17} aria-hidden />
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
      </aside>

      <section className="adminWorkspace">
        <header className="adminTopbar">
          <div>
            <p className="eyebrow">/admin</p>
            <h1>管理控制台</h1>
            <small className="adminSubtitle">{navItems.find((item) => item.key === tab)?.label ?? "管理端"}</small>
          </div>
          <div className="adminIdentity">
            <span>{principal ? `user ${principal.userId}` : loadState}</span>
            <small>{principal?.roles.join(", ") || "roles"}</small>
            <button className="iconButton" type="button" onClick={loadInitialData} title="刷新">
              <RefreshCw size={16} aria-hidden />
            </button>
          </div>
        </header>

        {error && (
          <div className="adminNotice">
            <strong>{loadState === "failed" ? "加载失败" : "操作失败"}</strong>
            <span>{error}</span>
          </div>
        )}

        {tab === "usage" && usageSummary && (
          <section className="adminPanel" aria-label="用量概览">
            <div className="panelHeader">
              <h2>平台用量</h2>
              <span>{usageSummary.overview.modelCallCount} model calls</span>
            </div>
            <div className="usageMetricGrid">
              <div>
                <span>Runs</span>
                <strong>{usageSummary.overview.runCount}</strong>
                <small>{usageSummary.overview.activeRunCount} active</small>
              </div>
              <div>
                <span>Model Calls</span>
                <strong>{usageSummary.overview.modelCallCount}</strong>
                <small>{usageSummary.overview.averageLatencyMs} ms avg</small>
              </div>
              <div>
                <span>Tokens</span>
                <strong>{usageSummary.overview.totalTokens}</strong>
                <small>{usageSummary.overview.inputTokens} in / {usageSummary.overview.outputTokens} out</small>
              </div>
              <div>
                <span>Errors</span>
                <strong>{usageSummary.overview.errorCount}</strong>
                <small>{usageSummary.overview.auditLogCount} audit logs</small>
              </div>
            </div>

            <div className="usageColumns">
              <section>
                <h3>Runtime workspaces</h3>
                <div className="usageList">
                  {usageSummary.runtimeWorkspaces.map((item) => (
                    <div className="usageRow" key={item.runtimeWorkspaceId}>
                      <div>
                        <strong>rw-{item.runtimeWorkspaceId}</strong>
                        <small>user {item.userId ?? "-"} / {item.runCount} runs / {item.activeRunCount} active</small>
                      </div>
                      <span>{item.totalTokens} tokens</span>
                      <em>{item.errorCount} errors</em>
                    </div>
                  ))}
                </div>
              </section>

              <section>
                <h3>Models</h3>
                <div className="usageList">
                  {usageSummary.models.map((item, index) => (
                    <div className="usageRow" key={`${item.modelEntryId ?? "unknown"}-${index}`}>
                      <div>
                        <strong>{item.modelDisplayName ?? `Model ${item.modelEntryId ?? "-"}`}</strong>
                        <small>{item.modelCallCount} calls / {item.averageLatencyMs} ms avg</small>
                      </div>
                      <span>{item.totalTokens} tokens</span>
                      <em>{item.errorCount} errors</em>
                    </div>
                  ))}
                </div>
              </section>
            </div>

            <div className="usageErrorList">
              <h3>Recent errors</h3>
              {usageSummary.recentErrors.map((item, index) => (
                <div className="usageError" key={`${item.source}-${item.runtimeRunId ?? index}`}>
                  <strong>{item.source}</strong>
                  <span>rw-{item.runtimeWorkspaceId ?? "-"} / {item.runtimeRunId ?? "-"}</span>
                  <small>{item.errorCode ?? item.message ?? "-"}</small>
                </div>
              ))}
              {usageSummary.recentErrors.length === 0 && <p className="muted">暂无错误</p>}
            </div>
          </section>
        )}

        {tab === "models" && (
          <section className="adminPanel" aria-label="模型治理">
            <div className="panelHeader">
              <div>
                <h2>模型治理</h2>
                <span>{modelConfigDrafts.length} configured models</span>
              </div>
              <div className="modelConfigActions">
                <button type="button" onClick={addModelConfigDraft} disabled={busyAction !== null}>
                  添加配置模型
                </button>
                <button
                  type="button"
                  onClick={() => applyBatchThinkingRuntimeProtocol(true)}
                  disabled={busyAction !== null || selectedReasoningProtocolDraftIds.size === 0}
                >
                  批量启用思考协议
                </button>
                <button
                  type="button"
                  onClick={() => applyBatchThinkingRuntimeProtocol(false)}
                  disabled={busyAction !== null || selectedReasoningProtocolDraftIds.size === 0}
                >
                  批量恢复标准协议
                </button>
                <button type="button" onClick={resetModelConfigDrafts} disabled={busyAction !== null}>
                  放弃草稿
                </button>
                <button type="button" onClick={retryPublishModelConfig} disabled={busyAction !== null || modelConfigDrafts.length === 0}>
                  发布当前草稿
                </button>
              </div>
            </div>
            {modelConfig?.latestRelease && (
              <div className="releaseStatusCard">
                <div>
                  <span>发布状态</span>
                  <strong>{modelConfig.latestRelease.status}</strong>
                  <small>
                    version {modelConfig.latestRelease.catalogVersion ?? "-"}
                    {" / "}prev {modelConfig.latestRelease.previousCatalogVersion ?? "-"}
                    {" / "}release #{modelConfig.latestRelease.releaseId}
                  </small>
                </div>
                <div>
                  <span>配置指纹</span>
                  <strong>{modelConfig.latestRelease.configHash?.slice(0, 12) ?? "-"}</strong>
                  <small>
                    {modelConfig.latestRelease.finishedAt ?? modelConfig.latestRelease.startedAt ?? "-"}
                  </small>
                </div>
                <div className="releaseError">
                  <span>失败原因</span>
                  <strong>{modelConfig.latestRelease.errorMessage ?? "无"}</strong>
                </div>
              </div>
            )}
            <AdminWeKnoraRerankConfig />

            <section className="modelConfigEditor" aria-label="模型配置草稿">
              <div className="modelConfigEditorHeader">
                <div>
                  <h3>模型配置与控制</h3>
                  <p className="muted">配置模型成功后即成为可控制条目；禁用会保留配置但对用户不可见，删除会从下次发布的配置清单移除。</p>
                  <p className="muted">选择模型后可批量设置推理协议；批量操作只修改当前草稿，仍需发布才会生效。</p>
                </div>
              </div>
              {reasoningProtocolChanges.length > 0 && (
                <p className="reasoningProtocolChangeSummary" role="status">
                  本次将启用 {reasoningProtocolChanges.filter((entry) => hasThinkingRuntimeProtocol(entry.runtimeCapabilities)).length} 个、恢复 {reasoningProtocolChanges.filter((entry) => !hasThinkingRuntimeProtocol(entry.runtimeCapabilities)).length} 个模型的思考协议；仅影响后续新 Run。
                </p>
              )}
              <div className="modelConfigDraftList">
                {modelConfigDrafts.map((entry, index) => (
                  <div className="modelConfigDraftCard" key={entry.draftId}>
                    <div className="modelConfigDraftTitle">
                      <label className="reasoningProtocolSelection">
                        <input
                          type="checkbox"
                          checked={selectedReasoningProtocolDraftIds.has(entry.draftId)}
                          onChange={() => toggleReasoningProtocolSelection(entry.draftId)}
                          disabled={busyAction !== null}
                          aria-label={`选择 ${entry.modelDisplayName.trim() || entry.modelKey.trim() || `模型 ${index + 1}`} 进行批量推理协议设置`}
                        />
                        批量选择
                      </label>
                      <div>
                        <strong>{entry.modelDisplayName.trim() || `模型 ${index + 1}`}</strong>
                        <small>
                          {entry.modelKey.trim() || "未填写 Model 标识"}
                          {" / "}
                          {entry.providerDisplayName.trim() || entry.providerName.trim() || "未填写 Provider"}
                        </small>
                      </div>
                      <span>{entry.credentialConfigured ? "已配置访问凭证" : "未配置访问凭证"}</span>
                      <span className="statusPill">{entry.modelStatus === "active" ? "启用" : "禁用"}</span>
                      {entry.isDefault && <span className="statusPill statusPillNeutral">默认</span>}
                      <button type="button" onClick={() => toggleModelConfigDraftStatus(entry.draftId)} disabled={busyAction !== null}>
                        <Wrench size={15} aria-hidden />
                        {entry.modelStatus === "active" ? "禁用" : "启用"}
                      </button>
                      <button
                        type="button"
                        onClick={() => setDefaultModelConfigDraft(entry.draftId)}
                        disabled={busyAction !== null || entry.modelStatus !== "active" || entry.isDefault}
                      >
                        <Star size={15} aria-hidden />
                        {entry.isDefault ? "默认" : "设默认"}
                      </button>
                      <button type="button" onClick={() => removeModelConfigDraft(entry.draftId)} disabled={busyAction !== null}>
                        删除
                      </button>
                    </div>
                    <div className="modelConfigGrid">
                      <label>
                        <span>Provider 标识</span>
                        <input
                          value={entry.providerName}
                          onChange={(event) => updateModelConfigDraft(entry.draftId, { providerName: event.target.value })}
                          placeholder="openai-main"
                        />
                      </label>
                      <label>
                        <span>Provider 名称</span>
                        <input
                          value={entry.providerDisplayName}
                          onChange={(event) => updateModelConfigDraft(entry.draftId, { providerDisplayName: event.target.value })}
                          placeholder="OpenAI Main"
                        />
                      </label>
                      <label>
                        <span>Provider URL</span>
                        <input
                          value={entry.apiEndpoint}
                          onChange={(event) => updateModelConfigDraft(entry.draftId, { apiEndpoint: event.target.value })}
                          placeholder="https://provider.example/v1"
                        />
                      </label>
                      <label>
                        <span>访问凭证</span>
                        <input
                          type="password"
                          autoComplete="new-password"
                          value={entry.credentialInput}
                          onChange={(event) => updateModelConfigDraft(entry.draftId, { credentialInput: event.target.value })}
                          placeholder={entry.credentialConfigured ? "留空保留现有凭证" : "可先留空，后续补齐"}
                        />
                      </label>
                      <label>
                        <span>Model 标识</span>
                        <input
                          value={entry.modelKey}
                          onChange={(event) => updateModelConfigDraft(entry.draftId, { modelKey: event.target.value })}
                          placeholder="gpt-4.1-mini"
                        />
                      </label>
                      <label>
                        <span>Model 名称</span>
                        <input
                          value={entry.modelDisplayName}
                          onChange={(event) => updateModelConfigDraft(entry.draftId, { modelDisplayName: event.target.value })}
                          placeholder="GPT 4.1 Mini"
                        />
                      </label>
                      <label>
                        <span>上下文长度</span>
                        <input
                          inputMode="numeric"
                          value={entry.contextLengthInput}
                          onChange={(event) => updateModelConfigDraft(entry.draftId, { contextLengthInput: event.target.value })}
                          placeholder="8192"
                        />
                      </label>
                      <label>
                        <span>标签</span>
                        <input
                          value={entry.tagsInput}
                          onChange={(event) => updateModelConfigDraft(entry.draftId, { tagsInput: event.target.value })}
                          placeholder="chat, fast"
                        />
                      </label>
                    </div>
                    <AdminModelVisibilityEditor
                      value={entry.visibilityScope}
                      selectedUsers={entry.visibilityUsers}
                      directory={modelDirectory}
                      loading={modelDirectoryLoading}
                      error={modelDirectoryError}
                      onRetry={() => void loadModelDirectory()}
                      onChange={(visibilityScope, visibilityUsers) => updateModelVisibility(
                        entry.draftId,
                        visibilityScope,
                        visibilityUsers
                      )}
                      disabled={busyAction !== null}
                    />
                    <label className="thinkingProtocolToggle">
                      <input
                        type="checkbox"
                        aria-label="思考模型协议"
                        checked={hasThinkingRuntimeProtocol(entry.runtimeCapabilities)}
                        onChange={(event) => setThinkingRuntimeProtocol(entry.draftId, event.target.checked)}
                        disabled={busyAction !== null}
                      />
                      <span>
                        <strong>思考模型协议</strong>
                        <small>启用 thinking，并在工具调用续写时回传 reasoning_content。仅为已确认兼容的模型开启。</small>
                      </span>
                    </label>
                    <label className="thinkingProtocolToggle">
                      <input
                        type="checkbox"
                        aria-label="支持图片输入"
                        checked={entry.runtimeCapabilities.vision === "native"}
                        onChange={(event) => setVisionRuntimeProtocol(entry.draftId, event.target.checked)}
                        disabled={busyAction !== null}
                      />
                      <span>
                        <strong>支持图片输入</strong>
                        <small>允许该模型接收图片输入并按 multimodal 请求发送。仅为确认支持视觉的模型开启。</small>
                      </span>
                    </label>
                  </div>
                ))}
                {modelConfigDrafts.length === 0 && <p className="muted">暂无草稿条目，请新增模型配置。</p>}
              </div>
            </section>
          </section>
        )}

        {tab === "resources" && (
          <>
            <AdminPresenceSummary summary={presenceSummary} error={presenceError} />
            <AdminRuntimeResourceConsole
              resources={resources}
              selectedResourceKey={selectedRuntimeResourceKey}
              onSelectResource={selectRuntimeResource}
              onError={setError}
            />
          </>
        )}

        {tab === "assets" && (
          <section className="adminPanel" aria-label="广场资产">
            <div className="assetConsoleTabs" aria-label="广场资产类型">
              <button type="button" aria-pressed={assetConsoleTab === "skill"} onClick={() => setAssetConsoleTab("skill")}>
                技能管理
                <span>{publicSkills.length}</span>
              </button>
              <button type="button" aria-pressed={assetConsoleTab === "mcp"} onClick={() => setAssetConsoleTab("mcp")}>
                MCP 管理
                <span>{publicMcps.length}</span>
              </button>
            </div>

            {lastUploadedAsset && (
              <div className="assetUploadResult" role="status">
                <strong>{lastSkillOperation === "version-update" ? "版本更新成功" : "上传成功"}</strong>
                <span>{publicAssetSummaryText(lastUploadedAsset)}</span>
                <small>管理员上传公共资产为可信直发，不进入审核队列。</small>
              </div>
            )}
            {lastMcpRolloutNotice && (
              <div className="assetUploadResult" role="status">
                <strong>资产操作已提交</strong>
                <span>{lastMcpRolloutNotice}</span>
                <small>可在“资产推送”查看每位用户和 Profile 的异步进度。</small>
              </div>
            )}

            {assetConsoleTab === "mcp" && (
              <div className="assetUploadGrid">
                <section className="assetUploadCard" aria-label="公共 MCP 说明">
                  <h3>远程 Streamable HTTP MCP</h3>
                  <p className="muted">仅支持无凭据的远程 Streamable HTTP 服务；已有 MCP 请使用“更新配置”，系统会自动切换所有正在使用它的用户 Profile。</p>
                </section>
              </div>
            )}

            <section className="publicAssetList" aria-label={assetConsoleTab === "skill" ? "公共技能列表" : "公共 MCP 列表"}>
              <div className="assetListHeader">
                <div>
                  <h3>{assetConsoleTab === "skill" ? "公共技能库" : "公共 MCP 库"}</h3>
                  <p className="muted">
                    {assetConsoleTab === "skill"
                      ? "每张卡片代表一个技能家族；新技能使用上传入口，已有技能请从卡片执行版本更新或管理市场发布状态。"
                      : "紧急停用会隐藏市场资产并通过 Rollout 暂停现有能力；恢复使用后重新开放市场与既有绑定。"}
                  </p>
                </div>
                {assetConsoleTab === "skill" ? (
                  <button type="button" className="primaryButton" onClick={openSkillUploadModal} disabled={busyAction !== null}>
                    <Upload size={15} aria-hidden />
                    上传技能
                  </button>
                ) : (
                  <div className="assetListHeaderActions">
                    <button type="button" className="primaryButton" onClick={openMcpUploadModal} disabled={busyAction !== null}>
                      <Upload size={15} aria-hidden />
                      创建 MCP
                    </button>
                  </div>
                )}
              </div>
              {assetConsoleTab === "skill" && (
                <div className="assetCategoryTabs" role="group" aria-label="技能分类">
                  {adminSkillCategoryTabs.map((category) => (
                    <button
                      key={category.key}
                      type="button"
                      aria-pressed={adminSkillCategoryTab === category.key}
                      onClick={() => setAdminSkillCategoryTab(category.key)}
                    >
                      {category.label}
                      <span>{category.count}</span>
                    </button>
                  ))}
                </div>
              )}
              <div className="publicAssetGrid">
                {visiblePublicAssets.map((asset) => {
                  const categoryLabel = publicAssetCategoryLabel(asset, skillCategories);
                  const isEditingCategory = editingSkillCategory?.assetId === asset.assetId;
                  return (
                    <article className="publicAssetItem" key={`${asset.assetType}-${asset.assetId}`}>
                      <button type="button" className="publicAssetCardBody publicAssetCardBodyButton" onClick={() => setSelectedPublicAssetDetail(asset)}>
                        <span className="assetKind">{publicAssetTypeLabel(asset.assetType)} · v{asset.version}</span>
                        <span className="assetSourceTag" data-source="public">公共资产</span>
                        <strong>{asset.name}</strong>
                        <p>{asset.description || "无描述"}</p>
                        <span className={`assetStatusBadge assetStatusBadge--${asset.status}`}>{publicAssetStatusLabel(asset.status)}</span>
                        {categoryLabel && <span className="assetCategoryBadge">分类：{categoryLabel}</span>}
                        {asset.assetType !== "skill" && (
                          <span className="assetCategoryBadge">协议：{publicAssetTransportLabel(asset)}</span>
                        )}
                      </button>
                      {asset.assetType === "skill" && isEditingCategory && (
                        <div className="assetCategoryEditor">
                          <label>
                            <span>技能分类</span>
                            <select
                              value={editingSkillCategory.categoryKey}
                              onChange={(event) => setEditingSkillCategory({
                                assetId: asset.assetId,
                                categoryKey: event.target.value
                              })}
                              disabled={busyAction !== null}
                            >
                              {skillCategories.map((option) => (
                                <option key={option.categoryKey} value={option.categoryKey}>
                                  {option.displayName} · {option.categoryKey}
                                </option>
                              ))}
                            </select>
                          </label>
                          <div className="assetCategoryCreate">
                            <input
                              aria-label="新分类名称"
                              value={newSkillCategoryName}
                              maxLength={100}
                              placeholder="新建分类"
                              onChange={(event) => setNewSkillCategoryName(event.target.value)}
                              disabled={busyAction !== null || creatingSkillCategory}
                            />
                            <button
                              type="button"
                              className="secondaryButton"
                              disabled={!newSkillCategoryName.trim() || busyAction !== null || creatingSkillCategory}
                              onClick={() => void createSkillCategory((categoryKey) => setEditingSkillCategory({ assetId: asset.assetId, categoryKey }))}
                            >
                              {creatingSkillCategory ? "新建中" : "新建分类"}
                            </button>
                          </div>
                          <div>
                            <button type="button" className="primaryButton" onClick={() => void saveSkillCategory(asset)} disabled={busyAction !== null}>
                              保存分类
                            </button>
                            <button type="button" className="secondaryButton" onClick={() => setEditingSkillCategory(null)} disabled={busyAction !== null}>
                              取消
                            </button>
                          </div>
                        </div>
                      )}
                      <div className="assetListActions">
                        {asset.assetType === "skill" && (
                          <>
                            {!isEditingCategory && (
                              <button type="button" className="secondaryButton" onClick={() => startEditSkillCategory(asset)} disabled={busyAction !== null}>
                                设置分类
                              </button>
                            )}
                            <button type="button" className="secondaryButton" onClick={() => openSkillVersionUpdateModal(asset)} disabled={busyAction !== null}>
                              版本更新
                            </button>
                            <button type="button" className="dangerButton" onClick={() => void setPublicAssetExecution(asset, asset.marketVisible === false)} disabled={busyAction !== null}>
                              {asset.marketVisible === false ? "技能发布" : "技能下架"}
                            </button>
                          </>
                        )}
                        {asset.assetType !== "skill" && (
                          <>
                            <button type="button" className="secondaryButton" onClick={() => openMcpVersionUpdateModal(asset)} disabled={busyAction !== null}>
                              更新配置
                            </button>
                            <button type="button" className="dangerButton" onClick={() => void setPublicAssetExecution(asset, asset.executionStatus === "force_disabled")} disabled={busyAction !== null}>
                              {asset.executionStatus === "force_disabled" ? "恢复使用" : "紧急停用"}
                            </button>
                          </>
                        )}
                      </div>
                    </article>
                  );
                })}
              </div>
              {visiblePublicAssets.length === 0 && (
                <p className="muted">
                  {assetConsoleTab === "skill" ? "暂无公共技能，点击右上角上传技能 zip 后会显示在这里。" : "暂无公共 MCP，点击创建 MCP 后会显示在这里。"}
                </p>
              )}
            </section>

            {skillUploadModalOpen && (
              <div className="adminModalBackdrop" role="presentation">
                <section className="adminModalCard" role="dialog" aria-modal="true" aria-label="上传公共技能">
                  <div className="adminModalHeader">
                    <div>
                      <h3>上传公共技能</h3>
                      <p className="muted">上传包含 SKILL.md 的 zip 包。请选择新技能的初始分类；已有技能请从卡片点击“版本更新”。</p>
                    </div>
                    <button type="button" className="secondaryButton adminModalCloseButton" onClick={() => { setSkillUploadError(null); setSkillUploadModalOpen(false); }} disabled={busyAction !== null}>
                      关闭
                    </button>
                  </div>
                  <div className="assetUploadForm">
                    <label className="wideField">
                      <span>初始分类</span>
                      <select
                        value={skillUploadDraft.categoryKey}
                        onChange={(event) => updateSkillUploadDraft({ categoryKey: event.target.value })}
                      >
                        {skillCategories.map((category) => (
                          <option key={category.categoryKey} value={category.categoryKey}>
                            {category.displayName} · {category.categoryKey}
                          </option>
                        ))}
                      </select>
                      <small>分类会映射到 Hermes Runtime 路径，例如 skills/productivity/skill-123/。</small>
                    </label>
                    <div className="assetCategoryCreate wideField">
                      <input
                        aria-label="新分类名称"
                        value={newSkillCategoryName}
                        maxLength={100}
                        placeholder="新建分类"
                        onChange={(event) => setNewSkillCategoryName(event.target.value)}
                        disabled={busyAction !== null || creatingSkillCategory}
                      />
                      <button
                        type="button"
                        className="secondaryButton"
                        disabled={!newSkillCategoryName.trim() || busyAction !== null || creatingSkillCategory}
                        onClick={() => void createSkillCategory((categoryKey) => updateSkillUploadDraft({ categoryKey }))}
                      >
                        {creatingSkillCategory ? "新建中" : "新建分类"}
                      </button>
                    </div>
                    <label className="wideField">
                        <span>技能 zip</span>
                      <input
                        ref={skillFileInputRef}
                        type="file"
                        accept=".zip,application/zip"
                        onChange={(event) => updateSkillUploadDraft({ file: event.target.files?.[0] ?? null })}
                      />
                      <small>{skillUploadDraft.file ? skillUploadDraft.file.name : "需要包含 SKILL.md，后端会拒绝 unsafe path。"}</small>
                    </label>
                  </div>
                  {skillUploadError && <p className="adminModalError" role="alert">{skillUploadError}</p>}
                  <div className="adminModalActions">
                    <button type="button" className="secondaryButton" onClick={() => { setSkillUploadError(null); setSkillUploadModalOpen(false); }} disabled={busyAction !== null}>
                      取消
                    </button>
                    <button
                      type="button"
                      className="primaryButton"
                      onClick={uploadPublicSkill}
                      disabled={busyAction !== null || !skillUploadDraft.file}
                    >
                      <Upload size={15} aria-hidden />
                      {busyAction === "public-skill:upload" ? "上传中" : "确认上传"}
                    </button>
                  </div>
                </section>
              </div>
            )}
            {skillVersionUpdateDraft && (
              <div className="adminModalBackdrop" role="presentation">
                <section className="adminModalCard" role="dialog" aria-modal="true" aria-label={`更新「${skillVersionUpdateDraft.asset.name}」`}>
                  <div className="adminModalHeader">
                    <div>
                      <h3>更新「{skillVersionUpdateDraft.asset.name}」</h3>
                      <p className="muted">当前版本：{skillVersionUpdateDraft.asset.version} · 当前分类：{publicAssetCategoryLabel(skillVersionUpdateDraft.asset, skillCategories) ?? "未分类"}</p>
                    </div>
                    <button type="button" className="secondaryButton" onClick={closeSkillVersionUpdateModal} disabled={busyAction !== null}>关闭</button>
                  </div>
                  <div className="assetUploadForm">
                    <label className="wideField">
                        <span>技能 zip</span>
                      <input
                        ref={skillVersionFileInputRef}
                        type="file"
                        aria-label="技能 zip"
                        accept=".zip,application/zip"
                        onChange={(event) => {
                          setSkillVersionUpdateError(null);
                          setSkillVersionUpdateDraft((current) => current ? { ...current, file: event.target.files?.[0] ?? null } : current);
                        }}
                      />
                      <small>{skillVersionUpdateDraft.file ? skillVersionUpdateDraft.file.name : "更新包内 SKILL.md 名称必须与当前技能一致。"}</small>
                    </label>
                  </div>
                  {skillVersionUpdateError && <p className="adminModalError" role="alert">{skillVersionUpdateError}</p>}
                  <div className="adminModalActions">
                    <button type="button" className="secondaryButton" onClick={closeSkillVersionUpdateModal} disabled={busyAction !== null}>取消</button>
                    <button type="button" className="primaryButton" onClick={() => void uploadPublicSkillVersion()} disabled={busyAction !== null || !skillVersionUpdateDraft.file}>
                      <Upload size={15} aria-hidden />
                      {busyAction === `public-skill:${skillVersionUpdateDraft.asset.assetId}:version-update` ? "更新中" : "确认更新"}
                    </button>
                  </div>
                </section>
              </div>
            )}
            {mcpUploadModalOpen && (
              <div className="adminModalBackdrop" role="presentation">
                <section className="adminModalCard" role="dialog" aria-modal="true" aria-label="创建公共 MCP">
                  <div className="adminModalHeader">
                    <div>
                      <h3>创建公共 MCP</h3>
                      <p className="muted">粘贴一个 mcpServers 条目。系统会从条目名解析 slug，从 url 解析服务地址；创建后可通过“更新配置”无感切换用户。</p>
                    </div>
                    <button type="button" className="secondaryButton" onClick={() => { setMcpUploadError(null); setMcpUploadModalOpen(false); }} disabled={busyAction !== null}>关闭</button>
                  </div>
                  <div className="assetUploadForm">
                    <label htmlFor="mcp-upload-name">
                      <span>名称</span>
                      <input id="mcp-upload-name" value={mcpUploadDraft.name} onChange={(event) => setMcpUploadDraft((current) => ({ ...current, name: event.target.value }))} placeholder="例如：内部搜索" />
                    </label>
                    <label htmlFor="mcp-upload-description">
                      <span>描述（可选）</span>
                      <input id="mcp-upload-description" value={mcpUploadDraft.description} onChange={(event) => setMcpUploadDraft((current) => ({ ...current, description: event.target.value }))} placeholder="说明该 MCP 的用途" />
                    </label>
                    <label htmlFor="mcp-upload-transport">
                      <span>传输方式</span>
                      <select id="mcp-upload-transport" value={mcpUploadDraft.transport} disabled>
                        <option value="streamable-http">Streamable HTTP（远程）</option>
                      </select>
                      <small>只支持远程无凭据服务；可使用内网或公网 HTTP(S) URL。</small>
                    </label>
                    <label className="manifestEditor wideField" htmlFor="mcp-upload-server-config">
                      <span>mcpServers 配置</span>
                      <textarea id="mcp-upload-server-config" aria-label="mcpServers 配置" value={mcpUploadDraft.serverConfigText} onChange={(event) => setMcpUploadDraft((current) => ({ ...current, serverConfigText: event.target.value }))} spellCheck={false} />
                      <small>只接受一个条目，且条目只包含 url。</small>
                    </label>
                  </div>
                  {mcpUploadError && <p className="adminModalError" role="alert">{mcpUploadError}</p>}
                  <div className="adminModalActions">
                    <button type="button" className="secondaryButton" onClick={() => { setMcpUploadError(null); setMcpUploadModalOpen(false); }} disabled={busyAction !== null}>取消</button>
                    <button type="button" className="primaryButton" onClick={() => void uploadPublicMcp()} disabled={busyAction !== null}>
                      <Upload size={15} aria-hidden />
                      {busyAction === "public-mcp:upload" ? "创建中" : "确认创建"}
                    </button>
                  </div>
                </section>
              </div>
            )}
            {mcpVersionUpdateDraftState && (
              <div className="adminModalBackdrop" role="presentation">
                <section className="adminModalCard" role="dialog" aria-modal="true" aria-label="更新 MCP 配置">
                  <div className="adminModalHeader">
                    <div>
                      <h3>更新 MCP 配置</h3>
                      <p className="muted">保留当前 MCP 身份和用户绑定，发布新版本后由系统自动切换正在使用它的 Profile。</p>
                    </div>
                    <button type="button" className="secondaryButton" onClick={closeMcpVersionUpdateModal} disabled={busyAction !== null}>关闭</button>
                  </div>
                  <div className="assetUploadForm">
                    <label htmlFor="mcp-update-slug">
                      <span>slug（不可修改）</span>
                      <input id="mcp-update-slug" value={mcpVersionUpdateDraftState.asset.slug} readOnly />
                    </label>
                    <label htmlFor="mcp-update-name">
                      <span>名称</span>
                      <input id="mcp-update-name" value={mcpVersionUpdateDraftState.name} onChange={(event) => setMcpVersionUpdateDraftState((current) => current ? ({ ...current, name: event.target.value }) : current)} />
                    </label>
                    <label htmlFor="mcp-update-description">
                      <span>描述（可选）</span>
                      <input id="mcp-update-description" value={mcpVersionUpdateDraftState.description} onChange={(event) => setMcpVersionUpdateDraftState((current) => current ? ({ ...current, description: event.target.value }) : current)} />
                    </label>
                    <label htmlFor="mcp-update-transport">
                      <span>传输方式</span>
                      <select id="mcp-update-transport" value={mcpVersionUpdateDraftState.transport} disabled>
                        <option value="streamable-http">Streamable HTTP（远程）</option>
                      </select>
                    </label>
                    <label className="manifestEditor wideField" htmlFor="mcp-update-server-config">
                      <span>mcpServers 配置</span>
                      <textarea id="mcp-update-server-config" aria-label="更新 mcpServers 配置" value={mcpVersionUpdateDraftState.serverConfigText} onChange={(event) => setMcpVersionUpdateDraftState((current) => current ? ({ ...current, serverConfigText: event.target.value }) : current)} spellCheck={false} />
                      <small>条目键必须与原 slug 一致；修改 slug 请创建全新 MCP。</small>
                    </label>
                  </div>
                  {mcpVersionUpdateError && <p className="adminModalError" role="alert">{mcpVersionUpdateError}</p>}
                  <div className="adminModalActions">
                    <button type="button" className="secondaryButton" onClick={closeMcpVersionUpdateModal} disabled={busyAction !== null}>取消</button>
                    <button type="button" className="primaryButton" onClick={() => void updatePublicMcp()} disabled={busyAction !== null}>
                      <RefreshCw size={15} aria-hidden />
                      {busyAction === `public-mcp:${mcpVersionUpdateDraftState.asset.assetId}:version-update` ? "更新中" : "确认更新"}
                    </button>
                  </div>
                </section>
              </div>
            )}
          </section>
        )}

        {tab === "rollouts" && <AdminAssetRolloutMonitor/>}

        {selectedPublicAssetDetail && (
          <div className="adminModalBackdrop" role="presentation" onClick={() => setSelectedPublicAssetDetail(null)}>
            <section
              className="adminCapabilityModal"
              role="dialog"
              aria-modal="true"
              aria-label={`${selectedPublicAssetDetail.name} 详情`}
              onClick={(event) => event.stopPropagation()}
            >
              <button type="button" className="drawerClose" onClick={() => setSelectedPublicAssetDetail(null)}>
                关闭
              </button>
              <div className="adminCapabilityHero">
                <span className="assetKind">{publicAssetTypeLabel(selectedPublicAssetDetail.assetType)} · v{selectedPublicAssetDetail.version}</span>
                <h2>{selectedPublicAssetDetail.name}</h2>
                <p>{selectedPublicAssetDetail.description || selectedPublicAssetDetail.slug}</p>
                <div className="adminAssetBadgeRow">
                  <span className="assetSourceTag" data-source="public">公共资产</span>
                  <span className={`assetStatusBadge assetStatusBadge--${selectedPublicAssetDetail.status}`}>{publicAssetStatusLabel(selectedPublicAssetDetail.status)}</span>
                  {selectedPublicAssetDetail.assetType === "skill" && (
                    <span className="assetCategoryBadge">分类：{publicAssetCategoryLabel(selectedPublicAssetDetail, skillCategories) ?? "未分类"}</span>
                  )}
                  {selectedPublicAssetDetail.assetType !== "skill" && (
                    <span className="assetCategoryBadge">协议：{publicAssetTransportLabel(selectedPublicAssetDetail)}</span>
                  )}
                </div>
              </div>

              <section className="adminCapabilitySection">
                <h3>基础信息</h3>
                <dl className="assetDetailMeta">
                  <dt>Slug</dt>
                  <dd>{selectedPublicAssetDetail.slug}</dd>
                  <dt>当前版本</dt>
                  <dd>{selectedPublicAssetDetail.version}</dd>
                  <dt>版本数量</dt>
                  <dd>{publicAssetVersionCount(selectedPublicAssetDetail)}</dd>
                  <dt>资产 ID</dt>
                  <dd>{selectedPublicAssetDetail.assetId}</dd>
                  <dt>版本 ID</dt>
                  <dd>{selectedPublicAssetDetail.versionId ?? "-"}</dd>
                  <dt>存储位置</dt>
                  <dd>{selectedPublicAssetDetail.storageUri}</dd>
                </dl>
                {selectedPublicAssetDetail.assetType === "skill" && publicSkillVersions.length > 0 && (
                  <section className="assetMarketVersions" aria-labelledby="asset-market-version-title">
                    <h4 id="asset-market-version-title">市场版本</h4>
                    <ul className="assetMarketVersionList" aria-label="市场版本列表">
                      {publicSkillVersions.map((version) => {
                        const isCurrent = version.versionId === selectedPublicAssetDetail.currentVersionId
                          || (selectedPublicAssetDetail.currentVersionId == null && version.version === selectedPublicAssetDetail.version);
                        const isSwitching = switchingPublicSkillVersionId === version.versionId;
                        return (
                          <li className={`assetMarketVersionItem${isCurrent ? " is-current" : ""}`} key={version.versionId ?? version.version}>
                            <div className="assetMarketVersionMeta">
                              <strong>v{version.version}</strong>
                              <span className={`assetMarketVersionState${isCurrent ? " is-current" : ""}`}>
                                {isCurrent ? "当前市场版本" : "历史版本"}
                              </span>
                            </div>
                            {!isCurrent && <span className="muted">仅保留审计记录</span>}
                          </li>
                        );
                      })}
                    </ul>
                  </section>
                )}
              </section>

              {selectedPublicAssetDetail.assetType === "skill" ? (
                <section className="adminCapabilitySection">
                  <h3>技能内容</h3>
                  <p className="adminSectionHint">{publicAssetFileCount(selectedPublicAssetDetail)}</p>
                  {publicAssetFileList(selectedPublicAssetDetail).length > 0 ? (
                    <ul className="assetDetailList">
                      {publicAssetFileList(selectedPublicAssetDetail).map((item) => <li key={item}>{item}</li>)}
                    </ul>
                  ) : (
                    <p className="muted">当前摘要没有记录文件列表。</p>
                  )}
                </section>
              ) : (
                <section className="adminCapabilitySection">
                  <h3>MCP 配置</h3>
                  <dl className="assetDetailMeta">
                    <dt>Transport</dt>
                    <dd>{publicAssetTransportLabel(selectedPublicAssetDetail)}</dd>
                    <dt>Endpoint</dt>
                    <dd>{publicAssetEndpointLabel(selectedPublicAssetDetail)}</dd>
                    <dt>权限</dt>
                    <dd>{String(selectedPublicAssetDetail.manifest?.permissionHint || selectedPublicAssetDetail.manifest?.permission_hint || "未记录")}</dd>
                  </dl>
                </section>
              )}

              <section className="adminCapabilitySection">
                <h3>管理操作</h3>
                <div className="assetDrawerActions">
                  {selectedPublicAssetDetail.assetType === "skill" ? (
                    <>
                      <button
                        type="button"
                        className="assetDrawerAction secondary"
                        onClick={() => {
                          startEditSkillCategory(selectedPublicAssetDetail);
                          setSelectedPublicAssetDetail(null);
                        }}
                        disabled={busyAction !== null}
                      >
                        设置分类
                      </button>
                      <button
                        type="button"
                        className="assetDrawerAction danger"
                        onClick={() => void setPublicAssetExecution(selectedPublicAssetDetail, selectedPublicAssetDetail.marketVisible === false)}
                        disabled={busyAction !== null}
                      >
                        {selectedPublicAssetDetail.marketVisible === false ? "技能发布" : "技能下架"}
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        type="button"
                        className="assetDrawerAction secondary"
                        onClick={() => openMcpVersionUpdateModal(selectedPublicAssetDetail)}
                        disabled={busyAction !== null}
                      >
                        更新配置
                      </button>
                      <button
                        type="button"
                        className="assetDrawerAction danger"
                        onClick={() => void setPublicAssetExecution(selectedPublicAssetDetail, selectedPublicAssetDetail.executionStatus === "force_disabled")}
                        disabled={busyAction !== null}
                      >
                        {selectedPublicAssetDetail.executionStatus === "force_disabled" ? "恢复使用" : "紧急停用"}
                      </button>
                    </>
                  )}
                  {selectedPublicAssetDetail.assetType !== "skill" && <button
                    type="button"
                    className="assetDrawerAction danger assetDrawerAction--permanentDelete"
                    onClick={() => void permanentlyDeletePublicAsset(selectedPublicAssetDetail)}
                    disabled={busyAction !== null}
                  >
                    {busyAction === `public-${selectedPublicAssetDetail.assetType}:${selectedPublicAssetDetail.assetId}:delete`
                      ? "删除中"
                      : "永久删除"}
                  </button>}
                </div>
              </section>

              <details className="adminTechnicalDetails">
                <summary>查看 Manifest / 原始摘要</summary>
                <section>
                  <h3>资产摘要</h3>
                  <pre className="assetDetailJson">{JSON.stringify(selectedPublicAssetDetail.summary ?? {}, null, 2)}</pre>
                </section>
                <section>
                  <h3>Manifest</h3>
                  <pre className="assetDetailJson">{JSON.stringify(selectedPublicAssetDetail.manifest ?? {}, null, 2)}</pre>
                </section>
              </details>
            </section>
          </div>
        )}

        {tab === "experts" && <AdminExpertManagement/>}

        {tab === "application-agent-permissions" && <AdminApplicationAgentPermissions/>}

        {tab === "knowledge-governance" && <AdminKnowledgeGovernance/>}

        {tab === "task-prompts" && <AdminTaskPromptConfig/>}

        {tab === "audit" && (
          <section className="adminPanel" aria-label="审计日志">
            <div className="panelHeader">
              <h2>审计日志</h2>
              <span>{auditLogs.length} logs</span>
            </div>
            <div className="auditList">
              {auditLogs.map((log) => (
                <div className="auditItem" key={log.auditLogId}>
                  <strong>{log.action}</strong>
                  <span>{log.targetType} #{log.targetId}</span>
                  <small>{detailText(log.detail)}</small>
                </div>
              ))}
              {auditLogs.length === 0 && <p className="muted">暂无审计日志</p>}
            </div>
          </section>
        )}
      </section>
    </main>
  );
}
