import { describe, expect, it } from "vitest";
import type { ExpertMarketDetail, ExpertMarketItem } from "@hermes-cloud/schemas";
import marketDetailFixture from "../../../packages/schemas/fixtures/skill-expert-integration-v1/expert-market-detail.json";
import marketItemFixture from "../../../packages/schemas/fixtures/skill-expert-integration-v1/expert-market-item.json";
import { camelize } from "./types";


describe("Skill/Expert public contract", () => {
  it("keeps the market list narrow and exposes only approved detail configuration", () => {
    const item = camelize<ExpertMarketItem>(marketItemFixture);
    const detail = camelize<ExpertMarketDetail>(marketDetailFixture);

    expect(item.allowedActions).toMatchObject({
      viewDetail: true,
      install: false,
      uninstall: true,
      summon: true
    });
    expect(item.installed).toBe(true);
    expect(item).not.toHaveProperty("skills");
    expect(item).not.toHaveProperty("mcps");
    expect(item).not.toHaveProperty("knowledgeBases");
    expect(item).not.toHaveProperty("soulContent");
    expect(detail).toMatchObject({
      soulContent: expect.stringContaining("写作专家"),
      systemPrompt: "先确认文章目标和读者，再生成内容。",
      skills: [{ name: "结构化写作" }],
      mcps: [{ name: "公开资料检索" }],
      knowledgeBases: [{ name: "品牌写作规范" }]
    });
    for (const forbidden of [
      "payload",
      "sourceAssetId",
      "sourceVersionId",
      "sourceMetadata",
      "storageUri",
      "sha256"
    ]) {
      expect(JSON.stringify(detail)).not.toContain(forbidden);
    }
  });
});
