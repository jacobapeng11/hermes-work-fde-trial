import type { RuntimeChatHistoryResponse } from "@hermes-cloud/schemas";
import { describe, expect, it } from "vitest";
import {
  initialChatHistoryLoadState,
  loadChatHistorySources,
  reduceChatHistoryLoad,
  stableHistoryMessageIdentity,
  type ChatHistoryLoadState,
  type ChatHistoryLoadTarget
} from "./chatHistoryLoader";

const target = (runtimeSessionId = "session-a", loadEpoch = 1): ChatHistoryLoadTarget => ({
  runtimeWorkspaceId: 1,
  runtimeSessionId,
  loadEpoch
});

const response = (
  source: "mysql" | "hermes",
  messages: RuntimeChatHistoryResponse["messages"],
  runtimeSessionId = "session-a"
): RuntimeChatHistoryResponse => ({
  runtimeWorkspaceId: 1,
  runtimeSessionId,
  source,
  consistency: source === "hermes" ? "authoritative" : "projection_preview",
  generatedAt: "2026-08-21T02:00:00.000Z",
  hasActiveRun: false,
  messages
});

function identities(state: ChatHistoryLoadState): string[] {
  return state.messages.map((message, index) => (
    stableHistoryMessageIdentity(message, state.target.runtimeSessionId, state.source ?? "mysql", index)
  ));
}

function expectStableMessageIdentitiesUnique(state: ChatHistoryLoadState): void {
  expect(new Set(identities(state)).size).toBe(state.messages.length);
}

function expectRunMessageSlotUnique(state: ChatHistoryLoadState): void {
  const slots = state.messages.flatMap((message) => {
    if (!message.runtimeRunId || !["user", "assistant"].includes(message.role)) return [];
    if (message.kind) return [];
    return [`${message.runtimeRunId}:${message.role}`];
  });
  expect(new Set(slots).size).toBe(slots.length);
}

