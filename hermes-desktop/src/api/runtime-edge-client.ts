import type {
  RuntimeChatRequest,
  RuntimeChatRunSummary,
  RuntimeEdgeClientMessage,
  RuntimeEdgeFollowupDisposition,
  RuntimeEdgeRedirectDisposition,
  RuntimeEdgeRunAccepted,
  RuntimeEdgeRunQueued,
  RuntimeEdgeRunCancelAck,
  RuntimeEdgeRunEvent,
  RuntimeEdgeRunSnapshot,
  RuntimeEdgeLocalCapabilityRequest,
  RuntimeEdgeLocalCapabilityResult,
  RuntimeEdgeSessionHistory,
  RuntimeEdgeSessionQueue,
  RuntimeEdgeSessionQueueUpdated,
  RuntimeEdgeServerMessage,
  RuntimeRunActionRequest
} from "@hermes-cloud/schemas";
import { camelize } from "./types";

export interface RuntimeEdgeChatEvent {
  event: string;
  data: Record<string, unknown>;
}

export interface RuntimeEdgeGrantTarget {
  schemaVersion: 1;
  runtimeId: string;
  runtimeSessionId: string;
  modelEntryId: number;
  profileId: string;
  configRevision: string;
  sessionGrant: string;
  sessionGrantExpiresAt: string;
}

export interface RuntimeEdgeBootstrap extends RuntimeEdgeGrantTarget {
  runtimeHandle: string;
  websocketPath: string;
  websocketProtocol: "hermes-runtime-v1";
  runtimeAccess: string;
  runtimeAccessExpiresAt: string;
}

interface TargetRequest {
  runtimeWorkspaceId: number;
  runtimeSessionId: string;
  modelEntryId?: number | null;
  attachmentFileIds?: number[];
}

interface PreparedTarget
  extends Omit<TargetRequest, "modelEntryId">, RuntimeEdgeGrantTarget {}

interface PrepareWaiter {
  resolve: () => void;
  reject: (reason: Error) => void;
  timer: number;
}

interface HistoryWaiter {
  runtimeSessionId: string;
  resolve: (messages: Record<string, unknown>[]) => void;
  reject: (reason: Error) => void;
  timer: number;
}

export interface RuntimeEdgeQueueSummary {
  queuedCount: number;
  scheduledQueuedCount: number;
  queuedRuns: Array<{
    runId: string;
    clientMessageId: string;
    enqueueSeq: number;
    prompt: string;
    attachmentFileIds: string[];
  }>;
  activeRun: {
    runId: string;
    state: string;
    cancellationRequestedAt: string | null;
  } | null;
}

export interface RuntimeEdgeQueueUpdatedDetail extends RuntimeEdgeQueueSummary {
  runtimeSessionId: string;
}

export const RUNTIME_EDGE_QUEUE_UPDATED_EVENT =
  "hermes:runtime-edge-queue-updated";

export const LOCAL_CAPABILITY_APPROVAL_EVENT =
  "hermes:local-capability-approval";

export interface LocalCapabilityApprovalDetail {
  requestId: string;
  runId: string;
  sessionId: string;
  capability: RuntimeEdgeLocalCapabilityRequest["capability"];
  args: Record<string, unknown>;
  question: string;
  target?: string;
}

interface QueueWaiter {
  resolve: (summary: RuntimeEdgeQueueSummary) => void;
  reject: (reason: Error) => void;
  timer: number;
}

interface RunWaiter {
  runtimeWorkspaceId: number;
  runtimeSessionId: string;
  modelEntryId: number;
  callback: (event: RuntimeEdgeChatEvent) => void;
  resolve: () => void;
  reject: (reason: Error) => void;
  abortCleanup: () => void;
  lastSeq: number;
}

interface CancelWaiter {
  runtimeWorkspaceId: number;
  runtimeSessionId: string;
  modelEntryId?: number | null;
  resolve: (result: RuntimeChatRunSummary) => void;
  reject: (reason: Error) => void;
  timer: number;
}

interface PendingSubmission {
  waiter: RunWaiter;
  clientMessageId: string;
  payload: RuntimeChatRequest;
}

export type RuntimeEdgeRedirectDispositionKind =
  | "received"
  | "applying"
  | "applied"
  | "continued"
  | "failed";

export interface RuntimeEdgeRedirectResult {
  kind: RuntimeEdgeRedirectDispositionKind;
  clientMessageId: string;
  parentRunId?: string;
  continuationRunId?: string;
}

interface RedirectWaiter {
  runtimeWorkspaceId: number;
  runtimeSessionId: string;
  modelEntryId: number;
  clientMessageId: string;
  callback: (event: RuntimeEdgeChatEvent) => void;
  resolve: (result: RuntimeEdgeRedirectResult) => void;
  reject: (reason: Error) => void;
  abortCleanup: () => void;
  seenDispositions: Set<string>;
  continuationRunId?: string;
}

type BootstrapRuntimeEdge = (
  target: TargetRequest
) => Promise<RuntimeEdgeBootstrap>;

type RefreshRuntimeEdgeGrant = (
  target: TargetRequest
) => Promise<RuntimeEdgeGrantTarget>;

const SESSION_GRANT_REFRESH_SKEW_MS = 5 * 60 * 1000;
const PREPARE_TIMEOUT_MS = 120_000;
const CANCEL_CONFIRM_TIMEOUT_MS = 10_000;
const RUNTIME_RECONNECT_DELAYS_MS = [1_000, 2_000, 4_000, 8_000];
const RUNTIME_RECONNECT_MAX_DELAY_MS = 30_000;
const TERMINAL_RUN_STATES = new Set([
  "COMPLETED",
  "FAILED",
  "CANCELED",
  "RUN_INTERRUPTED_BY_RUNTIME_RESTART",
  "RUN_INTERRUPTED_BY_RUNTIME_TERMINATION",
  "FAILED_SUBMISSION_UNCERTAIN"
]);
const GRANT_REFRESH_REQUIRED_CODES = new Set([
  "SESSION_GRANT_INVALID",
  "CONFIG_REVISION_STALE",
  "RUNTIME_ADMISSION_GENERATION_MISMATCH"
]);

const requiresGrantRefresh = (reason: unknown): boolean => (
  reason instanceof RuntimeEdgeTransportError
  && GRANT_REFRESH_REQUIRED_CODES.has(reason.code)
);

const canRetrySubmissionWithRefreshedGrant = (
  reason: unknown,
  signal?: AbortSignal
): boolean => (
  !signal?.aborted
  && reason instanceof RuntimeEdgeTransportError
  && requiresGrantRefresh(reason)
  && !reason.correlation.runId
);

export class RuntimeEdgeTransportError extends Error {
  constructor(
    message: string,
    readonly code = "RUNTIME_EDGE_TRANSPORT_ERROR",
    readonly retryable = true,
    readonly correlation: {
      requestId?: string | null;
      clientMessageId?: string | null;
      runId?: string | null;
      phase?: string | null;
      auditEventId?: string | null;
    } = {}
  ) {
    super(message);
    this.name = "RuntimeEdgeTransportError";
  }
}

const SESSION_PREPARE_PHASE_LABELS: Record<string, string> = {
  live_execution_release: "释放旧会话",
  model_route: "模型线路准备",
  session_import: "会话导入/修复",
  dashboard_prewarm: "Dashboard 预热"
};

export function formatRuntimeSessionPrepareError(
  reason: unknown,
  fallback = "会话预热失败"
): string {
  if (
    !(reason instanceof RuntimeEdgeTransportError)
    || reason.code !== "RUNTIME_SESSION_PREPARE_FAILED"
  ) {
    return reason instanceof Error ? reason.message : fallback;
  }
  const phase = String(reason.correlation.phase ?? "").trim();
  const auditEventId = String(reason.correlation.auditEventId ?? "").trim();
  const phaseLabel = SESSION_PREPARE_PHASE_LABELS[phase] ?? "未知阶段";
  const diagnostic = auditEventId || "未提供";
  return (
    `运行环境准备失败（阶段：${phaseLabel}；诊断编号：${diagnostic}），` +
    "请重试或联系管理员。"
  );
}

