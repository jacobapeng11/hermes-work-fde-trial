import type {
  ChatMessageAttachment,
  ActiveTurn,
  ActivityMessage,
  BackgroundTaskMessage,
  ChatMessage,
  ChatCorrectionStatus,
  ChatRole,
  ReasoningMessage,
  ToolMessage
} from "./chatRunProjection";
import type { ChatFileSummary } from "@hermes-cloud/schemas";
import { attachGeneratedImageArtifacts } from "./chat_image_output/chatImageArtifacts";
import {
  correctionStatusHint,
  scheduledSourceFromUnknown
} from "./chatRunProjection";

export const textFrom = (value: unknown): string => {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value
      .map((item) => textFrom(item))
      .filter(Boolean)
      .join("\n");
  }
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return textFrom(record.text ?? record.content ?? "");
  }
  return "";
};

function reasoningFrom(item: Record<string, unknown>): string {
  const explicit =
    item.reasoningContent ??
    item.reasoning_content ??
    item.reasoning ??
    item.thinkingText ??
    item.thinking_text;
  return textFrom(explicit);
}

function stripReasoningPrefix(content: string, reasoning: string): string {
  const visible = content.trimStart();
  const trace = reasoning.trim();
  if (!trace || !visible.startsWith(trace)) return content;
  const remainder = visible.slice(trace.length);
  return remainder.replace(/^\s+/, "");
}

function isReasoningItem(item: Record<string, unknown>): boolean {
  return [item.kind, item.type, item.event].some((value) =>
    typeof value === "string" && value.toLowerCase().includes("reasoning")
  );
}

const PLATFORM_PROMPT_METADATA_RE = /<platform_(?:attachments|session_contract)\b/i;
const PLATFORM_ATTACHMENTS_SUFFIX_RE = /\n\n[ \t]*<platform_attachments>\r?\nThe user attached these files(?: from WeCom)?\. Read them from the current (?:session )?workspace when needed\.\r?\n[\s\S]*?\r?\n[ \t]*<\/platform_attachments>\s*$/;
const PLATFORM_SESSION_CONTRACT_SUFFIX_RE = /\n\n<platform_session_contract version="2" digest="[0-9a-f]{64}">[\s\S]*?<\/platform_session_contract>\s*$/i;
const HERMES_IMAGE_DIRECTIVE_RE = /^@image:.*$/gm;
const RUNTIME_MEDIA_MARKER_RE = /^[^\r\n]*\/data\/workspace\/sessions\/[^\r\n]+$/gm;
const FAILED_TOOL_STATUSES = new Set([
  "failed",
  "failure",
  "error",
  "errored",
  "timeout",
  "timed_out",
  "canceled",
  "cancelled",
  "interrupted"
]);
const RUNNING_TOOL_STATUSES = new Set([
  "running",
  "pending",
  "queued",
  "in_progress"
]);
const TOOL_FAILURE_KEYS = [
  "error",
  "errorMessage",
  "error_message",
  "exception",
  "failure",
  "failureReason",
  "failure_reason"
] as const;

function stripPlatformPromptMetadata(text: string): string {
  return text
    .replace(PLATFORM_SESSION_CONTRACT_SUFFIX_RE, "")
    .replace(PLATFORM_ATTACHMENTS_SUFFIX_RE, "")
    .replace(HERMES_IMAGE_DIRECTIVE_RE, "")
    .replace(RUNTIME_MEDIA_MARKER_RE, "")
    .trim();
}

function isPromptEchoReasoning(text: string, userPrompts: Set<string>): boolean {
  if (!PLATFORM_PROMPT_METADATA_RE.test(text)) return false;
  const normalized = stripPlatformPromptMetadata(text);
  return normalized.length > 0 && userPrompts.has(normalized);
}

function hasToolCalls(item: Record<string, unknown>): boolean {
  const raw = item.toolCalls ?? item.tool_calls;
  if (Array.isArray(raw)) return raw.length > 0;
  if (typeof raw === "string" && raw.trim()) {
    try {
      const parsed: unknown = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.length > 0 : Boolean(parsed);
    } catch {
      // A non-empty legacy payload is still an execution intermediate.
      return true;
    }
  }
  const finishReason = item.finishReason ?? item.finish_reason;
  return typeof finishReason === "string" && finishReason.toLowerCase() === "tool_calls";
}

