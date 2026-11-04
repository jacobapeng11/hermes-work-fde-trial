import type { SessionPrewarmState } from "../api/client";

export interface SessionReadinessTarget {
  workspaceId: number;
  sessionId: string;
  modelId: number | null;
  agentProfileKind: "personal" | "expert";
  runtimeAgentProfileId: number | null;
  epoch: number;
}

export type SessionReadinessStatus =
  | "idle"
  | "prewarming"
  | "ready"
  | "queued"
  | "dispatching"
  | "streaming"
  | "retry_wait"
  | "terminal_error";

export interface SessionReadinessState {
  target: SessionReadinessTarget | null;
  status: SessionReadinessStatus;
  receipt: SessionPrewarmState | null;
  error?: string;
}

export type SessionReadinessEvent =
  | { type: "prewarm_started"; target: SessionReadinessTarget }
  | {
      type: "prewarm_ready";
      target: SessionReadinessTarget;
      receipt: SessionPrewarmState;
    }
  | {
      type: "prewarm_retry_wait";
      target: SessionReadinessTarget;
      error: string;
    }
  | {
      type: "prewarm_failed";
      target: SessionReadinessTarget;
      error: string;
    }
  | { type: "queued"; target: SessionReadinessTarget }
  | { type: "dispatch_claimed"; target: SessionReadinessTarget }
  | { type: "streaming"; target: SessionReadinessTarget }
  | { type: "cleared" };

export const initialSessionReadinessState: SessionReadinessState = {
  target: null,
  status: "idle",
  receipt: null
};

export function sessionReadinessTargetMatches(
  left: SessionReadinessTarget | null,
  right: SessionReadinessTarget
): boolean {
  return Boolean(
    left
    && left.workspaceId === right.workspaceId
    && left.sessionId === right.sessionId
    && left.modelId === right.modelId
    && left.agentProfileKind === right.agentProfileKind
    && left.runtimeAgentProfileId === right.runtimeAgentProfileId
    && left.epoch === right.epoch
  );
}

export function reduceSessionReadiness(
  state: SessionReadinessState,
  event: SessionReadinessEvent
): SessionReadinessState {
  if (event.type === "cleared") return initialSessionReadinessState;
  if (event.type === "prewarm_started") {
    return {
      target: event.target,
      status: "prewarming",
      receipt: null
    };
  }
  if (!sessionReadinessTargetMatches(state.target, event.target)) {
    return state;
  }
  switch (event.type) {
    case "prewarm_ready":
      return {
        target: state.target,
        status: state.status === "queued" ? "queued" : "ready",
        receipt: event.receipt
      };
    case "prewarm_retry_wait":
      return {
        ...state,
        status: state.status === "queued" ? "queued" : "retry_wait",
        error: event.error
      };
    case "prewarm_failed":
      return {
        ...state,
        status: "terminal_error",
        error: event.error
      };
    case "queued":
      return { ...state, status: "queued" };
    case "dispatch_claimed":
      return { ...state, status: "dispatching" };
    case "streaming":
      return { ...state, status: "streaming" };
    default:
      return state;
  }
}