export class RuntimeEdgeClient {
  private socket: WebSocket | null = null;
  private socketRuntimeId: string | null = null;
  private prepared: PreparedTarget | null = null;
  private lastTarget: TargetRequest | null = null;
  private prepareWaiters = new Map<string, PrepareWaiter>();
  private desktopCapabilityRequests = new Set<string>();
  private historyWaiters = new Map<string, HistoryWaiter>();
  private queueWaiters = new Map<string, QueueWaiter>();
  private pendingSubmissions = new Map<string, PendingSubmission>();
  private redirectWaiters = new Map<string, RedirectWaiter>();
  private runWaiters = new Map<string, RunWaiter>();
  private cancelWaiters = new Map<string, CancelWaiter>();
  private localCapabilityResults = new Map<string, RuntimeEdgeLocalCapabilityResult>();
  private pendingLocalCapabilities = new Map<string, RuntimeEdgeLocalCapabilityRequest>();
  private preparationTail: Promise<unknown> = Promise.resolve();
  private reconnectTimer: number | null = null;
  private reconnectAttempts = 0;
  private manualClose = false;

  constructor(
    private readonly bootstrapRuntimeEdge: BootstrapRuntimeEdge,
    private readonly refreshRuntimeEdgeGrant: RefreshRuntimeEdgeGrant
  ) {
    if (typeof window !== "undefined") {
      window.coworkDesktop?.onCapabilityResult?.((result) => {
        console.info(`[local-capability] result ${result.requestId} ok=${result.ok}`);
        this.deliverLocalCapabilityResult(result.requestId, result);
      });
    }
  }

  prepare(
    runtimeWorkspaceId: number,
    runtimeSessionId: string,
    modelEntryId?: number | null,
    attachmentFileIds?: number[],
    forceGrantRefresh = false
  ): Promise<RuntimeEdgeGrantTarget> {
    const target = {
      runtimeWorkspaceId,
      runtimeSessionId,
      modelEntryId,
      attachmentFileIds
    };
    this.lastTarget = target;
    const task = this.preparationTail.then(
      () => this.prepareNow(target, forceGrantRefresh)
    );
    this.preparationTail = task.catch(() => undefined);
    return task;
  }

