import type {
  AdminAuditLogSummary,
  AdminModelStatusRequest,
  AdminModelConfigEntryRequest,
  AdminModelConfigResponse,
  AdminModelCatalogPublishResponse,
  AdminModelSummary,
  AdminWeKnoraRerankConfigResponse,
  AdminWeKnoraRerankConfigUpdateRequest,
  AdminModelUpdateResponse,
  AdminPrincipalSummary,
  AdminClusterSummary,
  AdminKubernetesEventSummary,
  AdminRuntimeHealthSnapshot,
  AdminRuntimeKubernetesSnapshot,
  AdminSafeKubernetesSummary,
  AdminRuntimeWorkspaceResourceSummary,
  AdminUsageSummaryResponse,
  AdminPresenceSummaryResponse,
  ApplicationAgentAdminCandidateSummary,
  ApplicationAgentAdminSummary,
  AuthTokenResponse,
  DepartmentTreeNode,
  DirectoryUserSearchItem,
  ExpertReviewSummary,
  ExpertShareScope,
  OrganizationDirectoryNode,
  AdminAssetSummary,
  SkillCategorySummary,
  TaskPromptConfigSaveRequest,
  TaskPromptConfigSaveResponse,
  TaskPromptConfigState
} from "@hermes-cloud/schemas";
import { camelize, decamelize } from "./types";
import {
  AuthRefreshError,
  clearSession,
  getAccessToken,
  isPasswordAuthMode,
  redirectToLogin,
  refreshAccessToken,
  setSession,
  logout as logoutSession
} from "../auth";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api/v1";

type StableAssetSummaryResponse = {
  assetId: number;
  assetType: "skill" | "mcp";
  currentVersionId?: number | null;
  version?: number | null;
  slug: string;
  name: string;
  description?: string | null;
  status: string;
  marketVisible?: boolean;
  executionStatus: "enabled" | "force_disabled";
  visibility: string;
  categoryKey?: string | null;
  transport?: string | null;
  manifest: Record<string, unknown>;
  connectionConfig: Record<string, unknown>;
  toolsSchema?: Record<string, unknown>[] | null;
};

type StableAssetVersionResponse = {
  assetType: "skill" | "mcp";
  assetId: number;
  versionId: number;
  version: number;
  status: string;
  contentDigest: string;
  manifest: Record<string, unknown>;
};

export type AssetRolloutResult = {
  rolloutId: string;
  affectedUserCount: number;
  affectedProfileCount: number;
  status: "waiting" | "updating" | "completed" | "partial_failed" | "failed";
  created: boolean;
};

export type StableMcpPublicationResult = {
  asset: AdminAssetSummary;
  rollout: AssetRolloutResult | null;
};

type StableMcpPublicationResponse = {
  asset: StableAssetSummaryResponse;
  rollout?: AssetRolloutResult | null;
};

function stableAssetToAdminAsset(asset: StableAssetSummaryResponse): AdminAssetSummary {
  return {
    assetId: asset.assetId,
    versionId: asset.currentVersionId ?? null,
    assetType: asset.assetType,
    slug: asset.slug,
    version: String(asset.version ?? ""),
    name: asset.name,
    description: asset.description ?? null,
    status: asset.status,
    marketVisible: asset.marketVisible,
    executionStatus: asset.executionStatus,
    categoryKey: asset.categoryKey ?? null,
    currentVersionId: asset.currentVersionId ?? null,
    storageUri: "",
    manifest: {
      ...asset.manifest,
      ...(asset.transport ? { transport: asset.transport } : {}),
      ...(typeof asset.connectionConfig.url === "string"
        ? { endpoint: asset.connectionConfig.url }
        : {}),
    },
    summary: {
      transport: asset.transport,
      connection_config: asset.connectionConfig,
      tools_schema: asset.toolsSchema,
    },
    createdAt: null,
  };
}

export type AssetRolloutBatchSummary = {
  rolloutId: string;
  assetType: "skill" | "mcp" | "expert";
  assetId: number;
  action: "publish" | "rollback" | "revoke" | "enable" | "delete";
  sourceVersionId?: number | null;
  targetVersionId?: number | null;
  affectedUserCount: number;
  affectedProfileCount: number;
  waitingCount: number;
  updatingCount: number;
  appliedCount: number;
  failedCount: number;
  supersededCount: number;
  revokedCount: number;
  status: string;
  createdAt: string;
  startedAt?: string | null;
  completedAt?: string | null;
};

