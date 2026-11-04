import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useDropzone } from "react-dropzone";
import { ArrowDown, ArrowLeft, Clock3, Database, FolderOpen, KeyRound, LayoutDashboard, LayoutGrid, LoaderCircle, MessageCircleMore, MessageSquare, PlugZap, Sparkles, UploadCloud } from "lucide-react";
import { ExpertsNavIcon, SkillsNavIcon } from "../components/nav-icons";
import type { ChatFileSummary, RenameChatFileRequest, RuntimeAgentProfileSummary, RuntimeModelSummary, RuntimeSessionSummary, RuntimeStartupContextSummary, RuntimeWorkspaceSummary } from "@hermes-cloud/schemas";
import {
  ApiError,
  userApi as platformUserApi,
  type ActiveChatRunResponse,
  type RuntimeClarifyAnswer,
  type RuntimeClarifyRequest,
  type SessionPrewarmState
} from "../api/client";
import type { AppCenterListItem } from "../api/client";
import { getPrincipal, refreshPrincipal } from "../auth";
import { usePlatformTheme } from "../theme/platform-theme";
import {
  formatRuntimeSessionPrepareError,
  RUNTIME_EDGE_QUEUE_UPDATED_EVENT,
  type RuntimeEdgeQueueSummary,
  type RuntimeEdgeQueueUpdatedDetail
} from "../api/runtime-edge-client";
import { AppSidebar } from "../components/AppSidebar";
import { Composer, type AttachmentUploadItem } from "../components/Composer";
// 会话主轴布局（2026-11-03-014）：恒定 850px 车道 + 三段式 Flex，消息列与输入区共用 .main-axis
import "./conversation-lane.css";
import { ChatQueuedRunList } from "../components/ChatQueuedRunList";
import { ClarifyCard } from "../components/ClarifyCard";
import { ChatMessageList } from "../components/ChatMessageList";
import { FileRail } from "../components/FileRail";
import { WelcomeTaskPanel } from "../components/WelcomeTaskPanel";
import { FilePreviewDialog, filePreviewKind, type FilePreviewState } from "../components/FilePreviewDialog";
import {
  DEFAULT_TASK_TITLE,
  displayTaskTitle,
  firstMessageTaskTitle,
  isDefaultTaskTitle,
  numberedTaskTitle
} from "../taskPresentation";
import {
  nextFilesPaneOpen,
  type RuntimeConnectionState
} from "../ui-state";
import { loadUserProfile, saveUserProfile } from "../profile";
import { isCriticalRuntimeError } from "../runtimeColdStart";
import {
  acceptChatRun,
  acceptQueuedChatRun,
  beginChatCorrection,
  beginPromotedCorrectionRun,
  beginQueuedChatRun,
  beginQueuedChatSubmission,
  beginChatTurn,
  beginScheduledTask,
  cancelChatTurn,
  ensureActiveChatRun,
  failChatCorrection,
  failChatTurn,
  isFirstVisibleAgentEvent,
  progressMessageId,
  projectChatRunEvent,
  type ChatMessage,
  type ChatMessageAttachment,
  type SessionCache
} from "./chatRunProjection";
import {
  attachGeneratedImageArtifacts,
  chatAttachmentFromFile,
  isRuntimeArtifactAttachment
} from "./chat_image_output/chatImageArtifacts";
import { chatComposerAction } from "./chatComposerAction";
import {
  emptyChatQueueSnapshot,
  QueueSnapshotVersionGate,
  reconcileChatQueueSnapshot,
  removeStartedQueuedRun
} from "./chatQueueProjection";
import { SessionSubmissionGate } from "./sessionSubmissionGate";
import { historyMessages, reconcileSessionHistory } from "./chatHistory";
import {
  loadChatHistorySources,
  type ChatHistorySyncStatus
} from "./chatHistoryLoader";
import { currentDocumentVisibility, TurnTimingCoordinator } from "./chatTiming";
import { SessionPrewarmCoordinator } from "./sessionPrewarmCoordinator";
import { useAutoDismissError } from "./useAutoDismissError";
import { QueuedTurnDispatcher } from "./queuedTurnDispatcher";
import {
  initialSessionReadinessState,
  reduceSessionReadiness,
  type SessionReadinessEvent,
  type SessionReadinessTarget
} from "./sessionReadinessState";
import { chatPerformanceFlags } from "./chatPerformanceFlags";
import { ApplicationAgentConfigurationPanel } from "./ApplicationAgentConfigurationPanel";
import { retryApplicationRuntimeBusy } from "./applicationRuntimeRecovery";
import {
  ClarifyPollCoordinator,
  findClarifyContinuationReplay,
  isClarifyCardRenderable,
  mergeClarifyRequests,
  scopeClarifyRequests
} from "./clarifyRequestRecovery";
import { useAnchoredChatScroll } from "./useAnchoredChatScroll";
import {
  contextHeadSession,
  contextRootSession,
  findContextBranch,
  sidebarVisibleSessions,
} from "./runtimeSessionContextBranch";
import {
  SessionActivityCoordinator,
  type SessionActivityMap
} from "./sessionActivity";
import { extractHtmlArtifacts, htmlDocumentTitle, htmlPreviewSignature } from "../utils/htmlArtifact";
import { readZipTextEntry } from "../utils/appPackage";
import {
  htmlPathsFromToolEvents,
  isHtmlChatFile,
  isZipChatFile,
} from "../utils/htmlArtifactSources";

const KnowledgePanelView = lazy(() =>
  import("../components/knowledge").then((m) => ({ default: m.KnowledgePanelView })));
const AppCenterPanelView = lazy(() =>
  import("../components/app_center").then((m) => ({ default: m.AppCenterPanelView })));
const HtmlPreviewPanel = lazy(() =>
  import("../components/app_center").then((m) => ({ default: m.HtmlPreviewPanel })));
const AppUpdateClarifyCard = lazy(() =>
  import("../components/app_center").then((m) => ({ default: m.AppUpdateClarifyCard })));
const ExpertSkillsPanelView = lazy(() =>
  import("../components/expert_skills").then((m) => ({ default: m.ExpertSkillsPanelView })));
const SchedulesPanelView = lazy(() =>
  import("../components/schedules").then((m) => ({ default: m.SchedulesPanelView })));
const ProfileDialogView = lazy(() =>
  import("../components/profile").then((m) => ({ default: m.ProfileDialogView })));
const McpPanelView = lazy(() =>
  import("../components/mcp").then((m) => ({ default: m.McpPanelView })));
const ImPanelView = lazy(() =>
  import("../components/im").then((m) => ({ default: m.ImPanelView })));

type View = "chat" | "app-center" | "expert-skills" | "skills" | "mcps" | "knowledge" | "schedules" | "settings" | "im" | "channels" | "clients";
type RuntimeAwakeResult = {
  readyWorkspace: RuntimeWorkspaceSummary;
  recovered: boolean;
};
const RUNTIME_KEEPALIVE_INTERVAL_MS = 60_000;
const SESSION_PREWARM_INTERVAL_MS = 60_000;
const MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024;
const MAX_IMAGE_ATTACHMENT_BYTES = 20 * 1024 * 1024;
const SUPPORTED_IMAGE_MIME_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
const MAX_TEXT_PREVIEW_BYTES = 1024 * 1024;
const MAX_HTML_PREVIEW_BYTES = 16 * 1024 * 1024;
const MAX_APP_PACKAGE_PREVIEW_BYTES = 100 * 1024 * 1024;
const views = [{ id: "chat", label: "任务", icon: MessageSquare }, { id: "app-center", label: "看板区", icon: LayoutGrid }, { id: "expert-skills", label: "专家-技能", icon: ExpertsNavIcon }, { id: "knowledge", label: "知识库", icon: Database }, { id: "schedules", label: "定时任务", icon: Clock3 }] as const;
const applicationViews = [{ id: "chat", label: "调试", icon: MessageSquare }, { id: "settings", label: "Agent 配置", icon: LayoutDashboard }, { id: "skills", label: "技能", icon: SkillsNavIcon }, { id: "mcps", label: "MCP", icon: PlugZap }, { id: "knowledge", label: "知识库", icon: Database }, { id: "channels", label: "企微渠道", icon: MessageCircleMore }, { id: "clients", label: "API 调用方", icon: KeyRound }] as const;
const visibleSession = (session: RuntimeSessionSummary) => (
  session.sourceType === "web"
  || session.sourceType === "user"
  || session.sourceType === "wecom"
  || session.sourceType === "openapi"
);
const FRAME_COMMITTED_DELTA_EVENTS = new Set([
  "chat.reasoning.delta",
  "chat.activity.delta",
  "chat.progress.delta",
  "chat.message.delta"
]);
const isFrameCommittedDelta = (event: string) => (
  FRAME_COMMITTED_DELTA_EVENTS.has(event)
);
const isReadOnlySession = (session: RuntimeSessionSummary | null | undefined) => (
  Boolean(session?.isReadOnly || session?.status === "expert_deleted")
);
const isImmutableSession = (session: RuntimeSessionSummary | null | undefined) => (
  Boolean(
    session?.isImmutable
    || session?.sourceType === "wecom"
    || session?.sourceType === "openapi"
  )
);
const runtimeConnectionStateFromWorkspace = (status?: string | null): RuntimeConnectionState => {
  if (status === "failed") return "failed";
  if (status === "starting" || status === "mounting" || status === "probing") return status;
  return "connecting";
};
const runtimeStartupPending = (status?: string | null): boolean => (
  status == null || status === "absent" || ["starting", "mounting", "probing"].includes(status)
);
const INITIAL_SESSION_PROFILE_RETRY_DELAY_MS = 3_000;
const INITIAL_SESSION_PROFILE_MAX_ATTEMPTS = 40;
const RUNTIME_AGENT_PROFILE_PREPARING_CODE = "runtime_agent_profile_preparing";

function isRuntimeAgentProfilePreparing(reason: unknown): boolean {
  return (reason as { code?: unknown } | null)?.code
    === RUNTIME_AGENT_PROFILE_PREPARING_CODE;
}

function readBlobText(blob: Blob): Promise<string> {
  if (typeof blob.text === "function") return blob.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(typeof reader.result === "string" ? reader.result : ""));
    reader.addEventListener("error", () => reject(reader.error || new Error("文件读取失败")));
    reader.readAsText(blob);
  });
}

const isImageFile = (file: File) => file.type.startsWith("image/");

function isSupportedImageFile(file: File): boolean {
  return SUPPORTED_IMAGE_MIME_TYPES.has(file.type);
}

function normalizePastedImage(file: File, ordinal: number): File {
  if (file.name && file.name !== "image.png") return file;
  const extension = file.type === "image/jpeg"
    ? "jpg"
    : file.type === "image/webp" ? "webp" : "png";
  const timestamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
  return new File([file], `pasted-image-${timestamp}-${ordinal}.${extension}`, { type: file.type });
}

function hydrateAttachmentMetadata(
  messages: ChatMessage[],
  files: ChatFileSummary[]
): ChatMessage[] {
  const filesById = new Map(files.map((file) => [file.fileId, file]));
  return messages.map((message) => {
    if (message.role !== "user" || !message.attachments) return message;
    const attachments = message.attachments.map((attachment) => {
      const file = filesById.get(attachment.fileId);
      if (!file) return attachment;
      return chatAttachmentFromFile(file);
    });
    return { ...message, attachments };
  });
}

interface LocalAttachmentUpload extends AttachmentUploadItem {
  file: File;
  sessionId: string;
}

interface HtmlPreviewState {
  sessionId: string;
  messageId: string;
  sourceRunId: string | null;
  sourceKey: string;
  sourceName: string;
  html: string;
  packageSource: {
    fileId: number;
    displayName: string;
  } | null;
  version: number;
  sharedApp: { app: AppCenterListItem; version: number } | null;
}

interface EditAppContext {
  appId: number;
  appName: string;
  sessionId: string;
  startFileIds: number[];
}

interface EditAppUpdateCandidate {
  appId: number;
  appName: string;
  file: {
    fileId: number;
    displayName: string;
  };
}

interface AppUpdateDecision {
  sessionId: string;
  appId: number;
  appName: string;
  html?: string;
  version?: number;
  file?: {
    fileId: number;
    displayName: string;
  };
  sourceName: string;
}

interface HtmlPreviewContent {
  html: string;
  version: number;
  packageSource?: {
    fileId: number;
    displayName: string;
  } | null;
}

interface DesktopHtmlPreviewBridge {
    readHtmlPreview?: (path: string) => Promise<string>;
}

const getDesktopHtmlPreviewBridge = () => (
  window as Window & { coworkDesktop?: DesktopHtmlPreviewBridge }
).coworkDesktop;

const SCHEDULED_TASK_POLL_INTERVAL_MS = 3_000;
const SCHEDULED_TASK_POLL_MAX_ATTEMPTS = 20;
const SCHEDULED_TASK_POLL_MAX_BACKOFF_MS = 30_000;
const SCHEDULED_TASK_RECONNECT_TIMEOUT_MS = 15_000;
const SCHEDULED_TASK_OBSERVATION_ERROR = "无法确认定时任务是否已开始执行，请稍后刷新会话或再次尝试。";

const hasNewFiles = (previous: ChatFileSummary[], next: ChatFileSummary[]) => {
  const knownFileIds = new Set(previous.map((file) => file.fileId));
  return next.some((file) => !knownFileIds.has(file.fileId));
};
const SELF_EVOLUTION_POLL_INTERVAL_MS = 2_000;
const SELF_EVOLUTION_POLL_MAX_ATTEMPTS = 160;
const PREWARM_HINT_DELAY_MS = 5_000;
const REQUIRED_PREWARM_RETRY_DELAYS_MS = [500, 1_000, 2_000, 4_000, 5_000];
const REQUIRED_PREWARM_RETRY_WINDOW_MS = 30_000;
const ARTIFACT_INDEX_RETRY_DELAYS_MS = [100, 250, 500, 1_000, 2_000, 2_000];
const ARTIFACT_EVENTS = new Set([
  "chat.artifact.created",
  "chat.artifact.updated",
  "chat.artifact.deleted"
]);
const RECOVERABLE_RUN_STATUSES = new Set(["accepted", "pending", "running"]);

const isRecoverableRun = (
  run: ActiveChatRunResponse["run"] | null | undefined
): run is NonNullable<ActiveChatRunResponse["run"]> => {
  if (!run) return false;
  return RECOVERABLE_RUN_STATUSES.has(String(run.status).toLowerCase());
};

const activeChatRunFromQueueSummary = (
  runtimeWorkspaceId: number,
  runtimeSessionId: string,
  summary: RuntimeEdgeQueueSummary
): ActiveChatRunResponse => {
  const activeRun = summary.activeRun;
  if (!activeRun) return { run: null };
  const state = activeRun.state.trim().toLowerCase();
  if (["completed", "failed", "canceled", "cancelled", "interrupted"].includes(state)) {
    return { run: null };
  }
  return {
    run: {
      runtimeWorkspaceId,
      runtimeSessionId,
      runtimeRunId: activeRun.runId,
      status: state === "queued"
        ? "pending"
        : state === "accepted" || state === "pending"
          ? state
          : "running",
      lastSeq: 0
    }
  };
};

const historyCheckpointForRun = (
  run: ActiveChatRunResponse["run"] | null | undefined
): number | undefined => {
  if (!isRecoverableRun(run)) {
    return undefined;
  }
  return typeof run.historyMessageCountBeforeRun === "number"
    && run.historyMessageCountBeforeRun >= 0
    ? run.historyMessageCountBeforeRun
    : undefined;
};

const hasHttpStatus = (value: unknown, status: number) => (
  value instanceof Error && "status" in value && value.status === status
);
type ReconnectOutcome = "terminal" | "retryable" | "cancelled";
type TimingKind = "lifecycle" | "content";
type RecoveryOptions = {
  scheduled?: boolean;
  timeoutMs?: number;
  preferredRuntimeRunId?: string;
};
type PendingVisibleCandidate = {
  sessionId: string;
  runtimeRunId: string;
  targetId: string;
  receivedAt: number;
  timingKind: TimingKind;
};
type PaintFrameState = {
  sessionId: string;
  runtimeRunId: string;
  targetId: string;
  timingKind: TimingKind;
  firstFrame: number;
  secondFrame?: number;
};
type StopRequestState = {
  sessionId: string;
  workspaceId: number;
  controller: AbortController;
  runtimeRunId?: string;
  sent: boolean;
};
type SubmissionState = {
  sessionId: string;
  turnId: string;
  controller: AbortController;
  phase: "required-prewarm" | "submitting";
};
type RequiredPrewarmTarget = SessionReadinessTarget;
type RequiredPrewarmGate = {
  target: RequiredPrewarmTarget;
  status: "pending" | "ready" | "failed";
  receipt: SessionPrewarmState | null;
  promise: Promise<SessionPrewarmState>;
};
type FrozenQueuedTurnPayload = {
  content: string;
  attachmentFileIds: number[];
};
type SelfEvolutionObserver = {
  workspaceId: number;
  sessionId: string;
  attemptsByRun: Map<string, number>;
  timer?: number;
  polling: boolean;
};
const CANCELED_DONE_STATUSES = new Set(["canceled", "cancelled", "interrupted"]);
const CHAT_TRANSPORT_RECOVERY_MESSAGE = "连接暂时中断，任务仍在后台执行，请稍后刷新查看结果。";

function asSessionPrewarmState(value: unknown): SessionPrewarmState {
  if (
    value
    && typeof value === "object"
    && (
      (value as { state?: unknown }).state === "ready"
      || (value as { state?: unknown }).state
        === "skipped-unmaterialized"
    )
  ) {
    return value as SessionPrewarmState;
  }
  return { state: "ready" };
}

function createWebRuntimeRunId(): string {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  if (randomUuid) return `web-${randomUuid}`;
  return `web-${Date.now()}-${Math.random().toString(36).slice(2, 14)}`;
}

function cancelPendingChatTurn(cache: SessionCache, turnId: string): SessionCache {
  if (cache.activeTurn?.turnId !== turnId || cache.activeTurn.runtimeRunId) return cache;
  const assistantMessageId = cache.activeTurn.assistantMessageId;
  return {
    ...cache,
    messages: cache.messages.filter((message) => message.id !== assistantMessageId),
    streaming: false,
    runId: null,
    activeTurn: {
      ...cache.activeTurn,
      status: "completed",
      error: undefined
    }
  };
}

export interface AgentWorkspaceProps {
  context?:
    | { kind: "personal" }
    | { kind: "application"; applicationAgentId: number };
}

function applicationWorkspaceApi(
  applicationAgentId: number,
  recoverySignal: () => AbortSignal
): typeof platformUserApi {
  const overrides = {
    runtimeWorkspace: () =>
      platformUserApi.applicationAgentStudioWorkspace(applicationAgentId),
    bootstrapRuntime: () => retryApplicationRuntimeBusy(
      () => platformUserApi.warmupApplicationAgentStudio(applicationAgentId),
      { signal: recoverySignal() }
    ),
    warmupRuntime: () => retryApplicationRuntimeBusy(
      () => platformUserApi.warmupApplicationAgentStudio(applicationAgentId),
      { signal: recoverySignal() }
    ),
    keepaliveRuntime: async () => {
      await retryApplicationRuntimeBusy(
        () => platformUserApi.keepaliveApplicationAgentStudio(
          applicationAgentId
        ),
        { signal: recoverySignal() }
      );
    },
    prewarmRuntimeSession: (
      runtimeWorkspaceId: number,
      runtimeSessionId: string
    ) => platformUserApi.prewarmApplicationAgentRuntimeEdge(
      applicationAgentId,
      runtimeWorkspaceId,
      runtimeSessionId
    ),
    runtimeSessions: (_runtimeWorkspaceId: number) =>
      platformUserApi.applicationAgentStudioSessions(applicationAgentId),
    createRuntimeSession: (
      _runtimeWorkspaceId: number,
      payload: Parameters<typeof platformUserApi.createRuntimeSession>[1]
    ) => platformUserApi.createApplicationAgentStudioSession(
      applicationAgentId,
      payload
    ),
    renameRuntimeSession: (
      _runtimeWorkspaceId: number,
      runtimeSessionId: string,
      payload: Parameters<typeof platformUserApi.renameRuntimeSession>[2]
    ) => platformUserApi.renameApplicationAgentStudioSession(
      applicationAgentId,
      runtimeSessionId,
      payload
    ),
    deleteRuntimeSession: (
      _runtimeWorkspaceId: number,
      runtimeSessionId: string
    ) => platformUserApi.deleteApplicationAgentStudioSession(
      applicationAgentId,
      runtimeSessionId
    ),
    chatHistory: async (
      runtimeWorkspaceId: number,
      runtimeSessionId: string
    ) => {
      let messages: Record<string, unknown>[];
      try {
        messages = await platformUserApi.applicationAgentRuntimeEdgeHistory(
          applicationAgentId,
          runtimeWorkspaceId,
          runtimeSessionId
        );
      } catch {
        const history = await platformUserApi.applicationAgentStudioHistory(
          applicationAgentId,
          runtimeSessionId
        );
        messages = history.messages;
      }
      return {
        runtimeWorkspaceId,
        runtimeSessionId,
        source: "hermes" as const,
        consistency: "authoritative" as const,
        generatedAt: new Date().toISOString(),
        hasActiveRun: false,
        messages
      };
    },
    chatQueueSummary: (
      runtimeWorkspaceId: number,
      runtimeSessionId: string
    ) => platformUserApi.applicationAgentRuntimeEdgeQueueSummary(
      applicationAgentId,
      runtimeWorkspaceId,
      runtimeSessionId
    ),
    chatFiles: (_runtimeWorkspaceId: number, runtimeSessionId?: string) =>
      platformUserApi.applicationAgentChatFiles(applicationAgentId, runtimeSessionId),
    chatFileMetadata: (fileId: number) =>
      platformUserApi.applicationAgentChatFileMetadata(applicationAgentId, fileId),
    downloadChatFile: (fileId: number) =>
      platformUserApi.applicationAgentDownloadChatFile(applicationAgentId, fileId),
    renameChatFile: (fileId: number, payload: RenameChatFileRequest) =>
      platformUserApi.applicationAgentRenameChatFile(applicationAgentId, fileId, payload),
    deleteChatFile: (fileId: number) =>
      platformUserApi.applicationAgentDeleteChatFile(applicationAgentId, fileId),
    uploadChatAttachment: (file: File, _runtimeWorkspaceId: number, runtimeSessionId: string, onProgress?: (percent: number) => void) =>
      platformUserApi.applicationAgentUploadChatAttachment(applicationAgentId, file, runtimeSessionId, onProgress),
    activeChatRun: async (
      runtimeWorkspaceId: number,
      runtimeSessionId: string
    ) => activeChatRunFromQueueSummary(
      runtimeWorkspaceId,
      runtimeSessionId,
      await platformUserApi.applicationAgentRuntimeEdgeQueueSummary(
        applicationAgentId,
        runtimeWorkspaceId,
        runtimeSessionId
      )
    ),
    backgroundCollaborations: async () => ({
      collaborations: [],
      pendingCount: 0
    }),
    streamBackgroundCollaborations: async () => undefined,
    listScheduledTasks: async () => [],
    runnableExperts: async () => [],
    setSessionModel: async (
      runtimeWorkspaceId: number,
      runtimeSessionId: string,
      modelEntryId: number
    ) => ({
      runtimeWorkspaceId,
      runtimeSessionId,
      modelEntryId,
      appliesTo: "next_run",
      activeRunCount: 0
    }),
    streamChatRun: (
      payload: Parameters<typeof platformUserApi.streamChatRun>[0],
      onEvent: Parameters<typeof platformUserApi.streamChatRun>[1],
      signal?: AbortSignal
    ) => {
      return platformUserApi.streamApplicationAgentRuntimeEdge(
        applicationAgentId,
        payload,
        onEvent,
        signal
      );
    },
    submitChatCorrection: (
      payload: Parameters<typeof platformUserApi.submitChatCorrection>[0],
      onEvent: Parameters<typeof platformUserApi.submitChatCorrection>[1],
      signal?: AbortSignal
    ) => platformUserApi.submitApplicationAgentRuntimeEdgeCorrection(
      applicationAgentId,
      payload,
      onEvent,
      signal
    ),
    reconnectChatRun: (
      payload: Parameters<typeof platformUserApi.reconnectChatRun>[0],
      onEvent: Parameters<typeof platformUserApi.reconnectChatRun>[1],
      signal?: AbortSignal
    ) => platformUserApi.reconnectApplicationAgentRuntimeEdge(
      applicationAgentId,
      payload,
      onEvent,
      signal
    ),
    stopChatRun: async (
      payload: Parameters<typeof platformUserApi.stopChatRun>[0]
    ) => platformUserApi.stopApplicationAgentRuntimeEdge(
      applicationAgentId,
      payload
    )
  };
  return new Proxy(platformUserApi, {
    get(target, property, receiver) {
      if (property in overrides) {
        return overrides[property as keyof typeof overrides];
      }
      return Reflect.get(target, property, receiver);
    }
  }) as typeof platformUserApi;
}

export function AgentWorkspace({
  context = { kind: "personal" }
}: AgentWorkspaceProps = {}) {
  return <PersonalAgentWorkspace context={context} />;
}

