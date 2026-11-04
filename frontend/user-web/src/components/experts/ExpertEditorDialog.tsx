import { type ReactNode, type Dispatch, type RefObject, type SetStateAction } from "react";
import { Alert, Button, Card, Drawer, Input, type InputRef } from "antd";
import { ApiOutlined, BookOutlined, InfoCircleOutlined, PlusOutlined } from "@ant-design/icons";
import type {
  ExpertCapabilityCatalog,
  PersonalSkillAssetSummary,
  ExpertCapabilitySelection,
  ExpertShareScope,
  OrganizationDirectoryNode,
  RuntimeAgentProfileSummary
} from "@hermes-cloud/schemas";
import { AvatarPicker } from "../AvatarPicker";
import { ExpertPersonalSkillUpload } from "./ExpertPersonalSkillUpload";
import { SharingFields } from "./ExpertEditorFields";
import {
  capabilityReferences,
  type ExpertCapabilityPickerKind
} from "./ExpertCapabilityPicker";
import { CapabilitySelectCard, type CapabilitySelectedItem } from "./CapabilitySelectCard";
import type { ExpertCreateDraft } from "./expertEditorModel";
import { usePlatformTheme } from "../../theme/platform-theme";

// 专家创建/配置抽屉（按用户 demo 定版：右侧 760px Drawer + 灰底卡片流 +
// 手动 80px 右对齐 Label + 能力 CapabilitySelectCard 卡）。
// 提交、校验、请求代数防串逻辑全部留在 ExpertPage，本组件只做受控渲染；
// 锚点导航按 demo 移除（整列滚动），锚点 hook 退出编辑器链路。

export interface ExpertEditorIdentity {
  name: string;
  description: string;
  avatarUrl: string;
  soulContent: string;
}

interface SharedProps {
  busy: string | null;
  error: string | null;
  catalog: ExpertCapabilityCatalog;
  catalogLoading: boolean;
  catalogError: string | null;
  configurationStatus: ReactNode;
  organizationDirectory: OrganizationDirectoryNode[];
  directoryLoading: boolean;
  directoryError: string | null;
  onRetryDirectory: () => void;
  onRequestClose: () => void;
  onErrorClear: () => void;
  onRemoval: (label: string, apply: () => void) => void;
}

function soulHint() {
  return <p className="expert-soul-hint"><InfoCircleOutlined aria-hidden/> 内容会写入专家独立 Profile 的 SOUL.md，不会影响默认 Agent 或其他专家。</p>;
}

