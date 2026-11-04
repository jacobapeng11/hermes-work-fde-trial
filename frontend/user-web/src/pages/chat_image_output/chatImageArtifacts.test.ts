import { describe, expect, it } from "vitest";

import type { ChatFileSummary } from "@hermes-cloud/schemas";

import type { ChatMessage } from "../chatRunProjection";
import { attachGeneratedImageArtifacts } from "./chatImageArtifacts";

function imageFile(
  fileId: number,
  runtimeRunId: string | null
): ChatFileSummary {
  return {
    fileId,
    runtimeWorkspaceId: 115,
    runtimeSessionId: "rs-115-image-output",
    runtimeRunId,
    relativePath: `workspace/sessions/rs-115-image-output/dogs/dog-${fileId}.jpg`,
    displayName: `dog-${fileId}.jpg`,
    mimeType: "image/jpeg",
    sizeBytes: 1024,
    sourceType: "runtime"
  };
}

describe("attachGeneratedImageArtifacts", () => {
  it("binds generated images to the assistant bubble by runtime run", () => {
    const messages: ChatMessage[] = [
      { id: "user", role: "user", runtimeRunId: "rr-image", content: "找 5 张可爱狗狗照片" },
      { id: "assistant", role: "assistant", runtimeRunId: "rr-image", content: "找到了。" }
    ];

    const result = attachGeneratedImageArtifacts(messages, [
      imageFile(2, "rr-image"),
      imageFile(1, "rr-image")
    ]);

    expect("attachments" in result[0] ? result[0].attachments : undefined).toBeUndefined();
    expect(result[1]).toMatchObject({
      attachments: [
        { fileId: 1, sourceType: "runtime" },
        { fileId: 2, sourceType: "runtime" }
      ]
    });
  });

  it("ignores uploads, non-images, and artifacts outside an assistant run", () => {
    const messages: ChatMessage[] = [
      { id: "assistant", role: "assistant", runtimeRunId: "rr-image", content: "完成" }
    ];
    const result = attachGeneratedImageArtifacts(messages, [
      { ...imageFile(3, "rr-image"), sourceType: "upload" },
      { ...imageFile(4, "rr-image"), mimeType: "text/plain", displayName: "result.txt" },
      imageFile(5, "rr-other")
    ]);

    expect("attachments" in result[0] ? result[0].attachments : undefined).toBeUndefined();
  });

  it("removes an assistant image when its indexed artifact disappears", () => {
    const messages: ChatMessage[] = [{
      id: "assistant",
      role: "assistant",
      runtimeRunId: "rr-image",
      content: "完成",
      attachments: [{ fileId: 1, displayName: "dog-1.jpg", mimeType: "image/jpeg", sourceType: "runtime" }]
    }];

    const result = attachGeneratedImageArtifacts(messages, []);
    expect("attachments" in result[0] ? result[0].attachments : undefined).toBeUndefined();
  });
});
