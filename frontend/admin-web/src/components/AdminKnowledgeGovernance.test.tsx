import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  knowledgeGovernanceSummary: vi.fn(),
  knowledgeGovernanceValidation: vi.fn(),
  knowledgeGovernanceMetricContracts: vi.fn(),
  knowledgeGovernanceAssets: vi.fn(),
  knowledgeGovernanceAsset: vi.fn(),
  knowledgeGovernanceImpact: vi.fn(),
  refreshKnowledgeGovernance: vi.fn()
}));

vi.mock("../api/client", () => ({ adminApi: api }));

import { AdminKnowledgeGovernance } from "./AdminKnowledgeGovernance";

const cache = {
  state: "hit",
  sourceFingerprint: "fingerprint",
  lastSuccessAt: "2026-07-30T08:00:00Z",
  lastAttemptAt: "2026-07-30T08:00:00Z"
};

const latestAttempt = {
  status: "succeeded",
  attemptedAt: "2026-07-30T08:00:00Z",
  sourceHash: "a".repeat(64)
};

const projectNode = {
  id: "model:project",
  type: "semantic_model",
  name: "项目",
  properties: {
    table: "crm_project_info_t",
    description: "项目与商机主模型"
  },
  sources: ["semantic_models/project.mdl.yml"]
};

const contractNode = {
  id: "metric_contract:PROJECT_DEFAULT_AMOUNT_FLOOR",
  type: "metric_contract",
  name: "PROJECT_DEFAULT_AMOUNT_FLOOR",
  properties: { severity: "warning" },
  sources: ["contracts/metric_contracts.yml"]
};

const summary = {
  schemaVersion: "1.0",
  generatedAt: "2026-07-30T08:00:00Z",
  sourceHash: "a".repeat(64),
  summary: {
    nodeCount: 1469,
    edgeCount: 1756,
    modelCount: 34,
    fieldCount: 1200,
    departmentCount: 7,
    permissionCount: 34,
    relationshipCount: 19,
    metricContractCount: 14,
    nodeTypes: {
      semantic_model: 34,
      metric_contract: 14
    },
    edgeTypes: {
      CONSTRAINED_BY: 14
    }
  },
  validationSummary: {
    errors: 0,
    warnings: 1,
    info: 0,
    status: "passed"
  },
  cache,
  latestAttempt
};

const validation = {
  validation: {
    schemaVersion: "1.0",
    generatedAt: "2026-07-30T08:00:00Z",
    sourceHash: "a".repeat(64),
    summary: {
      errors: 0,
      warnings: 1,
      info: 0,
      status: "passed"
    },
    findings: [{
      level: "WARN",
      code: "RELATIONSHIP_REVIEW",
      message: "关系仅作口径说明，当前执行策略不支持跨表查询。",
      source: "relationships.yml",
      assetId: "model:project"
    }]
  },
  cache,
  latestAttempt
};

const contracts = {
  metricContracts: {
    schemaVersion: 1,
    summary: {
      total: 14,
      errors: 7,
      warnings: 7,
      coveredModels: 10,
      totalModels: 34
    },
    rules: [{
      id: "PROJECT_DEFAULT_AMOUNT_FLOOR",
      severity: "warning",
      section: "advisory_rules",
      source: "semantic_models/project.mdl.yml",
      relatedSources: [],
      appliesToTables: ["crm_project_info_t"],
      messages: ["项目聚合默认使用 advance_amount >= 1。"]
    }],
    reviewedExemptions: []
  },
  cache,
  latestAttempt
};

const assets = {
  items: [projectNode, contractNode],
  total: 2,
  page: 1,
  pageSize: 50,
  totalPages: 1,
  cache,
  latestAttempt
};

const detail = {
  asset: projectNode,
  relationships: [{
    id: "edge:1",
    type: "CONSTRAINED_BY",
    from: projectNode.id,
    to: contractNode.id,
    properties: {},
    sources: ["contracts/metric_contracts.yml"]
  }],
  dependency: {
    name: "项目",
    type: "semantic_model",
    sources: projectNode.sources,
    upstream: [],
    downstream: [contractNode.id],
    downstreamTwoHops: []
  },
  cache,
  latestAttempt
};

