import { useState } from "react";
import { Alert, Button, Modal } from "antd";
import { WarningOutlined } from "@ant-design/icons";
import { usePlatformTheme } from "../../theme/platform-theme";

interface Props {
  knowledgeBaseName: string;
  onClose: () => void;
  onDelete: () => Promise<void>;
}

// 危险确认弹窗基线：centered Modal 420px + 即将删除目标卡 + danger 主按钮。
export function PersonalKnowledgeDeleteDialog({ knowledgeBaseName, onClose, onDelete }: Props) {
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
      setError(reason instanceof Error ? reason.message : "删除知识库失败");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <Modal
      open
      title="永久删除知识库"
      centered
      width={420}
      rootClassName={theme === "dark" ? "knowledgeDialog knowledgeDialogDark" : "knowledgeDialog"}
      className="pkbDeleteModal"
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
        >{deleting ? "删除中…" : "确认永久删除"}</Button>,
      ]}
    >
      <div className="pkbDeleteConfirm">
        <p className="pkbDeleteWarning">删除后，知识库、文件记录、分享权限和 Runtime 绑定都将被永久移除。</p>
        <div className="pkbDeleteTarget">
          <span className="pkbDeleteTargetIcon"><WarningOutlined aria-hidden /></span>
          <div><small>即将删除</small><strong>{knowledgeBaseName}</strong></div>
        </div>
        {error && <Alert className="pkbFormError" type="error" showIcon message={error} />}
      </div>
    </Modal>
  );
}