  async streamRun(
    payload: RuntimeChatRequest,
    callback: (event: RuntimeEdgeChatEvent) => void,
    signal?: AbortSignal
  ): Promise<void> {
    const runtimeSessionId = payload.runtimeSessionId;
    if (!runtimeSessionId) {
      throw new RuntimeEdgeTransportError(
        "缺少运行会话，无法发送消息",
        "RUNTIME_SESSION_REQUIRED",
        false
      );
    }
    const clientMessageId = payload.runtimeRunId
      ?? `web-${Date.now()}-${randomIdentifier()}`;
    let forceGrantRefresh = false;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      let target: RuntimeEdgeGrantTarget;
      try {
        // A completed session prewarm already obtained a grant for this exact
        // target. Reuse it on the hot path; stale config/admission is fail-closed
        // by Supervisor and falls back to one explicit refresh below.
        target = await this.prepare(
          payload.runtimeWorkspaceId,
          runtimeSessionId,
          payload.modelEntryId,
          payload.attachmentFileIds,
          forceGrantRefresh
        );
      } catch (reason) {
        if (
          attempt !== 0
          || !requiresGrantRefresh(reason)
          || signal?.aborted
        ) {
          throw reason;
        }
        forceGrantRefresh = true;
        continue;
      }
      try {
        await this.submitRunWithTarget(
          payload,
          target,
          clientMessageId,
          callback,
          signal
        );
        return;
      } catch (reason) {
        if (!canRetrySubmissionWithRefreshedGrant(reason, signal)) {
          throw reason;
        }
        forceGrantRefresh = true;
      }
    }
    throw new RuntimeEdgeTransportError(
      "Runtime 授权刷新后仍无法提交任务",
      "RUNTIME_SUBMISSION_GRANT_REFRESH_FAILED"
    );
  }

  async resolveLocalCapability(requestId: string, approved: boolean): Promise<boolean> {
    const message = this.pendingLocalCapabilities.get(requestId);
    console.info(`[local-capability] resolve ${requestId} approved=${approved} pending=${message !== undefined}`);
    if (!message || !window.coworkDesktop?.resolveCapability) return false;
    const local = await window.coworkDesktop.resolveCapability({ requestId, approved });
    if (local.acknowledged === true) {
      console.info(`[local-capability] resolve ${requestId} acknowledged, execution backgrounded`);
      window.dispatchEvent(new CustomEvent("hermes:local-capability-approval-resolved", { detail: { requestId } }));
      return true;
    }
    console.info(`[local-capability] resolve ${requestId} ipc-settled ok=${local.ok}`);
    this.deliverLocalCapabilityResult(requestId, local);
    return true;
  }

  private deliverLocalCapabilityResult(
    requestId: string,
    local: { ok: boolean; value?: unknown; error?: string; canceled?: boolean }
  ): void {
    const message = this.pendingLocalCapabilities.get(requestId);
    if (!message) return;
    const result: RuntimeEdgeLocalCapabilityResult = {
      schema_version: 1,
      type: "local.capability.result",
      request_id: requestId,
      run_id: message.run_id,
      capability: message.capability,
      ok: local.ok,
      ...(local.value === undefined ? {} : { value: local.value }),
      ...(local.error ? { error: local.error.slice(0, 512) } : {}),
      ...(local.canceled ? { canceled: true } : {})
    };
    this.pendingLocalCapabilities.delete(requestId);
    this.localCapabilityResults.set(requestId, result);
    const waiter = this.runWaiters.get(message.run_id);
    waiter?.callback({
      event: "local.capability.result",
      data: {
        request_id: result.request_id,
        run_id: result.run_id,
        capability: result.capability,
        ok: result.ok,
        value: result.value,
        error: result.error ?? null,
        canceled: result.canceled ?? false
      }
    });
    const socket = this.socket;
    if (socket?.readyState === WebSocket.OPEN) this.send(socket, result);
    window.dispatchEvent(new CustomEvent("hermes:local-capability-approval-resolved", { detail: { requestId } }));
  }

  /**
   * Submit a human text correction without taking ownership of the active
   * parent Run's stream waiter. Runtime first persists this replacement turn,
   * interrupts the active parent, and streams the queued replacement Run.
   */
  async submitCorrection(
    payload: RuntimeChatRequest,
    callback: (event: RuntimeEdgeChatEvent) => void,
    signal?: AbortSignal
  ): Promise<RuntimeEdgeRedirectResult> {
    const runtimeSessionId = payload.runtimeSessionId;
    if (!runtimeSessionId) {
      throw new RuntimeEdgeTransportError(
        "缺少运行会话，无法发送修正",
        "RUNTIME_SESSION_REQUIRED",
        false
      );
    }
    const clientMessageId = payload.runtimeRunId
      ?? `web-${Date.now()}-${randomIdentifier()}`;
    const existing = this.redirectWaiters.get(clientMessageId);
    if (existing) {
      throw new RuntimeEdgeTransportError(
        "该修正仍在等待 Runtime 确认",
        "RUNTIME_REDIRECT_ALREADY_PENDING",
        false,
        { clientMessageId }
      );
    }
    if (!payload.parentRuntimeRunId) {
      throw new RuntimeEdgeTransportError(
        "缺少正在执行的任务，无法发送追问",
        "RUNTIME_FOLLOWUP_PARENT_REQUIRED",
        false
      );
    }
    let forceGrantRefresh = false;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      let target: RuntimeEdgeGrantTarget;
      try {
        // Corrections use the same hot Session/Grant as ordinary submissions;
        // refresh only when Supervisor rejects that grant as stale or invalid.
        target = await this.prepare(
          payload.runtimeWorkspaceId,
          runtimeSessionId,
          payload.modelEntryId,
          payload.attachmentFileIds,
          forceGrantRefresh
        );
      } catch (reason) {
        if (
          attempt !== 0
          || !requiresGrantRefresh(reason)
          || signal?.aborted
        ) {
          throw reason;
        }
        forceGrantRefresh = true;
        continue;
      }
      try {
        return await this.submitCorrectionWithTarget(
          payload,
          target,
          clientMessageId,
          callback,
          signal
        );
      } catch (reason) {
        if (!canRetrySubmissionWithRefreshedGrant(reason, signal)) {
          throw reason;
        }
        forceGrantRefresh = true;
      }
    }
    throw new RuntimeEdgeTransportError(
      "Runtime 授权刷新后仍无法提交追问",
      "RUNTIME_SUBMISSION_GRANT_REFRESH_FAILED"
    );
  }

  private submitRunWithTarget(
    payload: RuntimeChatRequest,
    target: RuntimeEdgeGrantTarget,
    clientMessageId: string,
    callback: (event: RuntimeEdgeChatEvent) => void,
    signal?: AbortSignal
  ): Promise<void> {
    const waiter = this.createRunWaiter({
      runtimeWorkspaceId: payload.runtimeWorkspaceId,
      runtimeSessionId: payload.runtimeSessionId ?? target.runtimeSessionId,
      modelEntryId: target.modelEntryId,
      callback,
      signal
    });
    if (signal?.aborted) {
      waiter.abortCleanup();
      return Promise.reject(abortError());
    }
    const completion = new Promise<void>((resolve, reject) => {
      waiter.resolve = resolve;
      waiter.reject = reject;
    });
    this.pendingSubmissions.set(clientMessageId, {
      waiter,
      clientMessageId,
      payload
    });
    try {
      this.send(this.requireOpenSocket(), {
        schema_version: 1,
        type: "run.submit",
        client_message_id: clientMessageId,
        session_grant: target.sessionGrant,
        prompt: payload.message,
        attachment_file_ids: (payload.attachmentFileIds ?? []).map(String)
      });
    } catch (reason) {
      this.pendingSubmissions.delete(clientMessageId);
      waiter.abortCleanup();
      waiter.reject(asError(reason));
    }
    return completion;
  }

  private submitCorrectionWithTarget(
    payload: RuntimeChatRequest,
    target: RuntimeEdgeGrantTarget,
    clientMessageId: string,
    callback: (event: RuntimeEdgeChatEvent) => void,
    signal?: AbortSignal
  ): Promise<RuntimeEdgeRedirectResult> {
    const completion = new Promise<RuntimeEdgeRedirectResult>((resolve, reject) => {
      const waiter: RedirectWaiter = {
        runtimeWorkspaceId: payload.runtimeWorkspaceId,
        runtimeSessionId: payload.runtimeSessionId ?? target.runtimeSessionId,
        modelEntryId: target.modelEntryId,
        clientMessageId,
        callback,
        resolve,
        reject,
        abortCleanup: () => undefined,
        seenDispositions: new Set()
      };
      if (signal) {
        const onAbort = () => {
          if (this.redirectWaiters.get(clientMessageId) === waiter) {
            this.redirectWaiters.delete(clientMessageId);
          }
          waiter.reject(abortError());
        };
        signal.addEventListener("abort", onAbort, { once: true });
        waiter.abortCleanup = () => signal.removeEventListener("abort", onAbort);
      }
      this.redirectWaiters.set(clientMessageId, waiter);
    });
    if (signal?.aborted) {
      const waiter = this.redirectWaiters.get(clientMessageId);
      this.redirectWaiters.delete(clientMessageId);
      waiter?.abortCleanup();
      return Promise.reject(abortError());
    }
    try {
      this.send(this.requireOpenSocket(), {
        schema_version: 1,
        type: "followup.submit",
        client_message_id: clientMessageId,
        parent_run_id: payload.parentRuntimeRunId as string,
        session_grant: target.sessionGrant,
        prompt: payload.message,
        attachment_file_ids: (payload.attachmentFileIds ?? []).map(String)
      });
    } catch (reason) {
      const waiter = this.redirectWaiters.get(clientMessageId);
      this.redirectWaiters.delete(clientMessageId);
      waiter?.abortCleanup();
      waiter?.reject(asError(reason));
    }
    return completion;
  }

  async history(
    runtimeWorkspaceId: number,
    runtimeSessionId: string,
    modelEntryId?: number | null
  ): Promise<Record<string, unknown>[]> {
    const grant = await this.prepare(
      runtimeWorkspaceId,
      runtimeSessionId,
      modelEntryId
    );
    return this.requestHistory(grant);
  }

  async historyPrepared(
    runtimeWorkspaceId: number,
    runtimeSessionId: string
  ): Promise<Record<string, unknown>[]> {
    return this.requestHistory(this.requirePreparedTarget(
      runtimeWorkspaceId,
      runtimeSessionId
    ));
  }

  private requestHistory(
    grant: RuntimeEdgeGrantTarget
  ): Promise<Record<string, unknown>[]> {
    const requestId = `history-${Date.now()}-${randomIdentifier()}`;
    return new Promise<Record<string, unknown>[]>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.historyWaiters.delete(requestId);
        reject(new RuntimeEdgeTransportError("Runtime 历史加载超时"));
      }, PREPARE_TIMEOUT_MS);
      this.historyWaiters.set(requestId, {
        runtimeSessionId: grant.runtimeSessionId,
        resolve,
        reject,
        timer
      });
      try {
        this.send(this.requireOpenSocket(), {
          schema_version: 1,
          type: "session.history",
          request_id: requestId,
          session_grant: grant.sessionGrant
        });
      } catch (reason) {
        window.clearTimeout(timer);
        this.historyWaiters.delete(requestId);
        reject(asError(reason));
      }
    });
  }

  async queueSummary(
    runtimeWorkspaceId: number,
    runtimeSessionId: string,
    modelEntryId?: number | null
  ): Promise<RuntimeEdgeQueueSummary> {
    const grant = await this.prepare(
      runtimeWorkspaceId,
      runtimeSessionId,
      modelEntryId
    );
    return this.requestQueueSummary(grant);
  }

  async queueSummaryPrepared(
    runtimeWorkspaceId: number,
    runtimeSessionId: string
  ): Promise<RuntimeEdgeQueueSummary> {
    return this.requestQueueSummary(this.requirePreparedTarget(
      runtimeWorkspaceId,
      runtimeSessionId
    ));
  }

  private requestQueueSummary(
    grant: RuntimeEdgeGrantTarget
  ): Promise<RuntimeEdgeQueueSummary> {
    const requestId = `queue-${Date.now()}-${randomIdentifier()}`;
    return new Promise<RuntimeEdgeQueueSummary>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.queueWaiters.delete(requestId);
        reject(new RuntimeEdgeTransportError("Runtime 排队状态加载超时"));
      }, PREPARE_TIMEOUT_MS);
      this.queueWaiters.set(requestId, { resolve, reject, timer });
      try {
        this.send(this.requireOpenSocket(), {
          schema_version: 1,
          type: "session.queue",
          request_id: requestId,
          session_grant: grant.sessionGrant
        });
      } catch (reason) {
        window.clearTimeout(timer);
        this.queueWaiters.delete(requestId);
        reject(asError(reason));
      }
    });
  }

  async resumeRun(
    payload: RuntimeRunActionRequest,
    callback: (event: RuntimeEdgeChatEvent) => void,
    signal?: AbortSignal
  ): Promise<void> {
    const target = this.targetForAction(payload);
    const prepared = await this.prepare(
      target.runtimeWorkspaceId,
      target.runtimeSessionId,
      target.modelEntryId
    );
    const waiter = this.createRunWaiter({
      runtimeWorkspaceId: target.runtimeWorkspaceId,
      runtimeSessionId: target.runtimeSessionId,
      modelEntryId: prepared.modelEntryId,
      callback,
      signal,
      lastSeq: payload.afterSeq ?? 0
    });
    if (signal?.aborted) {
      waiter.abortCleanup();
      throw abortError();
    }
    const completion = new Promise<void>((resolve, reject) => {
      waiter.resolve = resolve;
      waiter.reject = reject;
    });
    this.runWaiters.set(payload.runtimeRunId, waiter);
    try {
      this.send(this.requireOpenSocket(), {
        schema_version: 1,
        type: "run.resume",
        run_id: payload.runtimeRunId,
        after_seq: payload.afterSeq ?? 0
      });
    } catch (reason) {
      this.runWaiters.delete(payload.runtimeRunId);
      waiter.abortCleanup();
      waiter.reject(asError(reason));
    }
    return completion;
  }

  async cancelRun(
    payload: RuntimeRunActionRequest
  ): Promise<RuntimeChatRunSummary> {
    const target = this.targetForAction(payload);
    let modelEntryId = target.modelEntryId;
    if (!this.isSocketOpen()) {
      const bootstrap = await this.bootstrapRuntimeEdge(target);
      await this.connect(bootstrap);
      modelEntryId = bootstrap.modelEntryId;
      this.lastTarget = {
        runtimeWorkspaceId: target.runtimeWorkspaceId,
        runtimeSessionId: target.runtimeSessionId,
        modelEntryId
      };
    } else if (modelEntryId == null) {
      modelEntryId = this.prepared?.modelEntryId ?? this.lastTarget?.modelEntryId;
    }
    if (this.cancelWaiters.has(payload.runtimeRunId)) {
      throw new RuntimeEdgeTransportError(
        "停止请求正在确认中，请稍候",
        "RUNTIME_CANCEL_ALREADY_PENDING",
        false,
        { runId: payload.runtimeRunId }
      );
    }
    return new Promise<RuntimeChatRunSummary>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.cancelWaiters.delete(payload.runtimeRunId);
        reject(new RuntimeEdgeTransportError(
          "停止请求未送达，任务仍在执行，请重试",
          "RUNTIME_CANCEL_UNCONFIRMED",
          true,
          { runId: payload.runtimeRunId }
        ));
      }, CANCEL_CONFIRM_TIMEOUT_MS);
      this.cancelWaiters.set(payload.runtimeRunId, {
        runtimeWorkspaceId: target.runtimeWorkspaceId,
        runtimeSessionId: target.runtimeSessionId,
        modelEntryId,
        resolve,
        reject,
        timer
      });
      try {
        this.send(this.requireOpenSocket(), {
          schema_version: 1,
          type: "run.cancel",
          run_id: payload.runtimeRunId
        });
      } catch (reason) {
        window.clearTimeout(timer);
        this.cancelWaiters.delete(payload.runtimeRunId);
        reject(asError(reason));
      }
    });
  }

  close(): void {
    const socket = this.socket;
    this.manualClose = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.socket = null;
    this.socketRuntimeId = null;
    this.prepared = null;
    if (
      socket
      && socket.readyState !== WebSocket.CLOSED
      && socket.readyState !== WebSocket.CLOSING
    ) {
      socket.close(1000, "client shutdown");
    }
    this.rejectAll(new RuntimeEdgeTransportError("Runtime 连接已关闭"));
  }

  private async prepareNow(
    target: TargetRequest,
    forceGrantRefresh: boolean
  ): Promise<RuntimeEdgeGrantTarget> {
    let grant: RuntimeEdgeGrantTarget;
    if (!this.isSocketOpen()) {
      const bootstrap = await this.bootstrapRuntimeEdge(target);
      await this.connect(bootstrap);
      grant = bootstrap;
    } else if (
      !forceGrantRefresh
      && this.prepared
      && this.prepared.runtimeWorkspaceId === target.runtimeWorkspaceId
      && this.prepared.runtimeSessionId === target.runtimeSessionId
      && (
        target.modelEntryId == null
        || this.prepared.modelEntryId === target.modelEntryId
      )
      && sameIdentifiers(
        this.prepared.attachmentFileIds,
        target.attachmentFileIds
      )
      && Date.parse(this.prepared.sessionGrantExpiresAt) - Date.now()
        > SESSION_GRANT_REFRESH_SKEW_MS
    ) {
      grant = this.prepared;
    } else {
      grant = await this.refreshRuntimeEdgeGrant(target);
      if (
        this.socketRuntimeId !== null
        && this.socketRuntimeId !== grant.runtimeId
      ) {
        const bootstrap = await this.bootstrapRuntimeEdge(target);
        await this.connect(bootstrap);
        grant = bootstrap;
      }
    }

    await this.sendPrepare(grant);
    const prepared: PreparedTarget = {
      ...target,
      ...grant,
      modelEntryId: grant.modelEntryId
    };
    this.prepared = prepared;
    this.lastTarget = {
      runtimeWorkspaceId: prepared.runtimeWorkspaceId,
      runtimeSessionId: prepared.runtimeSessionId,
      modelEntryId: prepared.modelEntryId
    };
    return grant;
  }

  private requirePreparedTarget(
    runtimeWorkspaceId: number,
    runtimeSessionId: string
  ): PreparedTarget {
    const prepared = this.prepared;
    if (
      !prepared
      || !this.isSocketOpen()
      || prepared.runtimeWorkspaceId !== runtimeWorkspaceId
      || prepared.runtimeSessionId !== runtimeSessionId
      || Date.parse(prepared.sessionGrantExpiresAt) <= Date.now()
    ) {
      throw new RuntimeEdgeTransportError(
        "Runtime 会话尚未完成预热",
        "RUNTIME_SESSION_NOT_PREPARED",
        true
      );
    }
    return prepared;
  }

  private connect(bootstrap: RuntimeEdgeBootstrap): Promise<void> {
    const previous = this.socket;
    if (
      previous
      && previous.readyState !== WebSocket.CLOSED
      && previous.readyState !== WebSocket.CLOSING
    ) {
      previous.close(1000, "runtime target changed");
    }
    const websocketUrl = new URL(bootstrap.websocketPath, window.location.href);
    websocketUrl.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(websocketUrl, [
      bootstrap.websocketProtocol,
      `runtime-access.${bootstrap.runtimeAccess}`
    ]);
    this.socket = socket;
    this.socketRuntimeId = bootstrap.runtimeId;
    this.prepared = null;
    this.manualClose = false;

    return new Promise<void>((resolve, reject) => {
      let settled = false;
      socket.addEventListener("open", () => {
        if (this.socket !== socket) return;
        settled = true;
        resolve();
      }, { once: true });
      socket.addEventListener("message", (event) => {
        if (this.socket === socket) this.handleMessage(event);
      });
      socket.addEventListener("error", () => {
        if (!settled) {
          settled = true;
          reject(new RuntimeEdgeTransportError("Runtime 连接建立失败"));
        }
      });
      socket.addEventListener("close", () => {
        if (this.socket !== socket) return;
        this.socket = null;
        this.socketRuntimeId = null;
        this.prepared = null;
        const reason = new RuntimeEdgeTransportError(
          settled ? "Runtime 实时连接已中断" : "Runtime 连接建立失败"
        );
        if (!settled) {
          settled = true;
          reject(reason);
        }
        this.rejectNonRunWaiters(reason);
        this.notifyTransportWaiters("chat.transport.reconnecting");
        this.scheduleReconnect();
      });
    });
  }

  private scheduleReconnect(): void {
    if (this.manualClose || !this.hasDisconnectedRunWaiters()) {
      this.reconnectAttempts = 0;
      return;
    }
    if (this.reconnectTimer !== null) return;
    const delay = RUNTIME_RECONNECT_DELAYS_MS[
      Math.min(this.reconnectAttempts, RUNTIME_RECONNECT_DELAYS_MS.length - 1)
    ] ?? RUNTIME_RECONNECT_MAX_DELAY_MS;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      void this.recoverDisconnectedWaiters();
    }, Math.min(delay, RUNTIME_RECONNECT_MAX_DELAY_MS));
  }

  private hasDisconnectedRunWaiters(): boolean {
    return this.pendingSubmissions.size > 0 || this.runWaiters.size > 0;
  }

  private async recoverDisconnectedWaiters(): Promise<void> {
    if (!this.hasDisconnectedRunWaiters()) {
      this.reconnectAttempts = 0;
      return;
    }

    try {
      for (const [runId, expectedWaiter] of [...this.runWaiters]) {
        const waiter = this.runWaiters.get(runId);
        if (waiter !== expectedWaiter) continue;
        await this.prepare(
          waiter.runtimeWorkspaceId,
          waiter.runtimeSessionId,
          waiter.modelEntryId
        );
        if (this.runWaiters.get(runId) !== waiter) continue;
        this.send(this.requireOpenSocket(), {
          schema_version: 1,
          type: "run.resume",
          run_id: runId,
          after_seq: waiter.lastSeq
        });
      }

      for (const [clientMessageId, expectedPending] of [...this.pendingSubmissions]) {
        const pending = this.pendingSubmissions.get(clientMessageId);
        if (pending !== expectedPending) continue;
        const waiter = pending.waiter;
        const target = await this.prepare(
          waiter.runtimeWorkspaceId,
          waiter.runtimeSessionId,
          waiter.modelEntryId,
          pending.payload.attachmentFileIds
        );
        if (this.pendingSubmissions.get(clientMessageId) !== pending) continue;
        const summary = await this.requestQueueSummary(target);
        if (this.pendingSubmissions.get(clientMessageId) !== pending) continue;
        const queuedRun = summary.queuedRuns.find((run) => (
          run.clientMessageId === clientMessageId
        ));
        if (queuedRun) {
          this.pendingSubmissions.delete(clientMessageId);
          this.runWaiters.set(queuedRun.runId, waiter);
          waiter.callback({
            event: "chat.run.queued",
            data: {
              runtimeWorkspaceId: waiter.runtimeWorkspaceId,
              runtimeSessionId: waiter.runtimeSessionId,
              runtimeRunId: queuedRun.runId,
              modelEntryId: waiter.modelEntryId,
              status: "queued",
              enqueueSeq: queuedRun.enqueueSeq,
              clientMessageId
            }
          });
          this.send(this.requireOpenSocket(), {
            schema_version: 1,
            type: "run.resume",
            run_id: queuedRun.runId,
            after_seq: waiter.lastSeq
          });
        } else {
          this.send(this.requireOpenSocket(), {
            schema_version: 1,
            type: "run.submit",
            client_message_id: clientMessageId,
            session_grant: target.sessionGrant,
            prompt: pending.payload.message,
            attachment_file_ids: (pending.payload.attachmentFileIds ?? []).map(String)
          });
        }
      }

      this.reconnectAttempts = 0;
      this.notifyTransportWaiters("chat.transport.resumed");
    } catch {
      this.reconnectAttempts += 1;
      this.scheduleReconnect();
    }
  }

  private notifyTransportWaiters(event: "chat.transport.reconnecting" | "chat.transport.resumed"): void {
    for (const [runId, waiter] of this.runWaiters) {
      waiter.callback({
        event,
        data: {
          runtimeRunId: runId,
          attempt: this.reconnectAttempts + (event === "chat.transport.reconnecting" ? 1 : 0)
        }
      });
    }
    for (const [clientMessageId, pending] of this.pendingSubmissions) {
      pending.waiter.callback({
        event,
        data: {
          runtimeRunId: clientMessageId,
          clientMessageId,
          attempt: this.reconnectAttempts + (event === "chat.transport.reconnecting" ? 1 : 0)
        }
      });
    }
  }

  private sendPrepare(grant: RuntimeEdgeGrantTarget): Promise<void> {
    const requestId = `prepare-${Date.now()}-${randomIdentifier()}`;
    return new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.prepareWaiters.delete(requestId);
        reject(new RuntimeEdgeTransportError("Runtime 会话准备超时"));
      }, PREPARE_TIMEOUT_MS);
      this.prepareWaiters.set(requestId, { resolve, reject, timer });
      try {
        this.send(this.requireOpenSocket(), {
          schema_version: 1,
          type: "session.prepare",
          request_id: requestId,
          session_grant: grant.sessionGrant
        });
      } catch (reason) {
        window.clearTimeout(timer);
        this.prepareWaiters.delete(requestId);
        reject(asError(reason));
      }
    });
  }

  private handleMessage(event: MessageEvent): void {
    let message: RuntimeEdgeServerMessage;
    try {
      message = JSON.parse(String(event.data)) as RuntimeEdgeServerMessage;
    } catch {
      this.rejectAll(
        new RuntimeEdgeTransportError(
          "Runtime 返回了无效消息",
          "INVALID_RUNTIME_MESSAGE",
          false
        )
      );
      return;
    }
    if (message.type === "session.ready") {
      const waiter = this.prepareWaiters.get(message.request_id);
      if (!waiter) return;
      this.prepareWaiters.delete(message.request_id);
      window.clearTimeout(waiter.timer);
      waiter.resolve();
      this.announceDesktopCapabilities();
      return;
    }
    if (message.type === "session.history") {
      this.handleHistory(message);
      return;
    }
    if (message.type === "session.queue") {
      this.handleQueue(message);
      return;
    }
    if (message.type === "session.queue.updated") {
      this.handleQueueUpdated(message);
      return;
    }
    if (message.type === "run.queued") {
      this.handleQueued(message);
      return;
    }
    if (message.type === "run.accepted") {
      this.handleAccepted(message);
      return;
    }
    if (
      message.type === "run.redirect_pending"
      || message.type === "run.redirect_accepted"
      || message.type === "run.redirect_consumed"
      || message.type === "run.redirect_promoted"
      || message.type === "run.redirect_canceled"
      || message.type === "run.steer_submission_uncertain"
      || message.type === "followup.received"
      || message.type === "followup.applying"
      || message.type === "followup.applied"
      || message.type === "followup.continued"
      || message.type === "followup.failed"
    ) {
      this.handleRedirectDisposition(message);
      return;
    }
    if (message.type === "run.cancel_ack") {
      this.handleCancelAck(message);
      return;
    }
    if (message.type === "run.event") {
      this.handleRunEvent(message);
      return;
    }
    if (message.type === "run.event_batch") {
      for (const item of message.events) this.handleRunEvent(item);
      return;
    }
    if (message.type === "run.snapshot") {
      this.handleSnapshot(message);
      return;
    }
    if (message.type === "local.capability.request") {
      void this.handleLocalCapabilityRequest(message);
      return;
    }
    if (message.type === "error") {
      if (
        message.code === "UNSUPPORTED_MESSAGE"
        && message.request_id
        && this.desktopCapabilityRequests.delete(message.request_id)
      ) {
        return;
      }
      const reason = new RuntimeEdgeTransportError(
        message.message,
        message.code,
        message.retryable,
        {
          requestId: message.request_id,
          clientMessageId: message.client_message_id,
          runId: message.run_id,
          phase: message.phase,
          auditEventId: message.audit_event_id
        }
      );
      if (message.run_id) {
        if (this.rejectCancel(message.run_id, reason)) return;
        this.rejectRun(message.run_id, reason);
        return;
      }
      if (message.request_id) {
        const prepareWaiter = this.prepareWaiters.get(message.request_id);
        if (prepareWaiter) {
          this.prepareWaiters.delete(message.request_id);
          window.clearTimeout(prepareWaiter.timer);
          prepareWaiter.reject(reason);
          return;
        }
        const historyWaiter = this.historyWaiters.get(message.request_id);
        if (historyWaiter) {
          this.historyWaiters.delete(message.request_id);
          window.clearTimeout(historyWaiter.timer);
          historyWaiter.reject(reason);
          return;
        }
        const queueWaiter = this.queueWaiters.get(message.request_id);
        if (queueWaiter) {
          this.queueWaiters.delete(message.request_id);
          window.clearTimeout(queueWaiter.timer);
          queueWaiter.reject(reason);
          return;
        }
      }
      const pending = message.client_message_id
        ? this.pendingSubmissions.get(message.client_message_id)
        : undefined;
      if (pending) {
        this.pendingSubmissions.delete(pending.clientMessageId);
        pending.waiter.abortCleanup();
        pending.waiter.reject(reason);
        return;
      }
      const redirect = message.client_message_id
        ? this.redirectWaiters.get(message.client_message_id)
        : undefined;
      if (redirect) {
        this.rejectRedirect(redirect.clientMessageId, reason);
        return;
      }
      // An uncorrelated error cannot safely be assigned when one socket
      // carries many concurrent Sessions.  Fail all waiters so the caller can
      // reconnect/reconcile by its durable run id instead of corrupting one
      // arbitrary conversation.
      this.rejectAll(reason);
    }
  }

  /**
   * Execute a bounded capability in Electron's main process and return only
   * the structured result over the authenticated Runtime Edge socket. The
   * remote Runtime never receives a filesystem path or a shell handle.
   */
  private async handleLocalCapabilityRequest(
    message: RuntimeEdgeLocalCapabilityRequest
  ): Promise<void> {
    const cached = this.localCapabilityResults.get(message.request_id);
    if (cached) {
      const socket = this.socket;
      if (socket?.readyState === WebSocket.OPEN) this.send(socket, cached);
      return;
    }
    if (this.pendingLocalCapabilities.has(message.request_id)) return;
    const waiter = this.runWaiters.get(message.run_id);
    waiter?.callback({
      event: "local.capability.request",
      data: {
        request_id: message.request_id,
        run_id: message.run_id,
        capability: message.capability,
        args: message.args,
        reason: message.reason ?? null
      }
    });
    let result: RuntimeEdgeLocalCapabilityResult;
    try {
      if (!window.coworkDesktop?.requestCapability) {
        throw new Error("当前客户端不支持本地能力");
      }
      const local = await window.coworkDesktop.requestCapability({
        requestId: message.request_id,
        capability: message.capability,
        args: message.args
      });
      if (local.requiresApproval && local.approval) {
        this.pendingLocalCapabilities.set(message.request_id, message);
        const detail: LocalCapabilityApprovalDetail = {
          requestId: message.request_id,
          runId: message.run_id,
          sessionId: message.session_id,
          capability: message.capability,
          args: message.args,
          question: local.approval.question,
          ...(local.approval.target ? { target: local.approval.target } : {})
        };
        window.dispatchEvent(new CustomEvent(LOCAL_CAPABILITY_APPROVAL_EVENT, { detail }));
        return;
      }
      result = {
        schema_version: 1,
        type: "local.capability.result",
        request_id: message.request_id,
        run_id: message.run_id,
        capability: message.capability,
        ok: local.ok,
        ...(local.value === undefined ? {} : { value: local.value }),
        ...(local.error ? { error: local.error.slice(0, 512) } : {}),
        ...(local.canceled ? { canceled: true } : {})
      };
    } catch (reason) {
      result = {
        schema_version: 1,
        type: "local.capability.result",
        request_id: message.request_id,
        run_id: message.run_id,
        capability: message.capability,
        ok: false,
        error: reason instanceof Error ? reason.message.slice(0, 512) : "本地能力执行失败"
      };
    }
    this.localCapabilityResults.set(message.request_id, result);
    waiter?.callback({
      event: "local.capability.result",
      data: {
        request_id: result.request_id,
        run_id: result.run_id,
        capability: result.capability,
        ok: result.ok,
        value: result.value,
        error: result.error ?? null,
        canceled: result.canceled ?? false
      }
    });
    const socket = this.socket;
    if (socket?.readyState === WebSocket.OPEN) this.send(socket, result);
  }

  private handleHistory(message: RuntimeEdgeSessionHistory): void {
    const waiter = this.historyWaiters.get(message.request_id);
    if (!waiter) return;
    this.historyWaiters.delete(message.request_id);
    window.clearTimeout(waiter.timer);
    if (message.session_id !== waiter.runtimeSessionId) {
      waiter.reject(new RuntimeEdgeTransportError(
        "Runtime 返回了其他会话的历史，已拒绝显示",
        "RUNTIME_SESSION_HISTORY_MISMATCH",
        false,
        { requestId: message.request_id }
      ));
      return;
    }
    waiter.resolve(message.messages.map((item) => camelize(item)));
  }

  private handleQueue(message: RuntimeEdgeSessionQueue): void {
    const waiter = this.queueWaiters.get(message.request_id);
    if (!waiter) return;
    this.queueWaiters.delete(message.request_id);
    window.clearTimeout(waiter.timer);
    waiter.resolve({
      queuedCount: message.queued_count,
      scheduledQueuedCount: message.scheduled_queued_count,
      queuedRuns: (message.queued_runs ?? []).map((run) => ({
        runId: run.run_id,
        clientMessageId: run.client_message_id,
        enqueueSeq: run.enqueue_seq,
        prompt: run.prompt,
        attachmentFileIds: run.attachment_file_ids
      })),
      activeRun: message.active_run
        ? {
            runId: message.active_run.run_id,
            state: message.active_run.state,
            cancellationRequestedAt: message.active_run.cancellation_requested_at
          }
        : null
    });
  }

  private handleQueueUpdated(message: RuntimeEdgeSessionQueueUpdated): void {
    window.dispatchEvent(
      new CustomEvent<RuntimeEdgeQueueUpdatedDetail>(
        RUNTIME_EDGE_QUEUE_UPDATED_EVENT,
        {
          detail: {
            runtimeSessionId: message.session_id,
            queuedCount: message.queued_count,
            scheduledQueuedCount: message.scheduled_queued_count,
            queuedRuns: [],
            activeRun: null
          }
        }
      )
    );
  }

  private handleQueued(message: RuntimeEdgeRunQueued): void {
    const pending = this.pendingSubmissions.get(message.client_message_id);
    const redirect = this.redirectWaiters.get(message.client_message_id);
    if (!pending && redirect) {
      redirect.callback({
        event: "chat.run.queued",
        data: {
          runtimeWorkspaceId: redirect.runtimeWorkspaceId,
          runtimeSessionId: redirect.runtimeSessionId,
          runtimeRunId: message.run_id,
          modelEntryId: redirect.modelEntryId,
          status: "queued",
          enqueuedAt: message.enqueued_at,
          enqueueSeq: message.enqueue_seq,
          clientMessageId: message.client_message_id
        }
      });
      return;
    }
    const waiter = pending?.waiter ?? this.runWaiters.get(message.run_id);
    if (!waiter) return;
    waiter.callback({
      event: "chat.run.queued",
      data: {
        runtimeWorkspaceId: waiter.runtimeWorkspaceId,
        runtimeSessionId: waiter.runtimeSessionId,
        runtimeRunId: message.run_id,
        modelEntryId: waiter.modelEntryId,
        status: "queued",
        enqueuedAt: message.enqueued_at,
        enqueueSeq: message.enqueue_seq
      }
    });
  }

  private handleAccepted(message: RuntimeEdgeRunAccepted): void {
    const pending = this.pendingSubmissions.get(message.client_message_id);
    if (!pending) {
      const redirect = this.redirectWaiters.get(message.client_message_id);
      if (!redirect) return;
      redirect.callback({
        event: "chat.run.accepted",
        data: {
          runtimeWorkspaceId: redirect.runtimeWorkspaceId,
          runtimeSessionId: redirect.runtimeSessionId,
          runtimeRunId: message.run_id,
          modelEntryId: redirect.modelEntryId,
          status: "accepted",
          acceptedAt: message.accepted_at,
          clientMessageId: message.client_message_id
        }
      });
      this.promoteRedirectWaiter(redirect, message.run_id, false);
      this.resolveRedirect(redirect, {
        kind: "continued",
        clientMessageId: message.client_message_id,
        continuationRunId: message.run_id
      });
      return;
    }
    this.pendingSubmissions.delete(message.client_message_id);
    this.runWaiters.set(message.run_id, pending.waiter);
    pending.waiter.callback({
      event: "chat.run.accepted",
      data: {
        runtimeWorkspaceId: pending.waiter.runtimeWorkspaceId,
        runtimeSessionId: pending.waiter.runtimeSessionId,
        runtimeRunId: message.run_id,
        modelEntryId: pending.waiter.modelEntryId,
        status: "accepted",
        acceptedAt: message.accepted_at
      }
    });
  }

  private handleRedirectDisposition(
    message: RuntimeEdgeRedirectDisposition | RuntimeEdgeFollowupDisposition
  ): void {
    const waiter = this.redirectWaiters.get(message.client_message_id);
    if (!waiter) return;
    const kind = redirectDispositionKind(message.type);
    const dedupeKey = `${kind}:${message.event_seq}`;
    if (waiter.seenDispositions.has(dedupeKey)) return;
    waiter.seenDispositions.add(dedupeKey);
    waiter.callback({
      event: message.type,
      data: camelize(message as unknown as Record<string, unknown>)
    });

    if (
      message.type === "run.redirect_promoted"
      || message.type === "followup.continued"
    ) {
      const continuationRunId = message.type === "followup.continued"
        ? message.continuation_run_id
        : message.promoted_run_id;
      this.promoteRedirectWaiter(waiter, continuationRunId);
      this.resolveRedirect(waiter, {
        kind,
        clientMessageId: message.client_message_id,
        parentRunId: message.parent_run_id,
        continuationRunId
      });
      return;
    }
    if (
      message.type === "run.redirect_consumed"
      || message.type === "run.redirect_canceled"
      || message.type === "run.steer_submission_uncertain"
      || message.type === "followup.applied"
      || message.type === "followup.failed"
    ) {
      this.resolveRedirect(waiter, {
        kind,
        clientMessageId: message.client_message_id,
        parentRunId: message.parent_run_id
      });
    }
  }

  private promoteRedirectWaiter(
    waiter: RedirectWaiter,
    runId: string,
    resume = true
  ): void {
    if (waiter.continuationRunId === runId || this.runWaiters.has(runId)) return;
    waiter.continuationRunId = runId;
    const runWaiter = this.createRunWaiter({
      runtimeWorkspaceId: waiter.runtimeWorkspaceId,
      runtimeSessionId: waiter.runtimeSessionId,
      modelEntryId: waiter.modelEntryId,
      callback: waiter.callback,
      lastSeq: 0
    });
    // Promoted execution has its own lifecycle. It deliberately does not
    // reuse the parent Run's AbortSignal or waiter.
    runWaiter.resolve = () => undefined;
    runWaiter.reject = (reason) => waiter.callback({
      event: "chat.error",
      data: {
        runtimeRunId: runId,
        message: reason.message
      }
    });
    this.runWaiters.set(runId, runWaiter);
    if (!resume) return;
    try {
      this.send(this.requireOpenSocket(), {
        schema_version: 1,
        type: "run.resume",
        run_id: runId,
        after_seq: 0
      });
    } catch (reason) {
      this.runWaiters.delete(runId);
      runWaiter.reject(asError(reason));
    }
  }

  private handleRunEvent(message: RuntimeEdgeRunEvent): void {
    if (message.event_type === "chat.done") {
      this.resolveCancel(
        message.run_id,
        terminalStatus(String(message.data.status ?? "failed"))
      );
    }
    this.handleRedirectRunEvent(message);
    const waiter = this.runWaiters.get(message.run_id);
    if (!waiter || message.seq <= waiter.lastSeq) return;
    waiter.lastSeq = message.seq;
    if (message.event_type === "local.capability.request") {
      const requestId = stringField(message.data, "request_id", "requestId");
      const sessionId = stringField(message.data, "session_id", "sessionId")
        ?? waiter.runtimeSessionId;
      const capability = stringField(message.data, "capability");
      const args = message.data.args;
      if (
        requestId
        && sessionId
        && isLocalCapabilityName(capability)
        && args
        && typeof args === "object"
        && !Array.isArray(args)
      ) {
        void this.handleLocalCapabilityRequest({
          schema_version: 1,
          type: "local.capability.request",
          request_id: requestId,
          run_id: message.run_id,
          session_id: sessionId,
          capability,
          args: args as Record<string, unknown>,
          reason: stringField(message.data, "reason")
        });
      } else {
        void this.sendLocalCapabilityFailure(
          message.run_id,
          requestId ?? `invalid-${message.seq}`,
          isLocalCapabilityName(capability) ? capability : "workspace.read",
          "本地能力请求格式无效"
        );
      }
      return;
    }
    const data = camelize<Record<string, unknown>>(message.data);
    waiter.callback({
      event: message.event_type,
      data: {
        ...data,
        runtimeRunId: message.run_id,
        seq: message.seq,
        emittedAt: message.emitted_at
      }
    });
    if (message.event_type === "chat.done") {
      this.resolveRun(message.run_id);
    }
  }

  private async sendLocalCapabilityFailure(
    runId: string,
    requestId: string,
    capability: RuntimeEdgeLocalCapabilityRequest["capability"],
    error: string
  ): Promise<void> {
    const result: RuntimeEdgeLocalCapabilityResult = {
      schema_version: 1,
      type: "local.capability.result",
      request_id: requestId,
      run_id: runId,
      capability,
      ok: false,
      error
    };
    this.localCapabilityResults.set(requestId, result);
    const socket = this.socket;
    if (socket?.readyState === WebSocket.OPEN) this.send(socket, result);
  }

  private handleRedirectRunEvent(message: RuntimeEdgeRunEvent): void {
    const kind = followupEventDispositionKind(message.event_type);
    if (!kind) {
      if (
        message.event_type === "steered"
        || message.event_type === "queued"
        || message.event_type === "chat.steered"
        || message.event_type === "chat.queued"
      ) {
        const clientMessageId = stringField(
          message.data,
          "client_message_id",
          "clientMessageId"
        );
        if (clientMessageId && this.redirectWaiters.has(clientMessageId)) {
          this.rejectRedirect(clientMessageId, new RuntimeEdgeTransportError(
            "Runtime 返回了不可作为成功凭据的 Hermes redirect 状态",
            "RUNTIME_REDIRECT_PROTOCOL_ERROR",
            false,
            { clientMessageId, runId: message.run_id }
          ));
        }
      }
      return;
    }
    const clientMessageId = stringField(
      message.data,
      "client_message_id",
      "clientMessageId"
    );
    if (!clientMessageId) return;
    const waiter = this.redirectWaiters.get(clientMessageId);
    if (!waiter) return;
    const eventSeq = numberField(message.data, "event_seq", "eventSeq")
      ?? message.seq;
    const parentRunId = stringField(
      message.data,
      "parent_run_id",
      "parentRunId"
    ) ?? message.run_id;
    const dedupeKey = `${kind}:${eventSeq}`;
    if (waiter.seenDispositions.has(dedupeKey)) return;
    waiter.seenDispositions.add(dedupeKey);
    waiter.callback({
      event: message.event_type,
      data: {
        ...camelize<Record<string, unknown>>(message.data),
        runtimeRunId: message.run_id,
        seq: message.seq,
        emittedAt: message.emitted_at
      }
    });
    if (kind === "continued") {
      const continuationRunId = stringField(
        message.data,
        "continuation_run_id",
        "continuationRunId",
        "promoted_run_id",
        "promotedRunId"
      );
      if (!continuationRunId) {
        this.rejectRedirect(clientMessageId, new RuntimeEdgeTransportError(
          "Runtime 返回了缺少延续 Run 的追问终态",
          "RUNTIME_FOLLOWUP_PROTOCOL_ERROR",
          false,
          { clientMessageId, runId: message.run_id }
        ));
        return;
      }
      this.promoteRedirectWaiter(waiter, continuationRunId);
      this.resolveRedirect(waiter, {
        kind,
        clientMessageId,
        parentRunId,
        continuationRunId
      });
      return;
    }
    if (kind === "applied") {
      this.resolveRedirect(waiter, {
        kind,
        clientMessageId,
        parentRunId
      });
      return;
    }
    if (kind === "failed") {
      this.resolveRedirect(waiter, {
        kind,
        clientMessageId,
        parentRunId
      });
    }
  }

  private resolveRedirect(
    waiter: RedirectWaiter,
    result: RuntimeEdgeRedirectResult
  ): void {
    if (this.redirectWaiters.get(waiter.clientMessageId) !== waiter) return;
    this.redirectWaiters.delete(waiter.clientMessageId);
    waiter.abortCleanup();
    waiter.resolve(result);
  }

  private rejectRedirect(clientMessageId: string, reason: Error): void {
    const waiter = this.redirectWaiters.get(clientMessageId);
    if (!waiter) return;
    this.redirectWaiters.delete(clientMessageId);
    waiter.abortCleanup();
    waiter.reject(reason);
  }

  private handleSnapshot(message: RuntimeEdgeRunSnapshot): void {
    if (TERMINAL_RUN_STATES.has(message.status.toUpperCase())) {
      this.resolveCancel(message.run_id, terminalStatus(message.status));
    }
    const waiter = this.runWaiters.get(message.run_id);
    if (!waiter) return;
    for (const event of message.events) {
      this.handleRunEvent(event);
    }
    if (
      this.runWaiters.has(message.run_id)
      && TERMINAL_RUN_STATES.has(message.status.toUpperCase())
    ) {
      waiter.callback({
        event: "chat.done",
        data: {
          runtimeRunId: message.run_id,
          status: terminalStatus(message.status),
          answer: message.answer,
          seq: message.through_seq
        }
      });
      this.resolveRun(message.run_id);
    }
  }

  private createRunWaiter(input: {
    runtimeWorkspaceId: number;
    runtimeSessionId: string;
    modelEntryId: number;
    callback: (event: RuntimeEdgeChatEvent) => void;
    signal?: AbortSignal;
    lastSeq?: number;
  }): RunWaiter {
    const placeholder = () => undefined;
    const waiter: RunWaiter = {
      runtimeWorkspaceId: input.runtimeWorkspaceId,
      runtimeSessionId: input.runtimeSessionId,
      modelEntryId: input.modelEntryId,
      callback: input.callback,
      resolve: placeholder,
      reject: placeholder,
      abortCleanup: placeholder,
      lastSeq: input.lastSeq ?? 0
    };
    if (input.signal) {
      const onAbort = () => {
        for (const [clientMessageId, pending] of this.pendingSubmissions) {
          if (pending.waiter === waiter) {
            this.pendingSubmissions.delete(clientMessageId);
          }
        }
        for (const [runId, candidate] of this.runWaiters) {
          if (candidate === waiter) this.runWaiters.delete(runId);
        }
        waiter.reject(abortError());
      };
      input.signal.addEventListener("abort", onAbort, { once: true });
      waiter.abortCleanup = () => {
        input.signal?.removeEventListener("abort", onAbort);
      };
    }
    return waiter;
  }

  private resolveRun(runId: string): void {
    const waiter = this.runWaiters.get(runId);
    if (!waiter) return;
    this.runWaiters.delete(runId);
    waiter.abortCleanup();
    waiter.resolve();
  }

  private rejectRun(runId: string, reason: Error): void {
    const waiter = this.runWaiters.get(runId);
    if (!waiter) return;
    this.runWaiters.delete(runId);
    waiter.abortCleanup();
    waiter.reject(reason);
  }

  private handleCancelAck(message: RuntimeEdgeRunCancelAck): void {
    const status = message.status === "accepted"
      ? "canceling"
      : message.status === "already_terminal"
        ? "completed"
        : "cancel_failed";
    this.resolveCancel(message.run_id, status);
  }

  private resolveCancel(runId: string, status: string): boolean {
    const waiter = this.cancelWaiters.get(runId);
    if (!waiter) return false;
    this.cancelWaiters.delete(runId);
    window.clearTimeout(waiter.timer);
    waiter.resolve({
      runtimeWorkspaceId: waiter.runtimeWorkspaceId,
      runtimeSessionId: waiter.runtimeSessionId,
      runtimeRunId: runId,
      modelEntryId: waiter.modelEntryId,
      status
    });
    return true;
  }

  private rejectCancel(runId: string, reason: Error): boolean {
    const waiter = this.cancelWaiters.get(runId);
    if (!waiter) return false;
    this.cancelWaiters.delete(runId);
    window.clearTimeout(waiter.timer);
    waiter.reject(reason);
    return true;
  }

  private rejectAll(reason: Error): void {
    for (const waiter of this.prepareWaiters.values()) {
      window.clearTimeout(waiter.timer);
      waiter.reject(reason);
    }
    this.prepareWaiters.clear();
    for (const waiter of this.historyWaiters.values()) {
      window.clearTimeout(waiter.timer);
      waiter.reject(reason);
    }
    this.historyWaiters.clear();
    for (const waiter of this.queueWaiters.values()) {
      window.clearTimeout(waiter.timer);
      waiter.reject(reason);
    }
    this.queueWaiters.clear();
    for (const pending of this.pendingSubmissions.values()) {
      pending.waiter.abortCleanup();
      pending.waiter.reject(reason);
    }
    this.pendingSubmissions.clear();
    for (const waiter of this.redirectWaiters.values()) {
      waiter.abortCleanup();
      waiter.reject(reason);
    }
    this.redirectWaiters.clear();
    for (const waiter of this.runWaiters.values()) {
      waiter.abortCleanup();
      waiter.reject(reason);
    }
    this.runWaiters.clear();
    for (const waiter of this.cancelWaiters.values()) {
      window.clearTimeout(waiter.timer);
      waiter.reject(reason);
    }
    this.cancelWaiters.clear();
  }

  private rejectNonRunWaiters(reason: Error): void {
    for (const waiter of this.prepareWaiters.values()) {
      window.clearTimeout(waiter.timer);
      waiter.reject(reason);
    }
    this.prepareWaiters.clear();
    for (const waiter of this.historyWaiters.values()) {
      window.clearTimeout(waiter.timer);
      waiter.reject(reason);
    }
    this.historyWaiters.clear();
    for (const waiter of this.queueWaiters.values()) {
      window.clearTimeout(waiter.timer);
      waiter.reject(reason);
    }
    this.queueWaiters.clear();
    for (const waiter of this.redirectWaiters.values()) {
      waiter.abortCleanup();
      waiter.reject(reason);
    }
    this.redirectWaiters.clear();
    for (const waiter of this.cancelWaiters.values()) {
      window.clearTimeout(waiter.timer);
      waiter.reject(reason);
    }
    this.cancelWaiters.clear();
    this.desktopCapabilityRequests.clear();
  }

  /**
   * Advertise desktop execution after the legacy prepare handshake. Older
   * Supervisors reject unknown messages, so their correlated error is ignored
   * and the normal chat/history path remains fully compatible.
   */
  private announceDesktopCapabilities(): void {
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    const requestId = `desktop-capabilities-${Date.now()}-${randomIdentifier()}`;
    this.desktopCapabilityRequests.add(requestId);
    window.setTimeout(() => this.desktopCapabilityRequests.delete(requestId), 10_000);
    try {
      this.send(socket, {
        schema_version: 1,
        type: "desktop.capabilities",
        request_id: requestId,
        local_capabilities: true
      });
    } catch {
      this.desktopCapabilityRequests.delete(requestId);
    }
  }

  private targetForAction(payload: RuntimeRunActionRequest): TargetRequest {
    const runtimeWorkspaceId = payload.runtimeWorkspaceId
      ?? this.lastTarget?.runtimeWorkspaceId;
    const runtimeSessionId = payload.runtimeSessionId
      ?? this.lastTarget?.runtimeSessionId;
    if (runtimeWorkspaceId === undefined || !runtimeSessionId) {
      throw new RuntimeEdgeTransportError(
        "缺少运行会话上下文，无法恢复任务",
        "RUNTIME_SESSION_CONTEXT_REQUIRED",
        false
      );
    }
    return {
      runtimeWorkspaceId,
      runtimeSessionId,
      modelEntryId: payload.modelEntryId ?? this.lastTarget?.modelEntryId
    };
  }

  private isSocketOpen(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  private requireOpenSocket(): WebSocket {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new RuntimeEdgeTransportError("Runtime 实时连接尚未就绪");
    }
    return this.socket;
  }

  private send(socket: WebSocket, message: RuntimeEdgeClientMessage): void {
    socket.send(JSON.stringify(message));
  }
}

