import type {
  ExpertCapabilityCatalog,
  ExpertCapabilityReference,
  ExpertCapabilitySelection
} from "@hermes-cloud/schemas";
import {
  capabilityReferences,
  type ExpertCapabilityPickerKind
} from "./ExpertCapabilityPicker";

function removeCapabilityReference(
  value: ExpertCapabilitySelection,
  field: "skills" | "mcps" | "knowledgeBases",
  legacyField: "skillIds" | "mcpIds" | "knowledgeBaseIds",
  removed: ExpertCapabilityReference
): ExpertCapabilitySelection {
  const references = capabilityReferences(value, field, legacyField)
    .filter((item) => item.source !== removed.source || item.assetId !== removed.assetId);
  return {
    ...value,
    [field]: references,
    [legacyField]: references
      .filter((item) => item.source === "public")
      .map((item) => item.assetId)
  };
}

interface Props {
  kind: ExpertCapabilityPickerKind;
  capabilities: ExpertCapabilitySelection;
  catalog: ExpertCapabilityCatalog;
  onOpen: () => void;
  disabled?: boolean;
  loading?: boolean;
  error?: string | null;
  showAction?: boolean;
  onRequestRemoval?: (label: string, next: ExpertCapabilitySelection) => void;
}

export function ExpertCapabilitySummary({
  kind,
  capabilities,
  catalog,
  onOpen,
  disabled = false,
  loading = false,
  error = null,
  showAction = true,
  onRequestRemoval
}: Props) {
  const fields = kind === "skill"
    ? ["skills", "skillIds"] as const
    : kind === "mcp"
      ? ["mcps", "mcpIds"] as const
      : ["knowledgeBases", "knowledgeBaseIds"] as const;
  const references = capabilityReferences(capabilities, fields[0], fields[1]);
  const relevantCatalogItems = kind === "skill"
    ? [...catalog.marketSkills, ...catalog.installedSkills, ...catalog.personalSkills]
    : kind === "mcp"
      ? [...catalog.marketMcps, ...catalog.installedMcps]
      : [...catalog.knowledgeBases, ...catalog.personalKnowledgeBases];
  const capabilityName = (reference: ExpertCapabilityReference) => relevantCatalogItems
    .find((item) => item.assetId === reference.assetId)?.name || `不可用资产 #${reference.assetId}`;
  const count = references.length
    + (kind === "knowledge" && capabilities.enterpriseKnowledgeEnabled ? 1 : 0);

  return <div className="expertCapabilitySummary">
    <div className="expertSelectedChips">
      {kind === "knowledge" && capabilities.enterpriseKnowledgeEnabled && <span>
        企业知识库
        {onRequestRemoval && !disabled && <button type="button" aria-label="移除 企业知识库" onClick={() => onRequestRemoval("企业知识库", { ...capabilities, enterpriseKnowledgeEnabled: false })}>×</button>}
      </span>}
      {references.map((reference) => {
        const label = capabilityName(reference);
        return <span key={`${reference.source}:${reference.assetId}`}>
          {label}
          {onRequestRemoval && !disabled && <button type="button" aria-label={`移除 ${label}`} onClick={() => onRequestRemoval(label, removeCapabilityReference(capabilities, fields[0], fields[1], reference))}>×</button>}
        </span>;
      })}
      {count === 0 && <small>尚未选择，可稍后继续配置。</small>}
    </div>
    {showAction && <button type="button" className="secondary" onClick={onOpen} disabled={disabled || loading || Boolean(error)}>{count ? `管理已选（${count}）` : "选择能力"}</button>}
  </div>;
}
