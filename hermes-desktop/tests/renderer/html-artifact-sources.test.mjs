import test from "node:test";
import assert from "node:assert/strict";

import {
  htmlPathsFromToolEvents,
  isHtmlChatFile,
} from "../../src/utils/htmlArtifactSources.ts";

test("desktop html preview extracts local paths from completed tool events", () => {
  const events = [
    {
      runtimeRunId: "run-local",
      name: "desktop_workspace_write",
      status: "completed",
      input: JSON.stringify({ path: "solar-system.html" })
    },
    {
      runtimeRunId: "run-local",
      name: "desktop_workspace_write",
      status: "failed",
      input: JSON.stringify({ path: "failed.html" })
    }
  ];

  assert.deepEqual(htmlPathsFromToolEvents(events, "run-local"), ["solar-system.html"]);
});

test("desktop html preview recognizes generated html file metadata", () => {
  assert.equal(isHtmlChatFile({
    fileId: 1,
    runtimeWorkspaceId: 1,
    relativePath: "reports/solar-system.html",
    displayName: "solar-system.html",
    mimeType: null,
    sizeBytes: 31688,
    sourceType: "runtime"
  }), true);
});
