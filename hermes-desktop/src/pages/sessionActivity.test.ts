import type { RuntimeSessionSummary } from "@hermes-cloud/schemas";
import { beforeEach, describe, expect, it } from "vitest";
import {
  SessionActivityCoordinator,
  sessionActivityStorageKey
} from "./sessionActivity";

const scope = { orgId: 7, userId: 11, runtimeWorkspaceId: 23 };
const session = (
  runtimeSessionId: string,
  runtimeRunId: string,
  status: string
): RuntimeSessionSummary => ({
  sessionId: Number(runtimeSessionId.replace(/\D/g, "")) || 1,
  runtimeWorkspaceId: scope.runtimeWorkspaceId,
  runtimeSessionId,
  sourceType: "web",
  messageCount: 2,
  status: "active",
  latestRun: { runtimeRunId, status }
});

describe("SessionActivityCoordinator", () => {
  beforeEach(() => localStorage.clear());

  it("baselines existing successful runs without lighting every old session", () => {
    const coordinator = new SessionActivityCoordinator(scope, localStorage);
    const activities = coordinator.initialize([
      session("session-1", "run-old-1", "completed"),
      session("session-2", "run-old-2", "completed")
    ], "session-1");

    expect(activities).toEqual({
      "session-1": { state: "idle" },
      "session-2": { state: "idle" }
    });
    expect(JSON.parse(localStorage.getItem(sessionActivityStorageKey(scope)) || "{}").seen).toEqual({
      "session-1": "run-old-1",
      "session-2": "run-old-2"
    });
  });

  it("restores a later unseen completion across refresh and clears it on view", () => {
    const first = new SessionActivityCoordinator(scope, localStorage);
    first.initialize([session("session-1", "run-old", "completed")], null);

    const refreshed = new SessionActivityCoordinator(scope, localStorage);
    let activities = refreshed.initialize([
      session("session-1", "run-new", "completed")
    ], null);
    expect(activities["session-1"]).toEqual({
      state: "completed-unseen",
      runtimeRunId: "run-new"
    });

    activities = refreshed.markSeen(activities, "session-1");
    expect(activities["session-1"]).toEqual({ state: "idle" });

    const afterView = new SessionActivityCoordinator(scope, localStorage);
    expect(afterView.initialize([
      session("session-1", "run-new", "completed")
    ], null)["session-1"]).toEqual({ state: "idle" });
  });

  it("uses live running and terminal transitions without a successful dot for failures", () => {
    const coordinator = new SessionActivityCoordinator(scope, localStorage);
    let activities = coordinator.initialize([], null);
    activities = coordinator.markRunning(activities, "session-1", "run-live");
    expect(activities["session-1"]).toEqual({ state: "running", runtimeRunId: "run-live" });

    activities = coordinator.markTerminal(activities, "session-1", "run-live", false, null);
    expect(activities["session-1"]).toEqual({ state: "idle" });

    activities = coordinator.markRunning(activities, "session-1", "run-success");
    activities = coordinator.markTerminal(activities, "session-1", "run-success", true, "session-2");
    expect(activities["session-1"]).toEqual({
      state: "completed-unseen",
      runtimeRunId: "run-success"
    });
  });

  it("treats completion in the active session as seen immediately", () => {
    const coordinator = new SessionActivityCoordinator(scope, localStorage);
    let activities = coordinator.initialize([], "session-1");
    activities = coordinator.markRunning(activities, "session-1", "run-live");
    activities = coordinator.markTerminal(activities, "session-1", "run-live", true, "session-1");

    expect(activities["session-1"]).toEqual({ state: "idle" });
    expect(JSON.parse(localStorage.getItem(sessionActivityStorageKey(scope)) || "{}").seen).toEqual({
      "session-1": "run-live"
    });
  });

  it("does not let a stale durable snapshot replace a newer local run", () => {
    const coordinator = new SessionActivityCoordinator(scope, localStorage);
    let activities = coordinator.initialize([
      session("session-1", "run-old", "completed")
    ], null);
    activities = coordinator.markRunning(activities, "session-1", "run-local");

    expect(coordinator.reconcile(activities, [
      session("session-1", "run-old", "completed")
    ], null)["session-1"]).toEqual({ state: "running", runtimeRunId: "run-local" });
  });

  it("allows the durable terminal state of the same run to advance local running", () => {
    const coordinator = new SessionActivityCoordinator(scope, localStorage);
    let activities = coordinator.initialize([
      session("session-1", "run-1", "running")
    ], null);

    activities = coordinator.reconcile(activities, [
      session("session-1", "run-1", "completed")
    ], null);

    expect(activities["session-1"]).toEqual({
      state: "completed-unseen",
      runtimeRunId: "run-1"
    });
  });

  it("falls back to in-memory seen state when localStorage is unavailable", () => {
    const unavailableStorage = {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("quota"); }
    };
    const coordinator = new SessionActivityCoordinator(scope, unavailableStorage);
    let activities = coordinator.initialize([
      session("session-1", "run-old", "completed")
    ], null);
    activities = coordinator.markRunning(activities, "session-1", "run-new");
    activities = coordinator.markTerminal(activities, "session-1", "run-new", true, null);

    expect(activities["session-1"]).toEqual({
      state: "completed-unseen",
      runtimeRunId: "run-new"
    });
  });

  it("does not relight or regress a run after that exact completion was viewed", () => {
    const coordinator = new SessionActivityCoordinator(scope, localStorage);
    let activities = coordinator.initialize([], null);
    activities = coordinator.markTerminal(activities, "session-1", "run-1", true, null);
    activities = coordinator.markSeen(activities, "session-1");

    activities = coordinator.markTerminal(activities, "session-1", "run-1", true, "session-2");
    activities = coordinator.markRunning(activities, "session-1", "run-1");

    expect(activities["session-1"]).toEqual({ state: "idle" });
  });

  it("keeps a chat error red until the user explicitly reopens that session", () => {
    const coordinator = new SessionActivityCoordinator(scope, localStorage);
    let activities = coordinator.initialize([], "session-1");

    activities = coordinator.markError(activities, "session-1", "run-error");
    expect(activities["session-1"]).toEqual({
      state: "error-unseen",
      runtimeRunId: "run-error"
    });

    activities = coordinator.markSeen(activities, "session-1");
    expect(activities["session-1"]).toEqual({ state: "idle" });
  });

  it("keeps an unseen chat error across refresh and acknowledges that exact run", () => {
    const first = new SessionActivityCoordinator(scope, localStorage);
    let activities = first.initialize([], "session-1");
    activities = first.markError(activities, "session-1", "run-error");
    expect(activities["session-1"]).toEqual({
      state: "error-unseen",
      runtimeRunId: "run-error"
    });

    const refreshed = new SessionActivityCoordinator(scope, localStorage);
    activities = refreshed.initialize([
      session("session-1", "run-error", "failed")
    ], "session-1");
    expect(activities["session-1"]).toEqual({
      state: "error-unseen",
      runtimeRunId: "run-error"
    });

    activities = refreshed.markSeen(activities, "session-1");
    const acknowledged = new SessionActivityCoordinator(scope, localStorage);
    expect(acknowledged.initialize([
      session("session-1", "run-error", "failed")
    ], "session-1")["session-1"]).toEqual({ state: "idle" });
  });
});