export type AssetRolloutProfileProgress = {
  targetId: number;
  userId: number;
  runtimeWorkspaceId: number;
  runtimeAgentProfileId: number;
  profileKind: string;
  profileName: string;
  targetRevision: number;
  appliedRevision: number;
  status: string;
  attemptCount: number;
  errorCode?: string | null;
  errorMessage?: string | null;
};

export type AssetRolloutDetail = {
  batch: AssetRolloutBatchSummary;
  totalUsers: number;
  offset: number;
  limit: number;
  users: Array<{
    userId: number;
    userCode?: string | null;
    userName?: string | null;
    status: "success" | "updating" | "partial_failed" | "failed";
    profiles: AssetRolloutProfileProgress[];
  }>;
};

export type KnowledgeGovernanceCache = {
  state: "refreshed" | "hit" | "stale";
  sourceFingerprint?: string | null;
  lastSuccessAt?: string | null;
  lastAttemptAt?: string | null;
};

export type KnowledgeGovernanceAttempt = {
  status: "never" | "succeeded" | "failed";
  attemptedAt?: string | null;
  sourceHash?: string;
  error?: {
    code: string;
    message: string;
  };
};

export type KnowledgeGovernanceNode = {
  id: string;
  type: string;
  name: string;
  properties: Record<string, unknown>;
  sources: string[];
};

export type KnowledgeGovernanceEdge = {
  id: string;
  type: string;
  from: string;
  to: string;
  properties: Record<string, unknown>;
  sources: string[];
};

export type KnowledgeGovernanceSummary = {
  schemaVersion: string;
  generatedAt: string;
  sourceHash: string;
  summary: {
    nodeCount: number;
    edgeCount: number;
    modelCount: number;
    fieldCount: number;
    departmentCount: number;
    permissionCount: number;
    relationshipCount: number;
    metricContractCount: number;
    nodeTypes: Record<string, number>;
    edgeTypes: Record<string, number>;
  };
  validationSummary: {
    errors: number;
    warnings: number;
    info: number;
    status: "passed" | "failed";
  };
  cache: KnowledgeGovernanceCache;
  latestAttempt: KnowledgeGovernanceAttempt;
};

export type KnowledgeGovernanceFinding = {
  level: "ERROR" | "WARN" | "INFO";
  code: string;
  message: string;
  source?: string;
  assetId?: string;
  details?: Record<string, unknown>;
};

export type KnowledgeGovernanceValidation = {
  validation: {
    schemaVersion: string;
    generatedAt: string;
    sourceHash: string;
    summary: KnowledgeGovernanceSummary["validationSummary"];
    findings: KnowledgeGovernanceFinding[];
  };
  cache: KnowledgeGovernanceCache;
  latestAttempt: KnowledgeGovernanceAttempt;
};

export type KnowledgeGovernanceAssetList = {
  items: KnowledgeGovernanceNode[];
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
  cache: KnowledgeGovernanceCache;
  latestAttempt: KnowledgeGovernanceAttempt;
};

export type KnowledgeGovernanceDependency = {
  name: string;
  type: string;
  sources: string[];
  upstream: string[];
  downstream: string[];
  downstreamTwoHops: string[];
};

export type KnowledgeGovernanceAssetDetail = {
  asset: KnowledgeGovernanceNode;
  relationships: KnowledgeGovernanceEdge[];
  dependency?: KnowledgeGovernanceDependency | null;
  cache: KnowledgeGovernanceCache;
  latestAttempt: KnowledgeGovernanceAttempt;
};

export type KnowledgeGovernanceImpact = {
  assetId: string;
  asset: KnowledgeGovernanceDependency;
  affected: KnowledgeGovernanceNode[];
  recommendedActions: string[];
  cache: KnowledgeGovernanceCache;
  latestAttempt: KnowledgeGovernanceAttempt;
};

export type KnowledgeGovernanceMetricRule = {
  id: string;
  severity: "error" | "warning";
  section: string;
  source: string;
  relatedSources: string[];
  appliesToTables: string[];
  messages: string[];
};

