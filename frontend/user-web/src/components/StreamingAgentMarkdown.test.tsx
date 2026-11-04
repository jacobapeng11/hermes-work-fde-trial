import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  StreamingAgentMarkdown,
  stableStreamingMarkdownBoundary,
} from "./StreamingAgentMarkdown";

describe("StreamingAgentMarkdown", () => {
  it("settles a completed paragraph and leaves the active tail mutable", () => {
    const text = "第一段\n\n第二段";
    expect(stableStreamingMarkdownBoundary(text)).toBe(text.indexOf("第二段"));
  });

  it("does not settle blank lines inside an open fenced code block", () => {
    expect(stableStreamingMarkdownBoundary("```ts\nconst a = 1;\n\nconst b = 2;\n")).toBe(-1);
    expect(stableStreamingMarkdownBoundary("```ts\n```not-a-closer\n\nconst b = 2;\n")).toBe(-1);

    const closed = "```ts\nconst a = 1;\n\nconst b = 2;\n```\n\n下一段\n";
    expect(stableStreamingMarkdownBoundary(closed)).toBe(closed.indexOf("下一段"));
  });

  it("keeps a blank line inside an indented code block unsettled", () => {
    const text = "    first line\n\n    second line\n\n下一段\n";
    expect(stableStreamingMarkdownBoundary(text)).toBe(text.indexOf("下一段"));
  });

  it("keeps list, table and blockquote continuations in one unsettled structure", () => {
    const list = "- 第一项\n\n  延续说明\n\n下一段\n";
    const table = "| 名称 | 值 |\n| --- | --- |\n| A | 1 |\n\n下一段\n";
    const quote = "> 第一段\n\n> 第二段\n\n下一段\n";

    expect(stableStreamingMarkdownBoundary(list)).toBe(list.indexOf("下一段"));
    expect(stableStreamingMarkdownBoundary(table)).toBe(table.indexOf("下一段"));
    expect(stableStreamingMarkdownBoundary(quote)).toBe(quote.indexOf("下一段"));
  });

  it("preserves an already settled DOM block while only the tail grows", () => {
    const { rerender } = render(<StreamingAgentMarkdown content={"固定段落\n\n活动尾部"}/>);
    const settledParagraph = screen.getByText("固定段落");

    rerender(<StreamingAgentMarkdown content={"固定段落\n\n活动尾部继续增长"}/>);

    expect(screen.getByText("固定段落")).toBe(settledParagraph);
    expect(screen.getByText("活动尾部继续增长")).toBeVisible();
  });

  it("resets safely when a recovered snapshot no longer extends the old prefix", () => {
    const { rerender } = render(<StreamingAgentMarkdown content={"旧段落\n\n旧尾部"}/>);
    rerender(<StreamingAgentMarkdown content="恢复后的完整正文"/>);

    expect(screen.queryByText("旧段落")).not.toBeInTheDocument();
    expect(screen.getByText("恢复后的完整正文")).toBeVisible();
  });
});
