import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ExpertCapabilityCatalog, ExpertCapabilitySelection } from "@hermes-cloud/schemas";
import {
  ExpertCapabilityPicker,
  replaceCapabilityReferences
} from "./ExpertCapabilityPicker";

const emptySelection = (): ExpertCapabilitySelection => ({
  skillIds: [],
  mcpIds: [],
  knowledgeBaseIds: [],
  enterpriseKnowledgeEnabled: false,
  pluginIds: []
});

const catalog: ExpertCapabilityCatalog = {
  marketSkills: [],
  installedSkills: [{
    assetId: 61,
    versionId: 81,
    name: "个人写作 Skill",
    description: "默认 Profile 已安装",
    source: "default_installed"
  }],
  skillCategories: [],
  marketMcps: [],
  installedMcps: [],
  knowledgeBases: [],
  personalSkills: [{
    assetId: 61,
    versionId: 81,
    name: "个人写作 Skill",
    description: "本人上传",
    source: "personal"
  }],
  personalKnowledgeBases: [],
  pluginIds: []
};

describe("ExpertCapabilityPicker", () => {
  it("renders an installed personal Skill once and preserves its personal ownership", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ExpertCapabilityPicker
      mode="create"
      kind="skill"
      catalog={catalog}
      value={emptySelection()}
      onChange={onChange}
      onCancel={vi.fn()}
      onConfirm={vi.fn()}
    />);

    const matchingSkills = screen.getAllByRole("checkbox", { name: /个人写作 Skill/ });
    expect(matchingSkills).toHaveLength(1);
    await user.click(matchingSkills[0]);

    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({
      skillIds: [],
      skills: [{ source: "personal", assetId: 61, versionId: 81 }]
    }));
  });

  it("keeps one stable asset identity when its source changes", () => {
    const result = replaceCapabilityReferences(
      {
        ...emptySelection(),
        skills: [{ source: "public", assetId: 7, versionId: 70 }],
        skillIds: [7]
      },
      "skills",
      "skillIds",
      "default_installed",
      [7],
      new Map([[7, 71]])
    );

    expect(result.skills).toEqual([{ source: "default_installed", assetId: 7, versionId: 71 }]);
    expect(result.skillIds).toEqual([]);
  });
});