export type KnowledgeGovernanceMetricContracts = {
  metricContracts: {
    schemaVersion: number;
    summary: {
      total: number;
      errors: number;
      warnings: number;
      coveredModels: number;
      totalModels: number;
    };
    rules: KnowledgeGovernanceMetricRule[];
    reviewedExemptions: Array<{
      source: string;
      rationale: string;
    }>;
  };
  cache: KnowledgeGovernanceCache;
  latestAttempt: KnowledgeGovernanceAttempt;
};

type RuntimeResourceTarget = number | Pick<
  AdminRuntimeWorkspaceResourceSummary,
  "runtimeWorkspaceId" | "runtimeKind"
>;

function runtimeTarget(target: RuntimeResourceTarget) {
  return typeof target === "number"
    ? { runtimeWorkspaceId: target, runtimeKind: "personal" as const }
    : target;
}

function runtimeKindQuery(target: RuntimeResourceTarget): string {
  return `runtime_kind=${encodeURIComponent(runtimeTarget(target).runtimeKind)}`;
}
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response = await authorizedFetch(path, init);
  if (response.status === 401 && !path.startsWith("/auth/")) {
    clearSession();
    redirectToLogin();
    throw new Error("认证已过期");
  }
  if (!response.ok) {
    const body = await response.text();
    throw new Error(readableError(body) || `Request failed: ${response.status}`);
  }
  return camelize<T>(await response.json());
}

async function requestNoContent(path: string, init?: RequestInit): Promise<void> {
  const response = await authorizedFetch(path, init);
  if (response.status === 401 && !path.startsWith("/auth/")) {
    clearSession();
    redirectToLogin();
    throw new Error("认证已过期");
  }
  if (!response.ok) {
    const body = await response.text();
    throw new Error(readableError(body) || `Request failed: ${response.status}`);
  }
}

function readableError(body: string): string {
  if (!body) {
    return "";
  }
  try {
    const parsed = JSON.parse(body) as {
      code?: unknown;
      detail?: unknown;
      message?: unknown;
      error?: unknown;
    };
    const structuredError = parsed.error && typeof parsed.error === "object"
      ? parsed.error as { message?: unknown }
      : null;
    if (typeof parsed.code === "string") {
      const messages: Record<string, string> = {
        weknora_rerank_config_unavailable: "知识库底座 Rerank 配置暂不可用，请稍后重试",
        weknora_rerank_model_invalid: "请选择一个可用的 WeKnora Rerank 模型"
      };
      if (messages[parsed.code]) return messages[parsed.code];
    }
    const detail = parsed.detail ?? parsed.message ?? structuredError?.message;
    if (typeof detail === "string") {
      const messages: Record<string, string> = {
        skill_already_exists_use_version_update: "该技能已存在，请在对应技能卡片点击“版本更新”。",
        skill_name_mismatch: "更新包名称必须与当前技能一致。",
        asset_not_active: "该技能已不可更新，请刷新列表后重试。",
        duplicate_package: "该技能版本内容已存在。",
        catalog_conflict: "技能版本更新发生冲突，请刷新后重试。",
        invalid_expert_share_department:
          "审核失败：专家分享范围包含无效、已停用或不可勾选的部门，请联系创建者调整后重新提交审核。",
        invalid_expert_share_user:
          "审核失败：专家分享范围包含无效或已停用的人员，请联系创建者调整后重新提交审核。",
        expert_share_scope_required:
          "审核失败：专家缺少分享范围，请联系创建者重新提交审核。",
        expert_capability_scope_not_covered:
          "审核失败：所选私有知识库没有覆盖全部分享人员，请联系创建者调整知识库授权或分享范围后重新提交。",
        expert_market_name_conflict:
          "审核失败：市场专家名称与其他已发布专家重复，请联系创建者改名后重新提交。",
        expert_review_source_missing:
          "审核失败：待审核专家版本已缺失，请刷新列表后重试。",
        expert_review_not_pending:
          "该专家审核已被处理，请刷新列表后查看最新状态。",
        expert_review_not_found: "未找到该专家审核记录，请刷新列表后重试。",
        expert_package_not_found: "未找到该专家，请刷新列表后重试。",
        "invalid model visibility department":
          "模型可见范围包含无效、已停用或不可勾选的部门，请调整后重新发布。",
        "invalid model visibility user":
          "模型可见范围包含无效或已停用的人员，请调整后重新发布。",
        MCP_SLUG_EXISTS: "该 MCP 标识已被活动资产占用；如需修改现有服务，请使用“更新配置”。",
        MCP_VERSION_CONFLICT: "该 MCP 已被其他管理员更新，请刷新列表后重试。",
        MCP_UNAVAILABLE: "该 MCP 已删除或不可用，请刷新列表后重试。",
        MCP_ARCHIVED: "已归档 MCP 不能更新。",
        "task prompt configuration was updated by another administrator":
          "任务提示配置已被其他管理员更新，请刷新后重试。"
      };
      return messages[detail] ?? detail;
    }
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          if (typeof item === "string") return item;
          if (item && typeof item === "object" && "msg" in item) return String((item as { msg: unknown }).msg);
          return JSON.stringify(item);
        })
        .join("；");
    }
  } catch {
    return body;
  }
  return body;
}

