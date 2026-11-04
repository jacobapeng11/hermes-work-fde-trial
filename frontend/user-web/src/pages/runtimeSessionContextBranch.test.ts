import { describe, expect, it } from "vitest";
import type { RuntimeSessionSummary } from "@hermes-cloud/schemas";

import {
  contextRootSession,
  findContextBranch,
  contextHeadSession,
  sidebarVisibleSessions,
} from "./runtimeSessionContextBranch";

function session(
  runtimeSessionId: string,
  overrides: Partial<RuntimeSessionSummary> = {}
): RuntimeSessionSummary {
  return {
    sessionId: Number(runtimeSessionId.slice(-1)),
    runtimeWorkspaceId: 10,
    runtimeAgentProfileId: 1,
    runtimeSessionId,
    title: "市场调研",
    sourceType: "web",
    messageCount: 2,
    sessionModelEntryId: 3,
    status: "active",
    agentProfileKind: "default",
    ...overrides,
  };
}

describe("runtime session context branch", () => {
  it("resolves the logical personal root from nested branches", () => {
    const root = session("rs-10-1");
    const branch = session("rs-10-2", {
      contextSourceRuntimeSessionId: "rs-10-1",
      runtimeAgentProfileId: 7,
      agentProfileKind: "expert",
    });
    const nested = session("rs-10-3", {
      contextSourceRuntimeSessionId: "rs-10-2",
      runtimeAgentProfileId: 8,
      agentProfileKind: "expert",
    });

    expect(contextRootSession([root, branch, nested], nested)).toEqual(root);
  });

  it("reuses an expert or personal branch bound to the same source", () => {
    const source = session("rs-10-1");
    const branch = session("rs-10-2", {
      contextSourceRuntimeSessionId: "rs-10-1",
      runtimeAgentProfileId: 7,
      agentProfileKind: "expert",
    });
    const personalBranch = session("rs-10-4", {
      contextSourceRuntimeSessionId: "rs-10-3",
      runtimeAgentProfileId: 1,
      agentProfileKind: "default",
    });

    expect(findContextBranch([source, branch], "rs-10-1", 7)).toEqual(branch);
    expect(findContextBranch([source, branch], "rs-10-1", 8)).toBeNull();
    expect(findContextBranch([personalBranch], "rs-10-3", null)).toEqual(personalBranch);
  });

  it("resolves the persisted active context head", () => {
    const root = session("rs-10-1", {
      runtimeAgentProfileId: 1,
      agentProfileKind: "default",
      contextHeadRuntimeSessionId: "rs-10-3",
    });
    const head = session("rs-10-3", {
      contextSourceRuntimeSessionId: "rs-10-2",
      runtimeAgentProfileId: 8,
      agentProfileKind: "expert",
    });

    expect(contextHeadSession([root, head], root)).toEqual(head);
  });

  it("hides active branches from the sidebar but keeps deleted expert history", () => {
    const source = session("rs-10-1");
    const active = session("rs-10-2", {
      contextSourceRuntimeSessionId: "rs-10-1",
      runtimeAgentProfileId: 7,
    });
    const readOnly = session("rs-10-3", {
      contextSourceRuntimeSessionId: "rs-10-1",
      runtimeAgentProfileId: 8,
      status: "expert_deleted",
      isReadOnly: true,
    });

    expect(sidebarVisibleSessions([active, readOnly, source])).toEqual([
      readOnly,
      source,
    ]);
  });
});
