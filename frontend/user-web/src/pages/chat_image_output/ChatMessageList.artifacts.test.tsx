import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChatMessageList } from "../../components/ChatMessageList";

describe("ChatMessageList generated images", () => {
  it("renders assistant image artifacts before the answer and opens the preview", async () => {
    const objectUrl = "blob:generated-image";
    URL.createObjectURL = vi.fn(() => objectUrl);
    URL.revokeObjectURL = vi.fn();
    const onOpenImage = vi.fn();
    const attachment = {
      fileId: 34,
      displayName: "dog.jpg",
      mimeType: "image/jpeg",
      sourceType: "runtime"
    };

    render(<ChatMessageList
      streaming={false}
      messages={[{
        id: "assistant-image",
        role: "assistant",
        runtimeRunId: "rr-image",
        content: "找到了 5 张可爱狗狗照片。",
        attachments: [attachment]
      }]}
      resolveImageUrl={vi.fn().mockResolvedValue(objectUrl)}
      onOpenImage={onOpenImage}
    />);

    const answer = screen.getByText("找到了 5 张可爱狗狗照片。");
    const button = screen.getByRole("button", { name: "查看图片 dog.jpg" });
    expect(button.compareDocumentPosition(answer) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    await waitFor(() => expect(screen.getByAltText("dog.jpg")).toBeInTheDocument());
    fireEvent.click(button);

    expect(onOpenImage).toHaveBeenCalledWith(attachment, objectUrl);
  });
});