const COMPACTION_PREFIXES = [
  "[CONTEXT COMPACTION — REFERENCE ONLY]",
  "[CONTEXT COMPACTION - REFERENCE ONLY]",
  "[CONTEXT COMPACTION]",
  "[CONTEXT SUMMARY]:"
] as const;
const COMPACTION_END_MARKER = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---";
const MERGED_SUMMARY_DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]";
const MERGED_PRIOR_CONTEXT_HEADER = "[PRIOR CONTEXT — for reference only; not a new message]";
const COMPACTION_TEXT_KEYS = [
  "content",
  "text",
  "reasoning_content",
  "reasoningContent",
  "reasoning",
  "thinkingText",
  "thinking_text"
] as const;
const NOT_COMPACTION = Symbol("not-compaction");

function normalizeCompactionItem(item: Record<string, unknown>): Record<string, unknown> | null | typeof NOT_COMPACTION {
  const compressed = ["_compressed_summary", "compressed_summary", "is_compressed_summary"]
    .some((key) => item[key] === true);
  const textFields = COMPACTION_TEXT_KEYS.filter((key) => typeof item[key] === "string")
    .map((key) => [key, item[key] as string] as const);
  if (textFields.length === 0) return compressed ? null : NOT_COMPACTION;
  const compactionField = textFields.find(([, value]) => (
    COMPACTION_PREFIXES.some((prefix) => value.trimStart().startsWith(prefix))
    || value.includes(MERGED_SUMMARY_DELIMITER)
  ))?.[0];
  if (!compressed && !compactionField) return NOT_COMPACTION;
  const content = (textFields.find(([key]) => key === (compactionField ?? "content"))?.[1] ?? "").trimStart();
  const delimiterAt = content.indexOf(MERGED_SUMMARY_DELIMITER);
  if (delimiterAt >= 0) {
    let prior = content.slice(0, delimiterAt).trim();
    if (prior.startsWith(MERGED_PRIOR_CONTEXT_HEADER)) {
      prior = prior.slice(MERGED_PRIOR_CONTEXT_HEADER.length).trimStart();
    }
    if (prior) {
      const normalized = Object.fromEntries(
        Object.entries(item).filter(([key]) => !key.startsWith("_"))
      );
      if (compactionField) delete normalized[compactionField];
      if (compactionField === "content" || "content" in normalized) normalized.content = prior;
      else normalized.text = prior;
      delete normalized.compressed_summary;
      delete normalized.is_compressed_summary;
      return normalized;
    }
  }
  const markerAt = content.indexOf(COMPACTION_END_MARKER);
  if (markerAt < 0) return null;
  const remainder = content.slice(markerAt + COMPACTION_END_MARKER.length).trimStart();
  if (!remainder) return null;
  const normalized = Object.fromEntries(
    Object.entries(item).filter(([key]) => !key.startsWith("_"))
  );
  if (compactionField) delete normalized[compactionField];
  if (compactionField === "content" || "content" in normalized) normalized.content = remainder;
  else normalized.text = remainder;
  delete normalized.compressed_summary;
  delete normalized.is_compressed_summary;
  return normalized;
}

function historyTool(item: Record<string, unknown>, index: number): ToolMessage {
  const name = textFrom(item.name ?? item.toolName ?? item.tool_name ?? item.tool) || "tool";
  const providerCallId = textFrom(item.callId ?? item.toolCallId ?? item.tool_call_id);
  const hasStableCallId = item.hasStableCallId === true || item.has_stable_call_id === true;
  const callId = hasStableCallId && providerCallId ? providerCallId : `history-${index}`;
  const rawStatus = textFrom(item.status).trim().toLowerCase();
  const hasFailureMarker = TOOL_FAILURE_KEYS.some((key) => (
    item[key] !== undefined
    && item[key] !== null
    && textFrom(item[key]) !== ""
  )) || (
    item.result !== null
    && typeof item.result === "object"
    && TOOL_FAILURE_KEYS.some((key) => {
      const result = item.result as Record<string, unknown>;
      return result[key] !== undefined
        && result[key] !== null
        && textFrom(result[key]) !== "";
    })
  );
  const status: ToolMessage["status"] = FAILED_TOOL_STATUSES.has(rawStatus) || hasFailureMarker
    ? "failed"
    : RUNNING_TOOL_STATUSES.has(rawStatus)
      ? "running"
      : "completed";
  return {
    id: `history-tool-${index}-${callId}`,
    kind: "tool",
    role: "tool",
    callId,
    ...(providerCallId ? { providerCallId } : {}),
    hasStableCallId,
    name,
    status,
    ...(textFrom(item.input) ? { input: textFrom(item.input) } : {}),
    ...(textFrom(item.preview) ? { preview: textFrom(item.preview) } : {}),
    ...(textFrom(item.result ?? item.content) ? { result: textFrom(item.result ?? item.content) } : {})
  };
}