function capabilityCards(
  catalog: ExpertCapabilityCatalog,
  catalogError: string | null,
  value: ExpertCapabilitySelection,
  applyNext: (next: ExpertCapabilitySelection) => void,
  onOpenPicker: (kind: ExpertCapabilityPickerKind) => void,
  onRemoval: (label: string, apply: () => void) => void,
  personalSkillUpload: ReactNode
) {
  const disabled = false; // 入口始终可点（无目录时 picker 内自会提示）
  const nameOf = (kind: ExpertCapabilityPickerKind, source: string, assetId: number): string => {
    const pool = kind === "skill"
      ? [...catalog.personalSkills, ...catalog.installedSkills, ...catalog.marketSkills]
      : kind === "mcp"
        ? [...catalog.installedMcps, ...catalog.marketMcps]
        : [...catalog.knowledgeBases, ...catalog.personalKnowledgeBases];
    const found = pool.find((item) => item.assetId === assetId && (kind === "mcp" || kind === "knowledge" || true));
    return found?.name || `不可用资产 #${assetId}`;
  };
  const itemsOf = (kind: ExpertCapabilityPickerKind): CapabilitySelectedItem[] => {
    const fields = kind === "skill" ? (["skills", "skillIds"] as const) : kind === "mcp" ? (["mcps", "mcpIds"] as const) : (["knowledgeBases", "knowledgeBaseIds"] as const);
    const refs = capabilityReferences(value, fields[0], fields[1]);
    const items: CapabilitySelectedItem[] = refs.map((ref) => ({
      key: `${ref.source}:${ref.assetId}`,
      name: nameOf(kind, ref.source, ref.assetId)
    }));
    if (kind === "knowledge" && value.enterpriseKnowledgeEnabled) {
      items.unshift({ key: "enterprise", name: "企业知识库" });
    }
    return items;
  };
  const removeItem = (kind: ExpertCapabilityPickerKind, key: string) => {
    if (key === "enterprise") {
      onRemoval("企业知识库", () => applyNext({ ...value, enterpriseKnowledgeEnabled: false }));
      return;
    }
    const [source, idText] = key.split(":");
    const assetId = Number(idText);
    const fields = kind === "skill" ? (["skills", "skillIds"] as const) : kind === "mcp" ? (["mcps", "mcpIds"] as const) : (["knowledgeBases", "knowledgeBaseIds"] as const);
    const label = nameOf(kind, source, assetId);
    onRemoval(label, () => {
      const refs = capabilityReferences(value, fields[0], fields[1]).filter((ref) => !(ref.source === source && ref.assetId === assetId));
      applyNext({ ...value, [fields[0]]: refs, [fields[1]]: refs.filter((item) => item.source === "public").map((item) => item.assetId) } as ExpertCapabilitySelection);
    });
  };
  const clearOf = (kind: ExpertCapabilityPickerKind) => {
    const fields = kind === "skill" ? (["skills", "skillIds"] as const) : kind === "mcp" ? (["mcps", "mcpIds"] as const) : (["knowledgeBases", "knowledgeBaseIds"] as const);
    applyNext({ ...value, [fields[0]]: [], [fields[1]]: [], ...(kind === "knowledge" ? { enterpriseKnowledgeEnabled: false } : {}) } as ExpertCapabilitySelection);
  };
  const cardOf = (kind: "skill" | "mcp" | "knowledge", title: string, icon: ReactNode, actionTitle: string, secondary?: ReactNode) => (
    <CapabilitySelectCard
      title={title}
      desc={kind === "skill" ? "从技能市场选择技能，或上传个人技能供专家使用。" : kind === "mcp" ? "从广场选择服务，或使用已安装的 MCP。" : "选择企业或个人知识库，支持多选。"}
      icon={icon}
      actionTitle={actionTitle}
      actionDesc="点击搜索并多选"
      selectedTitle={`已选择${title}`}
      selectedItems={itemsOf(kind)}
      disabled={disabled}
      onAddClick={() => onOpenPicker(kind)}
      onRemoveItem={(key) => removeItem(kind, key)}
      onClear={() => clearOf(kind)}
      secondaryAction={secondary}
    />
  );
  return <>
    {cardOf("skill", "技能", <PlusOutlined aria-hidden/>, "从技能市场选择", personalSkillUpload)}
    <div className="expert-capability-row">
      <div className="expert-capability-col">
        {cardOf("mcp", "MCP", <ApiOutlined aria-hidden/>, "MCP 广场")}
      </div>
      <div className="expert-capability-col">
        {cardOf("knowledge", "知识库", <BookOutlined aria-hidden/>, "选择知识库")}
      </div>
    </div>
  </>;
}

