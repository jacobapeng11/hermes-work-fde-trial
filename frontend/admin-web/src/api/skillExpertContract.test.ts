import { describe, expect, it } from "vitest";
import approveFixture from "../../../../packages/schemas/fixtures/skill-expert-integration-v1/expert-review-approve.json";
import { camelize, decamelize } from "./types";


describe("Skill/Expert review contract", () => {
  it("approves the frozen snapshot without a scope override", () => {
    const payload = camelize<{ reviewComment?: string | null }>(approveFixture);

    expect(decamelize(payload)).toEqual(approveFixture);
    expect(payload).not.toHaveProperty("shareScope");
  });
});
