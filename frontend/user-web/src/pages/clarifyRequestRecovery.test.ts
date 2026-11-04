import { describe, expect, it } from "vitest";

import {
  ClarifyPollCoordinator,
  findClarifyContinuationReplay,
  isClarifyCardRenderable,
  mergeClarifyRequests,
  scopeClarifyRequests,
  type RuntimeClarifyRequest,
} from "./clarifyRequestRecovery";

function request(overrides: Partial<RuntimeClarifyRequest>): RuntimeClarifyRequest {
  return {
    requestId: "clr-1",
    runtimeSessionId: "rs-1",
    parentRuntimeRunId: "rr-parent",
    question: "问题",
    choices: [],
    multiSelect: false,
    status: "waiting",
    version: 1,
    createdAt: "2026-09-23T02:00:00Z",
    ...overrides,
  };
}

describe("clarify request recovery", () => {
  it("does not let a stale empty response erase a newer local state", () => {
    const current = [request({
      status: "claimed",
      version: 2,
      answer: { kind: "skip" },
    })];

    expect(mergeClarifyRequests(current, [])).toEqual(current);
  });

  it("prefers the newer server version for the same request", () => {
    const current = [request({ status: "waiting", version: 1 })];
    const incoming = [request({
      status: "answered",
      version: 2,
      resumeRuntimeRunId: "rr-continuation",
    })];

    expect(mergeClarifyRequests(current, incoming)).toEqual(incoming);
  });

  it("rejects poll responses from an invalidated session epoch", () => {
    const coordinator = new ClarifyPollCoordinator();
    const ticket = coordinator.begin();
    expect(coordinator.accept(ticket)).toBe(true);

    coordinator.invalidate();
    expect(coordinator.accept(ticket)).toBe(false);
  });

  it("scopes cards to the selected session and converged requests", () => {
    const sessionA = request({
      requestId: "clr-a",
      runtimeSessionId: "rs-a",
      status: "answered",
      version: 2,
      resumeRuntimeRunId: "rr-a"
    });
    const sessionB = request({
      requestId: "clr-b",
      runtimeSessionId: "rs-b",
      status: "waiting",
      version: 1
    });

  expect(scopeClarifyRequests(
      [sessionA, sessionB],
      "rs-b",
      new Set(["clr-a"])
    )).toEqual([sessionB]);
  });

  it("keeps auto-default continuation silent but preserves user-answer feedback", () => {
    expect(isClarifyCardRenderable(request({
      status: "waiting",
      answerSource: null,
    }))).toBe(true);
    expect(isClarifyCardRenderable(request({
      status: "claimed",
      answerSource: "auto",
      answer: { kind: "skip" },
    }))).toBe(false);
    expect(isClarifyCardRenderable(request({
      status: "resume_failed",
      answerSource: "auto",
      answer: { kind: "skip" },
    }))).toBe(false);
    expect(isClarifyCardRenderable(request({
      status: "answered",
      answerSource: "auto",
      answer: { kind: "skip" },
      resumeRuntimeRunId: "rr-auto",
    }))).toBe(false);
    expect(isClarifyCardRenderable(request({
      status: "claimed",
      answerSource: "user",
      answer: { kind: "single", text: "PDF" },
    }))).toBe(true);
    expect(isClarifyCardRenderable(request({
      status: "resume_failed",
      answerSource: "user",
      answer: { kind: "single", text: "PDF" },
    }))).toBe(true);
  });

  it("hides expert-worker cards once they leave waiting", () => {
    expect(isClarifyCardRenderable(request({
      mode: "expert_worker",
      status: "waiting",
      answerSource: null,
    }))).toBe(true);
    expect(isClarifyCardRenderable(request({
      mode: "expert_worker",
      status: "claimed",
      answerSource: "user",
      answer: { kind: "single", text: "PDF" },
    }))).toBe(false);
    expect(isClarifyCardRenderable(request({
      mode: "expert_worker",
      status: "answered",
      answerSource: "user",
      answer: { kind: "single", text: "PDF" },
    }))).toBe(false);
  });


  it("finds each deterministic continuation replay only once", () => {
    const items = [request({
      status: "answered",
      version: 2,
      resumeRuntimeRunId: "rr-continuation",
    })];
    const replayed = new Set<string>();

    const first = findClarifyContinuationReplay(items, replayed);
    replayed.add(first?.runtimeRunId ?? "");
    expect(first?.runtimeRunId).toBe("rr-continuation");
    expect(findClarifyContinuationReplay(items, replayed)).toBeUndefined();
  });

  it("never replays an expert-worker answer as a continuation run", () => {
    const items = [request({
      mode: "expert_worker",
      status: "answered",
      version: 2,
      resumeRuntimeRunId: "rr-worker-noise",
    })];

    expect(findClarifyContinuationReplay(items, new Set())).toBeUndefined();
  });
});
