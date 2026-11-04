export type ChatDocumentVisibility = "visible" | "hidden" | "prerender" | "unknown";

export interface ChatClientTimingPayload {
  sampleKind?: "lifecycle" | "content";
  requestToFirstEventMs?: number;
  requestToFirstVisibleReceivedMs: number;
  requestToFirstVisibleCommitMs: number;
  requestToFirstVisiblePaintMs: number;
  dispatchToFirstVisibleReceivedMs?: number;
  dispatchToFirstVisibleCommitMs?: number;
  dispatchToFirstVisiblePaintMs?: number;
  queuedWaitMs: number;
  receiptAgeMs?: number;
  requiredPrewarmWaitMs: number;
  documentVisibility: ChatDocumentVisibility;
  reconnectBeforeFirstVisible: boolean;
  sendOverlappedPrewarm: boolean;
  frontendBuild?: string;
}

interface TurnTimingSample {
  userClickedAt: number;
  dispatchStartedAt?: number;
  receiptAgeMs?: number;
  firstEventReceivedAt?: number;
  firstVisibleReceivedAt?: number;
  firstVisibleCommitAt?: number;
  visibleTargetId?: string;
  reconnectBeforeFirstVisible: boolean;
  sendOverlappedPrewarm: boolean;
  requiredPrewarmWaitMs: number;
}

const duration = (start: number, end: number) => Math.max(0, end - start);

export class TurnTimingCoordinator {
  private readonly samples = new Map<string, TurnTimingSample>();

  begin(
    key: string,
    requestStartedAt: number,
    visibility: ChatDocumentVisibility,
    sendOverlappedPrewarm: boolean
  ): void {
    if (visibility !== "visible") return;
    this.samples.set(key, {
      userClickedAt: requestStartedAt,
      reconnectBeforeFirstVisible: false,
      sendOverlappedPrewarm,
      requiredPrewarmWaitMs: 0
    });
  }

  markDispatchStarted(
    key: string,
    at: number,
    receiptAgeMs?: number
  ): void {
    const sample = this.samples.get(key);
    if (!sample || sample.dispatchStartedAt !== undefined) return;
    sample.dispatchStartedAt = at;
    if (receiptAgeMs !== undefined && Number.isFinite(receiptAgeMs)) {
      sample.receiptAgeMs = Math.max(0, receiptAgeMs);
    }
  }

  accept(turnKey: string, runId: string): void {
    const sample = this.samples.get(turnKey);
    if (!sample) return;
    this.samples.delete(turnKey);
    this.samples.set(runId, sample);
  }

  has(key: string): boolean {
    return this.samples.has(key);
  }

  markFirstEvent(key: string, at: number): void {
    const sample = this.samples.get(key);
    if (sample && sample.firstEventReceivedAt === undefined) sample.firstEventReceivedAt = at;
  }

  markReconnect(runId: string): void {
    const sample = this.samples.get(runId);
    if (sample) sample.reconnectBeforeFirstVisible = true;
  }

  markRequiredPrewarmWait(key: string, waitMs: number): void {
    const sample = this.samples.get(key);
    if (sample) sample.requiredPrewarmWaitMs = Math.max(0, waitMs);
  }

  offerVisibleCandidate(
    runId: string,
    targetId: string,
    receivedAt: number,
    visibility: ChatDocumentVisibility
  ): boolean {
    const sample = this.samples.get(runId);
    if (!sample) return false;
    if (visibility !== "visible") {
      this.samples.delete(runId);
      return false;
    }
    if (sample.visibleTargetId !== undefined) return sample.visibleTargetId === targetId;
    sample.visibleTargetId = targetId;
    sample.firstVisibleReceivedAt = receivedAt;
    return true;
  }

  abandonVisibleCandidate(runId: string, targetId: string): boolean {
    const sample = this.samples.get(runId);
    if (!sample || sample.visibleTargetId !== targetId || sample.firstVisibleCommitAt !== undefined) return false;
    sample.visibleTargetId = undefined;
    sample.firstVisibleReceivedAt = undefined;
    return true;
  }

  markVisibleCommit(
    runId: string,
    targetId: string,
    at: number,
    visibility: ChatDocumentVisibility
  ): boolean {
    const sample = this.samples.get(runId);
    if (!sample || visibility !== "visible" || sample.visibleTargetId !== targetId) return false;
    if (sample.firstVisibleCommitAt === undefined) sample.firstVisibleCommitAt = at;
    return true;
  }

  markVisiblePaint(
    runId: string,
    targetId: string,
    at: number,
    visibility: ChatDocumentVisibility
  ): ChatClientTimingPayload | undefined {
    const sample = this.samples.get(runId);
    if (
      !sample
      || visibility !== "visible"
      || sample.visibleTargetId !== targetId
      || sample.firstVisibleReceivedAt === undefined
      || sample.firstVisibleCommitAt === undefined
    ) return undefined;
    this.samples.delete(runId);
    const dispatchStartedAt = sample.dispatchStartedAt
      ?? sample.userClickedAt;
    return {
      ...(sample.firstEventReceivedAt !== undefined
        ? { requestToFirstEventMs: duration(sample.userClickedAt, sample.firstEventReceivedAt) }
        : {}),
      requestToFirstVisibleReceivedMs: duration(sample.userClickedAt, sample.firstVisibleReceivedAt),
      requestToFirstVisibleCommitMs: duration(sample.userClickedAt, sample.firstVisibleCommitAt),
      requestToFirstVisiblePaintMs: duration(sample.userClickedAt, at),
      dispatchToFirstVisibleReceivedMs: duration(dispatchStartedAt, sample.firstVisibleReceivedAt),
      dispatchToFirstVisibleCommitMs: duration(dispatchStartedAt, sample.firstVisibleCommitAt),
      dispatchToFirstVisiblePaintMs: duration(dispatchStartedAt, at),
      queuedWaitMs: duration(sample.userClickedAt, dispatchStartedAt),
      ...(sample.receiptAgeMs !== undefined
        ? { receiptAgeMs: sample.receiptAgeMs }
        : {}),
      requiredPrewarmWaitMs: sample.requiredPrewarmWaitMs,
      documentVisibility: "visible",
      reconnectBeforeFirstVisible: sample.reconnectBeforeFirstVisible,
      sendOverlappedPrewarm: sample.sendOverlappedPrewarm
    };
  }

  cancel(key: string): void {
    this.samples.delete(key);
  }

  clear(): void {
    this.samples.clear();
  }
}

export function currentDocumentVisibility(): ChatDocumentVisibility {
  if (typeof document === "undefined") return "unknown";
  const visibility = document.visibilityState;
  return visibility === "visible" || visibility === "hidden" || visibility === "prerender"
    ? visibility
    : "unknown";
}
