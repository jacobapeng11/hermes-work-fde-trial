import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Composer } from "./Composer";

describe("Composer", () => {
  it("focuses once for a new focus request when the input is available", () => {
    const props = {
      input: "",
      onInputChange: () => undefined,
      onSend: () => undefined,
      onStop: () => undefined,
      streaming: false,
      modelId: null,
      models: [],
      onModelChange: () => undefined,
      focusRequest: 1
    };
    const { rerender } = render(<Composer {...props} disabled />);
    const input = screen.getByLabelText("消息输入");

    expect(input).not.toHaveFocus();
    rerender(<Composer {...props} disabled={false} />);
    expect(input).toHaveFocus();

    input.blur();
    rerender(<Composer {...props} input="内容变化不应重新抢焦点" />);
    expect(input).not.toHaveFocus();
    rerender(<Composer {...props} input="内容变化不应重新抢焦点" focusRequest={2} />);
    expect(input).toHaveFocus();
  });

  it("keeps the textarea writable while a chat run is streaming", () => {
    render(<Composer input="" onInputChange={() => undefined} onSend={() => undefined} streaming focusRequest={1} modelId={null} models={[]} onModelChange={() => undefined} onStop={() => undefined} />);
    expect(screen.getByLabelText("消息输入")).toBeEnabled();
    expect(screen.getByLabelText("消息输入")).toHaveFocus();
  });

  it("sends with Enter but preserves Shift+Enter and an active Chinese IME composition", () => {
    const onSend = vi.fn();
    render(<Composer input="你好" onInputChange={() => undefined} onSend={onSend} streaming={false} modelId={null} models={[]} onModelChange={() => undefined} onStop={() => undefined} />);
    const input = screen.getByLabelText("消息输入");
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("provides an active attachment picker while keeping unavailable voice disabled", () => {
    const onAddAttachments = vi.fn();
    render(<Composer input="" onInputChange={() => undefined} onSend={() => undefined} streaming={false} modelId={null} models={[]} onModelChange={() => undefined} onStop={() => undefined} onAddAttachments={onAddAttachments} />);
    fireEvent.click(screen.getByLabelText("添加附件"));
    expect(onAddAttachments).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("添加附件")).toBeEnabled();
    expect(screen.getByLabelText("语音输入（服务尚未启用）")).toBeDisabled();
  });

  it("grows with content, caps at 120px, and shrinks when content is cleared", () => {
    const props = {
      onInputChange: () => undefined,
      onSend: () => undefined,
      onStop: () => undefined,
      streaming: false,
      modelId: null,
      models: [],
      onModelChange: () => undefined
    };
    const { rerender } = render(<Composer {...props} input="" />);
    const input = screen.getByLabelText("消息输入");
    let scrollHeight = 88;
    Object.defineProperty(input, "scrollHeight", {
      configurable: true,
      get: () => scrollHeight
    });

    rerender(<Composer {...props} input={"第一行\n第二行"} />);
    expect(input).toHaveStyle({ height: "88px" });

    scrollHeight = 160;
    rerender(<Composer {...props} input={"第一行\n第二行\n第三行"} />);
    expect(input).toHaveStyle({ height: "120px" });

    scrollHeight = 20;
    rerender(<Composer {...props} input="" />);
    expect(input).toHaveStyle({ height: "20px" });
  });

  it("freezes the message input and action control order", () => {
    const { container } = render(<Composer input="" onInputChange={() => undefined} onSend={() => undefined} streaming={false} modelId={null} models={[]} onModelChange={() => undefined} onStop={() => undefined} />);
    const composer = container.querySelector(".composerCard");
    const controls = Array.from(composer?.querySelectorAll(":scope > textarea, :scope > .composerActions > .composerAgentPicker > button, :scope > .composerActions > button, :scope > .composerActions > .modelPicker > button") ?? []);

    expect(controls.map((control) => control.getAttribute("aria-label") ?? control.textContent?.trim())).toEqual([
      "消息输入",
      "当前对话对象：个人",
      "添加附件",
      "语音输入（服务尚未启用）",
      "模型",
      "发送消息"
    ]);
    expect(screen.getByLabelText("发送消息")).toHaveClass("sendButton");
    expect(screen.getByLabelText("发送消息")).toHaveAttribute("data-action", "send");
    expect(screen.getByLabelText("消息输入")).toHaveAttribute("placeholder", "描述你希望数字人完成的任务……");
    expect(screen.getByLabelText("发送消息")).toBeDisabled();
    expect(screen.getByLabelText("语音输入（服务尚未启用）")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "能力配置" })).not.toBeInTheDocument();
  });

  it("uses Agent settings as the single model entry for Application Runtime", () => {
    render(<Composer input="" onInputChange={() => undefined} onSend={() => undefined} streaming={false} modelId={1} models={[]} onModelChange={() => undefined} onStop={() => undefined} showModelPicker={false} />);

    expect(screen.queryByRole("button", { name: "模型" }))
      .not.toBeInTheDocument();
  });

  it("opens a flat optional-object menu and keeps expert selection single-choice", () => {
    const onAgentChange = vi.fn();
    const installedExperts = [{
      runtimeAgentProfileId: 7,
      runtimeWorkspaceId: 1,
      hermesProfileName: "expert-7",
      profileKind: "expert" as const,
      displayName: "写作专家",
      description: "公众号文章写作",
      soulContent: "# 写作专家",
      ownershipType: "self_created",
      updateAvailable: false,
      capabilities: { skillIds: [], mcpIds: [], knowledgeBaseIds: [], enterpriseKnowledgeEnabled: false, pluginIds: [] },
      shareScope: { organization: false, userIds: [], departmentIds: [] },
      shareScopeUsers: [],
      allowedActions: { viewDetail: true, install: false, uninstall: false, summon: true, edit: true, copy: true, publish: true, unlist: false, delete: true },
      status: "active"
    }];
    render(<Composer input="" onInputChange={() => undefined} onSend={() => undefined} streaming={false} modelId={null} models={[]} onModelChange={() => undefined} onStop={() => undefined} installedExperts={installedExperts} onAgentChange={onAgentChange} />);

    fireEvent.click(screen.getByRole("button", { name: "当前对话对象：个人" }));
    const rootMenu = screen.getByRole("menu", { name: "可选对象" });
    expect(within(rootMenu).getByRole("menuitemradio", { name: "个人" })).toBeVisible();
    expect(within(rootMenu).getByText("专家")).toBeVisible();
    expect(within(rootMenu).getByRole("menuitemradio", { name: /写作专家/ })).toBeVisible();
    fireEvent.click(within(rootMenu).getByRole("menuitemradio", { name: /写作专家/ }));

    expect(onAgentChange).toHaveBeenCalledWith(7);
    expect(screen.queryByRole("menu", { name: "可选对象" })).not.toBeInTheDocument();
  });

  it("opens the optional-object menu by click and closes it with Escape", () => {
    render(<Composer input="" onInputChange={() => undefined} onSend={() => undefined} streaming={false} modelId={null} models={[]} onModelChange={() => undefined} onStop={() => undefined} />);

    const pickerButton = screen.getByRole("button", { name: "当前对话对象：个人" });
    fireEvent.click(pickerButton);
    expect(screen.getByRole("menu", { name: "可选对象" })).toHaveTextContent("尚未安装专家");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu", { name: "可选对象" })).not.toBeInTheDocument();
    expect(pickerButton).toHaveFocus();
  });

  it("disables duplicate stop actions while a stop is pending", () => {
    const onStop = vi.fn();
    render(<Composer input="" onInputChange={() => undefined} onSend={() => undefined} streaming stopping modelId={null} models={[]} onModelChange={() => undefined} onStop={onStop} disabled />);

    const stopButton = screen.getByLabelText("停止生成");
    expect(stopButton).toHaveClass("sendButton");
    expect(stopButton).toHaveAttribute("data-action", "stop");
    expect(stopButton).toBeDisabled();
    fireEvent.click(stopButton);
    expect(onStop).not.toHaveBeenCalled();
  });

  it("shows only Stop for an empty draft while streaming, then switches to Send for a correction", () => {
    const onSend = vi.fn();
    const onStop = vi.fn();
    const { rerender } = render(<Composer input="" onInputChange={() => undefined} onSend={onSend} streaming modelId={null} models={[]} onModelChange={() => undefined} onStop={onStop} />);

    const stop = screen.getByLabelText("停止生成");
    expect(stop).toBeEnabled();
    expect(stop).toHaveAttribute("data-action", "stop");
    expect(screen.queryByLabelText("发送消息")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "模型" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "当前对话对象：个人" }))
      .toBeDisabled();

    rerender(<Composer input="补充检查日志" onInputChange={() => undefined} onSend={onSend} streaming modelId={null} models={[]} onModelChange={() => undefined} onStop={onStop} />);
    const send = screen.getByLabelText("发送消息");
    expect(send).toBeEnabled();
    expect(send).toHaveAttribute("data-action", "send");
    expect(screen.queryByLabelText("停止生成")).not.toBeInTheDocument();

    fireEvent.click(send);
    expect(onSend).toHaveBeenCalledOnce();

    rerender(<Composer input="补充检查日志" onInputChange={() => undefined} onSend={onSend} streaming={false} modelId={null} models={[]} onModelChange={() => undefined} onStop={onStop} />);
    expect(screen.getByLabelText("发送消息")).toBeVisible();
    expect(screen.queryByLabelText("停止生成")).not.toBeInTheDocument();
  });

  it("blocks Send during an unfinished upload while keeping the draft", () => {
    const onSend = vi.fn();
    const onStop = vi.fn();
    render(<Composer
      input="附件上传后再发送"
      onInputChange={() => undefined}
      onSend={onSend}
      streaming
      modelId={null}
      models={[]}
      onModelChange={() => undefined}
      onStop={onStop}
      uploads={[{ id: "upload-1", name: "report.pdf", progress: 40, status: "uploading" }]}
    />);

    expect(screen.getByLabelText("消息输入")).toHaveValue("附件上传后再发送");
    expect(screen.getByLabelText("发送消息")).toBeDisabled();
    expect(screen.queryByLabelText("停止生成")).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByLabelText("消息输入"), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
  });
});
