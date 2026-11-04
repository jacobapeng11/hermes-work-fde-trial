import { useState, type ReactNode } from "react";
import { Search } from "lucide-react";
import type {
  ExpertCapabilityCatalogItem,
} from "@hermes-cloud/schemas";
export { ExpertVisibilityEditor as SharingFields } from "./ExpertVisibilityEditor";

export function CapabilityGroup({
  title,
  description,
  items,
  selected,
  onChange,
  categories = [],
  disabled = false
}: {
  title: string;
  description: string;
  items: ExpertCapabilityCatalogItem[];
  selected: number[];
  onChange: (selected: number[]) => void;
  categories?: Array<{ categoryKey: string; displayName: string }>;
  disabled?: boolean;
}) {
  const [category, setCategory] = useState("all");
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleItems = (category === "all" ? items : items.filter((item) => item.categoryKey === category))
    .filter((item) => !normalizedQuery || item.name.toLocaleLowerCase().includes(normalizedQuery) || (item.description || "").toLocaleLowerCase().includes(normalizedQuery));
  const visibleCategories = categories.filter((item) => items.some((candidate) => candidate.categoryKey === item.categoryKey));
  return <section className="expertCapabilityGroup">
    <div><h4>{title}</h4><p>{description}</p></div>
    <div className="expertCapabilityOptions">
      <label className="expertCapabilitySearch"><Search size={14}/><span className="srOnly">搜索{title}</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={`搜索${title}`}/></label>
      {visibleCategories.length > 0 && <div className="expertCapabilityCategoryTabs" role="tablist" aria-label={`${title}分类`}>
        <button type="button" role="tab" aria-selected={category === "all"} onClick={() => setCategory("all")} disabled={disabled}>全部</button>
        {visibleCategories.map((item) => <button type="button" role="tab" key={item.categoryKey} aria-selected={category === item.categoryKey} onClick={() => setCategory(item.categoryKey)} disabled={disabled}>{item.displayName}</button>)}
      </div>}
      {visibleItems.map((item) => <div className="expertCapabilityOptionRow" key={item.assetId}><label><input type="checkbox" checked={selected.includes(item.assetId)} disabled={disabled} onChange={() => onChange(selected.includes(item.assetId) ? selected.filter((id) => id !== item.assetId) : [...selected, item.assetId])}/><span><strong>{item.name}</strong>{item.description && <small>{item.description}</small>}</span></label></div>)}
      {items.length === 0 && <p className="muted">暂无当前账号可用的{title}。</p>}
      {items.length > 0 && visibleItems.length === 0 && <p className="muted">没有找到匹配的资产。</p>}
    </div>
  </section>;
}

export function CapabilitySourceTabs({
  label,
  tabs,
}: {
  label: string;
  tabs: Array<{ key: string; label: string; content: ReactNode }>;
}) {
  const [activeKey, setActiveKey] = useState(tabs[0]?.key ?? "");
  const active = tabs.find((tab) => tab.key === activeKey) ?? tabs[0];
  return <section className="expertCapabilitySourceTabs">
    <div className="expertCapabilitySourceToolbar" role="tablist" aria-label={label}>
      {tabs.map((tab) => <button key={tab.key} type="button" role="tab" aria-selected={tab.key === active?.key} onClick={() => setActiveKey(tab.key)}>{tab.label}</button>)}
    </div>
    <div className="expertCapabilitySourcePanel" role="tabpanel">{active?.content}</div>
  </section>;
}

export function EnterpriseKnowledgeOption({ enabled, onChange, disabled = false }: { enabled: boolean; onChange: (enabled: boolean) => void; disabled?: boolean }) {
  return <label className="expertEnterpriseKnowledgeOption"><input type="checkbox" aria-label="企业知识库" checked={enabled} disabled={disabled} onChange={(event) => onChange(event.target.checked)}/><span><strong>企业知识库</strong><small>允许此专家按当前用户身份和权限检索企业统一维护的内部知识</small></span></label>;
}

export function CreateCapabilityCard({ title, description, count, actionLabel, onOpen, disabled = false, secondaryAction, children, anchorPrefix = "create" }: { title: string; description: string; count: number; actionLabel: string; active: boolean; onOpen: () => void; disabled?: boolean; secondaryAction?: ReactNode; children: ReactNode; anchorPrefix?: "create" | "edit" }) {
  const anchorId = `${anchorPrefix}-${title === "技能" ? "skill" : title === "MCP" ? "mcp" : "knowledge"}`;
  const selectionLabel = title === "技能" ? "技能" : title === "MCP" ? "MCP" : "知识库";
  return <section className="expertCreateCapabilityCard expertEditorSection" id={anchorId}>
    <div className="expertEditorSectionTitle"><h3>{title}</h3><p>{description}</p></div>
    <div className={`expertResourceConfigRow${title === "MCP" ? " expertMcpResourceRow" : ""}`}>
      <div className="expertResourceEntries">
        <button type="button" className="expertResourceEntry" onClick={onOpen} disabled={disabled}>
          <span><strong>{actionLabel}</strong><small>{count ? `已选择 ${count} 项` : "点击搜索并多选"}</small></span><b aria-hidden="true">+</b>
        </button>
        {secondaryAction}
      </div>
      <aside className="expertSelectedResourcePanel">
        <div className="expertSelectedResourceTitle">已选择{selectionLabel}</div>
        <div className="expertSelectedResourceContent">{children}</div>
      </aside>
    </div>
    {title === "知识库" && <div className="expertKnowledgeChooseOnly"><p>这里只选择已有知识库，不在专家页上传文件。</p><small>如需新增，请先前往知识库配置。</small></div>}
  </section>;
}
