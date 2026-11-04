import type {
  AvatarAssetSummary,
  ApplicationAgentConfig,
  ApplicationAgentAccessSummary,
  ApplicationAgentConfigRevisionSummary,
  ApplicationAgentDetail,
  ApplicationAgentPersonalKnowledgeCatalog,
  ApplicationAgentSummary,
  ApplicationAgentSkillSummary,
  ApplicationClientCreated,
  ApplicationClientSummary,
  ApplicationWeComChannel,
  ApplicationWeComMessageList,
  ApplicationWeComUserList,
  ApplicationAgentWeComBindingResponse,
  ApplicationAgentWeComBindingSummary,
  ApplicationAgentWeComChannelSummary,
  AuthTokenResponse,
  CreateRuntimeSessionRequest,
  DepartmentTreeNode,
  DirectoryUserSearchItem,
  ChatDownloadIntent,
  ChatFileTransferIntent,
  ChatFileSummary,
  ChatUploadResponse,
  OfficePreviewSession,
  CreateExpertRequest,
  ExpertCapabilityCatalog,
  ExpertCapabilitySelection,
  ExpertMarketDetail,
  ExpertMarketItem,
  ExpertShareScope,
  RuntimeAgentProfileSummary,
  UpdateExpertConfigurationRequest,
  UpdateExpertRequest,
  PersonalSkillAssetDetailSummary,
  PersonalSkillInstallAccepted,
  PersonalSkillAssetSummary,
  SkillCategorySummary,
  SystemSkillSummary,
  PersonalKnowledgeBindingResponse,
  PersonalKnowledgeCreate,
  PersonalKnowledgeDetail,
  PersonalKnowledgeFileListResponse,
  PersonalKnowledgeFileSummary,
  PersonalKnowledgeListResponse,
  PersonalKnowledgePermissionReplace,
  PersonalKnowledgePermissionDirectoryNode,
  OrganizationDirectoryNode,
  PersonalKnowledgePermissionResponse,
  PersonalKnowledgeUpdate,
  RuntimeChatRequest,
  RuntimeChatHistoryMessage,
  RuntimeChatHistoryResponse,
  RuntimeChatRunSummary,
  RuntimeRunActionRequest,
  RenameRuntimeSessionRequest,
  RenameChatFileRequest,
  RuntimeModelSummary,
  SessionModelSelectionResponse,
  RuntimeSessionSummary,
  RuntimeStartupContextSummary,
  RuntimeWorkspaceSummary,
  EnterpriseKnowledgeSettings,
  EnterpriseKnowledgeSettingsUpdate,
  CreateScheduledTaskRequest,
  ScheduledTask,
  ScheduledTaskTriggerResponse,
  UpdateScheduledTaskRequest,
  VoiceTranscriptionResponse,
  DefaultProfileAssetView,
  DefaultProfileAssetOperationAccepted,
  UserAssetCatalogView,
  ImChannelListResponse,
  ImQrOnboardingCreated,
  ImQrOnboardingStatus,
  RegisterWeComChannelRequest,
  ImChannelCard,
  TaskPromptConfigState
} from "@hermes-cloud/schemas";
import { camelize, decamelize } from "./types";
import {
  RuntimeEdgeClient,
  type RuntimeEdgeBootstrap,
  type RuntimeEdgeGrantTarget
} from "./runtime-edge-client";

import {
  getAccessToken,
  refreshAccessToken,
  requireReauthentication,
  setSession,
  logout as logoutSession
} from "../auth";

export interface ApplicationAgentAuthorizationCodeResponse {
  code: string;
  expiresIn: number;
  redirectUri?: string | null;
}

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api/v1";
const CLIENT_TIMING_PROJECTION_RETRY_DELAYS_MS = [0, 250, 500] as const;

export interface ChatStreamEvent {
  event: string;
  data: Record<string, unknown>;
}

export interface ApplicationAgentStudioMessage {
  role?: string;
  content?: string;
  [key: string]: unknown;
}

export interface ApplicationAgentStudioResponse {
  responseId: string;
  requestId: string;
  runtimeSessionId: string;
  outputText: string;
  status: "completed";
}

export interface ApplicationAgentStudioHistory {
  runtimeSessionId: string;
  messages: ApplicationAgentStudioMessage[];
}

export interface ChatClientTimingPayload {
  sampleKind?: "lifecycle" | "content";
  requestToFirstEventMs?: number;
  requestToFirstVisibleReceivedMs: number;
  requestToFirstVisibleCommitMs: number;
  requestToFirstVisiblePaintMs: number;
  dispatchToFirstVisibleReceivedMs?: number;
  dispatchToFirstVisibleCommitMs?: number;
  dispatchToFirstVisiblePaintMs?: number;
  queuedWaitMs?: number;
  receiptAgeMs?: number;
  requiredPrewarmWaitMs: number;
  documentVisibility: "visible" | "hidden" | "prerender" | "unknown";
  reconnectBeforeFirstVisible: boolean;
  sendOverlappedPrewarm: boolean;
  frontendBuild?: string;
}

export interface SessionPrewarmState {
  state: "ready" | "skipped-unmaterialized";
  readyReceiptId?: string | null;
  runtimeWorkspaceId?: number;
  runtimeSessionId?: string;
  modelEntryId?: number | null;
  runtimeAgentProfileId?: number | null;
  profileGeneration?: number;
  selectionEpoch?: number;
  issuedAt?: string | null;
  expiresAt?: string | null;
}

export interface ActiveChatRunResponse {
  run: {
    runtimeWorkspaceId: number;
    runtimeSessionId: string;
    runtimeRunId: string;
    status: string;
    lastSeq: number;
    historyMessageCountBeforeRun?: number | null;
    source?: ({
      type: "scheduled";
      scheduledTaskId: number;
      scheduledTaskName: string;
      triggerKind: string;
      taskAvailable: boolean;
    } | {
      type: "expert_collaboration";
      collaborationId: string;
      originRuntimeRunId: string;
    }) | null;
  } | null;
}

export interface RuntimeClarifyAnswer {
  kind: "text" | "single" | "multi" | "skip";
  text?: string | null;
  values?: string[];
}

export interface RuntimeClarifyRequest {
  requestId: string;
  runtimeSessionId: string;
  parentRuntimeRunId: string;
  question: string;
  choices: string[];
  multiSelect: boolean;
  status: string;
  answer?: RuntimeClarifyAnswer | null;
  answerSource?: "user" | "auto" | null;
  autoDefaultAt?: string | null;
  expiresAt?: string | null;
  version: number;
  createdAt: string;
  answeredAt?: string | null;
  canceledAt?: string | null;
  resumeRuntimeRunId?: string | null;
  claimExpiresAt?: string | null;
  resumeAttempts?: number;
  nextResumeAt?: string | null;
  mode?: "web_run" | "expert_worker";
  requestedBy?: string | null;
}

export interface RuntimeRunActivityResponse {
  runtimeWorkspaceId: number;
  runtimeSessionId: string;
  runtimeRunId: string;
  status: string;
  events: ChatStreamEvent[];
}

export interface ExpertBackgroundCollaboration {
  collaborationId: string;
  runtimeWorkspaceId: number;
  runtimeSessionId: string;
  originRuntimeRunId: string;
  finalRuntimeRunId?: string | null;
  state: string;
  revision: number;
  taskCount: number;
  lastErrorCode?: string | null;
  updatedAt: string;
}

export type AppCenterSourceType = "agent_html" | "manual_zip";
export type AppCenterShareMode = "scope" | "link";

export interface AppCenterListItem {
  id: number;
  name: string;
  description: string;
  accessUrl: string;
  sourceType: AppCenterSourceType;
  shareMode: AppCenterShareMode;
  creatorName: string;
  mine: boolean;
  copiedFromAppId?: number | null;
  copiedFromCreatorName?: string | null;
  sourceSessionId?: string | null;
  updatedAt: string;
  scopeSummary: string;
  publishVersionId: string;
  scope?: ExpertShareScope | null;
}

export interface AppCenterCreateRequest {
  name: string;
  description: string;
  htmlContent: string;
  shareMode: AppCenterShareMode;
  idempotencyKey?: string | null;
  scope?: ExpertShareScope | null;
  sourceSessionId?: string | null;
  sourceRunId?: string | null;
}

export interface AppCenterUploadRequest {
  name: string;
  description: string;
  shareMode: AppCenterShareMode;
  idempotencyKey?: string | null;
  scope?: ExpertShareScope | null;
  sourceSessionId?: string | null;
  sourceRunId?: string | null;
  file: File;
}

export interface AppCenterPackageUpdateRequest {
  htmlContent?: string;
  file?: File;
  name?: string;
  description?: string;
  idempotencyKey?: string | null;
}

export interface ExpertBackgroundCollaborationListResponse {
  collaborations: ExpertBackgroundCollaboration[];
  pendingCount: number;
}

