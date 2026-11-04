import { useState } from "react";
import { Button, Checkbox, Empty, Input, Modal, Tabs, Tag, Typography } from "antd";
import { CheckOutlined, SearchOutlined } from "@ant-design/icons";
import type {
  ExpertCapabilityCatalog,
  ExpertCapabilityReference,
  ExpertCapabilitySelection
} from "@hermes-cloud/schemas";
import { usePlatformTheme } from "../../theme/platform-theme";
import { EnterpriseKnowledgeOption } from "./ExpertEditorFields";

export type ExpertCapabilityPickerKind = "skill" | "mcp" | "knowledge";

export function capabilityReferences(
  value: ExpertCapabilitySelection,
  field: "skills" | "mcps" | "knowledgeBases",
  legacyField: "skillIds" | "mcpIds" | "knowledgeBaseIds"
): ExpertCapabilityReference[] {
  return value[field]?.length
    ? value[field] || []
    : value[legacyField].map((assetId) => ({ source: "public", assetId }));
}

export function replaceCapabilityReferences(
  value: ExpertCapabilitySelection,
  field: "skills" | "mcps" | "knowledgeBases",
  legacyField: "skillIds" | "mcpIds" | "knowledgeBaseIds",
  source: "public" | "personal" | "default_installed",
  assetIds: number[],
  versionByAssetId: Map<number, number | null> = new Map()
): ExpertCapabilitySelection {
  const existing = capabilityReferences(value, field, legacyField);
  const selectedAssetIds = new Set(assetIds);
  const retained = existing.filter((item) => (
    item.source !== source && !selectedAssetIds.has(item.assetId)
  ));
  const existingVersions = new Map(
    existing
      .filter((item) => item.source === source)
      .map((item) => [item.assetId, item.versionId ?? null])
  );
  const selected = assetIds.map((assetId) => ({
    source,
    assetId,
    ...((existingVersions.get(assetId) ?? versionByAssetId.get(assetId))
      ? { versionId: existingVersions.get(assetId) ?? versionByAssetId.get(assetId) }
      : {})
  } satisfies ExpertCapabilityReference));
  const references = [...retained, ...selected];
  return {
    ...value,
    [field]: references,
    [legacyField]: references
      .filter((item) => item.source === "public")
      .map((item) => item.assetId)
  };
}

interface Props {
  mode: "create" | "edit";
  expertName?: string;
  kind: ExpertCapabilityPickerKind;
  catalog: ExpertCapabilityCatalog;
  value: ExpertCapabilitySelection;
  onChange: (value: ExpertCapabilitySelection) => void;
  onCancel: () => void;
  onConfirm: () => void;
}

const { Text } = Typography;

interface CatalogItem {
  assetId: number;
  name: string;
  description?: string | null;
  versionId?: number | null;
}

// 能力选择弹窗（按用户参考定版：600px + 来源 Tabs + 行点击选择列表 +
// 已选计数 footer）。标题保持「选择/配置+能力名」精确契约；副题移入 body。
// 行点击与 checkbox 点击双通道均单次 toggle（checkbox stopPropagation 防双切）。
function PickerGroup({
  title,
  description,
  items,
  selectedIds,
  onToggle,
  extra
}: {
  title: string;
  description: string;
  items: CatalogItem[];
  selectedIds: number[];
  onToggle: (assetId: number) => void;
  extra?: React.ReactNode;
}) {
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleItems = items.filter((item) => !normalizedQuery
    || item.name.toLocaleLowerCase().includes(normalizedQuery)
    || (item.description || "").toLocaleLowerCase().includes(normalizedQuery));
  return <div className="expertPickerGroup">
    {extra}
    <Input
      className="expertPickerSearch"
      prefix={<SearchOutlined aria-hidden/>}
      placeholder={`搜索${title}`}
      value={query}
      onChange={(event) => setQuery(event.target.value)}
      allowClear
    />
    <h4 className="expertPickerGroupTitle">{title}</h4>
    <p className="expertPickerGroupDesc">{description}</p>
    <div className="expertPickerListShell">
      {visibleItems.map((item) => {
        const isSelected = selectedIds.includes(item.assetId);
        return <div
          key={item.assetId}
          role="option"
          aria-selected={isSelected}
          className={`skill-list-item${isSelected ? " is-selected" : ""}`}
          onClick={() => onToggle(item.assetId)}
        >
          <Checkbox
            aria-label={item.name}
            checked={isSelected}
            onClick={(event) => { event.stopPropagation(); onToggle(item.assetId); }}
          />
          <span className="skill-list-item-text">
            <span className="skill-list-item-title-row">
              <Text strong className="skill-list-item-name">{item.name}</Text>
              {item.versionId != null && <Tag className="skill-list-item-version">v{item.versionId}</Tag>}
            </span>
            <Text type="secondary" className="skill-list-item-desc" ellipsis>{item.description || "未填写能力简介"}</Text>
          </span>
        </div>;
      })}
      {visibleItems.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`暂无${title}`} className="expertPickerEmpty"/>}
    </div>
  </div>;
}