export function ExpertCreateEditorDialog({
  busy, error, catalog, catalogLoading, catalogError, configurationStatus,
  organizationDirectory, directoryLoading, directoryError, onRetryDirectory,
  onRequestClose, onErrorClear, onRemoval,
  draft, setDraft,
  nameInputRef,
  picker, onOpenPicker,
  onPersonalSkillUploaded,
  onSubmit
}: SharedProps & {
  draft: ExpertCreateDraft;
  setDraft: Dispatch<SetStateAction<ExpertCreateDraft>>;
  nameInputRef: RefObject<InputRef>;
  picker: ExpertCapabilityPickerKind | null;
  onOpenPicker: (kind: ExpertCapabilityPickerKind) => void;
  onPersonalSkillUploaded: (asset: PersonalSkillAssetSummary) => Promise<void>;
  onSubmit: () => void;
}) {
  const { theme } = usePlatformTheme();
  const { TextArea } = Input;
  return <Drawer
    open
    title={<div className="expert-drawer-title">
      <div className="expert-drawer-title-main">创建专家</div>
      <div className="expert-drawer-title-sub">配置专属智能体，赋予其独立的身份、技能与知识</div>
    </div>}
    width={840}
    rootClassName={theme === "dark" ? "expertAntDialog expertAntDialogDark" : "expertAntDialog"}
    className="expert-create-drawer"
    closable={busy !== "create"}
    maskClosable={busy !== "create"}
    keyboard={busy !== "create"}
    onClose={onRequestClose}
    footer={<div className="expert-drawer-footer">
      <span className="expert-drawer-footer-hint"><InfoCircleOutlined aria-hidden/> 草稿仅保存在当前浏览器，刷新后可继续。</span>
      <span className="expert-drawer-footer-actions">
        <Button autoInsertSpace={false} onClick={onRequestClose} disabled={busy === "create"}>取消</Button>
        <Button type="primary" autoInsertSpace={false} loading={busy === "create"} disabled={catalogLoading || Boolean(catalogError)} onClick={onSubmit}>创建专家</Button>
      </span>
    </div>}
  >
    {error && <Alert className="expert-drawer-error" role="alert" type="error" showIcon message={error}/>}
    <Card className="expert-block-card" title={<h3 className="expert-block-title">基础信息</h3>} bordered={false}>
      <div className="expert-form-row">
        <div className="expert-form-label">头像</div>
        <AvatarPicker compact value={draft.avatarUrl} name={draft.name || "新专家"} onChange={(avatarUrl) => setDraft((current) => ({ ...current, avatarUrl }))}/>
      </div>
      <div className="expert-form-row">
        <div className="expert-form-label"><span className="expert-form-required" aria-hidden="true">*</span> 名称</div>
        <Input
          ref={nameInputRef}
          autoFocus
          maxLength={100}
          aria-label="名称（必填）"
          value={draft.name}
          placeholder="例如：微信公众号文章专家"
          onChange={(event) => { setDraft((current) => ({ ...current, name: event.target.value })); onErrorClear(); }}
        />
      </div>
      <div className="expert-form-row">
        <div className="expert-form-label">简介</div>
        <TextArea
          aria-label="简介"
          maxLength={2000}
          rows={2}
          value={draft.description}
          placeholder="介绍专家能解决的问题和适合的任务"
          onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))}
        />
      </div>
      <div className="expert-form-row">
        <div className="expert-form-label">个性</div>
        <div className="expert-form-control">
          {soulHint()}
          <TextArea
            aria-label="专家个性"
            maxLength={200000}
            rows={6}
            className="expert-soul-input"
            value={draft.soulContent}
            placeholder={"# 角色\n你是一名擅长微信公众号内容策划与写作的专家……"}
            onChange={(event) => setDraft((current) => ({ ...current, soulContent: event.target.value }))}
          />
        </div>
      </div>
    </Card>
    <Card className="expert-block-card" title={<h3 className="expert-block-title">可见范围</h3>} bordered={false}>
      <p className="expert-block-desc">未选择可见对象，专家只供自己使用，不进入管理员审核。</p>
      <SharingFields scope={draft.shareScope} onChange={(shareScope) => setDraft((current) => ({ ...current, shareScope }))} directory={organizationDirectory} loading={directoryLoading} error={directoryError} onRetry={onRetryDirectory}/>
      {(draft.shareScope.organization || draft.shareScope.userIds.length > 0 || draft.shareScope.departmentIds.length > 0) && <div className="expert-release-row">
        <div className="expert-release-label">初始发布说明</div>
        <Input
          className="expert-release-input"
          aria-label="初始发布说明（可选）"
          maxLength={2000}
          value={draft.changeSummary}
          placeholder="例如：初始发布，包含写作与知识检索能力"
          onChange={(event) => setDraft((current) => ({ ...current, changeSummary: event.target.value }))}
        />
      </div>}
    </Card>
    {configurationStatus}
    {capabilityCards(catalog, catalogError, draft.capabilities,
      (next) => setDraft((current) => ({ ...current, capabilities: next })),
      onOpenPicker, onRemoval,
      <ExpertPersonalSkillUpload onUploaded={onPersonalSkillUploaded}/>)}
  </Drawer>;
}

