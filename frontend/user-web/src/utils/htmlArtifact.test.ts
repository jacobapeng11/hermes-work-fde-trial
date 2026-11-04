import { describe, expect, it } from "vitest";

import {
  extractHtmlArtifacts,
  htmlDocumentTitle,
  isCompleteHtmlDocument,
} from "./htmlArtifact";

describe("extractHtmlArtifacts", () => {
  it("extracts closed complete html blocks", () => {
    const markdown = [
      "before",
      "```html",
      "<!doctype html><html><body>v1</body></html>",
      "```",
      "```html",
      "<html><body>v2</body></html>",
      "```",
    ].join("\n");

    expect(extractHtmlArtifacts(markdown)).toHaveLength(2);
    expect(extractHtmlArtifacts(markdown).at(-1)).toContain("v2");
  });

  it("ignores fragments and unclosed blocks", () => {
    const markdown = [
      "```html",
      "<div>fragment</div>",
      "```",
      "```html",
      "<html><body>unfinished</body>",
    ].join("\n");

    expect(extractHtmlArtifacts(markdown)).toEqual([]);
  });

  it("supports longer closing fences", () => {
    const markdown = "~~~~html\n<html></html>\n~~~~~~";

    expect(extractHtmlArtifacts(markdown)).toHaveLength(1);
  });
});

describe("html document helpers", () => {
  it("recognizes html and doctype documents", () => {
    expect(isCompleteHtmlDocument("<HTML></HTML>")).toBe(true);
    expect(isCompleteHtmlDocument("<!DOCTYPE html><div></div>")).toBe(true);
    expect(isCompleteHtmlDocument("<div></div>")).toBe(false);
  });

  it("normalizes document title", () => {
    expect(htmlDocumentTitle("<html><title>\n  销售看板 \n</title></html>")).toBe("销售看板");
    expect(htmlDocumentTitle("<html></html>")).toBeNull();
  });
});
