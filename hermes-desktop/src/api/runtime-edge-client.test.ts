import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  RuntimeEdgeClient,
  RuntimeEdgeTransportError,
  RUNTIME_EDGE_QUEUE_UPDATED_EVENT,
  formatRuntimeSessionPrepareError,
  type RuntimeEdgeBootstrap,
  type RuntimeEdgeGrantTarget
} from "./runtime-edge-client";

class FakeWebSocket extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static historySessionId = "session-1";

  readonly url: string;
  readonly protocols: string[];
  readonly sent: Record<string, unknown>[] = [];
  onSend?: (message: Record<string, unknown>) => void;
  readyState = FakeWebSocket.CONNECTING;
  autoQueueResponse = false;

  constructor(url: string | URL, protocols?: string | string[]) {
    super();
    this.url = String(url);
    this.protocols = Array.isArray(protocols)
      ? protocols
      : protocols
        ? [protocols]
        : [];
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.dispatchEvent(new Event("open"));
    });
  }

  send(raw: string): void {
    const message = JSON.parse(raw) as Record<string, unknown>;
    this.sent.push(message);
    this.onSend?.(message);
    if (message.type === "session.prepare") {
      queueMicrotask(() => this.serverMessage({
        schema_version: 1,
        type: "session.ready",
        request_id: message.request_id,
        session_id: "session-1",
        config_revision: "revision-1"
      }));
    }
    if (message.type === "session.history") {
      queueMicrotask(() => this.serverMessage({
        schema_version: 1,
        type: "session.history",
        request_id: message.request_id,
        session_id: FakeWebSocket.historySessionId,
        messages: [
          { role: "user", content: "hello_world" },
          { role: "assistant", content: "你好" }
        ]
      }));
    }
    if (message.type === "session.queue" && this.autoQueueResponse) {
      queueMicrotask(() => this.serverMessage({
        schema_version: 1,
        type: "session.queue",
        request_id: message.request_id,
        session_id: "session-1",
        queued_count: 0,
        scheduled_queued_count: 0,
        queued_runs: [],
        active_run: null
      }));
    }
  }

  close(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.dispatchEvent(new CloseEvent("close", { code: 1000 }));
  }

  serverMessage(payload: Record<string, unknown>): void {
    this.dispatchEvent(new MessageEvent("message", {
      data: JSON.stringify(payload)
    }));
  }
}

const grant: RuntimeEdgeGrantTarget = {
  schemaVersion: 1,
  runtimeId: "runtime-1",
  runtimeSessionId: "session-1",
  modelEntryId: 9,
  profileId: "profile-1",
  configRevision: "revision-1",
  sessionGrant: "session-grant",
  sessionGrantExpiresAt: "2099-01-01T00:00:00Z"
};

const bootstrap: RuntimeEdgeBootstrap = {
  ...grant,
  runtimeHandle: "runtime-1",
  websocketPath: "/runtime-edge/runtime-1/ws",
  websocketProtocol: "hermes-runtime-v1",
  runtimeAccess: "runtime-access",
  runtimeAccessExpiresAt: "2099-01-01T00:00:00Z"
};

