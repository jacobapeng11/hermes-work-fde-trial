import { Alert, Button, Modal } from "antd";
import type { RemovalConfirmation } from "./shared";

export interface SkillRemovalDialogProps {
  removal: RemovalConfirmation | null;
  removing: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}

// antd Modal 的可访问名由 title 提供，角色为 dialog（原 alertdialog，随组件标准变化）。
// 确认文案逐字保留；删除族为 danger 主按钮。
export function SkillRemovalDialog({ removal, removing, error, onCancel, onConfirm }: SkillRemovalDialogProps) {
  if (!removal) return null;
  const title = removal.kind === "personal_delete"
    ? "删除个人技能"
    : removal.kind === "personal_uninstall"
      ? "卸载个人技能"
      : removal.kind === "application_delete"
        ? "删除应用私有技能"
        : "卸载技能";
  const copy = removal.kind === "personal_delete"
    ? "删除后将永久停止该个人技能，并从全部已绑定 Profile 移除；已有 Run 不受影响，后续 Run 不再使用。确定继续吗？"
    : removal.kind === "personal_uninstall"
      ? "卸载只会从默认 Profile 移除，个人技能资产和专家中的绑定仍会保留。确定继续吗？"
      : removal.kind === "application_delete"
        ? "删除后将从当前应用 Agent 移除该私有技能及其上传包；已有历史 Run 不受影响，后续 Run 不再使用。确定继续吗？"
        : `确定要卸载“${removal.item.name}”吗？${removal.kind === "market" ? "卸载后仍可在专家市场重新安装。" : ""}`;
  const confirmText = removal.kind === "personal_delete" || removal.kind === "application_delete" ? "确认删除" : "确认卸载";
  return (
    <Modal
      open
      title={title}
      closable={!removing}
      maskClosable={!removing}
      keyboard={!removing}
      onCancel={onCancel}
      footer={[
        <Button key="cancel" autoInsertSpace={false} disabled={removing} onClick={onCancel}>取消</Button>,
        <Button
          key="confirm"
          type="primary"
          danger
          autoInsertSpace={false}
          loading={removing}
          onClick={() => void onConfirm()}
        >{confirmText}</Button>,
      ]}
    >
      <p className="skillRemovalCopy">{copy}</p>
      {error && <Alert type="error" showIcon message={error} />}
    </Modal>
  );
}
