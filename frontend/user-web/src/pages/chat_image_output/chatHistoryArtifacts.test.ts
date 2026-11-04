import { describe, expect, it } from "vitest";

import type { ChatFileSummary } from "@hermes-cloud/schemas";

import { historyMessages } from "../chatHistory";

describe("history generated image artifacts", () => {
  it("restores runtime images on the assistant message from the same run", () => {
    const files: ChatFileSummary[] = [{
      fileId: 34,
      runtimeWorkspaceId: 115,
      runtimeSessionId: "rs-115-image-output",
      runtimeRunId: "rr-image",
      relativePath: "workspace/sessions/rs-115-image-output/dogs/dog.jpg",
      displayName: "dog.jpg",
      mimeType: "image/jpeg",
      sizeBytes: 2048,
      sourceType: "runtime"
    }];

    const messages = historyMessages([
      { role: "user", content: "找可爱狗狗照片" },
      {
        role: "assistant",
        content: "找到了。\n\nMEDIA:/data/workspace/sessions/rs-115-image-output/dogs/dog.jpg\n\n文件都在这儿： /data/workspace/sessions/rs-115-image-output/dog.jpg",
        runtimeRunId: "rr-image"
      }
    ], undefined, files);

    expect(messages[1]).toMatchObject({
      runtimeRunId: "rr-image",
      attachments: [{ fileId: 34, mimeType: "image/jpeg", sourceType: "runtime" }]
    });
  });
});