const BACKGROUND_STATUSES = new Set<BackgroundTaskMessage["status"]>([
  "queued",
  "running",
  "completed",
  "failed",
  "interrupted"
]);
const SYNTHETIC_BACKGROUND_TASK_ID = /^task-\d+$/;

function historyBackground(item: Record<string, unknown>, index: number): BackgroundTaskMessage | null {
  if (item.role !== "system") return null;
  const taskId = textFrom(item.taskId ?? item.task_id);
  const status = textFrom(item.status) as BackgroundTaskMessage["status"];
  if (!taskId || SYNTHETIC_BACKGROUND_TASK_ID.test(taskId) || !BACKGROUND_STATUSES.has(status)) return null;
  const summary = textFrom(item.summary);
  const goal = textFrom(item.goal);
  const activity = textFrom(item.activity);
  return {
    id: `history-background-${index}-${taskId}`,
    kind: "background",
    role: "system",
    taskId,
    status,
    ...(summary ? { summary } : {}),
    ...(goal ? { goal } : {}),
    ...(activity ? { activity } : {})
  };
}

function correctionStatusFromHistory(
  item: Record<string, unknown>
): ChatCorrectionStatus | undefined {
  const value = textFrom(
    item.correctionStatus
      ?? item.correction_status
      ?? item.followupStatus
      ?? item.followup_status
      ?? item.redirectState
      ?? item.redirect_state
  ).toUpperCase();
  if (["SENDING"].includes(value)) return "sending";
  if ([
    "RECEIVED",
    "PENDING",
    "WAITING_AGENT_READY",
  ].includes(value)) return "received";
  if ([
    "APPLYING",
    "WAITING_REDIRECT_BOUNDARY",
    "PREPARED",
    "DISPATCH_INTENT",
    "ACCEPTED",
    "RPC_ACKNOWLEDGED",
    "ISOLATING_UNCERTAIN"
  ].includes(value)) return "applying";
  if (["APPLIED", "CONSUMED", "CANONICAL_CONSUMED"].includes(value)) return "applied";
  if (["CONTINUED", "PROMOTED", "PROMOTED_TO_RUN"].includes(value)) return "continued";
  if (["FAILED", "UNCERTAIN", "SUBMISSION_UNCERTAIN"].includes(value)) return "failed";
  if (["CANCELED", "CANCELLED", "CANCELED_WITH_PARENT"].includes(value)) {
    return "failed";
  }
  return undefined;
}

function attachmentsFromHistory(
  item: Record<string, unknown>,
  files: ChatFileSummary[]
): ChatMessageAttachment[] {
  const raw = item.attachments ?? item.source;
  if (!raw || typeof raw !== "object") return [];
  const record = raw as Record<string, unknown>;
  if (Array.isArray(record.attachments)) {
    return record.attachments.flatMap((value): ChatMessageAttachment[] => {
      if (!value || typeof value !== "object") return [];
      const attachment = value as Record<string, unknown>;
      const rawId = attachment.attachmentId ?? attachment.fileId;
      const fileId = Number(rawId);
      const displayName = typeof attachment.displayName === "string"
        ? attachment.displayName
        : "";
      if (!Number.isInteger(fileId) || fileId <= 0) return [];
      return [{
        fileId,
        displayName: displayName || `附件 ${fileId}`,
        ...(typeof attachment.mimeType === "string"
          ? { mimeType: attachment.mimeType }
          : {}),
        ...(typeof attachment.sourceType === "string"
          ? { sourceType: attachment.sourceType }
          : {})
      }];
    });
  }
  const rawIds = record.attachmentIds ?? record.attachment_file_ids;
  if (!Array.isArray(rawIds)) return [];
  const filesById = new Map(files.map((file) => [file.fileId, file]));
  return rawIds.flatMap((value): ChatMessageAttachment[] => {
    const fileId = Number(value);
    if (!Number.isInteger(fileId) || fileId <= 0) return [];
    const file = filesById.get(fileId);
    return [{
      fileId,
      displayName: file?.displayName ?? `附件 ${fileId}`,
      ...(file?.mimeType ? { mimeType: file.mimeType } : {}),
      ...(file ? { sourceType: file.sourceType } : {})
    }];
  });
}

