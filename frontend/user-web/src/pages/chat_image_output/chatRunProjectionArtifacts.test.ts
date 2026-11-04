import { describe, expect, it } from "vitest";

import {
  projectChatRunEvent,
  type SessionCache
} from "../chatRunProjection";

describe("chat run generated image artifacts", () => {
  it("keeps already attached runtime images when the terminal answer arrives", () => {
    const cache: SessionCache = {
      messages: [{
        id: "assistant-rr-image",
        role: "assistant",
        runtimeRunId: "rr-image",
        content: "",
        status: "streaming",
        attachments: [{
          fileId: 34,
          displayName: "dog.jpg",
          mimeType: "image/jpeg",
          sourceType: "runtime"
        }]
      }],
      files: [],
      runId: "rr-image",
      streaming: true
    };

    const result = projectChatRunEvent(cache, "rr-image", {
      event: "chat.message.completed",
      data: {
        text: "找到了 5 张可爱狗狗照片。\n\nMEDIA:/data/workspace/sessions/rs-115-image-output/dogs/dog.jpg\n\n文件都在这儿： /data/workspace/sessions/rs-115-image-output/dog.jpg"
      }
    });

    expect(result.cache.messages[0]).toMatchObject({
      content: "找到了 5 张可爱狗狗照片。",
      status: "completed",
      attachments: [{ fileId: 34, sourceType: "runtime" }]
    });
  });
});
