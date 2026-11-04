import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { RuntimeModelSummary } from "@hermes-cloud/schemas";
import { ModelPicker } from "./ModelPicker";

const models: RuntimeModelSummary[] = [
  { modelEntryId: 1, modelKey: "gpt-5", displayName: "GPT-5", providerId: 10, providerDisplayName: "OpenAI", isDefault: true },
  { modelEntryId: 2, modelKey: "gpt-5-mini", displayName: "GPT-5 Mini", providerId: 10, providerDisplayName: "OpenAI", isDefault: false },
  { modelEntryId: 3, modelKey: "claude-sonnet-4", displayName: "Claude Sonnet 4", providerId: 20, providerDisplayName: "Anthropic", isDefault: false }
];

describe("ModelPicker", () => {
  it("renders provider groups and marks the current model", async () => {
    render(<ModelPicker modelId={1} models={models} onModelChange={() => undefined}/>);

    const trigger = screen.getByRole("button", { name: "模型" });
    expect(trigger).toHaveTextContent("GPT-5");
    await userEvent.setup().click(trigger);

    expect(screen.getByRole("listbox", { name: "模型列表" })).toBeVisible();
    expect(screen.getByText("OpenAI")).toBeVisible();
    expect(screen.getByText("Anthropic")).toBeVisible();
    expect(screen.getByRole("option", { name: "GPT-5" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("option", { name: "Claude Sonnet 4" })).toHaveAttribute("aria-selected", "false");
    expect(screen.queryByText("gpt-5")).not.toBeInTheDocument();
    expect(screen.queryByText("claude-sonnet-4")).not.toBeInTheDocument();
  });

  it("filters models and selects a result", async () => {
    const onModelChange = vi.fn();
    render(<ModelPicker modelId={1} models={models} onModelChange={onModelChange}/>);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "模型" }));
    await user.type(screen.getByRole("searchbox", { name: "搜索模型" }), "claude-sonnet-4");

    expect(screen.queryByRole("option", { name: "GPT-5" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "Claude Sonnet 4" }));
    expect(onModelChange).toHaveBeenCalledWith(3);
    expect(screen.queryByRole("listbox", { name: "模型列表" })).not.toBeInTheDocument();
  });

  it("supports arrow navigation, Enter selection, and Escape focus restoration", async () => {
    const onModelChange = vi.fn();
    render(<ModelPicker modelId={1} models={models} onModelChange={onModelChange}/>);
    const user = userEvent.setup();
    const trigger = screen.getByRole("button", { name: "模型" });

    await user.click(trigger);
    expect(screen.getByRole("searchbox", { name: "搜索模型" })).toHaveFocus();
    await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    expect(onModelChange).toHaveBeenCalledWith(2);

    await user.click(trigger);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox", { name: "模型列表" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("closes on outside click and stays disabled without available models", async () => {
    const { rerender } = render(<div><ModelPicker modelId={1} models={models} onModelChange={() => undefined}/><button>外部</button></div>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "模型" }));
    await user.click(screen.getByRole("button", { name: "外部" }));
    expect(screen.queryByRole("listbox", { name: "模型列表" })).not.toBeInTheDocument();

    rerender(<ModelPicker modelId={null} models={[]} onModelChange={() => undefined}/>);
    expect(screen.getByRole("button", { name: "模型" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "模型" })).toHaveTextContent("暂无模型");
  });
});
