import type { SessionPrewarmState } from "../api/client";

export class SessionPrewarmCoordinator {
  private readonly inflight = new Map<string, Promise<SessionPrewarmState>>();

  has(key: string): boolean {
    return this.inflight.has(key);
  }

  ensure(
    key: string,
    request: () => Promise<SessionPrewarmState | undefined>
  ): Promise<SessionPrewarmState> {
    const current = this.inflight.get(key);
    if (current) return current;
    const pending = request()
      .then((state) => state ?? { state: "ready" as const })
      .finally(() => {
        if (this.inflight.get(key) === pending) this.inflight.delete(key);
      });
    this.inflight.set(key, pending);
    return pending;
  }

  invalidate(key: string): void {
    this.inflight.delete(key);
  }

  invalidateWorkspace(workspaceId: number): void {
    const prefix = `${workspaceId}:`;
    for (const key of this.inflight.keys()) {
      if (key.startsWith(prefix)) this.inflight.delete(key);
    }
  }

  clear(): void {
    this.inflight.clear();
  }
}
