import type { ChatFileSummary } from "@hermes-cloud/schemas";

export type ChatRole = "user" | "assistant" | "tool" | "system";

interface ChatMessageBase {
  id: string;
  role: ChatRole;
  runtimeRunId?: string;
  status?: string;
  content?: string;
  error?: string;
  ttftMs?: number;
  thinkingPhase?: "submitting" | "started";
}

export interface ScheduledMessageSource {
  type: "scheduled";
  scheduledTaskId: number;
  scheduledTaskName: string;
  triggerKind: string;
  taskAvailable: boolean;
}

export interface ActiveTurn {
  turnId: string;
  userMessageId: string;
  assistantMessageId: string;
  submittedText: string;
  runtimeRunId?: string;
  status: "submitting" | "streaming" | "completed" | "failed";
  error?: string;
}

export interface ChatBubbleMessage extends ChatMessageBase {
  kind?: "message";
  content: string;
  attachments?: ChatMessageAttachment[];
  scheduledSource?: ScheduledMessageSource;
  contextAgentProfileId?: number;
  clientMessageId?: string;
  redirectId?: string;
  parentRunId?: string;
  promotedRunId?: string;
  queuedRunId?: string;
  correctionStatus?: ChatCorrectionStatus;
  submissionHint?: string;
  redirectEventSeqs?: Partial<Record<ChatCorrectionStatus, number>>;
}

export interface ChatMessageAttachment {
  fileId: number;
  displayName: string;
  mimeType?: string | null;
  sourceType?: string;
}

export type ChatCorrectionStatus =
  | "sending"
  | "received"
  | "applying"
  | "applied"
  | "continued"
  | "failed";

export interface ReasoningMessage extends ChatMessageBase {
  kind: "reasoning";
  role: "assistant";
  text: string;
}

export interface ActivityMessage extends ChatMessageBase {
  kind: "activity";
  role: "assistant";
  text: string;
}

export interface ToolMessage extends ChatMessageBase {
  kind: "tool";
  role: "tool";
  callId: string;
  providerCallId?: string;
  hasStableCallId?: boolean;
  input?: string;
  name: string;
  status: "running" | "completed" | "failed";
  preview?: string;
  result?: string;
}

export interface BackgroundTaskMessage extends ChatMessageBase {
  kind: "background";
  role: "system";
  taskId: string;
  status: "queued" | "running" | "completed" | "failed" | "interrupted";
  goal?: string;
  activity?: string;
  summary?: string;
}

export interface CapabilityErrorMessage extends ChatMessageBase {
  kind: "capability-error";
  role: "system";
  capabilityType: "skill" | "mcp" | "knowledge_base";
  errorCode: string;
  displayMessage: string;
  recoverable: boolean;
  errorId: string;
}

export interface RunProgress {
  runtimeRunId: string;
  label: string;
  state: "running" | "waiting_subagents";
}

export type ChatMessage = ChatBubbleMessage | ReasoningMessage | ActivityMessage | ToolMessage | BackgroundTaskMessage | CapabilityErrorMessage;

export interface SessionCache {
  messages: ChatMessage[];
  files: ChatFileSummary[];
  runId?: string | null;
  streaming?: boolean;
  lastSeq?: number;
  activeTurn?: ActiveTurn;
  progress?: RunProgress;
  clarifyRunId?: string;
  pendingScheduledSource?: ScheduledMessageSource;
  deferredDone?: {
    runtimeRunId: string;
    event: ChatRunProjectionEvent;
  };
  responseSegmentGroupIds?: string[];
}

const RUNTIME_MEDIA_MARKER_RE = /^[^\r\n]*\/data\/workspace\/sessions\/[^\r\n]+$/gm;

export function stripRuntimeMediaMarkers(text: string): string {
  return text.replace(RUNTIME_MEDIA_MARKER_RE, "").trim();
}

export function beginScheduledTask(
  cache: SessionCache,
  content: string,
  source: ScheduledMessageSource,
  executionKey: string
): SessionCache {
  const userId = `scheduled-user-${executionKey}`;
  const assistantId = `scheduled-assistant-${executionKey}`;
  if (cache.messages.some((message) => message.id === userId)) return cache;
  return {
    ...cache,
    messages: [
      ...cache.messages,
      { id: userId, role: "user", content, scheduledSource: source }
    ],
    activeTurn: {
      turnId: executionKey,
      userMessageId: userId,
      assistantMessageId: assistantId,
      submittedText: content,
      status: "submitting"
    }
  };
}

export interface ChatRunProjectionEvent {
  event: string;
  data: Record<string, unknown>;
  ttftMs?: number;
}

export interface ChatRunProjectionResult {
  cache: SessionCache;
  applied: boolean;
  visibleMutation?: VisibleMutation;
  errorMessage?: string;
}

export type VisibleMutationKind = "assistant-text" | "activity" | "capability" | "lifecycle" | "progress" | "reasoning" | "tool" | "subagent";

export interface VisibleMutation {
  targetId: string;
  kind: VisibleMutationKind;
}

const hasVisibleText = (value: unknown) => typeof value === "string" && value.trim().length > 0;
const SUBAGENT_EVENT_TYPES = new Set([
  "subagent.start",
  "subagent.progress",
  "subagent.complete",
  "subagent.failed",
  "subagent.error",
  "subagent.interrupted",
  "subagent.canceled"
]);