function PersonalAgentWorkspace({ context }: Required<AgentWorkspaceProps>) {
  const applicationRuntimeRecoveryRef = useRef(new AbortController());
  useEffect(() => {
    if (applicationRuntimeRecoveryRef.current.signal.aborted) {
      applicationRuntimeRecoveryRef.current = new AbortController();
    }
    const controller = applicationRuntimeRecoveryRef.current;
    return () => controller.abort();
  }, [context.kind, context.kind === "application"
    ? context.applicationAgentId
    : null]);
  const userApi = useMemo(
    () => context.kind === "application"
      ? applicationWorkspaceApi(
        context.applicationAgentId,
        () => applicationRuntimeRecoveryRef.current.signal
      )
      : platformUserApi,
    [context.kind, context.kind === "application"
      ? context.applicationAgentId
      : null]
  );
  const isApplication = context.kind === "application";
  const applicationAgentId = context.kind === "application"
    ? context.applicationAgentId
    : null;
  const workspaceViews = isApplication ? applicationViews : views;
  const principal = getPrincipal();
  const [view, setView] = useState<View>("chat");
  const [composerFocusRequest, setComposerFocusRequest] = useState(0);
  const [runtime, setRuntime] = useState<RuntimeWorkspaceSummary | null>(null);
  const [runtimeStartupContext, setRuntimeStartupContext] = useState<RuntimeStartupContextSummary["context"] | null>(null);
  const [models, setModels] = useState<RuntimeModelSummary[]>([]);
  const [sessions, setSessions] = useState<RuntimeSessionSummary[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [files, setFiles] = useState<ChatFileSummary[]>([]);
  const [filePreview, setFilePreview] = useState<FilePreviewState | null>(null);
  const [pendingAttachmentIds, setPendingAttachmentIds] = useState<number[]>([]);
  const [uploadItems, setUploadItems] = useState<LocalAttachmentUpload[]>([]);
  const [installedExperts, setInstalledExperts] = useState<RuntimeAgentProfileSummary[]>([]);
  const [modelId, setModelId] = useState<number | null>(null);
  const [input, setInput] = useState(""); const [welcomeTaskPreview, setWelcomeTaskPreview] = useState<string | null>(null); const [error, setError] = useAutoDismissError(); const [loading, setLoading] = useState(true);
  const [runtimeAwake, setRuntimeAwake] = useState(false);
  const [, setInitialSessionPreparing] = useState(false);
  const [sessionReadiness, setSessionReadiness] = useState(
    initialSessionReadinessState
  );
  const waitingForPrewarmSend = sessionReadiness.status === "queued";
  const [, setRecoveryHintVisible] = useState(false);
  const [runtimeConnectionState, setRuntimeConnectionState] = useState<RuntimeConnectionState>("connecting");
  const [streaming, setStreaming] = useState(false); const [stopping, setStopping] = useState(false); const [runId, setRunId] = useState<string | null>(null); const [menuOpen, setMenuOpen] = useState(false);
  const [queueSummary, setQueueSummary] = useState<RuntimeEdgeQueueSummary>(
    emptyChatQueueSnapshot
  );
  const [historySyncStatus, setHistorySyncStatus] = useState<
    ChatHistorySyncStatus | "idle"
  >("idle");
  const [sessionActivities, setSessionActivities] = useState<SessionActivityMap>({});
  const [clarifyRequests, setClarifyRequests] = useState<RuntimeClarifyRequest[]>([]);
  const [clarifyBusyId, setClarifyBusyId] = useState<string | null>(null);
  const [canManageApplicationAgents, setCanManageApplicationAgents] = useState(
    Boolean(principal?.canManageApplicationAgents)
  );
  const [isAdmin, setIsAdmin] = useState(Boolean(principal?.isAdmin));
  const [profile, setProfile] = useState(() => loadUserProfile(principal));
  const [profileOpen, setProfileOpen] = useState(false);
  const [pendingScheduledTask, setPendingScheduledTask] = useState<{
    taskId: number;
    taskName: string;
    prompt: string;
    acceptedAt?: string;
    sessionId: string;
  } | null>(null);
  const { theme, setTheme } = usePlatformTheme();
  const [filesOpen, setFilesOpen] = useState(() => nextFilesPaneOpen(localStorage.getItem("hermes.files-pane"), window.innerWidth >= 1100));
  const [htmlPreview, setHtmlPreview] = useState<HtmlPreviewState | null>(null);
  // Session-level fallback for the preview panel's share state: the in-memory
  // sharedApp binding is reset by every new preview version and lost on
  // reload, but apps remember their source_session_id. Resolving it here
  // lets the panel offer "update share" instead of a duplicate "share".
  const [sessionSharedApp, setSessionSharedApp] = useState<AppCenterListItem | null>(null);
  const sessionSharedAppRef = useRef<Map<string, AppCenterListItem | null>>(new Map());
  const [editAppContext, setEditAppContext] = useState<EditAppContext | null>(null);
  const [editAppUpdating, setEditAppUpdating] = useState(false);
  const [appUpdateDecision, setAppUpdateDecision] = useState<AppUpdateDecision | null>(null);
  const refreshApplicationAgentAccess = async () => {
    try {
      const [access, refreshedPrincipal] = await Promise.all([
        userApi.applicationAgentAccess(),
        refreshPrincipal()
      ]);
      setCanManageApplicationAgents(access.canManageApplicationAgents);
      setIsAdmin(Boolean(refreshedPrincipal?.isAdmin));
    } catch {
      setCanManageApplicationAgents(false);
      setIsAdmin(false);
    }
  };
  useEffect(() => {
    void refreshApplicationAgentAccess();
    const refreshOnFocus = () => void refreshApplicationAgentAccess();
    window.addEventListener("focus", refreshOnFocus);
    return () => window.removeEventListener("focus", refreshOnFocus);
  }, []);
  const activeSessionRef = useRef<string | null>(null);
  // sessionId → 被关闭预览的内容签名；内容更新后允许再次自动打开
  const htmlPreviewDismissedRef = useRef<Map<string, string>>(new Map());
  // sessionId → 最近一次成功读取的本地 HTML 路径，用于预览关闭后手动重开
  const localHtmlPreviewTargetsRef = useRef<Map<string, string>>(new Map());
  const htmlPreviewSourceVersionsRef = useRef<Map<string, number>>(new Map());
  const htmlPreviewHandledRunsRef = useRef<Set<string>>(new Set());
  const queueSnapshotVersionGateRef = useRef(new QueueSnapshotVersionGate());
  const lastSessionIdForPreviewResetRef = useRef<string | null>(sessionId);
  useEffect(() => {
    if (lastSessionIdForPreviewResetRef.current === sessionId) return;
    lastSessionIdForPreviewResetRef.current = sessionId;
    setHtmlPreview(null);
    setEditAppContext(null);
    setAppUpdateDecision(null);
  }, [sessionId]);
  useEffect(() => {
    const preview = htmlPreview;
    const previewSessionId = preview?.sessionId ?? null;
    if (!preview || previewSessionId === null || preview.sharedApp !== null) {
      if (preview === null) setSessionSharedApp(null);
      return;
    }
    const cached = sessionSharedAppRef.current.get(previewSessionId);
    if (cached !== undefined) {
      setSessionSharedApp(cached);
      return;
    }
    let cancelled = false;
    void userApi.listApps().then((apps) => {
      if (cancelled) return;
      const match = apps
        .filter((app) => app.mine && app.sourceSessionId === previewSessionId)
        .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))[0] ?? null;
      sessionSharedAppRef.current.set(previewSessionId, match);
      setSessionSharedApp(match);
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [htmlPreview?.sessionId, htmlPreview?.sourceKey, htmlPreview?.sharedApp, htmlPreview]);
  const applyQueueSnapshot = useCallback((
    targetSessionId: string,
    requestVersion: number,
    summary: RuntimeEdgeQueueSummary
  ) => {
    if (
      activeSessionRef.current !== targetSessionId
      || !queueSnapshotVersionGateRef.current.isCurrent(
        targetSessionId,
        requestVersion
      )
    ) return;
    setQueueSummary(reconcileChatQueueSnapshot(summary));
  }, []);
  useEffect(() => {
    const updateQueue = (rawEvent: Event) => {
      const detail = (rawEvent as CustomEvent<RuntimeEdgeQueueUpdatedDetail>)
        .detail;
      if (!detail || detail.runtimeSessionId !== activeSessionRef.current) {
        return;
      }
      setQueueSummary((current) => ({
        ...current,
        queuedCount: detail.queuedCount,
        scheduledQueuedCount: detail.scheduledQueuedCount
      }));
      const workspaceId = runtime?.runtimeWorkspaceId;
      if (workspaceId === undefined) return;
      const requestVersion = queueSnapshotVersionGateRef.current.issue(
        detail.runtimeSessionId
      );
      void userApi.chatQueueSummary(workspaceId, detail.runtimeSessionId)
        .then((summary) => {
          applyQueueSnapshot(detail.runtimeSessionId, requestVersion, summary);
        })
        .catch(() => undefined);
    };
    window.addEventListener(RUNTIME_EDGE_QUEUE_UPDATED_EVENT, updateQueue);
    return () => {
      window.removeEventListener(
        RUNTIME_EDGE_QUEUE_UPDATED_EVENT,
        updateQueue
      );
    };
  }, [applyQueueSnapshot, runtime?.runtimeWorkspaceId, userApi]);
  const sessionsRef = useRef<RuntimeSessionSummary[]>([]);
  const initialLoadStartedRef = useRef(false);
  const runtimeRecoveryRef = useRef<Promise<RuntimeAwakeResult> | null>(null);
  const runtimeStartupContextEpochRef = useRef(0);
  const initialSessionRetryEpochRef = useRef(0);
  const abortBySessionRef = useRef(new Map<string, AbortController>());
  const submissionBySessionRef = useRef(new Map<string, SubmissionState>());
  const stopRequestBySessionRef = useRef(new Map<string, StopRequestState>());
  const reconnectBySessionRef = useRef(new Map<string, { key: string; controller: AbortController }>());
  const sessionLoadRef = useRef<{ key: string; promise: Promise<void> } | null>(null);
  const historyLoadEpochRef = useRef(0);
  const backgroundObserverRef = useRef<{ key: string; controller: AbortController } | null>(null);
  const backgroundRunRecoveryRef = useRef(new Set<string>());
  const previewRequestRef = useRef(0);
  const previewUrlRef = useRef<string | null>(null);
  const sessionCacheRef = useRef(new Map<string, SessionCache>());
  const artifactRefreshRef = useRef(new Map<string, Promise<void>>());
  const artifactVisibilityRetryRef = useRef(new Map<string, number>());
  const sessionPrewarmCoordinatorRef = useRef(new SessionPrewarmCoordinator());
  const sessionReadinessRef = useRef(initialSessionReadinessState);
  const queuedTurnDispatcherRef = useRef(
    new QueuedTurnDispatcher<FrozenQueuedTurnPayload>()
  );
  const sessionDraftsRef = useRef(new Map<string, string>());
  const skipImmediateSessionPrewarmRef = useRef<string | null>(null);
  const requiredPrewarmEpochRef = useRef(0);
  const requiredPrewarmGateRef = useRef<RequiredPrewarmGate | null>(null);
  const pendingAttachmentsRef = useRef(new Map<string, number[]>());
  const submissionIngressGateRef = useRef(new SessionSubmissionGate());
  const correctionControllersRef = useRef(new Map<string, AbortController>());
  const queuedSubmissionControllersRef = useRef(new Map<string, AbortController>());
  const pendingScheduledTaskObserverRef = useRef(0);
  const scheduledRecoverySessionRef = useRef<string | null>(null);
  const residentScheduledRunRef = useRef<{ sessionId: string; runtimeRunId: string; terminal: boolean } | null>(null);
  const chatTimingRef = useRef(new TurnTimingCoordinator());
  const lifecycleTimingRef = useRef(new TurnTimingCoordinator());
  const clarifyPollCoordinatorRef = useRef(new ClarifyPollCoordinator());
  const clarifyReplayInFlightRunIdsRef = useRef(new Set<string>());
  const clarifyConvergedRequestIdsRef = useRef(new Set<string>());
  const timingKeyBySessionRef = useRef(new Map<string, string>());
  const pendingVisibleCandidatesRef = useRef<PendingVisibleCandidate[]>([]);
  const paintFramesRef = useRef(new Map<TimingKind, PaintFrameState>());
  const immediateVisibleRunsRef = useRef(new Map<string, number>());
  const selfEvolutionObserversRef = useRef(
    new Map<string, SelfEvolutionObserver>()
  );
  const foregroundCriticalSessionsRef = useRef(new Set<string>());
  const messageCommitFrameRef = useRef<number | null>(null);
  const sessionActivityCoordinatorRef = useRef<SessionActivityCoordinator | null>(null);
  const sessionActivityScopeKeyRef = useRef<string | null>(null);
  const {
    messageFeedRef,
    onMessageFeedScroll,
    requestScrollToLatest,
    requestFollowLatestUnlessManual,
    prepareHistoryRefresh,
    anchorTurn,
    resumeLatest,
    turnSpacerHeight,
    showReturnToLatest
  } = useAnchoredChatScroll({
    sessionId,
    view,
    messages,
    streaming
  });
  const timingCoordinator = (kind: TimingKind) => (
    kind === "lifecycle" ? lifecycleTimingRef.current : chatTimingRef.current
  );
  const activityScopeKey = (workspaceId: number) => [
    principal?.orgId ?? 0,
    principal?.userId ?? 0,
    isApplication ? "application" : "personal",
    workspaceId
  ].join(":");
  const sessionActivityCoordinator = (workspaceId: number) => {
    const key = activityScopeKey(workspaceId);
    if (
      sessionActivityScopeKeyRef.current !== key
      || sessionActivityCoordinatorRef.current === null
    ) {
      sessionActivityScopeKeyRef.current = key;
      sessionActivityCoordinatorRef.current = new SessionActivityCoordinator({
        orgId: principal?.orgId ?? 0,
        userId: principal?.userId ?? 0,
        runtimeWorkspaceId: workspaceId
      }, window.localStorage);
    }
    return sessionActivityCoordinatorRef.current;
  };
  const reconcileSessionActivities = (
    workspaceId: number,
    nextSessions: RuntimeSessionSummary[],
    activeId: string | null
  ) => {
    const previousScope = sessionActivityScopeKeyRef.current;
    const coordinator = sessionActivityCoordinator(workspaceId);
    if (!coordinator) {
      setSessionActivities({});
      return;
    }
    setSessionActivities((current) => (
      previousScope === sessionActivityScopeKeyRef.current
        ? coordinator.reconcile(current, nextSessions, activeId)
        : coordinator.initialize(nextSessions, activeId)
    ));
  };
  const markSessionActivityRunning = (id: string, runtimeRunId: string) => {
    const coordinator = runtime ? sessionActivityCoordinator(runtime.runtimeWorkspaceId) : null;
    if (!coordinator) return;
    setSessionActivities((current) => coordinator.markRunning(current, id, runtimeRunId));
  };
  const markSessionActivityTerminal = (
    id: string,
    runtimeRunId: string,
    successful: boolean
  ) => {
    const coordinator = runtime ? sessionActivityCoordinator(runtime.runtimeWorkspaceId) : null;
    if (!coordinator) return;
    setSessionActivities((current) => coordinator.markTerminal(
      current,
      id,
      runtimeRunId,
      successful,
      activeSessionRef.current
    ));
  };
  const markSessionActivityError = (id: string, runtimeRunId: string) => {
    const coordinator = runtime ? sessionActivityCoordinator(runtime.runtimeWorkspaceId) : null;
    if (!coordinator) return;
    setSessionActivities((current) => coordinator.markError(current, id, runtimeRunId));
  };
  const chatSessions = useMemo(() => sessions.filter(visibleSession), [sessions]);
  const sidebarSessions = useMemo(() => sidebarVisibleSessions(chatSessions), [chatSessions]);
  const currentUploads = useMemo(() => uploadItems.filter((item) => item.sessionId === sessionId), [uploadItems, sessionId]);
  const activeSession = chatSessions.find((item) => item.runtimeSessionId === sessionId) ?? chatSessions[0] ?? null;
  const activeContextRoot = useMemo(
    () => contextRootSession(chatSessions, activeSession),
    [chatSessions, activeSession]
  );
  const agentNamesByProfileId = useMemo(() => {
    const names: Record<number, string> = {};
    for (const session of chatSessions) {
      if (session.runtimeAgentProfileId != null && session.agentDisplayName) {
        names[session.runtimeAgentProfileId] = session.agentDisplayName;
      }
    }
    for (const expert of installedExperts) {
      names[expert.runtimeAgentProfileId] = expert.displayName;
    }
    return names;
  }, [chatSessions, installedExperts]);
  const activeSessionReadOnly = isReadOnlySession(activeSession);
  const selectedPendingAttachments = useMemo(
    () => files.filter((file) => pendingAttachmentIds.includes(file.fileId)),
    [files, pendingAttachmentIds]
  );
  const hasImageAttachment = (
    currentUploads.some((upload) => isImageFile(upload.file))
    || selectedPendingAttachments.some((file) => file.mimeType?.startsWith("image/"))
  );
  const selectedModelSupportsVision = models.find((model) => model.modelEntryId === modelId)?.supportsVision === true;
  const imageInputSupported = !hasImageAttachment || selectedModelSupportsVision;
  const canSendWithoutText = hasImageAttachment && selectedModelSupportsVision;
  const scopedClarifyRequests = sessionId
    ? scopeClarifyRequests(
      clarifyRequests,
      sessionId,
      clarifyConvergedRequestIdsRef.current
    )
    : [];
  const renderableClarifyRequests = scopedClarifyRequests.filter(isClarifyCardRenderable);
  const activeClarifyRequest = renderableClarifyRequests.find((item) => (
    item.status === "waiting" || item.status === "claimed" || item.status === "resume_failed"
  )) ?? renderableClarifyRequests[renderableClarifyRequests.length - 1] ?? null;
  const activeProgress = activeSession ? sessionCacheRef.current.get(activeSession.runtimeSessionId)?.progress : undefined;
  useEffect(() => {
    if (isApplication || !sessionId || view !== "chat") {
      setClarifyRequests([]);
      return;
    }
    if (!runtime) return;
    const coordinator = clarifyPollCoordinatorRef.current;
    const convergedRequestIds = clarifyConvergedRequestIdsRef.current;
    const refresh = async () => {
      try {
        const ticket = coordinator.begin();
        const items = await platformUserApi.clarifyRequests(
          runtime.runtimeWorkspaceId,
          sessionId
        );
        if (coordinator.accept(ticket)) {
          setClarifyRequests((current) => mergeClarifyRequests(
            scopeClarifyRequests(current, sessionId, convergedRequestIds),
            scopeClarifyRequests(items, sessionId, convergedRequestIds)
          ));
        }
      } catch {
        // A clarify card is supplementary UI; keep an in-flight task usable
        // when its polling request is transiently unavailable.
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2_000);
    return () => {
      coordinator.invalidate();
      window.clearInterval(timer);
    };
  }, [isApplication, runtime?.runtimeWorkspaceId, sessionId, view]);
  sessionsRef.current = sessions;
  const sessionPrewarmKey = (workspaceId: number, selectedSessionId: string) => `${workspaceId}:${selectedSessionId}`;
  useEffect(() => {
    // Only show the recovery hint when the Runtime Pod is confirmed gone
    // (status=ready in DB) and the user's message has been queued for a
    // genuine cold wake. A model switch re-prewarms in under a second on
    // a warm socket, so a short delay filters it out completely.
    if (!waitingForPrewarmSend || !runtime || runtime.runtimeStatus !== "ready") {
      setRecoveryHintVisible(false);
      return;
    }
    const timer = window.setTimeout(() => {
      setRecoveryHintVisible(true);
    }, PREWARM_HINT_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [waitingForPrewarmSend, runtime?.runtimeStatus]);
  useEffect(() => {
    if (
      !runtimeAwake
      || waitingForPrewarmSend
      || !["ready", "dispatching", "streaming"].includes(sessionReadiness.status)
      || runtimeStartupContext === "none"
    ) return;
    setRuntimeStartupContext("none");
  }, [
    runtimeAwake,
    waitingForPrewarmSend,
    sessionReadiness.status,
    runtimeStartupContext
  ]);
  const ensureSessionPrewarm = (workspaceId: number, selectedSessionId: string) => (
    sessionPrewarmCoordinatorRef.current.ensure(
      sessionPrewarmKey(workspaceId, selectedSessionId),
      () => userApi.prewarmRuntimeSession(workspaceId, selectedSessionId)
    )
  );
  const selectedSessionPrewarm = (
    workspaceId: number,
    selectedSessionId: string
  ): Promise<SessionPrewarmState> => {
    const gate = requiredPrewarmGateRef.current;
    if (
      gate
      && gate.target.workspaceId === workspaceId
      && gate.target.sessionId === selectedSessionId
      && gate.status !== "failed"
    ) {
      return gate.promise;
    }
    return ensureSessionPrewarm(workspaceId, selectedSessionId);
  };
  const dispatchSessionReadiness = (event: SessionReadinessEvent) => {
    const next = reduceSessionReadiness(
      sessionReadinessRef.current,
      event
    );
    if (next === sessionReadinessRef.current) return;
    sessionReadinessRef.current = next;
    setSessionReadiness(next);
  };
  const requiredAgentIdentity = (session: RuntimeSessionSummary) => (
    session.agentProfileKind === "expert"
      ? {
          agentProfileKind: "expert" as const,
          runtimeAgentProfileId: session.runtimeAgentProfileId ?? null
        }
      : {
          agentProfileKind: "personal" as const,
          runtimeAgentProfileId: null
        }
  );
  const requiredTargetMatches = (
    gate: RequiredPrewarmGate | null,
    workspaceId: number,
    session: RuntimeSessionSummary,
    selectedModelId: number | null
  ) => {
    if (!gate) return false;
    const identity = requiredAgentIdentity(session);
    return gate.target.workspaceId === workspaceId
      && gate.target.sessionId === session.runtimeSessionId
      && gate.target.modelId === selectedModelId
      && gate.target.agentProfileKind === identity.agentProfileKind
      && gate.target.runtimeAgentProfileId === identity.runtimeAgentProfileId;
  };
  const clearRequiredPrewarm = () => {
    requiredPrewarmEpochRef.current += 1;
    requiredPrewarmGateRef.current = null;
    dispatchSessionReadiness({ type: "cleared" });
  };
  const bindRequiredPrewarmOperation = (
    gate: RequiredPrewarmGate,
    request: () => Promise<unknown>
  ): Promise<SessionPrewarmState> => {
    gate.status = "pending";
    gate.receipt = null;
    setRuntimeConnectionState("checking");
    let operation: Promise<unknown>;
    try {
      operation = request();
    } catch (reason) {
      operation = Promise.reject(reason);
    }
    gate.promise = operation.then(
      (value) => {
        const receipt = asSessionPrewarmState(value);
        gate.receipt = receipt;
        gate.status = "ready";
        dispatchSessionReadiness({
          type: "prewarm_ready",
          target: gate.target,
          receipt
        });
        if (
          requiredPrewarmGateRef.current === gate
          && activeSessionRef.current === gate.target.sessionId
        ) {
          setRuntimeConnectionState("ready");
        }
        return receipt;
      },
      (reason: unknown) => {
        gate.status = "failed";
        dispatchSessionReadiness({
          type: "prewarm_retry_wait",
          target: gate.target,
          error: reason instanceof Error
            ? reason.message
            : "会话预热失败"
        });
        if (
          requiredPrewarmGateRef.current === gate
          && activeSessionRef.current === gate.target.sessionId
        ) {
          setRuntimeConnectionState("connecting");
        }
        throw reason;
      }
    );
    return gate.promise;
  };
  const beginRequiredPrewarm = (
    workspaceId: number,
    session: RuntimeSessionSummary,
    selectedModelId: number | null,
    request: () => Promise<unknown> = () => ensureSessionPrewarm(
      workspaceId,
      session.runtimeSessionId
    )
  ): RequiredPrewarmGate | null => {
    if (isApplication || isReadOnlySession(session)) {
      clearRequiredPrewarm();
      return null;
    }
    const identity = requiredAgentIdentity(session);
    const gate: RequiredPrewarmGate = {
      target: {
        workspaceId,
        sessionId: session.runtimeSessionId,
        modelId: selectedModelId,
        agentProfileKind: identity.agentProfileKind,
        runtimeAgentProfileId: identity.runtimeAgentProfileId,
        epoch: requiredPrewarmEpochRef.current + 1
      },
      status: "pending",
      receipt: null,
      promise: Promise.resolve({ state: "ready" })
    };
    requiredPrewarmEpochRef.current = gate.target.epoch;
    requiredPrewarmGateRef.current = gate;
    dispatchSessionReadiness({
      type: "prewarm_started",
      target: gate.target
    });
    bindRequiredPrewarmOperation(gate, request);
    return gate;
  };
  const rearmSessionSendReady = async (workspaceId: number, selectedSessionId: string) => {
    if (isApplication || activeSessionRef.current !== selectedSessionId) {
      return;
    }
    const selectedSession = sessionsRef.current.find(
      (item) => item.runtimeSessionId === selectedSessionId
    );
    if (!selectedSession || isReadOnlySession(selectedSession)) return;
    sessionPrewarmCoordinatorRef.current.invalidate(
      sessionPrewarmKey(workspaceId, selectedSessionId)
    );
    const gate = beginRequiredPrewarm(
      workspaceId,
      selectedSession,
      selectedSession.sessionModelEntryId ?? modelId,
      () => ensureSessionPrewarm(workspaceId, selectedSessionId)
    );
    if (!gate) return;
    try {
      await gate.promise;
      if (
        activeSessionRef.current === selectedSessionId
        && requiredPrewarmGateRef.current === gate
        && gate.status === "ready"
      ) {
        setRuntimeConnectionState("ready");
      }
    } catch {
      if (
        activeSessionRef.current === selectedSessionId
        && requiredPrewarmGateRef.current === gate
      ) {
        setRuntimeConnectionState("connecting");
      }
    }
  };
  const waitForRequiredPrewarmWithRetry = async (
    gate: RequiredPrewarmGate,
    controller: AbortController
  ): Promise<SessionPrewarmState> => {
    const startedAt = performance.now();
    let retryIndex = 0;
    while (true) {
      try {
        return await gate.promise;
      } catch (reason) {
        const terminal = [403, 404, 410, 422].some(
          (statusCode) => hasHttpStatus(reason, statusCode)
        );
        if (
          terminal
          || controller.signal.aborted
          || requiredPrewarmGateRef.current !== gate
          || performance.now() - startedAt
            >= REQUIRED_PREWARM_RETRY_WINDOW_MS
        ) {
          dispatchSessionReadiness({
            type: "prewarm_failed",
            target: gate.target,
            error: reason instanceof Error
              ? reason.message
              : "会话预热失败"
          });
          throw reason;
        }
        const delay = REQUIRED_PREWARM_RETRY_DELAYS_MS[
          Math.min(
            retryIndex,
            REQUIRED_PREWARM_RETRY_DELAYS_MS.length - 1
          )
        ];
        retryIndex += 1;
        await new Promise<void>((resolve, reject) => {
          const onAbort = () => {
            window.clearTimeout(timer);
            reject(new DOMException("Aborted", "AbortError"));
          };
          const timer = window.setTimeout(() => {
            controller.signal.removeEventListener("abort", onAbort);
            resolve();
          }, delay);
          controller.signal.addEventListener("abort", onAbort, {
            once: true
          });
        });
        if (
          controller.signal.aborted
          || requiredPrewarmGateRef.current !== gate
        ) {
          throw new DOMException("Aborted", "AbortError");
        }
        const key = sessionPrewarmKey(
          gate.target.workspaceId,
          gate.target.sessionId
        );
        sessionPrewarmCoordinatorRef.current.invalidate(key);
        bindRequiredPrewarmOperation(
          gate,
          () => ensureSessionPrewarm(
            gate.target.workspaceId,
            gate.target.sessionId
          )
        );
      }
    }
  };
  const cancelMessageCommitFrame = () => {
    if (messageCommitFrameRef.current === null) return;
    window.cancelAnimationFrame(messageCommitFrameRef.current);
    messageCommitFrameRef.current = null;
  };
  const scheduleMessageCommitFrame = () => {
    if (messageCommitFrameRef.current !== null) return;
    messageCommitFrameRef.current = window.requestAnimationFrame(() => {
      messageCommitFrameRef.current = null;
      const activeId = activeSessionRef.current;
      if (!activeId) return;
      const cached = sessionCacheRef.current.get(activeId);
      if (cached) setMessages(cached.messages);
    });
  };
  useLayoutEffect(() => {
    const feed = messageFeedRef.current;
    if (!feed || !sessionId) return;
    for (const timingKind of ["lifecycle", "content"] as const) {
      if (paintFramesRef.current.has(timingKind)) continue;
      const candidates = pendingVisibleCandidatesRef.current;
      let selectedIndex = -1;
      let selectedNode: HTMLElement | undefined;
      for (let index = 0; index < candidates.length; index += 1) {
        const candidate = candidates[index];
        const coordinator = timingCoordinator(candidate.timingKind);
        if (
          candidate.timingKind !== timingKind
          || candidate.sessionId !== sessionId
          || !coordinator.has(candidate.runtimeRunId)
        ) continue;
        const node = Array.from(document.querySelectorAll<HTMLElement>("[data-message-id]"))
          .find((item) => item.dataset.messageId === candidate.targetId);
        if (!node || node.getClientRects().length === 0) continue;
        selectedIndex = index;
        selectedNode = node;
        break;
      }
      if (selectedIndex < 0 || !selectedNode) continue;
      const candidate = candidates[selectedIndex];
      pendingVisibleCandidatesRef.current = candidates.filter(
        (_item, index) => index !== selectedIndex
      );
      const visibility = currentDocumentVisibility();
      const coordinator = timingCoordinator(timingKind);
      if (!coordinator.offerVisibleCandidate(
        candidate.runtimeRunId,
        candidate.targetId,
        candidate.receivedAt,
        visibility
      )) continue;
      if (!coordinator.markVisibleCommit(
        candidate.runtimeRunId,
        candidate.targetId,
        performance.now(),
        visibility
      )) continue;

      const targetNode = selectedNode;
      const firstFrame = window.requestAnimationFrame(() => {
        const current = paintFramesRef.current.get(timingKind);
        if (
          !current
          || current.runtimeRunId !== candidate.runtimeRunId
          || current.targetId !== candidate.targetId
        ) return;
        if (
          currentDocumentVisibility() !== "visible"
          || !targetNode.isConnected
          || targetNode.getClientRects().length === 0
        ) {
          coordinator.cancel(candidate.runtimeRunId);
          paintFramesRef.current.delete(timingKind);
          return;
        }
        current.secondFrame = window.requestAnimationFrame(() => {
          const latest = paintFramesRef.current.get(timingKind);
          if (
            !latest
            || latest.runtimeRunId !== candidate.runtimeRunId
            || latest.targetId !== candidate.targetId
          ) return;
          paintFramesRef.current.delete(timingKind);
          const payload = coordinator.markVisiblePaint(
            candidate.runtimeRunId,
            candidate.targetId,
            performance.now(),
            currentDocumentVisibility()
          );
          if (!payload) return;
          if (
            timingKind === "content"
            && timingKeyBySessionRef.current.get(candidate.sessionId) === candidate.runtimeRunId
          ) {
            timingKeyBySessionRef.current.delete(candidate.sessionId);
          }
          if (timingKind === "content") {
            foregroundCriticalSessionsRef.current.delete(
              candidate.sessionId
            );
            resumeSelfEvolutionObserver(candidate.sessionId);
          }
          void userApi.reportChatClientTiming(candidate.runtimeRunId, {
            ...payload,
            sampleKind: timingKind
          });
        });
      });
      paintFramesRef.current.set(timingKind, {
        sessionId: candidate.sessionId,
        runtimeRunId: candidate.runtimeRunId,
        targetId: candidate.targetId,
        timingKind,
        firstFrame
      });
    }
  }, [messages, sessionId]);
  useEffect(() => () => {
    cancelMessageCommitFrame();
    for (const frame of paintFramesRef.current.values()) {
      window.cancelAnimationFrame(frame.firstFrame);
      if (frame.secondFrame !== undefined) window.cancelAnimationFrame(frame.secondFrame);
    }
    paintFramesRef.current.clear();
    pendingVisibleCandidatesRef.current = [];
    timingKeyBySessionRef.current.clear();
    chatTimingRef.current.clear();
    lifecycleTimingRef.current.clear();
    sessionPrewarmCoordinatorRef.current.clear();
    queuedTurnDispatcherRef.current.clear();
    sessionDraftsRef.current.clear();
    clearRequiredPrewarm();
    immediateVisibleRunsRef.current.clear();
    for (
      const observer
      of selfEvolutionObserversRef.current.values()
    ) {
      if (observer.timer !== undefined) {
        window.clearTimeout(observer.timer);
      }
    }
    selfEvolutionObserversRef.current.clear();
    foregroundCriticalSessionsRef.current.clear();
    for (const controller of abortBySessionRef.current.values()) {
      controller.abort();
    }
    abortBySessionRef.current.clear();
    for (const controller of correctionControllersRef.current.values()) {
      controller.abort();
    }
    correctionControllersRef.current.clear();
    for (const controller of queuedSubmissionControllersRef.current.values()) {
      controller.abort();
    }
    queuedSubmissionControllersRef.current.clear();
    submissionBySessionRef.current.clear();
    stopRequestBySessionRef.current.clear();
    reconnectBySessionRef.current.clear();
  }, []);
  const replaceSessionCache = (id: string, next: SessionCache) => {
    sessionCacheRef.current.set(id, next);
    if (activeSessionRef.current === id) setMessages(next.messages);
  };
  const cancelWaitingSubmission = (submission: SubmissionState) => {
    const cancelled = queuedTurnDispatcherRef.current.cancel(
      submission.turnId
    );
    if (cancelled) {
      sessionDraftsRef.current.set(
        submission.sessionId,
        cancelled.payload.content
      );
      if (activeSessionRef.current === submission.sessionId) {
        setInput(cancelled.payload.content);
      }
    }
    submission.controller.abort();
    chatTimingRef.current.cancel(submission.turnId);
    lifecycleTimingRef.current.cancel(submission.turnId);
    if (timingKeyBySessionRef.current.get(submission.sessionId) === submission.turnId) {
      timingKeyBySessionRef.current.delete(submission.sessionId);
    }
    const cached = sessionCacheRef.current.get(submission.sessionId) ?? {
      messages: [],
      files: []
    };
    replaceSessionCache(
      submission.sessionId,
      cancelPendingChatTurn(cached, submission.turnId)
    );
    if (submissionBySessionRef.current.get(submission.sessionId) === submission) {
      submissionBySessionRef.current.delete(submission.sessionId);
    }
    if (abortBySessionRef.current.get(submission.sessionId) === submission.controller) {
      abortBySessionRef.current.delete(submission.sessionId);
    }
    if (activeSessionRef.current === submission.sessionId) {
      setStreaming(false);
      setStopping(false);
      setRunId(null);
    }
    dispatchSessionReadiness({ type: "cleared" });
  };
  const replaceSessionFiles = (id: string, nextFiles: ChatFileSummary[]) => {
    const cached = sessionCacheRef.current.get(id) ?? { messages: [], files: [] };
    const nextMessages = attachGeneratedImageArtifacts(cached.messages, nextFiles);
    sessionCacheRef.current.set(id, { ...cached, messages: nextMessages, files: nextFiles });
    if (activeSessionRef.current === id) {
      setMessages(nextMessages);
      setFiles(nextFiles);
    }
  };
  const shouldAutoOpenHtmlPreview = (targetSessionId: string, auto: boolean) => (
    !auto || activeSessionRef.current === targetSessionId
  );
  const openHtmlPreview = (
    targetSessionId: string,
    messageId: string,
    html: string,
    sourceRunId: string | null,
    sourceKey: string,
    sourceName: string,
    auto = false,
    packageSource: HtmlPreviewState["packageSource"] = null
  ): HtmlPreviewContent | null => {
    // Automatic previews belong to the session that discovered the artifact.
    // A late response from another session must not steal the current chat.
    if (!shouldAutoOpenHtmlPreview(targetSessionId, auto)) return null;
    // 关闭只屏蔽「同一份内容」的自动重开；内容更新后签名变化，预览再次弹出。
    const signature = htmlPreviewSignature(sourceKey, html);
    if (auto && htmlPreviewDismissedRef.current.get(targetSessionId) === signature) return null;
    const knownVersion = htmlPreviewSourceVersionsRef.current.get(sourceKey);
    const maxVersion = Math.max(
      0,
      ...htmlPreviewSourceVersionsRef.current.values()
    );
    const version = !auto && knownVersion !== undefined
      ? knownVersion
      : Math.max(1, maxVersion + 1);
    htmlPreviewSourceVersionsRef.current.set(sourceKey, version);
    htmlPreviewDismissedRef.current.delete(targetSessionId);
    setHtmlPreview({
      sessionId: targetSessionId,
      messageId,
      sourceRunId,
      sourceKey,
      sourceName,
      html,
      version,
      packageSource,
      sharedApp: null
    });
    requestScrollToLatest();
    return { html, version };
  };
  const openHtmlPreviewFromMessage = (
    targetSessionId: string,
    message: ChatMessage,
    auto = false
  ) => {
    const html = extractHtmlArtifacts(message.content || "").at(-1);
    if (!html) return null;
    return openHtmlPreview(
      targetSessionId,
      message.id,
      html,
      message.runtimeRunId ?? null,
      `${targetSessionId}:message:${message.id}`,
      htmlDocumentTitle(html) ?? "HTML 产物",
      auto
    );
  };
  const openFilesPaneForSession = (id: string) => {
    if (activeSessionRef.current !== id) return;
    localStorage.setItem("hermes.files-pane", "true");
    setFilesOpen(true);
  };

  const openCloudHtmlFilePreview = async (
    targetSessionId: string,
    file: ChatFileSummary,
    auto = true
  ): Promise<HtmlPreviewContent | null> => {
    if (!shouldAutoOpenHtmlPreview(targetSessionId, auto)) return null;
    if (file.sizeBytes > MAX_HTML_PREVIEW_BYTES) {
      setError(`HTML 文件「${file.displayName}」超过 16 MB，已保留在任务文件中，请下载查看。`);
      return null;
    }
    try {
      const blob = await userApi.downloadChatFile(file.fileId);
      if (blob.size > MAX_HTML_PREVIEW_BYTES) {
        setError(`HTML 文件「${file.displayName}」超过 16 MB，已保留在任务文件中，请下载查看。`);
        return null;
      }
      const html = await readBlobText(blob);
      const sourceKey = `${targetSessionId}:cloud-file:${file.fileId}`;
      openHtmlPreview(
        targetSessionId,
        `html-file-${file.fileId}`,
        html,
        file.runtimeRunId ?? null,
        sourceKey,
        file.displayName,
        auto
      );
      return {
        html,
        version: htmlPreviewSourceVersionsRef.current.get(sourceKey) ?? 1
      };
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "HTML 文件预览失败，请稍后重试。");
      return null;
    }
  };

  const refreshSessionFiles = (workspaceId: number, id: string, openPane = false): Promise<void> => {
    const inFlight = artifactRefreshRef.current.get(id);
    if (inFlight) return inFlight;
    const refresh = userApi.chatFiles(workspaceId, id)
      .then((nextFiles) => {
        const previousFiles = sessionCacheRef.current.get(id)?.files ?? [];
        const discoveredFiles = hasNewFiles(previousFiles, nextFiles);
        replaceSessionFiles(id, nextFiles);
        if (discoveredFiles && !isApplication) {
          const knownFileIds = new Set(previousFiles.map((file) => file.fileId));
          const htmlFile = nextFiles
            .filter((file) => file.sourceType === "runtime" && isHtmlChatFile(file))
            .filter((file) => !knownFileIds.has(file.fileId))
            .sort((a, b) => b.fileId - a.fileId)[0];
          if (htmlFile) void openCloudHtmlFilePreview(id, htmlFile);
        }
        if (openPane && discoveredFiles) openFilesPaneForSession(id);
      })
      .catch(() => undefined)
      .finally(() => artifactRefreshRef.current.delete(id));
    artifactRefreshRef.current.set(id, refresh);
    return refresh;
  };
  const observeArtifactEvent = (
    workspaceId: number,
    id: string,
    event: { event: string; data: Record<string, unknown> }
  ) => {
    if (!ARTIFACT_EVENTS.has(event.event)) return;
    const relativePath = typeof event.data.relative_path === "string"
      ? event.data.relative_path
      : null;
    if (!relativePath) {
      void refreshSessionFiles(workspaceId, id, event.event === "chat.artifact.created");
      return;
    }
    const key = `${id}:${relativePath}`;
    const epoch = (artifactVisibilityRetryRef.current.get(key) ?? 0) + 1;
    artifactVisibilityRetryRef.current.set(key, epoch);
    const expectPresent = event.event !== "chat.artifact.deleted";
    const retry = async (attempt: number): Promise<void> => {
      await refreshSessionFiles(
        workspaceId,
        id,
        event.event === "chat.artifact.created"
      );
      if (artifactVisibilityRetryRef.current.get(key) !== epoch) return;
      const files = sessionCacheRef.current.get(id)?.files ?? [];
      const visible = files.some((file) => file.relativePath === relativePath);
      if (visible === expectPresent || attempt >= ARTIFACT_INDEX_RETRY_DELAYS_MS.length) {
        if (artifactVisibilityRetryRef.current.get(key) === epoch) {
          artifactVisibilityRetryRef.current.delete(key);
        }
        return;
      }
      window.setTimeout(() => void retry(attempt + 1), ARTIFACT_INDEX_RETRY_DELAYS_MS[attempt]);
    };
    void retry(0);
  };
  const projectSessionRunEvent = (
    id: string,
    runtimeRunId: string,
    event: { event: string; data: Record<string, unknown>; ttftMs?: number },
    receivedAt = performance.now()
  ) => {
    const cached = sessionCacheRef.current.get(id) ?? { messages: [], files: [] };
    const result = projectChatRunEvent(cached, runtimeRunId, event);
    const timingKind = result.visibleMutation?.kind === "lifecycle" ? "lifecycle" : "content";
    const coordinator = timingCoordinator(timingKind);
    if (
      result.visibleMutation
      && activeSessionRef.current === id
      && coordinator.has(runtimeRunId)
      && !pendingVisibleCandidatesRef.current.some((candidate) => (
        candidate.runtimeRunId === runtimeRunId
        && candidate.targetId === result.visibleMutation?.targetId
        && candidate.timingKind === timingKind
      ))
    ) {
      pendingVisibleCandidatesRef.current.push({
        sessionId: id,
        runtimeRunId,
        targetId: result.visibleMutation.targetId,
        receivedAt,
        timingKind
      });
    }
    if (result.applied) {
      const immediateKey = `${id}:${runtimeRunId}:${timingKind}`;
      const useImmediate = chatPerformanceFlags.firstVisibleImmediate
        && activeSessionRef.current === id
        && result.visibleMutation !== undefined
        && coordinator.has(runtimeRunId)
        && !immediateVisibleRunsRef.current.has(immediateKey);
      if (useImmediate) {
        cancelMessageCommitFrame();
        immediateVisibleRunsRef.current.set(immediateKey, performance.now());
        if (immediateVisibleRunsRef.current.size > 128) {
          const oldest = immediateVisibleRunsRef.current.keys().next().value;
          if (oldest !== undefined) immediateVisibleRunsRef.current.delete(oldest);
        }
        sessionCacheRef.current.set(id, result.cache);
        flushSync(() => setMessages(result.cache.messages));
      } else if (
        isFrameCommittedDelta(event.event)
        && chatPerformanceFlags.deltaFrameBatching
      ) {
        sessionCacheRef.current.set(id, result.cache);
        if (activeSessionRef.current === id) scheduleMessageCommitFrame();
      } else {
        if (activeSessionRef.current === id) cancelMessageCommitFrame();
        replaceSessionCache(id, result.cache);
      }
      if (event.event === "chat.done") {
        const status = String(event.data.status ?? "completed").toLowerCase();
        markSessionActivityTerminal(
          id,
          runtimeRunId,
          !CANCELED_DONE_STATUSES.has(status) && status !== "failed" && status !== "error"
        );
      } else if (event.event === "chat.error") {
        markSessionActivityError(id, runtimeRunId);
      } else if (result.cache.streaming) {
        markSessionActivityRunning(id, runtimeRunId);
      }
    }
    return result;
  };
  const recoverSession = async (
    workspaceId: number,
    id: string,
    knownActive?: ActiveChatRunResponse,
    options?: RecoveryOptions
  ): Promise<ReconnectOutcome | false> => {
    const cached = sessionCacheRef.current.get(id);
    const active = knownActive ?? (
      options?.preferredRuntimeRunId
        ? { run: null }
        : await userApi.activeChatRun(workspaceId, id)
    );
    if (activeSessionRef.current !== id) return false;
    const recoverableRun = isRecoverableRun(active.run) ? active.run : null;
    const detachedRunId = cached?.streaming
      ? cached.runId ?? cached.activeTurn?.runtimeRunId
      : undefined;
    const runtimeRunId = options?.preferredRuntimeRunId ?? recoverableRun?.runtimeRunId ?? detachedRunId;
    if (!runtimeRunId) return false;
    const submission = submissionBySessionRef.current.get(id);
    if (submission?.sessionId === id && !submission.controller.signal.aborted) return options?.scheduled ? "cancelled" : "terminal";
    const reconnectKey = `${id}:${runtimeRunId}`;
    const existingReconnect = reconnectBySessionRef.current.get(id);
    if (existingReconnect?.key === reconnectKey && !existingReconnect.controller.signal.aborted) return options?.scheduled ? "cancelled" : "terminal";
    const controller = new AbortController();
    reconnectBySessionRef.current.set(id, { key: reconnectKey, controller });
    chatTimingRef.current.markReconnect(runtimeRunId);
    lifecycleTimingRef.current.markReconnect(runtimeRunId);
    abortBySessionRef.current.set(id, controller);
    const cachedRunMatches = cached?.runId === runtimeRunId || cached?.activeTurn?.runtimeRunId === runtimeRunId;
    const afterSeq = cachedRunMatches ? cached?.lastSeq ?? 0 : 0;
    replaceSessionCache(id, ensureActiveChatRun(cached ?? { messages: [], files: [] }, runtimeRunId, afterSeq, recoverableRun?.source));
    setStreaming(true); setRunId(runtimeRunId);
    markSessionActivityRunning(id, runtimeRunId);
    requestFollowLatestUnlessManual();
    let sawTerminalEvent = false;
    let timedOut = false;
    const timeout = options?.scheduled && options.timeoutMs !== undefined
      ? window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, options.timeoutMs)
      : undefined;
    try {
      await userApi.reconnectChatRun({
        runtimeRunId,
        afterSeq,
        runtimeWorkspaceId: workspaceId,
        runtimeSessionId: id
      }, (event) => {
        const receivedAt = performance.now();
        chatTimingRef.current.markFirstEvent(runtimeRunId, receivedAt);
        lifecycleTimingRef.current.markFirstEvent(runtimeRunId, receivedAt);
        if (activeSessionRef.current !== id) return;
        if (event.event === "chat.done" || event.event === "chat.error") sawTerminalEvent = true;
        observeArtifactEvent(workspaceId, id, event);
        projectSessionRunEvent(id, runtimeRunId, event, receivedAt);
        if (event.event === "chat.done") {
          const pendingStop = stopRequestBySessionRef.current.get(id);
          if (
            pendingStop?.sessionId === id
            && (!pendingStop.runtimeRunId || pendingStop.runtimeRunId === runtimeRunId)
          ) {
            stopRequestBySessionRef.current.delete(id);
            setStopping(false);
          }
          if (CANCELED_DONE_STATUSES.has(String(event.data.status ?? "").toLowerCase())) {
            const cachedAfterDone = sessionCacheRef.current.get(id) ?? { messages: [], files: [] };
            replaceSessionCache(id, cancelChatTurn(cachedAfterDone, runtimeRunId));
            void refreshSessionHistory(workspaceId, id);
          }
        }
      }, controller.signal);
      if (!sawTerminalEvent) {
        if (!options?.scheduled) setError(CHAT_TRANSPORT_RECOVERY_MESSAGE);
        return "retryable";
      }
      return "terminal";
    } catch {
      if (options?.scheduled) {
        if (timedOut || !controller.signal.aborted) return "retryable";
        return "cancelled";
      }
      if (!controller.signal.aborted) {
        setError(CHAT_TRANSPORT_RECOVERY_MESSAGE);
        return "retryable";
      }
      return "cancelled";
    } finally {
      if (timeout !== undefined) window.clearTimeout(timeout);
      if (activeSessionRef.current === id && abortBySessionRef.current.get(id) === controller) {
        abortBySessionRef.current.delete(id);
        const next = sessionCacheRef.current.get(id);
        setStreaming(Boolean(next?.streaming));
        setRunId(next?.runId ?? null);
      }
      if (reconnectBySessionRef.current.get(id)?.controller === controller) {
        reconnectBySessionRef.current.delete(id);
      }
    }
  };

  const refreshSessionHistory = async (
    workspaceId: number,
    id: string,
    openPaneOnNewFiles = false
  ) => {
    const [historyResult, filesResult] = await Promise.allSettled([
      isApplication
        ? userApi.chatHistory(workspaceId, id)
        : selectedSessionPrewarm(workspaceId, id).then(
            () => userApi.chatHistoryAuthoritative(workspaceId, id)
          ),
      userApi.chatFiles(workspaceId, id)
    ]);
    const cached = sessionCacheRef.current.get(id) ?? { messages: [], files: [] };
    let next = cached;
    if (historyResult.status === "fulfilled") {
      const indexedFiles = filesResult.status === "fulfilled"
        ? filesResult.value
        : cached.files;
      const messages = reconcileSessionHistory(
        historyMessages(historyResult.value.messages, undefined, indexedFiles),
        cached.messages,
        cached.activeTurn
      );
      const historyCaughtUp = messages !== cached.messages;
      next = {
        ...next,
        messages,
        ...(historyCaughtUp && cached.activeTurn?.status === "completed" ? { activeTurn: undefined } : {})
      };
    }
    const discoveredFiles = filesResult.status === "fulfilled"
      && hasNewFiles(cached.files, filesResult.value);
    if (filesResult.status === "fulfilled") next = { ...next, files: filesResult.value };
    sessionCacheRef.current.set(id, next);
    if (activeSessionRef.current === id) {
      prepareHistoryRefresh();
      setMessages(next.messages);
      setFiles(next.files);
    }
    if (openPaneOnNewFiles && discoveredFiles) openFilesPaneForSession(id);
  };

  const answerClarify = async (request: RuntimeClarifyRequest, answer: RuntimeClarifyAnswer) => {
    if (!runtime || clarifyBusyId) return;
    setClarifyBusyId(request.requestId);
    setError(null);
    try {
      const updated = await platformUserApi.answerClarifyRequest(
        runtime.runtimeWorkspaceId,
        request.requestId,
        answer,
        request.version
      );
      if (activeSessionRef.current !== updated.runtimeSessionId) return;
      setClarifyRequests((items) => mergeClarifyRequests(
        scopeClarifyRequests(
          items,
          updated.runtimeSessionId,
          clarifyConvergedRequestIdsRef.current
        ),
        [updated]
      ));
    } catch (reason) {
      if (activeSessionRef.current !== request.runtimeSessionId) return;
      setError(reason instanceof Error ? reason.message : "提交确认失败");
    } finally {
      setClarifyBusyId(null);
    }
  };

  useEffect(() => {
    if (isApplication || !runtime || !sessionId || view !== "chat") return;
    const replay = findClarifyContinuationReplay(
      scopedClarifyRequests,
      clarifyReplayInFlightRunIdsRef.current
    );
    if (!replay) return;
    clarifyReplayInFlightRunIdsRef.current.add(replay.runtimeRunId);
    const workspaceId = runtime.runtimeWorkspaceId;
    void (async () => {
      // One bounded replay attempt preserves the streaming experience while
      // the continuation still runs. Whatever the transport returns, the card
      // converges afterwards: the previous retry loop could pin the session
      // in streaming state forever when a replay stream never delivered a
      // terminal event, leaving the user unable to send new input.
      try {
        // The list API returns the deterministic continuation ID. Attach its
        // replay immediately so a fast-completing continuation is not lost.
        await recoverSession(workspaceId, sessionId, undefined, {
          preferredRuntimeRunId: replay.runtimeRunId
        });
      } catch (reason) {
        // Transport recovery already surfaced its own error; history remains
        // the authoritative fallback below.
      }
      try {
        await refreshSessionHistory(workspaceId, sessionId, true);
      } catch {
        // A failed refresh must never keep the answered card mounted.
      }
      clarifyConvergedRequestIdsRef.current.add(replay.requestId);
      setClarifyRequests((items) => items.filter((item) => (
        item.requestId !== replay.requestId
      )));
      clarifyReplayInFlightRunIdsRef.current.delete(replay.runtimeRunId);
    })();
  }, [clarifyRequests, isApplication, runtime, sessionId, view]);

  function scheduleSelfEvolutionObserver(
    observer: SelfEvolutionObserver,
    delayMs: number
  ) {
    if (observer.timer !== undefined) return;
    observer.timer = window.setTimeout(() => {
      observer.timer = undefined;
      void pollSelfEvolutionObserver(observer);
    }, delayMs);
  }

  async function pollSelfEvolutionObserver(
    observer: SelfEvolutionObserver
  ): Promise<void> {
    if (
      observer.polling
      || selfEvolutionObserversRef.current.get(
        observer.sessionId
      ) !== observer
    ) return;
    if (activeSessionRef.current !== observer.sessionId) {
      selfEvolutionObserversRef.current.delete(observer.sessionId);
      return;
    }
    if (
      foregroundCriticalSessionsRef.current.has(
        observer.sessionId
      )
    ) {
      scheduleSelfEvolutionObserver(observer, 250);
      return;
    }
    const next = observer.attemptsByRun.entries().next().value as
      | [string, number]
      | undefined;
    if (!next) {
      selfEvolutionObserversRef.current.delete(observer.sessionId);
      return;
    }
    const [runtimeRunId, attempts] = next;
    if (attempts >= SELF_EVOLUTION_POLL_MAX_ATTEMPTS) {
      observer.attemptsByRun.delete(runtimeRunId);
      scheduleSelfEvolutionObserver(observer, 0);
      return;
    }
    observer.attemptsByRun.set(runtimeRunId, attempts + 1);
    observer.polling = true;
    let finished = false;
    try {
      const activity = await userApi.runActivity(
        observer.workspaceId,
        observer.sessionId,
        runtimeRunId
      );
      const reviewEvents = activity.events.filter(
        (event) => event.event === "chat.self_evolution.event"
      );
      for (const event of reviewEvents) {
        projectSessionRunEvent(
          observer.sessionId,
          runtimeRunId,
          event
        );
      }
      const last = reviewEvents[reviewEvents.length - 1];
      const status = typeof last?.data.status === "string"
        ? last.data.status
        : "";
      finished = (
        status === "completed"
        || status === "failed"
        || status === "expired"
        || (!last && attempts + 1 >= 20)
      );
      if (
        status === "completed"
        || status === "failed"
        || status === "expired"
      ) {
        void refreshSessionHistory(
          observer.workspaceId,
          observer.sessionId
        );
      }
    } catch (reason) {
      finished = (
        hasHttpStatus(reason, 404)
        || hasHttpStatus(reason, 410)
      );
    } finally {
      observer.polling = false;
    }
    if (finished) observer.attemptsByRun.delete(runtimeRunId);
    scheduleSelfEvolutionObserver(
      observer,
      finished ? 0 : SELF_EVOLUTION_POLL_INTERVAL_MS
    );
  }

  function resumeSelfEvolutionObserver(sessionId: string) {
    const observer = selfEvolutionObserversRef.current.get(
      sessionId
    );
    if (observer) scheduleSelfEvolutionObserver(observer, 0);
  }

  const observeSelfEvolution = (
    workspaceId: number,
    id: string,
    runtimeRunId: string
  ) => {
    let observer = selfEvolutionObserversRef.current.get(id);
    if (!observer) {
      observer = {
        workspaceId,
        sessionId: id,
        attemptsByRun: new Map(),
        polling: false
      };
      selfEvolutionObserversRef.current.set(id, observer);
    }
    if (!observer.attemptsByRun.has(runtimeRunId)) {
      observer.attemptsByRun.set(runtimeRunId, 0);
    }
    scheduleSelfEvolutionObserver(observer, 0);
  };

  const loadColdSession = async (workspaceId: number, id: string) => {
    if (activeSessionRef.current === id) setHistorySyncStatus("idle");
    const cached = sessionCacheRef.current.get(id) ?? { messages: [], files: [] };
    if (activeSessionRef.current === id) {
      setMessages(cached.messages);
      setFiles(cached.files);
    }
    const [historyResult, filesResult] = await Promise.allSettled([
      isApplication
        ? userApi.chatHistory(workspaceId, id)
        : userApi.chatHistoryPreview(workspaceId, id),
      userApi.chatFiles(workspaceId, id)
    ]);
    if (activeSessionRef.current !== id) return;
    let next = sessionCacheRef.current.get(id) ?? cached;
    if (historyResult.status === "fulfilled") {
      const indexedFiles = filesResult.status === "fulfilled"
        ? filesResult.value
        : next.files;
      next = {
        ...next,
        messages: reconcileSessionHistory(
          historyMessages(historyResult.value.messages, undefined, indexedFiles),
          next.messages,
          next.activeTurn
        )
      };
    }
    if (filesResult.status === "fulfilled") next = { ...next, files: filesResult.value };
    sessionCacheRef.current.set(id, next);
    setMessages(next.messages);
    setFiles(next.files);
    if (
      filesResult.status === "fulfilled"
      && hasNewFiles(cached.files, filesResult.value)
    ) {
      openFilesPaneForSession(id);
    }
  };

  const loadSession = async (workspaceId: number, id: string) => {
    // The scheduled-task observer owns active-run detection for its target.
    // Letting this loader inspect it as well races the observer and can open two reconnects.
    if (pendingScheduledTask?.sessionId === id || scheduledRecoverySessionRef.current === id) {
      // A trigger is accepted before its user message is durable.  Reloading
      // history here would replace the local scheduled turn with the previous
      // snapshot and make the task appear to have been sent in the background.
      return;
    }
    if (isReadOnlySession(sessionsRef.current.find((item) => item.runtimeSessionId === id))) {
      const [historyResult, filesResult] = await Promise.allSettled([
        isApplication
          ? userApi.chatHistory(workspaceId, id)
          : userApi.chatHistoryPreview(workspaceId, id),
        userApi.chatFiles(workspaceId, id)
      ]);
      if (activeSessionRef.current !== id) return;
      if (historyResult.status === "rejected") throw historyResult.reason;
      const history = historyResult.value;
      const indexedFiles = filesResult.status === "fulfilled" ? filesResult.value : [];
      const next = {
        messages: historyMessages(history.messages, undefined, indexedFiles),
        files: indexedFiles
      };
      sessionCacheRef.current.set(id, next);
      setMessages(next.messages);
      setFiles(next.files);
      setHistorySyncStatus("idle");
      setError(null);
      if (indexedFiles.length > 0) openFilesPaneForSession(id);
      return;
    }
    if (isApplication) {
      const cached = sessionCacheRef.current.get(id);
      if (cached) {
        const queueRequestVersion = queueSnapshotVersionGateRef.current.issue(id);
        const [indexedFiles, queued] = await Promise.all([
          userApi.chatFiles(workspaceId, id),
          userApi.chatQueueSummary(workspaceId, id).catch(emptyChatQueueSnapshot)
        ]);
        const active = activeChatRunFromQueueSummary(workspaceId, id, queued);
        if (activeSessionRef.current !== id) return;
        applyQueueSnapshot(id, queueRequestVersion, queued);
        const discoveredFiles = hasNewFiles(cached.files, indexedFiles);
        let next = { ...cached, files: indexedFiles };
        const hasLocalRun = Boolean(cached.streaming || cached.runId || cached.activeTurn);
        if (active.run && !hasLocalRun) {
          const history = await userApi.chatHistory(workspaceId, id);
          if (activeSessionRef.current !== id) return;
          next = {
            ...next,
            messages: historyMessages(
              history.messages,
              historyCheckpointForRun(active.run),
              indexedFiles
            )
          };
        }
        sessionCacheRef.current.set(id, next);
        setMessages(next.messages); setFiles(next.files); setError(null);
        if (discoveredFiles) openFilesPaneForSession(id);
        await recoverSession(workspaceId, id, active);
        if (next.streaming || next.runId) await refreshSessionHistory(workspaceId, id);
        return;
      }
      const queueRequestVersion = queueSnapshotVersionGateRef.current.issue(id);
      const [history, indexedFiles, queued] = await Promise.all([
        userApi.chatHistory(workspaceId, id),
        userApi.chatFiles(workspaceId, id),
        userApi.chatQueueSummary(workspaceId, id).catch(emptyChatQueueSnapshot)
      ]);
      const active = activeChatRunFromQueueSummary(workspaceId, id, queued);
      if (activeSessionRef.current !== id) return;
      applyQueueSnapshot(id, queueRequestVersion, queued);
      const sessionMessages = historyMessages(
        history.messages,
        historyCheckpointForRun(active.run),
        indexedFiles
      );
      sessionCacheRef.current.set(id, { messages: sessionMessages, files: indexedFiles });
      setMessages(sessionMessages);
      setFiles(indexedFiles);
      setError(null);
      if (indexedFiles.length > 0) openFilesPaneForSession(id);
      await recoverSession(workspaceId, id, active);
      return;
    }

    const cached = sessionCacheRef.current.get(id);
    const loadEpoch = historyLoadEpochRef.current + 1;
    historyLoadEpochRef.current = loadEpoch;
    setHistorySyncStatus("syncing");
    const activePromise = userApi.activeChatRun(workspaceId, id);
    const filesPromise = userApi.chatFiles(workspaceId, id);
    const prewarmPromise = selectedSessionPrewarm(workspaceId, id);
    const queueRequestVersion = queueSnapshotVersionGateRef.current.issue(id);
    const queuedPromise = prewarmPromise
      .then(() => userApi.chatQueueSummaryPrepared(workspaceId, id))
      .catch(emptyChatQueueSnapshot);
    let activeForHistory: ActiveChatRunResponse = { run: null };
    let resolveAuthoritySettled: (() => void) | undefined;
    const authoritySettled = new Promise<void>((resolve) => {
      resolveAuthoritySettled = resolve;
    });
    const historyOperation = loadChatHistorySources({
      target: {
        runtimeWorkspaceId: workspaceId,
        runtimeSessionId: id,
        loadEpoch
      },
      preview: cached
        ? undefined
        : () => userApi.chatHistoryPreview(workspaceId, id),
      authoritative: async () => {
        const [history, active] = await Promise.all([
          prewarmPromise.then(
            () => userApi.chatHistoryAuthoritative(workspaceId, id)
          ),
          activePromise
        ]);
        activeForHistory = active;
        return {
          ...history,
          hasActiveRun: Boolean(active.run)
        };
      },
      publish: (state) => {
        if (
          activeSessionRef.current !== id
          || historyLoadEpochRef.current !== loadEpoch
        ) return;
        setHistorySyncStatus(state.status);
        if (state.phase === "authoritative" || state.authorityFailed) {
          resolveAuthoritySettled?.();
          resolveAuthoritySettled = undefined;
        }
        if (state.phase !== "preview" && state.phase !== "authoritative") return;
        const current = sessionCacheRef.current.get(id) ?? {
          messages: [],
          files: []
        };
        const projected = historyMessages(
          state.messages,
          state.phase === "authoritative"
            ? historyCheckpointForRun(activeForHistory.run)
            : undefined,
          current.files
        );
        const nextMessages = state.phase === "authoritative" && current.activeTurn
          ? reconcileSessionHistory(projected, current.messages, current.activeTurn)
          : projected;
        const next = { ...current, messages: nextMessages };
        sessionCacheRef.current.set(id, next);
        prepareHistoryRefresh();
        setMessages(next.messages);
        setError(null);
      }
    });
    const [active, indexedFiles, queued] = await Promise.all([
      activePromise,
      filesPromise,
      queuedPromise
    ]);
    if (
      activeSessionRef.current !== id
      || historyLoadEpochRef.current !== loadEpoch
    ) return;
    applyQueueSnapshot(id, queueRequestVersion, queued);
    const current = sessionCacheRef.current.get(id) ?? cached ?? {
      messages: [],
      files: []
    };
    const discoveredFiles = hasNewFiles(current.files, indexedFiles);
    const hydratedCurrent = {
      ...current,
      files: indexedFiles,
      messages: hydrateAttachmentMetadata(current.messages, indexedFiles)
    };
    sessionCacheRef.current.set(id, hydratedCurrent);
    setMessages(hydratedCurrent.messages);
    setFiles(indexedFiles);
    if (discoveredFiles) openFilesPaneForSession(id);
    await authoritySettled;
    if (
      activeSessionRef.current !== id
      || historyLoadEpochRef.current !== loadEpoch
    ) return;
    await recoverSession(workspaceId, id, active);
    const finalHistory = await historyOperation;
    if (
      activeSessionRef.current !== id
      || historyLoadEpochRef.current !== loadEpoch
    ) return;
    if (
      finalHistory.authorityFailed
      && finalHistory.previewFailed
      && !cached
      && finalHistory.messages.length === 0
    ) {
      setError("读取任务失败");
    }
  };
  const ensureRuntimeAwake = (
    workspace: RuntimeWorkspaceSummary,
    showChecking: boolean,
    markReady = true
  ) => {
    if (runtimeRecoveryRef.current) return runtimeRecoveryRef.current;
    const recovery = (async () => {
      if (showChecking) setRuntimeConnectionState("checking");
      try {
        let readyWorkspace = workspace;
        let recovered = false;
        try {
          await userApi.keepaliveRuntime();
        } catch {
          recovered = true;
          setRuntimeConnectionState("waking");
          readyWorkspace = await userApi.warmupRuntime();
          await userApi.keepaliveRuntime();
        }
        if (markReady) setRuntimeConnectionState("ready");
        return { readyWorkspace, recovered };
      } catch (reason) {
        // 只有在真正失败时才设置 "failed"
        // 对于 503 "runtime is not ready"，保持 "starting" 状态
        if (isCriticalRuntimeError(reason)) {
          setRuntimeConnectionState("failed");
        } else {
          // 非关键错误，设置为 "starting" 状态，让后续流程可以继续等待
          setRuntimeConnectionState("starting");
        }
        throw reason;
      }
    })();
    runtimeRecoveryRef.current = recovery;
    const clearRecovery = () => {
      if (runtimeRecoveryRef.current === recovery) runtimeRecoveryRef.current = null;
    };
    void recovery.then(clearRecovery, clearRecovery);
    return recovery;
  };
  const load = async () => {
    let loadRequiredGate: RequiredPrewarmGate | null = null;
    let workspaceWakeSucceeded = false;
    const initialSessionRetryEpoch = ++initialSessionRetryEpochRef.current;
    const runtimeStartupContextEpoch = ++runtimeStartupContextEpochRef.current;
    setInitialSessionPreparing(false);
    setRuntimeStartupContext(null);
    if (!runtime) setLoading(true);
    const waitingSubmission = activeSessionRef.current
      ? submissionBySessionRef.current.get(activeSessionRef.current)
      : undefined;
    if (waitingSubmission?.phase === "required-prewarm") {
      cancelWaitingSubmission(waitingSubmission);
    }
    setError(null);
    setRuntimeAwake(false);
    clearRequiredPrewarm();
    if (!runtimeRecoveryRef.current) setRuntimeConnectionState("connecting");
    if (
      !isApplication
      && typeof userApi.runtimeStartupContext === "function"
    ) {
      void userApi.runtimeStartupContext()
        .then((startupContext) => {
          if (runtimeStartupContextEpochRef.current !== runtimeStartupContextEpoch) return;
          setRuntimeStartupContext(startupContext.context);
        })
        .catch(() => {
          if (runtimeStartupContextEpochRef.current !== runtimeStartupContextEpoch) return;
          setRuntimeStartupContext("none");
        });
    }
    try {
      const workspace = await userApi.bootstrapRuntime();
      setRuntime(workspace);
      // Bootstrap has established the workspace boundary. Do not hold the
      // whole page behind catalog/history requests while the exact Runtime
      // readiness probe is still in flight; the inline wake notice and
      // composer gate expose the remaining readiness state.
      setLoading(false);
      const wakeResultPromise = workspace.runtimeStatus === "ready"
        ? ensureRuntimeAwake(workspace, true, false).then(
          ({ readyWorkspace, recovered }) => ({
            ok: true as const,
            readyWorkspace,
            recovered
          }),
          (error: unknown) => ({ ok: false as const, error })
        )
        : null;
      if (!wakeResultPromise) setRuntimeConnectionState(runtimeConnectionStateFromWorkspace(workspace.runtimeStatus));
      const nextModelsPromise = userApi.runtimeModels();
      const nextInstalledExpertsPromise = typeof userApi.runnableExperts === "function"
        ? userApi.runnableExperts(workspace.runtimeWorkspaceId)
        : Promise.resolve([]);
      const sessionSelectionPromise = userApi.runtimeSessions(workspace.runtimeWorkspaceId)
        .then(async (nextSessions) => {
          let initialSession = nextSessions.find(
            (item) => visibleSession(item) && !isReadOnlySession(item)
          ) ?? nextSessions.find(visibleSession);
          if (initialSession) {
            const logicalRoot = contextRootSession(nextSessions, initialSession);
            initialSession = contextHeadSession(nextSessions, logicalRoot) ?? initialSession;
          }
          let initializedSessions = nextSessions;
          if (!initialSession) {
            const nextModels = await nextModelsPromise;
            // A personal session is control-plane metadata and can exist before
            // the first Runtime Pod becomes ready. Creating it early keeps the
            // composer usable, while the exact Runtime prewarm still waits for
            // the user's first send and the existing allocation polling.
            // Application Studio keeps its stricter ready gate because its
            // debug workspace and config lifecycle are owned by another surface.
            if (!isApplication || workspace.runtimeStatus === "ready") {
              for (
                let attempt = 1;
                attempt <= INITIAL_SESSION_PROFILE_MAX_ATTEMPTS;
                attempt += 1
              ) {
                try {
                  initialSession = await userApi.createRuntimeSession(
                    workspace.runtimeWorkspaceId,
                    {
                      title: DEFAULT_TASK_TITLE,
                      modelEntryId: nextModels[0]?.modelEntryId ?? null,
                      sourceType: "web"
                    }
                  );
                  setInitialSessionPreparing(false);
                  break;
                } catch (reason) {
                  if (
                    !isRuntimeAgentProfilePreparing(reason)
                    || attempt === INITIAL_SESSION_PROFILE_MAX_ATTEMPTS
                  ) {
                    setInitialSessionPreparing(false);
                    throw reason;
                  }
                  setInitialSessionPreparing(true);
                  setError(null);
                  await new Promise<void>((resolve) => {
                    window.setTimeout(resolve, INITIAL_SESSION_PROFILE_RETRY_DELAY_MS);
                  });
                  if (initialSessionRetryEpoch !== initialSessionRetryEpochRef.current) {
                    return {
                      earlySessionPrewarmPromise: null,
                      initializedSessions: nextSessions,
                      initialSession: null
                    };
                  }
                }
              }
            }
            if (initialSession) initializedSessions = [initialSession, ...nextSessions];
          }
          if (!initialSession) {
            return {
              earlySessionPrewarmPromise: null,
              initializedSessions: nextSessions,
              initialSession: null
            };
          }
          const selectedInitialSession = initialSession;
          const earlySessionPrewarmPromise = wakeResultPromise !== null
            && !isReadOnlySession(initialSession)
            ? ensureSessionPrewarm(
              workspace.runtimeWorkspaceId,
              selectedInitialSession.runtimeSessionId
            ).then(
              (receipt) => ({ ok: true as const, receipt }),
              (error: unknown) => ({ ok: false as const, error })
            )
            : null;
          return {
            earlySessionPrewarmPromise,
            initializedSessions,
            initialSession: selectedInitialSession
          };
        });
      const [
        nextModels,
        { earlySessionPrewarmPromise, initializedSessions, initialSession },
        nextInstalledExperts
      ] = await Promise.all([
        nextModelsPromise,
        sessionSelectionPromise,
        nextInstalledExpertsPromise
      ]);
      setModels(nextModels);
      setSessions(initializedSessions);
      setInstalledExperts(nextInstalledExperts);
      if (!initialSession) {
        activeSessionRef.current = null;
        setSessionId(null);
        setModelId(nextModels[0]?.modelEntryId ?? null);
        return;
      }
      const initialSessionId = initialSession.runtimeSessionId;
      const initialModelId = initialSession.sessionModelEntryId
        ?? nextModels[0]?.modelEntryId
        ?? null;
      activeSessionRef.current = initialSessionId;
      reconcileSessionActivities(
        workspace.runtimeWorkspaceId,
        initializedSessions,
        initialSessionId
      );
      setSessionId(initialSessionId);
      setModelId(initialModelId);
      setLoading(false);
      if (!wakeResultPromise) {
        return;
      }
      const sessionReadyPromise = (async () => {
        const wakeResult = await wakeResultPromise;
        if (!wakeResult.ok) throw wakeResult.error;
        const readyWorkspace = wakeResult.readyWorkspace;
        let readyReceipt: SessionPrewarmState = { state: "ready" };
        workspaceWakeSucceeded = true;
        setRuntime(readyWorkspace);
        if (initialSessionId && !isReadOnlySession(initialSession)) {
          // The initial prepare already owns this exact target.  Mark it
          // before runtimeAwake triggers the maintenance effect so that the
          // effect cannot issue a duplicate prepare while this one is still
          // awaiting its grant.
          skipImmediateSessionPrewarmRef.current = sessionPrewarmKey(
            readyWorkspace.runtimeWorkspaceId,
            initialSessionId
          );
        }
        setRuntimeAwake(true);
        if (initialSessionId && !isReadOnlySession(initialSession)) {
          if (wakeResult.recovered) {
            sessionPrewarmCoordinatorRef.current.invalidate(
              sessionPrewarmKey(
                readyWorkspace.runtimeWorkspaceId,
                initialSessionId
              )
            );
            readyReceipt = await ensureSessionPrewarm(
              readyWorkspace.runtimeWorkspaceId,
              initialSessionId
            );
          } else {
            const earlySessionPrewarm = await earlySessionPrewarmPromise;
            if (!earlySessionPrewarm?.ok) {
              readyReceipt = await ensureSessionPrewarm(
                readyWorkspace.runtimeWorkspaceId,
                initialSessionId
              );
            } else {
              readyReceipt = earlySessionPrewarm.receipt;
            }
          }
        }
        return readyReceipt;
      })();
      const requiredGate = beginRequiredPrewarm(
        workspace.runtimeWorkspaceId,
        initialSession,
        initialModelId,
        () => sessionReadyPromise
      );
      loadRequiredGate = requiredGate;
      if (requiredGate) {
        await requiredGate.promise;
      }
      await sessionReadyPromise;
      if (
        !requiredGate
        || (
          requiredPrewarmGateRef.current === requiredGate
          && requiredGate.status === "ready"
          && activeSessionRef.current === initialSessionId
        )
      ) {
        setRuntimeConnectionState("ready");
      }
    } catch (reason) {
      if (
        !workspaceWakeSucceeded
        || !loadRequiredGate
        || (
          requiredPrewarmGateRef.current === loadRequiredGate
          && activeSessionRef.current === loadRequiredGate.target.sessionId
        )
      ) {
        // 区分关键错误和非关键错误
        // 对于 503 "runtime is not ready" 等非关键错误，保持 "starting" 状态而不是 "failed"
        if (isCriticalRuntimeError(reason)) {
          setRuntimeConnectionState("failed");
          setError(reason instanceof Error ? reason.message : "加载失败");
        } else {
          // 非关键错误（如 503），设置为 "starting" 状态，让 UI 显示友好的提示
          setRuntimeConnectionState("starting");
          // 不设置 error，让 UI 显示"正在初始化..."的提示
        }
      }
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    if (initialLoadStartedRef.current) return;
    initialLoadStartedRef.current = true;
    void load();
  }, []);
  useEffect(() => () => {
    initialSessionRetryEpochRef.current += 1;
    previewRequestRef.current += 1;
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
  }, []);
  useEffect(() => {
    if (runtime?.runtimeStatus !== "ready") return;
    let canceled = false;
    let timer: number | undefined;
    const schedule = () => {
      timer = window.setTimeout(async () => {
        try {
          const { readyWorkspace, recovered } = await ensureRuntimeAwake(
            runtime,
            false,
            false
          );
          if (!canceled) {
            setRuntime(readyWorkspace);
            setRuntimeAwake(true);
            if (recovered) {
              const selectedSessionId = activeSessionRef.current;
              const selected = sessionsRef.current.find(
                (item) => item.runtimeSessionId === selectedSessionId
              );
              if (isApplication || isReadOnlySession(selected)) {
                clearRequiredPrewarm();
                setRuntimeConnectionState("ready");
              } else if (selectedSessionId && selected) {
                const currentGate = requiredPrewarmGateRef.current;
                const selectedModelId = currentGate?.target.sessionId === selectedSessionId
                  ? currentGate.target.modelId
                  : selected.sessionModelEntryId ?? null;
                const key = sessionPrewarmKey(
                  readyWorkspace.runtimeWorkspaceId,
                  selectedSessionId
                );
                sessionPrewarmCoordinatorRef.current.invalidate(key);
                skipImmediateSessionPrewarmRef.current = key;
                const gate = beginRequiredPrewarm(
                  readyWorkspace.runtimeWorkspaceId,
                  selected,
                  selectedModelId
                );
                void gate?.promise.catch(() => undefined);
              }
            }
          }
        } catch (reason) {
          if (!canceled) {
            setRuntimeAwake(false);
            setError(reason instanceof Error ? reason.message : "运行环境连接失败");
          }
        } finally {
          if (!canceled) schedule();
        }
      }, RUNTIME_KEEPALIVE_INTERVAL_MS);
    };
    schedule();
    return () => {
      canceled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runtime?.runtimeWorkspaceId, runtime?.runtimeStatus]);
  useEffect(() => {
    if (
      isApplication
      || runtime?.runtimeStatus !== "ready"
      || !runtimeAwake
      || !sessionId
      || !isReadOnlySession(
        sessionsRef.current.find((item) => item.runtimeSessionId === sessionId)
      )
    ) return;
    clearRequiredPrewarm();
    setRuntimeConnectionState("ready");
  }, [
    isApplication,
    runtime?.runtimeStatus,
    runtimeAwake,
    sessionId
  ]);
  useEffect(() => {
    const selected = sessionsRef.current.find((item) => item.runtimeSessionId === sessionId);
    if (!runtime || runtime.runtimeStatus !== "ready" || !runtimeAwake || !sessionId || isReadOnlySession(selected)) return;
    const workspaceId = runtime.runtimeWorkspaceId;
    const selectedSessionId = sessionId;
    const selectedPrewarmKey = sessionPrewarmKey(
      workspaceId,
      selectedSessionId
    );
    const renew = async () => {
      if (activeSessionRef.current !== selectedSessionId) return;
      if (
        submissionBySessionRef.current.has(selectedSessionId)
        || reconnectBySessionRef.current.get(selectedSessionId)?.key.startsWith(`${selectedSessionId}:`)
        || sessionCacheRef.current.get(selectedSessionId)?.streaming
        || scheduledRecoverySessionRef.current === selectedSessionId
      ) return;
      const requiredGate = requiredPrewarmGateRef.current;
      try {
        const receipt = await ensureSessionPrewarm(
          workspaceId,
          selectedSessionId
        );
        const persistedSession = sessionsRef.current.find(
          (item) => item.runtimeSessionId === selectedSessionId
        );
        const maintenanceMatchesGate = Boolean(
          requiredGate
          && requiredGate.target.modelId
            === (persistedSession?.sessionModelEntryId ?? null)
        );
        if (
          requiredGate
          && maintenanceMatchesGate
          && requiredPrewarmGateRef.current === requiredGate
          && requiredGate.target.sessionId === selectedSessionId
        ) {
          requiredGate.receipt = receipt;
          requiredGate.status = "ready";
          dispatchSessionReadiness({
            type: "prewarm_ready",
            target: requiredGate.target,
            receipt
          });
        }
        if (
          skipImmediateSessionPrewarmRef.current === selectedPrewarmKey
        ) {
          skipImmediateSessionPrewarmRef.current = null;
        }
        if (
          activeSessionRef.current === selectedSessionId
          && (
            !requiredGate
            || (
              requiredPrewarmGateRef.current === requiredGate
              && requiredGate.status === "ready"
            )
          )
        ) {
          setRuntimeConnectionState("ready");
        }
      } catch (reason) {
        if (
          requiredGate
          && requiredGate.target.modelId === (
            sessionsRef.current.find(
              (item) => item.runtimeSessionId === selectedSessionId
            )?.sessionModelEntryId ?? null
          )
          && requiredPrewarmGateRef.current === requiredGate
          && requiredGate.target.sessionId === selectedSessionId
        ) {
          dispatchSessionReadiness({
            type: "prewarm_retry_wait",
            target: requiredGate.target,
            error: reason instanceof Error
              ? reason.message
              : "会话预热失败"
          });
        }
        if (
          activeSessionRef.current === selectedSessionId
          && (
            !requiredGate
            || (
              requiredPrewarmGateRef.current === requiredGate
              && requiredGate.status === "ready"
            )
          )
        ) {
          setRuntimeConnectionState("connecting");
        }
      }
    };
    const timer = window.setInterval(() => void renew(), SESSION_PREWARM_INTERVAL_MS);
    const handleFocus = () => void renew();
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") void renew();
    };
    window.addEventListener("focus", handleFocus);
    document.addEventListener("visibilitychange", handleVisibilityChange);
    const skipImmediateRenew = (
      skipImmediateSessionPrewarmRef.current === selectedPrewarmKey
    );
    skipImmediateSessionPrewarmRef.current = null;
    if (!skipImmediateRenew) void renew();
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", handleFocus);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [runtime?.runtimeWorkspaceId, runtime?.runtimeStatus, runtimeAwake, sessionId]);
  useEffect(() => {
    if (!runtime || !sessionId) { setMessages([]); setFiles([]); return; }
    if (view !== "chat") return;
    activeSessionRef.current = sessionId;
    const loadKey = `${runtime.runtimeWorkspaceId}:${sessionId}`;
    const selected = sessionsRef.current.find((item) => item.runtimeSessionId === sessionId);
    if (isReadOnlySession(selected)) {
      const promise = loadSession(runtime.runtimeWorkspaceId, sessionId);
      sessionLoadRef.current = { key: loadKey, promise };
      void promise.catch((reason) => {
        if (activeSessionRef.current === sessionId) {
          setError(reason instanceof Error ? reason.message : "读取任务失败");
        }
      });
      return;
    }
    if (runtime.runtimeStatus !== "ready" || !runtimeAwake) {
      const promise = loadColdSession(runtime.runtimeWorkspaceId, sessionId);
      sessionLoadRef.current = { key: loadKey, promise };
      void promise.catch(() => undefined);
      return;
    }
    const promise = loadSession(runtime.runtimeWorkspaceId, sessionId);
    sessionLoadRef.current = { key: loadKey, promise };
    void promise.catch((reason) => {
      if (activeSessionRef.current === sessionId) setError(reason instanceof Error ? reason.message : "读取任务失败");
    });
  }, [runtime, runtimeAwake, sessionId, view]);
  useEffect(() => {
    if (
      !runtime
      || runtime.runtimeStatus !== "ready"
      || !runtimeAwake
      || !sessionId
      || view !== "chat"
      || streaming
      || isReadOnlySession(sessionsRef.current.find((item) => item.runtimeSessionId === sessionId))
    ) return;
    const workspaceId = runtime.runtimeWorkspaceId;
    const selectedSessionId = sessionId;
    const key = `${workspaceId}:${selectedSessionId}`;
    const existing = backgroundObserverRef.current;
    if (existing?.key === key && !existing.controller.signal.aborted) return;
    existing?.controller.abort();
    const controller = new AbortController();
    backgroundObserverRef.current = { key, controller };

    const reconnectFinalRun = (runtimeRunId: string) => {
      if (
        controller.signal.aborted
        || activeSessionRef.current !== selectedSessionId
        || backgroundRunRecoveryRef.current.has(runtimeRunId)
      ) return;
      backgroundRunRecoveryRef.current.add(runtimeRunId);
      void (async () => {
        const outcome = await recoverSession(
          workspaceId,
          selectedSessionId,
          undefined,
          { preferredRuntimeRunId: runtimeRunId }
        );
        if (outcome !== "terminal") {
          backgroundRunRecoveryRef.current.delete(runtimeRunId);
          await refreshSessionHistory(workspaceId, selectedSessionId);
        }
      })();
    };

    const waitBeforeRetry = () => new Promise<void>((resolve) => {
      const timer = window.setTimeout(resolve, 1000);
      controller.signal.addEventListener("abort", () => {
        window.clearTimeout(timer);
        resolve();
      }, { once: true });
    });
    const onBackgroundEvent = (event: { event: string; data: Record<string, unknown> }) => {
      if (controller.signal.aborted || activeSessionRef.current !== selectedSessionId) return;
      const reportBackgroundFailure = (state: unknown) => {
        if (state === "attention_required") {
          setError("专家任务执行失败或需要处理，请重新派发任务，或查看 Kanban 阻塞原因");
        } else if (state === "delivery_failed") {
          setError("专家任务已经结束，但主 Agent 汇总失败，请稍后重试");
        }
      };
      if (event.event === "chat.background.snapshot") {
        const collaborations = Array.isArray(event.data.collaborations)
          ? event.data.collaborations
          : [];
        for (const raw of collaborations) {
          if (!raw || typeof raw !== "object") continue;
          const item = raw as Record<string, unknown>;
          const finalRunId = typeof item.finalRuntimeRunId === "string"
            ? item.finalRuntimeRunId
            : "";
          reportBackgroundFailure(item.state);
          if (finalRunId && !["delivered", "delivery_failed", "canceled"].includes(String(item.state ?? ""))) {
            reconnectFinalRun(finalRunId);
          }
        }
        return;
      }
      if (event.event === "chat.background.run.created") {
        const finalRunId = typeof event.data.finalRuntimeRunId === "string"
          ? event.data.finalRuntimeRunId
          : "";
        if (finalRunId) reconnectFinalRun(finalRunId);
        return;
      }
      if (event.event === "chat.background.status") {
        reportBackgroundFailure(event.data.state);
      }
    };

    void (async () => {
      let pendingWasObserved = false;
      try {
        const sessionLoad = sessionLoadRef.current;
        if (sessionLoad?.key === key) {
          try {
            await sessionLoad.promise;
          } catch {
            return;
          }
        }
        while (!controller.signal.aborted) {
          try {
            const current = await userApi.backgroundCollaborations(
              workspaceId,
              selectedSessionId,
              controller.signal
            );
            if (controller.signal.aborted || current.pendingCount === 0) return;
            pendingWasObserved = true;
            await userApi.streamBackgroundCollaborations(
              workspaceId,
              selectedSessionId,
              onBackgroundEvent,
              controller.signal
            );
          } catch {
            // The durable active-run/history recovery paths remain
            // authoritative. Keep retrying only after this session has
            // confirmed pending collaboration work.
            if (controller.signal.aborted || !pendingWasObserved) return;
          }
          await waitBeforeRetry();
        }
      } finally {
        if (backgroundObserverRef.current?.controller === controller) {
          backgroundObserverRef.current = null;
        }
      }
    })();
    return () => {
      controller.abort();
      if (backgroundObserverRef.current?.controller === controller) {
        backgroundObserverRef.current = null;
      }
    };
  }, [
    runtime?.runtimeWorkspaceId,
    runtime?.runtimeStatus,
    runtimeAwake,
    sessionId,
    streaming,
    view
  ]);
  useEffect(() => {
    // The server starts provisioning in the background. Keep polling legacy
    // or in-flight null/absent summaries instead of leaving them at connecting.
    // Do not start a timer during the initial render before bootstrap has
    // returned a workspace summary. Once the server returns an explicit
    // null/absent status, keep polling it just like other startup states.
    if (!runtime || !runtimeStartupPending(runtime.runtimeStatus)) return;
    const timer = window.setInterval(() => void userApi.runtimeWorkspace().then((next) => {
      setRuntime(next);
      if (next.runtimeStatus === "ready") {
        const queuedSubmission = activeSessionRef.current
          ? submissionBySessionRef.current.get(activeSessionRef.current)
          : undefined;
        if (queuedSubmission?.phase === "required-prewarm") {
          // The send path already owns a single queued turn and a retrying
          // session-prewarm gate.  A full reload would invalidate both just
          // as the cold Runtime becomes usable, so only finish the Runtime
          // readiness probe here and let that gate dispatch exactly once.
          void ensureRuntimeAwake(next, true, false).then(
            ({ readyWorkspace }) => {
              setRuntime(readyWorkspace);
              setRuntimeAwake(true);
            },
            (reason: unknown) => {
              setRuntimeAwake(false);
              setRuntimeConnectionState("failed");
              setError(reason instanceof Error ? reason.message : "运行环境连接失败");
            }
          );
        } else {
          void load();
        }
      } else {
        setRuntimeConnectionState(runtimeConnectionStateFromWorkspace(next.runtimeStatus));
      }
    }).catch(() => undefined), 3000);
    return () => window.clearInterval(timer);
  }, [runtime?.runtimeStatus]);
  useEffect(() => {
    if (
      !runtime || runtime.runtimeStatus !== "ready" || !sessionId || pendingScheduledTask === null
      || pendingScheduledTask.sessionId !== sessionId || view !== "chat"
    ) return undefined;
    const observerId = ++pendingScheduledTaskObserverRef.current;
    const controller = new AbortController();
    let cancelled = false;
    let timer: number | undefined;
    let attempts = 0;
    let consecutiveFailures = 0;
    const isCurrent = () => !cancelled && pendingScheduledTaskObserverRef.current === observerId;
    const finish = (message?: string) => {
      if (!isCurrent()) return;
      setPendingScheduledTask(null);
      if (message) setError(message);
    };
    const scheduleNext = (delay: number) => {
      if (!isCurrent()) return;
      timer = window.setTimeout(() => void inspect(), delay);
    };
    const inspect = async () => {
      attempts += 1;
      try {
        const [active, tasks] = await Promise.all([
          userApi.activeChatRun(runtime.runtimeWorkspaceId, sessionId, controller.signal),
          userApi.listScheduledTasks(runtime.runtimeWorkspaceId, controller.signal)
        ]);
        if (!isCurrent()) return;
        consecutiveFailures = 0;
        if (
          active.run?.source?.type === "scheduled"
          && active.run.source.scheduledTaskId === pendingScheduledTask.taskId
        ) {
          scheduledRecoverySessionRef.current = sessionId;
          try {
            const outcome = await recoverSession(runtime.runtimeWorkspaceId, sessionId, active, {
              scheduled: true,
              timeoutMs: SCHEDULED_TASK_RECONNECT_TIMEOUT_MS
            });
            if (!isCurrent()) return;
            // Reconcile durable history after the replay is attached.  The
            // active-turn anchor keeps the live reasoning/tool projection
            // when the backend history is still one event behind.
            await refreshSessionHistory(runtime.runtimeWorkspaceId, sessionId, true);
            if (!isCurrent()) return;
            if (outcome === "terminal") {
              finish();
              return;
            }
            if (outcome === "retryable" || outcome === "cancelled") {
              scheduleNext(SCHEDULED_TASK_POLL_INTERVAL_MS);
            }
          } finally {
            if (scheduledRecoverySessionRef.current === sessionId) scheduledRecoverySessionRef.current = null;
          }
          return;
        }
        const task = tasks.find((item) => item.taskId === pendingScheduledTask.taskId);
        const acceptedAt = pendingScheduledTask.acceptedAt ? Date.parse(pendingScheduledTask.acceptedAt) : Number.NaN;
        const completedAfterTrigger = Boolean(
          task && task.lastRuntimeRunId && !task.isRunning && task.lastRunAt
          && Number.isFinite(acceptedAt) && Date.parse(task.lastRunAt) >= acceptedAt
        );
        if (completedAfterTrigger) {
          await refreshSessionHistory(runtime.runtimeWorkspaceId, sessionId, true);
          if (!isCurrent()) return;
          finish();
          return;
        }
      } catch {
        if (!isCurrent()) return;
        consecutiveFailures += 1;
      }
      if (!isCurrent()) return;
      if (attempts >= SCHEDULED_TASK_POLL_MAX_ATTEMPTS) {
        finish(SCHEDULED_TASK_OBSERVATION_ERROR);
        return;
      }
      const delay = consecutiveFailures === 0
        ? SCHEDULED_TASK_POLL_INTERVAL_MS
        : Math.min(
          SCHEDULED_TASK_POLL_INTERVAL_MS * (2 ** (consecutiveFailures - 1)),
          SCHEDULED_TASK_POLL_MAX_BACKOFF_MS
        );
      scheduleNext(delay);
    };
    void inspect();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runtime?.runtimeWorkspaceId, runtime?.runtimeStatus, sessionId, pendingScheduledTask, view]);
  useEffect(() => {
    if (
      !runtime || runtime.runtimeStatus !== "ready" || !runtimeAwake || !sessionId || view !== "chat"
      || pendingScheduledTask?.sessionId === sessionId || scheduledRecoverySessionRef.current === sessionId
    ) return undefined;
    if (residentScheduledRunRef.current?.sessionId !== sessionId) residentScheduledRunRef.current = null;
    const controller = new AbortController();
    let cancelled = false;
    let timer: number | undefined;
    let recovering = false;
    const isCurrent = () => !cancelled && activeSessionRef.current === sessionId;
    const scheduleNext = () => {
      if (!isCurrent()) return;
      timer = window.setTimeout(() => void inspect(), SCHEDULED_TASK_POLL_INTERVAL_MS);
    };
    const inspect = async () => {
      try {
        const active = await userApi.activeChatRun(runtime.runtimeWorkspaceId, sessionId, controller.signal);
        if (!isCurrent()) return;
        const scheduledRun = active.run?.source?.type === "scheduled" ? active.run : null;
        const observed = residentScheduledRunRef.current;
        if (!scheduledRun) {
          if (observed?.sessionId === sessionId) {
            residentScheduledRunRef.current = null;
            await refreshSessionHistory(runtime.runtimeWorkspaceId, sessionId, true);
          } else {
            await refreshSessionFiles(runtime.runtimeWorkspaceId, sessionId, true);
          }
          if (!isCurrent()) return;
          scheduleNext();
          return;
        }
        if (observed?.sessionId !== sessionId || observed.runtimeRunId !== scheduledRun.runtimeRunId) {
          residentScheduledRunRef.current = { sessionId, runtimeRunId: scheduledRun.runtimeRunId, terminal: false };
        }
        const current = residentScheduledRunRef.current;
        const reconnectKey = `${sessionId}:${scheduledRun.runtimeRunId}`;
        const sessionReconnect = reconnectBySessionRef.current.get(sessionId);
        if (recovering || current?.terminal || (sessionReconnect?.key === reconnectKey && !sessionReconnect.controller.signal.aborted)) {
          scheduleNext();
          return;
        }
        recovering = true;
        scheduledRecoverySessionRef.current = sessionId;
        try {
          const outcome = await recoverSession(runtime.runtimeWorkspaceId, sessionId, active, {
            scheduled: true,
            timeoutMs: SCHEDULED_TASK_RECONNECT_TIMEOUT_MS
          });
          if (!isCurrent()) return;
          if (outcome === "terminal" && residentScheduledRunRef.current?.runtimeRunId === scheduledRun.runtimeRunId) {
            residentScheduledRunRef.current = { sessionId, runtimeRunId: scheduledRun.runtimeRunId, terminal: true };
          }
        } finally {
          recovering = false;
          if (scheduledRecoverySessionRef.current === sessionId) scheduledRecoverySessionRef.current = null;
        }
      } catch {
        // A future resident poll rechecks durable active-run state.
      }
      if (isCurrent()) scheduleNext();
    };
    scheduleNext();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runtime?.runtimeWorkspaceId, runtime?.runtimeStatus, runtimeAwake, sessionId, pendingScheduledTask, view]);
  const chooseSession = (id: string, selectedSession?: RuntimeSessionSummary, preserveScheduledTaskPending = false) => {
    if (!preserveScheduledTaskPending) {
      pendingScheduledTaskObserverRef.current += 1;
      setPendingScheduledTask(null);
    }
    requestScrollToLatest();
    setView("chat");
    setComposerFocusRequest((request) => request + 1);
    const previousSessionId = activeSessionRef.current;
    const activityCoordinator = runtime
      ? sessionActivityCoordinator(runtime.runtimeWorkspaceId)
      : null;
    if (activityCoordinator) {
      setSessionActivities((current) => activityCoordinator.markSeen(current, id));
    }
    if (previousSessionId && previousSessionId !== id) {
      cancelMessageCommitFrame();
      for (const key of artifactVisibilityRetryRef.current.keys()) {
        if (key.startsWith(`${previousSessionId}:`)) {
          artifactVisibilityRetryRef.current.delete(key);
        }
      }
      sessionDraftsRef.current.set(previousSessionId, input);
      const previousTimingKey = timingKeyBySessionRef.current.get(previousSessionId);
      if (previousTimingKey) {
        chatTimingRef.current.cancel(previousTimingKey);
        lifecycleTimingRef.current.cancel(previousTimingKey);
      }
      timingKeyBySessionRef.current.delete(previousSessionId);
      pendingVisibleCandidatesRef.current = pendingVisibleCandidatesRef.current.filter(
        (candidate) => candidate.sessionId !== previousSessionId
      );
      for (const [kind, frame] of paintFramesRef.current) {
        if (frame.sessionId !== previousSessionId) continue;
        window.cancelAnimationFrame(frame.firstFrame);
        if (frame.secondFrame !== undefined) window.cancelAnimationFrame(frame.secondFrame);
        paintFramesRef.current.delete(kind);
      }
    }
    const previousSubmission = previousSessionId
      ? submissionBySessionRef.current.get(previousSessionId)
      : undefined;
    if (
      previousSubmission
      && previousSubmission.phase === "required-prewarm"
    ) {
      cancelWaitingSubmission(previousSubmission);
    }
    activeSessionRef.current = id;
    queueSnapshotVersionGateRef.current.invalidate(id);
    setError(null);
    const target = selectedSession ?? chatSessions.find((item) => item.runtimeSessionId === id);
    const sourceMessages = previousSessionId
      ? sessionCacheRef.current.get(previousSessionId)?.messages
      : undefined;
    const transitionMessages = sourceMessages?.length ? sourceMessages : messages;
    if (
      !sessionCacheRef.current.has(id)
      && target?.contextSourceRuntimeSessionId
    ) {
      sessionCacheRef.current.set(id, { messages: transitionMessages, files: [] });
    }
    const cached = sessionCacheRef.current.get(id);
    setStreaming(Boolean(cached?.streaming));
    setStopping(stopRequestBySessionRef.current.has(id));
    setRunId(cached?.runId ?? null);
    setQueueSummary(emptyChatQueueSnapshot());
    setMessages(cached?.messages ?? []);
    setFiles(cached?.files ?? []);
    setHistorySyncStatus("idle");
    setPendingAttachmentIds(pendingAttachmentsRef.current.get(id) ?? []);
    setWelcomeTaskPreview(null);
    setInput(sessionDraftsRef.current.get(id) ?? "");
    setSessionId(id);
    const targetModelId = target?.sessionModelEntryId
      ?? models[0]?.modelEntryId
      ?? null;
    setModelId(targetModelId);
    if (isApplication) {
      clearRequiredPrewarm();
    } else if (target && isReadOnlySession(target)) {
      clearRequiredPrewarm();
      if (runtime?.runtimeStatus === "ready" && runtimeAwake) {
        setRuntimeConnectionState("ready");
      }
    } else if (
      target
      && runtime?.runtimeStatus === "ready"
      && (
        previousSessionId !== id
        || !requiredTargetMatches(
          requiredPrewarmGateRef.current,
          runtime.runtimeWorkspaceId,
          target,
          targetModelId
        )
      )
    ) {
      const key = sessionPrewarmKey(runtime.runtimeWorkspaceId, id);
      sessionPrewarmCoordinatorRef.current.invalidate(key);
      skipImmediateSessionPrewarmRef.current = key;
      const gate = beginRequiredPrewarm(
        runtime.runtimeWorkspaceId,
        target,
        targetModelId
      );
      void gate?.promise.catch(() => undefined);
    }
  };
  const chooseLogicalSession = (
    id: string,
    selectedSession?: RuntimeSessionSummary
  ) => {
    const root = contextRootSession(
      sessionsRef.current,
      selectedSession ?? sessionsRef.current.find((item) => item.runtimeSessionId === id) ?? null
    );
    const head = contextHeadSession(sessionsRef.current, root);
    if (!head) return;
    chooseSession(head.runtimeSessionId, head);
  };
  const openScheduledTaskSession = (
    id: string,
    taskId?: number,
    acceptedAt?: string,
    taskName?: string,
    prompt?: string
  ) => {
    pendingScheduledTaskObserverRef.current += 1;
    if (taskId !== undefined && taskName && prompt) {
      const source = {
        type: "scheduled" as const,
        scheduledTaskId: taskId,
        scheduledTaskName: taskName,
        triggerKind: "manual",
        taskAvailable: true
      };
      const executionKey = `${taskId}-${acceptedAt ?? Date.now()}`;
      const cached = sessionCacheRef.current.get(id) ?? { messages: [], files: [] };
      replaceSessionCache(id, beginScheduledTask(cached, prompt, source, executionKey));
      markSessionActivityRunning(id, executionKey);
      setPendingScheduledTask({ taskId, taskName, prompt, acceptedAt, sessionId: id });
    } else {
      setPendingScheduledTask(null);
    }
    chooseSession(id, undefined, true);
  };
  const createSession = async (
    runtimeAgentProfileId: number | null = null,
    contextSourceRuntimeSessionId: string | null = null
  ) => {
    if (!runtime) return null;
    try {
      const created = await userApi.createRuntimeSession(runtime.runtimeWorkspaceId, {
        title: contextSourceRuntimeSessionId
          ? sessionsRef.current.find((item) => item.runtimeSessionId === contextSourceRuntimeSessionId)?.title ?? DEFAULT_TASK_TITLE
          : DEFAULT_TASK_TITLE,
        modelEntryId: modelId,
        sourceType: "web",
        runtimeAgentProfileId,
        contextSourceRuntimeSessionId
      });
      setSessions((items) => {
        const nextItems = [created, ...items];
        const logicalRoot = contextRootSession(nextItems, created);
        const next = logicalRoot?.contextHeadRuntimeSessionId
          ? nextItems.map((item) => (
            item.runtimeSessionId === logicalRoot.runtimeSessionId
              ? { ...item, contextHeadRuntimeSessionId: created.runtimeSessionId }
              : item
          ))
          : nextItems;
        sessionsRef.current = next;
        return next;
      });
      chooseSession(created.runtimeSessionId, created);
      return created;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建任务失败");
      return null;
    }
  };
  const selectConversationAgent = async (runtimeAgentProfileId: number | null) => {
    const currentProfileId = activeSession?.agentProfileKind === "expert"
      ? activeSession.runtimeAgentProfileId ?? null
      : null;
    const targetProfileId = runtimeAgentProfileId;
    if (currentProfileId === targetProfileId) return;
    const sourceSession = activeSession;
    if (!sourceSession || isReadOnlySession(sourceSession) || isImmutableSession(sourceSession)) {
      await createSession(runtimeAgentProfileId);
      return;
    }
    const existingBranch = findContextBranch(
      sessionsRef.current,
      sourceSession.runtimeSessionId,
      targetProfileId
    );
    if (existingBranch) {
      chooseSession(existingBranch.runtimeSessionId, existingBranch);
      return;
    }
    await createSession(runtimeAgentProfileId, sourceSession.runtimeSessionId);
  };
  const startExpertConversation = async (runtimeAgentProfileId: number) => {
    const sourceSession = activeSession;
    if (!sourceSession || isReadOnlySession(sourceSession) || isImmutableSession(sourceSession)) {
      await createSession(runtimeAgentProfileId);
      return;
    }
    const existingBranch = findContextBranch(
      sessionsRef.current,
      sourceSession.runtimeSessionId,
      runtimeAgentProfileId
    );
    if (existingBranch) {
      chooseSession(existingBranch.runtimeSessionId, existingBranch);
      setView("chat");
      return;
    }
    await createSession(runtimeAgentProfileId, sourceSession.runtimeSessionId);
    setView("chat");
  };
  const applyInstalledExperts = async (profiles: RuntimeAgentProfileSummary[]) => {
    setInstalledExperts(profiles);
    if (!runtime) return;
    let authoritativeSessions: RuntimeSessionSummary[];
    try {
      authoritativeSessions = await userApi.runtimeSessions(runtime.runtimeWorkspaceId);
    } catch {
      // Do not discard cached expert conversations when this supplemental
      // refresh fails. A later refresh will obtain their authoritative status.
      return;
    }
    const validExpertIds = new Set(profiles.map((item) => item.runtimeAgentProfileId));
    const nextSessions = authoritativeSessions.filter(
      (item) => item.agentProfileKind !== "expert"
        || isReadOnlySession(item)
        || (item.runtimeAgentProfileId !== null
          && item.runtimeAgentProfileId !== undefined
          && validExpertIds.has(item.runtimeAgentProfileId))
    );
    sessionsRef.current = nextSessions;
    setSessions(nextSessions);
    const currentStillExists = nextSessions.some(
      (item) => item.runtimeSessionId === activeSessionRef.current
    );
    if (currentStillExists) return;
    const fallback = nextSessions.find(visibleSession);
    if (fallback) {
      chooseSession(fallback.runtimeSessionId, fallback);
    } else {
      void createSession();
    }
  };
  const renameSession = async (id: string, title: string) => {
    if (!runtime) return false;
    const selected = sessionsRef.current.find((item) => item.runtimeSessionId === id);
    if (isReadOnlySession(selected) || isImmutableSession(selected)) return false;
    setError(null);
    try {
      const renamed = await userApi.renameRuntimeSession(runtime.runtimeWorkspaceId, id, { title });
      setSessions((items) => items.map((item) => item.runtimeSessionId === id ? renamed : item));
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重命名任务失败");
      return false;
    }
  };
  const autoTitleFirstMessage = async (workspaceId: number, id: string, message: string) => {
    if (isImmutableSession(sessionsRef.current.find((item) => item.runtimeSessionId === id))) return;
    const baseTitle = firstMessageTaskTitle(message);
    if (!baseTitle) return;
    for (let candidateNumber = 1; candidateNumber <= 20; candidateNumber += 1) {
      const current = sessionsRef.current.find((item) => item.runtimeSessionId === id);
      if (!current || !isDefaultTaskTitle(current.title)) return;
      const candidate = numberedTaskTitle(baseTitle, candidateNumber);
      const occupied = sessionsRef.current.some(
        (item) => item.runtimeSessionId !== id && item.title === candidate
      );
      if (occupied) continue;
      try {
        const renamed = await userApi.renameRuntimeSession(workspaceId, id, { title: candidate });
        setSessions((items) => {
          const latest = items.find((item) => item.runtimeSessionId === id);
          if (!latest || !isDefaultTaskTitle(latest.title)) return items;
          const next = items.map((item) => item.runtimeSessionId === id ? renamed : item);
          sessionsRef.current = next;
          return next;
        });
        return;
      } catch (reason) {
        if (!hasHttpStatus(reason, 409)) return;
      }
    }
  };
  const deleteSession = async (id: string) => {
    if (!runtime) throw new Error("运行环境未就绪");
    if (streaming) throw new Error("任务生成中，暂时无法删除");
    if (isImmutableSession(sessionsRef.current.find((item) => item.runtimeSessionId === id))) {
      throw new Error("企微固定会话不能删除；解除绑定后仍会保留全部历史");
    }
    try {
      await userApi.deleteRuntimeSession(runtime.runtimeWorkspaceId, id);
      const remaining = sessions.filter((item) => item.runtimeSessionId !== id);
      setSessions(remaining);
      const activityCoordinator = sessionActivityCoordinator(runtime.runtimeWorkspaceId);
      if (activityCoordinator) {
        setSessionActivities((current) => activityCoordinator.remove(current, id));
      }
      if (id === sessionId) {
        const next = remaining.find(visibleSession);
        if (next) {
          chooseSession(next.runtimeSessionId, next);
        } else {
          clearRequiredPrewarm();
          cancelMessageCommitFrame();
          activeSessionRef.current = null;
          setMessages([]);
          setFiles([]);
          setSessionId(null);
        }
      }
    } catch (reason) {
      const error = reason instanceof Error ? reason : new Error("删除任务失败");
      setError(error.message);
      throw error;
    }
  };

  const uploadOne = async (item: LocalAttachmentUpload): Promise<ChatFileSummary | null> => {
    if (!runtime) return null;
    if (isReadOnlySession(sessionsRef.current.find((session) => session.runtimeSessionId === item.sessionId))) return null;
    setUploadItems((items) => items.map((entry) => entry.id === item.id ? { ...entry, status: "uploading", progress: 0, error: undefined } : entry));
    try {
      const result = await userApi.uploadChatAttachment(
        item.file,
        runtime.runtimeWorkspaceId,
        item.sessionId,
        (progress) => setUploadItems((items) => items.map((entry) => entry.id === item.id ? { ...entry, progress } : entry))
      );
      const cached = sessionCacheRef.current.get(item.sessionId) ?? { messages: [], files: [] };
      const nextFiles = [result.file, ...cached.files.filter((file) => file.fileId !== result.file.fileId)];
      replaceSessionFiles(item.sessionId, nextFiles);
      const nextPending = [...(pendingAttachmentsRef.current.get(item.sessionId) ?? []), result.file.fileId];
      pendingAttachmentsRef.current.set(item.sessionId, nextPending);
      if (activeSessionRef.current === item.sessionId) setPendingAttachmentIds(nextPending);
      setUploadItems((items) => items.filter((entry) => entry.id !== item.id));
      setFilesOpen(true);
      return result.file;
    } catch (reason) {
      setUploadItems((items) => items.map((entry) => entry.id === item.id ? { ...entry, status: "failed", error: reason instanceof Error ? reason.message : "上传失败" } : entry));
      return null;
    }
  };
  const addFiles = (
    selected: File[],
    targetSessionOverride?: RuntimeSessionSummary
  ) => {
    const targetSession = targetSessionOverride ?? activeSession;
    if (!runtime || !targetSession || isReadOnlySession(targetSession)) return;
    const available = Math.max(0, 20 - pendingAttachmentIds.length - currentUploads.length);
    if (selected.length > available) setError("每条消息最多添加 20 个附件");
    const accepted: File[] = [];
    selected.slice(0, available).forEach((file, index) => {
      if (isImageFile(file)) {
        if (!selectedModelSupportsVision) {
          setError("当前模型不支持图片识别，请切换模型后重试");
          return;
        }
        if (!isSupportedImageFile(file)) {
          setError("仅支持粘贴 PNG、JPEG 或 WebP 图片");
          return;
        }
        if (file.size > MAX_IMAGE_ATTACHMENT_BYTES) {
          setError("单张图片不能超过 20 MB");
          return;
        }
        accepted.push(normalizePastedImage(file, index + 1));
        return;
      }
      accepted.push(file);
    });
    for (const file of accepted) {
      const item: LocalAttachmentUpload = { id: `${Date.now()}-${Math.random().toString(36).slice(2)}`, name: file.name, file, sessionId: targetSession.runtimeSessionId, progress: 0, status: "pending" };
      setUploadItems((items) => [...items, item]);
    }
  };
  const retryUpload = (uploadId: string) => {
    setUploadItems((items) => items.map((entry) => entry.id === uploadId ? { ...entry, status: "pending", progress: 0, error: undefined } : entry));
  };
  const removeUpload = (uploadId: string) => {
    setUploadItems((items) => items.filter((entry) => entry.id !== uploadId || entry.status === "uploading"));
  };
  const { getRootProps, getInputProps, open: openFilePicker, isDragActive } = useDropzone({
    noClick: true,
    noKeyboard: true,
    multiple: true,
    maxFiles: 20,
    maxSize: MAX_ATTACHMENT_BYTES,
    disabled: !activeSession || activeSessionReadOnly || runtime?.runtimeStatus !== "ready",
    onDropAccepted: (accepted) => addFiles(accepted),
    onDropRejected: (rejections) => setError(rejections.some((item) => item.file.size > MAX_ATTACHMENT_BYTES) ? "单个文件不能超过 100 MB" : "无法添加所选文件")
  });

  const startCreateAppSession = async (prompt: string): Promise<boolean> => {
    if (!runtime) {
      setError("聊天运行环境尚未就绪，暂时不能创建看板会话。");
      return false;
    }
    const targetSession = await createSession();
    if (!targetSession) return false;
    setInput(prompt);
    setComposerFocusRequest((request) => request + 1);
    return true;
  };

  // 新建看板弹窗「方式二」（2026-11-02-005 恢复）：下载广场看板 ZIP 作为模板，
  // 新建独立会话并同时注入附件与模板修改提示词。不设置 editAppContext——模板看板
  // 属于其他创建者，run 完成后产出的新包走标准「分享」流程发布为自己的新看板，
  // 不产生指向他人看板的更新提示。
  const startCreateAppFromTemplate = async (app: AppCenterListItem): Promise<boolean> => {
    if (!runtime) {
      setError("聊天运行环境尚未就绪，暂时不能基于模板新建看板。");
      return false;
    }
    try {
      const blob = await userApi.downloadAppPackage(app.id);
      const filename = app.name.toLowerCase().endsWith(".zip") ? app.name : `${app.name}.zip`;
      const packageFile = new File([blob], filename, { type: "application/zip" });
      const targetSession = await createSession();
      if (!targetSession) return false;
      addFiles([packageFile], targetSession);
      setInput(`附件中的看板包「${app.name}」来自看板广场，请以它为模板基础，帮我创建一个属于我自己的新看板。只修改我要求的部分，模板其余功能、样式、资源和目录结构保持不变；不要新增外部 CDN 或网络依赖，看板继续保持包内自包含。修改完成后，重新打包为根目录包含 index.html 的 ZIP，直接写入本次任务文件目录（不要放入 .work/，不要只保存到本地工作文件夹，也不要只输出代码），并告知文件名。我的修改要求：`);
      setComposerFocusRequest((request) => request + 1);
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "看板模板下载失败，请稍后重试。");
      return false;
    }
  };


  const startEditApp = async (app: AppCenterListItem) => {
    if (!runtime) {
      setError("聊天运行环境尚未就绪，暂时不能编辑看板。");
      return;
    }
    try {
      const blob = await userApi.downloadAppPackage(app.id);
      const filename = app.name.toLowerCase().endsWith(".zip") ? app.name : `${app.name}.zip`;
      const packageFile = new File([blob], filename, { type: "application/zip" });
      const sourceSession = app.sourceSessionId
        ? sessionsRef.current.find((session) => (
          session.runtimeSessionId === app.sourceSessionId
          && visibleSession(session)
          && !isReadOnlySession(session)
          && !isImmutableSession(session)
        )) ?? null
        : null;
      let targetSession: RuntimeSessionSummary | null = sourceSession;
      if (!targetSession && app.sourceSessionId) {
        try {
          const fetched = await userApi.runtimeSession(runtime.runtimeWorkspaceId, app.sourceSessionId);
          if (visibleSession(fetched) && !isReadOnlySession(fetched) && !isImmutableSession(fetched)) {
            targetSession = fetched;
            setSessions((current) => current.some((session) => session.runtimeSessionId === fetched.runtimeSessionId)
              ? current
              : [fetched, ...current]);
          }
        } catch {
          targetSession = null;
        }
      }
      if (!targetSession) targetSession = await createSession();
      if (!targetSession) return;
      if (activeSessionRef.current !== targetSession.runtimeSessionId) {
        chooseSession(targetSession.runtimeSessionId, targetSession);
      }
      setView("chat");
      const cachedFiles = sessionCacheRef.current.get(targetSession.runtimeSessionId)?.files ?? [];
      addFiles([packageFile], targetSession);
      setInput(`请以本次附件中的看板包为当前最新版本，继续修改「${app.name}」，不要误用历史会话中的旧文件。只修改我要求的部分，其余功能、样式、资源和目录结构保持不变；不要新增外部 CDN 或网络依赖，看板继续保持包内自包含。修改完成后，重新打包为根目录包含 index.html 的 ZIP，直接写入本次任务文件目录（不要放入 .work/，不要只保存到本地工作文件夹，也不要只输出代码），并告知文件名。我的修改要求：`);
      setEditAppContext({
        appId: app.id,
        appName: app.name,
        sessionId: targetSession.runtimeSessionId,
        startFileIds: cachedFiles.map((file) => file.fileId)
      });
      setAppUpdateDecision(null);
      setComposerFocusRequest((request) => request + 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "看板包下载失败，暂时不能编辑。");
    }
  };

  const clearEditAppState = () => {
    setEditAppContext(null);
    setAppUpdateDecision(null);
  };

  // 看板可能已在看板区被删除，而会话内仍持有发布时缓存的旧引用；
  // 更新请求 404 时用这个函数丢弃全部过期分享状态，入口随之回到「分享」新建流程。
  const forgetSharedApp = () => {
    sessionSharedAppRef.current.clear();
    setSessionSharedApp(null);
    setHtmlPreview((current) => current ? { ...current, sharedApp: null } : current);
  };

  // 会话维度解析「已分享到看板区」的看板：看板记录会记住 source_session_id，
  // 即使未从看板区「编辑」入口进入，也能在继续修改 HTML 后提示更新。
  const resolveSessionSharedApp = async (
    targetSessionId: string
  ): Promise<AppCenterListItem | null> => {
    if (sessionSharedAppRef.current.has(targetSessionId)) {
      return sessionSharedAppRef.current.get(targetSessionId) ?? null;
    }
    try {
      const apps = await userApi.listApps();
      const match = apps
        .filter((app) => app.mine && app.sourceSessionId === targetSessionId)
        .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))[0] ?? null;
      sessionSharedAppRef.current.set(targetSessionId, match);
      return match;
    } catch {
      return null;
    }
  };

  const updateEditingApp = async (options: {
    appId?: number;
    html?: string;
    version?: number;
    candidate?: EditAppUpdateCandidate;
  }) => {
    const candidate = options.candidate;
    const appId = options.appId ?? candidate?.appId ?? editAppContext?.appId;
    if (appId === undefined || editAppUpdating) return;
    setEditAppUpdating(true);
    setError(null);
    try {
      const updated = options.html
        ? await userApi.updateAppPackage(appId, { htmlContent: options.html })
        : candidate
          ? await userApi.updateAppPackage(appId, {
            file: new File(
              [await userApi.downloadChatFile(candidate.file.fileId)],
              candidate.file.displayName.endsWith(".zip")
                ? candidate.file.displayName
                : `${candidate.file.displayName}.zip`,
              { type: "application/zip" }
            )
          })
          : null;
      if (!updated) throw new Error("未找到可更新的看板内容");
      if (options.html && options.version !== undefined) {
        setHtmlPreview((current) => current && ({
          ...current,
          sharedApp: { app: updated, version: options.version ?? 0 }
        }));
      }
      if (updated.sourceSessionId) {
        sessionSharedAppRef.current.set(updated.sourceSessionId, updated);
      }
      clearEditAppState();
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === "APP_CENTER_APP_NOT_FOUND") {
        clearEditAppState();
        forgetSharedApp();
        setError("原看板已被删除，请改用「分享」重新发布");
      } else {
        setError(reason instanceof Error ? reason.message : "看板更新失败，请稍后重试");
      }
    } finally {
      setEditAppUpdating(false);
    }
  };

  const openLocalHtmlFilePreview = async (
    targetSessionId: string,
    path: string,
    runtimeRunId: string | null,
    auto = true
  ): Promise<HtmlPreviewContent | null> => {
    if (!shouldAutoOpenHtmlPreview(targetSessionId, auto)) return null;
    const desktopHtmlPreviewBridge = getDesktopHtmlPreviewBridge();
    if (!desktopHtmlPreviewBridge?.readHtmlPreview) return null;
    try {
      const html = await desktopHtmlPreviewBridge.readHtmlPreview(path);
      localHtmlPreviewTargetsRef.current.set(targetSessionId, path);
      const filename = path.replace(/\\/g, "/").split("/").pop() || "index.html";
      return openHtmlPreview(
        targetSessionId,
        `local-html-${path}`,
        html,
        runtimeRunId,
        `${targetSessionId}:local-file:${path}`,
        filename,
        auto
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "本地 HTML 文件预览失败。");
      return null;
    }
  };

  const openCloudZipHtmlPreview = async (
    targetSessionId: string,
    file: ChatFileSummary,
    auto = true
  ): Promise<HtmlPreviewContent | null> => {
    if (!shouldAutoOpenHtmlPreview(targetSessionId, auto)) return null;
    if (file.sizeBytes > MAX_APP_PACKAGE_PREVIEW_BYTES) {
      setError(`看板包「${file.displayName}」超过 100 MB，已保留在任务文件中，请下载查看。`);
      return null;
    }
    try {
      const blob = await userApi.downloadChatFile(file.fileId);
      if (blob.size > MAX_APP_PACKAGE_PREVIEW_BYTES) {
        setError(`看板包「${file.displayName}」超过 100 MB，已保留在任务文件中，请下载查看。`);
        return null;
      }
      const html = await readZipTextEntry(blob, "index.html");
      const sourceKey = `${targetSessionId}:zip-file:${file.fileId}`;
      openHtmlPreview(
        targetSessionId,
        `zip-html-${file.fileId}`,
        html,
        file.runtimeRunId ?? null,
        sourceKey,
        `${file.displayName}/index.html`,
        auto,
        { fileId: file.fileId, displayName: file.displayName }
      );
      return {
        html,
        version: htmlPreviewSourceVersionsRef.current.get(sourceKey) ?? 1,
        packageSource: { fileId: file.fileId, displayName: file.displayName }
      };
    } catch (reason) {
      if (!auto) {
        setError(reason instanceof Error ? reason.message : "看板包 HTML 预览失败，请稍后重试。");
      }
      return null;
    }
  };

  const handleAppRunCompleted = async (
    targetSessionId: string,
    runtimeRunId?: string | null
  ) => {
    const cache = sessionCacheRef.current.get(targetSessionId);
    const completedRunId = runtimeRunId ?? cache?.activeTurn?.runtimeRunId ?? cache?.runId ?? null;
    const handledRunKey = `${targetSessionId}:${completedRunId ?? "latest"}`;
    if (htmlPreviewHandledRunsRef.current.has(handledRunKey)) return;
    const htmlFile = (cache?.files ?? [])
      .filter((file) => file.sourceType === "runtime" && isHtmlChatFile(file))
      .filter((file) => completedRunId === null || file.runtimeRunId === completedRunId)
      .sort((a, b) => b.fileId - a.fileId)[0];
    const appPackageFile = (cache?.files ?? [])
        .filter((file) => file.sourceType === "runtime" && isZipChatFile(file))
        .filter((file) => completedRunId === null || file.runtimeRunId === completedRunId)
        .sort((a, b) => b.fileId - a.fileId)[0]
      ?? undefined;
    const lastAssistant = [...(cache?.messages ?? [])]
      .reverse()
      .find((message) => message.role === "assistant" && message.kind === undefined);
    const hasHtml = Boolean(lastAssistant && extractHtmlArtifacts(lastAssistant.content || "").length);
    let previewContent: HtmlPreviewContent | null = null;
    if (appPackageFile) {
      previewContent = await openCloudZipHtmlPreview(targetSessionId, appPackageFile);
    }
    if (!previewContent && htmlFile) {
      previewContent = await openCloudHtmlFilePreview(targetSessionId, htmlFile);
    } else if (getDesktopHtmlPreviewBridge()) {
      const localPaths = htmlPathsFromToolEvents(
        (cache?.messages ?? []).flatMap((message) => message.kind === "tool"
          ? [{
              runtimeRunId: message.runtimeRunId,
              name: message.name,
              status: message.status,
              input: message.input,
              result: message.result,
              preview: message.preview
            }]
          : []),
        completedRunId
      );
      for (const path of localPaths.reverse()) {
        const localPreview = await openLocalHtmlFilePreview(
          targetSessionId,
          path,
          completedRunId
        );
        if (localPreview) {
          previewContent = localPreview;
          break;
        }
      }
    }
    if (!htmlFile && lastAssistant && hasHtml) {
      previewContent = openHtmlPreviewFromMessage(targetSessionId, lastAssistant, true);
    }
    const activeEditContext = editAppContext && editAppContext.sessionId === targetSessionId
      ? editAppContext
      : null;
    if (!activeEditContext) {
      // 非「编辑」入口进入时，若会话已分享过看板且本轮产出了新预览，
      // 同样提示是否将最新渲染更新到看板区。
      if (isApplication || !previewContent) return;
      const sharedApp = await resolveSessionSharedApp(targetSessionId);
      if (!sharedApp) return;
      htmlPreviewHandledRunsRef.current.add(handledRunKey);
      if (previewContent.packageSource) {
        setAppUpdateDecision({
          sessionId: targetSessionId,
          appId: sharedApp.id,
          appName: sharedApp.name,
          file: previewContent.packageSource,
          sourceName: previewContent.packageSource.displayName
        });
        return;
      }
      setAppUpdateDecision({
        sessionId: targetSessionId,
        appId: sharedApp.id,
        appName: sharedApp.name,
        html: previewContent.html,
        version: previewContent.version,
        sourceName: htmlDocumentTitle(previewContent.html) ?? "最新 HTML"
      });
      return;
    }
    const candidate = appPackageFile
      && !activeEditContext.startFileIds.includes(appPackageFile.fileId)
      ? {
        fileId: appPackageFile.fileId,
        displayName: appPackageFile.displayName
      }
      : null;
    if (previewContent || candidate) htmlPreviewHandledRunsRef.current.add(handledRunKey);
    if (candidate) {
      setAppUpdateDecision({
        sessionId: targetSessionId,
        appId: activeEditContext.appId,
        appName: activeEditContext.appName,
        file: candidate,
        sourceName: candidate.displayName
      });
      return;
    }
    if (previewContent) {
      setAppUpdateDecision({
        sessionId: targetSessionId,
        appId: activeEditContext.appId,
        appName: activeEditContext.appName,
        html: previewContent.html,
        version: previewContent.version,
        sourceName: htmlDocumentTitle(previewContent.html) ?? "最新 HTML"
      });
    }
  };

  const dispatchStopRequest = async (request: StopRequestState, runtimeRunId: string) => {
    if (request.sent) return;
    request.sent = true;
    request.runtimeRunId = runtimeRunId;
    chatTimingRef.current.cancel(runtimeRunId);
    lifecycleTimingRef.current.cancel(runtimeRunId);
    pendingVisibleCandidatesRef.current = pendingVisibleCandidatesRef.current.filter(
      (candidate) => candidate.runtimeRunId !== runtimeRunId
    );
    for (const [kind, frame] of paintFramesRef.current) {
      if (frame.runtimeRunId !== runtimeRunId) continue;
      window.cancelAnimationFrame(frame.firstFrame);
      if (frame.secondFrame !== undefined) window.cancelAnimationFrame(frame.secondFrame);
      paintFramesRef.current.delete(kind);
    }
    try {
      const result = await userApi.stopChatRun({
        runtimeRunId,
        runtimeWorkspaceId: request.workspaceId,
        runtimeSessionId: request.sessionId
      });
      if (stopRequestBySessionRef.current.get(request.sessionId) !== request) return;
      if (result.status === "cancel_failed") {
        stopRequestBySessionRef.current.delete(request.sessionId);
        if (activeSessionRef.current === request.sessionId) {
          setStopping(false);
          setError("停止请求未送达，任务仍在执行，请重试");
        }
      } else if (["completed", "failed", "canceled"].includes(result.status)) {
        stopRequestBySessionRef.current.delete(request.sessionId);
        if (result.status === "canceled") {
          const cached = sessionCacheRef.current.get(request.sessionId) ?? { messages: [], files: [] };
          const next = cancelChatTurn(cached, runtimeRunId);
          replaceSessionCache(request.sessionId, next);
          void refreshSessionHistory(request.workspaceId, request.sessionId);
          void rearmSessionSendReady(request.workspaceId, request.sessionId);
          request.controller.abort();
          markSessionActivityTerminal(request.sessionId, runtimeRunId, false);
        }
        if (activeSessionRef.current === request.sessionId) setStopping(false);
      }
    } catch {
      if (stopRequestBySessionRef.current.get(request.sessionId) !== request) return;
      stopRequestBySessionRef.current.delete(request.sessionId);
      if (activeSessionRef.current === request.sessionId) {
        setStopping(false);
        setError("停止请求未送达，任务仍在执行，请重试");
      }
    }
  };

  const sendCorrection = async (
    workspaceId: number,
    targetSessionId: string,
    targetModelId: number | null,
    parentRuntimeRunId: string,
    content: string,
    attachmentFileIds: number[],
    attachments: ChatMessageAttachment[],
    clientMessageId: string
  ) => {
    const controller = new AbortController();
    correctionControllersRef.current.set(clientMessageId, controller);
    replaceSessionCache(
      targetSessionId,
      beginChatCorrection(
        sessionCacheRef.current.get(targetSessionId) ?? { messages: [], files: [] },
        content,
        clientMessageId,
        parentRuntimeRunId,
        attachments
      )
    );
    setInput("");
    sessionDraftsRef.current.delete(targetSessionId);
    anchorTurn(`correction-${clientMessageId}`);
    let promotedStarted = false;
    const beginPromotedRun = (promotedRunId: string) => {
      if (promotedStarted) return;
      promotedStarted = true;
      const cached = sessionCacheRef.current.get(targetSessionId)
        ?? { messages: [], files: [] };
      replaceSessionCache(
        targetSessionId,
        beginPromotedCorrectionRun(cached, clientMessageId, promotedRunId)
      );
      abortBySessionRef.current.set(targetSessionId, controller);
      if (activeSessionRef.current === targetSessionId) {
        setRunId(promotedRunId);
        setStreaming(true);
      }
    };
    await submissionIngressGateRef.current.run(targetSessionId, async (release) => {
      try {
        const dispatch = () => userApi.submitChatCorrection({
          runtimeWorkspaceId: workspaceId,
          runtimeSessionId: targetSessionId,
          modelEntryId: targetModelId,
          message: content,
          sourceType: "web",
          runtimeRunId: clientMessageId,
          parentRuntimeRunId,
          attachmentFileIds
        }, (event) => {
          if (
            event.event === "followup.received"
            || event.event === "followup.applying"
            || event.event === "followup.applied"
            || event.event === "followup.continued"
            || event.event === "followup.failed"
            || event.event === "run.redirect_pending"
            || event.event === "run.redirect_accepted"
            || event.event === "run.redirect_consumed"
            || event.event === "run.redirect_promoted"
            || event.event === "run.redirect_canceled"
            || event.event === "run.steer_submission_uncertain"
            || event.event === "chat.steer.accepted"
            || event.event === "chat.steer.consumed"
          ) release();
          const eventParentRunId = typeof event.data.parentRunId === "string"
            ? event.data.parentRunId
            : parentRuntimeRunId;
          const ordinaryRunId = typeof event.data.runtimeRunId === "string"
            ? event.data.runtimeRunId
            : undefined;
          const dispositionPromotedRunId = event.event === "followup.continued"
            && typeof event.data.continuationRunId === "string"
            ? event.data.continuationRunId
            : event.event === "run.redirect_promoted"
              && typeof event.data.promotedRunId === "string"
              ? event.data.promotedRunId
              : undefined;
          if (dispositionPromotedRunId) {
            beginPromotedRun(dispositionPromotedRunId);
          }
          if (
            (event.event === "chat.run.queued"
              || event.event === "chat.run.accepted")
            && ordinaryRunId
          ) {
            release();
            replaceSessionCache(
              targetSessionId,
              acceptQueuedChatRun(
                sessionCacheRef.current.get(targetSessionId)
                  ?? { messages: [], files: [] },
                clientMessageId,
                ordinaryRunId
              )
            );
            return;
          }
          if (
            event.event.startsWith("followup.")
            || event.event.startsWith("run.redirect_")
            || event.event === "run.steer_submission_uncertain"
            || event.event === "chat.steer.accepted"
            || event.event === "chat.steer.consumed"
          ) {
            projectSessionRunEvent(
              targetSessionId,
              eventParentRunId,
              event
            );
            return;
          }
          const promotedRunId = ordinaryRunId;
          if (!promotedRunId) return;
          if (event.event === "chat.run.started") {
            beginPromotedRun(promotedRunId);
          }
          if (!promotedStarted) {
            if (event.event === "chat.error" || event.event === "chat.done") {
              replaceSessionCache(
                targetSessionId,
                failChatCorrection(
                  sessionCacheRef.current.get(targetSessionId)
                    ?? { messages: [], files: [] },
                  clientMessageId,
                  typeof event.data.message === "string"
                    ? event.data.message
                    : "排队任务未能开始"
                )
              );
            }
            return;
          }
          observeArtifactEvent(workspaceId, targetSessionId, event);
          const result = projectSessionRunEvent(
            targetSessionId,
            promotedRunId,
            event
          );
          if (
            (event.event === "chat.done" || event.event === "chat.error")
            && abortBySessionRef.current.get(targetSessionId) === controller
          ) {
            abortBySessionRef.current.delete(targetSessionId);
            if (activeSessionRef.current === targetSessionId) {
              setStreaming(Boolean(result.cache.streaming));
              setRunId(result.cache.streaming ? result.cache.runId ?? null : null);
            }
            if (
              event.event === "chat.done"
              && result.cache.activeTurn?.status === "completed"
              && !isApplication
            ) {
              window.setTimeout(() => {
                void refreshSessionHistory(workspaceId, targetSessionId).finally(() => {
                  void handleAppRunCompleted(targetSessionId, promotedRunId);
                });
              }, 0);
            }
          }
        }, controller.signal);
        for (let attempt = 0; ; attempt += 1) {
          try {
            await dispatch();
            break;
          } catch (reason) {
            const retrySameIdentity = attempt === 0
              && !controller.signal.aborted
              && typeof reason === "object"
              && reason !== null
              && (reason as { retryable?: unknown }).retryable === true;
            if (retrySameIdentity) continue;
            throw reason;
          }
        }
      } catch (reason) {
        if (!controller.signal.aborted) {
          replaceSessionCache(
            targetSessionId,
            failChatCorrection(
              sessionCacheRef.current.get(targetSessionId) ?? { messages: [], files: [] },
              clientMessageId,
              reason instanceof Error ? reason.message : "修正发送失败"
            )
          );
        }
      } finally {
        release();
      }
    });
    if (correctionControllersRef.current.get(clientMessageId) === controller) {
      correctionControllersRef.current.delete(clientMessageId);
    }
  };

  const sendQueuedSubmission = async (
    workspaceId: number,
    targetSessionId: string,
    targetModelId: number | null,
    content: string,
    attachmentFileIds: number[],
    attachments: ChatMessageAttachment[],
    clientMessageId: string
  ) => {
    const controller = new AbortController();
    queuedSubmissionControllersRef.current.set(clientMessageId, controller);
    replaceSessionCache(
      targetSessionId,
      beginQueuedChatSubmission(
        sessionCacheRef.current.get(targetSessionId) ?? { messages: [], files: [] },
        content,
        clientMessageId,
        attachments
      )
    );
    setInput("");
    sessionDraftsRef.current.delete(targetSessionId);
    anchorTurn(`queued-user-${clientMessageId}`);
    await submissionIngressGateRef.current.run(targetSessionId, async (release) => {
      let queuedRunId: string | undefined;
      let started = false;
      try {
        const dispatch = () => userApi.streamChatRun({
          runtimeWorkspaceId: workspaceId,
          runtimeSessionId: targetSessionId,
          modelEntryId: targetModelId,
          message: content,
          sourceType: "web",
          runtimeRunId: clientMessageId,
          attachmentFileIds
        }, (event) => {
          const eventRunId = typeof event.data.runtimeRunId === "string"
            ? event.data.runtimeRunId
            : queuedRunId;
          if (event.event === "chat.run.queued") {
            queuedRunId = eventRunId;
            release();
            if (eventRunId) {
              replaceSessionCache(
                targetSessionId,
                acceptQueuedChatRun(
                  sessionCacheRef.current.get(targetSessionId)
                    ?? { messages: [], files: [] },
                  clientMessageId,
                  eventRunId
                )
              );
            }
            pendingAttachmentsRef.current.set(targetSessionId, []);
            if (activeSessionRef.current === targetSessionId) {
              setPendingAttachmentIds([]);
            }
          }
          if (event.event === "chat.run.accepted" && eventRunId) {
            queuedRunId = eventRunId;
            release();
            pendingAttachmentsRef.current.set(targetSessionId, []);
            if (activeSessionRef.current === targetSessionId) {
              setPendingAttachmentIds([]);
            }
            replaceSessionCache(
              targetSessionId,
              acceptQueuedChatRun(
                sessionCacheRef.current.get(targetSessionId) ?? { messages: [], files: [] },
                clientMessageId,
                eventRunId
              )
            );
          }
          if (!eventRunId) return;
          if (event.event === "chat.run.started") {
            started = true;
            queueSnapshotVersionGateRef.current.invalidate(targetSessionId);
            setQueueSummary((current) => removeStartedQueuedRun(
              current,
              eventRunId
            ));
            const cached = sessionCacheRef.current.get(targetSessionId)
              ?? { messages: [], files: [] };
            replaceSessionCache(
              targetSessionId,
              beginQueuedChatRun(cached, clientMessageId, eventRunId)
            );
            abortBySessionRef.current.set(targetSessionId, controller);
            if (activeSessionRef.current === targetSessionId) {
              setRunId(eventRunId);
              setStreaming(true);
            }
          }
          if (!started && (event.event === "chat.done" || event.event === "chat.error")) {
            replaceSessionCache(
              targetSessionId,
              failChatCorrection(
                sessionCacheRef.current.get(targetSessionId)
                  ?? { messages: [], files: [] },
                clientMessageId,
                typeof event.data.message === "string"
                  ? event.data.message
                  : "排队任务未能开始"
              )
            );
            return;
          }
          observeArtifactEvent(workspaceId, targetSessionId, event);
          if (started) {
            const result = projectSessionRunEvent(targetSessionId, eventRunId, event);
            if (
              event.event === "chat.done"
              && result.cache.activeTurn?.status === "completed"
              && !isApplication
            ) {
              window.setTimeout(() => {
                void refreshSessionHistory(workspaceId, targetSessionId).finally(() => {
                  void handleAppRunCompleted(targetSessionId, eventRunId);
                });
              }, 0);
            }
          }
        }, controller.signal);
        for (let attempt = 0; ; attempt += 1) {
          try {
            await dispatch();
            break;
          } catch (reason) {
            const retrySameIdentity = attempt === 0
              && !controller.signal.aborted
              && !started
              && typeof reason === "object"
              && reason !== null
              && (reason as { retryable?: unknown }).retryable === true;
            if (retrySameIdentity) continue;
            throw reason;
          }
        }
      } catch (reason) {
        if (!controller.signal.aborted && !started) {
          replaceSessionCache(
            targetSessionId,
            failChatCorrection(
              sessionCacheRef.current.get(targetSessionId) ?? { messages: [], files: [] },
              clientMessageId,
              reason instanceof Error ? reason.message : "排队消息发送失败"
            )
          );
        }
      } finally {
        release();
      }
    });
    if (queuedSubmissionControllersRef.current.get(clientMessageId) === controller) {
      queuedSubmissionControllersRef.current.delete(clientMessageId);
    }
    if (abortBySessionRef.current.get(targetSessionId) === controller) {
      abortBySessionRef.current.delete(targetSessionId);
      const cached = sessionCacheRef.current.get(targetSessionId);
      if (activeSessionRef.current === targetSessionId) {
        setStreaming(Boolean(cached?.streaming));
        setRunId(cached?.runId ?? null);
      }
    }
  };

  const send = async () => {
    if (
      !runtime
      || !activeSession
      || activeSessionReadOnly
      || (!input.trim() && !canSendWithoutText)
      || currentUploads.some((upload) => upload.status === "uploading")
    ) return;
    const draft = input.trim();
    const requestStartedAt = performance.now();
    if (currentUploads.some((upload) => upload.status === "failed")) {
      setError("请先移除或重试上传失败的附件");
      return;
    }
    const stagedUploads = currentUploads.filter((upload) => upload.status === "pending");
    const uploadedFiles = stagedUploads.length > 0
      ? await Promise.all(stagedUploads.map((upload) => uploadOne(upload)))
      : [];
    const uploadedFileIds = uploadedFiles.flatMap((file) => (
      file ? [file.fileId] : []
    ));
    if (uploadedFileIds.length !== stagedUploads.length) {
      setError("附件上传失败，消息尚未发送");
      return;
    }
    const attachmentFileIds = [...pendingAttachmentIds, ...uploadedFileIds];
    const content = draft || (attachmentFileIds.length > 1 ? "请识别这些图片" : "请识别这张图片");
    const knownFiles = [...uploadedFiles, ...files].filter(
      (file): file is ChatFileSummary => file !== null
    );
    const turnAttachments = attachmentFileIds.flatMap((fileId): ChatMessageAttachment[] => {
      const file = knownFiles.find((item) => item.fileId === fileId);
      if (file) return [chatAttachmentFromFile(file)];
      return Number.isInteger(fileId) && fileId > 0
        ? [{ fileId, displayName: `附件 ${fileId}` }]
        : [];
    });
    let ttftCaptured = false;
    let lifecycleTtftCaptured = false;
    const targetSessionId = activeSession.runtimeSessionId;
    const targetModelId = modelId;
    const cached = sessionCacheRef.current.get(targetSessionId);
    if (streaming || cached?.streaming) {
      const clientMessageId = createWebRuntimeRunId();
      const parentRuntimeRunId = cached?.runId
        ?? cached?.activeTurn?.runtimeRunId
        ?? runId;
      if (!parentRuntimeRunId) return;
      pendingAttachmentsRef.current.set(targetSessionId, []);
      setPendingAttachmentIds([]);
      void sendCorrection(
        runtime.runtimeWorkspaceId,
        targetSessionId,
        targetModelId,
        parentRuntimeRunId,
        content,
        attachmentFileIds,
        turnAttachments,
        clientMessageId
      );
      return;
    }
    const shouldAutoTitle = activeSession.messageCount === 0 && isDefaultTaskTitle(activeSession.title);
    const controller = new AbortController();
    abortBySessionRef.current.set(targetSessionId, controller);
    const turnId = createWebRuntimeRunId();
    let requiredGate: RequiredPrewarmGate | null = null;
    if (!isApplication) {
      requiredGate = requiredPrewarmGateRef.current;
      if (!requiredTargetMatches(
        requiredGate,
        runtime.runtimeWorkspaceId,
        activeSession,
        targetModelId
      )) {
        sessionPrewarmCoordinatorRef.current.invalidate(
          sessionPrewarmKey(runtime.runtimeWorkspaceId, targetSessionId)
        );
        requiredGate = beginRequiredPrewarm(
          runtime.runtimeWorkspaceId,
          activeSession,
          targetModelId
        );
      }
    }
    const submission: SubmissionState = {
      sessionId: targetSessionId,
      turnId,
      controller,
      phase: requiredGate && requiredGate.status !== "ready"
        ? "required-prewarm"
        : "submitting"
    };
    submissionBySessionRef.current.set(targetSessionId, submission);
    if (requiredGate && requiredGate.status !== "ready") {
      const queued = queuedTurnDispatcherRef.current.enqueue({
        runtimeRunId: turnId,
        target: requiredGate.target,
        payload: {
          content,
          attachmentFileIds: [...attachmentFileIds]
        },
        userClickedAt: requestStartedAt
      });
      if (!queued) {
        submissionBySessionRef.current.delete(targetSessionId);
        abortBySessionRef.current.delete(targetSessionId);
        controller.abort();
        setError("当前已有一条消息等待发送");
        return;
      }
      dispatchSessionReadiness({
        type: "queued",
        target: requiredGate.target
      });
    }
    const prewarmCoordinator = sessionPrewarmCoordinatorRef.current;
    const sendOverlappedPrewarm = !isApplication
      && requiredGate?.status === "ready"
      && typeof prewarmCoordinator.has === "function"
      && prewarmCoordinator.has(
        sessionPrewarmKey(runtime.runtimeWorkspaceId, targetSessionId)
      );
    chatTimingRef.current.begin(
      turnId,
      requestStartedAt,
      currentDocumentVisibility(),
      sendOverlappedPrewarm
    );
    lifecycleTimingRef.current.begin(
      turnId,
      requestStartedAt,
      currentDocumentVisibility(),
      sendOverlappedPrewarm
    );
    timingKeyBySessionRef.current.set(targetSessionId, turnId);
    const started = beginChatTurn(
      sessionCacheRef.current.get(targetSessionId) ?? { messages: [], files: [] },
      content,
      turnId,
      "",
      turnAttachments
    );
    let assistantId = started.activeTurn?.assistantMessageId;
    let streamRuntimeRunId: string | undefined;
    let sawDone = false;
    let promptDispatchAttempted = false;
    const finalizeAcceptedRun = () => {
      const completed = sessionCacheRef.current.get(targetSessionId)?.activeTurn?.status === "completed";
      if (!controller.signal.aborted && shouldAutoTitle && completed) {
        void autoTitleFirstMessage(runtime.runtimeWorkspaceId, targetSessionId, content);
      }
      if (!controller.signal.aborted) {
        // Keep the exact prepared Session hot after a successful Run. Session
        // or model changes still enter the required prewarm gate; starting a
        // second prewarm here would make an immediate follow-up wait on it.
        window.setTimeout(() => {
          void refreshSessionHistory(
            runtime.runtimeWorkspaceId,
            targetSessionId
          ).finally(() => {
            if (!isApplication && completed) {
              void handleAppRunCompleted(targetSessionId, streamRuntimeRunId ?? null);
            }
          });
        }, 0);
        if (streamRuntimeRunId) {
          observeSelfEvolution(runtime.runtimeWorkspaceId, targetSessionId, streamRuntimeRunId);
        }
      }
    };
    setInput("");
    sessionDraftsRef.current.delete(targetSessionId);
    setStreaming(true);
    markSessionActivityRunning(targetSessionId, turnId);
    anchorTurn(started.activeTurn?.userMessageId ?? `user-${turnId}`);
    replaceSessionCache(targetSessionId, started);
    foregroundCriticalSessionsRef.current.add(targetSessionId);
    try {
      if (requiredGate && requiredGate.status !== "ready") {
        const requiredPrewarmWaitStartedAt = performance.now();
        try {
          await waitForRequiredPrewarmWithRetry(
            requiredGate,
            controller
          );
        } finally {
          const requiredPrewarmWaitMs = Math.max(
            0,
            performance.now() - requiredPrewarmWaitStartedAt
          );
          chatTimingRef.current.markRequiredPrewarmWait(
            turnId,
            requiredPrewarmWaitMs
          );
          lifecycleTimingRef.current.markRequiredPrewarmWait(
            turnId,
            requiredPrewarmWaitMs
          );
        }
        if (controller.signal.aborted) return;
        if (
          submissionBySessionRef.current.get(targetSessionId) !== submission
          || requiredPrewarmGateRef.current !== requiredGate
          || activeSessionRef.current !== targetSessionId
          || !requiredTargetMatches(
            requiredGate,
            runtime.runtimeWorkspaceId,
            activeSession,
            targetModelId
          )
        ) {
          cancelWaitingSubmission(submission);
          return;
        }
        if (
          !queuedTurnDispatcherRef.current.claimForDispatch(
            turnId,
            requiredGate.target
          )
        ) {
          cancelWaitingSubmission(submission);
          return;
        }
        submission.phase = "submitting";
        dispatchSessionReadiness({
          type: "dispatch_claimed",
          target: requiredGate.target
        });
      }
      const readyReceipt = requiredGate?.receipt ?? null;
      const streamPayload = {
        runtimeWorkspaceId: runtime.runtimeWorkspaceId,
        runtimeSessionId: targetSessionId,
        modelEntryId: targetModelId,
        attachmentFileIds,
        message: content,
        sourceType: "web" as const,
        runtimeRunId: turnId,
        ...(
          readyReceipt?.readyReceiptId
            ? { readyReceiptId: readyReceipt.readyReceiptId }
            : {}
        ),
        ...(isApplication ? { requestId: turnId } : {})
      };
      const dispatchStartedAt = performance.now();
      const receiptIssuedAt = readyReceipt?.issuedAt
        ? Date.parse(readyReceipt.issuedAt)
        : Number.NaN;
      const receiptAgeMs = Number.isFinite(receiptIssuedAt)
        ? Math.max(0, Date.now() - receiptIssuedAt)
        : undefined;
      chatTimingRef.current.markDispatchStarted(
        turnId,
        dispatchStartedAt,
        receiptAgeMs
      );
      lifecycleTimingRef.current.markDispatchStarted(
        turnId,
        dispatchStartedAt,
        receiptAgeMs
      );
      if (requiredGate) {
        requiredGate.receipt = null;
        requiredGate.status = "pending";
      }
      queuedTurnDispatcherRef.current.complete(turnId);
      const dispatchStream = () => userApi.streamChatRun(
        streamPayload,
        (event) => {
        const receivedAt = performance.now();
        const eventRunId = typeof event.data.runtimeRunId === "string" ? event.data.runtimeRunId : undefined;
          if (event.event === "chat.transport.reconnecting" || event.event === "chat.transport.resumed") {
            const cached = sessionCacheRef.current.get(targetSessionId) ?? { messages: [], files: [] };
            const next = projectChatRunEvent(
              cached,
              eventRunId ?? streamRuntimeRunId ?? turnId,
              event
            );
            replaceSessionCache(targetSessionId, next.cache);
            if (activeSessionRef.current === targetSessionId) {
              setStreaming(Boolean(next.cache.streaming));
            }
            return;
          }
          chatTimingRef.current.markFirstEvent(streamRuntimeRunId ?? turnId, receivedAt);
          lifecycleTimingRef.current.markFirstEvent(streamRuntimeRunId ?? turnId, receivedAt);
        if (event.event === "chat.done") sawDone = true;
        if (
          (
            event.event === "chat.run.accepted"
            || event.event === "chat.reconnect.accepted"
          )
          && eventRunId
        ) {
          chatTimingRef.current.accept(turnId, eventRunId);
          lifecycleTimingRef.current.accept(turnId, eventRunId);
          timingKeyBySessionRef.current.set(targetSessionId, eventRunId);
          pendingAttachmentsRef.current.set(targetSessionId, []);
          if (activeSessionRef.current === targetSessionId) setPendingAttachmentIds([]);
          const cached = sessionCacheRef.current.get(targetSessionId) ?? { messages: [], files: [] };
          const next = acceptChatRun(cached, eventRunId, assistantId);
          replaceSessionCache(targetSessionId, next);
          streamRuntimeRunId = eventRunId;
          assistantId = `assistant-${eventRunId}`;
          if (activeSessionRef.current === targetSessionId) setRunId(eventRunId);
          const queueRequestVersion = queueSnapshotVersionGateRef.current.issue(
            targetSessionId
          );
          void userApi.chatQueueSummary(
            runtime.runtimeWorkspaceId,
            targetSessionId
          ).then((summary) => {
            applyQueueSnapshot(targetSessionId, queueRequestVersion, summary);
          }).catch(() => undefined);
          if (requiredGate) {
            dispatchSessionReadiness({
              type: "streaming",
              target: requiredGate.target
            });
          }
          const pendingStop = stopRequestBySessionRef.current.get(targetSessionId);
          if (
            pendingStop?.controller === controller
            && pendingStop.sessionId === targetSessionId
          ) {
            void dispatchStopRequest(pendingStop, eventRunId);
          }
        }
        const projectionRunId = eventRunId ?? streamRuntimeRunId;
        observeArtifactEvent(runtime.runtimeWorkspaceId, targetSessionId, event);
        if (projectionRunId) {
          const lifecycleTtftMs = !lifecycleTtftCaptured && event.event === "chat.run.started"
            ? Math.max(0, performance.now() - requestStartedAt)
            : undefined;
          const contentTtftMs = !ttftCaptured && isFirstVisibleAgentEvent(event)
            ? Math.max(0, performance.now() - requestStartedAt)
            : undefined;
          const result = projectSessionRunEvent(targetSessionId, projectionRunId, {
            ...event,
            ...(lifecycleTtftMs !== undefined
              ? { ttftMs: lifecycleTtftMs }
              : contentTtftMs !== undefined
                ? { ttftMs: contentTtftMs }
                : {})
          }, receivedAt);
          if (lifecycleTtftMs !== undefined && result.applied) lifecycleTtftCaptured = true;
          if (contentTtftMs !== undefined && result.applied) ttftCaptured = true;
          if (event.event === "chat.done") {
            const pendingStop = stopRequestBySessionRef.current.get(targetSessionId);
            const stopMatches = pendingStop?.sessionId === targetSessionId
              && (!pendingStop.runtimeRunId || pendingStop.runtimeRunId === projectionRunId);
            if (stopMatches) {
              stopRequestBySessionRef.current.delete(targetSessionId);
              if (activeSessionRef.current === targetSessionId) setStopping(false);
            }
            const doneStatus = String(event.data.status ?? "").toLowerCase();
            if (CANCELED_DONE_STATUSES.has(doneStatus)) {
              const cached = sessionCacheRef.current.get(targetSessionId) ?? { messages: [], files: [] };
              const next = cancelChatTurn(cached, projectionRunId);
              replaceSessionCache(targetSessionId, next);
              if (activeSessionRef.current === targetSessionId) {
                setStreaming(Boolean(next.streaming));
                setRunId(next.runId ?? null);
              }
              void refreshSessionHistory(runtime.runtimeWorkspaceId, targetSessionId);
              window.setTimeout(() => controller.abort(), 0);
            }
          }
          if (
            (event.event === "chat.error" || event.event === "chat.done")
            && !pendingVisibleCandidatesRef.current.some((candidate) => (
              candidate.runtimeRunId === projectionRunId && candidate.timingKind === "content"
            ))
            && !(
              paintFramesRef.current.get("content")?.runtimeRunId === projectionRunId
            )
          ) {
            if (chatTimingRef.current.has(projectionRunId)) {
              chatTimingRef.current.cancel(projectionRunId);
            }
            if (
              !pendingVisibleCandidatesRef.current.some((candidate) => (
                candidate.runtimeRunId === projectionRunId && candidate.timingKind === "lifecycle"
              ))
              && !(
                paintFramesRef.current.get("lifecycle")?.runtimeRunId === projectionRunId
              )
            ) {
              lifecycleTimingRef.current.cancel(projectionRunId);
            }
            if (timingKeyBySessionRef.current.get(targetSessionId) === projectionRunId) {
              timingKeyBySessionRef.current.delete(targetSessionId);
            }
          }
        }
        },
        controller.signal
      );
      promptDispatchAttempted = true;
      for (let transportAttempt = 0; ; transportAttempt += 1) {
        try {
          await dispatchStream();
          break;
        } catch (reason) {
          const retryableBeforeAcceptance = (
            transportAttempt === 0
            && !streamRuntimeRunId
            && typeof reason === "object"
            && reason !== null
            && (reason as { retryable?: unknown }).retryable === true
          );
          if (retryableBeforeAcceptance) continue;
          throw reason;
        }
      }
      if (!controller.signal.aborted && streamRuntimeRunId && !sawDone) {
        if (submissionBySessionRef.current.get(targetSessionId)?.controller === controller) {
          submissionBySessionRef.current.delete(targetSessionId);
        }
        const recovered = await recoverSession(
          runtime.runtimeWorkspaceId,
          targetSessionId,
          undefined,
          { preferredRuntimeRunId: streamRuntimeRunId }
        );
        if (recovered !== "terminal") return;
      }
      finalizeAcceptedRun();
    } catch (reason) {
      // A failed required prewarm never reached Runtime, so its local queued
      // turn must be released. Otherwise the single-flight dispatcher keeps
      // rejecting sends in every other conversation until the page reloads.
      if (
        submissionBySessionRef.current.get(targetSessionId) === submission
        && submission.phase === "required-prewarm"
      ) {
        cancelWaitingSubmission(submission);
        sessionDraftsRef.current.set(targetSessionId, content);
        if (activeSessionRef.current === targetSessionId) {
          setInput(content);
          setError(formatRuntimeSessionPrepareError(reason));
        }
        markSessionActivityTerminal(targetSessionId, turnId, false);
        return;
      }
      if (!controller.signal.aborted) {
        if (sawDone) {
          finalizeAcceptedRun();
          return;
        }
        // The request was accepted by the platform, so a later fetch/SSE
        // disconnect says nothing about the task itself.  Switch to the
        // durable run replay endpoint instead of turning a live task into a
        // visible "network error".
        if (streamRuntimeRunId) {
          if (submissionBySessionRef.current.get(targetSessionId)?.controller === controller) {
            submissionBySessionRef.current.delete(targetSessionId);
          }
          const recovered = await recoverSession(
            runtime.runtimeWorkspaceId,
            targetSessionId,
            undefined,
            { preferredRuntimeRunId: streamRuntimeRunId }
          );
          if (recovered === "terminal") finalizeAcceptedRun();
          if (recovered) return;
        }
        sessionDraftsRef.current.set(targetSessionId, content);
        if (activeSessionRef.current === targetSessionId) {
          setInput(content);
        }
        replaceSessionCache(targetSessionId, failChatTurn(
          sessionCacheRef.current.get(targetSessionId) ?? started,
          reason instanceof Error ? reason.message : "发送失败"
        ));
        markSessionActivityTerminal(targetSessionId, streamRuntimeRunId ?? turnId, false);
      }
    } finally {
      foregroundCriticalSessionsRef.current.delete(targetSessionId);
      resumeSelfEvolutionObserver(targetSessionId);
      if (!streamRuntimeRunId) {
        chatTimingRef.current.cancel(turnId);
        lifecycleTimingRef.current.cancel(turnId);
        if (timingKeyBySessionRef.current.get(targetSessionId) === turnId) {
          timingKeyBySessionRef.current.delete(targetSessionId);
        }
        if (promptDispatchAttempted && !isApplication) {
          void rearmSessionSendReady(
            runtime.runtimeWorkspaceId,
            targetSessionId
          );
        }
      }
      if (abortBySessionRef.current.get(targetSessionId) === controller) {
        abortBySessionRef.current.delete(targetSessionId);
        if (activeSessionRef.current === targetSessionId) {
          setStreaming(false);
          setRunId(null);
        }
      }
      if (submissionBySessionRef.current.get(targetSessionId)?.controller === controller) {
        submissionBySessionRef.current.delete(targetSessionId);
      }
      const pendingStop = stopRequestBySessionRef.current.get(targetSessionId);
      if (pendingStop?.controller === controller && !streamRuntimeRunId) {
        stopRequestBySessionRef.current.delete(targetSessionId);
        if (activeSessionRef.current === targetSessionId) setStopping(false);
      }
    }
  };
  const stop = () => {
    const targetRunId = runId;
    const targetSessionId = activeSessionRef.current;
    const targetWorkspaceId = runtime?.runtimeWorkspaceId;
    const controller = targetSessionId
      ? abortBySessionRef.current.get(targetSessionId)
      : undefined;
    if (!controller || !targetSessionId || targetWorkspaceId === undefined) return;
    const submission = submissionBySessionRef.current.get(targetSessionId);
    if (
      !targetRunId
      && submission?.controller === controller
      && submission.sessionId === targetSessionId
      && submission.phase === "required-prewarm"
    ) {
      cancelWaitingSubmission(submission);
      return;
    }
    const existing = stopRequestBySessionRef.current.get(targetSessionId);
    if (existing?.controller === controller && existing.sessionId === targetSessionId) return;
    const request: StopRequestState = {
      sessionId: targetSessionId,
      workspaceId: targetWorkspaceId,
      controller,
      ...(targetRunId ? { runtimeRunId: targetRunId } : {}),
      sent: false,
    };
    stopRequestBySessionRef.current.set(targetSessionId, request);
    setStopping(true);
    if (targetRunId) void dispatchStopRequest(request, targetRunId);
  };
  const selectSessionModel = async (nextModelId: number) => {
    if (activeSessionReadOnly) return;
    if (!runtime || !activeSession) {
      setModelId(nextModelId);
      return;
    }
    const previousModelId = modelId;
    const workspaceId = runtime.runtimeWorkspaceId;
    const targetSession = activeSession;
    const key = sessionPrewarmKey(
      workspaceId,
      targetSession.runtimeSessionId
    );
    setModelId(nextModelId);
    if (isApplication) {
      try {
        const result = await userApi.setSessionModel(
          workspaceId,
          targetSession.runtimeSessionId,
          nextModelId,
        );
        setSessions((items) => items.map((item) => (
          item.runtimeSessionId === targetSession.runtimeSessionId
            ? { ...item, sessionModelEntryId: result.modelEntryId }
            : item
        )));
        sessionPrewarmCoordinatorRef.current.invalidate(key);
        void ensureSessionPrewarm(
          workspaceId,
          targetSession.runtimeSessionId
        ).catch(() => undefined);
      } catch (reason) {
        setModelId(previousModelId);
        setError(reason instanceof Error ? reason.message : "切换模型失败");
      }
      return;
    }
    sessionPrewarmCoordinatorRef.current.invalidate(key);
    let modelPersisted = false;
    let requiredGate: RequiredPrewarmGate | null = null;
    requiredGate = beginRequiredPrewarm(
      workspaceId,
      targetSession,
      nextModelId,
      async () => {
        const result = await userApi.setSessionModel(
          workspaceId,
          targetSession.runtimeSessionId,
          nextModelId,
        );
        modelPersisted = true;
        setSessions((items) => items.map((item) => (
          item.runtimeSessionId === targetSession.runtimeSessionId
            ? { ...item, sessionModelEntryId: result.modelEntryId }
            : item
        )));
        if (requiredPrewarmGateRef.current !== requiredGate) return;
        sessionPrewarmCoordinatorRef.current.invalidate(key);
        return await ensureSessionPrewarm(
          workspaceId,
          targetSession.runtimeSessionId
        );
      }
    );
    try {
      await requiredGate?.promise;
    } catch (reason) {
      if (requiredPrewarmGateRef.current !== requiredGate) return;
      if (!modelPersisted) {
        setModelId(previousModelId);
        const restoredSession = {
          ...targetSession,
          sessionModelEntryId: previousModelId
        };
        sessionPrewarmCoordinatorRef.current.invalidate(key);
        const restoredGate = beginRequiredPrewarm(
          workspaceId,
          restoredSession,
          previousModelId
        );
        void restoredGate?.promise.catch(() => undefined);
      }
      setError(reason instanceof Error ? reason.message : "切换模型失败");
    }
  };
  const warmup = async () => {
    setRuntimeConnectionState("waking");
    sessionPrewarmCoordinatorRef.current.clear();
    try {
      setRuntime(await userApi.warmupRuntime());
      await load();
    } catch (reason) {
      setRuntimeConnectionState("failed");
      setError(formatRuntimeSessionPrepareError(reason, "预热失败"));
    }
  };
  const downloadFile = async (file: Pick<ChatFileSummary, "fileId" | "displayName">) => {
    try {
      const blob = await userApi.downloadChatFile(file.fileId);
      const href = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = file.displayName;
      anchor.click();
      URL.revokeObjectURL(href);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "文件下载失败");
    }
  };
  const previewFile = async (file: ChatFileSummary) => {
    if (!isApplication && sessionId && isHtmlChatFile(file)) {
      await openCloudHtmlFilePreview(sessionId, file, false);
      return;
    }
    if (!isApplication && sessionId && isZipChatFile(file)) {
      await openCloudZipHtmlPreview(sessionId, file, false);
      return;
    }
    const requestId = previewRequestRef.current + 1;
    previewRequestRef.current = requestId;
    if (previewUrlRef.current) {
      URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = null;
    }
    setFilePreview({ file, status: "loading" });
    try {
      const previewKind = filePreviewKind(file);
      if (isApplication && previewKind === "office") {
        if (previewRequestRef.current === requestId) {
          setFilePreview({ file, status: "ready", kind: "unsupported" });
        }
        return;
      }
      if (previewKind === "office") {
        const office = await userApi.officePreviewSession(file.fileId);
        if (previewRequestRef.current === requestId) setFilePreview({ file, status: "ready", kind: "office", office });
        return;
      }
      const blob = await userApi.downloadChatFile(file.fileId);
      if (previewRequestRef.current !== requestId) return;
      const kind = filePreviewKind(file, blob.type);
      if (kind === "text") {
        const truncated = blob.size > MAX_TEXT_PREVIEW_BYTES;
        const text = await readBlobText(blob.slice(0, MAX_TEXT_PREVIEW_BYTES));
        if (previewRequestRef.current === requestId) setFilePreview({ file, status: "ready", kind, text, truncated });
        return;
      }
      if (kind === "unsupported") {
        setFilePreview({ file, status: "ready", kind });
        return;
      }
      const url = URL.createObjectURL(blob);
      previewUrlRef.current = url;
      setFilePreview({ file, status: "ready", kind, url });
    } catch (reason) {
      if (previewRequestRef.current === requestId) setFilePreview({ file, status: "error", error: reason instanceof Error ? reason.message : "文件预览失败" });
    }
  };
  const resolveChatAttachmentImageUrl = useCallback(
    async (attachment: ChatMessageAttachment) => {
      if (
        isApplication
        && applicationAgentId !== null
        && activeSession?.sourceType === "wecom"
        && !isRuntimeArtifactAttachment(attachment)
      ) {
        const blob = await platformUserApi.applicationAgentDownloadWecomAttachment(
          applicationAgentId,
          attachment.fileId
        );
        return URL.createObjectURL(blob);
      }
      const blob = await userApi.downloadChatFile(attachment.fileId);
      return URL.createObjectURL(blob);
    },
    [
      activeSession?.sourceType,
      applicationAgentId,
      isApplication,
      userApi
    ]
  );
  const openChatAttachmentPreview = useCallback((
    attachment: ChatMessageAttachment,
    _objectUrl: string
  ) => {
    const isApplicationWecom = isApplication
      && applicationAgentId !== null
      && activeSession?.sourceType === "wecom"
      && !isRuntimeArtifactAttachment(attachment);
    const file = isApplicationWecom
      ? undefined
      : files.find((item) => item.fileId === attachment.fileId);
    if (file) {
      void previewFile(file);
      return;
    }
    if (isApplicationWecom && applicationAgentId !== null) {
      const applicationId = applicationAgentId;
      void (async () => {
        const requestId = previewRequestRef.current + 1;
        previewRequestRef.current = requestId;
        try {
          const blob = await platformUserApi.applicationAgentDownloadWecomAttachment(
            applicationId,
            attachment.fileId
          );
          if (previewRequestRef.current !== requestId) return;
          const url = URL.createObjectURL(blob);
          previewUrlRef.current = url;
          setFilePreview({
            file: {
              fileId: attachment.fileId,
              runtimeWorkspaceId: runtime?.runtimeWorkspaceId ?? 0,
              runtimeSessionId: sessionId,
              relativePath: "",
              displayName: attachment.displayName,
              mimeType: attachment.mimeType ?? null,
              sizeBytes: blob.size,
              sourceType: "wecom"
            },
            status: "ready",
            kind: "image",
            url
          });
        } catch (reason) {
          if (previewRequestRef.current !== requestId) return;
          setFilePreview({
            file: {
              fileId: attachment.fileId,
              runtimeWorkspaceId: runtime?.runtimeWorkspaceId ?? 0,
              runtimeSessionId: sessionId,
              relativePath: "",
              displayName: attachment.displayName,
              mimeType: attachment.mimeType ?? null,
              sizeBytes: 0,
              sourceType: "wecom"
            },
            status: "error",
            error: reason instanceof Error ? reason.message : "图片预览失败"
          });
        }
      })();
      return;
    }
    void previewFile({
      fileId: attachment.fileId,
      runtimeWorkspaceId: runtime?.runtimeWorkspaceId ?? 0,
      runtimeSessionId: sessionId,
      relativePath: "",
      displayName: attachment.displayName,
      mimeType: attachment.mimeType ?? null,
      sizeBytes: 0,
      sourceType: "upload"
    });
  }, [
    activeSession?.sourceType,
    applicationAgentId,
    files,
    isApplication,
    previewFile,
    runtime?.runtimeWorkspaceId,
    sessionId
  ]);
  const closeFilePreview = () => {
    previewRequestRef.current += 1;
    if (previewUrlRef.current) {
      URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = null;
    }
    setFilePreview(null);
  };
  const renameFile = async (file: ChatFileSummary, displayName: string) => {
    if (activeSessionReadOnly) return;
    const parent = file.relativePath.slice(0, Math.max(0, file.relativePath.lastIndexOf("/") + 1));
    try {
      const renamed = await userApi.renameChatFile(file.fileId, { relativePath: `${parent}${displayName}` });
      const nextFiles = files.map((item) => item.fileId === renamed.fileId ? renamed : item);
      if (sessionId) replaceSessionFiles(sessionId, nextFiles);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "文件重命名失败");
    }
  };
  const deleteFile = async (file: Pick<ChatFileSummary, "fileId" | "displayName">) => {
    if (activeSessionReadOnly) return;
    if (!window.confirm(`确定删除“${file.displayName}”吗？此操作无法恢复。`)) return;
    try {
      await userApi.deleteChatFile(file.fileId);
      const nextFiles = files.filter((item) => item.fileId !== file.fileId);
      if (sessionId) {
        replaceSessionFiles(sessionId, nextFiles);
        const nextPending = pendingAttachmentIds.filter((id) => id !== file.fileId);
        pendingAttachmentsRef.current.set(sessionId, nextPending);
        setPendingAttachmentIds(nextPending);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "文件删除失败");
    }
  };
  const removePendingAttachment = (fileId: number) => {
    if (!sessionId) return;
    const nextPending = pendingAttachmentIds.filter((id) => id !== fileId);
    pendingAttachmentsRef.current.set(sessionId, nextPending);
    setPendingAttachmentIds(nextPending);
  };
  const changeView = (next: View) => {
    if (next === "chat" && view !== "chat") setComposerFocusRequest((request) => request + 1);
    setView(next);
  };
  const previewWelcomeTask = (prompt: string | null) => {
    setWelcomeTaskPreview(prompt);
  };
  const commitWelcomeTask = (prompt: string) => {
    setWelcomeTaskPreview(null);
    setInput(prompt);
    setComposerFocusRequest((request) => request + 1);
  };
  const changeComposerInput = (value: string) => {
    setWelcomeTaskPreview(null);
    setInput(value);
  };
  const openWelcomeBoard = (board: AppCenterListItem) => {
    window.open(board.accessUrl, "_blank", "noopener,noreferrer");
  };
  const toggleFiles = () => setFilesOpen((open) => { const next = !open; localStorage.setItem("hermes.files-pane", String(next)); return next; });
  const renderKnowledgePage = () => {
    return (
      <Suspense fallback={<div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 48, color: "var(--text-muted)" }}><LoaderCircle className="uploadSpinner" size={28} /><span>正在加载知识库…</span></div>}>
        <KnowledgePanelView runtimeWorkspaceId={runtime?.runtimeWorkspaceId ?? null} />
      </Suspense>
    );
  };
  const renderAppCenterPage = () => {
    return (
      <Suspense fallback={<div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 48, color: "var(--text-muted)" }}><LoaderCircle className="uploadSpinner" size={28} /><span>正在加载看板区…</span></div>}>
        <AppCenterPanelView onEditApp={(app) => void startEditApp(app)} onCreateAppSession={startCreateAppSession} onCreateFromTemplate={startCreateAppFromTemplate} />
      </Suspense>
    );
  };
  // 聊天视图右侧 HTML 预览面板：run 完成后才出现，不在首 Token 路径；
  // 懒加载 chunk 与看板区页共享，二次渲染同步命中缓存。
  const renderExpertSkillsPage = () => {
    return (
      <Suspense fallback={<div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 48, color: "var(--text-muted)" }}><LoaderCircle className="uploadSpinner" size={28} /><span>正在加载专家-技能…</span></div>}>
        <ExpertSkillsPanelView runtimeWorkspaceId={runtime?.runtimeWorkspaceId ?? null} onInstalledExpertsChange={applyInstalledExperts} onStartExpertChat={(expert) => void startExpertConversation(expert.runtimeAgentProfileId)} />
      </Suspense>
    );
  };
  const renderSchedulesPage = () => {
    return (
      <Suspense fallback={<div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 48, color: "var(--text-muted)" }}><LoaderCircle className="uploadSpinner" size={28} /><span>正在加载定时任务…</span></div>}>
        <SchedulesPanelView runtimeWorkspaceId={runtime?.runtimeWorkspaceId ?? null} sessions={sessions} onOpenSession={openScheduledTaskSession} onRetryWorkspace={() => void warmup()} />
      </Suspense>
    );
  };
  const renderHtmlPreviewPanel = () => {
    if (!htmlPreview) return null;
    return (
      <Suspense fallback={<div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 24, color: "var(--text-muted)" }}><LoaderCircle className="uploadSpinner" size={16} /><span>正在打开预览…</span></div>}>
        <HtmlPreviewPanel html={htmlPreview.html} packageSource={htmlPreview.packageSource} version={htmlPreview.version} sourceSessionId={htmlPreview.sessionId} sourceRunId={htmlPreview.sourceRunId} sourceName={htmlPreview.sourceName} fallbackName={activeSession?.title || htmlDocumentTitle(htmlPreview.html) || "未命名看板"} sharedApp={htmlPreview.sharedApp} previouslySharedApp={htmlPreview.sharedApp?.app ?? sessionSharedApp} editTargetName={editAppContext?.sessionId === sessionId ? editAppContext.appName : null} onSharedAppChange={(value) => { setHtmlPreview((current) => current ? { ...current, sharedApp: value } : current); if (!htmlPreview) return; const app = value?.app ?? null; sessionSharedAppRef.current.set(htmlPreview.sessionId, app); setSessionSharedApp(app); }} onUpdateApp={(html, version) => void updateEditingApp({ html, version })} onAppMissing={() => forgetSharedApp()} onUpdateAppPackage={(packageSource, version) => void updateEditingApp({ candidate: { appId: editAppContext?.appId ?? 0, appName: editAppContext?.appName ?? '', file: packageSource }, version })} onClose={() => { if (sessionId) htmlPreviewDismissedRef.current.set(sessionId, htmlPreviewSignature(htmlPreview.sourceKey, htmlPreview.html)); setHtmlPreview(null); }} onOpenCenter={() => changeView("app-center")} />
      </Suspense>
    );
  };
  // 聊天消息流内「更新已分享看板」提示卡：出现时看板区 chunk 通常已加载。
  const renderAppUpdateClarifyCard = () => {
    if (!appUpdateDecision || appUpdateDecision.sessionId !== sessionId) return null;
    return (
      <Suspense fallback={null}>
        <AppUpdateClarifyCard appName={appUpdateDecision.appName} sourceName={appUpdateDecision.sourceName} busy={editAppUpdating} onConfirm={() => void updateEditingApp(appUpdateDecision.html !== undefined ? { appId: appUpdateDecision.appId, html: appUpdateDecision.html, version: appUpdateDecision.version } : appUpdateDecision.file ? { candidate: { appId: appUpdateDecision.appId, appName: appUpdateDecision.appName, file: appUpdateDecision.file } } : {})} onDismiss={clearEditAppState} />
      </Suspense>
    );
  };
  const renderMcpPage = () => (
    <Suspense fallback={<div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 48, color: "var(--text-muted)" }}><LoaderCircle className="uploadSpinner" size={28} /><span>正在加载 MCP…</span></div>}>
      <McpPanelView runtimeWorkspaceId={runtime?.runtimeWorkspaceId ?? null} />
    </Suspense>
  );
  const renderImPage = () => (
    <Suspense fallback={<div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 48, color: "var(--text-muted)" }}><LoaderCircle className="uploadSpinner" size={28} /><span>正在加载 IM…</span></div>}>
      <ImPanelView onChannelChanged={() => void load()} />
    </Suspense>
  );
  const runtimeStartupContextVisible = !isApplication
    && (
      runtimeStartupContext === "first_allocation"
      || runtimeStartupContext === "environment_recovery"
    )
    && !runtimeAwake
    && runtimeConnectionState !== "failed";
  const showRuntimeAllocatingHint = !error && (
    runtimeStartupContextVisible
    || waitingForPrewarmSend
  );
  const coldStartupContextLabelActive = runtimeStartupContextVisible
    || (
      waitingForPrewarmSend
      && (
        runtimeStartupContext === "first_allocation"
        || runtimeStartupContext === "environment_recovery"
      )
    );
  const runtimeAllocatingLabel = coldStartupContextLabelActive
    ? runtimeStartupContext === "first_allocation"
      ? "首次登录，给您分配云上资源，稍等，您可以先输入任务……"
      : "环境恢复中，马上好，您可以先输入任务……"
    : "会话准备中，你的消息将在就绪后自动发送…";
  const composerActionState = chatComposerAction({
    draft: input,
    hasActiveRun: streaming,
    uploadPending: currentUploads.some((upload) => upload.status !== "pending"),
    recovering: runtimeConnectionState === "connecting" && !runtimeAwake,
    cancellationPending: stopping,
    canSendWithoutText
  });
  const showFilesPane = filesOpen && !htmlPreview;
  const welcomeOwnerIdentity = principal?.name
    ? `${principal.name}${principal.userCode ? `-${principal.userCode}` : ""}`
    : null;
  const isDefaultPersonalWelcome = !isApplication
    && activeSession?.agentProfileKind !== "expert"
    && (
      activeSession?.sourceType === "web"
      || activeSession?.sourceType === "user"
      || activeSession === null
    );
  const chatSurface = (
    <>
<section {...getRootProps({ className: "chatLayout" })}><input {...getInputProps()} />{isDragActive && <div className="fileDropOverlay" role="status"><UploadCloud/><strong>释放以添加到本条消息</strong><span>单个文件最大 100 MB，发送时上传</span></div>}<section className="conversation"><div className="messageFeedStage"><div className="messageFeed" data-empty={messages.length === 0} ref={messageFeedRef} onScroll={onMessageFeedScroll}><div className="messageFeedInner main-axis" data-has-messages={messages.length > 0}>{loading ? <p className="muted">正在连接平台服务…</p> : <>{messages.length > 0 ? <ChatMessageList messages={messages.filter((message) => message.status !== "queued")} streaming={streaming} agentNamesByProfileId={agentNamesByProfileId} onOpenScheduledTask={() => changeView("schedules")} resolveImageUrl={resolveChatAttachmentImageUrl} onOpenImage={openChatAttachmentPreview} onOpenHtmlPreview={!isApplication && sessionId ? (message) => openHtmlPreviewFromMessage(sessionId, message) : undefined} htmlPreviewOpenedMessageIds={htmlPreview ? [htmlPreview.messageId] : []}/> : runtime?.runtimeStatus === "ready" ? (
              isDefaultPersonalWelcome
              ? <WelcomeTaskPanel ownerIdentity={welcomeOwnerIdentity} onTaskPreview={previewWelcomeTask} onTaskCommit={commitWelcomeTask} onOpenBoard={openWelcomeBoard} onViewAllBoards={() => changeView("app-center")}/>
              : <div className="welcome welcomeMessageOnly"><h2>{activeSession?.sourceType === "wecom" ? "查看企微会话" : isApplication ? "调试应用 Agent" : activeSession?.agentProfileKind === "expert" ? `与${activeSession.agentDisplayName || "专家"}开始对话` : "您好，我是您的数字分身，输入您的任务吧"}</h2><p>{activeSession?.sourceType === "wecom" ? "此处同步展示企微中的消息与公开推理过程；需要继续对话时请返回企微。" : isApplication ? "调试消息通过 Studio 身份进入应用 Agent 的独立 Runtime，聊天交互与个人 Agent 共用同一控制器。" : activeSession?.agentProfileKind === "expert" ? "当前任务绑定到独立专家 Profile，会话、人格、能力与工作区均和自己隔离。" : "在你的云端 Chat Agent 中完成任务；模型、能力与文件访问均由平台统一治理。"}</p></div>
              ) : null}</>}{turnSpacerHeight > 0 && <div className="turnAnchorSpacer" aria-hidden="true" style={{ height: turnSpacerHeight }}/>}</div></div>{showReturnToLatest && <button type="button" className="returnToLatestButton" aria-label="回到最新消息" onClick={resumeLatest}><ArrowDown size={14} aria-hidden="true"/><span>回到最新</span></button>}</div><ChatQueuedRunList runs={queueSummary.queuedRuns}/>{queueSummary.scheduledQueuedCount > 0 && <p className="scheduledTaskPendingHint" role="status">有定时任务排队中…</p>}{!isApplication && pendingScheduledTask !== null && scheduledRecoverySessionRef.current !== sessionId && <p className="scheduledTaskPendingHint" role="status">正在等待定时任务开始执行…</p>}{!isApplication && activeClarifyRequest && <ClarifyCard request={activeClarifyRequest} busy={clarifyBusyId === activeClarifyRequest.requestId} onAnswer={(answer) => void answerClarify(activeClarifyRequest, answer)} />}{renderAppUpdateClarifyCard()}<Composer input={welcomeTaskPreview ?? input} inputTone={welcomeTaskPreview === null ? "normal" : "preview"} onInputChange={changeComposerInput} onSend={() => void send()} onStop={() => void stop()} streaming={streaming} stopping={stopping} action={composerActionState.action} actionDisabled={composerActionState.disabled} disabled={!activeSession || activeSessionReadOnly || (isApplication && runtime?.runtimeStatus !== "ready")} focusRequest={composerFocusRequest} modelId={modelId} models={models} onModelChange={(nextModelId) => void selectSessionModel(nextModelId)} installedExperts={installedExperts} activeAgentProfileId={activeSession?.runtimeAgentProfileId} activeAgentKind={activeSession?.agentProfileKind} onAgentChange={(runtimeAgentProfileId) => void selectConversationAgent(runtimeAgentProfileId)} attachments={files.filter((file) => pendingAttachmentIds.includes(file.fileId))} uploads={currentUploads} onPasteFiles={addFiles} canSendWithoutText={canSendWithoutText} onAddAttachments={openFilePicker} onRemoveAttachment={removePendingAttachment} onRetryUpload={retryUpload} onRemoveUpload={removeUpload} showModelPicker={!isApplication}/></section>{showFilesPane && <FileRail files={files} pendingFileIds={pendingAttachmentIds} onClose={toggleFiles} onUpload={openFilePicker} onPreview={(file) => void previewFile(file as ChatFileSummary)} onDownload={(file) => void downloadFile(file)} onRename={(file, displayName) => void renameFile(file as ChatFileSummary, displayName)} onDelete={(file) => void deleteFile(file)}/>}</section>
    </>
  );

  return <><main className="appShell" data-theme={theme}>
    <AppSidebar view={view} views={workspaceViews} onViewChange={(next) => changeView(next as View)} sessions={sidebarSessions} activeSessionId={activeContextRoot?.runtimeSessionId ?? activeSession?.runtimeSessionId ?? null} onSessionChange={chooseLogicalSession} onCreateSession={() => void createSession()} onRenameSession={renameSession} onDeleteSession={deleteSession} menuOpen={menuOpen} onMenuToggle={() => { if (!menuOpen) void refreshApplicationAgentAccess(); setMenuOpen((open) => !open); }} profile={profile} onProfileOpen={() => setProfileOpen(true)} onImOpen={isApplication ? undefined : () => { setMenuOpen(false); changeView("im"); }} onMcpOpen={isApplication ? undefined : () => { setMenuOpen(false); changeView("mcps"); }} canManageApplicationAgents={canManageApplicationAgents} isAdmin={isAdmin} onAdminOpen={() => window.open("/admin/", "_blank", "noopener,noreferrer")} applicationAgentMode={isApplication} applicationAgentDebugMode={isApplication} sessionSectionLabel={isApplication ? "会话记录" : undefined} collapsibleSessions={true} sessionActivities={sessionActivities} onApplicationAgentsOpen={() => window.location.assign(isApplication ? "/user/chat" : "/user/application-agents")} theme={theme} onThemeToggle={() => setTheme(theme === "dark" ? "light" : "dark")}/>
    <section className="mainArea"><header className={`appHeader${isApplication ? " applicationDebugHeader" : ""}`} data-view={view}>{isApplication && applicationAgentId !== null && <button type="button" className="applicationDebugBack" onClick={() => window.location.assign(`/user/application-agents/${applicationAgentId}/manage`)}><ArrowLeft size={16}/>返回管理页</button>}<div className="headerIdentity"><div className="chatTitle">{view !== "skills" && view !== "knowledge" && view !== "app-center" && view !== "expert-skills" && view !== "schedules" && view !== "mcps" && view !== "im" && <strong>{view === "chat" ? displayTaskTitle(activeSession?.title) : workspaceViews.find((item) => item.id === view)?.label}</strong>}{isApplication && <span className="chatAgentBadge">应用 Agent</span>}{view === "chat" && (activeSession?.sourceType === "wecom" || activeSession?.sourceType === "openapi") && <span className="chatAgentBadge"><MessageCircleMore size={12}/>{activeSession.originLabel || (activeSession.sourceType === "openapi" ? "API" : "企微")} · 固定会话</span>}{!isApplication && view === "chat" && activeSession?.agentProfileKind === "expert" && <span className="chatAgentBadge"><Sparkles size={12}/>{activeSession.agentDisplayName || "专家"}</span>}{showRuntimeAllocatingHint && <span className="runtimeAllocatingHint" role="status"><span className="runtimeAllocatingHintIcon" aria-hidden="true">⏳</span><span>{runtimeAllocatingLabel}</span></span>}</div></div>{view === "chat" && !isApplication && !htmlPreview && sessionId && localHtmlPreviewTargetsRef.current.has(sessionId) ? <button type="button" className="filesRailHandle" onClick={() => { const targetPath = sessionId ? localHtmlPreviewTargetsRef.current.get(sessionId) : undefined; if (sessionId && targetPath) void openLocalHtmlFilePreview(sessionId, targetPath, null, false); }} title="重新打开 HTML 预览" aria-label="重新打开 HTML 预览"><LayoutGrid size={16} aria-hidden="true"/>打开预览</button> : null}{view === "chat" && !showFilesPane && <button type="button" className="filesRailHandle" onClick={toggleFiles} aria-label="打开当前任务文件" title="打开当前任务文件"><FolderOpen size={16} aria-hidden="true"/>{files.length > 0 ? <span className="filesRailCount" aria-hidden="true">{files.length}</span> : null}</button>}</header>
      {error && <div className="errorNotice" role="alert">{error}</div>}
      {!imageInputSupported && <div className="errorNotice" role="status">当前模型不支持图片识别，请切换模型后重试。</div>}
      {!isApplication && view === "chat" && historySyncStatus === "degraded" && <p className="runtimeSkillSyncWarning" role="alert">最新记录同步失败，可重试。{runtime && sessionId && <button type="button" onClick={() => void loadSession(runtime.runtimeWorkspaceId, sessionId)}>重试</button>}</p>}
      {view === "chat" && activeProgress && activeProgress.label !== "任务已暂停，等待确认" && <p className="runProgressNotice" role="status" aria-live="polite" data-message-id={progressMessageId(activeProgress.runtimeRunId)}><LoaderCircle size={14} aria-hidden="true"/>{activeProgress.label}</p>}
      {view === "chat" && activeSessionReadOnly && <p className="runtimeSkillSyncWarning" role="status">{activeSession?.sourceType === "wecom" ? "企微接入会话仅用于查看历史，请在企微中继续对话。" : activeSession?.sourceType === "openapi" ? "API 接入会话仅用于查看历史，请在调用方系统中继续对话。" : "该专家已卸载或删除，此旧会话仅可查看历史；重新安装会创建新的专家入口。"}</p>}
      {view === "chat" ? htmlPreview ? <section className="chatViewWithPreview">{chatSurface}{renderHtmlPreviewPanel()}</section> : chatSurface : view === "im" && !isApplication ? renderImPage() : isApplication && applicationAgentId !== null ? <ApplicationAgentConfigurationPanel applicationAgentId={applicationAgentId} section={view as "skills" | "mcps" | "knowledge" | "channels" | "settings"} onConfigurationApplied={() => setError(null)}/> : view === "app-center" && !isApplication ? renderAppCenterPage() : view === "schedules" ? renderSchedulesPage() : view === "mcps" ? renderMcpPage() : view === "knowledge" ? renderKnowledgePage() : view === "expert-skills" ? renderExpertSkillsPage() : null}    </section>



    {filePreview && <FilePreviewDialog preview={filePreview} onClose={closeFilePreview} onDownload={(file) => void downloadFile(file)}/>}
    {profileOpen && <Suspense fallback={null}><ProfileDialogView profile={profile} principal={principal} onClose={() => setProfileOpen(false)} onSave={(nextProfile) => { saveUserProfile(principal, nextProfile); setProfile(nextProfile); setProfileOpen(false); }}/></Suspense>}
  </main></>;
}
