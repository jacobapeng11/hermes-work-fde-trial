import { describe, expect, it, vi } from "vitest";
import { SessionSubmissionGate } from "./sessionSubmissionGate";

describe("SessionSubmissionGate", () => {
  it("does not let a later submission overtake an unresolved admission", async () => {
    const gate = new SessionSubmissionGate();
    const order: string[] = [];
    let releaseFirst: () => void = () => undefined;
    let finishFirst: () => void = () => undefined;
    const first = gate.run("session-1", async (release) => {
      order.push("first-start");
      releaseFirst = release;
      await new Promise<void>((resolve) => {
        finishFirst = resolve;
      });
    });
    const secondDispatch = vi.fn(async (release: () => void) => {
      order.push("second-start");
      release();
    });
    const second = gate.run("session-1", secondDispatch);

    await Promise.resolve();
    expect(secondDispatch).not.toHaveBeenCalled();
    releaseFirst();
    finishFirst();
    await Promise.all([first, second]);
    expect(order).toEqual(["first-start", "second-start"]);
  });

  it("allows different sessions to submit independently", async () => {
    const gate = new SessionSubmissionGate();
    const started: string[] = [];
    await Promise.all([
      gate.run("session-a", async (release) => {
        started.push("a");
        release();
      }),
      gate.run("session-b", async (release) => {
        started.push("b");
        release();
      })
    ]);
    expect(new Set(started)).toEqual(new Set(["a", "b"]));
  });
});
