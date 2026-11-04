import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ExpertMarketDetail, ExpertMarketItem } from "@hermes-cloud/schemas";

import { ExpertMarketDetailDialog } from "./ExpertCatalogPanels";

const allowedActions = {
  viewDetail: true,
  install: true,
  uninstall: false,
  summon: false,
  edit: false,
  copy: false,
  publish: false,
  unlist: false,
  delete: false,
};

const target: ExpertMarketItem = {
  expertPackageId: 17,
  expertPackageVersionId: 32,
  name: "市场写作专家",
  description: "公众号文章写作",
  avatarUrl: null,
  providerName: "创建者甲",
  version: 2,
  installed: false,
  installedRuntimeAgentProfileId: null,
  catalogStatus: "published",
  executionStatus: "enabled",
  allowedActions,
};

const detail: ExpertMarketDetail = {
  ...target,
  soulContent: "# 市场人格\n\n保持准确。",
  systemPrompt: "先确认目标读者。",
  skills: [{ name: "结构化写作", description: "生成文章结构" }],
  mcps: [{ name: "资料检索", description: null }],
  knowledgeBases: [],
};

function renderDetail(value: ExpertMarketDetail) {
  render(<ExpertMarketDetailDialog
    target={target}
    detail={value}
    loading={false}
    error={null}
    busy={null}
    onClose={vi.fn()}
    onRetry={vi.fn()}
    onInstall={vi.fn()}
    onSummon={vi.fn()}
    onUninstall={vi.fn()}
  />);
  return screen.getByRole("dialog", { name: "市场写作专家" });
}

describe("ExpertMarketDetailDialog", () => {
  it("renders the approved configuration without internal artifact fields", async () => {
    const dialog = renderDetail(detail);

    // antd Modal appear 阶段内容 opacity:0，先等首断言可见再继续同步断言。
    await waitFor(() => expect(within(dialog).getByText("市场人格", { exact: false })).toBeVisible());
    expect(within(dialog).getByText("先确认目标读者。")).toBeVisible();
    expect(within(dialog).getByText("结构化写作")).toBeVisible();
    expect(within(dialog).getByText("生成文章结构")).toBeVisible();
    expect(within(dialog).getByText("资料检索")).toBeVisible();
    expect(within(dialog).getByText("未填写能力简介")).toBeVisible();
    expect(dialog).not.toHaveTextContent("sourceAssetId");
    expect(dialog).not.toHaveTextContent("payload");
  });

  it("shows explicit empty states for missing configuration", async () => {
    const dialog = renderDetail({
      ...detail,
      soulContent: "",
      systemPrompt: null,
      skills: [],
      mcps: [],
      knowledgeBases: [],
    });

    await waitFor(() => expect(within(dialog).getByText("未配置人格设定")).toBeVisible());
    expect(within(dialog).getByText("未配置系统提示词")).toBeVisible();
    expect(within(dialog).getByText("未配置技能")).toBeVisible();
    expect(within(dialog).getByText("未配置MCP")).toBeVisible();
    expect(within(dialog).getByText("未配置知识库")).toBeVisible();
  });
});
