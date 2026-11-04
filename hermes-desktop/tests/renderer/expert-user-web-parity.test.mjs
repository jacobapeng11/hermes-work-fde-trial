import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const synchronizedFiles = [
  "src/components/experts/ExpertPage.tsx",
  "src/components/experts/ExpertPage.save.test.tsx",
  "src/components/experts/ExpertCatalogPanels.tsx",
  "src/components/experts/ExpertVisibilityEditor.tsx",
  "src/components/experts/ExpertVisibilityEditor.test.tsx",
  "src/components/experts/useExpertConvergencePolling.ts",
  "src/components/experts/ExpertConvergencePolling.test.tsx",
];

test("desktop Expert modules stay synchronized with User Web", () => {
  for (const relativePath of synchronizedFiles) {
    const userWeb = readFileSync(`../frontend/user-web/${relativePath}`, "utf8");
    const desktop = readFileSync(relativePath, "utf8");

    assert.equal(
      desktop,
      userWeb,
      `Desktop Expert file is out of sync with User Web: ${relativePath}`,
    );
  }
});
