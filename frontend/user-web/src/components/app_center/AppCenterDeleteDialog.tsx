import { useState } from "react";
import { Alert, Button, Modal } from "antd";
import { WarningOutlined } from "@ant-design/icons";
import { usePlatformTheme } from "../../theme/platform-theme";

interface Props {
  appName: string;
  onClose: () => void;
  onDelete: () => Promise<void>;
}

// 危险确认弹窗基线（对齐技能/知识库）：centered Modal 420px + 即将删除目标卡 + danger 主按钮。
// 用户决策（2026-10-27）：看板区删除确认从卡片内嵌小确认框改为本弹窗，交互对齐两模块。
export function AppCenterDeleteDialog({ appName, onClose, onDelete }: Props) {
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { theme } = usePlatformTheme();

  const close = () => {
    if (!deleting) onClose();
  };

  const remove = async () => {
    setDeleting(true);
    setError(null);
    try {
      await onDelete();
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除看板失败");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <Modal
      open
      title="删除看板"
      centered
      width={420}
      rootClassName={theme === "dark" ? "appCenterAntDialog appCenterAntDialogDark" : "appCenterAntDialog"}
      className="appCenterDeleteModal"
      closable={!deleting}
      maskClosable={!deleting}
      keyboard={!deleting}
      onCancel={close}
      footer={[
        <Button key="cancel" autoInsertSpace={false} disabled={deleting} onClick={close}>取消</Button>,
        <Button
          key="delete"
          danger
          type="primary"
          autoInsertSpace={false}
          loading={deleting}
          onClick={() => void remove()}
        >{deleting ? "删除中…" : "确认删除"}</Button>,
      ]}
    >
      <div className="appCenterDeleteConfirm">
        <p className="appCenterDeleteWarning">删除后看板将下线，已分享用户的访问链接立即失效，且无法恢复。</p>
        <div className="appCenterDeleteTarget">
          <span className="appCenterDeleteTargetIcon"><WarningOutlined aria-hidden /></span>
          <div><small>即将删除</small><strong>{appName}</strong></div>
        </div>
        {error && <Alert className="appCenterFormError" type="error" showIcon message={error} />}
      </div>
    </Modal>
  );
}
