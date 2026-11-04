import type {
  RuntimeChatHistoryMessage,
  RuntimeChatHistoryResponse
} from "@hermes-cloud/schemas";

export interface ChatHistoryLoadTarget {
  runtimeWorkspaceId: number;
  runtimeSessionId: string;
  loadEpoch: number;
}

export type ChatHistoryLoadPhase =
  | "empty"
  | "preview"
  | "authoritative"
  | "degraded";

export type ChatHistorySyncStatus = "syncing" | "ready" | "degraded";

export interface ChatHistoryLoadState {
  target: ChatHistoryLoadTarget;
  phase: ChatHistoryLoadPhase;
  status: ChatHistorySyncStatus;
  source?: "hermes" | "mysql";
  messages: RuntimeChatHistoryMessage[];
  previewFailed: boolean;
  authorityFailed: boolean;
  error?: string;
}

export type ChatHistoryLoadEvent =
  | {
      type: "preview_loaded";
      target: ChatHistoryLoadTarget;
      response: RuntimeChatHistoryResponse;
    }
  | {
      type: "authoritative_loaded";
      target: ChatHistoryLoadTarget;
      response: RuntimeChatHistoryResponse;
    }
  | {
      type: "preview_failed";
      target: ChatHistoryLoadTarget;
      error: string;
    }
  | {
      type: "authoritative_failed";
      target: ChatHistoryLoadTarget;
      error: string;
    };

interface LoadChatHistorySourcesOptions {
  target: ChatHistoryLoadTarget;
  initialMessages?: RuntimeChatHistoryMessage[];
  preview?: () => Promise<RuntimeChatHistoryResponse>;
  authoritative: () => Promise<RuntimeChatHistoryResponse>;
  publish: (state: ChatHistoryLoadState) => void;
}

const textValue = (value: unknown): string => (
  typeof value === "string" ? value : ""
);

const numericValue = (value: unknown): number | null => (
  typeof value === "number" && Number.isFinite(value) ? value : null
);

function messageField(message: RuntimeChatHistoryMessage, camel: string, snake: string): unknown {
  return message[camel] ?? message[snake];
}

export function stableHistoryMessageIdentity(
  message: RuntimeChatHistoryMessage,
  runtimeSessionId: string,
  source: "hermes" | "mysql",
  historyIndex: number
): string {
  const clientMessageId = textValue(messageField(
    message,
    "clientMessageId",
    "client_message_id"
  ));
  if (clientMessageId) return `client:${clientMessageId}`;

  const runtimeRunId = textValue(messageField(
    message,
    "runtimeRunId",
    "runtime_run_id"
  ));
  const kind = textValue(message.kind ?? message.type ?? message.event);
  const eventSeq = numericValue(messageField(message, "eventSeq", "event_seq"));
  if (runtimeRunId && eventSeq !== null && kind) {
    return `event:${runtimeRunId}:${eventSeq}:${kind}`;
  }

  const isOrdinaryRunMessage = !kind && (
    message.role === "user" || message.role === "assistant"
  );
  if (runtimeRunId && isOrdinaryRunMessage) {
    return `run:${runtimeRunId}:${message.role}`;
  }

  const providerCallId = textValue(
    message.callId
      ?? message.toolCallId
      ?? message.tool_call_id
      ?? message.call_id
  );
  if (runtimeRunId && providerCallId) {
    return `tool:${runtimeRunId}:${providerCallId}`;
  }

  const messageSeq = numericValue(messageField(message, "messageSeq", "message_seq"));
  if (messageSeq !== null) {
    return `snapshot:${source}:${runtimeSessionId}:${messageSeq}`;
  }
  return `snapshot:${source}:${runtimeSessionId}:index:${historyIndex}`;
}

export function deduplicateHistorySnapshot(
  messages: RuntimeChatHistoryMessage[],
  runtimeSessionId: string,
  source: "hermes" | "mysql"
): RuntimeChatHistoryMessage[] {
  const deduplicated: RuntimeChatHistoryMessage[] = [];
  const indexByIdentity = new Map<string, number>();
  messages.forEach((message, historyIndex) => {
    const identity = stableHistoryMessageIdentity(
      message,
      runtimeSessionId,
      source,
      historyIndex
    );
    const existingIndex = indexByIdentity.get(identity);
    if (existingIndex === undefined) {
      indexByIdentity.set(identity, deduplicated.length);
      deduplicated.push(message);
      return;
    }
    deduplicated[existingIndex] = message;
  });
  return deduplicated;
}

function stableHistoryIdentityOrNull(
  message: RuntimeChatHistoryMessage
): string | null {
  const clientMessageId = textValue(messageField(message, "clientMessageId", "client_message_id"));
  if (clientMessageId) return `client:${clientMessageId}`;
  const runtimeRunId = textValue(messageField(message, "runtimeRunId", "runtime_run_id"));
  const kind = textValue(message.kind ?? message.type ?? message.event);
  const eventSeq = numericValue(messageField(message, "eventSeq", "event_seq"));
  if (runtimeRunId && eventSeq !== null && kind) return `event:${runtimeRunId}:${eventSeq}:${kind}`;
  if (runtimeRunId && !kind && (message.role === "user" || message.role === "assistant")) {
    return `run:${runtimeRunId}:${message.role}`;
  }
  const providerCallId = textValue(message.callId ?? message.toolCallId ?? message.tool_call_id ?? message.call_id);
  if (runtimeRunId && providerCallId) return `tool:${runtimeRunId}:${providerCallId}`;
  return null;
}

