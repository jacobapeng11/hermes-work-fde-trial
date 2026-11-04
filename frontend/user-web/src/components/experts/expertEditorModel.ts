import type {
  ExpertCapabilitySelection,
  ExpertShareScope
} from "@hermes-cloud/schemas";
import { DEFAULT_AVATAR_VALUE } from "../../avatar";
import { capabilityReferences } from "./ExpertCapabilityPicker";

export interface ExpertCreateDraft {
  name: string;
  description: string;
  avatarUrl: string;
  soulContent: string;
  shareScope: ExpertShareScope;
  capabilities: ExpertCapabilitySelection;
  changeSummary: string;
}

export const emptyCapabilities = (): ExpertCapabilitySelection => ({
  skillIds: [],
  mcpIds: [],
  knowledgeBaseIds: [],
  enterpriseKnowledgeEnabled: false,
  pluginIds: []
});

export const copyCapabilities = (
  capabilities: ExpertCapabilitySelection
): ExpertCapabilitySelection => ({
  skillIds: [...capabilities.skillIds],
  mcpIds: [...capabilities.mcpIds],
  knowledgeBaseIds: [...capabilities.knowledgeBaseIds],
  enterpriseKnowledgeEnabled: capabilities.enterpriseKnowledgeEnabled ?? false,
  pluginIds: [...capabilities.pluginIds],
  skills: capabilities.skills?.map((item) => ({ ...item })),
  mcps: capabilities.mcps?.map((item) => ({ ...item })),
  knowledgeBases: capabilities.knowledgeBases?.map((item) => ({ ...item }))
});

export const emptyScope = (): ExpertShareScope => ({
  organization: false,
  userIds: [],
  departmentIds: []
});

export const canonicalScope = (scope: ExpertShareScope) => ({
  organization: scope.organization,
  userIds: [...scope.userIds].sort((left, right) => left - right),
  departmentIds: [...scope.departmentIds].sort((left, right) => left.localeCompare(right))
});

const canonicalCapabilityReferences = (
  capabilities: ExpertCapabilitySelection,
  field: "skills" | "mcps" | "knowledgeBases",
  legacyField: "skillIds" | "mcpIds" | "knowledgeBaseIds"
) => capabilityReferences(capabilities, field, legacyField)
  .map((item) => ({
    source: item.source,
    assetId: item.assetId,
    versionId: item.versionId ?? null
  }))
  .sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)));

export const runtimeCapabilitiesEqual = (
  left: ExpertCapabilitySelection,
  right: ExpertCapabilitySelection
) => JSON.stringify({
  skills: canonicalCapabilityReferences(left, "skills", "skillIds"),
  mcps: canonicalCapabilityReferences(left, "mcps", "mcpIds"),
  knowledgeBases: canonicalCapabilityReferences(left, "knowledgeBases", "knowledgeBaseIds"),
  enterpriseKnowledgeEnabled: left.enterpriseKnowledgeEnabled ?? false
}) === JSON.stringify({
  skills: canonicalCapabilityReferences(right, "skills", "skillIds"),
  mcps: canonicalCapabilityReferences(right, "mcps", "mcpIds"),
  knowledgeBases: canonicalCapabilityReferences(right, "knowledgeBases", "knowledgeBaseIds"),
  enterpriseKnowledgeEnabled: right.enterpriseKnowledgeEnabled ?? false
});

export const emptyCreateDraft = (): ExpertCreateDraft => ({
  name: "",
  description: "",
  avatarUrl: DEFAULT_AVATAR_VALUE,
  soulContent: "",
  shareScope: emptyScope(),
  capabilities: emptyCapabilities(),
  changeSummary: ""
});

export const createDraftStorageKey = (runtimeWorkspaceId: number) =>
  `hermes.expert-create-draft.v1:${runtimeWorkspaceId}`;

export function loadCreateDraft(runtimeWorkspaceId: number | null): ExpertCreateDraft {
  if (!runtimeWorkspaceId || typeof window === "undefined") return emptyCreateDraft();
  try {
    const stored = window.localStorage.getItem(createDraftStorageKey(runtimeWorkspaceId));
    if (!stored) return emptyCreateDraft();
    const parsed = JSON.parse(stored) as Partial<ExpertCreateDraft>;
    return {
      ...emptyCreateDraft(),
      ...parsed,
      shareScope: { ...emptyScope(), ...(parsed.shareScope || {}) },
      capabilities: { ...emptyCapabilities(), ...(parsed.capabilities || {}) }
    };
  } catch {
    return emptyCreateDraft();
  }
}
