import type { RuntimeSessionSummary } from "@hermes-cloud/schemas";

export function contextRootSession(
  sessions: RuntimeSessionSummary[],
  session: RuntimeSessionSummary | null
): RuntimeSessionSummary | null {
  let current = session;
  const visited = new Set<string>();
  while (current?.contextSourceRuntimeSessionId) {
    if (visited.has(current.runtimeSessionId)) break;
    visited.add(current.runtimeSessionId);
    const parent = sessions.find(
      (item) => item.runtimeSessionId === current?.contextSourceRuntimeSessionId
    );
    if (!parent) break;
    current = parent;
  }
  return current;
}

export function findContextBranch(
  sessions: RuntimeSessionSummary[],
  sourceSessionId: string,
  runtimeAgentProfileId: number | null
): RuntimeSessionSummary | null {
  return sessions.find((item) => (
    item.contextSourceRuntimeSessionId === sourceSessionId
    && (
      runtimeAgentProfileId === null
        ? item.agentProfileKind === "default"
        : item.runtimeAgentProfileId === runtimeAgentProfileId
    )
    && item.status !== "deleting"
  )) ?? null;
}

export function contextHeadSession(
  sessions: RuntimeSessionSummary[],
  root: RuntimeSessionSummary | null
): RuntimeSessionSummary | null {
  if (!root?.contextHeadRuntimeSessionId) return root;
  return sessions.find((item) => (
    item.runtimeSessionId === root.contextHeadRuntimeSessionId
    && item.status !== "deleting"
  )) ?? root;
}

export function sidebarVisibleSessions(
  sessions: RuntimeSessionSummary[]
): RuntimeSessionSummary[] {
  return sessions.filter((item) => !item.contextSourceRuntimeSessionId || item.isReadOnly);
}