describe("chatHistoryLoader", () => {
  it("shows MySQL preview and atomically replaces it with Hermes authority", async () => {
    let releaseAuthority: ((value: RuntimeChatHistoryResponse) => void) | undefined;
    const authority = new Promise<RuntimeChatHistoryResponse>((resolve) => {
      releaseAuthority = resolve;
    });
    const states: ChatHistoryLoadState[] = [];
    const operation = loadChatHistorySources({
      target: target(),
      preview: async () => response("mysql", [
        { role: "user", content: "问题", runtimeRunId: "run-1", messageSeq: 0 },
        { role: "assistant", content: "旧答案", runtimeRunId: "run-1", messageSeq: 1 }
      ]),
      authoritative: () => authority,
      publish: (state) => states.push(state)
    });

    await Promise.resolve();
    expect(states[states.length - 1]).toMatchObject({ phase: "preview", status: "syncing" });
    expect(states[states.length - 1]?.messages.map((message) => message.content)).toEqual(["问题", "旧答案"]);

    releaseAuthority?.(response("hermes", [
      { role: "user", content: "问题", runtimeRunId: "run-1" },
      { role: "assistant", content: "新答案", runtimeRunId: "run-1" }
    ]));
    const final = await operation;

    expect(final).toMatchObject({ phase: "authoritative", status: "ready", source: "hermes" });
    expect(final.messages.map((message) => message.content)).toEqual(["问题", "新答案"]);
    expectStableMessageIdentitiesUnique(final);
    expectRunMessageSlotUnique(final);
  });

  it("ignores a late preview after authority and remains idempotent", () => {
    const started = initialChatHistoryLoadState(target());
    const authoritative = reduceChatHistoryLoad(started, {
      type: "authoritative_loaded",
      target: target(),
      response: response("hermes", [
        { role: "user", content: "问题", runtimeRunId: "run-1" },
        { role: "assistant", content: "答案", runtimeRunId: "run-1" }
      ])
    });
    const afterPreview = reduceChatHistoryLoad(authoritative, {
      type: "preview_loaded",
      target: target(),
      response: response("mysql", [
        { role: "user", content: "问题", runtimeRunId: "run-1" }
      ])
    });
    const repeated = reduceChatHistoryLoad(afterPreview, {
      type: "authoritative_loaded",
      target: target(),
      response: response("hermes", [
        { role: "user", content: "问题", runtimeRunId: "run-1" },
        { role: "assistant", content: "答案", runtimeRunId: "run-1" }
      ])
    });

    expect(afterPreview).toBe(authoritative);
    expect(repeated.messages).toHaveLength(2);
    expectStableMessageIdentitiesUnique(repeated);
  });

  it("deduplicates stable identities but preserves equal text from different runs", () => {
    const state = reduceChatHistoryLoad(initialChatHistoryLoadState(target()), {
      type: "authoritative_loaded",
      target: target(),
      response: response("hermes", [
        { role: "user", content: "相同正文", runtimeRunId: "run-1", clientMessageId: "client-1" },
        { role: "user", content: "相同正文", runtimeRunId: "run-1", clientMessageId: "client-1" },
        { role: "assistant", content: "相同答案", runtimeRunId: "run-1" },
        { role: "assistant", content: "相同答案", runtimeRunId: "run-1" },
        { role: "user", content: "相同正文", runtimeRunId: "run-2", clientMessageId: "client-2" },
        { role: "assistant", content: "相同答案", runtimeRunId: "run-2" },
        { role: "assistant", kind: "reasoning", text: "分析", runtimeRunId: "run-2", eventSeq: 3 },
        { role: "assistant", kind: "reasoning", text: "分析", runtimeRunId: "run-2", eventSeq: 3 }
      ])
    });

    expect(state.messages).toHaveLength(5);
    expect(state.messages.filter((message) => message.content === "相同正文")).toHaveLength(2);
    expect(state.messages.filter((message) => message.content === "相同答案")).toHaveLength(2);
    expectStableMessageIdentitiesUnique(state);
    expectRunMessageSlotUnique(state);
  });

  it("ignores stale results from a different session or load epoch", () => {
    const current = initialChatHistoryLoadState(target("session-b", 2));
    const staleSession = reduceChatHistoryLoad(current, {
      type: "preview_loaded",
      target: target("session-a", 2),
      response: response("mysql", [{ role: "user", content: "A" }], "session-a")
    });
    const staleEpoch = reduceChatHistoryLoad(current, {
      type: "authoritative_loaded",
      target: target("session-b", 1),
      response: response("hermes", [{ role: "user", content: "旧 B" }], "session-b")
    });

    expect(staleSession).toBe(current);
    expect(staleEpoch).toBe(current);
  });

  it("keeps preview in degraded mode when Hermes fails", async () => {
    const final = await loadChatHistorySources({
      target: target(),
      preview: async () => response("mysql", [
        { role: "user", content: "可见历史", runtimeRunId: "run-1" }
      ]),
      authoritative: async () => { throw new Error("Hermes unavailable"); },
      publish: () => undefined
    });

    expect(final).toMatchObject({ phase: "degraded", status: "degraded", source: "mysql" });
    expect(final.messages).toHaveLength(1);
  });

  it("uses authority when MySQL fails and preserves cached content when both fail", async () => {
    const authority = await loadChatHistorySources({
      target: target(),
      preview: async () => { throw new Error("MySQL unavailable"); },
      authoritative: async () => response("hermes", [{ role: "assistant", content: "权威答案" }]),
      publish: () => undefined
    });
    expect(authority).toMatchObject({ phase: "authoritative", status: "ready" });

    const cached = [{ role: "assistant" as const, content: "缓存答案", runtimeRunId: "run-cache" }];
    const degraded = await loadChatHistorySources({
      target: target("session-a", 2),
      initialMessages: cached,
      preview: async () => { throw new Error("MySQL unavailable"); },
      authoritative: async () => { throw new Error("Hermes unavailable"); },
      publish: () => undefined
    });
    expect(degraded.messages).toEqual(cached);
    expect(degraded).toMatchObject({ phase: "degraded", status: "degraded" });
  });

  it("does not let an empty preview erase a non-empty cache", () => {
    const started = initialChatHistoryLoadState(target(), [
      { role: "assistant", content: "缓存答案", runtimeRunId: "run-cache" }
    ]);
    const preview = reduceChatHistoryLoad(started, {
      type: "preview_loaded",
      target: target(),
      response: response("mysql", [])
    });
    expect(preview.messages).toEqual(started.messages);
  });
});
