import { describe, expect, it } from "vitest";
import { selectCollapsedSessions } from "./sessionListLayout";

describe("selectCollapsedSessions", () => {
  const rows = [
    { id: "one", height: 30, priority: false },
    { id: "two", height: 30, priority: false },
    { id: "three", height: 30, priority: false }
  ];

  it("does not offer expansion when every session fits", () => {
    expect(selectCollapsedSessions(rows, 100)).toEqual({
      overflow: false,
      visibleIds: ["one", "two", "three"]
    });
  });

  it("reserves the bottom control only when rows overflow", () => {
    expect(selectCollapsedSessions(rows, 70, 34)).toEqual({
      overflow: true,
      visibleIds: ["one"]
    });
  });

  it("keeps active or notifying sessions visible before ordinary sessions", () => {
    const layout = selectCollapsedSessions([
      ...rows,
      { id: "running", height: 30, priority: true }
    ], 70, 34);
    expect(layout.visibleIds).toEqual(["running"]);
  });
});
