import { describe, expect, it } from "vitest";

import {
  QueueSnapshotVersionGate,
  reconcileChatQueueSnapshot,
  removeStartedQueuedRun
} from "./chatQueueProjection";

describe("chat queue projection", () => {
  it("sorts FIFO entries and removes the exact started run", () => {
    const snapshot = reconcileChatQueueSnapshot({
      queuedCount: 2,
      scheduledQueuedCount: 0,
      activeRun: null,
      queuedRuns: [
        { runId: "run-2", clientMessageId: "message-2", enqueueSeq: 2, prompt: "第二条", attachmentFileIds: ["9"] },
        { runId: "run-1", clientMessageId: "message-1", enqueueSeq: 1, prompt: "第一条", attachmentFileIds: [] }
      ]
    });

    expect(snapshot.queuedRuns.map((item) => item.runId)).toEqual(["run-1", "run-2"]);
    expect(removeStartedQueuedRun(snapshot, "run-1")).toMatchObject({
      queuedCount: 1,
      activeRun: { runId: "run-1" },
      queuedRuns: [{ runId: "run-2" }]
    });
  });

  it("rejects a late queue snapshot after a newer refresh for the same session", () => {
    const gate = new QueueSnapshotVersionGate();
    const first = gate.issue("session-1");
    const second = gate.issue("session-1");

    expect(gate.isCurrent("session-1", second)).toBe(true);
    expect(gate.isCurrent("session-1", first)).toBe(false);

    gate.invalidate("session-2");
    expect(gate.isCurrent("session-1", second)).toBe(false);
  });
});
