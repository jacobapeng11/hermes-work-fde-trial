import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

describe("Application Agent workspace header layout", () => {
  it("offsets the files button from the application back button", () => {
    const css = readFileSync(join(process.cwd(), "src/styles.css"), "utf8");
    const desktopRule = css.match(
      /\.appHeader\.applicationDebugHeader\[data-view="chat"\] \.headerFilesButton\s*\{[^}]+\}/
    );

    expect(desktopRule).not.toBeNull();
    expect(desktopRule?.[0]).toContain("right: 168px;");
    expect(css).not.toContain(
      '.applicationDebugHeader .appHeader[data-view="chat"] .headerFilesButton'
    );
  });
});