async function authorizedFetch(path: string, init?: RequestInit): Promise<Response> {
  const send = () => {
    const token = getAccessToken();
    const headers: HeadersInit = {
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers || {})
    };
    return fetch(`${API_BASE_URL}${path}`, { ...init, credentials: "include", headers });
  };
  let response = await send();
  if (response.status !== 401 || path.startsWith("/auth/") || isPasswordAuthMode()) {
    return response;
  }
  try {
    await refreshAccessToken();
  } catch (reason) {
    if (reason instanceof AuthRefreshError && reason.reauthRequired) redirectToLogin();
    throw reason;
  }
  response = await send();
  return response;
}

async function checkedResponse(path: string, init?: RequestInit): Promise<Response> {
  const response = await authorizedFetch(path, init);
  if (response.status === 401) {
    clearSession();
    redirectToLogin();
    throw new Error("认证已过期");
  }
  if (!response.ok) {
    const body = await response.text();
    throw new Error(readableError(body) || `Request failed: ${response.status}`);
  }
  return response;
}

function jsonBody(payload: unknown): string {
  return JSON.stringify(decamelize(payload));
}

function normalizeKnowledgeGovernanceSummary(
  value: KnowledgeGovernanceSummary
): KnowledgeGovernanceSummary {
  return {
    ...value,
    summary: {
      ...value.summary,
      // `camelize` correctly converts DTO field names, but node type names are
      // data values stored as map keys and must remain the compiler's canonical
      // snake_case identifiers for filtering.
      nodeTypes: Object.fromEntries(
        Object.entries(value.summary.nodeTypes).map(([key, count]) => [
          key.replace(/[A-Z]/g, (char) => `_${char.toLowerCase()}`),
          count
        ])
      )
    }
  };
}

