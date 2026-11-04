import type { RuntimeClarifyRequest } from "../api/client";

export type { RuntimeClarifyRequest };

export function mergeClarifyRequests(
  current: RuntimeClarifyRequest[],
  incoming: RuntimeClarifyRequest[]
): RuntimeClarifyRequest[] {
  const byRequestId = new Map<string, RuntimeClarifyRequest>();
  for (const item of current) byRequestId.set(item.requestId, item);
  for (const item of incoming) {
    const existing = byRequestId.get(item.requestId);
    if (!existing || item.version >= existing.version) {
      byRequestId.set(item.requestId, item);
    }
  }
  return [...byRequestId.values()];
}

export function scopeClarifyRequests(
  items: RuntimeClarifyRequest[],
  runtimeSessionId: string,
  convergedRequestIds: ReadonlySet<string>
): RuntimeClarifyRequest[] {
  return items.filter((item) => (
    item.runtimeSessionId === runtimeSessionId
    && !convergedRequestIds.has(item.requestId)
  ));
}

export function isClarifyCardRenderable(item: RuntimeClarifyRequest): boolean {
  // Auto-default is model-side silent continuation, not a user-confirmed
  // clarify answer. Once Backend claims it, the card must stop competing with
  // normal assistant output in this or another window.
  // Expert-worker requests likewise exit the stack once answered: the worker
  // consumes the answer in place and its output arrives through the
  // collaboration result, never as a second interactive card.
  if (item.mode === "expert_worker" && item.status !== "waiting") return false;
  return !(item.answerSource === "auto" && item.status !== "waiting");
}

export class ClarifyPollCoordinator {
  private currentTicket = 0;

  begin(): number {
    this.currentTicket += 1;
    return this.currentTicket;
  }

  invalidate(): void {
    this.currentTicket += 1;
  }

  accept(ticket: number): boolean {
    return ticket === this.currentTicket;
  }
}

export function findClarifyContinuationReplay(
  items: RuntimeClarifyRequest[],
  replayedRunIds: ReadonlySet<string>
): { requestId: string; runtimeRunId: string } | undefined {
  const item = items.find((
    candidate
  ) => candidate.status === "answered"
    && candidate.mode !== "expert_worker"
    && Boolean(candidate.resumeRuntimeRunId)
    && !replayedRunIds.has(candidate.resumeRuntimeRunId ?? ""));
  if (!item?.resumeRuntimeRunId) return undefined;
  return {
    requestId: item.requestId,
    runtimeRunId: item.resumeRuntimeRunId,
  };
}