export function ExpertCapabilityPicker({
  mode,
  expertName,
  kind,
  catalog,
  value,
  onChange,
  onCancel,
  onConfirm
}: Props) {
  const { theme } = usePlatformTheme();
  const capabilityLabel = kind === "skill" ? "技能" : kind === "mcp" ? "MCP" : "知识库";
  const fields = kind === "skill"
    ? (["skills", "skillIds"] as const)
    : kind === "mcp"
      ? (["mcps", "mcpIds"] as const)
      : (["knowledgeBases", "knowledgeBaseIds"] as const);

  const references = capabilityReferences(value, fields[0], fields[1]);
  const versionMap = (items: CatalogItem[]) => new Map(items.map((item) => [item.assetId, item.versionId ?? null]));
  const replace = (source: string, assetIds: number[], items: CatalogItem[]) =>
    onChange(replaceCapabilityReferences(value, fields[0], fields[1], source as "public" | "personal" | "default_installed", assetIds, versionMap(items)));

  const personalSkillIds = new Set(catalog.personalSkills.map((item) => item.assetId));
  const installedOnlySkills = catalog.installedSkills.filter((item) => !personalSkillIds.has(item.assetId));
  const mineSkills = [...catalog.personalSkills, ...installedOnlySkills];
  const mineSkillToggle = (assetId: number) => {
    const personalIds = references.filter((item) => item.source === "personal").map((item) => item.assetId);
    const installedIds = references.filter((item) => item.source === "default_installed").map((item) => item.assetId);
    if (personalSkillIds.has(assetId)) {
      const next = personalIds.includes(assetId) ? personalIds.filter((item) => item !== assetId) : [...personalIds, assetId];
      onChange(replaceCapabilityReferences(
        replaceCapabilityReferences(value, "skills", "skillIds", "default_installed", installedIds, versionMap(installedOnlySkills)),
        "skills", "skillIds", "personal", next, versionMap(catalog.personalSkills)
      ));
    } else {
      const next = installedIds.includes(assetId) ? installedIds.filter((item) => item !== assetId) : [...installedIds, assetId];
      onChange(replaceCapabilityReferences(
        replaceCapabilityReferences(value, "skills", "skillIds", "personal", personalIds, versionMap(catalog.personalSkills)),
        "skills", "skillIds", "default_installed", next, versionMap(installedOnlySkills)
      ));
    }
  };

  const count = references.length + (kind === "knowledge" && value.enterpriseKnowledgeEnabled ? 1 : 0);
  const clearSelected = () => onChange({
    ...value,
    [fields[0]]: [],
    [fields[1]]: [],
    ...(kind === "knowledge" ? { enterpriseKnowledgeEnabled: false } : {})
  } as ExpertCapabilitySelection);

  const tabs = kind === "skill"
    ? [
        { key: "mine", label: "我的技能", children: <PickerGroup title="我的技能" description="个人上传与默认 Profile 已安装的技能" items={mineSkills} selectedIds={references.filter((item) => item.source === "personal" || item.source === "default_installed").map((item) => item.assetId)} onToggle={mineSkillToggle}/> },
        { key: "market", label: "专家市场", children: <PickerGroup title="专家市场" description="选择公开市场当前可用的技能" items={catalog.marketSkills} selectedIds={references.filter((item) => item.source === "public").map((item) => item.assetId)} onToggle={(assetId) => {
          const current = references.filter((item) => item.source === "public").map((item) => item.assetId);
          replace("public", current.includes(assetId) ? current.filter((item) => item !== assetId) : [...current, assetId], catalog.marketSkills);
        }}/> }
      ]
    : kind === "mcp"
      ? [
          { key: "market", label: "MCP 市场", children: <PickerGroup title="MCP 市场" description="选择平台治理的 MCP 连接" items={catalog.marketMcps} selectedIds={references.filter((item) => item.source === "public").map((item) => item.assetId)} onToggle={(assetId) => {
            const current = references.filter((item) => item.source === "public").map((item) => item.assetId);
            replace("public", current.includes(assetId) ? current.filter((item) => item !== assetId) : [...current, assetId], catalog.marketMcps);
          }}/> },
          { key: "installed", label: "已安装 MCP", children: <PickerGroup title="已安装 MCP" description="选择默认 Profile 已安装的连接" items={catalog.installedMcps} selectedIds={references.filter((item) => item.source === "default_installed").map((item) => item.assetId)} onToggle={(assetId) => {
            const current = references.filter((item) => item.source === "default_installed").map((item) => item.assetId);
            replace("default_installed", current.includes(assetId) ? current.filter((item) => item !== assetId) : [...current, assetId], catalog.installedMcps);
          }}/> }
        ]
      : [
          { key: "enterprise", label: "企业知识库", children: <PickerGroup title="公共知识库" description="选择组织共享或已授权的知识库" items={catalog.knowledgeBases} selectedIds={references.filter((item) => item.source === "public").map((item) => item.assetId)} onToggle={(assetId) => {
            const current = references.filter((item) => item.source === "public").map((item) => item.assetId);
            replace("public", current.includes(assetId) ? current.filter((item) => item !== assetId) : [...current, assetId], catalog.knowledgeBases);
          }} extra={<EnterpriseKnowledgeOption enabled={value.enterpriseKnowledgeEnabled} onChange={(enterpriseKnowledgeEnabled) => onChange({ ...value, enterpriseKnowledgeEnabled })}/>}/> },
          { key: "personal", label: "个人知识库", children: <PickerGroup title="我的知识库" description="这里只选择已有知识库，不提供上传" items={catalog.personalKnowledgeBases} selectedIds={references.filter((item) => item.source === "personal").map((item) => item.assetId)} onToggle={(assetId) => {
            const current = references.filter((item) => item.source === "personal").map((item) => item.assetId);
            replace("personal", current.includes(assetId) ? current.filter((item) => item !== assetId) : [...current, assetId], catalog.personalKnowledgeBases);
          }}/> }
        ];

  return <Modal
    open
    title={`${mode === "create" ? "选择" : "配置"}${capabilityLabel}`}
    centered
    width={600}
    rootClassName={theme === "dark" ? "expertAntDialog expertAntDialogDark" : "expertAntDialog"}
    className="expertCapabilityPickerModal"
    onCancel={onCancel}
    footer={null}
  >
    <p className="expertPickerSubtitle">{mode === "create" ? `为新建专家选择${capabilityLabel}` : `为专家「${expertName}」选择${capabilityLabel}`}</p>
    <Tabs className="expertPickerTabs" defaultActiveKey={tabs[0].key} items={tabs} destroyInactiveTabPane/>
    <div className="expertPickerFooter">
      <span className="expertPickerCount">已选择 <b>{count}</b> 个{capabilityLabel}</span>
      {count > 0 && <Button type="link" size="small" className="expertPickerClear" autoInsertSpace={false} onClick={clearSelected}>清空已选</Button>}
      <span className="expertPickerFooterActions">
        <Button autoInsertSpace={false} onClick={onCancel}>取消</Button>
        <Button type="primary" autoInsertSpace={false} icon={<CheckOutlined aria-hidden/>} onClick={onConfirm}>确认选择</Button>
      </span>
    </div>
  </Modal>;
}
