import type { ChatMessage, ToolMessage } from "./pages/chatRunProjection";

export type ChatRenderNode =
  | { kind: "message"; message: ChatMessage }
  | { kind: "tool-group"; id: string; messages: ToolMessage[] };

export type ExecutionTraceMessage = Extract<ChatMessage, { kind: "reasoning" | "activity" | "tool" }>;
export type ExecutionTimelineMessage = Extract<ChatMessage, { kind: "reasoning" | "activity" | "tool" | "background" | "capability-error" }>;

export type ExecutionRenderNode =
  | { kind: "message"; message: ChatMessage }
  | { kind: "execution-timeline"; id: string; runtimeRunId?: string; messages: ExecutionTimelineMessage[] };

export function groupToolMessages(messages: ChatMessage[]): ChatRenderNode[] {
  const nodes: ChatRenderNode[] = [];
  for (const message of messages) {
    const previous = nodes[nodes.length - 1];
    if (message.kind === "tool" && previous?.kind === "tool-group") {
      previous.messages.push(message);
    } else if (message.kind === "tool") {
      nodes.push({ kind: "tool-group", id: `tool-group-${message.id}`, messages: [message] });
    } else {
      nodes.push({ kind: "message", message });
    }
  }
  return nodes;
}

function isExecutionTimelineMessage(message: ChatMessage): message is ExecutionTimelineMessage {
  return message.kind === "reasoning"
    || message.kind === "activity"
    || message.kind === "tool"
    || message.kind === "background"
    || message.kind === "capability-error";
}

export function groupExecutionMessages(messages: ChatMessage[]): ExecutionRenderNode[] {
  const nodes: ExecutionRenderNode[] = [];
  for (const message of messages) {
    const previous = nodes[nodes.length - 1];
    const sameRun = previous?.kind === "execution-timeline"
      && previous.runtimeRunId === message.runtimeRunId
      && (previous.runtimeRunId !== undefined || message.runtimeRunId === undefined);
    if (isExecutionTimelineMessage(message) && sameRun) {
      previous.messages.push(message);
    } else if (isExecutionTimelineMessage(message)) {
      nodes.push({
        kind: "execution-timeline",
        id: `execution-timeline-${message.id}`,
        ...(message.runtimeRunId ? { runtimeRunId: message.runtimeRunId } : {}),
        messages: [message]
      });
    } else {
      nodes.push({ kind: "message", message });
    }
  }
  return nodes;
}