const ERROR_MESSAGES: Record<string, string> = {
  "runtime is not ready": "云端运行环境尚未就绪，请稍后重试",
  "runtime_agent_profile_preparing": "云资源准备中，请稍后重试",
  "Office preview is not configured": "Office 预览服务尚未配置",
  "file is too large": "文件大小超过上传限制",
  "file is empty": "不能上传空文件",
  "session title is already in use": "已存在同名会话，请换一个名称",
  "expert_capability_not_visible": "部分专家能力已失效或你无权使用，请刷新能力列表后重新选择",
  "expert_default_profile_unavailable": "默认 Profile 暂不可用，无法读取已安装资产，请稍后重试",
  "expert_default_installed_asset_stale": "所选已安装资产已变更或不可用，请刷新能力列表后重新选择",
  "expert_duplicate_skill_identity": "同一技能不能同时选择市场版本和已安装版本，请保留其中一个后保存",
  "expert_duplicate_mcp_identity": "同一 MCP 不能重复选择，请保留其中一个后保存",
  "expert_duplicate_knowledge_base_identity": "同一知识库不能重复选择，请保留其中一个后保存",
  "expert_name_conflict": "当前工作区已有同名专家。请更换名称，或先删除同名专家后重试",
  "expert_operation_in_progress": "专家正在处理上一个操作，请稍后刷新再试",
  "expert_disabled": "该专家已停用，无法继续配置或召唤",
  "expert_not_runnable": "该专家尚无可执行版本，请等待配置生效后重试",
  "market_expert_is_not_editable": "市场安装的专家不支持修改配置",
  "expert_configuration_unchanged": "未检测到专家配置变更，无需发布新版本",
  "expert_change_summary_required": "发布新版本前，请填写本次版本改动简介",
  "expert_runtime_unavailable": "Runtime 尚未完成启动，请稍后重试",
  "invalid_expert_share_department": "分享范围包含无效、已停用或不可勾选的部门，请重新选择后保存",
  "invalid_expert_share_user": "分享范围包含无效或已停用的人员，请重新选择后保存",
  "market_expert_not_found": "该专家当前不可见或已从市场下架，请刷新专家市场后重试",
  "expert_profile_provision_failed": "专家初始化未完成，请稍后重试；如持续失败可在已安装列表中点击重试",
  "expert_plugin_selection_not_editable": "Plugin 由平台根据知识库自动配置，请刷新后重试",
  "avatar_not_available": "该头像已不可用，请重新选择",
  "avatar_file_empty": "不能上传空图片",
  "avatar_file_too_large": "头像图片不能超过 2 MB",
  "avatar_file_type_not_allowed": "请选择 PNG、JPEG 或 WebP 图片",
  "avatar_file_type_mismatch": "图片格式与文件内容不一致，请换一张后重试",
  "avatar_file_invalid": "图片无法读取，请换一张后重试",
  "avatar_dimensions_invalid": "图片尺寸无效或过大，请换一张后重试",
  "avatar_upload_failed": "头像上传失败，请稍后重试",
  "application_agent_skill_package_empty": "不能上传空技能包",
  "application_agent_skill_package_too_large": "应用 Agent 技能包不能超过 25 MiB，请压缩后重试",
  "application_agent_skill_package_invalid": "请选择包含有效 SKILL.md 的 ZIP 包",
  "application_agent_skill_name_conflict": "当前应用 Agent 已安装同名技能，请先卸载后再上传",
  "application_agent_skill_category_invalid": "所选技能分类不可用，请刷新后重试",
  "application_redirect_uri_insecure": "该 HTTP 回调地址未被平台批准，请使用已登记的内网地址或 HTTPS 地址",
  "personal_skill_package_empty": "不能上传空技能包",
  "personal_skill_package_too_large": "个人 Skill 包不能超过 25 MiB，请压缩后重试",
  "personal_skill_package_too_many_files": "个人 Skill 包文件超过 200 个，请精简后重试",
  "personal_skill_package_single_file_too_large": "个人 Skill 包中存在超过 25 MiB 的单个文件，请拆分或压缩后重试",
  "personal_skill_package_unpacked_too_large": "个人 Skill 包解压后超过 100 MiB，请精简后重试",
  "personal_skill_package_symlink": "技能包不允许包含符号链接",
  "personal_skill_package_encrypted": "技能包不允许包含加密文件",
  "personal_skill_package_missing_manifest": "技能包必须包含 SKILL.md",
  "personal_skill_package_unsafe_path": "技能包包含不安全的文件路径",
  "personal_skill_category_invalid": "技能分类格式无效，请重新选择",
  "personal_skill_category_unavailable": "所选技能分类不存在或已不可用，请刷新后重试",
  "personal_skill_category_name_invalid": "技能分类名称无效，请输入不超过 100 个字符的名称",
  "personal_skill_category_exists": "该技能分类已存在，请换一个名称",
  "personal_skill_package_invalid": "请上传包含有效 SKILL.md 的 ZIP 文件",
  "personal_skill_manifest_invalid_encoding": "SKILL.md 必须使用 UTF-8 编码",
  "personal_skill_manifest_invalid": "SKILL.md 中的技能元数据无效",
  "SKILL_SYSTEM_RESERVED": "该技能名称已被系统内置技能占用，请修改 SKILL.md 中的 name 后重新上传",
  "personal_knowledge_provider_mapping_required": "文件与知识底座的外部映射缺失，请联系管理员对账后重试",
  "personal_knowledge_file_duplicate": "知识库中已存在相同文件，请勿重复上传",
  "personal_knowledge_upload_failed": "文件上传到知识底座失败，请稍后重试",
  "personal_knowledge_delete_failed": "知识底座尚未确认删除，文件已保留，请稍后重试",
  "personal_knowledge_file_delivery_failed": "暂时无法读取文件内容，请稍后重试",
  "personal_knowledge_capability_unavailable": "当前知识库不支持该检索方式，请调整知识库范围",
  "personal_knowledge_wiki_page_not_found": "Wiki 页面不存在或已移除，请重新检索",
  "invalid_permission_department": "所选部门已失效或不可授权，请刷新目录后重新选择",
  "invalid_permission_user": "所选人员已失效或不可授权，请刷新目录后重新选择",
  "分享范围包含不可用部门": "分享范围包含无效或已停用的部门，请重新选择后保存",
  "分享范围包含不可用用户": "分享范围包含无效或已停用的人员，请重新选择后保存",
  "APP_CENTER_PACKAGE_INVALID": "看板包或分享范围无效，请检查后重试",
  "APP_CENTER_OPERATION_FAILED": "看板保存失败，请稍后重试或联系管理员",
  "APP_CENTER_APP_NOT_FOUND": "看板不存在或已删除，请刷新看板区",
  "APP_CENTER_PERMISSION_DENIED": "你没有该看板的操作权限",
  "APP_CENTER_LIMIT_REACHED": "每个用户最多创建 100 个看板，请先清理不需要的看板",
  "APP_CENTER_IDEMPOTENCY_CONFLICT": "看板提交状态已变化，请刷新后重试",
  "APP_CENTER_LIVE_DATA_UNAVAILABLE": "实时数据暂时不可用，请稍后重试",
  "APP_CENTER_LIVE_DATA_RATE_LIMITED": "刷新过于频繁，请稍后再试",
  "MODEL_VISION_UNSUPPORTED": "当前模型不支持图片识别，请切换模型后重试"
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    readonly runCreated?: boolean,
    readonly redirectUri?: string,
    readonly detail?: ApiErrorDetail
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export type ApiErrorDetail = Record<string, never>;

function responseErrorMessage(body: string, fallback: string): string {
  if (!body) return fallback;
  try {
    const detail = JSON.parse(body)?.detail;
    if (typeof detail === "string" && detail) return ERROR_MESSAGES[detail] || detail;
    if (Array.isArray(detail)) {
      return "请求参数与服务版本不一致，请刷新页面或稍后重试";
    }
    if (detail && typeof detail === "object") {
      const message = typeof detail.message === "string" ? detail.message : "";
      const code = typeof detail.code === "string" ? detail.code : "";
      return ERROR_MESSAGES[message] || ERROR_MESSAGES[code] || message || fallback;
    }
  } catch {
    // Non-JSON errors are displayed as returned by the platform.
  }
  return ERROR_MESSAGES[body] || fallback;
}

function responseErrorMetadata(body: string): {
  code?: string;
  runCreated?: boolean;
  redirectUri?: string;
  detail?: ApiErrorDetail;
} {
  try {
    const detail = JSON.parse(body)?.detail;
    if (typeof detail === "string" && detail) {
      return { code: detail };
    }
    if (detail && typeof detail === "object") {
      return {
        code: typeof detail.code === "string"
          ? detail.code
          : undefined,
        runCreated: typeof detail.runCreated === "boolean"
          ? detail.runCreated
          : undefined,
        redirectUri: typeof detail.redirect_uri === "string"
          ? detail.redirect_uri
          : undefined,
        detail: {}
      };
    }
  } catch {
    // Non-JSON errors do not carry structured metadata.
  }
  return {};
}

async function request<T>(
  path: string,
  init?: RequestInit,
  readSuccess?: (response: Response) => T | Promise<T>,
  tooLargeMessage?: string,
): Promise<T> {
  const response = await authorizedFetch(path, init);
  if (!response.ok) {
    const body = await response.text();
    const metadata = responseErrorMetadata(body);
    const fallback = response.status === 413
      ? (tooLargeMessage || "请求内容超过 25 MiB，请压缩文件后重试")
      : `请求失败：${response.status}`;
    throw new ApiError(
      responseErrorMessage(body, fallback),
      response.status,
      metadata.code,
      metadata.runCreated,
      metadata.redirectUri,
      metadata.detail
    );
  }
  if (readSuccess) return readSuccess(response);
  if (response.status === 204) {
    return undefined as T;
  }
  // DELETE /user/application-agents/{id} is intentionally asynchronous and
  // returns 202 Accepted with an empty body. Other 202 endpoints may return a
  // JSON acknowledgement, so only suppress parsing when the body is empty.
  if (response.status === 202) {
    const body = await response.text();
    if (!body.trim()) return undefined as T;
    return camelize<T>(JSON.parse(body));
  }
  return camelize<T>(await response.json());
}

async function authorizedFetch(path: string, init?: RequestInit): Promise<Response> {
  const send = () => {
    const token = getAccessToken();
    const headers: HeadersInit = {
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers || {})
    };
    return fetch(`${API_BASE_URL}${path}`, {
      ...init,
      credentials: "include",
      headers
    });
  };
  let response = await send();
  if (response.status !== 401 || path.startsWith("/auth/")) return response;
  await refreshAccessToken();
  response = await send();
  if (response.status === 401) requireReauthentication();
  return response;
}

function jsonBody(payload: unknown): string {
  return JSON.stringify(decamelize(payload));
}