export function historyMessages(
  items: Record<string, unknown>[],
  limit?: number | null,
  files: ChatFileSummary[] = []
): ChatMessage[] {
  const bounded = typeof limit === "number" && limit >= 0 ? items.slice(0, limit) : items;
  const userPrompts = new Set(
    bounded
      .filter((item) => String(item.role ?? "") === "user")
      .map((item) => stripPlatformPromptMetadata(textFrom(item.content ?? item.message ?? item.text)))
      .filter(Boolean)
  );
  const projected = bounded.flatMap((item, index): ChatMessage[] => {
    const compaction = normalizeCompactionItem(item);
    if (compaction === null) return [];
    if (compaction !== NOT_COMPACTION) item = compaction;
    const rawReasoning = reasoningFrom(item);
    const reasoning = rawReasoning ? stripPlatformPromptMetadata(rawReasoning) : "";
    if (isReasoningItem(item)) {
      const rawText = reasoning || textFrom(item.text ?? item.content);
      if (isPromptEchoReasoning(rawReasoning || rawText, userPrompts)) return [];
      const text = stripPlatformPromptMetadata(rawText);
      return text
        ? [{ id: `history-reasoning-${index}`, kind: "reasoning", role: "assistant", text } satisfies ReasoningMessage]
        : [];
    }

    if (item.kind === "activity") {
      const text = textFrom(item.text ?? item.content);
      return text
        ? [{ id: `history-activity-${index}`, kind: "activity", role: "assistant", text } satisfies ActivityMessage]
        : [];
    }

    const rawRole = String(item.role ?? "assistant");
    if (rawRole === "tool") return [historyTool(item, index)];
    if (item.kind === "background") {
      const background = historyBackground(item, index);
      return background ? [background] : [];
    }

    const messages: ChatMessage[] = [];
    if (reasoning && !isPromptEchoReasoning(rawReasoning, userPrompts)) {
      messages.push({
        id: `history-reasoning-${index}`,
        kind: "reasoning",
        role: "assistant",
        text: reasoning
      });
    }
    const rawContent = textFrom(item.content ?? item.message ?? item.text);
    // Some Hermes history adapters copy the private reasoning snapshot into
    // the assistant content as well as exposing `reasoning_content`. Keep the
    // snapshot in the execution fold and remove only an exact leading copy;
    // never heuristically rewrite an unrelated final answer.
    const strippedRawContent = stripPlatformPromptMetadata(rawContent);
    const content = reasoning
      ? stripReasoningPrefix(strippedRawContent, reasoning)
      : strippedRawContent;
    // Hermes persists an assistant row before each tool dispatch. Some models
    // include a short narrative in that row while others leave it empty. It
    // is execution trace, never the user-facing final answer; keeping it as a
    // timeline activity prevents model-dependent leakage into answer bubbles.
    if (rawRole === "assistant" && hasToolCalls(item)) {
      if (content) {
        messages.push({
          id: `history-activity-${index}`,
          kind: "activity",
          role: "assistant",
          text: content
        } satisfies ActivityMessage);
      }
      return messages;
    }
    if (content) {
      const role = ["user", "assistant", "system"].includes(rawRole) ? rawRole as ChatRole : "assistant";
      const runtimeRunId = textFrom(
        item.runtimeRunId ?? item.runtime_run_id
      );
      const attachments = role === "user"
        ? attachmentsFromHistory(item, files)
        : [];
      const scheduledSource = role === "user" ? scheduledSourceFromUnknown(item.source) : undefined;
      const clientMessageId = role === "user"
        ? textFrom(item.clientMessageId ?? item.client_message_id)
        : "";
      const redirectId = role === "user"
        ? textFrom(
          item.followupId
          ?? item.followup_id
          ?? item.redirectId
          ?? item.redirect_id
        )
        : "";
      const parentRunId = role === "user"
        ? textFrom(item.parentRunId ?? item.parent_run_id)
        : "";
      const promotedRunId = role === "user"
        ? textFrom(
          item.continuationRunId
          ?? item.continuation_run_id
          ?? item.promotedRunId
          ?? item.promoted_run_id
        )
        : "";
      const correctionStatus = clientMessageId
        ? correctionStatusFromHistory(item)
        : undefined;
      messages.push({
        id: clientMessageId
          ? `correction-${clientMessageId}`
          : `history-${index}`,
        role,
        content,
        ...(runtimeRunId ? { runtimeRunId } : {}),
        ...(attachments.length > 0 ? { attachments } : {}),
        ...(scheduledSource ? { scheduledSource } : {}),
        ...(clientMessageId ? { clientMessageId } : {}),
        ...(redirectId ? { redirectId } : {}),
        ...(parentRunId ? { parentRunId } : {}),
        ...(promotedRunId ? { promotedRunId } : {}),
        ...(correctionStatus ? {
          correctionStatus,
          submissionHint: correctionStatusHint(correctionStatus)
        } : {})
      });
    }
    return messages;
  });
  return attachGeneratedImageArtifacts(projected, files);
}

