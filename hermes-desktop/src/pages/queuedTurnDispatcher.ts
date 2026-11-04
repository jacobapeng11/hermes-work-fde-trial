import type { SessionReadinessTarget } from "./sessionReadinessState";

export interface QueuedTurn<T> {
  runtimeRunId: string;
  target: SessionReadinessTarget;
  payload: T;
  userClickedAt: number;
}

function sameTarget(
  left: SessionReadinessTarget,
  right: SessionReadinessTarget
): boolean {
  return left.workspaceId === right.workspaceId
    && left.sessionId === right.sessionId
    && left.modelId === right.modelId
    && left.agentProfileKind === right.agentProfileKind
    && left.runtimeAgentProfileId === right.runtimeAgentProfileId
    && left.epoch === right.epoch;
}

export class QueuedTurnDispatcher<T> {
  private current: QueuedTurn<T> | null = null;
  private claimed = false;

  enqueue(turn: QueuedTurn<T>): boolean {
    if (this.current !== null) return false;
    this.current = turn;
    this.claimed = false;
    return true;
  }

  claimForDispatch(
    runtimeRunId: string,
    target: SessionReadinessTarget
  ): QueuedTurn<T> | null {
    if (
      this.current === null
      || this.claimed
      || this.current.runtimeRunId !== runtimeRunId
      || !sameTarget(this.current.target, target)
    ) {
      return null;
    }
    this.claimed = true;
    return this.current;
  }

  cancel(runtimeRunId?: string): QueuedTurn<T> | null {
    if (
      this.current === null
      || (
        runtimeRunId !== undefined
        && this.current.runtimeRunId !== runtimeRunId
      )
    ) {
      return null;
    }
    const cancelled = this.current;
    this.current = null;
    this.claimed = false;
    return cancelled;
  }

  complete(runtimeRunId: string): void {
    if (this.current?.runtimeRunId !== runtimeRunId) return;
    this.current = null;
    this.claimed = false;
  }

  peek(): QueuedTurn<T> | null {
    return this.current;
  }

  isClaimed(runtimeRunId: string): boolean {
    return (
      this.current?.runtimeRunId === runtimeRunId
      && this.claimed
    );
  }

  clear(): void {
    this.current = null;
    this.claimed = false;
  }
}