function mergeAuthorityWithPreview(
  previewMessages: RuntimeChatHistoryMessage[],
  authorityMessages: RuntimeChatHistoryMessage[],
  runtimeSessionId: string
): RuntimeChatHistoryMessage[] {
  const merged = [...deduplicateHistorySnapshot(previewMessages, runtimeSessionId, "mysql")];
  const indexByIdentity = new Map<string, number>();
  merged.forEach((message, index) => {
    const identity = stableHistoryIdentityOrNull(message);
    if (identity) indexByIdentity.set(identity, index);
  });
  for (const message of deduplicateHistorySnapshot(authorityMessages, runtimeSessionId, "hermes")) {
    const identity = stableHistoryIdentityOrNull(message);
    if (!identity) {
      if (merged.length === 0) merged.push(message);
      continue;
    }
    const existingIndex = indexByIdentity.get(identity);
    if (existingIndex === undefined) {
      indexByIdentity.set(identity, merged.length);
      merged.push(message);
    } else {
      merged[existingIndex] = message;
    }
  }
  return merged;
}

export function initialChatHistoryLoadState(
  target: ChatHistoryLoadTarget,
  initialMessages: RuntimeChatHistoryMessage[] = []
): ChatHistoryLoadState {
  return {
    target,
    phase: "empty",
    status: "syncing",
    messages: initialMessages,
    previewFailed: false,
    authorityFailed: false
  };
}

function sameTarget(left: ChatHistoryLoadTarget, right: ChatHistoryLoadTarget): boolean {
  return left.runtimeWorkspaceId === right.runtimeWorkspaceId
    && left.runtimeSessionId === right.runtimeSessionId
    && left.loadEpoch === right.loadEpoch;
}

function responseMatchesTarget(
  response: RuntimeChatHistoryResponse,
  target: ChatHistoryLoadTarget
): boolean {
  return response.runtimeWorkspaceId === target.runtimeWorkspaceId
    && response.runtimeSessionId === target.runtimeSessionId;
}

export function reduceChatHistoryLoad(
  state: ChatHistoryLoadState,
  event: ChatHistoryLoadEvent
): ChatHistoryLoadState {
  if (!sameTarget(state.target, event.target)) return state;

  if (event.type === "preview_failed") {
    return state.phase === "authoritative"
      ? state
      : { ...state, previewFailed: true };
  }
  if (event.type === "authoritative_failed") {
    return {
      ...state,
      phase: "degraded",
      status: "degraded",
      authorityFailed: true,
      error: event.error
    };
  }
  if (!responseMatchesTarget(event.response, state.target)) return state;

  if (event.type === "preview_loaded") {
    if (state.phase === "authoritative") return state;
    const previewMessages = deduplicateHistorySnapshot(
      event.response.messages,
      state.target.runtimeSessionId,
      "mysql"
    );
    const messages = previewMessages.length === 0 && state.messages.length > 0
      ? state.messages
      : previewMessages;
    return {
      ...state,
      phase: state.authorityFailed ? "degraded" : "preview",
      status: state.authorityFailed ? "degraded" : "syncing",
      source: "mysql",
      messages
    };
  }

  const authorityMessages = deduplicateHistorySnapshot(
    event.response.messages,
    state.target.runtimeSessionId,
    "hermes"
  );
  const messages = state.source === "mysql"
    ? mergeAuthorityWithPreview(state.messages, authorityMessages, state.target.runtimeSessionId)
    : authorityMessages;
  return {
    ...state,
    phase: "authoritative",
    status: "ready",
    source: "hermes",
    messages,
    authorityFailed: false,
    error: undefined
  };
}

const errorText = (reason: unknown, fallback: string): string => (
  reason instanceof Error ? reason.message : fallback
);

export async function loadChatHistorySources(
  options: LoadChatHistorySourcesOptions
): Promise<ChatHistoryLoadState> {
  let state = initialChatHistoryLoadState(
    options.target,
    options.initialMessages
  );
  options.publish(state);

  const dispatch = (event: ChatHistoryLoadEvent) => {
    const next = reduceChatHistoryLoad(state, event);
    if (next === state) return;
    state = next;
    options.publish(state);
  };
  const operations: Promise<void>[] = [];
  if (options.preview) {
    operations.push(options.preview().then(
      (preview) => dispatch({
        type: "preview_loaded",
        target: options.target,
        response: preview
      }),
      (reason: unknown) => dispatch({
        type: "preview_failed",
        target: options.target,
        error: errorText(reason, "历史预览加载失败")
      })
    ));
  }
  operations.push(options.authoritative().then(
    (authoritative) => dispatch({
      type: "authoritative_loaded",
      target: options.target,
      response: authoritative
    }),
    (reason: unknown) => dispatch({
      type: "authoritative_failed",
      target: options.target,
      error: errorText(reason, "最新记录同步失败")
    })
  ));
  await Promise.all(operations);
  return state;
}