const normalizedText = (value: string | undefined) => (value ?? "").replace(/\s+/g, " ").trim();

function messageText(message: ChatMessage): string {
  if (message.kind === "reasoning") return message.text;
  if (message.kind === "activity") return message.text;
  if (message.kind === "tool") return message.result ?? message.preview ?? message.name;
  if (message.kind === "background") return message.summary ?? message.activity ?? message.goal ?? "";
  return message.content ?? "";
}

function exactMessageKey(message: ChatMessage): string | null {
  if (
    message.role === "user"
    && message.clientMessageId
  ) {
    return `ledger\u0000${message.clientMessageId}`;
  }
  const text = normalizedText(messageText(message));
  return text ? `${message.role}\u0000${message.kind ?? "message"}\u0000${text}` : null;
}

function userReconcileKey(message: ChatMessage): string {
  if (
    message.role === "user"
    && message.clientMessageId
  ) {
    return `ledger:${message.clientMessageId}`;
  }
  return `legacy:${normalizedText(message.content)}`;
}

interface ReasoningSegment {
  boundary: number;
  ordinal: number;
  selected: ReasoningMessage;
  members: ReasoningMessage[];
  selectedIndex: number;
}

function reasoningTextsSharePrefix(left: ReasoningMessage, right: ReasoningMessage): boolean {
  const leftText = left.text.replace(/\s+/g, "");
  const rightText = right.text.replace(/\s+/g, "");
  return Boolean(
    leftText
    && rightText
    && (leftText.startsWith(rightText) || rightText.startsWith(leftText))
  );
}

function reasoningSegments(
  messages: ChatMessage[],
  start: number,
  end: number
): ReasoningSegment[] {
  const segments: ReasoningSegment[] = [];
  let boundary = 0;
  let ordinal = 0;
  for (let index = start; index < end; index += 1) {
    const message = messages[index];
    if (message.kind === "tool" || message.kind === "background") {
      boundary += 1;
      ordinal = 0;
      continue;
    }
    if (message.kind !== "reasoning") continue;
    const previous = segments[segments.length - 1];
    if (
      previous
      && previous.boundary === boundary
      && reasoningTextsSharePrefix(previous.selected, message)
    ) {
      previous.selected = message;
      previous.selectedIndex = index;
      previous.members.push(message);
      continue;
    }
    segments.push({
      boundary,
      ordinal,
      selected: message,
      members: [message],
      selectedIndex: index
    });
    ordinal += 1;
  }
  return segments;
}

function matchReasoningSegments(
  remoteSegments: ReasoningSegment[],
  localSegments: ReasoningSegment[]
): Map<number, ReasoningSegment> {
  const matches = new Map<number, ReasoningSegment>();
  for (const remoteSegment of remoteSegments) {
    const localSegment = localSegments.find((candidate) =>
      candidate.boundary === remoteSegment.boundary
      && candidate.ordinal === remoteSegment.ordinal
      && reasoningTextsSharePrefix(remoteSegment.selected, candidate.selected)
    );
    if (localSegment) matches.set(remoteSegment.selectedIndex, localSegment);
  }
  return matches;
}

