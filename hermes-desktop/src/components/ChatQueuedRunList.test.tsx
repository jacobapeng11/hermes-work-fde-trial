import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ChatQueuedRunList } from "./ChatQueuedRunList";

describe("ChatQueuedRunList", () => {
  it("shows one compact FIFO container without per-run controls", () => {
    render(<ChatQueuedRunList runs={[
      { runId: "run-1", clientMessageId: "message-1", enqueueSeq: 1, prompt: "整理附件", attachmentFileIds: ["7"] },
      { runId: "run-2", clientMessageId: "message-2", enqueueSeq: 2, prompt: "继续总结", attachmentFileIds: [] }
    ]} />);

    expect(screen.getByRole("status", { name: "等待执行的消息" })).toHaveTextContent("等待执行2 条");
    expect(screen.getByText("含 1 个附件")).toBeVisible();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