// `enterprise_knowledge_enabled` was added after the original expert
// configuration contract.  The Backend model defaults it to false, so omit
// the disabled value on the wire to keep ordinary expert saves compatible
// with an older, strict Backend process.  An enabled value is still sent
// explicitly and must be understood by the service.
function expertCapabilitiesBody(capabilities?: ExpertCapabilitySelection) {
  if (!capabilities || capabilities.enterpriseKnowledgeEnabled) return capabilities;
  const { enterpriseKnowledgeEnabled: _disabled, ...legacy } = capabilities;
  return legacy;
}

function scheduledTaskCommandPayload(payload: CreateScheduledTaskRequest | UpdateScheduledTaskRequest): CreateScheduledTaskRequest {
  return {
    taskName: payload.taskName,
    prompt: payload.prompt,
    targetRuntimeSessionId: payload.targetRuntimeSessionId,
    scheduleConfig: payload.scheduleConfig
  };
}

function dispatchSseBlock(block: string, onEvent: (event: ChatStreamEvent) => void) {
  let eventName = "message";
  const dataLines: string[] = [];
  for (const rawLine of block.split(/\r?\n/)) {
    if (!rawLine || rawLine.startsWith(":")) {
      continue;
    }
    const separatorIndex = rawLine.indexOf(":");
    const field = separatorIndex === -1 ? rawLine : rawLine.slice(0, separatorIndex);
    const rawValue = separatorIndex === -1 ? "" : rawLine.slice(separatorIndex + 1);
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;
    if (field === "event") {
      eventName = value || "message";
    } else if (field === "data") {
      dataLines.push(value);
    }
  }
  if (dataLines.length === 0) {
    return;
  }
  const payload = dataLines.join("\n");
  const data = payload === "[DONE]" ? {} : camelize<Record<string, unknown>>(JSON.parse(payload));
  onEvent({ event: eventName, data });
}