const impact = {
  assetId: projectNode.id,
  asset: detail.dependency,
  affected: [contractNode],
  recommendedActions: [
    "运行 python workflows/ddl_init/check_consistency.py --strict",
    "运行 python ontology_compiler.py validate --strict"
  ],
  cache,
  latestAttempt
};

describe("AdminKnowledgeGovernance", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.knowledgeGovernanceSummary.mockResolvedValue(summary);
    api.knowledgeGovernanceValidation.mockResolvedValue(validation);
    api.knowledgeGovernanceMetricContracts.mockResolvedValue(contracts);
    api.knowledgeGovernanceAssets.mockResolvedValue(assets);
    api.knowledgeGovernanceAsset.mockResolvedValue(detail);
    api.knowledgeGovernanceImpact.mockResolvedValue(impact);
    api.refreshKnowledgeGovernance.mockResolvedValue({
      ...summary,
      cache: { ...cache, state: "refreshed" }
    });
  });

  it("shows governance health and validation findings", async () => {
    render(<AdminKnowledgeGovernance />);
    const user = userEvent.setup();

    expect(await screen.findByText("1469")).toBeVisible();
    expect(screen.getAllByText("34").length).toBeGreaterThan(0);

    await user.click(screen.getByRole("tab", { name: "校验中心" }));

    expect(screen.getByText("RELATIONSHIP_REVIEW")).toBeVisible();
    expect(screen.getByText(/当前执行策略不支持跨表查询/)).toBeVisible();
  });

  it("opens an asset and exposes its relationships and impact", async () => {
    render(<AdminKnowledgeGovernance />);
    const user = userEvent.setup();

    await screen.findByText("1469");
    await user.click(screen.getByRole("tab", { name: "资产目录" }));
    const assetList = screen.getByRole("button", { name: /项目 model:project/ });
    await user.click(assetList);

    const detailPanel = await screen.findByRole("complementary", { name: "知识资产详情" });
    expect(within(detailPanel).getByText("crm_project_info_t")).toBeVisible();

    await user.click(within(detailPanel).getByRole("button", { name: "查看关系" }));
    expect(await screen.findByText("CONSTRAINED_BY")).toBeVisible();
    expect(screen.getByText(contractNode.id)).toBeVisible();

    await user.click(screen.getByRole("tab", { name: "影响分析" }));
    expect(screen.getByText("变更将影响 1 项下游资产")).toBeVisible();
    expect(screen.getByText(/check_consistency.py --strict/)).toBeVisible();
  });

  it("filters contracts and refreshes the compiled snapshot", async () => {
    render(<AdminKnowledgeGovernance />);
    const user = userEvent.setup();

    await screen.findByText("1469");
    await user.click(screen.getByRole("tab", { name: "指标契约" }));
    expect(screen.getByText("PROJECT_DEFAULT_AMOUNT_FLOOR")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "阻断规则" }));
    expect(screen.queryByText("PROJECT_DEFAULT_AMOUNT_FLOOR")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重新扫描" }));
    await waitFor(() => expect(api.refreshKnowledgeGovernance).toHaveBeenCalledTimes(1));
    expect(api.knowledgeGovernanceValidation).toHaveBeenCalledTimes(2);
  });

  it("keeps a stale successful snapshot visible after a failed scan", async () => {
    api.knowledgeGovernanceSummary.mockResolvedValue({
      ...summary,
      cache: { ...cache, state: "stale" },
      latestAttempt: {
        status: "failed",
        attemptedAt: "2026-07-30T09:00:00Z",
        error: {
          code: "knowledge_governance_compile_failed",
          message: "知识治理编译失败，已保留上一次成功结果。"
        }
      }
    });

    render(<AdminKnowledgeGovernance />);

    expect(await screen.findByText("1469")).toBeVisible();
    expect(screen.getByText("当前显示上一次成功的治理结果")).toBeVisible();
    expect(screen.getByText(/已保留上一次成功结果/)).toBeVisible();
  });
});