function stableSubagentTaskId(data: Record<string, unknown>): string | undefined {
  if (!SUBAGENT_EVENT_TYPES.has(typeof data.type === "string" ? data.type : "")) return undefined;
  for (const key of ["subagent_id", "delegation_id"] as const) {
    const value = data[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

function visibleMutationFor(
  before: SessionCache,
  after: SessionCache,
  event: ChatRunProjectionEvent
): VisibleMutation | undefined {
  if (event.event === "chat.run.started") {
    const assistant = after.messages.find((message) =>
      message.id === assistantMessageId(String(after.runId ?? ""))
      && message.thinkingPhase === "started"
    );
    return assistant ? { targetId: assistant.id, kind: "lifecycle" } : undefined;
  }
  if (
    event.event === "chat.progress.delta"
    && hasVisibleText(event.data.delta)
    && after.runId
  ) {
    return { targetId: progressMessageId(after.runId), kind: "progress" };
  }
  if (event.event === "capability.error") {
    const errorId = event.data.error_id ?? event.data.errorId;
    return typeof errorId === "string"
      ? { targetId: `capability-${errorId}`, kind: "capability" }
      : undefined;
  }
  const eligible = event.event === "chat.activity.delta"
    || event.event === "chat.message.delta"
    || event.event === "chat.reasoning.delta"
    || event.event === "chat.message.completed"
    || event.event === "chat.tool.event"
    || (event.event === "chat.subagent.event" && Boolean(stableSubagentTaskId(event.data)));
  if (!eligible) return undefined;

  const beforeById = new Map(before.messages.map((message) => [message.id, message]));
  const changed = after.messages.filter((message) => {
    if (beforeById.get(message.id) === message) return false;
    if (message.kind === "activity" || message.kind === "reasoning") return Boolean(message.text.trim());
    if (message.kind === "tool") return Boolean(message.name.trim());
    if (message.kind === "background") return message.taskId !== "transport" && message.taskId !== "parked";
    return message.role === "assistant" && Boolean(message.content.trim());
  });
  const target = changed[changed.length - 1];
  if (!target) return undefined;

  const timelineStart = after.messages.find((message) => (
    (message.kind === "reasoning" || message.kind === "activity" || message.kind === "tool" || message.kind === "background" || message.kind === "capability-error")
    && message.runtimeRunId === target.runtimeRunId
  ));
  const timelineTargetId = timelineStart ? `execution-timeline-${timelineStart.id}` : target.id;

  if (target.kind === "tool") {
    return { targetId: timelineTargetId, kind: "tool" };
  }
  if (target.kind === "activity") return { targetId: timelineTargetId, kind: "activity" };
  if (target.kind === "reasoning") return { targetId: timelineTargetId, kind: "reasoning" };
  if (target.kind === "background") return { targetId: timelineTargetId, kind: "subagent" };
  return { targetId: target.id, kind: "assistant-text" };
}

export function isFirstVisibleAgentEvent(
  event: Pick<ChatRunProjectionEvent, "event" | "data">
): boolean {
  if (event.event === "chat.activity.delta" || event.event === "chat.message.delta" || event.event === "chat.reasoning.delta" || event.event === "chat.progress.delta") {
    return hasVisibleText(event.data.delta);
  }
  if (event.event === "chat.message.completed") return hasVisibleText(event.data.text);
  return event.event === "chat.tool.event"
    || event.event === "capability.error"
    || event.event === "chat.turn.parked"
    || (event.event === "chat.subagent.event" && Boolean(stableSubagentTaskId(event.data)));
}

export const assistantMessageId = (runtimeRunId: string) => `assistant-${runtimeRunId}`;
export const userMessageId = (runtimeRunId: string) => `user-${runtimeRunId}`;
export const progressMessageId = (runtimeRunId: string) => `progress-${runtimeRunId}`;

function appendCapabilityError(
  cache: SessionCache,
  runtimeRunId: string,
  data: Record<string, unknown>
): SessionCache {
  const errorId = data.error_id ?? data.errorId;
  const capabilityType = data.capability_type ?? data.capabilityType;
  const errorCode = data.error_code ?? data.errorCode;
  const displayMessage = data.display_message ?? data.displayMessage;
  if (
    typeof errorId !== "string"
    || !errorId.trim()
    || !["skill", "mcp", "knowledge_base"].includes(String(capabilityType))
    || typeof errorCode !== "string"
    || typeof displayMessage !== "string"
  ) return cache;
  const id = `capability-${errorId}`;
  if (cache.messages.some((message) => message.id === id)) return cache;
  return {
    ...cache,
    messages: insertBeforeAssistant(cache.messages, runtimeRunId, {
        id,
        kind: "capability-error",
        role: "system",
        runtimeRunId,
        capabilityType: capabilityType as "skill" | "mcp" | "knowledge_base",
        errorCode,
        displayMessage,
        recoverable: data.recoverable === true,
        errorId
      })
  };
}

export function scheduledSourceFromUnknown(value: unknown): ScheduledMessageSource | undefined {
  if (!value || typeof value !== "object") return undefined;
  const source = value as Record<string, unknown>;
  if (source.type !== "scheduled") return undefined;
  const taskId = source.scheduledTaskId ?? source.scheduled_task_id;
  const taskName = source.scheduledTaskName ?? source.scheduled_task_name;
  const triggerKind = source.triggerKind ?? source.trigger_kind;
  const taskAvailable = source.taskAvailable ?? source.task_available;
  if (typeof taskId !== "number" || !Number.isFinite(taskId)) return undefined;
  if (typeof taskName !== "string" || !taskName.trim()) return undefined;
  if (typeof triggerKind !== "string" || !triggerKind.trim()) return undefined;
  if (typeof taskAvailable !== "boolean") return undefined;
  return {
    type: "scheduled",
    scheduledTaskId: taskId,
    scheduledTaskName: taskName,
    triggerKind,
    taskAvailable
  };
}

export function beginChatTurn(
  cache: SessionCache,
  content: string,
  turnId: string,
  assistantContent = "",
  attachments: ChatMessageAttachment[] = []
): SessionCache {
  const userId = `user-${turnId}`;
  const assistantId = `assistant-pending-${turnId}`;
  return {
    ...cache,
    messages: [
      ...cache.messages,
      {
        id: userId,
        role: "user",
        content,
        ...(attachments.length > 0 ? { attachments } : {})
      },
      {
        id: assistantId,
        role: "assistant",
        content: assistantContent,
        status: "streaming",
        thinkingPhase: assistantContent ? undefined : "submitting"
      }
    ],
    streaming: true,
    activeTurn: {
      turnId,
      userMessageId: userId,
      assistantMessageId: assistantId,
      submittedText: content,
      status: "submitting"
    }
  };
}

export function beginChatCorrection(
  cache: SessionCache,
  content: string,
  clientMessageId: string,
  parentRunId: string,
  attachments: ChatMessageAttachment[] = []
): SessionCache {
  if (cache.messages.some((message) => (
    message.role === "user"
    && "clientMessageId" in message
    && message.clientMessageId === clientMessageId
  ))) return cache;
  return {
    ...cache,
    messages: [
      ...cache.messages,
      {
        id: `correction-${clientMessageId}`,
        role: "user",
        content,
        ...(attachments.length > 0 ? { attachments } : {}),
        clientMessageId,
        parentRunId,
        correctionStatus: "sending",
        submissionHint: "正在发送修正"
      }
    ]
  };
}

export function beginQueuedChatSubmission(
  cache: SessionCache,
  content: string,
  clientMessageId: string,
  attachments: ChatMessageAttachment[] = []
): SessionCache {
  if (cache.messages.some((message) => (
    message.role === "user"
    && "clientMessageId" in message
    && message.clientMessageId === clientMessageId
  ))) return cache;
  return {
    ...cache,
    messages: [
      ...cache.messages,
      {
        id: `queued-user-${clientMessageId}`,
        role: "user",
        content,
        ...(attachments.length > 0 ? { attachments } : {}),
        clientMessageId,
        status: "queued",
        submissionHint: "将在当前任务完成后执行"
      }
    ]
  };
}

export function acceptQueuedChatRun(
  cache: SessionCache,
  clientMessageId: string,
  runtimeRunId: string
): SessionCache {
  return updateCorrectionBubble(cache, clientMessageId, (message) => ({
    ...message,
    queuedRunId: runtimeRunId,
    status: "queued",
    submissionHint: "已排队，等待当前任务完成"
  }));
}

export function beginQueuedChatRun(
  cache: SessionCache,
  clientMessageId: string,
  runtimeRunId: string
): SessionCache {
  const queued = correctionBubble(cache, clientMessageId);
  if (!queued) return ensureActiveChatRun(cache, runtimeRunId, 0);
  const rebound = updateCorrectionBubble(
    cache,
    clientMessageId,
    (message) => ({
      ...message,
      status: undefined,
      submissionHint: undefined
    })
  );
  const messages = ensureAssistantMessage(rebound.messages, runtimeRunId);
  return {
    ...cache,
    messages,
    runId: runtimeRunId,
    streaming: true,
    lastSeq: 0,
    activeTurn: {
      turnId: clientMessageId,
      userMessageId: queued.id,
      assistantMessageId: assistantMessageId(runtimeRunId),
      submittedText: queued.content,
      runtimeRunId,
      status: "streaming"
    }
  };
}

export function failChatCorrection(
  cache: SessionCache,
  clientMessageId: string,
  error: string
): SessionCache {
  return updateCorrectionBubble(cache, clientMessageId, (message) => ({
    ...message,
    correctionStatus: "failed",
    submissionHint: "修正发送失败",
    error: error.trim() || "修正发送失败"
  }));
}

export function beginPromotedCorrectionRun(
  cache: SessionCache,
  clientMessageId: string,
  runtimeRunId: string
): SessionCache {
  const correction = correctionBubble(cache, clientMessageId);
  if (!correction) return ensureActiveChatRun(cache, runtimeRunId, 0);
  const assistantId = assistantMessageId(runtimeRunId);
  const rebound = updateCorrectionBubble(cache, clientMessageId, (message) => ({
    ...message,
    status: undefined,
    submissionHint: undefined
  }));
  const messages = ensureAssistantMessage(rebound.messages, runtimeRunId).map(
    (message) => message.id === assistantId
      ? { ...message, thinkingPhase: message.thinkingPhase ?? "submitting" }
      : message
  );
  return {
    ...rebound,
    messages,
    runId: runtimeRunId,
    streaming: true,
    lastSeq: 0,
    activeTurn: {
      turnId: clientMessageId,
      userMessageId: correction.id,
      assistantMessageId: assistantId,
      submittedText: correction.content,
      runtimeRunId,
      status: "streaming"
    }
  };
}

export function acceptChatRun(cache: SessionCache, runtimeRunId: string, pendingAssistantId?: string): SessionCache {
  const stableAssistantId = assistantMessageId(runtimeRunId);
  const messages = ensureAssistantMessage(
    cache.messages.map((message) => message.id === pendingAssistantId ? { ...message, id: stableAssistantId, runtimeRunId, content: "" } : message),
    runtimeRunId
  );
  const activeTurn = cache.activeTurn
    ? {
        ...cache.activeTurn,
        assistantMessageId: stableAssistantId,
        runtimeRunId,
        status: "streaming" as const
      }
    : undefined;
  return { ...cache, messages, runId: runtimeRunId, streaming: true, lastSeq: 0, ...(activeTurn ? { activeTurn } : {}) };
}

export function ensureActiveChatRun(
  cache: SessionCache,
  runtimeRunId: string,
  afterSeq: number,
  source?: unknown
): SessionCache {
  const scheduledSource = scheduledSourceFromUnknown(source);
  const messages = ensureAssistantMessage(cache.messages, runtimeRunId);
  if (!scheduledSource) {
    return {
      ...cache,
      messages,
      runId: runtimeRunId,
      streaming: true,
      lastSeq: afterSeq
    };
  }
  const lastUserIndex = [...messages].map((message, index) => ({ message, index }))
    .reverse()
    .find(({ message }) => message.role === "user")?.index;
  const scheduledTurnIndex = cache.activeTurn
    ? messages.findIndex((message) => message.id === cache.activeTurn?.userMessageId)
    : -1;
  const sourceIndex = scheduledTurnIndex >= 0 ? scheduledTurnIndex : lastUserIndex;
  const projectedMessages = sourceIndex === undefined
    ? messages
    : messages.map((message, index) => index === sourceIndex
      ? { ...message, id: userMessageId(runtimeRunId), runtimeRunId, scheduledSource }
      : message);
  const activeTurn = cache.activeTurn
    ? {
        ...cache.activeTurn,
        userMessageId: userMessageId(runtimeRunId),
        assistantMessageId: assistantMessageId(runtimeRunId),
        runtimeRunId,
        status: "streaming" as const
      }
    : undefined;
  return {
    ...cache,
    messages: projectedMessages,
    runId: runtimeRunId,
    streaming: true,
    lastSeq: afterSeq,
    pendingScheduledSource: scheduledSource,
    ...(activeTurn ? { activeTurn } : {})
  };
}

const CORRECTION_EVENT_STATUS: Record<string, ChatCorrectionStatus> = {
  "followup.received": "received",
  "followup.applying": "applying",
  "followup.applied": "applied",
  "followup.continued": "continued",
  "followup.failed": "failed",
  "run.redirect_pending": "received",
  "run.redirect_accepted": "applying",
  "chat.steer.accepted": "applying",
  "run.redirect_consumed": "applied",
  "chat.steer.consumed": "applied",
  "run.redirect_promoted": "continued",
  "run.steer_submission_uncertain": "failed",
  "run.redirect_canceled": "failed"
};

const CORRECTION_STATUS_RANK: Record<ChatCorrectionStatus, number> = {
  sending: 0,
  received: 1,
  applying: 2,
  applied: 3,
  continued: 3,
  failed: 3
};

const CORRECTION_STATUS_HINT: Record<ChatCorrectionStatus, string> = {
  sending: "正在发送追问",
  received: "已接收",
  applying: "正在应用",
  applied: "已应用",
  continued: "已继续",
  failed: "处理异常"
};

export function correctionStatusHint(status: ChatCorrectionStatus): string {
  return CORRECTION_STATUS_HINT[status];
}

function isCorrectionEvent(event: string): boolean {
  return event in CORRECTION_EVENT_STATUS;
}

function eventSequence(data: Record<string, unknown>): number | undefined {
  for (const key of ["seq", "eventSeq", "event_seq"] as const) {
    const value = data[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return undefined;
}

function stringValue(
  data: Record<string, unknown>,
  ...keys: string[]
): string | undefined {
  for (const key of keys) {
    const value = data[key];
    if (typeof value === "string" && value) return value;
  }
  return undefined;
}

function correctionBubble(
  cache: SessionCache,
  clientMessageId: string
): ChatBubbleMessage | undefined {
  return cache.messages.find((message): message is ChatBubbleMessage => (
    message.role === "user"
    && message.clientMessageId === clientMessageId
  ));
}

function updateCorrectionBubble(
  cache: SessionCache,
  clientMessageId: string,
  update: (message: ChatBubbleMessage) => ChatBubbleMessage
): SessionCache {
  let changed = false;
  const messages = cache.messages.map((message): ChatMessage => {
    if (
      message.role !== "user"
      || message.clientMessageId !== clientMessageId
    ) return message;
    changed = true;
    return update(message);
  });
  return changed ? { ...cache, messages } : cache;
}

function projectCorrectionEvent(
  cache: SessionCache,
  event: ChatRunProjectionEvent
): ChatRunProjectionResult {
  const clientMessageId = stringValue(
    event.data,
    "clientMessageId",
    "client_message_id"
  );
  if (!clientMessageId) return { cache, applied: false };
  const bubble = correctionBubble(cache, clientMessageId);
  if (!bubble) return { cache, applied: false };
  const status = CORRECTION_EVENT_STATUS[event.event];
  const seq = eventSequence(event.data);
  const seenSeq = bubble.redirectEventSeqs?.[status];
  if (seq !== undefined && seenSeq !== undefined && seq <= seenSeq) {
    return { cache, applied: false };
  }
  const previousStatus = bubble.correctionStatus ?? "sending";
  if (
    CORRECTION_STATUS_RANK[status] < CORRECTION_STATUS_RANK[previousStatus]
    || (
      CORRECTION_STATUS_RANK[status] === CORRECTION_STATUS_RANK[previousStatus]
      && status !== previousStatus
    )
  ) {
    return { cache, applied: false };
  }
  const redirectId = stringValue(
    event.data,
    "followupId",
    "followup_id",
    "redirectId",
    "redirect_id"
  );
  const parentRunId = stringValue(
    event.data,
    "parentRunId",
    "parent_run_id"
  );
  const promotedRunId = stringValue(
    event.data,
    "continuationRunId",
    "continuation_run_id",
    "promotedRunId",
    "promoted_run_id"
  );
  let next = updateCorrectionBubble(cache, clientMessageId, (message) => ({
    ...message,
    correctionStatus: status,
    submissionHint: CORRECTION_STATUS_HINT[status],
    ...(redirectId ? { redirectId } : {}),
    ...(parentRunId ? { parentRunId } : {}),
    ...(promotedRunId ? { promotedRunId } : {}),
    redirectEventSeqs: {
      ...message.redirectEventSeqs,
      ...(seq !== undefined ? { [status]: seq } : {})
    }
  }));
  if (status === "applied") {
    const groupId = stringValue(
      event.data,
      "dispatchGroupId",
      "dispatch_group_id"
    ) ?? `event-${seq ?? clientMessageId}`;
    const parent = parentRunId ?? bubble.parentRunId;
    if (
      parent
      && !next.responseSegmentGroupIds?.includes(groupId)
    ) {
      const baseAssistantId = assistantMessageId(parent);
      const archivedAssistantId = `${baseAssistantId}-segment-${groupId}`;
      let foundBase = false;
      const messages = next.messages.map((message): ChatMessage => {
        if (message.id !== baseAssistantId) return message;
        foundBase = true;
        return { ...message, id: archivedAssistantId };
      });
      if (foundBase) {
        messages.push({
          id: baseAssistantId,
          role: "assistant",
          content: "",
          status: "streaming"
        });
      }
      next = {
        ...next,
        messages,
        responseSegmentGroupIds: [
          ...(next.responseSegmentGroupIds ?? []),
          groupId
        ]
      };
    }
  }
  return { cache: next, applied: next !== cache };
}

function hasBlockingCorrection(
  cache: SessionCache,
  runtimeRunId: string
): boolean {
  return cache.messages.some((message) => (
    message.role === "user"
    && message.parentRunId === runtimeRunId
    && (
      message.correctionStatus === "sending"
      || message.correctionStatus === "received"
      || message.correctionStatus === "applying"
    )
  ));
}

export function projectChatRunEvent(
  cache: SessionCache,
  runtimeRunId: string,
  event: ChatRunProjectionEvent
): ChatRunProjectionResult {
  const seq = eventSequence(event.data);
  if (seq !== undefined && cache.runId === runtimeRunId && (cache.lastSeq ?? 0) >= seq) {
    return { cache, applied: false };
  }

  if (isCorrectionEvent(event.event)) {
    const projected = projectCorrectionEvent(cache, event);
    if (!projected.applied) return projected;
    const withSequence = {
      ...projected.cache,
      lastSeq: seq ?? projected.cache.lastSeq
    };
    const deferred = withSequence.deferredDone;
    if (
      deferred
      && !hasBlockingCorrection(withSequence, deferred.runtimeRunId)
    ) {
      return projectChatRunEvent(
        { ...withSequence, deferredDone: undefined },
        deferred.runtimeRunId,
        {
          ...deferred.event,
          data: Object.fromEntries(
            Object.entries(deferred.event.data).filter(([key]) => (
              key !== "seq" && key !== "eventSeq" && key !== "event_seq"
            ))
          )
        }
      );
    }
    return { ...projected, cache: withSequence };
  }

  if (event.event === "chat.transport.reconnecting" || event.event === "chat.transport.resumed") {
    const clientMessageId = stringValue(
      event.data,
      "clientMessageId",
      "client_message_id"
    );
    const transportCache = {
      ...cache,
      streaming: true,
      lastSeq: seq ?? cache.lastSeq
    };
    const next = event.event === "chat.transport.reconnecting"
      ? upsertTransportNotice(transportCache, runtimeRunId, clientMessageId)
      : removeTransportNotice(transportCache);
    return { cache: next, applied: next !== cache };
  }

  if (
    (event.event === "chat.done" || event.event === "chat.error")
    && cache.streaming
    && cache.runId
    && cache.runId !== runtimeRunId
  ) {
    const targetAssistantId = assistantMessageId(runtimeRunId);
    const error = event.event === "chat.error"
      ? String(event.data.message ?? "生成失败")
      : undefined;
    const next = removeLifecycleStatus({
      ...cache,
      messages: cache.messages.map((message) => (
        message.id === targetAssistantId
          ? {
              ...message,
              status: error ? "failed" as const : "completed" as const,
              ...(error ? { error } : {})
            }
          : message
      ))
    }, runtimeRunId);
    return { cache: next, applied: next !== cache };
  }

  const isSelfEvolutionEvent = event.event === "chat.self_evolution.event";
  let next: SessionCache = {
    ...cache,
    runId: isSelfEvolutionEvent ? cache.runId : runtimeRunId,
    streaming: isSelfEvolutionEvent ? cache.streaming : true,
    lastSeq: seq ?? cache.lastSeq
  };

  if (event.event === "chat.run.started") {
    if (typeof event.data.user_message === "string") {
      next = appendUserMessage(
        next,
        runtimeRunId,
        event.data.user_message,
        scheduledSourceFromUnknown(event.data.source) ?? next.pendingScheduledSource
      );
      next = { ...next, pendingScheduledSource: undefined };
    }
    next = appendLifecycleStatus(next, runtimeRunId, event.ttftMs);
  }

  if (event.event === "chat.activity.delta" && typeof event.data.delta === "string") {
    next = removeLifecycleStatus(next, runtimeRunId);
    next = appendActivityDelta(next, runtimeRunId, event.data.delta, seq, event.ttftMs);
  }

  // Compatibility for previously persisted/replayed platform events. New
  // Dashboard adapters emit answer text as `chat.message.delta`; activity
  // remains a distinct execution-record event.
  if (event.event === "chat.message.delta" && typeof event.data.delta === "string") {
    next = removeLifecycleStatus(next, runtimeRunId);
    // A reconnect may replay Hermes' in-flight assistant snapshot before the
    // turn has reached its terminal message. It is execution commentary, not
    // a user-facing answer, and must stay in the active thinking timeline.
    next = event.data.interim === true
      ? appendInterimAssistantDelta(next, runtimeRunId, event.data.delta, seq, event.ttftMs)
      : appendAssistantDelta(next, runtimeRunId, event.data.delta, event.ttftMs);
  }

  if (event.event === "chat.reasoning.delta" && typeof event.data.delta === "string") {
    next = removeLifecycleStatus(next, runtimeRunId);
    next = appendReasoningDelta(next, runtimeRunId, event.data.delta, seq, event.ttftMs);
  }

  if (event.event === "chat.tool.event") {
    next = removeLifecycleStatus(next, runtimeRunId);
    // Hermes can stream a short preamble as assistant text before emitting the
    // tool event. It is still execution commentary, not the terminal answer;
    // move it into the thinking timeline as soon as the tool boundary arrives.
    next = archiveAssistantTextAsActivity(next, runtimeRunId, seq, event.ttftMs);
    next = upsertToolEvent(next, runtimeRunId, event.data, seq, event.ttftMs);
  }

  if (event.event === "capability.error") {
    next = removeLifecycleStatus(next, runtimeRunId);
    next = appendCapabilityError(next, runtimeRunId, event.data);
  }

  if (event.event === "chat.progress.delta") {
    const label = typeof event.data.delta === "string" ? event.data.delta.trim() : "";
    if (label && label.length <= 80) {
      next = removeLifecycleStatus(next, runtimeRunId);
      const state = event.data.state === "waiting_subagents" ? "waiting_subagents" : "running";
      next = { ...next, progress: { runtimeRunId, label, state } };
    }
  }

  if (event.event === "chat.turn.parked") {
    next = next.progress
      ? { ...next, progress: { ...next.progress, state: "waiting_subagents" } }
      : { ...next, progress: { runtimeRunId, label: "正在处理任务", state: "waiting_subagents" } };
  }

  if (event.event === "chat.subagent.event") {
    next = removeLifecycleStatus(next, runtimeRunId);
    next = projectSubagentEvent(next, runtimeRunId, event.data, event.ttftMs);
  }

  if (event.event === "chat.turn.resumed") {
    next = {
      ...next,
      progress: undefined,
      clarifyRunId: undefined,
      messages: next.messages.filter((message) => (
        message.kind !== "background"
        || message.runtimeRunId !== runtimeRunId
        || message.taskId !== "clarify"
      ))
    };
  }

  if (event.event === "chat.clarify.requested") {
    next = upsertBackgroundTask({
      ...next,
      clarifyRunId: runtimeRunId,
      progress: { runtimeRunId, label: "任务已暂停，等待确认", state: "waiting_subagents" }
    }, runtimeRunId, {
      taskId: "clarify",
      status: "running",
      activity: "任务已暂停，等待确认"
    });
  }

  if (event.event === "chat.self_evolution.event") {
    const status = event.data.status === "running" || event.data.status === "completed" || event.data.status === "failed"
      ? event.data.status
      : "completed";
    const activity = typeof event.data.activity === "string" ? event.data.activity : undefined;
    const summary = typeof event.data.summary === "string" ? event.data.summary : undefined;
    next = upsertBackgroundTask(next, runtimeRunId, {
      taskId: "self-evolution",
      status,
      ...(activity ? { activity } : {}),
      ...(summary ? { summary } : {})
    });
  }

  if (event.event === "chat.message.completed") {
    const text = typeof event.data.text === "string" ? event.data.text : "";
    next = removeLifecycleStatus(next, runtimeRunId);
    next = event.data.interim === true
      ? archiveInterimAssistantMessage(
          next,
          runtimeRunId,
          text,
          seq,
          event.ttftMs
        )
      : completeAssistantMessage(next, runtimeRunId, text, event.ttftMs);
  }

  if (event.event === "chat.done") {
    if (hasBlockingCorrection(next, runtimeRunId)) {
      return {
        cache: {
          ...next,
          deferredDone: { runtimeRunId, event }
        },
        applied: true
      };
    }
    next = removeLifecycleStatus(next, runtimeRunId);
    const activeTurn = next.activeTurn
      ? {
          ...next.activeTurn,
          status: next.activeTurn.status === "failed" ? "failed" as const : "completed" as const
        }
      : undefined;
    next = {
      ...next,
      messages: next.messages.map((message) =>
        message.id === activeTurn?.assistantMessageId && message.status !== "failed"
          ? { ...message, status: "completed" as const }
          : message
      ),
      streaming: false,
      runId: null,
      progress: undefined,
      deferredDone: undefined,
      ...(activeTurn ? { activeTurn } : {})
    };
  }

  if (event.event === "chat.error") {
    const errorMessage = typeof event.data.message === "string" ? event.data.message : "生成失败";
    next = {
      ...failChatTurn(removeLifecycleStatus(next, runtimeRunId), errorMessage),
      progress: undefined,
      clarifyRunId: next.clarifyRunId === runtimeRunId ? undefined : next.clarifyRunId
    };
    return {
      cache: next,
      applied: true,
      errorMessage
    };
  }

  const visibleMutation = visibleMutationFor(cache, next, event);
  return {
    cache: next,
    applied: true,
    ...(visibleMutation ? { visibleMutation } : {})
  };
}

export function projectChatAssetEvent(cache: SessionCache, event: ChatRunProjectionEvent): ChatRunProjectionResult {
  if (!event.event.startsWith("chat.asset.")) return { cache, applied: false };
  return { cache, applied: false };
}

export function failChatTurn(cache: SessionCache, error: string): SessionCache {
  const message = error.trim() || "生成失败";
  const activeTurn = cache.activeTurn
    ? { ...cache.activeTurn, status: "failed" as const, error: message }
    : undefined;
  const assistantId = activeTurn?.assistantMessageId;
  let matched = false;
  const messages = cache.messages.map((item) => {
    if (!assistantId || item.id !== assistantId) return item;
    matched = true;
    return {
      ...item,
      status: "failed" as const,
      error: message,
      thinkingPhase: undefined,
      ttftMs: item.content?.trim() ? item.ttftMs : undefined
    };
  });
  if (!matched && activeTurn) {
    messages.push({
      id: activeTurn.assistantMessageId,
      role: "assistant",
      content: "",
      status: "failed",
      error: message
    });
  }
  return {
    ...cache,
    messages,
    streaming: false,
    ...(activeTurn ? { activeTurn } : {})
  };
}

export function cancelChatTurn(cache: SessionCache, runtimeRunId: string): SessionCache {
  const matchingTurn = cache.activeTurn?.runtimeRunId === runtimeRunId
    ? cache.activeTurn
    : undefined;
  const activeTurn = matchingTurn
    ? { ...matchingTurn, status: "completed" as const, error: undefined }
    : cache.activeTurn;
  const targetAssistantId = matchingTurn?.assistantMessageId ?? assistantMessageId(runtimeRunId);
  const messages = cache.messages.filter((message) =>
    message.id !== targetAssistantId
      || Boolean(message.content?.trim())
      || Boolean(message.error?.trim())
  );
  return {
    ...cache,
    messages,
    streaming: cache.runId === runtimeRunId ? false : cache.streaming,
    runId: cache.runId === runtimeRunId ? null : cache.runId,
    progress: cache.progress?.runtimeRunId === runtimeRunId ? undefined : cache.progress,
    ...(activeTurn ? { activeTurn } : {})
  };
}

function appendUserMessage(
  cache: SessionCache,
  runtimeRunId: string,
  content: string,
  scheduledSource?: ScheduledMessageSource
): SessionCache {
  const id = userMessageId(runtimeRunId);
  const existing = cache.messages.some((message) => message.id === id);
  const newUser: ChatBubbleMessage = {
    id,
    role: "user",
    runtimeRunId,
    content,
    ...(scheduledSource ? { scheduledSource } : {})
  };
  const messages = existing
    ? cache.messages
    : [...cache.messages, newUser];
  if (!scheduledSource) return existing ? cache : { ...cache, messages };
  const matchingIndexes = messages.reduce<number[]>((indexes, message, index) => {
    if (message.role === "user" && message.content === content) indexes.push(index);
    return indexes;
  }, []);
  const sourceIndex = scheduledSource
    ? messages.findIndex((message) => message.role === "user"
      && message.scheduledSource?.scheduledTaskId === scheduledSource.scheduledTaskId
      && message.content === content)
    : undefined;
  const fallbackSourceIndex = sourceIndex === -1
    ? matchingIndexes[matchingIndexes.length - 1]
    : sourceIndex;
  if (fallbackSourceIndex === undefined || fallbackSourceIndex === -1) return { ...cache, messages: [...messages, newUser] };
  return {
    ...cache,
    messages: messages.map((message, index) => index === fallbackSourceIndex
      ? { ...message, id, runtimeRunId, scheduledSource }
      : message)
  };
}

function appendLifecycleStatus(
  cache: SessionCache,
  runtimeRunId: string,
  ttftMs?: number
): SessionCache {
  const id = assistantMessageId(runtimeRunId);
  const messages = ensureAssistantMessage(cache.messages, runtimeRunId);
  return {
    ...cache,
    messages: messages.map((message) =>
      message.id === id
        ? {
            ...message,
            thinkingPhase: "started" as const,
            ...(ttftMs !== undefined ? { ttftMs } : {})
          }
        : message
    )
  };
}

function removeLifecycleStatus(cache: SessionCache, runtimeRunId: string): SessionCache {
  const id = assistantMessageId(runtimeRunId);
  let changed = false;
  const messages = cache.messages.map((message): ChatMessage => {
    if (message.id !== id || !message.thinkingPhase) return message;
    changed = true;
    const {
      thinkingPhase: _thinkingPhase,
      ttftMs: _lifecycleTtftMs,
      ...rest
    } = message;
    return rest as ChatMessage;
  });
  if (!changed) return cache;
  return {
    ...cache,
    messages
  };
}

function completeAssistantMessage(cache: SessionCache, runtimeRunId: string, text: string, ttftMs?: number): SessionCache {
  const id = assistantMessageId(runtimeRunId);
  const messages: ChatMessage[] = trimTerminalActivitySuffix(
    ensureAssistantMessage(cache.messages, runtimeRunId),
    runtimeRunId,
    text
  ).map((message): ChatMessage =>
    message.id === id
      ? {
          id,
          role: "assistant",
          runtimeRunId,
          content: stripRuntimeMediaMarkers(text),
          status: "completed",
          ...(message.kind === undefined || message.kind === "message"
            ? (message.attachments?.length ? { attachments: message.attachments } : {})
            : {}),
          ...(ttftMs !== undefined ? { ttftMs } : {})
        }
      : message
  );
  return { ...cache, messages };
}

function archiveInterimAssistantMessage(
  cache: SessionCache,
  runtimeRunId: string,
  text: string,
  seq?: number,
  ttftMs?: number
): SessionCache {
  const assistantId = assistantMessageId(runtimeRunId);
  const activityId = `activity-${runtimeRunId}-interim-${seq ?? "latest"}`;
  const prepared = ensureAssistantMessage(cache.messages, runtimeRunId);
  const assistant = prepared.find(
    (message): message is ChatBubbleMessage => message.id === assistantId
      && message.kind !== "reasoning"
      && message.kind !== "activity"
      && message.kind !== "tool"
      && message.kind !== "background"
  );
  const authoritativeText = text || assistant?.content || "";
  let messages: ChatMessage[] = prepared.map((message): ChatMessage => {
    if (message.id !== assistantId) return message;
    const {
      error: _error,
      thinkingPhase: _thinkingPhase,
      ttftMs: _ttftMs,
      ...rest
    } = message;
    return {
      ...rest,
      content: "",
      status: "streaming"
    } as ChatBubbleMessage;
  });
  if (!authoritativeText.trim()) return { ...cache, messages };

  const activity: ActivityMessage = {
    id: activityId,
    kind: "activity",
    role: "assistant",
    runtimeRunId,
    text: authoritativeText,
    status: "completed",
    ...(ttftMs !== undefined ? { ttftMs } : {})
  };
  const existing = messages.findIndex((message) => message.id === activityId);
  messages = existing >= 0
    ? messages.map((message) => message.id === activityId ? activity : message)
    : insertBeforeAssistant(messages, runtimeRunId, activity);
  return { ...cache, messages };
}

function trimTerminalActivitySuffix(messages: ChatMessage[], runtimeRunId: string, terminalText: string): ChatMessage[] {
  if (!terminalText) return messages;
  const assistantIndex = messages.findIndex((message) => isAssistantBubble(message, runtimeRunId));
  if (assistantIndex <= 0) return messages;

  return messages.flatMap((message, index) => {
    if (index >= assistantIndex || (message.kind !== "activity" && message.kind !== "reasoning")) {
      return [message];
    }
    if (!message.text.endsWith(terminalText)) return [message];
    const remainingText = message.text.slice(0, -terminalText.length);
    return remainingText ? [{ ...message, text: remainingText }] : [];
  });
}

function appendAssistantDelta(cache: SessionCache, runtimeRunId: string, delta: string, ttftMs?: number): SessionCache {
  const id = assistantMessageId(runtimeRunId);
  const messages = ensureAssistantMessage(cache.messages, runtimeRunId).map((message) =>
    message.id === id
      ? {
          ...message,
          content: stripRuntimeMediaMarkers((message.content ?? "") + delta),
          ...(ttftMs !== undefined ? { ttftMs } : {})
        }
      : message
  );
  return { ...cache, messages };
}

function ensureAssistantMessage(messages: ChatMessage[], runtimeRunId: string): ChatMessage[] {
  const id = assistantMessageId(runtimeRunId);
  return messages.some((message) => message.id === id)
    ? messages
    : [...messages, { id, role: "assistant", runtimeRunId, content: "", status: "streaming" }];
}

function isAssistantBubble(message: ChatMessage, runtimeRunId: string): message is ChatBubbleMessage {
  return message.id === assistantMessageId(runtimeRunId)
    && message.kind !== "reasoning"
    && message.kind !== "activity"
    && message.kind !== "tool"
    && message.kind !== "background";
}

function insertBeforeAssistant(messages: ChatMessage[], runtimeRunId: string, message: ChatMessage): ChatMessage[] {
  const assistantIndex = messages.findIndex((item) => isAssistantBubble(item, runtimeRunId));
  if (assistantIndex === -1) return [...messages, message];
  return [...messages.slice(0, assistantIndex), message, ...messages.slice(assistantIndex)];
}

function appendReasoningDelta(
  cache: SessionCache,
  runtimeRunId: string,
  delta: string,
  seq?: number,
  ttftMs?: number
): SessionCache {
  if (!delta.trim()) return cache;
  const messages = ensureAssistantMessage(cache.messages, runtimeRunId);
  const assistantIndex = messages.findIndex((item) => isAssistantBubble(item, runtimeRunId));
  const insertAt = assistantIndex === -1 ? messages.length : assistantIndex;
  const previous = messages[insertAt - 1];
  if (
    previous?.kind === "reasoning"
    && (previous.runtimeRunId === runtimeRunId || !previous.runtimeRunId)
  ) {
    return {
      ...cache,
      messages: messages.map((message) =>
        message.id === previous.id
          ? {
              ...previous,
              runtimeRunId,
              text: previous.text + delta,
              ...(ttftMs !== undefined
                ? { ttftMs }
                : previous.ttftMs !== undefined
                  ? { ttftMs: previous.ttftMs }
                  : {})
            }
          : message
      )
    };
  }
  const reasoning: ReasoningMessage = {
    id: `reasoning-${runtimeRunId}-${seq ?? messages.length}`,
    kind: "reasoning",
    role: "assistant",
    runtimeRunId,
    text: delta,
    status: "streaming",
    ...(ttftMs !== undefined ? { ttftMs } : {})
  };
  return { ...cache, messages: insertBeforeAssistant(messages, runtimeRunId, reasoning) };
}

function appendActivityDelta(
  cache: SessionCache,
  runtimeRunId: string,
  delta: string,
  seq?: number,
  ttftMs?: number
): SessionCache {
  if (!delta.trim()) return cache;
  const messages = ensureAssistantMessage(cache.messages, runtimeRunId);
  const assistantIndex = messages.findIndex((item) => isAssistantBubble(item, runtimeRunId));
  const insertAt = assistantIndex === -1 ? messages.length : assistantIndex;
  const previous = messages[insertAt - 1];
  if (previous?.kind === "activity" && previous.runtimeRunId === runtimeRunId) {
    return {
      ...cache,
      messages: messages.map((message) =>
        message.id === previous.id
          ? {
              ...previous,
              text: previous.text + delta,
              ...(ttftMs !== undefined
                ? { ttftMs }
                : previous.ttftMs !== undefined
                  ? { ttftMs: previous.ttftMs }
                  : {})
            }
          : message
      )
    };
  }
  const activity: ActivityMessage = {
    id: `activity-${runtimeRunId}-${seq ?? messages.length}`,
    kind: "activity",
    role: "assistant",
    runtimeRunId,
    text: delta,
    status: "streaming",
    ...(ttftMs !== undefined ? { ttftMs } : {})
  };
  return { ...cache, messages: insertBeforeAssistant(messages, runtimeRunId, activity) };
}

function appendInterimAssistantDelta(
  cache: SessionCache,
  runtimeRunId: string,
  delta: string,
  seq?: number,
  ttftMs?: number
): SessionCache {
  const assistantId = assistantMessageId(runtimeRunId);
  const assistant = cache.messages.find(
    (message): message is ChatBubbleMessage => isAssistantBubble(message, runtimeRunId)
  );
  const alreadyBuffered = assistant?.content ?? "";
  const cleared = alreadyBuffered
    ? {
        ...cache,
        messages: cache.messages.map((message) => message.id === assistantId
          ? { ...message, content: "" }
          : message)
      }
    : cache;
  return appendActivityDelta(
    cleared,
    runtimeRunId,
    `${alreadyBuffered}${delta}`,
    seq,
    ttftMs
  );
}

function archiveAssistantTextAsActivity(
  cache: SessionCache,
  runtimeRunId: string,
  seq?: number,
  ttftMs?: number
): SessionCache {
  const assistantId = assistantMessageId(runtimeRunId);
  const assistant = cache.messages.find(
    (message): message is ChatBubbleMessage => isAssistantBubble(message, runtimeRunId)
  );
  const text = assistant?.content ?? "";
  if (!text.trim()) return cache;
  const cleared: SessionCache = {
    ...cache,
    messages: cache.messages.map((message): ChatMessage => (
      message.id === assistantId && isAssistantBubble(message, runtimeRunId)
        ? { ...message, content: "", status: "streaming" as const }
        : message
    ))
  };
  return appendActivityDelta(cleared, runtimeRunId, text, seq, ttftMs ?? assistant?.ttftMs);
}

function upsertToolEvent(
  cache: SessionCache,
  runtimeRunId: string,
  data: Record<string, unknown>,
  seq?: number,
  ttftMs?: number
): SessionCache {
  const name = typeof data.name === "string" && data.name
    ? data.name
    : typeof data.toolName === "string" && data.toolName
      ? data.toolName
      : "tool";
  const providerCallId = typeof data.callId === "string" && data.callId ? data.callId : undefined;
  const hasStableCallId = typeof data.hasStableCallId === "boolean" ? data.hasStableCallId : Boolean(providerCallId);
  const status = data.status === "completed" || data.status === "failed" ? data.status : "running";
  const toolMessages = cache.messages.filter((message): message is ToolMessage => message.kind === "tool" && message.runtimeRunId === runtimeRunId);
  const unstableRunningMatches = toolMessages.filter((message) =>
    message.hasStableCallId === false && message.name === name && message.status === "running"
  );
  const existing = hasStableCallId
    ? toolMessages.find((message) => message.callId === providerCallId)
    : status === "running"
      ? unstableRunningMatches[unstableRunningMatches.length - 1]
      : unstableRunningMatches[0];
  const callId = existing?.callId
    ?? (hasStableCallId && providerCallId
      ? providerCallId
      : `unstable:${runtimeRunId}:${name}:${seq ?? cache.messages.length}`);
  const incomingPreview = typeof data.preview === "string" && data.preview ? data.preview : undefined;
  const incomingInput = typeof data.input === "string" && data.input ? data.input : undefined;
  const incomingResult = typeof data.result === "string" && data.result ? data.result : undefined;
  const mergedStatus = existing && existing.status !== "running" && status === "running" ? existing.status : status;
  const tool: ToolMessage = {
    ...existing,
    id: existing?.id ?? `tool-${runtimeRunId}-${callId}`,
    kind: "tool",
    role: "tool",
    runtimeRunId,
    callId,
    ...(providerCallId ? { providerCallId } : existing?.providerCallId ? { providerCallId: existing.providerCallId } : {}),
    hasStableCallId,
    name,
    status: mergedStatus,
    ...(ttftMs !== undefined ? { ttftMs } : existing?.ttftMs !== undefined ? { ttftMs: existing.ttftMs } : {}),
    ...(incomingInput ? { input: incomingInput } : existing?.input ? { input: existing.input } : {}),
    ...(incomingPreview ? { preview: incomingPreview } : existing?.preview ? { preview: existing.preview } : {}),
    ...(incomingResult ? { result: incomingResult } : existing?.result ? { result: existing.result } : {})
  };
  if (existing) {
    return {
      ...cache,
      messages: cache.messages.map((message) => message.id === existing.id ? tool : message)
    };
  }
  const messages = ensureAssistantMessage(cache.messages, runtimeRunId);
  return { ...cache, messages: insertBeforeAssistant(messages, runtimeRunId, tool) };
}

function upsertBackgroundTask(
  cache: SessionCache,
  runtimeRunId: string,
  task: Omit<BackgroundTaskMessage, "id" | "kind" | "role">
): SessionCache {
  const existing = cache.messages.find(
    (message): message is BackgroundTaskMessage => message.kind === "background" && message.runtimeRunId === runtimeRunId && message.taskId === task.taskId
  );
  const background: BackgroundTaskMessage = {
    ...existing,
    ...task,
    id: existing?.id ?? `background-${runtimeRunId}-${task.taskId}`,
    kind: "background",
    role: "system",
    runtimeRunId
  };
  if (existing) {
    return {
      ...cache,
      messages: cache.messages.map((message) => message.id === existing.id ? background : message)
    };
  }
  const messages = ensureAssistantMessage(cache.messages, runtimeRunId);
  return { ...cache, messages: insertBeforeAssistant(messages, runtimeRunId, background) };
}

function upsertTransportNotice(
  cache: SessionCache,
  runtimeRunId: string,
  clientMessageId?: string
): SessionCache {
  const existing = cache.messages.find(
    (message): message is BackgroundTaskMessage => (
      message.kind === "background"
      && message.taskId === "transport"
      && (
        message.runtimeRunId === runtimeRunId
        || (clientMessageId !== undefined && message.runtimeRunId === clientMessageId)
      )
    )
  );
  const task = {
    taskId: "transport",
    status: "running" as const,
    activity: "连接中断，正在恢复任务…"
  };
  if (existing) {
    return {
      ...cache,
      messages: cache.messages.map((message) => message.id === existing.id
        ? { ...existing, ...task }
        : message)
    };
  }
  if (
    cache.runId === runtimeRunId
    || cache.messages.some((message) => message.runtimeRunId === runtimeRunId)
  ) {
    return upsertBackgroundTask(cache, runtimeRunId, task);
  }
  if (
    cache.activeTurn
    && (
      cache.activeTurn.turnId === runtimeRunId
      || (clientMessageId !== undefined && cache.activeTurn.turnId === clientMessageId)
    )
  ) {
    const background: BackgroundTaskMessage = {
      id: `background-${runtimeRunId}-transport`,
      kind: "background",
      role: "system",
      runtimeRunId,
      ...task
    };
    const assistantIndex = cache.messages.findIndex((message) => (
      message.id === cache.activeTurn?.assistantMessageId
    ));
    const messages = assistantIndex >= 0
      ? [
          ...cache.messages.slice(0, assistantIndex),
          background,
          ...cache.messages.slice(assistantIndex)
        ]
      : [...cache.messages, background];
    return { ...cache, messages };
  }
  return cache;
}

function removeTransportNotice(cache: SessionCache): SessionCache {
  return {
    ...cache,
    messages: cache.messages.filter(
      (message) => message.kind !== "background" || message.taskId !== "transport"
    )
  };
}

function projectSubagentEvent(
  cache: SessionCache,
  runtimeRunId: string,
  data: Record<string, unknown>,
  ttftMs?: number
): SessionCache {
  const taskId = stableSubagentTaskId(data);
  if (!taskId) return cache;
  const eventType = typeof data.type === "string" ? data.type : "";
  const rawStatus = typeof data.status === "string" ? data.status.toLowerCase() : "";
  const terminalStatusByType: Record<string, BackgroundTaskMessage["status"]> = {
    "subagent.complete": "completed",
    "subagent.failed": "failed",
    "subagent.error": "failed",
    "subagent.interrupted": "interrupted",
    "subagent.canceled": "interrupted"
  };
  const status: BackgroundTaskMessage["status"] = ["failed", "error", "timeout", "failure"].includes(rawStatus)
    ? "failed"
    : ["interrupted", "canceled", "cancelled"].includes(rawStatus)
      ? "interrupted"
      : rawStatus === "completed" || rawStatus === "queued" || rawStatus === "running"
        ? rawStatus
        : terminalStatusByType[eventType] ?? "running";
  const withoutFallback = {
    ...cache,
    messages: cache.messages.filter((message) => message.kind !== "background" || message.taskId !== "parked")
  };
  const current = withoutFallback.messages.find(
    (message): message is BackgroundTaskMessage => message.kind === "background" && message.taskId === taskId
  );
  const goal = typeof data.goal === "string" && data.goal ? data.goal : current?.goal;
  const activity = typeof data.text === "string" && data.text
    ? data.text
    : typeof data.tool_preview === "string" && data.tool_preview ? data.tool_preview : current?.activity;
  const summary = typeof data.summary === "string" && data.summary ? data.summary : current?.summary;
  return upsertBackgroundTask(withoutFallback, runtimeRunId, {
    taskId,
    status,
    ...(goal ? { goal } : {}),
    ...(activity ? { activity } : {}),
    ...(summary ? { summary } : {}),
    ...(ttftMs !== undefined ? { ttftMs } : {})
  });
}