export const adminApi = {
  login: async (username: string, password: string) => {
    const response = await request<AuthTokenResponse>("/auth/admin/login", {
      method: "POST",
      body: jsonBody({ username, password })
    });
    setSession(response);
    return response;
  },
  logout: () => logoutSession(),
  me: () => request<AdminPrincipalSummary>("/admin/me"),
  taskPromptConfig: () => request<TaskPromptConfigState>("/admin/task-prompt-config"),
  saveTaskPromptConfig: (payload: TaskPromptConfigSaveRequest) =>
    request<TaskPromptConfigSaveResponse>("/admin/task-prompt-config", {
      method: "PUT",
      body: jsonBody(payload)
    }),
  applicationAgentAdminCandidates: (query: string, limit = 50) =>
    request<ApplicationAgentAdminCandidateSummary[]>(
      `/admin/application-agent-admins/candidates?q=${encodeURIComponent(query)}&limit=${limit}`
    ),
  applicationAgentAdmins: () =>
    request<ApplicationAgentAdminSummary[]>("/admin/application-agent-admins"),
  replaceApplicationAgentAdmins: (userCodes: string[]) =>
    request<ApplicationAgentAdminSummary[]>("/admin/application-agent-admins", {
      method: "PUT",
      body: jsonBody({ userCodes })
    }),
  expertReviews: (status?: string) =>
    request<ExpertReviewSummary[]>(
      `/admin/experts${status ? `?review_status=${encodeURIComponent(status)}` : ""}`
    ),
  approveExpertReview: (
    reviewId: number,
    payload: { reviewComment?: string | null }
  ) =>
    request<ExpertReviewSummary>(`/admin/experts/reviews/${reviewId}/approve`, {
      method: "POST",
      body: jsonBody(payload)
    }),
  rejectExpertReview: (reviewId: number, reviewComment: string) =>
    request<ExpertReviewSummary>(`/admin/experts/reviews/${reviewId}/reject`, {
      method: "POST",
      body: jsonBody({ reviewComment })
    }),
  unpublishExpertPackage: (expertPackageId: number) =>
    requestNoContent(`/admin/experts/packages/${expertPackageId}/unpublish`, {
      method: "POST"
    }),
  forceDisableExpertPackage: (
    expertPackageId: number,
    reason: string,
    idempotencyKey: string
  ) => request<AssetRolloutResult>(
    `/admin/experts/packages/${expertPackageId}/force-disable`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: jsonBody({ reason })
    }
  ),
  enableExpertPackage: (
    expertPackageId: number,
    reason: string,
    idempotencyKey: string
  ) => request<AssetRolloutResult>(
    `/admin/experts/packages/${expertPackageId}/enable`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: jsonBody({ reason })
    }
  ),
  searchDirectoryUsers: (keyword: string, limit = 50) =>
    request<DirectoryUserSearchItem[]>(
      `/admin/experts/directory/users/search?keyword=${encodeURIComponent(keyword)}&limit=${limit}`
    ),
  provisionDirectoryUsers: (userCodes: string[]) =>
    request<DirectoryUserSearchItem[]>("/admin/experts/directory/users/provision", {
      method: "POST",
      body: JSON.stringify({ user_codes: [...new Set(userCodes)] }),
    }),
  directoryDepartmentTree: () =>
    request<DepartmentTreeNode[]>("/admin/experts/directory/departments/tree"),
  modelVisibilityDirectoryTree: () =>
    request<OrganizationDirectoryNode[]>(
      "/admin/model-config/directory/tree"
    ),
  models: () => request<AdminModelSummary[]>("/admin/models"),
  modelConfig: () => request<AdminModelConfigResponse>("/admin/model-config"),
  setModelStatus: (modelEntryId: number, payload: AdminModelStatusRequest) =>
    request<AdminModelUpdateResponse>(`/admin/models/${modelEntryId}/status`, {
      method: "PUT",
      body: jsonBody(payload)
    }),
  publishModelConfig: (entries: AdminModelConfigEntryRequest[]) =>
    request<AdminModelCatalogPublishResponse>("/admin/model-config", {
      method: "PUT",
      body: jsonBody({ entries })
    }),
  weKnoraRerankConfig: () =>
    request<AdminWeKnoraRerankConfigResponse>(
      "/admin/knowledge-providers/weknora/rerank-config"
    ),
  updateWeKnoraRerankConfig: (payload: AdminWeKnoraRerankConfigUpdateRequest) =>
    request<AdminWeKnoraRerankConfigResponse>(
      "/admin/knowledge-providers/weknora/rerank-config",
      {
        method: "PUT",
        body: jsonBody(payload)
      }
    ),
  setDefaultModel: (modelEntryId: number) =>
    request<AdminModelUpdateResponse>(`/admin/models/${modelEntryId}/default`, { method: "PUT" }),
  runtimeResources: (filters?: { employeeNo?: string; userName?: string; runtimeStatus?: string }) => {
    const params = new URLSearchParams();
    if (filters?.employeeNo) params.set("employee_no", filters.employeeNo);
    if (filters?.userName) params.set("user_name", filters.userName);
    if (filters?.runtimeStatus) params.set("runtime_status", filters.runtimeStatus);
    const query = params.toString();
    return request<AdminRuntimeWorkspaceResourceSummary[]>(`/admin/resources/runtime-workspaces${query ? `?${query}` : ""}`);
  },
  clusterSummary: (signal?: AbortSignal) =>
    request<AdminClusterSummary>("/admin/resources/cluster-summary", { signal }),
  runtimeKubernetes: (target: RuntimeResourceTarget, signal?: AbortSignal) =>
    request<AdminRuntimeKubernetesSnapshot>(
      `/admin/resources/runtime-workspaces/${runtimeTarget(target).runtimeWorkspaceId}`
      + `/kubernetes?${runtimeKindQuery(target)}`,
      { signal }
    ),
  runtimeHealth: (target: RuntimeResourceTarget, signal?: AbortSignal) =>
    request<AdminRuntimeHealthSnapshot>(
      `/admin/resources/runtime-workspaces/${runtimeTarget(target).runtimeWorkspaceId}`
      + `/health?${runtimeKindQuery(target)}`,
      { signal }
    ),
  runtimeKubernetesSnapshots: (signal?: AbortSignal) =>
    request<AdminRuntimeKubernetesSnapshot[]>(
      "/admin/resources/runtime-workspaces/kubernetes-snapshots",
      { signal }
    ),
  runtimeEvents: (target: RuntimeResourceTarget) =>
    request<AdminKubernetesEventSummary[]>(
      `/admin/resources/runtime-workspaces/${runtimeTarget(target).runtimeWorkspaceId}`
      + `/events?${runtimeKindQuery(target)}`
    ),
  runtimeSafeYaml: (target: RuntimeResourceTarget) =>
    request<AdminSafeKubernetesSummary>(
      `/admin/resources/runtime-workspaces/${runtimeTarget(target).runtimeWorkspaceId}`
      + `/kubernetes/yaml-summary?${runtimeKindQuery(target)}`
    ),
  podLogs: async (
    target: RuntimeResourceTarget,
    podName: string,
    options: { container: string; tailLines: number; previous: boolean },
    signal?: AbortSignal
  ) => {
    const params = new URLSearchParams({
      runtime_kind: runtimeTarget(target).runtimeKind,
      container: options.container,
      tail_lines: String(options.tailLines),
      previous: String(options.previous)
    });
    const response = await checkedResponse(
      `/admin/resources/runtime-workspaces/${runtimeTarget(target).runtimeWorkspaceId}`
      + `/pods/${encodeURIComponent(podName)}/logs?${params}`,
      { signal }
    );
    return response.text();
  },
  followPodLogs: (
    target: RuntimeResourceTarget,
    podName: string,
    options: { container: string; tailLines: number },
    signal: AbortSignal
  ) => {
    const params = new URLSearchParams({
      runtime_kind: runtimeTarget(target).runtimeKind,
      container: options.container,
      tail_lines: String(options.tailLines)
    });
    return checkedResponse(
      `/admin/resources/runtime-workspaces/${runtimeTarget(target).runtimeWorkspaceId}`
      + `/pods/${encodeURIComponent(podName)}/logs/stream?${params}`,
      { signal, headers: { Accept: "text/event-stream" } }
    );
  },
  publicSkillCatalog: () =>
    request<StableAssetSummaryResponse[]>("/admin/assets/skills")
      .then((items) => items.map(stableAssetToAdminAsset)),
  uploadPublicSkill: async (payload: {
    file: File;
    categoryKey: string;
    categoryDisplayName?: string | null;
  }): Promise<AdminAssetSummary> => {
    const uploaded = await request<StableAssetSummaryResponse>(
      `/admin/assets/skills?category_key=${encodeURIComponent(payload.categoryKey)}`,
      {
        method: "POST",
        body: (() => {
          const form = new FormData();
          form.append("file", payload.file);
          return form;
        })()
      }
    );
    return stableAssetToAdminAsset(uploaded);
  },
  uploadPublicSkillVersion: async (skillId: number, file: File): Promise<AdminAssetSummary> => {
    const current = await request<StableAssetSummaryResponse[]>("/admin/assets/skills");
    const asset = current.find((item) => item.assetId === skillId);
    const uploaded = await request<StableAssetSummaryResponse>(
      `/admin/assets/skills/${skillId}/versions?category_key=${encodeURIComponent(asset?.categoryKey ?? "other")}`,
      {
        method: "POST",
        body: (() => {
          const form = new FormData();
          form.append("file", file);
          return form;
        })()
      }
    );
    return stableAssetToAdminAsset(uploaded);
  },
  updatePublicSkillCategory: (skillId: number, categoryKey: string) =>
    (async () => {
      const updated = await request<StableAssetSummaryResponse>(
        `/admin/assets/skills/${skillId}/category?category_key=${encodeURIComponent(categoryKey)}`,
        { method: "PATCH" }
      );
      return stableAssetToAdminAsset(updated);
    })(),
  unpublishPublicSkill: (skillId: number, reason: string): Promise<AdminAssetSummary> =>
    request<StableAssetSummaryResponse>(`/admin/assets/skills/${skillId}/unpublish`, {
      method: "POST",
      body: jsonBody({ reason })
    }).then(stableAssetToAdminAsset),
  republishPublicSkill: (skillId: number, reason: string, idempotencyKey: string): Promise<AdminAssetSummary> =>
    request<StableAssetSummaryResponse>(`/admin/assets/skills/${skillId}/publish`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: jsonBody({ reason })
    }).then(stableAssetToAdminAsset),
  forceDisablePublicSkill: (skillId: number, reason: string, idempotencyKey: string): Promise<AssetRolloutResult> =>
    request<AssetRolloutResult>(`/admin/assets/skills/${skillId}/force-disable`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: jsonBody({ reason })
    }),
  enablePublicSkill: (skillId: number, reason: string, idempotencyKey: string): Promise<AssetRolloutResult> =>
    request<AssetRolloutResult>(`/admin/assets/skills/${skillId}/enable`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: jsonBody({ reason })
    }),
  deletePublicSkill: (skillId: number): Promise<AssetRolloutResult> =>
    request<AssetRolloutResult>(`/admin/assets/skills/${skillId}`, {
      method: "DELETE"
    }),
  skillCategories: () => request<SkillCategorySummary[]>("/admin/assets/skill-categories"),
  createSkillCategory: (payload: { categoryKey?: string; displayName: string }) =>
    request<SkillCategorySummary>("/admin/assets/skill-categories", {
      method: "POST",
      body: jsonBody(payload)
    }),
  publicSkillVersions: async (skillId: number): Promise<AdminAssetSummary[]> => {
    const [versions, assets] = await Promise.all([
      request<StableAssetVersionResponse[]>(`/admin/assets/skills/${skillId}/versions`),
      request<StableAssetSummaryResponse[]>("/admin/assets/skills")
    ]);
    const rawAsset = assets.find((item) => item.assetId === skillId);
    const asset = rawAsset ? stableAssetToAdminAsset(rawAsset) : null;
    if (!asset) return [];
    return versions.map((version) => ({
      ...asset,
      versionId: version.versionId,
      version: String(version.version),
      summary: {
        ...asset.summary,
        content_digest: version.contentDigest,
      }
    }));
  },
  selectPublicSkillVersion: async (skillId: number, versionId: number): Promise<AdminAssetSummary> => {
    const updated = await request<StableAssetSummaryResponse>(
      `/admin/assets/skills/${skillId}/versions/${versionId}/activate`,
      { method: "POST" }
    );
    return stableAssetToAdminAsset(updated);
  },
  publicMcps: () => request<StableAssetSummaryResponse[]>("/admin/assets/mcps")
    .then((items) => items.map(stableAssetToAdminAsset)),
  createPublicMcp: (payload: {
    slug: string;
    name: string;
    description?: string | null;
    transport: string;
    endpoint: string;
    config?: Record<string, unknown>;
    permissionHint?: string | null;
  }) => request<StableAssetSummaryResponse>("/admin/assets/mcps", {
    method: "POST",
    body: jsonBody({
      slug: payload.slug,
      name: payload.name,
      description: payload.description,
      visibility: "organization",
      material: {
        transport: payload.transport.replace("-", "_"),
        connectionConfig: { ...payload.config, ...(payload.endpoint ? { url: payload.endpoint } : {}) },
        secretRefs: {},
        toolsSchema: null,
      },
    })
  }).then(stableAssetToAdminAsset),
  updatePublicMcp: async (payload: {
    assetId: number;
    expectedCurrentVersionId: number;
    name: string;
    description?: string | null;
    transport: string;
    endpoint: string;
    config?: Record<string, unknown>;
    changeSummary?: string | null;
  }): Promise<StableMcpPublicationResult> => {
    const response = await request<StableMcpPublicationResponse>(
      `/admin/assets/mcps/${payload.assetId}/versions`,
      {
        method: "POST",
        body: jsonBody({
          expectedCurrentVersionId: payload.expectedCurrentVersionId,
          name: payload.name,
          description: payload.description,
          material: {
            transport: payload.transport.replace("-", "_"),
            connectionConfig: {
              ...payload.config,
              ...(payload.endpoint ? { url: payload.endpoint } : {})
            },
            secretRefs: {},
            toolsSchema: null,
          },
          changeSummary: payload.changeSummary,
        })
      }
    );
    return {
      asset: stableAssetToAdminAsset(response.asset),
      rollout: response.rollout ?? null,
    };
  },
  forceDisablePublicMcp: (assetId: number, reason: string, idempotencyKey: string): Promise<AssetRolloutResult> =>
    request<AssetRolloutResult>(`/admin/assets/mcps/${assetId}/force-disable`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: jsonBody({ reason })
    }),
  enablePublicMcp: (assetId: number, reason: string, idempotencyKey: string): Promise<AssetRolloutResult> =>
    request<AssetRolloutResult>(`/admin/assets/mcps/${assetId}/enable`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: jsonBody({ reason })
    }),
  deletePublicMcp: (assetId: number): Promise<AssetRolloutResult> =>
    request<AssetRolloutResult>(`/admin/assets/mcps/${assetId}`, {
      method: "DELETE"
    }),
  assetRollouts: () => request<AssetRolloutBatchSummary[]>("/admin/asset-rollouts"),
  assetRollout: (rolloutId: string) =>
    request<AssetRolloutDetail>(`/admin/asset-rollouts/${encodeURIComponent(rolloutId)}`),
  retryFailedAssetRollout: (rolloutId: string) =>
    request<{ rolloutId: string; queuedTargetCount: number }>(
      `/admin/asset-rollouts/${encodeURIComponent(rolloutId)}/retry-failed`,
      { method: "POST" }
    ),
  retryFailedAssetRolloutTarget: (rolloutId: string, targetId: number) =>
    request<{ rolloutId: string; queuedTargetCount: number }>(
      `/admin/asset-rollouts/${encodeURIComponent(rolloutId)}/targets/${targetId}/retry`,
      { method: "POST" }
    ),
  usageSummary: () => request<AdminUsageSummaryResponse>("/admin/usage/summary"),
  presenceSummary: () => request<AdminPresenceSummaryResponse>("/admin/usage/presence"),
  auditLogs: (action?: string) => {
    const params = new URLSearchParams({ limit: "20" });
    if (action) {
      params.set("action", action);
    }
    return request<AdminAuditLogSummary[]>(`/admin/audit-logs?${params.toString()}`);
  },
  knowledgeGovernanceSummary: () =>
    request<KnowledgeGovernanceSummary>("/admin/knowledge-governance/summary")
      .then(normalizeKnowledgeGovernanceSummary),
  knowledgeGovernanceValidation: () =>
    request<KnowledgeGovernanceValidation>("/admin/knowledge-governance/validation"),
  knowledgeGovernanceAssets: (filters?: {
    query?: string;
    assetType?: string;
    page?: number;
    pageSize?: number;
  }) => {
    const params = new URLSearchParams();
    if (filters?.query) params.set("q", filters.query);
    if (filters?.assetType) params.set("asset_type", filters.assetType);
    params.set("page", String(filters?.page ?? 1));
    params.set("page_size", String(filters?.pageSize ?? 50));
    return request<KnowledgeGovernanceAssetList>(
      `/admin/knowledge-governance/assets?${params.toString()}`
    );
  },
  knowledgeGovernanceAsset: (assetId: string) =>
    request<KnowledgeGovernanceAssetDetail>(
      `/admin/knowledge-governance/assets/${encodeURIComponent(assetId)}`
    ),
  knowledgeGovernanceImpact: (assetId: string) =>
    request<KnowledgeGovernanceImpact>(
      `/admin/knowledge-governance/impact/${encodeURIComponent(assetId)}`
    ),
  knowledgeGovernanceMetricContracts: () =>
    request<KnowledgeGovernanceMetricContracts>(
      "/admin/knowledge-governance/metric-contracts"
    ),
  refreshKnowledgeGovernance: () =>
    request<KnowledgeGovernanceSummary>("/admin/knowledge-governance/refresh", {
      method: "POST"
    }).then(normalizeKnowledgeGovernanceSummary)
};
