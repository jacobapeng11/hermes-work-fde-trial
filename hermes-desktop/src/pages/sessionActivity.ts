import type { RuntimeSessionSummary } from "@hermes-cloud/schemas";

export type SessionActivity =
  | { state: "idle" }
  | { state: "running"; runtimeRunId: string }
  | { state: "completed-unseen"; runtimeRunId: string }
  | { state: "error-unseen"; runtimeRunId: string };

export type SessionActivityMap = Record<string, SessionActivity>;

export interface SessionActivityScope {
  orgId: number;
  userId: number;
  runtimeWorkspaceId: number;
}

type SeenRunEnvelope = {
  version: 1;
  initialized: true;
  seen: Record<string, string>;
  errors: Record<string, string>;
};

type StorageLike = Pick<Storage, "getItem" | "setItem">;

const ACTIVE_RUN_STATUSES = new Set([
  "accepted",
  "canceling",
  "pending",
  "queued",
  "running",
  "submitting"
]);
const SUCCESS_RUN_STATUSES = new Set(["completed", "succeeded", "success"]);
const ERROR_RUN_STATUSES = new Set(["error", "failed"]);

export function sessionActivityStorageKey(scope: SessionActivityScope): string {
  return [
    "hermes.chat.seen-runs.v1",
    scope.orgId,
    scope.userId,
    scope.runtimeWorkspaceId
  ].join(":");
}

function readEnvelope(storage: StorageLike, key: string): SeenRunEnvelope | null {
  try {
    const raw = storage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<SeenRunEnvelope>;
    if (parsed.version !== 1 || parsed.initialized !== true || !parsed.seen || typeof parsed.seen !== "object") {
      return null;
    }
    const seen = Object.fromEntries(
      Object.entries(parsed.seen).filter((entry): entry is [string, string] => (
        Boolean(entry[0]) && typeof entry[1] === "string" && Boolean(entry[1])
      ))
    );
    const errors = Object.fromEntries(
      Object.entries(parsed.errors ?? {}).filter((entry): entry is [string, string] => (
        Boolean(entry[0]) && typeof entry[1] === "string" && Boolean(entry[1])
      ))
    );
    return { version: 1, initialized: true, seen, errors };
  } catch {
    return null;
  }
}

function activityForSession(
  session: RuntimeSessionSummary,
  activeSessionId: string | null,
  seen: Record<string, string>,
  errors: Record<string, string>
): SessionActivity {
  const latestRun = session.latestRun;
  if (!latestRun?.runtimeRunId) return { state: "idle" };
  const status = latestRun.status.toLowerCase();
  if (ACTIVE_RUN_STATUSES.has(status)) {
    return { state: "running", runtimeRunId: latestRun.runtimeRunId };
  }
  if (
    SUCCESS_RUN_STATUSES.has(status)
    && session.runtimeSessionId !== activeSessionId
    && seen[session.runtimeSessionId] !== latestRun.runtimeRunId
  ) {
    return { state: "completed-unseen", runtimeRunId: latestRun.runtimeRunId };
  }
  if (
    ERROR_RUN_STATUSES.has(status)
    && errors[session.runtimeSessionId] !== latestRun.runtimeRunId
  ) {
    return { state: "error-unseen", runtimeRunId: latestRun.runtimeRunId };
  }
  return { state: "idle" };
}

export class SessionActivityCoordinator {
  private readonly key: string;
  private seen: Record<string, string> = {};
  private errors: Record<string, string> = {};
  private initialized = false;

  constructor(
    scope: SessionActivityScope,
    private readonly storage: StorageLike
  ) {
    this.key = sessionActivityStorageKey(scope);
  }

  initialize(
    sessions: RuntimeSessionSummary[],
    activeSessionId: string | null
  ): SessionActivityMap {
    const stored = readEnvelope(this.storage, this.key);
    if (stored) {
      this.seen = stored.seen;
      this.errors = stored.errors;
    } else {
      this.seen = Object.fromEntries(sessions.flatMap((session) => {
        const latestRun = session.latestRun;
        return latestRun?.runtimeRunId && SUCCESS_RUN_STATUSES.has(latestRun.status.toLowerCase())
          ? [[session.runtimeSessionId, latestRun.runtimeRunId]]
          : [];
      }));
      this.errors = Object.fromEntries(sessions.flatMap((session) => {
        const latestRun = session.latestRun;
        return latestRun?.runtimeRunId && ERROR_RUN_STATUSES.has(latestRun.status.toLowerCase())
          ? [[session.runtimeSessionId, latestRun.runtimeRunId]]
          : [];
      }));
    }
    this.initialized = true;
    this.compact(sessions);
    for (const session of sessions) {
      if (
        session.runtimeSessionId === activeSessionId
        && session.latestRun?.runtimeRunId
        && SUCCESS_RUN_STATUSES.has(session.latestRun.status.toLowerCase())
      ) {
        this.seen[session.runtimeSessionId] = session.latestRun.runtimeRunId;
      }
    }
    this.persist();
    return Object.fromEntries(sessions.map((session) => [
      session.runtimeSessionId,
      activityForSession(session, activeSessionId, this.seen, this.errors)
    ]));
  }

