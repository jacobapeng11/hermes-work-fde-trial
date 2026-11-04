import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

describe("Search label visibility", () => {
  it("loads the screen-reader utility before lazy knowledge styles", () => {
    const globalStyles = readFileSync(join(process.cwd(), "src/styles.css"), "utf8");
    const knowledgeStyles = readFileSync(
      join(process.cwd(), "src/components/knowledge/knowledge.css"),
      "utf8"
    );
    const rule = globalStyles.match(/\.srOnly\s*\{[^}]+\}/);

    expect(rule).not.toBeNull();
    expect(rule?.[0]).toContain("position: absolute");
    expect(rule?.[0]).toContain("width: 1px");
    expect(rule?.[0]).toContain("height: 1px");
    expect(rule?.[0]).toContain("overflow: hidden");
    expect(knowledgeStyles).not.toMatch(/\.srOnly\s*\{/);
  });
});
