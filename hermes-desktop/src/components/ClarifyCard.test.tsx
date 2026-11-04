import { fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ClarifyCard } from "./ClarifyCard";

type ClarifyRequest = Parameters<typeof ClarifyCard>[0]["request"];

function request(overrides: Partial<ClarifyRequest>): ClarifyRequest {
  return {
    requestId: "clr-math",
    runtimeSessionId: "rs-1",
    parentRuntimeRunId: "rr-parent",
    question: "17 × 23 = ？",
    choices: ["389", "391"],
    multiSelect: false,
    status: "waiting",
    version: 1,
    createdAt: "2026-09-23T02:00:00Z",
    ...overrides,
  };
}

describe("ClarifyCard", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-23T02:00:00Z"));
  });

  it("shows the server-backed auto-default countdown", () => {
    render(
      <ClarifyCard
        request={request({ autoDefaultAt: "2026-09-23T02:01:00Z" })}
        onAnswer={() => undefined}
      />
    );

    expect(screen.getByText("60 秒后按默认值继续")).toBeVisible();

    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(screen.getByText("59 秒后按默认值继续")).toBeVisible();
  });

  it("labels an expert-worker question with its requesting expert", () => {
    render(
      <ClarifyCard
        request={request({
          mode: "expert_worker",
          requestedBy: "报告专家",
        })}
        onAnswer={() => undefined}
      />
    );

    expect(screen.getByText("报告专家 需要你确认")).toBeVisible();
  });

  it("keeps a user-claimed request visible without editable answer controls", () => {
    render(
      <ClarifyCard
        request={request({
          status: "claimed",
          answerSource: "user",
          answer: { kind: "single", text: "391" },
        })}
        onAnswer={() => undefined}
      />
    );

    expect(screen.getByText("正在继续…")).toBeVisible();
    expect(screen.queryByRole("button", { name: "确认并继续" })).not.toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: "389" })).not.toBeInTheDocument();
  });

  it("allows retrying a failed continuation with the adopted answer", async () => {
    const onAnswer = vi.fn();
    render(
      <ClarifyCard
        request={request({
          status: "resume_failed",
          answer: { kind: "single", text: "391" },
          answerSource: "user",
          resumeAttempts: 2,
        })}
        busy={false}
        onAnswer={onAnswer}
      />
    );

    expect(screen.getByText("继续失败，可重试")).toBeVisible();
    expect(screen.getByText("已采纳用户回答：391")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重试继续" }));
    expect(onAnswer).toHaveBeenCalledWith({ kind: "single", text: "391" });
  });

  it("renders legacy timeout as a read-only result", () => {
    render(
      <ClarifyCard
        request={request({ status: "timed_out" })}
        onAnswer={() => undefined}
      />
    );

    expect(screen.getByText("该确认已超时，原任务未能自动继续。")).toBeVisible();
    expect(screen.queryByRole("button", { name: "重试继续" })).not.toBeInTheDocument();
  });
});
