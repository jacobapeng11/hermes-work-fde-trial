import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  DefaultProfileAssetOperationAccepted,
  DefaultProfileAssetView,
} from "@hermes-cloud/schemas";
import { plazaApi } from "../api/plaza-api";
import { userApi } from "../api/client";
import { PlazaSkillStore } from "./plaza-skill-store";

function view(overrides: Partial<DefaultProfileAssetView> = {}): DefaultProfileAssetView {
  return {
    runtimeWorkspaceId: 3,
    runtimeAgentProfileId: 10,
    desiredRevision: 1,
    appliedRevision: 1,
    convergenceStatus: "applied",
    skills: [],
    mcps: [],
    ...overrides,
  };
}

function accepted(assetId: number): DefaultProfileAssetOperationAccepted {
  return {
    operationId: `asset-${assetId}`,
    runtimeWorkspaceId: 3,
    runtimeAgentProfileId: 10,
    assetType: "skill",
    assetId,
    action: "install",
    desiredRevision: 2,
    appliedRevision: 1,
    convergenceStatus: "updating",
  };
}

describe("PlazaSkillStore stable Profile assets", () => {
  beforeEach(() => {
    vi.spyOn(userApi, "personalSkillCategories").mockResolvedValue([
      { categoryKey: "system", displayName: "系统技能", isBuiltin: true, scope: "system" },
      { categoryKey: "uncategorized", displayName: "未分类", isBuiltin: true, scope: "uncategorized" },
    ]);
    vi.spyOn(userApi, "systemSkills").mockResolvedValue([]);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.clearAllTimers();
  });

  it("maps stable Skill IDs and Configured/Applied state", async () => {
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view({
      desiredRevision: 2,
      appliedRevision: 1,
      skills: [{
        assetType: "skill",
        assetId: 71,
        currentVersionId: 81,
        assetScope: "public",
        version: 1,
        desired: true,
        applied: false,
        name: "Search",
        status: "published",
        categoryKey: "productivity",
      }],
    }));
    const store = new PlazaSkillStore();

    await store.load(3);

    expect(store.getSnapshot().marketItems[0]).toMatchObject({
      assetId: 71,
      state: "current",
      operating: true,
    });
    expect(store.getSnapshot().installedItems[0]).toMatchObject({
      assetId: 71,
      catalogAssetId: 71,
    });
    expect(store.getSnapshot().pendingOperations[0]).toMatchObject({
      targetKey: "asset:71",
      status: "running",
    });
  });

  it("preserves an organization category display name referenced by the default Profile", async () => {
    const categoryKey = "category-f6e0a1e2ac41945a";
    vi.mocked(userApi.personalSkillCategories).mockResolvedValue([
      { categoryKey: "system", displayName: "系统技能", isBuiltin: true, scope: "system" },
      { categoryKey: "uncategorized", displayName: "未分类", isBuiltin: true, scope: "uncategorized" },
      { categoryKey, displayName: "111", isBuiltin: false, scope: "organization" },
    ]);
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([{
      skillId: 1847,
      skillVersionId: 1848,
      slug: "scheduled-task",
      version: 1,
      name: "scheduled_task",
      description: null,
      categoryKey,
      status: "private",
      source: "personal_asset",
      applied: true,
    }]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view({
      skills: [{
        assetType: "skill",
        assetId: 1847,
        currentVersionId: 1848,
        assetScope: "personal",
        version: 1,
        desired: true,
        applied: true,
        name: "scheduled_task",
        status: "private",
        categoryKey,
      }],
    }));
    const store = new PlazaSkillStore();

    await store.load(3);

    expect(store.getSnapshot().categories).toContainEqual(expect.objectContaining({
      categoryKey,
      displayName: "111",
      scope: "organization",
    }));
  });

  it("merges read-only Runtime system Skills into My Skills", async () => {
    vi.mocked(userApi.systemSkills).mockResolvedValue([{
      key: "hermes-agent",
      name: "hermes-agent",
      description: "Configure Hermes Agent.",
      categoryKey: "system",
      source: "system",
    }]);
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view());
    const store = new PlazaSkillStore();

    await store.load(3);

    expect(store.getSnapshot().personalItems).toContainEqual(expect.objectContaining({
      source: "system",
      systemKey: "hermes-agent",
      description: "Configure Hermes Agent.",
      categoryKey: "system",
    }));
  });

  it("does not project a public duplicate of a Runtime system Skill into the market", async () => {
    vi.mocked(userApi.systemSkills).mockResolvedValue([{
      key: "hermes-agent",
      name: "hermes-agent",
      description: null,
      categoryKey: "system",
      source: "system",
    }]);
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view({
      skills: [{
        assetType: "skill",
        assetId: 99,
        currentVersionId: 199,
        assetScope: "public",
        version: 1,
        desired: false,
        applied: false,
        name: "Hermes Agent",
        status: "published",
        categoryKey: "other",
      }],
    }));
    const store = new PlazaSkillStore();

    await store.load(3);

    expect(store.getSnapshot().marketItems).toEqual([]);
    expect(store.getSnapshot().personalItems).toHaveLength(1);
  });

  it("does not project an archived personal migration copy of a Runtime system Skill", async () => {
    vi.mocked(userApi.systemSkills).mockResolvedValue([{
      key: "hermes-agent",
      name: "hermes-agent",
      description: null,
      categoryKey: "system",
      source: "system",
    }]);
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([{
      skillId: 1719,
      skillVersionId: 1715,
      slug: "legacy-expert-source-593",
      version: 1,
      name: "hermes-agent",
      description: null,
      categoryKey: "uncategorized",
      status: "archived",
      source: "personal_asset",
      applied: false,
    }]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view());
    const store = new PlazaSkillStore();

    await store.load(3);

    expect(store.getSnapshot().personalItems).toEqual([
      expect.objectContaining({ source: "system", systemKey: "hermes-agent" }),
    ]);
  });

  it("updates a personal Skill description without touching profile operations", async () => {
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([{
      skillId: 75,
      skillVersionId: 85,
      slug: "description-skill",
      version: 1,
      name: "Description Skill",
      description: "默认说明",
      status: "private",
      source: "personal_asset",
      applied: true,
    }]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view({
      skills: [{
        assetType: "skill",
        assetId: 75,
        currentVersionId: 85,
        assetScope: "personal",
        version: 1,
        desired: true,
        applied: true,
        name: "Description Skill",
        description: "默认说明",
        status: "private",
        categoryKey: "uncategorized",
      }],
    }));
    const update = vi.spyOn(userApi, "updatePersonalSkillDescription").mockResolvedValue({
      skillId: 75,
      skillVersionId: 85,
      slug: "description-skill",
      version: 1,
      name: "Description Skill",
      description: "自定义说明",
      status: "private",
      source: "personal_asset",
      applied: true,
    });
    const store = new PlazaSkillStore();
    await store.load(3);

    await store.submitDescriptionUpdate(75, "自定义说明", "已重命名 Skill");

    expect(update).toHaveBeenCalledWith(75, "自定义说明", "已重命名 Skill");
  });

  it("adds a newly created private category to the local category registry", async () => {
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view());
    vi.spyOn(userApi, "createPersonalSkillCategory").mockResolvedValue({
      categoryKey: "personal-writing",
      displayName: "写作",
      isBuiltin: false,
      scope: "personal",
    });
    const store = new PlazaSkillStore();
    await store.load(3);

    const created = await store.createPersonalCategory("写作");

    expect(userApi.createPersonalSkillCategory).toHaveBeenCalledWith("写作");
    expect(created.categoryKey).toBe("personal-writing");
    expect(store.getSnapshot().categories).toContainEqual(expect.objectContaining({
      categoryKey: "personal-writing",
      scope: "personal",
    }));
  });

  it("submits installation with the stable asset ID", async () => {
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([]);
    vi.spyOn(plazaApi, "install").mockResolvedValue(accepted(72));
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view());
    const store = new PlazaSkillStore();
    await store.load(3);

    await store.submitInstall(72);

    expect(plazaApi.install).toHaveBeenCalledWith(3, 72);
    expect(store.getSnapshot().pendingOperations).toContainEqual(
      expect.objectContaining({ operationId: "asset-72", targetKey: "asset:72" }),
    );
  });

  it("keeps personal ownership separate from the default Profile binding", async () => {
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([{
      skillId: 73,
      skillVersionId: 83,
      slug: "private-search",
      version: 1,
      name: "Private Search",
      description: null,
      status: "private",
      source: "personal_asset",
      applied: false,
    }]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view({
      skills: [{
        assetType: "skill",
        assetId: 73,
        currentVersionId: 83,
        assetScope: "personal",
        version: 1,
        desired: false,
        applied: false,
        name: "Private Search",
        status: "private",
        categoryKey: "personal",
      }],
    }));
    const install = vi.spyOn(userApi, "installDefaultProfileAsset").mockResolvedValue(accepted(73));
    const store = new PlazaSkillStore();

    await store.load(3);

    expect(store.getSnapshot().marketItems).toEqual([]);
    expect(store.getSnapshot().personalItems).toContainEqual(expect.objectContaining({
      assetId: 73,
      desired: false,
      applied: false,
    }));
    await store.submitPersonalInstall(73);
    expect(install).toHaveBeenCalledWith(3, "skill", 73);
  });

  it("keeps personal Skill uninstall separate from permanent deletion", async () => {
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([{
      skillId: 74,
      skillVersionId: 84,
      slug: "private-delete",
      version: 1,
      name: "Private Delete",
      description: null,
      status: "private",
      source: "personal_asset",
      applied: true,
    }]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view({
      skills: [{
        assetType: "skill",
        assetId: 74,
        currentVersionId: 84,
        assetScope: "personal",
        version: 1,
        desired: true,
        applied: true,
        name: "Private Delete",
        status: "private",
        categoryKey: "personal",
      }],
    }));
    const remove = vi.spyOn(userApi, "deletePersonalSkill").mockResolvedValue({
      skillId: 74,
      rolloutId: "personal-skill-delete-74",
      affectedProfileIds: [10, 11],
      desiredRevisions: { "10": 2 },
    });
    const uninstallDefault = vi.spyOn(userApi, "uninstallDefaultProfileAsset")
      .mockResolvedValue({ ...accepted(74), action: "uninstall" });
    const store = new PlazaSkillStore();

    await store.load(3);
    await store.submitPersonalUninstall(74);

    expect(uninstallDefault).toHaveBeenCalledWith(3, "skill", 74);
    expect(remove).not.toHaveBeenCalled();

    const result = await store.submitPersonalDelete(74);
    expect(remove).toHaveBeenCalledWith(74);
    expect(result).toMatchObject({ affectedProfileIds: [10, 11] });
  });

  it("keeps a failed operation visible and propagates the submission error to the dialog", async () => {
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view());
    vi.spyOn(userApi, "uploadAndInstallPersonalSkill").mockRejectedValue(new Error("invalid skill package"));
    const store = new PlazaSkillStore();
    await store.load(3);

    await expect(store.submitPersonalUpload(new File(["zip"], "broken.zip"), "uncategorized"))
      .rejects.toThrow("invalid skill package");
    expect(store.getSnapshot().pendingOperations).toContainEqual(expect.objectContaining({
      action: "personal_upload",
      status: "failed",
      errorMessage: "invalid skill package，可重试",
    }));
  });

  it("does not compose personal upload and default Profile install in the browser", async () => {
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view());
    const asset = {
      skillId: 75,
      skillVersionId: 85,
      slug: "private-atomic",
      version: 1,
      name: "Private Atomic",
      description: null,
      status: "private",
      source: "personal_asset",
      applied: false,
    } as const;
    const combined = vi.spyOn(userApi, "uploadAndInstallPersonalSkill")
      .mockResolvedValue({ asset, operation: accepted(75) });
    const legacyUpload = vi.spyOn(userApi, "uploadPersonalSkill");
    const legacyInstall = vi.spyOn(userApi, "installDefaultProfileAsset");
    const store = new PlazaSkillStore();
    await store.load(3);

    const file = new File(["zip"], "atomic.zip");
    await store.submitPersonalUpload(file, "uncategorized");

    expect(combined).toHaveBeenCalledWith(3, { file, categoryKey: "uncategorized" });
    expect(legacyUpload).not.toHaveBeenCalled();
    expect(legacyInstall).not.toHaveBeenCalled();
  });

  it("never classifies a personal Skill as market and shows its semantic version", async () => {
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view({
      skills: [{
        assetType: "skill",
        assetId: 75,
        currentVersionId: 1747,
        assetScope: "personal",
        version: 1,
        configured: true,
        desired: true,
        applied: true,
        name: "Legacy Personal Skill",
        status: "archived",
        categoryKey: "personal",
      }],
    }));
    const store = new PlazaSkillStore();

    await store.load(3);

    expect(store.getSnapshot().marketItems).toEqual([]);
    expect(store.getSnapshot().personalItems).toContainEqual(expect.objectContaining({
      assetId: 75,
      versionLabel: "1",
    }));
  });

  it("surfaces a failed convergence instead of leaving the Skill installing", async () => {
    vi.spyOn(userApi, "personalSkillAssets").mockResolvedValue([]);
    vi.spyOn(plazaApi, "getDefaultView").mockResolvedValue(view({
      desiredRevision: 2,
      appliedRevision: 1,
      convergenceStatus: "failed",
      lastError: "PROFILE_PROJECTION_STATE_DIVERGED",
      skills: [{
        assetType: "skill",
        assetId: 76,
        currentVersionId: 1760,
        assetScope: "public",
        version: 2,
        desired: true,
        applied: false,
        name: "Search",
        status: "published",
      }],
    }));
    const store = new PlazaSkillStore();

    await store.load(3);

    expect(store.getSnapshot().pendingOperations).toContainEqual(expect.objectContaining({
      targetKey: "asset:76",
      status: "failed",
      errorMessage: "PROFILE_PROJECTION_STATE_DIVERGED",
    }));
  });
});
