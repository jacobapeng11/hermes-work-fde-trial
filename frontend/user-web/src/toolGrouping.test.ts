import { describe, expect, it } from "vitest";

import type { ActivityMessage, BackgroundTaskMessage, ChatMessage, ReasoningMessage, ToolMessage } from "./pages/chatRunProjection";
import { groupExecutionMessages, groupToolMessages } from "./toolGrouping";

const reasoning: ReasoningMessage = { id: "r-1", kind: "reasoning", role: "assistant", text: "plan" };
const activity: ActivityMessage = { id: "p-1", kind: "activity", role: "assistant", text: "checking result" };
const background: BackgroundTaskMessage = { id: "b-1", kind: "background", role: "system", taskId: "task-1", status: "running" };
const toolA: ToolMessage = { id: "t-1", kind: "tool", role: "tool", callId: "c-1", name: "search_files", status: "completed" };
const toolB: ToolMessage = { id: "t-2", kind: "tool", role: "tool", callId: "c-2", name: "read_file", status: "completed" };
const toolC: ToolMessage = { id: "t-3", kind: "tool", role: "tool", callId: "c-3", name: "write_file", status: "failed" };
const answer: ChatMessage = { id: "a-1", role: "assistant", content: "done" };

describe("groupToolMessages", () => {
  it("groups only consecutive tools without crossing activity boundaries", () => {
    const nodes = groupToolMessages([reasoning, toolA, toolB, background, toolC, answer]);

    expect(nodes.map((node) => node.kind)).toEqual(["message", "tool-group", "message", "tool-group", "message"]);
    expect(nodes[1]).toMatchObject({ kind: "tool-group", id: "tool-group-t-1", messages: [toolA, toolB] });
    expect(nodes[3]).toMatchObject({ kind: "tool-group", id: "tool-group-t-3", messages: [toolC] });
  });

  it("preserves non-tool message identity and input order", () => {
    const nodes = groupToolMessages([answer, reasoning]);

    expect(nodes).toEqual([
      { kind: "message", message: answer },
      { kind: "message", message: reasoning }
    ]);
  });
});

describe("groupExecutionMessages", () => {
  it("keeps reasoning, tools, and the reasoning after a tool in one ordered execution trace", () => {
    const nodes = groupExecutionMessages([reasoning, toolA, activity, toolB, answer]);

    expect(nodes).toHaveLength(2);
    expect(nodes[0]).toMatchObject({
      kind: "execution-timeline",
      id: "execution-timeline-r-1",
      messages: [reasoning, toolA, activity, toolB]
    });
    expect(nodes[1]).toEqual({ kind: "message", message: answer });
  });

  it("does not merge execution messages across a user-facing message boundary", () => {
    const nodes = groupExecutionMessages([reasoning, toolA, answer, activity, toolB]);

    expect(nodes.map((node) => node.kind)).toEqual(["execution-timeline", "message", "execution-timeline"]);
  });

  it("keeps background work and capability errors in their run timeline without crossing runs", () => {
    const runOneReasoning = { ...reasoning, runtimeRunId: "rr-1" };
    const runOneBackground = { ...background, runtimeRunId: "rr-1" };
    const capability = {
      id: "capability-1",
      kind: "capability-error" as const,
      role: "system" as const,
      runtimeRunId: "rr-1",
      capabilityType: "skill" as const,
      errorCode: "unavailable",
      displayMessage: "Skill 不可用",
      recoverable: true,
      errorId: "1"
    };
    const runTwoTool = { ...toolA, runtimeRunId: "rr-2" };

    const nodes = groupExecutionMessages([runOneReasoning, runOneBackground, capability, runTwoTool]);

    expect(nodes).toHaveLength(2);
    expect(nodes[0]).toMatchObject({ kind: "execution-timeline", runtimeRunId: "rr-1", messages: [runOneReasoning, runOneBackground, capability] });
    expect(nodes[1]).toMatchObject({ kind: "execution-timeline", runtimeRunId: "rr-2", messages: [runTwoTool] });
  });
});