async function streamRequest(
  path: string,
  payload: RuntimeChatRequest | RuntimeRunActionRequest,
  onEvent: (event: ChatStreamEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const response = await authorizedFetch(path, {
    method: "POST",
    body: jsonBody(payload),
    signal
  });
  if (!response.ok) {
    const body = await response.text();
    const metadata = responseErrorMetadata(body);
    throw new ApiError(
      responseErrorMessage(
        body,
        `请求失败：${response.status}`
      ),
      response.status,
      metadata.code,
      metadata.runCreated
    );
  }
  await readSseResponse(response, onEvent);
}

async function streamGetRequest(
  path: string,
  onEvent: (event: ChatStreamEvent) => void,
  signal?: AbortSignal,
  headers?: HeadersInit
): Promise<void> {
  const response = await authorizedFetch(path, {
    method: "GET",
    signal,
    headers
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `Request failed: ${response.status}`);
  }
  await readSseResponse(response, onEvent);
}

async function readSseResponse(
  response: Response,
  onEvent: (event: ChatStreamEvent) => void
): Promise<void> {
  if (!response.body) {
    throw new Error("Streaming response body is not available");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() ?? "";
    for (const block of blocks) {
      dispatchSseBlock(block, onEvent);
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) {
    dispatchSseBlock(buffer, onEvent);
  }
}

const runtimeEdgeClient = new RuntimeEdgeClient(
  (target) => request<RuntimeEdgeBootstrap>("/user/runtime-edge/prepare", {
    method: "POST",
    body: jsonBody(target)
  }),
  (target) => request<RuntimeEdgeGrantTarget>("/user/runtime-edge/refresh-grant", {
    method: "POST",
    body: jsonBody(target)
  })
);

const applicationRuntimeEdgeClients = new Map<number, RuntimeEdgeClient>();

function applicationRuntimeEdgeClient(
  applicationAgentId: number
): RuntimeEdgeClient {
  const existing = applicationRuntimeEdgeClients.get(applicationAgentId);
  if (existing) return existing;
  const basePath = `/user/application-agents/${applicationAgentId}/studio/runtime-edge`;
  const client = new RuntimeEdgeClient(
    (target) => request<RuntimeEdgeBootstrap>(`${basePath}/prepare`, {
      method: "POST",
      body: jsonBody(target)
    }),
    (target) => request<RuntimeEdgeGrantTarget>(`${basePath}/refresh-grant`, {
      method: "POST",
      body: jsonBody(target)
    })
  );
  applicationRuntimeEdgeClients.set(applicationAgentId, client);
  return client;
}

export function closeApplicationAgentRuntimeEdge(applicationAgentId: number) {
  const client = applicationRuntimeEdgeClients.get(applicationAgentId);
  client?.close();
  applicationRuntimeEdgeClients.delete(applicationAgentId);
}

export const userApi = {
  streamBrowserPresence: (
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal
  ) => streamGetRequest("/user/browser-presence/stream", onEvent, signal, {
    "X-Hermes-Client-Type": "web"
  }),
  imChannels: () =>
    request<ImChannelListResponse>("/user/im-channels"),
  createWeComQrOnboarding: () =>
    request<ImQrOnboardingCreated>("/user/im-channels/wecom/onboarding/qrcode", {
      method: "POST"
    }),
  weComQrOnboarding: (onboardingId: string) =>
    request<ImQrOnboardingStatus>(
      `/user/im-channels/wecom/onboarding/${encodeURIComponent(onboardingId)}`
    ),
  registerWeComChannel: (payload: RegisterWeComChannelRequest) =>
    request<ImChannelCard>("/user/im-channels/wecom", {
      method: "PUT",
      body: jsonBody(payload)
    }),
  setWeComChannelEnabled: (enabled: boolean) =>
    request<ImChannelCard>("/user/im-channels/wecom/enabled", {
      method: "PATCH",
      body: jsonBody({ enabled })
    }),
  revokeWeComChannel: () =>
    request<ImChannelCard>("/user/im-channels/wecom", {
      method: "DELETE",
      body: jsonBody({ confirm: true })
    }),
  applicationWeComChannel: (applicationAgentId: number) =>
    request<ApplicationWeComChannel>(
      `/user/application-agents/${applicationAgentId}/im-channels/wecom`
    ),
  createApplicationWeComQrOnboarding: (
    applicationAgentId: number
  ) =>
    request<ImQrOnboardingCreated>(
      `/user/application-agents/${applicationAgentId}/im-channels/wecom/onboarding/qrcode`,
      { method: "POST" }
    ),
  applicationWeComQrOnboarding: (
    applicationAgentId: number,
    onboardingId: string
  ) =>
    request<ImQrOnboardingStatus>(
      `/user/application-agents/${applicationAgentId}/im-channels/wecom/onboarding/${encodeURIComponent(onboardingId)}`
    ),
  registerApplicationWeComChannel: (
    applicationAgentId: number,
    payload: RegisterWeComChannelRequest
  ) =>
    request<ApplicationWeComChannel>(
      `/user/application-agents/${applicationAgentId}/im-channels/wecom`,
      { method: "PUT", body: jsonBody(payload) }
    ),
  updateApplicationWeComSettings: (
    applicationAgentId: number,
    payload: { directEnabled: boolean; groupEnabled: boolean }
  ) =>
    request<ApplicationWeComChannel>(
      `/user/application-agents/${applicationAgentId}/im-channels/wecom/settings`,
      { method: "PATCH", body: jsonBody(payload) }
    ),
  setApplicationWeComEnabled: (
    applicationAgentId: number,
    enabled: boolean
  ) =>
    request<ApplicationWeComChannel>(
      `/user/application-agents/${applicationAgentId}/im-channels/wecom/${enabled ? "enable" : "disable"}`,
      { method: "POST" }
    ),
  reconnectApplicationWeComChannel: (
    applicationAgentId: number
  ) =>
    request<ApplicationWeComChannel>(
      `/user/application-agents/${applicationAgentId}/im-channels/wecom/reconnect`,
      { method: "POST" }
    ),
  rotateApplicationWeComChannel: (
    applicationAgentId: number
  ) =>
    request<ApplicationWeComChannel>(
      `/user/application-agents/${applicationAgentId}/im-channels/wecom/rotate`,
      { method: "POST" }
    ),
  applicationWeComUsers: (
    applicationAgentId: number,
    query = "",
    limit = 100
  ) =>
    request<ApplicationWeComUserList>(
      `/user/application-agents/${applicationAgentId}/im-channels/wecom/users?q=${encodeURIComponent(query)}&limit=${limit}`
    ),
  applicationWeComMessages: (
    applicationAgentId: number,
    limit = 100
  ) =>
    request<ApplicationWeComMessageList>(
      `/user/application-agents/${applicationAgentId}/im-channels/wecom/messages?limit=${limit}`
    ),
  revokeOrganizationWeComIdentity: (identityId: number) =>
    request<void>(`/user/wecom-identities/${identityId}/revoke`, {
      method: "POST"
    }),
  bindQwToken: (qwToken: string) =>
    request<{ status: string; qwToken: string }>("/user/wecom-bind", {
      method: "POST",
      body: jsonBody({ qw_token: qwToken })
    }),
  login: async (identifier: string, password: string) => {
    const response = await request<AuthTokenResponse>("/auth/login", {
      method: "POST",
      body: jsonBody({ identifier, password })
    });
    setSession(response);
    return response;
  },
  logout: () => logoutSession(),
  applicationAgentAccess: () =>
    request<ApplicationAgentAccessSummary>("/user/application-agents/access"),
  applicationAgents: () =>
    request<ApplicationAgentSummary[]>("/user/application-agents"),
  applicationAgent: (applicationAgentId: number) =>
    request<ApplicationAgentDetail>(`/user/application-agents/${applicationAgentId}`),
  applicationAgentPersonalKnowledge: (applicationAgentId: number) =>
    request<ApplicationAgentPersonalKnowledgeCatalog>(
      `/user/application-agents/${applicationAgentId}/personal-knowledge`
    ),
  selectApplicationAgentPersonalKnowledge: (
    applicationAgentId: number,
    knowledgeBaseId: number
  ) =>
    request<ApplicationAgentPersonalKnowledgeCatalog>(
      `/user/application-agents/${applicationAgentId}/personal-knowledge/${knowledgeBaseId}`,
      { method: "PUT" }
    ),
  disableApplicationAgentPersonalKnowledge: (
    applicationAgentId: number,
    knowledgeBaseId: number
  ) =>
    request<ApplicationAgentPersonalKnowledgeCatalog>(
      `/user/application-agents/${applicationAgentId}/personal-knowledge/${knowledgeBaseId}`,
      { method: "DELETE" }
    ),
  applicationAgentSkills: (applicationAgentId: number) =>
    request<ApplicationAgentSkillSummary[]>(
      `/user/application-agents/${applicationAgentId}/skills`
    ),
  uploadApplicationAgentSkill: async (
    applicationAgentId: number,
    file: File,
    categoryKey: string,
    description?: string
  ) => {
    const body = new FormData();
    body.append("file", file);
    body.append("category_key", categoryKey);
    if (description !== undefined) body.append("description", description);
    const response = await authorizedFetch(
      `/user/application-agents/${applicationAgentId}/skills/upload`,
      { method: "POST", body }
    );
    if (!response.ok) {
      const fallback = response.status === 413
        ? "应用 Agent 技能包超过 25 MiB，请压缩后重试"
        : "应用 Agent 技能上传失败";
      throw new ApiError(
        responseErrorMessage(
          await response.text(),
          fallback
        ),
        response.status
      );
    }
    return camelize<ApplicationAgentSkillSummary>(
      await response.json()
    );
  },
  updateApplicationAgentSkillDescription: (
    applicationAgentId: number,
    skillId: number,
    description: string | null,
    name?: string,
  ) =>
    request<ApplicationAgentSkillSummary>(
      `/user/application-agents/${applicationAgentId}/skills/${skillId}`,
      { method: "PUT", body: jsonBody({ description, name }) },
    ),
  deleteApplicationAgentSkill: (
    applicationAgentId: number,
    skillId: number
  ) =>
    request<void>(
      `/user/application-agents/${applicationAgentId}/skills/${skillId}`,
      { method: "DELETE" }
    ),
  deleteApplicationAgent: (applicationAgentId: number) =>
    request<void>(`/user/application-agents/${applicationAgentId}`, {
      method: "DELETE"
    }),
  createApplicationAgent: (payload: {
    slug: string;
    name: string;
    avatarUrl?: string | null;
    config: ApplicationAgentConfig;
  }) =>
    request<ApplicationAgentDetail>("/user/application-agents", {
      method: "POST",
      body: jsonBody(payload)
    }),
  updateApplicationAgent: (
    applicationAgentId: number,
    payload: {
      name: string;
      avatarUrl?: string | null;
      config: ApplicationAgentConfig;
      expectedConfigGeneration?: number | null;
    }
  ) =>
    request<ApplicationAgentDetail>(`/user/application-agents/${applicationAgentId}`, {
      method: "PUT",
      body: jsonBody(payload)
    }),
  applicationAgentRevisions: (applicationAgentId: number) =>
    request<ApplicationAgentConfigRevisionSummary[]>(
      `/user/application-agents/${applicationAgentId}/revisions`
    ),
  applicationAgentClients: (applicationAgentId: number) =>
    request<ApplicationClientSummary[]>(`/user/application-agents/${applicationAgentId}/clients`),
  authorizeApplicationAgent: (payload: {
    applicationAgent: string;
    clientId: string;
    redirectUri: string;
  }) =>
    request<ApplicationAgentAuthorizationCodeResponse>(
      "/user/application-agents/authorizations",
      {
        method: "POST",
        body: jsonBody(payload)
      }
    ),
  createApplicationAgentClient: (
    applicationAgentId: number,
    payload: {
      name: string;
      allowUserDelegation: boolean;
      allowServiceInvocation: boolean;
      scopes: string[];
      allowedRedirectUris: string[];
    }
  ) =>
    request<ApplicationClientCreated>(`/user/application-agents/${applicationAgentId}/clients`, {
      method: "POST",
      body: jsonBody(payload)
    }),
  updateApplicationAgentClient: (
    applicationAgentId: number,
    applicationClientId: number,
    payload: {
      name: string;
      allowUserDelegation: boolean;
      allowServiceInvocation: boolean;
      allowedRedirectUris: string[];
    }
  ) =>
    request<ApplicationClientSummary>(
      `/user/application-agents/${applicationAgentId}/clients/${applicationClientId}`,
      {
        method: "PUT",
        body: jsonBody(payload)
      }
    ),
  deleteApplicationAgentClient: (
    applicationAgentId: number,
    applicationClientId: number
  ) =>
    request<void>(
      `/user/application-agents/${applicationAgentId}/clients/${applicationClientId}`,
      { method: "DELETE" }
    ),
  applicationAgentWeComChannel: (applicationAgentId: number) =>
    request<ApplicationAgentWeComChannelSummary | null>(
      `/user/application-agents/${applicationAgentId}/wecom-channel`
    ),
  updateApplicationAgentWeComChannel: (
    applicationAgentId: number,
    payload: {
      name: string;
      corpId: string;
      wecomAgentId: number;
      callbackToken?: string | null;
      encodingAesKey?: string | null;
      agentSecret?: string | null;
      enabled: boolean;
    }
  ) =>
    request<ApplicationAgentWeComChannelSummary>(
      `/user/application-agents/${applicationAgentId}/wecom-channel`,
      {
        method: "PUT",
        body: jsonBody(payload)
      }
    ),
  rotateApplicationAgentWeComEntry: (applicationAgentId: number) =>
    request<ApplicationAgentWeComChannelSummary>(
      `/user/application-agents/${applicationAgentId}/wecom-channel/rotate-entry`,
      { method: "POST" }
    ),
  applicationAgentWeComBindings: (applicationAgentId: number) =>
    request<ApplicationAgentWeComBindingSummary[]>(
      `/user/application-agents/${applicationAgentId}/wecom-bindings`
    ),
  revokeApplicationAgentWeComBinding: (
    applicationAgentId: number,
    bindingId: number
  ) =>
    request<void>(
      `/user/application-agents/${applicationAgentId}/wecom-bindings/${bindingId}`,
      { method: "DELETE" }
    ),
  bindApplicationAgentWeComMember: (ticket: string) =>
    request<ApplicationAgentWeComBindingResponse>(
      "/user/application-agent-wecom/bind",
      {
        method: "POST",
        body: jsonBody({ ticket })
      }
    ),
  publishApplicationAgent: (applicationAgentId: number) =>
    request<ApplicationAgentDetail>(`/user/application-agents/${applicationAgentId}/publish`, {
      method: "POST"
    }),
  disableApplicationAgent: (applicationAgentId: number) =>
    request<ApplicationAgentDetail>(`/user/application-agents/${applicationAgentId}/disable`, {
      method: "POST"
    }),
  resumeApplicationAgent: (applicationAgentId: number) =>
    request<ApplicationAgentDetail>(`/user/application-agents/${applicationAgentId}/resume`, {
      method: "POST"
    }),
  prewarmApplicationAgentRuntimeEdge: async (
    applicationAgentId: number,
    runtimeWorkspaceId: number,
    runtimeSessionId: string
  ): Promise<SessionPrewarmState> => {
    const grant = await applicationRuntimeEdgeClient(
      applicationAgentId
    ).prepare(runtimeWorkspaceId, runtimeSessionId);
    return {
      state: "ready",
      runtimeWorkspaceId,
      runtimeSessionId,
      modelEntryId: grant.modelEntryId,
      readyReceiptId: null,
      issuedAt: new Date().toISOString(),
      expiresAt: grant.sessionGrantExpiresAt
    };
  },
  streamApplicationAgentRuntimeEdge: (
    applicationAgentId: number,
    payload: RuntimeChatRequest,
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal
  ) => applicationRuntimeEdgeClient(applicationAgentId).streamRun(
    payload,
    onEvent,
    signal
  ),
  submitApplicationAgentRuntimeEdgeCorrection: (
    applicationAgentId: number,
    payload: RuntimeChatRequest,
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal
  ) => applicationRuntimeEdgeClient(applicationAgentId).submitCorrection(
    payload,
    onEvent,
    signal
  ),
  reconnectApplicationAgentRuntimeEdge: (
    applicationAgentId: number,
    payload: RuntimeRunActionRequest,
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal
  ) => applicationRuntimeEdgeClient(applicationAgentId).resumeRun(
    payload,
    onEvent,
    signal
  ),
  stopApplicationAgentRuntimeEdge: (
    applicationAgentId: number,
    payload: RuntimeRunActionRequest
  ) => applicationRuntimeEdgeClient(applicationAgentId).cancelRun(payload),
  applicationAgentRuntimeEdgeHistory: (
    applicationAgentId: number,
    runtimeWorkspaceId: number,
    runtimeSessionId: string
  ) => applicationRuntimeEdgeClient(applicationAgentId).history(
    runtimeWorkspaceId,
    runtimeSessionId
  ),
  applicationAgentRuntimeEdgeQueueSummary: (
    applicationAgentId: number,
    runtimeWorkspaceId: number,
    runtimeSessionId: string
  ) => applicationRuntimeEdgeClient(applicationAgentId).queueSummary(
    runtimeWorkspaceId,
    runtimeSessionId
  ),
  applicationAgentStudioHistory: (
    applicationAgentId: number,
    runtimeSessionId: string
  ) =>
    request<ApplicationAgentStudioHistory>(
      `/user/application-agents/${applicationAgentId}`
      + `/studio/sessions/${encodeURIComponent(runtimeSessionId)}/history`
    ),
  applicationAgentStudioWorkspace: (applicationAgentId: number) =>
    request<RuntimeWorkspaceSummary>(
      `/user/application-agents/${applicationAgentId}/studio/workspace`
    ),
  warmupApplicationAgentStudio: (applicationAgentId: number) =>
    request<RuntimeWorkspaceSummary>(
      `/user/application-agents/${applicationAgentId}/studio/warmup`,
      { method: "POST" }
    ),
  keepaliveApplicationAgentStudio: (applicationAgentId: number) =>
    request<RuntimeWorkspaceSummary>(
      `/user/application-agents/${applicationAgentId}/studio/keepalive`,
      { method: "POST" }
    ),
  applicationAgentStudioSessions: (applicationAgentId: number) =>
    request<RuntimeSessionSummary[]>(
      `/user/application-agents/${applicationAgentId}/studio/sessions`
    ),
  createApplicationAgentStudioSession: (
    applicationAgentId: number,
    payload: CreateRuntimeSessionRequest
  ) =>
    request<RuntimeSessionSummary>(
      `/user/application-agents/${applicationAgentId}/studio/sessions`,
      { method: "POST", body: jsonBody(payload) }
    ),
  renameApplicationAgentStudioSession: (
    applicationAgentId: number,
    runtimeSessionId: string,
    payload: RenameRuntimeSessionRequest
  ) =>
    request<RuntimeSessionSummary>(
      `/user/application-agents/${applicationAgentId}/studio/sessions/`
      + encodeURIComponent(runtimeSessionId),
      { method: "PATCH", body: jsonBody(payload) }
    ),
  deleteApplicationAgentStudioSession: (
    applicationAgentId: number,
    runtimeSessionId: string
  ) =>
    request<void>(
      `/user/application-agents/${applicationAgentId}/studio/sessions/`
      + encodeURIComponent(runtimeSessionId),
      { method: "DELETE" }
    ),
  prewarmApplicationAgentStudioSession: (
    applicationAgentId: number,
    runtimeSessionId: string
  ) =>
    request<void>(
      `/user/application-agents/${applicationAgentId}/studio/sessions/`
      + `${encodeURIComponent(runtimeSessionId)}/prewarm`,
      { method: "POST" }
    ),
  // ── 应用Agent调试页面专用文件API ──────────────────────────────────────────
  applicationAgentChatFiles: (applicationAgentId: number, runtimeSessionId?: string) => {
    const params = new URLSearchParams();
    if (runtimeSessionId) params.set("runtime_session_id", runtimeSessionId);
    const query = params.toString();
    return request<ChatFileSummary[]>(
      `/user/application-agents/${applicationAgentId}/chat-files${query ? `?${query}` : ""}`
    );
  },
  applicationAgentChatFileMetadata: (applicationAgentId: number, fileId: number) =>
    request<ChatFileSummary>(
      `/user/application-agents/${applicationAgentId}/chat-files/${fileId}/download`
    ),
  applicationAgentDownloadChatFile: async (applicationAgentId: number, fileId: number) => {
    const intent = await request<ChatDownloadIntent>(
      `/user/application-agents/${applicationAgentId}/chat-files/${fileId}/download-intent`,
      { method: "POST" }
    );
    const response = await fetch(intent.runtimePath, {
      credentials: "include",
      headers: {
        Authorization: `Bearer ${intent.fileTransferTicket}`
      }
    });
    if (!response.ok) throw new Error("文件下载失败");
    return response.blob();
  },
  applicationAgentDownloadWecomAttachment: async (
    applicationAgentId: number,
    attachmentId: number
  ) => {
    const intent = await request<ChatDownloadIntent>(
      `/user/application-agents/${applicationAgentId}`
      + `/chat-files/wecom-attachments/${attachmentId}/download-intent`,
      { method: "POST" }
    );
    const response = await fetch(intent.runtimePath, {
      credentials: "include",
      headers: {
        Authorization: `Bearer ${intent.fileTransferTicket}`
      }
    });
    if (!response.ok) throw new Error("图片下载失败");
    return response.blob();
  },
  applicationAgentRenameChatFile: (applicationAgentId: number, fileId: number, payload: RenameChatFileRequest) =>
    request<ChatFileSummary>(
      `/user/application-agents/${applicationAgentId}/chat-files/${fileId}`,
      { method: "PATCH", body: jsonBody(payload) }
    ),
  applicationAgentDeleteChatFile: (applicationAgentId: number, fileId: number) =>
    request<ChatFileSummary>(
      `/user/application-agents/${applicationAgentId}/chat-files/${fileId}`,
      { method: "DELETE" }
    ),
  applicationAgentUploadChatAttachment: (
    applicationAgentId: number,
    file: File,
    runtimeSessionId: string,
    onProgress?: (percent: number) => void
  ): Promise<ChatUploadResponse> => request<ChatFileTransferIntent>(
    `/user/application-agents/${applicationAgentId}/chat-files/upload-intent`,
    {
      method: "POST",
      body: jsonBody({
        runtime_session_id: runtimeSessionId,
        display_name: file.name,
        mime_type: file.type || null,
        size_bytes: file.size
      })
    }
  ).then((intent) => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", intent.runtimePath);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.setRequestHeader(
      "Authorization",
      `Bearer ${intent.fileTransferTicket}`
    );
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100));
    });
    xhr.addEventListener("load", () => {
      if (xhr.status === 401) {
        reject(new Error("附件上传授权已过期，请重试"));
        return;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(responseErrorMessage(xhr.responseText, `附件上传失败：${xhr.status}`)));
        return;
      }
      void (async () => {
        for (let attempt = 0; attempt < 20; attempt += 1) {
          try {
            const readyFile = await request<ChatFileSummary>(
              `/user/application-agents/${applicationAgentId}/chat-files/${intent.file.fileId}/download`
            );
            resolve({ file: readyFile });
            return;
          } catch {
            await new Promise((wait) => window.setTimeout(wait, 250));
          }
        }
        reject(new Error("附件已上传，正在等待平台确认，请稍后重试"));
      })();
    });
    xhr.addEventListener("error", () => reject(new Error("附件上传网络连接失败")));
    xhr.send(file);
  })),
  runtimeWorkspace: () => request<RuntimeWorkspaceSummary>("/user/runtime-workspace"),
  avatarAssets: () => request<AvatarAssetSummary[]>("/user/avatar-assets"),
  uploadAvatarAsset: (file: File) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return request<AvatarAssetSummary>("/user/avatar-assets", {
      method: "POST",
      body: form
    });
  },
  enterpriseKnowledgeSettings: (runtimeWorkspaceId: number) =>
    request<EnterpriseKnowledgeSettings>(`/user/runtime-workspace/${runtimeWorkspaceId}/enterprise-knowledge`),
  updateEnterpriseKnowledgeSettings: (runtimeWorkspaceId: number, payload: EnterpriseKnowledgeSettingsUpdate) =>
    request<EnterpriseKnowledgeSettings>(`/user/runtime-workspace/${runtimeWorkspaceId}/enterprise-knowledge`, {
      method: "PUT",
      body: jsonBody(payload)
    }),
  listPersonalKnowledge: (
    runtimeWorkspaceId: number | null,
    ownership = "all",
    keyword = "",
    runtimeAgentProfileId?: number | null
  ) => {
    const params = new URLSearchParams({ ownership });
    if (runtimeWorkspaceId) params.set("runtime_workspace_id", String(runtimeWorkspaceId));
    if (runtimeAgentProfileId) params.set("runtime_agent_profile_id", String(runtimeAgentProfileId));
    if (keyword.trim()) params.set("keyword", keyword.trim());
    return request<PersonalKnowledgeListResponse>(`/user/personal-knowledge?${params.toString()}`);
  },
  createPersonalKnowledge: (payload: PersonalKnowledgeCreate) =>
    request<PersonalKnowledgeDetail>("/user/personal-knowledge", {
      method: "POST",
      body: jsonBody(payload)
    }),
  personalKnowledgeDetail: (knowledgeBaseId: number) =>
    request<PersonalKnowledgeDetail>(`/user/personal-knowledge/${knowledgeBaseId}`),
  updatePersonalKnowledge: (knowledgeBaseId: number, payload: PersonalKnowledgeUpdate) =>
    request<PersonalKnowledgeDetail>(`/user/personal-knowledge/${knowledgeBaseId}`, {
      method: "PUT",
      body: jsonBody(payload)
    }),
  deletePersonalKnowledge: (knowledgeBaseId: number) =>
    request<void>(`/user/personal-knowledge/${knowledgeBaseId}`, { method: "DELETE" }),
  personalKnowledgePermissions: (knowledgeBaseId: number) =>
    request<PersonalKnowledgePermissionResponse>(`/user/personal-knowledge/${knowledgeBaseId}/permissions`),
  replacePersonalKnowledgePermissions: (knowledgeBaseId: number, payload: PersonalKnowledgePermissionReplace) =>
    request<PersonalKnowledgePermissionResponse>(`/user/personal-knowledge/${knowledgeBaseId}/permissions`, {
      method: "PUT",
      body: jsonBody(payload)
    }),
  personalKnowledgePermissionDirectory: (knowledgeBaseId: number, keyword = "") => {
    const params = new URLSearchParams();
    if (keyword.trim()) params.set("keyword", keyword.trim());
    const query = params.toString();
    return request<PersonalKnowledgePermissionDirectoryNode[]>(
      `/user/personal-knowledge/${knowledgeBaseId}/permission-directory${query ? `?${query}` : ""}`
    );
  },
  setPersonalKnowledgeEnabled: (
    runtimeWorkspaceId: number,
    knowledgeBaseId: number,
    enabled: boolean,
    runtimeAgentProfileId?: number | null
  ) =>
    request<PersonalKnowledgeBindingResponse>(
      `/user/runtime-workspace/${runtimeWorkspaceId}/personal-knowledge/${knowledgeBaseId}`,
      { method: "PUT", body: jsonBody({ enabled, runtimeAgentProfileId }) }
    ),
  personalKnowledgeFiles: (knowledgeBaseId: number) =>
    request<PersonalKnowledgeFileListResponse>(`/user/personal-knowledge/${knowledgeBaseId}/files`),
  uploadPersonalKnowledgeFile: (knowledgeBaseId: number, file: File) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return request<PersonalKnowledgeFileSummary>(`/user/personal-knowledge/${knowledgeBaseId}/files`, {
      method: "POST",
      body: form
    });
  },
  renamePersonalKnowledgeFile: (knowledgeBaseId: number, fileId: number, filename: string) =>
    request<PersonalKnowledgeFileSummary>(`/user/personal-knowledge/${knowledgeBaseId}/files/${fileId}`, {
      method: "PUT",
      body: jsonBody({ filename })
    }),
  deletePersonalKnowledgeFile: (knowledgeBaseId: number, fileId: number) =>
    request<void>(`/user/personal-knowledge/${knowledgeBaseId}/files/${fileId}`, { method: "DELETE" }),
  personalKnowledgeFileContent: async (knowledgeBaseId: number, fileId: number, download: boolean) => {
    const action = download ? "download" : "preview";
    const response = await authorizedFetch(`/user/personal-knowledge/${knowledgeBaseId}/files/${fileId}/${action}`);
    if (!response.ok) throw new Error(download ? "文件下载失败" : "文件预览失败");
    return response.blob();
  },
  personalKnowledgeOfficePreviewSession: (knowledgeBaseId: number, fileId: number) =>
    request<OfficePreviewSession>(
      `/user/personal-knowledge/${knowledgeBaseId}/files/${fileId}/office-preview`,
      { method: "POST" }
    ),
  searchDirectoryUsers: (keyword: string, limit = 50) => {
    const params = new URLSearchParams({ keyword: keyword.trim(), limit: String(limit) });
    return request<DirectoryUserSearchItem[]>(`/user/directory/users/search?${params.toString()}`);
  },
  provisionDirectoryUsers: (userCodes: string[]) =>
    request<DirectoryUserSearchItem[]>("/user/directory/users/provision", {
      method: "POST",
      body: JSON.stringify({ user_codes: [...new Set(userCodes)] }),
    }),
  directoryDepartmentTree: () =>
    request<DepartmentTreeNode[]>("/user/directory/departments/tree"),
  organizationDirectoryTree: (keyword = "") => {
    const params = new URLSearchParams();
    if (keyword.trim()) params.set("keyword", keyword.trim());
    const query = params.toString();
    return request<OrganizationDirectoryNode[]>(`/user/directory/tree${query ? `?${query}` : ""}`);
  },
  listApps: () => request<AppCenterListItem[]>("/user/apps"),
  taskPromptConfig: () => request<TaskPromptConfigState>("/user/task-prompt-config"),
  createApp: (payload: AppCenterCreateRequest) =>
    request<AppCenterListItem>("/user/apps", {
      method: "POST",
      body: jsonBody(payload)
    }),
  uploadApp: async (payload: AppCenterUploadRequest) => {
    const body = new FormData();
    body.append(
      "metadata",
      JSON.stringify(decamelize({
        name: payload.name,
        description: payload.description,
        shareMode: payload.shareMode,
        idempotencyKey: payload.idempotencyKey ?? null,
        scope: payload.scope ?? null,
        sourceSessionId: payload.sourceSessionId ?? null,
        sourceRunId: payload.sourceRunId ?? null
      }))
    );
    body.append("file", payload.file);
    const response = await authorizedFetch("/user/apps/upload", { method: "POST", body });
    if (!response.ok) {
      throw new ApiError(
        responseErrorMessage(await response.text(), "看板上传失败，请稍后重试"),
        response.status
      );
    }
    return camelize<AppCenterListItem>(await response.json());
  },
  copyApp: (appId: number) =>
    request<AppCenterListItem>(`/user/apps/${encodeURIComponent(appId)}/copy`, {
      method: "POST"
    }),
  updateAppPermissions: (appId: number, scope: ExpertShareScope) =>
    request<AppCenterListItem>(`/user/apps/${encodeURIComponent(appId)}/permissions`, {
      method: "PUT",
      body: jsonBody({ scope })
    }),
  updateAppPackage: async (appId: number, payload: AppCenterPackageUpdateRequest) => {
    const path = `/user/apps/${encodeURIComponent(appId)}/package`;
    if (payload.htmlContent !== undefined) {
      return request<AppCenterListItem>(path, {
        method: "PUT",
        body: jsonBody({
          htmlContent: payload.htmlContent,
          name: payload.name ?? null,
          description: payload.description ?? null,
          idempotencyKey: payload.idempotencyKey ?? null
        })
      });
    }
    if (!payload.file) throw new Error("请选择新的看板包");
    const body = new FormData();
    body.append("file", payload.file);
    const response = await authorizedFetch(path, { method: "PUT", body });
    if (!response.ok) {
      const errorBody = await response.text();
      throw new ApiError(
        responseErrorMessage(errorBody, "看板更新失败，请稍后重试"),
        response.status,
        responseErrorMetadata(errorBody).code
      );
    }
    return camelize<AppCenterListItem>(await response.json());
  },
  downloadAppPackage: async (appId: number) => {
    const response = await authorizedFetch(`/user/apps/${encodeURIComponent(appId)}/package`);
    if (!response.ok) {
      throw new ApiError(
        responseErrorMessage(await response.text(), "看板包下载失败，请稍后重试"),
        response.status
      );
    }
    return response.blob();
  },
  deleteApp: (appId: number) =>
    request<void>(`/user/apps/${encodeURIComponent(appId)}`, { method: "DELETE" }),
  bootstrapRuntime: () => request<RuntimeWorkspaceSummary>("/user/runtime-workspace/bootstrap", { method: "POST" }),
  runtimeStartupContext: () => request<RuntimeStartupContextSummary>("/user/runtime-workspace/startup-context"),
  keepaliveRuntime: () => request<void>("/user/runtime-workspace/keepalive", { method: "POST" }),
  prewarmRuntimeSession: async (
    runtimeWorkspaceId: number,
    runtimeSessionId: string
  ): Promise<SessionPrewarmState> => {
    const grant = await runtimeEdgeClient.prepare(
      runtimeWorkspaceId,
      runtimeSessionId
    );
    return {
      state: "ready",
      runtimeWorkspaceId,
      runtimeSessionId,
      modelEntryId: grant.modelEntryId,
      readyReceiptId: null,
      issuedAt: new Date().toISOString(),
      expiresAt: grant.sessionGrantExpiresAt
    };
  },
  warmupRuntime: () => request<RuntimeWorkspaceSummary>("/user/runtime-workspace/warmup", { method: "POST" }),
  runtimeModels: () => request<RuntimeModelSummary[]>("/user/runtime-workspace/models"),
  setSessionModel: (runtimeWorkspaceId: number, runtimeSessionId: string, modelEntryId: number) =>
    request<SessionModelSelectionResponse>(
      `/user/runtime-workspace/${runtimeWorkspaceId}/sessions/${encodeURIComponent(runtimeSessionId)}/model`,
      { method: "PUT", body: jsonBody({ modelEntryId }) }
    ),
  runtimeSessions: (runtimeWorkspaceId: number) =>
    request<RuntimeSessionSummary[]>(`/user/runtime-workspace/${runtimeWorkspaceId}/sessions`),
  runtimeSession: (runtimeWorkspaceId: number, runtimeSessionId: string) =>
    request<RuntimeSessionSummary>(`/user/runtime-workspace/${runtimeWorkspaceId}/sessions/${encodeURIComponent(runtimeSessionId)}`),
  clarifyRequests: (runtimeWorkspaceId: number, runtimeSessionId: string) =>
    request<{ items: RuntimeClarifyRequest[] }>(
      `/user/runtime-workspace/${runtimeWorkspaceId}/sessions/${encodeURIComponent(runtimeSessionId)}/clarify-requests`
    ).then((response) => response.items),
  answerClarifyRequest: (
    runtimeWorkspaceId: number,
    requestId: string,
    answer: RuntimeClarifyAnswer,
    version: number
  ) => request<RuntimeClarifyRequest>(
    `/user/runtime-workspace/${runtimeWorkspaceId}/clarify-requests/${encodeURIComponent(requestId)}/answer`,
    { method: "POST", body: jsonBody({ answer, version }) }
  ),
  cancelClarifyRequest: (runtimeWorkspaceId: number, requestId: string) =>
    request<RuntimeClarifyRequest>(
      `/user/runtime-workspace/${runtimeWorkspaceId}/clarify-requests/${encodeURIComponent(requestId)}/cancel`,
      { method: "POST" }
    ),
  createRuntimeSession: (runtimeWorkspaceId: number, payload: CreateRuntimeSessionRequest) =>
    request<RuntimeSessionSummary>(`/user/runtime-workspace/${runtimeWorkspaceId}/sessions`, {
      method: "POST",
      body: jsonBody(payload)
    }),
  renameRuntimeSession: (runtimeWorkspaceId: number, runtimeSessionId: string, payload: RenameRuntimeSessionRequest) =>
    request<RuntimeSessionSummary>(`/user/runtime-workspace/${runtimeWorkspaceId}/sessions/${encodeURIComponent(runtimeSessionId)}`, {
      method: "PATCH",
      body: jsonBody(payload)
    }),
  deleteRuntimeSession: (runtimeWorkspaceId: number, runtimeSessionId: string) =>
    request<void>(`/user/runtime-workspace/${runtimeWorkspaceId}/sessions/${encodeURIComponent(runtimeSessionId)}`, {
      method: "DELETE"
    }),
  listScheduledTasks: (runtimeWorkspaceId: number, signal?: AbortSignal) =>
    request<{ tasks: ScheduledTask[] }>(
      `/user/runtime-workspace/${runtimeWorkspaceId}/scheduled-tasks`,
      { signal }
    ).then((response) => response.tasks),
  createScheduledTask: (runtimeWorkspaceId: number, payload: CreateScheduledTaskRequest) =>
    request<ScheduledTask>(`/user/runtime-workspace/${runtimeWorkspaceId}/scheduled-tasks`, {
      method: "POST",
      body: jsonBody(scheduledTaskCommandPayload(payload))
    }),
  updateScheduledTask: (runtimeWorkspaceId: number, taskId: number, payload: UpdateScheduledTaskRequest) =>
    request<ScheduledTask>(`/user/runtime-workspace/${runtimeWorkspaceId}/scheduled-tasks/${taskId}`, {
      method: "PATCH",
      body: jsonBody(scheduledTaskCommandPayload(payload))
    }),
  pauseScheduledTask: (runtimeWorkspaceId: number, taskId: number) =>
    request<ScheduledTask>(`/user/runtime-workspace/${runtimeWorkspaceId}/scheduled-tasks/${taskId}/pause`, { method: "POST" }),
  resumeScheduledTask: (runtimeWorkspaceId: number, taskId: number) =>
    request<ScheduledTask>(`/user/runtime-workspace/${runtimeWorkspaceId}/scheduled-tasks/${taskId}/resume`, { method: "POST" }),
  triggerScheduledTask: (runtimeWorkspaceId: number, taskId: number) =>
    request<ScheduledTaskTriggerResponse>(`/user/runtime-workspace/${runtimeWorkspaceId}/scheduled-tasks/${taskId}/trigger`, { method: "POST" }),
  deleteScheduledTask: (runtimeWorkspaceId: number, taskId: number) =>
    request<void>(`/user/runtime-workspace/${runtimeWorkspaceId}/scheduled-tasks/${taskId}`, { method: "DELETE" }),
  chatHistoryPreview: (
    runtimeWorkspaceId: number,
    runtimeSessionId: string,
    signal?: AbortSignal
  ): Promise<RuntimeChatHistoryResponse> => request<RuntimeChatHistoryResponse>(
    `/user/chat/history?runtime_workspace_id=${runtimeWorkspaceId}`
    + `&runtime_session_id=${encodeURIComponent(runtimeSessionId)}`,
    { signal }
  ),
  chatHistoryAuthoritative: async (
    runtimeWorkspaceId: number,
    runtimeSessionId: string
  ): Promise<RuntimeChatHistoryResponse> => ({
    runtimeWorkspaceId,
    runtimeSessionId,
    source: "hermes",
    consistency: "authoritative",
    generatedAt: new Date().toISOString(),
    hasActiveRun: false,
    messages: (await runtimeEdgeClient.historyPrepared(
      runtimeWorkspaceId,
      runtimeSessionId
    )).filter((message): message is RuntimeChatHistoryMessage => (
      ["user", "assistant", "system", "tool"].includes(String(message.role))
    ))
  }),
  chatHistory: (
    runtimeWorkspaceId: number,
    runtimeSessionId: string,
    signal?: AbortSignal
  ): Promise<RuntimeChatHistoryResponse> => request<RuntimeChatHistoryResponse>(
    `/user/chat/history?runtime_workspace_id=${runtimeWorkspaceId}`
    + `&runtime_session_id=${encodeURIComponent(runtimeSessionId)}`,
    { signal }
  ),
  chatQueueSummary: (
    runtimeWorkspaceId: number,
    runtimeSessionId: string
  ) => runtimeEdgeClient.queueSummary(
    runtimeWorkspaceId,
    runtimeSessionId
  ),
  chatQueueSummaryPrepared: (
    runtimeWorkspaceId: number,
    runtimeSessionId: string
  ) => runtimeEdgeClient.queueSummaryPrepared(
    runtimeWorkspaceId,
    runtimeSessionId
  ),
  activeChatRun: (runtimeWorkspaceId: number, runtimeSessionId: string, signal?: AbortSignal) =>
    request<ActiveChatRunResponse>(
      `/user/chat/runs/active?runtime_workspace_id=${runtimeWorkspaceId}&runtime_session_id=${encodeURIComponent(runtimeSessionId)}`,
      { signal }
    ),
  backgroundCollaborations: (
    runtimeWorkspaceId: number,
    runtimeSessionId: string,
    signal?: AbortSignal
  ) =>
    request<ExpertBackgroundCollaborationListResponse>(
      `/user/chat/background-collaborations?runtime_workspace_id=${runtimeWorkspaceId}&runtime_session_id=${encodeURIComponent(runtimeSessionId)}`,
      { signal }
    ),
  streamBackgroundCollaborations: (
    runtimeWorkspaceId: number,
    runtimeSessionId: string,
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal
  ) =>
    streamGetRequest(
      `/user/chat/background-collaborations/stream?runtime_workspace_id=${runtimeWorkspaceId}&runtime_session_id=${encodeURIComponent(runtimeSessionId)}`,
      onEvent,
      signal
    ),
  runActivity: (runtimeWorkspaceId: number, runtimeSessionId: string, runtimeRunId: string, signal?: AbortSignal) =>
    request<RuntimeRunActivityResponse>(
      `/user/chat/runs/${encodeURIComponent(runtimeRunId)}/activity?runtime_workspace_id=${runtimeWorkspaceId}&runtime_session_id=${encodeURIComponent(runtimeSessionId)}`,
      { signal }
    ),
  chatFiles: (runtimeWorkspaceId: number, runtimeSessionId?: string) => {
    const params = new URLSearchParams({ runtime_workspace_id: String(runtimeWorkspaceId) });
    if (runtimeSessionId) {
      params.set("runtime_session_id", runtimeSessionId);
    }
    return request<ChatFileSummary[]>(`/user/chat-files?${params.toString()}`);
  },
  chatFileMetadata: (fileId: number) => request<ChatFileSummary>(`/user/chat-files/${fileId}/download`),
  officePreviewSession: (fileId: number) => request<OfficePreviewSession>(`/user/chat-files/${fileId}/office-preview`, { method: "POST" }),
  downloadChatFile: async (fileId: number) => {
    const intent = await request<ChatDownloadIntent>(
      `/user/chat-files/${fileId}/download-intent`,
      { method: "POST" }
    );
    const response = await fetch(intent.runtimePath, {
      credentials: "include",
      headers: {
        Authorization: `Bearer ${intent.fileTransferTicket}`
      }
    });
    if (!response.ok) throw new Error("文件下载失败");
    return response.blob();
  },
  renameChatFile: (fileId: number, payload: RenameChatFileRequest) =>
    request<ChatFileSummary>(`/user/chat-files/${fileId}`, {
      method: "PATCH",
      body: jsonBody(payload)
    }),
  deleteChatFile: (fileId: number) => request<ChatFileSummary>(`/user/chat-files/${fileId}`, { method: "DELETE" }),
  uploadChatAttachment: (
    file: File,
    runtimeWorkspaceId: number,
    runtimeSessionId: string,
    onProgress?: (percent: number) => void
  ): Promise<ChatUploadResponse> => request<ChatFileTransferIntent>(
    "/user/chat-files/upload-intent",
    {
      method: "POST",
      body: jsonBody({
        runtime_workspace_id: runtimeWorkspaceId,
        runtime_session_id: runtimeSessionId,
        display_name: file.name,
        mime_type: file.type || null,
        size_bytes: file.size
      })
    }
  ).then((intent) => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", intent.runtimePath);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.setRequestHeader(
      "Authorization",
      `Bearer ${intent.fileTransferTicket}`
    );
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100));
    });
    xhr.addEventListener("load", () => {
      if (xhr.status === 401) {
        reject(new Error("附件上传授权已过期，请重试"));
        return;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(responseErrorMessage(xhr.responseText, `附件上传失败：${xhr.status}`)));
        return;
      }
      void (async () => {
        for (let attempt = 0; attempt < 20; attempt += 1) {
          try {
            const readyFile = await request<ChatFileSummary>(
              `/user/chat-files/${intent.file.fileId}/download`
            );
            resolve({ file: readyFile });
            return;
          } catch {
            await new Promise((wait) => window.setTimeout(wait, 250));
          }
        }
        reject(new Error("附件已上传，正在等待平台确认，请稍后重试"));
      })();
    });
    xhr.addEventListener("error", () => reject(new Error("附件上传网络连接失败")));
    xhr.send(file);
  })),
  transcribeVoice: async (audio: Blob, runtimeWorkspaceId: number, runtimeSessionId: string): Promise<VoiceTranscriptionResponse> => {
    const body = new FormData();
    body.append("audio", audio, "voice.webm");
    body.append("runtime_workspace_id", String(runtimeWorkspaceId));
    body.append("runtime_session_id", runtimeSessionId);
    const response = await authorizedFetch("/user/chat/voice/transcribe", { method: "POST", body });
    if (!response.ok) throw new Error("语音转写服务尚未启用");
    return camelize<VoiceTranscriptionResponse>(await response.json());
  },
  streamChatRun: (payload: RuntimeChatRequest, onEvent: (event: ChatStreamEvent) => void, signal?: AbortSignal) =>
    runtimeEdgeClient.streamRun(payload, onEvent, signal),
  submitChatCorrection: (payload: RuntimeChatRequest, onEvent: (event: ChatStreamEvent) => void, signal?: AbortSignal) =>
    runtimeEdgeClient.submitCorrection(payload, onEvent, signal),
  reconnectChatRun: (
    payload: RuntimeRunActionRequest,
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal
  ) => runtimeEdgeClient.resumeRun(payload, onEvent, signal),
  reportChatClientTiming: async (runtimeRunId: string, payload: ChatClientTimingPayload): Promise<void> => {
    for (const delayMs of CLIENT_TIMING_PROJECTION_RETRY_DELAYS_MS) {
      if (delayMs > 0) {
        await new Promise<void>((resolve) => window.setTimeout(resolve, delayMs));
      }
      try {
        const response = await authorizedFetch(
          `/user/chat/runs/${encodeURIComponent(runtimeRunId)}/client-timing`,
          {
            method: "POST",
            body: jsonBody(payload),
            keepalive: true
          }
        );
        if (response.ok || response.status !== 404) return;
      } catch {
        // Telemetry must never alter or interrupt the chat experience.
        return;
      }
    }
  },
  stopChatRun: (payload: RuntimeRunActionRequest) =>
    runtimeEdgeClient.cancelRun(payload),
  expertMarket: (runtimeWorkspaceId: number) =>
    request<ExpertMarketItem[]>(`/user/experts/market?runtime_workspace_id=${runtimeWorkspaceId}`),
  myExperts: (runtimeWorkspaceId: number) =>
    request<RuntimeAgentProfileSummary[]>(`/user/experts/mine?runtime_workspace_id=${runtimeWorkspaceId}`),
  runnableExperts: (runtimeWorkspaceId: number) =>
    request<RuntimeAgentProfileSummary[]>(`/user/experts/runnable?runtime_workspace_id=${runtimeWorkspaceId}`),
  marketExpertDetail: async (runtimeWorkspaceId: number, expertPackageId: number) => {
    const detail = await request<ExpertMarketDetail>(
      `/user/experts/market/${expertPackageId}?runtime_workspace_id=${runtimeWorkspaceId}`
    );
    return {
      ...detail,
      soulContent: detail.soulContent ?? "",
      systemPrompt: detail.systemPrompt ?? null,
      skills: Array.isArray(detail.skills) ? detail.skills : [],
      mcps: Array.isArray(detail.mcps) ? detail.mcps : [],
      knowledgeBases: Array.isArray(detail.knowledgeBases) ? detail.knowledgeBases : []
    };
  },
  expertInheritanceSources: (runtimeWorkspaceId: number) =>
    request<RuntimeAgentProfileSummary[]>(`/user/experts/inheritance-sources?runtime_workspace_id=${runtimeWorkspaceId}`),
  expertCapabilityCatalog: () =>
    request<ExpertCapabilityCatalog>("/user/experts/capability-catalog"),
  expertDetail: (runtimeWorkspaceId: number, runtimeAgentProfileId: number) =>
    request<RuntimeAgentProfileSummary>(
      `/user/experts/${runtimeAgentProfileId}?runtime_workspace_id=${runtimeWorkspaceId}`
    ),
  createExpert: (payload: CreateExpertRequest) =>
    request<RuntimeAgentProfileSummary>("/user/experts", {
      method: "POST",
      body: jsonBody({
        ...payload,
        capabilities: expertCapabilitiesBody(payload.capabilities)
      })
    }),
  copyExpert: (runtimeWorkspaceId: number, runtimeAgentProfileId: number) =>
    request<RuntimeAgentProfileSummary>(`/user/experts/${runtimeAgentProfileId}/copy`, {
      method: "POST",
      body: jsonBody({ runtimeWorkspaceId })
    }),
  updateExpert: (
    runtimeWorkspaceId: number,
    runtimeAgentProfileId: number,
    payload: UpdateExpertRequest
  ) =>
    request<RuntimeAgentProfileSummary>(
      `/user/experts/${runtimeAgentProfileId}?runtime_workspace_id=${runtimeWorkspaceId}`,
      { method: "PATCH", body: jsonBody(payload) }
    ),
  saveExpertConfiguration: (
    runtimeWorkspaceId: number,
    runtimeAgentProfileId: number,
    payload: UpdateExpertConfigurationRequest
  ) =>
    request<RuntimeAgentProfileSummary>(
      `/user/experts/${runtimeAgentProfileId}/configuration?runtime_workspace_id=${runtimeWorkspaceId}`,
      {
        method: "PUT",
        body: jsonBody({
          ...payload,
          capabilities: expertCapabilitiesBody(payload.capabilities)
        })
      }
    ),
  replaceExpertCapabilities: (
    runtimeWorkspaceId: number,
    runtimeAgentProfileId: number,
    capabilities: ExpertCapabilitySelection,
    changeSummary?: string | null
  ) =>
    request<RuntimeAgentProfileSummary>(
      `/user/experts/${runtimeAgentProfileId}/capabilities?runtime_workspace_id=${runtimeWorkspaceId}`,
      {
        method: "PUT",
        body: jsonBody({
          capabilities: expertCapabilitiesBody(capabilities),
          changeSummary
        })
      }
    ),
  replaceExpertPermissions: (
    runtimeWorkspaceId: number,
    runtimeAgentProfileId: number,
    shareScope: ExpertShareScope,
    changeSummary?: string | null
  ) =>
    request<RuntimeAgentProfileSummary>(
      `/user/experts/${runtimeAgentProfileId}/permissions?runtime_workspace_id=${runtimeWorkspaceId}`,
      { method: "PUT", body: jsonBody({ shareScope, changeSummary }) }
    ),
  submitExpertReview: (
    runtimeWorkspaceId: number,
    runtimeAgentProfileId: number,
    shareScope?: ExpertShareScope
  ) =>
    request<RuntimeAgentProfileSummary>(
      `/user/experts/${runtimeAgentProfileId}/submit-review`,
      {
        method: "POST",
        body: jsonBody({ runtimeWorkspaceId, ...(shareScope ? { shareScope } : {}) })
      }
    ),
  installMarketExpert: (runtimeWorkspaceId: number, expertPackageId: number) =>
    request<RuntimeAgentProfileSummary>(`/user/experts/market/${expertPackageId}/install`, {
      method: "POST",
      body: jsonBody({ runtimeWorkspaceId })
    }),
  unpublishExpert: (runtimeWorkspaceId: number, runtimeAgentProfileId: number) =>
    request<RuntimeAgentProfileSummary>(`/user/experts/${runtimeAgentProfileId}/unpublish`, {
      method: "POST",
      body: jsonBody({ runtimeWorkspaceId })
    }),
  uninstallExpert: (runtimeWorkspaceId: number, runtimeAgentProfileId: number) =>
    request<void>(
      `/user/experts/${runtimeAgentProfileId}?runtime_workspace_id=${runtimeWorkspaceId}`,
      { method: "DELETE" }
    ),
  defaultProfileAssets: (runtimeWorkspaceId: number, signal?: AbortSignal) =>
    request<DefaultProfileAssetView>(
      `/user/runtime-workspaces/${runtimeWorkspaceId}/default-profile/assets`,
      { signal }
    ),
  userAssetCatalog: (signal?: AbortSignal) =>
    request<UserAssetCatalogView>("/user/assets/catalog", { signal }),
  installDefaultProfileAsset: (
    runtimeWorkspaceId: number,
    assetType: "skill" | "mcp",
    assetId: number
  ) =>
    request<DefaultProfileAssetOperationAccepted>(
      `/user/runtime-workspaces/${runtimeWorkspaceId}/default-profile/${assetType === "skill" ? "skills" : "mcps"}/${assetId}`,
      { method: "PUT" }
    ),
  uninstallDefaultProfileAsset: (
    runtimeWorkspaceId: number,
    assetType: "skill" | "mcp",
    assetId: number
  ) =>
    request<DefaultProfileAssetOperationAccepted>(
      `/user/runtime-workspaces/${runtimeWorkspaceId}/default-profile/${assetType === "skill" ? "skills" : "mcps"}/${assetId}`,
      { method: "DELETE" }
    ),
  personalSkillAssets: () => request<PersonalSkillAssetSummary[]>("/user/personal-skills"),
  personalSkillCategories: () =>
    request<SkillCategorySummary[]>("/user/personal-skills/categories"),
  createPersonalSkillCategory: (displayName: string) =>
    request<SkillCategorySummary>("/user/personal-skills/categories", {
      method: "POST",
      body: jsonBody({ displayName }),
    }),
  systemSkills: () => request<SystemSkillSummary[]>("/user/personal-skills/system"),
  personalSkillAssetDetail: (skillId: number) =>
    request<PersonalSkillAssetDetailSummary>(`/user/personal-skills/${skillId}`),
  updatePersonalSkillDescription: (
    skillId: number,
    description: string | null,
    name?: string,
  ) =>
    request<PersonalSkillAssetSummary>(`/user/personal-skills/${skillId}`, {
      method: "PUT",
      body: jsonBody({ description, name }),
    }),
  archivePersonalSkill: (skillId: number) =>
    request<PersonalSkillAssetSummary>(`/user/personal-skills/${skillId}/archive`, { method: "POST" }),
  deletePersonalSkill: (skillId: number) =>
    request<{ skillId: number; rolloutId: string; affectedProfileIds: number[]; desiredRevisions: Record<string, number> }>(
      `/user/personal-skills/${skillId}`,
      { method: "DELETE" }
    ),
  // Keep the ZIP and user-provided description in the request body.  A
  // description is ordinary Unicode text and must not be placed in an HTTP
  // header, whose Fetch ByteString contract rejects characters such as
  // Chinese text before the request is sent.
  uploadPersonalSkill: async (payload: {
    file: File;
    categoryKey: string;
    description?: string;
  }) => {
    const body = new FormData();
    body.append("file", payload.file);
    if (payload.description !== undefined) body.append("description", payload.description);
    return request<PersonalSkillAssetSummary>(`/user/personal-skills?category_key=${encodeURIComponent(payload.categoryKey)}`, {
      method: "POST",
      body,
    }, undefined, "个人 Skill 包超过 25 MiB，请压缩后重试");
  },
  uploadAndInstallPersonalSkill: async (runtimeWorkspaceId: number, payload: {
    file: File;
    categoryKey: string;
    description?: string;
  }) => {
    const body = new FormData();
    body.append("file", payload.file);
    if (payload.description !== undefined) body.append("description", payload.description);
    return request<PersonalSkillInstallAccepted>(
      `/user/runtime-workspaces/${runtimeWorkspaceId}/default-profile/personal-skills?category_key=${encodeURIComponent(payload.categoryKey)}`,
      {
        method: "POST",
        body,
      },
      undefined,
      "个人 Skill 包超过 25 MiB，请压缩后重试"
    );
  },
};
