export interface ChatQueuedRun {
  runId: string;
  clientMessageId: string;
  enqueueSeq: number;
  prompt: string;
  attachmentFileIds: string[];
}

export interface ChatActiveRun {
  runId: string;
  state: string;
  cancellationRequestedAt: string | null;
}

export interface ChatQueueSnapshot {
  queuedCount: number;
  scheduledQueuedCount: number;
  queuedRuns: ChatQueuedRun[];
  activeRun: ChatActiveRun | null;
}

export const emptyChatQueueSnapshot = (): ChatQueueSnapshot => ({
  queuedCount: 0,
  scheduledQueuedCount: 0,
  queuedRuns: [],
  activeRun: null
});

/**
 * Keeps the browser's read-only queue projection monotonic while several
 * Runtime snapshots are in flight for the same Session.
 */
export class QueueSnapshotVersionGate {
  private sessionId: string | null = null;
  private version = 0;

  issue(sessionId: string): number {
    this.sessionId = sessionId;
    this.version += 1;
    return this.version;
  }

  invalidate(sessionId: string): void {
    this.sessionId = sessionId;
    this.version += 1;
  }

  isCurrent(sessionId: string, version: number): boolean {
    return this.sessionId === sessionId && this.version === version;
  }
}

export function reconcileChatQueueSnapshot(
  snapshot: ChatQueueSnapshot
): ChatQueueSnapshot {
  const seen = new Set<string>();
  const queuedRuns = [...snapshot.queuedRuns]
    .sort((left, right) => left.enqueueSeq - right.enqueueSeq)
    .filter((run) => {
      if (seen.has(run.runId)) return false;
      seen.add(run.runId);
      return true;
    });
  return {
    ...snapshot,
    queuedRuns,
    queuedCount: Math.max(snapshot.scheduledQueuedCount, snapshot.queuedCount)
  };
}

export function removeStartedQueuedRun(
  snapshot: ChatQueueSnapshot,
  runId: string
): ChatQueueSnapshot {
  const queuedRuns = snapshot.queuedRuns.filter((run) => run.runId !== runId);
  return {
    ...snapshot,
    queuedRuns,
    queuedCount: Math.max(
      snapshot.scheduledQueuedCount,
      snapshot.queuedCount - (queuedRuns.length === snapshot.queuedRuns.length ? 0 : 1)
    ),
    activeRun: snapshot.activeRun?.runId === runId
      ? snapshot.activeRun
      : { runId, state: "RUNNING", cancellationRequestedAt: null }
  };
}