function terminalStatus(state: string): string {
  const normalized = state.toUpperCase();
  if (normalized === "COMPLETED") return "completed";
  if (normalized === "CANCELED") return "canceled";
  return "failed";
}

function redirectDispositionKind(
  type: (RuntimeEdgeRedirectDisposition | RuntimeEdgeFollowupDisposition)["type"]
): RuntimeEdgeRedirectDispositionKind {
  return followupEventDispositionKind(type) ?? "failed";
}

function followupEventDispositionKind(
  type: string
): RuntimeEdgeRedirectDispositionKind | undefined {
  if (type === "followup.received" || type === "run.redirect_pending") return "received";
  if (
    type === "followup.applying"
    || type === "run.redirect_accepted"
    || type === "chat.steer.accepted"
  ) return "applying";
  if (
    type === "followup.applied"
    || type === "run.redirect_consumed"
    || type === "chat.steer.consumed"
  ) return "applied";
  if (type === "followup.continued" || type === "run.redirect_promoted") return "continued";
  if (
    type === "followup.failed"
    || type === "run.redirect_canceled"
    || type === "run.steer_submission_uncertain"
  ) return "failed";
  return undefined;
}

function stringField(
  record: Record<string, unknown>,
  ...keys: string[]
): string | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value) return value;
  }
  return undefined;
}

