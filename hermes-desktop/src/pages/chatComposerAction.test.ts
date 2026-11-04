import { describe, expect, it } from "vitest";

import { chatComposerAction } from "./chatComposerAction";

describe("chat composer action", () => {
  it("prioritizes Send for a draft and gates repeated cancellation", () => {
    expect(chatComposerAction({ draft: "下一条", hasActiveRun: true, uploadPending: false, recovering: false, cancellationPending: false }))
      .toEqual({ action: "send", disabled: false });
    expect(chatComposerAction({ draft: "", hasActiveRun: true, uploadPending: false, recovering: false, cancellationPending: true }))
      .toEqual({ action: "stop", disabled: true });
  });
});
