import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatAttachmentList } from "../ChatAttachmentList";

describe("ChatAttachmentList", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("loads an image lazily and opens the preview through the provided URL", async () => {
    const objectUrl = "blob:image-url";
    URL.createObjectURL = () => objectUrl;
    URL.revokeObjectURL = () => undefined;
    const onOpenImage = vi.fn();
    const attachment = {
      fileId: 12,
      displayName: "截图.png",
      mimeType: "image/png"
    };

    render(
      <ChatAttachmentList
        attachments={[attachment]}
        resolveImageUrl={vi.fn().mockResolvedValue(objectUrl)}
        onOpenImage={onOpenImage}
      />
    );

    const button = screen.getByRole("button", { name: "查看图片 截图.png" });
    await waitFor(() => expect(screen.getByAltText("截图.png")).toBeInTheDocument());
    fireEvent.click(button);

    expect(onOpenImage).toHaveBeenCalledWith(attachment, objectUrl);
  });
});
