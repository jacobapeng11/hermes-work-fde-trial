import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ExpertProjectionStatus } from "./ExpertProjectionStatus";


describe("ExpertProjectionStatus", () => {
  it("separates desired, projected and runtime-effective states", () => {
    render(<ExpertProjectionStatus profile={{
      runtimeAgentProfileId: 7,
      runtimeWorkspaceId: 1,
      hermesProfileName: "expert-7",
      profileKind: "expert",
      displayName: "写作专家",
      soulContent: "",
      ownershipType: "market_installed",
      updateAvailable: false,
      desiredRevision: 4,
      appliedRevision: 3,
      configuredExpertVersionId: 32,
      appliedExpertVersionId: 31,
      convergenceStatus: "updating",
      effectiveStatus: "updating",
      capabilities: {
        skillIds: [],
        mcpIds: [],
        knowledgeBaseIds: [],
        enterpriseKnowledgeEnabled: false,
        pluginIds: []
      },
      shareScope: { organization: false, userIds: [], departmentIds: [] },
      shareScopeUsers: [],
      allowedActions: { viewDetail: true, install: false, uninstall: true, summon: true, edit: false, copy: false, publish: false, unlist: false, delete: false },
      status: "active"
    }}/>)

    expect(screen.getByText("专家版本 #32")).toBeVisible();
    expect(screen.getByText(/专家版本 #31 · 自动更新中/)).toBeVisible();
    expect(screen.getByText("等待投影")).toBeVisible();
  });
});
