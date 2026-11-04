import { useEffect, useState } from "react";
import { Alert, Button, Input, Modal, Tag } from "antd";
import type { SkillCategorySummary } from "@hermes-cloud/schemas";
import type { PersonalSkillViewItem } from "../../state/plaza-skill-store";
import type { RemovalConfirmation, SkillSelection } from "./shared";
import { categoryLabel, versionLabel } from "./shared";
import { usePlatformTheme } from "../../theme/platform-theme";

export interface SkillDetailDialogProps {
  selection: SkillSelection | null;
  // removal 弹窗激活时隐藏但保持挂载，编辑草稿不丢失（与迁移前根组件持态语义一致）
  hidden: boolean;
  categories: SkillCategorySummary[];
  installTargetLabel: string;
  canEdit: boolean;
  onClose: () => void;
  onInstall: (selection: SkillSelection) => void;
  onRemovalRequest: (removal: RemovalConfirmation) => void;
  // 返回 null 表示保存成功；返回字符串为需要内联展示的错误文案
  onSaveDetails: (skillId: number, name: string, description: string) => Promise<string | null>;
}

// antd Modal 的可访问名由 title（技能详情）提供；原逐技能 aria-label 不再适用，
// 同一时间只有一个详情弹窗，查询语义不受影响。
export function SkillDetailDialog({
  selection,
  hidden,
  categories,
  installTargetLabel,
  canEdit,
  onClose,
  onInstall,
  onRemovalRequest,
  onSaveDetails,
}: SkillDetailDialogProps) {
  const { theme } = usePlatformTheme();
  const [editing, setEditing] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [descriptionDraft, setDescriptionDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [descriptionError, setDescriptionError] = useState<string | null>(null);

  useEffect(() => {
    if (!selection) return;
    setEditing(false);
    setNameDraft(selection.item.name);
    setDescriptionDraft(selection.item.description ?? "");
    setDescriptionError(null);
  }, [selection]);


  const saveSkillDetails = async () => {
    if (!selection || saving || selection.kind !== "mine") return;
    const name = nameDraft.trim();
    if (!name) {
      setDescriptionError("技能名称不能为空。");
      return;
    }
    setSaving(true);
    setDescriptionError(null);
    try {
      const error = await onSaveDetails(selection.item.assetId, name, descriptionDraft);
      if (error) {
        setDescriptionError(error);
        return;
      }
      setEditing(false);
    } finally {
      setSaving(false);
    }
  };

  const open = selection !== null && !hidden;
  const editingAllowed = selection !== null && selection.kind === "mine" && canEdit;

  const footer = [];
  if (selection) {
  if (editingAllowed && !editing) {
    footer.push(
      <Button key="edit" autoInsertSpace={false} onClick={() => setEditing(true)}>编辑技能信息</Button>,
    );
  }
  if (editing) {
    footer.push(
      <Button
        key="cancel-edit"
        autoInsertSpace={false}
        disabled={saving}
        onClick={() => {
          setEditing(false);
          setNameDraft(selection.item.name);
          setDescriptionDraft(selection.item.description ?? "");
          setDescriptionError(null);
        }}
      >取消编辑</Button>,
      <Button key="save" type="primary" autoInsertSpace={false} disabled={saving} onClick={() => void saveSkillDetails()}>
        {saving ? "保存中" : "保存"}
      </Button>,
    );
  }
  if (!editing) {
    footer.push(<Button key="close" autoInsertSpace={false} onClick={onClose}>关闭</Button>);
  }
  if (selection.kind === "market" && selection.item.state === "installable") {
    footer.push(
      <Button key="install" type="primary" autoInsertSpace={false} onClick={() => onInstall(selection)}>安装到{installTargetLabel}</Button>,
    );
  }
  if (selection.kind === "market" && selection.item.state === "current") {
    footer.push(
      <Button key="uninstall" danger type="primary" autoInsertSpace={false} onClick={() => onRemovalRequest({ kind: "market", item: selection.item })}>卸载</Button>,
    );
  }
  if (selection.kind === "mine" && selection.item.source === "personal" && "desired" in selection.item && (selection.item as PersonalSkillViewItem).status !== "archived") {
    const desired = (selection.item as PersonalSkillViewItem).desired;
    footer.push(
      <Button
        key="personal-toggle"
        type={desired ? "default" : "primary"}
        autoInsertSpace={false}
        onClick={() => {
          if (desired) onRemovalRequest({ kind: "personal_uninstall", item: selection.item as PersonalSkillViewItem });
          else onInstall(selection);
        }}
      >{desired ? "卸载个人技能" : "安装到默认 Profile"}</Button>,
    );
  }
  if (selection.kind === "mine" && selection.item.source === "application" && selection.item.catalogAssetId == null && !editing) {
    footer.push(
      <Button key="application-delete" danger type="primary" autoInsertSpace={false} onClick={() => onRemovalRequest({ kind: "application_delete", item: selection.item })}>删除</Button>,
    );
  }
  }

  return (
    <Modal
      rootClassName={theme === "dark" ? "skillDialogDark" : ""}
      className="skillDetailDialog"
      open={open}
      width={520}
      title="技能详情"
      centered
      onCancel={onClose}
      footer={footer}
    >
      {selection !== null && <div className="skillDetailBody">
        <div className="skillDetailHeader">
          <h3>{selection.item.name}</h3>
          <div className="skillDetailHeaderTags">
            <Tag>{versionLabel(selection.kind === "market" ? selection.item.currentVersionLabel : selection.item.versionLabel)}</Tag>
            <Tag>{categoryLabel(selection.item.categoryKey, categories)}</Tag>
          </div>
        </div>
        {editingAllowed && editing ? (
          <div className="skillDetailEdit">
            <div className="skillDetailField">
              <span>技能名称</span>
              <Input
                aria-label="编辑技能名称"
                value={nameDraft}
                maxLength={200}
                onChange={(event) => setNameDraft(event.target.value)}
              />
            </div>
            <div className="skillDetailField">
              <span>技能描述</span>
              <Input.TextArea
                aria-label="编辑技能描述"
                value={descriptionDraft}
                maxLength={2000}
                rows={4}
                showCount
                onChange={(event) => setDescriptionDraft(event.target.value)}
              />
            </div>
            <small>仅影响平台显示，不会修改 Skill 包内容或 SKILL.md。清空后保存将恢复技能包默认描述。</small>
            {descriptionError && <Alert className="skillDetailError" type="error" showIcon message={descriptionError} />}
          </div>
        ) : (
          <p>{selection.item.description || "暂无技能说明。"}</p>
        )}
        <div className="skillDetailMeta">
          <div className="skillDetailMetaRow">
            <span className="skillDetailMetaLabel">来源</span>
            <span>{selection.kind === "market" || selection.item.source === "market" ? "专家市场" : selection.item.source === "system" ? "Runtime 系统技能" : selection.item.source === "application" ? "应用 Agent" : selection.item.source === "hermes" ? "Hermes 托管" : "个人上传"}</span>
          </div>
          <div className="skillDetailMetaRow">
            <span className="skillDetailMetaLabel">状态</span>
            <span>{selection.kind === "market"
              ? selection.item.state === "installable" ? "未安装" : selection.item.state === "update_available" ? "自动更新中" : "已安装"
              : selection.item.source === "market" && selection.item.updateStatus === "update_available" ? "自动更新中"
                : selection.item.source === "personal" && "desired" in selection.item && (selection.item as PersonalSkillViewItem).status === "archived" ? "已归档"
                  : selection.item.source === "personal" && "desired" in selection.item && !(selection.item as PersonalSkillViewItem).desired ? "未安装" : "已安装"}</span>
          </div>
        </div>
      </div>}
    </Modal>
  );
}
