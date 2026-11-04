import { describe, expect, it } from "vitest";

import {
  htmlPathsFromToolEvents,
  isHtmlChatFile,
  isZipChatFile,
  isHtmlPath,
} from "./htmlArtifactSources";

describe("html artifact sources", () => {
  it("recognizes html files by mime or extension", () => {
    expect(isHtmlPath("workspace/太阳系.html")).toBe(true);
    expect(isHtmlPath("C:\\tmp\\page.HTM")).toBe(true);
    expect(isHtmlPath("report.md")).toBe(false);
    expect(isHtmlChatFile({
      fileId: 1,
      runtimeWorkspaceId: 1,
      relativePath: "a/b.html",
      displayName: "b.html",
      mimeType: null,
      sizeBytes: 1,
      sourceType: "runtime"
    })).toBe(true);
    expect(isHtmlChatFile({
      fileId: 2,
      runtimeWorkspaceId: 1,
      relativePath: "generated",
      displayName: "generated",
      mimeType: "text/html",
      sizeBytes: 1,
      sourceType: "runtime"
    })).toBe(true);
  });

  it("extracts html paths only from completed tools in the current run", () => {
    const events = [
      {
        runtimeRunId: "run-1",
        name: "desktop_workspace_write",
        status: "completed",
        input: JSON.stringify({ path: "solar-system.html", content: "<html></html>" })
      },
      {
        runtimeRunId: "run-1",
        name: "desktop_workspace_write",
        status: "failed",
        input: JSON.stringify({ path: "failed.html" })
      },
      {
        runtimeRunId: "run-2",
        name: "write_file",
        status: "completed",
        input: JSON.stringify({ file_path: "other.html" })
      }
    ];

    expect(htmlPathsFromToolEvents(events, "run-1")).toEqual(["solar-system.html"]);
  });

  it("recognizes runtime application zip artifacts", () => {
    expect(isZipChatFile({
      fileId: 3,
      runtimeWorkspaceId: 1,
      relativePath: "workspace/sessions/rs/app.zip",
      displayName: "app.zip",
      mimeType: null,
      sizeBytes: 4171,
      sourceType: "runtime"
    })).toBe(true);
  });

  it("extracts an html filename from a local command payload", () => {
    expect(htmlPathsFromToolEvents([
      {
        runtimeRunId: "run-local",
        name: "desktop_command_run",
        status: "completed",
        input: JSON.stringify({ command: "python make.py solar-system.html" })
      }
    ], "run-local")).toEqual(["solar-system.html"]);
  });
});