function matchedReasoningIds(matches: Map<number, ReasoningSegment>): Set<string> {
  return new Set(
    [...matches.values()].flatMap((segment) =>
      segment.members.map((message) => message.id)
    )
  );
}

function carryExactLocalMetadata(remote: ChatMessage[], local: ChatMessage[]): ChatMessage[] {
  const localByKey = new Map<string, ChatMessage[]>();
  for (const message of local) {
    const key = exactMessageKey(message);
    if (!key) continue;
    const matches = localByKey.get(key) ?? [];
    matches.push(message);
    localByKey.set(key, matches);
  }
  return remote.map((message) => {
    const key = exactMessageKey(message);
    const localMessage = key ? localByKey.get(key)?.shift() : undefined;
    if (!localMessage) return message;
    const remoteScheduledSource = (
      "scheduledSource" in message ? message.scheduledSource : undefined
    );
    const localScheduledSource = (
      "scheduledSource" in localMessage
        ? localMessage.scheduledSource
        : undefined
    );
    return {
      ...message,
      id: localMessage.id,
      ...(
        message.role === "user"
        && !remoteScheduledSource
        && localScheduledSource
          ? { scheduledSource: localScheduledSource }
          : {}
      ),
      ...(
        message.role === "user"
        && localMessage.role === "user"
        && message.clientMessageId
        && message.clientMessageId === localMessage.clientMessageId
        && !message.correctionStatus
        && localMessage.correctionStatus
          ? {
              correctionStatus: localMessage.correctionStatus,
              submissionHint: localMessage.submissionHint,
              ...(localMessage.redirectId
                ? { redirectId: localMessage.redirectId }
                : {}),
              ...(localMessage.parentRunId
                ? { parentRunId: localMessage.parentRunId }
                : {}),
              ...(localMessage.promotedRunId
                ? { promotedRunId: localMessage.promotedRunId }
                : {})
            }
          : {}
      ),
      ...(localMessage.ttftMs !== undefined ? { ttftMs: localMessage.ttftMs } : {})
    };
  });
}

function carryCompletedTurnActivity(
  remote: ChatMessage[],
  local: ChatMessage[],
  activeTurn: ActiveTurn
): ChatMessage[] {
  const localUserIndex = local.findIndex((message) => message.id === activeTurn.userMessageId);
  const localAssistantIndex = local.findIndex((message) => message.id === activeTurn.assistantMessageId);
  const remoteUserIndex = lastUserMessageIndex(remote);
  const remoteAssistantIndex = firstAssistantMessageIndexAfter(remote, remoteUserIndex);
  if (
    localUserIndex < 0
    || localAssistantIndex < 0
    || remoteUserIndex < 0
    || remoteAssistantIndex < 0
  ) {
    return carryExactLocalMetadata(remote, local);
  }
  const remoteWithRunId = attachTurnRuntimeRunId(
    remote,
    remoteUserIndex,
    remoteAssistantIndex,
    activeTurn.runtimeRunId
  );

  const localSegments = reasoningSegments(local, localUserIndex + 1, localAssistantIndex);
  const reasoningMatches = matchReasoningSegments(
    reasoningSegments(remoteWithRunId, remoteUserIndex + 1, remoteAssistantIndex),
    localSegments
  );
  const matchedLocalIds = matchedReasoningIds(reasoningMatches);
  const next = carryExactLocalMetadata(remoteWithRunId, local).map((message, index) => {
    const localSegment = reasoningMatches.get(index);
    if (!localSegment) return message;
    return {
      ...message,
      id: localSegment.selected.id,
      runtimeRunId: activeTurn.runtimeRunId,
      ...(localSegment.selected.ttftMs !== undefined
        ? { ttftMs: localSegment.selected.ttftMs }
        : {})
    };
  });
  const missingReasoningIds = new Set(
    localSegments
      .filter((segment) => segment.members.every((message) => !matchedLocalIds.has(message.id)))
      .map((segment) => segment.selected.id)
  );
  const selectedReasoningIds = new Set(localSegments.map((segment) => segment.selected.id));
  const missing = local
    .slice(localUserIndex + 1, localAssistantIndex)
    .filter((message) =>
      (message.kind === "reasoning" && selectedReasoningIds.has(message.id) && missingReasoningIds.has(message.id))
      || message.kind === "tool"
      || message.kind === "background"
    )
    .filter((message) => !next.some((candidate) => candidate.id === message.id));
  return missing.length === 0
    ? next
    : [...next.slice(0, remoteAssistantIndex), ...missing, ...next.slice(remoteAssistantIndex)];
}