describe("RuntimeEdgeClient", () => {
  const sockets: FakeWebSocket[] = [];

  beforeEach(() => {
    sockets.length = 0;
    FakeWebSocket.historySessionId = "session-1";
    vi.stubGlobal("WebSocket", class extends FakeWebSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        sockets.push(this);
      }
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("formats a safe session-prepare diagnostic without upstream details", () => {
    const error = new RuntimeEdgeTransportError(
      "Unknown provider custom:runtime-secret-route key=secret",
      "RUNTIME_SESSION_PREPARE_FAILED",
      true,
      {
        phase: "live_execution_release",
        auditEventId: "audit-prepare-0001"
      }
    );

    expect(formatRuntimeSessionPrepareError(error)).toBe(
      "运行环境准备失败（阶段：释放旧会话；诊断编号：audit-prepare-0001），请重试或联系管理员。"
    );
    expect(formatRuntimeSessionPrepareError(error)).not.toContain("custom:");
    expect(formatRuntimeSessionPrepareError(error)).not.toContain("secret");
  });

  it("opens the same-origin websocket and prepares a session", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );

    await client.prepare(1, "session-1", 9);

    expect(sockets).toHaveLength(1);
    expect(sockets[0]?.url).toBe(
      "ws://localhost:3000/runtime-edge/runtime-1/ws"
    );
    expect(sockets[0]?.protocols).toEqual([
      "hermes-runtime-v1",
      "runtime-access.runtime-access"
    ]);
    expect(sockets[0]?.sent[0]).toEqual(expect.objectContaining({
      type: "session.prepare",
      session_grant: "session-grant"
    }));
  });

  it("reuses a valid SessionGrant for hot newly submitted Runs", async () => {
    const refresh = vi.fn().mockResolvedValue(grant);
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      refresh
    );
    await client.prepare(1, "session-1", 9);
    const socket = sockets[0];
    if (!socket) throw new Error("expected websocket");
    let runIndex = 0;
    socket.onSend = (message) => {
      if (message.type !== "run.submit") return;
      runIndex += 1;
      queueMicrotask(() => {
        socket.serverMessage({
          schema_version: 1,
          type: "run.accepted",
          run_id: `run-refresh-${runIndex}`,
          client_message_id: message.client_message_id,
          accepted_at: "2026-08-24T00:00:00Z"
        });
        socket.serverMessage({
          schema_version: 1,
          type: "run.event",
          run_id: `run-refresh-${runIndex}`,
          seq: 1,
          event_type: "chat.done",
          emitted_at: "2026-08-24T00:00:01Z",
          data: { status: "completed" }
        });
      });
    };

    await client.streamRun({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "first",
      sourceType: "web",
      runtimeRunId: "message-refresh-1"
    }, () => undefined);
    await client.streamRun({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "second",
      sourceType: "web",
      runtimeRunId: "message-refresh-2"
    }, () => undefined);

    expect(refresh).toHaveBeenCalledTimes(0);
    expect(socket.sent.filter((item) => item.type === "session.prepare"))
      .toHaveLength(3);
  });

  it("refreshes once when a hot grant is rejected as stale", async () => {
    const refresh = vi.fn().mockResolvedValue(grant);
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      refresh
    );
    await client.prepare(1, "session-1", 9);
    const socket = sockets[0];
    if (!socket) throw new Error("expected websocket");

    let staleOnce = true;
    socket.onSend = (message) => {
      if (message.type === "session.prepare" && staleOnce) {
        staleOnce = false;
        queueMicrotask(() => socket.serverMessage({
          schema_version: 1,
          type: "error",
          request_id: message.request_id,
          code: "CONFIG_REVISION_STALE",
          message: "stale",
          retryable: true
        }));
        return;
      }
      if (message.type !== "run.submit") return;
      queueMicrotask(() => {
        socket.serverMessage({
          schema_version: 1,
          type: "run.accepted",
          run_id: "run-stale-retry",
          client_message_id: message.client_message_id,
          accepted_at: "2026-08-24T00:00:00Z"
        });
        socket.serverMessage({
          schema_version: 1,
          type: "run.event",
          run_id: "run-stale-retry",
          seq: 1,
          event_type: "chat.done",
          emitted_at: "2026-08-24T00:00:01Z",
          data: { status: "completed" }
        });
      });
    };

    await client.streamRun({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "retry",
      sourceType: "web",
      runtimeRunId: "message-stale-retry"
    }, () => undefined);

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(socket.sent.filter((item) => item.type === "session.prepare"))
      .toHaveLength(3);
  });

  it("refreshes and resubmits the same Run once when admission rejects a hot grant", async () => {
    const refresh = vi.fn().mockResolvedValue(grant);
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      refresh
    );
    await client.prepare(1, "session-1", 9);
    const socket = sockets[0];
    if (!socket) throw new Error("expected websocket");
    let submitCount = 0;
    socket.onSend = (message) => {
      if (message.type !== "run.submit") return;
      submitCount += 1;
      queueMicrotask(() => {
        if (submitCount === 1) {
          socket.serverMessage({
            schema_version: 1,
            type: "error",
            client_message_id: message.client_message_id,
            code: "CONFIG_REVISION_STALE",
            message: "stale",
            retryable: true
          });
          return;
        }
        socket.serverMessage({
          schema_version: 1,
          type: "run.accepted",
          run_id: "run-admission-retry",
          client_message_id: message.client_message_id,
          accepted_at: "2026-08-24T00:00:00Z"
        });
        socket.serverMessage({
          schema_version: 1,
          type: "run.event",
          run_id: "run-admission-retry",
          seq: 1,
          event_type: "chat.done",
          emitted_at: "2026-08-24T00:00:01Z",
          data: { status: "completed" }
        });
      });
    };

    await client.streamRun({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "retry after admission",
      sourceType: "web",
      runtimeRunId: "message-admission-retry"
    }, () => undefined);

    const submissions = socket.sent.filter((item) => item.type === "run.submit");
    expect(submissions).toHaveLength(2);
    expect(submissions[0]?.client_message_id)
      .toBe("message-admission-retry");
    expect(submissions[1]?.client_message_id)
      .toBe("message-admission-retry");
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("loads active history from Runtime Edge without backend streaming", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );

    const messages = await client.history(1, "session-1", 9);

    expect(messages).toEqual([
      { role: "user", content: "hello_world" },
      { role: "assistant", content: "你好" }
    ]);
    expect(sockets[0]?.sent.some((item) => item.type === "session.history"))
      .toBe(true);
  });

  it("rejects history returned for a different session", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );

    FakeWebSocket.historySessionId = "session-other";

    await expect(client.history(1, "session-1", 9)).rejects.toMatchObject({
      code: "RUNTIME_SESSION_HISTORY_MISMATCH"
    });
  });

  it("reads prepared history and queue without another session.prepare", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );

    await client.prepare(1, "session-1", 9);
    const socket = sockets[0];
    if (!socket) throw new Error("expected websocket");
    socket.onSend = (message) => {
      if (message.type !== "session.queue") return;
      queueMicrotask(() => socket.serverMessage({
        schema_version: 1,
        type: "session.queue",
        request_id: message.request_id,
        session_id: "session-1",
        queued_count: 1,
        scheduled_queued_count: 0
      }));
    };

    await Promise.all([
      client.historyPrepared(1, "session-1"),
      client.queueSummaryPrepared(1, "session-1")
    ]);

    expect(socket?.sent.filter((item) => item.type === "session.prepare")).toHaveLength(1);
    expect(socket?.sent.filter((item) => item.type === "session.history")).toHaveLength(1);
    expect(socket?.sent.filter((item) => item.type === "session.queue")).toHaveLength(1);
  });

  it("fails prepared-only reads for an unprepared or mismatched session", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );

    await expect(client.historyPrepared(1, "session-1")).rejects.toMatchObject({
      code: "RUNTIME_SESSION_NOT_PREPARED"
    });
    await client.prepare(1, "session-1", 9);
    await expect(client.queueSummaryPrepared(1, "session-2")).rejects.toMatchObject({
      code: "RUNTIME_SESSION_NOT_PREPARED"
    });
    expect(sockets[0]?.sent.filter((item) => item.type === "session.prepare")).toHaveLength(1);
  });

  it("publishes unsolicited queue updates without changing a run waiter", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const updates: unknown[] = [];
    const listener = (event: Event) => {
      updates.push((event as CustomEvent).detail);
    };
    window.addEventListener(RUNTIME_EDGE_QUEUE_UPDATED_EVENT, listener);

    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "session.queue.updated",
      session_id: "session-1",
      queued_count: 2,
      scheduled_queued_count: 1
    });

    expect(updates).toEqual([{
      runtimeSessionId: "session-1",
      queuedCount: 2,
      scheduledQueuedCount: 1,
      queuedRuns: [],
      activeRun: null
    }]);
    window.removeEventListener(RUNTIME_EDGE_QUEUE_UPDATED_EVENT, listener);
  });

  it("maps queued, accepted, and run events to the existing chat event contract", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const events: { event: string; data: Record<string, unknown> }[] = [];
    const stream = client.streamRun({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "hello",
      sourceType: "web",
      runtimeRunId: "web-message-0001"
    }, (event) => events.push(event));
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.some((item) => item.type === "run.submit"))
        .toBe(true);
    });

    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.queued",
      run_id: "run-00000001",
      client_message_id: "web-message-0001",
      enqueued_at: "2026-08-07T00:00:00Z",
      enqueue_seq: 1
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.accepted",
      run_id: "run-00000001",
      client_message_id: "web-message-0001",
      accepted_at: "2026-08-07T00:00:00Z"
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-00000001",
      seq: 1,
      event_type: "chat.message.delta",
      emitted_at: "2026-08-07T00:00:01Z",
      data: { runtime_session_id: "session-1", delta: "你好" }
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-00000001",
      seq: 2,
      event_type: "chat.done",
      emitted_at: "2026-08-07T00:00:02Z",
      data: { status: "completed" }
    });
    await stream;

    expect(events.map((event) => event.event)).toEqual([
      "chat.run.queued",
      "chat.run.accepted",
      "chat.message.delta",
      "chat.done"
    ]);
    expect(events[2]?.data).toEqual(expect.objectContaining({
      runtimeSessionId: "session-1",
      runtimeRunId: "run-00000001",
      delta: "你好",
      seq: 1
    }));
  });

  it("maps an interrupt replacement turn from queued to accepted streaming", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const events: string[] = [];
    const replacement = client.submitCorrection({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "停止，改查这份报告",
      sourceType: "web",
      parentRuntimeRunId: "run-parent",
      runtimeRunId: "replacement-message-0001"
    }, (event) => events.push(event.event));
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.some((item) => (
        item.type === "followup.submit"
        && item.client_message_id === "replacement-message-0001"
        && item.parent_run_id === "run-parent"
      ))).toBe(true);
    });

    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.queued",
      run_id: "run-replacement-0001",
      client_message_id: "replacement-message-0001",
      enqueued_at: "2026-10-14T00:00:00Z",
      enqueue_seq: 2
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.accepted",
      run_id: "run-replacement-0001",
      client_message_id: "replacement-message-0001",
      accepted_at: "2026-10-14T00:00:01Z"
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-replacement-0001",
      seq: 1,
      event_type: "chat.done",
      emitted_at: "2026-10-14T00:00:02Z",
      data: { status: "completed" }
    });

    await expect(replacement).resolves.toEqual({
      kind: "continued",
      clientMessageId: "replacement-message-0001",
      continuationRunId: "run-replacement-0001"
    });
    expect(events).toEqual([
      "chat.run.queued",
      "chat.run.accepted",
      "chat.done"
    ]);
  });

  it("keeps two redirect disposition waiters separate from the parent Run waiter", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const parentEvents: string[] = [];
    const parent = client.resumeRun({
      runtimeRunId: "run-parent",
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      afterSeq: 0
    }, (event) => parentEvents.push(event.event));
    const firstEvents: string[] = [];
    const secondEvents: string[] = [];
    const first = client.submitCorrection({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "先查日志",
      sourceType: "web",
      parentRuntimeRunId: "run-parent",
      attachmentFileIds: [91],
      runtimeRunId: "correction-1"
    }, (event) => firstEvents.push(event.event));
    const second = client.submitCorrection({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "再对比配置",
      sourceType: "web",
      parentRuntimeRunId: "run-parent",
      runtimeRunId: "correction-2"
    }, (event) => secondEvents.push(event.event));
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.filter((item) => item.type === "followup.submit"))
        .toHaveLength(2);
    });
    expect(sockets[0]?.sent.find((item) => (
      item.client_message_id === "correction-1"
    ))).toMatchObject({
      type: "followup.submit",
      parent_run_id: "run-parent",
      attachment_file_ids: ["91"]
    });

    const identity = (
      clientMessageId: string,
      redirectId: string,
      eventSeq: number
    ) => ({
      schema_version: 1,
      redirect_id: redirectId,
      client_message_id: clientMessageId,
      parent_run_id: "run-parent",
      input_admission_seq: eventSeq + 100,
      dispatch_group_id: `group-${redirectId}`,
      dispatch_group_position: 0,
      event_seq: eventSeq,
      start_after_seq: eventSeq
    });
    sockets[0]?.serverMessage({
      type: "run.redirect_accepted",
      ...identity("correction-1", "redirect-1", 1)
    });
    sockets[0]?.serverMessage({
      type: "run.redirect_accepted",
      ...identity("correction-2", "redirect-2", 2)
    });
    sockets[0]?.serverMessage({
      type: "run.redirect_consumed",
      ...identity("correction-1", "redirect-1", 3)
    });
    sockets[0]?.serverMessage({
      type: "run.redirect_consumed",
      ...identity("correction-2", "redirect-2", 4)
    });

    await expect(first).resolves.toMatchObject({
      kind: "applied",
      clientMessageId: "correction-1",
      parentRunId: "run-parent"
    });
    await expect(second).resolves.toMatchObject({
      kind: "applied",
      clientMessageId: "correction-2",
      parentRunId: "run-parent"
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-parent",
      seq: 5,
      event_type: "chat.message.delta",
      emitted_at: "2026-08-15T00:00:00Z",
      data: { delta: "父 Run 继续输出" }
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-parent",
      seq: 6,
      event_type: "chat.done",
      emitted_at: "2026-08-15T00:00:01Z",
      data: { status: "completed" }
    });
    await parent;

    expect(firstEvents).toEqual([
      "run.redirect_accepted",
      "run.redirect_consumed"
    ]);
    expect(secondEvents).toEqual([
      "run.redirect_accepted",
      "run.redirect_consumed"
    ]);
    expect(parentEvents).toEqual(["chat.message.delta", "chat.done"]);
  });

  it("consumes durable follow-up dispositions from the parent run event stream", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const events: string[] = [];
    const correction = client.submitCorrection({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "补充检查配置",
      sourceType: "web",
      parentRuntimeRunId: "run-parent",
      runtimeRunId: "followup-live-events"
    }, (event) => events.push(event.event));
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.some((item) => item.type === "followup.submit"))
        .toBe(true);
    });
    const data = {
      followup_id: "followup-live-events",
      client_message_id: "followup-live-events",
      parent_run_id: "run-parent",
      input_admission_seq: 8,
      dispatch_group_id: "group-live-events",
      dispatch_group_position: 0,
      event_seq: 2,
      start_after_seq: 2
    };
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "followup.received",
      ...data
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-parent",
      seq: 2,
      event_type: "followup.received",
      emitted_at: "2026-08-15T00:00:00Z",
      data
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-parent",
      seq: 3,
      event_type: "followup.applying",
      emitted_at: "2026-08-15T00:00:01Z",
      data: { ...data, event_seq: 3, start_after_seq: 3 }
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-parent",
      seq: 4,
      event_type: "followup.applied",
      emitted_at: "2026-08-15T00:00:02Z",
      data: {
        ...data,
        event_seq: 4,
        start_after_seq: 4,
        proof_kind: "canonical_history",
        proof_digest: `sha256:${"a".repeat(64)}`
      }
    });

    await expect(correction).resolves.toMatchObject({
      kind: "applied",
      clientMessageId: "followup-live-events",
      parentRunId: "run-parent"
    });
    expect(events).toEqual([
      "followup.received",
      "followup.applying",
      "followup.applied"
    ]);
  });

  it("keeps the original parent when a late follow-up joins an active continuation", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const correction = client.submitCorrection({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "迟到但仍进入同一延续任务",
      sourceType: "web",
      parentRuntimeRunId: "run-original-parent",
      runtimeRunId: "followup-active-continuation"
    }, vi.fn());
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.some((item) => item.type === "followup.submit"))
        .toBe(true);
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-one-continuation",
      seq: 9,
      event_type: "followup.continued",
      emitted_at: "2026-08-15T00:00:02Z",
      data: {
        followup_id: "followup-active-continuation",
        client_message_id: "followup-active-continuation",
        parent_run_id: "run-original-parent",
        input_admission_seq: 19,
        dispatch_group_id: "group-active-continuation",
        dispatch_group_position: 0,
        event_seq: 9,
        start_after_seq: 8,
        continuation_run_id: "run-one-continuation",
        member_client_message_ids: ["followup-active-continuation"]
      }
    });

    await expect(correction).resolves.toMatchObject({
      kind: "continued",
      clientMessageId: "followup-active-continuation",
      parentRunId: "run-original-parent",
      continuationRunId: "run-one-continuation"
    });
  });

  it("resumes a promoted redirect exactly once and waits for chat.run.started", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const events: string[] = [];
    const correction = client.submitCorrection({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "改成下一任务",
      sourceType: "web",
      parentRuntimeRunId: "run-parent",
      runtimeRunId: "correction-promoted"
    }, (event) => events.push(event.event));
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.some((item) => item.type === "followup.submit"))
        .toBe(true);
    });
    const promoted = {
      schema_version: 1,
      type: "run.redirect_promoted",
      redirect_id: "redirect-promoted",
      client_message_id: "correction-promoted",
      parent_run_id: "run-parent",
      input_admission_seq: 11,
      dispatch_group_id: null,
      dispatch_group_position: null,
      event_seq: 7,
      start_after_seq: null,
      promoted_run_id: "run-promoted",
      promoted_enqueue_seq: 11
    };
    sockets[0]?.serverMessage(promoted);
    sockets[0]?.serverMessage(promoted);

    await expect(correction).resolves.toMatchObject({
      kind: "continued",
      continuationRunId: "run-promoted"
    });
    expect(sockets[0]?.sent.filter((item) => (
      item.type === "run.resume" && item.run_id === "run-promoted"
    ))).toHaveLength(1);
    expect(events).toEqual(["run.redirect_promoted"]);

    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-promoted",
      seq: 1,
      event_type: "chat.run.started",
      emitted_at: "2026-08-15T00:00:00Z",
      data: {}
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-promoted",
      seq: 2,
      event_type: "chat.done",
      emitted_at: "2026-08-15T00:00:01Z",
      data: { status: "completed" }
    });
    await vi.waitFor(() => {
      expect(events).toEqual([
        "run.redirect_promoted",
        "chat.run.started",
        "chat.done"
      ]);
    });
  });

  it("migrates an idle-race correction to an ordinary Run without a second resume", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const events: string[] = [];
    const correction = client.submitCorrection({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "竞态后作为普通任务",
      sourceType: "web",
      parentRuntimeRunId: "run-parent",
      runtimeRunId: "correction-idle-race"
    }, (event) => events.push(event.event));
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.some((item) => item.type === "followup.submit"))
        .toBe(true);
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.accepted",
      run_id: "run-idle-race",
      client_message_id: "correction-idle-race",
      accepted_at: "2026-08-15T00:00:00Z"
    });

    await expect(correction).resolves.toMatchObject({
      kind: "continued",
      clientMessageId: "correction-idle-race",
      continuationRunId: "run-idle-race"
    });
    expect(sockets[0]?.sent.filter((item) => (
      item.type === "run.resume" && item.run_id === "run-idle-race"
    ))).toHaveLength(0);
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-idle-race",
      seq: 1,
      event_type: "chat.run.started",
      emitted_at: "2026-08-15T00:00:01Z",
      data: {}
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-idle-race",
      seq: 2,
      event_type: "chat.done",
      emitted_at: "2026-08-15T00:00:02Z",
      data: { status: "completed" }
    });
    await vi.waitFor(() => expect(events).toEqual([
      "chat.run.accepted",
      "chat.run.started",
      "chat.done"
    ]));
  });

  it("rejects raw Hermes steered or queued events as redirect protocol errors", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const events: string[] = [];
    const correction = client.submitCorrection({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "修正",
      sourceType: "web",
      parentRuntimeRunId: "run-parent",
      runtimeRunId: "correction-protocol-error"
    }, (event) => events.push(event.event));
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.some((item) => item.type === "followup.submit"))
        .toBe(true);
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-parent",
      seq: 1,
      event_type: "steered",
      emitted_at: "2026-08-15T00:00:00Z",
      data: { client_message_id: "correction-protocol-error" }
    });

    await expect(correction).rejects.toMatchObject({
      code: "RUNTIME_REDIRECT_PROTOCOL_ERROR"
    });
    expect(events).toEqual([]);
  });

  it("settles uncertain and canceled redirect members independently", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const uncertainEvents: string[] = [];
    const canceledEvents: string[] = [];
    const uncertain = client.submitCorrection({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "uncertain",
      sourceType: "web",
      parentRuntimeRunId: "run-parent",
      runtimeRunId: "correction-uncertain"
    }, (event) => uncertainEvents.push(event.event));
    const canceled = client.submitCorrection({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "canceled",
      sourceType: "web",
      parentRuntimeRunId: "run-parent",
      runtimeRunId: "correction-canceled"
    }, (event) => canceledEvents.push(event.event));
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.filter((item) => item.type === "followup.submit"))
        .toHaveLength(2);
    });
    const pending = (clientMessageId: string, redirectId: string, seq: number) => ({
      schema_version: 1,
      type: "run.redirect_pending",
      redirect_id: redirectId,
      client_message_id: clientMessageId,
      parent_run_id: "run-parent",
      input_admission_seq: seq + 20,
      dispatch_group_id: null,
      dispatch_group_position: null,
      event_seq: seq,
      start_after_seq: null
    });
    sockets[0]?.serverMessage(pending(
      "correction-uncertain",
      "redirect-uncertain",
      1
    ));
    sockets[0]?.serverMessage(pending(
      "correction-canceled",
      "redirect-canceled",
      2
    ));
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.steer_submission_uncertain",
      redirect_id: "redirect-uncertain",
      client_message_id: "correction-uncertain",
      parent_run_id: "run-parent",
      input_admission_seq: 21,
      dispatch_group_id: "group-uncertain",
      dispatch_group_position: 0,
      event_seq: 3,
      start_after_seq: 3,
      reason_code: "REDIRECT_DELIVERY_UNPROVEN"
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.redirect_canceled",
      redirect_id: "redirect-canceled",
      client_message_id: "correction-canceled",
      parent_run_id: "run-parent",
      input_admission_seq: 22,
      dispatch_group_id: null,
      dispatch_group_position: null,
      event_seq: 4,
      start_after_seq: null,
      cancel_reason: "PARENT_RUN_CANCELED"
    });

    await expect(uncertain).resolves.toMatchObject({ kind: "failed" });
    await expect(canceled).resolves.toMatchObject({ kind: "failed" });
    expect(uncertainEvents).toEqual([
      "run.redirect_pending",
      "run.steer_submission_uncertain"
    ]);
    expect(canceledEvents).toEqual([
      "run.redirect_pending",
      "run.redirect_canceled"
    ]);
  });

  it("turns a terminal replay snapshot into chat.done", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const events: string[] = [];
    const resumed = client.resumeRun({
      runtimeRunId: "run-00000002",
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      afterSeq: 4
    }, (event) => events.push(event.event));
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.some((item) => item.type === "run.resume"))
        .toBe(true);
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.snapshot",
      run_id: "run-00000002",
      through_seq: 4,
      status: "COMPLETED",
      answer: "done",
      events: []
    });

    await resumed;
    expect(events).toEqual(["chat.done"]);
  });

  it("reports queue state while a resumed run is still waiting", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const events: string[] = [];
    const resumed = client.resumeRun({
      runtimeRunId: "run-queued-resume",
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      afterSeq: 0
    }, (event) => events.push(event.event));
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.some((item) => item.type === "run.resume"))
        .toBe(true);
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.queued",
      run_id: "run-queued-resume",
      client_message_id: "scheduled-message",
      enqueued_at: "2026-08-14T00:00:00Z",
      enqueue_seq: 7
    });
    await Promise.resolve();
    expect(events).toEqual(["chat.run.queued"]);

    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.snapshot",
      run_id: "run-queued-resume",
      through_seq: 0,
      status: "CANCELED",
      answer: "",
      events: []
    });
    await resumed;
  });

  it("preserves event order inside a coalesced runtime frame", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const deltas: string[] = [];
    const stream = client.streamRun({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "hello",
      sourceType: "web",
      runtimeRunId: "web-message-0003"
    }, (event) => {
      if (event.event === "chat.activity.delta") {
        deltas.push(String(event.data.delta));
      }
    });
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.some((item) => item.type === "run.submit"))
        .toBe(true);
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.accepted",
      run_id: "run-00000003",
      client_message_id: "web-message-0003",
      accepted_at: "2026-08-07T00:00:00Z"
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event_batch",
      events: [
        {
          schema_version: 1,
          type: "run.event",
          run_id: "run-00000003",
          seq: 1,
          event_type: "chat.activity.delta",
          emitted_at: "2026-08-07T00:00:01Z",
          data: { delta: "你" }
        },
        {
          schema_version: 1,
          type: "run.event",
          run_id: "run-00000003",
          seq: 2,
          event_type: "chat.activity.delta",
          emitted_at: "2026-08-07T00:00:01Z",
          data: { delta: "好" }
        },
        {
          schema_version: 1,
          type: "run.event",
          run_id: "run-00000003",
          seq: 3,
          event_type: "chat.done",
          emitted_at: "2026-08-07T00:00:02Z",
          data: { status: "completed" }
        }
      ]
    });

    await stream;
    expect(deltas).toEqual(["你", "好"]);
  });

  it("correlates a pre-accept error to the exact concurrent submission", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    const first = client.streamRun({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "first",
      sourceType: "web",
      runtimeRunId: "web-message-0004"
    }, () => undefined);
    const second = client.streamRun({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "second",
      sourceType: "web",
      runtimeRunId: "web-message-0005"
    }, () => undefined);
    const secondOutcome = second.catch((reason: Error) => reason);
    await vi.waitFor(() => {
      expect(sockets[0]?.sent.filter((item) => item.type === "run.submit"))
        .toHaveLength(2);
    });

    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "error",
      code: "SESSION_BUSY",
      message: "当前会话正在回复中",
      retryable: false,
      client_message_id: "web-message-0005"
    });
    const failure = await secondOutcome;
    expect(failure).toBeInstanceOf(Error);
    expect((failure as Error).message).toBe("当前会话正在回复中");

    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.accepted",
      run_id: "run-00000004",
      client_message_id: "web-message-0004",
      accepted_at: "2026-08-07T00:00:00Z"
    });
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.event",
      run_id: "run-00000004",
      seq: 1,
      event_type: "chat.done",
      emitted_at: "2026-08-07T00:00:01Z",
      data: { status: "completed" }
    });
    await first;
  });

  it("settles a run when the socket replies synchronously from send", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    sockets[0]!.onSend = (message) => {
      if (message.type !== "run.submit") return;
      sockets[0]?.serverMessage({
        schema_version: 1,
        type: "run.accepted",
        run_id: "run-00000006",
        client_message_id: message.client_message_id,
        accepted_at: "2026-08-07T00:00:00Z"
      });
      sockets[0]?.serverMessage({
        schema_version: 1,
        type: "run.event",
        run_id: "run-00000006",
        seq: 1,
        event_type: "chat.done",
        emitted_at: "2026-08-07T00:00:01Z",
        data: { status: "completed" }
      });
    };

    await client.streamRun({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "synchronous",
      sourceType: "web",
      runtimeRunId: "web-message-0006"
    }, () => undefined);
  });

  it("reconnects and resumes an accepted run after the live socket closes", async () => {
    vi.useFakeTimers();
    try {
      const bootstrapRuntime = vi.fn().mockResolvedValue(bootstrap);
      const client = new RuntimeEdgeClient(
        bootstrapRuntime,
        vi.fn().mockResolvedValue(grant)
      );
      await client.prepare(1, "session-1", 9);
      const events: string[] = [];
      const stream = client.streamRun({
        runtimeWorkspaceId: 1,
        runtimeSessionId: "session-1",
        modelEntryId: 9,
        message: "recover accepted",
        sourceType: "desktop",
        runtimeRunId: "desktop-message-accepted-close"
      }, (event) => events.push(event.event));
      await vi.waitFor(() => {
        expect(sockets[0]?.sent.some((item) => item.type === "run.submit")).toBe(true);
      });
      sockets[0]?.serverMessage({
        schema_version: 1,
        type: "run.accepted",
        run_id: "run-accepted-close",
        client_message_id: "desktop-message-accepted-close",
        accepted_at: "2026-11-02T00:00:00Z"
      });
      sockets[0]?.serverMessage({
        schema_version: 1,
        type: "run.event",
        run_id: "run-accepted-close",
        seq: 3,
        event_type: "chat.activity.delta",
        emitted_at: "2026-11-02T00:00:01Z",
        data: { delta: "partial" }
      });
      sockets[0]!.close();

      await vi.advanceTimersByTimeAsync(1_000);

      expect(sockets).toHaveLength(2);
      expect(sockets[1]?.sent).toContainEqual(expect.objectContaining({
        type: "run.resume",
        run_id: "run-accepted-close",
        after_seq: 3
      }));
      sockets[1]?.serverMessage({
        schema_version: 1,
        type: "run.event",
        run_id: "run-accepted-close",
        seq: 4,
        event_type: "chat.done",
        emitted_at: "2026-11-02T00:00:02Z",
        data: { status: "completed" }
      });
      await stream;
      expect(events).toContain("chat.transport.reconnecting");
      expect(events).toContain("chat.transport.resumed");
      expect(bootstrapRuntime).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("adopts a durable queued submission without submitting it twice", async () => {
    vi.useFakeTimers();
    try {
      const client = new RuntimeEdgeClient(
        vi.fn().mockResolvedValue(bootstrap),
        vi.fn().mockResolvedValue(grant)
      );
      await client.prepare(1, "session-1", 9);
      const stream = client.streamRun({
        runtimeWorkspaceId: 1,
        runtimeSessionId: "session-1",
        modelEntryId: 9,
        message: "recover uncertain",
        sourceType: "desktop",
        runtimeRunId: "desktop-message-uncertain-close"
      }, () => undefined);
      const outcome = stream.catch((reason: unknown) => reason);
      await vi.waitFor(() => {
        expect(sockets[0]?.sent.some((item) => item.type === "run.submit")).toBe(true);
      });
      sockets[0]!.close();

      await vi.advanceTimersByTimeAsync(1_000);
      const queueRequest = sockets[1]?.sent.find((item) => item.type === "session.queue");
      expect(queueRequest).toBeTruthy();
      sockets[1]?.serverMessage({
        schema_version: 1,
        type: "session.queue",
        request_id: queueRequest?.request_id,
        session_id: "session-1",
        queued_count: 1,
        scheduled_queued_count: 0,
        queued_runs: [{
          run_id: "run-uncertain-close",
          client_message_id: "desktop-message-uncertain-close",
          enqueue_seq: 1,
          prompt: "recover uncertain",
          attachment_file_ids: []
        }],
        active_run: null
      });
      await vi.advanceTimersByTimeAsync(0);
      expect(sockets.flatMap((socket) => socket.sent.filter((item) => item.type === "run.submit")))
        .toHaveLength(1);
      expect(sockets[1]?.sent).toContainEqual(expect.objectContaining({
        type: "run.resume",
        run_id: "run-uncertain-close",
        after_seq: 0
      }));
      sockets[1]?.serverMessage({
        schema_version: 1,
        type: "run.event",
        run_id: "run-uncertain-close",
        seq: 1,
        event_type: "chat.done",
        emitted_at: "2026-11-02T00:00:02Z",
        data: { status: "completed" }
      });
      await expect(outcome).resolves.toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("resubmits an unproven submission with the same client message id", async () => {
    vi.useFakeTimers();
    try {
      const client = new RuntimeEdgeClient(
        vi.fn().mockResolvedValue(bootstrap),
        vi.fn().mockResolvedValue(grant)
      );
      await client.prepare(1, "session-1", 9);
      const stream = client.streamRun({
        runtimeWorkspaceId: 1,
        runtimeSessionId: "session-1",
        modelEntryId: 9,
        message: "resubmit uncertain",
        sourceType: "desktop",
        runtimeRunId: "desktop-message-resubmit-close"
      }, () => undefined);
      await vi.waitFor(() => {
        expect(sockets[0]?.sent.some((item) => item.type === "run.submit")).toBe(true);
      });
      sockets[0]!.close();

      await vi.advanceTimersByTimeAsync(1_000);
      const queueRequest = sockets[1]?.sent.find((item) => item.type === "session.queue");
      expect(queueRequest).toBeTruthy();
      sockets[1]?.serverMessage({
        schema_version: 1,
        type: "session.queue",
        request_id: queueRequest?.request_id,
        session_id: "session-1",
        queued_count: 0,
        scheduled_queued_count: 0,
        queued_runs: [],
        active_run: null
      });
      await vi.advanceTimersByTimeAsync(0);
      const submissions = sockets.flatMap((socket) => socket.sent.filter((item) => item.type === "run.submit"));
      expect(submissions).toHaveLength(2);
      expect(new Set(submissions.map((item) => item.client_message_id))).toEqual(
        new Set(["desktop-message-resubmit-close"])
      );
      sockets[1]?.serverMessage({
        schema_version: 1,
        type: "run.accepted",
        run_id: "run-resubmit-close",
        client_message_id: "desktop-message-resubmit-close",
        accepted_at: "2026-11-02T00:00:00Z"
      });
      sockets[1]?.serverMessage({
        schema_version: 1,
        type: "run.event",
        run_id: "run-resubmit-close",
        seq: 1,
        event_type: "chat.done",
        emitted_at: "2026-11-02T00:00:01Z",
        data: { status: "completed" }
      });
      await stream;
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops reconnecting when an uncertain submission is aborted", async () => {
    vi.useFakeTimers();
    try {
      const bootstrapRuntime = vi.fn().mockResolvedValue(bootstrap);
      const client = new RuntimeEdgeClient(
        bootstrapRuntime,
        vi.fn().mockResolvedValue(grant)
      );
      await client.prepare(1, "session-1", 9);
      const controller = new AbortController();
      const outcome = client.streamRun({
        runtimeWorkspaceId: 1,
        runtimeSessionId: "session-1",
        modelEntryId: 9,
        message: "abort recovery",
        sourceType: "desktop",
        runtimeRunId: "desktop-message-abort-close"
      }, () => undefined, controller.signal).catch((reason: unknown) => reason);
      await vi.waitFor(() => {
        expect(sockets[0]?.sent.some((item) => item.type === "run.submit")).toBe(true);
      });
      sockets[0]!.close();
      controller.abort();
      await expect(outcome).resolves.toMatchObject({ name: "AbortError" });

      await vi.advanceTimersByTimeAsync(30_000);
      expect(sockets).toHaveLength(1);
      expect(bootstrapRuntime).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels on an open socket without preparing the session again", async () => {
    const refresh = vi.fn().mockResolvedValue(grant);
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      refresh
    );
    await client.prepare(1, "session-1", 9);
    sockets[0]!.sent.length = 0;
    sockets[0]!.onSend = (message) => {
      if (message.type !== "run.cancel") return;
      sockets[0]?.serverMessage({
        schema_version: 1,
        type: "run.cancel_ack",
        run_id: message.run_id,
        status: "accepted"
      });
    };

    const result = await client.cancelRun({
      runtimeRunId: "run-cancel-open",
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9
    });

    expect(result.status).toBe("canceling");
    expect(sockets[0]?.sent).toEqual([expect.objectContaining({
      type: "run.cancel",
      run_id: "run-cancel-open"
    })]);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("bootstraps a closed socket for cancel without session.prepare", async () => {
    const bootstrapRuntime = vi.fn().mockResolvedValue(bootstrap);
    const refresh = vi.fn().mockResolvedValue(grant);
    const client = new RuntimeEdgeClient(bootstrapRuntime, refresh);
    const resultPromise = client.cancelRun({
      runtimeRunId: "run-cancel-bootstrap",
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9
    });
    await vi.waitFor(() => expect(sockets).toHaveLength(1));
    await vi.waitFor(() => expect(sockets[0]?.sent).toEqual([
      expect.objectContaining({ type: "run.cancel" })
    ]));
    sockets[0]?.serverMessage({
      schema_version: 1,
      type: "run.cancel_ack",
      run_id: "run-cancel-bootstrap",
      status: "accepted"
    });
    const result = await resultPromise;

    expect(result.status).toBe("canceling");
    expect(sockets[0]?.sent.some((message) => message.type === "session.prepare"))
      .toBe(false);
    expect(bootstrapRuntime).toHaveBeenCalledOnce();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("uses a real terminal event as the compatibility cancel confirmation", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    sockets[0]!.onSend = (message) => {
      if (message.type !== "run.cancel") return;
      sockets[0]?.serverMessage({
        schema_version: 1,
        type: "run.event",
        run_id: message.run_id,
        seq: 9,
        event_type: "chat.done",
        emitted_at: "2026-08-13T00:00:00Z",
        data: { status: "canceled" }
      });
    };

    const result = await client.cancelRun({
      runtimeRunId: "run-cancel-terminal",
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1"
    });

    expect(result.status).toBe("canceled");
  });

  it("returns an explicit cancel failure from Runtime", async () => {
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    sockets[0]!.onSend = (message) => {
      if (message.type !== "run.cancel") return;
      sockets[0]?.serverMessage({
        schema_version: 1,
        type: "run.cancel_ack",
        run_id: message.run_id,
        status: "failed"
      });
    };

    const result = await client.cancelRun({
      runtimeRunId: "run-cancel-failed",
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1"
    });

    expect(result.status).toBe("cancel_failed");
  });

  it("fails visibly when neither cancel ack nor terminal evidence arrives", async () => {
    vi.useFakeTimers();
    try {
      const client = new RuntimeEdgeClient(
        vi.fn().mockResolvedValue(bootstrap),
        vi.fn().mockResolvedValue(grant)
      );
      await client.prepare(1, "session-1", 9);
      const result = client.cancelRun({
        runtimeRunId: "run-cancel-timeout",
        runtimeWorkspaceId: 1,
        runtimeSessionId: "session-1"
      });
      const outcome = result.catch((reason: unknown) => reason);
      await vi.advanceTimersByTimeAsync(10_000);

      await expect(outcome).resolves.toMatchObject({
        code: "RUNTIME_CANCEL_UNCONFIRMED",
        message: "停止请求未送达，任务仍在执行，请重试"
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("executes a Runtime local capability request through the Electron bridge", async () => {
    const requestCapability = vi.fn().mockResolvedValue({
      requestId: "local-request-1",
      ok: true,
      value: "hello from desktop"
    });
    Object.defineProperty(window, "coworkDesktop", {
      configurable: true,
      value: { requestCapability }
    });
    const callback = vi.fn();
    const client = new RuntimeEdgeClient(
      vi.fn().mockResolvedValue(bootstrap),
      vi.fn().mockResolvedValue(grant)
    );
    await client.prepare(1, "session-1", 9);
    sockets[0]!.onSend = (message) => {
      if (message.type === "run.submit") {
        sockets[0]?.serverMessage({
          schema_version: 1,
          type: "run.accepted",
          run_id: "run-local-capability",
          client_message_id: message.client_message_id,
          accepted_at: "2026-08-13T00:00:00Z"
        });
        sockets[0]?.serverMessage({
          schema_version: 1,
          type: "local.capability.request",
          request_id: "local-request-1",
          run_id: "run-local-capability",
          session_id: "session-1",
          capability: "workspace.read",
          args: { path: "README.md" },
          reason: "读取用户选定的工作文件夹"
        });
      }
      if (message.type === "local.capability.result") {
        expect(message.ok).toBe(true);
        expect(message.value).toBe("hello from desktop");
        sockets[0]?.serverMessage({
          schema_version: 1,
          type: "run.event",
          run_id: "run-local-capability",
          seq: 1,
          event_type: "chat.done",
          emitted_at: "2026-08-13T00:00:00Z",
          data: { status: "completed" }
        });
      }
    };
    await client.streamRun({
      runtimeWorkspaceId: 1,
      runtimeSessionId: "session-1",
      modelEntryId: 9,
      message: "读取 README",
      sourceType: "desktop"
    }, callback);

    expect(requestCapability).toHaveBeenCalledWith({
      requestId: "local-request-1",
      capability: "workspace.read",
      args: { path: "README.md" }
    });
    expect(callback).toHaveBeenCalledWith(expect.objectContaining({ event: "local.capability.request" }));
    expect(callback).toHaveBeenCalledWith(expect.objectContaining({ event: "local.capability.result" }));
  });
});