  reconcile(
    current: SessionActivityMap,
    sessions: RuntimeSessionSummary[],
    activeSessionId: string | null
  ): SessionActivityMap {
    const projected = this.initialize(sessions, activeSessionId);
    return Object.fromEntries(sessions.map((session) => {
      const id = session.runtimeSessionId;
      const live = current[id];
      const durable = projected[id] ?? { state: "idle" as const };
      if (
        live?.state === "running"
        && session.latestRun?.runtimeRunId !== live.runtimeRunId
      ) {
        return [id, live];
      }
      if (
        live?.state === "completed-unseen"
        && durable.state === "running"
        && durable.runtimeRunId === live.runtimeRunId
      ) {
        return [id, live];
      }
      if (
        live?.state === "error-unseen"
        && durable.state === "running"
        && durable.runtimeRunId === live.runtimeRunId
      ) {
        return [id, live];
      }
      return [id, durable];
    }));
  }

  markRunning(
    current: SessionActivityMap,
    sessionId: string,
    runtimeRunId: string
  ): SessionActivityMap {
    const activity = current[sessionId];
    if (this.seen[sessionId] === runtimeRunId) return current;
    if (activity?.state === "running" && activity.runtimeRunId === runtimeRunId) {
      return current;
    }
    if (activity?.state === "completed-unseen" && activity.runtimeRunId === runtimeRunId) {
      return current;
    }
    if (activity?.state === "error-unseen" && activity.runtimeRunId === runtimeRunId) {
      return current;
    }
    return {
      ...current,
      [sessionId]: { state: "running", runtimeRunId }
    };
  }

  markTerminal(
    current: SessionActivityMap,
    sessionId: string,
    runtimeRunId: string,
    successful: boolean,
    activeSessionId: string | null
  ): SessionActivityMap {
    const activity = current[sessionId];
    if (!successful && activity?.state === "idle") return current;
    if (!successful || sessionId === activeSessionId) {
      if (successful && this.seen[sessionId] === runtimeRunId && activity?.state === "idle") {
        return current;
      }
      if (successful) this.markRunSeen(sessionId, runtimeRunId);
      return { ...current, [sessionId]: { state: "idle" } };
    }
    if (this.seen[sessionId] === runtimeRunId) {
      return { ...current, [sessionId]: { state: "idle" } };
    }
    if (activity?.state === "completed-unseen" && activity.runtimeRunId === runtimeRunId) {
      return current;
    }
    return {
      ...current,
      [sessionId]: { state: "completed-unseen", runtimeRunId }
    };
  }

  markSeen(current: SessionActivityMap, sessionId: string): SessionActivityMap {
    const activity = current[sessionId];
    if (!activity) return current;
    if (activity.state === "completed-unseen") {
      this.markRunSeen(sessionId, activity.runtimeRunId);
    } else if (activity.state === "error-unseen") {
      this.markErrorAcknowledged(sessionId, activity.runtimeRunId);
    } else {
      return current;
    }
    return { ...current, [sessionId]: { state: "idle" } };
  }

  markError(
    current: SessionActivityMap,
    sessionId: string,
    runtimeRunId: string
  ): SessionActivityMap {
    const activity = current[sessionId];
    if (this.errors[sessionId] === runtimeRunId) {
      return activity?.state === "idle" ? current : {
        ...current,
        [sessionId]: { state: "idle" }
      };
    }
    if (activity?.state === "error-unseen" && activity.runtimeRunId === runtimeRunId) {
      return current;
    }
    return {
      ...current,
      [sessionId]: { state: "error-unseen", runtimeRunId }
    };
  }

  remove(current: SessionActivityMap, sessionId: string): SessionActivityMap {
    const { [sessionId]: _removed, ...next } = current;
    if (sessionId in this.seen) {
      delete this.seen[sessionId];
    }
    if (sessionId in this.errors) delete this.errors[sessionId];
    this.persist();
    return next;
  }

  private markRunSeen(sessionId: string, runtimeRunId: string): void {
    this.seen[sessionId] = runtimeRunId;
    const entries = Object.entries(this.seen);
    if (entries.length > 200) {
      this.seen = Object.fromEntries(entries.slice(-200));
    }
    this.persist();
  }

  private markErrorAcknowledged(sessionId: string, runtimeRunId: string): void {
    this.errors[sessionId] = runtimeRunId;
    const entries = Object.entries(this.errors);
    if (entries.length > 200) {
      this.errors = Object.fromEntries(entries.slice(-200));
    }
    this.persist();
  }

  private compact(sessions: RuntimeSessionSummary[]): void {
    const currentIds = new Set(sessions.map((session) => session.runtimeSessionId));
    this.seen = Object.fromEntries(
      Object.entries(this.seen)
        .filter(([sessionId]) => currentIds.has(sessionId))
        .slice(-200)
    );
    this.errors = Object.fromEntries(
      Object.entries(this.errors)
        .filter(([sessionId]) => currentIds.has(sessionId))
        .slice(-200)
    );
  }

  private persist(): void {
    if (!this.initialized) return;
    try {
      this.storage.setItem(this.key, JSON.stringify({
        version: 1,
        initialized: true,
        seen: this.seen,
        errors: this.errors
      } satisfies SeenRunEnvelope));
    } catch {
      // Storage can be unavailable in private browsing or under quota pressure.
    }
  }
}