export function ExpertEditEditorDialog({
  busy, error, catalog, catalogLoading, catalogError, configurationStatus,
  organizationDirectory, directoryLoading, directoryError, onRetryDirectory,
  onRequestClose, onErrorClear, onRemoval,
  editing,
  identity, setIdentity,
  capabilities, setCapabilities,
  scope, setScope,
  changeSummary, onChangeSummary,
  nameInputRef,
  picker, onOpenPicker,
  onPersonalSkillUploaded,
  onSubmit
}: SharedProps & {
  editing: RuntimeAgentProfileSummary;
  identity: ExpertEditorIdentity;
  setIdentity: Dispatch<SetStateAction<ExpertEditorIdentity>>;
  capabilities: ExpertCapabilitySelection;
  setCapabilities: Dispatch<SetStateAction<ExpertCapabilitySelection>>;
  scope: ExpertShareScope;
  setScope: Dispatch<SetStateAction<ExpertShareScope>>;
  changeSummary: string;
  onChangeSummary: (value: string) => void;
  nameInputRef: RefObject<InputRef>;
  picker: ExpertCapabilityPickerKind | null;
  onOpenPicker: (kind: ExpertCapabilityPickerKind) => void;
  onPersonalSkillUploaded: (asset: PersonalSkillAssetSummary) => Promise<void>;
  onSubmit: () => void;
}) {
  const { theme } = usePlatformTheme();
  const { TextArea } = Input;
  const saveBusy = busy === `save:${editing.runtimeAgentProfileId}`;
  const publishing = scope.organization || scope.userIds.length > 0 || scope.departmentIds.length > 0;
  return <Drawer
    open
    title={<div className="expert-drawer-title">
      <div className="expert-drawer-title-main">{editing.displayName}</div>
      <div className="expert-drawer-title-sub">独立 Hermes Profile 配置</div>
    </div>}
    width={840}
    rootClassName={theme === "dark" ? "expertAntDialog expertAntDialogDark" : "expertAntDialog"}
    className="expert-create-drawer expert-edit-drawer"
    closable={!saveBusy}
    maskClosable={!saveBusy}
    keyboard={!saveBusy}
    onClose={onRequestClose}
    footer={<div className="expert-drawer-footer">
      <span className="expert-drawer-footer-hint">{editing.reviewStatus === "pending" ? "保存后作者配置立即更新，并替换上一份待审市场版本。" : "保存后，新配置将在后续任务中生效。"}</span>
      <span className="expert-drawer-footer-actions">
        <Button autoInsertSpace={false} onClick={onRequestClose}>取消</Button>
        <Button type="primary" autoInsertSpace={false} loading={saveBusy} onClick={onSubmit}>保存{publishing ? "并提交审核" : ""}</Button>
      </span>
    </div>}
  >
    {error && <Alert className="expert-drawer-error" role="alert" type="error" showIcon message={error}/>}
    {editing.reviewStatus === "pending" && <Alert className="expert-drawer-notice" type="warning" showIcon message={<span><strong>市场版本待管理员审核</strong> — 当前配置仍可继续修改并立即供你使用；再次保存会替换上一份待审版本。</span>}/>}
    <Card className="expert-block-card" title={<h3 className="expert-block-title">基础信息</h3>} bordered={false}>
      <div className="expert-form-row">
        <div className="expert-form-label">头像</div>
        <AvatarPicker compact value={identity.avatarUrl} name={identity.name || editing.displayName} onChange={(avatarUrl) => setIdentity((current) => ({ ...current, avatarUrl }))}/>
      </div>
      <div className="expert-form-row">
        <div className="expert-form-label"><span className="expert-form-required" aria-hidden="true">*</span> 名称</div>
        <Input
          ref={nameInputRef}
          maxLength={100}
          aria-label="名称（必填）"
          value={identity.name}
          onChange={(event) => setIdentity((current) => ({ ...current, name: event.target.value }))}
        />
      </div>
      <div className="expert-form-row">
        <div className="expert-form-label">简介</div>
        <TextArea
          aria-label="简介"
          rows={2}
          maxLength={2000}
          value={identity.description}
          onChange={(event) => setIdentity((current) => ({ ...current, description: event.target.value }))}
        />
      </div>
      <div className="expert-form-row">
        <div className="expert-form-label">个性</div>
        <div className="expert-form-control">
          {soulHint()}
          <TextArea
            aria-label="专家个性"
            rows={6}
            maxLength={200000}
            className="expert-soul-input"
            value={identity.soulContent}
            onChange={(event) => setIdentity((current) => ({ ...current, soulContent: event.target.value }))}
          />
        </div>
      </div>
    </Card>
    <Card className="expert-block-card" title={<h3 className="expert-block-title">可见范围</h3>} bordered={false}>
      <p className="expert-block-desc">未选择可见对象时，专家只供自己使用。</p>
      <SharingFields scope={scope} onChange={setScope} directory={organizationDirectory} loading={directoryLoading} error={directoryError} onRetry={onRetryDirectory} selectedUsers={editing.shareScopeUsers}/>
      {editing.reviewStatus === "rejected" && editing.reviewComment && <p className="expert-review-comment">上次驳回原因：{editing.reviewComment}</p>}
      {publishing && <div className="expert-form-row expert-release-row">
        <div className="expert-release-label expert-release-label-left">改动简介{editing.reviewStatus !== null ? "（必填）" : ""}</div>
        <Input
          aria-label={editing.reviewStatus !== null ? "本次版本改动简介（必填）" : "本次版本改动简介"}
          required={editing.reviewStatus !== null}
          maxLength={2000}
          value={changeSummary}
          placeholder="发布到市场的后续修改必须说明本次改动"
          onChange={(event) => onChangeSummary(event.target.value)}
        />
      </div>}
    </Card>
    {configurationStatus}
    {capabilityCards(catalog, catalogError, capabilities,
      (next) => setCapabilities(next),
      onOpenPicker, onRemoval,
      <ExpertPersonalSkillUpload onUploaded={onPersonalSkillUploaded}/>)}
  </Drawer>;
}