function isPrefix(prefix: string[], values: string[]): boolean {
  return prefix.length <= values.length && prefix.every((value, index) => values[index] === value);
}

function isSuffix(suffix: string[], values: string[]): boolean {
  const offset = values.length - suffix.length;
  return offset >= 0 && suffix.every((value, index) => values[offset + index] === value);
}

function appendLocalAfterRemote(remote: ChatMessage[], local: ChatMessage[]): ChatMessage[] {
  const maxOverlap = Math.min(remote.length, local.length);
  for (let overlap = maxOverlap; overlap > 0; overlap -= 1) {
    const matches = remote.slice(-overlap).every((message, index) => {
      const remoteKey = exactMessageKey(message);
      const localKey = exactMessageKey(local[index]);
      return remoteKey !== null && remoteKey === localKey;
    });
    if (matches) return [...remote, ...local.slice(overlap)];
  }
  return [...remote, ...local];
}

function isPlainMessage(message: ChatMessage): boolean {
  return message.kind === undefined || message.kind === "message";
}

function lastUserMessageIndex(messages: ChatMessage[]): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === "user" && isPlainMessage(messages[index])) return index;
  }
  return -1;
}

function firstAssistantMessageIndexAfter(messages: ChatMessage[], userIndex: number): number {
  return messages.findIndex((message, index) =>
    index > userIndex && message.role === "assistant" && isPlainMessage(message)
  );
}

function attachTurnRuntimeRunId(
  messages: ChatMessage[],
  userIndex: number,
  assistantIndex: number,
  runtimeRunId?: string
): ChatMessage[] {
  if (!runtimeRunId) return messages;
  return messages.map((message, index) => {
    const inTurn = index > userIndex && (assistantIndex < 0 || index < assistantIndex);
    const execution = message.kind === "reasoning"
      || message.kind === "activity"
      || message.kind === "tool"
      || message.kind === "background"
      || message.kind === "capability-error";
    return inTurn && execution && !message.runtimeRunId
      ? { ...message, runtimeRunId }
      : message;
  });
}

function containsMessage(messages: ChatMessage[], candidate: ChatMessage): boolean {
  if (messages.some((message) => message.id === candidate.id)) return true;
  if (candidate.kind === "tool" && candidate.hasStableCallId && candidate.providerCallId) {
    return messages.some((message) =>
      message.kind === "tool"
      && message.hasStableCallId
      && message.providerCallId === candidate.providerCallId
    );
  }
  if (candidate.kind === "background") {
    return messages.some((message) => message.kind === "background" && message.taskId === candidate.taskId);
  }
  const key = exactMessageKey(candidate);
  return key !== null && messages.some((message) => exactMessageKey(message) === key);
}

