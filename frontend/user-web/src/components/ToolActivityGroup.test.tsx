import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { ToolMessage } from "../pages/chatRunProjection";
import { ToolActivityGroup } from "./ToolActivityGroup";

const runningTool: ToolMessage = {
  id: "tool-running",
  kind: "tool",
  role: "tool",
  callId: "call-running",
  providerCallId: "call-running",
  hasStableCallId: true,
  name: "search_files",
  status: "running",
  preview: '{"matches":[]}'
};
const completedTool: ToolMessage = { ...runningTool, id: "tool-completed", callId: "call-completed", status: "completed", result: '{"matches":["a"]}' };
const failedTool: ToolMessage = { ...runningTool, id: "tool-failed", callId: "call-failed", name: "write_file", status: "failed", result: '{"error":"Write denied"}' };

describe("ToolActivityGroup", () => {
  it("shows running tools in an expanded accessible group", () => {
    const { container } = render(<ToolActivityGroup id="g1" messages={[runningTool]}/>);

    expect(screen.getByRole("button", { name: /正在执行 1 个操作/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("正在查找相关文件…")).toBeVisible();
    expect(screen.getByText("search_files")).not.toBeVisible();
    expect(container.querySelector('[aria-live="polite"]')).toBeInTheDocument();
  });

  it("collapses an all-success group but keeps failures expanded", () => {
    const { container, unmount } = render(<ToolActivityGroup id="g-success" messages={[completedTool]}/>);
    expect(screen.getByRole("button", { name: /已完成 1 个操作 · 查找相关文件/ })).toHaveAttribute("aria-expanded", "false");
    expect(container.querySelector(".toolActivityGroup")).toHaveAttribute("data-expanded", "false");
    unmount();

    render(<ToolActivityGroup id="g-failed" messages={[failedTool]}/>);
    expect(screen.getByRole("button", { name: /1 个操作未完成/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("该操作未完成，数字人仍可能通过其他方式继续任务")).toBeVisible();
  });

  it("names fixed user-facing capabilities in the group header", () => {
    render(<ToolActivityGroup id="g-names" messages={[
      { ...completedTool, name: "skill_view", callId: "call-skill" },
      { ...completedTool, name: "write_file", callId: "call-write" },
      { ...completedTool, name: "terminal", callId: "call-terminal" },
      { ...completedTool, name: "read_file", callId: "call-read" }
    ]}/>);

    expect(screen.getByRole("button", { name: /已完成 4 个操作 · 查看能力说明、保存文件、执行命令 \+1/ })).toBeVisible();
  });

  it("uses the skill identifier in a completed skill-view group header", () => {
    render(<ToolActivityGroup id="g-skill" messages={[{
      ...completedTool,
      name: "skill_view",
      input: '{"name":"powerpoint"}'
    }]}/>);

    expect(screen.getByRole("button", { name: /已完成 1 个操作 · 查看 powerpoint 技能能力/ })).toBeVisible();
  });

  it("distinguishes a skill document read and shows its fixed file path", async () => {
    const user = userEvent.setup();
    render(<ToolActivityGroup id="g-skill-file" messages={[{
      ...completedTool,
      name: "skill_view",
      input: '{"name":"powerpoint","file_path":"pptxgenjs.md"}'
    }]}/>);

    await user.click(screen.getByRole("button", { name: /已完成 1 个操作 · 查看 powerpoint 技能 · pptxgenjs\.md/ }));
    expect(screen.getByText("查看内容")).toBeVisible();
    expect(screen.getByText("pptxgenjs.md")).toBeVisible();
  });

  it("does not overwrite a manual toggle on rerender", async () => {
    const user = userEvent.setup();
    const { container, rerender } = render(<ToolActivityGroup id="g1" messages={[completedTool]}/>);
    const toggle = screen.getByRole("button", { name: /已完成 1 个操作/ });
    await user.click(toggle);
    rerender(<ToolActivityGroup id="g1" messages={[completedTool]}/>);

    expect(screen.getByRole("button", { name: /已完成 1 个操作/ })).toHaveAttribute("aria-expanded", "true");
    expect(container.querySelector(".toolActivityGroup")).toHaveAttribute("data-expanded", "true");
  });

  it("automatically collapses when a running group finishes successfully", async () => {
    const { rerender } = render(<ToolActivityGroup id="g1" messages={[runningTool]}/>);
    expect(screen.getByRole("button", { name: /正在执行 1 个操作/ })).toHaveAttribute("aria-expanded", "true");

    rerender(<ToolActivityGroup id="g1" messages={[completedTool]}/>);

    await waitFor(() => expect(screen.getByRole("button", { name: /已完成 1 个操作/ })).toHaveAttribute("aria-expanded", "false"));
  });

  it("reveals sanitized detail before technical metadata", async () => {
    const user = userEvent.setup();
    const message: ToolMessage = {
      ...completedTool,
      name: "read_excel",
      result: '{"rows":286,"token":"x"}',
      ttftMs: 840
    };
    render(<ToolActivityGroup id="g1" messages={[message]}/>);

    await user.click(screen.getByRole("button", { name: /已完成 1 个操作/ }));
    expect(screen.getByText("读取表格文件中的结构化数据。")).toBeVisible();
    expect(screen.getByText("read_excel")).not.toBeVisible();
    await user.click(screen.getByText("技术详情"));
    expect(screen.getByText((content) => content.includes('"token": "••••"'))).toBeVisible();
    expect(screen.getByText("read_excel")).toBeVisible();
    expect(screen.getByText("首次可见 Agent 内容")).toBeVisible();
    expect(screen.queryByText("首个有效内容")).not.toBeInTheDocument();
    expect(screen.getByText("TTFT 0.8s")).toBeVisible();
  });

  it("shows a concise command and result with full values kept in technical details", async () => {
    const user = userEvent.setup();
    const message: ToolMessage = {
      ...completedTool,
      name: "terminal",
      input: '{"command":"node -v"}',
      result: '{"output":"v22.22.3","exit_code":0}'
    };
    render(<ToolActivityGroup id="g-command" messages={[message]}/>);

    await user.click(screen.getByRole("button", { name: /已完成 1 个操作 · 执行命令/ }));
    expect(screen.getByText("在隔离运行环境中执行命令。")).toBeVisible();
    expect(screen.getByText("执行命令")).toBeVisible();
    expect(screen.getByText("node -v")).toBeVisible();
    expect(screen.getByText("v22.22.3")).toBeVisible();
    expect(screen.queryByText("exit_code")).not.toBeInTheDocument();
    await user.click(screen.getByText("技术详情"));
    expect(screen.getByText((content) => content.includes('"exit_code": 0'))).toBeVisible();
  });
});
