import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Composer } from "../Composer";

const baseProps = {
  input: "",
  onInputChange: () => undefined,
  onSend: () => undefined,
  onStop: () => undefined,
  streaming: false,
  modelId: null,
  models: [],
  onModelChange: () => undefined
};

describe("chat image input", () => {
  it("passes only pasted image files to the attachment queue", () => {
    const onPasteFiles = vi.fn();
    render(<Composer {...baseProps} onPasteFiles={onPasteFiles} />);
    const image = new File(["png"], "screenshot.png", { type: "image/png" });
    const text = new File(["text"], "note.txt", { type: "text/plain" });

    fireEvent.paste(screen.getByLabelText("消息输入"), {
      clipboardData: {
        files: [text, image]
      }
    });

    expect(onPasteFiles).toHaveBeenCalledOnce();
    expect(onPasteFiles).toHaveBeenCalledWith([image]);
  });

  it("keeps Send available for an image-only draft", () => {
    render(<Composer {...baseProps} canSendWithoutText />);

    expect(screen.getByLabelText("发送消息")).toBeEnabled();
  });
});