function mergeRemoteWithActiveTurn(
  remote: ChatMessage[],
  local: ChatMessage[],
  activeTurn: ActiveTurn
): ChatMessage[] | null {
  const localUserIndex = local.findIndex((message) => message.id === activeTurn.userMessageId);
  if (localUserIndex < 0) return null;

  const remoteUserIndex = lastUserMessageIndex(remote);
  if (
    remoteUserIndex < 0
    || normalizedText(messageText(remote[remoteUserIndex])) !== normalizedText(activeTurn.submittedText)
  ) {
    return null;
  }

  const localAssistantIndex = local.findIndex(
    (message) => message.id === activeTurn.assistantMessageId
  );
  const remoteAssistantIndex = firstAssistantMessageIndexAfter(remote, remoteUserIndex);
  const remoteWithRunId = attachTurnRuntimeRunId(
    remote,
    remoteUserIndex,
    remoteAssistantIndex,
    activeTurn.runtimeRunId
  );
  if (remoteAssistantIndex >= 0 && localAssistantIndex >= 0) {
    const localAssistantKey = exactMessageKey(local[localAssistantIndex]);
    const remoteAssistantKey = exactMessageKey(remoteWithRunId[remoteAssistantIndex]);
    if (localAssistantKey !== null && localAssistantKey !== remoteAssistantKey) return null;
  }
  const localActiveTurnEnd = localAssistantIndex >= 0 ? localAssistantIndex : local.length;
  const remoteActiveTurnEnd = remoteAssistantIndex >= 0 ? remoteAssistantIndex : remote.length;
  const localSegments = reasoningSegments(local, localUserIndex + 1, localActiveTurnEnd);
  const reasoningMatches = matchReasoningSegments(
    reasoningSegments(remoteWithRunId, remoteUserIndex + 1, remoteActiveTurnEnd),
    localSegments
  );
  const matchedLocalReasoningIds = matchedReasoningIds(reasoningMatches);
  const selectedLocalReasoningIds = new Set(
    localSegments.map((segment) => segment.selected.id)
  );
  const localOutsideActiveTurn = [
    ...local.slice(0, localUserIndex),
    ...(localAssistantIndex >= 0 ? local.slice(localAssistantIndex + 1) : [])
  ];
  let anchored = carryExactLocalMetadata(remoteWithRunId, localOutsideActiveTurn).map((message, index) => {
    const localReasoning = reasoningMatches.get(index);
    if (localReasoning) {
      return {
        ...localReasoning.selected,
        runtimeRunId: activeTurn.runtimeRunId
      };
    }
    if (index === remoteUserIndex) return { ...message, id: activeTurn.userMessageId };
    if (index !== remoteAssistantIndex) return message;
    const localAssistant = localAssistantIndex >= 0 ? local[localAssistantIndex] : undefined;
    return {
      ...message,
      id: activeTurn.assistantMessageId,
      ...(localAssistant?.ttftMs !== undefined ? { ttftMs: localAssistant.ttftMs } : {})
    };
  });

  const localSupplements = local
    .slice(localUserIndex + 1, localActiveTurnEnd)
    .filter((message) =>
      message.kind === "reasoning" || message.kind === "tool" || message.kind === "background"
    )
    .filter((message) =>
      message.kind !== "reasoning"
      || (
        selectedLocalReasoningIds.has(message.id)
        && !matchedLocalReasoningIds.has(message.id)
      )
    )
    .filter((message) => !containsMessage(anchored, message));

  if (remoteAssistantIndex < 0) {
    const localTail = local
      .slice(localUserIndex + 1)
      .filter((message) =>
        message.kind !== "reasoning"
        || (
          selectedLocalReasoningIds.has(message.id)
          && !matchedLocalReasoningIds.has(message.id)
        )
      )
      .filter((message) => !containsMessage(anchored, message));
    return [...anchored, ...localTail];
  }

  if (localSupplements.length > 0) {
    anchored = [
      ...anchored.slice(0, remoteAssistantIndex),
      ...localSupplements,
      ...anchored.slice(remoteAssistantIndex)
    ];
  }
  return anchored;
}

export function reconcileSessionHistory(
  remote: ChatMessage[],
  local: ChatMessage[],
  activeTurn?: ActiveTurn
): ChatMessage[] {
  if (local.length === 0) return remote;
  if (remote.length === 0) return local;
  if (local.some((message) => message.role === "assistant" && message.status === "failed")) return local;

  const localUsers = local
    .filter((message) => message.role === "user")
    .map(userReconcileKey);
  const remoteUsers = remote
    .filter((message) => message.role === "user")
    .map(userReconcileKey);
  if (activeTurn?.status === "submitting" || activeTurn?.status === "streaming" || activeTurn?.status === "failed") {
    const anchored = mergeRemoteWithActiveTurn(remote, local, activeTurn);
    if (anchored) return anchored;
    return isPrefix(remoteUsers, localUsers) ? local : appendLocalAfterRemote(remote, local);
  }
  if (activeTurn?.status === "completed" && isSuffix(localUsers, remoteUsers)) {
    return carryCompletedTurnActivity(remote, local, activeTurn);
  }
  if (remoteUsers.length < localUsers.length) return local;
  if (localUsers.some((text, index) => remoteUsers[index] !== text)) return local;
  if (
    activeTurn
    && remoteUsers[localUsers.length - 1]
      !== `legacy:${normalizedText(activeTurn.submittedText)}`
  ) return local;

  return carryExactLocalMetadata(remote, local);
}
