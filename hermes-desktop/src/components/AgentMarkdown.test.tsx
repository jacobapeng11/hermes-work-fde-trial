import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AgentMarkdown } from "./AgentMarkdown";

describe("AgentMarkdown", () => {
  it("renders GFM structure with safe external links", () => {
    const source = [
      "# 标题",
      "",
      "- 条目",
      "  - 嵌套条目",
      "",
      "> 引用",
      "",
      "| 名称 | 值 |",
      "| --- | --- |",
      "| A | `code` |",
      "",
      "**加粗**、*强调*和~~删除~~",
      "",
      "[OpenAI](https://openai.com)",
    ].join("\n");

    const { container } = render(<AgentMarkdown content={source} />);

    expect(screen.getByRole("heading", { level: 1, name: "标题" })).toBeVisible();
    expect(container.querySelectorAll("ul li")).toHaveLength(2);
    expect(container.querySelector("blockquote")).toHaveTextContent("引用");
    expect(container.querySelector(".markdownTableScroller table")).toBeInTheDocument();
    expect(container.querySelector("td code")).toHaveTextContent("code");
    expect(container.querySelector("strong")).toHaveTextContent("加粗");
    expect(container.querySelector("em")).toHaveTextContent("强调");
    expect(container.querySelector("del")).toHaveTextContent("删除");
    expect(screen.getByRole("link", { name: "OpenAI" })).toHaveAttribute("rel", "noreferrer noopener");
    expect(screen.getByRole("link", { name: "OpenAI" })).toHaveAttribute("target", "_blank");
  });

  it("does not mount raw html", () => {
    const source = '<img src=x onerror="alert(1)"><script>alert(1)</script>';
    const { container } = render(<AgentMarkdown content={source} />);

    expect(container.querySelector("img, script")).not.toBeInTheDocument();
    expect(container).toHaveTextContent("<img src=x");
  });

  it("renders fenced code with valid pre and code nesting", () => {
    const { container } = render(<AgentMarkdown content={"```ts\nconst answer = 42;\n```"} />);
    const region = screen.getByTestId("markdown-code-block");

    expect(region.querySelector(":scope > pre > code.language-ts")).toHaveTextContent("const answer = 42;");
    expect(region.querySelector("pre div")).not.toBeInTheDocument();
    expect(screen.getByText("ts")).toHaveClass("markdownCodeLanguage");
  });

  it("collapses code over fifteen lines and expands and collapses it", async () => {
    const user = userEvent.setup();
    const code = Array.from({ length: 16 }, (_, index) => `line ${index + 1}`).join("\n");
    render(<AgentMarkdown content={`\`\`\`ts\n${code}\n\`\`\``} />);
    const region = screen.getByTestId("markdown-code-block");

    expect(region).toHaveAttribute("data-collapsed", "true");
    await user.click(screen.getByRole("button", { name: "展开代码" }));
    expect(region).toHaveAttribute("data-collapsed", "false");
    await user.click(screen.getByRole("button", { name: "收起代码" }));
    expect(region).toHaveAttribute("data-collapsed", "true");
  });

  it("collapses code over eight hundred characters but not exact boundaries", () => {
    const { rerender } = render(<AgentMarkdown content={`\`\`\`\n${"x".repeat(801)}\n\`\`\``} />);
    expect(screen.getByTestId("markdown-code-block")).toHaveAttribute("data-collapsed", "true");

    rerender(<AgentMarkdown content={`\`\`\`\n${"x".repeat(800)}\n\`\`\``} />);
    expect(screen.getByTestId("markdown-code-block")).toHaveAttribute("data-collapsed", "false");

    const fifteenLines = Array.from({ length: 15 }, (_, index) => `line ${index + 1}`).join("\n");
    rerender(<AgentMarkdown content={`\`\`\`\n${fifteenLines}\n\`\`\``} />);
    expect(screen.getByTestId("markdown-code-block")).toHaveAttribute("data-collapsed", "false");
  });
});