// Mirror of LOCAL_CAPABILITY_NAMES in src/main/capability-protocol.ts; this
// file lives in the web tsconfig scope and cannot import from src/main.
const LOCAL_CAPABILITY_NAMES: ReadonlySet<string> = new Set([
  "workspace.list",
  "workspace.read",
  "workspace.write",
  "workspace.mkdir",
  "workspace.trash",
  "command.run",
  "browser.open",
  "browser.session_start",
  "browser.navigate",
  "browser.snapshot",
  "browser.click",
  "browser.type",
  "browser.press",
  "browser.scroll",
  "browser.screenshot",
  "browser.session_close",
  "browser.read",
  "browser.wait",
  "browser.hover",
  "browser.select",
  "browser.upload",
  "browser.downloads",
  "browser.back",
  "browser.reload",
  "browser.tab_list",
  "browser.tab_new",
  "browser.tab_select",
  "browser.tab_close"
]);

function isLocalCapabilityName(
  value: string | undefined
): value is RuntimeEdgeLocalCapabilityRequest["capability"] {
  return typeof value === "string" && LOCAL_CAPABILITY_NAMES.has(value);
}

function numberField(
  record: Record<string, unknown>,
  ...keys: string[]
): number | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return undefined;
}

function randomIdentifier(): string {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2).padEnd(12, "0");
}

function sameIdentifiers(
  left: number[] | undefined,
  right: number[] | undefined
): boolean {
  const normalizedLeft = left ?? [];
  const normalizedRight = right ?? [];
  return normalizedLeft.length === normalizedRight.length
    && normalizedLeft.every(
      (value, index) => value === normalizedRight[index]
    );
}

function abortError(): DOMException {
  return new DOMException("The operation was aborted", "AbortError");
}

function asError(reason: unknown): Error {
  return reason instanceof Error ? reason : new Error(String(reason));
}
