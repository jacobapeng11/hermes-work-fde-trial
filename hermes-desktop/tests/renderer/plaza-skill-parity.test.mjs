import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const synchronizedFiles = [
  "src/state/plaza-skill-store.ts",
  "src/state/plaza-skill-store.test.ts",
  "src/components/skills/PlazaSkillPanel.tsx",
  "src/components/skills/PlazaSkillPanel.test.tsx",
];

test("desktop Skill Plaza modules stay synchronized with User Web", () => {
  for (const relativePath of synchronizedFiles) {
    const userWeb = readFileSync(`../frontend/user-web/${relativePath}`, "utf8");
    const desktop = readFileSync(relativePath, "utf8");

    assert.equal(
      desktop,
      userWeb,
      `Desktop Skill Plaza file is out of sync with User Web: ${relativePath}`,
    );
  }
});
